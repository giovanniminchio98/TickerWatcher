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

1. **Whale/on-chain alerts (paused by default)** — large BTC (blockchain.info)
   / ETH (Etherscan) transfers. Paused because a mechanical, no-context
   alert (occasionally firing back-to-back with nothing tying them
   together) doesn't fit the "constant quality, meaningful posts" bar the
   account is now held to — AI Manager already covers genuinely notable
   market moves with real explanation attached. Code kept intact (flip
   `whale_alerts` back to `True` in `main.py`'s `ENABLED` to resume); the
   rest of this description is how it behaves if re-enabled. Siren count in
   the post scales with size (1 🚨 at the minimum threshold, up to 10 at
   $200M+). The first alert per asset per run also includes a same-asset
   market context line (🟢/🔴 price + 24h change) reusing data already
   fetched for the run, so it's free and the alert doesn't read as just a
   bare number. Capped at `thresholds.whale.max_alerts_per_run` **per chain
   per run** (BTC and ETH each have their own independent counter), so a
   busy run can't turn into a wall of alerts for one chain. No coin
   logo/media on the X post -- tried and pulled back, looked bad. The tx
   hash/explorer link is also gone from the X post, but the Telegram
   channel copy still gets the real block-explorer link (Telegram is
   free), same pattern as news alerts' source URL.
2. **"JUST IN" news** (disabled, 2026-07-21) — RSS + keyword/source filter,
   paraphrased, always sourced. The rest of this description is how it
   behaves if `news_alerts` is flipped back to `True` in `main.py`'s
   `ENABLED`. No link, ever, on X (2026-07-20) — the main post names the
   outlet only (e.g. "via CoinDesk"); the real source URL only ever shows up
   in the free Telegram channel mirror. What used to be a link-reply is now
   a mandatory plain-language explanation reply instead ("what this means,"
   same pattern as AI Manager's `second_part`). Capped at
   `keywords.max_articles_per_day` (default 2). Disabled because it fires on
   every hourly run with no checkpoint gate, so it kept posting its old
   wire-alert format at any hour — including overnight — clashing with AI
   Manager's owl persona; AI Manager now covers crypto/finance/AI news as a
   secondary input, in the same voice, when genuinely notable.
3. **Price threshold/milestone alerts** (disabled, 2026-07-21) —
   CoinGecko (crypto) + Twelve Data (stocks/ETFs). Disabled as the same
   "mechanical, no-context, off-persona" issue as `news_alerts` above — AI
   Manager already covers genuinely notable price moves with real
   explanation attached. Code kept intact — flip `price_alerts` back to
   `True` to resume.
4. **CryptoScope Oracle verdict alerts** (disabled by default, 2026-07-19 — see
   "CryptoScope Oracle" below) — a quant signal composite (Monte-Carlo
   forecast + technical signals, ported from the crypto-scope site's engine),
   recomputed fresh every run from live Kraken candles for every coin in
   `watchlist.crypto` — fires only on a genuinely strong, high-confidence
   Strongly Bullish/Bearish read
5. **Scheduled daily post** — market snapshot only (Fear & Greed Index disabled
   by default, 2026-07-19 — `scheduled_daily.feargreed_enabled` in
   `config/thresholds.json`)
6. **Historical flashback** (disabled, 2026-07-21) — filler, max once/day,
   only if nothing else fired. Same off-persona reasoning as above (a bare
   "price N years ago vs today" callout with no explanation). Code kept
   intact — flip `historical_flashback` back to `True` to resume.
7. **Polls** (disabled by default, 2026-07-19) — ~1x/week engagement mechanic
8. **Self-reply updates** — replies to the bot's *own* tweets only, never others'
9. **Filler** (disabled by default — see "Filler" below) — absolute last
   resort, only posts if nothing above did this run. Picks from
   `config/filler.json`'s ~100 generic engagement questions/facts (no
   repeats until the list is exhausted, then reshuffles).

Plus a separate **retweet pipeline** (disabled by default — see below), a
**content-drafts pipeline** that never touches X at all: it sends
ready-to-post draft ideas (crypto/stock moves, matching news) to your private
Telegram chat for you to review, refine, and post yourself, a handful a day
(see "Content drafts" below) — **AI Manager**, an opt-in fully autonomous
pipeline (disabled unless `ANTHROPIC_API_KEY` is set) where a Claude call,
roughly 6-7 times/day, decides a *batch* of up to 3 original posts each
time (queued and drained one per subsequent run, so total output is
10-14 posts/day from far fewer calls) and which candidate posts from
`config/reply_targets.json`'s bigger accounts are worth reposting — no
manual approval step (see "AI Manager" below). A second, faster-cadence
trigger (**Reply Manager**) was
built to handle replies automatically, but is disabled by default — X's
reply restriction turned out to hit every account regardless of size, so
automated replies aren't viable right now (see "Reply Manager" below);
manual replying via "Reply suggestions"' Telegram digest is the reply path
instead. This supersedes three older opt-in/mechanical pipelines:
**comment-engagement** (disabled by default — same restriction applies),
**retweets.py** (disabled by default — AI Manager now decides retweet vs.
quote-tweet vs. skip per candidate instead of retweeting everything), and
**filler.py** (disabled by default — AI Manager's own post decision absorbs
its role as an optional, quality-gated fallback).

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

### CryptoScope Oracle (quant signal alerts + AI Manager context)

`crypto-scope` (a separate static-site repo) has a client-side "Oracle"
engine (`oracle.js`) that turns a candle series into a full quant read: a
Monte-Carlo GBM forecast, Hurst exponent regime detection, risk stats
(VaR/CVaR/Sharpe/Sortino), distribution stats (skew/kurtosis/autocorrelation),
and a fused, weighted 0-100 "verdict" composite with a confidence score.
`src/sources/cryptoscope_oracle.py` is a straight Python port of that same
math — same functions, same weights, same signal set — so TickerWatch can
use it as shared per-run Context (`ctx.oracle`) instead of only having plain
spot prices to react to.

Unlike the crypto-scope site itself (a once-a-day static data bundle,
refreshed by its own separate GitHub Action), the Oracle here is recomputed
**fresh every TickerWatch run** — every hourly cron tick, not once a day —
from `src/sources/binance.py`'s keyless klines endpoint (1h candles, 200-bar
lookback, no API key or rate-limit headache), for every coin in
`watchlist.crypto`. `main.py`'s `_fetch_oracle_data` runs this once per run
(same per-symbol error isolation as every other data source: one bad/missing
Binance pair is logged and skipped, never breaks the others) and hands the
result to every trigger via `ctx.oracle[symbol]`.

Two things consume it:
- **`src/triggers/oracle_alerts.py`** — a new, deliberately conservative post
  type: it only fires when a coin's composite verdict reaches a genuinely
  strong reading (Strongly Bullish/Bearish) *and* the model's own signals
  agree enough to clear `thresholds.oracle.min_confidence`. Deduped per coin
  so re-alerting the same still-true verdict every hour is never noise (see
  `thresholds.oracle.min_hours_between_alerts`) — a repeat only fires once
  the read has actually changed.
- **`ai_manager_brain.py`'s prompt** — every coin's current verdict/
  confidence/regime/probability read is handed to Claude as a
  `QUANT ORACLE` section alongside prices and news, explicitly framed as a
  real (not fabricated) statistical read it may reference when relevant,
  never as financial advice or a guarantee.

This only ever adds a real, computed number to the pipeline — it never
changes what coins are tracked or how often prices are fetched; just add
more entries to `watchlist.crypto` and the Oracle (and its alerts) picks
them up automatically. If a coin's Binance pair ticker differs from
`f"{symbol}USDT"` (the default guess), set `"binance_symbol"` explicitly on
that watchlist entry.

### Oracle image attachments

CryptoScope Oracle posts (`src/triggers/oracle_alerts.py`) are the one
exception to the "no coin logos" rule below — every post attaches up to 2
images via `src/oracle_media.py`: the coin's own logo
(`assets/oracle/{btc,eth,sol,xrp}.*`, user-supplied) plus a green "up" or
red "down" trend chart (`assets/oracle/trend_{up,down}.jpeg`), picked from
whether the verdict is Bullish or Bearish. Gated independently by
`config/media.json`'s `"oracle_enabled"` flag (separate from every other
media gate in that file) so it can be switched off without touching news
alerts' own trend-icon feature below.

