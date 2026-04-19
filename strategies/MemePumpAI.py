"""
Black Swan Capital — Meme Pump AI Strategy
FreqAI + LightGBM, rolling retrain mỗi 7 ngày, lookback 90 ngày

Architecture:
  - FreqAI train LightGBM predict P(TP hit trước SL trong 32 candles)
  - Target: 1 nếu close tăng 5% trước khi giảm 5% trong 8h tới
  - Entry: AI prob >= 0.60 + ret_2h >= 15% + vol_spike 2x + good hour
  - Leverage: 2x → TP/SL = ±10% of position (= ±5% price move)
"""

import logging
import numpy as np
import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy, DecimalParameter, IntParameter

logger = logging.getLogger(__name__)


class MemePumpAI(IStrategy):
    # ── Basic config ──────────────────────────────────────────────────────────
    INTERFACE_VERSION = 3
    timeframe         = '15m'

    # 2x leverage: TP/SL in % of position
    # price +5% → position profit = 2x × 5% = +10% → hits roi
    # price -5% → position loss   = 2x × 5% = -10% → hits stoploss
    stoploss    = -0.10
    minimal_roi = {"0": 0.10}

    trailing_stop                = False
    use_exit_signal              = True
    exit_profit_only             = False
    can_short                    = False
    process_only_new_candles     = True

    startup_candle_count = 700
    MAX_CANDLES          = 32   # 8h time stop

    GOOD_HOURS = {0, 2, 5, 9, 10, 15, 16, 17, 22}

    # Khai báo FreqAI
    freqai_info: dict = {}

    # ── Hyperopt params ───────────────────────────────────────────────────────
    ai_prob_min   = DecimalParameter(0.55, 0.75, default=0.60, decimals=2,
                                     space='buy', optimize=True)
    ret_2h_min    = DecimalParameter(0.08, 0.25, default=0.15, decimals=2,
                                     space='buy', optimize=True)
    vol_spike_min = DecimalParameter(1.5, 4.0, default=2.0, decimals=1,
                                     space='buy', optimize=True)

    # ── FreqAI: Feature Engineering ──────────────────────────────────────────
    def feature_engineering_expand_all(self, dataframe: DataFrame, period: int,
                                        metadata: dict, **kwargs) -> DataFrame:
        """Features nhân với mỗi period trong indicator_periods_candles."""
        dataframe[f'%-rsi-period_{period}']  = ta.RSI(dataframe, timeperiod=period)
        dataframe[f'%-mom-period_{period}']  = ta.MOM(dataframe, timeperiod=period)
        dataframe[f'%-adx-period_{period}']  = ta.ADX(dataframe, timeperiod=period)
        dataframe[f'%-ret-period_{period}']  = dataframe['close'].pct_change(period)
        vol_ma = dataframe['volume'].rolling(period, min_periods=max(1, period//2)).mean()
        dataframe[f'%-volr-period_{period}'] = (
            dataframe['volume'] / vol_ma.replace(0, np.nan)
        )
        macd = ta.MACD(dataframe, fastperiod=period, slowperiod=period * 2,
                       signalperiod=max(2, period // 3))
        dataframe[f'%-macd-period_{period}'] = macd['macdhist']
        return dataframe

    def feature_engineering_expand_basic(self, dataframe: DataFrame,
                                          metadata: dict, **kwargs) -> DataFrame:
        """Features tính một lần (không nhân period)."""
        # Multi-timeframe returns
        for c in [1, 4, 8, 16, 32, 96]:
            dataframe[f'%-ret_{c}c'] = dataframe['close'].pct_change(c)

        # Volume spike 7d (672 × 15m)
        v672 = dataframe['volume'].rolling(672, min_periods=336).mean()
        dataframe['%-vol_spike_7d'] = dataframe['volume'] / v672.replace(0, np.nan)

        # Bollinger band position [0,1]
        bb    = ta.BBANDS(dataframe, timeperiod=20)
        band  = (bb['upperband'] - bb['lowerband']).replace(0, np.nan)
        dataframe['%-bb_pct'] = (dataframe['close'] - bb['lowerband']) / band

        # Stochastic
        stoch = ta.STOCH(dataframe, fastk_period=14, slowk_period=3, slowd_period=3)
        dataframe['%-stoch_k'] = stoch['slowk']

        # ATR ratio
        atr = ta.ATR(dataframe, timeperiod=14)
        dataframe['%-atr_ratio'] = atr / atr.rolling(96).mean().replace(0, np.nan)

        # Candle direction
        hl = (dataframe['high'] - dataframe['low']).replace(0, np.nan)
        dataframe['%-body_ratio'] = (
            (dataframe['close'] - dataframe['open']).abs() / hl
        )
        dataframe['%-bull_body'] = (
            (dataframe['close'] - dataframe['open']) / hl
        )

        # Gain from 8h low / 24h low
        dataframe['%-gain_low_8h']  = (
            (dataframe['close'] - dataframe['low'].rolling(32).min()) /
            dataframe['low'].rolling(32).min().replace(0, np.nan)
        )
        dataframe['%-gain_low_24h'] = (
            (dataframe['close'] - dataframe['low'].rolling(96).min()) /
            dataframe['low'].rolling(96).min().replace(0, np.nan)
        )

        # Time features (operator patterns are time-dependent)
        dataframe['%-hour']    = dataframe['date'].dt.hour
        dataframe['%-dow']     = dataframe['date'].dt.dayofweek
        dataframe['%-is_asia'] = ((dataframe['date'].dt.hour >= 1) &
                                   (dataframe['date'].dt.hour <= 8)).astype(float)
        dataframe['%-is_us']   = ((dataframe['date'].dt.hour >= 14) &
                                   (dataframe['date'].dt.hour <= 22)).astype(float)

        return dataframe

    def feature_engineering_standard(self, dataframe: DataFrame,
                                      metadata: dict, **kwargs) -> DataFrame:
        """Không override — target được set trong set_freqai_targets."""
        return dataframe

    def set_freqai_targets(self, dataframe: DataFrame,
                           metadata: dict, **kwargs) -> DataFrame:
        """
        Target: R:R 1:1 binary label
        1 = price tăng 5% trước khi giảm 5% trong 32 candles tới
        """
        closes = dataframe['close'].values
        highs  = dataframe['high'].values
        lows   = dataframe['low'].values
        n      = len(closes)
        labels = np.full(n, np.nan)

        TP = 0.05
        SL = 0.05
        MB = 32

        for i in range(n - MB):
            entry = closes[i]
            if entry <= 0:
                continue
            tp_price = entry * (1 + TP)
            sl_price = entry * (1 - SL)
            label    = np.nan
            for j in range(1, MB + 1):
                if highs[i + j] >= tp_price:
                    label = 1.0
                    break
                if lows[i + j] <= sl_price:
                    label = 0.0
                    break
            else:
                label = 1.0 if closes[i + MB] > entry else 0.0
            labels[i] = label

        dataframe['&-label_rr'] = labels
        return dataframe

    # ── Indicators (non-AI) + FreqAI inference ───────────────────────────────
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Extra indicators for entry filter (available after freqai.start())
        vol_ma_672 = dataframe['volume'].rolling(672, min_periods=336).mean()
        dataframe['vol_spike']  = dataframe['volume'] / vol_ma_672.replace(0, np.nan)
        dataframe['ret_2h']     = dataframe['close'] / dataframe['close'].shift(8) - 1
        dataframe['hour_utc']   = dataframe['date'].dt.hour

        # FreqAI: train model & run inference — adds &-label_rr column
        dataframe = self.freqai.start(dataframe, metadata, self)

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # FreqAI classifier output: &-label_rr (predicted class probability)
        # For binary classifier: FreqAI outputs the predicted class (0 or 1)
        # Use &-label_rr directly as signal, combine with do_predict quality gate
        dataframe.loc[
            (
                # AI model says BUY (predicted prob via &-label_rr)
                (dataframe['&-label_rr'] == 1) &
                # AI confidence gate
                (dataframe['do_predict'] == 1) &
                # Pre-filter: only in pump momentum conditions
                (dataframe['ret_2h'] >= self.ret_2h_min.value) &
                (dataframe['vol_spike'] >= self.vol_spike_min.value) &
                # Session filter (statistical edge by hour)
                (dataframe['hour_utc'].isin(self.GOOD_HOURS)) &
                (dataframe['volume'] > 0)
            ),
            ['enter_long', 'enter_tag']
        ] = (1, 'ai_pump')

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, 'exit_long'] = 0
        return dataframe

    def custom_exit(self, pair, trade, current_time, current_rate,
                    current_profit, **kwargs):
        """Time stop: exit sau 8h nếu TP/SL chưa trigger."""
        candles_held = int(
            (current_time - trade.open_date_utc).total_seconds() / 900
        )
        if candles_held >= self.MAX_CANDLES:
            return 'time_stop_8h'
        return None

    def confirm_trade_entry(self, pair, order_type, amount, rate,
                             time_in_force, current_time, entry_tag,
                             side, **kwargs):
        if current_time.hour not in self.GOOD_HOURS:
            return False
        return True

    def leverage(self, pair, current_time, current_rate, proposed_leverage,
                 max_leverage, entry_tag, side, **kwargs):
        """2x leverage."""
        return min(2.0, max_leverage)
