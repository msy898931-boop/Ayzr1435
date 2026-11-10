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
        # The frame tilt + EMA20 rule in isolation: the quality gate is turned
        # off here so this test pins ONE rule (the gate has its own tests).
        rising = list(range(4000, 4060))
        closes = {tf: rising for tf in bot.FRAMES}
        saved = bot.FILTERS_ON
        bot.FILTERS_ON = False
        try:
            with FakeFeed(mid=4059.0, closes=closes):
                p = bot.plan()
                self.assertIsNotNone(p)
                self.assertEqual(p["direction"], "long")
                self.assertEqual(p["up"], len(bot.FRAMES))
        finally:
            bot.FILTERS_ON = saved

    def test_frame_agrees_fails_open_on_a_missing_frame(self):
        # A broken feed must not stop the desk trading entirely: the filter is a
        # guard, and a guard that fails closed is a desk that goes silent.
        with FakeFeed(closes={"15m": []}):
            self.assertTrue(bot.frame_agrees("long", 1.0))


class FilterStatusTest(unittest.TestCase):
    """The combo's quality gate: four bands on the 15m frame. Every gate fails
    OPEN on a missing reading, the default set is the two that paid (adx+atr),
    and the gate as a whole must block a plan its own bands would refuse."""

    def setUp(self):
        self._gates = bot.FILTER_GATES
        bot.FILTER_GATES = {"adx", "atr", "macd", "rsi"}

    def tearDown(self):
        bot.FILTER_GATES = self._gates

    def _bars(self, closes, spread=0.0):
        out = []
        for i, c in enumerate(closes):
            out.append({"openTime": _iso(i), "open": c, "high": c + spread,
                        "low": c - spread, "close": c, "isOpen": False})
        return out

    def test_missing_frame_fails_open(self):
        fs = bot.filter_status("long", [])
        self.assertTrue(fs["ok"])
        self.assertEqual(fs["failed"], [])

    def test_default_gate_set_is_adx_and_atr(self):
        # The two that helped on both the study and the bot's own replay.
        self.assertEqual(bot.DEFAULT_GATES, {"adx", "atr"})

    def test_a_disabled_gate_never_blocks(self):
        bot.FILTER_GATES = {"adx"}
        # A relentless rise pins RSI high and the MACD histogram flat, but with
        # only ADX live neither can hold the trade back.
        closes = [4000 * (1.01 ** i) for i in range(120)]
        fs = bot.filter_status("long", self._bars(closes, spread=0.5))
        self.assertNotIn("RSI", fs["failed"])
        self.assertNotIn("MACD", fs["failed"])

    def test_a_strong_trend_passes_the_adx_band(self):
        # A clean staircase: ADX well above 20, ATR% inside the band.
        closes = [4000 + i * 2 for i in range(120)]
        fs = bot.filter_status("long", self._bars(closes, spread=1.5))
        self.assertIsNotNone(fs["readings"]["adx"])
        self.assertGreaterEqual(fs["readings"]["adx"], bot.ADX_MIN)

    def test_rsi_out_of_band_blocks_a_long(self):
        # A relentless rise pins RSI high; the long band is 35-58.
        closes = [4000 * (1.01 ** i) for i in range(120)]
        fs = bot.filter_status("long", self._bars(closes, spread=0.5))
        self.assertIn("RSI", fs["failed"])
        self.assertFalse(fs["ok"])

    def test_macd_must_turn_the_entrys_way(self):
        # An accelerating rise lifts the histogram, so the long's MACD gate
        # (histogram rising) is satisfied.
        closes = [4000 + i * i * 0.05 for i in range(120)]
        fs = bot.filter_status("long", self._bars(closes, spread=1.0))
        self.assertNotIn("MACD", fs["failed"])

    def test_gate_blocks_plan_but_toggle_restores_raw_tilt(self):
        # Six frames up, price above EMA20, but a dead-flat tape (ATR% ~ 0)
        # fails the ATR band -> no plan while the gate is on.
        rising = list(range(4000, 4060))
        closes = {tf: rising for tf in bot.FRAMES}
        with FakeFeed(mid=4059.0, closes=closes):
            self.assertIsNone(bot.plan())
            saved = bot.FILTERS_ON
            bot.FILTERS_ON = False
            try:
                self.assertIsNotNone(bot.plan())
            finally:
                bot.FILTERS_ON = saved