### News trend-line images

No other post type attaches a coin logo — that was tried for whale and price
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

### Filler (disabled by default — absorbed into AI Manager)

`src/triggers/filler.py` used to be the account's "never go quiet" safety
net: if nothing higher-priority posted this run, it mechanically picked one
of `config/filler.json`'s ~100 generic engagement prompts and posted it
unconditionally, no judgment at all about whether it was actually worth
posting right now.

**Set to `False` in `main.py`'s `ENABLED` dict** — AI Manager's own post
decision absorbs this role now, but as an optional, quality-gated fallback
rather than a mechanical one: a handful of `filler.json`'s examples are
handed to Claude purely as style reference (never posted verbatim), and it
may write something in that spirit only if it's genuinely good. Posting
nothing is explicitly the preferred outcome over posting mediocre filler —
see the AI Manager prompt below. Code is left intact; flip `filler` back to
`True` if account growth stalls and you'd rather trade quality for
guaranteed hourly posting volume again.

### AI Manager (opt-in via ANTHROPIC_API_KEY) — 4x/day world-news recap

`src/triggers/ai_manager.py` is the account's main content engine, rebuilt
(2026-07-20) around a much narrower, higher-bar design than what it used to
be: **four fixed posts a day (02:00 / 06:00 / 12:00 / 21:00 Europe/Brussels)**,
each one a single genuine "take" on the most important things that
happened since the last recap — not a stream of individual crypto/price
posts. The account owner's own honest read on the old, higher-frequency
design was that they wouldn't reliably follow most of what it posted; this
is a deliberate trade of volume for a real quality bar. **Every post must
still be genuinely useful and explained in plain, easy-to-follow language —
never a bare headline, never noise.**

