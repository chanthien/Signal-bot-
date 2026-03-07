
"""
strategy/grid_pyramid_v9.py
Optimized lightweight version of the original Grid Pyramid strategy.

Design goals:
- Keep logic SIMPLE
- Preserve breakout → trend → pyramid philosophy
- Increase trade frequency slightly
- ATR-adaptive grid
- Breakout quality filter
- Micro-pullback adds inside trend
"""

import math
from dataclasses import dataclass, field
from typing import Optional
import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────
# Signal schema
# ─────────────────────────────────────────────────────────────

@dataclass
class Signal:
    action: str
    symbol: str
    price: float
    direction: str
    confidence: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    atr: float
    momentum_score: float
    atr_ratio: float
    rr_ratio: float
    layer: int = 1
    avg_entry: float = 0.0
    exhaustion: bool = False


# ─────────────────────────────────────────────────────────────
# Runtime state
# ─────────────────────────────────────────────────────────────

@dataclass
class AssetState:
    direction: str = ""
    layers: int = 0
    entry_prices: list = field(default_factory=list)
    sizes: list = field(default_factory=list)
    last_signal: str = ""

    @property
    def avg_entry(self):
        if not self.entry_prices:
            return 0
        cost = sum(p * s for p, s in zip(self.entry_prices, self.sizes))
        size = sum(self.sizes)
        return cost / size if size > 0 else 0

    @property
    def last_entry(self):
        return self.entry_prices[-1] if self.entry_prices else 0


# ─────────────────────────────────────────────────────────────
# Strategy
# ─────────────────────────────────────────────────────────────

