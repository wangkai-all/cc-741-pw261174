"""参数拟合、标准误与轮廓似然。

似然
----
独立高斯测量误差 eps ~ N(0, sigma^2)，均值由 model.Layout.predict 给出。
- 精确读数：普通高斯密度；
- 右删失（超过检测仪上限，记录为 >=L）：P(Y >= L) = log Phi((mu-L)/sigma)；
- 左删失（低于下限，记录为 <=L）：P(Y <= L) = log Phi((L-mu)/sigma)。

因此“超过量程的截断读数”按删失处理，而不是把量程界限当成真实读数。

可分辨性
--------
除 Hessian/Fisher 标准误外，提供关键参数（寿命 L_e、tau、b2、仪器偏移）的
轮廓似然与 95% 轮廓似然置信区间（2*(ll_max-ll_prof) <= 3.84）。轮廓在搜索
范围内无法越过阈值 => 区间开放 / 参数不可分辨，由结果标志明确给出。
"""

from __future__ import annotations

import warnings
from typing import Any

import numpy as np
from scipy.optimize import minimize
from scipy.special import log_ndtr

from .model import Layout, life_time

CHI2_95_1DF = 3.841458820694124
DNLL_95 = CHI2_95_1DF / 2.0  # 负对数似然允许的增量 ≈ 1.9207


# ---------------------------------------------------------------------------
# 似然
# ---------------------------------------------------------------------------

def neg_log_likelihood(theta: np.ndarray, lay: Layout) -> float:
    return nll_and_grad(theta, lay, need_grad=False)


def _inv_mills(z: np.ndarray) -> np.ndarray:
    """m(z) = phi(z)/Phi(z)，用 log 域稳定计算（左删失梯度用）。"""
    from scipy.special import log_ndtr
    log_phi = -0.5 * z * z - 0.5 * np.log(2 * np.pi)
    log_m = log_phi - log_ndtr(z)
    return np.exp(np.clip(log_m, -60.0, 60.0))


def nll_and_grad(theta: np.ndarray, lay: Layout, need_grad: bool = True):
    """负对数似然及其解析梯度。

    模型可写成 mu = a - b1*t - d*(t-tau)_+ + delta，d = b2-b1 = exp(ld)。
    """
    theta = np.asarray(theta, dtype=float)
    sigma = float(np.exp(theta[lay.i_sigma]))
    sig2 = sigma * sigma

    a = theta[lay.ia]
    b1 = np.exp(theta[lay.ib1])
    d = np.exp(theta[lay.ild])
    tau = theta[lay.itau]
    h = np.maximum(lay.t - tau, 0.0)
    inside = (lay.t > tau).astype(float)
    mu = a - b1 * lay.t - d * h
    if lay.offset_enabled:
        mask = lay.idelta >= 0
        mu = mu.copy()
        mu[mask] += theta[lay.idelta[mask]]

    y, cens, lim = lay.y, lay.cens, lay.limit
    grad_mu = np.zeros(len(lay.rows))  # d nll_i / d mu_i
    nll = 0.0
    grad_sigma_log = 0.0

    exact = cens == 0
    if np.any(exact):
        r = y[exact] - mu[exact]
        n_exact = int(np.sum(exact))
        nll += 0.5 * np.sum(r * r) / sig2 + n_exact * np.log(sigma) \
            + 0.5 * n_exact * np.log(2 * np.pi)
        grad_mu[exact] = -r / sig2
        grad_sigma_log += float(np.sum(1.0 - r * r / sig2))

    right = cens == 1
    if np.any(right):
        z = (mu[right] - lim[right]) / sigma
        m = _inv_mills(z)
        nll -= float(np.sum(log_ndtr(z)))
        grad_mu[right] = -m / sigma
        grad_sigma_log += float(np.sum(z * m))

    left = cens == -1
    if np.any(left):
        z = (lim[left] - mu[left]) / sigma
        m = _inv_mills(z)
        nll -= float(np.sum(log_ndtr(z)))
        grad_mu[left] = m / sigma
        grad_sigma_log += float(np.sum(z * m))

    if not np.isfinite(nll):
        return (1e12, np.zeros(lay.n_params)) if need_grad else 1e12
    if not need_grad:
        return float(nll)

    grad = np.zeros(lay.n_params)
    # a
    np.add.at(grad, lay.ia, grad_mu)
    # log b1: dmu/d(lb1) = -b1*t
    np.add.at(grad, lay.ib1, grad_mu * (-b1 * lay.t))
    # log d: dmu/d(ld) = -d*h
    np.add.at(grad, lay.ild, grad_mu * (-d * h))
    # tau: dmu/dtau = d * I(t>tau)
    np.add.at(grad, lay.itau, grad_mu * (d * inside))
    # instrument delta
    if lay.offset_enabled:
        mask = lay.idelta >= 0
        np.add.at(grad, lay.idelta[mask], grad_mu[mask])
    grad[lay.i_sigma] = grad_sigma_log
    if not np.all(np.isfinite(grad)):
        grad = np.nan_to_num(grad, nan=0.0, posinf=1e6, neginf=-1e6)
    return float(nll), grad