**World news is the primary lens, not crypto.** `config/world_news.json`
lists general-interest outlets — The Guardian, BBC, Deutsche Welle, France
24, Euronews, plus non-English sources (la Repubblica, Corriere della Sera,
Le Monde, El País, Der Spiegel) whose headlines Claude translates inline
while writing the recap, no separate translation call needed. Unlike the
keyword-gated finance feeds, these are pulled unconditionally
(`news_rss.fetch_latest_articles` — the latest few items per feed, no
keyword filter) since "what's the latest important news" doesn't fit a
finance/crypto keyword whitelist the way a JUST IN alert does; Claude
itself judges what's genuinely important. Prices, the CryptoScope Oracle,
and the existing keyword-matched crypto/finance/AI news (`config/keywords.json`)
are still in the snapshot, but explicitly demoted to secondary material —
folded into a recap only when genuinely notable, never just because a
price moved.

**Up to 4 posts per call, no queue.** Each Claude call decides a batch of
0 to `max_posts_per_call` (config/ai_manager.json, default 4) posts — a
broad snapshot of the most important things since the last checkpoint, not
forced into a single post. A busy period can genuinely warrant several
distinct posts (each with its own topic and its own `second_part`); a quiet
one can just as correctly warrant zero. No two posts in the same batch may
cover the same story. Every accepted post fires immediately, one after
another, in that same run — there's still no queue to spread things across
the day, since the 4 fixed checkpoints already are the schedule. The
external cron dispatch (see "Scheduling" below) stays exactly as it is —
still hourly — the internal checkpoint gate (`_CALL_CHECKPOINT_HOURS`) is
what turns that into "only acts 4x/day," so
no cron-job.org changes are needed. The 02:00 checkpoint was added
2026-07-21 to cover the overnight gap once news_alerts, price_alerts, and
historical_flashback — the only things still posting overnight, in their
old off-persona, no-context formats — were disabled (see `ENABLED` in
`src/main.py`).

**Post shape.** Written in Mark's own genuine first-person voice (2026-07-21)
— a real reaction to something he just read, told the way you'd tell a
friend or colleague, not a sterile wire-alert headline. Every post opens
with `🌍 WORLD:` directly on the same line as a varied, never-repeated
reaction (`"🌍 WORLD: I just read that..."`, `"🌍 WORLD: Okay, this is
big:"`, `"🌍 WORLD: Wait, this actually happened:"`, etc.), leading
straight into the actual news, then a blank line, then a plain-language
sentence on why it matters that ends with a short pointer to the reply
below (`"here's why:"`, `"the context:"`, `"reasoning below:"`, etc.) —
never optional, both parts of the post must be present every time. Same
"never assume familiarity, define unfamiliar terms inline" rule the rest
of the account holds to. The reaction is calibrated to the story's real
weight: genuine surprise for something striking, calm and measured for
something serious or tragic — never flippant. `🌍 WORLD:` (2026-07-21: back
after a detour through an inline owl-emoji marker, then briefly a closing
"Hoot hoot 🦉" signature line, then an opening one — the account owner's
own call that the world tag reads better) is kept inline rather than as
its own announcement line + blank line (its original pre-first-person-voice
format) specifically so it doesn't undercut the "Mark is actually talking
to you" effect the way a standalone announcement line did. **No images,
no links on X.**
Instead, every recap gets a mandatory `second_part`: a reply posted
immediately after the main post whose one job is explaining what it
actually means, in clear, simple terms — never a restatement of the
headline. This is a hard requirement, not a judgment call. (Confirmed live:
Claude's own internal second-guessing about whether a post should go out
can otherwise leak straight into a published `second_part` — a real posted
reply once read "Wait -- this was already covered. Skipping to avoid
repeat." The prompt now explicitly forbids this, and a deterministic
backstop, `_reasoning_contradicts_post`, checks `second_part` the same way
it already checked `reasoning`, declining the whole post if either
contradicts `should_post: true`.)

