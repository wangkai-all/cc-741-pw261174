"""预置退化数据场景 + CSV 导入/解析。

所有场景均由固定随机种子的“真值”生成，truth 字段可选择性地在前端叠加，
用于核对参数拟合与外推是否正确（本地自检用途）。
"""

from __future__ import annotations

import csv
import io
from typing import Any

import numpy as np

from .model import DEFAULT_SCOPES

# ---------------------------------------------------------------------------
# CSV 解析
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# CSV 解析
# ---------------------------------------------------------------------------

HEADER_ALIASES = {
    "record_id": {"record_id", "id", "记录id", "编号"},
    "batch": {"batch", "批次", "批次号"},
    "cell": {"cell", "cell_id", "sample", "单体", "单体id", "样品编号"},
    "t": {"t", "time", "weeks", "cycles", "时间", "检测时间", "周数", "循环"},
    "y": {"y", "soh", "value", "reading", "容量保持率", "读数", "响应", "退化指标", "保持率"},
    "instrument": {"instrument", "inst", "detector", "检测仪", "仪器", "设备"},
    "censored": {"censored", "censor", "status", "删失", "截断", "删失类型"},
    "limit": {"limit", "censor_limit", "range_limit", "量程", "量程界限", "界限"},
    "repeat": {"repeat", "repeat_no", "replicate", "重复", "重复编号"},
    "provenance": {"provenance", "source", "来源"},
}

TEMPLATE_CSV = (
    "record_id,batch,cell,t,y,instrument,censored,limit,repeat\n"
    "EX-001,BAT-A,A01,0,99.4,INST-A,none,,1\n"
    "EX-002,BAT-A,A01,6,97.9,INST-A,none,,1\n"
    "EX-003,BAT-A,A01,12,\">=100\",INST-A,right,100,1\n"
    "# y 也可直接写 >100 / >=100（超上限右删失）或 <60 / <=60（低于下限左删失）\n"
)


def _norm_header(h: str) -> str:
    return (h or "").strip().lstrip("﻿").lower()


