"""
scheduler/engine.py — Main trading engine
Chạy loop mỗi H1 close:
1. Fetch OHLCV từ BingX public API
2. Chạy strategy cho từng asset
3. Execute lệnh nếu có signal
4. Gửi Telegram notification
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional

from config.settings import ASSETS, AssetConfig, CANDLE_INTERVAL, CANDLE_LIMIT, EXECUTION_ENABLED, MEME_ASSETS
from execution.gateway import gateway
from notifier.telegram import notifier
from strategy.grid_pyramid import GridPyramidStrategy, Signal, AssetState
from utils.logger import log


class TradingEngine:

    def __init__(self):
        # Khởi tạo strategy instance cho từng asset
        self.strategies: dict[str, GridPyramidStrategy] = {
            symbol: GridPyramidStrategy(cfg)
            for symbol, cfg in ASSETS.items()
        }
        # Track daily stats
        self.daily_stats: dict[str, dict] = {
            symbol: {"trades": 0, "wins": 0, "total_pnl": 0.0}
            for symbol in ASSETS
        }
        self._running = False
        self.execution_enabled = EXECUTION_ENABLED

    # ── Main loop ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        log.info("engine.started", assets=list(ASSETS.keys()), execution_enabled=self.execution_enabled)
        await notifier.bot_started(list(ASSETS.keys()))

        # Chạy ngay lần đầu, sau đó đợi mỗi phút
        await self._run_all()

        while self._running:
            wait = self._seconds_to_next_minute()
            log.info("engine.waiting", seconds=wait,
                     next_run=self._next_minute_str())
            await asyncio.sleep(wait + 1)   # +1s buffer
            if self._running:
                await self._run_all()

    async def stop(self) -> None:
        self._running = False
        self.execution_enabled = EXECUTION_ENABLED
        log.info("engine.stopped")

    # ── Per-bar execution ─────────────────────────────────────────────────

    async def _run_all(self) -> None:
        """Run strategy for all assets concurrently."""
        tasks = [
            self._run_asset(symbol, cfg)
            for symbol, cfg in ASSETS.items()
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_asset(self, symbol: str, cfg: AssetConfig) -> None:
        try:
            # Fetch OHLCV
            df = await gateway.fetch_ohlcv(symbol, CANDLE_INTERVAL, CANDLE_LIMIT)
            if df.empty:
                log.warning("engine.empty_ohlcv", symbol=symbol)
                return

            # Run strategy
            strategy = self.strategies[symbol]
            signal   = strategy.process(df)

            if signal is None:
                log.debug("engine.no_signal", symbol=symbol)
                return

            log.info("engine.signal",
                     symbol=symbol, action=signal.action,
                     direction=signal.direction, price=signal.price,
                     confidence=signal.confidence)

            # Minimum R:R filter
            if signal.action in ("LONG", "SHORT") and signal.rr_ratio < 1.5:
                log.info("engine.rr_filter_rejected",
                         symbol=symbol, rr=signal.rr_ratio)
                return

            # Execute + notify
            await self._execute_signal(signal, cfg, strategy)

        except Exception as e:
            log.error("engine.asset_error", symbol=symbol, error=str(e))
            await notifier.system_alert(f"Error on {symbol}: {e}")

    # ── Signal execution ──────────────────────────────────────────────────

    async def _execute_signal(
        self,
        signal:   Signal,
        cfg:      AssetConfig,
        strategy: GridPyramidStrategy,
    ) -> None:
        state        = strategy.state
        symbol       = signal.symbol
        display_name = cfg.display_name
        executed     = False
        order_id     = ""

        if not self.execution_enabled or symbol in MEME_ASSETS:
            await self._simulate_signal(signal, cfg, strategy)
            return

        # Set leverage once per session (if not already set)
        if state.layers == 0:
            await gateway.set_leverage(symbol, cfg.leverage)

        # ── FLAT / REVERSAL: close all ────────────────────────────────
        if signal.action == "FLAT":
            if state.layers > 0:
                ok = await gateway.close_all_positions(symbol)
                await gateway.cancel_all_orders(symbol)
                pnl_pct = self._estimate_pnl_pct(state, signal.price)
                if ok:
                    executed = True
                    self._record_trade(symbol, pnl_pct)
                state.reset()
            reason = "Trend End" if signal.direction == "" else "Reversal"
            await notifier.send_close(
                display_name, state.direction or signal.direction,
                reason, pnl_pct if state.layers > 0 else 0.0, executed
            )
            # If reversal, trigger new entry
            if signal.direction in ("LONG", "SHORT"):
                await asyncio.sleep(0.5)
                await self._open_core(signal, cfg, strategy)
            return

        # ── LONG / SHORT: core entry ──────────────────────────────────
        if signal.action in ("LONG", "SHORT"):
            await self._open_core(signal, cfg, strategy)
            return

        # ── ADD LAYER ─────────────────────────────────────────────────
        if signal.action == "ADD":
            qty = self._calc_qty(cfg, signal.price, layer=signal.layer)
            p_side = "LONG" if signal.direction == "LONG" else "SHORT"
            side   = "BUY"  if signal.direction == "LONG" else "SELL"
            order  = await gateway.place_order(symbol, side, p_side, qty)
            
            # State update happens regardless of order execution success
            state.layers += 1
            state.entry_prices.append(signal.price)
            state.sizes.append(qty)
            state.trail_active = False  # reset trailing on add
            
            if order:
                executed = True
                order_id = str(order.get("orderId", ""))
                # Update SL only if order succeeded (or we could always try to update)
                await self._update_sl(symbol, state, signal.sl_price, qty)
            await notifier.send_add_layer(signal, display_name, executed)
            return

        # ── REDUCE ────────────────────────────────────────────────────
        if signal.action == "REDUCE":
            to_close = min(2, state.layers - 1)
            for _ in range(to_close):
                if len(state.sizes) <= 1:
                    break
                qty    = state.sizes[-1]
                p_side = "LONG" if signal.direction == "LONG" else "SHORT"
                side   = "SELL" if signal.direction == "LONG" else "BUY"
                order  = await gateway.place_order(
                    symbol, side, p_side, qty, reduce_only=True
                )
                
                # State update happens regardless of order execution success
                state.layers -= 1
                state.entry_prices.pop()
                state.sizes.pop()
                
                if order:
                    executed = True
            await notifier.send_reduce(signal, display_name, executed)
            return

        # ── WEAK TREND warning ────────────────────────────────────────
        if signal.action == "WEAK_TREND":
            await notifier.send_weak_trend(signal, display_name)
            return

    async def _simulate_signal(
        self,
        signal: Signal,
        cfg: AssetConfig,
        strategy: GridPyramidStrategy,
    ) -> None:
        """Signal-only mode: update internal state and send notifications without placing orders."""
        state = strategy.state
        display_name = cfg.display_name

        if signal.action == "FLAT":
            pnl_pct = self._estimate_pnl_pct(state, signal.price)
            was_open = state.layers > 0
            old_direction = state.direction or signal.direction
            if was_open:
                self._record_trade(signal.symbol, pnl_pct)
            state.reset()
            await notifier.send_close(display_name, old_direction, "Trend End" if signal.direction == "" else "Reversal", pnl_pct if was_open else 0.0, False)
            if signal.direction in ("LONG", "SHORT"):
                state.direction = signal.direction
                state.layers = 1
                state.entry_prices = [signal.price]
                state.sizes = [self._calc_qty(cfg, signal.price, layer=0)]
                state.peak_price = signal.price
                state.trail_active = False
                await notifier.send_signal(signal, display_name, False, "")
            return

        if signal.action in ("LONG", "SHORT"):
            state.direction = signal.direction
            state.layers = 1
            state.entry_prices = [signal.price]
            state.sizes = [self._calc_qty(cfg, signal.price, layer=0)]
            state.peak_price = signal.price
            state.trail_active = False
            await notifier.send_signal(signal, display_name, False, "")
            return

        if signal.action == "ADD":
            qty = self._calc_qty(cfg, signal.price, layer=signal.layer)
            state.layers += 1
            state.entry_prices.append(signal.price)
            state.sizes.append(qty)
            state.trail_active = False
            await notifier.send_add_layer(signal, display_name, False)
            return

        if signal.action == "REDUCE":
            to_close = min(2, state.layers - 1)
            for _ in range(to_close):
                if len(state.sizes) <= 1:
                    break
                state.layers -= 1
                state.entry_prices.pop()
                state.sizes.pop()
            await notifier.send_reduce(signal, display_name, False)
            return

        if signal.action == "WEAK_TREND":
            await notifier.send_weak_trend(signal, display_name)
            return


    async def _open_core(
        self,
        signal:   Signal,
        cfg:      AssetConfig,
        strategy: GridPyramidStrategy,
    ) -> None:
        symbol   = signal.symbol
        state    = strategy.state
        qty      = self._calc_qty(cfg, signal.price, layer=0)
        p_side   = "LONG" if signal.direction == "LONG" else "SHORT"
        side     = "BUY"  if signal.direction == "LONG" else "SELL"

        order = await gateway.place_order(symbol, side, p_side, qty)
        executed = False
        order_id = ""

        # Update state regardless of order execution success
        state.direction    = signal.direction
        state.layers       = 1
        state.entry_prices = [signal.price]
        state.sizes        = [qty]
        state.peak_price   = signal.price
        state.trail_active = False

        if order:
            executed = True
            order_id = str(order.get("orderId", ""))
            # Place SL only if order succeeds
            await self._update_sl(symbol, state, signal.sl_price, qty)

        await notifier.send_signal(signal, cfg.display_name, executed, order_id)

    # ── SL management ─────────────────────────────────────────────────────

    async def _update_sl(self, symbol: str, state: AssetState,
                          sl_price: float, qty: float) -> None:
        """Cancel old SL orders and place new one."""
        await gateway.cancel_all_orders(symbol)
        p_side = "LONG" if state.direction == "LONG" else "SHORT"
        await gateway.set_sl(symbol, p_side, sl_price, qty)

    async def update_trailing_sl(self, symbol: str, cfg: AssetConfig) -> None:
        """
        Called every 5 minutes by heartbeat.
        Update trailing SL if activated.
        """
        strategy = self.strategies[symbol]
        state    = strategy.state

        if state.layers == 0 or not state.direction:
            return

        current = await gateway.fetch_ticker(symbol)
        if current <= 0:
            return

        avg  = state.avg_entry
        direction = state.direction

        # Update peak
        if direction == "LONG":
            if current > state.peak_price:
                state.peak_price = current
        else:
            if state.peak_price == 0 or current < state.peak_price:
                state.peak_price = current

        # Check trail activation
        act_pct = cfg.trail_act_pct / 100.0
        was_active = state.trail_active

        if direction == "LONG":
            gain = (state.peak_price - avg) / avg if avg > 0 else 0
        else:
            gain = (avg - state.peak_price) / avg if avg > 0 else 0

        if gain >= act_pct:
            state.trail_active = True

        if not state.trail_active:
            return

        # Notify on activation
        if not was_active:
            dist_pct = cfg.trail_dist_pct / 100.0
            trail_sl = (state.peak_price * (1 - dist_pct)
                        if direction == "LONG"
                        else state.peak_price * (1 + dist_pct))
            await notifier.send_trail_activated(
                cfg.display_name, direction, trail_sl
            )

        # Calculate new trail SL
        dist_pct = cfg.trail_dist_pct / 100.0
        if direction == "LONG":
            new_sl = state.peak_price * (1 - dist_pct)
        else:
            new_sl = state.peak_price * (1 + dist_pct)

        # Get current positions and total qty
        positions = await gateway.get_positions(symbol)
        total_qty = sum(abs(float(p.get("positionAmt", 0))) for p in positions)

        if total_qty > 0:
            await self._update_sl(symbol, state, new_sl, total_qty)
            log.debug("engine.trail_sl_updated",
                      symbol=symbol, new_sl=new_sl, peak=state.peak_price)

    # ── Helpers ───────────────────────────────────────────────────────────

    def _calc_qty(self, cfg: AssetConfig, price: float, layer: int) -> float:
        """Calculate order quantity for layer."""
        if cfg.quantity > 0:
            base = cfg.quantity
        else:
            base = cfg.usdt_per_trade / price * cfg.leverage

        qty = base * (cfg.geo_mult ** layer)
        # Round to reasonable precision
        if price >= 1000:
            return round(qty, 3)
        elif price >= 1:
            return round(qty, 2)
        else:
            return round(qty, 0)

    def _estimate_pnl_pct(self, state: AssetState, current: float) -> float:
        if not state.avg_entry or state.avg_entry == 0:
            return 0.0
        if state.direction == "LONG":
            return (current - state.avg_entry) / state.avg_entry * 100.0
        else:
            return (state.avg_entry - current) / state.avg_entry * 100.0

    def _record_trade(self, symbol: str, pnl_pct: float) -> None:
        stats = self.daily_stats[symbol]
        stats["trades"] += 1
        if pnl_pct > 0:
            stats["wins"] += 1
        stats["total_pnl"] += pnl_pct

    # ── Timing helpers ────────────────────────────────────────────────────

    @staticmethod
    def _seconds_to_next_minute() -> int:
        now = datetime.now(timezone.utc)
        return 60 - now.second

    @staticmethod
    def _next_minute_str() -> str:
        now = datetime.now(timezone.utc)
        next_min = (now.minute + 1) % 60
        next_hour = now.hour if next_min > 0 else (now.hour + 1) % 24
        return f"{next_hour:02d}:{next_min:02d} UTC"

    # ── Daily summary ─────────────────────────────────────────────────────

    def build_summary(self) -> str:
        now = datetime.now(timezone.utc).strftime("%d/%m/%Y")
        lines = [f"📊 <b>DAILY SUMMARY — {now}</b>", "━" * 28]

        total_trades = 0
        total_wins   = 0
        total_pnl    = 0.0

        for symbol, stats in self.daily_stats.items():
            cfg = ASSETS[symbol]
            t   = stats["trades"]
            w   = stats["wins"]
            pnl = stats["total_pnl"]
            wr  = (w / t * 100) if t > 0 else 0
            lines.append(
                f"<b>{cfg.display_name}</b>\n"
                f"  Trades: {t} | Win: {w} ({wr:.0f}%)\n"
                f"  P&L ước tính: {'%.2f' % pnl}%"
            )
            total_trades += t
            total_wins   += w
            total_pnl    += pnl

        overall_wr = (total_wins / total_trades * 100) if total_trades > 0 else 0
        lines += [
            "━" * 28,
            f"📈 Tổng: {total_trades} trades | {overall_wr:.0f}% win",
            f"💰 P&L tổng ước tính: {'%.2f' % total_pnl}%",
            "\n<i>*P&L ước tính, không tính phí giao dịch</i>",
        ]

        # Reset daily stats
        for symbol in self.daily_stats:
            self.daily_stats[symbol] = {"trades": 0, "wins": 0, "total_pnl": 0.0}

        return "\n".join(lines)


# Singleton
engine = TradingEngine()