# ---------------------------------------------------------------------------
# 拟合前可行性检查
# ---------------------------------------------------------------------------

def feasibility_check(rows: list[dict], lay: Layout) -> list[str]:
    problems: list[str] = []
    n = len(rows)
    n_exact = int(sum(1 for r in rows if r.get("cens", 0) == 0))
    distinct_t = len({round(float(r["t"]), 6) for r in rows})
    if n < lay.n_params:
        problems.append(
            f"数据点 {n} 条少于待估参数 {lay.n_params} 个"
            f"（当前共享假设下），自由度 {n - lay.n_params}，无法唯一拟合。")
    elif n - lay.n_params < 1:
        problems.append("自由度为 0，参数估计与误差估计不可靠。")
    if distinct_t < 3:
        problems.append(f"只有 {distinct_t} 个不同检测时间，无法分辨两阶段结构（至少需要 3 个时间点）。")
    # 每个单体的时间点数（cell 作用域参数需要）
    per_cell: dict[str, int] = {}
    for r in rows:
        per_cell[str(r["cell"])] = per_cell.get(str(r["cell"]), 0) + 1
    if lay.scopes["a"] == "cell":
        sparse = [c for c, k in per_cell.items() if k < 2]
        if sparse:
            problems.append(
                f"单体 {', '.join(sparse[:5])}"
                + (" 等" if len(sparse) > 5 else "")
                + " 不足 2 个读数，单体初始水平 a 只能勉强/无法估计；建议把 a 改为批次或全局共享。")
    return problems


# ---------------------------------------------------------------------------
# 多起点拟合
# ---------------------------------------------------------------------------

def _starts(lay: Layout) -> list[np.ndarray]:
    """生成多组初值，重点对 tau 与 b2-b1 网格搜索，避免折线 kink 局部解。"""
    t = lay.t
    t_lo = float(np.min(t)) if len(t) else 0.0
    t_hi = float(np.max(t)) if len(t) else 40.0
    tau_grid = sorted(set([
        max(0.0, t_lo + 0.15 * (t_hi - t_lo)),
        t_lo + 0.4 * (t_hi - t_lo),
        t_lo + 0.65 * (t_hi - t_lo),
        t_hi,
        t_hi + 0.35 * max(10.0, t_hi - t_lo),
    ]))
    starts = []
    # 用简单线性回归给 a / b1 一个整体初值
    try:
        exact = lay.cens == 0
        coef = np.polyfit(lay.t[exact], lay.y[exact], 1)
        slope0 = max(0.02, min(5.0, -float(coef[0])))
        intercept0 = float(coef[1])
    except Exception:
        slope0, intercept0 = 0.4, 100.0

    for tau0 in tau_grid:
        for ld0 in (np.log(0.3), np.log(1.5)):
            th = lay.initial_theta()
            th[lay.segments["a"]] = np.clip(intercept0, 60, 120)
            th[lay.segments["lb1"]] = np.log(slope0)
            th[lay.segments["tau"]] = np.clip(tau0, 0.01, 239.0)
            th[lay.segments["ld"]] = ld0
            starts.append(th)
    return starts


