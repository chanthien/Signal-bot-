FROM python:3.11-slim

# ── System deps ───────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── App directory ─────────────────────────────────────────
WORKDIR /app

# ── Install Python deps (layer cache) ────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Copy source ───────────────────────────────────────────
COPY . .

# ── Create logs dir ───────────────────────────────────────
RUN mkdir -p logs

# ── Expose port ───────────────────────────────────────────
EXPOSE 8000

# ── Health check ──────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/live || exit 1

# ── Run ───────────────────────────────────────────────────
CMD ["python", "-m", "uvicorn", "main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
