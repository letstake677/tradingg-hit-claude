# Kehlo Trading — Bitget Auto-Trading Bot

An SMC (Smart Money Concepts) auto-trading bot for Bitget: structure-based
entries, ATR + liquidity-aware stops, multiple take-profits with real chart
targets, breakeven automation, and a dashboard to run and watch it from.

## ⚠️ Read this first

- **No bot guarantees profit.** SMC rules give structure to decisions — they
  don't remove market risk. Backtest and demo-trade before ever going live.
- **Never share your real API secret, passphrase, or PIN** in chat, in git,
  or anywhere outside your own `.env` file / the dashboard's Connect tab.
- **API key permissions:** create each key with **Trade + Read only**. Never
  enable **Withdraw** on a bot's API key.
- This needs to run 24/7 to actually trade — deploy it on something
  always-on (a VPS or your own server), not inside a chat session.
- The API assumes a **private network** (your own server, behind a
  firewall/VPN). It has no general authentication beyond the PIN on
  sensitive actions — see the security section below before exposing it
  publicly.

## What's built

| File | What it does |
|---|---|
| `backend/bitget_client.py` | Bitget v2 REST client — auth, candles, positions, leverage, order placement, per-level TP/SL, cancel-plan-order, history-position (real closed-PnL) |
| `backend/smc_engine.py` | Market structure (BOS/CHoCH), order blocks, FVGs, liquidity zones; structure-based TP targeting; ATR + liquidity-aware structural stop-loss |
| `backend/risk_manager.py` | Position sizing by % risk, multi-TP split (40/35/25 default), auto-breakeven after TP1 |
| `backend/crypto_utils.py` | Encrypts/decrypts Bitget credentials before they touch the database |
| `backend/database.py` | SQLite (WAL) — trades, TP legs, signal log, system logs, settings, encrypted per-mode credentials |
| `backend/bot.py` | Main loop: candles → signal → risk check → place entry+SL+TPs → record. Detects partial fills and moves SL to breakeven. Pulls real PnL on close. Switches demo/live at runtime |
| `backend/api.py` | FastAPI backend — status, start/stop, settings, trades, signals, logs, stats, PIN-gated credentials + mode switching |
| `backend/test_smc_engine.py` | Offline test proving the signal pipeline works — no network needed |
| `backend/start_combined.py` | Alternate entrypoint for single-service platforms (Railway) — runs bot + API in one process so they can share one volume |
| `frontend/dashboard.jsx` | Live-wired dashboard: Positions / Signals / History / Logs / **Connect** / Settings tabs |

## How the signal engine works

`smc_engine.py` builds a `Signal` (direction, entry, stop_loss, take_profits)
from real chart structure, not arbitrary numbers:

- **Entry**: price returning into an unmitigated order block during a
  confirmed trend (BOS/CHoCH), ideally overlapping an unfilled FVG or near a
  liquidity pool — each confluence adds to `confidence`.
- **Stop-loss** (`_structural_stop`): the order block's far edge — pushed
  further out if a liquidity pool sits just beyond it (a likely stop-hunt
  target) — plus an ATR-based buffer that scales with current volatility
  instead of a flat percentage. `sl_reason` explains which applied.
- **Take-profits** (`_collect_targets`): the nearest real opposing order
  blocks, unfilled FVGs, liquidity pools, or swing points in the trade's
  direction. Only falls back to a raw R-multiple — clearly labelled as a
  fallback, never a promise — when no real level exists yet. `tp_reasons`
  explains each level.

This is a rules-based *approximation* of discretionary SMC, not a finished
edge — backtest it and tune `swing_lookback`, `fvg_min_gap_pct`, etc. before
trusting it with real money.

## Multiple TP/SL and breakeven automation

Bitget has no single "3 take-profits" field — `place_tpsl_leg()` is called
once per level, each with a partial size, plus once for the SL:

```
place_tpsl_leg(..., plan_type="profit_plan", trigger_price=TP1, size=40% of position)
place_tpsl_leg(..., plan_type="profit_plan", trigger_price=TP2, size=35% of position)
place_tpsl_leg(..., plan_type="profit_plan", trigger_price=TP3, size=25% of position)
place_tpsl_leg(..., plan_type="loss_plan",   trigger_price=SL,  size=100% of position)
```

