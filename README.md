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
   No coin logo/media and no tx hash reference right now -- both were tried
   and pulled back (media looked bad, the tx reference is paused pending a
   cleaner presentation); the tx id is still tracked internally for BTC
   dedup even though it's no longer shown in the post.
2. **"JUST IN" news** — RSS + keyword/source filter, paraphrased, always sourced.
   The main post names the outlet only (no link, e.g. "via CoinDesk"), with
   the real source URL in a follow-up reply -- X's algorithm has suppressed
   reach on linked posts hard since March 2026, so this keeps the main post's
   reach intact. Unlike whale alerts this doesn't save money (the link still
   costs $0.20 wherever it lands, so it's ~$0.215/post total now) -- it's a
   reach optimization, not a cost one. Capped at `keywords.max_articles_per_day`
   (default 2), the main cost lever since news is the only post type with a
   real clickable link anywhere in the thread.
3. **Price threshold/milestone alerts** — CoinGecko (crypto) + Twelve Data (stocks/ETFs).
   Crypto alerts also carry the coin's logo as post media.
4. **Scheduled daily post** — market snapshot / Fear & Greed Index (rotates, or both)
5. **Historical flashback** — filler, max once/day, only if nothing else fired
6. **Polls** — ~1x/week engagement mechanic
7. **Self-reply updates** — replies to the bot's *own* tweets only, never others'

