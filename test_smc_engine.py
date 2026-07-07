"""
Offline sanity test for the SMC engine + risk manager — no network calls.
Uses a hand-built candle sequence with a clear structure (uptrend, reversal
down, sharp reversal back up, pullback into the fresh order block) so we can
verify swing detection, BOS/CHoCH, order blocks, and signal generation all
work correctly end-to-end before wiring up the real Bitget connection.

Run: python3 test_smc_engine.py
"""

from smc_engine import Candle, SMCEngine
from risk_manager import RiskManager


def make_candles(closes, wick=0.3):
    """Turns a list of closing prices into candles with small wicks."""
    candles = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        hi = max(o, c) + wick
        lo = min(o, c) - wick
        candles.append(Candle(ts=i, open=o, high=hi, low=lo, close=c, volume=100))
        prev = c
    return candles


def build_scenario():
    closes = []
    # Leg 1: uptrend 100 -> 115, tiny early dip creates a minor swing low
    closes += [100, 101, 100.5, 99.8, 100.8, 102, 103.5, 105, 106.5, 108,
               109.5, 111, 112.5, 114, 115]
    # Leg 2: reversal down, breaks below the early minor low -> CHoCH bearish
    closes += [113.5, 111, 108, 105, 102, 100, 98.5, 98]
    # Leg 3: sharp reversal back up, breaks back above the leg-1 high -> CHoCH bullish
    closes += [100, 104, 110, 116, 118]
    # Leg 4: pullback retracing into the fresh bullish order block
    closes += [115, 110, 104, 100, 98.3]
    return make_candles(closes)


def main():
    candles = build_scenario()
    engine = SMCEngine(swing_lookback=2)

    swings = engine.find_swings(candles)
    trend, events = engine.detect_structure(candles, swings)
    obs = engine.find_order_blocks(candles, events)
    fvgs = engine.find_fvgs(candles)

    print(f"Candles           : {len(candles)}")
    print(f"Swings found      : {len(swings)}")
    print(f"Final trend       : {trend.value}")
    print(f"Structure events  : {[(i, e.name) for i, e in events]}")
    print(f"Order blocks      : {[(ob.kind, round(ob.bottom, 2), round(ob.top, 2)) for ob in obs]}")
    print(f"Fair value gaps   : {len(fvgs)}")

    signal = engine.generate_signal(candles)
    if signal:
        print("\n--- SIGNAL GENERATED ---")
        print(f"direction    : {signal.direction}")
        print(f"entry        : {signal.entry:.2f}")
        print(f"stop_loss    : {signal.stop_loss:.2f}  -  {signal.sl_reason}")
        print(f"confidence   : {signal.confidence:.2f}")
        print(f"reasons      : {signal.reasons}")
        print("take_profits (price -> what it actually targets):")
        for tp, why in zip(signal.take_profits, signal.tp_reasons):
            print(f"  {tp:7.2f}  -  {why}")

        rm = RiskManager(risk_per_trade_pct=1.0)
        plan = rm.build_trade_plan(
            direction=signal.direction, entry=signal.entry, stop_loss=signal.stop_loss,
            take_profits=signal.take_profits, account_equity=1000.0,
        )
        print("\n--- TRADE PLAN ($1,000 demo account, 1% risk) ---")
        print(f"position_size: {plan.position_size:.4f} units")
        print(f"risk_amount  : ${plan.risk_amount():.2f}")
        for i, tp in enumerate(plan.tp_levels, 1):
            print(f"TP{i}: {tp.price:.2f}  close {tp.close_fraction*100:.0f}%  "
                  f"breakeven_after={tp.move_sl_to_breakeven}")
    else:
        print("\nNo signal fired on this scenario.")


if __name__ == "__main__":
    main()
