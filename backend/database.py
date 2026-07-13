"""
SQLite persistence for Kehlo Trading.

Why SQLite: zero setup, file-based, plenty for a solo/small bot running on
one server. If this ever grows into multi-user or high write volume, swap
for Postgres — the query shapes below translate directly.
"""

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Optional

# Configurable so Docker/production can point this at a mounted volume
# (e.g. /data/kehlo_trading.db) instead of the working directory.
DB_PATH = os.getenv("KEHLO_DB_PATH", "kehlo_trading.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,               -- 'long' or 'short'
    entry_price REAL NOT NULL,
    stop_loss REAL NOT NULL,
    sl_reason TEXT,                        -- why the stop sits exactly there
    sl_order_id TEXT,                      -- Bitget's plan order id for the current SL leg
    breakeven_applied INTEGER NOT NULL DEFAULT 0,  -- guards against re-moving the SL every tick
    dry_run INTEGER NOT NULL DEFAULT 0,             -- simulated trade, never sent to Bitget at all
    position_size REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',   -- open | closed
    opened_at INTEGER NOT NULL,
    closed_at INTEGER,
    realized_pnl REAL,
    close_reason TEXT,                     -- sl_hit | tp_all_hit | manual | closed_on_exchange
    confidence REAL,
    demo INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS tp_legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL REFERENCES trades(id),
    level INTEGER NOT NULL,                -- 1, 2, 3
    price REAL NOT NULL,
    close_fraction REAL NOT NULL,
    reason TEXT NOT NULL,                  -- what this level actually targets
    bitget_order_id TEXT,                  -- Bitget's plan order id for this leg
    hit INTEGER NOT NULL DEFAULT 0,
    hit_at INTEGER
);

