"""
strategy/grid_pyramid_v9_optimized.py

v9 spirit giữ nguyên:
    Breakout → trend → pyramid → exit by reversal breakout

Chỉ fix những gì broken + thêm tối thiểu để live được:

    [FIX 1] apply_signal()        — state không bao giờ update trong v9 gốc
    [FIX 2] Pivot cả 2 chiều      — v9 gốc chỉ check left side → false pivot
    [FIX 3] Micro-pullback        — dùng recent low window thay vì bar.low

    [ADD 1] Hard SL động          — |MA25 - MA10| × (e/2) = × 1.359
                                    SL tự co giãn theo sức mạnh trend
                                    rộng khi trend mạnh, tight khi sideway
    [ADD 2] Position sizing       — risk_pct % equity / sl_distance
    [ADD 3] DD guard              — daily 5% + total 10% → force FLAT

Không thêm: ADX, volume filter, EMA50, trailing SL, counter scalp
"""

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

# e/2 constant
_E_HALF = math.e / 2   # ≈ 1.359


# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────

@dataclass
class PyramidConfig:
    symbol: str          = "BTC/USDT"
    grid_atr_mult: float = 0.8
    breakout_atr: float  = 0.35
    max_layers: int      = 4

    # TP reference (dùng để tính RR trong signal, không auto-close)
    tp1_atr_mult: float  = 1.8
    tp2_atr_mult: float  = 3.0

    # Risk
    risk_pct: float      = 0.01      # 1% equity per layer
    account_equity: float = 1000.0

    # Hard SL: |MA25 - MA10| × e/2
    # Cap để tránh SL quá rộng trên altcoin volatile
    sl_atr_cap: float    = 3.0       # tối đa 3× ATR

    # DD guard
    max_daily_dd_pct: float  = 0.05  # 5%
    max_total_dd_pct: float  = 0.10  # 10%

    # Micro-pullback lookback
    pullback_lookback: int = 3


# ─────────────────────────────────────────────────────────────
# Signal
# ─────────────────────────────────────────────────────────────

@dataclass
class Signal:
    action: str           # LONG | SHORT | ADD | REDUCE | FLAT
    symbol: str
    price: float
    direction: str
    confidence: float
    sl_price: float       # hard SL level tại thời điểm signal
    tp1_price: float
    tp2_price: float
    atr: float
    momentum_score: float
    atr_ratio: float
    rr_ratio: float
    layer: int   = 1
    avg_entry: float = 0.0
    size: float  = 0.0    # qty tính từ risk_pct
    reason: str  = ""
    exhaustion: bool = False


# ─────────────────────────────────────────────────────────────
# State
# ─────────────────────────────────────────────────────────────

@dataclass
class AssetState:
    direction: str  = ""
    layers: int     = 0
    entry_prices: list = field(default_factory=list)
    sizes: list        = field(default_factory=list)
    last_signal: str   = ""

    # Trailing SL tracking
    peak_price: float  = 0.0
    trail_active: bool = False

    # DD tracking
    daily_date: date       = field(default_factory=date.today)
    daily_start_equity: float = 0.0
    daily_realized_pnl: float = 0.0
    peak_equity: float     = 0.0

    @property
    def avg_entry(self) -> float:
        if not self.entry_prices:
            return 0.0
        cost  = sum(p * s for p, s in zip(self.entry_prices, self.sizes))
        total = sum(self.sizes)
        return cost / total if total > 0 else 0.0

    @property
    def last_entry(self) -> float:
        return self.entry_prices[-1] if self.entry_prices else 0.0

    @property
    def total_size(self) -> float:
        return sum(self.sizes)

    def reset(self):
        self.direction = ""
        self.layers    = 0
        self.entry_prices.clear()
        self.sizes.clear()
        self.peak_price   = 0.0
        self.trail_active = False

    def new_day_check(self, equity: float):
        today = date.today()
        if today != self.daily_date:
            self.daily_date        = today
            self.daily_start_equity = equity
            self.daily_realized_pnl = 0.0