`bot.py` records each leg's Bitget order id. Every loop tick,
`manage_open_positions()` compares live position size to what was recorded
at entry — a smaller live size means a TP filled. Once TP1 is confirmed, it
cancels the original SL order and places a new one at entry price for
whatever size remains, marking `breakeven_applied` so it's never redone.
When a position fully closes, `reconcile_open_trades()` pulls the actual
`netProfit` from Bitget's `history-position` endpoint instead of guessing.

The daily-loss circuit breaker (`max_daily_loss_pct`) uses real closed-trade
PnL since UTC midnight, not a stub — confirmed to actually block new trades
once today's losses cross the limit.

## Attaching API keys and switching demo/live

Bitget's demo and live trading use **completely separate API keys** — one
doesn't work in the other mode. The dashboard's **Connect tab** lets you
attach both independently (encrypted before storage); `.env` works too as a
bootstrap/fallback (`BITGET_DEMO_*` / `BITGET_LIVE_*`). The database is
checked first.

The bot **always boots in demo**. Switching to live is a deliberate runtime
action — `bot.py` rebuilds its Bitget client with the other credential set,
it never just flips a flag. The dashboard's mode badge is clickable
("Switch"):

- **Going to live requires the PIN** (`LIVE_MODE_PIN` in `.env`) and live
  credentials already attached. 5 wrong attempts locks it out for 15
  minutes — even the correct PIN is rejected during a lockout.
- **Going back to demo needs no PIN** — retreating to the safe side should
  never have friction.
- The PIN is checked server-side only (`hmac.compare_digest`), never stored
  or sent by the browser.
- **This is a lightweight gate for a private, single-operator deployment**,
  not a substitute for the API having no other auth. If the API is ever
  reachable beyond your own machine/VPN, put real authentication in front
  of it too.

## Logs

`logs` table (auto-pruned to 2000 rows) captures every trade open, skipped
signal, and error via a small logger that both prints (systemd/docker logs
still show everything) and writes to the db. The dashboard's **Logs tab**
reads `/api/logs` with a level filter, so problems are visible without
shell access to the server.

## Running it

```bash
cd backend
pip install -r requirements.txt
cp ../.env.example ../.env
# fill in .env: at minimum CREDENTIAL_ENCRYPTION_KEY and LIVE_MODE_PIN.
# Bitget keys can go here too, or be attached later via the Connect tab.
python3 test_smc_engine.py                    # offline sanity check, no keys needed
python3 bot.py                                # process 1: the trading engine
uvicorn api:app --host 0.0.0.0 --port 8000    # process 2: the API
```

Then open `frontend/dashboard.jsx` (as a Claude artifact, or built into a
static site you deploy alongside the API) and point the address field in
the top bar at your server, e.g. `http://your-server-ip:8000`.

### How the dashboard is wired

- Polls every endpoint on independent timers (5–10s) using `setTimeout`
  chains, so a slow request never overlaps the next tick.
- The API address is editable right in the top bar — point the same
  dashboard at a local test server or your real deployed one.
- A connection banner appears if the API can't be reached, telling you what
  to check instead of silently showing stale data.
- Settings load from the server once, then stay put while you edit —
  polling won't overwrite what you're typing.
- The equity curve is computed client-side from trade history's
  `realized_pnl` — no separate endpoint needed.
- Starting the bot while already live requires a second confirming click.

## Deployment

Full guide for both paths is in **[DEPLOY.md](./DEPLOY.md)**:
- **VPS + Docker Compose** — `bot.py` and `api.py` as independent
  containers sharing one volume, Caddy for automatic HTTPS.
- **Railway** — no server to manage, but Railway doesn't support sharing a
  volume between two services, so `start_combined.py` runs both in one
  service instead (one process, one volume, HTTPS handled by Railway).

## Still ahead

- No backtester against real historical candles yet — everything so far is
  verified with hand-built or synthetic scenarios, not a real market history.
- No general API authentication — see the security note above.
- Partial-fill detection is poll-based (every ~60s), not real-time. Bitget's
  private WebSocket order/position channel would catch fills the moment
  they happen instead.
- `history-position` matching for real PnL is best-effort (price + timing
  window), since that endpoint doesn't accept a client-supplied reference
  id — fine for one bot on one account, not bulletproof if you're also
  manually trading the same symbol at the same time.
