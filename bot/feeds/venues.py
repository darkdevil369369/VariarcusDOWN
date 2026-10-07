"""Venue feeds. All public, no keys.

Arcus      wss://api.arcus.xyz/v1/ws            l2OrderbookUpdates (snapshot + seq'd deltas)
Variational wss://omni-ws-server.prod.ap-northeast-1.variational.io/prices   mark price ~1/s
Binance    wss://fstream.binance.com            <sym>usdt@bookTicker
Bybit      wss://stream.bybit.com/v5/public/linear   orderbook.1.<SYM>USDT
OKX        wss://ws.okx.com:8443/ws/v5/public  bbo-tbt <SYM>-USDT-SWAP
"""

from __future__ import annotations

import json
import logging

from ..market import now
from .base import Feed

log = logging.getLogger("feed")


class ArcusFeed(Feed):
    """One connection per market keeps sequence handling unambiguous."""

    url = "wss://api.arcus.xyz/v1/ws"
    n_levels = 20

    def __init__(self, markets: dict, symbol: str):
        super().__init__(markets)
        self.symbol = symbol
        self.market_id = f"{symbol}-USD"
        self.name = f"arcus:{symbol}"
        self.ws = None

    def _sub(self, kind: str) -> str:
        msg = {"type": kind, "channel": "l2OrderbookUpdates", "id": self.market_id}
        if kind == "subscribe":
            msg["nLevels"] = self.n_levels
        return json.dumps(msg)

    async def on_open(self, ws) -> None:
        self.ws = ws
        await ws.send(self._sub("subscribe"))

    def on_message(self, msg) -> None:
        if msg.get("channel") != "l2OrderbookUpdates":
            if msg.get("type") == "error":
                self.last_error = str(msg)[:200]
            return
        ident = msg.get("id")
        if ident is not None and str(ident).upper() != self.market_id:
            return
        c = msg.get("contents") or {}
        book = self.markets[self.symbol].arcus
        t = now()
        if msg.get("type") == "subscribed":
            book.snapshot(c["bids"], c["asks"], int(c["lastSequenceId"]), t)
        elif msg.get("type") == "channel_data":
            if not book.update(c["bids"], c["asks"], int(c["lastSequenceId"]), t) and book.health == "RESYNC":
                log.warning("[%s] sequence gap -> resync", self.name)
                self._resync()

    def _resync(self) -> None:
        import asyncio

        ws = self.ws
        if ws is None:
            return

        async def go():
            await ws.send(self._sub("unsubscribe"))
            await ws.send(self._sub("subscribe"))

        asyncio.get_running_loop().create_task(go())

    def on_disconnect(self) -> None:
        self.ws = None
        self.markets[self.symbol].arcus.mark_stale()


class VariationalFeed(Feed):
    name = "variational"
    url = "wss://omni-ws-server.prod.ap-northeast-1.variational.io/prices"

    async def on_open(self, ws) -> None:
        instruments = [
            {
                "underlying": sym,
                "instrument_type": "perpetual_future",
                "settlement_asset": "USDC",
                "funding_interval_s": 3600,
            }
            for sym in self.markets
        ]
        await ws.send(json.dumps({"action": "subscribe", "instruments": instruments}))

    def on_message(self, msg) -> None:
        channel = msg.get("channel") or ""
        if not channel.startswith("instrument_price:"):
            if msg.get("type") not in (None, "heartbeat"):
                self.last_error = str(msg)[:200]
            return
        # instrument_price:P-BTC-USDC-3600
        sym = channel.split(":", 1)[1].split("-")[1]
        market = self.markets.get(sym)
        if market is None:
            return
        price = float(msg["pricing"]["price"])
        if price > 0:
            market.leaders["variational"].set(price, price)


class BinanceFeed(Feed):
    name = "binance"

    def ws_url(self) -> str:
        streams = "/".join(f"{s.lower()}usdt@bookTicker" for s in self.markets)
        return f"wss://fstream.binance.com/stream?streams={streams}"

    def on_message(self, msg) -> None:
        d = msg.get("data") or msg
        sym = d.get("s", "")
        if not sym.endswith("USDT"):
            return
        market = self.markets.get(sym[:-4])
        if market:
            market.leaders["binance"].set(float(d["b"]), float(d["a"]))


class BybitFeed(Feed):
    name = "bybit"
    url = "wss://stream.bybit.com/v5/public/linear"
    ping_text = json.dumps({"op": "ping"})

    async def on_open(self, ws) -> None:
        args = [f"orderbook.1.{s}USDT" for s in self.markets]
        await ws.send(json.dumps({"op": "subscribe", "args": args}))

    def on_message(self, msg) -> None:
        topic = msg.get("topic", "")
        if not topic.startswith("orderbook.1."):
            return
        sym = topic.rsplit(".", 1)[1][:-4]
        market = self.markets.get(sym)
        if market is None:
            return
        q = market.leaders["bybit"]
        d = msg["data"]
        bid, ask = q.bid, q.ask
        if d.get("b") and float(d["b"][0][1]) > 0:
            bid = float(d["b"][0][0])
        if d.get("a") and float(d["a"][0][1]) > 0:
            ask = float(d["a"][0][0])
        if bid > 0 and ask > 0:
            q.set(bid, ask)


class OkxFeed(Feed):
    name = "okx"
    url = "wss://ws.okx.com:8443/ws/v5/public"
    ping_text = "ping"
    ping_every_s = 25.0

    async def on_open(self, ws) -> None:
        args = [{"channel": "bbo-tbt", "instId": f"{s}-USDT-SWAP"} for s in self.markets]
        await ws.send(json.dumps({"op": "subscribe", "args": args}))

    def on_message(self, msg) -> None:
        arg = msg.get("arg") or {}
        if arg.get("channel") != "bbo-tbt" or "data" not in msg:
            return
        market = self.markets.get(arg["instId"].split("-")[0])
        if market is None:
            return
        d = msg["data"][0]
        if d.get("bids") and d.get("asks"):
            market.leaders["okx"].set(float(d["bids"][0][0]), float(d["asks"][0][0]))


LEADER_FEEDS = {
    "variational": VariationalFeed,
    "binance": BinanceFeed,
    "bybit": BybitFeed,
    "okx": OkxFeed,
}
