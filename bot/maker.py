"""Maker-in / maker-out mode: trade the Arcus-vs-leader gap with resting ALO orders only.

fair  = reference leader mid x exp(learned basis)  (the "middle" of the normal gap)
Flat:     bid at fair x (1 - entry_bps), ask at fair x (1 + entry_bps); never crossing the book
In a position: one exit order at fair x (1 +/- exit_bps), never worse than entry +/- min_profit_bps
Stop loss / max hold: cancel quotes, exit with a taker IOC (pays the taker fee).

Paper fill model (queue-aware, from Arcus public prints):
  * a quote goes live rtt/2 after the decision (ALO skips the speed bump) at the BACK
    of its price level: queue ahead = displayed size there at that moment;
  * prints at our price by the other side (taker SELL for our bid, BUY for our ask,
    or unknown side) eat that queue; when they reach us we are filled;
  * a print through our price, or the book moving through it, fills us outright;
  * the queue ahead shrinks with the displayed level (cancels ahead of us).
"""

from __future__ import annotations

import time

from .market import Market, now
from .strategy import Position, Strategy


class MakerStrategy(Strategy):
    def __init__(self, markets: dict[str, Market], cfg, data_dir=None, leadlag=None):
        super().__init__(markets, cfg, data_dir=data_dir, leadlag=leadlag)
        self.mm = cfg.maker_mm
        self.quotes: dict[str, dict[str, dict]] = {s: {} for s in markets}

    # ------------------------------------------------------------------ quotes
    def _ref_fair(self, sym: str, view: dict) -> float | None:
        refs = self.mm.ref_leaders
        fairs = sorted(v["fair"] for n, v in view.items() if v.get("fresh") and (not refs or n in refs))
        if not fairs:
            return None
        n = len(fairs)
        return fairs[n // 2] if n % 2 else (fairs[n // 2 - 1] + fairs[n // 2]) / 2

    def _set_quote(self, sym: str, key: str, px: float, t: float) -> None:
        q = self.quotes[sym].get(key)
        live_after = t + self.cfg.paper.rtt_ms / 2000
        if q is None:
            self.quotes[sym][key] = {"px": None, "live_t": live_after, "next_px": px, "queue": 0.0,
                                     "seen": self.markets[sym].trade_id}
            return
        target = q["next_px"] if q["next_px"] is not None else q["px"]
        if target and abs(px / target - 1) * 1e4 < self.mm.requote_bps:
            return
        q["next_px"], q["live_t"] = px, live_after

    def _active_px(self, sym: str, key: str, t: float) -> float | None:
        q = self.quotes[sym].get(key)
        if q is None:
            return None
        if q["next_px"] is not None and t >= q["live_t"]:
            q["px"], q["next_px"] = q["next_px"], None
            side = self._side(sym, key)
            book = self.markets[sym].arcus
            levels = book.bids if side == "buy" else book.asks
            q["queue"] = levels.get(q["px"], 0.0)          # join the back of the level
            q["seen"] = self.markets[sym].trade_id
        return q["px"]

    def _side(self, sym: str, key: str) -> str:
        if key == "bid":
            return "buy"
        if key == "ask":
            return "sell"
        pos = self.positions.get(sym)
        return "sell" if pos is not None and pos.side > 0 else "buy"

    def _check_fill(self, sym: str, key: str, t: float) -> float | None:
        """Price we got filled at, or None."""
        px = self._active_px(sym, key, t)
        if px is None:
            return None
        q = self.quotes[sym][key]
        side = self._side(sym, key)
        market = self.markets[sym]
        book = market.arcus
        if (side == "buy" and book.best_bid < px) or (side == "sell" and book.best_ask > px):
            return px                                       # book moved through us
        level = (book.bids if side == "buy" else book.asks).get(px, 0.0)
        q["queue"] = min(q["queue"], level)                 # cancels ahead of us
        hit_side = "SELL" if side == "buy" else "BUY"
        if market.trade_id <= q["seen"]:
            return None
        new = []
        for tr in reversed(market.trades):                  # only prints we have not seen yet
            if tr[0] <= q["seen"]:
                break
            new.append(tr)
        q["seen"] = market.trade_id
        for tid, _ts, tpx, tsz, tside in reversed(new):
            if tside not in (None, hit_side):
                continue
            through = tpx < px if side == "buy" else tpx > px
            if through:
                return px
            if abs(tpx - px) <= px * 1e-9:
                q["queue"] -= tsz
                if q["queue"] < 0:
                    return px
        return None

    def _cancel(self, sym: str, *keys: str) -> None:
        for k in keys or list(self.quotes[sym]):
            self.quotes[sym].pop(k, None)

    # ------------------------------------------------------------------ main tick
    def tick(self, t: float | None = None) -> None:
        t = now() if t is None else t
        self._process_pending(t)
        for sym, market in self.markets.items():
            sig = self.signals[sym]
            view = sig.update(t)
            book = market.arcus
            book_ok = book.valid and (t - book.ts) * 1000 <= self.cfg.risk.arcus_stale_ms
            fair = self._ref_fair(sym, view) if book_ok else None
            pos = self.positions.get(sym)
            if pos is not None:
                if not pos.exit_pending:
                    self._manage_position(t, sym, pos, fair, book)
                continue
            if fair is None or t < self.cooldown_until[sym] or any(o.symbol == sym for o in self.pending):
                self._cancel(sym)
                continue
            self._quote_flat(t, sym, fair, book)
        self._mark_equity()

    def _quote_flat(self, t: float, sym: str, fair: float, book) -> None:
        mm = self.mm
        if book.spread_bps > self.cfg.strategy.max_spread_bps or self._risk_block(t):
            self._cancel(sym)
            return
        notional = min(self.cfg.paper.order_notional_usd, self.cfg.paper.max_notional_usd - self.open_notional())
        if notional < 5:
            self._cancel(sym)
            return
        bid = min(fair * (1 - mm.entry_bps / 1e4), book.best_bid)     # never cross: at most join the bid
        ask = max(fair * (1 + mm.entry_bps / 1e4), book.best_ask)
        self._set_quote(sym, "bid", bid, t)
        self._set_quote(sym, "ask", ask, t)
        for key, side in (("bid", "buy"), ("ask", "sell")):
            px = self._check_fill(sym, key, t)
            if px:
                self._open(t, sym, side, px, notional, fair)
                return

    def _open(self, t: float, sym: str, side: str, px: float, notional: float, fair: float) -> None:
        qty = notional / px
        fee = notional * self.cfg.paper.maker_fee_bps / 1e4
        gap = abs(fair / px - 1) * 1e4
        self.positions[sym] = Position(
            symbol=sym, side=1 if side == "buy" else -1, qty=qty, entry_px=px, entry_t=t,
            entry_wall=time.time(), entry_fee=fee, entry_edge_bps=gap, leaders="maker",
        )
        self.stats.signals += 1
        self._cancel(sym)
        self._event(sym, f"MAKER FILL {side.upper()} {qty:.5f} @ {px:.2f} (${notional:.0f}, {gap:.1f}bps from fair)")

    def _manage_position(self, t: float, sym: str, pos: Position, fair: float | None, book) -> None:
        mm = self.mm
        if not book.valid:
            return
        mark = book.best_bid if pos.side > 0 else book.best_ask
        pnl_bps = pos.side * (mark / pos.entry_px - 1) * 1e4
        held_ms = (t - pos.entry_t) * 1000
        if pnl_bps <= -mm.stop_loss_bps or held_ms >= mm.max_hold_ms:
            self._cancel(sym)
            self._send_exit(t, pos, "stop_loss" if pnl_bps <= -mm.stop_loss_bps else "max_hold", book)
            return
        floor = pos.entry_px * (1 + pos.side * mm.min_profit_bps / 1e4)
        if fair is not None:
            target = fair * (1 + pos.side * mm.exit_bps / 1e4)
            px = max(target, floor) if pos.side > 0 else min(target, floor)
        else:
            px = floor
        side = "sell" if pos.side > 0 else "buy"
        if side == "sell" and px <= book.best_bid:      # would cross: join the ask instead
            px = book.best_ask
        if side == "buy" and px >= book.best_ask:
            px = book.best_bid
        self._set_quote(sym, "exit", px, t)
        active = self._check_fill(sym, "exit", t)
        if active:
            self._cancel(sym)
            self._close(t, pos, pos.qty, active, "maker_exit", self.cfg.paper.maker_fee_bps / 1e4)

    def quotes_view(self, sym: str) -> dict:
        t = now()
        out = {}
        for k, q in self.quotes.get(sym, {}).items():
            px = self._active_px(sym, k, t)
            out[k] = None if px is None else {"px": px, "queue": round(q["queue"], 5)}
        return out

    def flatten(self) -> None:
        for sym in self.quotes:
            self._cancel(sym)
        super().flatten()