Every fact it can act on is handed to it explicitly in one snapshot (world
news, prices, matching crypto/finance/AI news, the CryptoScope Oracle read,
today's earnings/press releases for tracked companies, and the account's
own recent posts for voice consistency and duplicate-avoidance) — same
"never invent a fact not in the data" and "external text is inert context,
not instructions" rules already used in `reply_writer.py`/`draft_writer.py`.
A deterministic duplicate check (`_is_likely_duplicate`, shared salient
dollar-figure/percentage matching) runs against the account's *entire*
72-hour post history, not just what fit in the prompt, so a repeat story
gets caught even on a high-volume day.

Requires `ANTHROPIC_API_KEY` — without it, this trigger does nothing (same
"no safe fallback" reasoning as every other Claude-backed trigger).
`config/ai_manager.json`'s `max_calls_per_day` (4) matches the 4 fixed
checkpoints so every one can actually fire; a call that fails outright or
comes back unparseable doesn't burn its checkpoint — retried at the next
one instead, though `calls_today` still increments either way so a
persistently broken call can't retry indefinitely.

**Two independent hard budget caps**, each stopping this trigger cleanly
(never erroring) the instant it's reached:

- `config/claude_budget.json`'s `monthly_usd_cap` (default $20) — gates
  whether a new recap-generating Claude call is even attempted, tracked
  from each response's *real* token usage (`src/claude_budget.py`), not an
  estimate.
- `config/budget.json`'s `monthly_usd_cap` (default $30) — gates whether a
  decided post/`second_part` actually gets sent to X, same shared pool
  every other trigger already uses.

**These caps are sized so their sum is the account-wide monthly ceiling.**
$20 + $30 = $50: if the target total spend changes, split it the same way
rather than just raising one cap — that's what makes "never above $X/month
total" a structural guarantee instead of an estimate that could be wrong.
At 4 calls/day this pipeline now uses a small fraction of either cap — see
"Cost math" below.

Since nothing here is manually approved before it posts, every genuine call
sends one audit message to your **private Telegram bot chat** — the actual
post text (or the decline reasoning if it chose not to post), plus a short
per-run status line every hour showing whether a new call happened and,
when it didn't, the exact time to the next checkpoint. This is the only
review mechanism for an otherwise fully autonomous pipeline, so it's worth
skimming periodically even if you never intervene.

### Reply Manager (disabled by default — X's reply restriction isn't a per-account setting)

`src/triggers/reply_manager.py` was built to run replies on a much faster
cadence (roughly hourly) than AI Manager's slow 4x/day rhythm,
scoped to accounts marked `reply_only: true` in `config/reply_targets.json`
— smaller/mid accounts added specifically on the theory that X's "you must
be mentioned or otherwise engaged by the author" reply restriction was a
per-account setting bigger accounts commonly enable and smaller ones don't.

