"""
Regression tests for market_snapshot_telegram.py (POC added 2026-07-23,
Telegram-only market snapshot while X posting stays off). No network,
stdlib unittest/unittest.mock against a fake Context.
"""
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.triggers import market_snapshot_telegram as mst


def _make_ctx(thresholds_cfg=None, seasonality_cfg=None, watchlist=None, now=None, state=None):
    ctx = MagicMock()
    ctx.config = {
        "thresholds": {
            "market_snapshot": thresholds_cfg
            or {"symbols": ["SPY", "QQQ", "AAPL"], "max_posts_per_run": 2}
        },
        "seasonality": seasonality_cfg if seasonality_cfg is not None else {},
        "watchlist": watchlist
        or {"stocks_broad": [{"symbol": "SPY"}, {"symbol": "QQQ"}], "stocks": [{"symbol": "SPY"}]},
    }
    # 2026-07-23 15:00 UTC == 11:00 EDT (a Thursday, regular US session)
    ctx.now = now or datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)
    ctx.state = state if state is not None else {}
    return ctx


class TestEmojiForChange(unittest.TestCase):
    def test_strong_gain_is_green(self):
        self.assertEqual(mst._emoji_for_change(2.5), "🟢")

    def test_strong_loss_is_red(self):
        self.assertEqual(mst._emoji_for_change(-3.0), "🔴")

    def test_small_move_is_yellow(self):
        self.assertEqual(mst._emoji_for_change(0.2), "🟡")
        self.assertEqual(mst._emoji_for_change(-0.5), "🟡")

    def test_missing_data_is_white(self):
        self.assertEqual(mst._emoji_for_change(None), "⚪")

    def test_boundary_values(self):
        self.assertEqual(mst._emoji_for_change(1.0), "🟢")
        self.assertEqual(mst._emoji_for_change(-1.0), "🔴")


class TestScenarioTemplates(unittest.TestCase):
    def test_strong_gain_bucket(self):
        self.assertEqual(mst._scenario_templates(5.0), mst._STRONG_GAIN_TEMPLATES)
        self.assertEqual(mst._scenario_templates(2.0), mst._STRONG_GAIN_TEMPLATES)

    def test_mild_gain_bucket(self):
        self.assertEqual(mst._scenario_templates(1.0), mst._MILD_GAIN_TEMPLATES)
        self.assertEqual(mst._scenario_templates(0.3), mst._MILD_GAIN_TEMPLATES)

    def test_flat_bucket(self):
        self.assertEqual(mst._scenario_templates(0.0), mst._FLAT_TEMPLATES)
        self.assertEqual(mst._scenario_templates(0.29), mst._FLAT_TEMPLATES)
        self.assertEqual(mst._scenario_templates(-0.29), mst._FLAT_TEMPLATES)
        self.assertEqual(mst._scenario_templates(None), mst._FLAT_TEMPLATES)

    def test_mild_loss_bucket(self):
        self.assertEqual(mst._scenario_templates(-0.3), mst._MILD_LOSS_TEMPLATES)
        self.assertEqual(mst._scenario_templates(-1.9), mst._MILD_LOSS_TEMPLATES)

    def test_strong_loss_bucket(self):
        self.assertEqual(mst._scenario_templates(-2.0), mst._STRONG_LOSS_TEMPLATES)
        self.assertEqual(mst._scenario_templates(-9.0), mst._STRONG_LOSS_TEMPLATES)


class TestChooseTemplate(unittest.TestCase):
    def test_never_repeats_the_immediately_prior_template(self):
        templates = ("A {symbol}", "B {symbol}", "C {symbol}")
        state = {}
        first = mst._choose_template(templates, "SPY", state)
        for _ in range(20):
            nxt = mst._choose_template(templates, "SPY", state)
            self.assertNotEqual(nxt, first)
            first = nxt

    def test_single_template_bank_still_works(self):
        templates = ("only option {symbol}",)
        state = {}
        for _ in range(5):
            self.assertEqual(mst._choose_template(templates, "SPY", state), "only option {symbol}")

    def test_tracks_last_template_independently_per_symbol(self):
        templates = ("A {symbol}", "B {symbol}")
        state = {}
        mst._choose_template(templates, "SPY", state)
        mst._choose_template(templates, "QQQ", state)
        self.assertIn("SPY", state["last_template_index"])
        self.assertIn("QQQ", state["last_template_index"])


