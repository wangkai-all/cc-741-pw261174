"""追加检测设计：从可用时间窗口中选下一次检测，最小化关键寿命参数的不确定范围。

方法
----
采用局部二次（Fisher / Schur 补）近似，它与轮廓似然在 MLE 附近等价：

* 现有数据的均值参数信息矩阵 I = Σ w_i j_i j_iᵀ，j_i = ∂μ_i/∂θ；
* 精确观测 w=1/σ²；超量程删失观测的 w 是“带量程区间的单观测期望负对数似然
  对 μ 的二阶曲率”，自动计入删失概率；
* 候选时间 t 上增加 n 次重复 => I_aug = I + n·w(t)·j(t)j(t)ᵀ；
* 寿命 L(θ) 的梯度 g 下，Var(L) ≈ gᵀ I_aug^{-1} g（对全部 nuisance 做了
  profile/Schur 补），预测 95% 区间宽度 = 2·1.96·√Var。

若当前 I 在 g 投影方向上奇异，则现有数据局部不可分辨（宽度 ∞），此时以
“增广后能否满秩 + 增广后宽度”排序。样本破坏数是硬预算，由 session 控制。
"""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy.special import ndtr

from .model import Layout

_SQRT_2PI = np.sqrt(2 * np.pi)
_Z95 = 1.959963984540054


def _phi(z):
    return np.exp(-0.5 * z * z) / _SQRT_2PI


def interval_expected_nll(mu: float, sigma: float, lo: float, hi: float) -> float:
    """量程区间 [lo, hi] 下单次观测的期望负对数似然（含双侧删失）。

    仅用于诊断/测试。注意：Fisher 信息不能直接把“期望 NLL 表达式”对 mu
    求全导数（期望分布也随 mu 移动，会得到错误的 0 曲率）；信息量权重请用
    :func:`censor_info_weight` 的 score 方差解析公式。
    """
    if lo is None or not np.isfinite(lo):
        lo = -1e7 * sigma + mu
    if hi is None or not np.isfinite(hi):
        hi = 1e7 * sigma + mu
    zl = (lo - mu) / sigma
    zh = (hi - mu) / sigma
    pL = float(ndtr(zl))
    pR = float(ndtr(-zh))
    pE = float(ndtr(zh) - ndtr(zl))
    ex2 = ((float(ndtr(zh)) - zh * _phi(zh))
           - (float(ndtr(zl)) - zl * _phi(zl)))
    q = pE * 0.5 * np.log(2 * np.pi * sigma * sigma) + 0.5 * ex2
    if pL > 1e-300:
        q -= pL * np.log(pL)
    if pR > 1e-300:
        q -= pR * np.log(pR)
    return q


def _censor_term_left(z: float) -> float:
    """Phi(z) * [phi(z)/Phi(z)]^2 = phi(z)^2/Phi(z)，log 域稳定计算。"""
    if z == -np.inf:
        return 0.0
    from scipy.special import log_ndtr
    log_phi = -0.5 * z * z - 0.5 * np.log(2 * np.pi)
    val = np.exp(np.clip(2 * log_phi - log_ndtr(z), -745.0, 60.0))
    return float(val)


def _censor_term_right(z: float) -> float:
    """phi(z)^2 / Phi(-z)，log 域稳定计算。z=+inf 时为 0。"""
    if z == np.inf:
        return 0.0
    from scipy.special import log_ndtr
    log_phi = -0.5 * z * z - 0.5 * np.log(2 * np.pi)
    val = np.exp(np.clip(2 * log_phi - log_ndtr(-z), -745.0, 60.0))
    return float(val)


def censor_info_weight(mu: float, sigma: float,
                       lo: float | None, hi: float | None) -> float:
    """单次（可能双侧删失的）高斯观测对 μ 的 Fisher 信息权重 w。

    w = E[(d logL/dμ)^2] = 1/σ² × {精确区间 x²φ 积分
        + 左删失概率×左 Mills² + 右删失概率×右 Mills²}。
    无删失界限时严格退化为 1/σ²。
    """
    zl = -np.inf if lo is None or not np.isfinite(lo) else (lo - mu) / sigma
    zh = np.inf if hi is None or not np.isfinite(hi) else (hi - mu) / sigma

    def interval_x2(z):
        if not np.isfinite(z):
            return 0.0 if z == -np.inf else 1.0  # Phi(z)-z phi(z) 在 ±inf 的极限
        return float(ndtr(z) - z * _phi(z))

    exact_info = interval_x2(zh) - interval_x2(zl)
    left_info = _censor_term_left(float(zl)) if np.isfinite(zl) else 0.0
    right_info = _censor_term_right(float(zh)) if np.isfinite(zh) else 0.0
    total = exact_info + left_info + right_info
    return max(0.0, float(total) / sigma ** 2)


