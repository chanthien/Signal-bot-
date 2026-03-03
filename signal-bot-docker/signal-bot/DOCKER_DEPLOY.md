# DOCKER DEPLOY GUIDE
# TF + Grid Pyramid Signal Bot v8.0
# Tài liệu này đủ để dev setup từ đầu không cần hỏi thêm

---

## 1. TỔNG QUAN

### Hệ thống làm gì
Bot tự động mỗi H1:
1. Fetch OHLCV từ BingX public API (không cần auth)
2. Chạy thuật toán Grid Pyramid Trend Following
3. Tự execute lệnh vào BingX Futures account
4. Gửi signal + notification lên Telegram channel

### Stack
```
GitHub (source code + CI/CD)
    ↓ push to main → GitHub Actions build Docker image
    ↓ push image to GitHub Container Registry (ghcr.io)
VPS Ubuntu (pull image → docker-compose up)
    ↓
BingX Futures API + Telegram Bot API
```

### Cấu trúc project
```
signal-bot/
├── main.py                      # FastAPI entry point, lifespan, endpoints
├── config/
│   └── settings.py              # ← TẤT CẢ config ở đây, kể cả asset list
├── strategy/
│   └── grid_pyramid.py          # ← ĐỔI FILE NÀY KHI SWAP THUẬT TOÁN
├── exchange/
│   └── bingx.py                 # BingX REST API client
├── scheduler/
│   └── engine.py                # Main loop, trailing SL, daily summary
├── notifier/
│   └── telegram.py              # Telegram message formatter + sender
├── utils/
│   └── logger.py                # Structured logging
├── Dockerfile
├── docker-compose.yml
├── .github/workflows/
│   └── docker-build.yml         # CI/CD pipeline
├── requirements.txt
├── .env.example                 # Template credentials
└── .gitignore
```

---

## 2. YÊU CẦU

### VPS
- Ubuntu 20.04+ hoặc Debian 11+
- RAM: 512MB tối thiểu (bot dùng ~150MB)
- Swap: 1GB khuyến nghị
- Port 8000 mở (hoặc đặt sau Nginx)

### Máy dev
- Git
- Docker Desktop (optional, chỉ cần để build/test local)

### Accounts cần có
- GitHub account (để host code + CI/CD)
- BingX account với API key có quyền Futures trading
- Telegram Bot token + Channel ID

---

## 3. SETUP LẦN ĐẦU

### Bước 1: Fork / clone repo lên GitHub

```bash
# Nếu nhận source code dạng zip:
# 1. Tạo repo mới trên GitHub (private)
# 2. Upload source lên

git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/YOUR_USERNAME/signal-bot.git
git push -u origin main
```

### Bước 2: Cấu hình GitHub Actions permissions

Vào GitHub repo → Settings → Actions → General:
- **Workflow permissions**: Read and write permissions ✅
- **Allow GitHub Actions to create and approve pull requests** ✅

### Bước 3: Verify CI/CD chạy

Vào tab Actions trên GitHub → xem workflow `Build & Push Docker Image` đang chạy.

Sau khi success, image sẽ có tại:
```
ghcr.io/YOUR_USERNAME/signal-bot/signal-bot:latest
```

---

## 4. SETUP VPS

### Bước 1: Cài Docker + Docker Compose

```bash
# Chạy với quyền root
curl -fsSL https://get.docker.com | sh
systemctl enable docker
systemctl start docker

# Verify
docker --version
docker compose version
```

### Bước 2: Tạo thư mục và file .env

```bash
mkdir -p /opt/signal-bot/logs
cd /opt/signal-bot

# Tải .env template
curl -o .env.example https://raw.githubusercontent.com/YOUR_USERNAME/signal-bot/main/.env.example
cp --update=none .env.example .env
nano .env
```

Điền đầy đủ vào `.env` (bot sẽ **không khởi động** nếu thiếu TELEGRAM/BINGX credentials):
```env
# ── Telegram ──────────────────────────────────────────────
TELEGRAM_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
CHANNEL_ID=-1001234567890
MY_CHAT_ID=123456789

# ── BingX Futures API ─────────────────────────────────────
BINGX_API_KEY=your_api_key_here
BINGX_API_SECRET=your_secret_here

# ── Server ────────────────────────────────────────────────
PORT=8000

# ── Optional asset groups ─────────────────────────────────
ENABLE_MEME_GROUP=false

# ── Docker image (tuỳ chọn override) ─────────────────────
IMAGE_NAME=ghcr.io/YOUR_USERNAME/signal-bot/signal-bot:latest
```

