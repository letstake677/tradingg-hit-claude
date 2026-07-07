"""
Combined entrypoint — for single-service platforms (Railway, Render, etc.)
where bot.py and api.py CAN'T share a volume the way two containers in
docker-compose.yml can (Railway in particular does not support attaching
one volume to two separate services — confirmed in their own docs, which
recommend a real database for that instead of a raw volume).

Running both in one process/container sidesteps that: one service, one
volume, one shared SQLite file. The tradeoff is they restart together
if either crashes — a fine trade for a single-operator bot, and arguably
fine given they already share one database.

If you're on a VPS with docker-compose.yml instead, you don't need this
file — that setup runs bot.py and api.py as independent containers so one
can restart without the other, sharing one mounted volume directly.

Railway-specific notes:
  - Set this as the service's Custom Start Command: python3 start_combined.py
  - Set Root Directory to `backend` (this file's folder)
  - Attach a Volume, mount path e.g. /data, and set KEHLO_DB_PATH=/data/kehlo_trading.db
  - Don't hardcode a port — Railway assigns one via the PORT env var, read below
  - Use "Generate Domain" in the service's Networking settings for a free
    HTTPS URL — no Caddy needed here, Railway terminates TLS for you
"""

import os
import threading
import time
import traceback

import uvicorn

import bot as bot_module
import database as db


def run_bot_loop():
    """bot.main() runs forever unless something unexpected kills it — if
    that happens, log it and restart rather than silently leaving the
    thread dead while the API keeps responding as if all is well."""
    while True:
        try:
            bot_module.main()
        except Exception:
            msg = f"bot.py main() crashed, restarting in 10s:\n{traceback.format_exc()}"
            print(f"[FATAL] {msg}")
            try:
                db.log_event("error", "bot", msg)
            except Exception:
                pass
            time.sleep(10)


def main():
    db.init_db()

    t = threading.Thread(target=run_bot_loop, daemon=True, name="kehlo-bot-loop")
    t.start()

    port = int(os.getenv("PORT", "8000"))  # Railway injects PORT — never hardcode it
    uvicorn.run("api:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
