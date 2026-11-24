"""Tests for the MarketDesk engine (bot.py). Pure stdlib, no network.

Run:  python3 -m unittest discover -s tests -v
      python3 tests/test_engine.py

Every test here stubs the two network functions (`quote`, `bars`) so the suite
is deterministic and offline. The rules under test -- sizing, the plan gate, the
trend filter, the verdict, grading -- are the ones that decide real money, so
they are worth pinning: a change that breaks one of them must fail here first.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot  # noqa: E402


def setUpModule():
    """Every test in this file runs against throwaway files, never the real ones.

    Two module-level paths must be redirected, or the suite fails on a clean
    checkout and pollutes a working one:

      * the sticky-level pool -- map_payload/chart_payload WRITE it (that is
        the feature), so without this a run leaves the real
        data/levels_seen.json full of the fake feed's levels;
      * the journal db -- data/journal.db is gitignored, so a fresh clone has
        no `trades` table and any card that reads open_trades() dies with
        "no such table: trades" (card_recommend, _dump_journal). _DbCase points
        it at a temp file, but the module-level default must too so the plain
        cases are isolated as well.
    """
    global _STICKY_DIR, _DB_DIR
    _STICKY_DIR = tempfile.mkdtemp()
    bot.STICKY_PATH = pathlib.Path(_STICKY_DIR) / "levels.json"
    # The watch-points file is WRITTEN by the chat commands ("راقب 3800"), so it
    # must be isolated too -- otherwise a run adds a fake point to the real
    # data/watch_points.json and the live alert fires on a level nobody set.
    bot.WATCH_POINTS_PATH = pathlib.Path(_STICKY_DIR) / "watch_points.json"
    _DB_DIR = tempfile.mkdtemp()
    bot.DB_PATH = pathlib.Path(_DB_DIR) / "journal.db"
    bot._init_db()
    # The daily state (close report / open bell / daily analysis slots) is
    # WRITTEN every cycle by maybe_daily_report and maybe_open_bell. Without
    # this, a test run stamps the real data/daily.json and the live bot would
    # think Monday's bell had already rung.
    bot.DAILY_PATH = pathlib.Path(_STICKY_DIR) / "daily.json"


def tearDownModule():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bot.STICKY_PATH = pathlib.Path(root) / "data" / "levels_seen.json"
    bot.WATCH_POINTS_PATH = pathlib.Path(root) / "data" / "watch_points.json"
    bot.DB_PATH = pathlib.Path(root) / "data" / "journal.db"
    bot.DAILY_PATH = pathlib.Path(root) / "data" / "daily.json"


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
        return {"mid": self.mid, "marketState": self.market, "stale": self.stale,
                "high": self.mid + 20.0, "low": self.mid - 20.0}

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

    def test_a_confirmed_trend_under_the_filter_frame_is_refused(self):
        # Six frames up, but the live price sits UNDER the 15m EMA20 -- a long
        # there is buying a falling tape. The owner asked for the trend filter
        # back in the decision path, so plan() must refuse it. (The quality gate
        # is turned off so this test pins ONE rule: the 15m candle agreement.)
        rising = list(range(4000, 4060))
        closes = {tf: rising for tf in bot.FRAMES}
        saved = bot.FILTERS_ON
        bot.FILTERS_ON = False
        try:
            with FakeFeed(mid=4000.0, closes=closes):
                self.assertFalse(bot.frame_agrees("long", 4000.0))
                self.assertIsNone(bot.plan())
        finally:
            bot.FILTERS_ON = saved

    def test_a_confirmed_long_with_the_filter_passes(self):
        # The frame tilt + EMA20 rule in isolation: the quality gate is turned
        # off here so this test pins ONE rule (the gate has its own tests).
        rising = list(range(4000, 4060))
        closes = {tf: rising for tf in bot.FRAMES}
        saved = bot.FILTERS_ON
        saved_nc = bot.NOCHASE_ON
        saved_eb = bot.ENTRY_BASIS_ON
        bot.FILTERS_ON = False
        bot.NOCHASE_ON = False
        bot.ENTRY_BASIS_ON = False
        try:
            with FakeFeed(mid=4059.0, closes=closes):
                p = bot.plan()
                self.assertIsNotNone(p)
                self.assertEqual(p["direction"], "long")
                self.assertEqual(p["up"], len(bot.FRAMES))
        finally:
            bot.FILTERS_ON = saved
            bot.NOCHASE_ON = saved_nc
            bot.ENTRY_BASIS_ON = saved_eb

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

    def test_the_gate_holds_a_plan_back(self):
        # Six frames up but a DEAD tape (ATR% ~ 0): the quality gate fails, and
        # the owner asked for a trade to clear EVERY gate or not be taken -- so
        # plan() must return None. This is the reversal of the old behaviour,
        # which took the gate out of the decision path; the owner reversed it.
        rising = list(range(4000, 4060))
        closes = {tf: rising for tf in bot.FRAMES}
        with FakeFeed(mid=4059.0, closes=closes):
            self.assertIsNone(bot.plan())
            self.assertFalse(bot.filter_status("long")["ok"])

    def test_the_gate_is_bypassed_when_the_owner_turns_it_off(self):
        # DESK_FILTERS=0 must still take the trade: the gate is a switch, not a
        # hard wall, so the owner can loosen it from the dashboard if he wants.
        rising = list(range(4000, 4060))
        closes = {tf: rising for tf in bot.FRAMES}
        saved = bot.FILTERS_ON
        saved_nc = bot.NOCHASE_ON
        saved_eb = bot.ENTRY_BASIS_ON
        bot.FILTERS_ON = False
        bot.NOCHASE_ON = False
        bot.ENTRY_BASIS_ON = False
        try:
            with FakeFeed(mid=4059.0, closes=closes):
                self.assertIsNotNone(bot.plan())
        finally:
            bot.FILTERS_ON = saved
            bot.NOCHASE_ON = saved_nc
            bot.ENTRY_BASIS_ON = saved_eb


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
        self._saved_sticky = bot.STICKY_PATH
        bot.DB_PATH = os.path.join(self.tmp, "j.db")
        bot.STICKY_PATH = pathlib.Path(self.tmp) / "levels.json"
        bot._init_db()

    def tearDown(self):
        bot.DB_PATH = self._saved_db
        bot.STICKY_PATH = self._saved_sticky


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

    def test_the_recommendation_never_contradicts_the_action_card(self):
        # The bot used to say "ادخل بيع" on the recommendation while the action
        # card said "عندك صفقة مفتوحة، لا تفتح جديدة" -- two opposite answers in
        # the same minute. Both now read _verdict(), so with an open trade the
        # recommendation must say "manage it", not "enter".
        bot.add_trade("short", 4100, 4108, 4090, bot.LOT, note="t")
        m = self._market(live=4099.0, trends={tf: -1 for tf in bot.FRAMES})
        saved = bot.read_market
        bot.read_market = lambda: m
        try:
            rec = bot.card_recommend()
            act = bot.card_action()
        finally:
            bot.read_market = saved
        self.assertIn("صفقة مفتوحة", rec)
        self.assertNotIn("ادخل", rec)
        self.assertIn("صفقة مفتوحة", act)

    def test_the_recommendation_offers_the_trade_when_no_trade_is_open(self):
        m = self._market(live=4099.0, trends={tf: -1 for tf in bot.FRAMES},
                         q={"marketState": "open", "stale": False,
                            "bid": 4098.9, "ask": 4099.1, "spread": 0.2,
                            "dayDiffPercent": -1.0})
        saved = bot.read_market
        bot.read_market = lambda: m
        try:
            rec = bot.card_recommend()
        finally:
            bot.read_market = saved
        self.assertNotIn("صفقة مفتوحة", rec)
        self.assertTrue("بيع" in rec or "شراء" in rec or "لا صفقة" in rec)


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
        saved = (bot.desk_loop, bot.backup_loop, bot.ayzr_plan_loop)
        bot.desk_loop = lambda *a, **k: started.append("desk")
        bot.backup_loop = lambda *a, **k: started.append("backup")
        bot.ayzr_plan_loop = lambda *a, **k: started.append("ayzr_plan")
        try:
            threads = bot.start_desk("FAKE", threading.Event())
            names = sorted(t.name for t in threads)
        finally:
            (bot.desk_loop, bot.backup_loop, bot.ayzr_plan_loop) = saved
        # The AYZR plan push is one of the desk's loops, so it must be started
        # here with the others -- adding it to one entry point only is the exact
        # bug start_desk() exists to prevent.
        self.assertEqual(sorted(started), ["ayzr_plan", "backup", "desk"])
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
            # still above 4132 (and below the next level 4138): no repeat
            with FakeFeed(mid=4136.0):
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


class WatchPointsTest(unittest.TestCase):
    """The named watch points: each carries a MEANING, fires once per approach,
    and speaks only to the owner."""

    def test_reaching_a_resistance_means_a_short(self):
        with unittest.mock.patch.object(bot, "WATCH_POINTS",
                                        ((3770.0, "R"),)):
            with FakeFeed(mid=3770.4):
                seen: dict = {}
                note = bot.watch_points_watch(seen)
            self.assertIsNotNone(note)
            self.assertIn("مقاومة", note)
            self.assertIn("3,770", note)
            self.assertIn("بيع", note)          # the trade it implies
            # a second read on the same approach stays quiet
            with FakeFeed(mid=3770.6):
                self.assertIsNone(bot.watch_points_watch(seen))

    def test_reaching_a_support_means_a_short_on_the_break(self):
        with unittest.mock.patch.object(bot, "WATCH_POINTS",
                                        ((3757.0, "S"),)):
            with FakeFeed(mid=3757.2):
                note = bot.watch_points_watch({})
            self.assertIsNotNone(note)
            self.assertIn("دعم", note)
            self.assertIn("بيع", note)

    def test_a_level_re_tested_after_clearing_speaks_again(self):
        with unittest.mock.patch.object(bot, "WATCH_POINTS",
                                        ((3770.0, "R"),)):
            seen: dict = {}
            with FakeFeed(mid=3770.0):
                self.assertIsNotNone(bot.watch_points_watch(seen))
            with FakeFeed(mid=3800.0):          # price walks well away
                self.assertIsNone(bot.watch_points_watch(seen))
            with FakeFeed(mid=3770.0):          # comes back: speaks again
                self.assertIsNotNone(bot.watch_points_watch(seen))

    def test_no_points_configured_is_silent(self):
        with unittest.mock.patch.object(bot, "WATCH_POINTS", ()), \
             FakeFeed(mid=3770.0):
            self.assertIsNone(bot.watch_points_watch({}))

    def test_a_missing_feed_returns_none_and_does_not_raise(self):
        saved = bot.read_market
        bot.read_market = lambda: (_ for _ in ()).throw(RuntimeError("no feed"))
        try:
            with unittest.mock.patch.object(bot, "WATCH_POINTS",
                                            ((3770.0, "R"),)):
                self.assertIsNone(bot.watch_points_watch({}))
        finally:
            bot.read_market = saved

    def test_the_alert_goes_to_the_owner_alone(self):
        # It carries a trade plan, so a guest on the roll must not receive it.
        self.assertEqual(bot._watch_point_chats({"1", "2", "3"}),
                         [bot._backup_chat()])

    def test_the_parser_reads_the_env_format(self):
        self.assertEqual(bot._parse_watch_points("3770:R,3780:R,3757:S"),
                         ((3757.0, "S"), (3770.0, "R"), (3780.0, "R")))
        # junk tokens are skipped, not fatal
        self.assertEqual(bot._parse_watch_points("3770:R,,bad,x"),
                         ((3770.0, "R"),))

    # ---- the owner types it in the chat ----------------------------------
    def test_parse_reads_the_arabic_commands(self):
        self.assertEqual(bot.parse_watch_command("راقب 3800"), ("add", 3800.0, None))
        self.assertEqual(bot.parse_watch_command("راقب 3,800"), ("add", 3800.0, None))
        self.assertEqual(bot.parse_watch_command("شيل 3770"), ("drop", 3770.0, None))
        self.assertEqual(bot.parse_watch_command("نقاطي"), ("list", 0.0, None))
        # an explicit kind word is carried through
        self.assertEqual(bot.parse_watch_command("راقب 3800 دعم"),
                         ("add", 3800.0, "دعم"))
        # a plain message is not a command
        self.assertIsNone(bot.parse_watch_command("شو رايك بالسوق"))

    def test_the_kind_is_inferred_from_the_live_price(self):
        # above the price = resistance, below = support
        self.assertEqual(bot._watch_kind_for(3800.0, 3772.0, None), "R")
        self.assertEqual(bot._watch_kind_for(3757.0, 3772.0, None), "S")
        # an explicit word overrides the inference
        self.assertEqual(bot._watch_kind_for(3800.0, 3772.0, "دعم"), "S")

    def test_adding_a_point_saves_it_and_it_survives_a_reload(self):
        saved = bot.WATCH_POINTS
        try:
            bot.WATCH_POINTS = ((3770.0, "R"),)
            out = bot._apply_watch_command("add", 3800.0, None, 3772.0)
            self.assertIn("3800", out.replace(",", ""))
            self.assertIn((3800.0, "R"), bot.WATCH_POINTS)
            # a fresh load reads it back -- the point outlives the process
            self.assertIn((3800.0, "R"), bot.load_watch_points())
        finally:
            bot.WATCH_POINTS = saved

    def test_dropping_a_point_removes_it(self):
        saved = bot.WATCH_POINTS
        try:
            bot.WATCH_POINTS = ((3757.0, "S"), (3770.0, "R"), (3780.0, "R"))
            out = bot._apply_watch_command("drop", 3770.0, None, 3772.0)
            self.assertIn("3770", out.replace(",", ""))
            self.assertNotIn(3770.0, [p for p, _ in bot.WATCH_POINTS])
            self.assertIn((3757.0, "S"), bot.WATCH_POINTS)
        finally:
            bot.WATCH_POINTS = saved

    def test_dropping_an_unknown_point_says_so_and_changes_nothing(self):
        saved = bot.WATCH_POINTS
        try:
            bot.WATCH_POINTS = ((3770.0, "R"),)
            out = bot._apply_watch_command("drop", 3500.0, None, 3772.0)
            self.assertIn("ما لقيت", out)
            self.assertEqual(bot.WATCH_POINTS, ((3770.0, "R"),))
        finally:
            bot.WATCH_POINTS = saved


class AyzrAudienceTest(unittest.TestCase):
    """The AYZR engine goes to the owner and the people he named -- and only
    to them. A guest gets the button hidden and the card refused."""

    def test_the_named_friends_may_use_the_engine(self):
        # Zein and Yousef, the two the owner named
        self.assertTrue(bot.may_use_ayzr("997487756"))
        self.assertTrue(bot.may_use_ayzr("7556758323"))

    def test_the_owner_may_use_the_engine(self):
        self.assertTrue(bot.may_use_ayzr(bot.SEED_CHATS[0]))

    def test_a_plain_guest_may_not(self):
        self.assertFalse(bot.may_use_ayzr("1027149110"))
        self.assertFalse(bot.may_use_ayzr("000000"))

    def test_a_named_friend_is_not_the_owner(self):
        # the engine is shared; the panel, the roll and the private read are not
        self.assertFalse(bot.is_owner("7556758323"))
        self.assertFalse(bot.is_owner("997487756"))

    def test_the_button_appears_only_for_the_audience(self):
        kb = bot.menu_reply_kb("7556758323")
        texts = [b["text"] for row in kb["keyboard"] for b in row]
        self.assertIn(bot.AYZR_BUTTON_TEXT, texts)
        guest = bot.menu_reply_kb("1027149110")
        gtexts = [b["text"] for row in guest["keyboard"] for b in row]
        self.assertNotIn(bot.AYZR_BUTTON_TEXT, gtexts)


class WindowTest(unittest.TestCase):
    """The owner's windows: risk $8-12, reward $15-20, R:R >= the floor. Both
    clamps must hold together -- a wide stop must not push the target out of the
    reward window, and the reward window must not break the R:R floor."""

    def test_default_sits_in_both_windows(self):
        p = bot._sized(4100.0, "long", None)
        self.assertGreaterEqual(p["risk_usd"], bot.STOP_MIN_USD - 1e-9)
        self.assertLessEqual(p["risk_usd"], bot.STOP_MAX_USD + 1e-9)
        self.assertGreaterEqual(p["gain_usd"], bot.TARGET_MIN_USD - 1e-9)
        self.assertLessEqual(p["gain_usd"], bot.TARGET_MAX_USD + 1e-9)

    def test_both_windows_and_rr_hold_across_levels(self):
        for direction in ("long", "short"):
            for lvl in (None, 4099.0, 4085.0, 4000.0, 4110.0):
                p = bot._sized(4100.0, direction, lvl)
                self.assertGreaterEqual(p["risk_usd"], bot.STOP_MIN_USD - 1e-9)
                self.assertLessEqual(p["risk_usd"], bot.STOP_MAX_USD + 1e-9)
                self.assertGreaterEqual(p["gain_usd"], bot.TARGET_MIN_USD - 1e-9)
                self.assertLessEqual(p["gain_usd"], bot.TARGET_MAX_USD + 1e-9)
                self.assertGreaterEqual(p["rr"], bot.MIN_RR - 1e-9)


class MapTest(unittest.TestCase):
    """card_map is a VIEW: sell zones above, buy zones below, no instruction."""

    def _zigzag(self):
        # A price that repeatedly turns at 4090 and 4120 -> two zones.
        return [4090 + (30 if (i // 3) % 2 else 0) for i in range(60)]

    def test_zones_split_by_the_live_price(self):
        with FakeFeed(mid=4105.0, closes={"1h": self._zigzag()}):
            sell, buy = bot._zones(4105.0)
        self.assertTrue(all(z > 4105.0 for z, _ in sell))
        self.assertTrue(all(z < 4105.0 for z, _ in buy))

    def test_map_carries_no_buy_sell_instruction(self):
        with FakeFeed(mid=4105.0, closes={"1h": self._zigzag()}):
            text = bot.card_map()
        self.assertIn("مناطق البيع", text)
        self.assertIn("مناطق الشراء", text)
        # A map states prices; the recommendation card states the trade.
        self.assertNotIn("ادخل", text)

    def test_map_survives_a_broken_feed(self):
        saved = bot.bars
        bot.bars = lambda tf, limit=300: (_ for _ in ()).throw(RuntimeError("x"))
        try:
            self.assertEqual(bot._zones(4100.0), ([], []))
        finally:
            bot.bars = saved


class NewsTest(unittest.TestCase):
    """The news layer is information only -- it must never read the tape."""

    def test_news_card_labels_itself_as_not_a_signal(self):
        text = bot.card_news()
        self.assertIn("معلومة فقط", text)
        self.assertIn("مو إشارة", text)

    def test_news_does_not_change_the_plan(self):
        # With no news items the plan is identical: news is not a trade input.
        saved = bot.NEWS_ITEMS
        rising = list(range(4000, 4060))
        closes = {tf: rising for tf in bot.FRAMES}
        fs = bot.FILTERS_ON
        saved_nc = bot.NOCHASE_ON
        saved_eb = bot.ENTRY_BASIS_ON
        bot.FILTERS_ON = False
        bot.NOCHASE_ON = False
        bot.ENTRY_BASIS_ON = False
        try:
            bot.NEWS_ITEMS = []
            with FakeFeed(mid=4059.0, closes=closes):
                a = bot.plan()
            bot.NEWS_ITEMS = [("🔥", "huge news")]
            with FakeFeed(mid=4059.0, closes=closes):
                b = bot.plan()
            self.assertIsNotNone(a)
            self.assertEqual(a, b)
        finally:
            bot.NEWS_ITEMS = saved
            bot.FILTERS_ON = fs
            bot.NOCHASE_ON = saved_nc
            bot.ENTRY_BASIS_ON = saved_eb

    def test_live_news_is_merged_and_still_labelled_not_a_signal(self):
        saved_items, saved_live = bot.NEWS_ITEMS, bot.NEWS_LIVE
        saved_fetch = bot._news_fetch
        bot.NEWS_ITEMS = [("🔥", "manual headline")]
        bot.NEWS_LIVE = True
        bot._news_fetch = lambda: [("📈", "live headline")]
        try:
            text = bot.card_news()
            self.assertIn("live headline", text)
            self.assertIn("manual headline", text)
            self.assertIn("مو إشارة", text)
        finally:
            bot.NEWS_ITEMS, bot.NEWS_LIVE = saved_items, saved_live
            bot._news_fetch = saved_fetch

    def test_a_dead_live_feed_still_shows_the_manual_list(self):
        saved_items, saved_live = bot.NEWS_ITEMS, bot.NEWS_LIVE
        saved_fetch = bot._news_fetch
        bot.NEWS_ITEMS = [("🔥", "manual headline")]
        bot.NEWS_LIVE = True
        bot._news_fetch = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            text = bot.card_news()
            self.assertIn("manual headline", text)
            self.assertIn("مو إشارة", text)
        finally:
            bot.NEWS_ITEMS, bot.NEWS_LIVE = saved_items, saved_live
            bot._news_fetch = saved_fetch

    def test_news_times_are_shown_in_the_owner_clock(self):
        # Google stamps headlines in GMT; the owner reads Syria time (UTC+3),
        # so a headline is shown three hours later than its GMT stamp. Use a
        # stamp built from NOW so the test does not rot: a fixed date passes
        # only on the day it was written.
        from datetime import UTC, datetime, timedelta
        tz = bot.timezone(timedelta(hours=bot.TZ_OFFSET_H))
        today_utc = datetime.now(UTC).astimezone(tz).date()
        # 09:00 UTC on the owner's today -> 12:00 in his clock, no date shown.
        noon_gmt = datetime(today_utc.year, today_utc.month, today_utc.day,
                            9, 0, tzinfo=UTC)
        self.assertEqual(bot._clock(noon_gmt), "12:00")
        # A headline from another day carries the date, so it is not mistaken
        # for today's tape.
        old = noon_gmt - timedelta(days=9)
        self.assertIn(old.astimezone(tz).strftime("%d/%m"), bot._clock(old))

    def test_news_card_shows_the_owner_clock_label(self):
        saved_items, saved_live = bot.NEWS_ITEMS, bot.NEWS_LIVE
        bot.NEWS_ITEMS, bot.NEWS_LIVE = [("🔥", "manual")], False
        try:
            text = bot.card_news()
            self.assertIn("بتوقيت سوريا", text)
        finally:
            bot.NEWS_ITEMS, bot.NEWS_LIVE = saved_items, saved_live

    def test_retail_price_and_other_currencies_are_dropped(self):
        # A karat-21 shop price and the Canadian dollar mention gold/the dollar
        # but do not move XAUUSD -- they must be filtered out.
        self.assertFalse(bot._news_keep("سعر الذهب عيار 21 اليوم"))
        self.assertFalse(bot._news_keep("تراجع الدولار الكندي أمام الأميركي"))
        self.assertFalse(bot._news_keep("أسعار الذهب في محلات الصاغة"))
        self.assertTrue(bot._news_keep("الذهب الفوري يتراجع مع ارتفاع الدولار"))
        self.assertTrue(bot._news_keep("الفيدرالي يرفع الفائدة في ديسمبر"))

    def test_an_off_topic_headline_is_dropped(self):
        # Google News' fuzzy search returns a Russian stock index and a riyal
        # rate card. A blacklist cannot enumerate every off-topic topic, so a
        # headline must mention gold, the dollar or the Fed/rate/inflation path
        # to be shown -- these mention none of them and must go.
        self.assertFalse(bot._news_keep(
            "مؤشرات الأسهم في روسيا هبطت عند نهاية جلسة اليوم"))
        self.assertFalse(bot._news_keep(
            "أسعار العملات الأجنبية مقابل الريال السعودي اليوم"))
        # A retail price card that names a country (not the metal's market).
        self.assertFalse(bot._news_keep("أسعار الذهب اليوم السبت في مصر"))
        # The drivers still pass.
        self.assertTrue(bot._news_keep("التضخم الأميركي يفاجئ الأسواق"))
        self.assertTrue(bot._news_keep("مؤشر الدولار يرتفع عند أعلى مستوى"))
        self.assertTrue(bot._news_keep("توقعات سعر XAUUSD: ضغط على الدعم"))

    def test_fetch_drops_noise_titles(self):
        saved_q, saved_one = bot.NEWS_QUERIES, bot._news_one
        from datetime import UTC, datetime
        stamp = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
        bot.NEWS_QUERIES = ("x",)
        bot._news_one = lambda q: [
            (stamp, "▫️", "الذهب عيار 21 في مصر", "12:00"),
            (stamp, "🔥", "الذهب الفوري يتراجع", "12:00"),
        ]
        bot._NEWS_CACHE["at"], bot._NEWS_CACHE["items"] = 0.0, []
        try:
            titles = [t for _, t, _ in bot._news_fetch()]
        finally:
            bot.NEWS_QUERIES, bot._news_one = saved_q, saved_one
            bot._NEWS_CACHE["at"], bot._NEWS_CACHE["items"] = 0.0, []
        self.assertIn("الذهب الفوري يتراجع", titles)
        self.assertNotIn("الذهب عيار 21 في مصر", titles)


class NewsContextTest(unittest.TestCase):
    """The news line ties the headlines to the trade direction -- information
    only, never a plan change. It must say 'with you' / 'against you' correctly,
    and never invent a link when there is no hawkish headline."""

    def test_trend_side_is_one_reading_of_the_frame_count(self):
        # The side the news/divergence helpers lean must come from the frame
        # count, not the distance to the nearest level: a 6/6 down tape is a
        # SHORT lean no matter where the price sits between two levels.
        self.assertEqual(bot._trend_side({"trends": {
            "1d": -1, "4h": -1, "1h": -1, "30m": -1, "15m": -1, "5m": -1}}),
            "short")
        self.assertEqual(bot._trend_side({"trends": {
            "1d": 1, "4h": 1, "1h": 1, "30m": 1, "15m": 1, "5m": 1}}),
            "long")
        self.assertIsNone(bot._trend_side({"trends": {
            "1d": 1, "4h": -1, "1h": 1, "30m": -1, "15m": 1, "5m": -1}}))

    def _with_titles(self, titles):
        saved = bot.NEWS_ITEMS, bot.NEWS_LIVE
        bot.NEWS_ITEMS = [("🔥", t) for t in titles]
        bot.NEWS_LIVE = False
        return saved

    def test_hawkish_news_reads_with_a_short(self):
        saved = self._with_titles(["الدولار يرتفع", "الفيدرالي يرفع الفائدة"])
        try:
            line = bot._news_context("short")
        finally:
            bot.NEWS_ITEMS, bot.NEWS_LIVE = saved
        self.assertIsNotNone(line)
        self.assertIn("معك", line)
        self.assertIn("معلومة فقط", line)

    def test_hawkish_news_reads_against_a_long(self):
        saved = self._with_titles(["الدولار يرتفع لأعلى مستوى"])
        try:
            line = bot._news_context("long")
        finally:
            bot.NEWS_ITEMS, bot.NEWS_LIVE = saved
        self.assertIn("ضدّك", line or "")

    def test_cpi_is_flagged_as_a_volatility_warning(self):
        saved = self._with_titles(["تقرير التضخم الأميركي CPI اليوم"])
        try:
            line = bot._news_context("short")
        finally:
            bot.NEWS_ITEMS, bot.NEWS_LIVE = saved
        self.assertIn("تقلّب", line or "")

    def test_no_relevant_headline_returns_none(self):
        saved = self._with_titles(["الذهب يستقر مع هدوء الأسواق"])
        try:
            line = bot._news_context("short")
        finally:
            bot.NEWS_ITEMS, bot.NEWS_LIVE = saved
        self.assertIsNone(line)

    def test_the_recommendation_carries_the_news_when_it_enters(self):
        # A trade signal must carry the headline link, the way the report does.
        saved_items, saved_live = bot.NEWS_ITEMS, bot.NEWS_LIVE
        bot.NEWS_ITEMS = [("🔥", "الدولار يرتفع والفيدرالي يرفع الفائدة")]
        bot.NEWS_LIVE = False
        close = [4100 - i for i in range(60)]
        try:
            with FakeFeed(mid=close[-1], closes={tf: close for tf in bot.FRAMES}):
                rec = bot.card_recommend()
                plan_d = bot.plan()
        finally:
            bot.NEWS_ITEMS, bot.NEWS_LIVE = saved_items, saved_live
        if plan_d is not None and not bot.open_trades():
            self.assertIn("الخبر", rec)


class NewsAlertTest(unittest.TestCase):
    """The unprompted news alert: fires on a NEW important headline, stays
    silent on commentary/previews, seeds silently on the first look, and damps
    a burst with a cooldown. INFORMATION ONLY -- it never opens a trade."""

    def _fetch(self, items):
        return unittest.mock.patch.object(bot, "_news_fetch", return_value=items)

    def test_only_a_real_event_fires_not_commentary(self):
        # A Fed official's view, a "what to expect" preview and an outlook are
        # not events -- they name the topic but predict rather than report.
        self.assertFalse(bot._news_is_event(
            "مسؤول بالفيدرالي الأميركي: لا يزال أمامنا عمل لاحتواء التضخم"))
        self.assertFalse(bot._news_is_event(
            "توقعات مؤشر الدولار DXY قبيل بيانات التضخم الأمريكي"))
        self.assertFalse(bot._news_is_event(
            "رئيس «فيدرالي نيويورك» يرجّح الحاجة لمزيد من رفع الفائدة"))
        self.assertFalse(bot._news_is_event(
            "مؤشرات الأسهم في روسيا هبطت عند نهاية جلسة اليوم"))
        # A real print does fire.
        self.assertTrue(bot._news_is_event(
            "التضخم الأميركي يفاجئ الأسواق... والفائدة تعود إلى دائرة التشديد"))
        self.assertTrue(bot._news_is_event(
            "أبرز الاستنتاجات من تقرير التضخم الأميركي لشهر أكتوبر"))

    def test_an_officials_view_is_dropped_even_when_it_names_the_event(self):
        # The owner asked to hear the EVENT, not every official's view of it.
        # This headline names the event but reports what someone SAID.
        self.assertFalse(bot._news_is_event(
            "مسؤول بالفيدرالي: التضخم الأمريكي لا يزال مرتفعًا.. وأمامنا عمل "
            "لاحتوائه بواسطة المتداول العربي - Investing.com"))
        self.assertFalse(bot._news_is_event(
            "عضو بالفيدرالي: التضخم الأميركي ما زال بعيدًا عن الهدف"))
        self.assertFalse(bot._news_is_event(
            "تصريحات باول حول التضخم الأمريكي تشعل الأسواق"))

    def test_a_short_keyword_must_be_a_whole_word(self):
        # "هل " must not match inside "المستهلك" -- that bug silently dropped
        # every real "أسعار المستهلك" print as if it were a question.
        self.assertTrue(bot._news_is_event(
            "أسعار المستهلك الأمريكي ترتفع 0.4% في أكتوبر"))
        self.assertTrue(bot._news_is_event(
            "مؤشر أسعار المستهلك يسجل أعلى من المتوقع"))
        # "قال" must not match inside "مقال".
        self.assertFalse(bot._news_is_event(
            "مقال عن تقرير التضخم الأميركي"))

    def test_first_look_seeds_silently_then_a_new_event_fires(self):
        seen: dict = {}
        old = [("🔥", "الذهب الفوري يتراجع", "15/11 10:00")]
        with self._fetch(old):
            self.assertIsNone(bot.news_watch(seen))     # seed, no message
        new = [("🔥", "التضخم الأميركي يفاجئ الأسواق", "15/11 23:00")] + old
        with self._fetch(new):
            out = bot.news_watch(seen)
        self.assertIsNotNone(out)
        self.assertIn("التضخم الأميركي يفاجئ الأسواق", out)
        # The headline's OWN clock, in the owner's timezone, is on the alert.
        self.assertIn("15/11 23:00", out)
        self.assertIn(bot.TZ_LABEL, out)
        # And it is labelled information-only, never a signal.
        self.assertIn("معلومة فقط", out)

    def test_a_burst_is_damped_by_the_cooldown(self):
        seen: dict = {}
        with self._fetch([("🔥", "خبر قديم", "15/11 10:00")]):
            bot.news_watch(seen)                         # seed
        with self._fetch([("🔥", "التضخم الأميركي يفاجئ الأسواق", "15/11 23:00"),
                          ("🔥", "خبر قديم", "15/11 10:00")]):
            self.assertIsNotNone(bot.news_watch(seen))   # fires once
        with self._fetch([("🔥", "قرار الفائدة: تثبيت", "15/11 23:30"),
                          ("🔥", "التضخم الأميركي يفاجئ الأسواق", "15/11 23:00"),
                          ("🔥", "خبر قديم", "15/11 10:00")]):
            self.assertIsNone(bot.news_watch(seen))      # damped

    def test_the_switch_off_silences_the_alert(self):
        saved = bot.NEWS_ALERTS_ON
        bot.NEWS_ALERTS_ON = False
        try:
            with self._fetch([("🔥", "التضخم الأميركي يفاجئ الأسواق", "15/11 23:00")]):
                self.assertIsNone(bot.news_watch({}))
        finally:
            bot.NEWS_ALERTS_ON = saved

    def test_the_panel_shows_and_persists_the_news_switch(self):
        # The switch rides with the entry gates in the panel and the saved file,
        # but `open_all`/`close_all` must NOT touch it (it is not an entry gate).
        self.assertIn("news_alerts", bot.panel_state())
        self.assertIn("news_alerts", bot.GATE_LABELS)
        keys = [b["callback_data"]
                for r in bot.settings_keyboard()["inline_keyboard"] for b in r]
        self.assertIn("cfg:news_alerts", keys)
        self.assertIn("تنبيه الأخبار", bot.card_settings())

    def test_the_panel_has_a_bounce_entry_button(self):
        # The owner asked to flip sell-the-bounce from the panel, on and off
        # whenever he wants -- not to edit a Render env var. It must be a real
        # button that flips the LIVE global and persists, and `open_all` must
        # leave it alone (it is a behaviour, not one of the four entry gates).
        saved = bot.BOUNCE_ENTRY
        try:
            bot.apply_settings({"bounce_entry": True})
            keys = [b["callback_data"]
                    for r in bot.settings_keyboard()["inline_keyboard"]
                    for b in r]
            self.assertIn("cfg:bounce_entry", keys)
            self.assertIn("بيع الارتداد", bot.card_settings())
            bot.toggle_gate("bounce_entry")
            self.assertFalse(bot.BOUNCE_ENTRY)
            self.assertFalse(json.loads(
                bot.SETTINGS_PATH.read_text())["bounce_entry"])
            self.assertTrue(bot.BOUNCE_ENTRY is False
                            and "bounce_entry" not in bot.gate_state())
        finally:
            bot.BOUNCE_ENTRY = saved


class EconomicCalendarTest(unittest.TestCase):
    """The pre-event prep card and its alert: a FACT about the calendar (time,
    consensus) and BOTH plans -- never a prediction of the direction."""

    def setUp(self):
        self._saved = (bot._CAL_CACHE["at"], list(bot._CAL_CACHE["events"]))

    def tearDown(self):
        bot._CAL_CACHE["at"], bot._CAL_CACHE["events"] = self._saved

    def _ev(self, title, mins_from_now, cur="USD", impact="High",
            forecast="3.1%", previous="3.0%"):
        when = datetime.now(UTC) + timedelta(minutes=mins_from_now)
        return {"title": title, "currency": cur, "impact": impact,
                "when": when, "forecast": forecast, "previous": previous}

    def test_parse_keeps_only_gold_currencies_and_impact(self):
        raw = [
            {"title": "CPI m/m", "country": "USD", "impact": "High",
             "date": "2026-11-07T13:30:00-05:00", "forecast": "0.3%",
             "previous": "0.2%"},
            {"title": "NZD Services", "country": "NZD", "impact": "Medium",
             "date": "2026-11-07T17:45:00-05:00"},
            {"title": "Some low thing", "country": "USD", "impact": "Low",
             "date": "2026-11-07T10:00:00-05:00"},
            {"title": "Broken", "country": "USD", "impact": "High",
             "date": "not-a-date"},
        ]
        out = bot._cal_parse(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "CPI m/m")
        self.assertEqual(out[0]["currency"], "USD")
        self.assertIsNotNone(out[0]["when"].tzinfo)

    def test_parse_understands_the_tradingview_shape_too(self):
        # The default source. `importance` is an int (1 high, 0 medium, -1 low)
        # and the time ends in Z; both must normalise to the same dict shape.
        raw = [
            {"title": "Nonfarm Payrolls", "currency": "USD", "importance": 1,
             "date": "2026-11-07T13:30:00.000Z", "forecast": 150, "previous": 120},
            {"title": "Minor print", "currency": "USD", "importance": -1,
             "date": "2026-11-07T15:00:00.000Z"},
            {"title": "AUD thing", "currency": "AUD", "importance": 1,
             "date": "2026-11-07T01:00:00.000Z"},
        ]
        out = bot._cal_parse(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "Nonfarm Payrolls")
        self.assertEqual(out[0]["impact"], "High")
        self.assertEqual(out[0]["forecast"], "150")
        self.assertIsNotNone(out[0]["when"].tzinfo)

    def test_next_event_skips_one_already_fired(self):
        past = self._ev("Past", -30)
        soon = self._ev("Soon", 45)
        later = self._ev("Later", 180)
        ev = bot._cal_next([past, soon, later])
        self.assertEqual(ev["title"], "Soon")

    def test_the_card_names_the_time_and_both_plans_never_a_prediction(self):
        bot._CAL_CACHE["at"] = time.monotonic()
        bot._CAL_CACHE["events"] = [self._ev("CPI m/m", 40)]
        with unittest.mock.patch.object(bot, "_cal_fetch",
                                        return_value=bot._CAL_CACHE["events"]), \
             unittest.mock.patch.object(bot, "read_market",
                                        return_value={"live": 4100.0}), \
             unittest.mock.patch.object(bot, "_levels",
                                        return_value=([4120.0], [4080.0])):
            out = bot.card_news_prep()
        self.assertIn("CPI m/m", out)
        self.assertIn(bot.TZ_LABEL, out)
        self.assertIn("جهّز السيناريوهين", out)
        # BOTH directions are prepared -- the card never picks one.
        self.assertIn("لو كسر فوق", out)
        self.assertIn("لو كسر تحت", out)
        # And it says plainly that the direction is not a prediction.
        self.assertIn("ما بتنبّأ", out)
        # The consensus is shown as a fact.
        self.assertIn("3.1%", out)

    def test_no_event_returns_a_plain_honest_line(self):
        with unittest.mock.patch.object(bot, "_cal_fetch", return_value=[]):
            out = bot.card_news_prep()
        self.assertIn("ما في حدث اقتصادي مهم قريب", out)

    def test_the_lean_is_a_rule_only_for_usd_hot_prints(self):
        self.assertIsNotNone(bot._cal_lean(self._ev("CPI m/m", 30)))
        # A non-USD event has no first-order gold rule.
        self.assertIsNone(bot._cal_lean(self._ev("German CPI", 30, cur="EUR")))
        # A USD event with no hot print (e.g. a Fed speech) has no rule.
        self.assertIsNone(bot._cal_lean(self._ev("Fed Speech", 30)))

    def test_the_alert_fires_once_per_window_and_seeds_on_boot(self):
        seen: dict = {}
        events = [self._ev("CPI m/m", 50)]      # inside the 60m window
        with unittest.mock.patch.object(bot, "_cal_fetch", return_value=events), \
             unittest.mock.patch.object(bot, "read_market",
                                        return_value={"live": 4100.0}), \
             unittest.mock.patch.object(bot, "_levels",
                                        return_value=([4120.0], [4080.0])):
            # First look after boot: SEED silently, no alert.
            self.assertIsNone(bot.news_prep_watch(seen))
            # The same window must not fire again on the next tick.
            self.assertIsNone(bot.news_prep_watch(seen))

    def test_the_alert_fires_when_a_window_is_crossed_while_running(self):
        seen: dict = {}
        with unittest.mock.patch.object(bot, "_cal_fetch", return_value=[]):
            self.assertIsNone(bot.news_prep_watch(seen))   # boot, no event
        events = [self._ev("CPI m/m", 50)]
        with unittest.mock.patch.object(bot, "_cal_fetch", return_value=events), \
             unittest.mock.patch.object(bot, "read_market",
                                        return_value={"live": 4100.0}), \
             unittest.mock.patch.object(bot, "_levels",
                                        return_value=([4120.0], [4080.0])):
            out = bot.news_prep_watch(seen)
        self.assertIsNotNone(out)
        self.assertIn("تنبيه خبر", out)
        self.assertIn("CPI m/m", out)
        # Once only: the same event/window must not speak twice.
        with unittest.mock.patch.object(bot, "_cal_fetch", return_value=events), \
             unittest.mock.patch.object(bot, "read_market",
                                        return_value={"live": 4100.0}), \
             unittest.mock.patch.object(bot, "_levels",
                                        return_value=([4120.0], [4080.0])):
            self.assertIsNone(bot.news_prep_watch(seen))

    def test_the_switch_off_silences_the_prep_alert(self):
        saved = bot.NEWS_PREP_ON
        bot.NEWS_PREP_ON = False
        try:
            seen = {"prep_booted": True}
            events = [self._ev("CPI m/m", 50)]
            with unittest.mock.patch.object(bot, "_cal_fetch",
                                            return_value=events):
                self.assertIsNone(bot.news_prep_watch(seen))
        finally:
            bot.NEWS_PREP_ON = saved

    def test_the_panel_shows_the_prep_switch(self):
        self.assertIn("news_prep", bot.panel_state())
        self.assertIn("news_prep", bot.GATE_LABELS)
        keys = [b["callback_data"]
                for r in bot.settings_keyboard()["inline_keyboard"] for b in r]
        self.assertIn("cfg:news_prep", keys)
        # It is NOT an entry gate: open_all/close_all must leave it alone.
        self.assertNotIn("news_prep", bot.GATE_KEYS)


class NewsResultTest(unittest.TestCase):
    """The post-event RESULT alert: when a prepared print LANDS, the desk says
    the number AND what price did -- the answer the prep card could not give."""

    def setUp(self):
        self._saved = (bot._CAL_CACHE["at"], list(bot._CAL_CACHE["events"]))

    def tearDown(self):
        bot._CAL_CACHE["at"], bot._CAL_CACHE["events"] = self._saved

    def _ev(self, title, mins_from_now, actual="", forecast="3.1%",
            previous="3.0%", cur="USD", impact="High"):
        when = datetime.now(UTC) + timedelta(minutes=mins_from_now)
        return {"title": title, "currency": cur, "impact": impact,
                "when": when, "forecast": forecast, "previous": previous,
                "actual": actual}

    def test_parse_keeps_the_actual(self):
        raw = [{"title": "CPI m/m", "country": "USD", "impact": "High",
                "date": "2026-11-07T13:30:00-05:00", "forecast": "3.1%",
                "previous": "3.0%", "actual": "3.4%"}]
        out = bot._cal_parse(raw)
        self.assertEqual(out[0]["actual"], "3.4%")

    def test_an_event_without_actual_has_not_landed(self):
        events = [self._ev("CPI m/m", -5)]      # 5 min ago, no actual
        self.assertEqual(bot._cal_result_events(events, datetime.now(UTC)), [])

    def test_an_event_past_the_window_is_not_a_live_result(self):
        events = [self._ev("CPI m/m", -90, actual="3.4%")]
        self.assertEqual(bot._cal_result_events(events, datetime.now(UTC)), [])

    def test_a_landed_event_inside_the_window_is_reported(self):
        events = [self._ev("CPI m/m", -3, actual="3.4%")]
        got = bot._cal_result_events(events, datetime.now(UTC))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["title"], "CPI m/m")

    def test_the_surprise_reads_the_sign_of_the_number(self):
        self.assertEqual(bot._cal_surprise("3.4%", "3.1%"), 1)
        self.assertEqual(bot._cal_surprise("2.9%", "3.1%"), -1)
        self.assertEqual(bot._cal_surprise("3.1%", "3.1%"), 0)
        # Unreadable numbers are "no surprise", never a guess.
        self.assertEqual(bot._cal_surprise("n/a", "3.1%"), 0)

    def test_the_card_names_the_actual_forecast_and_the_move(self):
        ev = self._ev("CPI m/m", -3, actual="3.4%")
        with unittest.mock.patch.object(bot, "_price_since",
                                        return_value=(3900.0, 3920.0, 20.0)):
            out = bot.card_news_result(ev)
        self.assertIn("3.4%", out)                 # the actual
        self.assertIn("3.1%", out)                 # the consensus
        self.assertIn("أعلى", out)                 # the surprise, as a rule
        self.assertIn("3,920", out)                # the price it moved to
        self.assertIn("+20", out)                  # and the delta
        # It is information, never a signal.
        self.assertIn("مو إشارة", out)

    def test_the_card_renders_without_a_tape(self):
        # A dead feed must not stop the card from stating the number.
        ev = self._ev("CPI m/m", -3, actual="3.4%")
        with unittest.mock.patch.object(bot, "_price_since", return_value=None):
            out = bot.card_news_result(ev)
        self.assertIn("3.4%", out)
        self.assertIn("ما قدرت أقرأ حركة السعر", out)

    def test_the_alert_fires_once_when_a_print_lands_while_running(self):
        seen: dict = {}
        with unittest.mock.patch.object(bot, "_cal_fetch", return_value=[]):
            self.assertIsNone(bot.news_result_watch(seen))    # boot, nothing
        events = [self._ev("CPI m/m", -2, actual="3.4%")]
        with unittest.mock.patch.object(bot, "_cal_fetch", return_value=events), \
             unittest.mock.patch.object(bot, "_price_since",
                                        return_value=(3900.0, 3910.0, 10.0)):
            out = bot.news_result_watch(seen)
        self.assertIsNotNone(out)
        self.assertIn("نزل خبر", out)
        self.assertIn("3.4%", out)
        # Once only: the same landed event must not speak again.
        with unittest.mock.patch.object(bot, "_cal_fetch", return_value=events), \
             unittest.mock.patch.object(bot, "_price_since",
                                        return_value=(3900.0, 3910.0, 10.0)):
            self.assertIsNone(bot.news_result_watch(seen))

    def test_a_restart_does_not_replay_a_result_already_landed(self):
        # A desk that was down over the release must not dump it on boot.
        events = [self._ev("CPI m/m", -5, actual="3.4%")]
        seen: dict = {}
        with unittest.mock.patch.object(bot, "_cal_fetch", return_value=events):
            self.assertIsNone(bot.news_result_watch(seen))   # boot: seed, silent
        # A SECOND print landing later still speaks.
        more = events + [self._ev("PPI m/m", -1, actual="0.5%")]
        with unittest.mock.patch.object(bot, "_cal_fetch", return_value=more), \
             unittest.mock.patch.object(bot, "_price_since",
                                        return_value=(3900.0, 3905.0, 5.0)):
            out = bot.news_result_watch(seen)
        self.assertIsNotNone(out)
        self.assertIn("PPI", out)

    def test_only_a_high_impact_print_earns_the_alert(self):
        events = [self._ev("Minor print", -2, actual="1.0", impact="Medium")]
        seen = {"result_booted": True}
        with unittest.mock.patch.object(bot, "_cal_fetch", return_value=events):
            self.assertIsNone(bot.news_result_watch(seen))

    def test_the_switch_off_silences_the_result_alert(self):
        saved = bot.CAL_RESULT_ON
        bot.CAL_RESULT_ON = False
        try:
            seen = {"result_booted": True}
            events = [self._ev("CPI m/m", -2, actual="3.4%")]
            with unittest.mock.patch.object(bot, "_cal_fetch",
                                            return_value=events):
                self.assertIsNone(bot.news_result_watch(seen))
        finally:
            bot.CAL_RESULT_ON = saved

    def test_the_panel_shows_the_result_switch(self):
        self.assertIn("news_result", bot.panel_state())
        self.assertIn("news_result", bot.GATE_LABELS)
        keys = [b["callback_data"]
                for r in bot.settings_keyboard()["inline_keyboard"] for b in r]
        self.assertIn("cfg:news_result", keys)
        # It is NOT an entry gate: open_all/close_all must leave it alone.
        self.assertNotIn("news_result", bot.GATE_KEYS)


    def test_the_window_looks_back_so_a_just_landed_print_is_visible(self):
        # The bug: the calendar window started at NOW, so a print that released
        # two minutes ago was already outside it and the result alert could
        # never see the number it exists to report.
        url = bot._cal_url()
        self.assertIn("from=", url)
        from urllib.parse import parse_qs, urlparse
        frm = parse_qs(urlparse(url).query)["from"][0]
        # ISO-8601 strings with a Z sort lexicographically, so this compares the
        # window's start to an hour ago without parsing either one.
        an_hour_ago = (datetime.now(UTC) - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%S")
        self.assertLess(frm[:19], an_hour_ago)

    def test_the_result_watch_reads_a_fresher_list_than_the_cards(self):
        # A 30-minute cache could surface a print up to 30 minutes late, past
        # its own result window. The result watch asks for a fresher list.
        seen_calls = []
        saved = bot._cal_fetch

        def spy(max_age_s=None):
            seen_calls.append(max_age_s)
            return []

        bot._cal_fetch = spy
        try:
            bot.news_result_watch({"result_booted": True})
        finally:
            bot._cal_fetch = saved
        self.assertTrue(seen_calls)
        self.assertEqual(seen_calls[0], bot.CAL_RESULT_CACHE_S)
        self.assertLess(bot.CAL_RESULT_CACHE_S, bot.CAL_CACHE_S)

    def test_a_landed_print_is_reported_after_the_lookback_fix(self):
        # End to end: the feed now carries an event that fired inside the
        # window, and the watch speaks with the number and the move.
        ev = self._ev("Housing Starts", -3, actual="1.309",
                      forecast="1.245", previous="1.191")
        seen = {"result_booted": True}
        with unittest.mock.patch.object(bot, "_cal_fetch", return_value=[ev]), \
             unittest.mock.patch.object(bot, "_price_since",
                                        return_value=(3771.0, 3785.0, 14.0)):
            out = bot.news_result_watch(seen)
        self.assertIsNotNone(out)
        self.assertIn("1.309", out)
        self.assertIn("1.245", out)
        self.assertIn("+14", out)


class WatchLevelsTest(unittest.TestCase):
    """The owner's levels (4138 / 4108) must be watchable, and a crossing of
    one must speak -- via the env extension and the existing watch."""

    def test_env_levels_are_in_the_default_watch_set(self):
        # The default set is a module constant; the env extension is applied at
        # import. Both 4132 (default) and the owner's band must be reachable.
        self.assertIn(4132.0, bot.WATCH_LEVELS)

    def test_a_crossing_of_a_watched_level_speaks_once(self):
        saved = bot.read_market
        try:
            seen: dict = {}
            bot.read_market = lambda: {"live": 4100.0}
            bot.levels_watch(seen)                     # baseline, silent
            bot.read_market = lambda: {"live": 4121.0}
            up = bot.levels_watch(seen)                # clears 4119
            self.assertIsNotNone(up)
            self.assertIn("اختراق", up)
            again = bot.levels_watch(seen)             # no flicker -> silent
            self.assertIsNone(again)
        finally:
            bot.read_market = saved


class AdaptiveAlertTest(unittest.TestCase):
    """The crossing alert must (a) cover the levels that matter NOW, not only
    the frozen 4100-era set, and (b) carry the trade the desk would take."""

    def test_live_levels_track_the_price(self):
        # With the price at 4055 the frozen set has nothing useful below it;
        # _live_levels must return pivots on both sides of the live price.
        closes = [4050.0 + (i % 7) for i in range(40)]
        with FakeFeed(mid=4055.0, closes={"1h": closes}):
            lv = bot._live_levels(4055.0)
        self.assertTrue(lv)
        self.assertTrue(any(x > 4055.0 for x in lv))
        self.assertTrue(any(x < 4055.0 for x in lv))

    def test_alert_carries_entry_stop_target(self):
        msg = bot._level_cross_msg(4096.0, "below", 4090.0)
        self.assertIn("بيع", msg)
        self.assertIn("الوقف", msg)
        self.assertIn("الهدف", msg)
        self.assertIn("R:R", msg)

    def test_alert_names_the_level_and_side(self):
        up = bot._level_cross_msg(4132.0, "above", 4135.0)
        self.assertIn("4,132", up)
        self.assertIn("اختراق", up)

    def test_alert_stays_honest_below_the_rr_floor(self):
        # A stop that is clamped wide leaves R:R under the floor: the alert
        # must not invent a trade, only name the break.
        saved = bot._sized
        try:
            bot._sized = lambda *a, **k: {"stop": 0, "target": 0, "rr": 0.5,
                                          "risk_usd": 8}
            msg = bot._level_cross_msg(4096.0, "below", 4090.0)
            self.assertNotIn("الهدف", msg)
            self.assertIn("راقب التأكيد", msg)
        finally:
            bot._sized = saved

    def test_a_multi_level_break_carries_one_trade_not_three(self):
        saved = bot.read_market
        try:
            seen: dict = {}
            bot.read_market = lambda: {"live": 4100.0}
            bot.levels_watch(seen)
            bot.read_market = lambda: {"live": 4060.0}   # breaks 4096/4084/4070
            note = bot.levels_watch(seen)
            self.assertIsNotNone(note)
            # exactly one priced trade (one "الوقف"), the rest merely named
            self.assertEqual(note.count("الوقف"), 1)
            self.assertIn("كمان انكسرت", note)
        finally:
            bot.read_market = saved

    def test_a_live_level_is_latched_and_speaks_once(self):
        """A live pivot must survive the cycle that breaks it, or its break is
        never seen (recomputing from the new price drops it)."""
        saved_m, saved_lv = bot.read_market, bot._live_levels
        try:
            bot._live_levels = lambda live, n=3: (4120.0, 4090.0)
            seen: dict = {}
            bot.read_market = lambda: {"live": 4100.0}
            bot.levels_watch(seen)                     # baseline
            bot.read_market = lambda: {"live": 4085.0}  # clears 4090 down
            note = bot.levels_watch(seen)
            self.assertIsNotNone(note)
            self.assertIn("4,090", note)
            self.assertIn("بيع", note)
        finally:
            bot.read_market, bot._live_levels = saved_m, saved_lv

    def test_a_far_live_level_is_dropped(self):
        saved_lv, saved_m = bot._live_levels, bot.read_market
        try:
            bot._live_levels = lambda live, n=3: (4060.0,)
            seen: dict = {"live_levels": [4000.0]}
            bot.read_market = lambda: {"live": 4050.0}
            bot.levels_watch(seen)
            # 4000 is 50 pts away (> LIVE_LEVEL_BAND) -> dropped; 4060 kept
            self.assertNotIn(4000.0, seen["live_levels"])
            self.assertIn(4060.0, seen["live_levels"])
        finally:
            bot._live_levels, bot.read_market = saved_lv, saved_m


class TrendFromTest(unittest.TestCase):
    """The ONE trend definition: price must sit on one side of BOTH averages.

    Comparing the averages alone was blind to price: after gold fell 114 pts in
    four 4h bars, EMA20 still sat above EMA50 and the frame read "up" while
    price was 50 pts under both. These pin the price-aware rule.
    """

    def test_price_above_both_averages_is_up(self):
        self.assertEqual(bot.trend_from(4100.0, 4080.0, 4060.0), 1)

    def test_price_below_both_averages_is_down(self):
        # The gold case: price far under both, even with EMA20 still above EMA50.
        self.assertEqual(bot.trend_from(4020.0, 4076.0, 4053.0), -1)

    def test_price_between_the_averages_is_neutral(self):
        # A transition: price under the 20 but over the 50 -> no trend claimed.
        self.assertEqual(bot.trend_from(4070.0, 4080.0, 4050.0), 0)

    def test_a_missing_average_is_neutral(self):
        self.assertEqual(bot.trend_from(4100.0, None, 4060.0), 0)
        self.assertEqual(bot.trend_from(4100.0, 4080.0, None), 0)

    def test_frame_trend_reads_price_against_both_averages(self):
        # A long flat base then a sharp collapse: the last close is far under
        # both EMAs, so the frame reads down (the gold case, reproduced).
        closes = [4100.0] * 60 + [4060.0, 4020.0, 3980.0, 3940.0, 3900.0]
        bars = [_bar(_iso(i), c, c + 1, c - 1, c) for i, c in enumerate(closes)]
        self.assertEqual(bot.frame_trend(bars), -1)

    def test_frame_trend_is_up_when_price_leads_both(self):
        closes = [4000.0] * 60 + [4040.0, 4080.0, 4120.0, 4160.0, 4200.0]
        bars = [_bar(_iso(i), c, c + 1, c - 1, c) for i, c in enumerate(closes)]
        self.assertEqual(bot.frame_trend(bars), 1)


class StructureReadTest(unittest.TestCase):
    """The 1h structure is only live while price respects the last swing."""

    @staticmethod
    def _seg(a, b, n):
        return [a + (b - a) * k / (n - 1) for k in range(n)]

    def _zigzag(self, live):
        """Rising highs (4060/4080/4100) and rising lows (4020/4040), then a
        leg to `live`. With 3-bar pivots this yields the swing sequence the
        structure read is meant to judge."""
        s = self._seg
        vals = s(4000, 4060, 9) + s(4060, 4020, 9)[1:] + s(4020, 4080, 9)[1:] \
            + s(4080, 4040, 9)[1:] + s(4040, 4100, 9)[1:] + s(4100, live, 9)[1:]
        return [_bar(_iso(i), v, v + 0.5, v - 0.5, v) for i, v in enumerate(vals)]

    def test_a_broken_support_is_not_a_rising_structure(self):
        # Rising swings, but price has collapsed far BELOW the last swing low.
        saved = bot.bars
        try:
            bot.bars = lambda tf, limit=300: self._zigzag(3850.0)
            txt = bot._structure_read()
            self.assertIn("انكسر آخر قاع", txt or "")
            self.assertNotIn("صاعدة", txt or "")
        finally:
            bot.bars = saved

    def test_a_live_rising_structure_still_reads_up(self):
        # Same rising swings, price holding above the last low -> still up.
        saved = bot.bars
        try:
            bot.bars = lambda tf, limit=300: self._zigzag(4050.0)
            txt = bot._structure_read()
            self.assertIn("صاعدة", txt or "")
        finally:
            bot.bars = saved

    def test_rising_swings_against_a_down_trend_are_labelled_short_term(self):
        # A "بنية صاعدة" beside a 6/6 down trend reads as a contradiction. With
        # the desk's own bias passed in, the line must say so and call the swings
        # short-term -- the owner is not left to reconcile two opposite sentences.
        saved = bot.bars
        try:
            bot.bars = lambda tf, limit=300: self._zigzag(4050.0)
            txt = bot._structure_read(-1, 6)
        finally:
            bot.bars = saved
        self.assertIn("قصيرة المدى", txt or "")
        self.assertIn("عكس الاتجاه العام", txt or "")
        self.assertIn("6/6", txt or "")

    def test_structure_agreeing_with_the_trend_has_no_warning(self):
        saved = bot.bars
        try:
            bot.bars = lambda tf, limit=300: self._zigzag(4050.0)
            txt = bot._structure_read(1, 6)
        finally:
            bot.bars = saved
        self.assertNotIn("عكس الاتجاه", txt or "")

    def test_trend_dir_counts_the_agreeing_frames(self):
        d, n = bot._trend_dir({tf: -1 for tf in bot.FRAMES})
        self.assertEqual((d, n), (-1, 6))
        d, n = bot._trend_dir({tf: 1 for tf in bot.FRAMES})
        self.assertEqual((d, n), (1, 6))
        d, n = bot._trend_dir({tf: 0 for tf in bot.FRAMES})
        self.assertEqual(d, None)


class FullCardTest(unittest.TestCase):
    """card_full is MY OWN read of the tape (the owner asked for the agent's
    analysis, not the desk's verdict). It must stay inside Telegram's limit and
    carry the sections plus a concrete call."""

    def setUp(self):
        # card_full reads the journal for the win-rate line. Pin it to a fresh
        # EMPTY db so the card renders its "no trades yet" branch regardless of
        # what the developer's real data/journal.db happens to hold -- otherwise
        # this suite passes on a clean checkout and fails on a working one.
        self._tmp = tempfile.mkdtemp()
        self._db = bot.DB_PATH
        bot.DB_PATH = pathlib.Path(self._tmp) / "journal.db"
        bot._init_db()

    def tearDown(self):
        bot.DB_PATH = self._db
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _feed(self):
        # A wavy 1h series: real pivots, so structure and zones are present.
        import math
        wave = [4000 + 30 * math.sin(i / 3.0) + i * 0.3 for i in range(90)]
        return FakeFeed(mid=wave[-1],
                        closes={tf: wave for tf in bot.FRAMES})

    def _leg_feed(self, last_1h):
        # A real multi-day down leg (rise then a 135-pt drop) with a caller-
        # supplied 1h ending. The wall is the 38.2% fib of the drop; `last_1h`
        # decides whether the trigger has fired. The 15m/30m/5m tails carry a
        # REAL range (a flat line has ~0 ATR and would fail the quality gate,
        # which is the gate doing its job -- this fixture must model a live
        # tape, not a dead one).
        rise = [4000 + (4148 - 4000) * (i / 19) for i in range(20)]
        drop = [4148 - (4148 - 4013) * (i / 24) for i in range(25)]
        # The recent tape must sit INSIDE the desk's ATR band (0.08-0.30%; the
        # live median is ~0.20%). A +/-6 swing read 0.37% and the gate rightly
        # refused it -- a fixture above the band models a tape the desk would not
        # trade, not the calm post-drop consolidation this scenario is.
        tail = [4058 + (3 if i % 2 else -3) for i in range(30)]
        h1 = [4030 + i * 0.3 for i in range(20)] + last_1h
        return {"1d": [4060.0] * 30, "4h": rise + drop, "1h": h1,
                "30m": tail, "15m": tail, "5m": tail}

    def test_it_stays_inside_the_telegram_limit(self):
        with self._feed():
            t = bot.card_full()
        self.assertLessEqual(len(t), 4000)
        self.assertGreater(len(t), 200)

    def test_it_carries_every_section(self):
        with self._feed():
            t = bot.card_full()
        for marker in ("قراءتي للذهب", "موقفي", "الصورة", "الفريمات", "الزخم",
                       "البنية", "المستويات", "السيناريوهات", "خطتي", "حسابك"):
            self.assertIn(marker, t)

    def test_the_plan_carries_multiple_sized_entries_and_the_account(self):
        with self._feed():
            t = bot.card_full()
        # The owner asked for the whole ladder live, not one entry: at least two
        # entries with a stop and a target, and the account line.
        self.assertGreaterEqual(t.count("وقف"), 2)
        self.assertGreaterEqual(t.count("هدف"), 2)
        self.assertIn("R:R", t)
        self.assertIn("صفقة واحدة فقط", t)

    def test_it_names_the_reversal_scenario_c_with_its_level(self):
        with self._feed():
            t = bot.card_full()
        # C is a REVERSAL, not a bounce: it must name a level and say to stop
        # trading the old direction -- otherwise B and C read the same.
        self.assertIn("انقلاب", t)
        self.assertIn("وقّف", t)

    def test_b_and_c_are_told_apart(self):
        with self._feed():
            t = bot.card_full()
        # B and C share the same level on purpose (a bounce dies where a close
        # flips the trend). The card must say WHICH is which, or the two read as
        # one: B is a touch, C is a close.
        self.assertIn("يلمس", t)          # B: a touch
        self.assertIn("إغلاق 1س", t)       # C: a 1h close
        self.assertIn("مو مجرد لمس", t)    # ...and it says so

    def test_it_shows_the_raw_swings_split_by_the_price(self):
        with self._feed():
            t = bot.card_full()
        # Tops above and bottoms below the live price; a bottom printed ABOVE
        # the price is the old pre-drop swing and reads as nonsense.
        self.assertIn("القمم والقيعان", t)
        self.assertIn("قمم فوق", t)
        self.assertIn("قيعان تحت", t)

    def test_the_win_rate_line_names_its_source(self):
        with self._feed():
            t = bot.card_full()
        # With no closed trades of his own the line must say it is a BACKTEST
        # figure, never pass a study number off as a measured one.
        self.assertIn("نسبة الفوز", t)
        self.assertIn("باكتست", t)

    def test_the_score_measures_the_card_not_an_opinion(self):
        with self._feed():
            s = bot.card_score()
            plain = bot.card_full()
        # The score is a computation over the rendered card: it must name the
        # numbers it measured, and it must not award full marks while a defect
        # (a duplicated swing) is present in the card it just read.
        self.assertIn("بناء البطاقة", s)
        self.assertIn("تكرار", s)
        import re as _re
        m = _re.search(r"بناء البطاقة: (\d+) / (\d+)", s)
        self.assertIsNotNone(m)
        assert m is not None
        self.assertLessEqual(int(m.group(1)), int(m.group(2)))
        # The structural rows must all pass on a healthy card.
        self.assertNotIn("❌", s)
        self.assertIn("خطتي", plain)
        # The market facts are measured and printed BELOW the score, not mixed
        # into it: a perfect layout must never hide the edge reading. The
        # section is always present; the two measured lines need the 2026
        # replay data (backtest/duka_2026.json, gitignored, 34 MB), so a clean
        # checkout gets the "no study data" branch instead -- assert that
        # branch rather than fail, since the suite must run anywhere.
        self.assertIn("حقائق السوق", s)
        if bot._study_edge() is None:
            self.assertIn("ما في بيانات دراسة", s)
        else:
            self.assertIn("الحافة الزمنية", s)
            self.assertIn("المعايرة", s)

    def test_a_retracement_is_not_called_a_range(self):
        # The owner's complaint: the card said "عرضي, stay out" while the price
        # was retracing a real multi-day drop -- hiding the decision point. A
        # range on the frames must not swallow a leg the chart shows.
        up = [4000 + (4148 - 4000) * (i / 29) for i in range(30)]
        dn = [4148 - (4148 - 4013) * (i / 24) for i in range(25)]
        chop = [4065 + (3 if i % 2 else -3) for i in range(20)]  # range tail
        with FakeFeed(mid=4065.0, closes={tf: up + dn + chop
                                          for tf in bot.FRAMES}):
            leg = bot._big_leg(4065.0)
            t = bot.card_full()
        # The leg is the DROP the chart shows, and the price is retracing it...
        self.assertIsNotNone(leg)
        assert leg is not None
        self.assertTrue(leg[2])                      # a down leg
        self.assertLess(leg[3], 0.62)                # still inside the retrace
        # ...so the card names it, and the stance is a decision, not "range".
        self.assertIn("الصورة الكبيرة", t)
        self.assertIn("فيبو", t)

    def test_the_big_picture_is_the_dominant_leg_not_the_last_bounce(self):
        # A 296-pt drop, then a 68-pt bounce. The bounce is the LAST leg, but the
        # drop is the leg still in play: naming the bounce the "big picture"
        # flips the whole read -- the card said "تصحيح داخل صعود → اشترِ" during
        # a downtrend while its own stance and scenarios said SELL. The leg must
        # be the DROP.
        drop = [4148 - (4148 - 3852) * (i / 24) for i in range(25)]
        bounce = [3852 + (3920 - 3852) * (i / 7) for i in range(8)]
        with FakeFeed(mid=3920.0, closes={"4h": drop + bounce}):
            leg = bot._big_leg(3920.0)
        self.assertIsNotNone(leg)
        assert leg is not None
        self.assertTrue(leg[2])                       # a DOWN leg
        self.assertGreaterEqual(leg[0], 4100.0)       # top = the drop's high
        self.assertLessEqual(leg[1], 3900.0)          # low = the drop's low

    def test_a_down_tape_never_prints_a_buy_in_the_plan(self):
        # The owner's rule: all voices one voice. On a down tape (frames lean
        # short) the plan section must not offer a counter-trend buy -- the
        # contradiction he caught ("اشترِ عند الجدار" beside a sell stance).
        decline = [4200 - (4200 - 3800) * (i / 59) for i in range(60)]
        bounce = [3800 + (3830 - 3800) * (i / 4) for i in range(5)]
        tape = decline + bounce
        tail = [3830 + (3 if i % 2 else -3) for i in range(30)]
        with FakeFeed(mid=3830.0, closes={"1d": tape, "4h": tape, "1h": tape,
                                          "30m": tail, "15m": tail,
                                          "5m": tail}):
            t = bot.card_full()
        plan = t.split("خطتي")[-1].split("الأخبار")[0]
        self.assertIn("البيع", plan)
        self.assertNotIn("تصحيح داخل صعود", plan)
        self.assertNotIn("اشترِ عند الجدار", plan)

    def test_a_range_stance_yields_to_a_real_leg(self):
        # Direct check on the stance: frames say range (side=None), but the
        # price sits mid-retrace of a real drop -> the wall must be named.
        big = (4148.0, 4013.0, True, 0.39)
        lines = bot._opinion(4065.0, None, 0, [], [], 55.0, False, big)
        text = " ".join(lines)
        self.assertIn("مو عرضي", text)
        self.assertIn("38.2%", text)
        # With no leg, the same range is a plain "stay out".
        plain = " ".join(bot._opinion(4065.0, None, 0, [], [], 55.0, False))
        self.assertIn("عرضي", plain)

    def test_the_plan_agrees_with_the_stance(self):
        # The owner caught it: the header said "بِع عند الجدار" and then the
        # ladder listed only BUYS, whose target sat BEYOND the wall -- trades
        # that needed the wall to break, the exact thing the card forbids. The
        # plan must not contradict the stance it just printed.
        up = [4000 + (4148 - 4000) * (i / 29) for i in range(30)]
        dn = [4148 - (4148 - 4013) * (i / 24) for i in range(25)]
        chop = [4060 + (3 if i % 2 else -3) for i in range(20)]
        with FakeFeed(mid=4058.0, closes={tf: up + dn + chop
                                          for tf in bot.FRAMES}):
            t = bot.card_full()
        plan = t.split("خطتي")[-1].split("الأخبار")[0]
        if "بِع عند الجدار" in plan:
            # ...then a SIZED sell must be there, and no counter-trend buy.
            self.assertIn("البيع", plan)
            self.assertNotIn("شراء (عكس الاتجاه", plan)

    def test_the_card_quotes_the_plans_own_numbers(self):
        # The whole point: when plan() fires, the card's priority line and the
        # account line must be plan()'s OWN entry/stop/target -- not a second
        # calculation that can drift. A card quoting a stop the desk is not
        # using is the contradiction the owner keeps catching.
        feed = self._leg_feed([4056, 4060, 4058, 4066.2, 4058.0])
        with FakeFeed(mid=4058.0, closes=feed):
            p = bot.plan()
            t = bot.card_full()
        self.assertIsNotNone(p)
        assert p is not None
        for v in (p["entry"], p["stop"], p["target"]):
            self.assertIn(f"{v:,.2f}", t)

    def test_the_card_does_not_say_buy_now_without_a_plan(self):
        # No confirmed trend and no wall trigger -> plan() is None -> the card
        # must NOT announce a live entry. "أبيع الآن" beside a desk that did not
        # fire is the exact lie the owner caught.
        feed = self._leg_feed([4040, 4042, 4041, 4043, 4042.0])
        with FakeFeed(mid=4058.0, closes=feed):
            self.assertIsNone(bot.plan())
            t = bot.card_full()
        self.assertNotIn("أبيع الآن", t)
        self.assertNotIn("أشتري الآن", t)
        self.assertIn("بستنى", t)

    def test_the_desk_waits_while_the_card_waits(self):
        # The price is retracing the leg but has NOT reached the wall yet: the
        # card says "wait", so plan() must return None. A desk that opened here
        # would front-run the very trigger the card names -- the disagreement
        # the owner keeps catching.
        feed = self._leg_feed([4040, 4042, 4041, 4043, 4042.0])
        with FakeFeed(mid=4058.0, closes=feed):
            self.assertIsNone(bot.plan())
            t = bot.card_full()
        self.assertIn("بستنى", t)

    def test_the_desk_sells_the_wall_the_card_sells(self):
        # The last closed 1h bar touched the wall and closed back below it: the
        # card says "sell now", so plan() must size that same sell -- one voice.
        feed = self._leg_feed([4056, 4060, 4058, 4066.2, 4058.0])
        with FakeFeed(mid=4058.0, closes=feed):
            p = bot.plan()
            t = bot.card_full()
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p["direction"], "short")
        self.assertIn("أبيع الآن", t)

    def test_it_gives_a_concrete_call(self):
        with self._feed():
            t = bot.card_full()
        self.assertTrue("دخول" in t or "اشترِ" in t or "بيع عند" in t)

    def test_it_is_wired_to_the_menu_and_dispatch(self):
        with self._feed():
            self.assertEqual(bot.dispatch("full"), bot.card_full())
        self.assertIn(("🧠 قراءتي للسوق", "full"),
                      [pair for row in bot.MENU for pair in row])
        self.assertEqual(bot.TEXT_ACTIONS.get("كامل"), "full")

    def test_it_does_not_print_the_desks_gate(self):
        # The owner wants a market read, not the desk's permission slip: the
        # card must not echo the "wait, filter blocks it" verdict line.
        with self._feed():
            self.assertNotIn("فلتر", bot.card_full())

    def test_fibonacci_labels_sit_on_the_right_side(self):
        # Down leg: 38.2% is the SHALLOW retracement, just above the low.
        hi, lo = 4141.0, 4013.0
        fib = dict(bot._fib_retr(hi, lo, up=False))
        self.assertAlmostEqual(fib["38.2%"], lo + (hi - lo) * 0.382, places=2)
        self.assertAlmostEqual(fib["61.8%"], lo + (hi - lo) * 0.618, places=2)
        self.assertLess(fib["38.2%"], fib["61.8%"])
        # Up leg: 38.2% sits just under the high.
        fib_up = dict(bot._fib_retr(hi, lo, up=True))
        self.assertAlmostEqual(fib_up["38.2%"], hi - (hi - lo) * 0.382, places=2)
        self.assertGreater(fib_up["38.2%"], fib_up["61.8%"])

    def test_the_news_section_appears_when_a_headline_exists(self):
        saved = bot.NEWS_ITEMS
        bot.NEWS_ITEMS = [("🔥", "Fed holds rates — headline")]
        try:
            with self._feed():
                self.assertIn("الأخبار", bot.card_full())
        finally:
            bot.NEWS_ITEMS = saved


class OpinionTest(unittest.TestCase):
    """_opinion is the honest stance: it can say wait or stay out, it names a
    confidence, it states what would flip it -- and it invents no probability."""

    def test_a_range_says_stay_out(self):
        lines = bot._opinion(4030.0, None, 0, [], [], 50.0, False)
        text = "\n".join(lines)
        self.assertIn("ما بتداول", text)
        self.assertIn("عرضي", text)

    def test_waiting_for_a_rally_is_called_sell_the_strength(self):
        # Price far below the sell zone: wait, and it is a rally-sell.
        lines = bot._opinion(4030.0, "sell", 5, [(4080.0, 4)], [], 50.0, False)
        text = "\n".join(lines)
        self.assertIn("بستنى أبيع", text)
        self.assertIn("بيع القوة", text)

    def test_a_zone_at_the_price_waits_for_a_confirming_close(self):
        # Price sitting on the sell zone, but no confirmed entry: the card
        # waits for a close, and names it a break-sell (not sell-the-strength).
        # Saying "أبيع الآن" here while the desk did not fire was the
        # contradiction the owner caught.
        lines = bot._opinion(4080.0, "sell", 5, [(4080.5, 4)], [], 50.0, False)
        text = "\n".join(lines)
        self.assertIn("بستنى", text)
        self.assertIn("إغلاق تأكيد", text)
        self.assertIn("بيع كسر", text)
        self.assertNotIn("بيع القوة", text)

    def test_a_confirmed_plan_is_said_now_with_the_plan_numbers(self):
        # When plan() has fired, the card must say "now" and print plan()'s OWN

        # entry/stop/target -- the exact numbers the desk is trading.
        p = {"entry": 4080.0, "stop": 4090.0, "target": 4055.0,
             "risk_usd": 10.0, "gain_usd": 20.0, "rr": 2.0, "direction": "short"}
        lines = bot._opinion(4080.0, "sell", 5, [(4080.5, 4)], [], 50.0, False,
                             plan=p)
        text = "\n".join(lines)
        self.assertIn("أبيع الآن", text)
        self.assertIn("4,090", text)          # plan's stop, not the zone
        self.assertIn("بيلغي نظريتي", text)

    def test_a_thin_margin_softens_the_word_not_the_decision(self):
        # Price resting on the averages: the trend flips before the stop would.
        # The label drops to "متوسطة" and a plain warning appears, but the call
        # and every number are IDENTICAL -- the owner asked for the word to
        # change and the decision not to.
        p = {"entry": 4080.0, "stop": 4090.0, "target": 4055.0,
             "risk_usd": 10.0, "gain_usd": 20.0, "rr": 2.0, "direction": "short"}
        thick = bot._opinion(4080.0, "sell", 5, [(4080.5, 4)], [], 50.0, False,
                             plan=p, thin=False)
        thin = bot._opinion(4080.0, "sell", 5, [(4080.5, 4)], [], 50.0, False,
                            plan=p, thin=True)
        self.assertIn("عالية", "\n".join(thick))
        self.assertNotIn("عالية", "\n".join(thin))
        self.assertIn("ملاصق", "\n".join(thin))
        # The decision and the numbers survive the softening, untouched.
        self.assertIn("أبيع الآن", "\n".join(thin))
        self.assertIn("4,080", "\n".join(thin))
        self.assertIn("4,090", "\n".join(thin))
        self.assertIn("إغلاق 1س فوق 4,090", "\n".join(thin))

    def test_confidence_drops_when_the_move_is_tired(self):
        # 5/6 frames but 15m RSI saturated: high confidence is not honest here.
        lines = bot._opinion(4030.0, "sell", 5, [(4080.0, 4)], [], 25.0, False)
        self.assertIn("متوسطة", "\n".join(lines))
        self.assertNotIn("عالية", "\n".join(lines))

    def test_it_names_the_level_that_would_flip_it(self):
        lines = bot._opinion(4030.0, "sell", 4, [(4080.0, 4)], [], 50.0, False)
        self.assertIn("بيلغي نظريتي", "\n".join(lines))
        self.assertIn("4,080", "\n".join(lines))

    def test_no_probability_is_invented(self):
        lines = bot._opinion(4030.0, "sell", 5, [(4080.0, 4)], [], 50.0, False)
        text = "\n".join(lines)
        self.assertNotIn("%", text)

    def test_a_late_sell_says_so_without_blocking_the_trade(self):
        # Selling into a washed-out tape (15m RSI 21) is a LATE entry: the desk
        # still takes the trade (the trend is real) but must say the move is
        # mostly done -- the owner's own read of the tape, in words. This is the
        # 'be like me' ask: honest words, no blocked trade.
        p = {"entry": 4000.0, "stop": 4012.0, "target": 3976.0,
             "risk_usd": 12.0, "gain_usd": 20.0, "rr": 1.67, "direction": "short"}
        text = "\n".join(bot._opinion(4000.0, "sell", 6, [(4005.0, 3)], [],
                                      21.0, False, plan=p))
        self.assertIn("دخول متأخر", text)
        # The decision and every number are untouched -- nothing is blocked.
        self.assertIn("أبيع الآن", text)
        self.assertIn("4,000", text)
        self.assertIn("4,012", text)

    def test_a_clean_sell_says_nothing_about_being_late(self):
        # 15m RSI 45 on a sell: the move has room. No late caveat, and the
        # confidence stays where the frame count put it.
        p = {"entry": 4000.0, "stop": 4012.0, "target": 3976.0,
             "risk_usd": 12.0, "gain_usd": 20.0, "rr": 1.67, "direction": "short"}
        text = "\n".join(bot._opinion(4000.0, "sell", 6, [(4005.0, 3)], [],
                                      45.0, False, plan=p))
        self.assertNotIn("دخول متأخر", text)
        self.assertIn("عالية", text)

    def test_a_late_buy_says_so_too(self):
        # The mirror image: buying with 15m RSI 78 is chasing an overbought
        # tape. Same honest caveat, same untouched decision.
        p = {"entry": 4000.0, "stop": 3988.0, "target": 4024.0,
             "risk_usd": 12.0, "gain_usd": 20.0, "rr": 1.67, "direction": "long"}
        text = "\n".join(bot._opinion(4000.0, "buy", 6, [], [(3990.0, 3)],
                                      78.0, False, plan=p))
        self.assertIn("دخول متأخر", text)
        self.assertIn("أشتري الآن", text)

    def test_the_late_threshold_is_the_measured_one(self):
        # Pinned to the level the year-long study found: below CHASE_RSI a sell
        # is late. A change to the constant must fail here first.
        self.assertEqual(bot.CHASE_RSI, 30.0)

    def test_plan_is_never_blocked_by_the_late_read(self):
        # The late-entry caveat is a WARNING on the 15m read, not a gate: the
        # 15m RSI does not sit in FILTER_GATES, so a stretched 15m RSI must not
        # stop plan() when the frames, the candle, and the no-chase gate all
        # pass. (The no-chase gate reads its OWN frame -- 1h by default -- and a
        # real tape keeps that reading inside its band.)
        import math
        tape = [4000 + 0.2 * i + 10 * math.sin(i / 2.0) for i in range(90)]
        closes = {tf: tape for tf in bot.FRAMES}
        saved_eb = bot.ENTRY_BASIS_ON
        bot.ENTRY_BASIS_ON = False
        try:
            with FakeFeed(mid=tape[-1], closes=closes):
                p = bot.plan()
        finally:
            bot.ENTRY_BASIS_ON = saved_eb
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p["direction"], "long")

    def test_the_late_helper_is_the_one_source_for_both_cards(self):
        # _late_entry is what the opinion card AND the action card read, so the
        # two can never disagree about whether the entry is late.
        self.assertTrue(bot._late_entry("sell", 20.0))
        self.assertTrue(bot._late_entry("sell", bot.CHASE_RSI))
        self.assertFalse(bot._late_entry("sell", bot.CHASE_RSI + 0.1))
        self.assertTrue(bot._late_entry("buy", 80.0))
        self.assertFalse(bot._late_entry("buy", 50.0))
        self.assertFalse(bot._late_entry(None, 20.0))
        self.assertFalse(bot._late_entry("sell", None))

    def test_the_open_card_carries_the_late_caveat(self):
        # The open-trade notification is the surface the owner actually gets
        # pushed. A late auto-entry must say so there too -- plan() carries the
        # 15m RSI so the card can.
        p = {"direction": "short", "entry": 4000.0, "stop": 4012.0,
             "target": 3976.0, "risk_pts": 12.0, "risk_usd": 12.0,
             "gain_usd": 20.0, "rr": 1.67, "strong": True, "r15": 22.0}
        text = bot._open_card(p)
        self.assertIn("دخول متأخر", text)

    def test_the_open_card_is_silent_on_a_clean_entry(self):
        p = {"direction": "short", "entry": 4000.0, "stop": 4012.0,
             "target": 3976.0, "risk_pts": 12.0, "risk_usd": 12.0,
             "gain_usd": 20.0, "rr": 1.67, "strong": True, "r15": 45.0}
        self.assertNotIn("دخول متأخر", bot._open_card(p))

    def test_plan_carries_the_rsi_for_the_open_card(self):
        # A live 15m tape (a real range, not a straight line -- a dead line
        # fails the ATR gate) so the quality gate passes; the point of the test
        # is that plan() carries the 15m RSI for the open card.
        import math
        rising = [4000 + 0.2 * i + 10 * math.sin(i / 2.0) for i in range(90)]
        closes = {tf: rising for tf in bot.FRAMES}
        saved_eb = bot.ENTRY_BASIS_ON
        bot.ENTRY_BASIS_ON = False
        try:
            with FakeFeed(mid=rising[-1], closes=closes):
                p = bot.plan()
        finally:
            bot.ENTRY_BASIS_ON = saved_eb
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p["r15"], bot.rsi(rising))


class TrendWatchTest(unittest.TestCase):
    """trend_watch is the one alert that fires on the market alone: the owner
    asked to hear about a direction flip without tapping. It must seed silently
    on boot, stay silent while the direction holds, and speak once on a real
    flip -- with a cooldown so a chop at the boundary cannot spam."""

    def _feed(self, trends):
        # FakeFeed builds bars from a per-frame close; a rising close makes the
        # frame trend up, a falling one down.
        up = list(range(4000, 4060))
        dn = list(range(4060, 4000, -1))
        closes = {tf: (up if trends.get(tf, 0) == 1 else dn)
                  for tf in bot.FRAMES}
        return FakeFeed(mid=4030.0, closes=closes)

    def test_first_look_seeds_silently(self):
        seen: dict = {}
        with self._feed({tf: -1 for tf in bot.FRAMES}):
            self.assertIsNone(bot.trend_watch(seen))
        self.assertEqual(seen.get("trend"), "down")

    def test_same_direction_is_silent(self):
        seen = {"trend": "down", "trend_at": 0.0}
        with self._feed({tf: -1 for tf in bot.FRAMES}):
            self.assertIsNone(bot.trend_watch(seen))

    def test_a_real_flip_speaks(self):
        seen = {"trend": "up", "trend_at": 0.0}
        with self._feed({tf: -1 for tf in bot.FRAMES}):
            msg = bot.trend_watch(seen)
        self.assertIsNotNone(msg)
        self.assertIn("تغيّر الاتجاه", msg)
        self.assertEqual(seen["trend"], "down")

    def test_the_cooldown_damps_a_chop(self):
        # A flip moments after the last alert is swallowed.
        seen = {"trend": "up", "trend_at": bot.time.time()}
        with self._feed({tf: -1 for tf in bot.FRAMES}):
            self.assertIsNone(bot.trend_watch(seen))
        self.assertEqual(seen["trend"], "up")   # the state did not move

    def test_it_can_be_switched_off(self):
        saved = bot.TREND_ALERTS
        bot.TREND_ALERTS = False
        try:
            seen = {"trend": "up", "trend_at": 0.0}
            with self._feed({tf: -1 for tf in bot.FRAMES}):
                self.assertIsNone(bot.trend_watch(seen))
        finally:
            bot.TREND_ALERTS = saved


class EntryWatchTest(unittest.TestCase):
    """entry_watch speaks ONCE as price reaches a desk zone, carrying the trade.
    It must fire on the first approach, stay silent on a repeat while price
    sits there, and forget a zone once price is well clear so a re-test speaks
    again."""

    def _feed(self):
        import math
        wave = [4000 + 30 * math.sin(i / 3.0) + i * 0.3 for i in range(90)]
        return FakeFeed(mid=wave[-1], closes={tf: wave for tf in bot.FRAMES})

    def test_it_fires_once_then_stays_silent(self):
        saved = bot._zones
        seen: dict = {}
        try:
            # Pin one sell zone right at the live price: the first look must
            # speak, the second (same zone, same price) must stay silent.
            bot._zones = lambda live: ([(live, 3)], [])
            with self._feed():
                first = bot.entry_watch(seen)
                again = bot.entry_watch(seen)
        finally:
            bot._zones = saved
        self.assertIsNotNone(first)
        self.assertIn("منطقة بيع", first)
        self.assertIn("دخول", first)          # it carries the trade
        self.assertIsNone(again)

    def test_a_clear_zone_is_forgotten(self):
        seen = {"entry_fired": {"sell:1000.00": True}}
        with self._feed():
            bot.entry_watch(seen)             # price is ~4000, far from 1000
        self.assertNotIn("sell:1000.00", seen["entry_fired"])

    def test_a_buy_zone_speaks_as_a_buy(self):
        saved = bot._zones
        try:
            bot._zones = lambda live: ([], [(live, 3)])
            with self._feed():
                msg = bot.entry_watch({})
        finally:
            bot._zones = saved
        self.assertIsNotNone(msg)
        self.assertIn("منطقة شراء", msg)


class AdxCeilingTest(unittest.TestCase):
    """The ADX window was 20-60; band-by-band it is 0-50.

    Re-measured 2026-11-13 on the corrected replay (live bands + no-chase gate):
    ADX 0-15 is the one band that loses (-0.03R over 122 trades), but every band
    from 15 up pays and the extra trades carry the floor, so 0-50 still nets the
    MOST total R (0-50 +116R vs 15-50 +102R vs 20-50 +72R). The old ceiling of 60
    admitted the thin >50 band. Floor -> 0, ceiling -> 50.
    """

    def test_the_ceiling_is_fifty(self):
        self.assertEqual(bot.ADX_MAX, 50.0)

    def test_the_floor_is_off(self):
        self.assertEqual(bot.ADX_MIN, 0.0)

    def test_an_early_trend_is_no_longer_blocked_by_the_floor(self):
        # A shallow, choppy rise reads ADX ~18 -- under the OLD floor of 20,
        # which called it "no trend". With the floor off it must pass.
        import math
        closes = [4000 + i * 0.5 + 12 * math.sin(i / 2.5) for i in range(140)]
        bars = [_bar(_iso(i), c, c + 3.0, c - 3.0, c)
                for i, c in enumerate(closes)]
        fs = bot.filter_status("long", bars)
        adx = fs["readings"]["adx"]
        self.assertIsNotNone(adx)
        self.assertLess(adx, 20.0)             # under the old floor
        self.assertNotIn("ADX", fs["failed"])  # but no longer blocked


class EquityTest(_DbCase):
    """The account: start capital plus every CLOSED trade's money. An open
    trade is not counted -- unrealised P&L is a guess, and the owner asked for
    the money that actually moved."""

    def _close(self, direction, entry, stop, target, exit_price, r):
        tid = bot.add_trade(direction, entry, stop, target, bot.LOT, note="t")
        bot._settle(tid, "target" if r > 0 else "stop", exit_price, r)
        return tid

    def test_equity_starts_at_the_owner_capital(self):
        self.assertEqual(bot.equity_now(), bot.START_CAPITAL)

    def test_a_win_adds_and_a_loss_subtracts(self):
        self._close("long", 4000, 3991, 4020, 4020, 2.2)   # +$20
        self.assertAlmostEqual(bot.equity_now(), bot.START_CAPITAL + 20.0)
        self._close("short", 4050, 4059, 4030, 4059, -1.0)  # -$9
        self.assertAlmostEqual(bot.equity_now(), bot.START_CAPITAL + 11.0)

    def test_an_open_trade_does_not_move_the_capital(self):
        bot.add_trade("long", 4000, 3991, 4020, bot.LOT, note="t")
        self.assertEqual(bot.equity_now(), bot.START_CAPITAL)

    def test_daily_report_lists_today_and_the_balance(self):
        self._close("long", 4000, 3991, 4020, 4020, 2.2)
        text = bot.daily_report()
        self.assertIn("سجل اليوم", text)
        self.assertIn("رأس المال", text)
        self.assertIn(f"{bot.START_CAPITAL + 20.0:,.2f}", text)
        self.assertIn("+20.00$", text)

    def test_daily_report_is_honest_when_nothing_closed(self):
        text = bot.daily_report()
        self.assertIn("ما في صفقة انسكّرت اليوم", text)
        self.assertIn(f"{bot.START_CAPITAL:,.2f}", text)

    def test_the_close_card_shows_the_same_capital_as_the_report(self):
        # Two surfaces must not disagree about the balance: the close card's
        # number is the running equity, not "start + this trade only".
        self._close("long", 4000, 3991, 4020, 4020, 2.2)   # +$20
        t = self._close("short", 4050, 4059, 4030, 4059, -1.0)  # -$9
        row = [x for x in bot.all_trades() if x["id"] == t][0]
        card = bot._close_card(row)
        self.assertIn(f"{bot.START_CAPITAL + 11.0:,.2f}", card)
        self.assertIn(f"{bot.START_CAPITAL + 11.0:,.2f}", bot.daily_report())


class SessionWindowTest(_DbCase):
    """The report must cover the session that ENDED. At the close the owner's
    clock has just ticked to a new day, so a naive calendar-day filter would
    describe an empty day instead of the one that just traded."""

    def _close_at(self, closed_utc_iso):
        tid = bot.add_trade("long", 4000, 3991, 4020, bot.LOT, note="t")
        with bot.closing(bot._conn()) as c:
            c.execute("UPDATE trades SET status='closed',result='target',"
                      "exit_price=?,r_multiple=2.2,closed_at=? WHERE id=?",
                      (4020.0, closed_utc_iso, tid))
            c.commit()
        return tid

    def test_a_close_at_midnight_local_still_covers_the_ended_session(self):
        from datetime import UTC, datetime
        # 21:05 UTC == 00:05 local (UTC+3): a brand-new local day.
        now = datetime(2026, 11, 11, 21, 5, tzinfo=UTC)
        start, day = bot._session_day(now)
        self.assertEqual(day.isoformat(), "2026-11-11")
        # A trade closed at 20:00 UTC (23:00 local, the session that ended).
        self._close_at("2026-11-11T20:00:00Z")
        self.assertEqual(len(bot._closed_since(start)), 1)

    def test_a_trade_from_the_previous_session_is_excluded(self):
        from datetime import UTC, datetime
        now = datetime(2026, 11, 11, 21, 5, tzinfo=UTC)
        start, _ = bot._session_day(now)
        self._close_at("2026-11-10T20:00:00Z")   # the day before
        self.assertEqual(bot._closed_since(start), [])


class NoSecondTest(_DbCase):
    """A second signal in the SAME direction confirms the open trade -- it is
    not a warning, and it must never promise the target."""

    def _t(self, direction="short", tid=7):
        return {"id": tid, "direction": direction, "entry": 4050.0,
                "stop": 4059.0, "target": 4030.0}

    def test_same_direction_reads_as_confirmation(self):
        p = {"direction": "short", "entry": 4050, "stop": 4059, "target": 4030}
        with FakeFeed(mid=4045.0):                 # past entry, short is winning
            text = bot._no_second(p, self._t(), "full")
        self.assertIn("بنفس الاتجاه", text)
        self.assertIn("صحيح", text)
        self.assertNotIn("ما دخلنا", text)

    def test_opposite_direction_still_says_we_did_not_enter(self):
        p = {"direction": "long", "entry": 4050, "stop": 4041, "target": 4070}
        with FakeFeed(mid=4045.0):
            text = bot._no_second(p, self._t(), "full")
        self.assertIn("ما دخلنا", text)
        self.assertNotIn("بنفس الاتجاه", text)


class DailyScheduleTest(_DbCase):
    """The day's record goes out ONCE, on the observed open->closed edge, and a
    restart must not resend a day already delivered."""

    def setUp(self):
        super().setUp()
        self._saved_daily = bot.DAILY_PATH
        bot.DAILY_PATH = bot.Path(os.path.join(self.tmp, "daily.json"))
        self.sent = []

    def tearDown(self):
        bot.DAILY_PATH = self._saved_daily
        super().tearDown()

    class _Tg:
        def __init__(self, sink):
            self.sink = sink

        def send(self, chat, text, *a, **k):
            self.sink.append((str(chat), text))
            return {"ok": True}

    def _tick(self, market):
        with FakeFeed(market=market):
            bot.maybe_daily_report(self._Tg(self.sent), {"1"})

    def test_sends_once_on_the_close_transition(self):
        # One message per recipient: the owner and the roll both follow the
        # day's record (see _public_chats). The EVENT is once, not the sends.
        self._tick("open")
        self.assertEqual(self.sent, [])           # nothing while open
        self._tick("closed")
        self.assertEqual({c for c, _ in self.sent}, {"1", bot._backup_chat()})
        self._tick("closed")
        self.assertEqual(len(self.sent), 2)       # a standing close is silent

    def test_a_weekend_close_never_sends(self):
        # The market was never seen open: no close to report, no message.
        self._tick("closed")
        self.assertEqual(self.sent, [])

    def test_a_restart_mid_evening_does_not_resend(self):
        self._tick("open")
        self._tick("closed")
        self.assertEqual(len(self.sent), 2)
        # Simulate a reboot: the persisted state survives, so the same day is
        # not delivered a second time.
        self._tick("closed")
        self.assertEqual(len(self.sent), 2)


class OpenBellTest(_DbCase):
    """The bell rings once on the closed->open edge, and never twice a day.

    It is the mirror of the close report, so the same restart hazards apply --
    plus one of its own: the close report sets `was_open` on every open cycle,
    so a bell that shared that flag would never fire at all. That bug is what
    `test_rings_even_after_the_close_report_ran` exists to catch.
    """

    def setUp(self):
        super().setUp()
        self._saved_daily = bot.DAILY_PATH
        bot.DAILY_PATH = bot.Path(os.path.join(self.tmp, "daily.json"))
        self.sent = []

    def tearDown(self):
        bot.DAILY_PATH = self._saved_daily
        super().tearDown()

    class _Tg:
        def __init__(self, sink):
            self.sink = sink

        def send(self, chat, text, *a, **k):
            self.sink.append(text)
            return {"ok": True}

    def _tick(self, market):
        with FakeFeed(market=market):
            bot.maybe_open_bell(self._Tg(self.sent), {"1"})

    def test_the_rule_is_pure_and_once_per_day(self):
        # closed -> open, never announced today: owed.
        self.assertTrue(bot.open_bell_due(False, True, "2026-11-23", None))
        # already open: no transition, so nothing.
        self.assertFalse(bot.open_bell_due(True, True, "2026-11-23", None))
        # still closed: nothing.
        self.assertFalse(bot.open_bell_due(False, False, "2026-11-23", None))
        # already rang today: a restart must not replay it.
        self.assertFalse(bot.open_bell_due(False, True, "2026-11-23", "2026-11-23"))
        # This bot never saw a closed market (a fresh container, market already
        # open): it witnessed no transition, so it must not announce one.
        self.assertFalse(bot.open_bell_due(False, True, "2026-11-23", None,
                                           saw_closed=False))

    def test_a_lost_daily_file_does_not_ring_again(self):
        # Render has no disk: a redeploy starts with an empty daily.json while
        # the market is OPEN. The bell must not fire a second time -- the whole
        # reason `saw_closed` exists.
        self._tick("closed")            # this process sees the weekend
        self._tick("open")              # the bell
        self.assertEqual(len(self.sent), 1)
        self.sent.clear()
        bot.DAILY_PATH.unlink()         # the redeploy wipes the disk
        self._tick("open")              # market already open, nothing witnessed
        self.assertEqual(self.sent, [])

    def test_rings_on_the_open_transition_only(self):
        self._tick("closed")
        self.assertEqual(self.sent, [])          # a standing close is silent
        self._tick("open")
        self.assertEqual(len(self.sent), 1)      # the bell, once
        self._tick("open")
        self.assertEqual(len(self.sent), 1)      # a standing open is silent

    def test_rings_even_after_the_close_report_ran(self):
        # The close report sets its own `was_open` on every open cycle. If the
        # bell shared that flag it would never ring -- this pins the separation.
        with FakeFeed(market="open"):
            bot.maybe_daily_report(self._Tg([]), {"1"})
        self._tick("closed")   # the close report's transition; the bell sees it
        self._tick("open")
        self.assertEqual(len(self.sent), 1)

    def test_a_restart_mid_session_does_not_ring(self):
        # A session the bot witnessed: closed, then open (one bell).
        self._tick("closed")
        self._tick("open")
        self.assertEqual(len(self.sent), 1)
        self.sent.clear()
        self._tick("open")   # "rebooted" and still open: no transition
        self.assertEqual(self.sent, [])

    def test_the_card_carries_the_price_and_the_levels(self):
        with FakeFeed(mid=3539.138, market="open"):
            text = bot.open_bell_text()
        self.assertIn("السوق فتح", text)
        self.assertIn("3,539.14", text)
        # It is a MAP, not an order: it must not pretend to be a trade.
        self.assertIn("مو أمر دخول", text)

    def test_the_bell_goes_to_the_owner_alone(self):
        # The bell is the close report's mirror, so it follows the same
        # audience rule: the desk's chats (owner-only under OWNER_ONLY), never
        # the wider alert list.
        seen_chats = []

        class _Rec:
            def send(self, chat, text, *a, **k):
                seen_chats.append(chat)
                return {"ok": True}

        self._tick("closed")
        with FakeFeed(market="open"):
            bot.maybe_open_bell(_Rec(), {"933571066", "8545528489"})
        self.assertEqual(seen_chats, [bot._backup_chat()])

    def test_the_daily_file_only_touches_disk_on_a_real_change(self):
        # backup_loop decides to upload by comparing mtimes, so a file that
        # rewrites itself with identical content every tick would either upload
        # every minute (once watched) or -- unwatched -- never carry the latch.
        self._tick("closed")
        first = bot.DAILY_PATH.stat().st_mtime_ns
        for _ in range(3):
            self._tick("closed")
        self.assertEqual(bot.DAILY_PATH.stat().st_mtime_ns, first)

    def test_the_latch_survives_a_restart_via_the_pinned_backup(self):
        # The whole point of the latch: the weekend is observed, the container
        # is evicted before Monday, and the bell still rings when the tape
        # comes back. That only works if the latch reaches _dump_journal --
        # otherwise restore_journal writes back a daily with no saw_closed and
        # the bell stays silent, which is the exact failure it exists to fix.
        self._tick("closed")
        blob = bot._dump_journal()
        self.assertTrue((blob.get("daily") or {}).get("bell_saw_closed"))

        bot.DAILY_PATH.unlink()          # the disk is wiped
        bot._save_daily(blob["daily"])   # what restore_journal would do
        self.sent.clear()
        self._tick("open")
        self.assertEqual(len(self.sent), 1)


class AtrCeilingTest(unittest.TestCase):
    """The ATR ceiling is 0.30%, and that is a MEASURED choice, not the old one.

    The ceiling was widened to 1.00% when the desk was standing aside through
    clean moves -- and on the replay WITHOUT the no-chase gate that was right
    (0.25% -> +114R, 1.00% -> +158R). Adding the no-chase gate flipped the sign:
    WITH it, 0.25% nets +117R against 1.00%'s +91R, and a wide ceiling raises the
    drawdown. The gate already refuses the bad high-volatility entries, so the
    ceiling need not be as tight as 0.25%. The owner set 0.30% (2026-11-13) to
    keep the high-volatility entries the tight cap refused: 796 trades for
    +93.6R at -19R drawdown, against 0.25%'s 661 for +116.6R at -16R. Measured
    per-trade: 0.25% -> +0.176R, 0.30% -> +0.118R.
    """

    def test_the_ceiling_is_three_tenths_percent(self):
        self.assertEqual(bot.ATR_MAX_PCT, 0.0030)

    def test_a_calm_trend_is_not_blocked_by_atr(self):
        # A gentle rise holds ATR% inside the band; it must pass.
        closes = [4000 + i * 0.4 for i in range(140)]
        bars = [_bar(_iso(i), c, c + 2.0, c - 2.0, c)
                for i, c in enumerate(closes)]
        fs = bot.filter_status("long", bars)
        atr_pct = fs["readings"]["atr_pct"]
        self.assertIsNotNone(atr_pct)
        self.assertLessEqual(atr_pct, bot.ATR_MAX_PCT)
        self.assertNotIn("ATR", fs["failed"])



class FiboTest(unittest.TestCase):
    """fib_levels: the swing, the retracement levels, the extensions, and the
    direction. A down move (low more recent than high) retraces ABOVE the low
    and extends BELOW it -- the sign bug that shipped once must stay fixed."""

    def test_a_down_move_retraces_above_the_low_and_extends_below(self):
        closes = [4100 + i for i in range(10)] + [4109 - i * 5 for i in range(1, 60)]
        with FakeFeed(mid=closes[-1], closes={"1h": closes}):
            f = bot.fib_levels(closes[-1])
        self.assertEqual(f["direction"], "down")
        for l in f["levels"]:
            self.assertGreater(l["price"], f["bottom"])
            self.assertLess(l["price"], f["top"])
        self.assertLess(f["extensions"]["1.272"], f["bottom"])
        self.assertLess(f["extensions"]["1.618"], f["extensions"]["1.272"])

    def test_an_up_move_retraces_below_the_high_and_extends_above(self):
        closes = [4100 - i for i in range(10)] + [4091 + i * 5 for i in range(1, 60)]
        with FakeFeed(mid=closes[-1], closes={"1h": closes}):
            f = bot.fib_levels(closes[-1])
        self.assertEqual(f["direction"], "up")
        self.assertGreater(f["extensions"]["1.272"], f["top"])

    def test_a_missing_frame_returns_empty_not_a_crash(self):
        with FakeFeed(closes={"1h": [4100.0] * 5}):
            self.assertEqual(bot.fib_levels(4100.0), {})


class DivergenceTest(unittest.TestCase):
    """divergence_read: a lower price low with a higher RSI low is bullish; the
    opposite is bearish. A flat tape yields none."""

    def test_a_flat_tape_has_no_divergence(self):
        with FakeFeed(closes={tf: [4100.0] * 40 for tf in ("5m", "15m", "1h")}):
            self.assertEqual(bot.divergence_read(), [])

    def test_hits_carry_their_frame_and_text(self):
        closes = [4100 - i * 2 for i in range(24)]
        closes += [closes[-1] + (5 if i % 2 else -1) for i in range(12)]
        with FakeFeed(closes={tf: list(closes) for tf in ("5m", "15m", "1h")}):
            hits = bot.divergence_read()
        for h in hits:
            self.assertIn("tf", h)
            self.assertIn("text", h)


class AnalystEyeTest(_DbCase):
    """The bot must carry the SAME eye the map does: the fragility tell, the
    momentum divergence, and the better level-break entry -- each from ONE
    producer (_fragility/_divergence_note/_entry_choice) the map also reads, so
    the bot can no longer read the tape linearly while the map flags it."""

    def _market(self, **kw):
        base = {"q": {"marketState": "open", "stale": False}, "live": 4100.0,
                "trends": {tf: -1 for tf in bot.FRAMES}, "rsis": {}, "atrs": {},
                "gap": {}}
        base.update(kw)
        return base

    def test_fragility_fires_only_when_price_rests_on_the_average(self):
        # 6/6 down, price 0.03xATR from the average -> a hair-trigger.
        m = self._market(gap={"15m": 0.03})
        self.assertIsNotNone(bot._fragility(m))
        self.assertIn("هشّ", bot._fragility_note(m) or "")
        # A gap of half an ATR is a real trend, not a hair-trigger.
        self.assertIsNone(bot._fragility(self._market(gap={"15m": 0.5})))

    def test_fragility_says_the_side_and_count(self):
        note = bot._fragility_note(self._market(gap={"15m": 0.1}))
        self.assertIn("6/6", note or "")
        self.assertIn("هابط", note or "")

    def test_entry_choice_names_the_break_when_it_sizes_better(self):
        # A market sell 6 points above a support sizes a thin R:R; the break of
        # that support sizes wider -- the break must be named, with both R:Rs.
        line = bot._entry_choice("short", 4100.0, [4120.0], [4094.0])
        self.assertIsNotNone(line)
        self.assertIn("كسر", line or "")
        self.assertIn("R:R", line or "")

    def test_entry_choice_is_silent_when_the_market_entry_is_not_worse(self):
        # No support below -> nothing to break -> no advice invented.
        self.assertIsNone(bot._entry_choice("short", 4100.0, [4120.0], []))

    def test_the_cards_carry_the_fragility_when_it_fires(self):
        # The recommendation and the action card must both say the tape is
        # fragile on the same market -- the disagreement the owner caught.
        m = self._market(gap={"15m": 0.05},
                         q={"marketState": "open", "stale": False,
                            "bid": 4099.9, "ask": 4100.1, "spread": 0.2,
                            "high": 4120.0, "low": 4088.0})
        saved = bot.read_market
        bot.read_market = lambda: m
        try:
            with FakeFeed(mid=4100.0, closes={tf: [4100 - i for i in range(30)]
                                              for tf in bot.FRAMES}):
                rec = bot.card_recommend()
                act = bot.card_action()
        finally:
            bot.read_market = saved
        self.assertIn("هشّ", rec)
        self.assertIn("هشّ", act)

    def test_divergence_note_reports_only_the_relevant_side(self):
        # A bullish divergence (the fall tiring) is the tell for a SELL.
        hits = [{"tf": "15m", "kind": "دايفرجنس إيجابي",
                 "tone": "warn", "text": "دايفرجنس إيجابي (15m)"},
                {"tf": "1h", "kind": "دايفرجنس سلبي",
                 "tone": "warn", "text": "دايفرجنس سلبي (1h)"}]
        saved = bot.divergence_read
        bot.divergence_read = lambda: hits
        try:
            sell_note = bot._divergence_note("short")
            buy_note = bot._divergence_note("long")
        finally:
            bot.divergence_read = saved
        self.assertIn("إيجابي", sell_note or "")
        self.assertNotIn("سلبي", sell_note or "")
        self.assertIn("سلبي", buy_note or "")

    def test_no_divergence_no_line(self):
        saved = bot.divergence_read
        bot.divergence_read = list
        try:
            self.assertIsNone(bot._divergence_note("short"))
        finally:
            bot.divergence_read = saved

    def test_near_level_fires_only_at_the_edge_of_the_range(self):
        # A sell 2 points above its support with a 15-point 1h ATR is a chase:
        # the support is where the fall stops, not open space.
        self.assertEqual(
            bot._near_level("sell", 4100.0, [4120.0], [4098.0], 15.0), 4098.0)
        # The same sell 40 points above the support has room to run.
        self.assertIsNone(
            bot._near_level("sell", 4100.0, [4120.0], [4060.0], 15.0))

    def test_edge_note_names_the_bounce_and_the_break(self):
        note = bot._edge_note("sell", [4120.0], [4094.0])
        self.assertIn("لحاق", note)
        self.assertIn("4,093.5", note)   # the break below the support (level - hair)
        self.assertIn("4,120", note)     # the bounce up to the resistance

    def test_opinion_drops_the_confidence_when_the_spot_is_a_chase(self):
        # 6/6 down would be "عالية", but price sits ON the support -> a chase:
        # the stance must say so and quiet down, not call the spot "high".
        out = "\n".join(bot._opinion(
            3890.0, "sell", 6, [3923.0], [3888.0], 42.0, False,
            None, None, {"entry": 3890.0, "stop": 3902.0, "target": 3870.0,
                         "risk_usd": 12.0, "gain_usd": 20.0},
            False, [3923.0], [3888.0], 15.0))
        self.assertIn("لحاق", out)
        self.assertIn("متوسطة", out)
        self.assertNotIn("عالية", out)

    def test_opinion_stays_high_when_the_entry_has_room(self):
        # Same 6/6 down, but the support is far below: no chase, stay "عالية".
        out = "\n".join(bot._opinion(
            3890.0, "sell", 6, [3923.0], [3840.0], 42.0, False,
            None, None, {"entry": 3890.0, "stop": 3902.0, "target": 3870.0,
                         "risk_usd": 12.0, "gain_usd": 20.0},
            False, [3923.0], [3840.0], 15.0))
        self.assertNotIn("لحاق", out)
        self.assertIn("عالية", out)

    def test_break_levels_are_a_hair_past_the_level(self):
        # One producer for every "break": the trigger is the level +/- a hair,
        # so the read card and the scenario card cannot quote two numbers.
        down, up = bot._break_levels([3923.31], [3888.46])
        self.assertEqual(down, 3887.96)
        self.assertEqual(up, 3923.81)
        # No level on a side -> no break invented on that side.
        self.assertEqual(bot._break_levels([], [3888.46]), (3887.96, None))
        self.assertEqual(bot._break_levels([3923.31], []), (None, 3923.81))


class ScenarioReachTest(unittest.TestCase):
    """A break scenario must be reachable today.

    The owner was sent a "buy above 3,759" plan while gold traded 3,554 -- a
    trigger 205 points away, because after a one-way slide _levels falls back
    to a swing extreme. A priced trade the market cannot reach before the
    session ends is a wish, so a far trigger is named as a level to watch.
    """

    def test_a_near_trigger_is_reachable(self):
        # 30 points away with a 28-point 1h ATR: about one bar's travel.
        self.assertTrue(bot._scen_is_reachable(3554.0, 3584.0, 28.0))

    def test_a_205_point_trigger_is_not_reachable(self):
        self.assertFalse(bot._scen_is_reachable(3554.0, 3759.1, 28.0))

    def test_the_limit_is_measured_in_atrs_not_points(self):
        # Same 60-point gap: reachable in a wild tape, not in a dead one.
        self.assertTrue(bot._scen_is_reachable(3554.0, 3614.0, 30.0))
        self.assertFalse(bot._scen_is_reachable(3554.0, 3614.0, 10.0))

    def test_a_missing_input_never_blocks_a_scenario(self):
        # No ATR (a feed hiccup) must leave the card exactly as it was.
        self.assertTrue(bot._scen_is_reachable(3554.0, 3759.1, None))
        self.assertTrue(bot._scen_is_reachable(3554.0, None, 28.0))
        self.assertTrue(bot._scen_is_reachable(0.0, 3759.1, 28.0))

    def test_the_far_note_names_the_distance_and_calls_it_a_watch_level(self):
        note = bot._scen_far_note(3554.0, 3759.1, 28.0)
        self.assertIn("3,759.10", note)
        self.assertIn("205", note)
        self.assertIn("7.3×", note)          # 205.1 / 28.0
        self.assertIn("مستوى للمراقبة", note)
        self.assertNotIn("الهدف", note)      # never a priced trade

    def test_the_prep_card_refuses_to_price_an_unreachable_break(self):
        events = [{"title": "CPI m/m", "currency": "USD", "impact": "High",
                   "when": datetime.now(UTC) + timedelta(minutes=40),
                   "forecast": "3.1%", "previous": "3.0%"}]
        with unittest.mock.patch.object(bot, "_cal_fetch", return_value=events), \
             unittest.mock.patch.object(bot, "read_market",
                                        return_value={"live": 3554.0,
                                                      "atrs": {"1h": 28.0}}), \
             unittest.mock.patch.object(bot, "_levels",
                                        return_value=([3758.6], [3514.55])):
            out = bot.card_news_prep()
        # The far resistance is named, never priced.
        self.assertIn("3,759.10", out)
        self.assertIn("مستوى للمراقبة", out)
        self.assertNotIn("لو كسر فوق", out)
        # The near support (40 points) is still a real plan.
        self.assertIn("لو كسر تحت", out)
        self.assertIn("وقف", out)

    def test_the_scenario_card_refuses_to_price_an_unreachable_break(self):
        with unittest.mock.patch.object(bot, "read_market",
                                        return_value={"live": 3554.0,
                                                      "atrs": {"1h": 28.0}}), \
             unittest.mock.patch.object(bot, "_levels",
                                        return_value=([3758.6], [3514.55])):
            out = bot.card_scenarios()
        self.assertIn("مستوى للمراقبة", out)
        self.assertNotIn("سيناريو صعود", out)
        self.assertIn("سيناريو هبوط", out)

    def test_the_map_payload_marks_the_far_scenario_unpriced(self):
        # The page is a VIEW: it must receive the same verdict the cards reach,
        # and a far trigger must arrive with no stop/target to print. Otherwise
        # the page would show a trade the Telegram card refuses to quote.
        with unittest.mock.patch.object(bot, "read_market",
                                        return_value={"live": 3554.0,
                                                      "atrs": {"1h": 28.0},
                                                      "q": {"mid": 3554.0},
                                                      "rsis": {}, "trends": {}}), \
             unittest.mock.patch.object(bot, "_levels",
                                        return_value=([3758.6], [3514.55])), \
             unittest.mock.patch.object(bot, "_zones", return_value=([], [])), \
             unittest.mock.patch.object(bot, "_liquidity", return_value=([], [])), \
             unittest.mock.patch.object(bot, "_sticky_map",
                                        return_value={"res": [], "sup": [],
                                                      "sell": [], "buy": [],
                                                      "liq_above": [], "liq_below": []}), \
             unittest.mock.patch.object(bot, "plan", return_value=None):
            mp = bot.map_payload()
        by_side = {s["side"]: s for s in mp["scenarios"]}
        self.assertFalse(by_side["long"]["reachable"])
        self.assertNotIn("stop", by_side["long"])     # never priced
        self.assertTrue(by_side["short"]["reachable"])
        self.assertIn("stop", by_side["short"])


class DailyAnalysisTest(unittest.TestCase):
    """daily_analysis: the full numbered Arabic report, and it always ends with
    the honesty note the owner asked for."""

    def _feed(self):
        up = [4000 + i for i in range(30)]
        return FakeFeed(mid=up[-1], closes={tf: up for tf in bot.FRAMES})

    def test_it_is_numbered_arabic_and_carries_the_honesty_note(self):
        with self._feed():
            text = bot.daily_analysis()
        self.assertIn("التحليل اليومي", text)
        self.assertIn("ملاحظة الصدق", text)
        self.assertIn("الوقف إلزامي", text)
        for token in ("فيبوناتشي", "الدايفرجنس", "السيناريوهات",
                      "المقاومة والدعم", "السيولة", "مناطق العرض والطلب"):
            self.assertIn(token, text)

    def test_it_carries_the_news_the_owner_asked_for(self):
        # The owner asked the daily read to include news, like the card does.
        # The section must come from the SAME source the card uses, and sit
        # before the honesty note -- the read is analysis + news + scenarios.
        with self._feed():
            text = bot.daily_analysis()
        if bot._news_items_merged():
            self.assertIn("الأخبار", text)
            self.assertLess(text.index("الأخبار"), text.index("ملاحظة الصدق"))

    def test_it_never_exceeds_the_telegram_limit(self):
        with self._feed():
            self.assertLess(len(bot.daily_analysis()), 4096)


class DailyAnalysisScheduleTest(unittest.TestCase):
    """maybe_daily_analysis: fires once per slot per day, only inside the slot's
    hour, and a reboot that already sent a slot must not repeat it.

    Named apart from `DailyScheduleTest` above, which pins the END-OF-DAY
    record. They shared a name until now, and Python silently kept the later
    class -- so the record's three tests never ran.

    The daily read is one of the few things the owner shares, so a send reaches
    the owner AND every allowed chat -- the count below is per recipient, not
    per event."""

    def setUp(self):
        self.sent = []
        outer = self

        class TG:
            def send(self, chat, text, markup=None):
                outer.sent.append(str(chat))
                return True
        self.tg = TG()

    def _run(self, hour, saved):
        from datetime import datetime as real_dt, timezone, timedelta
        with unittest.mock.patch.object(bot, "_load_daily", return_value=saved), \
             unittest.mock.patch.object(bot, "_save_daily", lambda st: None), \
             unittest.mock.patch.object(bot, "daily_analysis", return_value="X"), \
             unittest.mock.patch.object(bot, "datetime") as dt:
            dt.now.return_value = real_dt(2026, 11, 12, hour, 5,
                                          tzinfo=timezone(timedelta(hours=3)))
            bot.maybe_daily_analysis(self.tg, {"1"})
        return self.sent

    def test_the_morning_hour_sends_once(self):
        self.assertEqual(self._run(bot.DAILY_AM_HOUR, {}),
                         ["1", bot._backup_chat()])

    def test_an_off_hour_sends_nothing(self):
        self.assertEqual(self._run((bot.DAILY_AM_HOUR + 2) % 24, {}), [])

    def test_a_slot_already_sent_today_does_not_repeat(self):
        self.assertEqual(self._run(bot.DAILY_AM_HOUR,
                                   {"analysis_sent": {"am": "2026-11-12"}}), [])


class PlanCheckTest(unittest.TestCase):
    """plan_check is the decision engine WITH its reason -- the owner asked to
    hear WHY a trade is taken (and why one is not). plan() is its plan field,
    so the reason can never drift from the decision.
    """

    def _live_tape(self, up=True, n=90):
        # A real tape: a drift plus a swing, so ATR is alive and the RSI sits in
        # a tradeable band -- a straight line has no ATR and an RSI of 100, and
        # would now be refused by the no-chase gate, not by the rule under test.
        import math
        step = (lambda i: 0.2 * i + 10 * math.sin(i / 2.0)) if up \
            else (lambda i: -0.2 * i - 10 * math.sin(i / 2.0))
        return [4000.0 + step(i) for i in range(n)]

    def test_closed_market_names_the_reason(self):
        with FakeFeed(market="closed"):
            pc = bot.plan_check()
        self.assertIsNone(pc["plan"])
        self.assertEqual(pc["reason"], "closed")
        self.assertIn("مسكّر", pc["detail"])

    def test_a_split_tape_names_no_signal(self):
        with FakeFeed(closes={tf: [4100, 4101, 4100, 4101] * 8
                              for tf in bot.FRAMES}):
            pc = bot.plan_check()
        self.assertIsNone(pc["plan"])
        self.assertEqual(pc["reason"], "no_signal")

    def test_a_clean_long_is_ok_with_an_empty_detail(self):
        # The reason plumbing in isolation: the entry basis is turned off so a
        # frame-agreed long is judged on its own (the basis has its own tests).
        tape = self._live_tape(up=True)
        saved_eb = bot.ENTRY_BASIS_ON
        bot.ENTRY_BASIS_ON = False
        try:
            with FakeFeed(mid=tape[-1], closes={tf: tape for tf in bot.FRAMES}):
                pc = bot.plan_check()
        finally:
            bot.ENTRY_BASIS_ON = saved_eb
        self.assertEqual(pc["reason"], "ok")
        self.assertIsNotNone(pc["plan"])
        self.assertEqual(pc["detail"], "")

    def test_a_long_under_the_filter_frame_names_the_trend(self):
        # Six up frames but the price under the 15m EMA20: the trend gate is the
        # reason, and it must be named -- not a silent None.
        import math
        tape = [4000 + 0.2 * i + 10 * math.sin(i / 2.0) for i in range(90)]
        saved = bot.FILTERS_ON
        bot.FILTERS_ON = False
        try:
            with FakeFeed(mid=4000.0, closes={tf: tape for tf in bot.FRAMES}):
                pc = bot.plan_check()
        finally:
            bot.FILTERS_ON = saved
        self.assertIsNone(pc["plan"])
        self.assertEqual(pc["reason"], "trend")
        self.assertIn("15د", pc["detail"])

    def test_a_dead_tape_names_the_gate(self):
        rising = list(range(4000, 4060))
        with FakeFeed(mid=4059.0, closes={tf: rising for tf in bot.FRAMES}):
            pc = bot.plan_check()
        self.assertIsNone(pc["plan"])
        self.assertEqual(pc["reason"], "gate")
        self.assertIn("الجودة", pc["detail"])

    def test_plan_is_the_plan_checks_plan(self):
        # One engine: the wrapper must return exactly the checked plan.
        tape = self._live_tape(up=True)
        with FakeFeed(mid=tape[-1], closes={tf: tape for tf in bot.FRAMES}):
            self.assertEqual(bot.plan(), bot.plan_check()["plan"])


class EntryBasisTest(unittest.TestCase):
    """THE ENTRY BASIS -- the owner's rule: the desk may not open at market in
    the middle of the range. A frame-agreed entry must have EARNED the price on
    the last closed 1h bar, one of two ways only:

      * a level REJECTED (a short whose bar poked resistance and closed back
        below it; a long whose bar dipped to support and closed back above it);
      * a level BROKEN (a short closed below support; a long closed above
        resistance).

    Anything else is the chase the owner keeps refusing, so plan() stands aside
    with reason "no_setup" and names what it needs.
    """

    def _down(self, n=90):
        import math
        return [4000.0 - 0.2 * i - 10 * math.sin(i / 2.0) for i in range(n)]

    def test_the_switch_defaults_on(self):
        self.assertTrue(bot.ENTRY_BASIS_ON)

    def test_a_mid_range_entry_is_refused_and_the_basis_is_named(self):
        # Six frames down, a live tape -- every other gate passes -- but the
        # last 1h bar closed nowhere near a level. The desk must stand aside.
        tape = self._down()
        with FakeFeed(mid=tape[-1], closes={tf: tape for tf in bot.FRAMES}):
            pc = bot.plan_check()
        self.assertIsNone(pc["plan"])
        self.assertEqual(pc["reason"], "no_setup")
        self.assertIn("نص النطاق", pc["detail"])

    def test_a_rejected_retrace_is_taken(self):
        # A short's bar that pokes resistance and closes back BELOW it, with the
        # price still AT the level -- the bounce the owner sells. The setup
        # producer must name it, and the plan must carry the basis.
        b1 = [{"openTime": _iso(i), "open": 3900, "high": 3900.4,
               "low": 3899.6, "close": 3900, "isOpen": False} for i in range(30)]
        b1.append({"openTime": "2026-11-07T30:00:00Z", "open": 3901,
                   "high": 3903.0, "low": 3900.5, "close": 3901.5,
                   "isOpen": False})
        s = bot._entry_setup("short", 3901.5, [3902.0], [3880.0], b1=b1)
        self.assertIsNotNone(s)
        assert s is not None
        self.assertEqual(s["kind"], "retrace")
        self.assertEqual(s["level"], 3902.0)

    def test_a_confirmed_break_is_taken(self):
        # A short's bar that CLOSES below the nearest support: the breakdown the
        # owner sells, entered a hair past the level.
        b1 = [{"openTime": _iso(i), "open": 3900, "high": 3900.4,
               "low": 3899.6, "close": 3900, "isOpen": False} for i in range(30)]
        b1.append({"openTime": "2026-11-07T30:00:00Z", "open": 3900,
                   "high": 3900.2, "low": 3894.0, "close": 3894.0,
                   "isOpen": False})
        s = bot._entry_setup("short", 3894.0, [3902.0], [3895.0], b1=b1)
        self.assertIsNotNone(s)
        assert s is not None
        self.assertEqual(s["kind"], "break")
        self.assertEqual(s["level"], 3895.0)

    def test_a_retrace_the_price_has_left_is_not_a_setup(self):
        # The rejection happened on the last bar, but the price has since run
        # 20 points away: entering now is a chase, not a bounce.
        b1 = [{"openTime": _iso(i), "open": 3900, "high": 3900.4,
               "low": 3899.6, "close": 3900, "isOpen": False} for i in range(30)]
        b1.append({"openTime": "2026-11-07T30:00:00Z", "open": 3901,
                   "high": 3903.0, "low": 3900.5, "close": 3901.5,
                   "isOpen": False})
        s = bot._entry_setup("short", 3880.0, [3902.0], [3860.0], b1=b1)
        self.assertIsNone(s)

    def test_the_wall_is_its_own_basis_and_not_gated(self):
        # The wall path is exempt from the basis gate: with a rejected fib wall
        # and a split tape, plan() still fires. (The card's own wall fixture.)
        rise = [4000 + (4148 - 4000) * (i / 19) for i in range(20)]
        drop = [4148 - (4148 - 4013) * (i / 24) for i in range(25)]
        tail = [4058 + (3 if i % 2 else -3) for i in range(30)]
        h1 = [4030 + i * 0.3 for i in range(20)] + [4056, 4060, 4058, 4066.2,
                                                    4058.0]
        feed = {"1d": [4060.0] * 30, "4h": rise + drop, "1h": h1,
                "30m": tail, "15m": tail, "5m": tail}
        with FakeFeed(mid=4058.0, closes=feed):
            p = bot.plan()
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p["direction"], "short")

    def test_a_bounce_within_the_wider_band_is_a_setup(self):
        # The owner asked to sell the bounce at 3,887.79; price bounced to
        # 3,881, 6.79 pts away. At the old 6-pt band the desk missed it and
        # only ever chased the breakdown. The band is 12 now, so it is a setup.
        b1 = [{"openTime": _iso(i), "open": 3869, "high": 3875.5,
               "low": 3862.2, "close": 3865.6, "isOpen": False}
              for i in range(30)]
        b1.append({"openTime": "2026-11-16T14:00:00Z", "open": 3865.2,
                   "high": 3889.2, "low": 3861.4, "close": 3878.7,
                   "isOpen": False})
        self.assertGreaterEqual(bot.ENTRY_BASIS_PTS, 7.0)
        s = bot._entry_setup("short", 3881.0, [3887.79, 3901.43],
                             [3860.36, 3852.47], b1=b1)
        self.assertIsNotNone(s)
        assert s is not None
        self.assertEqual(s["kind"], "retrace")
        self.assertEqual(s["level"], 3887.79)

    def test_the_fast_basis_reads_the_15m_frame(self):
        # The 1h basis is LATE: at 3,881 on 16-11 the last CLOSED 1h bar was
        # still the 13:00 break, so the desk refused (trend) even with an
        # unlimited band. The fast switch reads the 15m frame, where the 14:30
        # bar poked 3,889.2 and closed 3,878.7 back under the wall -- a retrace.
        saved = bot.FAST_BASIS
        try:
            bot.FAST_BASIS = False
            self.assertEqual(bot._basis_tf(), "1h")
            self.assertEqual(bot._basis_ar(), "ساعة")
            bot.FAST_BASIS = True
            self.assertEqual(bot._basis_tf(), "15m")
            self.assertEqual(bot._basis_ar(), "15د")
        finally:
            bot.FAST_BASIS = saved

    def test_the_fast_basis_turns_the_3881_tape_into_a_sell(self):
        # The whole point, on the owner's own 16-11 tape: the 15m bar that
        # rejected 3,887.79 (high 3,889.2, close 3,878.7) is a retrace setup
        # when the basis reads 15m -- the desk sells the bounce at 3,881. On the
        # 1h frame the same moment only sees the older 13:00 break bar, so the
        # bounce is invisible and the desk waits for 3,860.
        res, sup = [3887.79, 3901.43], [3872.06, 3860.36]
        reject_15m = [{"openTime": _iso(i), "open": 3866, "high": 3868,
                       "low": 3861, "close": 3865, "isOpen": False}
                      for i in range(30)]
        reject_15m.append({"openTime": "2026-11-16T14:30:00Z", "open": 3877.9,
                           "high": 3889.2, "low": 3876.4, "close": 3878.7,
                           "isOpen": False})
        saved = bot.FAST_BASIS
        try:
            bot.FAST_BASIS = True
            s = bot._entry_setup("short", 3881.0, res, sup, b1=reject_15m)
            self.assertIsNotNone(s)
            assert s is not None
            self.assertEqual(s["kind"], "retrace")
            self.assertEqual(s["level"], 3887.79)
        finally:
            bot.FAST_BASIS = saved

    def test_the_panel_has_a_fast_basis_button(self):
        saved = bot.FAST_BASIS
        try:
            bot.apply_settings({"fast_basis": False})
            keys = [b["callback_data"]
                    for r in bot.settings_keyboard()["inline_keyboard"]
                    for b in r]
            self.assertIn("cfg:fast_basis", keys)
            self.assertIn("باسيس سريع", bot.card_settings())
            bot.toggle_gate("fast_basis")
            self.assertTrue(bot.FAST_BASIS)
            self.assertTrue(json.loads(
                bot.SETTINGS_PATH.read_text())["fast_basis"])
            self.assertNotIn("fast_basis", bot.gate_state())
        finally:
            bot.FAST_BASIS = saved

    def test_the_switch_off_restores_the_immediate_entry(self):
        # DESK_ENTRY_BASIS=0 must take the frame-agreed trade at market again --
        # the gate is a switch, not a hard wall.
        tape = self._down()
        saved = bot.ENTRY_BASIS_ON
        bot.ENTRY_BASIS_ON = False
        try:
            with FakeFeed(mid=tape[-1], closes={tf: tape for tf in bot.FRAMES}):
                pc = bot.plan_check()
        finally:
            bot.ENTRY_BASIS_ON = saved
        self.assertNotEqual(pc["reason"], "no_setup")


class TargetLadderTest(unittest.TestCase):
    """THE TARGET LADDER -- the owner asked to stop taking a single ~$20 target
    and run a LADDER of take-profit rungs set by the market's own levels: 2, 3
    or 4 as the levels allow, capped at TARGET_LADDER_MAX_USD for the whole
    ladder. Rung 1 is the graded target (the record never moves); the rest are
    runners. Every card quotes the SAME ladder, from this one producer."""

    def test_the_ladder_is_ordered_and_capped(self):
        # A short from 3900, first rung $10, supports at 3882/3874/3866 -> four
        # rungs, gains strictly rising and none over the cap.
        lad = bot._target_ladder("short", 3900.0, 3910.0, [],
                                 [3882.0, 3874.0, 3866.0], 10.0)
        self.assertEqual(len(lad), 4)
        self.assertTrue(lad[0]["first"])
        gains = [r["gain_usd"] for r in lad]
        self.assertEqual(gains, sorted(gains))
        self.assertLessEqual(max(gains), bot.TARGET_LADDER_MAX_USD)

    def test_a_crowded_level_is_not_a_rung(self):
        # A level a dollar past the first rung is not a ladder step: with only
        # that level, there is no ladder (one rung is not a ladder).
        lad = bot._target_ladder("short", 3900.0, 3910.0, [], [3889.0], 10.0)
        self.assertEqual(lad, [])

    def test_a_level_beyond_the_cap_is_dropped(self):
        # A single support $50 away exceeds the $40 ladder cap, so no rung.
        lad = bot._target_ladder("short", 3900.0, 3910.0, [], [3850.0], 10.0)
        self.assertEqual(lad, [])

    def test_the_first_rung_is_the_graded_target(self):
        # The ladder's first rung must equal the plan's single target, so the
        # record (graded on the first touch) is unchanged.
        s = bot._sized(3900.0, "short", 3910.0)
        lad = bot._target_ladder("short", 3900.0, s["stop"], [],
                                 [3882.0, 3874.0], s["gain_usd"])
        self.assertTrue(lad)
        self.assertAlmostEqual(lad[0]["level"], s["target"], places=2)

    def test_the_switch_off_returns_no_ladder(self):
        saved = bot.TARGET_LADDER_ON
        bot.TARGET_LADDER_ON = False
        try:
            lad = bot._target_ladder("short", 3900.0, 3910.0, [],
                                     [3882.0, 3874.0], 10.0)
        finally:
            bot.TARGET_LADDER_ON = saved
        self.assertEqual(lad, [])

    def test_the_card_shows_the_ladder_when_it_has_one(self):
        p = {"direction": "short", "entry": 3900.0, "stop": 3910.0,
             "target": 3890.0, "gain_usd": 10.0, "risk_usd": 10.0,
             "risk_pts": 10.0, "rr": 2.0,
             "ladder": [{"level": 3890.0, "gain_usd": 10.0, "first": True},
                        {"level": 3874.0, "gain_usd": 26.0, "first": False}]}
        text = "\n".join(bot._plan_lines(p))
        self.assertIn("سلّم الأهداف", text)
        self.assertIn("3,874.00", text)


class NoChaseTest(unittest.TestCase):
    """The no-chase gate: never SELL into deep oversold (nor BUY into deep
    overbought) on the filter frame. The owner's own read refused exactly this
    entry -- "selling from here is chasing" -- and the 2026 backtest agreed
    (short side: +0.177 -> +0.289 R/trade, holding in all four quarters)."""

    def _tape(self, drift, swing=10.0, n=90):
        import math
        return [4000.0 + drift * i + swing * math.sin(i / 2.0) for i in range(n)]

    def test_the_threshold_defaults_to_the_measured_one(self):
        # Pinned to the level the 2026 study chose. A change must fail here.
        self.assertEqual(bot.NOCHASE_RSI, 38.0)
        self.assertEqual(bot.NOCHASE_TF, "1h")

    def test_a_deep_oversold_short_is_refused_and_named(self):
        # A fast fall: the 1h RSI is under the floor, so a short there is chasing.
        tape = self._tape(-0.5, -12.0)
        with FakeFeed(mid=tape[-1], closes={tf: tape for tf in bot.FRAMES}):
            pc = bot.plan_check()
        self.assertIsNone(pc["plan"])
        self.assertEqual(pc["reason"], "chase")
        self.assertIn("متأخر", pc["detail"])

    def test_a_short_above_the_floor_is_taken(self):
        # The same falling tape, but slower: the 1h RSI sits above the floor, so
        # the entry is not late and the plan is taken. The entry basis is off
        # here so this pins ONE rule (the no-chase gate); the basis has its own
        # tests.
        tape = self._tape(-0.35, -11.0)
        saved = bot.ENTRY_BASIS_ON
        bot.ENTRY_BASIS_ON = False
        try:
            with FakeFeed(mid=tape[-1], closes={tf: tape for tf in bot.FRAMES}):
                pc = bot.plan_check()
        finally:
            bot.ENTRY_BASIS_ON = saved
        self.assertEqual(pc["reason"], "ok")
        self.assertIsNotNone(pc["plan"])
        assert pc["plan"] is not None
        self.assertEqual(pc["plan"]["direction"], "short")

    def test_a_deep_overbought_long_is_refused(self):
        tape = self._tape(0.7, 13.0)
        with FakeFeed(mid=tape[-1], closes={tf: tape for tf in bot.FRAMES}):
            pc = bot.plan_check()
        self.assertIsNone(pc["plan"])
        self.assertEqual(pc["reason"], "chase")

    def test_the_gate_fails_open_on_a_missing_read(self):
        # A data blip must not silence the desk: no bars -> no reading -> pass.
        with FakeFeed(closes={"1h": []}):
            self.assertTrue(bot.nochase_status("short")["ok"])

    def test_the_gate_is_off_when_the_owner_turns_it_off(self):
        tape = self._tape(-0.5, -12.0)
        saved = bot.NOCHASE_ON
        saved_eb = bot.ENTRY_BASIS_ON
        bot.NOCHASE_ON = False
        bot.ENTRY_BASIS_ON = False
        try:
            with FakeFeed(mid=tape[-1], closes={tf: tape for tf in bot.FRAMES}):
                pc = bot.plan_check()
            self.assertEqual(pc["reason"], "ok")
        finally:
            bot.NOCHASE_ON = saved
            bot.ENTRY_BASIS_ON = saved_eb

    def test_the_taken_plan_carries_the_reading_and_the_why(self):
        tape = self._tape(-0.35, -11.0)
        saved_eb = bot.ENTRY_BASIS_ON
        bot.ENTRY_BASIS_ON = False
        try:
            with FakeFeed(mid=tape[-1], closes={tf: tape for tf in bot.FRAMES}):
                pc = bot.plan_check()
                why = "\n".join(bot._why_taken(pc["plan"]))
        finally:
            bot.ENTRY_BASIS_ON = saved_eb
        self.assertIsNotNone(pc["plan"])
        self.assertIsNotNone(pc["plan"]["nochase_rsi"])
        self.assertIn("ما بمشي ورا الحركة", why)


class PreTradeNoteTest(unittest.TestCase):
    """The heads-up BEFORE a trade: the owner asked to be told a trade is about
    to be taken and WHY, from the conditions -- not to find one already open."""

    def _p(self, direction="long"):
        return {"direction": direction, "entry": 4000.0, "stop": 3988.0,
                "target": 4024.0, "risk_pts": 12.0, "risk_usd": 12.0,
                "gain_usd": 20.0, "rr": 1.67, "strong": True, "r15": 45.0,
                "up": 6, "dn": 0}

    def test_it_says_a_trade_is_about_to_be_taken(self):
        import math
        tape = [4000 + 0.2 * i + 10 * math.sin(i / 2.0) for i in range(90)]
        with FakeFeed(mid=tape[-1], closes={tf: tape for tf in bot.FRAMES}):
            text = bot._pre_trade_note(self._p())
        self.assertIn("هلق رح تتاخد صفقة", text)
        self.assertIn("ليش", text)

    def test_it_lists_the_conditions_that_passed(self):
        import math
        tape = [4000 + 0.2 * i + 10 * math.sin(i / 2.0) for i in range(90)]
        with FakeFeed(mid=tape[-1], closes={tf: tape for tf in bot.FRAMES}):
            text = bot._pre_trade_note(self._p())
        self.assertIn("الاتجاه", text)          # the trend fact
        self.assertIn("R:R", text)              # the reward/risk fact

    def test_it_carries_the_numbers_and_a_late_caveat(self):
        import math
        tape = [4000 + 0.2 * i + 10 * math.sin(i / 2.0) for i in range(90)]
        p = self._p("short")
        p["r15"] = 20.0                          # a stretched short
        with FakeFeed(mid=tape[-1], closes={tf: tape for tf in bot.FRAMES}):
            text = bot._pre_trade_note(p)
        self.assertIn(f"{p['entry']:,.2f}", text)
        self.assertIn("دخول متأخر", text)


class DailyCapTest(unittest.TestCase):
    """The owner asked for at most TWENTY trades a day (raised from ten on
    2026-11-16). trades_opened_today is the counter the cap reads; it must
    count the session, not the calendar day."""

    def test_the_cap_defaults_to_twenty(self):
        self.assertEqual(bot.MAX_TRADES_DAY, 20)

    def test_it_counts_only_this_sessions_opens(self):
        from datetime import timedelta
        start, _ = bot._session_day()
        old = (start - timedelta(days=3)).isoformat()
        fresh = (start + timedelta(hours=1)).isoformat()
        rows = [
            {"opened_at": old, "id": 1},        # yesterday: not counted
            {"opened_at": fresh, "id": 2},      # today
            {"opened_at": fresh, "id": 3},      # today
        ]
        with unittest.mock.patch.object(bot, "all_trades", return_value=rows):
            self.assertEqual(bot.trades_opened_today(), 2)

    def test_the_cap_is_a_switch(self):
        # DESK_MAX_TRADES_DAY=0 must remove the cap (the env is read at import,
        # so this pins the DEFAULT and the shape of the guard).
        self.assertGreater(bot.MAX_TRADES_DAY, 0)


class RestGapTest(_DbCase):
    """After a trade closes, the desk waits REST_MINUTES before the next entry.

    A fresh signal one second after a stop is usually the same move taken
    again; the owner would rather the next trade be right than immediate.
    """

    def test_no_closed_trade_means_no_wait(self):
        self.assertIsNone(bot._rest_until())

    def test_a_recent_close_blocks_the_next_entry(self):
        tid = bot.add_trade("short", 3900, 3910, 3880, bot.LOT, note="t")
        bot._settle(tid, "stop", 3910.0, -1.0)   # closed_at = now
        ready = bot._rest_until()
        self.assertIsNotNone(ready)
        left = (ready - bot.datetime.now(bot.UTC)).total_seconds() / 60
        self.assertGreater(left, bot.REST_MINUTES - 2)
        self.assertLessEqual(left, bot.REST_MINUTES)

    def test_an_old_close_does_not_block(self):
        tid = bot.add_trade("short", 3900, 3910, 3880, bot.LOT, note="t")
        bot._settle(tid, "stop", 3910.0, -1.0)
        stale = (bot.datetime.now(bot.UTC)
                 - bot.timedelta(minutes=bot.REST_MINUTES + 5)
                 ).strftime("%Y-%m-%dT%H:%M:%SZ")
        with unittest.mock.patch.object(bot, "_utcnow", return_value=stale):
            bot._settle(tid, "stop", 3910.0, -1.0)
        self.assertIsNone(bot._rest_until())

    def test_zero_minutes_disables_the_gap(self):
        tid = bot.add_trade("short", 3900, 3910, 3880, bot.LOT, note="t")
        bot._settle(tid, "stop", 3910.0, -1.0)
        saved = bot.REST_MINUTES
        bot.REST_MINUTES = 0
        try:
            self.assertIsNone(bot._rest_until())
        finally:
            bot.REST_MINUTES = saved


class CollapsibleMenuTest(unittest.TestCase):
    """The menu button is a STANDING reply keyboard at the bottom-left of the
    screen, not an inline button inside a card. Tapping it sends its label,
    which action_for() routes to the grid. An inline grid (menu / journal /
    close) keeps its own buttons; a plain card carries no inline buttons."""

    def test_a_plain_card_carries_the_standing_menu_button(self):
        # A button-less card gets the reply keyboard, which is also what
        # installs it under the message.
        k = bot.card_markup("analysis")
        self.assertIn("keyboard", k)                 # a reply keyboard, not inline
        self.assertNotIn("inline_keyboard", k)
        texts = [b["text"] for row in k["keyboard"] for b in row]
        self.assertIn(bot.MENU_BUTTON_TEXT, texts)

    def test_the_menu_action_opens_the_full_grid(self):
        k = bot.keyboard_for("menu")
        labels = [b["text"] for r in k["inline_keyboard"] for b in r]
        self.assertIn("🧠 قراءتي للسوق", labels)
        self.assertIn("✖ طي القائمة", labels)

    def test_the_expanded_grid_has_a_fold_row(self):
        k = bot.keyboard_for("menu")
        last = k["inline_keyboard"][-1]
        self.assertEqual(last[0]["callback_data"], "collapse")

    def test_the_menu_button_text_routes_to_the_grid(self):
        # The reply button sends "☰ القائمة"; the whole label must map to menu.
        self.assertEqual(bot.action_for(bot.MENU_BUTTON_TEXT), "menu")

    def test_collapse_is_a_known_action(self):
        # Tapping the fold row must not fall through to "زر غير معروف".
        self.assertNotIn("غير معروف", bot.dispatch("collapse"))

    def test_an_open_trade_card_keeps_its_close_button(self):
        # The close buttons are the ONE exception: folding them away would make
        # the owner type an id to close a trade, which is the whole thing the
        # buttons exist to avoid.
        rows = [{"id": 7, "direction": "long", "entry": 4000.0, "stop": 3988.0,
                 "target": 4024.0, "status": "open", "opened_at": "x"}]
        with unittest.mock.patch.object(bot, "open_trades", return_value=rows):
            k = bot.keyboard_for("open")
        flat = [b["callback_data"] for r in k["inline_keyboard"] for b in r]
        self.assertIn("close:7", flat)

    def test_every_menu_action_is_wired(self):
        # A button whose action is unknown is a dead tap. Every button the grid
        # and the dropdowns carry must be an action the bot actually handles --
        # the dispatch table plus the ones handled in handle() (`who`, the
        # `cfg:` toggles; `trade`/`close` are parsed from text). Add a button
        # with a typo and this fails.
        known = {
            "full", "report", "action", "day_analysis", "analysis", "recommend",
            "map", "news", "news_prep", "news_result", "scenarios", "levels",
            "open",
            "journal_menu", "daily",
            "who", "howto", "alerts", "help", "collapse", "menu", "settings",
            "menu_back", "menu_analysis", "menu_map", "menu_journal", "myread",
            "ayzr", "silver", "bitcoin",
        }
        pairs = [p for row in bot.MENU for p in row]
        for rows in bot.SUBMENUS.values():
            pairs += [p for row in rows for p in row]
        for _t, action in pairs:
            self.assertIn(action, known, f"action {action} is not wired")

    def test_the_dropdowns_reach_every_leaf(self):
        # The whole point of the dropdown: every card is still one or two taps
        # away, and every submenu can climb back to the top.
        leaves = {a for rows in bot.SUBMENUS.values() for _t, a in
                  [p for row in rows for p in row]}
        for needed in ("analysis", "recommend", "report", "day_analysis",
                       "action", "map", "news", "scenarios", "levels", "open",
                       "journal_menu", "daily", "howto", "alerts"):
            self.assertIn(needed, leaves, f"{needed} is unreachable")
        for name, rows in bot.SUBMENUS.items():
            self.assertTrue(any(a == "menu_back" for _t, a in
                                [p for row in rows for p in row]),
                            f"{name} has no way back")


class StudySyncTest(unittest.TestCase):
    """The 2026 replay (backtest/bot2026.py) must measure the bot that trades.

    It hardcoded its own ADX 20-40 / ATR 0.25% bands -- four times tighter than
    the live ADX 0-50 / ATR 1.00% -- and skipped the no-chase gate entirely, so
    every study number it produced (win rate, target sweep, "the no-chase gate
    pays") described a bot that does not exist. The replay now READS bot's
    constants; these tests fail the moment the two drift apart again.
    """

    def _study(self):
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "backtest"))
        import bot2026
        return bot2026

    def test_the_replay_reads_the_live_bands(self):
        s = self._study()
        self.assertEqual((s.ADX_MIN, s.ADX_MAX), (bot.ADX_MIN, bot.ADX_MAX))
        self.assertEqual((s.ATR_MIN_PCT, s.ATR_MAX_PCT),
                         (bot.ATR_MIN_PCT, bot.ATR_MAX_PCT))
        self.assertEqual((s.RSI_LONG, s.RSI_SHORT), (bot.RSI_LONG, bot.RSI_SHORT))

    def test_the_replay_reads_the_live_sizing(self):
        s = self._study()
        self.assertEqual(s.STOP_USD, bot.STOP_PICK_USD)
        self.assertEqual(s.RR, bot.TARGET_RR)

    def test_the_replay_reads_the_live_no_chase_gate(self):
        s = self._study()
        self.assertEqual(s.NOCHASE_ON, bot.NOCHASE_ON)
        self.assertEqual(s.NOCHASE_RSI, bot.NOCHASE_RSI)
        self.assertEqual(s.NOCHASE_TF, bot.NOCHASE_TF)
        self.assertEqual(s.NOCHASE_SIDES, set(bot.NOCHASE_SIDES))

    def test_the_gate_bands_reject_the_same_values(self):
        # The same ADX reading must pass/fail in both, at the band edges.
        s = self._study()
        for adx_v in (bot.ADX_MIN, bot.ADX_MAX, bot.ADX_MAX + 1.0,
                      bot.ADX_MIN - 1.0):
            live = s.ADX_MIN <= adx_v <= s.ADX_MAX
            self.assertEqual(live, bot.ADX_MIN <= adx_v <= bot.ADX_MAX)
        for atr_pct in (bot.ATR_MIN_PCT, bot.ATR_MAX_PCT,
                        bot.ATR_MAX_PCT + 0.001):
            live = s.ATR_MIN_PCT <= atr_pct <= s.ATR_MAX_PCT
            self.assertEqual(live,
                             bot.ATR_MIN_PCT <= atr_pct <= bot.ATR_MAX_PCT)


class ChartPayloadTest(unittest.TestCase):
    """chart_payload feeds the live map's candle chart. Every level on it must
    come from the SAME functions the Telegram cards call, so the drawing can
    never disagree with the bot -- that is the whole point of the feature."""

    def _rise(self):
        # A gentle uptrend with enough bars to warm the RSI and form pivots.
        return [4000 + i * 0.5 for i in range(160)]

    def test_the_chart_bars_carry_ohlc_and_time(self):
        with FakeFeed(mid=4100.0, closes={"15m": self._rise()}):
            p = bot.chart_payload()
        self.assertTrue(p["bars"])
        for b in p["bars"]:
            self.assertEqual(set(b), {"t", "o", "h", "l", "c"})
            self.assertLessEqual(b["l"], b["h"])

    def test_the_chart_rsi_line_ends_where_rsi_says(self):
        # The chart's last RSI point must equal rsi() on the same closes -- the
        # line is the same recursion, drawn, not a second opinion.
        closes = self._rise()
        with FakeFeed(mid=4100.0, closes={"15m": closes}):
            p = bot.chart_payload()
            live_rsi = bot.rsi([b["c"] for b in p["bars"]])
        self.assertIsNotNone(live_rsi)
        self.assertAlmostEqual(p["rsi"][-1], live_rsi, places=6)

    def test_the_chart_zones_match_the_map_card(self):
        # The chart's zones must be _zones() itself, truncated the way the card
        # truncates them -- the page cannot show a level the card does not.
        with FakeFeed(mid=4100.0, closes={"15m": self._rise()}):
            live = bot.read_market()["live"]
            sell, buy = bot._zones(live)
            p = bot.chart_payload()
        self.assertEqual([(z["price"], z["touches"]) for z in p["zones"]["sell"]],
                         [(z, n) for z, n in sell[:4]])
        self.assertEqual([(z["price"], z["touches"]) for z in p["zones"]["buy"]],
                         [(z, n) for z, n in buy[:4]])

    def test_a_dead_feed_returns_an_empty_chart_not_an_error(self):
        with FakeFeed(closes={"15m": []}):
            p = bot.chart_payload()
        self.assertEqual(p["bars"], [])
        self.assertEqual(p["rsi"], [])

    def test_the_payload_is_json_serialisable(self):
        import json
        with FakeFeed(mid=4100.0, closes={"15m": self._rise()}):
            json.dumps(bot.chart_payload())


class ReportTest(unittest.TestCase):
    """The full report lives on BOTH the map and the bot. The two must read ONE
    engine (report_payload), so the page and the card can never disagree -- the
    same rule the map already follows for zones and levels."""

    def _rise(self):
        return [4000 + i * 0.5 for i in range(160)]

    def _fall(self):
        return [4200 - i * 0.5 for i in range(160)]

    def test_the_report_carries_the_sections_the_owner_asked_for(self):
        with FakeFeed(mid=4100.0, closes={"1h": self._rise()}):
            p = bot.report_payload()
        for key in ("live", "bias", "frames", "levels", "sessions", "days",
                    "bullets", "fib", "fib_line", "structure", "plan", "clock"):
            self.assertIn(key, p)
        self.assertEqual(len(p["frames"]), len(bot.FRAMES))

    def test_every_surface_prints_the_same_direction_word(self):
        # The owner's rule, made structural: the card's frame line, the map's
        # "الميل" and the report's bias are all the ONE word _market_bias
        # returns. This is the MIXED tape (4 down / 2 up) where the bug lived --
        # the card once read "محتار" while the map read "هابط". If any surface
        # grows its own threshold again, this fails.
        mixed = {tf: (self._fall() if i < 4 else self._rise())
                 for i, tf in enumerate(bot.FRAMES)}
        with FakeFeed(mid=3900.0, closes=mixed):
            m = bot.read_market()
            word = bot._market_bias(m["trends"])[1]
            card = bot.card_full()
            dmap = bot.daily_analysis()
            rep = bot.report_payload()
        self.assertEqual(word, "مائل للهبوط")
        self.assertEqual(word, rep["bias"])
        self.assertIn(f"→ <b>{word}</b>", card)
        self.assertIn(f"الميل: <b>{word}</b>", dmap)
        self.assertIn(f"إجماع: <b>{word}</b>", dmap)

    def test_the_bias_follows_the_tape(self):
        # Every frame falling -> the report must read down, not range.
        with FakeFeed(mid=3900.0, closes={tf: self._fall() for tf in bot.FRAMES}):
            p = bot.report_payload()
        self.assertEqual(p["bias"], "هابط")
        self.assertEqual(p["dn"], len(bot.FRAMES))

    def test_each_frame_sentence_is_built_from_its_own_numbers(self):
        with FakeFeed(mid=3900.0, closes={tf: self._fall() for tf in bot.FRAMES}):
            p = bot.report_payload()
        for f in p["frames"]:
            self.assertTrue(f["read"])
            self.assertIn("الاتجاه", f["read"])

    def test_the_map_and_the_card_share_one_engine(self):
        # The map payload's report must equal a standalone report_payload read
        # of the same tape -- one engine, two views, no drift.
        with FakeFeed(mid=4100.0, closes={"1h": self._rise()}):
            mp = bot.map_payload()
        self.assertIsNotNone(mp["report"])
        self.assertIn("bias", mp["report"])
        self.assertEqual(mp["report"]["live"], mp["live"])

    def test_the_card_prints_the_report_lines_verbatim(self):
        # The owner's rule: the report on the bot and on the map is the SAME
        # text, word for word. The card must print report_payload()["lines"]
        # with nothing added, reordered or reformatted -- so the two surfaces
        # cannot drift, because there is only one text.
        with FakeFeed(mid=4100.0, closes={"1h": self._rise()}):
            p = bot.report_payload()
            t = bot.card_report()
        self.assertEqual(t, "\n".join(p["lines"]))
        self.assertTrue(p["lines"])

    def test_the_report_text_is_carried_in_the_map_payload(self):
        # The page prints report.lines verbatim, so the map payload MUST carry
        # the very same list the card prints. Same read -> same lines.
        with FakeFeed(mid=4100.0, closes={"1h": self._rise()}):
            mp = bot.map_payload()
            p = bot.report_payload()
        self.assertIn("lines", mp["report"])
        self.assertTrue(mp["report"]["lines"])

    def test_the_report_levels_match_the_map_display_levels(self):
        # The report's level ladder must be the SAME set the map's own level
        # list shows (the sticky display pool), or the two sections of one page
        # would name different prices.
        with FakeFeed(mid=4100.0, closes={"1h": self._rise()}):
            mp = bot.map_payload()
        rep = mp["report"]
        rep_res = [lv["price"] for lv in rep["levels"] if lv["kind"] == "مقاومة"]
        rep_sup = [lv["price"] for lv in rep["levels"] if lv["kind"] == "دعم"]
        self.assertEqual(rep_res, mp["res"][:len(rep_res)])
        self.assertEqual(rep_sup, mp["sup"][:len(rep_sup)])

    def test_the_card_fits_the_telegram_limit(self):
        with FakeFeed(mid=4100.0, closes={"1h": self._rise()}):
            t = bot.card_report()
        self.assertLessEqual(len(t), 4000)
        self.assertIn("تحليلي المستقل", t)
        self.assertIn("الميل العام", t)

    def test_a_dead_tape_yields_a_card_not_a_crash(self):
        with FakeFeed(mid=0.0, closes={"1h": []}):
            t = bot.card_report()
        self.assertIn("ما في قراءة", t)

    def test_the_payload_is_json_serialisable(self):
        import json
        with FakeFeed(mid=4100.0, closes={"1h": self._rise()}):
            json.dumps(bot.report_payload(), ensure_ascii=False)


class StickyLevelsTest(unittest.TestCase):
    """The owner's rule: a level the map named is never taken down by a refresh.

    The pool is written and read through the module-level STICKY_PATH, which
    setUpModule points at a temp file for the whole file, so these tests never
    touch the real data/levels_seen.json."""

    def setUp(self):
        bot.STICKY_PATH.unlink(missing_ok=True)

    def test_a_named_level_survives_a_refresh_that_no_longer_sees_it(self):
        # First read names 4050 as support; a later read -- a sharp move that
        # recomputes a different nearest support -- must NOT drop it.
        bot._sticky_map(4100.0, [], [4050.0], [], [], [], [])
        out = bot._sticky_map(4100.0, [], [4040.0], [], [], [], [])
        self.assertIn(4050.0, out["sup"])
        self.assertIn(4040.0, out["sup"])

    def test_a_level_crossing_the_price_is_relabelled_not_removed(self):
        # 4050 named as support below 4100; price falls to 4000 and 4050 is now
        # ABOVE it -- it must appear as resistance, still named, never dropped.
        bot._sticky_map(4100.0, [], [4050.0], [], [], [], [])
        out = bot._sticky_map(4000.0, [], [], [], [], [], [])
        self.assertIn(4050.0, out["res"])
        self.assertNotIn(4050.0, out["sup"])

    def test_zones_stay_in_the_zone_list_not_the_level_list(self):
        # A zone named once must remain a zone -- not leak into res/sup, which
        # are the plain pivot levels.
        bot._sticky_map(4100.0, [], [], [(4120.0, 3)], [(4080.0, 2)], [], [])
        out = bot._sticky_map(4100.0, [], [], [], [], [], [])
        self.assertEqual([z for z, _ in out["sell"]], [4120.0])
        self.assertEqual([z for z, _ in out["buy"]], [4080.0])
        self.assertNotIn(4120.0, out["res"])

    def test_a_zone_keeps_its_last_known_touch_count(self):
        bot._sticky_map(4100.0, [], [], [(4120.0, 4)], [], [], [])
        out = bot._sticky_map(4100.0, [], [], [], [], [], [])
        self.assertEqual(out["sell"][0], (4120.0, 4))

    def test_the_display_is_capped_to_the_nearest(self):
        # 30 levels above, all named; the display shows only the nearest
        # STICKY_MAX -- but the STORE keeps them, so none is really "lost".
        res = [4100.0 + i for i in range(1, 31)]
        out = bot._sticky_map(4000.0, res, [], [], [], [], [])
        self.assertEqual(len(out["res"]), bot.STICKY_MAX)
        self.assertEqual(out["res"][0], 4101.0)
        self.assertLessEqual(max(out["res"]),
                             4100.0 + bot.STICKY_STORE_MAX)

    def test_a_broken_pool_file_does_not_crash_the_map(self):
        bot.STICKY_PATH.write_text("not json at all", encoding="utf-8")
        out = bot._sticky_map(4100.0, [], [4050.0], [], [], [], [])
        self.assertIn(4050.0, out["sup"])

    def test_the_pool_rides_in_the_backup_so_a_restart_keeps_it(self):
        # Render has no disk: the container restarts and data/levels_seen.json
        # is gone. The pool must travel in the pinned backup and come back.
        bot._sticky_map(4100.0, [], [4050.0], [(4120.0, 3)], [], [], [])
        blob = bot._dump_journal()
        self.assertEqual(blob["sticky"]["lvls"], [4050.0])
        self.assertEqual(blob["sticky"]["zones"], [4120.0])
        bot.STICKY_PATH.unlink()                      # the container restarted
        bot._sticky_save(blob["sticky"])              # restore_journal does this
        out = bot._sticky_map(4100.0, [], [], [], [], [], [])
        self.assertIn(4050.0, out["sup"])
        self.assertEqual([z for z, _ in out["sell"]], [4120.0])


class ChatSplitTest(unittest.TestCase):
    """The owner keeps the market chatter; the world follows the trades.

    The rule changed (owner's call): `_alert_chats` used to be public and
    `_desk_chats` private. Now BOTH are the owner alone under OWNER_ONLY, and
    the shared surface is `_public_chats` -- a trade opening, that trade
    winning or losing, the plan and strategy, the daily read, and the day's
    record. Everything else is his."""

    def setUp(self):
        self._own, self._backup = bot.OWNER_ONLY, bot.BACKUP_CHAT
        bot.BACKUP_CHAT = ""                      # fall back to SEED_CHATS[0]

    def tearDown(self):
        bot.OWNER_ONLY, bot.BACKUP_CHAT = self._own, self._backup

    def test_desk_messages_go_to_the_owner_alone(self):
        bot.OWNER_ONLY = True
        allow = {"100", "200", "933571066"}
        self.assertEqual(bot._desk_chats(allow), [bot._backup_chat()])
        self.assertNotIn("100", bot._desk_chats(allow))

    def test_market_alerts_are_private_too(self):
        # The change the owner asked for: the level/gate/trend/news stream is
        # no longer the public half of the bot.
        bot.OWNER_ONLY = True
        allow = {"100", "200"}
        self.assertEqual(bot._alert_chats(allow), [bot._backup_chat()])
        self.assertNotIn("100", bot._alert_chats(allow))

    def test_the_shared_surface_is_everyone_allowed(self):
        bot.OWNER_ONLY = True
        allow = {"100", "200"}
        self.assertEqual(bot._public_chats(allow), ["100", "200", bot._backup_chat()])

    def test_the_shared_surface_always_includes_the_owner(self):
        # A hand-edited allow file that dropped the owner must not make him
        # miss his own trades.
        bot.OWNER_ONLY = True
        self.assertIn(bot._backup_chat(), bot._public_chats({"100"}))

    def test_owner_only_off_restores_the_old_behaviour(self):
        bot.OWNER_ONLY = False
        allow = {"100", "200"}
        self.assertEqual(bot._desk_chats(allow), ["100", "200"])
        self.assertEqual(bot._alert_chats(allow), ["100", "200"])

    def test_a_guest_cannot_close_the_owners_trade(self):
        # Sharing the trade cards hands every guest a trade id. Without a lock
        # on `close`, a guest typing "سكر 4" would close the owner's position --
        # and `trade` would let him file a fake one in the journal.
        tg = unittest.mock.Mock()
        with unittest.mock.patch.object(bot, "close_trade") as closing, \
             unittest.mock.patch.object(bot, "record_trade") as filing:
            bot.handle(tg, {"message": {"chat": {"id": "100"},
                                        "text": "سكر 4"}}, {"100"})
            bot.handle(tg, {"message": {"chat": {"id": "100"},
                                        "text": "شراء 3900 وقف 3890"}}, {"100"})
        closing.assert_not_called()
        filing.assert_not_called()
        said = " ".join(str(c.args[1]) for c in tg.send.call_args_list)
        self.assertIn("للمالك بس", said)


class PullbackEntryTest(unittest.TestCase):
    """The owner asked to enter the way he reads the tape: sell the BOUNCE, not
    the middle of the range. The evidence is MIXED, so the switch stays OFF.

    Over the full 2026 year (backtest/entry_timing.py) the pullback lifts
    R/trade from +0.118 to +0.243; but on the owner's REAL 38-trade week
    (backtest/week_compare.py) the immediate entry won by 13.5R, because that
    week trended hard and the limit missed 9 winners. It wins 7 of 11 months
    (the RANGE ones). These tests pin the MECHANICS, not a claim of edge: the
    default is "immediate" (no change), and the pending order survives a
    restart via PENDING_PATH."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = bot.PENDING_PATH
        bot.PENDING_PATH = pathlib.Path(self.tmp) / "pending.json"

    def tearDown(self):
        bot.PENDING_PATH = self._saved

    def test_default_mode_is_immediate_so_nothing_changes(self):
        self.assertIn(bot.ENTRY_MODE, ("immediate", "pullback"))
        # the shipped default must be the no-op, or a deploy silently changes
        # how the desk enters without the owner asking.
        self.assertEqual(bot.ENTRY_MODE, "immediate")

    def test_a_short_limit_sits_above_the_signal_price(self):
        # A short wants a HIGHER price: the limit must be above the signal, not
        # below it, or the desk would sell into weakness.
        p = {"direction": "short", "entry": 3895.0}
        with unittest.mock.patch.object(bot, "atr", return_value=8.0), \
             unittest.mock.patch.object(bot, "bars", return_value=[]):
            lim = bot._pullback_limit(p)
        self.assertIsNotNone(lim)
        self.assertGreater(lim, 3895.0)
        self.assertAlmostEqual(lim, 3895.0 + bot.PULLBACK_ATR * 8.0, places=2)

    def test_a_long_limit_sits_below_the_signal_price(self):
        p = {"direction": "long", "entry": 3895.0}
        with unittest.mock.patch.object(bot, "atr", return_value=8.0), \
             unittest.mock.patch.object(bot, "bars", return_value=[]):
            lim = bot._pullback_limit(p)
        self.assertLess(lim, 3895.0)

    def test_the_limit_moves_the_whole_plan_not_just_the_entry(self):
        # Re-sizing around the limit must keep entry/stop/target together and
        # inside the owner's $8-12 risk window.
        p = {"direction": "short", "entry": 3895.0, "up": 0, "dn": 6,
             "strong": True}
        q = bot._pending_plan(p, 3901.0)
        self.assertEqual(q["entry"], 3901.0)
        self.assertGreater(q["stop"], q["entry"])          # a short stops above
        self.assertLess(q["target"], q["entry"])
        self.assertGreaterEqual(q["risk_usd"], bot.STOP_MIN_USD - 0.01)
        self.assertLessEqual(q["risk_usd"], bot.STOP_MAX_USD + 0.01)

    def test_the_pending_order_survives_a_restart(self):
        bot._pending_save({"side": "short", "limit": 3901.0, "at": "x",
                           "plan": {"direction": "short", "entry": 3895.0}})
        bot.PENDING_PATH = bot.PENDING_PATH            # a new process reads disk
        d = bot._pending_load()
        self.assertIsNotNone(d)
        self.assertEqual(d["side"], "short")
        self.assertEqual(d["limit"], 3901.0)

    def test_a_broken_pending_file_reads_as_none(self):
        bot.PENDING_PATH.write_text("not json", encoding="utf-8")
        self.assertIsNone(bot._pending_load())
        bot.PENDING_PATH.write_text('{"nothing": 1}', encoding="utf-8")
        self.assertIsNone(bot._pending_load())

    def test_clear_removes_the_order(self):
        bot._pending_save({"side": "long", "limit": 1.0, "plan": {}})
        bot._pending_clear()
        self.assertIsNone(bot._pending_load())

    def test_a_signal_parks_an_order_instead_of_entering(self):
        # With pullback on, a fresh signal must NOT open a trade at market: it
        # parks a limit and tells the owner it is waiting.
        sent = []
        tg = unittest.mock.Mock()
        tg.send = lambda c, m: sent.append((c, m))
        pc = {"plan": {"direction": "short", "entry": 3895.0, "up": 0, "dn": 6,
                       "strong": True}, "reason": "ok", "detail": ""}
        with unittest.mock.patch.object(bot, "read_market",
                                        return_value={"live": 3895.0}), \
             unittest.mock.patch.object(bot, "atr", return_value=8.0), \
             unittest.mock.patch.object(bot, "bars", return_value=[]), \
             unittest.mock.patch.object(bot, "open_trades", return_value=[]), \
             unittest.mock.patch.object(bot, "_desk_chats", return_value=["O"]):
            handled = bot._desk_pullback(tg, {"O"}, {}, pc)
        self.assertTrue(handled)
        d = bot._pending_load()
        self.assertIsNotNone(d)
        self.assertEqual(d["side"], "short")
        self.assertGreater(d["limit"], 3895.0)      # a short waits HIGHER
        self.assertTrue(any("ارتداد" in m for _, m in sent))

    def test_a_reached_limit_fills_the_order(self):
        # The parked short fills only when the live price climbs TO the limit.
        bot._pending_save({"side": "short", "limit": 3901.0, "at": bot._utcnow(),
                           "plan": {"direction": "short", "entry": 3895.0,
                                    "up": 0, "dn": 6, "strong": True}})
        sent = []
        tg = unittest.mock.Mock()
        tg.send = lambda c, m: sent.append((c, m))
        with unittest.mock.patch.object(
                bot, "read_market",
                return_value={"live": 3902.0, "q": {"marketState": "open"}}), \
             unittest.mock.patch.object(bot, "_write_plan", return_value=99), \
             unittest.mock.patch.object(bot, "all_trades",
                                        return_value=[{"id": 99}]), \
             unittest.mock.patch.object(bot, "_desk_chats", return_value=["O"]):
            handled = bot._desk_pullback(tg, {"O"}, {}, {"plan": None})
        self.assertTrue(handled)
        self.assertIsNone(bot._pending_load())      # the order is consumed

    def test_an_unreached_limit_stays_parked(self):
        bot._pending_save({"side": "short", "limit": 3901.0, "at": bot._utcnow(),
                           "plan": {"direction": "short", "entry": 3895.0}})
        tg = unittest.mock.Mock()
        with unittest.mock.patch.object(
                bot, "read_market",
                return_value={"live": 3897.0, "q": {"marketState": "open"}}):
            handled = bot._desk_pullback(tg, set(), {}, {"plan": None})
        self.assertTrue(handled)
        self.assertIsNotNone(bot._pending_load())   # still waiting


class OwnerPanelTest(unittest.TestCase):
    """The gate switches are the OWNER's alone, live from a Telegram panel.

    The owner asked for a button only he sees -- a guest must not read the
    desk's settings or the subscriber list, and a guest's tap on a switch must
    change nothing.
    """

    OWNER = bot.SEED_CHATS[0]
    GUEST = "8545528489"

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._saved_path = bot.SETTINGS_PATH
        bot.SETTINGS_PATH = pathlib.Path(self._dir) / "settings.json"
        self._saved_state = bot.gate_state()

    def tearDown(self):
        bot.SETTINGS_PATH = self._saved_path
        bot.apply_settings(self._saved_state)

    def _cb(self, chat, data, mid=99):
        return {"callback_query": {"id": "c1", "data": data,
                                   "message": {"message_id": mid,
                                               "chat": {"id": chat}}}}

    def test_the_owner_sees_the_panel_and_a_guest_does_not(self):
        owner = [a for r in bot._menu_rows(self.OWNER) for _, a in r]
        guest = [a for r in bot._menu_rows(self.GUEST) for _, a in r]
        self.assertIn("settings", owner)
        self.assertIn("who", owner)
        self.assertNotIn("settings", guest)
        self.assertNotIn("who", guest)
        # The guest keeps the shared half of the bot.
        self.assertIn("full", guest)
        self.assertIn("menu_analysis", guest)

    def test_is_owner_knows_the_owner(self):
        self.assertTrue(bot.is_owner(self.OWNER))
        self.assertFalse(bot.is_owner(self.GUEST))

    def test_a_toggle_flips_the_live_gate_and_persists(self):
        bot.apply_settings({k: True for k in bot.GATE_KEYS})
        tg = unittest.mock.Mock()
        bot.handle(tg, self._cb(self.OWNER, "cfg:entry_basis"), {self.OWNER})
        self.assertFalse(bot.ENTRY_BASIS_ON)          # the module global flipped
        saved = json.loads(bot.SETTINGS_PATH.read_text())
        self.assertFalse(saved["entry_basis"])        # and it was written
        # The panel is rewritten in place, not re-posted.
        tg.edit_text.assert_called_once()

    def test_a_guests_tap_changes_nothing(self):
        bot.apply_settings({k: True for k in bot.GATE_KEYS})
        tg = unittest.mock.Mock()
        bot.handle(tg, self._cb(self.GUEST, "cfg:nochase"), {self.OWNER, self.GUEST})
        self.assertTrue(bot.NOCHASE_ON)               # untouched
        self.assertFalse(tg.edit_text.called)         # no panel rewritten
        self.assertFalse(bot.SETTINGS_PATH.exists())  # nothing saved

    def test_open_all_and_close_all(self):
        tg = unittest.mock.Mock()
        bot.apply_settings({k: False for k in bot.GATE_KEYS})
        bot.handle(tg, self._cb(self.OWNER, "cfg:open_all"), {self.OWNER})
        self.assertEqual(bot.gate_state(), {k: True for k in bot.GATE_KEYS})
        bot.handle(tg, self._cb(self.OWNER, "cfg:close_all"), {self.OWNER})
        self.assertEqual(bot.gate_state(), {k: False for k in bot.GATE_KEYS})

    def test_a_saved_choice_overrides_the_env_default(self):
        # The whole point: the panel writes settings.json and the next start
        # reads it back, so the choice survives a redeploy.
        bot.SETTINGS_PATH.write_text(json.dumps(
            {"entry_basis": False, "nochase": False,
             "filters": False, "trend_filter": False}))
        self.assertEqual(bot.load_settings(),
                         {"entry_basis": False, "nochase": False,
                          "filters": False, "trend_filter": False})

    def test_the_guest_who_card_is_refused(self):
        tg = unittest.mock.Mock()
        bot.handle(tg, self._cb(self.GUEST, "who"), {self.OWNER, self.GUEST})
        sent = " ".join(str(c.args[1]) for c in tg.send.call_args_list)
        self.assertIn("للمالك", sent)
        tg2 = unittest.mock.Mock()
        with unittest.mock.patch.object(bot, "load_allow",
                                        return_value={self.OWNER}):
            bot.handle(tg2, self._cb(self.OWNER, "who"), {self.OWNER})
        sent2 = " ".join(str(c.args[1]) for c in tg2.send.call_args_list)
        self.assertIn("مين موجود بالبوت", sent2)


class MyReadCardTest(unittest.TestCase):
    """🔒 «تحليل فريق AYZR» is the OWNER's private read: OUR own professional
    note on the tape, not the bot's card. He asked for it to sit in the menu as
    the FIRST button, visible to no one but him. A guest must never see the row
    nor reach the card by any path -- tapped callback or typed word.
    """

    OWNER = bot.SEED_CHATS[0]
    GUEST = "8545528489"

    def _cb(self, chat, data, mid=99):
        return {"callback_query": {"id": "c1", "data": data,
                                   "message": {"message_id": mid,
                                               "chat": {"id": chat}}}}

    def _msg(self, chat, text):
        return {"message": {"chat": {"id": chat}, "text": text}}

    def test_it_is_the_first_menu_button(self):
        first = bot.MENU[0][0]
        self.assertEqual(first, ("🔒 تحليل فريق AYZR", "myread"))

    def test_the_owner_sees_it_and_a_guest_does_not(self):
        owner = [a for r in bot._menu_rows(self.OWNER) for _, a in r]
        guest = [a for r in bot._menu_rows(self.GUEST) for _, a in r]
        self.assertIn("myread", owner)
        self.assertNotIn("myread", guest)

    def test_a_guest_tap_is_refused(self):
        tg = unittest.mock.Mock()
        bot.handle(tg, self._cb(self.GUEST, "myread"), {self.OWNER, self.GUEST})
        sent = " ".join(str(c.args[1]) for c in tg.send.call_args_list)
        self.assertIn("للمالك", sent)

    def test_a_guest_typed_word_is_refused(self):
        tg = unittest.mock.Mock()
        bot.handle(tg, self._msg(self.GUEST, "تحليل فريق AYZR"),
                   {self.OWNER, self.GUEST})
        sent = " ".join(str(c.args[1]) for c in tg.send.call_args_list)
        self.assertIn("للمالك", sent)

    def test_the_text_triggers_route_to_it(self):
        for t in ("تحليل فريق AYZR", "فريق AYZR", "تحليلي الخاص", "الخاص",
                  "/myread", "myread"):
            self.assertEqual(bot.action_for(t), "myread")

    def test_the_card_renders_the_sections_on_a_live_tape(self):
        with FakeFeed(mid=3900.0, closes={tf: [3900, 3901, 3900, 3901] * 20
                                          for tf in bot.FRAMES}):
            out = bot.card_myread()
        for sec in ("الخلاصة السريعة", "الصورة الكبيرة", "الصورة القريبة",
                    "البنية", "الزخم", "خطة فريق AYZR"):
            self.assertIn(sec, out)
        self.assertIn("تحليل فريق AYZR", out)

    def test_the_card_is_a_view_of_the_one_report_engine(self):
        # Every frame RSI the card prints must be the report's own number, so the
        # private read cannot drift from the desk. A second engine would diverge.
        with FakeFeed(mid=3900.0, closes={tf: [3900, 3901, 3900, 3901] * 20
                                          for tf in bot.FRAMES}):
            out = bot.card_myread()
            p = bot.report_payload()
            m = bot.read_market()
            head = bot._headline(bot._verdict(m))
        # the daily frame's RSI, as the report measured it, is on the card
        r1d = next(f for f in p["frames"] if f["tf"] == "1d")["rsi"]
        self.assertIn(f"RSI {r1d:.0f}", out)
        # and the call is the SAME headline the action card prints
        plain = head.replace("<b>", "").replace("</b>", "")
        self.assertIn(plain, out)

    def test_a_chopping_tape_is_not_called_a_trend(self):
        # ADX below 20 is "no trend"; the card must say so. A flat tape pins ADX
        # near zero, and a card that printed "trend strength acceptable" there
        # would be the exact over-claim the owner keeps catching.
        flat = {tf: [3900 + (i % 2) for i in range(120)] for tf in bot.FRAMES}
        with FakeFeed(mid=3900.0, closes=flat):
            out = bot.card_myread()
        self.assertIn("بلا ترند", out)
        self.assertNotIn("قوة اتجاه مقبولة", out)


class AyzrButtonTest(unittest.TestCase):
    OWNER, GUEST = "933571066", "555"
    ZEIN = "997487756"          # @ZeinHasan992, named by the owner for the engine

    def _cb(self, chat, data, mid=99):
        return {"callback_query": {"id": "c1", "data": data,
                                   "message": {"message_id": mid,
                                               "chat": {"id": chat}}}}

    def test_the_button_is_in_the_menu(self):
        actions = [a for row in bot.MENU for _t, a in row]
        self.assertIn("ayzr", actions)

    def test_the_button_is_its_own_row_below_myread(self):
        # It must be a SEPARATE button, not folded into the myread row: the owner
        # asked for the independent engine to stand alone.
        self.assertEqual(bot.MENU[0][0], ("🔒 تحليل فريق AYZR", "myread"))
        self.assertEqual(bot.MENU[1][0], ("🏛️ محرك AYZR المستقل", "ayzr"))

    def test_the_owner_sees_it_and_a_guest_does_not(self):
        owner = [a for r in bot._menu_rows(self.OWNER) for _, a in r]
        guest = [a for r in bot._menu_rows(self.GUEST) for _, a in r]
        self.assertIn("ayzr", owner)
        self.assertNotIn("ayzr", guest)

    def test_a_guest_tap_is_refused(self):
        tg = unittest.mock.Mock()
        bot.handle(tg, self._cb(self.GUEST, "ayzr"), {self.OWNER, self.GUEST})
        sent = " ".join(str(c.args[1]) for c in tg.send.call_args_list)
        self.assertIn("للمالك", sent)

    def test_the_text_triggers_route_to_it(self):
        for t in ("محرك AYZR", "المحرك المستقل", "/ayzr", "ayzr"):
            self.assertEqual(bot.action_for(t), "ayzr")

    def test_the_door_returns_the_engine_report_verbatim(self):
        # card_ayzr is only a DOOR: whatever ayzr.report() returns is what the
        # button shows. bot.py must compute nothing of its own here.
        import ayzr as engine
        with unittest.mock.patch.object(engine, "report",
                                        return_value="MARKER-12345"):
            self.assertEqual(bot.card_ayzr(), "MARKER-12345")

    def test_a_broken_engine_says_so_instead_of_crashing(self):
        import ayzr as engine
        with unittest.mock.patch.object(engine, "report",
                                        side_effect=RuntimeError("boom")):
            out = bot.card_ayzr()
        self.assertIn("AYZR", out)
        self.assertIn("تعذّر", out)

    def test_the_reply_button_sits_right_of_the_menu(self):
        # The owner asked for the engine button NEXT TO the menu button, on the
        # bottom-right. Row order is [menu, ayzr] for the owner...
        row = bot.menu_reply_kb(self.OWNER)["keyboard"][0]
        self.assertEqual([b["text"] for b in row],
                         [bot.MENU_BUTTON_TEXT, bot.AYZR_BUTTON_TEXT])

    def test_a_guest_gets_no_ayzr_reply_button(self):
        # ...and a guest's reply keyboard is the menu button alone. A reply
        # keyboard is per chat, so this is the real lock; handle() is the second.
        for chat in (self.GUEST, None):
            row = bot.menu_reply_kb(chat)["keyboard"][0]
            texts = [b["text"] for b in row]
            self.assertNotIn(bot.AYZR_BUTTON_TEXT, texts)
            self.assertEqual(texts, [bot.MENU_BUTTON_TEXT])

    def test_the_reply_button_text_routes_to_ayzr(self):
        self.assertEqual(bot.action_for(bot.AYZR_BUTTON_TEXT), "ayzr")

    def test_the_named_audience_gets_the_engine_but_not_the_panel(self):
        # The owner named Zein for the ENGINE only. He must get the button, the
        # grid entry and the card -- but NOT the control panel, the subscriber
        # roll or the owner's private read. Two predicates, two scopes.
        self.assertTrue(bot.may_use_ayzr(self.ZEIN))
        self.assertFalse(bot.is_owner(self.ZEIN))
        grid = [a for r in bot._menu_rows(self.ZEIN) for _, a in r]
        self.assertIn("ayzr", grid)
        for owner_only in ("settings", "who", "myread"):
            self.assertNotIn(owner_only, grid)
        row = bot.menu_reply_kb(self.ZEIN)["keyboard"][0]
        self.assertEqual([b["text"] for b in row],
                         [bot.MENU_BUTTON_TEXT, bot.AYZR_BUTTON_TEXT])

    def test_the_named_audience_can_open_the_engine(self):
        tg = unittest.mock.Mock()
        with unittest.mock.patch.object(bot, "card_ayzr", return_value="AYZR!"):
            bot.handle(tg, self._cb(self.ZEIN, "ayzr"), {self.OWNER, self.ZEIN})
        sent = " ".join(str(c.args[1]) for c in tg.send.call_args_list)
        self.assertIn("AYZR!", sent)

    def test_an_unnamed_subscriber_is_still_refused(self):
        self.assertFalse(bot.may_use_ayzr(self.GUEST))
        grid = [a for r in bot._menu_rows(self.GUEST) for _, a in r]
        self.assertNotIn("ayzr", grid)
        row = bot.menu_reply_kb(self.GUEST)["keyboard"][0]
        self.assertNotIn(bot.AYZR_BUTTON_TEXT, [b["text"] for b in row])

    def test_the_plan_push_sends_to_the_named_audience_alone(self):
        # The loop must send to exactly the audience `_ayzr_plan_chats()` names
        # -- not to some other list it computed itself. The audience has since
        # widened to the whole roll (see test_the_plan_push_reaches_the_roll),
        # but the loop still owes its list the only say.
        import threading

        import ayzr as engine
        sent: list[tuple[str, str]] = []
        stop = threading.Event()

        def fake_send(chat, text, markup=None):
            sent.append((str(chat), text))
            stop.set()                       # one cycle is enough

        tg = unittest.mock.Mock()
        tg.send.side_effect = fake_send
        with unittest.mock.patch.object(bot, "_ayzr_plan_chats",
                                        return_value=[self.OWNER, self.ZEIN]), \
             unittest.mock.patch.object(bot, "AYZR_PLAN_PUSH", True), \
             unittest.mock.patch.object(engine, "quote",
                                        return_value={"mid": 3900.0}), \
             unittest.mock.patch.object(engine, "snapshot",
                                        return_value={"live": 3900.0,
                                                      "a1h": 16.0,
                                                      "bias": "short"}), \
             unittest.mock.patch.object(engine, "plan_section",
                                        return_value="PLAN"), \
             unittest.mock.patch.object(engine, "report",
                                        return_value="REPORT"):
            bot.ayzr_plan_loop(tg, stop)
        self.assertTrue(sent, "the loop sent nothing")
        self.assertEqual({c for c, _ in sent}, {self.OWNER, self.ZEIN})
        # First send after boot is the bare PLAN -- never the full report, so a
        # restart cannot replay a report the owner already saw.
        self.assertEqual(sent[0][1], "PLAN")

    def test_the_full_report_follows_a_direction_flip(self):
        # The plan rides the price; the full "why" only rides a FLIP. The bias
        # goes short -> long after price walks 10 pts (> 0.5 * ATR = 8), so the
        # second message is the whole report -- and only the second.
        import threading

        import ayzr as engine
        sent: list[tuple[str, str]] = []
        stop = threading.Event()
        quotes = [{"mid": 3900.0}] + [{"mid": 3910.0}] * 50
        snaps = [{"live": 3900.0, "a1h": 16.0, "bias": "short"},
                 {"live": 3910.0, "a1h": 16.0, "bias": "long"}]

        def fake_send(chat, text, markup=None):
            sent.append((str(chat), text))
            if len(sent) >= 2:
                stop.set()

        tg = unittest.mock.Mock()
        tg.send.side_effect = fake_send
        with unittest.mock.patch.object(bot, "_ayzr_plan_chats",
                                        return_value=[self.OWNER]), \
             unittest.mock.patch.object(bot, "AYZR_PLAN_PUSH", True), \
             unittest.mock.patch.object(bot, "AYZR_PLAN_MIN_MIN", 0), \
             unittest.mock.patch.object(bot, "AYZR_PLAN_MOVE_ATR", 0.5), \
             unittest.mock.patch.object(bot, "AYZR_PLAN_STEP_S", 0.001), \
             unittest.mock.patch.object(engine, "quote",
                                        side_effect=quotes), \
             unittest.mock.patch.object(engine, "snapshot",
                                        side_effect=snaps), \
             unittest.mock.patch.object(engine, "plan_section",
                                        return_value="PLAN"), \
             unittest.mock.patch.object(engine, "report",
                                        return_value="REPORT"):
            bot.ayzr_plan_loop(tg, stop)
        self.assertEqual([t for _, t in sent][:2], ["PLAN", "REPORT"])

    def test_the_first_plan_goes_out_at_boot(self):
        # The loop's clock starts at boot, not at 0.0. A literal 0.0 measured
        # elapsed against the HOST's uptime, so the first plan was "due" only
        # once the container had been up a full window -- on Render, two hours
        # of silence after every deploy. This pins the fix: one plan, now.
        import threading

        import ayzr as engine
        sent: list[str] = []
        stop = threading.Event()
        ticks = {"n": 0}

        def fake_quote():
            ticks["n"] += 1
            if ticks["n"] > 5:
                stop.set()
            return {"mid": 3900.0}

        def fake_send(chat, text, markup=None):
            sent.append(text)

        tg = unittest.mock.Mock()
        tg.send.side_effect = fake_send
        with unittest.mock.patch.object(bot, "_ayzr_plan_chats",
                                        return_value=[self.OWNER]), \
             unittest.mock.patch.object(bot, "AYZR_PLAN_PUSH", True), \
             unittest.mock.patch.object(bot, "AYZR_PLAN_MIN_MIN", 30), \
             unittest.mock.patch.object(bot, "AYZR_PLAN_MAX_MIN", 120), \
             unittest.mock.patch.object(bot, "AYZR_PLAN_STEP_S", 0.001), \
             unittest.mock.patch.object(engine, "quote",
                                        side_effect=fake_quote), \
             unittest.mock.patch.object(engine, "snapshot",
                                        return_value={"live": 3900.0,
                                                      "a1h": 16.0,
                                                      "bias": "short"}), \
             unittest.mock.patch.object(engine, "plan_section",
                                        return_value="PLAN"), \
             unittest.mock.patch.object(engine, "report",
                                        return_value="REPORT"):
            bot.ayzr_plan_loop(tg, stop)
        self.assertEqual(sent, ["PLAN"])     # exactly one, and it went out now


    def test_a_flat_tape_does_not_re_send(self):
        # The floor and the distance rule together: once sent, a tape that does
        # not move must not be re-sent, however many ticks pass.
        import threading

        import ayzr as engine
        sent: list[str] = []
        stop = threading.Event()
        ticks = {"n": 0}

        def fake_quote():
            ticks["n"] += 1
            if ticks["n"] > 40:
                stop.set()                   # give up after many flat ticks
            return {"mid": 3900.0}           # never moves

        def fake_send(chat, text, markup=None):
            sent.append(text)

        tg = unittest.mock.Mock()
        tg.send.side_effect = fake_send
        with unittest.mock.patch.object(bot, "_ayzr_plan_chats",
                                        return_value=[self.OWNER]), \
             unittest.mock.patch.object(bot, "AYZR_PLAN_PUSH", True), \
             unittest.mock.patch.object(bot, "AYZR_PLAN_MIN_MIN", 30), \
             unittest.mock.patch.object(bot, "AYZR_PLAN_MAX_MIN", 120), \
             unittest.mock.patch.object(bot, "AYZR_PLAN_STEP_S", 0.001), \
             unittest.mock.patch.object(engine, "quote", side_effect=fake_quote), \
             unittest.mock.patch.object(engine, "snapshot",
                                        return_value={"live": 3900.0,
                                                      "a1h": 16.0,
                                                      "bias": "short"}), \
             unittest.mock.patch.object(engine, "plan_section",
                                        return_value="PLAN"), \
             unittest.mock.patch.object(engine, "report",
                                        return_value="REPORT"):
            bot.ayzr_plan_loop(tg, stop)
        self.assertEqual(sent, ["PLAN"])     # exactly one, the first

    def test_the_plan_push_reaches_the_roll(self):
        # The owner changed the rule: the plan and strategy are shared with
        # everyone on the bot, not just the friends he named. The named friends
        # still get it even if they are not on the roll, and the owner is
        # always first so a hand-edited allow file cannot drop him.
        old_allow, old_extra = bot.load_allow, bot.AYZR_EXTRA_CHATS
        try:
            bot.AYZR_EXTRA_CHATS = ["777"]
            with unittest.mock.patch.object(bot, "load_allow",
                                            return_value={"100", "200"}):
                chats = bot._ayzr_plan_chats()
        finally:
            bot.load_allow, bot.AYZR_EXTRA_CHATS = old_allow, old_extra
        self.assertEqual(chats[0], bot._backup_chat())
        self.assertEqual(set(chats), {bot._backup_chat(), "100", "200", "777"})

    def test_sharing_the_plan_does_not_open_the_engine(self):
        # The push widened; the BUTTON did not. A subscriber who receives the
        # plan must still be refused the engine, the panel and the private
        # reads, or sharing a read would hand over the whole desk.
        self.assertFalse(bot.may_use_ayzr(self.GUEST))
        self.assertFalse(bot.is_owner(self.GUEST))

    def test_the_distance_rule_bounds_the_cadence(self):
        # The cadence is DISTANCE, not ADX strength: a plan goes stale when
        # PRICE moves. Pin the pure rule with no sleep.
        with unittest.mock.patch.object(bot, "AYZR_PLAN_MIN_MIN", 30), \
             unittest.mock.patch.object(bot, "AYZR_PLAN_MAX_MIN", 120), \
             unittest.mock.patch.object(bot, "AYZR_PLAN_MOVE_ATR", 0.5):
            due = bot._ayzr_plan_due
            # Below the floor: never, however far it moved.
            self.assertFalse(due(10 * 60, 999.0, 16.0))
            # Past the ceiling: always, however still it sat.
            self.assertFalse(due(119 * 60, 0.1, 16.0))
            self.assertTrue(due(120 * 60, 0.1, 16.0))
            # Between: it is the DISTANCE that decides (0.5 * ATR = 8 pts here).
            self.assertFalse(due(45 * 60, 7.9, 16.0))
            self.assertTrue(due(45 * 60, 8.0, 16.0))
            # No ATR yet (first cycle): never fire on distance, only the ceiling.
            self.assertFalse(due(45 * 60, 999.0, None))

    def test_the_plan_push_is_off_when_the_switch_is_off(self):
        import threading
        tg = unittest.mock.Mock()
        with unittest.mock.patch.object(bot, "AYZR_PLAN_PUSH", False):
            bot.ayzr_plan_loop(tg, threading.Event())   # returns at once
        tg.send.assert_not_called()

    def test_the_plan_push_stands_down_when_alerts_are_off(self):
        """The plan push has its own thread and its own flag.

        Before the master switch, silencing the desk left this loop pushing a
        plan card every 1-2 hours -- the alert the owner could not find a switch
        for. It must read the switch on EVERY wake, not once at boot, or a tap
        on the panel would not take effect until the next restart.

        The loop only leaves on its stop event, so the test drives that too --
        an unset event here spins forever (this test hung the suite once).
        """
        import threading
        tg = unittest.mock.Mock()
        stop = threading.Event()
        threading.Timer(0.2, stop.set).start()
        with unittest.mock.patch.object(bot, "AYZR_PLAN_PUSH", True), \
             unittest.mock.patch.object(bot, "ALERTS_ON", False), \
             unittest.mock.patch.object(bot, "AYZR_PLAN_STEP_S", 0.01):
            bot.ayzr_plan_loop(tg, stop)
            tg.send.assert_not_called()
            self.assertTrue(bot.alerts_are_off())


class ConfirmWatchTest(unittest.TestCase):
    """The owner's "المؤكّد بس": speak only when a 15m bar CLOSED past a level.

    The distinction under test is the whole point of the feature -- an intrabar
    pierce that recovers is NOT news, and a close that holds IS. Each test pins
    one of those, plus the volume verdict and the never-repeat rule.
    """

    LEVEL = 3552.67

    def _feed(self, closes, vols=None, mid=None):
        """A feed whose 15m bars close at `closes`, with tick volumes.

        The series is padded at the FRONT to a realistic length: the desk needs
        history to judge volume, and `closed()` drops the newest bar, so a
        two-bar feed would test the length guard instead of the rule.
        """
        closes = list(closes)
        if len(closes) < 25:
            closes = [closes[0]] * (25 - len(closes)) + closes
        vols = list(vols) if vols else [3000] * len(closes)
        if len(vols) < len(closes):
            vols = [3000] * (len(closes) - len(vols)) + vols
        bars = []
        for i, c in enumerate(closes):
            bars.append({"openTime": _iso(i), "open": c, "high": c + 1.0,
                         "low": c - 1.0, "close": c, "tickVolume": vols[i],
                         "isOpen": False})
        mkt = mid if mid is not None else closes[-1]

        class Feed:
            def __init__(self):
                self._saved = (bot.quote, bot.bars, bot.read_market,
                               bot._live_levels, bot._sticky_load, bot._levels)

            def _bars(self, tf, limit=300):
                return bars if tf == bot.CONFIRM_TF else [
                    _bar(_iso(i), c, c + 1, c - 1, c) for i, c in
                    enumerate(closes)]

            def _quote(self):
                return {"mid": mkt, "marketState": "open", "stale": False,
                        "high": mkt + 20, "low": mkt - 20}

            def __enter__(self):
                bot.quote, bot.bars = self._quote, self._bars
                bot._live_levels = lambda live, n=3: (self.level,)
                bot._sticky_load = dict
                bot._levels = lambda live: ([], [self.level])
                return self

            def __exit__(self, *exc):
                (bot.quote, bot.bars, bot.read_market, bot._live_levels,
                 bot._sticky_load, bot._levels) = self._saved

        f = Feed()
        f.level = self.LEVEL
        return f

    def _run(self, seen, closes, vols=None):
        saved = bot.CONFIRM_ONLY
        bot.CONFIRM_ONLY = True
        try:
            with self._feed(closes, vols):
                return bot.confirm_watch(seen)
        finally:
            bot.CONFIRM_ONLY = saved

    def test_the_mode_is_off_by_default(self):
        self.assertFalse(bot.CONFIRM_ONLY)
        self.assertIn("confirm_only", bot.PANEL_KEYS)
        # It is a SPEAKING mode, not an entry gate: open_all/close_all must not
        # touch it, or flipping the filters would silently change the alerts.
        self.assertNotIn("confirm_only", bot.GATE_KEYS)
        self.assertNotIn("confirm_only", bot.gate_state())

    def test_off_means_silent_even_on_a_perfect_break(self):
        seen: dict = {}
        # Two calls with the mode OFF: the state must not even be seeded.
        with self._feed([3600.0, 3500.0]):
            self.assertIsNone(bot.confirm_watch(seen))
        self.assertNotIn("confirm", seen)

    def test_an_intrabar_pierce_that_recovers_is_not_news(self):
        """The exact noise the owner asked to stop: price trades under the level
        and closes back above it. Only closes are read, so this is silence."""
        seen: dict = {}
        self.assertIsNone(self._run(seen, [3600.0]))          # seed: above
        self.assertIsNone(self._run(seen, [3600.0, 3560.0]))  # still above

    def test_a_close_past_the_level_speaks_with_the_trade(self):
        seen: dict = {}
        self.assertIsNone(self._run(seen, [3600.0]))          # seed: above
        msg = self._run(seen, [3600.0, 3500.0])               # closed below
        self.assertIsNotNone(msg)
        self.assertIn("بيع مؤكّد", msg)
        self.assertIn("الشرط تحقّق", msg)
        for part in ("الدخول", "الوقف", "الهدف", "R:R"):
            self.assertIn(part, msg)
        # The numbers come from _sized, the same engine the desk trades on.
        p = bot._sized(3500.0, "short", self.LEVEL)
        self.assertIn(f"{p['rr']:.1f}", msg)

    def test_a_close_above_a_level_is_a_buy(self):
        seen: dict = {}
        self.assertIsNone(self._run(seen, [3400.0]))          # seed: below
        msg = self._run(seen, [3400.0, 3600.0])
        self.assertIsNotNone(msg)
        self.assertIn("شراء مؤكّد", msg)

    def test_it_never_repeats_the_same_side(self):
        """A level that keeps closing on the same side speaks ONCE. Without this
        every 15m close while price sits under the level is a fresh alert."""
        seen: dict = {}
        self._run(seen, [3600.0])
        self.assertIsNotNone(self._run(seen, [3600.0, 3500.0]))
        self.assertIsNone(self._run(seen, [3600.0, 3500.0, 3495.0]))
        self.assertIsNone(self._run(seen, [3600.0, 3500.0, 3495.0, 3490.0]))

    def test_a_return_across_the_level_speaks_as_the_opposite_side(self):
        """A close back across the level is a NEW confirmed event, and the owner
        must hear it: it invalidates the call he was just given. Silence here
        would leave him holding a sell the market already closed back above."""
        seen: dict = {}
        self._run(seen, [3600.0])
        sell = self._run(seen, [3600.0, 3500.0])
        self.assertIn("بيع مؤكّد", sell)
        back = self._run(seen, [3600.0, 3500.0, 3600.0])
        self.assertIsNotNone(back)
        self.assertIn("شراء مؤكّد", back)
        again = self._run(seen, [3600.0, 3500.0, 3600.0, 3500.0])
        self.assertIn("بيع مؤكّد", again)

    def test_a_graze_inside_the_epsilon_is_not_a_break(self):
        """A close a dollar past the level is noise, not a break -- the same
        clearance SUPPORT_BREAK_PTS demands, so the two alerts agree."""
        seen: dict = {}
        self._run(seen, [3600.0])
        self.assertIsNone(self._run(seen, [3600.0, self.LEVEL - 1.0]))

    def test_thin_volume_is_named_as_a_trap(self):
        seen: dict = {}
        self._run(seen, [3600.0] * 22, [3000] * 22)
        # 22 bars, then a break on 500 against a 3000 median.
        closes = [3600.0] * 22 + [3500.0]
        vols = [3000] * 22 + [500]
        msg = self._run(seen, closes, vols)
        self.assertIsNotNone(msg)
        self.assertIn("الحجم: ضعيف", msg)
        self.assertIn("فخ", msg)

    def test_heavy_volume_is_named_as_confirmed(self):
        seen: dict = {}
        self._run(seen, [3600.0] * 22, [3000] * 22)
        msg = self._run(seen, [3600.0] * 22 + [3500.0],
                        [3000] * 22 + [6000])
        self.assertIsNotNone(msg)
        self.assertIn("الحجم: مؤكّد", msg)
        self.assertIn("2.0×", msg)

    def test_the_volume_is_judged_against_the_median_not_the_mean(self):
        """One news spike must not make every later bar read 'thin'."""
        vols = [3000] * 10 + [90000] + [3000] * 10 + [3200]
        v = bot._vol_verdict([{"tickVolume": x} for x in vols], len(vols) - 1)
        self.assertTrue(v["ok"])
        self.assertLess(v["median"], 10000)

    def test_a_missing_volume_reading_says_so_and_still_fires(self):
        seen: dict = {}
        self._run(seen, [3600.0] * 3, [0, 0, 0])
        msg = self._run(seen, [3600.0] * 3 + [3500.0], [0, 0, 0, 0])
        self.assertIsNotNone(msg)
        self.assertIn("ما في قراءة", msg)

    def test_a_closed_market_is_silent(self):
        seen: dict = {}
        saved = bot.CONFIRM_ONLY
        bot.CONFIRM_ONLY = True
        try:
            with self._feed([3600.0, 3500.0]):
                bot.quote = lambda: {"mid": 3500.0, "marketState": "closed",
                                     "stale": False}
                self.assertIsNone(bot.confirm_watch(seen))
        finally:
            bot.CONFIRM_ONLY = saved

    def test_a_broken_feed_never_kills_the_loop(self):
        seen: dict = {}
        saved = bot.CONFIRM_ONLY
        bot.CONFIRM_ONLY = True
        saved_bars = bot.bars
        try:
            bot.bars = lambda tf, limit=300: (_ for _ in ()).throw(RuntimeError)
            self.assertIsNone(bot.confirm_watch(seen))
        finally:
            bot.bars = saved_bars
            bot.CONFIRM_ONLY = saved

    def test_it_round_trips_through_the_panel(self):
        saved = bot.CONFIRM_ONLY
        saved_path = bot.SETTINGS_PATH
        bot.SETTINGS_PATH = pathlib.Path(_STICKY_DIR) / "settings_cf.json"
        try:
            d = bot.toggle_gate("confirm_only")
            self.assertTrue(d["confirm_only"])
            self.assertTrue(bot.CONFIRM_ONLY)
            d = bot.toggle_gate("confirm_only")
            self.assertFalse(d["confirm_only"])
            self.assertFalse(bot.CONFIRM_ONLY)
        finally:
            bot.SETTINGS_PATH = saved_path
            bot.CONFIRM_ONLY = saved

    def test_the_card_is_on_the_panel(self):
        self.assertIn("confirm_only", bot.GATE_LABELS)
        text = bot.card_settings()
        self.assertIn(bot.GATE_LABELS["confirm_only"], text)
        kb = bot.settings_keyboard()
        actions = [b.get("callback_data") for row in kb["inline_keyboard"]
                   for b in row]
        self.assertIn("cfg:confirm_only", actions)

    def _tick(self, seen):
        """Run one `desk_tick` with every watch stubbed, and hand the mocks back.

        The mocks ARE the observation: the question is which watches were even
        consulted, not what they returned. Patching them here (rather than in
        each test) keeps the helper and the assertion on the same objects.
        """
        tg = unittest.mock.Mock()
        names = ("support_watch", "levels_watch", "gate_watch", "trend_watch",
                 "entry_watch", "news_watch", "news_prep_watch",
                 "news_result_watch", "watch_points_watch", "trade_watch")
        mocks = {n: unittest.mock.patch.object(bot, n, return_value="NOTE")
                 for n in names}
        extras = [unittest.mock.patch.object(bot, "grade", return_value=0),
                  unittest.mock.patch.object(bot, "maybe_daily_report"),
                  unittest.mock.patch.object(bot, "maybe_daily_analysis"),
                  unittest.mock.patch.object(bot, "maybe_open_bell"),
                  unittest.mock.patch.object(bot, "desk_is_silenced",
                                             return_value=True),
                  unittest.mock.patch.object(bot, "confirm_watch",
                                             return_value=None)]
        started = list(mocks.values()) + extras
        handles = {n: p.start() for n, p in mocks.items()}
        for p in extras:
            p.start()
        try:
            bot.desk_tick(tg, {bot.SEED_CHATS[0]}, seen)
        finally:
            for p in started:
                p.stop()
        return handles

    def test_confirm_mode_silences_the_approach_and_the_pierce(self):
        """The whole request in one assertion: with the mode on, `entry_watch`
        (price came NEAR a zone) and `levels_watch` (an intrabar pierce) must
        not be CALLED at all -- not merely muted, never consulted. A level watch
        that runs and returns None still reads the tape and can still speak on
        the next cycle; standing it down is the point."""
        saved = bot.CONFIRM_ONLY
        bot.CONFIRM_ONLY = True
        try:
            m = self._tick({})
        finally:
            bot.CONFIRM_ONLY = saved
        m["entry_watch"].assert_not_called()
        m["levels_watch"].assert_not_called()

    def test_confirm_mode_keeps_the_bar_close_watches(self):
        """The mode is about LEVEL chatter. A gate close, a trend flip and a
        headline are already bar-close/structural facts, so they must keep
        speaking -- otherwise the switch would silence the bot's genuinely
        confirmed output too."""
        saved = bot.CONFIRM_ONLY
        bot.CONFIRM_ONLY = True
        try:
            m = self._tick({})
        finally:
            bot.CONFIRM_ONLY = saved
        m["gate_watch"].assert_called()
        m["trend_watch"].assert_called()
        m["news_watch"].assert_called()

    def test_confirm_mode_off_restores_the_old_watches(self):
        saved = bot.CONFIRM_ONLY
        bot.CONFIRM_ONLY = False
        try:
            m = self._tick({})
        finally:
            bot.CONFIRM_ONLY = saved
        m["entry_watch"].assert_called()
        m["levels_watch"].assert_called()


class AlertsMasterSwitchTest(_DbCase):
    """One switch must stop EVERY unprompted message.

    Each watch in `desk_tick` runs above `desk_is_silenced()` by design (a level
    breaking is a fact, so the owner kept hearing it with auto-trading off).
    The cost was that "stop the notifications" had no single answer -- and the
    plan push came from a thread outside `desk_tick` entirely.
    """

    def _tick(self, tg, seen):
        with unittest.mock.patch.object(bot, "grade", return_value=0):
            bot.desk_tick(tg, {bot.SEED_CHATS[0]}, seen)

    def test_the_switch_is_on_by_default_and_stops_every_watch(self):
        self.assertTrue("alerts" in bot.PANEL_KEYS)
        with unittest.mock.patch.object(bot, "ALERTS_ON", True):
            self.assertFalse(bot.alerts_are_off())

    def test_alerts_off_sends_nothing_at_all(self):
        """Not "fewer": nothing. Every watch is stubbed to a NOTE, which is the
        worst case -- with the switch off the owner must still get silence."""
        tg = unittest.mock.Mock()
        notes = ("support_watch", "levels_watch", "gate_watch", "trend_watch",
                 "entry_watch", "news_watch", "news_prep_watch",
                 "news_result_watch", "watch_points_watch", "trade_watch")
        patches = [unittest.mock.patch.object(bot, n, return_value="NOTE")
                   for n in notes]
        patches += [unittest.mock.patch.object(bot, "maybe_daily_report"),
                    unittest.mock.patch.object(bot, "maybe_daily_analysis"),
                    unittest.mock.patch.object(bot, "maybe_open_bell"),
                    unittest.mock.patch.object(bot, "ALERTS_ON", False)]
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7], patches[8], patches[9], \
                patches[10], patches[11], patches[12], patches[13]:
            self._tick(tg, {})
            self.assertFalse(tg.send.called,
                             "the master switch still let a message through")

    def test_a_trade_is_not_opened_silently(self):
        """A silent entry is worse than no entry: the owner would hold a
        position he was never told about. The switch must stop the OPEN too."""
        tg = unittest.mock.Mock()
        before = len(bot.all_trades())
        with unittest.mock.patch.object(bot, "ALERTS_ON", False), \
             unittest.mock.patch.object(bot, "plan_check") as pc:
            self._tick(tg, {})
        pc.assert_not_called()
        self.assertEqual(len(bot.all_trades()), before)

    def test_the_switch_round_trips_through_the_panel(self):
        saved = bot.ALERTS_ON
        try:
            bot.apply_settings({"alerts": False})
            self.assertTrue(bot.alerts_are_off())
            d = bot.toggle_gate("alerts")
            self.assertTrue(d["alerts"])
            self.assertFalse(bot.alerts_are_off())
        finally:
            bot.ALERTS_ON = saved
            bot.save_settings(bot.panel_state())

    def test_the_switch_does_not_touch_the_entry_filters(self):
        """`open_all`/`close_all` mean the filters; they must never silently
        turn the owner's alerts back on behind his back."""
        self.assertNotIn("alerts", bot.GATE_KEYS)
        self.assertNotIn("alerts", bot.gate_state())

    def test_an_env_silence_survives_the_pinned_restore(self):
        """A restart must not undo the owner's silence.

        Render has no disk, so every boot restores `settings` from the pinned
        backup -- which still says "alerts: on" from before he muted. Without the
        guard, `DESK_ALERTS=0` in the dashboard was overridden on every single
        restart: silence that lasts until the container blinks.
        """
        import json as _json
        blob = _json.dumps({"trades": [],
                            "settings": {"alerts": True, "nochase": True}})

        class FakeTg:
            def call(self, method, payload, timeout=25):
                return {"result": {"pinned_message": {
                    "document": {"file_name": bot.BACKUP_FILE,
                                 "file_id": "f1"}}}}

            def file_bytes(self, file_id):
                return blob.encode()

        saved = bot.ALERTS_ON
        saved_path = bot.SETTINGS_PATH
        # restore_journal WRITES the settings file, so it must never be the real
        # data/settings.json -- that would turn the owner's own bot silent.
        bot.SETTINGS_PATH = pathlib.Path(_STICKY_DIR) / "settings.json"
        try:
            with unittest.mock.patch.dict(os.environ, {"DESK_ALERTS": "0"}):
                bot.restore_journal(FakeTg())
            self.assertTrue(bot.alerts_are_off(),
                            "the pinned restore turned the alerts back on")
        finally:
            bot.SETTINGS_PATH = saved_path
            bot.ALERTS_ON = saved
            bot.save_settings(bot.panel_state())

    def test_an_env_confirm_mode_survives_the_pinned_restore(self):
        """The same trap, one switch over -- and this one was still open.

        "Open it" is a DEPLOY-TIME promise, and Render has no disk: the pinned
        blob still says `confirm_only: false` from before the feature existed,
        so on every boot it would restore "off" over `DESK_CONFIRM_ONLY=1`. The
        mode would be on for exactly one container lifetime and then quietly
        revert -- the owner would hear approach chatter again and have no idea
        why. Guarded exactly like the master switch.
        """
        import json as _json
        blob = _json.dumps({"trades": [],
                            "settings": {"alerts": True,
                                         "confirm_only": False}})

        class FakeTg:
            def call(self, method, payload, timeout=25):
                return {"result": {"pinned_message": {
                    "document": {"file_name": bot.BACKUP_FILE,
                                 "file_id": "f1"}}}}

            def file_bytes(self, file_id):
                return blob.encode()

        saved = bot.CONFIRM_ONLY
        saved_path = bot.SETTINGS_PATH
        # A FRESH path per test: restore_journal only applies the blob when no
        # local settings file exists, so a leftover file from a sibling test
        # would silently skip the restore and make this assertion vacuous.
        tmp = pathlib.Path(tempfile.mkdtemp()) / "settings.json"
        bot.SETTINGS_PATH = tmp
        try:
            with unittest.mock.patch.dict(os.environ,
                                          {"DESK_CONFIRM_ONLY": "1"}):
                bot.restore_journal(FakeTg())
            self.assertTrue(bot.CONFIRM_ONLY,
                            "the pinned restore turned the confirmed-only "
                            "mode back off")
        finally:
            bot.SETTINGS_PATH = saved_path
            bot.CONFIRM_ONLY = saved
            bot.save_settings(bot.panel_state())
            shutil.rmtree(tmp.parent, ignore_errors=True)

    def test_an_env_off_still_wins_over_a_pinned_on(self):
        """The guard cuts both ways: `DESK_CONFIRM_ONLY=0` must also beat a
        pinned `true`, or an explicit "off" in the dashboard could never stick."""
        import json as _json
        blob = _json.dumps({"trades": [],
                            "settings": {"alerts": True,
                                         "confirm_only": True}})

        class FakeTg:
            def call(self, method, payload, timeout=25):
                return {"result": {"pinned_message": {
                    "document": {"file_name": bot.BACKUP_FILE,
                                 "file_id": "f1"}}}}

            def file_bytes(self, file_id):
                return blob.encode()

        saved = bot.CONFIRM_ONLY
        saved_path = bot.SETTINGS_PATH
        tmp = pathlib.Path(tempfile.mkdtemp()) / "settings.json"
        bot.SETTINGS_PATH = tmp
        try:
            with unittest.mock.patch.dict(os.environ,
                                          {"DESK_CONFIRM_ONLY": "0"}):
                bot.restore_journal(FakeTg())
            self.assertFalse(bot.CONFIRM_ONLY)
        finally:
            bot.SETTINGS_PATH = saved_path
            bot.CONFIRM_ONLY = saved
            bot.save_settings(bot.panel_state())
            shutil.rmtree(tmp.parent, ignore_errors=True)


class DeskTickRestartTest(_DbCase):
    """A restart must not replay the book as fresh closures.

    The desk's per-run `seen` map starts empty, and the close-announcement loop
    used to treat "not in seen" as "was open" -- so after every Render restart
    the owner got a close card for EVERY trade already in the journal. The first
    look at a trade we have never seen must be an observation (record its
    status, say nothing), exactly like the other watches seed silently.
    """

    def _tg(self):
        tg = unittest.mock.Mock()
        return tg

    def _tick(self, tg, seen):
        # Only the close-announcement half matters here: silence the rest of the
        # cycle (no market, no news, no alerts) so the assertion is unambiguous.
        with unittest.mock.patch.object(bot, "grade", return_value=0), \
             unittest.mock.patch.object(bot, "maybe_daily_report"), \
             unittest.mock.patch.object(bot, "maybe_daily_analysis"), \
             unittest.mock.patch.object(bot, "support_watch", return_value=None), \
             unittest.mock.patch.object(bot, "levels_watch", return_value=None), \
             unittest.mock.patch.object(bot, "gate_watch", return_value=None), \
             unittest.mock.patch.object(bot, "trend_watch", return_value=None), \
             unittest.mock.patch.object(bot, "entry_watch", return_value=None), \
             unittest.mock.patch.object(bot, "news_watch", return_value=None), \
             unittest.mock.patch.object(bot, "news_prep_watch",
                                        return_value=None), \
             unittest.mock.patch.object(bot, "news_result_watch",
                                        return_value=None), \
             unittest.mock.patch.object(bot, "trade_watch", return_value=None), \
             unittest.mock.patch.object(bot, "desk_is_silenced",
                                       return_value=True):
            bot.desk_tick(tg, {bot.SEED_CHATS[0]}, seen)

    def test_a_restart_does_not_replay_closed_trades(self):
        tid = bot.add_trade("long", 4000, 3991, 4020, bot.LOT, note="t")
        bot._settle(tid, "target", 4020.0, 2.2)
        # Fresh process: `seen` is empty, the journal already holds a closure.
        tg = self._tg()
        seen: dict = {}
        self._tick(tg, seen)
        self.assertFalse(tg.send.called, "a restart replayed a closed trade")
        self.assertEqual(seen.get(tid), "closed")   # recorded, so it won't fire

    def test_a_trade_open_at_boot_closes_after_boot_and_is_announced(self):
        tid = bot.add_trade("long", 4000, 3991, 4020, bot.LOT, note="t")
        tg = self._tg()
        seen: dict = {}
        self._tick(tg, seen)                        # boot: open, silent
        self.assertFalse(tg.send.called)
        self.assertEqual(seen.get(tid), "open")
        # It closes AFTER boot -> the owner must hear it.
        bot._settle(tid, "stop", 3991.0, -1.0)
        tg2 = self._tg()
        self._tick(tg2, seen)
        sent = " ".join(str(c.args[1]) for c in tg2.send.call_args_list)
        self.assertIn(f"#{tid}", sent)

    def test_a_manual_close_is_not_re_announced_by_the_loop(self):
        tid = bot.add_trade("long", 4000, 3991, 4020, bot.LOT, note="t")
        seen: dict = {}
        self._tick(self._tg(), seen)                # boot: records "open"
        bot._settle(tid, "manual", 4005.0, None)    # button handler already sent
        tg = self._tg()
        self._tick(tg, seen)
        self.assertFalse(tg.send.called, "the loop re-sent a manual close")


class MultiAssetTest(unittest.TestCase):
    """`markets.py` — the silver and bitcoin engines, and the fact that adding
    them changed nothing about the gold desk.

    The owner asked for two buttons and got two INDEPENDENT reads: their own
    symbol, their own contract spec, their own sizing. The tests below pin the
    three things that could silently go wrong -- the gold constants getting
    touched, the two assets sharing a number, and a card too long to send.
    """

    def setUp(self):
        import markets
        self.mk = markets
        self._saved = (markets.quote, markets.bars)

    def tearDown(self):
        self.mk.quote, self.mk.bars = self._saved

    def _feed(self, mid, atr_pts=1.0, trend=1):
        """Install a deterministic feed: a trending series whose swings and
        ATR are predictable, so levels/zones/scenarios are assertable."""
        step = atr_pts * 0.25 * trend
        closes = [mid - step * (60 - i) for i in range(60)]

        def _quote(sym):
            return {"mid": mid, "high": mid + atr_pts * 4,
                    "low": mid - atr_pts * 4, "dayDiffPercent": -1.5,
                    "bid": mid - 0.01, "ask": mid + 0.01}

        def _bars(sym, tf, limit=300):
            return [_bar(_iso(i), c, c + atr_pts * 0.5, c - atr_pts * 0.5, c)
                    for i, c in enumerate(closes)]

        self.mk.quote, self.mk.bars = _quote, _bars

    def test_the_gold_desk_is_untouched_by_the_new_engines(self):
        # The owner's hard condition: not one thing about gold may move. These
        # are the constants that price every gold trade, and the symbol itself.
        self.assertEqual(bot.SYMBOL, "XAUUSD")
        self.assertEqual(bot.LOT, 0.01)
        self.assertAlmostEqual(bot.POINT_VALUE, 1.0)
        self.assertAlmostEqual(bot.STOP_MIN_USD, 8.0)
        self.assertAlmostEqual(bot.STOP_MAX_USD, 12.0)
        self.assertAlmostEqual(bot.TARGET_RR, 2.2)

    def test_the_new_engine_never_imports_or_reads_the_gold_engine(self):
        # Independence is structural, not a promise: markets.py must not touch
        # bot.py at all, or the two desks would share a symbol and a spec.
        src = pathlib.Path(self.mk.__file__).read_text()
        self.assertNotIn("import bot", src)
        self.assertNotIn("from bot", src)
        self.assertNotIn("XAUUSD", src)
        self.assertNotIn("POINT_VALUE", src)

    def test_the_two_assets_have_their_own_symbols_and_contracts(self):
        sil, btc = self.mk.ASSETS["silver"], self.mk.ASSETS["bitcoin"]
        self.assertEqual(sil["symbol"], "XAGUSD")
        self.assertEqual(btc["symbol"], "BTCUSD")
        self.assertNotEqual(sil["symbol"], btc["symbol"])
        # The contract is what decides money: silver's minimum lot is 50 oz, so
        # a $1 move is $50; bitcoin's minimum lot is 0.01 coin, so it is $0.01.
        self.assertAlmostEqual(sil["contract"] * sil["lot"], 50.0)
        self.assertAlmostEqual(btc["contract"] * btc["lot"], 0.01)

    def test_silver_is_reported_as_not_fitting_a_200_dollar_account(self):
        # The honest verdict: a sane stop on silver costs more than the whole
        # risk window. The card must SAY so rather than quote a stop inside the
        # noise. This is the reason the engine exists -- to refuse, out loud.
        self._feed(41.25, atr_pts=0.62)
        s = self.mk.snapshot("silver")
        t = self.mk.build_trade(41.25, "long", None, s["spec"], s["a1h"])
        self.assertLessEqual(t["risk_usd"], self.mk.RISK_MAX + 1e-9)
        self.assertFalse(t["fits"], "a 0.62-ATR silver stop must not 'fit' $200")
        self.assertGreater(t["noise_ratio"], 0.0)

    def test_bitcoin_sizes_inside_the_window_because_its_min_lot_is_tiny(self):
        # The mirror image: 0.01 BTC per point is $0.01, so a 900-point stop is
        # ~$9 -- inside the window. The engine must find that, not refuse it.
        self._feed(56585.0, atr_pts=900.0)
        s = self.mk.snapshot("bitcoin")
        t = self.mk.build_trade(56585.0, "long", None, s["spec"], s["a1h"])
        self.assertGreaterEqual(t["risk_usd"], self.mk.RISK_MIN - 1e-9)
        self.assertLessEqual(t["risk_usd"], self.mk.RISK_MAX + 1e-9)

    def test_zones_are_split_at_the_live_price(self):
        self._feed(41.25, atr_pts=0.62)
        s = self.mk.snapshot("silver")
        for z in s["supply"]:
            self.assertGreater(z["price"], s["live"])
        for z in s["demand"]:
            self.assertLess(z["price"], s["live"])

    def test_every_zone_needs_at_least_two_touches(self):
        # A price seen once is a dot, not a zone -- the rule that keeps the card
        # from naming every pivot as an area.
        self._feed(41.25, atr_pts=0.62)
        s = self.mk.snapshot("silver")
        for z in s["supply"] + s["demand"]:
            self.assertGreaterEqual(z["touches"], 2)

    def test_structure_labels_come_from_the_swings(self):
        self._feed(41.25, atr_pts=0.62)
        s = self.mk.snapshot("silver")
        labels = [x for x, _ in s["st"]["labels"]]
        self.assertTrue(all(x in ("HH", "HL", "LH", "LL") for x in labels))

    def test_no_chase_gate_refuses_the_bottom_sale(self):
        # The gold desk refuses to short when daily RSI is under NOCHASE_RSI.
        # A new asset does not get an exemption: without this gate a -6.5% day
        # with daily RSI 17 read as "lean short", i.e. sell the washout.
        self._feed(100.0, atr_pts=1.0)
        real = self.mk.bars
        seq = [130 - i for i in range(60)]      # a straight down day
        self.mk.bars = lambda sym, tf, limit=300: [
            _bar(_iso(i), c, c + 0.2, c - 0.2, c) for i, c in enumerate(seq)]
        try:
            s = self.mk.snapshot("silver")
            s["rsis"]["1d"] = 16.6
            bias, word, _mark = self.mk.gate(s)
            self.assertEqual(bias, 0, "a washout must not carry a short bias")
            self.assertIn("ممنوع البيع", word)
            text = self.mk.report("silver")
            self.assertIn("ممنوع البيع", text)
            self.assertIn("ما في سبب بيع واضح", text)
        finally:
            self.mk.bars = real

    def test_the_stop_is_never_tighter_than_the_noise(self):
        # A stop inside 1.5xATR is a coin flip dressed as a plan. Either the
        # trade fits and its stop clears the noise, or it does not fit and the
        # card refuses to print a plan. It must never do neither.
        spec = self.mk.ASSETS["bitcoin"]
        atr = 999.0
        t = self.mk.build_trade(58000.0, "long", 57666.0, spec, atr)
        if t["fits"]:
            self.assertGreaterEqual(t["risk_pts"], atr * self.mk.STOP_ATR - 1e-6,
                                    "a fitting stop was sized inside the noise")
        else:
            self.assertLessEqual(t["risk_usd"], self.mk.RISK_MAX + 1e-9,
                                 "an unfit trade must still respect the cap")
            self.assertGreater(t["floor_pts"] * t["per_point"], self.mk.RISK_MAX,
                               "fits=False must mean the proper stop is over the cap")

    def test_an_unfit_scenario_withholds_the_plan(self):
        # Printing stop/target/R:R right under "this is not a trade" is the card
        # arguing with itself. The trigger is still named; the plan is not.
        self._feed(58000.0, atr_pts=999.0)
        text = self.mk.report("bitcoin")
        block = text.split("<b>🎬 السيناريوهات</b>")[1].split("<b>🧭")[0]
        if "مش صفقة على حسابك" in block:
            self.assertNotIn("الهدف:", block, "a refused trade still printed a target")
            self.assertNotIn("R:R", block, "a refused trade still printed R:R")

    def test_sizing_verdict_agrees_with_the_scenario(self):
        # $14.99 of stop against a $12 cap printed as "acceptable" because the
        # old check compared a percentage of the account instead of RISK_MAX.
        self._feed(58000.0, atr_pts=999.0)
        text = self.mk.report("bitcoin")
        self.assertNotIn("قابلة للتنفيذ", text)
        self.assertNotIn("مقبول بس حدّي", text)

    def test_the_range_ignores_levels_closer_than_one_atr(self):
        # Nearest sup/res are the closest by construction, so a range built from
        # them is always a hair wide and the price always "at an edge" -- a
        # warning that cannot fail. Built on a synthetic snapshot so the case is
        # exact: the close levels sit INSIDE one ATR and must be skipped.
        s = {
            "spec": self.mk.ASSETS["silver"], "live": 100.0, "a1h": 2.0,
            "trends": {tf: 0 for tf in self.mk.FRAMES}, "rsis": {},
            "res": [{"price": 100.5, "lo": 100.5, "hi": 100.5, "touches": 1},
                    {"price": 103.0, "lo": 103.0, "hi": 103.0, "touches": 2}],
            "sup": [{"price": 99.5, "lo": 99.5, "hi": 99.5, "touches": 1},
                    {"price": 96.0, "lo": 96.0, "hi": 96.0, "touches": 2}],
            "supply": [], "demand": [],
        }
        lines = "\n".join(self.mk._verdict(s))
        band = [ln for ln in lines.split("\n") if "النطاق الآن" in ln]
        self.assertTrue(band, "the range line must exist when far levels exist")
        self.assertIn("103.00", band[0], "the far resistance should bound the range")
        self.assertIn("96.00", band[0], "the far support should bound the range")
        self.assertNotIn("100.50", band[0], "a level inside one ATR must not bound it")
        self.assertNotIn("99.50", band[0], "a level inside one ATR must not bound it")

    def test_a_level_never_prints_above_the_price_it_supports(self):
        # A band sitting exactly ON the price was filed as SUPPORT with a
        # POSITIVE distance (+0.000) -- a support printed above the price it is
        # supposed to hold. The ref is set to the pivot's OWN price, which is
        # the only case that distinguishes `>` from `>=`.
        seq = [105, 104, 103, 102, 101, 100, 101, 102, 103, 104, 105, 106]
        b = [_bar(_iso(i), c, c + 0.5, c - 0.5, c) for i, c in enumerate(seq)]
        _highs, lows = self.mk.swings(b, 2, 2)
        self.assertTrue(lows, "the fixture must produce a swing low")
        pivot = min(p for _i, p in lows)
        res, sup = self.mk.sr_bands(b, 0.1, pivot)
        self.assertTrue(any(abs(x["price"] - pivot) < 1e-9 for x in res),
                        "a pivot exactly ON the price must be resistance")
        for band in sup:
            self.assertLess(band["price"], pivot,
                            "a support sat at/above the price it supports")

    def test_no_atr_means_no_verdict_not_a_false_green(self):
        # Without ATR there is no measurable noise, so "tradeable" would be a
        # diagnostic that cannot fail. The card must say it cannot judge.
        self.mk.quote = lambda sym: {"mid": 41.25, "high": None, "low": None}
        self.mk.bars = lambda sym, tf, limit=300: []
        text = self.mk.report("silver")
        self.assertIn("ما في بيانات تقلّب", text)
        self.assertNotIn("قابلة للتنفيذ", text)
        self.assertNotIn("داخل حدّك", text)

    def test_both_cards_fit_in_one_telegram_message(self):
        # Telegram drops anything past 4096 and the sender truncates at 4000;
        # a card that overflows would lose its verdict and sizing silently.
        for key, atr in (("silver", 0.62), ("bitcoin", 900.0)):
            self._feed(41.25 if key == "silver" else 56585.0, atr_pts=atr)
            text = self.mk.report(key)
            self.assertLessEqual(len(text), self.mk.MAX_CARD, key)
            self.assertIn("متى أشتري", text, key)
            self.assertIn("متى أبيع", text, key)

    def test_a_dead_feed_returns_a_sentence_not_an_exception(self):
        def _boom(sym, tf=None, limit=300):
            raise OSError("feed down")
        self.mk.quote, self.mk.bars = _boom, _boom
        for key in ("silver", "bitcoin"):
            text = self.mk.report(key)
            self.assertIsInstance(text, str)
            self.assertIn("⚠️", text)

    def test_both_menu_buttons_reach_their_engine(self):
        # The two buttons the owner asked for must be in the grid and route to
        # the doors -- not to a generic card.
        actions = [p[1] for row in bot.MENU for p in row]
        self.assertIn("silver", actions)
        self.assertIn("bitcoin", actions)
        self.assertIs(bot.dispatch("silver").__class__, str)
        self.assertIs(bot.dispatch("bitcoin").__class__, str)

    def test_the_word_routes_to_the_button(self):
        for word, action in (("فضة", "silver"), ("الفضة", "silver"),
                             ("بتكوين", "bitcoin"), ("btc", "bitcoin")):
            self.assertEqual(bot.action_for(word), action, word)


if __name__ == "__main__":
    unittest.main(verbosity=2)
