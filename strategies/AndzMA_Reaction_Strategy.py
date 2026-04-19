# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
# --- Do not remove these libs ---
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import talib.abstract as ta
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import (
    DecimalParameter,
    IntParameter,
    IStrategy,
)


class AndzMA_Reaction_Strategy(IStrategy):
    """
    MA Reaction Strategy — ITER5 (Bounce + Crossover)

    TWO entry modes (both require EMA200 trend + ADX + Volume):

    Mode 1 — EMA Crossover (ITER4):
      LONG : EMA9 crosses above EMA21
      SHORT: EMA9 crosses below EMA21

    Mode 2 — MA Bounce (NEW ITER5):
      LONG : Price pulls back to EMA_slow from above, candle low touches
             EMA_slow zone (within 0.5×ATR), close reclaims above EMA_slow
             + bullish candle (close > open)
      SHORT: Price rallies back to EMA_slow from below, candle high enters
             EMA_slow zone (within 0.5×ATR), close stays below EMA_slow
             + bearish candle (close < open)

    Exit: ROI table + trailing stop + 48h time exit (no exit signal)

    Timeframe: 15m
    """

    INTERFACE_VERSION = 3

    # -------------------------------------------------------------------------
    # Core settings
    # -------------------------------------------------------------------------
    timeframe = '15m'

    # ROI: let trailing stop work, hard TP as safety
    minimal_roi = {
        "0":   0.08,   # 8%  hard TP
        "480": 0.03,   # 3%  after 8h
        "960": 0.01    # 1%  after 16h
    }

    stoploss = -0.04  # widened -3% → -4% to reduce SL hits from normal volatility

    trailing_stop = True
    trailing_stop_positive = 0.015        # trail 1.5% from peak (was 1.0%)
    trailing_stop_positive_offset = 0.025  # activate at +2.5% profit (was 1.5%)
    trailing_only_offset_is_reached = True

    use_custom_stoploss = False

    process_only_new_candles = True
    use_exit_signal = False
    exit_profit_only = False
    exit_profit_offset = 0.0
    ignore_roi_if_entry_signal = False

    max_open_trades = 5

    can_short = True

    # -------------------------------------------------------------------------
    # Hyperopt Parameters
    # -------------------------------------------------------------------------

    ema_fast_period  = IntParameter(5,   15,  default=9,   space='buy', optimize=True)
    ema_slow_period  = IntParameter(15,  30,  default=21,  space='buy', optimize=True)
    ema_trend_period = IntParameter(150, 250, default=200, space='buy', optimize=True)

    volume_sma_period  = IntParameter(10,  30,  default=20,  space='buy', optimize=True)
    volume_multiplier  = DecimalParameter(1.0, 3.0, default=2.0, space='buy', optimize=True)  # raised 1.2→2.0

    rsi_period    = IntParameter(10, 20, default=14, space='buy', optimize=True)
    adx_threshold = IntParameter(20, 40, default=25, space='buy', optimize=True)  # raised 18→25

    # Bounce zone: how close (× ATR) the candle must come to EMA_slow
    bounce_atr_mult = DecimalParameter(0.2, 1.0, default=0.6, space='buy', optimize=True)

    # -------------------------------------------------------------------------
    # Indicators
    # -------------------------------------------------------------------------

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:

        # --- EMAs ---
        dataframe['ema_fast']  = ta.EMA(dataframe, timeperiod=self.ema_fast_period.value)
        dataframe['ema_slow']  = ta.EMA(dataframe, timeperiod=self.ema_slow_period.value)
        dataframe['ema_trend'] = ta.EMA(dataframe, timeperiod=self.ema_trend_period.value)

        # --- EMA Crossover signals (Mode 1) ---
        dataframe['ema_cross_up'] = (
            (dataframe['ema_fast'] > dataframe['ema_slow']) &
            (dataframe['ema_fast'].shift(1) <= dataframe['ema_slow'].shift(1))
        )
        dataframe['ema_cross_down'] = (
            (dataframe['ema_fast'] < dataframe['ema_slow']) &
            (dataframe['ema_fast'].shift(1) >= dataframe['ema_slow'].shift(1))
        )

        # --- ATR (for bounce zone) ---
        dataframe['atr'] = ta.ATR(dataframe, timeperiod=14)

        # --- MA Bounce signals (Mode 2) ---
        bounce_zone = dataframe['atr'] * self.bounce_atr_mult.value

        # LONG bounce: price was above EMA_slow, pulled back, low entered the zone,
        #              close reclaimed above EMA_slow, bullish candle
        dataframe['ma_bounce_long'] = (
            (dataframe['close'].shift(1) > dataframe['ema_slow'].shift(1)) &  # was above
            (dataframe['low']  <= dataframe['ema_slow'] + bounce_zone) &      # low touched zone
            (dataframe['low']  >= dataframe['ema_slow'] - bounce_zone) &      # not too deep
            (dataframe['close'] > dataframe['ema_slow']) &                    # closed above
            (dataframe['close'] > dataframe['open'])                          # bullish candle
        )

        # SHORT bounce: price was below EMA_slow for 2+ candles (confirmed downtrend),
        #               high of this candle touched/entered the EMA zone,
        #               close stayed below EMA_slow, bearish candle
        #               + close in bottom 40% of candle range (strong rejection)
        candle_range = dataframe['high'] - dataframe['low']
        close_pos = (dataframe['close'] - dataframe['low']) / candle_range.replace(0, np.nan)

        dataframe['ma_bounce_short'] = (
            (dataframe['close'].shift(1) < dataframe['ema_slow'].shift(1)) &  # was below prev
            (dataframe['close'].shift(2) < dataframe['ema_slow'].shift(2)) &  # 2 candles below (confirmed)
            (dataframe['high'] >= dataframe['ema_slow'] - bounce_zone) &      # high touched zone
            (dataframe['high'] <= dataframe['ema_slow'] + bounce_zone) &      # not too deep
            (dataframe['close'] < dataframe['ema_slow']) &                    # closed below
            (dataframe['close'] < dataframe['open']) &                        # bearish candle
            (close_pos <= 0.4)                                                # close in bottom 40% (strong rejection)
        )

        # --- RSI ---
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=self.rsi_period.value)

        # --- Volume filter ---
        dataframe['volume_sma']   = ta.SMA(dataframe['volume'], timeperiod=self.volume_sma_period.value)
        dataframe['volume_ratio'] = dataframe['volume'] / dataframe['volume_sma']
        dataframe['volume_ok']    = dataframe['volume_ratio'] >= self.volume_multiplier.value

        # --- ADX ---
        dataframe['adx'] = ta.ADX(dataframe, timeperiod=14)

        # --- Macro trend ---
        dataframe['above_trend_ema'] = dataframe['close'] > dataframe['ema_trend']
        dataframe['below_trend_ema'] = dataframe['close'] < dataframe['ema_trend']

        return dataframe

    # -------------------------------------------------------------------------
    # Entry Signals
    # -------------------------------------------------------------------------

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Shared filters (both modes): EMA200 trend + ADX trending + Volume spike

        LONG  = (EMA crossover OR MA bounce) + uptrend filters
        SHORT = (EMA crossover OR MA bounce) + downtrend filters
        """

        # Shared filters
        trend_filters_long  = (
            dataframe['above_trend_ema'] &
            (dataframe['adx'] > self.adx_threshold.value) &
            dataframe['volume_ok']
        )
        trend_filters_short = (
            dataframe['below_trend_ema'] &
            (dataframe['adx'] > self.adx_threshold.value) &
            dataframe['volume_ok']
        )

        # Mode 1: Crossover
        long_cross  = dataframe['ema_cross_up']   & trend_filters_long
        short_cross = dataframe['ema_cross_down']  & trend_filters_short

        # Mode 2: MA Bounce (latest MA reaction)
        long_bounce  = dataframe['ma_bounce_long']  & trend_filters_long
        short_bounce = dataframe['ma_bounce_short'] & trend_filters_short

        # Combined
        long_conditions  = long_cross  | long_bounce
        short_conditions = short_cross | short_bounce

        dataframe.loc[long_conditions,  'enter_long']  = 1
        dataframe.loc[short_conditions, 'enter_short'] = 1

        # Tag to distinguish mode — bounce takes priority when both fire same candle
        dataframe.loc[long_cross,   'enter_tag'] = 'cross_long'
        dataframe.loc[short_cross,  'enter_tag'] = 'cross_short'
        dataframe.loc[long_bounce,  'enter_tag'] = 'bounce_long'   # overwrites cross intentionally
        dataframe.loc[short_bounce, 'enter_tag'] = 'bounce_short'  # overwrites cross intentionally

        return dataframe

    # -------------------------------------------------------------------------
    # Exit Signals — ROI + stoploss handles everything
    # -------------------------------------------------------------------------

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    # -------------------------------------------------------------------------
    # Custom Exit — time-based safety
    # -------------------------------------------------------------------------

    def custom_exit(
        self,
        pair: str,
        trade: 'Trade',
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs
    ) -> Optional[str]:
        trade_duration = current_time - trade.open_date_utc
        if trade_duration >= timedelta(hours=48):
            return 'time_exit_48h'
        return None

    # -------------------------------------------------------------------------
    # Metadata
    # -------------------------------------------------------------------------

    def version(self) -> str:
        return "1.5.0 - AndzMA Reaction ITER5 (Crossover + MA Bounce)"
