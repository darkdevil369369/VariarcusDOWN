#!/usr/bin/env python3
"""VariarcusDOWN — paper-trade Arcus off leader prices, with a live dashboard.

    python run.py              # live public data, paper fills, opens the dashboard
    python run.py --sim        # offline synthetic market (no network) to try the UI
    python run.py --config my.yaml --no-browser
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import time
import webbrowser

from bot.config import load_config
from bot.dashboard import Dashboard
from bot.feeds.sim import SimFeed
from bot.feeds.venues import LEADER_FEEDS, ArcusFeed
from bot.leadlag import LeadLag
from bot.maker import MakerStrategy
from bot.market import Market
from bot.notify import Notifier
from bot.strategy import Strategy

TICK_S = 0.01   # strategy loop: 100 Hz


class App:
    def __init__(self, cfg, sim: bool):
        self.cfg = cfg
        self.sim = sim
        self.started = time.time()
        leaders = [n for n, c in cfg.leaders.items() if c.get("enabled", True)]
        self.markets = {s: Market(s, leaders) for s in cfg.symbols}
        self.leadlag = LeadLag(self.markets, cfg)
        cls = MakerStrategy if cfg.strategy.get("mode") == "maker_mm" else Strategy
        self.strategy = cls(self.markets, cfg, leadlag=self.leadlag)
        if sim:
            self.feeds = [SimFeed(self.markets)]
        else:
            self.feeds = [ArcusFeed(self.markets, s) for s in cfg.symbols]
            self.feeds += [LEADER_FEEDS[n](self.markets) for n in leaders]

    async def loop(self, stop: asyncio.Event) -> None:
        log = logging.getLogger("loop")
        while not stop.is_set():
            try:
                self.strategy.tick()
                self.leadlag.tick()
            except Exception:
                log.exception("tick failed")
            await asyncio.sleep(TICK_S)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="YAML overriding config.yaml (default: config.local.yaml if present)")
    ap.add_argument("--sim", action="store_true", help="synthetic offline market")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname).1s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = load_config(args.config)
    app = App(cfg, args.sim)
    dash = Dashboard(app, cfg)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:   # Windows
            pass

    url = await dash.start()
    print(f"\n  VariarcusDOWN paper bot — dashboard: {url}\n  cap ${cfg.paper.max_notional_usd:.0f} paper, Ctrl+C to stop\n")
    if cfg.dashboard.open_browser and not args.no_browser:
        webbrowser.open(url)

    notifier = Notifier(app, cfg)
    notify_task = asyncio.create_task(notifier.run(stop))
    tasks = [asyncio.create_task(f.run(stop)) for f in app.feeds]
    tasks += [asyncio.create_task(app.loop(stop)), asyncio.create_task(dash.broadcast(stop))]
    try:
        await stop.wait()
    finally:
        try:
            await asyncio.wait_for(notify_task, 8)    # lets the "stopped" alert go out
        except (asyncio.TimeoutError, Exception):
            pass
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await dash.stop()
        s = app.strategy.stats
        print(f"\n  stopped. trades={s.wins + s.losses} realized=${s.realized:+.3f} fees=${s.fees:.3f}"
              f" -> {app.strategy.trades_csv}\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
