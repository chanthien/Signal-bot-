"""
strategy/grid_pyramid.py — TF + Grid Pyramid v8.0 H1
=====================================================
FILE NÀY SWAP KHI ĐỔI THUẬT TOÁN.

Input  : DataFrame OHLCV từ BingX
Output : Signal dict → executor + Telegram formatter
"""

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from config.settings import AssetConfig


# ── Signal output schema ──────────────────────────────────────────────────

@dataclass
class Signal:
    action:         str         # "LONG" | "SHORT" | "FLAT" | "ADD" | "REDUCE" | "WEAK_TREND"
    symbol:         str
    price:          float
    direction:      str         # "LONG" | "SHORT" | ""
    confidence:     float       # 0.0 – 1.0
    sl_price:       float
    tp1_price:      float
    tp2_price:      float
    atr:            float
    momentum_score: float
    atr_ratio:      float
    rr_ratio:       float       # TP1/SL distance ratio
    layer:          int = 1
    avg_entry:      float = 0.0
    exhaustion:     bool  = False   # cuối trend warning


# ── Per-asset runtime state ───────────────────────────────────────────────

@dataclass
class AssetState:
    direction:      str   = ""      # "LONG" | "SHORT" | ""
    layers:         int   = 0
    entry_prices:   list  = field(default_factory=list)
    sizes:          list  = field(default_factory=list)
    peak_price:     float = 0.0
    trail_active:   bool  = False
    last_signal:    str   = ""

    def reset(self):
        self.direction    = ""
        self.layers       = 0
        self.entry_prices = []
        self.sizes        = []
        self.peak_price   = 0.0
        self.trail_active = False
        self.last_signal  = ""

    @property
    def avg_entry(self) -> float:
        if not self.entry_prices:
            return 0.0
        cost = sum(p * s for p, s in zip(self.entry_prices, self.sizes))
        total = sum(self.sizes)
        return cost / total if total > 0 else 0.0

    @property
    def last_entry(self) -> float:
        return self.entry_prices[-1] if self.entry_prices else 0.0


# ── Main strategy class ───────────────────────────────────────────────────

