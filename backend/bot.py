"""
Kehlo Trading — main bot loop.

Ties bitget_client + smc_engine + risk_manager + database together:
  1. pull candles for each watched symbol
  2. run the SMC engine, log every signal to the DB (taken or not — useful
     later for checking how good the signals actually are)
  3. if a signal exists and risk limits allow it (including a REAL daily-loss
     circuit breaker, not a stub), place the entry + SL leg + every TP leg
     on Bitget, and record the trade with their order ids
  4. manage_open_positions(): detect partial TP fills by watching live
     position size shrink, mark those legs hit, and move the SL to
     breakeven once TP1 fills
  5. reconcile_open_trades(): once a position fully disappears, pull the
     ACTUAL realised PnL from Bitget's closed-position history and close
     the trade out with a real number, not a guess

Demo vs. live: the bot ALWAYS boots in demo. Every few seconds it checks the
`live_mode_requested` setting (set by api.py's PIN-gated /api/mode/set) and,
if it differs from what's currently active, rebuilds its Bitget client with
that mode's credentials via load_credentials_for_mode() — demo and live use
completely separate API keys, so this is a real client swap, not a flag
flip. If the requested mode has no credentials configured yet, the switch
is refused and logged rather than silently failing.

⚠️ THIS HAS NOT BEEN RUN AGAINST THE REAL BITGET API — this sandbox's
network can't reach api.bitget.com. Every endpoint used here was verified
against Bitget's official v2 docs, but you must test it end-to-end against
DEMO trading on your own server before ever switching to a live key. A few
response field names (marked with comments below — 'total' as the
position-size field, 'marginCoin'/'accountEquity' for account balance) are
best inferred from docs rather than a live response; print the raw response
once during your first demo run and adjust if Bitget shapes them differently.
"""

import json
import os
import time
import traceback
from datetime import datetime, timezone

from dotenv import load_dotenv

from bitget_client import BitgetClient, BitgetCredentials, BitgetAPIError
from smc_engine import Candle, SMCEngine
from risk_manager import RiskManager
import database as db
import crypto_utils as cu

load_dotenv()

POLL_INTERVAL_SECONDS = 60
MODE_CHECK_INTERVAL_SECONDS = 5
GRANULARITY = os.getenv("TIMEFRAME", "15m")
PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "USDT-FUTURES")


def _start_of_today_ts() -> int:
    now = datetime.now(timezone.utc)
    return int(datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp())


def load_credentials_for_mode(demo: bool) -> BitgetCredentials:
    """
    Credentials can come from two places: the database (attached through
    the dashboard, encrypted at rest) or .env (BITGET_DEMO_* / BITGET_LIVE_*).
    The database is checked first so a dashboard update takes effect without
    editing files on the server; .env is the bootstrap/fallback path.
    Demo and live need SEPARATE Bitget API keys — one will not work in the
    other mode — so both sets can be configured independently and the bot
    switches which one it's using, it never reuses one for the other.
    """
    mode = "demo" if demo else "live"
    prefix = "BITGET_DEMO" if demo else "BITGET_LIVE"

    def _get(field: str, env_name: str) -> str:
        enc = db.get_credential(mode, field)
        if enc:
            return cu.decrypt(enc)
        return os.getenv(env_name, "")

    api_key = _get("api_key", f"{prefix}_API_KEY")
    api_secret = _get("api_secret", f"{prefix}_API_SECRET")
    passphrase = _get("passphrase", f"{prefix}_PASSPHRASE")

    if not (api_key and api_secret and passphrase):
        raise ValueError(
            f"No complete {mode} credentials found (checked database, then "
            f"{prefix}_API_KEY/{prefix}_API_SECRET/{prefix}_PASSPHRASE in .env). "
            f"Attach {mode} API keys through the dashboard or .env before using this mode."
        )
    return BitgetCredentials(api_key=api_key, api_secret=api_secret, passphrase=passphrase, demo=demo)


def _log(level: str, message: str):
    """Prints to the console (systemd/pm2/docker logs still show everything)
    AND writes to the db so the dashboard's Logs tab shows it without
    needing shell access to the server."""
    print(f"[{level.upper()}] {message}")
    try:
        db.log_event(level, "bot", message)
    except Exception:
        pass  # never let logging itself crash the bot loop


def get_equity(client: BitgetClient) -> float:
    """NOTE: 'marginCoin'/'accountEquity' field names are inferred from docs —
    print(accounts) once against demo trading and adjust if the real
    response shapes these differently."""
    accounts = client.get_accounts(product_type=PRODUCT_TYPE)
    for acc in accounts:
        if acc.get("marginCoin", "").upper() == "USDT":
            return float(acc.get("accountEquity") or acc.get("usdtEquity") or 0)
    return 0.0


