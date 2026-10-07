# VariarcusDOWN

Lead-lag **paper-trading** bot. It watches public prices on Variational Omni and on the
CEX perps that price everything (Binance, Bybit, OKX). When they move and **Arcus has not
caught up yet**, it simulates an IOC taker order on Arcus BTC-USD / ETH-USD. It exits when
Arcus converges. The paper fills model latency honestly. A live dashboard opens in your
browser.

> Paper only. Cap $500. No keys, no wallet, no real orders. Variational is only *read*
> (public price feed), never traded.

## Run

```bash
# Windows: double-click start.bat
# macOS / Linux:
./start.sh                 # live public data, paper fills, opens http://127.0.0.1:8787
./start.sh --sim           # offline synthetic market, to see the UI without network
```

Manual: `pip install -r requirements.txt` then `python run.py`.

**Pehle apna RTT measure karo.** Is strategy mein sabse zaroori number yahi hai:

```bash
python tools/ping_arcus.py      # prints p50; put it into config.local.yaml -> paper.rtt_ms
```

### On the bots server (same setup as polypf15min)

```bash
URL=$(git -C ~/mirofish-bots remote get-url origin | sed 's/mirofish-bots/VariarcusDOWN/')
git clone "$URL" ~/VariarcusDOWN
bash ~/VariarcusDOWN/deploy/install_server.sh
```

The script installs the `variarcus` systemd service and serves the dashboard on
127.0.0.1:8798. It publishes the dashboard at **https://variarcus.tryrealo.com** through the
existing cloudflared tunnel, so no AWS port needs to be opened. Login is the same as
edge.tryrealo.com (`CROSSEDGE_DASH_AUTH`). Telegram alerts use `~/.mirofish.env`. It also
measures RTT to Arcus and writes it into the config. Note: Arcus's servers are in Tokyo, so
from Ireland the RTT is about 320 ms. Run the script again to update. Logs:
`journalctl -u variarcus -f`. Pause with `touch ~/.variarcus_STOP`.

To change settings, copy `config.yaml` to `config.local.yaml` and edit that copy. It overrides
the defaults and is ignored by git.

## How it decides

**Fair value.** For each leader L: `fair_L = mid_L × exp(basis_L)`. `basis_L` is a 90 s
time-weighted EMA of `ln(arcus_mid / mid_L)`. Every venue trades at its own small premium,
so the bot learns that premium and only acts on deviations from it. The EMA is frozen while
the leader is moving fast, so a lead move does not leak into the basis.

**Leader qualification (the core safety rule).** A built-in analyzer runs all the time and
measures whether Arcus really follows each leader, and how late:
- **Event study.** A leader moves ≥ 5 bps in 1 s. How long until Arcus covers half of that
  move, and how often does it follow at all?
- **Cross-correlation.** 500 ms returns, leader(t) vs Arcus(t+k). At which k is the
  correlation highest?

A leader can **trigger trades only once both tests prove that Arcus lags it by more than
your RTT + Arcus's 50 ms taker speed bump + 150 ms margin**. Your observation ("price drops on
Variational first, then on Arcus a second later") gets tested on live data, per leader and
per market. If it doesn't hold, the bot doesn't trade.

**Entry (long; short is the mirror):**
1. A qualified leader with a fresh quote (≤ 500 ms for CEX, ≤ 1.5 s for Variational) moved
   ≥ 4 bps in the last 1.5 s.
2. That leader's fair value is above Arcus's executable ask (VWAP for your size) by at least
   `2 × 2.25 bps fee + half spread + 2 bps`.
3. No other fresh leader signals the opposite side.
4. The Arcus book is healthy (sequence OK) and its spread is ≤ 6 bps.
5. Risk checks pass: $500 total cap, daily loss limit, trades/hour limit, loss-streak pause,
   cooldown.

**Exit (first one that hits):** converged (Arcus has reached fair) · +15 bps take profit ·
−10 bps stop loss · 8 s max hold. Exits are IOC with a 15 bps cap and retry if not filled.

**Paper fills are not optimistic.** An order is matched against the Arcus book as it looks
`rtt + 50 ms` after the decision. Your view of the book is already rtt/2 old, the order needs
rtt/2 to arrive, and then Arcus holds takers for 50 ms. The fill walks real L2 depth, can be
partial, can miss if the book moved past the limit, and pays the 2.25 bps taker fee on both
legs.

## Dashboard

- Equity, realized and unrealized PnL, fees, win rate, signals and missed fills, open
  notional vs cap.
- Per market: Arcus bid/ask/spread/health and an L2 ladder. For each leader: mid, age,
  learned basis, recent move, fair − Arcus. A live chart of every leader's gap with the
  dashed "edge needed" band.
- The lead-lag table, showing which leaders are qualified to trigger and why.
- Trades, an event log, `trades.csv` download, and Pause / Resume / Flatten buttons.

## Files

| path | what |
|---|---|
| `run.py` | entry point |
| `config.yaml` | all parameters, with comments |
| `bot/feeds/venues.py` | Arcus L2 WS, Variational `/prices` WS, Binance / Bybit / OKX BBO WS |
| `bot/signals.py` | basis EMA, fair value, momentum |
| `bot/leadlag.py` | live lead-lag analyzer and leader qualification |
| `bot/strategy.py` | entry/exit logic, risk, paper executor |
| `bot/dashboard.py`, `bot/static/` | web UI (Chart.js bundled locally) |
| `tools/ping_arcus.py` | RTT measurement (`--write` saves it to config.local.yaml) |
| `deploy/install_server.sh` | one-command server install as a systemd service |
| `data/trades.csv` | every paper trade |

Tests: `python -m pytest -q`. They include a synthetic market where the bot must make money
when Arcus lags more than your latency, and must take **zero** trades when it doesn't.

## Read this before trusting any number

- **Variational's public feed is about 1 tick per second** (mark price). The browser UI
  feels faster than that. So the leaders that actually matter are usually Binance, Bybit and
  OKX, which push updates in milliseconds. Variational itself prices off those venues. The
  analyzer will show which leader really leads.
- **Latency decides everything.** Earlier public research on Arcus found that Binance leads
  Arcus by about 1–3 s. A slow participant (≈150 ms behind the feed, ≈200 ms RTT to Arcus)
  still got picked off by faster bots, which took the good fills. Running on a VPS near
  Arcus's servers is the single biggest upgrade.
- **Fees are 4.5 bps round trip at base tier.** Small moves are not worth it. The thresholds
  are set for that.
- **Run paper for at least a week**, across volatile and quiet hours, before even
  considering real money. Check that net bps per trade stays positive after fees.
- Live order placement (Arcus Ed25519-signed API) is deliberately **not** included. Add it
  only after paper results hold up, and test it on Arcus testnet first.

## Sources

- Variational public API (read-only SDK and protocol notes): <https://github.com/tonymontanov/go-variational>
- Arcus public WS/REST usage and venue facts (fees, 50 ms taker speed bump, rate limits):
  <https://github.com/lspss93119/arcus_arb>, <https://github.com/dhruvamity/arcus-mm> (see `research/FINDINGS.md`),
  <https://github.com/chiwalfrm/arcustools>
- Official docs: <https://docs.arcus.xyz/>, <https://docs.variational.io/technical-documentation/api>
