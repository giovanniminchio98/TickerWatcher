"""
CryptoScope Oracle -- Python port of crypto-scope's client-side quant engine
(oracle.js), so TickerWatch can carry the same statistical read as shared
Context for every trigger, instead of just plain spot prices.

Same math as the JS original, function-for-function: trend/momentum/
volatility indicators, Hurst exponent (persistence vs. mean reversion),
risk stats (VaR/CVaR/max drawdown/Sharpe/Sortino), distribution stats
(skew/kurtosis/autocorrelation/Z-score), a Monte-Carlo GBM forecast, and a
fused weighted "verdict" composite (0-100) with a confidence read. Pure
functions, no I/O -- given a list of OHLC candles it returns one dict, or
None if there isn't enough history yet (matches oracle.js's own `n < 30`
guard).

Deliberately drops oracle.js's full Monte-Carlo "cone" (the per-step
quantile bands used only for the site's chart overlay) -- nothing here
consumes per-step data, only the terminal-price distribution (used for the
touch/target probabilities), so keeping it out saves memory with zero loss
of anything actually used by src/triggers/oracle_alerts.py or ai_manager.

Unlike crypto-scope's site (a daily static bundle), this is recomputed
fresh every TickerWatch run straight from src/sources/kraken.py's keyless
klines -- see main.py's _fetch_oracle_data.
"""
import math
import random


def _mean(a):
    return sum(a) / len(a) if a else 0.0


def _std(a, m=None):
    if len(a) < 2:
        return 0.0
    m = _mean(a) if m is None else m
    return math.sqrt(sum((x - m) ** 2 for x in a) / (len(a) - 1))


def _skewness(a):
    m = _mean(a)
    s = _std(a, m)
    n = len(a)
    if not s or n < 3:
        return 0.0
    return sum(((x - m) / s) ** 3 for x in a) / n


def _kurtosis(a):
    m = _mean(a)
    s = _std(a, m)
    n = len(a)
    if not s or n < 4:
        return 0.0
    return sum(((x - m) / s) ** 4 for x in a) / n - 3


def _quantile(sorted_a, q):
    if not sorted_a:
        return 0.0
    idx = (len(sorted_a) - 1) * q
    lo, hi = math.floor(idx), math.ceil(idx)
    if lo == hi:
        return sorted_a[lo]
    return sorted_a[lo] + (sorted_a[hi] - sorted_a[lo]) * (idx - lo)


def _log_returns(c):
    return [math.log(c[i] / c[i - 1]) for i in range(1, len(c)) if c[i - 1] > 0 and c[i] > 0]


def _simple_returns(c):
    return [c[i] / c[i - 1] - 1 for i in range(1, len(c)) if c[i - 1]]


def _linreg_slope(xs, ys):
    n = len(xs)
    sx = sy = sxy = sx2 = 0.0
    for i in range(n):
        sx += xs[i]
        sy += ys[i]
        sxy += xs[i] * ys[i]
        sx2 += xs[i] * xs[i]
    d = n * sx2 - sx * sx
    return (n * sxy - sx * sy) / d if d else 0.0


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _sign(x):
    return (x > 0) - (x < 0)


