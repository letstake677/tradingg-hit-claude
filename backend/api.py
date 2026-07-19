"""
FastAPI backend for the Kehlo Trading dashboard.

Runs as its OWN process alongside bot.py — both share the same SQLite file
(WAL mode makes that safe). This process never talks to Bitget directly; it
only reads/writes the database and the settings table that bot.py checks
every loop tick, so starting/stopping the bot or changing risk settings from
the dashboard takes effect within one loop interval.

Run: uvicorn api:app --host 0.0.0.0 --port 8000
"""

import hmac
import json
import os
import time
import traceback
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import bot as bot_module
import crypto_utils as cu
import database as db
from bitget_client import BitgetClient, BitgetAPIError

app = FastAPI(title="Kehlo Trading API")

# Dev-friendly CORS — tighten allow_origins to your real dashboard domain
# before going live.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

PIN_MAX_ATTEMPTS = 5
PIN_LOCKOUT_SECONDS = 900  # 15 minutes


@app.on_event("startup")
def _startup():
    db.init_db()


@app.exception_handler(Exception)
async def log_unhandled_errors(request: Request, exc: Exception):
    db.log_event("error", "api", f"{request.method} {request.url.path} -> "
                                  f"{exc}\n{traceback.format_exc()}")
    return JSONResponse(status_code=500, content={"error": "internal error, check the Logs tab"})


def _check_pin(submitted: str) -> Optional[str]:
    """
    Returns None if the PIN is correct, or a user-facing error string if not.
    A 4-6 digit PIN alone is a small search space, so failed attempts are
    tracked and locked out for PIN_LOCKOUT_SECONDS after PIN_MAX_ATTEMPTS —
    that lockout, not the PIN's length, is what actually stops brute-forcing.
    This is a deliberately lightweight gate for a single-operator tool on a
    private server, not a substitute for putting the API behind real auth
    if it's ever reachable from the open internet.
    """
    lockout_until = float(db.get_setting("pin_lockout_until") or 0)
    now = time.time()
    if now < lockout_until:
        return f"Too many wrong attempts — locked out for {int(lockout_until - now)}s."

    expected = os.getenv("LIVE_MODE_PIN", "")
    if not expected:
        return "LIVE_MODE_PIN isn't set in .env on the server yet — set one before using this."

    if hmac.compare_digest(submitted or "", expected):
        db.set_setting("pin_failed_attempts", "0")
        return None

    attempts = int(db.get_setting("pin_failed_attempts") or 0) + 1
    db.set_setting("pin_failed_attempts", str(attempts))
    if attempts >= PIN_MAX_ATTEMPTS:
        db.set_setting("pin_lockout_until", str(now + PIN_LOCKOUT_SECONDS))
        db.set_setting("pin_failed_attempts", "0")
        return f"Wrong PIN. Too many attempts — locked out for {PIN_LOCKOUT_SECONDS // 60} minutes."
    return f"Wrong PIN. {PIN_MAX_ATTEMPTS - attempts} attempt(s) left before a lockout."


# ---------------- schemas ----------------

class SettingsUpdate(BaseModel):
    risk_per_trade_pct: Optional[float] = None
    max_concurrent_positions: Optional[int] = None
    max_daily_loss_pct: Optional[float] = None
    symbols: Optional[list] = None
    timeframe: Optional[str] = None
    dry_run: Optional[bool] = None
    dry_run_starting_balance: Optional[float] = None
    leverage: Optional[int] = None
    session_filter_enabled: Optional[bool] = None
    htf_bias_enabled: Optional[bool] = None
    require_sweep_confirmation: Optional[bool] = None
    displacement_filter_enabled: Optional[bool] = None


class ModeSwitchRequest(BaseModel):
    live: bool
    pin: str = ""


class CredentialsUpdate(BaseModel):
    mode: str  # 'demo' | 'live'
    api_key: str
    api_secret: str
    passphrase: str
    pin: str = ""


# ---------------- status & control ----------------

