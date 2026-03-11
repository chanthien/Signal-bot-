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

from config.settings import ASSETS, AssetConfig, CANDLE_INTERVAL, CANDLE_LIMIT, EXECUTION_ENABLED, MEME_ASSETS, CORE_ASSETS
from execution.gateway import gateway
from notifier.telegram import notifier
from strategy.grid_pyramid_v9_optimized import GridPyramidStrategy, Signal, AssetState
from utils.logger import log


import importlib

class TradingEngine:

    def __init__(self):
        # Khởi tạo strategy instance cho từng asset
        self.strategies: dict[str, any] = {}
        for symbol, cfg in ASSETS.items():
            try:
                module_path = getattr(cfg, "strategy_module", "strategy.grid_pyramid")
                module = importlib.import_module(module_path)
                
                # Guess class name (e.g., "GridPyramidV9Optimized" from "grid_pyramid_v9_optimized")
                # Alternatively we can define a standard class name or try both:
                class_names = [
                    "AndzV80Strategy", 
                    "GridPyramidStrategy", 
                    "AndzV71Strategy"
                ]
                
                strategy_class = None
                for name in class_names:
                    if hasattr(module, name):
                        strategy_class = getattr(module, name)
                        break
                        
                if strategy_class:
                    self.strategies[symbol] = strategy_class(cfg)
                else:
                    log.error(f"engine.init_error", msg=f"No matching strategy class found for {symbol} in {module_path}")
            except Exception as e:
                log.error(f"engine.import_error", error=str(e), symbol=symbol)
                
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

        # ── Position reconciliation on startup ────────────────────────
        if self.execution_enabled:
            await self._reconcile_positions()

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

    # ── Position reconciliation ────────────────────────────────────────

    async def _reconcile_positions(self) -> None:
        """Sync internal state with actual BingX positions on startup."""
        for symbol, cfg in ASSETS.items():
            if symbol in MEME_ASSETS:
                continue  # meme = signal-only, no reconciliation needed
            try:
                positions = await gateway.get_positions(symbol)
                strategy = self.strategies.get(symbol)
                if not strategy:
                    continue
                state = strategy.state

                for pos in positions:
                    amt = float(pos.get("positionAmt", 0))
                    if amt == 0:
                        continue
                    p_side = pos.get("positionSide", "").upper()
                    avg_price = float(pos.get("avgPrice", 0))
                    
                    if p_side in ("LONG", "SHORT") and abs(amt) > 0:
                        # We have an open position on exchange — sync state
                        if state.direction == "" or state.layers == 0:
                            state.direction = p_side
                            state.layers = 1
                            state.entry_prices = [avg_price]
                            state.sizes = [abs(amt)]
                            state.peak_price = avg_price
                            state.trail_active = False
                            log.info("engine.reconcile_synced",
                                     symbol=symbol, direction=p_side,
                                     avg_price=avg_price, qty=abs(amt))
                            await notifier.system_alert(
                                f"♻️ Synced {symbol}: {p_side} @ {avg_price}, qty={abs(amt)}"
                            )
            except Exception as e:
                log.error("engine.reconcile_error", symbol=symbol, error=str(e))

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
            strategy_name = strategy.__class__.__name__
            await notifier.send_close(
                display_name, state.direction or signal.direction,
                reason, pnl_pct if state.layers > 0 else 0.0, executed,
                strategy_name=strategy_name
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
            # ── Layer overflow guard ──────────────────────────────
            if state.layers >= cfg.max_layers:
                log.warning("engine.add_rejected_max_layers",
                            symbol=symbol, layers=state.layers,
                            max_layers=cfg.max_layers)
                return

            qty = self._calc_qty(cfg, signal.price, layer=signal.layer,
                                 confidence=signal.confidence)
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
                # Update hard SL for entire position
                await self._update_hard_sl(symbol, state, signal.sl_price)
            await notifier.send_add_layer(signal, display_name, executed, strategy_name=strategy.__class__.__name__)
            return

        # ── REDUCE ────────────────────────────────────────────────────
        if signal.action == "REDUCE":
            to_close = min(2, state.layers - 1)
            log.info("engine.reduce_start", symbol=symbol,
                     layers=state.layers, to_close=to_close)
            for i in range(to_close):
                if len(state.sizes) <= 1:
                    log.info("engine.reduce_skip_core", symbol=symbol)
                    break
                qty    = state.sizes[-1]
                p_side = "LONG" if signal.direction == "LONG" else "SHORT"
                side   = "SELL" if signal.direction == "LONG" else "BUY"
                # Remove reduce_only=True to prevent BingX API rejection in Hedge mode
                order  = await gateway.place_order(
                    symbol, side, p_side, qty, reduce_only=False
                )
                
                # State update happens regardless of order execution success
                state.layers -= 1
                state.entry_prices.pop()
                state.sizes.pop()
                
                if order:
                    executed = True
                    log.info("engine.reduce_executed", symbol=symbol,
                             layer_closed=i+1, qty=qty)
                else:
                    log.warning("engine.reduce_order_failed", symbol=symbol,
                                layer_closed=i+1, qty=qty)

            # Update hard SL for remaining position
            if executed and state.layers > 0:
                await self._update_hard_sl(symbol, state, signal.sl_price)

            await notifier.send_reduce(signal, display_name, executed, strategy_name=strategy.__class__.__name__)
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
            strategy_name = strategy.__class__.__name__
            await notifier.send_close(display_name, old_direction, "Trend End" if signal.direction == "" else "Reversal", pnl_pct if was_open else 0.0, False, strategy_name=strategy_name)
            if signal.direction in ("LONG", "SHORT"):
                state.direction = signal.direction
                state.layers = 1
                state.entry_prices = [signal.price]
                state.sizes = [self._calc_qty(cfg, signal.price, layer=0)]
                state.peak_price = signal.price
                state.trail_active = False
                await notifier.send_signal(signal, display_name, False, "", strategy_name=strategy_name)
            return

        if signal.action in ("LONG", "SHORT"):
            state.direction = signal.direction
            state.layers = 1
            state.entry_prices = [signal.price]
            state.sizes = [self._calc_qty(cfg, signal.price, layer=0)]
            state.peak_price = signal.price
            state.trail_active = False
            await notifier.send_signal(signal, display_name, False, "", strategy_name=strategy.__class__.__name__)
            return

        if signal.action == "ADD":
            qty = self._calc_qty(cfg, signal.price, layer=signal.layer)
            state.layers += 1
            state.entry_prices.append(signal.price)
            state.sizes.append(qty)
            state.trail_active = False
            await notifier.send_add_layer(signal, display_name, False, strategy_name=strategy.__class__.__name__)
            return

        if signal.action == "REDUCE":
            to_close = min(2, state.layers - 1)
            for _ in range(to_close):
                if len(state.sizes) <= 1:
                    break
                state.layers -= 1
                state.entry_prices.pop()
                state.sizes.pop()
            await notifier.send_reduce(signal, display_name, False, strategy_name=strategy.__class__.__name__)
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
        qty      = self._calc_qty(cfg, signal.price, layer=0,
                                  confidence=signal.confidence)
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
            # Place hard SL on exchange (persists even if VPS goes down)
            await self._update_hard_sl(symbol, state, signal.sl_price)

        await notifier.send_signal(signal, cfg.display_name, executed, order_id, strategy_name=strategy.__class__.__name__)

    # ── SL management ─────────────────────────────────────────────────────

    async def _update_sl(self, symbol: str, state: AssetState,
                          sl_price: float, qty: float) -> None:
        """Cancel old SL orders and place new one."""
        await gateway.cancel_all_orders(symbol)
        p_side = "LONG" if state.direction == "LONG" else "SHORT"
        await gateway.set_sl(symbol, p_side, sl_price, qty)

    async def _update_hard_sl(self, symbol: str, state: AssetState,
                               sl_price: float) -> None:
        """
        Place/update hard SL on exchange for ENTIRE position.
        This SL persists on BingX even if the bot/VPS goes offline.
        """
        total_qty = state.total_size
        if total_qty <= 0:
            return
        await gateway.cancel_all_orders(symbol)
        p_side = "LONG" if state.direction == "LONG" else "SHORT"
        result = await gateway.set_sl(symbol, p_side, sl_price, total_qty)
        if result:
            log.info("engine.hard_sl_placed",
                     symbol=symbol, sl_price=sl_price, qty=total_qty)
        else:
            log.warning("engine.hard_sl_failed",
                        symbol=symbol, sl_price=sl_price)

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

        # Get actual position qty from exchange for accuracy
        total_qty = state.total_size
        if total_qty <= 0:
            # Fallback: query exchange
            positions = await gateway.get_positions(symbol)
            total_qty = sum(abs(float(p.get("positionAmt", 0))) for p in positions)

        if total_qty > 0:
            await self._update_sl(symbol, state, new_sl, total_qty)
            log.debug("engine.trail_sl_updated",
                      symbol=symbol, new_sl=new_sl, peak=state.peak_price)

    # ── Helpers ───────────────────────────────────────────────────────────

    def _calc_qty(self, cfg: AssetConfig, price: float, layer: int,
                  confidence: float = 1.0) -> float:
        """
        Calculate order quantity for layer.
        For core assets (BTC, ETH, XAUT): scale by confidence.
        e.g. confidence=0.58 → qty *= 0.58
        """
        if cfg.quantity > 0:
            base = cfg.quantity
        else:
            base = cfg.usdt_per_trade / price * cfg.leverage

        if cfg.symbol in CORE_ASSETS:
            # Core layer is 3x Add layer size for core assets
            if layer > 0:
                qty = base / 3.0
            else:
                qty = base
            # Skip confidence scaling for fixed minimal sizes to maintain strict 3x ratio and avoid hitting < 0.0001 error
        else:
            qty = base * (cfg.geo_mult ** layer)
            if confidence > 0:
                qty *= confidence

        # Apply strict precision rounding and HARD minimums to prevent API `qty=0` rejection
        if "BTC" in cfg.symbol:
            qty = round(qty, 4)
            return max(qty, 0.0001)
        elif "ETH" in cfg.symbol:
            qty = round(qty, 2)
            return max(qty, 0.01)
        elif "XAUT" in cfg.symbol:
            qty = round(qty, 2)
            return max(qty, 0.01)
        else:
            if price >= 1000:
                return round(qty, 4)
            elif price >= 1:
                return round(qty, 3)
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
