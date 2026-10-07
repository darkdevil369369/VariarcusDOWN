"""Offline simulator: a synthetic market where Arcus lags the leaders.

Used for `python run.py --sim` (dashboard demo without network) and tests.
It proves the plumbing, not the edge: the lag here is put in by hand.
"""

from __future__ import annotations

import asyncio
import math
import random
from collections import deque

from ..market import now

START = {"BTC": 112_000.0, "ETH": 4_100.0}


class SimFeed:
    name = "sim"

    def __init__(self, markets: dict, arcus_lag_ms: float = 1200, variational_lag_ms: float = 300,
                 seed: int | None = None, tick_ms: float = 50):
        self.markets = markets
        self.arcus_lag = arcus_lag_ms / 1000
        self.var_lag = variational_lag_ms / 1000
        self.tick = tick_ms / 1000
        self.rng = random.Random(seed)
        self.hist = {s: deque(maxlen=2000) for s in markets}
        self.price = {s: START.get(s, 100.0) for s in markets}
        self.seq = {s: 0 for s in markets}
        self.next_var = 0.0
        self.messages = 0

    def status(self) -> dict:
        return {"connected": True, "messages": self.messages, "reconnects": 0,
                "last_error": "", "last_msg_age_s": 0}

    def _price_at(self, sym: str, t: float) -> float:
        for ts, p in reversed(self.hist[sym]):
            if ts <= t:
                return p
        return self.hist[sym][0][1]

    def step(self, t: float) -> None:
        for sym, market in self.markets.items():
            p = self.price[sym]
            ret = self.rng.gauss(0, 0.6e-4)                 # ~0.6 bps per 50 ms
            if self.rng.random() < 0.004:                   # a sharp move every ~12 s
                ret += self.rng.choice((-1, 1)) * self.rng.uniform(8e-4, 20e-4)
            p *= math.exp(ret)
            self.price[sym] = p
            self.hist[sym].append((t, p))
            for name in ("binance", "bybit", "okx"):
                if name in market.leaders:
                    n = p * (1 + self.rng.gauss(0, 0.2e-4))
                    market.leaders[name].set(n - p * 0.5e-4, n + p * 0.5e-4, t)
            if "variational" in market.leaders and t >= self.next_var:
                v = self._price_at(sym, t - self.var_lag) * (1 + 3e-4)   # constant premium
                market.leaders["variational"].set(v, v, t)
            # Arcus: lagged mid, 1 bp spread, 20 levels each side
            mid = self._price_at(sym, t - self.arcus_lag) * (1 - 2e-4)   # constant basis
            half = mid * 0.5e-4
            tick = mid * 0.2e-4
            size = 25_000 / mid
            bids = [(mid - half - i * tick, size * (1 + i * 0.3)) for i in range(20)]
            asks = [(mid + half + i * tick, size * (1 + i * 0.3)) for i in range(20)]
            self.seq[sym] += 1
            market.arcus.snapshot(bids, asks, self.seq[sym], t)
            self.messages += 1
        if t >= self.next_var:
            self.next_var = t + 1.0

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self.step(now())
            await asyncio.sleep(self.tick)
