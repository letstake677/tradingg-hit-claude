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
        },
        "open_position_count": len(db.get_open_trades()),
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
    return db.get_open_trades()


@app.get("/api/trades/history")
def trade_history(limit: int = 100):
    return db.get_trade_history(limit=limit)


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
    return db.get_stats()


@app.get("/api/account/balance")
def account_balance():
    """
    Live USDT equity straight from Bitget, for whichever mode bot.py has
    confirmed is currently active. Builds its own short-lived client rather
    than reaching into bot.py's running one — in the VPS/Docker-Compose
    deployment, api.py and bot.py are separate processes and don't share
    memory, only the database.
    """
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