CREATE TABLE IF NOT EXISTS signals_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    direction TEXT NOT NULL,
    entry REAL NOT NULL,
    stop_loss REAL NOT NULL,
    sl_reason TEXT,
    confidence REAL NOT NULL,
    reasons TEXT,                          -- JSON list
    tp_reasons TEXT,                       -- JSON list
    taken INTEGER NOT NULL DEFAULT 0       -- did the bot actually act on it
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    level TEXT NOT NULL,        -- info | warning | error
    source TEXT NOT NULL,       -- 'bot' | 'api'
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    mode TEXT NOT NULL,          -- 'demo' | 'live'
    key TEXT NOT NULL,           -- 'api_key' | 'api_secret' | 'passphrase'
    value TEXT NOT NULL,         -- ENCRYPTED (see crypto_utils.py) — never stored plain
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (mode, key)
);
"""

DEFAULT_SETTINGS = {
    "bot_running": "false",
    "risk_per_trade_pct": "1.0",
    "max_concurrent_positions": "3",
    "max_daily_loss_pct": "5.0",
    "symbols": json.dumps(["BTCUSDT", "ETHUSDT"]),
    "timeframe": "15m",
    "live_mode_requested": "false",
    "dry_run": "false",
    "dry_run_starting_balance": "1000.0",
}


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with get_conn() as conn:
        # WAL mode lets bot.py and api.py (two separate processes) both read
        # and write this file concurrently without locking each other out.
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))


# ---------------- settings ----------------

def get_setting(key: str) -> Optional[str]:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_all_settings() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}


# ---------------- signals log (every signal, whether traded or not) ----------------

def log_signal(symbol: str, signal, taken: bool):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO signals_log (symbol, ts, direction, entry, stop_loss, sl_reason, "
            "confidence, reasons, tp_reasons, taken) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, int(time.time()), signal.direction, signal.entry, signal.stop_loss,
             getattr(signal, "sl_reason", ""), signal.confidence, json.dumps(signal.reasons),
             json.dumps(signal.tp_reasons), int(taken)),
        )


def get_recent_signals(limit: int = 50) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals_log ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["reasons"] = json.loads(d["reasons"]) if d["reasons"] else []
            d["tp_reasons"] = json.loads(d["tp_reasons"]) if d["tp_reasons"] else []
            out.append(d)
        return out


# ---------------- logs ----------------

def log_event(level: str, source: str, message: str):
    """
    level: 'info' | 'warning' | 'error'
    source: 'bot' | 'api'
    Called from bot.py/api.py alongside normal print()/logging output so the
    dashboard's Logs tab can show the same events without needing server
    shell access. Auto-prunes to the most recent 2000 rows so this can't
    quietly grow the db file forever on a server left running for months.
    """
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO logs (ts, level, source, message) VALUES (?, ?, ?, ?)",
            (int(time.time()), level, source, message),
        )
        conn.execute(
            "DELETE FROM logs WHERE id NOT IN "
            "(SELECT id FROM logs ORDER BY ts DESC LIMIT 2000)"
        )


def get_recent_logs(limit: int = 200, level: Optional[str] = None) -> list:
    with get_conn() as conn:
        if level:
            rows = conn.execute(
                "SELECT * FROM logs WHERE level = ? ORDER BY ts DESC LIMIT ?", (level, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM logs ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


# ---------------- trades ----------------

def open_trade(symbol: str, direction: str, entry_price: float, stop_loss: float,
                position_size: float, confidence: float, demo: bool, tp_levels: list,
                sl_reason: str = "", sl_order_id: Optional[str] = None, dry_run: bool = False) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO trades (symbol, direction, entry_price, stop_loss, sl_reason, sl_order_id, "
            "position_size, opened_at, confidence, demo, dry_run) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, direction, entry_price, stop_loss, sl_reason, sl_order_id, position_size,
             int(time.time()), confidence, int(demo), int(dry_run)),
        )
        trade_id = cur.lastrowid
        for i, tp in enumerate(tp_levels, start=1):
            conn.execute(
                "INSERT INTO tp_legs (trade_id, level, price, close_fraction, reason) "
                "VALUES (?, ?, ?, ?, ?)",
                (trade_id, i, tp.price, tp.close_fraction, tp.reason),
            )
        return trade_id


def get_dry_run_equity() -> float:
    """Starting balance + all realized PnL from dry-run (simulated) trades
    only — kept completely separate from anything touching a real Bitget
    balance."""
    starting = float(get_setting("dry_run_starting_balance") or 1000.0)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT SUM(realized_pnl) as pnl FROM trades WHERE dry_run = 1 AND status = 'closed'"
        ).fetchone()
        return starting + (row["pnl"] or 0.0)


def get_dry_run_realized_pnl_since(since_ts: int) -> float:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT SUM(realized_pnl) as pnl FROM trades WHERE dry_run = 1 AND status = 'closed' "
            "AND closed_at >= ?",
            (since_ts,),
        ).fetchone()
        return row["pnl"] or 0.0


def set_tp_leg_order_id(trade_id: int, level: int, order_id: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE tp_legs SET bitget_order_id = ? WHERE trade_id = ? AND level = ?",
            (order_id, trade_id, level),
        )


def update_trade_sl(trade_id: int, new_stop_loss: float, sl_order_id: Optional[str],
                     sl_reason: Optional[str] = None, breakeven_applied: bool = False):
    """Used after cancelling the old SL plan order and placing a fresh one —
    e.g. moving to breakeven once TP1 fills. Setting breakeven_applied=True
    here is what stops the bot from trying to redo this every loop tick."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE trades SET stop_loss = ?, sl_order_id = ?, "
            "sl_reason = COALESCE(?, sl_reason), "
            "breakeven_applied = CASE WHEN ? THEN 1 ELSE breakeven_applied END "
            "WHERE id = ?",
            (new_stop_loss, sl_order_id, sl_reason, int(breakeven_applied), trade_id),
        )


def mark_tp_hit(trade_id: int, level: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE tp_legs SET hit = 1, hit_at = ? WHERE trade_id = ? AND level = ?",
            (int(time.time()), trade_id, level),
        )