@app.get("/api/status")
def get_status():
    settings = db.get_all_settings()
    return {
        "bot_running": settings.get("bot_running") == "true",
        # Truth published by bot.py once it actually confirms which mode
        # it's running with — not just what was requested a moment ago.
        "live_mode_active": settings.get("live_mode_active") == "true",
        "settings": {
            "risk_per_trade_pct": float(settings.get("risk_per_trade_pct", 1.0)),
            "max_concurrent_positions": int(settings.get("max_concurrent_positions", 3)),
            "max_daily_loss_pct": float(settings.get("max_daily_loss_pct", 5.0)),
            "symbols": json.loads(settings.get("symbols", "[]")),
            "timeframe": settings.get("timeframe", "15m"),
            "dry_run": settings.get("dry_run") == "true",
            "dry_run_starting_balance": float(settings.get("dry_run_starting_balance", 1000.0)),
            "leverage": int(settings.get("leverage", 5)),
            "session_filter_enabled": settings.get("session_filter_enabled") == "true",
            "htf_bias_enabled": settings.get("htf_bias_enabled") == "true",
            "require_sweep_confirmation": settings.get("require_sweep_confirmation") == "true",
            "displacement_filter_enabled": settings.get("displacement_filter_enabled") == "true",
        },
        "open_position_count": len(db.get_open_trades(dry_run=settings.get("dry_run") == "true")),
    }


@app.post("/api/bot/start")
def start_bot():
    db.set_setting("bot_running", "true")
    return {"bot_running": True}


@app.post("/api/bot/stop")
def stop_bot():
    db.set_setting("bot_running", "false")
    return {"bot_running": False}


@app.post("/api/settings")
def update_settings(update: SettingsUpdate):
    if update.risk_per_trade_pct is not None:
        db.set_setting("risk_per_trade_pct", str(update.risk_per_trade_pct))
    if update.max_concurrent_positions is not None:
        db.set_setting("max_concurrent_positions", str(update.max_concurrent_positions))
    if update.max_daily_loss_pct is not None:
        db.set_setting("max_daily_loss_pct", str(update.max_daily_loss_pct))
    if update.symbols is not None:
        db.set_setting("symbols", json.dumps(update.symbols))
    if update.timeframe is not None:
        db.set_setting("timeframe", update.timeframe)
    if update.dry_run is not None:
        db.set_setting("dry_run", "true" if update.dry_run else "false")
        db.log_event("info", "api", f"Dry run mode {'enabled' if update.dry_run else 'disabled'} via dashboard")
    if update.dry_run_starting_balance is not None:
        db.set_setting("dry_run_starting_balance", str(update.dry_run_starting_balance))
    if update.leverage is not None:
        db.set_setting("leverage", str(update.leverage))
    if update.session_filter_enabled is not None:
        db.set_setting("session_filter_enabled", "true" if update.session_filter_enabled else "false")
    if update.htf_bias_enabled is not None:
        db.set_setting("htf_bias_enabled", "true" if update.htf_bias_enabled else "false")
    if update.require_sweep_confirmation is not None:
        db.set_setting("require_sweep_confirmation", "true" if update.require_sweep_confirmation else "false")
    if update.displacement_filter_enabled is not None:
        db.set_setting("displacement_filter_enabled", "true" if update.displacement_filter_enabled else "false")
    return {"settings": db.get_all_settings()}


# ---------------- mode switching (PIN-gated) ----------------

@app.post("/api/mode/set")
def set_mode(req: ModeSwitchRequest):
    """
    Requests a demo<->live switch. Going TO live requires the correct PIN
    (see LIVE_MODE_PIN in .env) and live credentials already attached;
    going back to demo needs neither, since retreating to the safe side
    should never have friction. This only sets a REQUEST — bot.py picks it
    up within ~5s, actually swaps its Bitget client, and publishes the
    confirmed result back as live_mode_active. Poll /api/status afterwards
    to see it actually take effect.
    """
    if req.live:
        error = _check_pin(req.pin)
        if error:
            return JSONResponse(status_code=403, content={"error": error})
        if not db.get_credential_status()["live"]["configured"]:
            return JSONResponse(status_code=400, content={
                "error": "Live credentials aren't attached yet — add them via /api/credentials first."
            })
        db.log_event("warning", "api", "Live mode requested via dashboard (PIN verified)")
        # Going live should mean real trading actually happens — dry_run
        # silently staying on was confusing (bot only ever simulated,
        # never placed a real order, with no clear indication why).
        if db.get_setting("dry_run") == "true":
            db.set_setting("dry_run", "false")
            db.log_event("info", "api", "Dry run auto-disabled — you just confirmed going live")
    else:
        db.log_event("info", "api", "Demo mode requested via dashboard")

    db.set_setting("live_mode_requested", "true" if req.live else "false")
    return {"requested": "live" if req.live else "demo"}


# ---------------- credentials (PIN-gated) ----------------