def fit_model(rows: list[dict], scopes: dict | None = None,
              model_options: dict | None = None) -> dict[str, Any]:
    """拟合模型。返回结构化结果（永远不抛优化异常给调用方）。"""
    lay = Layout(rows, scopes, model_options)
    feas = feasibility_check(rows, lay)
    hard_fail = any(("少于待估参数" in p) or ("只有 " in p and "不同检测时间" in p)
                    for p in feas)

    result: dict[str, Any] = {
        "ok": False, "layout_meta": _layout_meta(lay),
        "feasibility": feas, "warnings": [], "estimates": None,
        "theta": None, "nll": None, "aic": None, "bic": None,
        "hessian_se": None, "gof": None, "hard_fail": hard_fail,
    }
    if hard_fail:
        result["warnings"].append("未执行数值拟合：数据信息量低于模型自由度要求。")
        return result

    bounds = lay.bounds()
    best = None
    best_nll = np.inf
    opt_messages = []

    def obj(th):
        return nll_and_grad(th, lay, need_grad=True)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        # 第一轮：所有起点粗筛
        candidates = []
        for th0 in _starts(lay):
            try:
                opt = minimize(
                    obj, th0, jac=True, method="L-BFGS-B", bounds=bounds,
                    options={"maxiter": 250, "ftol": 1e-9, "gtol": 1e-6})
                if np.isfinite(opt.fun):
                    candidates.append((float(opt.fun), opt.x.copy()))
            except Exception as e:  # pragma: no cover
                opt_messages.append(str(e))
        candidates.sort(key=lambda z: z[0])
        # 第二轮：只精修最好的 2 个不同候选
        seen_tau = set()
        refined = 0
        for fun0, th_c in candidates:
            tau_sig = tuple(np.round(th_c[lay.segments["tau"]], 1))
            if tau_sig in seen_tau and refined >= 1:
                continue
            seen_tau.add(tau_sig)
            try:
                opt = minimize(
                    obj, th_c, jac=True, method="L-BFGS-B", bounds=bounds,
                    options={"maxiter": 1500, "ftol": 1e-13, "gtol": 1e-9})
                if np.isfinite(opt.fun) and opt.fun < best_nll:
                    best_nll = float(opt.fun)
                    best = opt.x.copy()
                refined += 1
            except Exception:
                if fun0 < best_nll:
                    best_nll, best = fun0, th_c
            if refined >= 2:
                break
        if best is None and candidates:
            best_nll, best = candidates[0]

    result["ok"] = True
    result["theta"] = best.tolist()
    result["nll"] = best_nll
    k = lay.n_params
    n = len(rows)
    result["aic"] = 2 * k + 2 * best_nll
    result["bic"] = k * np.log(max(n, 2)) + 2 * best_nll
    result["estimates"] = lay.physical_params(best)
    result["hessian_se"] = _hessian_standard_errors(lay, best)
    result["gof"] = _goodness_of_fit(lay, best)
    result["warnings"] += _estimate_warnings(lay, best, result["hessian_se"])
    return result


def _layout_meta(lay: Layout) -> dict[str, Any]:
    return {
        "cells": lay.cell_ids,
        "batches": lay.batches,
        "cell_batch": lay.cell_batch,
        "instruments": lay.instruments,
        "reference_instrument": lay.opts.get("reference_instrument"),
        "offset_enabled": lay.offset_enabled,
        "scopes": lay.scopes,
        "n_params": lay.n_params,
        "groups": lay.groups,
    }


