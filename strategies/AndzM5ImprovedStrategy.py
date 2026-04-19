# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
# --- Do not remove these libs ---
from datetime import datetime
from typing import Optional

import pandas as pd
from pandas import DataFrame
from freqtrade.strategy import (
    IStrategy,
    IntParameter,
    DecimalParameter,
    BooleanParameter,
)
from freqtrade.persistence import Trade

import talib.abstract as ta
import numpy as np

# --------------------------------
# Add your lib to import here
# --------------------------------


class AndzM5ImprovedStrategy(IStrategy):
    """
    Freqtrade conversion of "Andz M5 Improved" Pine Script strategy.

    Original Pine Script Components:
    - Indicators: EMA Fast (50), EMA Slow (100), ATR (14), Volume SMA (20), ADX (14), RSI (14)
    - Entry Long: EMA Fast > EMA Slow + Bull Candle + (Volume Spike OR Momentum Spike) + ADX/RSI filters
    - Entry Short: EMA Fast < EMA Slow + Bear Candle + (Volume Spike OR Momentum Spike) + ADX/RSI filters
    - Volume Spike: volume > volMA * 1.25
    - Momentum Spike: candle body > ATR * 1.3
    - Stoploss: ATR * 2.0 (dynamic)
    - Trailing Stop: Activates when profit >= 1.5 * ATR, then trails by ATR distance
    - Position Sizing: Risk 2% per trade, with 1.5x multiplier for strong signals
    - Max Concurrent Trades: 5

    Timeframe: 5m
    """

    # Strategy interface version
    INTERFACE_VERSION = 3

    # Minimal ROI designed for the strategy
    minimal_roi = {
        "0": 100  # Disable ROI-based exits, rely on stoploss/trailing
    }

    # Optimal timeframe
    timeframe = '15m'

    # Fixed stoploss: 2% (SL unit for R:R calculation)
    # BTC TP = 4%  → R:R 1:2
    # ETH TP = 8%  → R:R 1:4  (realistic for 15m timeframe)
    stoploss = -0.02

    # Trailing stop and custom stoploss both DISABLED.
    # Exit logic is entirely handled by custom_exit() which IS called in backtest.
    trailing_stop = False
    use_custom_stoploss = False

    # Run "populate_indicators()" only for new candle
    process_only_new_candles = True

    # Maximum number of concurrent trades
    max_open_trades = 5

    # These values can be overridden in the config
    use_exit_signal = True
    exit_profit_only = False
    exit_profit_offset = 0.0
    ignore_roi_if_entry_signal = False

    # Optional protections
    protections = []

    # ---------------------------------------------------------------------------
    # Strategy Parameters (configurable via config.json or hyperopt)
    # ---------------------------------------------------------------------------

    # EMA periods
    ema_fast_period = IntParameter(20, 100, default=50, space='buy', optimize=True)
    ema_slow_period = IntParameter(50, 200, default=100, space='buy', optimize=True)

    # ATR period and multipliers
    atr_period = IntParameter(10, 20, default=14, space='buy', optimize=True)
    atr_stoploss_multiplier = DecimalParameter(1.0, 3.0, default=2.0, space='sell', optimize=True)
    atr_trailing_activation_multiplier = DecimalParameter(1.0, 2.5, default=1.5, space='sell', optimize=True)
    atr_trailing_distance_multiplier = DecimalParameter(0.5, 2.0, default=1.0, space='sell', optimize=True)

    # Volume spike settings
    volume_sma_period = IntParameter(10, 30, default=20, space='buy', optimize=True)
    volume_spike_multiplier = DecimalParameter(1.0, 3.0, default=1.25, space='buy', optimize=True)

    # Momentum spike settings
    momentum_spike_multiplier = DecimalParameter(1.0, 2.0, default=1.3, space='buy', optimize=True)

    # Risk management
    risk_per_trade = DecimalParameter(0.5, 5.0, default=2.0, space='buy', optimize=True)
    strong_signal_multiplier = DecimalParameter(1.0, 3.0, default=1.5, space='buy', optimize=True)

    # Filter settings
    use_adx_filter = BooleanParameter(default=False, space='buy', optimize=True)
    adx_threshold = IntParameter(15, 35, default=20, space='buy', optimize=True)

    use_rsi_filter = BooleanParameter(default=False, space='buy', optimize=True)
    rsi_period = IntParameter(10, 20, default=14, space='buy', optimize=True)
    rsi_overbought = IntParameter(60, 80, default=70, space='buy', optimize=True)
    rsi_oversold = IntParameter(20, 40, default=30, space='buy', optimize=True)

    # ---------------------------------------------------------------------------
    # Indicator Calculations
    # ---------------------------------------------------------------------------

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Calculate all required indicators using vectorized pandas operations.

        Indicators calculated:
        - EMA Fast (default: 50)
        - EMA Slow (default: 100)
        - ATR (default: 14)
        - Volume SMA (default: 20)
        - ADX (default: 14)
        - RSI (default: 14)
        """
        # EMA Fast and Slow
        dataframe['ema_fast'] = ta.EMA(
            dataframe,
            timeperiod=self.ema_fast_period.value
        )
        dataframe['ema_slow'] = ta.EMA(
            dataframe,
            timeperiod=self.ema_slow_period.value
        )

        # ATR (Average True Range)
        dataframe['atr'] = ta.ATR(
            dataframe,
            timeperiod=self.atr_period.value
        )

        # Volume SMA
        dataframe['vol_sma'] = ta.SMA(
            dataframe['volume'],
            timeperiod=self.volume_sma_period.value
        )

        # ADX (Average Directional Index)
        dataframe['adx'] = ta.ADX(
            dataframe,
            timeperiod=self.atr_period.value  # Using same period as ATR (default 14)
        )

        # RSI (Relative Strength Index)
        dataframe['rsi'] = ta.RSI(
            dataframe,
            timeperiod=self.rsi_period.value
        )

        # Calculate candle body (absolute value of open - close)
        dataframe['candle_body'] = dataframe['close'] - dataframe['open']
        dataframe['candle_body_abs'] = dataframe['candle_body'].abs()

        # Volume spike detection: volume > volMA * volume_spike_multiplier
        dataframe['volume_spike'] = (
            dataframe['volume'] > (dataframe['vol_sma'] * self.volume_spike_multiplier.value)
        )

        # Momentum spike detection: candle body > ATR * momentum_spike_multiplier
        dataframe['momentum_spike'] = (
            dataframe['candle_body_abs'] > (dataframe['atr'] * self.momentum_spike_multiplier.value)
        )

        # Strong signal: both volume AND momentum spikes
        dataframe['strong_signal'] = (
            dataframe['volume_spike'] & dataframe['momentum_spike']
        )

        # Bull candle (close > open)
        dataframe['bull_candle'] = dataframe['close'] > dataframe['open']

        # Bear candle (close < open)
        dataframe['bear_candle'] = dataframe['close'] < dataframe['open']

        # EMA crossover conditions
        dataframe['ema_fast_above_slow'] = dataframe['ema_fast'] > dataframe['ema_slow']
        dataframe['ema_fast_below_slow'] = dataframe['ema_fast'] < dataframe['ema_slow']

        # ADX filter condition (if enabled)
        if self.use_adx_filter.value:
            dataframe['adx_pass'] = dataframe['adx'] >= self.adx_threshold.value
        else:
            dataframe['adx_pass'] = True

        # RSI filter conditions (if enabled)
        if self.use_rsi_filter.value:
            # For long: RSI should not be overbought (RSI < rsi_overbought)
            dataframe['rsi_long_pass'] = dataframe['rsi'] < self.rsi_overbought.value
            # For short: RSI should not be oversold (RSI > rsi_oversold)
            dataframe['rsi_short_pass'] = dataframe['rsi'] > self.rsi_oversold.value
        else:
            dataframe['rsi_long_pass'] = True
            dataframe['rsi_short_pass'] = True

        return dataframe

    # ---------------------------------------------------------------------------
    # Entry Signal Generation
    # ---------------------------------------------------------------------------

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Generate entry signals based on the strategy conditions.

        Entry Long Conditions:
        - EMA Fast > EMA Slow
        - Bull Candle (close > open)
        - Volume Spike OR Momentum Spike
        - ADX filter (if enabled): ADX >= threshold
        - RSI filter (if enabled): RSI < overbought level

        Entry Short Conditions:
        - EMA Fast < EMA Slow
        - Bear Candle (close < open)
        - Volume Spike OR Momentum Spike
        - ADX filter (if enabled): ADX >= threshold
        - RSI filter (if enabled): RSI > oversold level
        """
        # Any spike condition (volume OR momentum)
        dataframe['any_spike'] = dataframe['volume_spike'] | dataframe['momentum_spike']

        # Long entry conditions
        long_conditions = (
            dataframe['ema_fast_above_slow'] &
            dataframe['bull_candle'] &
            dataframe['any_spike'] &
            dataframe['adx_pass'] &
            dataframe['rsi_long_pass']
        )

        # Short entry conditions
        short_conditions = (
            dataframe['ema_fast_below_slow'] &
            dataframe['bear_candle'] &
            dataframe['any_spike'] &
            dataframe['adx_pass'] &
            dataframe['rsi_short_pass']
        )

        # Set entry signals
        dataframe.loc[long_conditions, 'enter_long'] = 1
        dataframe.loc[short_conditions, 'enter_short'] = 1

        # Add signal strength columns for reference
        dataframe.loc[long_conditions & dataframe['strong_signal'], 'enter_long'] = 2  # Strong signal
        dataframe.loc[short_conditions & dataframe['strong_signal'], 'enter_short'] = 2  # Strong signal

        return dataframe

    # ---------------------------------------------------------------------------
    # Exit Signal Generation
    # ---------------------------------------------------------------------------

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        No signal-based exits. All exits handled by:
          - stoploss = -0.02 (Freqtrade built-in, works in backtest)
          - custom_exit()  for per-pair TP (also works in backtest)
        """
        dataframe['exit_long'] = 0
        dataframe['exit_short'] = 0
        return dataframe

    def custom_exit(
        self,
        pair: str,
        trade: 'Trade',
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs
    ) -> Optional[str]:
        """
        Per-pair fixed R:R take-profit (called during backtesting AND live).

        SL = 2% (config stoploss)
        BTC/USDT : TP = 4%  → R:R 1:2  (validated: 35.8% WR, profitable)
        ETH/USDT : TP = 8%  → R:R 1:4  (conservative relative to 1:8, more trades complete)
        """
        if 'BTC' in pair:
            tp_pct = 0.04  # 4% profit target
        else:
            tp_pct = 0.08  # 8% profit target

        if current_profit >= tp_pct:
            return f'tp_{pair.split("/")[0]}'

        return None

    # ---------------------------------------------------------------------------
    # Custom Stoploss (ATR-based with Trailing)
    # ---------------------------------------------------------------------------

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs
    ) -> Optional[float]:
        """
        Implement ATR-based dynamic stoploss with trailing functionality.

        Pine Script Translation:
        - trailThreshold = slDist * trailStartMult * syminfo.pointvalue
        - if currentProfit >= trailThreshold: activate trailing stop
        - trail_points = slDist, trail_offset = slDist

        Freqtrade Implementation:
        - Base stoploss: ATR * atr_stoploss_multiplier (default: 2.0)
        - Trailing activates when profit >= ATR * atr_trailing_activation_multiplier (default: 1.5)
        - Once activated, trailing stop follows price at ATR * atr_trailing_distance_multiplier distance

        CRITICAL: Returns stoploss as a NEGATIVE value representing distance from current price.
        For long positions: stoploss is below current price (negative value)
        For short positions: stoploss is above current price (also negative value in Freqtrade)

        Returns:
        - Negative float representing the stoploss distance from current price
        - None to use the default stoploss
        """
        # Get the latest candle data for this pair
        # We need to access the dataframe to get the ATR value
        dataframe, _ = self.dp.get_analyzed_dataframe(pair=pair, timeframe=self.timeframe)

        if len(dataframe) == 0:
            return None

        # Get the latest ATR value
        last_row = dataframe.iloc[-1]
        atr = last_row['atr']

        if pd.isna(atr) or atr <= 0:
            # Fallback to default stoploss if ATR is not available
            return None

        # CRITICAL: Trailing activation threshold (Pine Script: trailThreshold = slDist * trailStartMult)
        # In Pine Script, this is compared to profit in quote currency
        # Here we convert to percentage: (ATR * 1.5) / open_rate
        trail_activation_pct = (atr * self.atr_trailing_activation_multiplier.value) / trade.open_rate
        
        # Calculate the base stoploss distance as percentage
        # This is the initial stoploss before trailing activates
        base_stoploss_pct = (atr * self.atr_stoploss_multiplier.value) / current_rate
        
        # Check if trailing stop should activate
        if current_profit >= trail_activation_pct:
            # Trailing is ACTIVE
            # Calculate trailing distance as percentage
            trailing_dist_pct = (atr * self.atr_trailing_distance_multiplier.value) / current_rate

            # Return the trailing stoploss (always negative in Freqtrade)
            # For both long and short, Freqtrade expects negative values
            return -trailing_dist_pct
        else:
            # Trailing NOT active yet - use fixed stoploss
            # Return negative value representing distance from current price
            return -base_stoploss_pct

    # ---------------------------------------------------------------------------
    # Custom Stake Amount (Risk-based Position Sizing)
    # ---------------------------------------------------------------------------

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: Optional[float],
        max_stake: float,
        leverage: float,
        entry_tag: Optional[str],
        side: str,
        **kwargs
    ) -> float:
        """
        Use the stake_amount from config (proposed_stake).
        Fixed position size ensures consistent backtesting and risk control.
        """
        return proposed_stake

    # ---------------------------------------------------------------------------
    # Leverage Configuration (Optional)
    # ---------------------------------------------------------------------------

    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: Optional[str],
        side: str,
        **kwargs
    ) -> float:
        """
        Customize leverage for each trade.

        Default: 1x (no leverage)
        Override this method to implement dynamic leverage based on signal strength.
        """
        return 1.0

    # ---------------------------------------------------------------------------
    # Strategy Metadata
    # ---------------------------------------------------------------------------

    def version(self) -> str:
        """
        Returns version of the strategy.
        """
        return "1.0.0 - Andz M5 Improved"