@app.post("/api/credentials")
def set_credentials(update: CredentialsUpdate):
    if update.mode not in ("demo", "live"):
        return JSONResponse(status_code=400, content={"error": "mode must be 'demo' or 'live'"})
    error = _check_pin(update.pin)
    if error:
        return JSONResponse(status_code=403, content={"error": error})
    if not (update.api_key and update.api_secret and update.passphrase):
        return JSONResponse(status_code=400, content={
            "error": "api_key, api_secret, and passphrase are all required"})
    try:
        db.set_credential(update.mode, "api_key", cu.encrypt(update.api_key))
        db.set_credential(update.mode, "api_secret", cu.encrypt(update.api_secret))
        db.set_credential(update.mode, "passphrase", cu.encrypt(update.passphrase))
    except cu.CredentialCryptoError as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    db.log_event("info", "api", f"{update.mode} credentials attached via dashboard "
                                 f"(key ending {cu.mask(update.api_key)})")
    return {"mode": update.mode, "saved": True, "key_hint": cu.mask(update.api_key)}


@app.get("/api/credentials/status")
def credentials_status():
    """Safe to expose — configured flags and last-updated times only, never
    the actual secrets."""
    return db.get_credential_status()


# ---------------- trades ----------------

@app.get("/api/trades/open")
def open_trades():
    is_dry_run = db.get_setting("dry_run") == "true"
    trades = db.get_open_trades(dry_run=is_dry_run)
    if not trades:
        return trades

    price_cache = {}
    try:
        demo = db.get_setting("live_mode_active") != "true"
        creds = bot_module.load_credentials_for_mode(demo)
        client = BitgetClient(creds)
        product_type = os.getenv("BITGET_PRODUCT_TYPE", "USDT-FUTURES")
        for trade in trades:
            symbol = trade["symbol"]
            if symbol not in price_cache:
                try:
                    raw = client.get_candles(symbol, "1m", product_type=product_type, limit=1)
                    price_cache[symbol] = float(raw[-1][4]) if raw else None
                except Exception:
                    # ANY failure here (API error, empty/malformed response,
                    # etc.) should never take down the whole endpoint —
                    # worst case this trade just shows no current_price.
                    price_cache[symbol] = None
            trade["current_price"] = price_cache[symbol]
    except ValueError:
        # No credentials configured at all — still return the trades, just
        # without live prices, rather than failing the whole request.
        for trade in trades:
            trade["current_price"] = None
    return trades


@app.post("/api/trades/{trade_id}/close")
def close_trade_manually(trade_id: int):
    """
    Manual close from the dashboard. Dry-run trades close immediately at
    the current market price. Real trades: cancel any pending SL/TP plan
    orders first (so they can't fire after the position is gone), then
    close the position on Bitget — the trade stays 'open' in our db until
    bot.py's next reconcile_open_trades() pass picks up the real PnL from
    Bitget's history within ~a minute, rather than guessing it here.
    """
    trade = next((t for t in db.get_open_trades() if t["id"] == trade_id), None)
    if not trade:
        return JSONResponse(status_code=404, content={"error": "Trade not found or already closed"})

    product_type = os.getenv("BITGET_PRODUCT_TYPE", "USDT-FUTURES")

    if trade.get("dry_run"):
        try:
            demo = db.get_setting("live_mode_active") != "true"
            creds = bot_module.load_credentials_for_mode(demo)
            client = BitgetClient(creds)
            raw = client.get_candles(trade["symbol"], "1m", product_type=product_type, limit=1)
            current_price = float(raw[-1][4])
        except Exception as e:
            return JSONResponse(status_code=502, content={"error": f"Couldn't fetch current price to close: {e}"})
        pnl = bot_module.dry_run_unrealized_pnl(trade, current_price)
        db.close_trade(trade_id, realized_pnl=pnl, close_reason="manual")
        db.log_event("info", "api", f"[DRY RUN][{trade['symbol']}] trade #{trade_id} manually closed "
                                     f"@ ~{current_price:.4f}, pnl {pnl:.4f}")
        return {"closed": True, "trade_id": trade_id, "pnl": round(pnl, 4), "close_price": current_price}

    try:
        creds = bot_module.load_credentials_for_mode(bool(trade["demo"]))
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    client = BitgetClient(creds)
    hold_side = "long" if trade["direction"] == "long" else "short"

    warnings = []
    if trade.get("sl_order_id"):
        try:
            client.cancel_tpsl_order(trade["symbol"], trade["sl_order_id"], product_type=product_type)
        except BitgetAPIError as e:
            warnings.append(f"cancel SL order: {e}")
    for leg in trade.get("tp_legs", []):
        if leg.get("bitget_order_id") and leg["hit"] != 1:
            try:
                client.cancel_tpsl_order(trade["symbol"], leg["bitget_order_id"], product_type=product_type)
            except BitgetAPIError as e:
                warnings.append(f"cancel TP{leg['level']} order: {e}")

    try:
        client.close_position(trade["symbol"], product_type=product_type, hold_side=hold_side)
    except BitgetAPIError as e:
        return JSONResponse(status_code=502, content={"error": f"Failed to close position on Bitget: {e}",
                                                        "warnings": warnings})

    db.log_event("info", "api", f"[{trade['symbol']}] trade #{trade_id} manual close sent to Bitget via "
                                 f"dashboard — real PnL will show once reconciliation confirms it")
    return {"closed": "pending", "trade_id": trade_id, "warnings": warnings,
            "note": "Close request sent to Bitget. This will show as closed with real PnL once bot.py's "
                    "next reconciliation pass confirms it (within about a minute)."}


