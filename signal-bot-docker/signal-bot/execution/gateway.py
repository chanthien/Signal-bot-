"""execution/gateway.py — route private trading actions to local BingX or remote executor service."""

from __future__ import annotations

from typing import Optional

import httpx

from config.settings import EXECUTOR_BASE_URL
from exchange.bingx import bingx
from utils.logger import log


class TradeGateway:
    def __init__(self, executor_base_url: str = EXECUTOR_BASE_URL):
        self.executor_base_url = executor_base_url.rstrip("/")

    @property
    def remote_enabled(self) -> bool:
        return bool(self.executor_base_url)

    async def fetch_ohlcv(self, symbol: str, interval: str, limit: int):
        return await bingx.fetch_ohlcv(symbol, interval, limit)

    async def fetch_ticker(self, symbol: str) -> float:
        return await bingx.fetch_ticker(symbol)

    async def get_balance(self) -> float:
        if not self.remote_enabled:
            return await bingx.get_balance()
        payload = await self._remote("POST", "/balance", {})
        return float(payload.get("balance", 0.0)) if payload else 0.0

    async def get_positions(self, symbol: str) -> list[dict]:
        if not self.remote_enabled:
            return await bingx.get_positions(symbol)
        payload = await self._remote("POST", "/positions", {"symbol": symbol})
        return payload.get("positions", []) if payload else []

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        if not self.remote_enabled:
            return await bingx.set_leverage(symbol, leverage)
        payload = await self._remote("POST", "/set-leverage", {"symbol": symbol, "leverage": leverage})
        return bool(payload.get("ok")) if payload else False

    async def place_order(self, symbol: str, side: str, position_side: str,
                          quantity: float, order_type: str = "MARKET",
                          reduce_only: bool = False) -> dict:
        if not self.remote_enabled:
            return await bingx.place_order(symbol, side, position_side, quantity, order_type, reduce_only)
        payload = await self._remote("POST", "/place-order", {
            "symbol": symbol,
            "side": side,
            "position_side": position_side,
            "quantity": quantity,
            "order_type": order_type,
            "reduce_only": reduce_only,
        })
        return payload.get("order", {}) if payload else {}

    async def close_all_positions(self, symbol: str) -> bool:
        if not self.remote_enabled:
            return await bingx.close_all_positions(symbol)
        payload = await self._remote("POST", "/close-all", {"symbol": symbol})
        return bool(payload.get("ok")) if payload else False

    async def set_sl(self, symbol: str, position_side: str,
                     sl_price: float, quantity: float) -> dict:
        if not self.remote_enabled:
            return await bingx.set_sl(symbol, position_side, sl_price, quantity)
        payload = await self._remote("POST", "/set-sl", {
            "symbol": symbol,
            "position_side": position_side,
            "sl_price": sl_price,
            "quantity": quantity,
        })
        return payload.get("result", {}) if payload else {}

    async def cancel_all_orders(self, symbol: str) -> bool:
        if not self.remote_enabled:
            return await bingx.cancel_all_orders(symbol)
        payload = await self._remote("POST", "/cancel-all", {"symbol": symbol})
        return bool(payload.get("ok")) if payload else False

    async def _remote(self, method: str, path: str, json_payload: dict) -> Optional[dict]:
        url = f"{self.executor_base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.request(method, url, json=json_payload)
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            log.error("gateway.remote_error", url=url, error=str(e))
            return None


gateway = TradeGateway()
