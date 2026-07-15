# TickerWatch

A fully automated crypto/stock X (Twitter) bot that runs on GitHub Actions'
free tier. No manual approval step in the posting pipeline — you edit config
files by hand occasionally, the pipeline does the rest.

## ⚠️ Read this first: the "free tier" landscape changed in 2026

The original design assumed several free APIs that are no longer usable as of
mid-2026. This build adapts to what's actually free/available today. Full
detail on each swap is in the source code comments; short version:

| Original plan | Status (2026) | What this repo uses instead |
|---|---|---|
| X API Free tier, 500 posts/month, $0 | **Discontinued for new developers** (Feb 2026 cutover). New accounts get pay-per-use: **$0.015/post, $0.20/post-with-a-link, $0.005/read**, no monthly minimum. | Pay-per-use, with a hard monthly `$` (or post-count, if you have a legacy account) budget cap built into the pipeline — see [Cost math](#cost-math-and-the-budget-cap) below. |
| Whale Alert API (free tier) | Programmatic API is **paid-only** ($15+/mo). Free tier is web/app viewing only. | **blockchain.info** (BTC, keyless, free) + **Etherscan** (ETH, free API key). No wallet/exchange attribution available for free, so posts report the real on-chain amount + a tx link instead of guessing "exchange to wallet" labels — never fabricated. |
| CryptoPanic free API | **Discontinued April 1, 2026.** | Public RSS feeds (CoinDesk, CoinTelegraph, Decrypt, The Block, CryptoSlate, CNBC, MarketWatch) — free indefinitely, no key, no ToS restriction. |
| NewsAPI.org free tier | Technically free but **contractually forbids production use** (localhost-only, no commercial use) — using it here would violate their ToS. | Same RSS approach as above. |
| Alpha Vantage (considered) | Free tier cut to **25 requests/day** — too low to check a watchlist multiple times a day. | **Twelve Data** free tier: 800 requests/day, 8/min. |

None of this breaks the "100% automated, free-tier-first" goal — the X API
cost is a few dollars a month at these volumes (see below), and every other
data source is genuinely free.

## What it posts

Every run, in strict priority order (higher priority always gets first claim
on the monthly budget):

1. **Whale/on-chain alerts** — large BTC (blockchain.info) / ETH (Etherscan) transfers.
   Siren count in the post scales with size (1 🚨 at the minimum threshold, up
   to 10 at $200M+). The first alert per asset per run also includes a
   same-asset market context line (🟢/🔴 price + 24h change) reusing data
   already fetched for the run, so it's free and the alert doesn't read as
   just a bare number. Capped at `thresholds.whale.max_alerts_per_run`
   **per chain per run** (BTC and ETH each have their own independent
   counter), so a busy run can't turn into a wall of alerts for one chain.
   No coin logo/media on the X post -- tried and pulled back, looked bad.
   The tx hash/explorer link is also gone from the X post, but the Telegram
   channel copy still gets the real block-explorer link (Telegram is free),
   same pattern as news alerts' source URL.
2. **"JUST IN" news** — RSS + keyword/source filter, paraphrased, always sourced.
   The main post names the outlet only (no link, e.g. "via CoinDesk"), with
   the real source URL in a follow-up reply -- X's algorithm has suppressed
   reach on linked posts hard since March 2026, so this keeps the main post's
   reach intact. Unlike whale alerts this doesn't save money (the link still
   costs $0.20 wherever it lands, so it's ~$0.215/post total now) -- it's a
   reach optimization, not a cost one. Capped at `keywords.max_articles_per_day`
   (default 2), the main cost lever since news is the only post type with a
   real clickable link anywhere in the thread.
3. **Price threshold/milestone alerts** — CoinGecko (crypto) + Twelve Data (stocks/ETFs)
4. **Scheduled daily post** — market snapshot / Fear & Greed Index (rotates, or both)
5. **Historical flashback** — filler, max once/day, only if nothing else fired
6. **Polls** — ~1x/week engagement mechanic
7. **Self-reply updates** — replies to the bot's *own* tweets only, never others'
8. **Filler** — absolute last resort, only posts if nothing above did this run.
   Picks from `config/filler.json`'s ~100 generic engagement questions/facts
   (no repeats until the list is exhausted, then reshuffles). This is what
   keeps the account posting roughly once/hour even on quiet news/market
   days — see the cost note below before raising check frequency further.

Plus a separate **retweet pipeline** (disabled by default — see below), a
**content-drafts pipeline** that never touches X at all: it sends
ready-to-post draft ideas (crypto/stock moves, matching news) to your private
Telegram chat for you to review, refine, and post yourself, a handful a day
(see "Content drafts" below) — and **AI Manager**, an opt-in fully autonomous
pipeline (disabled unless `ANTHROPIC_API_KEY` is set) where one Claude call,
~5-10 times/day, decides whether to publish an original post, which candidate
posts from `config/reply_targets.json`'s accounts are worth replying to, AND
which of those same candidates are worth reposting — either a plain retweet
or a quote-tweet with Claude's own short take added — no manual approval step
(see "AI Manager" below). It supersedes two older opt-in/mechanical
pipelines: **comment-engagement** (disabled by default — same
`reply_targets.json` pool, but AI Manager decides *whether* a reply is
worth sending rather than sending one whenever the cap allows it) and
**retweets.py** (disabled by default — that trigger retweeted every new post
from every monitored account unconditionally with zero judgment; AI Manager
now decides retweet vs. quote-tweet vs. skip per candidate instead).

Every crypto ticker mentioned anywhere (whale alerts, price alerts, snapshot,
flashback, self-replies, polls) uses a `$` cashtag (`$BTC`, `$ETH`, ...)
rather than plain text, so X renders its dynamic cashtag chart wherever the
coin comes up. Stock tickers (AAPL, SPY, QQQ) stay plain text.

Every post type that shows a price/%/index change uses the same 🟢/🔴 dot
convention (`src/formatting.py`'s `dot_for_change`), so the visual language
stays consistent across whale alerts, the snapshot, Fear & Greed, price
alerts, flashback, and self-replies rather than each post type inventing
its own look.

Each post type is its own function in `src/triggers/`, toggled independently
in the `ENABLED` dict at the top of `src/main.py`.

### News trend-line images

No post type attaches a coin logo — that was tried for whale and price
alerts and pulled back in both cases (didn't look good in practice). The one
remaining media attachment is on news alerts: a small themed red/green/gray
trend-line graphic (`assets/trend_up.png` / `trend_down.png` /
`trend_neutral.png`, pre-generated and checked into the repo — no network
call needed to attach one) matching the "chart snippet + terse JUST IN line"
look other crypto news accounts use. Which one gets attached comes from the
same Claude call already used to paraphrase the headline (it also tags the
story bullish/bearish/neutral); the mechanical-fallback path (no
`ANTHROPIC_API_KEY`) has no sentiment signal, so no image gets attached then.

This is gated by `config/media.json`'s `"enabled"` flag so it can be
switched off instantly (a config push, no code change) if a live billing
check ever shows X charging extra for posts with media attached — the same
kind of test that confirmed cashtags were free (isolate one trigger, cap it
to one post, check the X credits balance before/after).

**On the liquidation-stat post style specifically** (e.g. Watcher.Guru's
"$100,000,000 worth of crypto shorts liquidated in the past 60 minutes"):
there's no free way to source that data honestly on our hourly-cron
architecture. The only genuinely free feed is Binance's public
`!forceOrder@arr` WebSocket stream — real-time, no key — but it's a
persistent connection, not a REST endpoint; a batch job that connects for a
few seconds once an hour would only catch whatever liquidations happen to
fire in that narrow window, and reporting that as "in the past 60 minutes"
would be a fabricated claim about data we never actually collected. The
aggregators that do publish a clean rolling "$X liquidated in the last hour"
number (CoinGlass, CoinAPI) are paid APIs — CoinGlass's cheapest plan is
$29/month, no free tier at all. Doing this properly would mean a second,
long-running GitHub Actions job that keeps the WebSocket open for most of
each hour and accumulates real totals — a materially bigger, different piece
of infrastructure than anything else in this repo (all of which is
short batch runs). Worth building if you want it, but it's a separate
project, not a quick add — let me know if you want to go ahead with it.

### Retweets (disabled by default — superseded by AI Manager)

`src/triggers/retweets.py` watches `config/accounts.json`'s monitored
accounts and, for each one, retweets only its single newest post per run —
unconditionally, with zero judgment about whether the post is actually worth
amplifying. This was useful early on as a low-risk engagement lever (a plain
retweet has no reply-audience anti-spam check the way API replies do), but
it doesn't fit this project's direction of having Claude make every
engagement decision.

**Set to `False` in `main.py`'s `ENABLED` dict** now that AI Manager folds
reposting into its own judgment (retweet vs. quote-tweet vs. skip, see below)
over the same kind of candidate pool. Code is left intact — flip `retweets`
back to `True` if you want the old mechanical behavior running alongside AI
Manager.

### Comment engagement (disabled by default — superseded by AI Manager)

`config/reply_targets.json` lists specific accounts TickerWatch will reply
under (not just retweet), each with a hard `times_per_day` cap. An entry is
inert until `"enabled": true` — `user_id` is optional and auto-resolves from
the handle on first use (one read call, then cached in state so it's never
looked up again), so adding an account is just adding its handle and
flipping `enabled` to `true`, no manual lookup step required. The shipped
default has one entry (`WatcherGuru`) with `enabled: true` but a blank
`user_id`, which will auto-resolve the first time it runs.

Reply text is always freshly written by Claude from the target tweet's own
content (`src/sources/reply_writer.py`) — never a generic "Great post!" —
and requires `ANTHROPIC_API_KEY`; without it, this trigger just skips
(there's no safe mechanical fallback for a reply the way there is for news
paraphrasing, since a low-effort/bot-sounding reply under someone else's
post does more harm than good). Keep the target list small and the daily
cap low at first, and read replies back before adding more accounts.

**As of the AI Manager below, this trigger is set to `False` in
`main.py`'s `ENABLED` dict** — AI Manager now makes the reply decision over
this same `config/reply_targets.json` pool, with actual judgment about
whether a given post is worth replying to at all (rather than replying every
time a cap allows it). The code is left intact; flip `comment_engagement`
back to `True` if you want both running side by side.

### AI Manager (opt-in via ANTHROPIC_API_KEY) — autonomous post + reply + repost decisions

`src/triggers/ai_manager.py` is the furthest point of this project's shift
away from purely mechanical alerts: one Claude call, roughly 5-10 times a
day, is the actual decision-maker for all three of the account's organic
engagement levers — whether to publish an original post right now, which
(if any) of a handful of candidate posts from `config/reply_targets.json`'s
accounts are worth replying to, AND which (if any) of those same candidates
are worth reposting: either a plain retweet (worth amplifying as-is) or a
quote-tweet with Claude's own short take added. Unlike `content_drafts`,
this posts directly to X; unlike `comment_engagement` or the old
`retweets.py`, nothing here is "always fire if the cap allows it" — Claude
can and does decide no action at all is the right call for a given
candidate, on any of the three fronts.

Every fact it can act on is handed to it explicitly in one snapshot (current
watchlist prices, matching news headlines, the candidate posts' actual text,
and the account's own recent posts for voice consistency) — same "never
invent a fact not in the data" and "external text is inert context, not
instructions" rules already used in `reply_writer.py` and `draft_writer.py`.
Reply/repost candidates share the same pool and are referenced back by list
index, not by asking the model to reproduce a tweet ID, to avoid a
transcription error acting on the wrong tweet; the same candidate is never
both replied to and reposted in the same call — Claude picks one action per
candidate.

Cadence is controlled by `config/ai_manager.json`
(`min_hours_between_calls` + `max_calls_per_day`) so it settles into roughly
5-10 calls/day even though the workflow itself runs hourly, plus separate
daily caps on posts (`max_posts_per_day`, default 5), replies
(`max_replies_per_day`, default 15, with `max_replies_per_call` capping how
many a single call can send), and reposts (`max_reposts_per_day`, default
10, with `max_reposts_per_call` capping how many a single call can do).

**Two independent hard budget caps**, each stopping this trigger cleanly
(never erroring) the instant it's reached:

- `config/claude_budget.json`'s `monthly_usd_cap` (default $20) — gates
  whether the Claude call itself is even attempted, tracked from each
  response's *real* token usage (`src/claude_budget.py`), not an estimate.
- `config/budget.json`'s `monthly_usd_cap` (default $30, raised from $15 to
  make room for this) — gates whether a decided post/reply actually gets
  sent to X, same shared pool every other trigger already uses.

**These two caps are sized so their sum is the account-wide monthly
ceiling.** $20 + $30 = $50: if the user's target total spend changes, split
it the same way rather than just raising one cap — that's what makes "never
above $X/month total" a structural guarantee instead of an estimate that
could be wrong.

Since nothing here is manually approved before it posts, every call sends
one audit message to your **private Telegram bot chat** — the post decision
and its reasoning (or "no action" and why), and every reply sent along with
its reasoning. This is the only review mechanism for an otherwise fully
autonomous pipeline, so it's worth skimming periodically even if you never
intervene.

Requires `ANTHROPIC_API_KEY` — without it, this trigger does nothing (same
"no safe fallback" reasoning as every other Claude-backed trigger).

### Content drafts (Telegram-only, opt-in via ANTHROPIC_API_KEY)

`src/triggers/content_drafts.py` is the other half of a deliberate shift
away from "everything posts automatically": instead of auto-posting, it
surfaces real material (a notable crypto or stock/ETF move, a matching news
article) and has Claude draft a short, ready-to-post take on it — then sends
that draft to your **private Telegram bot chat only**. It never touches X.
You read it, refine it (add your own opinion, fix the tone), and post it
yourself whenever you like.

```
📝 Draft idea (crypto):

BTC just broke $65K again -- worth watching if it holds through the weekend.
```

News drafts also get the real source URL appended after the text — since
posting is manual here, there's no X-style cost/reach reason to hold it
back, unlike the main auto-posted pipeline:

```
📝 Draft idea (news):

Mizuho's note on Circle's bank approval is a reminder regulatory clarity
doesn't equal an instant stablecoin growth story.

https://www.coindesk.com/business/2026/07/13/mizuho-says-circle-bank-approval...
```

Capped at `thresholds.content_drafts.max_drafts_per_day` (8) combined across
both pools (price moves + news), `max_drafts_per_run` (2) per hourly check,
and a per-symbol cooldown (`min_hours_between_repeat_drafts`) so the same
asset doesn't get drafted again right away. News articles here use their own
dedup list, separate from `news_alerts`' — the same article can legitimately
be both auto-posted by `news_alerts` *and* separately drafted here, since
they serve different purposes (automatic vs. a manually-refined take).

Requires `ANTHROPIC_API_KEY` — like comment-engagement's replies, there's no
safe mechanical fallback for "curated insight" text, so this trigger simply
does nothing without it.

### Reply suggestions (Telegram-only)

`src/triggers/reply_suggestions.py` is a stopgap for manual replying while
X API replies stay blocked by the anti-spam/reputation gate (see AI Manager's
notes above): every run it checks the same `config/reply_targets.json`
account pool AI Manager already considers, ranks candidates by real
engagement (likes + retweets), and sends the top few (`max_per_run`, default
3) to your **private Telegram bot chat only** as a direct `x.com/.../status/...`
link plus a text snippet — tap the link, X opens straight to that post, write
your own reply from there.

A tweet is only ever suggested once (tracked in state) and is skipped if AI
Manager already replied to or reposted it. Free, mechanical, no
`ANTHROPIC_API_KEY` needed, never touches X — doesn't affect `filler`'s
"anything fired this run" check any more than `content_drafts` does.

## Run frequency and cron schedule

The workflow has **no in-repo `schedule:` trigger** — GitHub's own scheduler
is documented to be unreliable in 2026 (delayed or dropped runs, worse right
at the top of the hour, when every repo on the platform tends to schedule).
Instead, an external free cron service ([cron-job.org](https://cron-job.org))
calls the workflow's `workflow_dispatch` REST endpoint on a schedule:

1. Create a GitHub **fine-grained personal access token**: Settings →
   Developer settings → Personal access tokens → Fine-grained tokens →
   scope it to **only this repo**, with **Actions: Read and write** permission
   (nothing else needed).
2. In cron-job.org, create a job: `POST` to
   `https://api.github.com/repos/<owner>/<repo>/actions/workflows/tickerwatch.yml/dispatches`,
   headers `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`,
   `Content-Type: application/json`, body `{"ref":"main"}`, schedule hourly.
3. A successful call returns `204 No Content`; check the repo's Actions tab
   to confirm a run actually started.

Hourly → **24 runs/day → ~730 runs/month**. Note that higher check
frequency mostly buys faster alert latency and better whale-scan coverage,
**not** proportionally higher cost: scheduled daily/flashback/polls are
capped by date regardless of check frequency, and whale/news/price alerts
are capped by real-world event rate and per-asset cooldowns, not by how
often the workflow runs. The budget cap and `thresholds.json` cooldowns are
what actually control spend — see below.

Two dispatches landing within the same run's duration (e.g. while testing
with a 1-minute interval) can race on the final "commit updated state" step,
since `actions/checkout` pins the branch snapshot from when the run was
*queued*, not a live pull. That step retries with a fetch + rebase a few
times before giving up; a still-unresolved conflict just means that one
run's bookkeeping update doesn't persist (safe — never a corrupted push to
main), which is harmless as long as dispatches stay roughly an hour apart.

## Cost math and the budget cap

Because the literal "500 free posts/month" cap no longer exists for new X
developers, `config/budget.json` implements a **hard monthly spending/post
cap enforced by the pipeline itself** — this is the actual safety net, not
the numbers below. Once the cap is hit, the pipeline stops posting for the
rest of the month, throttling from the *bottom* of the priority list up
(retweets and polls get cut before whale/news alerts do).

**Original estimate vs. observed reality:** the first-day estimate below
assumed ~8 news posts/month, but the RSS feeds turned out to be far more
active than that — real usage hit 15 news posts in under a day before the
per-day cap existed. That's why `keywords.max_articles_per_day` (default 2)
exists: it's the single biggest lever on cost, since news is the only post
type where a real clickable link exists anywhere in the thread.

| Post type | ~posts/month | Link? | Cost |
|---|---|---|---|
| Whale alerts (capped 1/chain/run, cashtag only) | ~12 | no (see below) | $0.18 |
| News (capped at 2/day, main + source-link reply, ~$0.215/article) | up to ~120 (60 articles) | reply only | up to $12.90 |
| Price alerts | ~20 | no | $0.30 |
| Scheduled daily | ~30 | no | $0.45 |
| Flashback | ~8 | no | $0.12 |
| Polls | ~4 | no | $0.06 |
| Self-reply | ~15 | no | $0.23 |
| **Real-content subtotal** | | | **~$1.34 - $14.24/month**, depending on how often news actually matches |

Whale alerts use the asset as a `$` cashtag ($BTC/$ETH) rather than plain
text or a hashtag-only mention — confirmed via a live billing test that this
does **not** trigger the $0.20 link surcharge (X's Smart Cashtags are a
distinct in-app entity, never an external URL). X also caps posts at one
cashtag each (403 Forbidden otherwise) — worth knowing if you ever add
another asset mention to this post type.
News still needs a real clickable source link somewhere (never reproduce
article text verbatim, always cite a real source) — but that link lives in
a reply instead of the main post, so
the main post's reach isn't hit by X's link-suppression algorithm. This
doesn't reduce cost the way whale alerts did (the link still costs $0.20
wherever it lands), which is exactly why the per-day cap matters more here.

Watch the Telegram per-post/daily notifications for the first week or two to
see where your real news volume lands, and adjust `max_articles_per_day`
(or `monthly_usd_cap`) accordingly.

**Filler adds on top of the real-content range above.** With hourly checks
and `filler.max_per_day` at 24 (i.e. "fill every empty hour"), most hours
have no real content, so filler ends up posting roughly 600-650 times/month
— **~$9-9.75/month on its own**. Combined with real content, monthly total
could range from ~$10.50 (quiet news) up toward or past the cap (active
news + heavy filler). If a busy month does push past the cap, the budget
guard just does its job: it stops posting non-critical content for the rest
of the month rather than overspending — you'd see this as the account going
quiet plus the 90% Telegram alert firing well before it happens. Two levers
if you want more headroom before that point:

- lower `filler.max_per_day` in `config/thresholds.json` (e.g. to 10-12,
  roughly "fill every other empty hour"), or
- raise `monthly_usd_cap` in `config/budget.json` further.

Enabling 2-3 moderately active retweet accounts adds roughly 60-150 more
actions/month (~$1-2) on top of the total above.

### Two-budget design: X API + Claude API summing to one account-wide ceiling

Once AI Manager is in the mix, total spend is the sum of **two independent
caps**, each enforced by its own budget object that stops that half of the
pipeline cleanly the instant it's hit:

| Budget | File | Default cap | Covers |
|---|---|---|---|
| X API | `config/budget.json` | $30/month | Every actual X post/reply from every trigger (whale/news/price/daily/flashback/polls/self-reply/retweets/AI Manager's posts+replies) |
| Claude API | `config/claude_budget.json` | $20/month | Every AI Manager Claude call, billed on real token usage (`src/claude_budget.py`), not an estimate |

**These are sized so they sum to the account-wide monthly ceiling** — the
combined total this project targets is **$50/month**. If you want to change
that ceiling, split the new number across both caps rather than raising just
one; because each budget stops independently at its own cap, the sum is a
structural guarantee (worst case: both caps get fully used, total spend is
exactly the sum, never more), not just a hopeful estimate. At AI Manager's
default cadence (~8 calls/day, `claude-sonnet-5`), realistic Claude spend
works out to roughly $14-18/month against the $20 cap — the daily recap
(`budget_report.py`) now reports both caps' usage every night at 9pm
Brussels time, so drift either direction shows up quickly.

Note: `claude-sonnet-5`'s introductory pricing ($2/$10 per 1M input/output
tokens) runs through 2026-08-31, after which it reverts to $3/$15 — a ~50%
increase in AI Manager's Claude cost, still comfortably inside the $20 cap
at this call volume, but worth knowing about in advance.

**Theoretical worst case** (literally every trigger fires on every single run,
at the current hourly cadence, ~730 runs/month): whale + news + all 7
watchlist assets alerting every run comes out to **~15,000 posts/month and
~$650/month** — news's mandatory link dominates this number. This is not a
realistic scenario — it's exactly what the hard budget cap in `budget.json`
exists to prevent. Regardless of the cap's exact value, the bot simply stops
posting non-critical content once it's spent, no matter how noisy the
underlying data gets.

If you still have a **legacy X free-tier account** (created before Feb 2026,
not yet migrated to pay-per-use), set `"mode": "posts"` and
`"monthly_post_cap": 480` in `config/budget.json` instead — same mechanism,
counting posts instead of dollars.

## Repo structure

```
config/           watchlist.json, keywords.json, accounts.json, reply_targets.json,
                   thresholds.json, budget.json, claude_budget.json, ai_manager.json,
                   media.json
state/state.json  dedup/budget state, committed back to the repo after every run
src/
  main.py         orchestrator — priority pipeline, per-trigger error isolation
  context.py      shared per-run objects passed to every trigger
  budget.py       monthly X API $/post cap enforcement
  claude_budget.py  monthly Claude API $ cap enforcement, billed on real token usage
  ops_alerts.py   "something is broken" Telegram safety net for X/Claude API failures
  x_client.py     tweepy wrapper (post/reply/retweet/poll/media upload), DRY_RUN support
  media.py        news trend-icon -> X media_id helper
  formatting.py   number/text formatting, thread splitting
  sources/        one file per external API (coingecko, twelvedata, whale_btc,
                  whale_eth, news_rss, paraphrase, reply_writer, draft_writer,
                  ai_manager_brain, feargreed)
  triggers/       one file per post type (whale_alerts, news_alerts,
                  price_alerts, scheduled_daily, historical_flashback, polls,
                  self_reply, filler, retweets, comment_engagement,
                  content_drafts, ai_manager, budget_report)
  telegram_client.py  bot-chat + channel message senders, free, independent of the X budget
.github/workflows/tickerwatch.yml   cron schedule + secret wiring + state commit
```

## Getting API keys and adding GitHub secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**
in this repo, and add:

| Secret name | Where to get it | Required? |
|---|---|---|
| `X_API_KEY`, `X_API_SECRET` | [developer.x.com](https://developer.x.com) → create a Project + App → "Keys and tokens" → Consumer Keys. Set App permissions to **Read and Write** *before* generating access tokens. | Required |
| `X_ACCESS_TOKEN`, `X_ACCESS_SECRET` | Same App → "Keys and tokens" → Access Token and Secret (generate *after* setting Read+Write permission) | Required |
| `TWELVEDATA_API_KEY` | [twelvedata.com](https://twelvedata.com) → free signup → dashboard API key | Required (for stock/ETF prices) |
| `ETHERSCAN_API_KEY` | [etherscan.io/apis](https://etherscan.io/apis) → free signup → create API key | Required (for ETH whale alerts) |
| `COINGECKO_API_KEY` | [coingecko.com/en/api/pricing](https://www.coingecko.com/en/api/pricing) → free Demo plan (no card) | Optional — improves rate limits, code falls back to keyless public API without it |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) | Optional — enables real LLM paraphrasing of news headlines (Claude Haiku, a fraction of a cent/call), and is *required* for AI Manager's autonomous post/reply decisions, content-drafts' draft text, and (if re-enabled) comment-engagement's reply text — none of those have a safe generic fallback. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | See [Telegram notifications](#telegram-notifications) below | Optional — enables budget notifications in your private bot chat |
| `TELEGRAM_CHANNEL_ID` | See [Telegram notifications](#telegram-notifications) below | Optional — enables the public-ish channel that mirrors every post |

No key needed for: blockchain.info (BTC whale data), alternative.me (Fear &
Greed Index), or the RSS feeds.

## Telegram notifications

Two separate destinations, kept deliberately apart:

- **Your private bot chat** (`TELEGRAM_CHAT_ID`) — technical messages only:
  a short confirmation after every post and the daily/low-budget reports.
  No post content here, just budget bookkeeping.
- **A Telegram channel** (`TELEGRAM_CHANNEL_ID`) — a full mirror of every
  post that actually fires. Since Telegram is free, the channel copy can be
  *more generous* than the X post itself: it always includes the news
  article's source URL, regardless of whether the X-side reply carrying
  that same link ended up firing.

**Bot chat, per-post** — sent right after every single successful post/reply/retweet:

```
✅ X post created — $6.30/$30.00
```

**Bot chat, daily recap** — sent once/day at **9pm Europe/Brussels time**
(handles the CET/CEST switch automatically), now covering both budgets:

```
📅 Daily recap
X API: $6.30/$30.00 (21% used)
Claude API: $4.10/$20.00 (21% used)
```

**Bot chat, low-budget alert** — a one-time nudge the moment month-to-date
spend crosses 90% of either cap (won't repeat again until next month), with
a direct link to add credits for the X side:

```
⚠️ TickerWatch budget alert: $27.24/$30.00 used (91%) this month.
Add credits: https://console.x.com/ (Billing -> Credits)
```

```
⚠️ TickerWatch Claude API budget alert: $18.00/$20.00 used (90%) this month.
```

**Bot chat, AI Manager audit** — sent after every AI Manager call, roughly
5-10 times/day, so an otherwise fully autonomous decision is still visible:

```
🤖 AI Manager decision:

📝 Post (posted): BTC holding steady above 65k while volume thins out into
the weekend.
Reasoning: notable but not extreme move, worth a low-key observation

💬 Reply to @WatcherGuru (sent): Worth noting volume is down 18% vs last
week even as price holds.
Reasoning: adds a concrete data point the original post didn't mention

🔁 Quote-tweet of @saylor (sent): This is the kind of accumulation pace that
actually moves the supply/demand math, not just headlines.
Reasoning: genuinely notable number, worth adding independent context to
```

**Bot chat, outright API failure** — distinct from the budget alerts above
(which fire when spend is fine but approaching a cap): `src/ops_alerts.py`
fires when an X or Claude API call itself fails outright (bad/expired
credentials, an outage, a rate limit, exhausted account-side credits) — the
kind of failure every call site in this codebase already catches internally
and just skips/returns `None` for, so it would otherwise be silent. At most
one of each per run, even if the same broken dependency is hit repeatedly
in one run (e.g. several triggers all failing to post):

```
⚠️ TickerWatch: an X API call failed (post: 401 Unauthorized).
Check: https://console.x.com/
```

```
⚠️ TickerWatch: a Claude API call failed (ai_manager: authentication_error).
Check: https://console.anthropic.com/
```

**Channel, every post** — same text as what went to X, plus the restored
link where one exists, e.g. for news:

```
🚨 JUST IN: Mizuho says Circle bank approval doesn't change stablecoin outlook
(via CoinDesk)
Source: https://www.coindesk.com/policy/...
```

### Setup: bot chat (technical messages)

1. In Telegram, message **@BotFather** → `/newbot` → follow the prompts →
   it gives you a token like `123456789:AAH...` → this is `TELEGRAM_BOT_TOKEN`.
2. Send your new bot any message first (bots can't message you until you've
   messaged them), then get your chat ID: message **@userinfobot** (or
   **@get_id_bot**) and it'll reply with your numeric ID → this is
   `TELEGRAM_CHAT_ID`.
3. Add both as GitHub secrets (see the table above).

### Setup: public channel (full post mirror)

1. In the Telegram app: **New Channel** (the pencil/compose icon → New
   Channel), give it a name (e.g. "TickerWatch Feed"), and choose Public or
   Private — either works, Public just gets you a shareable `t.me/...` link
   if you ever want to let others follow along.
2. Open the channel → **Administrators** → **Add Admin** → search for your
   bot by its @username (the one you created with BotFather) → add it, no
   special admin rights beyond "Post Messages" are needed.
3. Get the channel's chat ID:
   - If it's a **public** channel: the chat ID is just `@yourchannelusername`
     (with the `@`) — you can use that directly as `TELEGRAM_CHANNEL_ID`,
     no numeric ID needed.
   - If it's a **private** channel: post any message in it, then forward
     that message to **@userinfobot** (or **@get_id_bot**) — it'll show the
     channel's numeric ID (a negative number starting with `-100`). Use that
     as `TELEGRAM_CHANNEL_ID`.
4. Add `TELEGRAM_CHANNEL_ID` as a GitHub secret (see the table above).

Without `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` set, the bot-chat messages
just log "not configured" and skip. Without `TELEGRAM_CHANNEL_ID` set, same
thing for the channel mirror — either can be set independently, and neither
ever blocks or breaks the rest of the run.

## Editing config files

- **`config/watchlist.json`** — crypto (needs a valid [CoinGecko id](https://api.coingecko.com/api/v3/coins/list)) and stock/ETF tickers (must be a symbol Twelve Data recognizes). `snapshot_order` controls what appears in the daily market snapshot.
- **`config/keywords.json`** — `keywords` (case-insensitive substring match against RSS title+summary), `rss_feeds` (only feeds with `"whitelisted": true` are checked; add/remove feeds freely, but broken feed URLs are just logged and skipped, never crash the run), and `max_articles_per_day` (hard daily cap on the only post type that still carries a link — this is the main cost lever).
- **`config/accounts.json`** — accounts `retweets.py` would auto-retweet if re-enabled (disabled by default, see [Retweets](#retweets-disabled-by-default--superseded-by-ai-manager)). `user_id` is optional and auto-resolves from `handle` on first use. Set `"enabled": true` to activate an account.
- **`config/reply_targets.json`** — accounts to *comment or repost* under, now used as the shared candidate pool for AI Manager's reply AND repost decisions (see [AI Manager](#ai-manager-opt-in-via-anthropic_api_key--autonomous-post--reply--repost-decisions)) and, if re-enabled, comment-engagement. Just add a `handle` and set `enabled: true` — `user_id` auto-resolves on first use, no manual lookup needed. Plus a `times_per_day` hard cap per account.
- **`config/thresholds.json`** — whale minimums (and `max_alerts_per_day`), price % trigger, milestone price levels per symbol, poll day/asset, self-reply timing window, daily-post rotation, `filler.max_per_day` (how many empty-hour fillers/day at most), and `content_drafts` (Telegram-only draft cadence/cooldowns).
- **`config/filler.json`** — the ~100 generic engagement prompts/facts used as the last-resort safety net. Add/remove freely; just keep entries factual or purely rhetorical (no specific prices/dates, since those need to trace to a real live source).
- **`config/budget.json`** — the monthly X API cap (see [Cost math](#cost-math-and-the-budget-cap)).
- **`config/claude_budget.json`** — the monthly Claude API cap, sized alongside `budget.json`'s to sum to the account-wide ceiling (see [Cost math](#cost-math-and-the-budget-cap)).
- **`config/ai_manager.json`** — AI Manager's model, call cadence, and post/reply/repost daily caps (see [AI Manager](#ai-manager-opt-in-via-anthropic_api_key--autonomous-post--reply--repost-decisions)).
- **`config/media.json`** — the on/off switch for attaching the news trend-icon (see [News trend-line images](#news-trend-line-images)).

## Testing locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export DRY_RUN=1              # never calls the X API, just logs what it would post
export TWELVEDATA_API_KEY=... # real key needed even in dry run, since price data is real
export ETHERSCAN_API_KEY=...
# COINGECKO_API_KEY / ANTHROPIC_API_KEY / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID optional

python -m src.main
```

With `DRY_RUN=1`, `XClient` never imports `tweepy` or touches the network for
X — it just logs the exact text of every post/reply/retweet/poll it *would*
have sent. Run it a few times with different `state/state.json` contents (or
delete it to simulate a first-ever run) to sanity-check each trigger. You can
also flip individual entries in `ENABLED` (top of `src/main.py`) to `False` to
isolate one trigger at a time.

To test against the real X API without spending on writes, you'd need a
sandboxed/dev environment — X doesn't offer one on pay-per-use, so the first
real run *is* a real, billed post. Keep the budget cap low (e.g. `$1`) for
your first few live runs.

## Safely increasing run frequency later

Once the bot is generating engagement/revenue you want to reinvest, increasing
frequency is a two-line change:

1. Edit the cron in `.github/workflows/tickerwatch.yml` (e.g. `0 */1 * * *` for hourly → ~730 runs/month).
2. Raise `monthly_usd_cap` (or `monthly_post_cap`) in `config/budget.json` to match what you're willing to spend — the pipeline's priority ordering means quality (whale/news) never degrades, only the *volume* of lower-priority filler/retweets scales up.

Nothing else needs to change — dedup state, per-source rate limits (Twelve
Data 8/min, CoinGecko, Etherscan 5 req/s), and error isolation all still hold
at higher frequency. Just watch Twelve Data's 800/day cap if you go beyond
roughly hourly with a large watchlist.
