"""Shared market state: leader quotes and the Arcus L2 book."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


def now() -> float:
    """Monotonic seconds; all strategy timing uses this clock."""
    return time.monotonic()


@dataclass
class Quote:
    """Top-of-book (or a single mark price, bid == ask) from one venue."""

    bid: float = 0.0
    ask: float = 0.0
    ts: float = 0.0          # local monotonic receive time
    updates: int = 0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def valid(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid

    def set(self, bid: float, ask: float, ts: float | None = None) -> None:
        self.bid, self.ask = bid, ask
        self.ts = now() if ts is None else ts
        self.updates += 1

    def age_ms(self, t: float | None = None) -> float:
        return ((now() if t is None else t) - self.ts) * 1000.0


@dataclass
class ArcusBook:
    """Local Arcus L2 book kept in sync by per-market lastSequenceId."""

    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    seq: int | None = None
    health: str = "STALE"     # OK | RESYNC | STALE
    ts: float = 0.0
    updates: int = 0
    gaps: int = 0

    def snapshot(self, bids, asks, seq: int, ts: float | None = None) -> None:
        self.bids = {float(p): float(s) for p, s in bids if float(s) > 0}
        self.asks = {float(p): float(s) for p, s in asks if float(s) > 0}
        self.seq = seq
        self.health = "OK"
        self._touch(ts)

    def update(self, bids, asks, seq: int, ts: float | None = None) -> bool:
        """Apply a delta. Returns False when the book needs a fresh snapshot."""
        if self.health != "OK" or self.seq is None:
            return False
        if seq <= self.seq:
            return True        # duplicate / old frame
        if seq != self.seq + 1:
            self.gaps += 1
            self.health = "RESYNC"
            return False
        for side, levels in ((self.bids, bids), (self.asks, asks)):
            for p, s in levels:
                price, size = float(p), float(s)
                if size > 0:
                    side[price] = size
                else:
                    side.pop(price, None)
        self.seq = seq
        self._touch(ts)
        return True

    def _touch(self, ts: float | None) -> None:
        self.ts = now() if ts is None else ts
        self.updates += 1

    def mark_stale(self) -> None:
        self.health = "STALE"

    @property
    def best_bid(self) -> float:
        return max(self.bids) if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return min(self.asks) if self.asks else 0.0

    @property
    def valid(self) -> bool:
        bb, ba = self.best_bid, self.best_ask
        return self.health == "OK" and bb > 0 and ba > 0 and ba > bb

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread_bps(self) -> float:
        m = self.mid
        return (self.best_ask - self.best_bid) / m * 1e4 if m > 0 else float("inf")

    def walk(self, side: str, notional: float, limit: float) -> tuple[float, float]:
        """Fill `notional` USD as a taker up to `limit`. Returns (qty, vwap).

        side='buy' lifts asks, side='sell' hits bids. Partial fills allowed (IOC).
        """
        if side == "buy":
            levels = sorted(self.asks.items())
            ok = lambda p: p <= limit  # noqa: E731
        else:
            levels = sorted(self.bids.items(), reverse=True)
            ok = lambda p: p >= limit  # noqa: E731
        qty = cost = 0.0
        remaining = notional
        for price, size in levels:
            if not ok(price) or remaining <= 1e-9:
                break
            take = min(size, remaining / price)
            qty += take
            cost += take * price
            remaining -= take * price
        return (qty, cost / qty) if qty > 0 else (0.0, 0.0)

    def walk_qty(self, side: str, qty: float, limit: float) -> tuple[float, float]:
        """Fill a base quantity (used for exits). Returns (filled_qty, vwap)."""
        if side == "buy":
            levels = sorted(self.asks.items())
            ok = lambda p: p <= limit  # noqa: E731
        else:
            levels = sorted(self.bids.items(), reverse=True)
            ok = lambda p: p >= limit  # noqa: E731
        filled = cost = 0.0
        for price, size in levels:
            if not ok(price) or qty - filled <= 1e-12:
                break
            take = min(size, qty - filled)
            filled += take
            cost += take * price
        return (filled, cost / filled) if filled > 0 else (0.0, 0.0)

    def vwap_for(self, side: str, notional: float) -> float:
        """Price you would pay for `notional` with no limit (0 if book too thin)."""
        limit = float("inf") if side == "buy" else 0.0
        qty, px = self.walk(side, notional, limit)
        return px if qty * px >= notional * 0.999 else 0.0

    def top(self, n: int = 10) -> dict:
        return {
            "bids": sorted(self.bids.items(), reverse=True)[:n],
            "asks": sorted(self.asks.items())[:n],
        }


class Market:
    """Everything known about one symbol."""

    def __init__(self, symbol: str, leader_names: list[str]):
        self.symbol = symbol
        self.arcus = ArcusBook()
        self.leaders: dict[str, Quote] = {name: Quote() for name in leader_names}
