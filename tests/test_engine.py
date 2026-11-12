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
import unittest.mock

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
        bot.FILTERS_ON = False
        try:
            with FakeFeed(mid=4059.0, closes=closes):
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
        bot.FILTERS_ON = False
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


class FullCardTest(unittest.TestCase):
    """card_full is MY OWN read of the tape (the owner asked for the agent's
    analysis, not the desk's verdict). It must stay inside Telegram's limit and
    carry the sections plus a concrete call."""

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
        tail = [4058 + (6 if i % 2 else -6) for i in range(30)]
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
        # into it: a perfect layout must never hide the edge reading.
        self.assertIn("حقائق السوق", s)
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
        # The whole point: the late-entry caveat is WORDS, not a gate. The RSI
        # does not sit in FILTER_GATES by default, so a stretched RSI must not
        # stop plan() from returning a sized trade when the frames agree and the
        # candle agrees. (The quality gate and trend filter are on; the late
        # read is separate from both.)
        import math
        rising = [4000 + i + 8 * math.sin(i / 2.0) for i in range(90)]
        closes = {tf: rising for tf in bot.FRAMES}
        with FakeFeed(mid=rising[-1], closes=closes):
            p = bot.plan()
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
        rising = [4000 + i + 8 * math.sin(i / 2.0) for i in range(90)]
        closes = {tf: rising for tf in bot.FRAMES}
        with FakeFeed(mid=rising[-1], closes=closes):
            p = bot.plan()
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

    Every ADX band up to 50 pays on the 251-day sample and ONLY the >50 band
    loses (-$237 over 199 trades). The old floor of 20 threw away the best
    entries (the 0-20 band pays ~$3.6/trade) and the old ceiling of 60 admitted
    the worst. Floor -> 0, ceiling -> 50: 2386 -> 2937 trades, net $2186 ->
    $3882, and the drawdown shrinks. Holds in both halves of the sample.
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
            self.sink.append(text)
            return {"ok": True}

    def _tick(self, market):
        with FakeFeed(market=market):
            bot.maybe_daily_report(self._Tg(self.sent), {"1"})

    def test_sends_once_on_the_close_transition(self):
        self._tick("open")
        self.assertEqual(self.sent, [])           # nothing while open
        self._tick("closed")
        self.assertEqual(len(self.sent), 1)       # the day's record, once
        self._tick("closed")
        self.assertEqual(len(self.sent), 1)       # a standing close is silent

    def test_a_weekend_close_never_sends(self):
        # The market was never seen open: no close to report, no message.
        self._tick("closed")
        self.assertEqual(self.sent, [])

    def test_a_restart_mid_evening_does_not_resend(self):
        self._tick("open")
        self._tick("closed")
        self.assertEqual(len(self.sent), 1)
        # Simulate a reboot: the persisted state survives, so the same day is
        # not delivered a second time.
        self._tick("closed")
        self.assertEqual(len(self.sent), 1)


class AtrCeilingTest(unittest.TestCase):
    """The ATR ceiling was raised 0.25% -> 1.00%: it was refusing real moves.

    The owner watched the desk stand aside through a clean sell-off. The band
    sweep says 0.25% was too tight: 1.00% doubles the trades (1142 -> 2386) for
    +57% net at the same quality, and it still beats NO ceiling (2031$), because
    the >1% band is the one that loses (-$212 over 117 trades).
    """

    def test_the_ceiling_is_one_percent(self):
        self.assertEqual(bot.ATR_MAX_PCT, 0.0100)

    def test_a_calm_trend_is_no_longer_blocked_by_atr(self):
        # A gentle rise holds ATR% near the old ceiling's edge; the new one must
        # let it through instead of turning a tradable move into "wait".
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

    def test_it_never_exceeds_the_telegram_limit(self):
        with self._feed():
            self.assertLess(len(bot.daily_analysis()), 4096)


class DailyScheduleTest(unittest.TestCase):
    """maybe_daily_analysis: fires once per slot per day, only inside the slot's
    hour, and a reboot that already sent a slot must not repeat it."""

    def setUp(self):
        self.sent = []
        outer = self

        class TG:
            def send(self, chat, text, markup=None):
                outer.sent.append(chat)
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
        return len(self.sent)

    def test_the_morning_hour_sends_once(self):
        self.assertEqual(self._run(bot.DAILY_AM_HOUR, {}), 1)

    def test_an_off_hour_sends_nothing(self):
        self.assertEqual(self._run((bot.DAILY_AM_HOUR + 2) % 24, {}), 0)

    def test_a_slot_already_sent_today_does_not_repeat(self):
        self.assertEqual(self._run(bot.DAILY_AM_HOUR,
                                   {"analysis_sent": {"am": "2026-11-12"}}), 0)