def _row_weights(lay: Layout, theta: np.ndarray) -> np.ndarray:
    sigma = float(np.exp(theta[lay.i_sigma]))
    mu = lay.predict(theta)
    w = np.empty(len(lay.rows))
    for i, r in enumerate(lay.rows):
        cens = int(r.get("cens", 0))
        if cens == 0:
            w[i] = 1.0 / sigma ** 2
        else:
            lim = float(r.get("limit"))
            if cens == 1:      # 右删失：量程 (-inf, lim]
                w[i] = censor_info_weight(mu[i], sigma, None, lim)
            else:              # 左删失：量程 [lim, inf)
                w[i] = censor_info_weight(mu[i], sigma, lim, None)
    return w


def fisher_information(lay: Layout, theta: np.ndarray) -> np.ndarray:
    J = lay.mean_jacobian(theta)
    w = _row_weights(lay, theta)
    return J.T @ (J * w[:, None])


def width_from_information(I: np.ndarray, g: np.ndarray
                            ) -> dict[str, Any]:
    """给定信息矩阵与寿命梯度，返回 Wald 95% 宽度及可分辨性。"""
    eigvals, eigvecs = np.linalg.eigh(0.5 * (I + I.T))
    wmax = max(float(np.max(eigvals)), 1e-12)
    tol = max(1e-9 * wmax, 1e-11)
    var = 0.0
    null_proj2 = 0.0
    gnorm2 = float(g @ g) + 1e-12
    rank = 0
    for k, ev in enumerate(eigvals):
        proj = float(g @ eigvecs[:, k]) ** 2
        if ev > tol:
            var += proj / float(ev)
            rank += 1
        else:
            null_proj2 += proj
    null_frac = null_proj2 / gnorm2
    infinite = null_frac > 1e-6 or not np.isfinite(var)
    se = float(np.sqrt(max(var, 0.0)))
    return {
        "se": se,
        "width95": (None if infinite else float(2 * _Z95 * se)),
        "infinite": bool(infinite),
        "rank": int(rank), "p": int(I.shape[0]),
        "null_projection_frac": float(null_frac),
    }


def candidate_augmentation(lay: Layout, theta: np.ndarray, cell: str,
                           t: float, instrument: str | None, n: int = 1,
                           range_lo: float | None = None,
                           range_hi: float | None = None):
    row = {"cell": cell, "t": float(t),
           "instrument": instrument or lay.opts.get("reference_instrument")}
    J1 = lay.mean_jacobian(theta, rows=[row])[0]
    mu = lay.predict_one(theta, cell, t, row["instrument"])
    sigma = float(np.exp(theta[lay.i_sigma]))
    w = censor_info_weight(mu, sigma, range_lo, range_hi)
    add = int(n) * w * np.outer(J1, J1)
    return add, {"mu": float(mu), "weight": float(w), "sigma": sigma,
                 "instrument": row["instrument"]}


def evaluate_choice(rows, scopes, model_options, fit: dict, cell: str,
                    eol: float, t: float, n: int = 1,
                    instrument: str | None = None,
                    range_lo: float | None = None,
                    range_hi: float | None = None) -> dict[str, Any]:
    """评估在指定 t 增加 n 次测量后的预测寿命区间宽度。"""
    lay = Layout(rows, scopes, model_options)
    theta = np.array(fit["theta"], dtype=float)
    I0 = fisher_information(lay, theta)
    g = lay.life_gradient(theta, cell, eol)
    base = width_from_information(I0, g)
    add, cinfo = candidate_augmentation(
        lay, theta, cell, t, instrument, n, range_lo, range_hi)
    aug = width_from_information(I0 + add, g)

    w0 = base["width95"]
    w1 = aug["width95"]
    if w0 and w1 is not None and w0 > 0:
        reduction = max(0.0, (w0 - w1) / w0)
    else:
        reduction = None
    return {
        "t": float(t), "n": int(n), **cinfo,
        "width_before_fisher": w0,
        "width_after_fisher": w1,
        "se_after": aug["se"],
        "reduction_frac": reduction,
        "currently_unidentifiable": base["infinite"],
        "augmented_full_rank": (not aug["infinite"]),
        "rank_before": base["rank"], "rank_after": aug["rank"],
        "p": aug["p"],
    }