**Confirmed live that theory was wrong.** The smaller `reply_only` accounts
hit the exact same 403 as the bigger ones — this is a blanket API
limitation, not something any choice of target account gets around. Since
automated replies can't succeed regardless of target, this trigger is
**disabled by default** (`main.py`'s `ENABLED` dict) — reposting
(retweet/quote) of the bigger accounts is unaffected and still works fine
via AI Manager, and manual replying (see "Reply suggestions" below) is now
the only reply path, for every account. Code is left intact in case the
restriction ever eases — flip `reply_manager` back to `True` to try again.

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

`src/triggers/reply_suggestions.py` is the only reply path now that Reply
Manager is disabled (see above — X's reply restriction turned out to hit
every account regardless of size, not just the bigger ones). Covers every
enabled account in `config/reply_targets.json`, big and small alike. Every
run it drops anything older than `max_age_hours` (default 6 — replying to
a stale post reads badly no matter how much engagement it got), ranks
what's left by real engagement (likes + retweets), and sends the top few
(`max_per_run`, default 3) to your **private Telegram bot chat only** as a
direct `x.com/.../status/...` link plus a text snippet — tap the link, X
opens straight to that post, write your own reply from there.

A tweet is only ever suggested once (tracked in state) and is skipped if AI
Manager already reposted it. Free, mechanical, no `ANTHROPIC_API_KEY`
needed, never touches X — doesn't affect `filler`'s "anything fired this
run" check any more than `content_drafts` does.

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

| Post type | Status | ~posts/month | Link? | Cost |
|---|---|---|---|---|
| Whale alerts (capped 1/chain/run, cashtag only) | disabled | ~12 if re-enabled | no (see below) | $0.18 if re-enabled |
| News (capped at 2/day, main + text-only explainer reply, no link ever) | on | up to ~120 (60 articles) | no | up to $1.80 |
| Price alerts | on | ~20 | no | $0.30 |
| Scheduled daily / Fear & Greed | disabled | 0 | no | $0 |
| Flashback | on | ~8 | no | $0.12 |
| Polls | disabled | 0 | no | $0 |
| Self-reply | on | ~15 | no | $0.23 |
| **Real-content subtotal** | | | | **~$0.65 - $2.45/month**, depending on how often news actually matches |

Whale alerts use the asset as a `$` cashtag ($BTC/$ETH) rather than plain
text or a hashtag-only mention — confirmed via a live billing test that this
does **not** trigger the $0.20 link surcharge (X's Smart Cashtags are a
distinct in-app entity, never an external URL). X also caps posts at one
cashtag each (403 Forbidden otherwise) — worth knowing if you ever add
another asset mention to this post type.
News no longer carries a link anywhere on X (2026-07-20) — the real source
URL only ever shows up in the free Telegram channel mirror. What used to be
a link-reply is now a plain-language explanation reply instead (no link
surcharge), which is what dropped this row's cost by roughly 7x.

Watch the Telegram per-post/daily notifications for the first week or two to
see where your real news volume lands, and adjust `max_articles_per_day`
(or `monthly_usd_cap`) accordingly.

**Filler is disabled by default** (see "Filler" above — AI Manager's own
post decision absorbs its role now, but only posts an evergreen/engagement
take when it's genuinely good, not mechanically every empty hour), so the
numbers below only apply if you re-enable it. **If re-enabled, filler adds
on top of the real-content range above:** with hourly checks and
`filler.max_per_day` at 24 (i.e. "fill every empty hour"), most hours have
no real content, so filler would post roughly 600-650 times/month — **~$9-
9.75/month on its own**. Combined with real content, monthly total could
range from ~$10.50 (quiet news) up toward or past the cap (active news +
heavy filler). If a busy month does push past the cap, the budget guard
just does its job: it stops posting non-critical content for the rest of
the month rather than overspending — you'd see this as the account going
quiet plus the 90% Telegram alert firing well before it happens. Two levers
if you want more headroom before that point (with filler re-enabled):

- lower `filler.max_per_day` in `config/thresholds.json` (e.g. to 10-12,
  roughly "fill every other empty hour"), or
- raise `monthly_usd_cap` in `config/budget.json` further.

Enabling 2-3 moderately active retweet accounts adds roughly 60-150 more
actions/month (~$1-2) on top of the total above.

**AI Manager's posts specifically — 4 calls/day, up to 4 posts each
(2026-07-20 redesign, 4th call added 2026-07-21 for overnight coverage).**
Every post is a plain, link-free tweet at the base $0.015 rate, plus its
mandatory `second_part` explainer reply, also $0.015 — so each post is
really 2 tweets. Theoretical worst case (every one of the 4 daily calls
maxes out at 4 posts):

4 calls × 4 posts × 2 tweets × $0.015 ≈ $0.48/day → **~$14.40/month worst case**

In practice, an empty batch (0 posts) is explicitly correct whenever
nothing clears the bar — especially at the 02:00 checkpoint, where most
nights should return zero — and there's no pressure to pad up toward the
max, so real usage should land well under that ceiling most days, likely
closer to 1-2 posts/call than 4. Either way it's bounded by the same $30 X
cap, with real headroom versus the worst case. Reposts (retweet/quote,
capped at 3/day) add on top of this at the same ~$0.015/action rate if
ever re-enabled (currently disabled — reposting is manual-only).

**Claude call cost** — 4 calls/day instead of the old ~6-7, each with a
larger prompt (world news added) and output sized for up to `max_posts_per_call`
full posts (same shape as the old per-call output, just fewer calls/day).
At Sonnet 5's full post-intro pricing ($3/$15 per 1M tokens), expect
roughly **~$7-13/month**, comfortably inside the $20 Claude cap — worth
keeping an eye on the Telegram budget recap for the first week or two to
confirm real token usage lands where expected.

### Two-budget design: X API + Claude API summing to one account-wide ceiling, plus a separate image budget

Total spend against the **$50/month structural ceiling** is the sum of
**two independent caps**, each enforced by its own budget object that stops
that half of the pipeline cleanly the instant it's hit:

| Budget | File | Default cap | Covers |
|---|---|---|---|
| X API | `config/budget.json` | $30/month | Every actual X post/repost from every trigger (whale/news/price/daily/flashback/polls/self-reply/retweets/AI Manager's posts+reposts; Reply Manager's replies too, if ever re-enabled) |
| Claude API | `config/claude_budget.json` | $20/month | Every AI Manager Claude call (and Reply Manager's, if ever re-enabled), billed on real token usage (`src/claude_budget.py`), not an estimate |

**These are sized so they sum to the account-wide monthly ceiling** — the
combined total this project targets is **$50/month**. If you want to change
that ceiling, split the new number across both caps rather than raising just
one; because each budget stops independently at its own cap, the sum is a
structural guarantee (worst case: both caps get fully used, total spend is
exactly the sum, never more), not just a hopeful estimate. The daily recap
(`budget_report.py`) reports all budgets' usage every night at 9pm Brussels
time to your **cost-tracking Telegram chat** (see below), so drift in any
direction shows up quickly.

**A third budget exists but is currently unused by AI Manager**: image
generation (`config/image_budget.json`, default $10/month cap,
`src/image_budget.py`) — DALL-E is a different provider (OpenAI) with its
own bill, sitting *outside* the $50 X+Claude structure. AI Manager doesn't
use images right now (see "AI Manager" above), so this stays at $0 unless
that changes later — the code (`src/sources/image_gen.py`) is untouched
and ready if it does.

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
config/           watchlist.json, keywords.json, world_news.json, accounts.json,
                   reply_targets.json, thresholds.json, budget.json, claude_budget.json,
                   ai_manager.json, media.json
state/state.json  dedup/budget state, committed back to the repo after every run
src/
  main.py         orchestrator — priority pipeline, per-trigger error isolation
  context.py      shared per-run objects passed to every trigger
  budget.py       monthly X API $/post cap enforcement
  claude_budget.py  monthly Claude API $ cap enforcement, billed on real token usage
  ops_alerts.py   "something is broken" Telegram safety net for X/Claude API failures
  x_client.py     tweepy wrapper (post/reply/retweet/poll/media upload), DRY_RUN support
  media.py        news trend-icon -> X media_id helper
  oracle_media.py oracle_alerts coin-logo + trend-chart -> X media_ids helper
  formatting.py   number/text formatting, thread splitting
  sources/        one file per external API (coingecko, binance, twelvedata,
                  whale_btc, whale_eth, news_rss, paraphrase, reply_writer,
                  draft_writer, ai_manager_brain, feargreed), plus
                  cryptoscope_oracle.py (Python port of crypto-scope's
                  oracle.js quant engine, see "CryptoScope Oracle" above)
  triggers/       one file per post type (whale_alerts, news_alerts,
                  price_alerts, oracle_alerts, scheduled_daily,
                  historical_flashback, polls, self_reply, filler, retweets,
                  comment_engagement, content_drafts, ai_manager,
                  reply_manager, budget_report)
  image_budget.py     third, independent budget cap for image generation (OpenAI/DALL-E) -- currently unused by AI Manager, kept ready if that changes
  telegram_client.py  bot-chat + channel + cost-chat message senders, free, independent of the X budget
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
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) | Optional — enables real LLM paraphrasing of news headlines (Claude Haiku, a fraction of a cent/call), and is *required* for AI Manager's autonomous decisions, content-drafts' draft text, and (if re-enabled) Reply Manager's or comment-engagement's reply text — none of those have a safe generic fallback. |
| `OPENAI_API_KEY` | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) → requires adding billing credit separately from your Anthropic/X accounts | Not currently used — AI Manager doesn't generate images right now (see "AI Manager"). The code (`src/sources/image_gen.py`) is untouched if this is revisited later. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | See [Telegram notifications](#telegram-notifications) below | Optional — enables operational notifications in your private bot chat |
| `TELEGRAM_CHANNEL_ID` | See [Telegram notifications](#telegram-notifications) below | Optional — enables the public-ish channel that mirrors every post |
| `TELEGRAM_COST_CHAT_ID` | See [Telegram notifications](#telegram-notifications) below | Optional — enables the dedicated cost-tracking chat; falls back to the bot chat if unset, so cost visibility never disappears |

No key needed for: blockchain.info (BTC whale data), alternative.me (Fear &
Greed Index), or the RSS feeds.

## Telegram notifications

Three separate destinations, kept deliberately apart:

- **Your private bot chat** (`TELEGRAM_CHAT_ID`) — operational messages
  only: the AI Manager / Reply Manager audit, reply suggestions, the
  quiet-run heartbeat, and outright API failure alerts. No dollar figures
  here anymore — see the cost chat below.
- **Your private cost-tracking chat** (`TELEGRAM_COST_CHAT_ID`) — every
  dollar figure lives here instead: the per-post budget confirmation, the
  daily recap, and low-budget/recharge-credits alerts across all three
  budgets (X, Claude, image generation). Falls back to the bot chat if
  unset, so cost visibility never silently disappears just because this
  wasn't configured yet.
- **A Telegram channel** (`TELEGRAM_CHANNEL_ID`) — a mirror of *original
  posts only* (whale/news/price/scheduled/flashback/polls/self-reply/AI
  Manager's own post decision). Since Telegram is free, the channel copy can
  be *more generous* than the X post itself: news alerts always include the
  article's source URL here regardless of whether the X-side reply carrying
  that same link ended up firing, and AI Manager's `second_part` (when a
  post has one) is folded straight into the same channel message rather
  than needing a second one. Replies and reposts (retweets/quote-tweets)
  never mirror here (`Budget.record_spend`'s `mirror_to_channel=False`) —
  the channel is meant to read as "everything this account itself wrote,"
  not a log of every engagement action. (`telegram_client.send_channel_photo`
  also exists for forwarding a real image, unused while AI Manager doesn't
  generate any — see "AI Manager" above.)

**Cost chat, per-post** — sent right after every single successful post/reply/retweet:

```
✅ X post created — $6.30/$30.00
```

**Cost chat, daily recap** — sent once/day at **9pm Europe/Brussels time**
(handles the CET/CEST switch automatically), now covering all three budgets:

```
📅 Daily recap
X API: $6.30/$30.00 (21% used)
Claude API: $4.10/$20.00 (21% used)
Image generation: $2.40/$10.00 (24% used)
```

**Cost chat, low-budget alert** — a one-time nudge the moment month-to-date
spend crosses 90% of any cap (won't repeat again until next month), with a
direct link to add credits for each provider:

```
⚠️ TickerWatch budget alert: $27.24/$30.00 used (91%) this month.
Add credits: https://console.x.com/ (Billing -> Credits)
```

```
⚠️ TickerWatch Claude API budget alert: $18.00/$20.00 used (90%) this month.
```

```
⚠️ TickerWatch image-generation budget alert: $9.10/$10.00 used (91%) this month.
Add credits: https://platform.openai.com/settings/organization/billing
```

**Bot chat, AI Manager audit** — sent after every AI Manager call, roughly
4-6 times/day, so an otherwise fully autonomous decision is still visible:

```
🤖 AI Manager decision:

📝 Post (posted): BTC is holding steady above 65k while volume thins out
into the weekend 📉📊 -- worth watching whether that quiet volume flips into
a real move once liquidity returns Monday.
Reasoning: notable but not extreme move, worth a low-key observation with a
clear takeaway

🔁 Quote-tweet of @saylor (sent): This is the kind of accumulation pace that
actually moves the supply/demand math, not just headlines.
Reasoning: genuinely notable number, worth adding independent context to
```

**Bot chat, Reply Manager audit** — only relevant if `reply_manager` is
re-enabled (disabled by default, see "Reply Manager" above); would be sent
whenever it actually sends a reply:

```
💬 Reply Manager decision:

Reply to @iamcryptowolfy (sent): Worth noting that's roughly 3x the usual
daily volume for that pair.
Reasoning: adds a concrete data point the original post didn't mention
```

**Bot chat, reply suggestions** — sent every run by `reply_suggestions.py`
whenever there are new candidates, the only reply path now (see "Reply
suggestions" above); tap a link to open straight to that post and reply
from the X app yourself:

```
💬 Reply candidates (biggest right now, tap to open + comment):

@WatcherGuru (1.2K likes / 340 RTs): $2.1B in crypto shorts liquidated in
the past 24 hours
https://x.com/WatcherGuru/status/1234567890
```

**Bot chat, quiet-run confirmation** — sent whenever nothing posted/replied/
reposted this run, so a genuinely quiet check never looks indistinguishable
from a silently broken one:

```
✅ TickerWatch check complete — no post/reply/repost this run (nothing
warranted it). Everything's running fine.
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

### Setup: bot chat (operational messages)

1. In Telegram, message **@BotFather** → `/newbot` → follow the prompts →
   it gives you a token like `123456789:AAH...` → this is `TELEGRAM_BOT_TOKEN`.
2. Send your new bot any message first (bots can't message you until you've
   messaged them), then get your chat ID: message **@userinfobot** (or
   **@get_id_bot**) and it'll reply with your numeric ID → this is
   `TELEGRAM_CHAT_ID`.
3. Add both as GitHub secrets (see the table above).

### Setup: cost-tracking chat (budget/recharge alerts)

1. Same idea as the bot chat above, but a **separate** Telegram chat so
   dollar figures don't get mixed in with operational messages — either a
   second private chat with the same bot, or a small private group with
   just you in it.
2. Message the bot (or add it to the group) first, then get the chat ID the
   same way: **@userinfobot** (or **@get_id_bot**) → for a group, forward
   any message from the group to that bot to get the group's numeric ID.
3. Add it as `TELEGRAM_COST_CHAT_ID` (see the table above). If you skip this
   entirely, cost messages just fall back to the regular bot chat — nothing
   breaks, you just don't get the separation.

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
- **`config/reply_targets.json`** — accounts to *repost or (manually) reply to*: non-`reply_only` accounts are AI Manager's repost candidate pool (see [AI Manager](#ai-manager-opt-in-via-anthropic_api_key--autonomous-post--repost-decisions)); `reply_only: true` marks accounts Reply Manager would have exclusively targeted before it was disabled (see [Reply Manager](#reply-manager-disabled-by-default--xs-reply-restriction-isnt-a-per-account-setting)) — kept as metadata in case that trigger is ever re-enabled. Every enabled account (both kinds) now shows up in "Reply suggestions" for manual replying. Just add a `handle` and set `enabled: true` — `user_id` auto-resolves on first use, no manual lookup needed. Plus a `times_per_day` hard cap per account.
- **`config/thresholds.json`** — whale minimums (and `max_alerts_per_day`), price % trigger, milestone price levels per symbol, poll day/asset, self-reply timing window, daily-post rotation, `filler.max_per_day` (only relevant if `filler` is re-enabled, see "Filler" above), and `content_drafts` (Telegram-only draft cadence/cooldowns).
- **`config/filler.json`** — the ~100 generic engagement prompts/facts. `filler.py` itself is disabled by default (see "Filler" above), but this file is still in active use: AI Manager samples a few entries each call as style reference for its own optional generic-post fallback. Add/remove freely; just keep entries factual or purely rhetorical (no specific prices/dates, since those need to trace to a real live source).
- **`config/budget.json`** — the monthly X API cap (see [Cost math](#cost-math-and-the-budget-cap)).
- **`config/claude_budget.json`** — the monthly Claude API cap, sized alongside `budget.json`'s to sum to the account-wide ceiling (see [Cost math](#cost-math-and-the-budget-cap)).
- **`config/image_budget.json`** — the monthly image-generation (DALL-E) cap, a separate provider/bill outside the X+Claude $50 structure (see [Cost math](#cost-math-and-the-budget-cap)).
- **`config/ai_manager.json`** — AI Manager's model and `max_calls_per_day` (4, matching the fixed 02:00/06:00/12:00/21:00 Brussels checkpoints) — see [AI Manager](#ai-manager-opt-in-via-anthropic_api_key--4xday-world-news-recap). Every recap's `second_part` explainer reply is mandatory (enforced in the prompt, not a config knob).
- **`config/world_news.json`** — general world-news RSS feeds (Guardian, BBC, DW, France 24, Euronews, plus non-English outlets translated inline) that feed AI Manager's recap as its primary input, separate from `config/keywords.json`'s finance/crypto feeds since these are pulled unconditionally, not keyword-gated.
- **`config/reply_manager.json`** — Reply Manager's model, call cadence, and daily reply cap — see [Reply Manager](#reply-manager-disabled-by-default--xs-reply-restriction-isnt-a-per-account-setting).
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
