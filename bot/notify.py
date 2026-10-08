"""Telegram alerts, same convention as the other [DRY] bots on the server.

Token/chat come from env SHADOW_TG_TOKEN / MIROFISH_TG_CHAT, falling back to
/home/ubuntu/.mirofish.env. Without them this is a no-op.
Also honours a STOP file: if it exists, the strategy is paused.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import aiohttp

log = logging.getLogger("notify")

TAG = "🧪 <b>[VARIARCUS-DRY]</b>"
ENV_FILE = Path("/home/ubuntu/.mirofish.env")
STOP_FILE = Path("/home/ubuntu/.variarcus_STOP")


def _creds() -> tuple[str, str]:
    token = os.environ.get("SHADOW_TG_TOKEN", "")
    chat = os.environ.get("MIROFISH_TG_CHAT", "")
    if (not token or not chat) and ENV_FILE.exists():
        try:
            for line in ENV_FILE.read_text().splitlines():
                k, _, v = line.strip().partition("=")
                v = v.strip().strip('"').strip("'")
                if k == "SHADOW_TG_TOKEN" and not token:
                    token = v
                elif k == "MIROFISH_TG_CHAT" and not chat:
                    chat = v
        except OSError:
            pass
    return token, chat


class Notifier:
    def __init__(self, app, cfg):
        self.app = app
        self.cfg = cfg
        self.token, self.chat = _creds()
        tcfg = cfg.get("telegram") or {}
        self.per_trade = tcfg.get("per_trade", True)
        self.summary_every = tcfg.get("summary_every_min", 60) * 60
        self.enabled = bool(self.token and self.chat) and tcfg.get("enabled", True)
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=200)

    def send(self, text: str) -> None:
        if self.enabled:
            try:
                self.queue.put_nowait(f"{TAG}\n{text}")
            except asyncio.QueueFull:
                pass

    async def _sender(self, stop: asyncio.Event) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            while not stop.is_set():
                msg = await self.queue.get()
                try:
                    await s.post(url, json={"chat_id": self.chat, "text": msg, "parse_mode": "HTML"})
                except Exception as exc:
                    log.debug("telegram: %s", exc)
                await asyncio.sleep(1.5)        # stay far below Telegram limits

    def _summary(self) -> str:
        st = self.app.strategy.stats
        n = st.wins + st.losses
        wr = f"{100 * st.wins / n:.0f}%" if n else "–"
        q = [f"{s}:{l}" for (s, l), (ok, _) in self.app.leadlag.qualified.items() if ok]
        gaps = []
        for sym in self.cfg.symbols:
            d = self.app.strategy.diag_prev.get(sym) or self.app.strategy.diag.get(sym)
            if d:
                gaps.append(f"{sym} best gap {d['best_edge']:.1f} / need {d['need']:.1f} bps "
                            f"({d['leader']}, {d['over_need']} ticks over)")
        return (f"Equity <b>${st.equity:.2f}</b> (start ${self.cfg.paper.capital_usd:.0f})\n"
                f"Gross ${st.realized + st.fees:+.3f} − fees ${st.fees:.3f} = net ${st.realized:+.3f} · today ${st.day_pnl:+.3f}\n"
                f"Trades {n} · win {wr} · signals {st.signals} · missed {st.missed_fills}\n"
                f"Qualified leaders: {', '.join(q) if q else 'none yet'}\n"
                + "\n".join(gaps))

    async def run(self, stop: asyncio.Event) -> None:
        if not self.enabled:
            log.info("telegram off (no SHADOW_TG_TOKEN / MIROFISH_TG_CHAT)")
        sender = asyncio.create_task(self._sender(stop)) if self.enabled else None
        strat, ll = self.app.strategy, self.app.leadlag
        seen_trades = 0
        qualified: set = set()
        halted = ""
        stop_paused = False
        next_summary = time.time() + self.summary_every
        self.send(f"started · {', '.join(self.cfg.symbols)} · cap ${self.cfg.paper.max_notional_usd:.0f} · "
                  f"rtt {self.cfg.paper.rtt_ms} ms")
        try:
            while not stop.is_set():
                await asyncio.sleep(2)
                # STOP file -> pause (same habit as the other bots)
                if STOP_FILE.exists() and not stop_paused:
                    strat.paused, stop_paused = True, True
                    self.send(f"paused by {STOP_FILE}")
                elif stop_paused and not STOP_FILE.exists():
                    strat.paused, stop_paused = False, False
                    self.send("STOP file removed, resumed")
                if self.per_trade:
                    for t in strat.trades[seen_trades:]:
                        icon = "✅" if t.net_usd > 0 else "❌"
                        self.send(f"{icon} {t.symbol} {t.side} ${t.notional:.0f} · net <b>${t.net_usd:+.3f}</b> "
                                  f"({t.net_bps:+.1f} bps) · {t.hold_ms / 1000:.1f}s · {t.exit_reason}\n"
                                  f"edge {t.entry_edge_bps:.1f} bps via {t.leaders}")
                seen_trades = len(strat.trades)
                now_q = {k for k, (ok, _) in ll.qualified.items() if ok}
                for s, l in sorted(now_q - qualified):
                    self.send(f"🟢 leader qualified: {s} ← {l} ({ll.qualified[(s, l)][1]})")
                for s, l in sorted(qualified - now_q):
                    self.send(f"⚪ leader dropped: {s} ← {l} ({ll.qualified.get((s, l), (0, '?'))[1]})")
                qualified = now_q
                if strat.stats.halted_reason and strat.stats.halted_reason != halted:
                    self.send(f"⛔ halted: {strat.stats.halted_reason}\n{self._summary()}")
                halted = strat.stats.halted_reason
                if time.time() >= next_summary:
                    next_summary = time.time() + self.summary_every
                    self.send(f"⏱ summary\n{self._summary()}")
        finally:
            if self.enabled:
                self.send(f"stopped\n{self._summary()}")
                await asyncio.sleep(2)
            if sender:
                sender.cancel()
