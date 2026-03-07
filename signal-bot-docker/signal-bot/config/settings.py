"""
config/settings.py — Centralized configuration
Tất cả credentials và parameters ở đây.
Đổi thuật toán: chỉ cần thay STRATEGY_MODULE.
"""

import os
from dotenv import load_dotenv
from dataclasses import dataclass

load_dotenv()


def _env(name: str, default: str = "") -> str:
    """Read env var and trim spaces/newlines to avoid hidden signature issues."""
    return os.getenv(name, default).strip()


# ── Telegram ──────────────────────────────────────────────
TELEGRAM_TOKEN   = _env("TELEGRAM_TOKEN")
CHANNEL_ID       = _env("CHANNEL_ID")       # channel khách xem
MY_CHAT_ID       = _env("MY_CHAT_ID")       # private chat mày review

# ── BingX ─────────────────────────────────────────────────
BINGX_API_KEY    = _env("BINGX_API_KEY")
BINGX_API_SECRET = _env("BINGX_API_SECRET")
BINGX_BASE_URL   = "https://open-api.bingx.com"

# ── Webhook server ─────────────────────────────────────────
WEBHOOK_SECRET   = _env("WEBHOOK_SECRET", "changeme")
PORT             = int(_env("PORT", "8000"))

# ── Executor ───────────────────────────────────────────────
EXECUTOR_BASE_URL= _env("EXECUTOR_BASE_URL", "")
EXECUTION_ENABLED= _env("EXECUTION_ENABLED", "false").lower() in {"1", "true", "yes", "on"}

# ── Strategy module (swap khi đổi algo) ───────────────────
STRATEGY_MODULE  = "strategy.grid_pyramid"

# ── Global H1 defaults (override per-asset trong AssetConfig) ─
CANDLE_INTERVAL  = "1h"
CANDLE_LIMIT     = 100    # số nến fetch mỗi lần
ENABLE_MEME_GROUP = _env("ENABLE_MEME_GROUP", "false").lower() in {"1", "true", "yes", "on"}


@dataclass
class AssetConfig:
    """Per-asset configuration."""
    symbol:          str            # BingX symbol, e.g. "BTC-USDT"
    display_name:    str            # Tên hiển thị trên Telegram
    leverage:        int   = 5      # đòn bẩy
    quantity:        float = 0.0    # contract size (0 = tự tính từ usdt_per_trade)
    usdt_per_trade:  float = 10.0   # USD mỗi lệnh core (nếu quantity=0)
    pip_value:       float = 0.1    # giá trị 1 pip (price point)
    strategy_module: str   = "strategy.grid_pyramid" # Add strategy override module per asset
    account_equity:  float = 1000.0 # base account equity for risk
    risk_pct:        float = 0.01   # base max risk per trade

    # H1 Strategy params
    pivot_len:       int   = 4
    atr_len:         int   = 14
    impulse_mult:    float = 1.1
    min_atr_pct:     float = 0.2

    # Grid
    grid_pct:        float = 0.8    # % thay vì pip (dễ áp dụng đa asset)
    geo_mult:        float = 1.3
    max_layers:      int   = 5

    # Stop Loss (% từ avg entry)
    hard_sl_pct:     float = 1.2    # hard SL %
    trail_act_pct:   float = 0.8    # trailing activate khi lãi X%
    trail_dist_pct:  float = 0.4    # trailing distance %

    # Risk
    max_dd_pct:      float = 10.0

    # TP targets (% từ entry, dùng để hiển thị trên Telegram)
    tp1_atr_mult:    float = 2.0
    tp2_atr_mult:    float = 4.0

    # Win rate baseline cho confidence score
    win_rate_base:   float = 0.60   # H1 baseline cao hơn M15


# ── Core asset configs ─────────────────────────────────────

