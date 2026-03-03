"""
config/settings.py — Centralized configuration
Tất cả credentials và parameters ở đây.
Đổi thuật toán: chỉ cần thay STRATEGY_MODULE.
"""

import os
from dotenv import load_dotenv
from dataclasses import dataclass, field

load_dotenv()

# ── Telegram ──────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
CHANNEL_ID       = os.getenv("CHANNEL_ID", "")       # channel khách xem
MY_CHAT_ID       = os.getenv("MY_CHAT_ID", "")        # private chat mày review

# ── BingX ─────────────────────────────────────────────────
BINGX_API_KEY    = os.getenv("BINGX_API_KEY", "")
BINGX_API_SECRET = os.getenv("BINGX_API_SECRET", "")
BINGX_BASE_URL   = "https://open-api.bingx.com"

# ── Webhook server ─────────────────────────────────────────
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET", "changeme")
PORT             = int(os.getenv("PORT", "8000"))

# ── Strategy module (swap khi đổi algo) ───────────────────
STRATEGY_MODULE  = "strategy.grid_pyramid"

# ── Global H1 defaults (override per-asset trong AssetConfig) ─
CANDLE_INTERVAL  = "1h"
CANDLE_LIMIT     = 100    # số nến fetch mỗi lần


@dataclass
class AssetConfig:
    """Per-asset configuration."""
    symbol:          str            # BingX symbol, e.g. "BTC-USDT"
    display_name:    str            # Tên hiển thị trên Telegram
    leverage:        int   = 5      # đòn bẩy
    quantity:        float = 0.0    # contract size (0 = tự tính từ usdt_per_trade)
    usdt_per_trade:  float = 10.0   # USD mỗi lệnh core (nếu quantity=0)
    pip_value:       float = 0.1    # giá trị 1 pip (price point)

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
    win_rate_base:   float = 0.55   # H1 baseline cao hơn M15


# ── Asset configs ─────────────────────────────────────────

ASSETS: dict[str, AssetConfig] = {
    "GOLD-USDT": AssetConfig(
        symbol        = "GOLD-USDT",
        display_name  = "XAU/USDT (Vàng)",
        leverage      = 5,
        usdt_per_trade= 10.0,
        pip_value     = 0.1,
        grid_pct      = 0.8,
        hard_sl_pct   = 1.2,
        trail_act_pct = 0.8,
        trail_dist_pct= 0.4,
        win_rate_base = 0.55,
    ),
    "BTC-USDT": AssetConfig(
        symbol        = "BTC-USDT",
        display_name  = "BTC/USDT",
        leverage      = 5,
        usdt_per_trade= 10.0,
        pip_value     = 1.0,
        grid_pct      = 0.8,
        hard_sl_pct   = 1.2,
        trail_act_pct = 0.8,
        trail_dist_pct= 0.4,
        win_rate_base = 0.55,
    ),
    "ETH-USDT": AssetConfig(
        symbol        = "ETH-USDT",
        display_name  = "ETH/USDT",
        leverage      = 5,
        usdt_per_trade= 10.0,
        pip_value     = 0.1,
        grid_pct      = 0.8,
        hard_sl_pct   = 1.2,
        trail_act_pct = 0.8,
        trail_dist_pct= 0.4,
        win_rate_base = 0.55,
    ),
}