def process_symbol(client: BitgetClient, engine: SMCEngine, risk_mgr: RiskManager, symbol: str):
    dry_run = db.get_setting("dry_run") == "true"

    raw_candles = client.get_candles(symbol, GRANULARITY, product_type=PRODUCT_TYPE, limit=150)
    candles = [Candle.from_bitget_row(row) for row in raw_candles]
    if len(candles) > 1 and candles[0].ts > candles[-1].ts:
        candles.reverse()  # make sure it's oldest-first regardless of API order

    if dry_run:
        _dry_run_check_fills(symbol, candles)

    signal = engine.generate_signal(candles)
    if signal is None:
        return

    equity = db.get_dry_run_equity() if dry_run else get_equity(client)
    today_pnl_pct = 0.0
    if equity > 0:
        since = _start_of_today_ts()
        today_pnl = (db.get_dry_run_realized_pnl_since(since) if dry_run
                     else db.get_realized_pnl_since(since))
        today_pnl_pct = (today_pnl / equity) * 100

    already_open_same_symbol = any(t["symbol"] == symbol for t in db.get_open_trades(symbol))
    can_open = risk_mgr.can_open_new_position(
        open_position_count=len(db.get_open_trades()),
        today_realised_pnl_pct=today_pnl_pct,
    )
    taken = can_open and not already_open_same_symbol
    db.log_signal(symbol, signal, taken=taken)
    if not taken:
        if not can_open and today_pnl_pct <= -abs(risk_mgr.max_daily_loss_pct):
            _log("warning", f"[{symbol}] signal skipped — daily loss circuit breaker tripped "
                             f"({today_pnl_pct:.2f}% today)")
        return

    if equity <= 0:
        _log("warning", f"[{symbol}] could not read {'dry-run' if dry_run else 'account'} equity, skipping trade")
        return

    plan = risk_mgr.build_trade_plan(
        direction=signal.direction, entry=signal.entry, stop_loss=signal.stop_loss,
        take_profits=signal.take_profits, account_equity=equity, tp_reasons=signal.tp_reasons,
    )

    if dry_run:
        # Signal and risk math are real — only the actual Bitget order calls
        # are skipped. The trade is recorded exactly like a real one so it
        # shows up identically on the dashboard, just tagged dry_run=1.
        trade_id = db.open_trade(
            symbol=symbol, direction=signal.direction, entry_price=signal.entry,
            stop_loss=signal.stop_loss, sl_reason=signal.sl_reason, sl_order_id=None,
            position_size=plan.position_size, confidence=signal.confidence,
            demo=client.creds.demo, tp_levels=plan.tp_levels, dry_run=True,
        )
        tp_summary = " | ".join(f"TP{i+1} {tp.price:.4f} ({tp.reason})" for i, tp in enumerate(plan.tp_levels))
        _log("info", f"[DRY RUN][{symbol}] simulated trade #{trade_id}: {signal.direction} @ "
                      f"{signal.entry:.4f} (SL {signal.stop_loss:.4f} — {signal.sl_reason}), "
                      f"size {plan.position_size:.4f}, confidence {signal.confidence:.2f}. {tp_summary} "
                      f"— NOT sent to Bitget")
        return

    side = "buy" if signal.direction == "long" else "sell"
    hold_side = "long" if signal.direction == "long" else "short"
    size_str = f"{plan.position_size:.6f}"

    client.place_order(symbol=symbol, side=side, trade_side="open", order_type="market",
                        size=size_str, product_type=PRODUCT_TYPE)

    sl_result = client.place_tpsl_leg(symbol=symbol, plan_type="loss_plan",
                                       trigger_price=f"{plan.stop_loss:.6f}", hold_side=hold_side,
                                       size=size_str, product_type=PRODUCT_TYPE.lower())
    sl_order_id = sl_result.get("orderId") if isinstance(sl_result, dict) else None

    tp_order_ids = []
    for tp in plan.tp_levels:
        leg_size = f"{plan.position_size * tp.close_fraction:.6f}"
        tp_result = client.place_tpsl_leg(symbol=symbol, plan_type="profit_plan", trigger_price=f"{tp.price:.6f}",
                                           hold_side=hold_side, size=leg_size, product_type=PRODUCT_TYPE.lower())
        tp_order_ids.append(tp_result.get("orderId") if isinstance(tp_result, dict) else None)

    trade_id = db.open_trade(
        symbol=symbol, direction=signal.direction, entry_price=signal.entry,
        stop_loss=signal.stop_loss, sl_reason=signal.sl_reason, sl_order_id=sl_order_id,
        position_size=plan.position_size,
        confidence=signal.confidence, demo=client.creds.demo, tp_levels=plan.tp_levels,
    )
    for level, order_id in enumerate(tp_order_ids, start=1):
        if order_id:
            db.set_tp_leg_order_id(trade_id, level, order_id)

    tp_summary = " | ".join(f"TP{i+1} {tp.price:.4f} ({tp.reason})" for i, tp in enumerate(plan.tp_levels))
    _log("info", f"[{symbol}] opened trade #{trade_id}: {signal.direction} @ {signal.entry:.4f} "
                  f"(SL {signal.stop_loss:.4f} — {signal.sl_reason}), size {plan.position_size:.4f}, "
                  f"confidence {signal.confidence:.2f}. {tp_summary}")


