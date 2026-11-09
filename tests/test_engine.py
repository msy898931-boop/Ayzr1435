"""Tests for the MarketDesk engine (bot.py). Pure stdlib, no network.

Run:  python3 -m unittest discover -s tests -v
      python3 tests/test_engine.py

Every test here stubs the two network functions (`quote`, `bars`) so the suite
is deterministic and offline. The rules under test -- sizing, the plan gate, the
trend filter, the verdict, grading -- are the ones that decide real money, so
they are worth pinning: a change that breaks one of them must fail here first.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot  # noqa: E402


def _bar(open_time, o, h, lo, c):
    """A bar whose openTime is the ISO string the feed returns."""
    return {"openTime": open_time, "open": o, "high": h, "low": lo,
            "close": c, "isOpen": False}


def _iso(i):
    return f"2026-11-07T{i // 60:02d}:{i % 60:02d}:00Z"


class FakeFeed:
    """Install deterministic quote/bars in place of the network, and undo on
    exit. Bars are built from a per-frame close so trends are predictable."""

    def __init__(self, mid=4100.0, market="open", stale=False, closes=None):
        self.mid, self.market, self.stale = mid, market, stale
        self.closes = closes or {}
        self._saved = (bot.quote, bot.bars)

    def _quote(self):
        return {"mid": self.mid, "marketState": self.market, "stale": self.stale}

    def _bars(self, tf, limit=300):
        closes = self.closes.get(tf)
        if closes is None:
            # A flat line: no pivots, no trend -- a neutral default.
            closes = [self.mid] * 30
        return [_bar(_iso(i), c, c + 1, c - 1, c) for i, c in enumerate(closes)]

    def __enter__(self):
        bot.quote, bot.bars = self._quote, self._bars
        return self

    def __exit__(self, *exc):
        bot.quote, bot.bars = self._saved


class SizingTest(unittest.TestCase):
    """_sized is the desk's single set of rules: risk inside the $8-10 window,
    target a risk MULTIPLE -- so R:R can never fall under the floor."""

    def test_default_risk_sits_in_the_owner_window(self):
        p = bot._sized(4100.0, "long", None)
        self.assertGreaterEqual(p["risk_usd"], bot.STOP_MIN_USD - 1e-9)
        self.assertLessEqual(p["risk_usd"], bot.STOP_MAX_USD + 1e-9)
        self.assertGreaterEqual(p["rr"], bot.MIN_RR)

    def test_stop_level_tightens_but_never_leaves_the_window(self):
        # A support one point away must not pull the stop inside $8 of risk.
        p = bot._sized(4100.0, "long", 4099.0)
        self.assertGreaterEqual(p["risk_usd"], bot.STOP_MIN_USD - 1e-9)
        self.assertLessEqual(p["risk_usd"], bot.STOP_MAX_USD + 1e-9)

    def test_direction_flips_stop_and_target_sides(self):
        long = bot._sized(4100.0, "long", None)
        short = bot._sized(4100.0, "short", None)
        self.assertLess(long["stop"], long["entry"])
        self.assertGreater(long["target"], long["entry"])
        self.assertGreater(short["stop"], short["entry"])
        self.assertLess(short["target"], short["entry"])

    def test_rr_is_never_below_the_floor(self):
        for direction in ("long", "short"):
            for stop in (None, 4099.0, 4085.0, 4000.0):
                p = bot._sized(4100.0, direction, stop)
                self.assertGreaterEqual(p["rr"], bot.MIN_RR)


class PlanGateTest(unittest.TestCase):
    """plan() is the one decision engine. It must refuse a closed market, a
    split tape, and a long taken under the filter frame's EMA20."""

    def test_no_plan_when_market_closed(self):
        with FakeFeed(market="closed"):
            self.assertIsNone(bot.plan())

    def test_no_plan_when_frames_split(self):
        # A gently alternating tape: no frame agrees enough to reach 4/6.
        with FakeFeed(closes={tf: [4100, 4101, 4100, 4101] * 8
                              for tf in bot.FRAMES}):
            self.assertIsNone(bot.plan())

    def test_long_requires_price_above_the_filter_ema(self):
        # Six frames all up, but the live price sits UNDER the 15m EMA20.
        rising = list(range(4000, 4060))
        closes = {tf: rising for tf in bot.FRAMES}
        with FakeFeed(mid=4000.0, closes=closes):
            self.assertFalse(bot.frame_agrees("long", 4000.0))
            self.assertIsNone(bot.plan())

    def test_a_confirmed_long_with_the_filter_passes(self):
        rising = list(range(4000, 4060))
        closes = {tf: rising for tf in bot.FRAMES}
        with FakeFeed(mid=4059.0, closes=closes):
            p = bot.plan()
            self.assertIsNotNone(p)
            self.assertEqual(p["direction"], "long")
            self.assertEqual(p["up"], len(bot.FRAMES))

    def test_frame_agrees_fails_open_on_a_missing_frame(self):
        # A broken feed must not stop the desk trading entirely: the filter is a
        # guard, and a guard that fails closed is a desk that goes silent.
        with FakeFeed(closes={"15m": []}):
            self.assertTrue(bot.frame_agrees("long", 1.0))