def rank_windows(rows, scopes, model_options, fit: dict, cell: str,
                 eol: float, windows: list[dict],
                 range_lo=None, range_hi=None, n: int = 1,
                 instrument: str | None = None,
                 n_grid_per_window: int = 9) -> dict[str, Any]:
    """对每个可用时间段搜索最佳检测时刻并排序。"""
    existing_t = {round(float(r["t"]), 4)
                  for r in rows if str(r["cell"]) == cell}
    lay = Layout(rows, scopes, model_options)
    theta = np.array(fit["theta"], dtype=float)
    I0 = fisher_information(lay, theta)
    g = lay.life_gradient(theta, cell, eol)
    base = width_from_information(I0, g)

    candidates = []
    for w in windows:
        lo, hi = float(w["lo"]), float(w["hi"])
        ts = np.linspace(lo, hi, n_grid_per_window)
        best = None
        evaluations = []
        for t in ts:
            ev = evaluate_choice(rows, scopes, model_options, fit, cell, eol,
                                 float(t), n, instrument, range_lo, range_hi)
            ev["already_measured"] = round(float(t), 4) in existing_t
            evaluations.append(ev)
            if ev["already_measured"]:
                continue
            key = ev["width_after_fisher"]
            if key is None:
                # 仍不可分辨：用 rank 与一个大惩罚宽度排序
                sort_key = (1, -ev["rank_after"], 1e9)
            else:
                sort_key = (0, 0, key)
            if best is None or sort_key < best[0]:
                best = (sort_key, ev)
        mid = evaluate_choice(rows, scopes, model_options, fit, cell, eol,
                              (lo + hi) / 2, n, instrument, range_lo, range_hi)
        if best is None:
            best_ev = mid
        else:
            best_ev = best[1]
        candidates.append({
            "window_id": w.get("id"),
            "label": w.get("label", w.get("id")),
            "lo": lo, "hi": hi,
            "best_t": best_ev["t"],
            "best": best_ev,
            "midpoint": mid,
            "grid": [{"t": e["t"], "width_after": e["width_after_fisher"],
                      "reduction_frac": e["reduction_frac"],
                      "already_measured": e["already_measured"]}
                     for e in evaluations],
        })

    def rank_key(c):
        b = c["best"]
        if b["width_after_fisher"] is None:
            return (1, -b["rank_after"])
        return (0, b["width_after_fisher"])

    candidates.sort(key=rank_key)
    top = candidates[0] if candidates else None
    return {
        "target_cell": cell, "eol": eol, "n": int(n),
        "width_before_fisher": base["width95"],
        "se_before_fisher": base["se"],
        "currently_unidentifiable": base["infinite"],
        "rank_before": base["rank"], "p": base["p"],
        "candidates": candidates,
        "top_window_id": top["window_id"] if top else None,
        "top_t": top["best_t"] if top else None,
        "top_prediction": top["best"] if top else None,
    }


# ---------------------------------------------------------------------------
# 合成测量
# ---------------------------------------------------------------------------

def generate_synthetic_round(rows, scopes, model_options, fit: dict,
                             cell: str, t: float, n: int,
                             instrument: str, range_lo, range_hi,
                             seed: int) -> list[dict[str, Any]]:
    """按当前 MLE 模型 + 高斯噪声生成 n 条合成读数，并执行量程截断。"""
    lay = Layout(rows, scopes, model_options)
    theta = np.array(fit["theta"], dtype=float)
    sigma = float(np.exp(theta[lay.i_sigma]))
    rng = np.random.default_rng(seed)
    out = []
    for k in range(1, int(n) + 1):
        mu = lay.predict_one(theta, cell, float(t), instrument)
        y_obs = mu + rng.normal(0.0, sigma)
        cens, limit, y_rec = 0, None, y_obs
        if range_hi is not None and y_obs > range_hi:
            cens, limit, y_rec = 1, float(range_hi), float(range_hi)
        elif range_lo is not None and y_obs < range_lo:
            cens, limit, y_rec = -1, float(range_lo), float(range_lo)
        out.append({
            "record_id": f"SYN-{seed}-{k}",
            "batch": lay.cell_batch[cell], "cell": cell, "t": float(t),
            "y": round(float(y_rec), 3), "cens": cens, "limit": limit,
            "instrument": instrument, "repeat": k,
            "provenance": "synthetic",
            "synthetic_mu": round(float(mu), 3),
        })
    return out
