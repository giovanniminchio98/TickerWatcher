"""
Regression tests for ai_manager.py's v2 "financial intelligence" redesign
(2026-07-23) -- no tests/ directory existed before this, this codebase's
established pattern was DRY_RUN=1 + manual `python -m src.main` runs. These
use stdlib unittest/unittest.mock against a fake Context, no network calls.
"""
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.sources import ai_manager_brain
from src.triggers import ai_manager


def _make_ctx(config=None, state=None, now=None, budget_can_spend=True, claude_can_spend=True):
    ctx = MagicMock()
    ctx.config = config if config is not None else {}
    ctx.state = (
        state
        if state is not None
        else {
            "ai_manager": {"last_call_time": None, "last_call_checkpoint": None, "date": None, "calls_today": 0},
            "story_history": [],
        }
    )
    # 2026-07-23 04:00 UTC == 06:00 Europe/Brussels (CEST, UTC+2 in July) --
    # an aligned checkpoint hour (see ai_manager._CALL_CHECKPOINT_HOURS).
    ctx.now = now or datetime(2026, 7, 23, 4, 0, tzinfo=timezone.utc)
    ctx.budget = MagicMock()
    ctx.budget.can_spend.return_value = budget_can_spend
    ctx.claude_budget = MagicMock()
    ctx.claude_budget.can_spend.return_value = claude_can_spend
    ctx.x = MagicMock()
    ctx.prices = {}
    ctx.oracle = {}
    return ctx


class TestEnforceCategoryTag(unittest.TestCase):
    def test_known_category_gets_its_emoji(self):
        text = ai_manager._enforce_category_tag("Nvidia beats estimates", "Earnings")
        self.assertTrue(text.startswith(ai_manager_brain.CATEGORY_EMOJI["Earnings"] + " "))

    def test_unknown_category_falls_back_to_macro(self):
        text = ai_manager._enforce_category_tag("Something happened", "NotARealCategory")
        self.assertTrue(text.startswith(ai_manager_brain.CATEGORY_EMOJI["Macro"] + " "))

    def test_idempotent_when_already_tagged(self):
        tagged = ai_manager._enforce_category_tag("Hi there", "AI")
        self.assertEqual(ai_manager._enforce_category_tag(tagged, "AI"), tagged)


class TestAssembly(unittest.TestCase):
    def test_reply_card_always_ends_with_bottom_line(self):
        item = {"why_it_matters": ["a", "b"], "bottom_line": "This matters a lot."}
        card = ai_manager._assemble_reply_card(item)
        self.assertIn("🎯 Bottom line: This matters a lot.", card)

    def test_reply_card_includes_optional_fields_when_present(self):
        item = {
            "why_it_matters": ["a"],
            "tickers_bullish": ["NVDA"],
            "tickers_bearish": ["RIOT"],
            "impact_score": 8,
            "confidence": "High",
            "time_horizon": "3-12 months",
            "bottom_line": "Bottom.",
        }
        card = ai_manager._assemble_reply_card(item)
        self.assertIn("🐂 Bullish: $NVDA", card)
        self.assertIn("🐻 Bearish: $RIOT", card)
        self.assertIn("📊 Impact: 8/10", card)
        self.assertIn("🔍 Confidence: High", card)
        self.assertIn("⏳ Horizon: 3-12 months", card)

    def test_digest_tweet_format(self):
        item = {"category": "Crypto", "headline": "Big move", "why_it_matters": "Because reasons", "tickers": ["BTC"]}
        text = ai_manager._assemble_digest_tweet(item, 2, 5)
        self.assertTrue(text.startswith(f"{ai_manager_brain.CATEGORY_EMOJI['Crypto']} 2/5: Big move"))
        self.assertIn("$BTC", text)


class TestDuplicateDetection(unittest.TestCase):
    def test_is_likely_duplicate_catches_unit_normalized_repeat(self):
        # Confirmed live: the same underlying story slipped a naive check
        # because the original telling used spelled-out units and the
        # repeat used abbreviations.
        original = "Citadel Securities invests $400 million into Crypto.com, now valued at $20 billion"
        repeat = "Citadel's $400M bet on Crypto.com lifts its valuation to $20B"
        self.assertTrue(ai_manager._is_likely_duplicate(repeat, [original]))

    def test_is_likely_duplicate_ignores_unrelated_stories(self):
        a = "Fed holds rates steady amid 3.2% inflation reading"
        b = "Nvidia stock jumps after strong earnings beat"
        self.assertFalse(ai_manager._is_likely_duplicate(b, [a]))

    def test_is_same_story_title_catches_reworded_repeat(self):
        # Confirmed live: this exact class of story (personnel/political,
        # no shared number) is what got the previous design paused --
        # _is_likely_duplicate alone can't catch it.
        original = "Zelensky fires his army chief amid mounting battlefield pressure"
        repeat = "Ukraine's army chief removed by Zelensky as war pressure mounts"
        self.assertTrue(ai_manager._is_same_story_title(repeat, [original]))

    def test_is_same_story_title_ignores_unrelated_titles(self):
        a = "Zelensky fires his army chief amid mounting battlefield pressure"
        b = "France bans social media for under-15s nationwide"
        self.assertFalse(ai_manager._is_same_story_title(b, [a]))