CORE_ASSETS: dict[str, AssetConfig] = {
    "XAUT-USDT": AssetConfig(
        symbol        = "XAUT-USDT",
        display_name  = "XAU/USDT (Vàng)",
        leverage      = 40,
        quantity      = 0.0125, # 0.05 / 4
        pip_value     = 0.1,
        strategy_module = "strategy.andz_v71",
    ),
    "BTC-USDT": AssetConfig(
        symbol        = "BTC-USDT",
        display_name  = "BTC/USDT",
        leverage      = 40,
        quantity      = 0.0025, # 0.01 / 4
        pip_value     = 1.0,
        strategy_module = "strategy.grid_pyramid_v9_optimized",
    ),
    "ETH-USDT": AssetConfig(
        symbol        = "ETH-USDT",
        display_name  = "ETH/USDT",
        leverage      = 40,
        quantity      = 0.025, # 0.1 / 4
        pip_value     = 0.1,
        strategy_module = "strategy.andz_v80_strategy",
    ),
}


# ── Meme/alt group (bật qua ENABLE_MEME_GROUP=true) ─────────

MEME_ASSETS: dict[str, AssetConfig] = {
    "ARC-USDT": AssetConfig(symbol="ARC-USDT", display_name="ARC/USDT", leverage=3, usdt_per_trade=5.0, pip_value=0.0001),
    "MYX-USDT": AssetConfig(symbol="MYX-USDT", display_name="MYX/USDT", leverage=3, usdt_per_trade=5.0, pip_value=0.0001),
    "POWER-USDT": AssetConfig(symbol="POWER-USDT", display_name="POWER/USDT", leverage=3, usdt_per_trade=5.0, pip_value=0.0001),
    "PHA-USDT": AssetConfig(symbol="PHA-USDT", display_name="PHA/USDT", leverage=3, usdt_per_trade=5.0, pip_value=0.0001),
    "B-USDT": AssetConfig(symbol="B-USDT", display_name="B/USDT", leverage=3, usdt_per_trade=5.0, pip_value=0.0001),
    "BABY-USDT": AssetConfig(symbol="BABY-USDT", display_name="BABY/USDT", leverage=3, usdt_per_trade=5.0, pip_value=0.0001),
    "PIPPIN-USDT": AssetConfig(symbol="PIPPIN-USDT", display_name="PIPPIN/USDT", leverage=3, usdt_per_trade=5.0, pip_value=0.0001),
    "ADA-USDT": AssetConfig(symbol="ADA-USDT", display_name="ADA/USDT", leverage=3, usdt_per_trade=5.0, pip_value=0.0001),
    "MAGIC-USDT": AssetConfig(symbol="MAGIC-USDT", display_name="MAGIC/USDT", leverage=3, usdt_per_trade=5.0, pip_value=0.0001),
    "RIVER-USDT": AssetConfig(symbol="RIVER-USDT", display_name="RIVER/USDT", leverage=3, usdt_per_trade=5.0, pip_value=0.0001),
    "TAU-USDT": AssetConfig(symbol="TAU-USDT", display_name="TAU/USDT", leverage=3, usdt_per_trade=5.0, pip_value=0.0001),
    "DAM-USDT": AssetConfig(symbol="DAM-USDT", display_name="DAM/USDT", leverage=3, usdt_per_trade=5.0, pip_value=0.0001),
    "EUL-USDT": AssetConfig(symbol="EUL-USDT", display_name="EUL/USDT", leverage=3, usdt_per_trade=5.0, pip_value=0.0001),
}

ASSETS: dict[str, AssetConfig] = {**CORE_ASSETS, **(MEME_ASSETS if ENABLE_MEME_GROUP else {})}


def validate_runtime_settings() -> list[str]:
    """Return missing required settings for live trading runtime."""
    required = {
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "CHANNEL_ID": CHANNEL_ID,
        "MY_CHAT_ID": MY_CHAT_ID,
        "BINGX_API_KEY": BINGX_API_KEY,
        "BINGX_API_SECRET": BINGX_API_SECRET,
    }
    return [name for name, value in required.items() if not value]
