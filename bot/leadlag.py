"""Live lead-lag measurement: does Arcus really follow each leader, and how late?

Every `sample_ms` we sample every venue's mid. Two measurements:

1. Event study. A leader "event" is a move >= event_move_bps within 1 s.
   We then watch Arcus: the lag is the time until Arcus has covered
   follow_fraction of that move in the same direction (10 s max, else a miss).
   This is exactly "price drops on Variational first, Arcus a second later".

2. Cross-correlation of 500 ms log returns, leader(t) vs Arcus(t + k),
   k in [-2 s, +4 s]. The k with the highest correlation is the typical lag.
"""

from __future__ import annotations

import math
from collections import deque
from itertools import islice

import numpy as np
from statistics import median

from .market import Market, now

HORIZON_S = 1.0
WATCH_S = 10.0
DEBOUNCE_S = 3.0


class LeadLag:
    def __init__(self, markets: dict[str, Market], cfg):
        self.markets = markets
        self.step = cfg.leadlag.sample_ms / 1000
        self.event_bps = cfg.leadlag.event_move_bps
        self.follow = cfg.leadlag.follow_fraction
        n = int(600 / self.step)                          # 10 minutes of samples
        self.samples = {s: deque(maxlen=n) for s in markets}   # (t, arcus_mid, {leader: mid})
        self.last_event: dict[tuple[str, str], float] = {}
        self.open_events: list[dict] = []
        self.done: dict[tuple[str, str], deque] = {}
        self.xcorr: dict[tuple[str, str], dict] = {}
        self.next_sample = 0.0
        self.next_xcorr = 0.0
        self.cfg = cfg.leadlag
        self.rtt_ms = cfg.paper.rtt_ms
        self.bump_ms = cfg.paper.taker_speed_bump_ms
        self.qualified: dict[tuple[str, str], tuple[bool, str]] = {}
        self.next_qualify = 0.0

    def tick(self, t: float | None = None) -> None:
        t = now() if t is None else t
        if t < self.next_sample:
            return
        self.next_sample = t + self.step
        for sym, m in self.markets.items():
            if not m.arcus.valid:
                continue
            leaders = {n: q.mid for n, q in m.leaders.items() if q.valid and q.age_ms(t) < 3000}
            self.samples[sym].append((t, m.arcus.mid, leaders))
            self._detect(sym, t)
        self._resolve(t)
        if t >= self.next_qualify:
            self.next_qualify = t + 2
            self.qualification(self.rtt_ms, self.bump_ms)
        if t >= self.next_xcorr:
            self.next_xcorr = t + 15
            for sym in self.markets:
                self._xcorr(sym)

    def _value_at(self, buf, t_target: float, leader: str | None):
        """Latest sample value at or before t_target (scan from the end)."""
        for t, a, ls in reversed(buf):
            if t <= t_target:
                return a if leader is None else ls.get(leader)
        return None

    def _detect(self, sym: str, t: float) -> None:
        buf = self.samples[sym]
        _, arcus_now, leaders = buf[-1]
        for name, mid in leaders.items():
            key = (sym, name)
            if t - self.last_event.get(key, -1e9) < DEBOUNCE_S:
                continue
            past = self._value_at(buf, t - HORIZON_S, name)
            if not past:
                continue
            move = (mid / past - 1) * 1e4
            if abs(move) >= self.event_bps:
                arcus_past = self._value_at(buf, t - HORIZON_S, None)
                self.last_event[key] = t
                self.open_events.append({
                    "sym": sym, "leader": name, "t0": t, "move": move,
                    "arcus0": arcus_past or arcus_now, "already": (arcus_now / (arcus_past or arcus_now) - 1) * 1e4,
                })

    def _resolve(self, t: float) -> None:
        keep = []
        for ev in self.open_events:
            buf = self.samples[ev["sym"]]
            target = ev["move"] * self.follow
            lag = None
            tail = []
            for row in reversed(buf):
                if row[0] < ev["t0"]:
                    break
                tail.append(row)
            for ts, a, _ in reversed(tail):
                covered = (a / ev["arcus0"] - 1) * 1e4
                if (target > 0 and covered >= target) or (target < 0 and covered <= target):
                    lag = (ts - ev["t0"]) * 1000
                    break
            if lag is not None or t - ev["t0"] > WATCH_S:
                key = (ev["sym"], ev["leader"])
                self.done.setdefault(key, deque(maxlen=200)).append(
                    {"lag_ms": lag, "move": ev["move"], "t": ev["t0"]})
            else:
                keep.append(ev)
        self.open_events = keep

    def _xcorr(self, sym: str) -> None:
        buf = self.samples[sym]
        n = min(len(buf), int(300 / self.step))          # last 5 minutes
        if n < 300:
            return
        rows = list(islice(buf, len(buf) - n, len(buf)))
        h = max(1, int(round(0.5 / self.step)))
        arcus = np.log([a for _, a, _ in rows])
        ra = arcus[h:] - arcus[:-h]
        names = set().union(*(ls.keys() for _, _, ls in rows))
        for name in names:
            vals, last = [], None
            for _, _, ls in rows:
                last = ls.get(name, last)
                vals.append(last if last else np.nan)
            series = np.log(np.array(vals, dtype=float))
            if np.isnan(series).mean() > 0.2:
                continue
            series = _ffill(series)
            rl = series[h:] - series[:-h]
            best_k, best_c, curve = 0, -2.0, []
            for k in range(int(-2 / self.step), int(4 / self.step) + 1, max(1, int(0.1 / self.step))):
                c = _corr(rl, ra, k)
                curve.append((round(k * self.step * 1000), None if c is None else round(c, 3)))
                if c is not None and c > best_c:
                    best_k, best_c = k, c
            if best_c > -2:
                self.xcorr[(sym, name)] = {"lag_ms": round(best_k * self.step * 1000),
                                           "corr": round(best_c, 3), "curve": curve}

    def qualification(self, rtt_ms: float, bump_ms: float) -> dict[tuple[str, str], tuple[bool, str]]:
        """Which (symbol, leader) pairs have proven Arcus follows them late enough to trade.

        Both tests must pass: event-study median lag >= rtt + bump + margin, and the
        5-minute return cross-correlation peaks at a lag >= rtt + bump.
        """
        need = rtt_ms + bump_ms + self.cfg.qualify_margin_ms
        out = {}
        for row in self.summary():
            key = (row["symbol"], row["leader"])
            if not self.cfg.qualify:
                out[key] = (True, "qualification off")
            elif row["events"] < self.cfg.qualify_min_events:
                out[key] = (False, f"{row['events']}/{self.cfg.qualify_min_events} events")
            elif (row["followed_pct"] or 0) < self.cfg.qualify_min_follow_pct:
                out[key] = (False, f"Arcus followed only {row['followed_pct']}%")
            elif (row["median_lag_ms"] or 0) < need:
                out[key] = (False, f"lag {row['median_lag_ms']}ms < {need:.0f}ms")
            elif row["xcorr_lag_ms"] is None or row["xcorr_lag_ms"] < rtt_ms + bump_ms or (row["xcorr"] or 0) < 0.2:
                out[key] = (False, f"xcorr lag {row['xcorr_lag_ms']}ms / corr {row['xcorr']} too weak")
            else:
                out[key] = (True, f"lag {row['median_lag_ms']}ms >= {need:.0f}ms")
        self.qualified = out
        return out

    def is_qualified(self, sym: str, leader: str) -> bool:
        if not self.cfg.qualify:
            return True
        return self.qualified.get((sym, leader), (False, ""))[0]

    def summary(self) -> list[dict]:
        rows = []
        keys = set(self.done) | set(self.xcorr)
        for sym, name in sorted(keys):
            evs = list(self.done.get((sym, name), []))
            lags = [e["lag_ms"] for e in evs if e["lag_ms"] is not None]
            xc = self.xcorr.get((sym, name), {})
            rows.append({
                "symbol": sym, "leader": name, "events": len(evs),
                "followed_pct": round(100 * len(lags) / len(evs)) if evs else None,
                "median_lag_ms": round(median(lags)) if lags else None,
                "p25_lag_ms": round(sorted(lags)[len(lags) // 4]) if lags else None,
                "xcorr_lag_ms": xc.get("lag_ms"), "xcorr": xc.get("corr"),
                "qualified": self.qualified.get((sym, name), (False, "collecting"))[0],
                "why": self.qualified.get((sym, name), (False, "collecting"))[1],
            })
        return rows


def _ffill(x: np.ndarray) -> np.ndarray:
    idx = np.where(~np.isnan(x), np.arange(len(x)), 0)
    np.maximum.accumulate(idx, out=idx)
    out = x[idx]
    first = np.flatnonzero(~np.isnan(out))
    if len(first):
        out[: first[0]] = out[first[0]]
    return out


def _corr(x: np.ndarray, y: np.ndarray, k: int) -> float | None:
    """corr(x[i], y[i+k])."""
    if k >= 0:
        a, b = x[: len(x) - k], y[k:]
    else:
        a, b = x[-k:], y[: len(y) + k]
    n = min(len(a), len(b))
    if n < 50:
        return None
    a, b = a[:n] - a[:n].mean(), b[:n] - b[:n].mean()
    den = math.sqrt(float((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / den) if den > 0 else None