def _hessian_standard_errors(lay: Layout, theta: np.ndarray) -> dict[str, Any]:
    """数值 Hessian -> 协方差 -> SE。Hessian 非正定时用伪逆并标记。"""
    p = lay.n_params
    H = np.zeros((p, p))
    eps0 = 1e-4

    def f(x):
        return neg_log_likelihood(x, lay)

    f0 = f(theta)
    for i in range(p):
        hi = eps0 * max(1.0, abs(theta[i]))
        for j in range(i, p):
            hj = eps0 * max(1.0, abs(theta[j]))
            tp = theta.copy(); tp[i] += hi
            tm = theta.copy(); tm[i] -= hi
            if i == j:
                val = (f(tp) - 2 * f0 + f(tm)) / hi ** 2
            else:
                tpp = theta.copy(); tpp[i] += hi; tpp[j] += hj
                tpm = theta.copy(); tpm[i] += hi; tpm[j] -= hj
                tmp = theta.copy(); tmp[i] -= hi; tmp[j] += hj
                tmm = theta.copy(); tmm[i] -= hi; tmm[j] -= hj
                val = (f(tpp) - f(tpm) - f(tmp) + f(tmm)) / (4 * hi * hj)
            H[i, j] = H[j, i] = val
    cov, rank, singular = _safe_inv(H)
    se = np.sqrt(np.clip(np.diag(cov), 0, None))
    return {
        "se_vector": se.tolist(),
        "cov": cov.tolist(),
        "hessian_rank": int(rank),
        "singular": bool(singular),
        "coordinate_index": {
            "segments": {k: [int(v.start), int(v.stop)]
                         for k, v in lay.segments.items()},
            "i_sigma": int(lay.i_sigma),
            "group_index": lay.group_index,
        },
    }


def _safe_inv(M: np.ndarray):
    """对称矩阵求逆；近奇异时特征值截断伪逆。返回 (inv, rank, singular)。"""
    w, V = np.linalg.eigh(M)
    wmax = np.max(np.abs(w)) if len(w) else 0.0
    tol = max(1e-10 * max(wmax, 1e-12), 1e-12)
    keep = w > tol
    rank = int(np.sum(keep))
    singular = rank < M.shape[0] or np.any(w <= 0)
    winv = np.zeros_like(w)
    winv[keep] = 1.0 / w[keep]
    inv = (V * winv) @ V.T
    return inv, rank, singular


def _goodness_of_fit(lay: Layout, theta: np.ndarray) -> dict[str, Any]:
    mu = lay.predict(theta)
    exact = lay.cens == 0
    y = lay.y[exact]
    m = mu[exact]
    resid = y - m
    n = len(y)
    sse = float(np.sum(resid ** 2))
    rmse = float(np.sqrt(sse / max(n, 1)))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2)) if n > 1 else 0.0
    r2 = float(1 - sse / ss_tot) if ss_tot > 1e-12 else None
    return {
        "rmse": rmse, "r2": r2, "n_exact": n,
        "residuals": [
            {"t": float(lay.t[i]), "cell": lay.rows[i]["cell"],
             "instrument": lay.rows[i].get("instrument"),
             "fitted": float(mu[i]), "observed": float(lay.y[i]),
             "residual": float(lay.y[i] - mu[i])}
            for i in np.where(exact)[0]
        ],
    }


def _estimate_warnings(lay: Layout, theta: np.ndarray, se_info: dict) -> list[str]:
    warns: list[str] = []
    pp = lay.physical_params(theta)
    for gk, tau in pp["tau"].items():
        if tau <= float(np.min(lay.t)) + 1e-6:
            warns.append(f"转折点 τ（{gk}）贴在下界/最早检测时间，可能未被数据分辨。")
        if tau >= float(np.max(lay.t)) - 1e-6:
            warns.append(f"转折点 τ（{gk}）≥ 最晚检测时间：转折点很可能尚未被观测到，"
                         f"b2 与寿命仅靠外推。")
    for gk, b2 in pp["b2"].items():
        for cell in lay.cell_ids:
            if lay.cell_group["b2"][cell] == gk:
                b1gk = lay.cell_group["b1"][cell]
                b1 = pp["b1"][b1gk]
                if b2 < b1 * 1.05:
                    warns.append(f"组 {gk} 的 b2≈b1，未见明显加速阶段，寿命对模型形式高度敏感。")
                break
    if se_info and se_info.get("singular"):
        warns.append("信息/Hessian 矩阵近奇异：存在强相关或不可分辨参数，"
                     "标准误可能低估，请以轮廓似然区间为准。")
    return warns