class TestPostIndividualItem(unittest.TestCase):
    def _cfg(self):
        return {"individual_post_min_score": 75}

    def test_declines_low_score(self):
        ctx = _make_ctx()
        item = {"score": 50, "source_index": 0, "hook": "Something", "bottom_line": "Matters"}
        fired, detail, candidate = ai_manager._post_individual_item(
            ctx, item, [{"title": "T", "url": "u"}], [], [], self._cfg(), {"BTC"}
        )
        self.assertFalse(fired)
        self.assertEqual(detail, "score below individual-post bar")
        self.assertIsNone(candidate)

    def test_declines_invalid_source_index(self):
        ctx = _make_ctx()
        item = {"score": 90, "source_index": 5, "hook": "Something", "bottom_line": "Matters"}
        fired, detail, candidate = ai_manager._post_individual_item(
            ctx, item, [{"title": "T", "url": "u"}], [], [], self._cfg(), {"BTC"}
        )
        self.assertFalse(fired)
        self.assertEqual(detail, "invalid source_index")

    def test_posts_valid_item_and_records_history(self):
        ctx = _make_ctx()
        ctx.x.post.return_value = "tweet-1"
        ctx.x.reply.return_value = "reply-1"
        item = {
            "score": 90,
            "source_index": 0,
            "category": "AI",
            "hook": "OpenAI ships a new model",
            "bottom_line": "This changes the competitive picture.",
            "why_it_matters": ["Point one"],
            "chart_symbol": None,
        }
        candidates = [{"title": "OpenAI ships new model", "url": "https://example.com/a"}]
        fired, detail, candidate = ai_manager._post_individual_item(
            ctx, item, candidates, [], [], self._cfg(), {"BTC"}
        )
        self.assertTrue(fired)
        self.assertEqual(candidate, candidates[0])
        ctx.x.post.assert_called_once()
        ctx.x.reply.assert_called_once()

    def test_declines_when_bottom_line_missing(self):
        ctx = _make_ctx()
        item = {"score": 90, "source_index": 0, "hook": "Hook", "bottom_line": ""}
        fired, detail, candidate = ai_manager._post_individual_item(
            ctx, item, [{"title": "T", "url": "u"}], [], [], self._cfg(), {"BTC"}
        )
        self.assertFalse(fired)
        self.assertEqual(detail, "missing mandatory bottom_line")


class TestPostDigestThread(unittest.TestCase):
    def _items(self):
        return [
            {
                "raw": {"category": "Crypto", "headline": "H1", "why_it_matters": "W1", "tickers": []},
                "candidate": {"title": "T1", "url": "u1"},
                "score": 60,
            },
            {
                "raw": {"category": "AI", "headline": "H2", "why_it_matters": "W2", "tickers": []},
                "candidate": {"title": "T2", "url": "u2"},
                "score": 55,
            },
        ]

    def test_chains_replies_to_previous_tweet_id(self):
        ctx = _make_ctx()
        ctx.x.post.return_value = "intro-1"
        reply_ids = iter(["reply-1", "reply-2"])
        reply_calls = []

        def fake_reply(text, in_reply_to):
            reply_calls.append(in_reply_to)
            return next(reply_ids)

        ctx.x.reply.side_effect = fake_reply

        fired, texts = ai_manager._post_digest_thread(ctx, self._items(), "Intro line")

        self.assertTrue(fired)
        self.assertEqual(reply_calls, ["intro-1", "reply-1"])
        self.assertEqual(len(texts), 3)

    def test_stops_early_on_reply_failure_but_keeps_partial_thread(self):
        ctx = _make_ctx()
        ctx.x.post.return_value = "intro-1"
        ctx.x.reply.side_effect = [None]

        fired, texts = ai_manager._post_digest_thread(ctx, self._items(), "Intro")

        self.assertTrue(fired)  # the intro tweet itself went out
        self.assertEqual(len(texts), 1)

    def test_no_items_does_not_post_anything(self):
        ctx = _make_ctx()
        fired, texts = ai_manager._post_digest_thread(ctx, [], "Intro")
        self.assertFalse(fired)
        self.assertEqual(texts, [])
        ctx.x.post.assert_not_called()