class TestSessionPhase(unittest.TestCase):
    def _ctx_at(self, utc_dt):
        ctx = MagicMock()
        ctx.now = utc_dt
        return ctx

    def test_regular_session_on_a_weekday(self):
        # 15:00 UTC == 11:00 EDT on a Thursday
        ctx = self._ctx_at(datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(mst._session_phase(ctx), "regular")

    def test_premarket_on_a_weekday(self):
        # 11:00 UTC == 07:00 EDT
        ctx = self._ctx_at(datetime(2026, 7, 23, 11, 0, tzinfo=timezone.utc))
        self.assertEqual(mst._session_phase(ctx), "premarket")

    def test_afterhours_on_a_weekday(self):
        # 21:00 UTC == 17:00 EDT
        ctx = self._ctx_at(datetime(2026, 7, 23, 21, 0, tzinfo=timezone.utc))
        self.assertEqual(mst._session_phase(ctx), "afterhours")

    def test_closed_late_at_night_on_a_weekday(self):
        # 03:00 UTC == 23:00 EDT (previous day) -- outside 4am-8pm ET
        ctx = self._ctx_at(datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc))
        self.assertEqual(mst._session_phase(ctx), "closed")

    def test_closed_on_saturday(self):
        # 2026-07-25 is a Saturday
        ctx = self._ctx_at(datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(mst._session_phase(ctx), "closed")

    def test_closed_on_sunday(self):
        # 2026-07-26 is a Sunday
        ctx = self._ctx_at(datetime(2026, 7, 26, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(mst._session_phase(ctx), "closed")


class TestSeasonalNote(unittest.TestCase):
    def test_includes_month_and_weekday_notes_when_present(self):
        ctx = _make_ctx(seasonality_cfg={"months": {"7": "July note."}, "weekdays": {"Thursday": "Thu note."}})
        note = mst._seasonal_note(ctx, ctx.config["seasonality"])
        self.assertIn("July note.", note)
        self.assertIn("Thu note.", note)

    def test_empty_config_gives_empty_note(self):
        ctx = _make_ctx(seasonality_cfg={})
        note = mst._seasonal_note(ctx, ctx.config["seasonality"])
        self.assertEqual(note, "")

    def test_missing_month_entry_still_includes_weekday(self):
        ctx = _make_ctx(seasonality_cfg={"weekdays": {"Thursday": "Thu note."}})
        note = mst._seasonal_note(ctx, ctx.config["seasonality"])
        self.assertEqual(note, "🗓️ Thursday: Thu note.")


class TestRun(unittest.TestCase):
    @patch("src.triggers.market_snapshot_telegram.telegram_client.send_channel_message", return_value=True)
    @patch("src.triggers.market_snapshot_telegram.twelvedata.get_quotes_batch")
    def test_sends_biggest_movers_first_capped_at_max_posts(self, mock_batch, mock_send):
        mock_batch.return_value = {
            "SPY": {"price": 600.0, "percent_change": 0.2},
            "QQQ": {"price": 500.0, "percent_change": -2.5},
            "AAPL": {"price": 200.0, "percent_change": 1.1},
        }
        ctx = _make_ctx()

        fired = mst.run(ctx)

        self.assertTrue(fired)
        self.assertEqual(mock_send.call_count, 2)  # max_posts_per_run=2
        sent_texts = [call.args[0] for call in mock_send.call_args_list]
        # QQQ (-2.5%) and AAPL (1.1%) are the two biggest absolute movers --
        # SPY (0.2%) should NOT have been sent.
        self.assertTrue(any("QQQ" in t for t in sent_texts))
        self.assertTrue(any("AAPL" in t for t in sent_texts))
        self.assertFalse(any("SPY" in t for t in sent_texts))

    @patch("src.triggers.market_snapshot_telegram.telegram_client.send_channel_message", return_value=True)
    @patch("src.triggers.market_snapshot_telegram.twelvedata.get_quotes_batch")
    def test_message_includes_emoji_price_and_seasonal_note(self, mock_batch, mock_send):
        mock_batch.return_value = {"SPY": {"price": 123.45, "percent_change": 2.0}}
        ctx = _make_ctx(
            thresholds_cfg={"symbols": ["SPY"], "max_posts_per_run": 2},
            seasonality_cfg={"months": {"7": "July note."}, "weekdays": {"Thursday": "Thu note."}},
        )

        mst.run(ctx)

        text = mock_send.call_args[0][0]
        self.assertIn("🟢", text)
        self.assertIn("SPY", text)
        self.assertIn("123.45", text)
        self.assertIn("July note.", text)
        self.assertIn("Thu note.", text)

    @patch("src.triggers.market_snapshot_telegram.telegram_client.send_channel_message", return_value=True)
    @patch("src.triggers.market_snapshot_telegram.twelvedata.get_quotes_batch")
    def test_premarket_gets_session_label(self, mock_batch, mock_send):
        mock_batch.return_value = {"SPY": {"price": 500.0, "percent_change": 1.5}}
        ctx = _make_ctx(
            thresholds_cfg={"symbols": ["SPY"], "max_posts_per_run": 2},
            now=datetime(2026, 7, 23, 11, 0, tzinfo=timezone.utc),  # 07:00 EDT -- pre-market
        )
        mst.run(ctx)
        self.assertIn("Pre-market", mock_send.call_args[0][0])

    @patch("src.triggers.market_snapshot_telegram.telegram_client.send_channel_message", return_value=True)
    @patch("src.triggers.market_snapshot_telegram.twelvedata.get_quotes_batch")
    def test_regular_session_gets_no_label(self, mock_batch, mock_send):
        mock_batch.return_value = {"SPY": {"price": 500.0, "percent_change": 1.5}}
        ctx = _make_ctx(thresholds_cfg={"symbols": ["SPY"], "max_posts_per_run": 2})
        mst.run(ctx)
        text = mock_send.call_args[0][0]
        self.assertNotIn("Pre-market", text)
        self.assertNotIn("After-hours", text)

    @patch("src.triggers.market_snapshot_telegram.telegram_client.send_channel_message")
    @patch("src.triggers.market_snapshot_telegram.twelvedata.get_quotes_batch")
    def test_skips_entirely_on_weekend(self, mock_batch, mock_send):
        ctx = _make_ctx(now=datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc))  # a Saturday
        fired = mst.run(ctx)
        self.assertFalse(fired)
        mock_batch.assert_not_called()
        mock_send.assert_not_called()

    @patch("src.triggers.market_snapshot_telegram.telegram_client.send_channel_message", return_value=True)
    @patch("src.triggers.market_snapshot_telegram.twelvedata.get_quotes_batch")
    def test_missing_symbol_in_batch_result_is_skipped(self, mock_batch, mock_send):
        # get_quotes_batch never raises -- a symbol whose chunk failed is
        # simply absent from the returned dict, not an exception to catch.
        mock_batch.return_value = {"QQQ": {"price": 500.0, "percent_change": 1.5}}
        ctx = _make_ctx(thresholds_cfg={"symbols": ["SPY", "QQQ"], "max_posts_per_run": 2})

        fired = mst.run(ctx)

        self.assertTrue(fired)
        mock_send.assert_called_once()
        self.assertIn("QQQ", mock_send.call_args[0][0])

    @patch("src.triggers.market_snapshot_telegram.telegram_client.send_channel_message")
    @patch("src.triggers.market_snapshot_telegram.twelvedata.get_quotes_batch", return_value={})
    def test_returns_false_when_no_quotes_available(self, mock_batch, mock_send):
        ctx = _make_ctx()
        fired = mst.run(ctx)
        self.assertFalse(fired)
        mock_send.assert_not_called()

    @patch("src.triggers.market_snapshot_telegram.telegram_client.send_channel_message", return_value=True)
    @patch("src.triggers.market_snapshot_telegram.twelvedata.get_quotes_batch")
    def test_never_touches_x_or_budget(self, mock_batch, mock_send):
        mock_batch.return_value = {"SPY": {"price": 500.0, "percent_change": 1.5}}
        ctx = _make_ctx()
        mst.run(ctx)
        ctx.x.post.assert_not_called()
        ctx.x.reply.assert_not_called()
        ctx.budget.record_spend.assert_not_called()

    @patch("src.triggers.market_snapshot_telegram.telegram_client.send_channel_message", return_value=True)
    @patch("src.triggers.market_snapshot_telegram.twelvedata.get_quotes_batch")
    def test_falls_back_to_stocks_broad_when_no_symbols_configured(self, mock_batch, mock_send):
        mock_batch.return_value = {"NVDA": {"price": 900.0, "percent_change": 3.0}}
        ctx = _make_ctx(
            thresholds_cfg={"max_posts_per_run": 2},  # no "symbols" override
            watchlist={"stocks_broad": [{"symbol": "NVDA"}, {"symbol": "TSLA"}], "stocks": [{"symbol": "SPY"}]},
        )

        mst.run(ctx)

        mock_batch.assert_called_once_with(["NVDA", "TSLA"])

    @patch("src.triggers.market_snapshot_telegram.telegram_client.send_channel_message", return_value=True)
    @patch("src.triggers.market_snapshot_telegram.twelvedata.get_quotes_batch")
    def test_consecutive_runs_do_not_repeat_the_same_template_for_a_symbol(self, mock_batch, mock_send):
        mock_batch.return_value = {"SPY": {"price": 500.0, "percent_change": 1.5}}
        state = {}
        ctx1 = _make_ctx(thresholds_cfg={"symbols": ["SPY"], "max_posts_per_run": 2}, state=state)
        mst.run(ctx1)
        first_text = mock_send.call_args[0][0]

        mock_send.reset_mock()
        ctx2 = _make_ctx(thresholds_cfg={"symbols": ["SPY"], "max_posts_per_run": 2}, state=state)
        mst.run(ctx2)
        second_text = mock_send.call_args[0][0]

        self.assertNotEqual(first_text, second_text)


if __name__ == "__main__":
    unittest.main()
