"""Lead-lag sniper with a latency-honest paper executor.

Entry (long; short is the mirror). Only leaders the live lead-lag analyzer has
qualified (Arcus provably follows them later than rtt + speed bump) may trigger:
  * a fresh leader moved >= min_leader_move_bps up within momentum_window_ms
  * its fair value for Arcus is above Arcus's executable ask (VWAP for our size)
    by at least  2 x taker fee + half spread + min_net_edge_bps
  * >= min_leaders_agree leaders say so, and (optionally) none says the opposite
  * Arcus book healthy, spread <= max_spread_bps, risk limits OK, not in cooldown
Exit, first that hits:
  * converged: composite fair - Arcus bid <= exit_edge_bps     (edge harvested)
  * take profit / stop loss on mark-to-bid PnL
  * max_hold_ms
Paper fills: the IOC is matched against the Arcus book as it looks
rtt_ms + speed bump AFTER the decision (our view is rtt/2 old, the order needs
rtt/2 to arrive, then Arcus holds takers 50 ms). Slippage cap applies; partial
fills are kept. Fees are charged on every fill.
"""

from __future__ import annotations

import csv
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import ROOT
from .market import Market, now
from .signals import SymbolSignals

log = logging.getLogger("strategy")


@dataclass
class Position:
    symbol: str
    side: int                     # +1 long, -1 short
    qty: float
    entry_px: float
    entry_t: float
    entry_wall: float
    entry_fee: float
    entry_edge_bps: float
    leaders: str
    exit_pending: bool = False
    tp_px: float = 0.0            # resting maker (ALO) exit price, 0 = none
    tp_live_t: float = 0.0        # when that order is resting on the book

    @property
    def notional(self) -> float:
        return self.qty * self.entry_px


@dataclass
class PendingOrder:
    symbol: str
    side: str                     # buy | sell
    arrive_t: float
    limit: float
    notional: float = 0.0         # entries size by notional
    qty: float = 0.0              # exits size by quantity
    kind: str = "entry"           # entry | exit
    reason: str = ""
    edge_bps: float = 0.0
    leaders: str = ""
    decided_px: float = 0.0


@dataclass
class Trade:
    symbol: str
    side: str
    entry_time: str
    exit_time: str
    qty: float
    entry_px: float
    exit_px: float
    notional: float
    gross_usd: float
    fees_usd: float
    net_usd: float
    net_bps: float
    hold_ms: int
    entry_edge_bps: float
    exit_reason: str
    leaders: str


@dataclass
class Stats:
    equity: float = 0.0
    realized: float = 0.0
    fees: float = 0.0
    wins: int = 0
    losses: int = 0
    signals: int = 0
    missed_fills: int = 0
    partial_fills: int = 0
    consecutive_losses: int = 0
    paused_until: float = 0.0
    day: str = ""
    day_pnl: float = 0.0
    trade_times: list = field(default_factory=list)
    halted_reason: str = ""


def _wall(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts or time.time()))


