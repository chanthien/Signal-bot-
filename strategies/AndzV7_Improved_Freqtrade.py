
import numpy as np
import pandas as pd
from pandas import DataFrame
from typing import Optional
from freqtrade.strategy import IStrategy, DecimalParameter, IntParameter
import talib.abstract as ta
from datetime import datetime, timedelta
import freqtrade.vendor.qtpylib.indicators as qtpylib

class AndzV7_Improved_Freqtrade(IStrategy):
    INTERFACE_VERSION = 3
    
    # Strategy parameters
    timeframe = '15m'
    can_short = False  # Disable shorting for spot mode
    
    # Minimal ROI designed to be turned off in favor of dynamic exits
    minimal_roi = {"0": 100}
    
    # Stoploss - will be overridden by custom_stoploss
    stoploss = -0.99
    
    # Trailing stop
    trailing_stop = False
    
    # Hyperopt parameters
    ema_fast = IntParameter(40, 60, default=50, space='buy')
    ema_slow = IntParameter(80, 120, default=100, space='buy')
    adx_threshold = DecimalParameter(15.0, 30.0, default=20.0, space='buy')
    volume_multiplier = DecimalParameter(1.2, 2.0, default=1.5, space='buy')
    rsi_lower = DecimalParameter(40, 50, default=45, space='buy')
    rsi_upper = DecimalParameter(65, 75, default=70, space='buy')
    rsi_short_lower = DecimalParameter(25, 35, default=30, space='sell')
    rsi_short_upper = DecimalParameter(50, 60, default=55, space='sell')
    
    # Exit parameters
    tp1_ratio = DecimalParameter(0.8, 1.2, default=1.0, space='sell')
    tp2_ratio = DecimalParameter(1.8, 2.5, default=2.0, space='sell')
    trailing_activation = DecimalParameter(2.0, 3.0, default=2.5, space='sell')
    trailing_distance = DecimalParameter(1.2, 2.0, default=1.5, space='sell')
    
    # Stop loss parameters
    sl_multiplier_strong = DecimalParameter(2.0, 3.0, default=2.5, space='sell')
    sl_multiplier_moderate = DecimalParameter(1.5, 2.5, default=2.0, space='sell')
    sl_multiplier_weak = DecimalParameter(1.0, 2.0, default=1.5, space='sell')
    
    # Position sizing parameters
    base_risk = DecimalParameter(0.01, 0.03, default=0.02, space='buy')
    max_position = DecimalParameter(0.03, 0.08, default=0.05, space='buy')
    
    # Time limits (in minutes)
    max_losing_hold = IntParameter(60, 180, default=120, space='sell')
    max_total_hold = IntParameter(180, 300, default=240, space='sell')
    
    # Trade management
    position_adjustment_enable = True
    max_entry_position_adjustment = 2
    
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # EMAs
        dataframe['ema50'] = ta.EMA(dataframe, timeperiod=self.ema_fast.value)
        dataframe['ema100'] = ta.EMA(dataframe, timeperiod=self.ema_slow.value)
        
        # ADX system
        dataframe['adx'] = ta.ADX(dataframe, timeperiod=14)
        
        # RSI
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)
        
        # ATR for dynamic stops
        dataframe['atr'] = ta.ATR(dataframe, timeperiod=14)
        dataframe['atr_mean'] = dataframe['atr'].rolling(window=50).mean()
        
        # Volume analysis
        dataframe['volume_sma'] = ta.SMA(dataframe['volume'], timeperiod=20)
        
        # EMA cross signals
        dataframe['ema_cross_up'] = qtpylib.crossed_above(dataframe['ema50'], dataframe['ema100'])
        dataframe['ema_cross_down'] = qtpylib.crossed_below(dataframe['ema50'], dataframe['ema100'])
        
        # Volume spike
        dataframe['volume_spike'] = dataframe['volume'] > (dataframe['volume_sma'] * self.volume_multiplier.value)
        
        # Market hours filter (UTC)
        dataframe['hour'] = pd.to_datetime(dataframe['date']).dt.hour
        dataframe['active_hours'] = (dataframe['hour'] >= 8) & (dataframe['hour'] <= 22)
        
        # Price position relative to EMA
        dataframe['price_above_ema'] = dataframe['close'] > (dataframe['ema50'] * 1.001)
        dataframe['price_below_ema'] = dataframe['close'] < (dataframe['ema50'] * 0.999)
        
        # Volatility filter
        dataframe['volatility_sufficient'] = dataframe['atr'] > (dataframe['atr_mean'] * 0.8)
        
        return dataframe
    
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Long conditions
        dataframe.loc[
            (
                dataframe['ema_cross_up'] &
                (dataframe['adx'] > self.adx_threshold.value) &
                dataframe['volume_spike'] &
                (dataframe['rsi'] > self.rsi_lower.value) &
                (dataframe['rsi'] < self.rsi_upper.value) &
                dataframe['active_hours'] &
                dataframe['price_above_ema'] &
                dataframe['volatility_sufficient']
            ),
            ['enter_long', 'enter_tag']
        ] = (1, 'ema_cross_long')
        
        # Short conditions
        dataframe.loc[
            (
                dataframe['ema_cross_down'] &
                (dataframe['adx'] > self.adx_threshold.value) &
                dataframe['volume_spike'] &
                (dataframe['rsi'] < self.rsi_short_upper.value) &
                (dataframe['rsi'] > self.rsi_short_lower.value) &
                dataframe['active_hours'] &
                dataframe['price_below_ema'] &
                dataframe['volatility_sufficient']
            ),
            ['enter_short', 'enter_tag']
        ] = (1, 'ema_cross_short')
        
        return dataframe
    
    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Momentum reversal exits
        dataframe.loc[
            (
                (dataframe['ema50'] < dataframe['ema100']) &
                (dataframe['adx'] < 15)
            ),
            ['exit_long', 'exit_tag']
        ] = (1, 'momentum_reversal')
        
        dataframe.loc[
            (
                (dataframe['ema50'] > dataframe['ema100']) &
                (dataframe['adx'] < 15)
            ),
            ['exit_short', 'exit_tag']
        ] = (1, 'momentum_reversal')
        
        return dataframe
    
    def custom_stoploss(self, pair: str, trade: 'Trade', current_time: datetime,
                       current_rate: float, current_profit: float, **kwargs) -> float:
        
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty:
            return self.stoploss
        
        current_candle = dataframe.iloc[-1]
        
        # Dynamic stop based on ADX regime
        if current_candle['adx'] > 25:
            sl_multiplier = self.sl_multiplier_strong.value
        elif current_candle['adx'] > 20:
            sl_multiplier = self.sl_multiplier_moderate.value
        else:
            sl_multiplier = self.sl_multiplier_weak.value
        
        # Calculate stop distance
        atr_value = current_candle['atr']
        stop_distance = atr_value * sl_multiplier
        
        # Convert to percentage
        if trade.is_short:
            stoploss_value = stop_distance / current_rate
        else:
            stoploss_value = -stop_distance / current_rate
        
        # Time-based exit logic
        trade_duration = (current_time - trade.open_date_utc).total_seconds() / 60  # minutes
        
        # Exit losing trades after max_losing_hold minutes
        if current_profit < 0 and trade_duration > self.max_losing_hold.value:
            return 0.001 if trade.is_short else -0.001
        
        # Exit all trades after max_total_hold minutes
        if trade_duration > self.max_total_hold.value:
            return 0.001 if trade.is_short else -0.001
        
        # Trailing stop logic
        if current_profit > (self.trailing_activation.value / 100):
            trailing_stop_distance = (atr_value * self.trailing_distance.value) / current_rate
            if trade.is_short:
                return trailing_stop_distance
            else:
                return -trailing_stop_distance
        
        return stoploss_value
    
    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float,
                           proposed_stake: float, min_stake: Optional[float], max_stake: float,
                           leverage: float, entry_tag: Optional[str], side: str, **kwargs) -> float:
        
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty:
            return proposed_stake
        
        current_candle = dataframe.iloc[-1]
        
        # Calculate position size based on risk
        atr_value = current_candle['atr']
        
        # Dynamic risk multiplier based on ADX
        if current_candle['adx'] > 30:
            risk_multiplier = 1.2
        elif current_candle['adx'] > 25:
            risk_multiplier = 1.0
        else:
            risk_multiplier = 0.8
        
        # Stop loss distance
        if current_candle['adx'] > 25:
            sl_multiplier = self.sl_multiplier_strong.value
        elif current_candle['adx'] > 20:
            sl_multiplier = self.sl_multiplier_moderate.value
        else:
            sl_multiplier = self.sl_multiplier_weak.value
        
        stop_distance = atr_value * sl_multiplier
        stop_percentage = stop_distance / current_rate
        
        # Calculate stake amount
        account_balance = self.wallets.get_total_stake_amount()
        risk_amount = account_balance * self.base_risk.value * risk_multiplier
        
        # Position size based on risk
        if stop_percentage > 0:
            position_value = risk_amount / stop_percentage
        else:
            position_value = proposed_stake
        
        # Apply maximum position limit
        max_position_value = account_balance * self.max_position.value
        final_stake = min(position_value, max_position_value)
        
        # Ensure within broker limits
        final_stake = max(final_stake, min_stake or 0)
        final_stake = min(final_stake, max_stake)
        
        return final_stake
    
    def custom_exit(self, pair: str, trade: 'Trade', current_time: datetime, current_rate: float,
                   current_profit: float, **kwargs) -> Optional[str]:
        
        # Partial take profit logic
        if current_profit > (self.tp1_ratio.value / 100) and trade.amount == trade.amount:
            return f"tp1_{self.tp1_ratio.value}%"
        
        if current_profit > (self.tp2_ratio.value / 100):
            return f"tp2_{self.tp2_ratio.value}%"
        
        return None
    
    def adjust_trade_position(self, trade: 'Trade', current_time: datetime,
                             current_rate: float, current_profit: float,
                             min_stake: Optional[float], max_stake: float,
                             current_entry_rate: float, current_exit_rate: float,
                             current_entry_profit: float, current_exit_profit: float,
                             **kwargs) -> Optional[float]:
        
        # Partial take profit implementation
        if current_profit > (self.tp1_ratio.value / 100) and trade.nr_of_successful_exits == 0:
            # Take 50% profit at first TP
            return -(trade.stake_amount * 0.5)
        
        if current_profit > (self.tp2_ratio.value / 100) and trade.nr_of_successful_exits == 1:
            # Take 30% of remaining position at second TP
            remaining_stake = trade.stake_amount - (trade.stake_amount * 0.5)
            return -(remaining_stake * 0.3)
        
        return None
    
    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float,
                           time_in_force: str, current_time: datetime, entry_tag: Optional[str],
                           side: str, **kwargs) -> bool:
        
        # Additional safety checks
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty:
            return False
        
        current_candle = dataframe.iloc[-1]
        
        # Ensure we have sufficient volatility
        if current_candle['atr'] <= 0:
            return False
        
        # Ensure ADX is valid
        if current_candle['adx'] <= 0:
            return False
        
        return True