# ---------------------------------------------------------------------------
# 轮廓似然
# ---------------------------------------------------------------------------

def profile_likelihood(rows: list[dict], scopes: dict | None,
                       model_options: dict | None, fit: dict[str, Any],
                       target: dict[str, Any],
                       n_grid: int = 21) -> dict[str, Any]:
    """对目标参数做轮廓似然（中心向外链式热启动 + CI 交点二分精修）。"""
    lay = Layout(rows, scopes, model_options)
    if not fit.get("ok") or fit.get("theta") is None:
        return {"target": target, "available": False,
                "reason": "当前数据无法完成基准拟合，轮廓似然不可用。"}
    theta_hat = np.array(fit["theta"], dtype=float)
    nll_min = float(fit["nll"])
    bounds = lay.bounds()

    fval, _ = _target_functions(lay, target)
    x_hat = fval(theta_hat)
    target_se = _target_standard_error(fit, lay, fval, theta_hat)
    xs = _profile_grid(target, x_hat, lay, max(n_grid, 15), target_se)

    # 中心索引：最接近 x_hat 的网格点
    c = int(np.argmin(np.abs(xs - x_hat)))
    thetas: list[np.ndarray | None] = [None] * len(xs)
    nlls = np.full(len(xs), np.inf)
    conv = [False] * len(xs)

    def solve(i: int, warm: np.ndarray):
        th, nll_p, ok = _profile_point(lay, warm, bounds, fval, float(xs[i]))
        thetas[i] = th
        nlls[i] = nll_p
        conv[i] = bool(ok)
        return th if ok else warm

    # 中心点：用 MLE 热启动
    warm = solve(c, theta_hat.copy())
    center_nll = nlls[c]
    if np.isfinite(center_nll) and center_nll < nll_min - 1e-6:
        # 轮廓点反而找到更优解：以它为新基准（多起点拟合的兜底）
        nll_min = float(center_nll)
        theta_hat = thetas[c].copy()
    # 向下扫描
    warm = thetas[c].copy() if thetas[c] is not None else theta_hat.copy()
    for i in range(c - 1, -1, -1):
        warm = solve(i, warm)
    # 向上扫描
    warm = thetas[c].copy() if thetas[c] is not None else theta_hat.copy()
    for i in range(c + 1, len(xs)):
        warm = solve(i, warm)

    dnlls = np.array([max(0.0, v - nll_min) if np.isfinite(v) else np.inf
                      for v in nlls])
    prof = [{"x": float(xs[i]), "dnll": (None if not np.isfinite(dnlls[i])
                                         else float(dnlls[i])),
             "converged": conv[i]} for i in range(len(xs))]

    # 边界开放性：搜索端点仍在阈值内
    cut = DNLL_95
    low_open = bool(dnlls[0] <= cut)
    high_open = bool(dnlls[-1] <= cut)

    # 找交点并二分精修
    ci_low = _refine_bound(lay, bounds, fval, xs, dnlls, thetas,
                           side="low", cut=cut, nll_min=nll_min)
    ci_high = _refine_bound(lay, bounds, fval, xs, dnlls, thetas,
                            side="high", cut=cut, nll_min=nll_min)

    finite_dnll = dnlls[np.isfinite(dnlls)]
    max_dnll = float(np.max(finite_dnll)) if len(finite_dnll) else 0.0
    flat = bool(max_dnll < cut)
    center_dnll = float(dnlls[c]) if np.isfinite(dnlls[c]) else None
    unreliable = bool(center_dnll is not None and center_dnll > 0.5 * cut)
    identifiable = (ci_low is not None and ci_high is not None
                    and not flat and not low_open and not high_open)
    width = (ci_high - ci_low) if (ci_low is not None and ci_high is not None) else None
    scale = max(0.35 * max(abs(x_hat), float(np.max(lay.t)), 20.0), 8.0)
    wide = bool(width is not None and width > 1.2 * scale)
    return {
        "target": target, "available": True,
        "x_hat": float(x_hat),
        "label": _target_label(target), "unit": _target_unit(target),
        "grid": prof, "cutoff_dnll": cut,
        "ci_low": (float(ci_low) if ci_low is not None else None),
        "ci_high": (float(ci_high) if ci_high is not None else None),
        "low_open": bool(low_open),
        "high_open": bool(high_open),
        "flat": flat, "identifiable": bool(identifiable),
        "weakly_identifiable": bool((not flat) and (not identifiable)
                                    and (ci_low is not None or ci_high is not None)),
        "unreliable": unreliable,
        "wide": wide,
        "search_lo": float(xs[0]), "search_hi": float(xs[-1]),
        "max_dnll": max_dnll,
    }


