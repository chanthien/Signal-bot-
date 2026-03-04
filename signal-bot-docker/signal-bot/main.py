"""
main.py — Bot entry point
FastAPI app + background tasks:
- Trading engine (mỗi H1 close)
- Heartbeat 5 phút (trailing SL update)
- Daily summary 23:55 UTC → review → forward
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from config.settings import ASSETS, PORT, validate_runtime_settings
from exchange.bingx import bingx
from notifier.telegram import notifier
from scheduler.engine import engine
from utils.logger import log


# ── Background tasks ──────────────────────────────────────────────────────

async def _heartbeat_loop():
    """Every 5 minutes: update trailing SL for all assets."""
    while True:
        try:
            await asyncio.sleep(300)   # 5 minutes
            for symbol, cfg in ASSETS.items():
                await engine.update_trailing_sl(symbol, cfg)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("heartbeat.error", error=str(e))


async def _daily_summary_loop():
    """
    23:55 UTC: build summary → send to MY_CHAT_ID for review
    Wait up to 30 min for reply 'send' or 'skip'
    """
    while True:
        try:
            # Wait until 23:55 UTC
            now = datetime.now(timezone.utc)
            target_hour, target_min = 23, 55
            secs_today = (target_hour * 3600 + target_min * 60)
            secs_now   = now.hour * 3600 + now.minute * 60 + now.second
            wait       = secs_today - secs_now
            if wait <= 0:
                wait += 86400   # next day

            await asyncio.sleep(wait)

            # Build and send for review
            summary = engine.build_summary()
            msg_id  = await notifier.send_summary_for_review(summary)
            log.info("summary.sent_for_review")

            # Poll for reply up to 30 minutes
            if msg_id:
                for _ in range(36):   # 36 × 50s = 30 min
                    await asyncio.sleep(50)
                    reply = await notifier.check_reply(msg_id)
                    if reply == "send":
                        await notifier.forward_summary_to_channel(summary)
                        log.info("summary.forwarded_to_channel")
                        break
                    elif reply == "skip":
                        log.info("summary.skipped")
                        break
                else:
                    log.info("summary.timeout_no_reply")

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("daily_summary.error", error=str(e))


# ── App lifecycle ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("bot.starting")

    missing = validate_runtime_settings()
    if missing:
        msg = f"Missing required settings: {', '.join(missing)}"
        log.error("config.missing_required", missing=missing)
        raise RuntimeError(msg)

    # Start background tasks
    engine_task   = asyncio.create_task(engine.start())
    heartbeat     = asyncio.create_task(_heartbeat_loop())
    daily_summary = asyncio.create_task(_daily_summary_loop())

    yield

    # Shutdown
    engine_task.cancel()
    heartbeat.cancel()
    daily_summary.cancel()
    await engine.stop()
    await notifier.system_alert("🔴 Signal Bot STOPPED")
    log.info("bot.stopped")


app = FastAPI(
    title    = "TF Grid Pyramid Signal Bot",
    version  = "8.0.0",
    lifespan = lifespan,
)


# ── API endpoints ─────────────────────────────────────────────────────────


@app.get("/live")
async def live():
    """Lightweight liveness probe (no external dependency)."""
    return JSONResponse({"status": "alive"})


@app.get("/health")
async def health():
    status = {}
    for symbol, strategy in engine.strategies.items():
        s = strategy.state
        status[symbol] = {
            "direction"   : s.direction,
            "layers"      : s.layers,
            "avg_entry"   : round(s.avg_entry, 4),
            "trail_active": s.trail_active,
        }
    balance = await gateway.get_balance()
    return JSONResponse({
        "status" : "ok",
        "balance": balance,
        "assets" : status,
    })


@app.get("/status/{symbol}")
async def asset_status(symbol: str):
    symbol = symbol.upper()
    if symbol not in engine.strategies:
        return JSONResponse({"error": "unknown symbol"}, status_code=404)
    s = engine.strategies[symbol].state
    current = await gateway.fetch_ticker(symbol)
    pnl = engine._estimate_pnl_pct(s, current) if current > 0 else 0.0
    return JSONResponse({
        "symbol"      : symbol,
        "direction"   : s.direction,
        "layers"      : s.layers,
        "avg_entry"   : round(s.avg_entry, 4),
        "current_price": current,
        "estimated_pnl": round(pnl, 4),
        "trail_active": s.trail_active,
        "peak_price"  : s.peak_price,
    })


@app.post("/close/{symbol}")
async def close_symbol(symbol: str):
    """Emergency close all positions for symbol."""
    symbol = symbol.upper()
    if symbol not in engine.strategies:
        return JSONResponse({"error": "unknown symbol"}, status_code=404)

    strategy = engine.strategies[symbol]
    state    = strategy.state
    current  = await gateway.fetch_ticker(symbol)
    pnl      = engine._estimate_pnl_pct(state, current)
    close_direction = state.direction or "ALL"

    ok = await gateway.close_all_positions(symbol)
    await gateway.cancel_all_orders(symbol)
    state.reset()

    await notifier.send_close(
        ASSETS[symbol].display_name,
        close_direction,
        "Manual Close",
        pnl, ok
    )
    return JSONResponse({"closed": ok, "estimated_pnl": round(pnl, 4)})


@app.post("/run-now")
async def run_now():
    """Force run strategy for all assets immediately (for testing)."""
    await engine._run_all()
    return JSONResponse({"status": "executed"})


# ── Entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host    = "0.0.0.0",
        port    = PORT,
        reload  = False,
        workers = 1,
    )
