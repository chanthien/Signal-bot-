"""
strategy/andz_v80_strategy.py
ANDZ Momentum Trend Strategy V8.0
Optimized for higher trade frequency, pyramiding, and faster risk control.
Refactored to match Signal and AssetState bot interfaces.
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

from config.settings import AssetConfig
from strategy.grid_pyramid_v9_optimized import Signal, AssetState


class AndzV80Strategy:

    def __init__(self, config: AssetConfig):
        self.cfg = config
        self.state = AssetState()
        
        # Override local config mapping dynamically to original config params
        self.max_layers = 3
        self.risk_atr_mult = 1.0
        self.trail_atr_mult = 0.7
        self.breakout_period = 20

    def process(self, df: pd.DataFrame) -> Optional[Signal]:

        if len(df) < 100:
            return None

        df = self._indicators(df)
        bar = df.iloc[-1]

        close = float(bar.close)
        ema25 = float(bar.ema25)
        ema100 = float(bar.ema100)
        ema200 = float(bar.ema200)  # [MTF] M15 EMA200
        adx = float(bar.adx)
        rsi = float(bar.rsi)
        atr = float(bar.atr)

        # [MTF] H1 Trend definitions
        h1_long = close > ema200
        h1_short = close < ema200

        breakout_high = float(df.high.rolling(self.breakout_period).max().iloc[-2])
        breakout_low = float(df.low.rolling(self.breakout_period).min().iloc[-2])

        trend_long = ema25 > ema100
        trend_short = ema25 < ema100

        pullback_long = close > ema25 and rsi > 50 and adx > 18
        pullback_short = close < ema25 and rsi < 50 and adx > 18

        breakout_long = close > breakout_high
        breakout_short = close < breakout_low

        long_entry = trend_long and (pullback_long or breakout_long) and h1_long
        short_entry = trend_short and (pullback_short or breakout_short) and h1_short

        state = self.state

        # --- ENTRY ---
        if state.direction == "":
            if long_entry:
                sl = close - atr * self.risk_atr_mult
                tp1 = close + atr * 2
                tp2 = close + atr * 4

                return self._build_signal(
                    "LONG", "LONG", close, atr, 
                    sl_price=sl, tp1_price=tp1, tp2_price=tp2, layer=1
                )

            if short_entry:
                sl = close + atr * self.risk_atr_mult
                tp1 = close - atr * 2
                tp2 = close - atr * 4

                return self._build_signal(
                    "SHORT", "SHORT", close, atr, 
                    sl_price=sl, tp1_price=tp1, tp2_price=tp2, layer=1
                )

        # --- H1 TREND REVERSAL ---
        if state.direction == "LONG" and h1_short:
            return self._build_signal("FLAT", "SHORT", close, atr, sl_price=close, tp1_price=close, tp2_price=close, layer=0)
        if state.direction == "SHORT" and h1_long:
            return self._build_signal("FLAT", "LONG", close, atr, sl_price=close, tp1_price=close, tp2_price=close, layer=0)

        avg = state.avg_entry

        # --- PYRAMIDING (ADD) ---
        if state.layers < self.max_layers:

            if state.direction == "LONG" and close > avg + atr:
                sl = close - atr
                tp1 = close + atr * 2
                tp2 = close + atr * 4
                return self._build_signal(
                    "ADD", "LONG", close, atr, 
                    sl_price=sl, tp1_price=tp1, tp2_price=tp2, layer=state.layers + 1
                )

            if state.direction == "SHORT" and close < avg - atr:
                sl = close + atr
                tp1 = close - atr * 2
                tp2 = close - atr * 4
                return self._build_signal(
                    "ADD", "SHORT", close, atr, 
                    sl_price=sl, tp1_price=tp1, tp2_price=tp2, layer=state.layers + 1
                )

        # --- EXITS (FLAT) ---
        if state.direction == "LONG":

            if close > state.peak_price:
                state.peak_price = close

            if close - avg > atr:
                trail = state.peak_price - atr * self.trail_atr_mult
                if close < trail:
                    return self._build_signal(
                        "FLAT", "", close, atr, 
                        sl_price=close, tp1_price=close, tp2_price=close, layer=0
                    )

            if rsi < 45:
                return self._build_signal(
                    "FLAT", "", close, atr, 
                    sl_price=close, tp1_price=close, tp2_price=close, layer=0
                )

        if state.direction == "SHORT":

            if state.peak_price == 0 or close < state.peak_price:
                state.peak_price = close

            if avg - close > atr:
                trail = state.peak_price + atr * self.trail_atr_mult
                if close > trail:
                    return self._build_signal(
                        "FLAT", "", close, atr, 
                        sl_price=close, tp1_price=close, tp2_price=close, layer=0
                    )

            if rsi > 55:
                return self._build_signal(
                    "FLAT", "", close, atr, 
                    sl_price=close, tp1_price=close, tp2_price=close, layer=0
                )

        return None
        
    def _build_signal(self, action, direction, price, atr, sl_price, tp1_price, tp2_price, layer):
        # Calculate distance for rr
        sl_dist = abs(price - sl_price)
        tp1_dist = abs(tp1_price - price)
        rr = tp1_dist / sl_dist if sl_dist > 0 else 0.0

        return Signal(
            action=action,
            symbol=self.cfg.symbol,
            price=price,
            direction=direction,
            confidence=0.70 if action in ["LONG", "SHORT"] else 0.65,
            sl_price=sl_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            atr=atr,
            momentum_score=0.0,
            atr_ratio=1.0,
            rr_ratio=rr,
            layer=layer,
            avg_entry=self.state.avg_entry,
            exhaustion=False
        )

    def _indicators(self, df):

        df = df.copy()

        df["ema25"] = df.close.ewm(span=25).mean()
        df["ema100"] = df.close.ewm(span=100).mean()
        df["ema200"] = df.close.ewm(span=200).mean() # MTF

        df["atr"] = self._atr(df, 14)
        df["rsi"] = self._rsi(df, 14)
        df["adx"] = self._adx(df, 14)

        return df

    def _atr(self, df, period):

        h = df.high
        l = df.low
        c = df.close.shift()

        tr = pd.concat([h-l, (h-c).abs(), (l-c).abs()], axis=1).max(axis=1)

        return tr.ewm(span=period).mean()

    def _rsi(self, df, period):

        delta = df.close.diff()

        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)

        gain = gain.rolling(period).mean()
        loss = loss.rolling(period).mean()

        rs = gain / loss

        return 100 - (100/(1+rs))

    def _adx(self, df, period):

        high = df.high
        low = df.low
        close = df.close

        plus_dm = high.diff()
        minus_dm = -low.diff()

        plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
        minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)

        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()

        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        tr_smooth = tr.ewm(span=period).mean()
        plus_dm_smooth = plus_dm.ewm(span=period).mean()
        minus_dm_smooth = minus_dm.ewm(span=period).mean()

        plus_di = 100 * (plus_dm_smooth / tr_smooth)
        minus_di = 100 * (minus_dm_smooth / tr_smooth)

        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)

        adx = dx.ewm(span=period).mean()

        return adx
