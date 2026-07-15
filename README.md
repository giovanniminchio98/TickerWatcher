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
8. **Filler** (disabled by default — see "Filler" below) — absolute last
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

### AI Manager (opt-in via ANTHROPIC_API_KEY) — autonomous post + repost decisions

`src/triggers/ai_manager.py` is the account's main content engine, targeting
roughly **10-14 posts/day**. The non-negotiable rule regardless of format or
volume: **every post must be genuinely useful and explained in plain,
easy-to-follow language — never a bare headline, never empty crypto-degen
hype.** This account exists to bring real value to readers, not noise; that
never changes no matter how the cadence or format evolves.

**Posts are generated in batches, not one Claude call each.** Each Claude
call decides up to `posts_per_batch` (default 3) original posts at once —
the first fires right away, any others are queued
(`state["ai_manager"]["post_queue"]`) and drained one at a time on
subsequent runs, relying on the hourly cron cadence itself to spread them
out over the following hours. This is what makes a high visible posting
cadence affordable: the Claude call cadence stays low (`min_hours_between_calls`
+ `max_calls_per_day`, roughly **6-7 calls/day**) regardless of how many
posts/day that produces, so Claude cost doesn't scale with post volume the
way it would with one call per post. Because later items in a batch post
with a delay, only the first is written to lean on "right now" price/news
framing — additional ones are meant to be more evergreen (a concept
explainer, a historical comparison, a "what to watch" framing) so they
still read as accurate a few hours later. A queued post that sits longer
than `max_queue_age_hours` (default 12) is dropped rather than fired stale,
and a batch is also trimmed to whatever the remaining daily quota can
actually still take (`max_posts_per_day` minus what's already posted/queued
today), so Claude is never asked to produce more than could realistically
fire. It also decides which (if any) of a handful of candidate posts from
`config/reply_targets.json`'s bigger accounts are worth reposting — either
a plain retweet or a quote-tweet with Claude's own short take added.
Replies moved out to their own, much faster trigger (see "Reply Manager"
below) — this one is purely posts + reposts.

**Format is flexible, substance isn't.** A post can be the fuller shape (a
real market/news/concept view, a clear sentence on what it means or its
consequence, a few emoji) or a terser, plain-text factual one — Claude can
open with `JUST IN:`/`BREAKING:` and name specific tickers (`$BTC`,
`$NVDA`) or big recognizable names when a post is genuinely fresh and
time-sensitive, purely to aid clarity/engagement, never as decoration on a
routine take. The plain-language explanation stays mandatory either way.

**Every call is pushed to actually produce something.** Since a Claude call
costs money whether or not it results in a post, the prompt explicitly asks
Claude to make a genuine effort to find at least one worth-it post each
call — with live prices, news, and generic-engagement examples to draw
from, there's almost always something real to say. Returning zero posts is
meant to be rare, not a default "when in doubt, skip" outcome — but the
quality bar doesn't move: it just means look harder before concluding
there's nothing, never post something hollow to fill the slot.