class Strategy:
    def __init__(self, markets: dict[str, Market], cfg, data_dir: Path | None = None, leadlag=None):
        self.cfg = cfg
        self.leadlag = leadlag      # LeadLag; when set, only qualified leaders may trigger
        self.markets = markets
        self.signals = {s: SymbolSignals(m, cfg) for s, m in markets.items()}
        self.positions: dict[str, Position] = {}
        self.pending: list[PendingOrder] = []
        self.cooldown_until: dict[str, float] = {s: 0.0 for s in markets}
        self.trades: list[Trade] = []
        self.events: list[dict] = []
        self.stats = Stats(equity=cfg.paper.capital_usd, day=time.strftime("%Y-%m-%d", time.gmtime()))
        self.paused = False
        self.equity_curve: list[tuple[float, float]] = [(time.time(), cfg.paper.capital_usd)]
        self.data_dir = data_dir or ROOT / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.trades_csv = self.data_dir / "trades.csv"
        self.diag: dict[str, dict] = {}
        self.diag_prev: dict[str, dict] = {}

    # ------------------------------------------------------------------ helpers
    def _diag(self, sym: str, leader: str, edge: float, need: float, mom: float) -> None:
        """Largest tradeable gap seen per symbol in the current hour, to explain no-trade hours."""
        hour = int(time.time() // 3600)
        d = self.diag.get(sym)
        if d is None or d["hour"] != hour:
            if d is not None:
                self.diag_prev[sym] = d
            d = self.diag[sym] = {"hour": hour, "best_edge": -99.0, "need": need, "leader": "",
                                  "momentum": 0.0, "over_need": 0}
        if edge > d["best_edge"]:
            d.update(best_edge=round(edge, 2), need=round(need, 2), leader=leader, momentum=round(mom, 2))
        if edge >= need:
            d["over_need"] += 1

    def _event(self, symbol: str, text: str) -> None:
        self.events.append({"t": time.time(), "symbol": symbol, "text": text})
        del self.events[:-200]
        log.info("[%s] %s", symbol, text)

    def open_notional(self) -> float:
        pos = sum(p.notional for p in self.positions.values())
        return pos + sum(o.notional for o in self.pending if o.kind == "entry")

    def _risk_block(self, t: float) -> str:
        r = self.cfg.risk
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today != self.stats.day:
            self.stats.day, self.stats.day_pnl, self.stats.halted_reason = today, 0.0, ""
        if self.paused:
            return "paused by user"
        if self.stats.day_pnl <= -r.daily_loss_limit_usd:
            self.stats.halted_reason = "daily loss limit"
            return "daily loss limit"
        if t < self.stats.paused_until:
            return "loss-streak pause"
        hour_ago = time.time() - 3600
        self.stats.trade_times = [x for x in self.stats.trade_times if x > hour_ago]
        if len(self.stats.trade_times) >= r.max_trades_per_hour:
            return "max trades/hour"
        return ""

    # ------------------------------------------------------------------ main tick
    def tick(self, t: float | None = None) -> None:
        t = now() if t is None else t
        self._process_pending(t)
        for sym, market in self.markets.items():
            sig = self.signals[sym]
            view = sig.update(t)
            book = market.arcus
            book_ok = book.valid and (t - book.ts) * 1000 <= self.cfg.risk.arcus_stale_ms
            pos = self.positions.get(sym)
            if pos is not None:
                if not pos.exit_pending and book_ok:
                    self._check_exit(t, pos, sig, market)
                continue
            if not book_ok or any(o.symbol == sym for o in self.pending):
                continue
            if t < self.cooldown_until[sym]:
                continue
            self._check_entry(t, sym, market, view)
        self._mark_equity()

    def _check_entry(self, t: float, sym: str, market: Market, view: dict) -> None:
        s, p = self.cfg.strategy, self.cfg.paper
        book = market.arcus
        spread = book.spread_bps
        if spread > s.max_spread_bps:
            return
        size = p.order_notional_usd
        ask_exec = book.vwap_for("buy", size)
        bid_exec = book.vwap_for("sell", size)
        if not ask_exec or not bid_exec:
            return
        if p.exit_mode == "maker":
            # taker in, resting ALO out at entry +/- (fee + min profit): no exit fee, no spread crossed
            need = p.taker_fee_bps + s.min_net_edge_bps + s.maker_exit_buffer_bps
        else:
            need = 2 * p.taker_fee_bps + spread / 2 + s.min_net_edge_bps
        longs, shorts = [], []
        for name, v in view.items():
            if not v.get("fresh") or v["age_ms"] > self.cfg.leaders[name]["trigger_age_ms"]:
                continue
            long_edge = (v["fair"] / ask_exec - 1) * 1e4
            short_edge = (bid_exec / v["fair"] - 1) * 1e4
            self._diag(sym, name, max(long_edge, short_edge), need, v["momentum_bps"])
            if self.leadlag is not None and not self.leadlag.is_qualified(sym, name):
                continue
            if long_edge >= need and v["momentum_bps"] >= s.min_leader_move_bps:
                longs.append((name, long_edge))
            if short_edge >= need and v["momentum_bps"] <= -s.min_leader_move_bps:
                shorts.append((name, short_edge))
        side = None
        if len(longs) >= s.min_leaders_agree and s.direction in ("both", "long"):
            if not (s.veto_on_disagreement and shorts):
                side, hits = "buy", longs
        if side is None and len(shorts) >= s.min_leaders_agree and s.direction in ("both", "short"):
            if not (s.veto_on_disagreement and longs):
                side, hits = "sell", shorts
        if side is None:
            return
        self.stats.signals += 1
        block = self._risk_block(t)
        if block:
            return
        room = p.max_notional_usd - self.open_notional()
        notional = min(size, room)
        if notional < 5:     # Arcus minimum order notional
            return
        edge = min(e for _, e in hits)
        touch = book.best_ask if side == "buy" else book.best_bid
        slip = p.entry_max_slippage_bps / 1e4
        limit = touch * (1 + slip) if side == "buy" else touch * (1 - slip)
        leaders = ",".join(f"{n}:{e:.1f}" for n, e in hits)
        self.pending.append(PendingOrder(
            symbol=sym, side=side, arrive_t=t + (p.rtt_ms + p.taker_speed_bump_ms) / 1000,
            limit=limit, notional=notional, kind="entry", edge_bps=edge, leaders=leaders,
            decided_px=touch,
        ))
        self._event(sym, f"SIGNAL {side.upper()} edge {edge:.1f}bps (need {need:.1f}) via {leaders}")

    def _check_exit(self, t: float, pos: Position, sig: SymbolSignals, market: Market) -> None:
        s = self.cfg.strategy
        book = market.arcus
        mark = book.best_bid if pos.side > 0 else book.best_ask
        pnl_bps = pos.side * (mark / pos.entry_px - 1) * 1e4
        if pos.tp_px and t >= pos.tp_live_t:
            # resting ALO: long sells at tp (filled once a bid reaches it), short buys at tp
            touched = book.best_bid >= pos.tp_px if pos.side > 0 else book.best_ask <= pos.tp_px
            if touched:
                self._close(t, pos, pos.qty, pos.tp_px, "maker_tp", self.cfg.paper.maker_fee_bps / 1e4)
                return
        fair = sig.composite_fair()
        reason = ""
        if fair is not None and not pos.tp_px:
            remaining = pos.side * (fair / mark - 1) * 1e4
            if remaining <= s.exit_edge_bps:
                reason = "converged"
        if pnl_bps >= s.take_profit_bps and not pos.tp_px:
            reason = "take_profit"
        elif pnl_bps <= -s.stop_loss_bps:
            reason = "stop_loss"
        elif (t - pos.entry_t) * 1000 >= s.max_hold_ms:
            reason = reason or "max_hold"
        if reason:
            pos.tp_px = 0.0           # cancel the resting maker exit, then exit as taker
            self._send_exit(t, pos, reason, book)

    def _place_maker_tp(self, t: float, pos: Position, book) -> None:
        p, s = self.cfg.paper, self.cfg.strategy
        if p.exit_mode != "maker":
            return
        # aim for most of the gap the leader showed, never less than fee + min profit
        dist = max(p.taker_fee_bps + s.min_net_edge_bps, s.maker_tp_capture * pos.entry_edge_bps) / 1e4
        tp = pos.entry_px * (1 + dist) if pos.side > 0 else pos.entry_px * (1 - dist)
        crosses = tp <= book.best_bid if pos.side > 0 else tp >= book.best_ask
        if crosses:
            # ALO would be rejected (already through the book): take it as a taker instead
            self._send_exit(t, pos, "tp_cross", book)
            return
        pos.tp_px = tp
        pos.tp_live_t = t + p.rtt_ms / 2000   # ALO skips the speed bump; half RTT to arrive
        self._event(pos.symbol, f"MAKER EXIT resting @ {tp:.2f}")

    def _send_exit(self, t: float, pos: Position, reason: str, book) -> None:
        p = self.cfg.paper
        side = "sell" if pos.side > 0 else "buy"
        touch = book.best_bid if side == "sell" else book.best_ask
        slip = p.exit_max_slippage_bps / 1e4
        limit = touch * (1 - slip) if side == "sell" else touch * (1 + slip)
        pos.exit_pending = True
        self.pending.append(PendingOrder(
            symbol=pos.symbol, side=side, arrive_t=t + (p.rtt_ms + p.taker_speed_bump_ms) / 1000,
            limit=limit, qty=pos.qty, kind="exit", reason=reason, decided_px=touch,
        ))

    # ------------------------------------------------------------------ paper matching
    def _process_pending(self, t: float) -> None:
        due = [o for o in self.pending if o.arrive_t <= t]
        if not due:
            return
        self.pending = [o for o in self.pending if o.arrive_t > t]
        fee_rate = self.cfg.paper.taker_fee_bps / 1e4
        for o in due:
            book = self.markets[o.symbol].arcus
            if o.kind == "entry":
                qty, px = book.walk(o.side, o.notional, o.limit) if book.valid else (0.0, 0.0)
                if qty <= 0 or qty * px < 5:
                    self.stats.missed_fills += 1
                    self._event(o.symbol, f"MISSED {o.side.upper()} — book moved past limit {o.limit:.2f}")
                    self.cooldown_until[o.symbol] = t + self.cfg.strategy.cooldown_ms / 1000
                    continue
                if qty * px < o.notional * 0.98:
                    self.stats.partial_fills += 1
                fee = qty * px * fee_rate
                self.positions[o.symbol] = Position(
                    symbol=o.symbol, side=1 if o.side == "buy" else -1, qty=qty, entry_px=px,
                    entry_t=t, entry_wall=time.time(), entry_fee=fee, entry_edge_bps=o.edge_bps,
                    leaders=o.leaders,
                )
                self._place_maker_tp(t, self.positions[o.symbol], book)
                slip = (px / o.decided_px - 1) * 1e4 * (1 if o.side == "buy" else -1)
                self._event(o.symbol, f"FILLED {o.side.upper()} {qty:.5f} @ {px:.2f} (${qty*px:.0f}, slip {slip:+.1f}bps)")
            else:
                pos = self.positions.get(o.symbol)
                if pos is None:
                    continue
                qty, px = book.walk_qty(o.side, o.qty, o.limit) if book.valid else (0.0, 0.0)
                if qty <= 0:
                    pos.exit_pending = False      # retry next tick
                    self._event(o.symbol, f"EXIT RETRY ({o.reason}) — no liquidity inside limit")
                    continue
                self._close(t, pos, qty, px, o.reason, fee_rate)

    def _close(self, t: float, pos: Position, qty: float, px: float, reason: str, fee_rate: float) -> None:
        frac = qty / pos.qty
        entry_fee = pos.entry_fee * frac
        exit_fee = qty * px * fee_rate
        gross = pos.side * (px - pos.entry_px) * qty
        net = gross - entry_fee - exit_fee
        notional = qty * pos.entry_px
        trade = Trade(
            symbol=pos.symbol, side="LONG" if pos.side > 0 else "SHORT",
            entry_time=_wall(pos.entry_wall), exit_time=_wall(), qty=round(qty, 6),
            entry_px=round(pos.entry_px, 4), exit_px=round(px, 4), notional=round(notional, 2),
            gross_usd=round(gross, 4), fees_usd=round(entry_fee + exit_fee, 4), net_usd=round(net, 4),
            net_bps=round(net / notional * 1e4, 2), hold_ms=int((t - pos.entry_t) * 1000),
            entry_edge_bps=round(pos.entry_edge_bps, 2), exit_reason=reason, leaders=pos.leaders,
        )
        self.trades.append(trade)
        self._append_csv(trade)
        st = self.stats
        st.realized += net
        st.day_pnl += net
        st.fees += entry_fee + exit_fee
        st.trade_times.append(time.time())
        if net > 0:
            st.wins += 1
            st.consecutive_losses = 0
        else:
            st.losses += 1
            st.consecutive_losses += 1
            if st.consecutive_losses >= self.cfg.risk.max_consecutive_losses:
                st.paused_until = t + 300
                st.consecutive_losses = 0
                self._event(pos.symbol, "PAUSE 5 min after loss streak")
        self._event(pos.symbol, f"CLOSED {trade.side} {reason} net ${net:+.3f} ({trade.net_bps:+.1f}bps, {trade.hold_ms}ms)")
        remaining = pos.qty - qty
        if remaining * px < 1.0:
            del self.positions[pos.symbol]
            self.cooldown_until[pos.symbol] = t + self.cfg.strategy.cooldown_ms / 1000
        else:
            pos.qty = remaining
            pos.entry_fee -= entry_fee
            pos.exit_pending = False

    def _append_csv(self, trade: Trade) -> None:
        new = not self.trades_csv.exists()
        with self.trades_csv.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(asdict(trade)))
            if new:
                w.writeheader()
            w.writerow(asdict(trade))

    # ------------------------------------------------------------------ accounting
    def unrealized(self) -> float:
        total = 0.0
        for pos in self.positions.values():
            book = self.markets[pos.symbol].arcus
            if not book.valid:
                continue
            mark = book.best_bid if pos.side > 0 else book.best_ask
            total += pos.side * (mark - pos.entry_px) * pos.qty - pos.entry_fee
        return total

    def _mark_equity(self) -> None:
        eq = self.cfg.paper.capital_usd + self.stats.realized + self.unrealized()
        self.stats.equity = eq
        wall = time.time()
        if wall - self.equity_curve[-1][0] >= 1.0:
            self.equity_curve.append((wall, eq))
            del self.equity_curve[:-3600]

    def flatten(self) -> None:
        t = now()
        for pos in list(self.positions.values()):
            if not pos.exit_pending:
                pos.tp_px = 0.0
                self._send_exit(t, pos, "manual_flatten", self.markets[pos.symbol].arcus)
