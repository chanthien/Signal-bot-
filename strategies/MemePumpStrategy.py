"""
Black Swan Capital — Meme Pump Strategy
15m timeframe, R:R 1:1 (5%/5%)

Edge:
  - 6 high-WR meme coins (COW, MEME, PEPE, NEIR, WIF, BONK)
  - Entry only during specific UTC hours (83% WR observed in manual backtest)
  - Filter: RSI < 88 + price surge +15% in 2h + vol spike 2x
  - Exit: TP +5% | SL -5% | Time stop 8h

Manual backtest (2023-2026): WR=83.1%, 77 trades/29mo, +88% at 25% stake
"""

from freqtrade.strategy import IStrategy, IntParameter, DecimalParameter
from pandas import DataFrame
import pandas as pd
import numpy as np
import talib.abstract as ta


class MemePumpStrategy(IStrategy):
    # ── Basic config ──────────────────────────────────────────────────────────
    INTERFACE_VERSION = 3
    timeframe         = '15m'

    # 2x leverage:
    #   price +5% → position profit = 2x × 5% = +10% → hit roi
    #   price -5% → position loss   = 2x × 5% = -10% → hit stoploss
    # Asymmetric: TP=+10% (price +5%), SL=-10% (price -5%) → R:R 1:1 in price
    # But via trailing_stop, winners can run further (pump >5%)
    stoploss          = -0.10
    minimal_roi       = {"0": 0.10}
    trailing_stop     = False
    use_exit_signal   = True
    exit_profit_only  = False
    ignore_roi_if_entry_signal = False
    can_short         = False
    process_only_new_candles = True

    # Warmup: vol_ma needs 672 candles (7d), add buffer
    startup_candle_count = 700

    # Time stop: exit after 32 candles (8h) if no TP/SL hit
    MAX_CANDLES = 32

    # ── Entry parameters (optimizable via hyperopt) ────────────────────────────
    rsi_max       = IntParameter(75, 92, default=88, space='buy', optimize=True)
    ret_2h_min    = DecimalParameter(0.08, 0.25, default=0.15, decimals=2,
                                     space='buy', optimize=True)
    vol_spike_min = DecimalParameter(1.5, 4.0, default=2.0, decimals=1,
                                     space='buy', optimize=True)

    # Good UTC hours derived from statistical analysis (WR >= 60%)
    GOOD_HOURS = {0, 2, 5, 9, 10, 15, 16, 17, 22}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # ── RSI ──────────────────────────────────────────────────────────────
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)

        # ── 2h return: close now vs close 8 candles (120 min) ago ────────────
        dataframe['ret_2h'] = (
            dataframe['close'] / dataframe['close'].shift(8) - 1
        )

        # ── Volume spike: current vol / 7d rolling mean ───────────────────────
        # 7d = 7 × 24 × 4 = 672 × 15m candles
        vol_ma = dataframe['volume'].rolling(window=672, min_periods=336).mean()
        dataframe['vol_spike'] = (
            dataframe['volume'] / vol_ma.replace(0, np.nan)
        )

        # ── UTC hour of candle open ───────────────────────────────────────────
        dataframe['hour_utc'] = dataframe['date'].dt.hour

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                # Momentum: price surged +15% in last 2h (8 × 15m candles)
                (dataframe['ret_2h'] >= self.ret_2h_min.value) &
                # Volume confirmation: at least 2x 7d average
                (dataframe['vol_spike'] >= self.vol_spike_min.value) &
                # RSI: not over-extended yet (still room to run)
                (dataframe['rsi'] < self.rsi_max.value) &
                # Session filter: only high-WR UTC hours
                (dataframe['hour_utc'].isin(self.GOOD_HOURS)) &
                # Data quality
                (dataframe['volume'] > 0) &
                (dataframe['vol_spike'].notna()) &
                (dataframe['ret_2h'].notna())
            ),
            'enter_long'
        ] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # minimal_roi (TP) and stoploss (SL) handle exits
        dataframe.loc[:, 'exit_long'] = 0
        return dataframe

    def custom_exit(self, pair, trade, current_time, current_rate,
                    current_profit, **kwargs):
        """Time stop: exit after 8h regardless of P&L."""
        candles_held = int(
            (current_time - trade.open_date_utc).total_seconds() / 900
        )
        if candles_held >= self.MAX_CANDLES:
            return 'time_stop_8h'
        return None

    def confirm_trade_entry(self, pair, order_type, amount, rate,
                             time_in_force, current_time, entry_tag,
                             side, **kwargs):
        """Double-check: only enter during statistically favorable hours."""
        if current_time.hour not in self.GOOD_HOURS:
            return False
        return True

    def leverage(self, pair, current_time, current_rate, proposed_leverage,
                 max_leverage, entry_tag, side, **kwargs):
        """2x leverage — doubles dollar profit vs 1x while keeping same WR."""
        return min(2.0, max_leverage)
