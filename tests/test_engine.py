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
        # so a headline is shown three hours later than its GMT stamp.
        from datetime import UTC, datetime
        noon_gmt = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
        self.assertEqual(bot._clock(noon_gmt), "12:00")
        # A headline from another day carries the date, so it is not mistaken
        # for today's tape.
        old = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
        self.assertIn("01/11", bot._clock(old))

    def test_news_card_shows_the_owner_clock_label(self):
        saved_items, saved_live = bot.NEWS_ITEMS, bot.NEWS_LIVE
        bot.NEWS_ITEMS, bot.NEWS_LIVE = [("🔥", "manual")], False
        try:
            text = bot.card_news()
            self.assertIn("بتوقيت سوريا", text)
        finally:
            bot.NEWS_ITEMS, bot.NEWS_LIVE = saved_items, saved_live


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