class IndicatorTest(unittest.TestCase):
    """The series helpers the gate leans on: alignment and warm-up."""

    def test_ema_series_matches_ema_at_the_tail(self):
        vals = [4000 + i for i in range(80)]
        s = bot.ema_series(vals, 20)
        self.assertEqual(s[-1], bot.ema(vals, 20))

    def test_adx_series_is_none_before_warmup(self):
        bars = [{"openTime": _iso(i), "open": 4000 + i, "high": 4001 + i,
                 "low": 3999 + i, "close": 4000 + i, "isOpen": False}
                for i in range(60)]
        a = bot.adx_series(bars)
        self.assertIsNone(a[0])
        self.assertIsNotNone(a[-1])
        self.assertGreaterEqual(a[-1], 0.0)

    def test_macd_hist_series_positive_on_a_rising_tape(self):
        bars = [{"openTime": _iso(i), "open": 4000 + i, "high": 4001 + i,
                 "low": 3999 + i, "close": 4000 + i, "isOpen": False}
                for i in range(120)]
        h = bot.macd_hist_series(bars)
        self.assertIsNotNone(h[-1])
        self.assertGreater(h[-1], 0)


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


class TokenResolutionTest(unittest.TestCase):
    """A token is only a token if getMe reaches the bot. A stale env value is a
    non-empty string that 404s on every send -- the exact bug that made every
    read look fine while the bot was silent. token() must skip it."""

    def setUp(self):
        self._env = dict(os.environ)
        self._ident = bot.token_identity
        self._path = bot.TOKEN_PATH
        # No token file: force the environment path.
        bot.TOKEN_PATH = bot.Path(os.path.join(tempfile.mkdtemp(), ".tg_token"))

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        bot.token_identity = self._ident
        bot.TOKEN_PATH = self._path

    def test_a_dead_candidate_is_skipped_for_a_live_one(self):
        os.environ.pop("TG_TOKEN", None)
        os.environ["TELEGRAM_BOT_TOKEN"] = "dead-value"
        bot.token_identity = lambda t: "Ayzr1324_bot" if t == "live-value" else ""
        bot.TOKEN_PATH.write_text("live-value\n")
        self.assertEqual(bot.token(), "live-value")

    def test_the_dead_env_value_alone_does_not_win(self):
        # Only the dead value exists: token() still returns it (so the bot is
        # not left tokenless) but token_identity is what reports the truth.
        os.environ.pop("TG_TOKEN", None)
        os.environ["TELEGRAM_BOT_TOKEN"] = "dead-value"
        bot.token_identity = lambda t: ""
        self.assertEqual(bot.token(), "dead-value")
        self.assertEqual(bot.token_identity(bot.token()), "")

    def test_the_file_wins_over_the_environment(self):
        os.environ["TG_TOKEN"] = "env-value"
        bot.token_identity = lambda t: "Ayzr1324_bot" if t == "file-value" else ""
        bot.TOKEN_PATH.write_text("file-value\n")
        self.assertEqual(bot.token(), "file-value")

    def test_token_state_reports_dead_not_ok(self):
        # The bug this guards: a dead token printed "ok" because the check only
        # asked "is there a string?". token_state must answer the real question.
        os.environ.pop("TG_TOKEN", None)
        os.environ["TELEGRAM_BOT_TOKEN"] = "dead-value"
        bot.token_identity = lambda t: ""
        self.assertEqual(bot.token_state(),
                         "DEAD (has a value, but getMe fails)")

    def test_token_state_reports_ok_with_the_identity(self):
        os.environ["TG_TOKEN"] = "live-value"
        bot.token_identity = lambda t: "Ayzr1324_bot"
        self.assertEqual(bot.token_state(), "ok (@Ayzr1324_bot)")

    def test_token_state_reports_missing_when_nothing_is_set(self):
        os.environ.pop("TG_TOKEN", None)
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        bot.token_identity = lambda t: ""
        self.assertEqual(bot.token_state(), "MISSING")


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