@app.get("/api/trades/history")
def trade_history(limit: int = 100):
    is_dry_run = db.get_setting("dry_run") == "true"
    return db.get_trade_history(limit=limit, dry_run=is_dry_run)


# ---------------- signals ----------------

@app.get("/api/signals/recent")
def recent_signals(limit: int = 50):
    return db.get_recent_signals(limit=limit)


# ---------------- logs ----------------

@app.get("/api/logs")
def logs(limit: int = 200, level: Optional[str] = None):
    return db.get_recent_logs(limit=limit, level=level)


# ---------------- stats ----------------

@app.get("/api/stats")
def stats():
    is_dry_run = db.get_setting("dry_run") == "true"
    return db.get_stats(dry_run=is_dry_run)


@app.get("/api/account/balance")
def account_balance():
    """
    Live USDT equity straight from Bitget, for whichever mode bot.py has
    confirmed is currently active. Builds its own short-lived client rather
    than reaching into bot.py's running one — in the VPS/Docker-Compose
    deployment, api.py and bot.py are separate processes and don't share
    memory, only the database. In dry run, returns realized PnL from
    closed trades PLUS mark-to-market unrealized PnL from any still-open
    simulated positions, so the number actually moves with the market
    instead of sitting frozen until a trade fully closes.
    """
    if db.get_setting("dry_run") == "true":
        realized_equity = db.get_dry_run_equity()
        open_dry_trades = [t for t in db.get_open_trades() if t.get("dry_run")]
        unrealized_total = 0.0
        if open_dry_trades:
            try:
                demo = db.get_setting("live_mode_active") != "true"
                creds = bot_module.load_credentials_for_mode(demo)
                client = BitgetClient(creds)
                product_type = os.getenv("BITGET_PRODUCT_TYPE", "USDT-FUTURES")
                price_cache = {}
                for trade in open_dry_trades:
                    symbol = trade["symbol"]
                    if symbol not in price_cache:
                        raw = client.get_candles(symbol, "1m", product_type=product_type, limit=1)
                        price_cache[symbol] = float(raw[-1][4])  # close of the latest candle
                    unrealized_total += bot_module.dry_run_unrealized_pnl(trade, price_cache[symbol])
            except Exception as e:
                # Price fetch failing shouldn't take down the whole balance
                # display — just fall back to realized-only and say so.
                return {"equity": realized_equity, "mode": "dry_run",
                        "unrealized_pnl": None,
                        "note": f"showing realized only — couldn't fetch live prices ({e})"}
        return {"equity": realized_equity + unrealized_total, "mode": "dry_run",
                "unrealized_pnl": unrealized_total}

    demo = db.get_setting("live_mode_active") != "true"
    try:
        creds = bot_module.load_credentials_for_mode(demo)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    try:
        equity = bot_module.get_equity(BitgetClient(creds))
    except BitgetAPIError as e:
        return JSONResponse(status_code=502, content={"error": f"Bitget error: {e}"})
    return {"equity": equity, "mode": "demo" if demo else "live"}


# ---------------- dashboard (static site) ----------------
# Mounted LAST and at "/" so every /api/* route above is matched first —
# anything that isn't an API call falls through to the dashboard's files.
# If dashboard_dist wasn't built/copied in, the API still works fine on its
# own; visiting "/" would just 404 as before.
_dashboard_dir = os.path.join(os.path.dirname(__file__), "dashboard_dist")
if os.path.isdir(_dashboard_dir):
    app.mount("/", StaticFiles(directory=_dashboard_dir, html=True), name="dashboard")
