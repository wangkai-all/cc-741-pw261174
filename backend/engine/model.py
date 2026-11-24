"""双阶段（hockey-stick）退化模型 —— 项目内唯一的退化函数定义。

模型定义（容量保持率 / 退化指标 y，单位通常为 %）
--------------------------------------------------
对于单体 i，令 t 为检测时间：

    mu_i(t) = a_g - b1_g * t - (b2_g - b1_g) * (t - tau_g)_+ + d_{inst}

其中 (x)_+ = max(x, 0)。这是在转折点 tau 处连续的两段线性退化函数：

    阶段一 (t <= tau):  mu = a - b1 * t                 （缓慢退化）
    阶段二 (t >  tau):  mu = a - b1*tau - b2*(t - tau)  （加速退化, b2 >= b1）

* a   : t=0 时的初始水平
* b1  : 阶段一退化速率（>0，单位 y / 时间）
* b2  : 阶段二退化速率（>= b1）
* tau : 转折点（knee point）时间
* d   : 检测仪加性偏移（指定一台参考仪器 d=0），属于共享 nuisance，
        只平移读数，不随时间变化，因此不会被解释成单体退化速率。

不同参数的共享作用域 scope ∈ {"cell", "batch", "global"}：
cell = 每单体一个值；batch = 同批次共享；global = 所有批次共享。

寿命参数 L_e（默认 L80）：mu_e(L_e) = y_eol（默认 80%）的时间，
按转折点分两支反解：

    若 a - b1*tau <= y_eol（阈值在阶段一已被穿过）:
        L = (a - y_eol) / b1
    否则:
        L = tau + (a - b1*tau - y_eol) / b2

删失（超量程截断读数）不改变均值函数，只改变似然，见 fit.py。
"""

from __future__ import annotations

from typing import Any

import numpy as np

# 优化向量原始坐标的物理界（速率在向量里取对数，b2-b1 也取对数）
PARAM_BOUNDS = {
    "a": (40.0, 130.0),
    "lb1": (np.log(1e-3), np.log(12.0)),
    "ld": (np.log(1e-4), np.log(30.0)),   # ld = log(b2 - b1)
    "tau": (0.0, 240.0),
    "delta": (-20.0, 20.0),
    "lsigma": (np.log(0.03), np.log(12.0)),
}

DEFAULT_SCOPES = {"a": "cell", "b1": "batch", "b2": "batch", "tau": "batch"}
SCOPE_CHOICES = ["cell", "batch", "global"]
SCOPE_LABELS = {"cell": "每单体", "batch": "批次内共享", "global": "全部批次共享"}
KIND_LABELS = {
    "a": "初始水平 a",
    "b1": "一阶段速率 b1",
    "b2": "二阶段速率 b2",
    "tau": "转折点 τ",
}

# theta 段的固定排列顺序
THETA_ORDER = ("a", "b1", "tau", "ld", "delta", "lsigma")
# 每个逻辑参数对应的 theta 段 / 变换
KIND_TO_SEGMENT = {"a": "a", "b1": "b1", "b2": "ld", "tau": "tau"}


def curve(t, a, b1, b2, tau):
    """双阶段退化均值（不含仪器偏移）。t 可为标量或数组。"""
    t = np.asarray(t, dtype=float)
    return a - b1 * t - (b2 - b1) * np.maximum(t - tau, 0.0)


def life_time(a: float, b1: float, b2: float, tau: float, eol: float = 80.0):
    """反解达到 EOL 阈值的寿命时间。"""
    q_tau = a - b1 * tau
    if q_tau <= eol:
        return (a - eol) / b1
    return tau + (q_tau - eol) / b2


