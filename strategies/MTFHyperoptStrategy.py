"""
MTF Strategy with Hyperopt Support
Optimize: Stoploss, ROI, and entry filters
"""

import talib.abstract as ta
from freqtrade.strategy import IStrategy, DecimalParameter, IntParameter
from pandas import DataFrame
import numpy as np


class MTFHyperoptStrategy(IStrategy):
    """
    Multi-Timeframe Strategy with Hyperoptable Parameters
    
    Hyperopt this to find optimal:
    - Stoploss
    - ROI table
    - Volume multiplier
    - ADX threshold
    """
    
    timeframe = '15m'
    
    # =====================================================================
    # HYPEROPTABLE PARAMETERS
    # =====================================================================
    
    # Stoploss: Search between -0.5% and -3%
    stoploss = DecimalParameter(-0.03, -0.005, default=-0.01, space='sell', optimize=True)
    
    # ROI: Search for optimal takeprofit levels
    # We'll optimize 3 ROI levels
    roi_1 = DecimalParameter(0.02, 0.10, default=0.06, space='sell', optimize=True)
    roi_2 = DecimalParameter(0.06, 0.15, default=0.10, space='sell', optimize=True)
    
    # Volume multiplier for entry filter
    volume_mult = DecimalParameter(1.0, 2.5, default=1.5, space='buy', optimize=True)
    
    # ADX threshold for trend strength
    adx_threshold = IntParameter(15, 30, default=20, space='buy', optimize=True)
    
    # Minimal ROI (will be overridden by roi_1, roi_2)
    minimal_roi = {
        "0": 0.06,
        "180": 0.10,
        "360": 1.0
    }
    
    # Position sizing
    stake_amount = 50
    max_open_trades = 3
    
    # Trailing stop (can also be hyperopted)
    trailing_stop = True
    trailing_stop_positive = 0.02
    trailing_stop_positive_offset = 0.06
    trailing_only_offset_is_reached = True
    
    use_custom_stoploss = False
    
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Calculate indicators"""
        
        # Trend
        dataframe['ema50'] = ta.EMA(dataframe, timeperiod=50)
        dataframe['ema200'] = ta.EMA(dataframe, timeperiod=200)
        
        # Volatility
        dataframe['atr'] = ta.ATR(dataframe, timeperiod=14)
        
        # Volume
        dataframe['volume_sma'] = ta.SMA(dataframe['volume'], timeperiod=20)
        dataframe['volume_ratio'] = dataframe['volume'] / dataframe['volume_sma']
        
        # Trend strength
        dataframe['adx'] = ta.ADX(dataframe, timeperiod=14)
        
        return dataframe
    
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Entry with hyperoptable filters
        """
        
        # Get hyperoptable values
        volume_mult = self.volume_mult.value
        adx_threshold = self.adx_threshold.value
        
        # Trend alignment
        dataframe['trend_aligned'] = (
            (dataframe['ema50'] > dataframe['ema200']) &
            (dataframe['close'] > dataframe['ema50'])
        ).astype(int)
        
        # Volume filter (hyperoptable)
        dataframe['volume_confirmed'] = (
            dataframe['volume_ratio'] > volume_mult
        ).astype(int)
        
        # ADX filter (hyperoptable)
        dataframe['strong_trend'] = (
            dataframe['adx'] > adx_threshold
        ).astype(int)
        
        # Conviction score
        dataframe['conviction'] = (
            dataframe['trend_aligned'] +
            dataframe['volume_confirmed'] +
            dataframe['strong_trend']
        )
        
        # Entry: conviction >= 2
        dataframe['enter_long'] = (
            dataframe['conviction'] >= 2
        ).astype(int)
        
        return dataframe
    
    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Exit with hyperoptable ROI levels
        """
        
        # Get hyperoptable values - use .value for DecimalParameter
        roi_1 = self.roi_1.value
        roi_2 = self.roi_2.value
        stoploss_val = self.stoploss.value  # This is already a float in hyperopt
        
        # TP1 (hyperoptable)
        dataframe['tp1_hit'] = (
            (dataframe['high'] >= dataframe['close'].shift(1) * (1 + roi_1)) &
            (dataframe['enter_long'].shift(1) == 1)
        ).astype(int)
        
        # TP2 (hyperoptable)
        dataframe['tp2_hit'] = (
            (dataframe['high'] >= dataframe['close'].shift(1) * (1 + roi_2)) &
            (dataframe['enter_long'].shift(1) == 1)
        ).astype(int)
        
        # Stoploss (hyperoptable) - ensure it's negative
        sl_multiplier = 1 + stoploss_val if isinstance(stoploss_val, (int, float)) else 1 + stoploss_val.value
        dataframe['sl_hit'] = (
            (dataframe['low'] <= dataframe['close'].shift(1) * sl_multiplier) &
            (dataframe['enter_long'].shift(1) == 1)
        ).astype(int)
        
        # Exit on any
        dataframe['exit_long'] = (
            dataframe['tp1_hit'] |
            dataframe['tp2_hit'] |
            dataframe['sl_hit']
        ).astype(int)
        
        return dataframe


# =====================================================================
# HYPEROPT LOSS FUNCTION - FOCUS ON ROI + LOW DD
# =====================================================================

from freqtrade.optimize.hyperopt_loss import HyperoptLoss
import numpy as np


class HyperoptLossROI(HyperoptLoss):
    """
    Custom loss function focusing on:
    1. Maximize ROI
    2. Minimize Max Drawdown
    3. Penalize low trade count
    """
    
    @staticmethod
    def hyperopt_loss_function(
        results: dict,
        trade_count: int,
        *args,
        **kwargs
    ) -> float:
        """
        Calculate loss (lower = better)
        
        We want to:
        - Maximize ROI (so minimize -ROI)
        - Minimize Max DD
        - Ensure minimum trades
        """
        
        # Extract metrics
        total_profit = results['profit_total']
        max_drawdown = results.get('max_drawdown', 0)
        trade_count = results['trade_count']
        
        # ROI component (we want high ROI, so minimize negative)
        roi_component = -total_profit * 100  # Scale up
        
        # Drawdown penalty (we want low DD)
        dd_penalty = max_drawdown * 50  # Weight DD heavily
        
        # Trade count penalty (ensure minimum 100 trades)
        if trade_count < 100:
            trade_penalty = (100 - trade_count) * 10
        else:
            trade_penalty = 0
        
        # Total loss (lower = better)
        loss = roi_component + dd_penalty + trade_penalty
        
        return loss