def _dry_run_check_fills(symbol: str, candles: list):
    """
    Dry-run only. Checks every simulated open trade on this symbol against
    the latest candle's high/low to see if a TP or SL level would have been
    hit — entirely in the database, no Bitget account calls at all. Assumes
    a perfect fill exactly at the planned price (no slippage), since
    nothing is actually being executed on an order book.
    """
    if not candles:
        return
    last = candles[-1]

    for trade in db.get_open_trades(symbol):
        if not trade.get("dry_run"):
            continue

        direction = trade["direction"]
        entry = trade["entry_price"]
        size = trade["position_size"]

        legs = sorted(trade["tp_legs"], key=lambda l: l["level"])
        sl_hit = (last.low <= trade["stop_loss"]) if direction == "long" else (last.high >= trade["stop_loss"])

        newly_hit_levels = []
        for leg in legs:
            if leg["hit"] == 1:
                continue
            tp_reached = (last.high >= leg["price"]) if direction == "long" else (last.low <= leg["price"])
            if tp_reached:
                db.mark_tp_hit(trade["id"], leg["level"])
                leg["hit"] = 1
                newly_hit_levels.append(leg)
                _log("info", f"[DRY RUN][{symbol}] trade #{trade['id']} TP{leg['level']} "
                              f"simulated fill @ {leg['price']:.4f}")

        for leg in newly_hit_levels:
            if leg["level"] == 1 and not trade["breakeven_applied"]:
                db.update_trade_sl(trade["id"], new_stop_loss=entry, sl_order_id=None,
                                    sl_reason="moved to breakeven after TP1 filled (simulated)",
                                    breakeven_applied=True)
                trade["stop_loss"] = entry
                trade["breakeven_applied"] = 1
                _log("info", f"[DRY RUN][{symbol}] trade #{trade['id']} SL moved to breakeven "
                              f"({entry:.4f}) after TP1")

        legs = sorted(db.get_open_trades(symbol), key=lambda t: t["id"])
        legs = next((t["tp_legs"] for t in legs if t["id"] == trade["id"]), legs)
        all_hit = all(l["hit"] == 1 for l in legs) if legs else False

        if all_hit:
            pnl = sum(_dry_run_pnl_for_leg(direction, entry, l["price"], size, l["close_fraction"]) for l in legs)
            db.close_trade(trade["id"], realized_pnl=pnl, close_reason="tp_all_hit")
            _log("info", f"[DRY RUN][{symbol}] trade #{trade['id']} closed — all TPs hit, pnl {pnl:.4f}")
        elif sl_hit:
            hit_fraction = sum(l["close_fraction"] for l in legs if l["hit"] == 1)
            remaining_fraction = max(0.0, 1.0 - hit_fraction)
            pnl = sum(_dry_run_pnl_for_leg(direction, entry, l["price"], size, l["close_fraction"])
                      for l in legs if l["hit"] == 1)
            pnl += _dry_run_pnl_for_leg(direction, entry, trade["stop_loss"], size, remaining_fraction)
            reason = "breakeven" if trade["breakeven_applied"] else "sl_hit"
            db.close_trade(trade["id"], realized_pnl=pnl, close_reason=reason)
            _log("info", f"[DRY RUN][{symbol}] trade #{trade['id']} closed — {reason}, pnl {pnl:.4f}")


def _dry_run_pnl_for_leg(direction: str, entry: float, fill_price: float, size: float, fraction: float) -> float:
    sign = 1 if direction == "long" else -1
    return (fill_price - entry) * sign * size * fraction


