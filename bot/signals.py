"""Fair value of Arcus from each leader, plus leader momentum.

fair_L = leader_mid * exp(basis_L), where basis_L is a slow time-weighted EMA of
ln(arcus_mid / leader_mid). Each venue has its own persistent premium, so the
basis is learned and the edge is measured against it.

The EMA is frozen while the leader is moving fast, so a lead move is not
absorbed into the basis before Arcus has caught up.
"""

from __future__ import annotations

import math
from collections import deque

from .market import Market, now


class LeaderSignal:
    def __init__(self, tau_s: float, momentum_window_s: float):
        self.tau = tau_s
        self.window = momentum_window_s
        self.basis: float | None = None
        self.basis_time = 0.0          # seconds of data in the basis
        self.last_t: float | None = None
        self.hist: deque[tuple[float, float]] = deque()
        self.last_quote_ts = -1.0

    def momentum_bps(self, t: float, mid: float) -> float:
        """Leader move over the momentum window, in bps (oldest point inside the window)."""
        while len(self.hist) > 1 and self.hist[1][0] <= t - self.window:
            self.hist.popleft()
        if not self.hist:
            return 0.0
        return (mid / self.hist[0][1] - 1.0) * 1e4

    def update(self, t: float, leader_mid: float, quote_ts: float, arcus_mid: float | None,
               freeze_bps: float) -> float:
        if quote_ts != self.last_quote_ts:
            self.hist.append((t, leader_mid))
            self.last_quote_ts = quote_ts
        mom = self.momentum_bps(t, leader_mid)
        if arcus_mid and self.last_t is not None and abs(mom) < freeze_bps:
            dt = t - self.last_t
            x = math.log(arcus_mid / leader_mid)
            if self.basis is None:
                self.basis = x
            else:
                a = 1.0 - math.exp(-dt / self.tau)
                self.basis += a * (x - self.basis)
            self.basis_time += dt
        self.last_t = t
        return mom

    def fair(self, leader_mid: float) -> float | None:
        return None if self.basis is None else leader_mid * math.exp(self.basis)


class SymbolSignals:
    def __init__(self, market: Market, cfg):
        self.market = market
        self.cfg = cfg
        s = cfg.strategy
        self.leaders = {
            name: LeaderSignal(s.basis_tau_s, s.momentum_window_ms / 1000)
            for name in market.leaders
        }
        self.view: dict[str, dict] = {}

    def update(self, t: float | None = None) -> dict[str, dict]:
        """Refresh per-leader view: fresh?, mid, fair, momentum, edges."""
        t = now() if t is None else t
        s = self.cfg.strategy
        book = self.market.arcus
        arcus_ok = book.valid
        arcus_mid = book.mid if arcus_ok else None
        out: dict[str, dict] = {}
        for name, q in self.market.leaders.items():
            sig = self.leaders[name]
            max_age = self.cfg.leaders[name]["max_age_ms"]
            if not q.valid:
                out[name] = {"fresh": False}
                continue
            mom = sig.update(t, q.mid, q.ts, arcus_mid, s.min_leader_move_bps)
            fair = sig.fair(q.mid)
            fresh = q.age_ms(t) <= max_age and fair is not None and sig.basis_time >= s.basis_warmup_s
            out[name] = {
                "fresh": fresh,
                "mid": q.mid,
                "age_ms": q.age_ms(t),
                "fair": fair,
                "basis_bps": None if sig.basis is None else sig.basis * 1e4,
                "momentum_bps": mom,
            }
        self.view = out
        return out

    def composite_fair(self) -> float | None:
        fairs = sorted(v["fair"] for v in self.view.values() if v.get("fresh"))
        if not fairs:
            return None
        n = len(fairs)
        return fairs[n // 2] if n % 2 else (fairs[n // 2 - 1] + fairs[n // 2]) / 2