def close_trade(trade_id: int, realized_pnl: float, close_reason: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE trades SET status = 'closed', closed_at = ?, realized_pnl = ?, "
            "close_reason = ? WHERE id = ?",
            (int(time.time()), realized_pnl, close_reason, trade_id),
        )


def get_open_trades(symbol: Optional[str] = None, dry_run: Optional[bool] = None) -> list:
    with get_conn() as conn:
        conditions = ["status = 'open'"]
        params = []
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if dry_run is not None:
            conditions.append("dry_run = ?")
            params.append(int(dry_run))
        rows = conn.execute(
            "SELECT * FROM trades WHERE " + " AND ".join(conditions), params
        ).fetchall()
        trades = [dict(r) for r in rows]
        for t in trades:
            with get_conn() as c2:
                legs = c2.execute(
                    "SELECT * FROM tp_legs WHERE trade_id = ? ORDER BY level", (t["id"],)
                ).fetchall()
                t["tp_legs"] = [dict(l) for l in legs]
        return trades


def get_trade_history(limit: int = 100, dry_run: Optional[bool] = None) -> list:
    with get_conn() as conn:
        if dry_run is not None:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status = 'closed' AND dry_run = ? "
                "ORDER BY closed_at DESC LIMIT ?",
                (int(dry_run), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status = 'closed' ORDER BY closed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_stats(dry_run: Optional[bool] = None) -> dict:
    with get_conn() as conn:
        if dry_run is not None:
            row = conn.execute(
                "SELECT COUNT(*) as n, SUM(realized_pnl) as total_pnl, "
                "SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins "
                "FROM trades WHERE status = 'closed' AND dry_run = ?",
                (int(dry_run),),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) as n, SUM(realized_pnl) as total_pnl, "
                "SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins "
                "FROM trades WHERE status = 'closed'"
            ).fetchone()
        n = row["n"] or 0
        wins = row["wins"] or 0
        return {
            "closed_trades": n,
            "total_pnl": round(row["total_pnl"] or 0.0, 4),
            "win_rate_pct": round((wins / n * 100) if n else 0.0, 1),
        }


def get_realized_pnl_since(since_ts: int) -> float:
    """Sum of realized_pnl for trades closed at/after since_ts — used to
    drive the max_daily_loss_pct circuit breaker with a real number instead
    of a hardcoded 0.0."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT SUM(realized_pnl) as pnl FROM trades WHERE status = 'closed' AND closed_at >= ?",
            (since_ts,),
        ).fetchone()
        return row["pnl"] or 0.0


# ---------------- credentials (encrypted Bitget API keys per mode) ----------------

def set_credential(mode: str, key: str, encrypted_value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO credentials (mode, key, value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(mode, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (mode, key, encrypted_value, int(time.time())),
        )


def get_credential(mode: str, key: str) -> Optional[str]:
    """Returns the ENCRYPTED value as stored — caller decrypts with
    crypto_utils.decrypt(). This is the only read path; there is
    deliberately no endpoint that hands the decrypted value to the
    dashboard."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM credentials WHERE mode = ? AND key = ?", (mode, key)
        ).fetchone()
        return row["value"] if row else None


def get_credential_status() -> dict:
    """Safe-to-expose summary for the dashboard: which modes have
    credentials configured and when they were last updated. Never includes
    the actual secret values."""
    with get_conn() as conn:
        rows = conn.execute("SELECT mode, key, updated_at FROM credentials").fetchall()
    have = {"demo": set(), "live": set()}
    updated = {"demo": None, "live": None}
    for r in rows:
        if r["mode"] in have:
            have[r["mode"]].add(r["key"])
            updated[r["mode"]] = r["updated_at"]
    required = {"api_key", "api_secret", "passphrase"}
    return {
        mode: {"configured": required.issubset(have[mode]), "updated_at": updated[mode]}
        for mode in ("demo", "live")
    }
