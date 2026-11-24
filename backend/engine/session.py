"""本地应用会话状态：数据、共享假设、拟合缓存与建议台账。

台账中的建议记录一旦创建，其排序与预测数字即冻结；接受建议后把合成行追加进
数据并复算轮廓似然，将“预测信息增益 vs 实际信息增益”写入 outcome，
原始预测不被覆盖，保证可追溯。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np

from .datasets import SCENARIOS, parse_csv
from .fit import fit_model, profile_likelihood, extrapolation_assessment
from .design import rank_windows, evaluate_choice, generate_synthetic_round

STATE_PATH = Path(__file__).resolve().parents[2] / "state" / "session.json"


def jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, float) and not np.isfinite(obj):
        return None
    return obj


class Session:
    def __init__(self):
        self.scenario_id: str | None = None
        self.name: str = "未命名会话"
        self.rows: list[dict[str, Any]] = []
        self.meta: dict[str, Any] = {}
        self.scopes: dict[str, str] = {}
        self.model_options: dict[str, Any] = {
            "instrument_offset": True, "reference_instrument": "INST-A"}
        self.selected_cell: str | None = None
        self.used_samples: int = 0
        self.recommendations: list[dict[str, Any]] = []
        self.rec_counter: int = 0
        self.fit: dict[str, Any] | None = None
        self.import_errors: list[str] = []
        self.import_warnings: list[str] = []

    # ------------------------------------------------------------------
    def load_scenario(self, sid: str) -> dict:
        if sid not in SCENARIOS:
            raise KeyError(f"未知场景 {sid}")
        scn = SCENARIOS[sid]
        self.scenario_id = sid
        self.name = scn["name"]
        # 深拷贝行，避免污染注册表
        self.rows = [dict(r) for r in scn["rows"]]
        self.meta = {k: scn[k] for k in (
            "description", "highlights", "time_unit", "eol", "range_hi",
            "range_lo", "budget_samples", "windows", "truth", "default_cell")
            if k in scn}
        self.scopes = dict(scn.get("default_scopes") or {
            "a": "cell", "b1": "batch", "b2": "batch", "tau": "batch"})
        insts = sorted({r["instrument"] for r in self.rows})
        self.model_options = {"instrument_offset": len(insts) > 1,
                              "reference_instrument": insts[0] if insts else "INST-A"}
        self.selected_cell = scn.get("default_cell") or (
            self.rows[0]["cell"] if self.rows else None)
        self.used_samples = 0
        self.recommendations = []
        self.rec_counter = 0
        self.import_errors = []
        self.import_warnings = []
        self.refit()
        self.save()
        return self.state()

    def import_csv_text(self, text: str, filename: str = "导入数据") -> dict:
        parsed = parse_csv(text)
        self.import_errors = parsed["errors"]
        self.import_warnings = parsed["warnings"]
        if parsed["errors"]:
            # 保留旧会话，仅返回错误
            return self.state()
        self.scenario_id = None
        self.name = filename
        self.rows = parsed["rows"]
        batches = sorted({r["batch"] for r in self.rows})
        self.meta = {
            "description": "从 CSV 导入的数据。", "highlights": [],
            "time_unit": "时间单位", "eol": 80.0,
            "range_hi": 100.0, "range_lo": 0.0,
            "budget_samples": 3,
            "windows": [
                {"id": "W1", "label": "近期窗口 t+6", "lo": self._tmax() + 4,
                 "hi": self._tmax() + 10},
                {"id": "W2", "label": "中期窗口 t+14", "lo": self._tmax() + 12,
                 "hi": self._tmax() + 18},
                {"id": "W3", "label": "晚期窗口 t+24", "lo": self._tmax() + 22,
                 "hi": self._tmax() + 30},
            ],
            "truth": None, "default_cell": self.rows[0]["cell"]}
        self.scopes = {"a": "cell", "b1": "batch", "b2": "batch", "tau": "batch"}
        insts = sorted({r["instrument"] for r in self.rows})
        self.model_options = {"instrument_offset": len(insts) > 1,
                              "reference_instrument": insts[0] if insts else "INST-A"}
        self.selected_cell = self.rows[0]["cell"]
        self.used_samples = sum(1 for r in self.rows
                                if r.get("provenance") == "synthetic")
        # 导入数据里的 synthetic 行不计入本轮预算；预算只统计本会话接受次数
        self.used_samples = 0
        self.recommendations = []
        self.rec_counter = 0
        self.refit()
        self.save()
        return self.state()

    def _tmax(self) -> float:
        return max((float(r["t"]) for r in self.rows), default=0.0)

    # ------------------------------------------------------------------
    def refit(self) -> dict[str, Any]:
        if not self.rows:
            self.fit = None
            return {}
        self.fit = fit_model(self.rows, self.scopes, self.model_options)
        return self.fit

    def update_settings(self, scopes: dict | None = None,
                        model_options: dict | None = None,
                        selected_cell: str | None = None,
                        eol: float | None = None) -> dict:
        if scopes:
            for k, v in scopes.items():
                if k in ("a", "b1", "b2", "tau") and v in ("cell", "batch", "global"):
                    self.scopes[k] = v
        if model_options:
            self.model_options.update(model_options)
        if selected_cell is not None:
            self.selected_cell = selected_cell
        if eol is not None:
            self.meta["eol"] = float(eol)
        # 共享假设变化后，未接受的建议基础已失效 -> 标记 superseded
        if scopes or model_options:
            for rec in self.recommendations:
                if rec["status"] == "pending":
                    rec["status"] = "superseded"
        self.refit()
        self.save()
        return self.state()

    # ------------------------------------------------------------------
    def get_profile(self, target: dict, n_grid: int = 21) -> dict:
        if not self.fit or not self.fit.get("ok"):
            return {"available": False, "reason": "拟合尚未成功，无法计算轮廓似然。"}
        prof = profile_likelihood(self.rows, self.scopes, self.model_options,
                                  self.fit, target, n_grid=n_grid)
        # 若是寿命轮廓，附加外推评估
        if target.get("kind") == "life":
            prof["extrapolation"] = extrapolation_assessment(
                self.rows, self.fit, target["cell"],
                float(target.get("eol", self.meta.get("eol", 80.0))), prof)
        return jsonable(prof)

    # ------------------------------------------------------------------
    def create_recommendation(self, params: dict | None = None) -> dict:
        params = params or {}
        if not self.fit or not self.fit.get("ok"):
            return {"error": "FIT_UNAVAILABLE",
                    "message": "当前数据拟合失败或不可识别，暂不能给出量化建议；"
                               "请先调整共享假设或导入更多检测点。"}
        cell = params.get("cell") or self.selected_cell
        if cell not in self.fit["layout_meta"]["cells"]:
            return {"error": "BAD_CELL", "message": f"未知单体 {cell}"}
        eol = float(params.get("eol", self.meta.get("eol", 80.0)))
        n = int(params.get("n", 1))
        instrument = params.get("instrument") or (
            self.fit["layout_meta"].get("reference_instrument"))
        windows = params.get("windows") or self.meta.get("windows", [])
        rlo = self.meta.get("range_lo")
        rhi = self.meta.get("range_hi")

        ranking = rank_windows(
            self.rows, self.scopes, self.model_options, self.fit,
            cell, eol, windows, rlo, rhi, n=n, instrument=instrument)

        # 精确寿命轮廓（冻结接受前证据）
        life_target = {"kind": "life", "cell": cell, "eol": eol}
        prof_before = profile_likelihood(
            self.rows, self.scopes, self.model_options, self.fit,
            life_target, n_grid=19)
        extra = extrapolation_assessment(
            self.rows, self.fit, cell, eol, prof_before)

        self.rec_counter += 1
        rec_id = f"REC-{self.rec_counter:03d}"
        budget = int(self.meta.get("budget_samples", 3))
        w_prof_before = _ci_width(prof_before.get("ci_low"),
                                  prof_before.get("ci_high"))
        top_pred = ranking.get("top_prediction") or {}
        calibrated = _calibrated_width(
            w_prof_before, ranking.get("width_before_fisher"),
            top_pred.get("width_after_fisher"))
        rec = {
            "id": rec_id,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "pending",
            "params": {"cell": cell, "eol": eol, "n": n,
                       "instrument": instrument},
            "budget": budget,
            "used_before": self.used_samples,
            "ranking": jsonable(ranking),
            "profile_before": jsonable(_compact_profile(prof_before)),
            "extrapolation_before": jsonable(extra),
            "width_profile_before": w_prof_before,
            "predicted_top_width_after": calibrated,
            "theta_snapshot": list(self.fit["theta"]),
            "scopes_snapshot": dict(self.scopes),
            "options_snapshot": dict(self.model_options),
            "chosen": None,
            "prediction_accepted_choice": None,
            "synthetic_rows": [],
            "outcome": None,
            "latest_check": None,
        }
        self.recommendations.insert(0, rec)
        self.save()
        return jsonable({"ok": True, "recommendation": _public_rec(rec)})

    def accept_recommendation(self, rec_id: str,
                              override: dict | None = None) -> dict:
        rec = self._find_rec(rec_id)
        if rec is None:
            return {"error": "NOT_FOUND", "message": f"找不到建议 {rec_id}"}
        if rec["status"] == "accepted":
            return {"error": "ALREADY_ACCEPTED",
                    "message": f"建议 {rec_id} 已被接受，原始预测与结果已冻结。"}
        if rec["status"] == "superseded":
            return {"error": "SUPERSEDED",
                    "message": "共享假设已更改，该建议基础失效；请基于当前拟合重新生成建议。"}

        override = override or {}
        p = rec["params"]
        cell = override.get("cell", p["cell"])
        eol = float(override.get("eol", p["eol"]))
        n = int(override.get("n", p["n"]))
        instrument = override.get("instrument", p["instrument"])
        budget = int(rec.get("budget", self.meta.get("budget_samples", 3)))
        if n < 1:
            return {"error": "BAD_N", "message": "每轮至少 1 个试样/读数。"}
        if self.used_samples + n > budget:
            return {"error": "BUDGET_EXCEEDED",
                    "message": (f"破坏样本预算不足：剩余 {budget - self.used_samples} 个，"
                                f"本次需要 {n} 个。"),
                    "remaining": budget - self.used_samples}

        # 选择检测时刻：显式 t > window_id 最佳点 > 排名第一
        t = override.get("t")
        window_id = override.get("window_id")
        chosen_window = None
        if t is None:
            cand_list = rec["ranking"]["candidates"]
            if window_id:
                chosen_window = next((c for c in cand_list
                                      if c["window_id"] == window_id), None)
            else:
                chosen_window = cand_list[0] if cand_list else None
            if chosen_window is None:
                return {"error": "NO_WINDOW", "message": "没有可用时间窗口。"}
            t = float(chosen_window["best_t"])
            window_id = chosen_window["window_id"]
        t = float(t)

        # 用建议创建时冻结的模型状态评估“被接受选择”的预测增益
        pred_choice = evaluate_choice(
            self.rows, rec["scopes_snapshot"], rec["options_snapshot"],
            {"theta": rec["theta_snapshot"]}, cell, eol, t, n, instrument,
            self.meta.get("range_lo"), self.meta.get("range_hi"))
        w_prof_before = rec.get("width_profile_before")
        pred_choice["width_after_calibrated"] = _calibrated_width(
            w_prof_before, pred_choice.get("width_before_fisher"),
            pred_choice.get("width_after_fisher"))

        seed = 900000 + self.rec_counter * 97 + int(round(t * 13)) + n
        new_rows = generate_synthetic_round(
            self.rows, rec["scopes_snapshot"], rec["options_snapshot"],
            {"theta": rec["theta_snapshot"]}, cell, t, n, instrument,
            self.meta.get("range_lo"), self.meta.get("range_hi"), seed)

        self.rows.extend(new_rows)
        self.used_samples += n
        rec["status"] = "accepted"
        rec["accepted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        rec["chosen"] = {"t": t, "n": n, "instrument": instrument,
                         "window_id": window_id,
                         "window_label": (chosen_window or {}).get("label"),
                         "adjusted": bool(override.get("t") is not None
                                          or override.get("n") is not None
                                          or override.get("instrument") is not None)}
        rec["prediction_accepted_choice"] = jsonable(pred_choice)
        rec["synthetic_rows"] = jsonable(new_rows)

        # 新数据到达 -> 重新拟合 + 新轮廓
        self.refit()
        prof_after = profile_likelihood(
            self.rows, self.scopes, self.model_options, self.fit,
            {"kind": "life", "cell": cell, "eol": eol}, n_grid=19)
        extra_after = extrapolation_assessment(
            self.rows, self.fit, cell, eol, prof_after)

        before = rec.get("profile_before") or {}
        w_before = _ci_width(before.get("ci_low"), before.get("ci_high"))
        w_after = _ci_width(prof_after.get("ci_low"), prof_after.get("ci_high"))
        pred_w_fisher = pred_choice.get("width_after_fisher")
        pred_w = pred_choice.get("width_after_calibrated")
        if w_before and w_after is not None:
            actual_reduction = max(0.0, (w_before - w_after) / w_before)
        else:
            actual_reduction = None
        if pred_w and w_after is not None and pred_w > 0:
            width_ratio = w_after / pred_w
        else:
            width_ratio = None
        rec["outcome"] = {
            "evaluated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "profile_after": jsonable(_compact_profile(prof_after)),
            "extrapolation_after": jsonable(extra_after),
            "width_profile_before": w_before,
            "width_profile_after": w_after,
            "actual_reduction_frac": actual_reduction,
            "predicted_width_after": pred_w,
            "predicted_width_after_fisher": pred_w_fisher,
            "predicted_reduction_frac": pred_choice.get("reduction_frac"),
            "actual_over_predicted_width_ratio": width_ratio,
            "prediction_quality": _quality_label(width_ratio, actual_reduction,
                                                 pred_choice.get("reduction_frac")),
            "fit_ok_after": bool(self.fit and self.fit.get("ok")),
        }
        self.save()
        return jsonable({"ok": True, "state": self.state(),
                         "rec": _public_rec(rec)})

    def reevaluate(self, rec_id: str) -> dict:
        """用当前全部数据重新核对某条已接受建议的实际信息增益（不覆盖原 outcome）。"""
        rec = self._find_rec(rec_id)
        if rec is None or rec["status"] != "accepted":
            return {"error": "NOT_ACCEPTED", "message": "只能复核已接受的建议。"}
        ch = rec["chosen"]
        target = {"kind": "life", "cell": rec["params"]["cell"],
                  "eol": rec["params"]["eol"]}
        prof = profile_likelihood(self.rows, self.scopes, self.model_options,
                                  self.fit, target, n_grid=19)
        w_now = _ci_width(prof.get("ci_low"), prof.get("ci_high"))
        before = rec.get("profile_before") or {}
        w_before = _ci_width(before.get("ci_low"), before.get("ci_high"))
        rec["latest_check"] = {
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "profile": jsonable(_compact_profile(prof)),
            "width_now": w_now,
            "additional_reduction_from_original": (
                max(0.0, (w_before - w_now) / w_before)
                if w_before and w_now is not None else None),
        }
        self.save()
        return jsonable({"ok": True, "latest_check": rec["latest_check"]})

    # ------------------------------------------------------------------
    def reset_demo(self) -> dict:
        if self.scenario_id and self.scenario_id in SCENARIOS:
            return self.load_scenario(self.scenario_id)
        self.rows = []
        self.recommendations = []
        self.used_samples = 0
        self.fit = None
        self.save()
        return self.state()

    def _find_rec(self, rec_id: str):
        return next((r for r in self.recommendations if r["id"] == rec_id), None)

    # ------------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        cells_meta = []
        for c in (self.fit or {}).get("layout_meta", {}).get("cells", []):
            cells_meta.append({
                "cell": c,
                "batch": (self.fit or {}).get("layout_meta", {})
                .get("cell_batch", {}).get(c)})
        return jsonable({
            "scenario_id": self.scenario_id,
            "name": self.name,
            "rows": self.rows,
            "meta": self.meta,
            "scopes": self.scopes,
            "model_options": self.model_options,
            "selected_cell": self.selected_cell,
            "used_samples": self.used_samples,
            "budget_samples": int(self.meta.get("budget_samples", 3)),
            "remaining_samples": int(self.meta.get("budget_samples", 3))
            - self.used_samples,
            "fit": self.fit,
            "cells_meta": cells_meta,
            "recommendations": [_public_rec(r) for r in self.recommendations],
            "import_errors": self.import_errors,
            "import_warnings": self.import_warnings,
        })

    # ------------------------------------------------------------------
    def save(self):
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "scenario_id": self.scenario_id, "name": self.name,
                "rows": self.rows, "meta": self.meta, "scopes": self.scopes,
                "model_options": self.model_options,
                "selected_cell": self.selected_cell,
                "used_samples": self.used_samples,
                "recommendations": self.recommendations,
                "rec_counter": self.rec_counter,
                "import_errors": self.import_errors,
                "import_warnings": self.import_warnings,
            }
            tmp = STATE_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(jsonable(payload), ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(STATE_PATH)
        except Exception:
            pass  # 持久化失败不影响内存会话

    @classmethod
    def load(cls) -> "Session":
        s = cls()
        try:
            if STATE_PATH.exists():
                d = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                s.__dict__.update({k: d.get(k, getattr(s, k)) for k in (
                    "scenario_id", "name", "rows", "meta", "scopes",
                    "model_options", "selected_cell", "used_samples",
                    "recommendations", "rec_counter",
                    "import_errors", "import_warnings")})
                s.refit()
        except Exception:
            # 状态损坏时回到标准场景，避免应用起不来
            s = cls()
            s.load_scenario("standard")
        return s


def _compact_profile(p: dict) -> dict:
    """轮廓快照只保留关键量（网格保留，供前端画前后对比）。"""
    if not p:
        return {}
    return {k: p.get(k) for k in (
        "available", "x_hat", "ci_low", "ci_high", "low_open", "high_open",
        "flat", "identifiable", "cutoff_dnll", "search_lo", "search_hi",
        "max_dnll", "grid", "label", "unit", "reason")}


def _public_rec(rec: dict) -> dict:
    """对外记录去掉 theta 快照等内部字段。"""
    return {k: v for k, v in rec.items()
            if k not in ("theta_snapshot", "scopes_snapshot",
                          "options_snapshot")}


def _ci_width(lo, hi):
    if lo is None or hi is None:
        return None
    return float(hi) - float(lo)


def _calibrated_width(w_profile_before, w_fisher_before, w_fisher_after):
    """用精确轮廓宽度校准 Fisher 相对收缩比，得到可与新轮廓比较的预测宽度。

    预测后宽度 = 轮廓前宽度 × (Fisher 后宽度 / Fisher 前宽度)。
    任一基准缺失时返回 None（前端仍可展示 Fisher 局部近似值）。
    """
    if (w_profile_before is None or w_fisher_before is None
            or w_fisher_after is None or w_fisher_before <= 0):
        return None
    return float(w_profile_before) * float(w_fisher_after) / float(w_fisher_before)


def _quality_label(width_ratio, actual_red, pred_red) -> str:
    if actual_red is None:
        return "区间仍开放，增益无法用有限宽度衡量（建议继续补点）"
    if width_ratio is None:
        return "预测宽度不可比"
    if 0.6 <= width_ratio <= 1.7:
        return "预测与实际基本一致（局部近似可靠）"
    if width_ratio < 0.6:
        return "实际区间比预测更窄：实测信息量高于局部预测"
    return "实际区间比预测更宽：非线性/参数相关使局部预测偏乐观"
