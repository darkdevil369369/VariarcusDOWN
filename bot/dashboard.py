"""Local web dashboard: http://127.0.0.1:8787 — state pushed over a websocket at 4 Hz."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import asdict
from pathlib import Path

from aiohttp import WSMsgType, web

log = logging.getLogger("dashboard")
STATIC = Path(__file__).parent / "static"


class Dashboard:
    def __init__(self, app_state, cfg):
        self.s = app_state          # object with .markets .strategy .leadlag .feeds .started .sim
        self.cfg = cfg
        self.clients: set[web.WebSocketResponse] = set()
        self.runner: web.AppRunner | None = None
        # Required when the dashboard is reachable from the internet (host 0.0.0.0).
        self.token = os.environ.get("VARIARCUS_TOKEN") or cfg.dashboard.get("token") or ""
        # Basic auth "user:pass", same login as the other dashboards on the server
        # (CROSSEDGE_DASH_AUTH in ~/.crossedge_dash.env).
        self.basic = ""
        if cfg.dashboard.get("auth", True):
            self.basic = os.environ.get("CROSSEDGE_DASH_AUTH") or _read_env_key(
                Path.home() / ".crossedge_dash.env", "CROSSEDGE_DASH_AUTH")
        self.basic_header = "Basic " + base64.b64encode(self.basic.encode()).decode() if self.basic else ""
        self.session = hashlib.sha256(f"variarcus:{self.basic}:{self.token}".encode()).hexdigest()

    @web.middleware
    async def auth(self, request, handler):
        if not self.token and not self.basic:
            return await handler(request)
        if hmac.compare_digest(request.cookies.get("vsess", ""), self.session):
            return await handler(request)
        ok = False
        if self.basic and hmac.compare_digest(request.headers.get("Authorization", ""), self.basic_header):
            ok = True
        if self.token and hmac.compare_digest(request.query.get("token", ""), self.token):
            ok = True
        if not ok:
            headers = {"WWW-Authenticate": 'Basic realm="variarcus"'} if self.basic else {}
            return web.Response(status=401, text="401 unauthorized\n", headers=headers)
        resp = await handler(request)
        if isinstance(resp, web.StreamResponse) and not resp.prepared:
            resp.set_cookie("vsess", self.session, httponly=True, samesite="Strict", max_age=30 * 86400)
        return resp

    def snapshot(self) -> dict:
        st = self.s.strategy
        stats = st.stats
        markets = {}
        for sym, m in self.s.markets.items():
            book = m.arcus
            sig = st.signals[sym]
            pos = st.positions.get(sym)
            mark = None
            if pos and book.valid:
                mark = book.best_bid if pos.side > 0 else book.best_ask
            fair = sig.composite_fair()
            markets[sym] = {
                "arcus": {
                    "bid": book.best_bid, "ask": book.best_ask,
                    "mid": book.mid if book.valid else None,
                    "spread_bps": round(book.spread_bps, 2) if book.valid else None,
                    "health": book.health, "age_ms": round((time.monotonic() - book.ts) * 1000) if book.ts else None,
                    "updates": book.updates, "gaps": book.gaps, "top": book.top(8),
                },
                "leaders": {n: _clean(v) for n, v in sig.view.items()},
                "fair": fair,
                "gap_bps": round((fair / book.mid - 1) * 1e4, 2) if fair and book.valid else None,
                "best_gap": st.diag.get(sym),
                "quotes": st.quotes_view(sym) if hasattr(st, "quotes_view") else None,
                "cooldown_s": max(0.0, round(st.cooldown_until[sym] - time.monotonic(), 1)),
                "position": None if pos is None else {
                    "side": "LONG" if pos.side > 0 else "SHORT", "qty": pos.qty,
                    "entry_px": pos.entry_px, "notional": round(pos.notional, 2),
                    "held_ms": round((time.monotonic() - pos.entry_t) * 1000),
                    "pnl_bps": round(pos.side * (mark / pos.entry_px - 1) * 1e4, 2) if mark else None,
                    "exit_pending": pos.exit_pending,
                    "tp_px": pos.tp_px or None,
                },
            }
        closed = stats.wins + stats.losses
        return {
            "t": time.time(),
            "mode": "SIM" if self.s.sim else "PAPER (live data)",
            "uptime_s": round(time.time() - self.s.started),
            "paused": st.paused,
            "halted": stats.halted_reason,
            "account": {
                "capital": self.cfg.paper.capital_usd,
                "equity": round(stats.equity, 4),
                "realized": round(stats.realized, 4),
                "unrealized": round(st.unrealized(), 4),
                "fees": round(stats.fees, 4),
                "day_pnl": round(stats.day_pnl, 4),
                "trades": closed,
                "win_rate": round(100 * stats.wins / closed, 1) if closed else None,
                "signals": stats.signals,
                "missed": stats.missed_fills,
                "open_notional": round(st.open_notional(), 2),
                "cap": self.cfg.paper.max_notional_usd,
            },
            "markets": markets,
            "feeds": {f.name: f.status() for f in self.s.feeds},
            "trades": [asdict(x) for x in st.trades[-50:]][::-1],
            "events": st.events[-60:][::-1],
            "equity": st.equity_curve[-900:],
            "leadlag": self.s.leadlag.summary(),
            "config": {
                "rtt_ms": self.cfg.paper.rtt_ms,
                "order_notional": self.cfg.paper.order_notional_usd,
                "fee_bps": self.cfg.paper.taker_fee_bps,
                "min_net_edge": self.cfg.strategy.min_net_edge_bps,
                "exit_mode": self.cfg.paper.exit_mode,
                "maker_buffer": self.cfg.strategy.maker_exit_buffer_bps,
                "direction": self.cfg.strategy.direction,
                "mode": self.cfg.strategy.get("mode", "taker"),
                "mm_entry": self.cfg.maker_mm.entry_bps,
                "tp": self.cfg.strategy.take_profit_bps,
                "sl": self.cfg.strategy.stop_loss_bps,
                "max_hold_ms": self.cfg.strategy.max_hold_ms,
            },
        }

    async def index(self, _req):
        return web.FileResponse(STATIC / "index.html")

    async def state(self, _req):
        return web.json_response(self.snapshot(), dumps=_dumps)

    async def trades_csv(self, _req):
        path = self.s.strategy.trades_csv
        if not path.exists():
            return web.Response(text="no trades yet\n")
        return web.FileResponse(path, headers={"Content-Disposition": "attachment; filename=trades.csv"})

    async def control(self, req):
        body = await req.json()
        action = body.get("action")
        st = self.s.strategy
        if action == "pause":
            st.paused = True
        elif action == "resume":
            st.paused = False
        elif action == "flatten":
            st.paused = True
            st.flatten()
        else:
            return web.json_response({"ok": False, "error": "unknown action"}, status=400)
        st._event("ALL", f"user: {action}")
        return web.json_response({"ok": True})

    async def ws(self, req):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(req)
        self.clients.add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.clients.discard(ws)
        return ws

    async def broadcast(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(0.25)
            if not self.clients:
                continue
            payload = _dumps(self.snapshot())
            for ws in list(self.clients):
                try:
                    await ws.send_str(payload)
                except Exception:  # client went away
                    self.clients.discard(ws)

    async def start(self) -> str:
        d = self.cfg.dashboard
        if d.host not in ("127.0.0.1", "localhost", "::1") and not (self.token or self.basic):
            raise SystemExit("Dashboard host is public but no auth is set. Set VARIARCUS_TOKEN or CROSSEDGE_DASH_AUTH.")
        app = web.Application(middlewares=[self.auth])
        app.router.add_get("/", self.index)
        app.router.add_get("/api/state", self.state)
        app.router.add_get("/api/trades.csv", self.trades_csv)
        app.router.add_post("/api/control", self.control)
        app.router.add_get("/ws", self.ws)
        app.router.add_static("/static", STATIC)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, d.host, d.port).start()
        url = f"http://{d.host}:{d.port}/"
        log.info("dashboard on %s", url)
        return url

    async def stop(self) -> None:
        for ws in list(self.clients):
            await ws.close()
        if self.runner:
            await self.runner.cleanup()


def _read_env_key(path: Path, key: str) -> str:
    try:
        for line in path.read_text().splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _clean(v: dict) -> dict:
    return {k: (round(x, 3) if isinstance(x, float) else x) for k, x in v.items()}


def _dumps(obj) -> str:
    return json.dumps(_sanitize(obj), default=str)


def _sanitize(obj):
    if isinstance(obj, float):
        return obj if obj == obj and abs(obj) != float("inf") else None
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj
