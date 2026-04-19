"""
Black Swan Capital — AndzV80 Momentum Trend Strategy (FreqTrade Port)
Ported from VPS andz_v80_strategy.py

Logic:
  - EMA25/EMA100 for trend direction
  - EMA200 as H1 macro trend filter (same timeframe = approximate)
  - Breakout of 20-period high/low as additional entry trigger
  - ADX > 20 + RSI confirmation
  - Pyramiding via max_entry_position_adjustment (up to 2 adds)

SL/TP:
  - SL = 1.5 ATR from entry (custom_stoploss)
  - Trailing: activates at +1 ATR profit, trails by 0.8 ATR from peak
  - RSI exit: RSI < 42 (long) / RSI > 58 (short)
  - Time exit: 48h

Pairs: BTC/USDT, ETH/USDT (Binance futures — dry_run)
Timeframe: 15m
"""

from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import talib.abstract as ta
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import DecimalParameter, IntParameter, IStrategy


class AndzV80Strategy(IStrategy):

    INTERFACE_VERSION = 3

    timeframe = '15m'

    # ROI: rely on custom trailing stop + RSI exit
    minimal_roi = {"0": 0.20, "960": 0.05}   # 20% hard TP, 5% after 16h

    stoploss = -0.05          # fallback hard SL (custom_stoploss takes over)

    trailing_stop = False     # handled in custom_stoploss
    use_custom_stoploss = True

    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    process_only_new_candles = True

    max_open_trades = 4
    can_short = True

    startup_candle_count = 250

    # ── Pyramiding ────────────────────────────────────────────────────────
    position_adjustment_enable = True
    max_entry_position_adjustment = 2    # 1 initial + 2 adds = 3 layers max

    # ── Hyperopt params ───────────────────────────────────────────────────
    adx_threshold    = IntParameter(20, 35, default=25, space='buy', optimize=True)
    breakout_period  = IntParameter(15, 30, default=20, space='buy', optimize=True)
    volume_mult      = DecimalParameter(1.2, 3.0, default=1.8, decimals=1, space='buy', optimize=True)
    atr_sl_mult      = DecimalParameter(1.0, 2.5, default=1.5, decimals=1, space='buy', optimize=True)
    atr_trail_mult   = DecimalParameter(0.5, 1.5, default=0.8, decimals=1, space='buy', optimize=True)
    # RSI exit: only exit on extreme exhaustion (not normal RSI fluctuation)
    rsi_exit_long    = IntParameter(25, 40, default=35, space='sell', optimize=True)
    rsi_exit_short   = IntParameter(60, 75, default=65, space='sell', optimize=True)

    # ── Indicators ────────────────────────────────────────────────────────

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:

        dataframe['ema25']  = ta.EMA(dataframe, timeperiod=25)
        dataframe['ema100'] = ta.EMA(dataframe, timeperiod=100)
        dataframe['ema200'] = ta.EMA(dataframe, timeperiod=200)

        dataframe['atr']    = ta.ATR(dataframe, timeperiod=14)
        dataframe['rsi']    = ta.RSI(dataframe, timeperiod=14)
        dataframe['adx']    = ta.ADX(dataframe, timeperiod=14)

        # Volume filter: only trade on volume spikes
        dataframe['vol_sma']   = ta.SMA(dataframe['volume'], timeperiod=20)
        dataframe['vol_ratio'] = dataframe['volume'] / dataframe['vol_sma']

        # Breakout levels (use .shift(1) to avoid look-ahead)
        bp = self.breakout_period.value
        dataframe['breakout_high'] = dataframe['high'].rolling(bp).max().shift(1)
        dataframe['breakout_low']  = dataframe['low'].rolling(bp).min().shift(1)

        # Trend flags
        dataframe['trend_long']  = dataframe['ema25'] > dataframe['ema100']
        dataframe['trend_short'] = dataframe['ema25'] < dataframe['ema100']
        dataframe['h1_long']     = dataframe['close'] > dataframe['ema200']
        dataframe['h1_short']    = dataframe['close'] < dataframe['ema200']

        # Entry conditions
        pullback_long  = (
            (dataframe['close'] > dataframe['ema25']) &
            (dataframe['rsi'] > 50) &
            (dataframe['adx'] > self.adx_threshold.value)
        )
        pullback_short = (
            (dataframe['close'] < dataframe['ema25']) &
            (dataframe['rsi'] < 50) &
            (dataframe['adx'] > self.adx_threshold.value)
        )
        breakout_long  = dataframe['close'] > dataframe['breakout_high']
        breakout_short = dataframe['close'] < dataframe['breakout_low']

        vol_ok = dataframe['vol_ratio'] >= self.volume_mult.value

        dataframe['long_entry']  = (
            dataframe['trend_long'] &
            (pullback_long | breakout_long) &
            dataframe['h1_long'] &
            vol_ok
        )
        dataframe['short_entry'] = (
            dataframe['trend_short'] &
            (pullback_short | breakout_short) &
            dataframe['h1_short'] &
            vol_ok
        )

        return dataframe

    # ── Entry Signals ─────────────────────────────────────────────────────

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            dataframe['long_entry'] & (dataframe['volume'] > 0),
            'enter_long'
        ] = 1
        dataframe.loc[
            dataframe['short_entry'] & (dataframe['volume'] > 0),
            'enter_short'
        ] = 1
        return dataframe

    # ── Exit Signals — RSI exhaustion ─────────────────────────────────────

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Exit LONG when RSI drops below threshold (momentum exhaustion)
        dataframe.loc[
            (dataframe['rsi'] < self.rsi_exit_long.value) & (dataframe['volume'] > 0),
            'exit_long'
        ] = 1
        # Exit SHORT when RSI rises above threshold
        dataframe.loc[
            (dataframe['rsi'] > self.rsi_exit_short.value) & (dataframe['volume'] > 0),
            'exit_short'
        ] = 1
        return dataframe

    # ── Pyramiding: add on 1-ATR move in favor ────────────────────────────

    def adjust_trade_position(
        self, trade: Trade, current_time: datetime,
        current_rate: float, current_profit: float,
        min_stake: Optional[float], max_stake: float,
        current_entry_rate: float, current_exit_rate: float,
        current_entry_profit: float, current_exit_profit: float,
        **kwargs
    ) -> Optional[float]:

        if trade.nr_of_successful_entries >= 3:   # max 3 layers
            return None

        # Get last dataframe
        dataframe, _ = self.dp.get_analyzed_dataframe(trade.pair, self.timeframe)
        if dataframe is None or len(dataframe) < 2:
            return None

        last = dataframe.iloc[-1]
        atr = float(last['atr'])
        avg = trade.open_rate    # use first entry as reference

        # Add when price moved 1 ATR in favor from avg entry
        if trade.is_short:
            if current_rate < avg - atr:
                return trade.stake_amount   # same size as initial
        else:
            if current_rate > avg + atr:
                return trade.stake_amount

        return None

    # ── Custom Stoploss — ATR trailing ────────────────────────────────────

    def custom_stoploss(
        self,
        pair: str, trade: Trade,
        current_time: datetime, current_rate: float,
        current_profit: float, after_fill: bool,
        **kwargs
    ) -> Optional[float]:

        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is None or len(dataframe) == 0:
            return None

        atr = float(dataframe.iloc[-1]['atr'])
        sl_mult = self.atr_sl_mult.value
        trail_mult = self.atr_trail_mult.value

        # Phase 1: hard SL based on ATR
        if current_profit < 0:
            initial_sl = -(atr * sl_mult / trade.open_rate)
            return max(initial_sl, -0.08)   # cap at -8%

        # Phase 2: trailing stop once profit > 1 ATR
        profit_atr = current_profit * trade.open_rate / atr
        if profit_atr >= 1.0:
            trail_distance = (atr * trail_mult) / current_rate
            return current_profit - trail_distance

        return None

    # ── Time exit ─────────────────────────────────────────────────────────

    def custom_exit(
        self, pair: str, trade: Trade,
        current_time: datetime, current_rate: float,
        current_profit: float, **kwargs
    ) -> Optional[str]:
        if current_time - trade.open_date_utc >= timedelta(hours=48):
            return 'time_exit_48h'
        return None

    def version(self) -> str:
        return "1.0.0 - AndzV80 Momentum Trend (EMA25/100/200 + Breakout)"