def _ema_series(vals, period):
    n = len(vals)
    out = [None] * n
    if n < period:
        return out
    k = 2 / (period + 1)
    prev = _mean(vals[:period])
    out[period - 1] = prev
    for i in range(period, n):
        prev = vals[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _rsi(c, p=14):
    if len(c) < p + 1:
        return None
    gain = loss = 0.0
    for i in range(1, p + 1):
        d = c[i] - c[i - 1]
        if d >= 0:
            gain += d
        else:
            loss -= d
    gain /= p
    loss /= p
    for i in range(p + 1, len(c)):
        d = c[i] - c[i - 1]
        gain = (gain * (p - 1) + max(d, 0)) / p
        loss = (loss * (p - 1) + max(-d, 0)) / p
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


def _macd(c, f=12, s=26, sig=9):
    if len(c) < s + sig:
        return None
    ef, es = _ema_series(c, f), _ema_series(c, s)
    macd_line = [ef[i] - es[i] for i in range(len(c)) if ef[i] is not None and es[i] is not None]
    if len(macd_line) < sig:
        return None
    sig_line = _ema_series(macd_line, sig)
    m, sg = macd_line[-1], sig_line[-1]
    return {"macd": m, "signal": sg, "hist": m - sg}


def _bollinger(c, p=20, k=2):
    if len(c) < p:
        return None
    sl = c[-p:]
    m = _mean(sl)
    sd = _std(sl, m)
    price = c[-1]
    upper, lower = m + k * sd, m - k * sd
    return {
        "mid": m, "upper": upper, "lower": lower,
        "pct_b": (price - lower) / ((upper - lower) or 1),
        "bandwidth": (upper - lower) / (m or 1),
    }


def _stochastic(h, l, c, p=14):
    if len(c) < p:
        return None
    hh, ll = max(h[-p:]), min(l[-p:])
    return {"k": (c[-1] - ll) / ((hh - ll) or 1) * 100}


def _atr(h, l, c, p=14):
    if len(c) < p + 1:
        return None
    tr = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    a = _mean(tr[:p])
    for i in range(p, len(tr)):
        a = (a * (p - 1) + tr[i]) / p
    return a


def _hurst(series):
    """Rescaled-range (R/S) analysis across chunk sizes. H > 0.5 =>
    persistent/trending, H < 0.5 => anti-persistent/mean-reverting."""
    n = len(series)
    if n < 32:
        return 0.5
    xs, ys = [], []
    size = 8
    while size <= n // 2:
        chunks = n // size
        rs_acc, cnt = 0.0, 0
        for ci in range(chunks):
            chunk = series[ci * size:(ci + 1) * size]
            m = _mean(chunk)
            cum, mx, mn = 0.0, -math.inf, math.inf
            for v in chunk:
                cum += v - m
                mx = max(mx, cum)
                mn = min(mn, cum)
            r, s = mx - mn, _std(chunk, m)
            if s > 0:
                rs_acc += r / s
                cnt += 1
        if cnt:
            xs.append(math.log(size))
            ys.append(math.log(rs_acc / cnt))
        size = int(size * 1.6)
    if len(xs) < 2:
        return 0.5
    return _clamp(_linreg_slope(xs, ys), 0, 1)


def _autocorr(series, lag=1):
    n = len(series)
    if n <= lag + 1:
        return 0.0
    m = _mean(series)
    num = den = 0.0
    for i in range(n):
        den += (series[i] - m) ** 2
        if i >= lag:
            num += (series[i] - m) * (series[i - lag] - m)
    return num / den if den else 0.0


def _max_drawdown(c):
    peak, mdd = c[0], 0.0
    for v in c:
        peak = max(peak, v)
        mdd = min(mdd, (v - peak) / peak)
    return mdd


def _pivot_levels(h, l, c, w=3):
    """Significant price levels via fractal pivots, clustered near current price."""
    price = c[-1]
    res, sup = [], []
    for i in range(w, len(c) - w):
        is_high = all(h[j] <= h[i] for j in range(i - w, i + w + 1))
        is_low = all(l[j] >= l[i] for j in range(i - w, i + w + 1))
        if is_high and h[i] > price:
            res.append(h[i])
        if is_low and l[i] < price:
            sup.append(l[i])

    def dedupe(arr, descending):
        out = []
        for v in sorted(arr, reverse=descending):
            if not any(abs(x - v) / price < 0.012 for x in out):
                out.append(v)
            if len(out) >= 3:
                break
        return out

    return {"resistances": dedupe(res, False), "supports": dedupe(sup, True)}


def _monte_carlo(closes, horizon, paths):
    """Geometric Brownian Motion forecast. Returns terminal prices plus each
    path's max/min running return (for touch-probability targets) -- the
    full per-step cone is deliberately not tracked, see module docstring."""
    r = _log_returns(closes)
    mu = _mean(r)
    sigma = _std(r) or 1e-6
    s0 = closes[-1]
    terminals, max_rets, min_rets = [], [], []
    for _ in range(paths):
        log_cum, mx, mn = 0.0, 0.0, 0.0
        for _step in range(horizon):
            log_cum += mu + sigma * random.gauss(0, 1)
            mx = max(mx, log_cum)
            mn = min(mn, log_cum)
        terminals.append(s0 * math.exp(log_cum))
        max_rets.append(math.exp(mx) - 1)
        min_rets.append(math.exp(mn) - 1)
    terminals.sort()  # quantile() below requires a sorted sequence
    return {
        "mu": mu, "sigma": sigma, "s0": s0, "horizon": horizon, "paths": paths,
        "terminals": terminals, "max_rets": max_rets, "min_rets": min_rets,
    }


def analyze(candles, paths=3000):
    """candles: ascending-time list of {"t": ms, "o", "h", "l", "c"} (see
    src/sources/binance.get_klines). Returns None if there are fewer than
    30 candles (matches oracle.js's own minimum-history guard)."""
    if not candles:
        return None
    closes = [d["c"] for d in candles]
    highs = [d["h"] for d in candles]
    lows = [d["l"] for d in candles]
    times = [d["t"] for d in candles]
    n = len(closes)
    if n < 30:
        return None

    diffs = sorted(times[i] - times[i - 1] for i in range(1, len(times)))
    bar_ms = diffs[len(diffs) // 2] if diffs else 3600000
    bars_per_year = (365.25 * 24 * 3600 * 1000) / bar_ms

    lr = _log_returns(closes)
    sr = _simple_returns(closes)
    mu_bar = _mean(lr)
    sd_bar = _std(lr) or 1e-6
    price = closes[-1]

    e20 = _ema_series(closes, min(20, n))[-1]
    e50 = _ema_series(closes, min(50, n))[-1]
    look = min(20, n - 1)
    slope_pct = _linreg_slope(list(range(n - look, n)), closes[n - look:n]) / price * 100 if price else 0.0

    rsi_v = _rsi(closes)
    macd_v = _macd(closes)
    bb_v = _bollinger(closes)
    stoch_v = _stochastic(highs, lows, closes)
    atr_v = _atr(highs, lows, closes)
    atr_pct = (atr_v / price * 100) if (atr_v is not None and price) else None

    hurst_v = _hurst(lr)
    skew = _skewness(lr)
    kurt = _kurtosis(lr)
    ac1 = _autocorr(lr, 1)
    z_slice = closes[-min(20, n):]
    z_mean = _mean(z_slice)
    z_std = _std(z_slice, z_mean) or 1e-9
    z_score = (price - z_mean) / z_std

    ann_vol = sd_bar * math.sqrt(bars_per_year)
    ann_ret = mu_bar * bars_per_year
    sr_sorted = sorted(sr)
    var95 = -_quantile(sr_sorted, 0.05)
    tail = [x for x in sr_sorted if x <= -var95]
    cvar95 = -_mean(tail) if tail else var95
    mdd = _max_drawdown(closes)
    downside = [x for x in sr if x < 0]
    sharpe = (mu_bar / sd_bar * math.sqrt(bars_per_year)) if sd_bar else 0.0
    sortino = (mu_bar / (_std(downside) or 1e-9) * math.sqrt(bars_per_year)) if downside else 0.0

    horizon = int(_clamp(round(n * 0.2), 12, 60))
    mc = _monte_carlo(closes, horizon, paths)

    term = mc["terminals"]
    total = len(term)
    p_up = sum(1 for v in term if v > price) / total
    exp_ret = _mean(term) / price - 1
    med_ret = _quantile(term, 0.5) / price - 1
    range_lo = _quantile(term, 0.05) / price - 1
    range_hi = _quantile(term, 0.95) / price - 1
    sig_h = sd_bar * math.sqrt(horizon)

    def p_term_above(pct):
        return sum(1 for v in term if v >= price * (1 + pct / 100)) / total

    def p_term_below(pct):
        return sum(1 for v in term if v <= price * (1 - pct / 100)) / total

    def p_touch_up(pct):
        return sum(1 for v in mc["max_rets"] if v * 100 >= pct) / total

    def p_touch_dn(pct):
        return sum(1 for v in mc["min_rets"] if v * 100 <= -pct) / total

    t1 = round(sig_h * 100, 1)
    targets = [
        {"label": f"+{t1}% (1σ)", "p": p_term_above(t1), "touch": p_touch_up(t1), "dir": 1},
        {"label": f"−{t1}% (1σ)", "p": p_term_below(t1), "touch": p_touch_dn(t1), "dir": -1},
        {"label": "+5%", "p": p_term_above(5), "touch": p_touch_up(5), "dir": 1},
        {"label": "−5%", "p": p_term_below(5), "touch": p_touch_dn(5), "dir": -1},
        {"label": "+10%", "p": p_term_above(10), "touch": p_touch_up(10), "dir": 1},
        {"label": "−10%", "p": p_term_below(10), "touch": p_touch_dn(10), "dir": -1},
    ]

    signals = []

    def add_signal(key, score):
        signals.append({"key": key, "score": _clamp(score, -1, 1)})

    trend_score = 0.0
    if e20 is not None and e50 is not None:
        trend_score += _clamp((e20 - e50) / (e50 or 1) / 0.02, -1, 1) * 0.6
        trend_score += _clamp((price - e50) / (e50 or 1) / 0.03, -1, 1) * 0.4
    add_signal("trend", trend_score)
    add_signal("slope", slope_pct / 0.5)
    if macd_v:
        add_signal("macd", macd_v["hist"] / (price * 0.01) if price else 0.0)
    if rsi_v is not None:
        r_score = (rsi_v - 50) / 30
        if rsi_v > 72:
            r_score = -((rsi_v - 72) / 28) * 0.6
        if rsi_v < 28:
            r_score = ((28 - rsi_v) / 28) * 0.6
        add_signal("rsi", r_score)
    if stoch_v:
        k = stoch_v["k"]
        mult = -0.7 if (k > 80 or k < 20) else 1
        add_signal("stoch", (k - 50) / 50 * mult)
    if bb_v:
        add_signal("boll", (0.5 - bb_v["pct_b"]) / 0.5 * 0.8)
    add_signal("z", -z_score / 2.5)
    drift_score = _clamp(mc["mu"] / sd_bar, -1, 1)
    h_adj = (hurst_v - 0.5) * 2
    add_signal("mc", drift_score * (0.5 + 0.5 * _sign(h_adj) * abs(h_adj)))

    weights = {"trend": 1.4, "slope": 0.9, "macd": 1.0, "rsi": 0.9, "stoch": 0.6, "boll": 0.7, "z": 0.8, "mc": 1.3}
    w_sum = w_abs = w_sign = w_mag = 0.0
    for s in signals:
        w = weights.get(s["key"], 1)
        w_sum += w * s["score"]
        w_abs += w
        w_sign += w * _sign(s["score"])
        w_mag += w * abs(s["score"])
    norm = (w_sum / w_abs) if w_abs else 0.0
    score100 = round(_clamp(50 + norm * 50, 0, 100))

    agreement = (abs(w_sign) / w_abs) if w_abs else 0.0
    strength = (w_mag / w_abs) if w_abs else 0.0
    data_factor = _clamp(n / 150, 0.5, 1)
    confidence = round(_clamp((0.55 * agreement + 0.45 * strength) * data_factor * 100, 5, 95))

    if score100 >= 72:
        label = "Strongly Bullish"
    elif score100 >= 60:
        label = "Bullish"
    elif score100 >= 54:
        label = "Lean Bullish"
    elif score100 > 46:
        label = "Neutral"
    elif score100 > 40:
        label = "Lean Bearish"
    elif score100 > 28:
        label = "Bearish"
    else:
        label = "Strongly Bearish"

    persistence = "Persistent / trending" if hurst_v > 0.55 else ("Mean-reverting" if hurst_v < 0.45 else "Random walk")
    direction = "up" if norm > 0.12 else ("down" if norm < -0.12 else "sideways")
    vol_state = "extreme" if ann_vol > 1.2 else ("high" if ann_vol > 0.8 else ("moderate" if ann_vol > 0.4 else "low"))
    regime = {
        "persistence": persistence, "dir": direction, "vol_state": vol_state,
        "label": f"{persistence} · {vol_state} vol · biased {direction}",
    }

    return {
        "meta": {"bars": n, "bar_ms": bar_ms, "bars_per_year": bars_per_year, "horizon": horizon, "price": price},
        "trend": {"e20": e20, "e50": e50, "slope_pct": slope_pct},
        "momentum": {"rsi": rsi_v, "macd": macd_v, "stoch": stoch_v["k"] if stoch_v else None},
        "volatility": {"ann_vol": ann_vol, "atr": atr_v, "atr_pct": atr_pct, "bollinger": bb_v},
        "stats": {"hurst": hurst_v, "skew": skew, "kurtosis": kurt, "autocorr": ac1, "z_score": z_score},
        "risk": {
            "var95": var95, "cvar95": cvar95, "max_drawdown": mdd,
            "sharpe": sharpe, "sortino": sortino, "ann_ret": ann_ret,
        },
        "regime": regime,
        "signals": signals,
        "composite": {"score": score100, "label": label, "confidence": confidence, "norm": norm},
        "probs": {
            "p_up": p_up, "exp_ret": exp_ret, "med_ret": med_ret,
            "range_lo": range_lo, "range_hi": range_hi, "sig_h": sig_h, "targets": targets,
        },
        "levels": _pivot_levels(highs, lows, closes),
    }