def manage_open_positions(client: BitgetClient):
    """
    For each open trade: compare the live position size to what we recorded
    when it was opened. A smaller live size means one or more TP legs have
    filled. Mark those hit, and once TP1 is confirmed filled, cancel the
    original SL and replace it with one at breakeven (entry price) for
    whatever size remains.

    NOTE: 'total' as the live position-size field is inferred from docs —
    verify against a real get_positions() response on your first demo test.
    """
    try:
        live_positions = client.get_positions(product_type=PRODUCT_TYPE)
    except BitgetAPIError as e:
        _log("error", f"manage_open_positions: could not fetch positions ({e})")
        return

    live_by_symbol = {p["symbol"]: p for p in live_positions if float(p.get("total", 0)) > 0}

    for trade in db.get_open_trades():
        if trade.get("dry_run"):
            continue  # simulated trades are managed entirely by _dry_run_check_fills instead

        live = live_by_symbol.get(trade["symbol"])
        if live is None:
            continue  # fully closed — reconcile_open_trades() handles this case

        original_size = trade["position_size"]
        if original_size <= 0:
            continue
        live_size = float(live.get("total", 0))
        filled_fraction = max(0.0, min(1.0, 1 - (live_size / original_size)))

        legs = sorted(trade["tp_legs"], key=lambda l: l["level"])
        cumulative = 0.0
        for leg in legs:
            cumulative += leg["close_fraction"]
            # small tolerance for fees/rounding — live size is rarely an
            # exact fraction of the original
            if leg["hit"] != 1 and filled_fraction >= cumulative - 0.03:
                db.mark_tp_hit(trade["id"], leg["level"])
                leg["hit"] = 1
                _log("info", f"[{trade['symbol']}] trade #{trade['id']} TP{leg['level']} appears "
                              f"filled (~{filled_fraction*100:.0f}% of position closed)")

        tp1 = next((l for l in legs if l["level"] == 1), None)
        if tp1 and tp1["hit"] == 1 and not trade["breakeven_applied"]:
            hold_side = "long" if trade["direction"] == "long" else "short"
            try:
                if trade.get("sl_order_id"):
                    client.cancel_tpsl_order(trade["symbol"], trade["sl_order_id"], product_type=PRODUCT_TYPE)
                new_sl = client.place_tpsl_leg(
                    symbol=trade["symbol"], plan_type="loss_plan",
                    trigger_price=f"{trade['entry_price']:.6f}", hold_side=hold_side,
                    size=f"{live_size:.6f}", product_type=PRODUCT_TYPE.lower(),
                )
                new_sl_order_id = new_sl.get("orderId") if isinstance(new_sl, dict) else None
                db.update_trade_sl(trade["id"], new_stop_loss=trade["entry_price"],
                                    sl_order_id=new_sl_order_id,
                                    sl_reason="moved to breakeven after TP1 filled",
                                    breakeven_applied=True)
                _log("info", f"[{trade['symbol']}] trade #{trade['id']} SL moved to breakeven "
                              f"({trade['entry_price']:.4f}) after TP1")
            except BitgetAPIError as e:
                _log("error", f"[{trade['symbol']}] trade #{trade['id']} failed to move SL to "
                               f"breakeven: {e}")


def reconcile_open_trades(client: BitgetClient):
    """
    Compares Bitget's live positions against what the database thinks is
    open, and closes out trades whose position has disappeared — pulling
    the ACTUAL realised PnL (net of fees) from Bitget's closed-position
    history instead of guessing 0.0.
    """
    try:
        live_positions = client.get_positions(product_type=PRODUCT_TYPE)
    except BitgetAPIError as e:
        _log("error", f"reconcile: could not fetch positions ({e})")
        return

    live_symbols = {p["symbol"] for p in live_positions if float(p.get("total", 0)) > 0}

    for trade in db.get_open_trades():
        if trade.get("dry_run"):
            continue  # simulated trades are closed by _dry_run_check_fills instead

        if trade["symbol"] in live_symbols:
            continue

        pnl = 0.0
        try:
            history = client.get_history_positions(symbol=trade["symbol"], product_type=PRODUCT_TYPE, limit=10)
            # best-effort match: same symbol, opened near the price we recorded,
            # and closed at/after we opened our side of it
            match = next(
                (h for h in history if
                 abs(float(h.get("openAvgPrice", 0)) - trade["entry_price"]) / max(trade["entry_price"], 1e-9) < 0.01
                 and int(h.get("ctime", 0)) >= trade["opened_at"] * 1000 - 5000),
                None,
            )
            if match:
                pnl = float(match.get("netProfit", 0))
            else:
                _log("warning", f"[{trade['symbol']}] trade #{trade['id']} closed but no matching "
                                 f"history-position record found — pnl recorded as 0.0, verify on Bitget")
        except BitgetAPIError as e:
            _log("error", f"[{trade['symbol']}] could not fetch history-position for trade "
                           f"#{trade['id']}: {e}")

        all_tp_hit = bool(trade["tp_legs"]) and all(leg["hit"] == 1 for leg in trade["tp_legs"])
        if all_tp_hit:
            close_reason = "tp_all_hit"
        elif trade["breakeven_applied"]:
            close_reason = "breakeven"
        else:
            close_reason = "sl_hit"

        db.close_trade(trade["id"], realized_pnl=pnl, close_reason=close_reason)
        _log("info", f"[{trade['symbol']}] trade #{trade['id']} closed — pnl {pnl:.4f}, reason {close_reason}")


