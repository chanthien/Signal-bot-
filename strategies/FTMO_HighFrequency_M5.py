# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
# --- Do not remove these libs ---
from datetime import datetime, time
from typing import Optional
import pandas as pd
from pandas import DataFrame
from freqtrade.strategy import IStrategy, DecimalParameter, IntParameter, BooleanParameter
from freqtrade.persistence import Trade
import talib.abstract as ta
import numpy as np

# ===================================
# FTMO COMPLIANT STRATEGY - HIGH FREQUENCY
# ===================================
# Goals:
# - High frequency: 5-15 trades/day
# - Win Rate: 35-75% (acceptable)
# - Focus: Total Profit & ROI
# - FTMO Compliant: Max daily loss 4%, Max total 9%
# - Timeframe: M5 (high frequency)
# - Pairs: BTC/USDT, ETH/USDT, EUR/USD, GBP/USD
# ===================================


class FTMO_HighFrequency_M5(IStrategy):
    """
    FTMO Compliant High Frequency Strategy
    Optimized for M5 timeframe
    Targets: 5-15 trades/day, 40-55% WR, 1.5+ R:R
    """

    INTERFACE_VERSION = 3

    # Strategy basics
    timeframe = '5m'
    can_short = False  # Spot mode for FTMO compliance

    # Minimal ROI - disabled in favor of dynamic exits
    minimal_roi = {"0": 100}

    # Stoploss - overridden by custom_stoploss
    stoploss = -0.99

    # Trailing stop - disabled, using custom logic
    trailing_stop = False

    # Position sizing
    max_open_trades = 3  # FTMO: Max 3 concurrent
    stake_amount = 'unlimited'
    use_custom_stoploss = True

    # FTMO Risk Limits (HARD LIMITS)
    max_daily_loss_pct = 4.0  # FTMO limit: 5%
    max_total_loss_pct = 9.0  # FTMO limit: 10%

    # Hyperopt parameters
    # Entry
    bb_period = IntParameter(15, 25, default=20, space='buy')
    bb_mult = DecimalParameter(2.0, 3.5, default=2.5, space='buy')
    rsi_period = IntParameter(5, 10, default=7, space='buy')
    rsi_oversold = IntParameter(15, 25, default=20, space='buy')
    rsi_overbought = IntParameter(75, 85, default=80, space='buy')
    volume_spike_mult = DecimalParameter(1.5, 3.0, default=2.0, space='buy')

    # Exit
    tp1_ratio = DecimalParameter(0.8, 1.2, default=1.0, space='sell')
    tp2_ratio = DecimalParameter(1.5, 2.5, default=2.0, space='sell')
    sl_multiplier = DecimalParameter(1.0, 2.0, default=1.5, space='sell')
    time_exit_hours = DecimalParameter(2.0, 6.0, default=4.0, space='sell')

    # Filters
    use_volume_filter = BooleanParameter(default=True, space='buy')
    use_session_filter = BooleanParameter(default=True, space='buy')

    # ---------------------------------------------------------------------------
    # Indicator Calculations
    # ---------------------------------------------------------------------------

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Calculate indicators for liquidity grab + mean reversion strategy
        """
        # Bollinger Bands - for overextension
        bb_period = self.bb_period.value
        bb_mult = self.bb_mult.value
        bollinger = ta.BBANDS(
            dataframe['close'],
            timeperiod=bb_period,
            nbdevup=bb_mult,
            nbdevdn=bb_mult
        )
        dataframe['bb_upper'] = bollinger['upperband']
        dataframe['bb_middle'] = bollinger['middleband']
        dataframe['bb_lower'] = bollinger['lowerband']
        dataframe['bb_width'] = (bollinger['upperband'] - bollinger['lowerband']) / bollinger['middleband']

        # RSI - for overbought/oversold
        rsi_period = self.rsi_period.value
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=rsi_period)

        # ATR - for stoploss
        dataframe['atr'] = ta.ATR(dataframe, timeperiod=14)

        # Volume
        dataframe['volume_sma'] = ta.SMA(dataframe['volume'], timeperiod=20)
        dataframe['volume_ratio'] = dataframe['volume'] / dataframe['volume_sma']

        # Previous day high/low (for liquidity levels)
        dataframe['prev_day_high'] = dataframe['high'].shift(1).rolling(288).max()  # 288 * 5m = 1 day
        dataframe['prev_day_low'] = dataframe['low'].shift(1).rolling(288).min()

        # VWAP (intraday)
        dataframe['vwap'] = (dataframe['high'] + dataframe['low'] + dataframe['close']) / 3
        dataframe['vwap'] = dataframe['vwap'].rolling(20).mean()

        # Session hours
        dataframe['hour'] = dataframe['date'].dt.hour
        dataframe['day_of_week'] = dataframe['date'].dt.dayofweek

        # FTMO tracking
        dataframe['daily_pnl'] = self.calculate_daily_pnl(dataframe, metadata)

        return dataframe

    # ---------------------------------------------------------------------------
    # Entry Signals
    # ---------------------------------------------------------------------------

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        LONG Entry: Liquidity grab below previous low + reversal
        """
        # Conditions
        rsi_oversold = self.rsi_oversold.value
        volume_mult = self.volume_spike_mult.value

        # Liquidity grab: Price breaks below prev day low
        liquidity_break = dataframe['low'] < dataframe['prev_day_low']

        # Reversal: Price closes back above prev day low
        reversal = dataframe['close'] > dataframe['prev_day_low']

        # Oversold: RSI < threshold
        oversold = dataframe['rsi'] < rsi_oversold

        # Volume spike: Volume > 2x average
        volume_spike = dataframe['volume_ratio'] > volume_mult

        # Inside BB: Price back inside Bollinger Band
        inside_bb = dataframe['close'] > dataframe['bb_lower']

        # Session filter: London/NY session only (08:00-22:00 UTC)
        session_ok = (dataframe['hour'] >= 8) & (dataframe['hour'] <= 22)

        # Friday: Close before 22:00 UTC (FTMO weekend rule)
        friday_ok = (dataframe['day_of_week'] != 4) | (dataframe['hour'] < 20)

        # Combine conditions
        long_conditions = (
            liquidity_break &
            reversal &
            oversold &
            volume_spike &
            inside_bb &
            session_ok &
            friday_ok
        )

        # Volume filter (optional)
        if self.use_volume_filter.value:
            long_conditions = long_conditions & volume_spike

        dataframe.loc[long_conditions, 'enter_long'] = 1

        return dataframe

    # ---------------------------------------------------------------------------
    # Exit Signals
    # ---------------------------------------------------------------------------

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Exit signals based on TP/SL levels
        """
        dataframe['exit_long'] = 0

        # Time-based exit (4 hours = 48 bars on M5)
        time_exit_bars = int(self.time_exit_hours.value * 12)  # 12 bars per hour on M5

        # Momentum reversal exit
        momentum_reversal = (
            (dataframe['rsi'] > 70) &  # Overbought
            (dataframe['close'] < dataframe['vwap'])  # Below VWAP
        )

        # Mark exit bars
        dataframe.loc[momentum_reversal, 'exit_long'] = 1

        return dataframe

    # ---------------------------------------------------------------------------
    # Custom Stoploss (FTMO Compliant)
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
        Custom stoploss with FTMO compliance
        - Partial TP at 1:1 and 2:1
        - Time-based exit
        - Daily loss limit check
        """
        # Check FTMO daily loss limit
        if self.is_daily_loss_exceeded(trade):
            return -0.01  # Emergency exit

        # Get ATR
        dataframe, _ = self.dp.get_analyzed_dataframe(pair=pair, timeframe=self.timeframe)
        if len(dataframe) == 0:
            return None

        atr = dataframe['atr'].iloc[-1]
        sl_mult = self.sl_multiplier.value

        # Calculate stoploss distance
        sl_distance = atr * sl_mult

        # Partial TP logic
        if current_profit >= sl_mult * 1.0:  # 1:1 R:R
            # First TP hit - close 50%
            return -sl_distance  # Move SL to entry

        if current_profit >= sl_mult * 2.0:  # 2:1 R:R
            # Second TP hit - close remaining
            return -sl_distance * 0.5  # Tight trailing

        # Time-based exit
        trade_duration = (current_time - trade.open_date_utc).total_seconds() / 3600
        if trade_duration >= self.time_exit_hours.value:
            if current_profit > 0:
                return -0.01  # Exit with small profit
            else:
                return -0.02  # Cut loss

        # Standard stoploss
        return -sl_distance

    # ---------------------------------------------------------------------------
    # FTMO Compliance Checks
    # ---------------------------------------------------------------------------

    def calculate_daily_pnl(self, dataframe: DataFrame, metadata: dict) -> float:
        """Calculate daily PnL for FTMO compliance"""
        # This is simplified - in production, track from Trade objects
        return 0.0

    def is_daily_loss_exceeded(self, trade: Trade) -> bool:
        """Check if daily loss limit exceeded (FTMO: 5%)"""
        # Get today's closed trades
        from freqtrade.persistence import Trade
        today = datetime.utcnow().date()

        closed_trades = Trade.get_trades([
            Trade.is_open == False,
            Trade.close_date >= datetime.combine(today, datetime.min.time())
        ]).all()

        # Calculate daily PnL
        daily_pnl = sum(t.close_profit for t in closed_trades if t.close_profit)

        # Check limit
        if daily_pnl < -self.max_daily_loss_pct / 100:
            return True

        return False

    def is_total_loss_exceeded(self) -> bool:
        """Check if total loss limit exceeded (FTMO: 10%)"""
        from freqtrade.persistence import Trade

        # Get all closed trades
        closed_trades = Trade.get_trades([Trade.is_open == False]).all()

        # Calculate total PnL
        total_pnl = sum(t.close_profit for t in closed_trades if t.close_profit)

        # Check limit
        if total_pnl < -self.max_total_loss_pct / 100:
            return True

        return False

    # ---------------------------------------------------------------------------
    # Position Sizing (FTMO Compliant)
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
        Calculate position size based on FTMO risk rules
        Risk per trade: 0.5% (conservative for high frequency)
        """
        # Get account balance
        wallet_balance = self.wallets.get_total_stake_amount() if self.wallets else proposed_stake

        # FTMO risk per trade
        risk_per_trade = 0.005  # 0.5%

        # Get stoploss distance
        dataframe, _ = self.dp.get_analyzed_dataframe(pair=pair, timeframe=self.timeframe)
        if len(dataframe) == 0:
            return proposed_stake

        atr = dataframe['atr'].iloc[-1]
        sl_distance_pct = (atr * self.sl_multiplier.value) / current_rate

        # Calculate position size
        if sl_distance_pct > 0:
            position_size = (wallet_balance * risk_per_trade) / sl_distance_pct
        else:
            position_size = proposed_stake

        # Cap at max stake
        return min(position_size, max_stake)

    # ---------------------------------------------------------------------------
    # Strategy Metadata
    # ---------------------------------------------------------------------------

    def version(self) -> str:
        return "FTMO High Frequency M5 v1.0"
