"""AYZR's independent engine: pin the things that must never regress.

These tests need NO network -- they exercise the pure functions with hand-built
candles. The point of the file is twofold: prove the engine is genuinely
INDEPENDENT of bot.py, and pin the money rules (a target on the wrong side, a
too-close target, and the round-number chain bug are all real bugs that shipped
once and must not ship again).
"""
import ast
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import ayzr


def _bars(prices: list[float]) -> list[dict]:
    out = []
    for i, p in enumerate(prices):
        out.append({"openTime": f"2026-11-18T{i:02d}:00:00Z", "open": p,
                    "high": p + 1, "low": p - 1, "close": p,
                    "volume": 0, "tickVolume": 1})
    return out


def _zigzag(start: float, step: float, legs: int, up: bool) -> list[float]:
    """A sawtooth with real swing pivots: an impulse leg in the trend direction
    then a smaller pullback, so each successive high AND low is higher (up=True)
    or lower (up=False). A straight line has NO fractal pivots, so a trend test
    built on one would measure nothing.
    """
    prices, p = [], start
    sign = 1 if up else -1
    for _ in range(legs):
        for _ in range(3):
            p += sign * step
            prices.append(p)
        for _ in range(2):
            p -= sign * step * 0.5
            prices.append(p)
    return prices


class IndependenceTest(unittest.TestCase):
    def test_ayzr_does_not_import_bot(self):
        # The whole point: the owner asked for a reader that is his OWN, with no
        # tie to the bot's engine. An import of bot would silently re-couple it.
        src = pathlib.Path(ayzr.__file__).read_text()
        tree = ast.parse(src)
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0])
        self.assertNotIn("bot", mods)


class BuildPlanTest(unittest.TestCase):
    def test_a_short_never_targets_above_entry(self):
        p = ayzr.build_plan(3800.0, "short", 3812.0, [3790.0, 3780.0])
        self.assertLess(p["target"], p["entry"])   # target BELOW entry
        self.assertGreater(p["stop"], p["entry"])  # stop ABOVE entry
        self.assertLess(p["stop"], 3812.0 + 0.01)

    def test_a_long_never_targets_below_entry(self):
        p = ayzr.build_plan(3800.0, "long", 3788.0, [3810.0, 3820.0])
        self.assertGreater(p["target"], p["entry"])
        self.assertLess(p["stop"], p["entry"])

    def test_a_too_close_target_is_dropped(self):
        # A rung 2 points away against a 12-point stop is R:R 0.2 -- a losing
        # trade dressed as a plan. It must be dropped, and the fallback target
        # must clear the R:R floor.
        p = ayzr.build_plan(3800.0, "short", 3812.0, [3798.0])
        self.assertNotIn(3798.0, p["targets"])
        self.assertGreaterEqual(p["rr"], 1.5)

    def test_risk_stays_inside_the_dollar_window(self):
        for stop in (3750.0, 3785.0, 3812.0, 3900.0):
            p = ayzr.build_plan(3800.0, "short", stop, [3700.0])
            self.assertGreaterEqual(p["risk_usd"], ayzr.RISK_MIN - 0.01)
            self.assertLessEqual(p["risk_usd"], ayzr.RISK_MAX + 0.01)


class LevelClusterTest(unittest.TestCase):
    def test_round_numbers_do_not_chain_into_one_band(self):
        # Four round numbers ten apart, tolerance six. They used to merge into
        # ONE band with a fake multi-touch score. A monotonic tape has no swing
        # pivots, so the only candidates here are the extras: each must stand
        # alone at one touch.
        bars = _bars([3800.0 + i for i in range(40)])
        res, sup = ayzr.sr_levels(bars, 6.0, bars[-1]["close"],
                                  extra=[3800.0, 3810.0, 3820.0, 3830.0])
        bands = res + sup
        self.assertTrue(bands)
        self.assertEqual(max(b["touches"] for b in bands), 1,
                         "round numbers chained into a fake multi-touch band")

    def test_band_carries_its_real_range(self):
        bars = _bars(_zigzag(3800.0, 5.0, 10, True))
        res, sup = ayzr.sr_levels(bars, 8.0, bars[-1]["close"])
        for b in res + sup:
            self.assertLessEqual(b["lo"], b["price"])
            self.assertLessEqual(b["price"], b["hi"])


class StructureTest(unittest.TestCase):
    def test_a_rising_staircase_reads_up(self):
        st = ayzr.structure(_bars(_zigzag(3800.0, 5.0, 10, True)))
        self.assertEqual(st["trend"], 1)

    def test_a_falling_staircase_reads_down(self):
        st = ayzr.structure(_bars(_zigzag(3900.0, 5.0, 10, False)))
        self.assertEqual(st["trend"], -1)


class IndicatorsTest(unittest.TestCase):
    def test_rsi_pins_at_100_when_only_gains(self):
        self.assertAlmostEqual(ayzr.rsi([100 + i for i in range(30)], 14),
                               100.0, places=1)

    def test_rsi_pins_at_0_when_only_losses(self):
        self.assertAlmostEqual(ayzr.rsi([100 - i for i in range(30)], 14),
                               0.0, places=1)

    def test_ema_of_a_flat_line_is_that_line(self):
        self.assertAlmostEqual(ayzr.ema([50.0] * 30, 20), 50.0, places=6)


if __name__ == "__main__":
    unittest.main()