> Nếu không set `IMAGE_NAME`, docker-compose sẽ dùng image mặc định ở trên.

**Cách lấy các giá trị:**

| Giá trị | Cách lấy |
|---------|----------|
| `TELEGRAM_TOKEN` | Nhắn @BotFather trên Telegram → /newbot |
| `CHANNEL_ID` | Forward 1 message từ channel vào @userinfobot |
| `MY_CHAT_ID` | Nhắn bot bất kỳ gì → `https://api.telegram.org/bot<TOKEN>/getUpdates` → tìm `"chat":{"id":...}` |
| `BINGX_API_KEY` | BingX → Account → API Management → Create API |
| `BINGX_API_SECRET` | Lấy cùng lúc với API key (chỉ hiện 1 lần) |
| `ENABLE_MEME_GROUP` | `true` để bật thêm nhóm coin rác/alt |

### Bước 3: Tải docker-compose.yml

```bash
curl -o docker-compose.yml https://raw.githubusercontent.com/YOUR_USERNAME/signal-bot/main/docker-compose.yml
```

Hoặc tạo tay:
```bash
cat > /opt/signal-bot/docker-compose.yml << 'EOF'
services:
  signal-bot:
    image: ${IMAGE_NAME:-ghcr.io/YOUR_USERNAME/signal-bot/signal-bot:latest}
    container_name: signal-bot
    restart: unless-stopped
    ports:
      - "8000:8000"
    env_file:
      - .env
    volumes:
      - ./logs:/app/logs
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "5"
EOF
```

### Bước 4: Login GitHub Container Registry

```bash
# Cần GitHub Personal Access Token với quyền read:packages
# Tạo tại: GitHub → Settings → Developer Settings → Personal Access Tokens → Classic
# Scope: read:packages

echo "YOUR_GITHUB_PAT" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```

### Bước 5: Pull và start

```bash
cd /opt/signal-bot
# Nếu dùng image từ GHCR
docker compose pull
docker compose up -d

# Nếu muốn build local từ source (không cần GHCR)
# docker compose up -d --build

# Verify
docker compose ps
docker compose logs -f --tail=50
```

---

## 5. VERIFY HOẠT ĐỘNG

```bash
# Health check
curl http://localhost:8000/health

# Response mẫu:
# {
#   "status": "ok",
#   "balance": 1250.50,
#   "assets": {
#     "XAUT-USDT": {"direction": "", "layers": 0, ...},
#     "BTC-USDT":  {"direction": "LONG", "layers": 2, ...},
#     "ETH-USDT":  {"direction": "", "layers": 0, ...}
#   }
# }

# Force chạy strategy ngay (test)
curl -X POST http://localhost:8000/run-now

# Xem status từng asset
curl http://localhost:8000/status/BTC-USDT

# Emergency close
curl -X POST http://localhost:8000/close/BTC-USDT
```

---

## 6. UPDATE CODE → TỰ ĐỘNG DEPLOY

Khi dev sửa code và push lên GitHub:

```bash
# Trên máy dev
git add .
git commit -m "fix: improve signal detection"
git push origin main

# GitHub Actions tự động:
# 1. Build Docker image mới
# 2. Push lên ghcr.io với tag :latest
```

Trên VPS, pull và restart:
```bash
cd /opt/signal-bot
docker compose pull           # pull image mới nhất
docker compose up -d          # restart với image mới (zero-downtime nếu dùng rolling)

# Verify
docker compose ps
curl http://localhost:8000/health
```

### Tự động hóa update (optional)

Cài Watchtower để VPS tự pull image mới mỗi 5 phút:
```bash
docker run -d \
  --name watchtower \
  --restart unless-stopped \
  -v /var/run/docker.sock:/var/run/docker.sock \
  containrrr/watchtower \
  --interval 300 \
  signal-bot
```

---

## 7. SWAP THUẬT TOÁN

Khi muốn đổi thuật toán:

**Bước 1:** Tạo file mới `strategy/ten_algo_moi.py`

Convention bắt buộc:
```python
# strategy/ten_algo_moi.py
from config.settings import AssetConfig
from strategy.grid_pyramid import Signal   # reuse Signal dataclass

class TenAlgoMoiStrategy:
    def __init__(self, cfg: AssetConfig):
        self.cfg   = cfg
        self.state = ...   # state riêng của algo

    def process(self, df) -> Optional[Signal]:
        """
        Nhận OHLCV DataFrame.
        Trả về Signal nếu có action, None nếu không.
        PHẢI implement method này.
        """
        ...
```