class TestRunIntegration(unittest.TestCase):
    def _base_config(self):
        return {
            "ai_manager": {
                "model": "claude-sonnet-5",
                "max_calls_per_day": 4,
                "max_individual_posts_per_call": 3,
                "individual_post_min_score": 75,
                "digest_min_score": 45,
                "digest_min_items": 3,
                "digest_max_items": 8,
                "candidate_pool_size": 80,
                "candidate_pool_max_per_feed": 8,
            },
            "keywords": {"rss_feeds": [], "keywords": []},
            "watchlist": {"crypto": [{"symbol": "BTC", "coingecko_id": "bitcoin"}], "stocks_broad": []},
            "media": {"ai_manager_chart_enabled": False},
        }

    @patch("src.triggers.ai_manager.twelvedata.get_press_releases", return_value=[])
    @patch("src.triggers.ai_manager.twelvedata.get_earnings_calendar", return_value=[])
    @patch("src.triggers.ai_manager.twelvedata.get_quotes_batch", return_value={})
    @patch("src.triggers.ai_manager.news_rss.fetch_matching_articles")
    @patch("src.triggers.ai_manager.ai_manager_brain.decide")
    def test_posts_above_bar_declines_below_and_skips_thin_digest(
        self, mock_decide, mock_fetch, mock_quotes, mock_earnings, mock_press
    ):
        candidates = [
            {"title": "Story A", "summary": "Summary A", "url": "https://x.test/a", "source": "Test"},
            {"title": "Story B", "summary": "Summary B", "url": "https://x.test/b", "source": "Test"},
            {"title": "Story C", "summary": "Summary C", "url": "https://x.test/c", "source": "Test"},
        ]
        mock_fetch.return_value = candidates
        mock_decide.return_value = (
            {
                "posts": [
                    {
                        "category": "AI", "score": 90, "source_index": 0, "hook": "Hook A",
                        "bottom_line": "Bottom A", "why_it_matters": ["a"], "chart_symbol": None,
                        "reasoning": "clearly notable",
                    },
                    {
                        "category": "Macro", "score": 50, "source_index": 1, "hook": "Hook B",
                        "bottom_line": "Bottom B", "why_it_matters": ["b"], "chart_symbol": None,
                        "reasoning": "not notable enough",
                    },
                ],
                "digest": {
                    "should_post": True,
                    "intro": "More stories:",
                    "items": [
                        {
                            "category": "Markets", "score": 50, "source_index": 2, "headline": "Headline C",
                            "why_it_matters": "Because reasons", "tickers": [], "reasoning": "digest-worthy",
                        },
                    ],
                },
            },
            MagicMock(),
        )

        ctx = _make_ctx(config=self._base_config())
        ctx.x.post.return_value = "tweet-1"
        ctx.x.reply.return_value = "reply-1"

        fired = ai_manager.run(ctx)

        self.assertTrue(fired)
        self.assertEqual(ctx.state["ai_manager"]["calls_today"], 1)
        self.assertEqual(ctx.state["ai_manager"]["last_call_checkpoint"], "2026-07-23-06")
        # Only the >=75-scored post should go out. The digest candidate
        # (score 50) alone never reaches digest_min_items (3), so the
        # digest thread is skipped entirely -- one story_history entry
        # total, not two.
        self.assertEqual(len(ctx.state["story_history"]), 1)
        self.assertEqual(ctx.state["story_history"][0]["source_title"], "Story A")
        ctx.x.post.assert_called_once()

    @patch("src.triggers.ai_manager.twelvedata.get_press_releases", return_value=[])
    @patch("src.triggers.ai_manager.twelvedata.get_earnings_calendar", return_value=[])
    @patch("src.triggers.ai_manager.twelvedata.get_quotes_batch", return_value={})
    @patch("src.triggers.ai_manager.news_rss.fetch_matching_articles", return_value=[])
    def test_off_checkpoint_hour_no_ops(self, mock_fetch, mock_quotes, mock_earnings, mock_press):
        ctx = _make_ctx(
            config=self._base_config(),
            now=datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc),  # 10:00 Brussels -- not a checkpoint
        )
        fired = ai_manager.run(ctx)
        self.assertFalse(fired)
        self.assertEqual(ctx.state["ai_manager"]["calls_today"], 0)
        ctx.x.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
