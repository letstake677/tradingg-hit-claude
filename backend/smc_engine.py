"""
SMC (Smart Money Concepts) signal engine for Kehlo Trading.

This is a RULES-BASED APPROXIMATION of discretionary SMC analysis: market
structure (BOS/CHoCH), order blocks, fair value gaps (FVG), and liquidity
zones. Real SMC traders apply judgement; codifying it always means picking
concrete thresholds (swing_lookback, fvg tolerance, etc).

Treat this as v1 of the strategy, not a finished edge. Backtest it across
real historical candles, look at where it's wrong, and tune the numbers —
that loop is what turns this into something worth risking money on.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @staticmethod
    def from_bitget_row(row: list) -> "Candle":
        # Bitget candle row: [ts, open, high, low, close, base_volume, quote_volume]
        return Candle(
            ts=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]) if len(row) > 5 else 0.0,
        )


class Trend(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    RANGING = "ranging"


class StructureEvent(Enum):
    BOS_BULLISH = "bos_bullish"
    BOS_BEARISH = "bos_bearish"
    CHOCH_BULLISH = "choch_bullish"
    CHOCH_BEARISH = "choch_bearish"


@dataclass
class SwingPoint:
    index: int
    price: float
    kind: str  # 'high' or 'low'


@dataclass
class OrderBlock:
    index: int
    kind: str  # 'bullish' or 'bearish'
    top: float
    bottom: float
    mitigated: bool = False


@dataclass
class FairValueGap:
    index: int
    kind: str  # 'bullish' or 'bearish'
    top: float
    bottom: float
    filled: bool = False


@dataclass
class Signal:
    direction: str  # 'long' or 'short'
    entry: float
    stop_loss: float
    sl_reason: str  # why the stop sits exactly there — never just "a small % away"
    take_profits: list
    tp_reasons: list  # what each TP level actually targets — check this before trusting a TP
    has_real_tp_structure: bool  # False = every TP is a raw R-multiple guess, not a real level
    confidence: float  # 0-1 rough score based on how many confluences lined up
    reasons: list = field(default_factory=list)


class SMCEngine:
    def __init__(self, swing_lookback: int = 3, fvg_min_gap_pct: float = 0.0005):
        """
        swing_lookback: candles needed on EACH side to confirm a fractal swing
                        point (higher = fewer, more significant swings)
        fvg_min_gap_pct: minimum imbalance size (as % of price) to count as a
                         real FVG — filters out noise on tight ranges
        """
        self.swing_lookback = swing_lookback
        self.fvg_min_gap_pct = fvg_min_gap_pct

    # ---------------- swing points ----------------

    def find_swings(self, candles: list) -> list:
        n = len(candles)
        lb = self.swing_lookback
        swings = []
        for i in range(lb, n - lb):
            window = candles[i - lb:i + lb + 1]
            if candles[i].high == max(c.high for c in window):
                swings.append(SwingPoint(index=i, price=candles[i].high, kind="high"))
            if candles[i].low == min(c.low for c in window):
                swings.append(SwingPoint(index=i, price=candles[i].low, kind="low"))
        swings.sort(key=lambda s: s.index)
        return swings

    # ---------------- market structure ----------------

    def detect_structure(self, candles: list, swings: list):
        """
        Walks candles in order, tracking the most recent confirmed swing
        high/low, and flags a BOS when price closes beyond it in the
        direction of the current trend, or a CHoCH when it closes beyond it
        AGAINST the current trend (signalling a possible reversal).
        Returns (trend, events) where events is [(candle_index, StructureEvent), ...]
        """
        events = []
        trend = Trend.RANGING
        last_high = None
        last_low = None

        swings_by_index = {}
        for s in swings:
            swings_by_index.setdefault(s.index, []).append(s)

        for idx, candle in enumerate(candles):
            if last_high and candle.close > last_high.price:
                if trend == Trend.BEARISH:
                    events.append((idx, StructureEvent.CHOCH_BULLISH))
                    trend = Trend.BULLISH
                elif trend == Trend.BULLISH:
                    events.append((idx, StructureEvent.BOS_BULLISH))
                else:
                    trend = Trend.BULLISH
                last_high = None

            if last_low and candle.close < last_low.price:
                if trend == Trend.BULLISH:
                    events.append((idx, StructureEvent.CHOCH_BEARISH))
                    trend = Trend.BEARISH
                elif trend == Trend.BEARISH:
                    events.append((idx, StructureEvent.BOS_BEARISH))
                else:
                    trend = Trend.BEARISH
                last_low = None

            for s in swings_by_index.get(idx, []):
                if s.kind == "high":
                    last_high = s
                else:
                    last_low = s

        return trend, events

    # ---------------- order blocks ----------------

    def find_order_blocks(self, candles: list, events: list) -> list:
        obs = []
        for idx, event in events:
            if event in (StructureEvent.BOS_BULLISH, StructureEvent.CHOCH_BULLISH):
                j = idx
                while j > 0 and candles[j].close >= candles[j].open:
                    j -= 1
                if j >= 0:
                    obs.append(OrderBlock(index=j, kind="bullish",
                                           top=candles[j].high, bottom=candles[j].low))
            elif event in (StructureEvent.BOS_BEARISH, StructureEvent.CHOCH_BEARISH):
                j = idx
                while j > 0 and candles[j].close <= candles[j].open:
                    j -= 1
                if j >= 0:
                    obs.append(OrderBlock(index=j, kind="bearish",
                                           top=candles[j].high, bottom=candles[j].low))
        return obs

    # ---------------- fair value gaps ----------------

    def find_fvgs(self, candles: list) -> list:
        fvgs = []
        for i in range(2, len(candles)):
            c1, c3 = candles[i - 2], candles[i]
            mid_price = candles[i - 1].close
            if mid_price <= 0:
                continue
            if c1.high < c3.low and (c3.low - c1.high) / mid_price >= self.fvg_min_gap_pct:
                fvgs.append(FairValueGap(index=i - 1, kind="bullish", top=c3.low, bottom=c1.high))
            if c1.low > c3.high and (c1.low - c3.high) / mid_price >= self.fvg_min_gap_pct:
                fvgs.append(FairValueGap(index=i - 1, kind="bearish", top=c1.low, bottom=c3.high))
        return fvgs

    # ---------------- liquidity zones (equal highs/lows) ----------------

    def find_liquidity_zones(self, swings: list, tolerance_pct: float = 0.001) -> list:
        zones = []
        for kind, pts in (("sell_side", [s for s in swings if s.kind == "high"]),
                          ("buy_side", [s for s in swings if s.kind == "low"])):
            used = set()
            for i, a in enumerate(pts):
                if i in used:
                    continue
                cluster = [a]
                for j in range(i + 1, len(pts)):
                    b = pts[j]
                    if j not in used and abs(b.price - a.price) / a.price <= tolerance_pct:
                        cluster.append(b)
                        used.add(j)
                if len(cluster) >= 2:
                    zones.append({
                        "kind": kind,
                        "price": sum(c.price for c in cluster) / len(cluster),
                        "touches": len(cluster),
                    })
        return zones

    # ---------------- structure-based TP targets ----------------

    def _collect_targets(self, direction: str, entry: float, order_blocks: list,
                          fvgs: list, liquidity: list, swings: list, min_distance: float) -> list:
        """
        Real chart levels in the trade's direction — NOT a fixed multiple of
        risk. For a long: the nearest opposing (bearish) order blocks/FVGs,
        sell-side liquidity pools, and swing highs sitting above entry.
        For a short: the mirror image, all below entry.
        These are the levels price has an actual reason to react at — the
        whole point of asking "will TP ever be reached" is answered here,
        not by picking a round R-multiple.
        Returns [(price, label), ...] sorted nearest-to-entry first.
        """
        raw = []
        if direction == "long":
            for ob in order_blocks:
                if ob.kind == "bearish" and ob.bottom > entry:
                    raw.append((ob.bottom, "unmitigated bearish order block"))
            for f in fvgs:
                if f.kind == "bearish" and f.bottom > entry:
                    raw.append((f.bottom, "unfilled bearish FVG"))
            for z in liquidity:
                if z["kind"] == "sell_side" and z["price"] > entry:
                    raw.append((z["price"], f"liquidity pool ({z['touches']} equal highs)"))
            for s in swings:
                if s.kind == "high" and s.price > entry:
                    raw.append((s.price, "prior swing high"))
        else:
            for ob in order_blocks:
                if ob.kind == "bullish" and ob.top < entry:
                    raw.append((ob.top, "unmitigated bullish order block"))
            for f in fvgs:
                if f.kind == "bullish" and f.top < entry:
                    raw.append((f.top, "unfilled bullish FVG"))
            for z in liquidity:
                if z["kind"] == "buy_side" and z["price"] < entry:
                    raw.append((z["price"], f"liquidity pool ({z['touches']} equal lows)"))
            for s in swings:
                if s.kind == "low" and s.price < entry:
                    raw.append((s.price, "prior swing low"))

        # drop anything too close to be worth a TP slot, then sort nearest-first
        raw = [(p, label) for p, label in raw if abs(p - entry) >= min_distance]
        raw.sort(key=lambda t: abs(t[0] - entry))

        # merge near-duplicate levels (within 0.15% of each other) so an OB
        # and a liquidity pool sitting at basically the same price don't
        # both eat a TP slot
        merged = []
        for price, label in raw:
            if merged and abs(price - merged[-1][0]) / entry < 0.0015:
                continue
            merged.append((price, label))
        return merged

    # ---------------- ATR (volatility yardstick) ----------------

    def _compute_atr(self, candles: list, period: int = 14) -> Optional[float]:
        """
        Average True Range over the last `period` candles. Used so the
        stop-loss buffer scales with how noisy THIS market actually is right
        now, instead of a flat % that's too tight in a volatile stretch and
        too loose in a quiet one.
        """
        if len(candles) < period + 1:
            return None
        trs = []
        for i in range(len(candles) - period, len(candles)):
            high, low, prev_close = candles[i].high, candles[i].low, candles[i - 1].close
            trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        return sum(trs) / len(trs)

    # ---------------- structural stop-loss ----------------

    def _structural_stop(self, direction: str, ob: OrderBlock, liquidity: list,
                          atr: Optional[float], entry: float):
        """
        The stop sits beyond the REAL invalidation point, not an arbitrary
        distance away:
          1. start at the order block's far edge — the standard SMC
             invalidation level; if price closes back through it, the setup
             was wrong
          2. if a liquidity pool sits just beyond that edge, push the stop
             past it too — that pool is a likely sweep target, and a stop
             sitting between the OB and the pool gets taken out by the
             sweep itself, not a genuine reversal
          3. add an ATR-based buffer (not a flat %) so the stop clears
             normal wick noise for this market's actual current volatility
        Returns (stop_loss_price, reason_string).
        """
        buffer = (atr * 0.25) if atr else (entry * 0.0015)
        reason = "beyond the order block (structural invalidation)"

        if direction == "long":
            floor = ob.bottom
            for z in liquidity:
                if z["kind"] == "buy_side" and z["price"] <= ob.bottom and z["price"] < floor:
                    floor = z["price"]
                    reason = "beyond the order block AND a liquidity pool below it (likely sweep zone)"
            return floor - buffer, reason
        else:
            ceiling = ob.top
            for z in liquidity:
                if z["kind"] == "sell_side" and z["price"] >= ob.top and z["price"] > ceiling:
                    ceiling = z["price"]
                    reason = "beyond the order block AND a liquidity pool above it (likely sweep zone)"
            return ceiling + buffer, reason

    # ---------------- signal generation ----------------

    def generate_signal(self, candles: list) -> Optional[Signal]:
        min_needed = self.swing_lookback * 2 + 10
        if len(candles) < min_needed:
            return None

        swings = self.find_swings(candles)
        trend, events = self.detect_structure(candles, swings)
        order_blocks = self.find_order_blocks(candles, events)
        fvgs = self.find_fvgs(candles)
        liquidity = self.find_liquidity_zones(swings)

        last = candles[-1]
        reasons = []
        confidence = 0.0
        direction = None
        stop_loss = None
        entry = last.close

        bull_obs = [ob for ob in order_blocks if ob.kind == "bullish" and not ob.mitigated]
        bear_obs = [ob for ob in order_blocks if ob.kind == "bearish" and not ob.mitigated]

        atr = self._compute_atr(candles)
        sl_reason = ""

        if trend == Trend.BULLISH and bull_obs:
            ob = bull_obs[-1]
            if ob.bottom <= last.low <= ob.top:
                direction = "long"
                stop_loss, sl_reason = self._structural_stop("long", ob, liquidity, atr, entry)
                confidence += 0.4
                reasons.append("Price tapped a bullish order block during an uptrend")
                if any(f.kind == "bullish" and f.bottom <= last.low <= f.top for f in fvgs):
                    confidence += 0.2
                    reasons.append("Order block overlaps an unfilled bullish FVG")
                if any(z["kind"] == "buy_side" for z in liquidity):
                    confidence += 0.15
                    reasons.append("Equal-lows liquidity pool nearby (possible sweep)")

        elif trend == Trend.BEARISH and bear_obs:
            ob = bear_obs[-1]
            if ob.bottom <= last.high <= ob.top:
                direction = "short"
                stop_loss, sl_reason = self._structural_stop("short", ob, liquidity, atr, entry)
                confidence += 0.4
                reasons.append("Price tapped a bearish order block during a downtrend")
                if any(f.kind == "bearish" and f.bottom <= last.high <= f.top for f in fvgs):
                    confidence += 0.2
                    reasons.append("Order block overlaps an unfilled bearish FVG")
                if any(z["kind"] == "sell_side" for z in liquidity):
                    confidence += 0.15
                    reasons.append("Equal-highs liquidity pool nearby (possible sweep)")

        if direction is None:
            return None

        risk = abs(entry - stop_loss)
        if risk <= 0:
            return None

        # Real structure first — ignore anything closer than 0.5R, too tight
        # to be a meaningful TP slot.
        targets = self._collect_targets(direction, entry, order_blocks, fvgs,
                                         liquidity, swings, min_distance=risk * 0.5)

        take_profits, tp_reasons = [], []
        for price, label in targets[:3]:
            take_profits.append(price)
            tp_reasons.append(label)

        # Only fall back to a raw R-multiple for whatever TP slots structure
        # couldn't fill — and label it clearly so it's never mistaken for a
        # real level with an actual reason to be reached.
        for mult in (1, 2, 3):
            if len(take_profits) >= 3:
                break
            fallback_price = entry + risk * mult if direction == "long" else entry - risk * mult
            if any(abs(fallback_price - p) / entry < 0.0015 for p in take_profits):
                continue
            take_profits.append(fallback_price)
            tp_reasons.append(f"{mult}R fallback — no real structure found yet, "
                               f"treat as a trail checkpoint, not a promised target")

        ordered = sorted(zip(take_profits, tp_reasons), key=lambda t: abs(t[0] - entry))
        take_profits = [p for p, _ in ordered]
        tp_reasons = [r for _, r in ordered]

        has_real_tp_structure = any("fallback" not in r for r in tp_reasons)
        if has_real_tp_structure:
            confidence += 0.1
            reasons.append("At least one TP targets real structure, not just an R-multiple")

        return Signal(
            direction=direction,
            entry=entry,
            stop_loss=stop_loss,
            sl_reason=sl_reason,
            take_profits=take_profits,
            tp_reasons=tp_reasons,
            has_real_tp_structure=has_real_tp_structure,
            confidence=min(confidence, 1.0),
            reasons=reasons,
        )
