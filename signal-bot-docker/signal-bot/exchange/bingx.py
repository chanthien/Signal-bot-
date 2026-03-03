"""
exchange/bingx.py — BingX Futures USDT-M API client
- Public endpoints: OHLCV, ticker (không cần auth)
- Private endpoints: order execution (cần API key)
"""

import hashlib
import hmac
import time
from typing import Optional
from urllib.parse import urlencode

import httpx
import pandas as pd

from config.settings import BINGX_API_KEY, BINGX_API_SECRET, BINGX_BASE_URL
from utils.logger import log


class BingXClient:

    def __init__(self):
        self.base    = BINGX_BASE_URL
        self.api_key = BINGX_API_KEY
        self.secret  = BINGX_API_SECRET

    # ── Signature ─────────────────────────────────────────────────────────

    def _sign(self, params: dict) -> str:
        query = urlencode(sorted(params.items()))
        return hmac.new(
            self.secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    def _auth_headers(self) -> dict:
        return {"X-BX-APIKEY": self.api_key}

    def _signed_params(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = self._sign(params)
        return params

    # ── Public: OHLCV ─────────────────────────────────────────────────────

    async def fetch_ohlcv(self, symbol: str, interval: str = "1h",
                          limit: int = 100) -> pd.DataFrame:
        """
        Fetch OHLCV candles. No auth needed.
        Returns DataFrame: open, high, low, close, volume (float, newest last)
        """
        url    = f"{self.base}/openApi/swap/v3/quote/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()

            if data.get("code") != 0:
                log.error("bingx.ohlcv_error", symbol=symbol,
                          msg=data.get("msg"))
                return pd.DataFrame()

            candles = data["data"]
            df = pd.DataFrame(candles, columns=[
                "open_time", "open", "high", "low", "close",
                "volume", "close_time"
            ])
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
            df = df.sort_values("open_time").reset_index(drop=True)
            return df

        except Exception as e:
            log.error("bingx.ohlcv_exception", symbol=symbol, error=str(e))
            return pd.DataFrame()

    async def fetch_ticker(self, symbol: str) -> float:
        """Get current mark price."""
        url    = f"{self.base}/openApi/swap/v2/quote/price"
        params = {"symbol": symbol}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, params=params)
                data = resp.json()
            if data.get("code") == 0:
                return float(data["data"]["price"])
        except Exception as e:
            log.error("bingx.ticker_error", symbol=symbol, error=str(e))
        return 0.0

    # ── Private: Account ──────────────────────────────────────────────────

    async def get_balance(self) -> float:
        """Get available USDT balance."""
        url    = f"{self.base}/openApi/swap/v2/user/balance"
        params = self._signed_params({})
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params,
                                        headers=self._auth_headers())
                data = resp.json()
            if data.get("code") == 0:
                for asset in data["data"]["balance"]:
                    if asset["asset"] == "USDT":
                        return float(asset["availableMargin"])
        except Exception as e:
            log.error("bingx.balance_error", error=str(e))
        return 0.0

    async def get_positions(self, symbol: str) -> list[dict]:
        """Get open positions for symbol."""
        url    = f"{self.base}/openApi/swap/v2/user/positions"
        params = self._signed_params({"symbol": symbol})
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params,
                                        headers=self._auth_headers())
                data = resp.json()
            if data.get("code") == 0:
                return [p for p in data["data"] if float(p.get("positionAmt", 0)) != 0]
        except Exception as e:
            log.error("bingx.positions_error", symbol=symbol, error=str(e))
        return []

    # ── Private: Orders ───────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        url    = f"{self.base}/openApi/swap/v2/trade/leverage"
        params = self._signed_params({
            "symbol"      : symbol,
            "leverage"    : leverage,
            "side"        : "LONG",
        })
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, params=params,
                                         headers=self._auth_headers())
                data = resp.json()
            # Also set SHORT side
            params2 = self._signed_params({
                "symbol"  : symbol,
                "leverage": leverage,
                "side"    : "SHORT",
            })
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(url, params=params2,
                                  headers=self._auth_headers())
            return data.get("code") == 0
        except Exception as e:
            log.error("bingx.leverage_error", symbol=symbol, error=str(e))
            return False

    async def place_order(
        self,
        symbol:     str,
        side:       str,       # "BUY" | "SELL"
        position_side: str,    # "LONG" | "SHORT"
        quantity:   float,
        order_type: str = "MARKET",
        reduce_only: bool = False,
    ) -> dict:
        """
        Place futures market order.
        side=BUY + positionSide=LONG  → open long
        side=SELL + positionSide=LONG → close long
        side=SELL + positionSide=SHORT → open short
        side=BUY + positionSide=SHORT  → close short
        """
        url    = f"{self.base}/openApi/swap/v2/trade/order"
        params = {
            "symbol"      : symbol,
            "side"        : side,
            "positionSide": position_side,
            "type"        : order_type,
            "quantity"    : quantity,
        }
        if reduce_only:
            params["reduceOnly"] = "true"

        signed = self._signed_params(params)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, params=signed,
                                         headers=self._auth_headers())
                data = resp.json()
            if data.get("code") == 0:
                log.info("bingx.order_placed", symbol=symbol, side=side,
                         position_side=position_side, qty=quantity,
                         order_id=data["data"]["order"]["orderId"])
                return data["data"]["order"]
            else:
                log.error("bingx.order_failed", symbol=symbol,
                          code=data.get("code"), msg=data.get("msg"))
                return {}
        except Exception as e:
            log.error("bingx.order_exception", symbol=symbol, error=str(e))
            return {}

    async def close_all_positions(self, symbol: str) -> bool:
        """Close all open positions for symbol."""
        positions = await self.get_positions(symbol)
        success = True
        for pos in positions:
            amt   = float(pos["positionAmt"])
            p_side = pos["positionSide"]
            if amt == 0:
                continue
            # Close: opposite side
            side = "SELL" if p_side == "LONG" else "BUY"
            result = await self.place_order(
                symbol, side, p_side, abs(amt), reduce_only=True
            )
            if not result:
                success = False
        return success

    async def set_sl(self, symbol: str, position_side: str,
                     sl_price: float, quantity: float) -> dict:
        """Place stop-market SL order."""
        url  = f"{self.base}/openApi/swap/v2/trade/order"
        side = "SELL" if position_side == "LONG" else "BUY"
        params = self._signed_params({
            "symbol"       : symbol,
            "side"         : side,
            "positionSide" : position_side,
            "type"         : "STOP_MARKET",
            "stopPrice"    : sl_price,
            "quantity"     : quantity,
            "reduceOnly"   : "true",
        })
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, params=params,
                                         headers=self._auth_headers())
                return resp.json()
        except Exception as e:
            log.error("bingx.sl_error", error=str(e))
            return {}

    async def cancel_all_orders(self, symbol: str) -> bool:
        """Cancel all open orders (SL/TP) for symbol."""
        url    = f"{self.base}/openApi/swap/v2/trade/allOpenOrders"
        params = self._signed_params({"symbol": symbol})
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.delete(url, params=params,
                                           headers=self._auth_headers())
                data = resp.json()
            return data.get("code") == 0
        except Exception as e:
            log.error("bingx.cancel_error", error=str(e))
            return False


# Singleton
bingx = BingXClient()