class _DbCase(unittest.TestCase):
    """Point the journal at a fresh temp file so tests never touch the real one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved_db = bot.DB_PATH
        bot.DB_PATH = os.path.join(self.tmp, "j.db")
        bot._init_db()

    def tearDown(self):
        bot.DB_PATH = self._saved_db


class VerdictTest(_DbCase):
    """The verdict is decided once and formatted by both cards, so the two can
    never contradict. These pin the shared states."""

    def _market(self, **kw):
        base = {"q": {"marketState": "open", "stale": False}, "live": 4100.0,
                "trends": {tf: 0 for tf in bot.FRAMES}}
        base.update(kw)
        return base

    def test_closed_market_verdict(self):
        v = bot._verdict(self._market(q={"marketState": "closed"}))
        self.assertEqual(v["state"], "closed")

    def test_wait_when_frames_disagree(self):
        v = bot._verdict(self._market())
        self.assertEqual(v["state"], "wait")
        self.assertEqual(v["up"], 0)
        self.assertEqual(v["dn"], 0)

    def test_hold_when_a_trade_is_open(self):
        bot.add_trade("long", 4100, 4091, 4118, bot.LOT, note="t")
        v = bot._verdict(self._market())
        self.assertEqual(v["state"], "hold")
        self.assertEqual(v["trade"]["direction"], "long")

    def test_headline_and_detail_agree_on_the_state(self):
        v = bot._verdict(self._market())
        self.assertIn("استنى", bot._headline(v))
        self.assertTrue(bot._verdict_detail(v).startswith("⏸️"))


class GradeTest(_DbCase):
    """A trade without a stop cannot be graded, so it is not a record. First
    touch wins; a bar that spans both the stop and the target takes the stop."""

    def _open(self, direction="long", entry=4100, stop=4091, target=4118):
        # Backdate the open: grade() only looks at bars at/after opened_at, and
        # the real clock would sit after the fixed bars below.
        tid = bot.add_trade(direction, entry, stop, target, bot.LOT, note="t")
        with bot.closing(bot._conn()) as c:
            c.execute("UPDATE trades SET opened_at=? WHERE id=?",
                      ("2026-01-01T00:00:00Z", tid))
            c.commit()
        return tid

    def _one(self):
        rows = bot.all_trades()
        self.assertEqual(len(rows), 1)
        return rows[0]

    def _grade_with(self, bars):
        with FakeFeed():
            bot.bars = lambda tf, limit=300: bars
            bot.grade()

    def test_stop_is_hit_and_graded_minus_one(self):
        self._open()
        self._grade_with([_bar(_iso(0), 4100, 4101, 4099, 4100),
                          _bar(_iso(1), 4099, 4099, 4090, 4092)])
        t = self._one()
        self.assertEqual(t["status"], "closed")
        self.assertEqual(t["result"], "stop")
        self.assertEqual(t["r_multiple"], -1.0)

    def test_target_is_hit_and_graded_the_r_multiple(self):
        self._open()
        self._grade_with([_bar(_iso(0), 4100, 4102, 4099, 4101),
                          _bar(_iso(1), 4101, 4120, 4101, 4119)])
        t = self._one()
        self.assertEqual(t["result"], "target")
        self.assertAlmostEqual(t["r_multiple"], 2.0, places=6)

    def test_a_bar_spanning_both_takes_the_stop(self):
        # The ambiguous bar must resolve against the trade -- no free win.
        self._open()
        self._grade_with([_bar(_iso(0), 4100, 4120, 4090, 4119)])
        self.assertEqual(self._one()["result"], "stop")

    def test_an_unresolved_trade_stays_open(self):
        self._open()
        self._grade_with([_bar(_iso(0), 4100, 4101, 4099, 4100)])
        self.assertEqual(self._one()["status"], "open")


class ParseTradeTest(unittest.TestCase):
    def test_full_arabic_line(self):
        got = bot.parse_trade("سجل شراء 4075 وقف 4069 هدف 4085")
        self.assertEqual(got["direction"], "long")
        self.assertEqual(got["entry"], 4075.0)
        self.assertEqual(got["stop"], 4069.0)
        self.assertEqual(got["target"], 4085.0)

    def test_missing_stop_is_refused(self):
        self.assertIsInstance(bot.parse_trade("شراء 4075"), str)

    def test_missing_direction_is_refused(self):
        self.assertIsInstance(bot.parse_trade("4075 وقف 4069"), str)


class EntryPointTest(unittest.TestCase):
    """The regression that started this: every host must start the SAME desk.
    bot.py's main() once started only the monitor and never the auto-desk."""

    def test_start_desk_starts_the_desk_and_backup_but_not_the_poller(self):
        import threading
        started = []
        saved = (bot.desk_loop, bot.backup_loop)
        bot.desk_loop = lambda *a, **k: started.append("desk")
        bot.backup_loop = lambda *a, **k: started.append("backup")
        try:
            threads = bot.start_desk("FAKE", threading.Event())
            names = sorted(t.name for t in threads)
        finally:
            bot.desk_loop, bot.backup_loop = saved
        self.assertEqual(sorted(started), ["backup", "desk"])
        self.assertNotIn("poller", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