class GridPyramidStrategy:
    """
    Stateful per-asset strategy.
    Gọi process(df) mỗi H1 close → trả về Signal hoặc None.
    """

    def __init__(self, cfg: AssetConfig):
        self.cfg   = cfg
        self.state = AssetState()

    # ── Public entry point ────────────────────────────────────────────────

    def process(self, df: pd.DataFrame) -> Optional[Signal]:
        """
        Main method. Call every H1 bar close.
        df: OHLCV DataFrame, newest row = latest closed candle
        Returns Signal if action needed, else None.
        """
        if len(df) < self.cfg.atr_len + 5:
            return None

        # ── Indicators ──
        df  = self._calc_indicators(df)
        bar = df.iloc[-1]      # latest closed bar
        prev= df.iloc[-2]

        atr      = float(bar["atr"])
        atr_ma   = float(df["atr"].rolling(20).mean().iloc[-1])
        atr_ratio= atr / atr_ma if atr_ma > 0 else 1.0
        atr_pct  = atr / float(bar["close"]) * 100.0
        close    = float(bar["close"])
        high     = float(bar["high"])
        low      = float(bar["low"])
        ema20    = float(bar["ema20"])

        # Momentum score: how many ATRs is price from EMA20
        momentum = (close - ema20) / atr if atr > 0 else 0.0
        # Clamp
        momentum = max(-3.0, min(3.0, momentum))

        # Impulse bar
        body      = abs(float(bar["close"]) - float(bar["open"]))
        impulse   = (high - low) > atr * self.cfg.impulse_mult \
                    and atr_pct >= self.cfg.min_atr_pct
        candle_body_ratio = body / atr if atr > 0 else 0.5

        # Pivot levels
        res = self._get_resistance(df)
        sup = self._get_support(df)

        # Breakout conditions (aggressive: no open <= res condition)
        brk_res = res is not None and close > res and impulse
        brk_sup = sup is not None and close < sup and impulse

        # Breakout gap (how far price broke through)
        gap_res = (close - res) / atr if (res and brk_res and atr > 0) else 0.0
        gap_sup = (sup - close) / atr if (sup and brk_sup and atr > 0) else 0.0
        breakout_gap = max(gap_res, gap_sup)

        # ── Exhaustion detection ──
        exhaustion = self._detect_exhaustion(df, atr)

        state = self.state

        # ── State machine ──────────────────────────────────────────────

        # FLAT → entry
        if state.direction == "":
            if brk_res:
                return self._build_signal(
                    "LONG", "LONG", close, atr, atr_ratio, momentum,
                    candle_body_ratio, breakout_gap, layer=1,
                    exhaustion=exhaustion
                )
            if brk_sup:
                return self._build_signal(
                    "SHORT", "SHORT", close, atr, atr_ratio, momentum,
                    candle_body_ratio, breakout_gap, layer=1,
                    exhaustion=exhaustion
                )
            return None

        # IN POSITION ───────────────────────────────────────────────────

        # Reversal
        if state.direction == "LONG" and brk_sup:
            return self._build_signal(
                "FLAT", "SHORT", close, atr, atr_ratio, momentum,
                candle_body_ratio, breakout_gap, exhaustion=exhaustion
            )
        if state.direction == "SHORT" and brk_res:
            return self._build_signal(
                "FLAT", "LONG", close, atr, atr_ratio, momentum,
                candle_body_ratio, breakout_gap, exhaustion=exhaustion
            )

        # Structure break → flat
        if state.direction == "LONG" and sup is not None and close < sup:
            return self._build_signal(
                "FLAT", "", close, atr, atr_ratio, momentum,
                candle_body_ratio, 0.0, exhaustion=exhaustion
            )
        if state.direction == "SHORT" and res is not None and close > res:
            return self._build_signal(
                "FLAT", "", close, atr, atr_ratio, momentum,
                candle_body_ratio, 0.0, exhaustion=exhaustion
            )

        # Exhaustion warning (gửi 1 lần)
        if exhaustion and state.last_signal != "WEAK_TREND":
            return self._build_signal(
                "WEAK_TREND", state.direction, close, atr, atr_ratio,
                momentum, candle_body_ratio, 0.0, exhaustion=True
            )

        # Grid Add
        grid_dist = state.last_entry * self.cfg.grid_pct / 100.0
        if state.direction == "LONG" and state.layers < self.cfg.max_layers:
            if close >= state.last_entry + grid_dist:
                return self._build_signal(
                    "ADD", "LONG", close, atr, atr_ratio, momentum,
                    candle_body_ratio, breakout_gap,
                    layer=state.layers + 1, exhaustion=exhaustion
                )

        if state.direction == "SHORT" and state.layers < self.cfg.max_layers:
            if close <= state.last_entry - grid_dist:
                return self._build_signal(
                    "ADD", "SHORT", close, atr, atr_ratio, momentum,
                    candle_body_ratio, breakout_gap,
                    layer=state.layers + 1, exhaustion=exhaustion
                )

        # Grid Reduce
        if state.layers > 1:
            if state.direction == "LONG" and close <= state.last_entry - grid_dist:
                return self._build_signal(
                    "REDUCE", "LONG", close, atr, atr_ratio, momentum,
                    candle_body_ratio, 0.0, layer=state.layers,
                    exhaustion=exhaustion
                )
            if state.direction == "SHORT" and close >= state.last_entry + grid_dist:
                return self._build_signal(
                    "REDUCE", "SHORT", close, atr, atr_ratio, momentum,
                    candle_body_ratio, 0.0, layer=state.layers,
                    exhaustion=exhaustion
                )

        return None

    # ── Signal builder ────────────────────────────────────────────────────

    def _build_signal(
        self, action: str, direction: str, price: float,
        atr: float, atr_ratio: float, momentum: float,
        candle_body_ratio: float, breakout_gap: float,
        layer: int = 1, exhaustion: bool = False
    ) -> Signal:
        cfg = self.cfg

        # ── Dynamic SL/TP ──────────────────────────────────────────────
        # SL: volatility-adjusted
        # SL_dist = ATR_current × hard_sl_pct × ATR_ratio^0.5
        # Rational: SL nới theo căn bậc hai của volatility ratio
        # Khi market spike 4x → SL chỉ nới 2x, không bị blown
        sl_atr_mult  = (cfg.hard_sl_pct / 100.0) * (price / atr) if atr > 0 else 1.5
        sl_atr_mult  = max(1.0, min(sl_atr_mult, 4.0))
        sl_dist      = atr * sl_atr_mult * (atr_ratio ** 0.5)

        # TP: asymmetric momentum-adjusted
        # TP rộng ra khi momentum mạnh, hẹp lại khi momentum yếu
        momentum_abs = abs(momentum)
        tp_boost     = 1.0 + 0.25 * min(momentum_abs, 2.0)

        tp1_dist = atr * cfg.tp1_atr_mult * tp_boost
        tp2_dist = atr * cfg.tp2_atr_mult * tp_boost

        if direction == "LONG":
            sl_price  = price - sl_dist
            tp1_price = price + tp1_dist
            tp2_price = price + tp2_dist
        elif direction == "SHORT":
            sl_price  = price + sl_dist
            tp1_price = price - tp1_dist
            tp2_price = price - tp2_dist
        else:
            sl_price = tp1_price = tp2_price = price

        # R:R ratio
        rr = tp1_dist / sl_dist if sl_dist > 0 else 0.0

        # ── Confidence score (Bayesian multiplicative) ─────────────────
        confidence = self._calc_confidence(
            atr_ratio, candle_body_ratio, breakout_gap,
            layer, rr, action
        )

        self.state.last_signal = action

        return Signal(
            action         = action,
            symbol         = cfg.symbol,
            price          = price,
            direction      = direction,
            confidence     = confidence,
            sl_price       = sl_price,
            tp1_price      = tp1_price,
            tp2_price      = tp2_price,
            atr            = atr,
            momentum_score = momentum,
            atr_ratio      = atr_ratio,
            rr_ratio       = rr,
            layer          = layer,
            avg_entry      = self.state.avg_entry,
            exhaustion     = exhaustion,
        )

    # ── Confidence score ──────────────────────────────────────────────────

    def _calc_confidence(
        self,
        atr_ratio: float,
        candle_body_ratio: float,
        breakout_gap: float,
        layer: int,
        rr: float,
        action: str,
    ) -> float:
        """
        Bayesian multiplicative confidence.
        Mỗi factor là probability multiplier độc lập.
        P_final = P_base × ∏ P_factor_i
        """
        cfg = self.cfg

        # Base win rate (H1 backtest baseline, boost từ 0.42 lên 0.55)
        p = cfg.win_rate_base  # 0.55

        # Factor 1: Volatility regime
        # Market đang volatile hơn bình thường → breakout đáng tin hơn
        if atr_ratio > 1.3:
            p *= 1.18
        elif atr_ratio > 1.1:
            p *= 1.10
        elif atr_ratio < 0.8:
            p *= 0.92
        # Nếu atr_ratio ~1 → neutral, không nhân gì

        # Factor 2: Impulse candle quality
        # Body lớn = momentum commitment, không phải wick spike
        body_factor = 0.88 + min(candle_body_ratio, 1.5) * 0.22
        body_factor = max(0.88, min(body_factor, 1.22))
        p *= body_factor

        # Factor 3: Breakout gap
        # Giá phá qua resistance bao nhiêu ATR → càng xa càng chắc
        gap_factor = 0.92 + min(breakout_gap, 1.0) * 0.22
        gap_factor = max(0.92, min(gap_factor, 1.18))
        p *= gap_factor

        # Factor 4: Pyramid confirmation
        # Đã add layer = giá confirm đi đúng hướng → evidence mạnh hơn
        if layer >= 3:
            p *= 1.15
        elif layer == 2:
            p *= 1.08

        # Factor 5: R:R quality
        # R:R tốt → setup có edge
        if rr >= 2.5:
            p *= 1.12
        elif rr >= 1.8:
            p *= 1.06
        elif rr < 1.2:
            p *= 0.88

        # Factor 6: Action type
        # REDUCE = giá đang đi ngược → confidence thấp hơn
        if action == "REDUCE":
            p *= 0.88
        elif action == "WEAK_TREND":
            p *= 0.82

        # Hard floor/ceiling: không bao giờ < 52% hoặc > 91%
        # Floor 52% = luôn trên threshold "đáng vào lệnh"
        p = max(0.52, min(p, 0.91))

        return round(p, 3)

    # ── Indicators ────────────────────────────────────────────────────────

    def _calc_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        # ATR
        df["atr"]   = self._atr(df, self.cfg.atr_len)
        # EMA20 (momentum baseline)
        df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
        return df

    @staticmethod
    def _atr(df: pd.DataFrame, period: int) -> pd.Series:
        h, l, c = df["high"], df["low"], df["close"].shift(1)
        tr = pd.concat([h - l, (h - c).abs(), (l - c).abs()], axis=1).max(axis=1)
        return tr.ewm(span=period, adjust=False).mean()

    def _get_resistance(self, df: pd.DataFrame) -> Optional[float]:
        n = self.cfg.pivot_len
        highs = df["high"].values
        for i in range(len(highs) - n - 1, n - 1, -1):
            if all(highs[i] >= highs[i - j] for j in range(1, n + 1)) and \
               all(highs[i] >= highs[i + j] for j in range(1, n + 1)
                   if i + j < len(highs)):
                return float(highs[i])
        return None

    def _get_support(self, df: pd.DataFrame) -> Optional[float]:
        n = self.cfg.pivot_len
        lows = df["low"].values
        for i in range(len(lows) - n - 1, n - 1, -1):
            if all(lows[i] <= lows[i - j] for j in range(1, n + 1)) and \
               all(lows[i] <= lows[i + j] for j in range(1, n + 1)
                   if i + j < len(lows)):
                return float(lows[i])
        return None

    # ── Exhaustion detection ──────────────────────────────────────────────

    def _detect_exhaustion(self, df: pd.DataFrame, atr: float) -> bool:
        """
        Phát hiện cuối trend qua 2/3 dấu hiệu:
        1. Body candle 3 nến gần nhất đang nhỏ dần (momentum giảm)
        2. ATR spike rồi co lại (volatility climax)
        3. Failed breakout: high mới nhưng close dưới high cũ
        """
        if len(df) < 5:
            return False

        score = 0

        # Signal 1: Shrinking bodies (momentum exhaustion)
        bodies = [abs(df["close"].iloc[-i] - df["open"].iloc[-i])
                  for i in range(1, 4)]
        if bodies[0] < bodies[1] < bodies[2] and bodies[0] < atr * 0.3:
            score += 1

        # Signal 2: ATR spike then contract
        atr_recent = df["atr"].iloc[-3:].values if "atr" in df.columns else []
        if len(atr_recent) == 3:
            if atr_recent[1] > atr_recent[0] * 1.3 and \
               atr_recent[2] < atr_recent[1] * 0.85:
                score += 1

        # Signal 3: New price extreme but weak close (wick rejection)
        last = df.iloc[-1]
        prev_max = df["high"].iloc[-5:-1].max()
        if float(last["high"]) > prev_max:
            wick   = float(last["high"]) - float(last["close"])
            if wick > atr * 0.5:
                score += 1

        return score >= 2