class Layout:
    """根据数据行与共享作用域，构造优化参数向量到各作用域参数的索引映射。

    theta 排列：[a 组..., log b1 组..., tau 组..., log(b2-b1) 组...,
                 delta 仪器组..., log sigma]
    """

    def __init__(
        self,
        rows: list[dict[str, Any]],
        scopes: dict[str, str] | None = None,
        model_options: dict[str, Any] | None = None,
    ):
        self.rows = rows
        self.scopes = dict(DEFAULT_SCOPES)
        if scopes:
            self.scopes.update({k: v for k, v in scopes.items() if k in DEFAULT_SCOPES})
        self.opts = {"instrument_offset": True, "reference_instrument": None}
        if model_options:
            self.opts.update(model_options)

        # ---- 单体 / 批次（保持出现顺序）----
        cell_ids: list[str] = []
        cell_batch: dict[str, str] = {}
        batches: list[str] = []
        for r in rows:
            cid = str(r["cell"])
            bid = str(r.get("batch", "?"))
            if cid not in cell_batch:
                cell_ids.append(cid)
                cell_batch[cid] = bid
            if bid not in batches:
                batches.append(bid)
        self.cell_ids = cell_ids
        self.cell_batch = cell_batch
        self.batches = batches
        self.cell_index = {c: i for i, c in enumerate(cell_ids)}

        # ---- 检测仪（第一台为参考，偏移固定 0）----
        insts: list[str] = []
        for r in rows:
            g = str(r.get("instrument", "INST-A"))
            if g not in insts:
                insts.append(g)
        ref = self.opts.get("reference_instrument")
        if ref in insts:
            insts.remove(ref)
            insts = [ref] + insts
        else:
            self.opts["reference_instrument"] = insts[0] if insts else None
        self.instruments = insts

        # ---- 每类参数的作用域组 ----
        self.groups: dict[str, list[str]] = {}
        self.cell_group: dict[str, dict[str, str]] = {}
        for kind in ("a", "b1", "b2", "tau"):
            scope = self.scopes[kind]
            gkeys: list[str] = []
            c2g: dict[str, str] = {}
            for cid in cell_ids:
                if scope == "cell":
                    gk = cid
                elif scope == "batch":
                    gk = f"batch:{cell_batch[cid]}"
                else:
                    gk = "global"
                c2g[cid] = gk
                if gk not in gkeys:
                    gkeys.append(gk)
            self.groups[kind] = gkeys
            self.cell_group[kind] = c2g

        # ---- theta 段与组坐标 ----
        self.segments: dict[str, slice] = {}
        self.group_index: dict[str, dict[str, int]] = {}
        self.segment_kinds: list[str] = []  # 每个 theta 坐标的 bound key
        pos = 0

        def add_segment(seg_name: str, group_kind: str, bound_key: str):
            nonlocal pos
            gkeys = self.groups[group_kind]
            self.segments[seg_name] = slice(pos, pos + len(gkeys))
            self.group_index[seg_name] = {gk: pos + j for j, gk in enumerate(gkeys)}
            self.segment_kinds.extend([bound_key] * len(gkeys))
            pos += len(gkeys)

        add_segment("a", "a", "a")
        add_segment("lb1", "b1", "lb1")
        add_segment("tau", "tau", "tau")
        add_segment("ld", "b2", "ld")

        self.offset_enabled = bool(self.opts["instrument_offset"]) and len(insts) > 1
        if self.offset_enabled:
            self.offset_instruments = insts[1:]
            self.segments["delta"] = slice(pos, pos + len(self.offset_instruments))
            self.group_index["delta"] = {
                g: pos + j for j, g in enumerate(self.offset_instruments)
            }
            self.segment_kinds.extend(["delta"] * len(self.offset_instruments))
            pos += len(self.offset_instruments)
        else:
            self.offset_instruments = []
            self.segments["delta"] = slice(pos, pos)
            self.group_index["delta"] = {}

        self.i_sigma = pos
        self.segment_kinds.append("lsigma")
        pos += 1
        self.n_params = pos

        # ---- 每行索引 ----
        n = len(rows)
        self.ia = np.empty(n, dtype=int)
        self.ib1 = np.empty(n, dtype=int)
        self.ild = np.empty(n, dtype=int)
        self.itau = np.empty(n, dtype=int)
        self.idelta = np.full(n, -1, dtype=int)
        for i, r in enumerate(rows):
            cid = str(r["cell"])
            self.ia[i] = self.group_index["a"][self.cell_group["a"][cid]]
            self.ib1[i] = self.group_index["lb1"][self.cell_group["b1"][cid]]
            self.itau[i] = self.group_index["tau"][self.cell_group["tau"][cid]]
            self.ild[i] = self.group_index["ld"][self.cell_group["b2"][cid]]
            if self.offset_enabled:
                inst = str(r.get("instrument", "INST-A"))
                self.idelta[i] = self.group_index["delta"].get(inst, -1)

        # ---- 观测数组 ----
        self.t = np.array([float(r["t"]) for r in rows], dtype=float)
        self.y = np.array(
            [np.nan if r.get("y") is None else float(r["y"]) for r in rows],
            dtype=float,
        )
        # cens: 0=精确, 1=右删失(读数>=limit, 超上限), -1=左删失(读数<=limit)
        self.cens = np.array([int(r.get("cens", 0)) for r in rows], dtype=int)
        self.limit = np.array(
            [np.nan if r.get("limit") is None else float(r["limit"]) for r in rows],
            dtype=float,
        )

    # ---------- 坐标 / 界 ----------
    def bounds(self) -> list[tuple[float, float]]:
        return [PARAM_BOUNDS[k] for k in self.segment_kinds]

    def physical_params(self, theta: np.ndarray) -> dict[str, dict[str, float]]:
        """返回各作用域组的物理参数值（b1, b2 已反变换）。"""
        theta = np.asarray(theta, dtype=float)
        out: dict[str, dict[str, float]] = {"a": {}, "b1": {}, "b2": {}, "tau": {}}
        for gk, idx in self.group_index["a"].items():
            out["a"][gk] = float(theta[idx])
        for gk, idx in self.group_index["lb1"].items():
            out["b1"][gk] = float(np.exp(theta[idx]))
        for gk, idx in self.group_index["tau"].items():
            out["tau"][gk] = float(theta[idx])
        for gk, idx_ld in self.group_index["ld"].items():
            cell = next(c for c in self.cell_ids
                        if self.cell_group["b2"][c] == gk)
            b1_gk = self.cell_group["b1"][cell]
            b1_idx = self.group_index["lb1"][b1_gk]
            out["b2"][gk] = float(np.exp(theta[b1_idx]) + np.exp(theta[idx_ld]))
        out["delta"] = {g: float(theta[i]) for g, i in self.group_index["delta"].items()}
        out["sigma"] = float(np.exp(theta[self.i_sigma]))
        return out

    # ---------- 预测 ----------
    def _row_indices_for(self, rows: list[dict[str, Any]]):
        """为外部候选行构造索引数组（行内单体/仪器必须已存在于 layout）。"""
        m = len(rows)
        ia = np.empty(m, dtype=int)
        ib1 = np.empty(m, dtype=int)
        ild = np.empty(m, dtype=int)
        itau = np.empty(m, dtype=int)
        idelta = np.full(m, -1, dtype=int)
        tt = np.empty(m, dtype=float)
        for i, r in enumerate(rows):
            cid = str(r["cell"])
            ia[i] = self.group_index["a"][self.cell_group["a"][cid]]
            ib1[i] = self.group_index["lb1"][self.cell_group["b1"][cid]]
            ild[i] = self.group_index["ld"][self.cell_group["b2"][cid]]
            itau[i] = self.group_index["tau"][self.cell_group["tau"][cid]]
            tt[i] = float(r["t"])
            if self.offset_enabled:
                inst = str(r.get("instrument", "INST-A"))
                idelta[i] = self.group_index["delta"].get(inst, -1)
        return tt, ia, ib1, ild, itau, idelta

    def predict(self, theta: np.ndarray, rows: list[dict] | None = None) -> np.ndarray:
        theta = np.asarray(theta, dtype=float)
        if rows is None:
            t, ia, ib1, ild, itau, idelta = (
                self.t, self.ia, self.ib1, self.ild, self.itau, self.idelta)
        else:
            t, ia, ib1, ild, itau, idelta = self._row_indices_for(rows)
        a = theta[ia]
        b1 = np.exp(theta[ib1])
        b2 = b1 + np.exp(theta[ild])
        tau = theta[itau]
        mu = curve(t, a, b1, b2, tau)
        if self.offset_enabled:
            mask = idelta >= 0
            if np.any(mask):
                mu = mu.copy()
                mu[mask] += theta[idelta[mask]]
        return mu

    def predict_one(self, theta, cell_id: str, t, instrument: str | None = None):
        row = {"cell": cell_id, "t": t, "instrument": instrument}
        return float(self.predict(theta, [row])[0])

    def life_of_cell(self, theta, cell_id: str, eol: float = 80.0) -> float:
        theta = np.asarray(theta, dtype=float)
        ia = self.group_index["a"][self.cell_group["a"][cell_id]]
        ib1 = self.group_index["lb1"][self.cell_group["b1"][cell_id]]
        ild = self.group_index["ld"][self.cell_group["b2"][cell_id]]
        itau = self.group_index["tau"][self.cell_group["tau"][cell_id]]
        a = theta[ia]
        b1 = float(np.exp(theta[ib1]))
        b2 = b1 + float(np.exp(theta[ild]))
        tau = theta[itau]
        return life_time(a, b1, b2, tau, eol)

    def life_gradient(self, theta, cell_id: str, eol: float = 80.0,
                      eps: float = 1e-5) -> np.ndarray:
        """数值梯度 dL/dtheta 的均值参数部分（前 n_params-1 维）。"""
        theta = np.asarray(theta, dtype=float)
        p = self.n_params - 1
        g = np.zeros(p)
        for j in range(p):
            h = eps * max(1.0, abs(theta[j]))
            tp = theta.copy(); tp[j] += h
            tm = theta.copy(); tm[j] -= h
            g[j] = (self.life_of_cell(tp, cell_id, eol)
                    - self.life_of_cell(tm, cell_id, eol)) / (2 * h)
        return g

    def mean_jacobian(self, theta, eps: float = 1e-5,
                      rows: list[dict] | None = None) -> np.ndarray:
        """d mu_i / d theta_mean 中心差分 Jacobian（不含 log_sigma）。"""
        theta = np.asarray(theta, dtype=float)
        p = self.n_params - 1
        base = self.predict(theta, rows)
        n = len(base)
        J = np.zeros((n, p))
        for j in range(p):
            h = eps * max(1.0, abs(theta[j]))
            tp = theta.copy(); tp[j] += h
            tm = theta.copy(); tm[j] -= h
            J[:, j] = (self.predict(tp, rows) - self.predict(tm, rows)) / (2 * h)
        return J

    def initial_theta(self) -> np.ndarray:
        theta = np.zeros(self.n_params)
        theta[self.segments["a"]] = 100.0
        theta[self.segments["lb1"]] = np.log(0.4)
        theta[self.segments["tau"]] = 24.0
        theta[self.segments["ld"]] = np.log(1.0)
        theta[self.i_sigma] = np.log(0.8)
        return theta