def _map_headers(fieldnames: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    norm_to_canon = {}
    for canon, aliases in HEADER_ALIASES.items():
        for a in aliases:
            norm_to_canon[_norm_header(a)] = canon
    for h in fieldnames:
        nh = _norm_header(h)
        if nh in norm_to_canon:
            mapping[h] = norm_to_canon[nh]
    return mapping


def parse_reading(raw: str):
    """把读数解析成 (y, cens, limit)。支持 >100 / >=100 / <60 语法。"""
    if raw is None:
        return None, 0, None
    s = str(raw).strip()
    if s == "":
        return None, 0, None
    if s.startswith((">=", "≥")):
        num = float(s.lstrip(">=≥ "))
        return num, 1, num
    if s.startswith(">"):
        num = float(s[1:].strip())
        return num, 1, num
    if s.startswith(("<=", "≤")):
        num = float(s.lstrip("<=≤ "))
        return num, -1, num
    if s.startswith("<"):
        num = float(s[1:].strip())
        return num, -1, num
    return float(s), 0, None


def parse_csv(text: str) -> dict[str, Any]:
    """解析 CSV 文本。返回 {rows, errors, warnings}。"""
    errors: list[str] = []
    warnings: list[str] = []
    rows: list[dict[str, Any]] = []
    # 去掉以 # 开头的注释行
    clean_lines = [ln for ln in text.splitlines() if not ln.strip().startswith("#")]
    reader = csv.DictReader(io.StringIO("\n".join(clean_lines)))
    if reader.fieldnames is None:
        return {"rows": [], "errors": ["CSV 为空或缺少表头"], "warnings": warnings}
    mapping = _map_headers(reader.fieldnames)
    if "cell" not in mapping.values() or "t" not in mapping.values() or "y" not in mapping.values():
        errors.append("必须包含 单体(cell)、时间(t)、读数(y) 三列（支持中文表头）")
        return {"rows": [], "errors": errors, "warnings": warnings}

    cens_word = {
        "none": 0, "exact": 0, "0": 0, "": 0, "no": 0,
        "right": 1, "upper": 1, "1": 1, ">": 1, "右删失": 1, "超量程": 1, "上删失": 1,
        "left": -1, "lower": -1, "-1": -1, "<": -1, "左删失": -1, "下删失": -1,
    }
    seen_ids: set[str] = set()
    for ln, raw in enumerate(reader, start=2):
        if raw is None:
            continue
        if all((v is None or str(v).strip() == "") for v in raw.values()):
            continue

        def get(canon: str):
            for h, c in mapping.items():
                if c == canon:
                    return raw.get(h)
            return None

        try:
            cell = str(get("cell") or "").strip()
            if not cell:
                errors.append(f"第 {ln} 行缺少单体编号")
                continue
            t = float(str(get("t")).strip())
            y_raw = get("y")
            y, cens_inline, lim_inline = parse_reading(y_raw)
            if y is None:
                errors.append(f"第 {ln} 行读数为空")
                continue
            cens = cens_inline
            limit = lim_inline
            cword = get("censored")
            if cword is not None and str(cword).strip() != "":
                key = str(cword).strip().lower()
                if key in cens_word:
                    cens = cens_word[key]
                else:
                    warnings.append(f"第 {ln} 行未知删失标记 {cword!r}，按精确读数处理")
            lim_raw = get("limit")
            if lim_raw not in (None, ""):
                limit = float(str(lim_raw).strip())
            if cens != 0 and limit is None:
                # 没有显式界限时，用读数本身作为量程界限（截断记录的惯例）
                limit = y
            if cens == 0:
                limit = None
            batch = str(get("batch") or "IMP").strip() or "IMP"
            inst = str(get("instrument") or "INST-A").strip() or "INST-A"
            rep_raw = get("repeat")
            rep = int(float(str(rep_raw).strip())) if rep_raw not in (None, "") else 1
            prov = str(get("provenance") or "imported").strip() or "imported"
            rid = str(get("record_id") or "").strip()
            if not rid:
                rid = f"IMP-{ln:04d}"
            if rid in seen_ids:
                rid = f"{rid}-{ln}"
            seen_ids.add(rid)
            rows.append({
                "record_id": rid, "batch": batch, "cell": cell, "t": t,
                "y": (limit if cens != 0 else y),
                "cens": cens, "limit": limit,
                "instrument": inst, "repeat": rep,
                "provenance": prov,
            })
        except ValueError as e:
            errors.append(f"第 {ln} 行数值解析失败：{e}")
    if not rows and not errors:
        errors.append("未解析到任何数据行")
    if rows:
        n_cens = sum(1 for r in rows if r["cens"] != 0)
        if n_cens:
            warnings.append(f"包含 {n_cens} 条超量程删失/截断读数，将使用删失似然而非当作精确值")
        insts = {r["instrument"] for r in rows}
        if len(insts) > 1:
            warnings.append(f"检测到多台检测仪 {sorted(insts)}，建议开启仪器偏移项")
    return {"rows": rows, "errors": errors, "warnings": warnings}


def rows_to_csv(rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    cols = ["record_id", "batch", "cell", "t", "y", "censored", "limit",
            "instrument", "repeat", "provenance"]
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for r in rows:
        cens = r.get("cens", 0)
        y = r.get("y")
        if cens == 1:
            y_out = f">={r.get('limit', y)}"
            cword, lim = "right", r.get("limit")
        elif cens == -1:
            y_out = f"<={r.get('limit', y)}"
            cword, lim = "left", r.get("limit")
        else:
            y_out = y
            cword, lim = "none", ""
        w.writerow({
            "record_id": r.get("record_id", ""),
            "batch": r.get("batch", ""),
            "cell": r.get("cell", ""),
            "t": r.get("t", ""),
            "y": y_out,
            "censored": cword,
            "limit": lim if lim is not None else "",
            "instrument": r.get("instrument", ""),
            "repeat": r.get("repeat", 1),
            "provenance": r.get("provenance", ""),
        })
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 合成数据生成
# ---------------------------------------------------------------------------

def _censor(y_true: float, hi: float | None, lo: float | None, rng, sigma: float):
    """加噪并按量程序截断。返回 (recorded_y, cens, limit)。"""
    y_obs = y_true + rng.normal(0, sigma)
    if hi is not None and y_obs > hi:
        return hi, 1, hi
    if lo is not None and y_obs < lo:
        return lo, -1, lo
    return y_obs, 0, None


def _build_scenario(sid, name, desc, seed, cell_specs, schedules, windows,
                    eol=80.0, range_hi=100.0, range_lo=0.0,
                    budget_samples=3, default_cell=None,
                    instrument_rule=None, sigma=0.7, extra_rows=None,
                    highlights=None):
    """cell_specs: list of dict(cell,batch,a,b1,b2,tau)。"""
    rng = np.random.default_rng(seed)
    rows: list[dict[str, Any]] = []
    truth: dict[str, Any] = {"cells": {}, "sigma": sigma,
                             "range_hi": range_hi, "range_lo": range_lo}
    counter = 0
    for spec in cell_specs:
        cid = spec["cell"]
        truth["cells"][cid] = {k: spec[k] for k in ("a", "b1", "b2", "tau")}
        times = schedules[spec["batch"]]
        for t in times:
            mu = (spec["a"] - spec["b1"] * t
                  - (spec["b2"] - spec["b1"]) * max(t - spec["tau"], 0.0))
            inst = instrument_rule(t, spec) if instrument_rule else "INST-A"
            offset = INSTRUMENT_OFFSETS.get(inst, 0.0)
            y_rec, cens, lim = _censor(mu + offset, range_hi, range_lo, rng, sigma)
            counter += 1
            rows.append({
                "record_id": f"{sid.upper()}-{counter:03d}",
                "batch": spec["batch"], "cell": cid, "t": float(t),
                "y": round(float(y_rec), 3), "cens": cens,
                "limit": lim, "instrument": inst, "repeat": 1,
                "provenance": "observed",
            })
    if extra_rows:
        rows.extend(extra_rows)
    return {
        "id": sid, "name": name, "description": desc,
        "highlights": highlights or [],
        "time_unit": "周", "eol": eol,
        "range_hi": range_hi, "range_lo": range_lo,
        "budget_samples": budget_samples,
        "default_cell": default_cell or cell_specs[0]["cell"],
        "windows": windows,
        "rows": rows, "truth": truth,
        "default_scopes": dict(DEFAULT_SCOPES),
    }


INSTRUMENT_OFFSETS = {"INST-A": 0.0, "INST-B": -2.4}


def _std_specs():
    rng = np.random.default_rng(101)
    batch_true = {
        "BAT-A": dict(b1=0.32, b2=1.50, tau=24.0),
        "BAT-B": dict(b1=0.28, b2=1.05, tau=30.0),
        "BAT-C": dict(b1=0.42, b2=2.10, tau=20.0),
    }
    specs = []
    for b, p in batch_true.items():
        for j in range(1, 4):
            specs.append({
                "cell": f"{b[-1]}0{j}", "batch": b,
                "a": round(99.2 + rng.normal(0, 0.35), 2),
                **p,
            })
    return specs


def _knee_specs():
    rng = np.random.default_rng(202)
    bt = {
        "K1": dict(b1=0.35, b2=1.30, tau=27.0),
        "K2": dict(b1=0.30, b2=1.00, tau=31.0),
    }
    specs = []
    for b, p in bt.items():
        for j in range(1, 4):
            specs.append({
                "cell": f"{b}-{j}", "batch": b,
                "a": round(99.3 + rng.normal(0, 0.3), 2), **p})
    return specs


def _inst_specs():
    rng = np.random.default_rng(303)
    specs = []
    for j in range(1, 5):
        specs.append({
            "cell": f"I0{j}", "batch": "BAT-I",
            "a": round(99.3 + rng.normal(0, 0.3), 2),
            "b1": 0.32, "b2": 1.70, "tau": 29.0})
    return specs


def _rep_specs():
    rng = np.random.default_rng(404)
    bt = {
        "R1": dict(b1=0.40, b2=1.80, tau=22.0),
        "R2": dict(b1=0.30, b2=1.20, tau=28.0),
    }
    specs = []
    for b, p in bt.items():
        for j in range(1, 4):
            specs.append({
                "cell": f"{b}-{j}", "batch": b,
                "a": round(99.4 + rng.normal(0, 0.25), 2), **p})
    return specs


def _replicate_rows(sid, specs, times, n_rep, seed, sigma, range_hi=100.2):
    rng = np.random.default_rng(seed)
    offsets = {"INST-A": 0.0}
    rows = []
    k = 0
    for spec in specs:
        for t in times:
            mu = (spec["a"] - spec["b1"] * t
                  - (spec["b2"] - spec["b1"]) * max(t - spec["tau"], 0.0))
            for rep in range(1, n_rep + 1):
                y_rec, cens, lim = _censor(mu + offsets["INST-A"],
                                           range_hi, 0.0, rng, sigma)
                k += 1
                rows.append({
                    "record_id": f"{sid.upper()}-R{k:03d}",
                    "batch": spec["batch"], "cell": spec["cell"], "t": float(t),
                    "y": round(float(y_rec), 3), "cens": cens, "limit": lim,
                    "instrument": "INST-A", "repeat": rep,
                    "provenance": "observed"})
    return rows


def load_scenarios() -> dict[str, dict[str, Any]]:
    scenarios: dict[str, dict[str, Any]] = {}

    # 1) 标准：三批次、检测时间安排不同、含超量程删失
    std_sched = {
        "BAT-A": [0, 6, 12, 18, 24, 30, 36, 40],
        "BAT-B": [0, 8, 16, 24, 32, 40],
        "BAT-C": [0, 4, 10, 16, 22, 28, 34, 40],
    }
    scenarios["standard"] = _build_scenario(
        "std", "标准三批次（检测日程不同）",
        "三个批次、每批 3 个单体；各批次巡检时间点不同。早期曲线接近，"
        "但批次真值寿命差异明显（C 短、B 长）。初始读数含少量超过检测仪上限的删失记录。",
        1, _std_specs(), std_sched,
        windows=[
            {"id": "W1", "label": "第 42–46 周窗口", "lo": 42, "hi": 46},
            {"id": "W2", "label": "第 48–52 周窗口", "lo": 48, "hi": 52},
            {"id": "W3", "label": "第 54–60 周窗口", "lo": 54, "hi": 60},
        ],
        range_hi=100.0, budget_samples=3, default_cell="B01",
        sigma=0.7,
        highlights=["不同批次检测时间不同", "少量超量程右删失读数", "曲线接近但 L80 不同"])

    # 2) 转折点未观测
    knee_sched = {"K1": [0, 5, 10, 15, 20], "K2": [0, 6, 12, 18, 22]}
    scenarios["knee_unobserved"] = _build_scenario(
        "knee", "转折点未观测",
        "所有数据都停留在真实转折点之前：只能看到阶段一近似直线，"
        "τ、b2 与长寿命外推不可分辨。轮廓似然应给出开放/平坦区间，"
        "追加检测建议应偏好能观测到阶段二的晚时段。",
        2, _knee_specs(), knee_sched,
        windows=[
            {"id": "W1", "label": "第 22–26 周窗口（可能刚到转折点）", "lo": 22, "hi": 26},
            {"id": "W2", "label": "第 28–32 周窗口（转折点附近）", "lo": 28, "hi": 32},
            {"id": "W3", "label": "第 34–40 周窗口（阶段二/EOL 附近）", "lo": 34, "hi": 40},
            {"id": "W4", "label": "第 42–50 周窗口（明确阶段二）", "lo": 42, "hi": 50},
        ],
        range_hi=100.4, budget_samples=3, default_cell="K1-1",
        sigma=0.6,
        highlights=["τ 轮廓似然平坦/开放", "寿命为强外推", "建议偏向晚期窗口"])

    # 3) 仪器更换（B 仪器读数系统偏低 2.4；15 周有双机比对）
    def inst_rule(t, spec):
        return "INST-A" if t <= 14 else "INST-B"

    inst_scn = _build_scenario(
        "inst", "检测仪更换（含系统偏移）",
        "第 15 周前后由 INST-A 换成 INST-B，B 读数系统偏低约 2.4 个百分点，"
        "真实转折点在第 29 周附近，第 15 周保留两台仪器的比对重复测量。"
        "开启仪器偏移项可正确恢复 δ_B 与转折点；关闭偏移项时，整台仪器的"
        "共享偏移会被错误分摊：一阶段速率被抬高、σ 与不确定度膨胀，残差按"
        "仪器分组系统偏离，并可能扭曲转折点——这正是“不能把整批/整台仪器的"
        "偏移当成单体退化”的核对用例。",
        3, _inst_specs(), {"BAT-I": [0, 6, 12, 18, 24, 30, 36, 42]},
        windows=[
            {"id": "W1", "label": "第 44–48 周窗口", "lo": 44, "hi": 48},
            {"id": "W2", "label": "第 50–54 周窗口", "lo": 50, "hi": 54},
            {"id": "W3", "label": "第 56–62 周窗口", "lo": 56, "hi": 62},
        ],
        range_hi=100.2, budget_samples=3, default_cell="I01",
        instrument_rule=inst_rule, sigma=0.6,
        highlights=["仪器偏移 δ_B≈−2.4", "双机比对读数", "关闭偏移项可复现错误拟合"])
    # 追加 t=15 的双机比对行
    extra = []
    k0 = len(inst_scn["rows"])
    rng = np.random.default_rng(3031)
    for j, spec in enumerate(_inst_specs(), start=1):
        mu_cal = spec["a"] - spec["b1"] * 15 - (spec["b2"] - spec["b1"]) * max(15 - spec["tau"], 0)
        for inst in ("INST-A", "INST-B"):
            off = INSTRUMENT_OFFSETS[inst]
            y_rec, cens, lim = _censor(mu_cal + off, 100.2, 0.0, rng, 0.5)
            k0 += 1
            extra.append({
                "record_id": f"INST-CAL-{j}-{inst[-1]}",
                "batch": "BAT-I", "cell": spec["cell"], "t": 15.0,
                "y": round(float(y_rec), 3), "cens": cens, "limit": lim,
                "instrument": inst, "repeat": 1, "provenance": "observed"})
    inst_scn["rows"].extend(extra)
    inst_scn["truth"]["instruments"] = dict(INSTRUMENT_OFFSETS)
    scenarios["instrument_swap"] = inst_scn

    # 4) 重复读数
    rep_specs = _rep_specs()
    rep_rows = _replicate_rows("rep", rep_specs, [0, 10, 20, 30, 40],
                               n_rep=3, seed=4041, sigma=0.85)
    scenarios["replicates"] = {
        "id": "replicates",
        "name": "重复读数（同点 3 次）",
        "description": "每个检测时间有 3 次独立重复读数，可直接观察测量噪声、"
                       "核对重复测量对 σ 与参数置信区间的收窄作用。",
        "highlights": ["同时点重复行", "噪声可估", "置信区间更窄"],
        "time_unit": "周", "eol": 80.0, "range_hi": 100.2, "range_lo": 0.0,
        "budget_samples": 3, "default_cell": "R1-1",
        "windows": [
            {"id": "W1", "label": "第 42–46 周窗口", "lo": 42, "hi": 46},
            {"id": "W2", "label": "第 48–52 周窗口", "lo": 48, "hi": 52},
            {"id": "W3", "label": "第 54–60 周窗口", "lo": 54, "hi": 60},
        ],
        "rows": rep_rows,
        "truth": {"cells": {s["cell"]: {k: s[k] for k in ("a", "b1", "b2", "tau")}
                            for s in rep_specs}, "sigma": 0.85},
        "default_scopes": dict(DEFAULT_SCOPES),
    }

    # 5) 数据不足 / 拟合失败核对
    fail_rows = [
        {"record_id": "FAIL-001", "batch": "BAT-X", "cell": "X1", "t": 0.0,
         "y": 99.6, "cens": 0, "limit": None, "instrument": "INST-A",
         "repeat": 1, "provenance": "observed"},
        {"record_id": "FAIL-002", "batch": "BAT-X", "cell": "X1", "t": 10.0,
         "y": 96.1, "cens": 0, "limit": None, "instrument": "INST-A",
         "repeat": 1, "provenance": "observed"},
        {"record_id": "FAIL-003", "batch": "BAT-X", "cell": "X1", "t": 20.0,
         "y": 92.8, "cens": 0, "limit": None, "instrument": "INST-A",
         "repeat": 1, "provenance": "observed"},
    ]
    scenarios["fit_failure"] = {
        "id": "fit_failure",
        "name": "数据不足（拟合失败核对）",
        "description": "只有 3 个检测点，却想同时估计 a、b1、b2、τ、σ。"
                       "应用必须明确报告自由度不足/拟合不可识别，而不是给出看似精确的寿命。",
        "highlights": ["硬失败提示", "禁止输出确定寿命", "建议补充晚期检测"],
        "time_unit": "周", "eol": 80.0, "range_hi": 100.0, "range_lo": 0.0,
        "budget_samples": 2, "default_cell": "X1",
        "windows": [
            {"id": "W1", "label": "第 28–34 周窗口", "lo": 28, "hi": 34},
            {"id": "W2", "label": "第 38–46 周窗口", "lo": 38, "hi": 46},
        ],
        "rows": fail_rows, "truth": {"cells": {}, "sigma": None},
        "default_scopes": {"a": "global", "b1": "global", "b2": "global", "tau": "global"},
    }
    return scenarios


SCENARIOS = load_scenarios()
