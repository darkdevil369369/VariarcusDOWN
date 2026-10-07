"""Reconnecting websocket feed base."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

log = logging.getLogger("feed")

USER_AGENT = "Mozilla/5.0 (VariarcusDOWN paper bot)"


class Feed:
    name = "feed"
    url = ""
    ping_text: str | None = None    # app-level ping payload, if the venue wants one
    ping_every_s = 20.0

    def __init__(self, markets: dict):
        self.markets = markets           # symbol -> Market
        self.connected = False
        self.messages = 0
        self.reconnects = 0
        self.last_error = ""
        self.last_msg_wall = 0.0

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "messages": self.messages,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
            "last_msg_age_s": round(time.time() - self.last_msg_wall, 1) if self.last_msg_wall else None,
        }

    def ws_url(self) -> str:
        return self.url

    async def on_open(self, ws) -> None:  # subscribe here
        pass

    def on_message(self, msg) -> None:
        raise NotImplementedError

    def on_disconnect(self) -> None:
        pass

    async def _pinger(self, ws) -> None:
        while True:
            await asyncio.sleep(self.ping_every_s)
            await ws.send(self.ping_text)

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            pinger = None
            try:
                async with websockets.connect(
                    self.ws_url(),
                    max_size=2**23,
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    user_agent_header=USER_AGENT,
                    compression=None,
                ) as ws:
                    self.connected = True
                    self.last_error = ""
                    log.info("[%s] connected", self.name)
                    await self.on_open(ws)
                    if self.ping_text:
                        pinger = asyncio.create_task(self._pinger(ws))
                    async for raw in ws:
                        backoff = 1.0
                        self.messages += 1
                        self.last_msg_wall = time.time()
                        if raw == "pong":
                            continue
                        try:
                            self.on_message(json.loads(raw))
                        except (ValueError, KeyError, TypeError, IndexError) as exc:
                            log.debug("[%s] bad frame %s: %s", self.name, exc, str(raw)[:200])
                        if stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network errors of every flavour
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("[%s] %s; reconnect in %.0fs", self.name, self.last_error, backoff)
            finally:
                if pinger:
                    pinger.cancel()
                self.connected = False
                self.on_disconnect()
            if stop.is_set():
                break
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
