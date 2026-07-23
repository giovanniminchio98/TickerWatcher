"""
Regression tests for src/sources/chart_gen.py (ai_manager's crypto price
charts, added 2026-07-23) -- no network, no real matplotlib display, just
the fetch/render/gate logic in isolation.
"""
import unittest
from unittest.mock import MagicMock, patch

from src.sources import chart_gen


class TestFetchPriceSeries(unittest.TestCase):
    @patch("src.sources.chart_gen.coingecko.get_market_chart")
    def test_returns_series_on_success(self, mock_market_chart):
        mock_market_chart.return_value = [[1700000000000, 50000.0], [1700003600000, 50500.5]]
        series = chart_gen.fetch_price_series("bitcoin", days=14)
        self.assertEqual(series, [(1700000000000, 50000.0), (1700003600000, 50500.5)])

    @patch("src.sources.chart_gen.coingecko.get_market_chart")
    def test_returns_none_on_empty_response(self, mock_market_chart):
        mock_market_chart.return_value = []
        self.assertIsNone(chart_gen.fetch_price_series("bitcoin"))

    @patch("src.sources.chart_gen.coingecko.get_market_chart", side_effect=Exception("boom"))
    def test_returns_none_on_exception(self, mock_market_chart):
        self.assertIsNone(chart_gen.fetch_price_series("bitcoin"))


class TestRenderPriceChart(unittest.TestCase):
    def test_returns_png_bytes_for_a_real_series(self):
        series = [(1700000000000 + i * 3600000, 50000.0 + i * 10) for i in range(20)]
        png_bytes = chart_gen.render_price_chart("BTC", series)
        self.assertIsInstance(png_bytes, bytes)
        self.assertGreater(len(png_bytes), 0)
        self.assertEqual(png_bytes[:8], b"\x89PNG\r\n\x1a\n")  # PNG magic number

    def test_returns_none_for_empty_series(self):
        self.assertIsNone(chart_gen.render_price_chart("BTC", []))

    def test_returns_none_on_render_exception(self):
        # A single-point series with a mocked pyplot that raises exercises
        # the try/except without needing to break matplotlib itself.
        with patch("matplotlib.pyplot.subplots", side_effect=Exception("boom")):
            self.assertIsNone(chart_gen.render_price_chart("BTC", [(1700000000000, 50000.0)]))


class TestGenerateChartForSymbol(unittest.TestCase):
    def _ctx(self, chart_enabled=True):
        ctx = MagicMock()
        ctx.config = {
            "media": {"ai_manager_chart_enabled": chart_enabled},
            "watchlist": {"crypto": [{"symbol": "BTC", "name": "Bitcoin", "coingecko_id": "bitcoin"}]},
        }
        return ctx

    def test_returns_none_when_gate_disabled(self):
        ctx = self._ctx(chart_enabled=False)
        self.assertIsNone(chart_gen.generate_chart_for_symbol(ctx, "BTC"))

    def test_returns_none_for_untracked_symbol(self):
        ctx = self._ctx()
        self.assertIsNone(chart_gen.generate_chart_for_symbol(ctx, "DOGE"))

    @patch("src.sources.chart_gen.render_price_chart", return_value=b"fake-png-bytes")
    @patch("src.sources.chart_gen.fetch_price_series", return_value=[(1700000000000, 50000.0)])
    def test_wires_symbol_to_coingecko_id(self, mock_fetch, mock_render):
        ctx = self._ctx()
        result = chart_gen.generate_chart_for_symbol(ctx, "BTC")
        mock_fetch.assert_called_once_with("bitcoin", days=14)
        self.assertEqual(result, b"fake-png-bytes")

    @patch("src.sources.chart_gen.fetch_price_series", return_value=None)
    def test_returns_none_when_series_fetch_fails(self, mock_fetch):
        ctx = self._ctx()
        self.assertIsNone(chart_gen.generate_chart_for_symbol(ctx, "BTC"))


if __name__ == "__main__":
    unittest.main()