class LevelsWatchTest(unittest.TestCase):
    """The multi-level crossing watch: speaks once per crossing, never spams."""

    def test_first_call_records_sides_silently(self):
        with FakeFeed(mid=4120.0):
            seen: dict = {}
            self.assertIsNone(bot.levels_watch(seen))
        # 4120 is above 4096/4084/4070/4055 and below 4119? no: above 4119.
        self.assertEqual(seen["levels"]["4119.00"], "above")
        self.assertEqual(seen["levels"]["4132.00"], "below")

    def test_a_break_speaks_once_then_stays_quiet(self):
        with FakeFeed(mid=4120.0):
            seen: dict = {}
            bot.levels_watch(seen)  # record baseline
            # price rises through 4132 (needs >= 4133 to clear the band)
            with FakeFeed(mid=4135.0):
                note = bot.levels_watch(seen)
            self.assertIsNotNone(note)
            self.assertIn("4,132", note)
            self.assertIn("اختراق", note)
            # still above: no repeat
            with FakeFeed(mid=4140.0):
                self.assertIsNone(bot.levels_watch(seen))

    def test_a_fall_speaks_and_names_the_level(self):
        with FakeFeed(mid=4120.0):
            seen: dict = {}
            bot.levels_watch(seen)
            with FakeFeed(mid=4090.0):  # drops under 4096 (needs <= 4095)
                note = bot.levels_watch(seen)
            self.assertIsNotNone(note)
            self.assertIn("4,096", note)
            self.assertIn("كسر", note)

    def test_hovering_on_a_level_does_not_flicker(self):
        with FakeFeed(mid=4132.0):
            seen: dict = {}
            bot.levels_watch(seen)  # baseline sits exactly on the level
            # a nudge of half a point stays inside the band: no event
            with FakeFeed(mid=4132.4):
                self.assertIsNone(bot.levels_watch(seen))
            with FakeFeed(mid=4131.6):
                self.assertIsNone(bot.levels_watch(seen))

    def test_two_levels_broken_together_speak_in_one_message(self):
        with FakeFeed(mid=4100.0):
            seen: dict = {}
            bot.levels_watch(seen)
            with FakeFeed(mid=4060.0):  # under 4096, 4084, 4070 -- not yet 4055
                note = bot.levels_watch(seen)
            self.assertIsNotNone(note)
            for lvl in ("4,096", "4,084", "4,070"):
                self.assertIn(lvl, note)

    def test_a_missing_feed_returns_none_and_does_not_raise(self):
        saved = bot.read_market
        bot.read_market = lambda: (_ for _ in ()).throw(RuntimeError("no feed"))
        try:
            self.assertIsNone(bot.levels_watch({}))
        finally:
            bot.read_market = saved

    def test_a_level_that_lands_on_the_latched_support_stays_quiet(self):
        """support_watch owns the latched support; levels_watch must not echo it."""
        with FakeFeed(mid=4090.0):
            seen: dict = {"support_level": 4096.0}
            bot.levels_watch(seen)          # baseline, both sides recorded
            with FakeFeed(mid=4080.0):      # drops under the fixed 4096 and 4084
                note = bot.levels_watch(seen)
            # 4096 is the latched support -> left to support_watch, so it is
            # absent; 4084 is a separate level and still speaks.
            self.assertIsNotNone(note)
            self.assertNotIn("4,096", note)
            self.assertIn("4,084", note)
            self.assertEqual(seen["levels"]["4096.00"], "below")


if __name__ == "__main__":
    unittest.main(verbosity=2)