class GridPyramidStrategy:

    def __init__(self, cfg):
        self.cfg = cfg
        self.state = AssetState()

        # fallback parameters if config missing
        self.grid_atr_mult = getattr(cfg, "grid_atr_mult", 0.8)
        self.breakout_atr = getattr(cfg, "breakout_atr", 0.35)
        self.max_layers = getattr(cfg, "max_layers", 4)

    # ─────────────────────────────────────────────────────────

    def process(self, df: pd.DataFrame) -> Optional[Signal]:

        if len(df) < 50:
            return None

        df = self._indicators(df)

        bar = df.iloc[-1]
        close = float(bar.close)
        high = float(bar.high)
        low = float(bar.low)
        ema20 = float(bar.ema20)
        atr = float(bar.atr)

        atr_ma = float(df.atr.rolling(20).mean().iloc[-1])
        atr_ratio = atr / atr_ma if atr_ma > 0 else 1

        momentum = (close - ema20) / atr if atr > 0 else 0

        res = self._get_resistance(df)
        sup = self._get_support(df)

        state = self.state

        # ───────────────── breakout strength
        breakout_long = False
        breakout_short = False

        if res:
            strength = (close - res) / atr if atr > 0 else 0
            breakout_long = strength > self.breakout_atr

        if sup:
            strength = (sup - close) / atr if atr > 0 else 0
            breakout_short = strength > self.breakout_atr

        # ───────────────── grid distance (ATR adaptive)
        grid = atr * self.grid_atr_mult

        # ======================================================
        # ENTRY
        # ======================================================

        if state.direction == "":

            if breakout_long:
                return self._signal("LONG", "LONG", close, atr, atr_ratio, momentum, 1)

            if breakout_short:
                return self._signal("SHORT", "SHORT", close, atr, atr_ratio, momentum, 1)

            return None

        # ======================================================
        # REVERSAL
        # ======================================================

        if state.direction == "LONG" and breakout_short:
            return self._signal("FLAT", "SHORT", close, atr, atr_ratio, momentum, 1)

        if state.direction == "SHORT" and breakout_long:
            return self._signal("FLAT", "LONG", close, atr, atr_ratio, momentum, 1)

        # ======================================================
        # PYRAMID ADD
        # ======================================================

        if state.direction == "LONG" and state.layers < self.max_layers:

            if close >= state.last_entry + grid:
                return self._signal("ADD", "LONG", close, atr, atr_ratio, momentum, state.layers + 1)

            # micro pullback add (increase trade frequency)
            pullback = (ema20 - low) / atr if atr > 0 else 0
            if 0.2 < pullback < 0.6 and close > ema20:
                return self._signal("ADD", "LONG", close, atr, atr_ratio, momentum, state.layers + 1)

        if state.direction == "SHORT" and state.layers < self.max_layers:

            if close <= state.last_entry - grid:
                return self._signal("ADD", "SHORT", close, atr, atr_ratio, momentum, state.layers + 1)

            pullback = (high - ema20) / atr if atr > 0 else 0
            if 0.2 < pullback < 0.6 and close < ema20:
                return self._signal("ADD", "SHORT", close, atr, atr_ratio, momentum, state.layers + 1)

        # ======================================================
        # REDUCE (layer recycle)
        # ======================================================

        if state.layers > 1:

            if state.direction == "LONG" and close <= state.last_entry - grid:
                return self._signal("REDUCE", "LONG", close, atr, atr_ratio, momentum, state.layers)

            if state.direction == "SHORT" and close >= state.last_entry + grid:
                return self._signal("REDUCE", "SHORT", close, atr, atr_ratio, momentum, state.layers)

        # ======================================================
        # TREND WEAKENING EXIT
        # ======================================================

        distance = abs(close - ema20) / atr if atr > 0 else 0

        if distance < 0.2 and state.layers > 1:
            return self._signal("REDUCE", state.direction, close, atr, atr_ratio, momentum, state.layers)

        return None

    # ─────────────────────────────────────────────────────────

    def _signal(self, action, direction, price, atr, atr_ratio, momentum, layer):

        sl_dist = atr * 1.5
        tp1 = atr * 1.8
        tp2 = atr * 3.0

        if direction == "LONG":
            sl = price - sl_dist
            tp1_price = price + tp1
            tp2_price = price + tp2
        elif direction == "SHORT":
            sl = price + sl_dist
            tp1_price = price - tp1
            tp2_price = price - tp2
        else:
            sl = tp1_price = tp2_price = price

        rr = tp1 / sl_dist if sl_dist > 0 else 0

        confidence = self._confidence(atr_ratio, momentum, rr, layer)

        return Signal(
            action=action,
            symbol=self.cfg.symbol,
            price=price,
            direction=direction,
            confidence=confidence,
            sl_price=sl,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            atr=atr,
            momentum_score=momentum,
            atr_ratio=atr_ratio,
            rr_ratio=rr,
            layer=layer,
            avg_entry=self.state.avg_entry,
        )

    # ─────────────────────────────────────────────────────────

    def _confidence(self, atr_ratio, momentum, rr, layer):

        p = 0.55

        if atr_ratio > 1.2:
            p *= 1.1

        if abs(momentum) > 1:
            p *= 1.05

        if rr > 2:
            p *= 1.08

        if layer >= 3:
            p *= 1.05

        return round(max(0.5, min(p, 0.9)), 3)

    # ───────────────── indicators

    def _indicators(self, df):

        df = df.copy()

        df["atr"] = self._atr(df, 14)
        df["ema20"] = df.close.ewm(span=20, adjust=False).mean()

        return df

    def _atr(self, df, n):

        h = df.high
        l = df.low
        c = df.close.shift()

        tr = pd.concat([h - l, (h - c).abs(), (l - c).abs()], axis=1).max(axis=1)
        return tr.ewm(span=n, adjust=False).mean()

    # ───────────────── pivots

    def _get_resistance(self, df, n=5):

        highs = df.high.values

        for i in range(len(highs) - n - 1, n, -1):
            if all(highs[i] >= highs[i - j] for j in range(1, n + 1)):
                return float(highs[i])

        return None

    def _get_support(self, df, n=5):

        lows = df.low.values

        for i in range(len(lows) - n - 1, n, -1):
            if all(lows[i] <= lows[i - j] for j in range(1, n + 1)):
                return float(lows[i])

        return None