def main():
    db.init_db()
    engine = SMCEngine(swing_lookback=3)

    state = {"client": None, "demo": True}

    def switch_to(demo: bool):
        creds = load_credentials_for_mode(demo)  # raises ValueError if that mode isn't configured
        state["client"] = BitgetClient(creds)
        state["demo"] = demo
        db.set_setting("live_mode_active", "false" if demo else "true")
        _log("info", f"Now operating in {'DEMO' if demo else 'LIVE — REAL MONEY'} mode "
                      f"(product_type={PRODUCT_TYPE}, timeframe={GRANULARITY})")

    # Always boot in demo, never assume live on startup — going live is only
    # ever a deliberate runtime switch (see below), not a boot-time default.
    try:
        switch_to(demo=True)
    except ValueError as e:
        if db.get_setting("dry_run") == "true":
            # Dry run never touches account endpoints (positions, orders,
            # balance) — only public candle data, which may work even
            # without a correctly-matched demo/live key. Don't crash the
            # whole loop over this; let get_candles() fail per-symbol
            # instead if it truly can't authenticate at all.
            _log("warning", f"No working demo/live credentials yet ({e}). Continuing in "
                             f"DRY RUN anyway — candle fetches may still fail per-symbol "
                             f"until ANY valid Bitget key is attached (even a live-labeled "
                             f"one is fine for dry run, since only public market data is used).")
            state["client"] = BitgetClient(BitgetCredentials(api_key="", api_secret="",
                                                              passphrase="", demo=True))
            state["demo"] = True
        else:
            _log("error", f"Cannot start: {e}")
            raise
    _log("info", "Bot process started")

    last_mode_check = 0.0

    while True:
        now = time.time()
        if now - last_mode_check >= MODE_CHECK_INTERVAL_SECONDS:
            last_mode_check = now
            requested_live = db.get_setting("live_mode_requested") == "true"
            currently_live = not state["demo"]
            if requested_live != currently_live:
                try:
                    switch_to(demo=not requested_live)
                except ValueError as e:
                    _log("error", f"Could not switch to {'live' if requested_live else 'demo'} "
                                   f"mode: {e}. Staying on {'demo' if state['demo'] else 'live'}.")
                    # revert the request so the dashboard doesn't show a stuck pending switch
                    db.set_setting("live_mode_requested", "false" if state["demo"] else "true")
            else:
                db.set_setting("live_mode_active", "false" if state["demo"] else "true")

        client = state["client"]
        settings = db.get_all_settings()
        if settings.get("bot_running", "false").lower() != "true":
            time.sleep(5)
            continue

        risk_mgr = RiskManager(
            risk_per_trade_pct=float(settings.get("risk_per_trade_pct", 1.0)),
            max_concurrent_positions=int(settings.get("max_concurrent_positions", 3)),
            max_daily_loss_pct=float(settings.get("max_daily_loss_pct", 5.0)),
        )
        symbols = json.loads(settings.get("symbols", '["BTCUSDT"]'))
        dry_run = settings.get("dry_run", "false") == "true"

        for symbol in symbols:
            try:
                process_symbol(client, engine, risk_mgr, symbol)
            except BitgetAPIError as e:
                _log("error", f"[{symbol}] Bitget API error: {e}")
            except Exception:
                _log("error", f"[{symbol}] unexpected error:\n{traceback.format_exc()}")

        # In dry run, nothing was ever sent to Bitget, so there's nothing
        # real to reconcile — skip these entirely rather than making
        # account calls that would just fail for someone who hasn't sorted
        # out a working demo/live key yet.
        if not dry_run:
            try:
                manage_open_positions(client)
            except Exception:
                _log("error", f"manage_open_positions crashed:\n{traceback.format_exc()}")

            try:
                reconcile_open_trades(client)
            except Exception:
                _log("error", f"reconcile_open_trades crashed:\n{traceback.format_exc()}")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
