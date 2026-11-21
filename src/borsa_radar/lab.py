"""Strategy lab: rule-based swing strategies judged on ~20 years of daily bars.

Rules are fixed and mechanical (no discretion), so the same functions drive the backtest, the live signal scan and
the paper-trading book. Execution is modelled the way the radar can actually be used: the scan runs after the US
close, so an entry is the next session's open (or a limit order valid for that one session), and an exit signal seen
at a close is executed at the next open. Costs: COST_RT round trip (slippage + fees) on every trade.

Pure numpy, one symbol at a time (the VM has little free memory).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

COST_RT = 0.001          # 0.10 % round trip; the stress run doubles it
MIN_PRICE = 5.0
MIN_DVOL = 20e6          # 20-day average dollar volume
TRAIN_END = 20161231     # train 2006-2016 (incl. 2008), test 2017 → today (2018 Q4, 2020, 2022)


# ── indicators ────────────────────────────────────────────

def sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        cs = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def rsi(x: np.ndarray, n: int) -> np.ndarray:
    """Wilder RSI."""
    out = np.full(len(x), np.nan)
    if len(x) <= n:
        return out
    d = np.diff(x)
    up, dn = np.maximum(d, 0.0), np.maximum(-d, 0.0)
    g, lo = up[:n].mean(), dn[:n].mean()
    k = n - 1
    vals = out
    vals[n] = 50.0 if g == lo == 0 else 100.0 if lo == 0 else 100 - 100 / (1 + g / lo)
    for i in range(n, len(d)):
        g = (g * k + up[i]) / n
        lo = (lo * k + dn[i]) / n
        vals[i + 1] = 50.0 if g == lo == 0 else 100.0 if lo == 0 else 100 - 100 / (1 + g / lo)
    return vals


def streak(c: np.ndarray) -> np.ndarray:
    """Consecutive up (+) / down (−) closes."""
    out = np.zeros(len(c))
    for i in range(1, len(c)):
        if c[i] > c[i - 1]:
            out[i] = out[i - 1] + 1 if out[i - 1] > 0 else 1
        elif c[i] < c[i - 1]:
            out[i] = out[i - 1] - 1 if out[i - 1] < 0 else -1
    return out


def pct_rank(x: np.ndarray, n: int) -> np.ndarray:
    """Share (0-100) of the previous n values below today's."""
    out = np.full(len(x), np.nan)
    if len(x) > n:
        w = sliding_window_view(x[:-1], n)          # windows ending at i-1
        out[n:] = (w < x[n:, None]).sum(axis=1) / n * 100
    return out