Every fact it can act on is handed to it explicitly in one snapshot (current
watchlist prices, matching news headlines, the candidate posts' actual text,
and the account's own recent posts for voice consistency) — same "never
invent a fact not in the data" and "external text is inert context, not
instructions" rules already used in `reply_writer.py` and `draft_writer.py`.
Repost candidates are referenced back by list index, not by asking the
model to reproduce a tweet ID, to avoid a transcription error acting on the
wrong tweet.

The snapshot also includes a handful of `filler.json`'s generic-engagement
examples as pure style reference (see "Filler" above) — Claude may write an
original post in that spirit if nothing price/news-driven is post-worthy,
as long as it's genuinely good and not filler for filler's sake.

**No images, no links on X, by deliberate choice — the profile itself
should be enough to inform a reader end to end.** Instead of image/link
"extras", Claude decides `second_part` per post: an optional genuine
continuation posted immediately as its own reply, when a topic has real
depth worth adding (more mechanism, a concrete example, the second half of
a comparison — never a restatement or filler). Nudged by
`second_part_every_n_posts` (default 4, i.e. roughly 1 in every 4 posts)
and how many posts have gone out since the last one that used it — but
it's a loose guide, not a rule: a genuinely deep topic gets a `second_part`
regardless of the count, and a routine post stays a single tweet even when
one's "due." Most posts are a single tweet. Whenever a post does get a
`second_part`, its main text ends with a short, natural pointer to it
(varied wording, not the same phrase every time) so a reader knows to
check the reply. Telegram is the one exception to "no links": when a post
is based on a specific news article (`news_index`), the channel copy
always shows that article's real source link — X itself still never
carries one. (`src/sources/image_gen.py`, DALL-E-based image generation,
is untouched and still callable — this
trigger just doesn't use it right now; flip it back on later if that
changes.)

`config/ai_manager.json` controls cadence: `min_hours_between_calls` +
`max_calls_per_day` bound Claude calls to roughly 6-7/day, `posts_per_batch`
controls how many posts each call can produce, `max_posts_per_day` (default
14) caps real posts, and reposts are kept deliberately sparse
(`max_reposts_per_day`, default 3, `max_reposts_per_call`, default 1 — one
repost per call naturally spreads them across the day instead of bursting
several at once) so the feed reads mostly as original content.

A call that fails outright or comes back unparseable doesn't start the
cooldown -- `last_call_time` only updates on a successfully parsed decision,
so a dropped call is retried on the very next hourly run instead of
waiting out a full cooldown for a call that never actually produced
anything. `calls_today` still increments on every attempt regardless, so a
persistently broken call can't retry more than `max_calls_per_day` times in
one day.

**Two independent hard budget caps**, each stopping this trigger cleanly
(never erroring) the instant it's reached:

- `config/claude_budget.json`'s `monthly_usd_cap` (default $20) — gates
  whether a new batch-generating Claude call is even attempted (draining
  the queue never needs this, since it doesn't call Claude), tracked from
  each response's *real* token usage (`src/claude_budget.py`), not an
  estimate.
- `config/budget.json`'s `monthly_usd_cap` (default $30) — gates whether a
  decided post/repost/`second_part` actually gets sent to X, same shared
  pool every other trigger already uses.

**These caps are sized so their sum is the account-wide monthly ceiling.**
$20 + $30 = $50: if the target total spend changes, split it the same way
rather than just raising one cap — that's what makes "never above $X/month
total" a structural guarantee instead of an estimate that could be wrong.

Since nothing here is manually approved before it posts, every
batch-generating call sends one audit message to your **private Telegram
bot chat** — every queued post + its reasoning (or "no posts queued" and
why), and every repost attempted along with its reasoning. Each individual
post firing (the first-in-batch or a later queue drain) also gets its own
short bot-chat line, plus the existing per-post cost-chat notification.
This is the only review mechanism for an otherwise fully autonomous
pipeline, so it's worth skimming periodically even if you never intervene.

Requires `ANTHROPIC_API_KEY` — without it, this trigger does nothing (same
"no safe fallback" reasoning as every other Claude-backed trigger).

### Reply Manager (disabled by default — X's reply restriction isn't a per-account setting)

`src/triggers/reply_manager.py` was built to run replies on a much faster
cadence (roughly hourly) than AI Manager's slow 4-6 posts/day rhythm,
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

| Post type | ~posts/month | Link? | Cost |
|---|---|---|---|
| Whale alerts (**paused by default**, capped 1/chain/run, cashtag only) | ~12 if re-enabled | no (see below) | $0.18 if re-enabled |
| News (capped at 2/day, main + source-link reply, ~$0.215/article) | up to ~120 (60 articles) | reply only | up to $12.90 |
| Price alerts | ~20 | no | $0.30 |
| Scheduled daily | ~30 | no | $0.45 |
| Flashback | ~8 | no | $0.12 |
| Polls | ~4 | no | $0.06 |
| Self-reply | ~15 | no | $0.23 |
| **Real-content subtotal** | | | **~$1.16 - $14.06/month**, depending on how often news actually matches |

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

**AI Manager's posts specifically — no images, no links means a flat, low
rate.** Every post is a plain, link-free tweet at the base $0.015 rate —
there's no $0.20 link surcharge to worry about since links are never used
here at all. A post's optional `second_part` (roughly 1 in 4,
`second_part_every_n_posts`) is just another $0.015 reply, not a cost
multiplier. At 12 posts/day with 3 of them getting a `second_part` (15
total tweets):

15 × $0.015 ≈ $0.225/day → **~$6.75/month**

Simple and cheap regardless of format mix, since nothing here varies in
price the way image-vs-link used to. Reposts (retweet/quote, capped at
3/day) add on top of this at the same ~$0.015/action rate (replies are
manual-only for now, see "Reply Manager").

**Claude call cost stays flat regardless of post volume**, since batching
means posts/day scales without scaling calls/day. At ~6-7 calls/day and
Sonnet 5's full post-intro pricing ($3/$15 per 1M tokens), real observed
cost is ~$0.06-0.07/call → **~$11-15/month**, comfortably inside the $20
Claude cap with headroom to spare.

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
                  content_drafts, ai_manager, reply_manager, budget_report)
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
- **`config/ai_manager.json`** — AI Manager's model, call cadence, post/repost daily caps, `posts_per_batch`, and `second_part_every_n_posts` (how often a post gets a genuine continuation reply) — see [AI Manager](#ai-manager-opt-in-via-anthropic_api_key--autonomous-post--repost-decisions).
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
