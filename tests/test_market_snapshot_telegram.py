"""
Regression tests for market_snapshot_telegram.py (POC added 2026-07-23,
Telegram-only market snapshot while X posting stays off). No network,
stdlib unittest/unittest.mock against a fake Context.
"""
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.triggers import market_snapshot_telegram as mst


def _make_ctx(thresholds_cfg=None, seasonality_cfg=None, watchlist_stocks=None):
    ctx = MagicMock()
    ctx.config = {
        "thresholds": {
            "market_snapshot": thresholds_cfg
            or {"symbols": ["SPY", "QQQ", "AAPL"], "max_posts_per_run": 2}
        },
        "seasonality": seasonality_cfg if seasonality_cfg is not None else {},
        "watchlist": {"stocks": watchlist_stocks or [{"symbol": "SPY"}, {"symbol": "QQQ"}]},
    }
    ctx.now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)  # a Thursday
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
    @patch("src.triggers.market_snapshot_telegram.twelvedata.get_quote")
    def test_sends_biggest_movers_first_capped_at_max_posts(self, mock_get_quote, mock_send):
        quotes = {
            "SPY": {"price": 600.0, "percent_change": 0.2},
            "QQQ": {"price": 500.0, "percent_change": -2.5},
            "AAPL": {"price": 200.0, "percent_change": 1.1},
        }
        mock_get_quote.side_effect = lambda symbol: quotes[symbol]
        ctx = _make_ctx()

        fired = mst.run(ctx)

        self.assertTrue(fired)
        self.assertEqual(mock_send.call_count, 2)  # max_posts_per_run=2
        sent_texts = [call.args[0] for call in mock_send.call_args_list]
        # QQQ (-2.5%) and AAPL (1.1%) are the two biggest absolute movers --
        # SPY (0.2%) should NOT have been sent.
        self.assertTrue(any("QQQ" in t for t in sent_texts))
        self.assertTrue(any("AAPL" in t for t in sent_texts))
        self.assertFalse(any("SPY:" in t for t in sent_texts))

    @patch("src.triggers.market_snapshot_telegram.telegram_client.send_channel_message", return_value=True)
    @patch("src.triggers.market_snapshot_telegram.twelvedata.get_quote")
    def test_message_includes_emoji_price_and_seasonal_note(self, mock_get_quote, mock_send):
        mock_get_quote.return_value = {"price": 123.45, "percent_change": 2.0}
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
    @patch("src.triggers.market_snapshot_telegram.twelvedata.get_quote")
    def test_skips_symbol_on_fetch_failure(self, mock_get_quote, mock_send):
        def side_effect(symbol):
            if symbol == "SPY":
                raise Exception("boom")
            return {"price": 500.0, "percent_change": 1.5}

        mock_get_quote.side_effect = side_effect
        ctx = _make_ctx(thresholds_cfg={"symbols": ["SPY", "QQQ"], "max_posts_per_run": 2})

        fired = mst.run(ctx)

        self.assertTrue(fired)
        mock_send.assert_called_once()
        self.assertIn("QQQ", mock_send.call_args[0][0])

    @patch("src.triggers.market_snapshot_telegram.telegram_client.send_channel_message")
    @patch("src.triggers.market_snapshot_telegram.twelvedata.get_quote", return_value=None)
    def test_returns_false_when_no_quotes_available(self, mock_get_quote, mock_send):
        ctx = _make_ctx()
        fired = mst.run(ctx)
        self.assertFalse(fired)
        mock_send.assert_not_called()

    @patch("src.triggers.market_snapshot_telegram.telegram_client.send_channel_message", return_value=True)
    @patch("src.triggers.market_snapshot_telegram.twelvedata.get_quote")
    def test_never_touches_x_or_budget(self, mock_get_quote, mock_send):
        mock_get_quote.return_value = {"price": 500.0, "percent_change": 1.5}
        ctx = _make_ctx()
        mst.run(ctx)
        ctx.x.post.assert_not_called()
        ctx.x.reply.assert_not_called()
        ctx.budget.record_spend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