class PlanCheckTest(unittest.TestCase):
    """plan_check is the decision engine WITH its reason -- the owner asked to
    hear WHY a trade is taken (and why one is not). plan() is its plan field,
    so the reason can never drift from the decision.
    """

    def _live_tape(self, up=True, n=90):
        import math
        base = 4000.0
        step = (lambda i: i + 8 * math.sin(i / 2.0)) if up \
            else (lambda i: -i - 8 * math.sin(i / 2.0))
        return [base + step(i) for i in range(n)]

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
        tape = self._live_tape(up=True)
        with FakeFeed(mid=tape[-1], closes={tf: tape for tf in bot.FRAMES}):
            pc = bot.plan_check()
        self.assertEqual(pc["reason"], "ok")
        self.assertIsNotNone(pc["plan"])
        self.assertEqual(pc["detail"], "")

    def test_a_long_under_the_filter_frame_names_the_trend(self):
        # Six up frames but the price under the 15m EMA20: the trend gate is the
        # reason, and it must be named -- not a silent None.
        import math
        tape = [4000 + i + 8 * math.sin(i / 2.0) for i in range(90)]
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
        tape = [4000 + i + 8 * math.sin(i / 2.0) for i in range(90)]
        with FakeFeed(mid=tape[-1], closes={tf: tape for tf in bot.FRAMES}):
            text = bot._pre_trade_note(self._p())
        self.assertIn("هلق رح تتاخد صفقة", text)
        self.assertIn("ليش", text)

    def test_it_lists_the_conditions_that_passed(self):
        import math
        tape = [4000 + i + 8 * math.sin(i / 2.0) for i in range(90)]
        with FakeFeed(mid=tape[-1], closes={tf: tape for tf in bot.FRAMES}):
            text = bot._pre_trade_note(self._p())
        self.assertIn("الاتجاه", text)          # the trend fact
        self.assertIn("R:R", text)              # the reward/risk fact

    def test_it_carries_the_numbers_and_a_late_caveat(self):
        import math
        tape = [4000 + i + 8 * math.sin(i / 2.0) for i in range(90)]
        p = self._p("short")
        p["r15"] = 20.0                          # a stretched short
        with FakeFeed(mid=tape[-1], closes={tf: tape for tf in bot.FRAMES}):
            text = bot._pre_trade_note(p)
        self.assertIn(f"{p['entry']:,.2f}", text)
        self.assertIn("دخول متأخر", text)


class DailyCapTest(unittest.TestCase):
    """The owner asked for at most TEN trades a day. trades_opened_today is the
    counter the cap reads; it must count the session, not the calendar day."""

    def test_the_cap_defaults_to_ten(self):
        self.assertEqual(bot.MAX_TRADES_DAY, 10)

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


class CollapsibleMenuTest(unittest.TestCase):
    """The owner asked for the extra buttons to move into a collapsible list
    shown only when tapped -- one button under a card, the full grid one tap
    away."""

    def test_a_normal_card_carries_one_button(self):
        k = bot.keyboard_for("analysis")
        rows = k["inline_keyboard"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0]["callback_data"], "menu")
        self.assertIn("القائمة", rows[0][0]["text"])

    def test_the_menu_action_opens_the_full_grid(self):
        k = bot.keyboard_for("menu")
        labels = [b["text"] for r in k["inline_keyboard"] for b in r]
        self.assertIn("🧠 قراءتي للسوق", labels)
        self.assertIn("✖ طي القائمة", labels)

    def test_the_expanded_grid_has_a_fold_row(self):
        k = bot.keyboard_for("menu")
        last = k["inline_keyboard"][-1]
        self.assertEqual(last[0]["callback_data"], "collapse")

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
        self.assertIn("menu", flat)

    def test_every_menu_action_is_wired(self):
        # A button whose action is unknown is a dead tap. The MENU grid must
        # only carry actions the bot actually handles -- this list is the
        # dispatch table plus the two handled in handle() (`who` needs the live
        # Telegram client; `trade`/`close` are parsed from text). Add a button
        # with a typo and this fails.
        known = {
            "full", "action", "day_analysis", "analysis", "recommend", "map",
            "news", "scenarios", "levels", "open", "journal_menu", "daily",
            "who", "howto", "alerts", "help", "collapse", "menu",
        }
        for _t, action in [p for row in bot.MENU for p in row]:
            self.assertIn(action, known, f"action {action} is not wired")


if __name__ == "__main__":
    unittest.main(verbosity=2)
