"""引擎核心测试：模型反解、参数恢复、删失处理、不可分辨性、设计与增益闭环。

运行：cd /workspace && python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.engine.model import curve, life_time, Layout  # noqa: E402
from backend.engine.datasets import SCENARIOS, parse_csv  # noqa: E402
from backend.engine.fit import fit_model, profile_likelihood  # noqa: E402
from backend.engine import design as D  # noqa: E402
from backend.engine.session import Session  # noqa: E402


class TestModelMath(unittest.TestCase):
    def test_continuity_at_tau(self):
        a, b1, b2, tau = 99.0, 0.3, 1.5, 24.0
        left = curve(tau - 1e-9, a, b1, b2, tau)
        right = curve(tau + 1e-9, a, b1, b2, tau)
        self.assertAlmostEqual(float(left), float(right), places=6)
        self.assertAlmostEqual(float(left), a - b1 * tau, places=6)

    def test_life_inversion_stage2(self):
        a, b1, b2, tau, eol = 100.0, 0.4, 2.0, 20.0, 80.0
        L = life_time(a, b1, b2, tau, eol)
        self.assertGreater(L, tau)
        self.assertAlmostEqual(float(curve(L, a, b1, b2, tau)), eol, places=8)

    def test_life_inversion_stage1(self):
        a, b1, b2, tau, eol = 100.0, 1.0, 3.0, 60.0, 80.0
        L = life_time(a, b1, b2, tau, eol)  # =20 < tau
        self.assertLess(L, tau)
        self.assertAlmostEqual(float(curve(L, a, b1, b2, tau)), eol, places=8)

    def test_layout_scope_group_counts(self):
        rows = SCENARIOS["standard"]["rows"]
        lay = Layout(rows, {"a": "cell", "b1": "batch",
                            "b2": "global", "tau": "batch"},
                     {"instrument_offset": False})
        self.assertEqual(len(lay.groups["a"]), 9)
        self.assertEqual(len(lay.groups["b1"]), 3)
        self.assertEqual(len(lay.groups["b2"]), 1)
        self.assertEqual(len(lay.groups["tau"]), 3)


class TestCSVParsing(unittest.TestCase):
    def test_censor_syntax_and_chinese_headers(self):
        csv_text = (
            "批次,单体,时间,读数,检测仪\n"
            "B1,C1,0,99.1,A\n"
            "B1,C1,10,\">=100\",A\n"
            "B1,C1,20,<65,A\n"
        )
        out = parse_csv(csv_text)
        self.assertEqual(out["errors"], [])
        rows = out["rows"]
        self.assertEqual(rows[1]["cens"], 1)
        self.assertEqual(rows[1]["limit"], 100.0)
        self.assertEqual(rows[2]["cens"], -1)
        self.assertEqual(rows[2]["limit"], 65.0)

    def test_missing_required_column(self):
        out = parse_csv("batch,t\nB1,1\n")
        self.assertTrue(out["errors"])


def _fit(sid, offset=None):
    scn = SCENARIOS[sid]
    insts = sorted({r["instrument"] for r in scn["rows"]})
    if offset is None:
        offset = len(insts) > 1
    opts = {"instrument_offset": offset, "reference_instrument": insts[0]}
    return scn, opts, fit_model(scn["rows"], scn["default_scopes"], opts)


class TestParameterRecovery(unittest.TestCase):
    def test_standard_recovers_batch_truth(self):
        scn, opts, fit = _fit("standard", offset=False)
        self.assertTrue(fit["ok"])
        tau = fit["estimates"]["tau"]
        self.assertAlmostEqual(tau["batch:BAT-A"], 24.0, delta=2.0)
        self.assertAlmostEqual(tau["batch:BAT-B"], 30.0, delta=2.5)
        self.assertAlmostEqual(tau["batch:BAT-C"], 20.0, delta=2.0)
        b2 = fit["estimates"]["b2"]
        self.assertAlmostEqual(b2["batch:BAT-C"], 2.1, delta=0.4)
        self.assertAlmostEqual(b2["batch:BAT-B"], 1.05, delta=0.3)

    def test_instrument_offset_recovered(self):
        scn, opts, fit = _fit("instrument_swap", offset=True)
        self.assertTrue(fit["ok"])
        delta = fit["estimates"]["delta"]["INST-B"]
        self.assertAlmostEqual(delta, -2.4, delta=0.6)
        tau = fit["estimates"]["tau"]["batch:BAT-I"]
        self.assertAlmostEqual(tau, 29.0, delta=2.0)

    def test_offset_model_beats_no_offset_on_swap_data(self):
        scn = SCENARIOS["instrument_swap"]
        insts = sorted({r["instrument"] for r in scn["rows"]})
        on = fit_model(scn["rows"], scn["default_scopes"],
                        {"instrument_offset": True,
                         "reference_instrument": insts[0]})
        off = fit_model(scn["rows"], scn["default_scopes"],
                        {"instrument_offset": False,
                         "reference_instrument": insts[0]})
        self.assertLess(on["nll"], off["nll"] - 3.0)

    def test_fit_failure_scenario_reports_hard_fail(self):
        scn, opts, fit = _fit("fit_failure")
        self.assertFalse(fit["ok"])
        self.assertTrue(fit["hard_fail"])
        self.assertTrue(fit["feasibility"])


class TestProfiles(unittest.TestCase):
    def test_knee_unobserved_tau_flat(self):
        scn, opts, fit = _fit("knee_unobserved", offset=False)
        p = profile_likelihood(
            scn["rows"], scn["default_scopes"], opts, fit,
            {"kind": "tau", "group": "batch:K1"}, n_grid=13)
        self.assertTrue(p["available"])
        self.assertTrue(p["flat"])
        self.assertFalse(p["identifiable"])

    def test_observed_knee_tau_finite_ci(self):
        scn, opts, fit = _fit("standard", offset=False)
        p = profile_likelihood(
            scn["rows"], scn["default_scopes"], opts, fit,
            {"kind": "tau", "group": "batch:BAT-A"}, n_grid=13)
        self.assertTrue(p["available"])
        self.assertIsNotNone(p["ci_low"])
        self.assertIsNotNone(p["ci_high"])
        self.assertLess(p["ci_high"] - p["ci_low"], 6.0)

    def test_life_profile_returns_grid(self):
        scn, opts, fit = _fit("standard", offset=False)
        p = profile_likelihood(
            scn["rows"], scn["default_scopes"], opts, fit,
            {"kind": "life", "cell": "B01", "eol": 80}, n_grid=13)
        self.assertGreater(len(p["grid"]), 10)
        self.assertFalse(p["unreliable"])


class TestDesignWeights(unittest.TestCase):
    def test_uncensored_weight(self):
        self.assertAlmostEqual(
            D.censor_info_weight(90.0, 0.7, None, None),
            1 / 0.49, places=5)

    def test_heavy_censor_has_less_info(self):
        w_exact = D.censor_info_weight(100.0, 0.7, None, None)
        w_cens = D.censor_info_weight(101.5, 0.7, None, 100.0)
        self.assertLess(w_cens, 0.5 * w_exact)

    def test_later_stage2_preferred_for_knee(self):
        scn, opts, fit = _fit("knee_unobserved", offset=False)
        rank = D.rank_windows(
            scn["rows"], scn["default_scopes"], opts, fit, "K1-1", 80,
            scn["windows"], 0.0, 100.4)
        # 最晚窗口应排在最前（最能识别阶段二）
        self.assertEqual(rank["top_window_id"], "W4")


class TestSessionLoop(unittest.TestCase):
    def test_recommend_accept_narrows_and_traces(self):
        s = Session()
        s.load_scenario("standard")
        rec = s.create_recommendation({"cell": "B01", "n": 1})
        self.assertNotIn("error", rec)
        rid = rec["recommendation"]["id"]
        before_w = rec["recommendation"]["width_profile_before"]
        self.assertIsNotNone(before_w)
        out = s.accept_recommendation(rid, {})
        self.assertTrue(out.get("ok"), out.get("message"))
        r = out["rec"]
        self.assertEqual(r["status"], "accepted")
        self.assertEqual(len(r["synthetic_rows"]), 1)
        self.assertIsNotNone(r["outcome"]["actual_reduction_frac"])
        # 原始预测被保留
        self.assertIn("ranking", r)
        self.assertIsNotNone(r["outcome"]["predicted_width_after"])
        # 预算计数
        self.assertEqual(s.used_samples, 1)

    def test_budget_blocks_extra_accepts(self):
        s = Session()
        s.load_scenario("fit_failure")
        rec = s.create_recommendation({})
        self.assertEqual(rec.get("error"), "FIT_UNAVAILABLE")

    def test_knee_accept_late_point(self):
        s = Session()
        s.load_scenario("knee_unobserved")
        rec = s.create_recommendation({"cell": "K1-1", "n": 1})
        rid = rec["recommendation"]["id"]
        top_t = rec["recommendation"]["ranking"]["top_t"]
        out = s.accept_recommendation(rid, {})
        self.assertTrue(out.get("ok"))
        self.assertGreaterEqual(top_t, 40.0)
        self.assertEqual(out["rec"]["chosen"]["t"], top_t)


if __name__ == "__main__":
    unittest.main(verbosity=2)