# ─────────────────────────────────────────────────────────────
# Strategy
# ─────────────────────────────────────────────────────────────

from config.settings import AssetConfig

class GridPyramidStrategy:

    def __init__(self, cfg: AssetConfig):
        self.cfg   = cfg
        self.state = AssetState()
        self.state.peak_equity       = getattr(cfg, "account_equity", 1000.0)
        self.state.daily_start_equity = getattr(cfg, "account_equity", 1000.0)
        
        # Override dynamic constants locally
        self.cfg.sl_atr_cap = 3.0
        self.cfg.max_daily_dd_pct = 0.05
        self.cfg.max_total_dd_pct = 0.10
        self.cfg.grid_atr_mult = 0.8
        self.cfg.breakout_atr = 0.35
        self.cfg.pullback_lookback = 3

    # ─────────────────────────────────────────────────────────
    # Main loop — gọi mỗi bar mới
    # ─────────────────────────────────────────────────────────

    def process(self, df: pd.DataFrame) -> Optional[Signal]:
        cfg   = self.cfg
        state = self.state

        if len(df) < 50:
            return None

        df = self._indicators(df)

        bar    = df.iloc[-1]
        close  = float(bar.close)
        high   = float(bar.high)
        low    = float(bar.low)
        ema20  = float(bar.ema20)
        ma10   = float(bar.ma10)
        ma25   = float(bar.ma25)
        ema200 = float(bar.ema200)  # [ADD MTF] H1 EMA50 equivalent = M15 EMA200
        atr    = float(bar.atr)

        # [ADD MTF] H1 Trend definitions
        h1_long = close > ema200
        h1_short = close < ema200

        atr_ma    = float(df.atr.rolling(20).mean().iloc[-1])
        atr_ratio = atr / atr_ma if atr_ma > 0 else 1.0
        momentum  = (close - ema20) / atr if atr > 0 else 0.0

        # Hard SL distance = |MA25 - MA10| × e/2, cap tại sl_atr_cap × ATR
        ma_spread  = abs(ma25 - ma10)
        sl_dist    = ma_spread * _E_HALF
        sl_dist    = min(sl_dist, atr * cfg.sl_atr_cap)   # cap cho altcoin
        sl_dist    = max(sl_dist, atr * 0.8)              # floor tối thiểu

        # ── [ADD 3] DD Guard — luôn check trước
        guard = self._dd_guard(close)
        if guard:
            return guard

        # ── [FIX 2] Hard SL check — dùng avg_entry + sl_dist động
        if state.direction == "LONG" and state.layers > 0:
            hard_sl = state.avg_entry - sl_dist
            if close <= hard_sl:
                return self._make_signal(
                    "FLAT", "LONG", close, atr, atr_ratio, momentum,
                    state.layers, sl_dist, "hard_sl"
                )

        if state.direction == "SHORT" and state.layers > 0:
            hard_sl = state.avg_entry + sl_dist
            if close >= hard_sl:
                return self._make_signal(
                    "FLAT", "SHORT", close, atr, atr_ratio, momentum,
                    state.layers, sl_dist, "hard_sl"
                )

        # ── Breakout detection
        res = self._get_resistance(df)
        sup = self._get_support(df)

        breakout_long  = bool(res and atr > 0 and (close - res) / atr > cfg.breakout_atr)
        breakout_short = bool(sup and atr > 0 and (sup - close) / atr > cfg.breakout_atr)

        grid = atr * cfg.grid_atr_mult

        # ======================================================
        # ENTRY — lọc bằng H1 trend
        # ======================================================

        if state.direction == "":

            if breakout_long and h1_long:
                return self._make_signal("LONG", "LONG", close, atr, atr_ratio, momentum, 1, sl_dist, "breakout")

            if breakout_short and h1_short:
                return self._make_signal("SHORT", "SHORT", close, atr, atr_ratio, momentum, 1, sl_dist, "breakout")

            return None

        # ======================================================
        # H1 TREND REVERSAL — Đóng tất cả nếu H1 đảo chiều
        # ======================================================
        if state.direction == "LONG" and h1_short:
            return self._make_signal("FLAT", "SHORT", close, atr, atr_ratio, momentum, 1, sl_dist, "h1_reversal")

        if state.direction == "SHORT" and h1_long:
            return self._make_signal("FLAT", "LONG", close, atr, atr_ratio, momentum, 1, sl_dist, "h1_reversal")

        # ======================================================
        # REVERSAL M15 — Chỉ đóng lệnh ADD nếu H1 vẫn cùng trend
        # ======================================================

        if state.direction == "LONG" and breakout_short:
            if state.layers > 1:
                return self._make_signal("REDUCE", "LONG", close, atr, atr_ratio, momentum, state.layers, sl_dist, "mtf_reduce")
            # Nếu chỉ còn lệnh Core, giữ nguyên cho đến khi H1 đảo chiều (h1_short)

        if state.direction == "SHORT" and breakout_long:
            if state.layers > 1:
                return self._make_signal("REDUCE", "SHORT", close, atr, atr_ratio, momentum, state.layers, sl_dist, "mtf_reduce")
            # Nếu chỉ còn lệnh Core, giữ nguyên cho đến khi H1 đảo chiều (h1_long)

        # ======================================================
        # PYRAMID ADD — [FIX 3] micro-pullback dùng recent window
        # ======================================================

        if state.direction == "LONG" and state.layers < cfg.max_layers:

            if close >= state.last_entry + grid:
                return self._make_signal("ADD", "LONG", close, atr, atr_ratio, momentum, state.layers + 1, sl_dist, "grid_add")

            # [FIX 3] dùng min của N bars gần nhất thay vì bar.low
            recent_low = float(df.low.iloc[-cfg.pullback_lookback:].min())
            pullback   = (ema20 - recent_low) / atr if atr > 0 else 0
            if 0.2 < pullback < 0.6 and close > ema20:
                return self._make_signal("ADD", "LONG", close, atr, atr_ratio, momentum, state.layers + 1, sl_dist, "pb_add")

        if state.direction == "SHORT" and state.layers < cfg.max_layers:

            if close <= state.last_entry - grid:
                return self._make_signal("ADD", "SHORT", close, atr, atr_ratio, momentum, state.layers + 1, sl_dist, "grid_add")

            recent_high = float(df.high.iloc[-cfg.pullback_lookback:].max())
            pullback    = (recent_high - ema20) / atr if atr > 0 else 0
            if 0.2 < pullback < 0.6 and close < ema20:
                return self._make_signal("ADD", "SHORT", close, atr, atr_ratio, momentum, state.layers + 1, sl_dist, "pb_add")

        # ======================================================
        # REDUCE — giữ nguyên v9
        # ======================================================

        if state.layers > 1:

            if state.direction == "LONG" and close <= state.last_entry - grid:
                return self._make_signal("REDUCE", "LONG", close, atr, atr_ratio, momentum, state.layers, sl_dist, "grid_reduce")

            if state.direction == "SHORT" and close >= state.last_entry + grid:
                return self._make_signal("REDUCE", "SHORT", close, atr, atr_ratio, momentum, state.layers, sl_dist, "grid_reduce")

        # ======================================================
        # TREND WEAKENING — giữ nguyên v9
        # ======================================================

        distance = abs(close - ema20) / atr if atr > 0 else 0

        if distance < 0.2 and state.layers > 1:
            return self._make_signal("REDUCE", state.direction, close, atr, atr_ratio, momentum, state.layers, sl_dist, "trend_weak")

        return None

    # ─────────────────────────────────────────────────────────
    # [FIX 1] apply_signal — MUST gọi sau mỗi fill
    # Đây là bug cốt lõi của v9 gốc: state không bao giờ update
    # ─────────────────────────────────────────────────────────

    def apply_signal(self, sig: Signal, filled_price: float, filled_size: float):
        """
        Cập nhật state sau khi exchange confirm fill.
        filled_price: giá fill thực tế
        filled_size:  qty thực tế
        """
        state = self.state
        cfg   = self.cfg

        if sig.action in ("LONG", "SHORT"):
            state.reset()
            state.direction = sig.direction
            state.layers    = 1
            state.entry_prices.append(filled_price)
            state.sizes.append(filled_size)

        elif sig.action == "ADD":
            state.layers += 1
            state.entry_prices.append(filled_price)
            state.sizes.append(filled_size)

        elif sig.action == "REDUCE":
            if state.layers > 1:
                state.layers -= 1
                if state.entry_prices: state.entry_prices.pop()
                if state.sizes:        state.sizes.pop()

        elif sig.action == "FLAT":
            pnl = self._calc_pnl(filled_price)
            state.daily_realized_pnl += pnl
            new_equity         = cfg.account_equity + pnl
            cfg.account_equity = new_equity
            state.peak_equity  = max(state.peak_equity, new_equity)
            state.reset()

        state.last_signal = sig.action

    # ─────────────────────────────────────────────────────────
    # DD Guard
    # ─────────────────────────────────────────────────────────

    def _dd_guard(self, current_price: float) -> Optional[Signal]:
        state  = self.state
        cfg    = self.cfg
        equity = cfg.account_equity

        state.new_day_check(equity)

        if state.daily_start_equity > 0:
            daily_dd = (state.daily_start_equity - equity - state.daily_realized_pnl) / state.daily_start_equity
            if daily_dd >= cfg.max_daily_dd_pct and state.direction:
                return self._guard_flat(current_price, "daily_dd_guard")

        if state.peak_equity > 0:
            total_dd = (state.peak_equity - equity) / state.peak_equity
            if total_dd >= cfg.max_total_dd_pct and state.direction:
                return self._guard_flat(current_price, "total_dd_guard")

        return None

    def _guard_flat(self, price: float, reason: str) -> Signal:
        state = self.state
        cfg   = self.cfg
        return Signal(
            action="FLAT", symbol=cfg.symbol, price=price,
            direction=state.direction, confidence=1.0,
            sl_price=price, tp1_price=price, tp2_price=price,
            atr=0, momentum_score=0, atr_ratio=0, rr_ratio=0,
            reason=reason,
        )

    # ─────────────────────────────────────────────────────────
    # Signal builder
    # ─────────────────────────────────────────────────────────

    def _make_signal(
        self, action, direction, price, atr, atr_ratio,
        momentum, layer, sl_dist, reason=""
    ) -> Signal:
        cfg = self.cfg

        tp1 = atr * cfg.tp1_atr_mult
        tp2 = atr * cfg.tp2_atr_mult

        if direction == "LONG":
            sl_price  = price - sl_dist
            tp1_price = price + tp1
            tp2_price = price + tp2
        elif direction == "SHORT":
            sl_price  = price + sl_dist
            tp1_price = price - tp1
            tp2_price = price - tp2
        else:
            sl_price  = tp1_price = tp2_price = price

        rr   = tp1 / sl_dist if sl_dist > 0 else 0
        conf = self._confidence(atr_ratio, momentum, rr, layer)
        size = self._position_size(price, sl_dist)

        return Signal(
            action=action, symbol=cfg.symbol, price=price,
            direction=direction, confidence=conf,
            sl_price=sl_price, tp1_price=tp1_price, tp2_price=tp2_price,
            atr=atr, momentum_score=momentum,
            atr_ratio=atr_ratio, rr_ratio=rr,
            layer=layer, avg_entry=self.state.avg_entry,
            size=size, reason=reason,
        )

    # ─────────────────────────────────────────────────────────
    # [ADD 2] Position sizing — risk_pct × equity / sl_dist
    # ─────────────────────────────────────────────────────────

    def _position_size(self, price: float, sl_dist: float) -> float:
        cfg = self.cfg
        if sl_dist <= 0 or price <= 0:
            return 0.0
        risk_usd = cfg.account_equity * cfg.risk_pct
        return round(risk_usd / sl_dist, 6)

    # ─────────────────────────────────────────────────────────
    # Confidence — giữ nguyên v9 gốc
    # ─────────────────────────────────────────────────────────

    def _confidence(self, atr_ratio, momentum, rr, layer) -> float:
        p = 0.55
        if atr_ratio > 1.2: p *= 1.10
        if abs(momentum) > 1: p *= 1.05
        if rr > 2: p *= 1.08
        if layer >= 3: p *= 1.05
        return round(max(0.5, min(p, 0.9)), 3)

    # ─────────────────────────────────────────────────────────
    # Indicators — thêm MA10, MA25
    # ─────────────────────────────────────────────────────────

    def _indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df      = df.copy()
        df["atr"]   = self._atr(df, 14)
        df["ema20"] = df.close.ewm(span=20, adjust=False).mean()
        df["ma10"]  = df.close.rolling(10).mean()
        df["ma25"]  = df.close.rolling(25).mean()
        df["ema200"]= df.close.ewm(span=200, adjust=False).mean() # MTF H1 approx
        return df

    def _atr(self, df, n):
        h  = df.high
        l  = df.low
        c  = df.close.shift()
        tr = pd.concat([(h - l), (h - c).abs(), (l - c).abs()], axis=1).max(axis=1)
        return tr.ewm(span=n, adjust=False).mean()

    # ─────────────────────────────────────────────────────────
    # [FIX 2] Pivot detection — cả 2 chiều (left + right)
    # v9 gốc chỉ check left → false pivot nhiều
    # ─────────────────────────────────────────────────────────

    def _get_resistance(self, df: pd.DataFrame, n: int = 5) -> Optional[float]:
        highs = df.high.values
        L     = len(highs)
        for i in range(L - n - 1, n - 1, -1):
            left  = all(highs[i] >= highs[i - j] for j in range(1, n + 1))
            right = all(highs[i] >= highs[i + j] for j in range(1, n + 1))
            if left and right:
                return float(highs[i])
        return None

    def _get_support(self, df: pd.DataFrame, n: int = 5) -> Optional[float]:
        lows = df.low.values
        L    = len(lows)
        for i in range(L - n - 1, n - 1, -1):
            left  = all(lows[i] <= lows[i - j] for j in range(1, n + 1))
            right = all(lows[i] <= lows[i + j] for j in range(1, n + 1))
            if left and right:
                return float(lows[i])
        return None

    # ─────────────────────────────────────────────────────────
    # PnL helper
    # ─────────────────────────────────────────────────────────

    def _calc_pnl(self, exit_price: float) -> float:
        state = self.state
        pnl   = 0.0
        for ep, sz in zip(state.entry_prices, state.sizes):
            if state.direction == "LONG":
                pnl += (exit_price - ep) * sz
            elif state.direction == "SHORT":
                pnl += (ep - exit_price) * sz
        return pnl


# ─────────────────────────────────────────────────────────────
# Checklist trước khi live
# ─────────────────────────────────────────────────────────────
# 1. Gọi apply_signal() sau MỖI fill từ exchange
# 2. Sau mỗi FLAT → sync cfg.account_equity từ BingX API
# 3. Timeframe khuyến nghị: 15m–1h (pivot n=5 cần đủ bar)
# 4. Volume col không bắt buộc (strategy không dùng)
# 5. sl_atr_cap=3.0 phù hợp BTC/ETH, có thể giảm xuống 2.5 cho SOL
#
# Backtest loop skeleton:
#
#   cfg   = PyramidConfig(symbol="BTC/USDT", account_equity=10_000.0)
#   strat = GridPyramidStrategy(cfg)
#
#   for i in range(50, len(df)):
#       window = df.iloc[:i]
#       sig    = strat.process(window)
#       if sig is None:
#           continue
#       filled_price = sig.price      # live: dùng giá fill thực từ exchange
#       filled_size  = sig.size
#       strat.apply_signal(sig, filled_price, filled_size)
#       if sig.action == "FLAT":
#           cfg.account_equity = fetch_bingx_equity()