def _target_label(target: dict) -> str:
    k = target["kind"]
    if k == "life":
        return f"寿命 L{target.get('eol', 80):g}"
    return {"tau": "转折点 τ", "b2": "二阶段速率 b2", "b1": "一阶段速率 b1",
            "a": "初始水平 a", "delta": f"仪器偏移 δ({target.get('group')})",
            "sigma": "测量标准差 σ"}.get(k, k)


def _target_unit(target: dict) -> str:
    k = target["kind"]
    if k in ("life", "tau"):
        return "时间单位"
    if k in ("b1", "b2"):
        return "y/时间"
    return "y"


def _target_standard_error(fit: dict, lay: Layout, fval,
                           theta_hat: np.ndarray) -> float | None:
    """用 Hessian 协方差 + 目标函数梯度估计目标参数 SE，用于网格加密。"""
    try:
        cov = np.array(fit["hessian_se"]["cov"], dtype=float)
        if cov.shape[0] != lay.n_params:
            return None
        g = _constraint_jac(lay, fval, theta_hat, eps=1e-5)
        var = float(g @ cov @ g)
        if not np.isfinite(var) or var <= 0:
            return None
        return float(np.sqrt(var))
    except Exception:
        return None


def _profile_bounds(target: dict, x_hat: float, lay: Layout) -> tuple[float, float]:
    tmax = float(np.max(lay.t))
    kind = target["kind"]
    if kind == "life":
        s = max(0.35 * max(abs(x_hat), tmax, 20.0), 8.0)
        lo = max(0.0, min(x_hat - 1.1 * s, tmax - 0.5 * s))
        hi = max(x_hat + 2.4 * s, tmax + 2.0 * s, 60.0)
        hi = min(hi, 220.0)
        if hi <= lo:
            hi = lo + 4 * s
        return lo, hi
    if kind == "tau":
        return 0.0, min(max(tmax * 1.9, tmax + 25.0, x_hat + 10.0), 200.0)
    if kind == "b2":
        return 0.0, max(6.0, 1.8 * abs(x_hat) + 0.5)
    if kind == "b1":
        return 0.0, max(3.0, 1.8 * abs(x_hat) + 0.2)
    if kind == "a":
        return 85.0, 112.0
    if kind == "delta":
        return -8.0, 8.0
    if kind == "sigma":
        return 0.05, max(3.0, 2.0 * abs(x_hat))
    return min(0.0, 0.5 * x_hat), 1.5 * x_hat + 1e-6