Every post type that shows a price/%/index change uses the same 🟢/🔴 dot
convention (`src/formatting.py`'s `dot_for_change`), so the visual language
stays consistent across whale alerts, the snapshot, Fear & Greed, price
alerts, flashback, and self-replies rather than each post type inventing
its own look.
8. **Filler** — absolute last resort, only posts if nothing above did this run.
   Picks from `config/filler.json`'s ~100 generic engagement questions/facts
   (no repeats until the list is exhausted, then reshuffles). This is what
   keeps the account posting roughly once/hour even on quiet news/market
   days — see the cost note below before raising check frequency further.

Plus a separate **retweet pipeline** (retweet only, never auto-reply/comment
under someone else's tweet) and an **opt-in comment-engagement pipeline** —
the one deliberate exception to that rule, kept on a short leash: it only
replies under accounts explicitly listed in `config/reply_targets.json`,
with a hard per-account daily cap, and only ever with a fresh Claude-written
reply (never a canned line — see "Comment engagement" below).

Every crypto ticker mentioned anywhere (whale alerts, price alerts, snapshot,
flashback, self-replies, polls) uses a `$` cashtag (`$BTC`, `$ETH`, ...)
rather than plain text, so X renders its dynamic cashtag chart wherever the
coin comes up. Stock tickers (AAPL, SPY, QQQ) stay plain text.

Every post type that shows a price/%/index change uses the same 🟢/🔴 dot
convention (`src/formatting.py`'s `dot_for_change`), so the visual language
stays consistent across whale alerts, the snapshot, Fear & Greed, price
alerts, flashback, and self-replies rather than each post type inventing
its own look.
8. **Filler** — absolute last resort, only posts if nothing above did this run.
   Picks from `config/filler.json`'s ~100 generic engagement questions/facts
   (no repeats until the list is exhausted, then reshuffles). This is what
   keeps the account posting roughly once/hour even on quiet news/market
   days — see the cost note below before raising check frequency further.

Each post type is its own function in `src/triggers/`, toggled independently
in the `ENABLED` dict at the top of `src/main.py`.

### Coin logo images

Crypto price alerts attach the coin's official logo as post media. The image
URL comes back for free in the same CoinGecko `/coins/markets` call already
made for prices every run (no extra request, no extra API key), and X's media
upload is the older v1.1 endpoint (same OAuth1 credentials already in use, no
new secrets needed either). Whale alerts used to attach one too but it was
pulled back — didn't look good in practice.

This is gated by `config/media.json`'s `"enabled"` flag specifically so it
can be switched off instantly (a config push, no code change) if a live
billing check ever shows X charging extra for posts with media attached —
the same kind of test that confirmed cashtags were free (isolate one trigger,
cap it to one post, check the X credits balance before/after). Recommended:
watch your X credits balance for a day or two after this ships and flip
`media.json`'s `enabled` to `false` if the per-post cost looks higher than
the usual $0.015.

News alerts aren't tied to one coin, so they instead get a small themed
red/green/gray trend-line graphic (`assets/trend_up.png` / `trend_down.png` /
`trend_neutral.png`, pre-generated and checked into the repo — no network
call needed to attach one) matching the "chart snippet + terse JUST IN line"
look other crypto news accounts use. Which one gets attached comes from the
same Claude call already used to paraphrase the headline (it also tags the
story bullish/bearish/neutral); the mechanical-fallback path (no
`ANTHROPIC_API_KEY`) has no sentiment signal, so no image gets attached then.

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

### Comment engagement (opt-in, off by default in practice)

`config/reply_targets.json` lists specific accounts TickerWatch will reply
under (not just retweet), each with a hard `times_per_day` cap. An entry is
inert until you resolve its numeric `user_id` (same one-time lookup as
`config/accounts.json`) — the shipped default has one placeholder entry
(`WatcherGuru`) with a blank `user_id`, so it does nothing until you fill
that in.

Reply text is always freshly written by Claude from the target tweet's own
content (`src/sources/reply_writer.py`) — never a generic "Great post!" —
and requires `ANTHROPIC_API_KEY`; without it, this trigger just skips
(there's no safe mechanical fallback for a reply the way there is for news
paraphrasing, since a low-effort/bot-sounding reply under someone else's
post does more harm than good). Keep the target list small and the daily
cap low at first, and read replies back before adding more accounts.

## Run frequency and cron schedule

```
7 * * * *
```

Every hour, offset to :07 rather than the exact top of the hour (GitHub's
scheduler is most congested at `:00` since every repo tends to schedule
there, which can delay or skip runs) → **24 runs/day → ~730 runs/month**.
Note that higher check
frequency mostly buys faster alert latency and better whale-scan coverage,
**not** proportionally higher cost: scheduled daily/flashback/polls are
capped by date regardless of check frequency, and whale/news/price alerts
are capped by real-world event rate and per-asset cooldowns, not by how
often the workflow runs. The budget cap and `thresholds.json` cooldowns are
what actually control spend — see below.

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
could range from ~$10.50 (quiet news) up toward or past the $15 cap (active
news + heavy filler). If a busy month does push past $15, the budget guard
just does its job: it stops posting non-critical content for the rest of the
month rather than overspending — you'd see this as the account going quiet
plus the 90% Telegram alert firing well before it happens. Two levers if you
want more headroom before that point:

- lower `filler.max_per_day` in `config/thresholds.json` (e.g. to 10-12,
  roughly "fill every other empty hour"), or
- raise `monthly_usd_cap` in `config/budget.json` further.

Enabling 2-3 moderately active retweet accounts adds roughly 60-150 more
actions/month (~$1-2) on top of the total above.

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
                   thresholds.json, budget.json, media.json
state/state.json  dedup/budget state, committed back to the repo after every run
src/
  main.py         orchestrator — priority pipeline, per-trigger error isolation
  context.py      shared per-run objects passed to every trigger
  budget.py       monthly $/post cap enforcement
  x_client.py     tweepy wrapper (post/reply/retweet/poll/media upload), DRY_RUN support
  media.py        coin logo (price alerts) / trend icon (news) -> X media_id helper
  formatting.py   number/text formatting, thread splitting
  sources/        one file per external API (coingecko, twelvedata, whale_btc,
                  whale_eth, news_rss, paraphrase, reply_writer, feargreed)
  triggers/       one file per post type (whale_alerts, news_alerts,
                  price_alerts, scheduled_daily, historical_flashback, polls,
                  self_reply, filler, retweets, comment_engagement, budget_report)
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
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) | Optional — enables real LLM paraphrasing of news headlines (Claude Haiku, a fraction of a cent/call), and is *required* for the comment-engagement pipeline's reply text (see [config/reply_targets.json](#editing-config-files)) since there's no safe generic fallback for that one. |
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
✅ X post created — $6.30/$15.00
```

**Bot chat, daily recap** — sent once/day at **9pm Europe/Brussels time**
(handles the CET/CEST switch automatically):

```
📅 Daily recap
$6.30/$15.00 (42% used)
```

**Bot chat, low-budget alert** — a one-time nudge the moment month-to-date
spend crosses 90% of the cap (won't repeat again until next month), with a
direct link to add credits:

```
⚠️ TickerWatch budget alert: $13.62/$15.00 used (91%) this month.
Add credits: https://console.x.com/ (Billing -> Credits)
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
- **`config/accounts.json`** — accounts to auto-retweet. You must resolve each `@handle` to its numeric `user_id` once (e.g. via a one-off API call or a tool like [tweeterid.com](https://tweeterid.com)) and paste it in — looking it up every run would burn extra API budget. Set `"enabled": true` to activate an account.
- **`config/reply_targets.json`** — accounts to *comment* under (see [Comment engagement](#comment-engagement-opt-in-off-by-default-in-practice)). Same `user_id` resolution as `accounts.json`, plus a `times_per_day` hard cap per account.
- **`config/thresholds.json`** — whale minimums, price % trigger, milestone price levels per symbol, poll day/asset, self-reply timing window, daily-post rotation, and `filler.max_per_day` (how many empty-hour fillers/day at most).
- **`config/filler.json`** — the ~100 generic engagement prompts/facts used as the last-resort safety net. Add/remove freely; just keep entries factual or purely rhetorical (no specific prices/dates, since those need to trace to a real live source).
- **`config/budget.json`** — the monthly cap (see [Cost math](#cost-math-and-the-budget-cap)).
- **`config/media.json`** — the on/off switch for attaching coin logos to posts (see [Coin logo images](#coin-logo-images)).

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