def roll_min(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = sliding_window_view(x, n).min(axis=1)
    return out


def roll_max(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = sliding_window_view(x, n).max(axis=1)
    return out


class Ind:
    """Indicator arrays for one symbol (bars: day, open, high, low, close, volume)."""

    def __init__(self, bars: np.ndarray):
        self.day = bars[:, 0].astype(np.int64)
        self.o, self.h, self.l, self.c, self.v = (bars[:, k] for k in range(1, 6))
        c = self.c
        self.n = len(c)
        self.sma5 = sma(c, 5)
        self.sma200 = sma(c, 200)
        self.rsi2 = rsi(c, 2)
        roc = np.concatenate([[np.nan], c[1:] / c[:-1] - 1])
        roc[0] = 0.0
        self.crsi = (rsi(c, 3) + rsi(streak(c), 2) + pct_rank(roc, 100)) / 3
        rng = self.h - self.l
        with np.errstate(invalid="ignore", divide="ignore"):
            self.ibs = np.where(rng > 0, (c - self.l) / rng, 0.5)
        self.min7 = roll_min(c, 7)
        self.max7 = roll_max(c, 7)
        self.dvol = sma(c * self.v, 20)
        self.prev_c = np.concatenate([[np.nan], c[:-1]])
        self.prev_h = np.concatenate([[np.nan], self.h[:-1]])
        down = np.concatenate([[False], c[1:] < c[:-1]])
        self.down3 = down & np.roll(down, 1) & np.roll(down, 2)
        self.down3[:3] = False
        with np.errstate(invalid="ignore"):
            self.liquid = (c >= MIN_PRICE) & (self.dvol >= MIN_DVOL)
            self.up_trend = c > self.sma200


# ── market context (macro filters) ────────────────────────

@dataclass
class Macro:
    day: np.ndarray                 # SPY calendar
    spy_up: np.ndarray              # SPY close above its 200-day average
    vix: np.ndarray
    credit_ok: np.ndarray           # HYG/LQD ratio above its 50-day average (risk appetite intact)

    def at(self, days: np.ndarray, arr: np.ndarray, default):
        """Values of `arr` as of each of `days` (last SPY session on or before)."""
        idx = np.searchsorted(self.day, days, side="right") - 1
        out = np.where(idx >= 0, arr[np.clip(idx, 0, None)], default)
        return out


def build_macro(get) -> Macro:
    """`get(symbol)` returns the bar array or None."""
    spy = get("SPY")
    day = spy[:, 0].astype(np.int64)
    c = spy[:, 4]
    with np.errstate(invalid="ignore"):
        spy_up = c > sma(c, 200)

    def aligned(sym, col=4):
        a = get(sym)
        out = np.full(len(day), np.nan)
        if a is None:
            return out
        d = a[:, 0].astype(np.int64)
        idx = np.searchsorted(d, day, side="right") - 1
        ok = idx >= 0
        out[ok] = a[idx[ok], col]
        return out

    vix = aligned("^VIX")
    ratio = aligned("HYG") / aligned("LQD")
    with np.errstate(invalid="ignore"):
        credit_ok = np.where(np.isnan(ratio), True, ratio > sma(np.nan_to_num(ratio, nan=np.nanmean(ratio)), 50))
    return Macro(day, spy_up, vix, credit_ok.astype(bool))


# ── strategies ────────────────────────────────────────────

@dataclass(frozen=True)
class Spec:
    name: str
    entry: str            # rsi2 | cumrsi | crsi | ibs | dbl7 | down3
    th: float             # entry threshold
    exit: str             # sma5 | rsi2 | crsi | prevhigh | max7 | first_up | above_entry
    mode: str = "open"    # open (next open) | limit (next session, limit below signal close)
    limit: float = 0.0    # limit depth, fraction below the signal close
    hold: int = 10        # max sessions in the trade
    macro: str = "none"   # none | spy | vix | credit | all
    stop: float = 0.0     # hard stop as a fraction below entry (0 = none)

    @property
    def key(self) -> str:
        parts = [self.entry, f"{self.th:g}", self.exit, self.mode + (f"{self.limit * 100:g}" if self.mode == "limit" else ""),
                 f"h{self.hold}", self.macro] + ([f"s{self.stop * 100:g}"] if self.stop else [])
        return "|".join(parts)


def entry_mask(s: Spec, x: Ind) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        base = x.liquid & x.up_trend
        if s.entry == "rsi2":
            sig = x.rsi2 < s.th
        elif s.entry == "cumrsi":
            sig = (x.rsi2 + np.concatenate([[np.nan], x.rsi2[:-1]])) < s.th
        elif s.entry == "crsi":
            sig = x.crsi < s.th
        elif s.entry == "ibs":
            sig = (x.ibs < s.th) & (x.c < x.prev_c)
        elif s.entry == "dbl7":
            sig = x.c <= x.min7
        elif s.entry == "down3":
            sig = x.down3 & (x.rsi2 < s.th)
        else:
            raise ValueError(s.entry)
    return base & sig


def exit_mask(s: Spec, x: Ind) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        if s.exit == "sma5":
            return x.c > x.sma5
        if s.exit == "rsi2":
            return x.rsi2 > 70
        if s.exit == "crsi":
            return x.crsi > 70
        if s.exit == "prevhigh":
            return x.c > x.prev_h
        if s.exit == "max7":
            return x.c >= x.max7
        if s.exit == "first_up":
            return x.c > x.prev_c
        if s.exit == "above_entry":          # trade-specific: handled in simulate()
            return np.zeros(x.n, dtype=bool)
    raise ValueError(s.exit)


def rank_value(s: Spec, x: Ind) -> np.ndarray:
    """Lower = more stretched = taken first when there are more signals than free slots."""
    return x.crsi if s.entry == "crsi" else x.ibs if s.entry == "ibs" else x.rsi2


def macro_mask(s: Spec, x: Ind, m: Macro | None) -> np.ndarray:
    if s.macro == "none" or m is None:
        return np.ones(x.n, dtype=bool)
    spy = m.at(x.day, m.spy_up, True).astype(bool)
    vix = m.at(x.day, m.vix, np.nan)
    cred = m.at(x.day, m.credit_ok, True).astype(bool)
    with np.errstate(invalid="ignore"):
        vix_ok = ~(vix > 30)
    return {"spy": spy, "vix": vix_ok, "credit": cred, "all": spy & vix_ok & cred}[s.macro]


@dataclass
class Trade:
    sym: str
    sig: int        # signal day (yyyymmdd)
    entry_day: int
    entry: float
    exit_day: int
    exit: float
    ret: float      # net of costs
    hold: int
    rank: float
    why: str        # exit reason: signal | time | stop | open (still running at the data end)


def follow_one(s: Spec, sym: str, x: Ind, t: int, ex: np.ndarray, rank: float = 50.0, cost: float = COST_RT) -> Trade | None:
    """The trade born from a signal at bar t. None = no fill (limit not reached, or no session after t yet).

    why: signal | time | stop (closed), open (still running at the last bar), exit_next (exit signal seen at the last
    close; the exit is the next open)."""
    n = x.n
    if t + 1 >= n:
        return None
    e = t + 1
    if s.mode == "limit":
        lim = x.c[t] * (1 - s.limit)
        if x.l[e] > lim:
            return None                      # not filled: the order lapses after one session
        px = min(x.o[e], lim)
    else:
        px = x.o[e]
    stop_px = px * (1 - s.stop) if s.stop else None
    exit_i, exit_px, why = None, None, "time"
    last = min(n - 1, e + s.hold - 1)
    for j in range(e, last + 1):
        if stop_px is not None and x.l[j] <= stop_px:
            exit_i, exit_px, why = j, (stop_px if j == e else min(x.o[j], stop_px)), "stop"
            break
        hit = x.c[j] > px * (1 + cost) if s.exit == "above_entry" else ex[j]
        if hit:
            if j + 1 < n:
                exit_i, exit_px, why = j + 1, x.o[j + 1], "signal"
            else:
                exit_i, exit_px, why = j, x.c[j], "exit_next"
            break
    if exit_i is None:
        if last + 1 < n:
            exit_i, exit_px = last + 1, x.o[last + 1]
        elif last - e + 1 >= s.hold:
            exit_i, exit_px, why = last, x.c[last], "exit_next"     # time is up: out at the next open
        else:
            exit_i, exit_px, why = last, x.c[last], "open"
    ret = exit_px / px - 1 - cost
    return Trade(sym, int(x.day[t]), int(x.day[e]), float(px), int(x.day[exit_i]), float(exit_px),
                 float(ret), int(exit_i - e), rank, why)


def member_mask(days: np.ndarray, intervals) -> np.ndarray:
    """Days on which the stock was an index member; intervals = [[start, end_exclusive], …] as yyyymmdd ints."""
    out = np.zeros(len(days), dtype=bool)
    for a, b in intervals:
        out |= (days >= a) & (days < b)
    return out


def simulate(s: Spec, sym: str, x: Ind, m: Macro | None, start: int = 0, end: int = 99999999,
             cost: float = COST_RT, sig_mask: np.ndarray | None = None, member: np.ndarray | None = None) -> list[Trade]:
    """All trades of one symbol, one position at a time. A signal while a trade is open is ignored.

    `member` (point-in-time universe): signals only on days the stock was in the index; an open trade still runs to
    its exit rule after the stock leaves the index."""
    ent = entry_mask(s, x) & macro_mask(s, x, m) if sig_mask is None else sig_mask
    if member is not None:
        ent = ent & member
    ex = exit_mask(s, x)
    rk = rank_value(s, x)
    out: list[Trade] = []
    busy = -1
    for t in np.flatnonzero(ent & (x.day >= start) & (x.day <= end)):
        if t <= busy:
            continue
        tr = follow_one(s, sym, x, int(t), ex, float(rk[t]) if not math.isnan(rk[t]) else 50.0, cost)
        if tr is None:
            continue
        out.append(tr)
        busy = int(np.searchsorted(x.day, tr.exit_day))
    return out


# ── statistics ────────────────────────────────────────────

def stats(trades: list[Trade]) -> dict:
    done = [t for t in trades if t.why not in ("open", "exit_next")]
    n = len(done)
    if not n:
        return {"n": 0}
    r = np.array([t.ret for t in done])
    wins, losses = r[r > 0], r[r <= 0]
    gross_w, gross_l = wins.sum(), -losses.sum()
    years: dict[int, list[float]] = {}
    for t in done:
        years.setdefault(t.exit_day // 10000, []).append(t.ret)
    by_year = {y: {"n": len(v), "win": sum(1 for z in v if z > 0) / len(v) * 100, "avg": float(np.mean(v)) * 100}
               for y, v in sorted(years.items())}
    return {
        "n": n,
        "win": len(wins) / n * 100,
        "avg": float(r.mean()) * 100,
        "avg_win": float(wins.mean()) * 100 if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) * 100 if len(losses) else 0.0,
        "pf": float(gross_w / gross_l) if gross_l > 0 else float("inf"),
        "worst": float(r.min()) * 100,
        "p5": float(np.percentile(r, 5)) * 100,
        "hold": float(np.mean([t.hold for t in done])),
        "stops": sum(1 for t in done if t.why == "stop"),
        "years": by_year,
        "worst_year_win": min(v["win"] for v in by_year.values() if v["n"] >= 20) if any(v["n"] >= 20 for v in by_year.values()) else None,
        "neg_years": sum(1 for v in by_year.values() if v["n"] >= 20 and v["avg"] <= 0),
    }


@dataclass
class Book:
    """Portfolio replay: `slots` equal positions, signals ranked by `rank` (most stretched first)."""
    slots: int = 10
    curve: list = field(default_factory=list)


def portfolio(trades: list[Trade], closes: dict[str, tuple[np.ndarray, np.ndarray]], calendar: np.ndarray,
              slots: int = 10, start: int = 0, cost: float = COST_RT) -> dict:
    """Daily mark-to-market replay of a capped book. `closes[sym] = (days, close)`; `cost` = round trip, half per side.

    Each session: positions whose exit day has come are closed at their exit price; then new trades whose entry day
    is today take free slots (lowest rank first), each sized at equity / slots. Returns CAGR, max drawdown, exposure,
    win rate of the trades actually taken, and the equity curve (monthly points)."""
    by_entry: dict[int, list[Trade]] = {}
    for t in trades:
        if t.entry_day >= start:
            by_entry.setdefault(t.entry_day, []).append(t)
    cash, equity = 1.0, 1.0
    open_pos: list[tuple[Trade, float]] = []     # (trade, shares)
    taken: list[Trade] = []
    peak, max_dd = 1.0, 0.0
    curve: list[tuple[int, float]] = []
    exposure_days = 0
    cal = calendar[calendar >= start]
    def close_on(sym, d):
        ds, cs = closes[sym]
        i = np.searchsorted(ds, d, side="right") - 1
        return float(cs[i]) if i >= 0 else None
    for d in cal:
        still = []
        for t, sh in open_pos:
            if t.exit_day <= d:
                cash += sh * t.exit * (1 - cost / 2)
            else:
                still.append((t, sh))
        open_pos = still
        # equity before new entries (marked at today's open ≈ yesterday's close is close enough for sizing)
        mark = cash + sum(sh * (close_on(t.sym, d) or t.entry) for t, sh in open_pos)
        free = slots - len(open_pos)
        held = {t.sym for t, _ in open_pos}
        for t in sorted(by_entry.get(int(d), []), key=lambda z: z.rank):
            if free <= 0:
                break
            if t.sym in held:
                continue
            alloc = min(cash, mark / slots)
            if alloc <= 0:
                break
            sh = alloc / (t.entry * (1 + cost / 2))
            cash -= alloc
            open_pos.append((t, sh))
            held.add(t.sym)
            taken.append(t)
            free -= 1
        equity = cash + sum(sh * (close_on(t.sym, d) or t.entry) for t, sh in open_pos)
        if open_pos:
            exposure_days += 1
        peak = max(peak, equity)
        max_dd = max(max_dd, 1 - equity / peak)
        if not curve or curve[-1][0] // 100 != int(d) // 100:
            curve.append((int(d), equity))
        else:
            curve[-1] = (int(d), equity)
    yrs = len(cal) / 252 if len(cal) else 1
    st = stats([t for t in taken if t.exit_day <= int(cal[-1])]) if len(cal) else {"n": 0}
    equity = float(equity)
    return {"cagr": (equity ** (1 / yrs) - 1) * 100 if equity > 0 else -100.0, "max_dd": float(max_dd) * 100,
            "exposure": exposure_days / max(1, len(cal)) * 100, "final": equity, "taken": st,
            "curve": [(d, float(v)) for d, v in curve]}


# ── batch runs over the long history ──────────────────────

class Agg:
    """Compact per-spec accumulator (the VM cannot hold every trade of hundreds of specs)."""

    def __init__(self):
        self.ret: list[np.ndarray] = []
        self.sig: list[np.ndarray] = []
        self.hold: list[np.ndarray] = []
        self.stop: int = 0

    def add(self, trades: list[Trade]):
        done = [t for t in trades if t.why not in ("open", "exit_next")]
        if done:
            self.ret.append(np.array([t.ret for t in done], dtype=np.float32))
            self.sig.append(np.array([t.sig for t in done], dtype=np.int32))
            self.hold.append(np.array([t.hold for t in done], dtype=np.int16))
            self.stop += sum(1 for t in done if t.why == "stop")

    def summary(self, lo: int = 0, hi: int = 99999999) -> dict:
        if not self.ret:
            return {"n": 0}
        r, s, h = np.concatenate(self.ret), np.concatenate(self.sig), np.concatenate(self.hold)
        k = (s >= lo) & (s <= hi)
        r, s, h = r[k].astype(np.float64), s[k], h[k]
        n = len(r)
        if not n:
            return {"n": 0}
        w, l_ = r[r > 0], r[r <= 0]
        yrs = s // 10000
        by_year = {}
        for y in np.unique(yrs):
            ry = r[yrs == y]
            by_year[int(y)] = {"n": int(len(ry)), "win": float((ry > 0).mean() * 100), "avg": float(ry.mean() * 100)}
        big = [v for v in by_year.values() if v["n"] >= 20]
        return {"n": n, "win": float((r > 0).mean() * 100), "avg": float(r.mean() * 100),
                "avg_win": float(w.mean() * 100) if len(w) else 0.0, "avg_loss": float(l_.mean() * 100) if len(l_) else 0.0,
                "pf": float(w.sum() / -l_.sum()) if l_.sum() < 0 else float("inf"), "worst": float(r.min() * 100),
                "p5": float(np.percentile(r, 5) * 100), "hold": float(h.mean()), "years": by_year,
                "worst_year_win": min((v["win"] for v in big), default=None),
                "neg_years": sum(1 for v in big if v["avg"] <= 0)}


def run(specs: list[Spec], hist=None, symbols: list[str] | None = None, keep: set[str] | None = None,
        cost: float = COST_RT, progress=None, members: dict | None = None) -> tuple[dict[str, Agg], dict[str, list[Trade]], dict]:
    """Replay every spec on every stock of the long history.

    `members` = {symbol: intervals} switches to the point-in-time universe (only those symbols, only on member days).
    Returns (aggregates by spec key, full trade lists for the keys in `keep`, closes for those trades' symbols)."""
    from . import lab_data
    if hist is None:
        hist = np.load(lab_data.HIST_PATH)
    keymap = {lab_data._unkey(k): k for k in hist.files}
    get = lambda sym: hist[keymap[sym]] if sym in keymap else None  # noqa: E731
    m = build_macro(get)
    syms = symbols or [s for s in sorted(keymap) if s not in lab_data.MACRO and (members is None or s in members)]
    aggs = {s.key: Agg() for s in specs}
    kept: dict[str, list[Trade]] = {k: [] for k in (keep or set())}
    closes: dict = {}
    for i, sym in enumerate(syms):
        bars = get(sym)
        if bars is None or len(bars) < 260:
            continue
        x = Ind(bars)
        mem = member_mask(x.day, members[sym]) if members is not None else None
        for s in specs:
            tr = simulate(s, sym, x, m, cost=cost, member=mem)
            aggs[s.key].add(tr)
            if s.key in kept:
                kept[s.key].extend(tr)
                if tr:
                    closes[sym] = (x.day, x.c.astype(np.float32))
        if progress and i % 100 == 0:
            progress(i, len(syms))
    closes["__cal__"] = m.day
    return aggs, kept, closes


# ── plain-language rules (the page and the digest print exactly the tested rule) ──

_ENTRY_TR = {
    "rsi2": "2 günlük RSI {th:g} eşiğinin altına indi (kısa vadede aşırı satış)",
    "cumrsi": "son iki günün 2 günlük RSI toplamı {th:g} eşiğinin altında",
    "crsi": "ConnorsRSI {th:g} eşiğinin altına indi (3 günlük RSI, art arda düşüş serisi ve günlük düşüşün son 100 gün içindeki sırası birlikte; derin kısa vadeli aşırı satış)",
    "ibs": "gün düşüşle kapandı ve kapanış, günün fiyat aralığının alttaki yüzde {pct:g} diliminde",
    "dbl7": "kapanış son 7 günün en düşüğü",
    "down3": "art arda 3 gün düşüş ve 2 günlük RSI {th:g} eşiğinin altında",
}
_EXIT_TR = {
    "sma5": "kapanış 5 günlük ortalamanın üstüne çıkınca",
    "rsi2": "2 günlük RSI 70'in üstüne çıkınca",
    "crsi": "ConnorsRSI 70'in üstüne çıkınca",
    "prevhigh": "kapanış bir önceki günün en yükseğini aşınca",
    "max7": "kapanış son 7 günün en yükseği olunca",
    "first_up": "ilk yükseliş kapanışında",
    "above_entry": "kapanış alış fiyatının (maliyet dahil) üstüne çıkınca",
}
_MACRO_TR = {"none": "", "spy": " S&P 500 de kendi 200 günlük ortalamasının üstündeyken.",
             "vix": " VIX 30'un altındayken.", "credit": " kredi piyasası (HYG/LQD) 50 günlük ortalamasının üstündeyken.",
             "all": " S&P 500 200 günlük ortalamasının üstünde, VIX 30'un altında ve kredi piyasası sakinken."}


def describe(s: Spec) -> tuple[str, str]:
    """(entry, exit) sentences in Turkish for a spec."""
    cond = _ENTRY_TR[s.entry].format(th=s.th, pct=s.th * 100)
    entry = (f"Hisse 200 günlük ortalamasının üstünde (uzun trend yukarı), fiyat ≥ {MIN_PRICE:g} $ ve günlük işlem hacmi ≥ "
             f"{MIN_DVOL / 1e6:g} M$ iken {cond}.{_MACRO_TR[s.macro]} ")
    entry += (f"Ertesi seans için sinyal kapanışının %{s.limit * 100:g} altına limit alış emri; o seansta dolmazsa iptal."
              if s.mode == "limit" else "Ertesi seansın açılışında alış.")
    ex = f"{_EXIT_TR[s.exit][0].upper()}{_EXIT_TR[s.exit][1:]}, ertesi açılışta satış. En fazla {s.hold} seans tutulur; süre dolunca açılışta satış."
    ex += f" Alışın %{s.stop * 100:g} altında zarar kes." if s.stop else " Sabit zarar kes yok."
    return entry, ex