def _profile_grid(target: dict, x_hat: float, lay: Layout, n_total: int,
                  target_se: float | None = None) -> np.ndarray:
    """宽边界 + MLE 附近按估计 SE 加密的非均匀网格。"""
    lo, hi = _profile_bounds(target, x_hat, lay)
    # 中心密网格：步长 ~0.4 SE，覆盖 ±5 SE（至少保证 11 个中心点）
    if target_se and np.isfinite(target_se) and 1e-5 < target_se < (hi - lo):
        step = max(0.35 * target_se, (hi - lo) / 400.0)
        half = min(6.0 * target_se, 0.45 * (hi - lo))
        clo = max(lo, x_hat - half)
        chi = min(hi, x_hat + half)
        fine = np.arange(clo, chi + 0.5 * step, step)
        if len(fine) < 9:  # SE 太小时强制足够密
            fine = np.linspace(max(lo, x_hat - 4 * target_se),
                               min(hi, x_hat + 4 * target_se), 13)
        outer_lo = np.linspace(lo, fine[0], 7)[:-1]
        outer_hi = np.linspace(fine[-1], hi, 9)[1:]
        xs = np.unique(np.concatenate([outer_lo, fine, outer_hi]))
    else:
        xs = np.linspace(lo, hi, int(max(n_total, 15)))
    # 保证 x_hat 附近有点
    if np.min(np.abs(xs - x_hat)) > (hi - lo) / (2 * n_total):
        xs = np.unique(np.sort(np.append(xs, x_hat)))
    return xs


def _refine_bound(lay, bounds, fval, xs, dnlls, thetas, side: str,
                  cut: float, nll_min: float, iters: int = 7):
    """在 dnll 跨越 cut 的相邻网格点间二分，返回边界 x；无交点返回 None。"""
    n = len(xs)
    idx = None
    if side == "low":
        for i in range(1, n):
            if dnlls[i - 1] > cut and dnlls[i] <= cut:
                idx = i
                break
        if idx is None:
            return None
        x_out, x_in = float(xs[idx - 1]), float(xs[idx])
        th_warm = thetas[idx] if thetas[idx] is not None else thetas[idx - 1]
    else:
        for i in range(n - 2, -1, -1):
            if dnlls[i] <= cut and dnlls[i + 1] > cut:
                idx = i
                break
        if idx is None:
            return None
        x_in, x_out = float(xs[idx]), float(xs[idx + 1])
        th_warm = thetas[idx] if thetas[idx] is not None else thetas[idx + 1]
    if th_warm is None:
        return (x_in + x_out) / 2
    a, b = x_out, x_in  # a: dnll>cut 一侧, b: <=cut 一侧
    for _ in range(iters):
        m = 0.5 * (a + b)
        th, nll_p, ok = _profile_point(lay, th_warm, bounds, fval, float(m))
        if ok:
            th_warm = th
        d = max(0.0, nll_p - nll_min)
        if d > cut:
            a = m
        else:
            b = m
    return 0.5 * (a + b)


def _target_functions(lay: Layout, target: dict):
    kind = target["kind"]
    if kind == "life":
        cell = target["cell"]
        eol = float(target.get("eol", 80.0))

        def f(th):
            return lay.life_of_cell(th, cell, eol)
        return f, None

    group = target.get("group")

    def f(th):
        pp = lay.physical_params(th)
        if kind == "delta":
            return pp["delta"].get(group, 0.0)
        if kind == "sigma":
            return pp["sigma"]
        return pp[kind][group]
    return f, None


def _constraint_jac(lay: Layout, f_target, theta, eps: float = 1e-5):
    """轮廓目标 f_target(theta) 的中心差分数值梯度。"""
    theta = np.asarray(theta, dtype=float)
    g = np.zeros(lay.n_params)
    for j in range(lay.n_params):
        h = eps * max(1.0, abs(theta[j]))
        tp = theta.copy(); tp[j] += h
        tm = theta.copy(); tm[j] -= h
        g[j] = (f_target(tp) - f_target(tm)) / (2 * h)
    return g