**Bước 2:** Sửa `config/settings.py`:
```python
STRATEGY_MODULE = "strategy.ten_algo_moi"
```

**Bước 3:** Push lên GitHub → CI/CD tự build → VPS pull mới.

---

## 8. THÊM ASSET MỚI

Chỉ cần thêm vào `config/settings.py`, không cần sửa gì khác:

```python
ASSETS: dict[str, AssetConfig] = {
    # ... existing assets ...

    "SOL-USDT": AssetConfig(
        symbol         = "SOL-USDT",
        display_name   = "SOL/USDT",
        leverage       = 5,
        usdt_per_trade = 10.0,
        grid_pct       = 0.8,
        hard_sl_pct    = 1.2,
        trail_act_pct  = 0.8,
        trail_dist_pct = 0.4,
        win_rate_base  = 0.55,
    ),
}
```

Push → auto deploy → bot tự chạy thêm SOL.

---

## 9. MONITORING

### Logs
```bash
# Live logs
docker compose logs -f signal-bot

# Logs file (persist sau khi container restart)
tail -f /opt/signal-bot/logs/bot.log
```

### Resource usage
```bash
docker stats signal-bot
# CPU: <5% bình thường
# RAM: ~150MB
```

### Nếu bot crash
```bash
docker compose ps              # xem status
docker compose logs --tail=100 # xem error
docker compose restart         # restart
```

---

## 10. BẢO MẬT

### Không bao giờ commit `.env` lên GitHub
File `.gitignore` đã có rule này. Verify:
```bash
git status   # .env không được xuất hiện trong list
```

### GitHub Secrets (nếu muốn CI/CD tự deploy lên VPS)
Vào GitHub repo → Settings → Secrets → Actions → New repository secret:
- `VPS_HOST`: IP của VPS
- `VPS_USER`: root
- `VPS_SSH_KEY`: private key SSH

Thêm deploy step vào `.github/workflows/docker-build.yml`:
```yaml
- name: Deploy to VPS
  uses: appleboy/ssh-action@v1.0.0
  with:
    host: ${{ secrets.VPS_HOST }}
    username: ${{ secrets.VPS_USER }}
    key: ${{ secrets.VPS_SSH_KEY }}
    script: |
      cd /opt/signal-bot
      docker compose pull
      docker compose up -d
```

### BingX API Key
Khi tạo API key trên BingX:
- ✅ Futures Trading
- ❌ Withdrawal (KHÔNG tick)
- Whitelist IP của VPS nếu có thể

---

## 11. TROUBLESHOOTING

| Lỗi | Nguyên nhân | Fix |
|-----|-------------|-----|
| `image not found` | Chưa login ghcr.io | `docker login ghcr.io` |
| `health: unhealthy` | Bot crash khi start | `docker logs signal-bot` |
| Không nhận signal Telegram | TELEGRAM_TOKEN sai | Kiểm tra `.env` |
| BingX order failed | API key sai hoặc hết margin | Kiểm tra key + balance |
| `connection refused :8000` | Container chưa start | `docker compose up -d` |
| Tín hiệu không gửi lên channel | Bot chưa được add vào channel | Add bot làm admin channel |

---

## 12. QUICK REFERENCE

```bash
# Start
docker compose up -d

# Stop
docker compose down

# Restart
docker compose restart

# Update to latest
docker compose pull && docker compose up -d

# Logs
docker compose logs -f --tail=100

# Health
curl http://localhost:8000/health

# Force run now
curl -X POST http://localhost:8000/run-now

# Close all positions for asset
curl -X POST http://localhost:8000/close/BTC-USDT
```


### Debug nhanh lỗi ký lệnh BingX

Nếu log có `Incorrect apiKey` hoặc `Signature verification failed`, kiểm tra theo thứ tự:
1. API key đúng loại **Futures** và đã bật quyền trade.
2. Key/secret trong `.env` không có khoảng trắng/newline thừa (copy lại tay nếu cần).
3. VPS đã `git pull` bản mới rồi restart bot.
4. Test lại endpoint health và logs:
```bash
docker compose logs -f --tail=100
# hoặc
journalctl -u signal-bot -f
```
