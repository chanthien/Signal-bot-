"""
Black Swan Capital — RAVE-Specific Long Strategy
15m timeframe, trailing stop để bắt các pump 40%+

Edge Analysis (Dec 2025 - Apr 2026):
  - RAVE uptrend +2050% — SHORT là tự sát (77% SL rate)
  - LONG bias hours: 1, 5, 6, 8, 10, 13, 15, 16, 17, 19 (pump/dump ratio > 1.5)
  - SHORT bias hours: 7, 9, 11, 12, 14 → AVOID entering
  - Vol spike median 0.54x → dùng 1.5x threshold (không phải 2x)
  - Biggest pumps: 22h, 6h, 7h (Apr 9-13 run) — trailing stop cần để runner chạy
  - TP cứng 10% bỏ miss 40%+ pumps → dùng trailing stop thay thế

Strategy:
  - Entry: ret_2h >= 8% + vol_spike >= 1.5x + RSI < 82 + LONG hour
  - Exit: trailing stop 5% (activate at +6%) → bắt pump runners
  - Hard SL: -8%
  - Time stop: 12h (48 candles) nếu không hit TP/SL
"""

from freqtrade.strategy import IStrategy, IntParameter, DecimalParameter
from pandas import DataFrame
import pandas as pd
import numpy as np
import talib.abstract as ta


class RaveStrategy(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = '15m'

    # ── CORE INSIGHT ──────────────────────────────────────────────────────────
    # RAVE pump cycles lên 40-70%+ → SL chật (-8%) bị kick ra trước khi bắt move
    # Simulation 97 trades: SL-8% trail5@6% → avg +1.1%/trade
    #                        SL-20% trail12@25% → avg +9.4%/trade (WR 68.7%)
    # → Cần SL rộng để survive correction, trailing rộng để bắt pump dài

    stoploss = -0.20                      # -20%: survive RAVE's normal corrections
    trailing_stop = True
    trailing_stop_positive = 0.12        # trail 12% sau khi đạt offset
    trailing_stop_positive_offset = 0.25 # kích hoạt trailing khi giá +25%
    trailing_only_offset_is_reached = True

    # Không dùng ROI cứng — để trailing stop quyết định exit
    minimal_roi = {"0": 10.0}  # effectively disabled

    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    can_short = False  # LONG ONLY — RAVE đang uptrend mạnh

    process_only_new_candles = True
    startup_candle_count = 700

    MAX_CANDLES = 96  # 24h time stop (RAVE pump cycles kéo dài hơn)

    # === RAVE-specific hours từ phân tích pump/dump ratio ===
    # Tránh giờ dump bias: 7 (43%), 9 (41%), 11 (33%), 12 (29%), 14 (29%)
    AVOID_HOURS = {7, 9, 11, 12, 14}

    # Hyperopt parameters
    rsi_max = IntParameter(75, 92, default=85, space='buy', optimize=True)
    ret_2h_min = DecimalParameter(0.04, 0.15, default=0.06, decimals=2,
                                  space='buy', optimize=True)
    vol_spike_min = DecimalParameter(1.0, 3.0, default=1.2, decimals=1,
                                     space='buy', optimize=True)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)

        # 2h return (8 candles × 15m = 120 min)
        dataframe['ret_2h'] = (
            dataframe['close'] / dataframe['close'].shift(8) - 1
        )

        # Volume spike vs 7d rolling mean
        vol_ma = dataframe['volume'].rolling(window=672, min_periods=200).mean()
        dataframe['vol_spike'] = (
            dataframe['volume'] / vol_ma.replace(0, np.nan)
        )

        # 4h momentum (trend confirmation)
        dataframe['ret_4h'] = (
            dataframe['close'] / dataframe['close'].shift(16) - 1
        )

        dataframe['hour_utc'] = dataframe['date'].dt.hour

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                # Momentum: giá tăng ít nhất 8% trong 2h
                (dataframe['ret_2h'] >= self.ret_2h_min.value) &
                # Volume xác nhận: ít nhất 1.5x trung bình 7d
                (dataframe['vol_spike'] >= self.vol_spike_min.value) &
                # RSI chưa quá overbought
                (dataframe['rsi'] < self.rsi_max.value) &
                # 4h trend cũng đang up (tránh dead cat bounce)
                (dataframe['ret_4h'] >= 0.0) &
                # Tránh giờ dump bias
                (~dataframe['hour_utc'].isin(self.AVOID_HOURS)) &
                # Data quality
                (dataframe['volume'] > 0) &
                (dataframe['vol_spike'].notna()) &
                (dataframe['ret_2h'].notna())
            ),
            'enter_long'
        ] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Trailing stop + time stop xử lý exit
        dataframe.loc[:, 'exit_long'] = 0
        return dataframe

    def custom_exit(self, pair, trade, current_time, current_rate,
                    current_profit, **kwargs):
        """Time stop: thoát sau 24h nếu không hit TP/SL và không có lời."""
        candles_held = int(
            (current_time - trade.open_date_utc).total_seconds() / 900
        )
        if candles_held >= self.MAX_CANDLES and current_profit < 0:
            return 'time_stop_24h_loss'
        return None

    def confirm_trade_entry(self, pair, order_type, amount, rate,
                             time_in_force, current_time, entry_tag,
                             side, **kwargs):
        """Tránh vào lệnh trong giờ dump bias."""
        if current_time.hour in self.AVOID_HOURS:
            return False
        return True