def _profile_point(lay, theta_warm, bounds, f_target, x):
    """在 f_target(theta)=x 约束下最小化 nll。SLSQP 等式约束 + 惩罚后备。"""
    cons = ({"type": "eq",
             "fun": lambda th: f_target(th) - x,
             "jac": lambda th: _constraint_jac(lay, f_target, th)},)

    def obj(th):
        return nll_and_grad(th, lay, need_grad=True)

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            opt = minimize(obj, theta_warm, jac=True, method="SLSQP",
                           bounds=bounds, constraints=cons,
                           options={"maxiter": 200, "ftol": 1e-9})
        err = abs(f_target(opt.x) - x)
        if np.isfinite(opt.fun) and err < max(1e-3, 0.01 * abs(x)):
            return opt.x, float(opt.fun), True
    except Exception:
        pass

    # 后备：增广二次惩罚，逐渐加大 rho
    th = theta_warm.copy()
    nll_best = np.inf
    for rho in (1e2, 1e4, 1e6):
        def penalized(thv):
            f0, g0 = nll_and_grad(thv, lay, True)
            e = f_target(thv) - x
            ge = _constraint_jac(lay, f_target, thv)
            return f0 + rho * e * e, g0 + 2 * rho * e * ge
        try:
            opt = minimize(penalized, th, jac=True, method="L-BFGS-B",
                           bounds=bounds,
                           options={"maxiter": 600, "ftol": 1e-12})
            th = opt.x
            err = abs(f_target(th) - x)
            nll_best = neg_log_likelihood(th, lay)
            if err < max(2e-3, 0.02 * abs(x)):
                return th, float(nll_best), True
        except Exception:
            continue
    if np.isfinite(nll_best):
        return th, float(nll_best), False
    return None, np.inf, False


# ---------------------------------------------------------------------------
# 证据覆盖 / 外推评估
# ---------------------------------------------------------------------------

def extrapolation_assessment(rows: list[dict], fit: dict,
                             cell: str, eol: float = 80.0,
                             profile_life: dict | None = None) -> dict[str, Any]:
    tmax = max(float(r["t"]) for r in rows)
    cell_rows = [r for r in rows if str(r["cell"]) == cell]
    min_y = min(float(r["y"]) for r in cell_rows) if cell_rows else np.nan
    crossed_observed = min_y <= eol

    theta = np.array(fit["theta"]) if fit.get("theta") else None
    L = Layout(rows, fit["layout_meta"]["scopes"],
               {"instrument_offset": fit["layout_meta"]["offset_enabled"],
                "reference_instrument": fit["layout_meta"]["reference_instrument"]}) \
        if theta is not None else None
    l_hat = L.life_of_cell(theta, cell, eol) if L else None

    ci_lo = profile_life.get("ci_low") if profile_life else None
    ci_hi = profile_life.get("ci_high") if profile_life else None
    high_open = profile_life.get("high_open") if profile_life else False

    if l_hat is None:
        level = "fit_unavailable"
        text = "拟合不可用，无法评估寿命外推。"
    elif crossed_observed and l_hat <= tmax + 1e-6:
        level = "observed"
        text = "EOL 阈值已在观测数据中被穿过，寿命估计有直接证据支持。"
    elif l_hat <= tmax * 1.25 + 2:
        level = "mild"
        text = (f"寿命估计 {l_hat:.1f} 略晚于最晚检测时间 {tmax:.0f}，"
                "属于短期外推，仍需后续检测确认。")
    elif l_hat <= tmax * 2.0:
        level = "strong"
        text = (f"寿命估计 {l_hat:.1f} 明显晚于证据边界 {tmax:.0f}，"
                "属于较强外推，不能作为确定结论。")
    else:
        level = "speculative"
        text = (f"寿命估计 {l_hat:.1f} 超过证据边界 {tmax:.0f} 两倍以上，"
                "主要依赖模型形式假设，只能作为待检验的外推情景。")
    if high_open:
        text += " 轮廓似然置信区间上界开放：现有数据不能排除更长寿命。"
    return {
        "level": level, "text": text, "t_max_observed": tmax,
        "life_hat": l_hat, "ci_low": ci_lo, "ci_high": ci_hi,
        "high_open": high_open, "eol_crossed_in_data": crossed_observed,
    }
