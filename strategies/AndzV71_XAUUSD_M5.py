"""
AndzV7.1 Optimized Strategy - XAU/USD M5
Based on: AndzV7.1_Optimized_Blueprint.md
Adapted for: XAU/USD (Gold) 5-minute timeframe
Direction: Short-biased momentum exhaustion strategy
"""

import pandas as pd
import numpy as np
from freqtrade.strategy import IStrategy, DecimalParameter
from pandas import DataFrame
import talib.abstract as ta
from datetime import timedelta, datetime
from freqtrade.persistence import Trade

class AndzV71_XAUUSD_M5(IStrategy):
    """
    AndzV7.1 Optimized for XAU/USD M5
    - EMA crossover + Volume + ADX filter
    - Tighter stoploss (1.5x ATR)
    - Daily stoploss circuit breaker (-8%)
    - Partial take profit system
    """
    
    # Strategy metadata
    timeframe = '5m'
    minimal_roi = {
        "0": 100  # Use custom stoploss instead
    }
    stoploss = -0.06  # Max 6% stoploss
    
    # Position sizing
    stake_amount = 50  # USDT per trade
    max_open_trades = 5
    
    # Risk parameters
    base_risk_per_trade = 0.015  # 1.5%
    max_position_size = 0.04  # 4%
    
    # Daily stoploss
    daily_stoploss = -0.08  # -8% daily circuit breaker
    
    # Trailing stop
    trailing_stop = True
    trailing_stop_positive = 0.015  # 1.5%
    trailing_stop_positive_offset = 0.025  # 2.5%
    trailing_only_offset_is_reached = False
    
    # Order types
    order_types = {
        'entry': 'limit',
        'exit': 'limit',
        'stoploss': 'market',
        'stoploss_on_exchange': False
    }
    
    # Optional order time in force
    order_time_in_force = {
        'entry': 'GTC',
        'exit': 'GTC'
    }
    
    # Performance tracking
    daily_start_equity = None
    daily_pnl = 0.0
    trades_today = 0
    max_trades_per_day = 8
    
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Calculate required indicators"""
        
        # EMAs
        dataframe['ema50'] = ta.EMA(dataframe, timeperiod=50)
        dataframe['ema100'] = ta.EMA(dataframe, timeperiod=100)
        dataframe['ema200'] = ta.EMA(dataframe, timeperiod=200)
        
        # ADX/DI System
        dataframe['adx'] = ta.ADX(dataframe, timeperiod=14)
        dataframe['plus_di'] = ta.PLUS_DI(dataframe, timeperiod=14)
        dataframe['minus_di'] = ta.MINUS_DI(dataframe, timeperiod=14)
        
        # RSI
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)
        
        # ATR for stops (XAU/USD has higher volatility)
        dataframe['atr'] = ta.ATR(dataframe, timeperiod=14)
        
        # Volume analysis
        dataframe['volume_sma'] = ta.SMA(dataframe['volume'], timeperiod=20)
        dataframe['volume_ratio'] = dataframe['volume'] / dataframe['volume_sma']
        
        # Time features
        dataframe['hour'] = dataframe['date'].dt.hour
        dataframe['dayofweek'] = dataframe['date'].dt.dayofweek
        dataframe['day'] = dataframe['date'].dt.day
        
        # ATR ratio for volatility filter
        dataframe['atr_ratio'] = dataframe['atr'] / dataframe['atr'].rolling(50).mean()
        
        return dataframe
    
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Generate entry signals"""
        
        # Market conditions filter
        adx_trending = dataframe['adx'] >= 15
        volume_confirmation = dataframe['volume'] > dataframe['volume_sma'] * 1.3
        volatility_ok = dataframe['atr_ratio'] > 0.8
        
        # Active hours (XAU/USD: London + NY session, 8:00-22:00 UTC)
        active_hours = (dataframe['hour'] >= 8) & (dataframe['hour'] <= 22)
        
        # Weekend filter (avoid low liquidity)
        not_weekend = (dataframe['dayofweek'] < 5) | (
            (dataframe['dayofweek'] == 5) & (dataframe['hour'] >= 8) & (dataframe['hour'] <= 22)
        )
        
        # === LONG ENTRY (Only in STRONG uptrend) ===
        ema_bullish_cross = (
            (dataframe['ema50'] > dataframe['ema100']) &
            (dataframe['ema50'].shift(1) <= dataframe['ema100'].shift(1))
        )
        
        # Strong uptrend filter
        strong_uptrend = (
            (dataframe['ema50'] > dataframe['ema100']) &
            (dataframe['ema50'] > dataframe['ema200']) &
            (dataframe['close'] > dataframe['ema200'])
        )
        
        # Momentum filters for long
        adx_filter = dataframe['adx'] >= 15
        volume_spike = dataframe['volume'] > dataframe['volume_sma'] * 1.3
        rsi_filter_long = (dataframe['rsi'] > 50) & (dataframe['rsi'] < 70)
        price_above_ema = dataframe['close'] > dataframe['ema50'] * 1.001
        
        long_conditions = (
            ema_bullish_cross &
            adx_filter &
            volume_spike &
            rsi_filter_long &
            strong_uptrend &
            price_above_ema &
            active_hours &
            not_weekend &
            adx_trending &
            volume_confirmation &
            volatility_ok
        )
        
        # === SHORT ENTRY (PRIMARY DIRECTION) ===
        ema_bearish_cross = (
            (dataframe['ema50'] < dataframe['ema100']) &
            (dataframe['ema50'].shift(1) >= dataframe['ema100'].shift(1))
        )
        
        # Momentum filters for short
        rsi_filter_short = (dataframe['rsi'] < 50) & (dataframe['rsi'] > 30)
        price_below_ema = dataframe['close'] < dataframe['ema50'] * 0.999
        
        short_conditions = (
            ema_bearish_cross &
            adx_filter &
            volume_spike &
            rsi_filter_short &
            price_below_ema &
            active_hours &
            not_weekend &
            adx_trending &
            volume_confirmation &
            volatility_ok
        )
        
        dataframe.loc[long_conditions, 'enter_long'] = 1
        dataframe.loc[short_conditions, 'enter_short'] = 1
        dataframe.loc[long_conditions, 'enter_tag'] = 'ema_cross_long'
        dataframe.loc[short_conditions, 'enter_tag'] = 'ema_cross_short'
        
        return dataframe
    
    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Generate exit signals"""
        
        # Momentum reversal exits
        momentum_reversal_long = (
            (dataframe['ema50'] < dataframe['ema100']) &
            (dataframe['adx'] < 15)
        )
        
        momentum_reversal_short = (
            (dataframe['ema50'] > dataframe['ema100']) &
            (dataframe['adx'] < 15)
        )
        
        dataframe.loc[momentum_reversal_long, 'exit_long'] = 1
        dataframe.loc[momentum_reversal_short, 'exit_short'] = 1
        
        return dataframe
    
    def custom_stoploss(self, pair: str, trade: 'Trade', current_time: datetime,
                        current_rate: float, current_profit: float, **kwargs) -> float:
        """Dynamic stoploss based on ADX and profit"""
        
        # Get current candle data
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        current_candle = dataframe.iloc[-1]
        
        if dataframe.empty:
            return -0.06
        
        adx = current_candle['adx']
        atr = current_candle['atr']
        
        # Dynamic stoploss based on ADX strength
        if adx > 25:  # Strong trend
            sl_multiplier = 1.8
        elif adx > 20:  # Moderate trend
            sl_multiplier = 1.5
        else:  # Weak trend (15-20)
            sl_multiplier = 1.3
        
        # Calculate stoploss distance
        stop_loss_distance = (atr * sl_multiplier) / current_rate
        
        # For profitable trades, use tighter trailing
        if current_profit > 0.025:  # After 2.5% profit
            return -0.015  # Trail at 1.5%
        elif current_profit > 0.01:  # After 1% profit
            return -0.005  # Trail at 0.5%
        
        return -stop_loss_distance
    
    def check_daily_stoploss(self, current_time: datetime) -> bool:
        """Check if daily stoploss has been hit"""
        
        # Reset at start of new day
        if self.daily_start_equity is None or current_time.day != getattr(self, '_last_day', None):
            self.daily_start_equity = self.wallets.get_total_stake_amount() if self.wallets else 1000
            self.daily_pnl = 0.0
            self.trades_today = 0
            self._last_day = current_time.day
        
        # Calculate daily PnL
        current_equity = self.wallets.get_total_stake_amount() if self.wallets else 1000
        self.daily_pnl = (current_equity - self.daily_start_equity) / self.daily_start_equity
        
        # Check daily stoploss
        if self.daily_pnl <= self.daily_stoploss:
            return False  # Stop trading
        
        # Check max trades per day
        if self.trades_today >= self.max_trades_per_day:
            return False  # Stop trading
        
        return True  # Allow trading
    
    def should_enter(self, pair: str, signal: str) -> bool:
        """Final check before entering"""
        return self.check_daily_stoploss(datetime.now())
    
    def custom_exit(self, pair: str, trade: 'Trade', current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs):
        """Custom exit logic including time-based exits"""
        
        # Time-based exit (max 3 hours for XAU/USD M5)
        trade_duration = current_time - trade.open_date_utc
        max_hold = timedelta(hours=3)
        max_losing_hold = timedelta(hours=1.5)
        
        # Force exit after max hold time
        if trade_duration >= max_hold:
            return f"time_exit_{int(trade_duration.total_seconds() / 60)}m"
        
        # Exit losing trades faster
        if trade_duration >= max_losing_hold and current_profit < 0:
            return f"time_exit_loss_{int(trade_duration.total_seconds() / 60)}m"
        
        return None
    
    def bot_loop_start(self, **kwargs) -> None:
        """Called at start of each bot loop"""
        pass
    
    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float,
                            time_in_force: str, current_time: datetime, entry_tag,
                            side: str, **kwargs) -> bool:
        """Confirm trade entry with daily stoploss check"""
        return self.should_enter(pair, entry_tag)
    
    def confirm_trade_exit(self, pair: str, trade: 'Trade', order_type: str, amount: float,
                           rate: float, time_in_force: str, exit_reason: str,
                           current_time: datetime, **kwargs) -> bool:
        """Confirm trade exit"""
        if exit_reason.startswith('time_exit'):
            return True
        return True
    
    def informative_pairs(self):
        """Define informative pairs if needed"""
        return []
