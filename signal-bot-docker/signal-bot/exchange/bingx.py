"""
exchange/bingx.py — BingX Futures USDT-M API client
- Public endpoints: OHLCV, ticker (không cần auth)
- Private endpoints: order execution (cần API key)
"""

import hashlib
import hmac
import time
from urllib.parse import urlencode

import httpx
import pandas as pd

from config.settings import BINGX_API_KEY, BINGX_API_SECRET, BINGX_BASE_URL
from utils.logger import log


class BingXClient:

    def __init__(self):
        self.base    = BINGX_BASE_URL.rstrip("/")
        self.api_key = BINGX_API_KEY.strip()
        self.secret  = BINGX_API_SECRET.strip()

    # ── Signature ─────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_params(params: dict) -> dict:
        normalized: dict[str, str] = {}
        for key, value in params.items():
            if isinstance(value, bool):
                normalized[key] = "true" if value else "false"
            elif isinstance(value, float):
                normalized[key] = format(value, "f").rstrip("0").rstrip(".") or "0"
            else:
                normalized[key] = str(value)
        return normalized

    def _sign(self, params: dict) -> str:
        query = urlencode(params)
        return hmac.new(
            self.secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _auth_headers(self) -> dict:
        return {"X-BX-APIKEY": self.api_key}

    def _signed_params(self, params: dict) -> dict:
        payload = self._normalize_params(params)
        payload["timestamp"] = str(int(time.time() * 1000))
        payload["signature"] = self._sign(payload)
        return payload

    @staticmethod
    def _to_dataframe(candles) -> pd.DataFrame:
        if not candles:
            return pd.DataFrame()

        if isinstance(candles[0], dict):
            df = pd.DataFrame(candles)
            if "time" in df.columns and "open_time" not in df.columns:
                df["time"] = pd.to_numeric(df["time"])
                df["open_time"] = df["time"]
        else:
            df = pd.DataFrame(candles, columns=[
                "open_time", "open", "high", "low", "close", "volume", "close_time"
            ])

        required = ["open_time", "open", "high", "low", "close", "volume"]
        if not all(col in df.columns for col in required):
            return pd.DataFrame()

        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        ts = pd.to_numeric(df["open_time"], errors="coerce")
        if ts.dropna().empty:
            return pd.DataFrame()

        median_ts = float(ts.dropna().median())
        unit = "ms" if median_ts < 1e14 else "ns"
        df["open_time"] = pd.to_datetime(ts, unit=unit, errors="coerce", utc=True)

        df = df.dropna(subset=["open_time", "open", "high", "low", "close", "volume"])
        df = df.sort_values("open_time").reset_index(drop=True)
        return df

    @staticmethod
    def _code_and_msg(data: dict) -> tuple[int | None, str]:
        return data.get("code"), str(data.get("msg", ""))

    # ── Public: OHLCV ─────────────────────────────────────────────────────

    async def fetch_ohlcv(self, symbol: str, interval: str = "1h", limit: int = 100) -> pd.DataFrame:
        """
        Fetch OHLCV candles. No auth needed.
        Returns DataFrame: open, high, low, close, volume (float, newest last)
        """
        url = f"{self.base}/openApi/swap/v3/quote/klines"
        params = {"symbol": symbol, "interval": interval, "limit": str(limit)}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()

            if data.get("code") != 0:
                code, msg = self._code_and_msg(data)
                log.error("bingx.ohlcv_error", symbol=symbol, code=code, msg=msg)
                return pd.DataFrame()

            return self._to_dataframe(data.get("data", []))

        except Exception as e:
            log.error("bingx.ohlcv_exception", symbol=symbol, error=str(e))
            return pd.DataFrame()

    async def fetch_ticker(self, symbol: str) -> float:
        """Get current mark price."""
        url = f"{self.base}/openApi/swap/v2/quote/price"
        params = {"symbol": symbol}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, params=params)
                data = resp.json()
            if data.get("code") == 0:
                return float(data["data"]["price"])
            code, msg = self._code_and_msg(data)
            log.error("bingx.ticker_api_error", symbol=symbol, code=code, msg=msg)
        except Exception as e:
            log.error("bingx.ticker_error", symbol=symbol, error=str(e))
        return 0.0

    # ── Private: Account ──────────────────────────────────────────────────

    async def get_balance(self) -> float:
        """Get available USDT balance."""
        url = f"{self.base}/openApi/swap/v2/user/balance"
        params = self._signed_params({})
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params, headers=self._auth_headers())
                data = resp.json()

            if data.get("code") != 0:
                code, msg = self._code_and_msg(data)
                log.error("bingx.balance_api_error", code=code, msg=msg)
                return 0.0

            payload = data.get("data", {})
            
            # Debug the payload structure
            print(f"DEBUG: get_balance data = {data}")
            
            # data structure: {"code": 0, "msg": "", "data": {"balance": {"asset": "USDT", "availableMargin": "123.45", ...}}}
            # BingX API V2 sometimes returns balance as a dict, not a list
            
            if isinstance(payload, dict) and "balance" in payload:
                balances = payload["balance"]
                # In swap v2/user/balance, it is usually a dict
                if isinstance(balances, dict):
                    if balances.get("asset", "").upper() == "USDT":
                        return float(balances.get("availableMargin", 0.0))
                # Or a list of dicts
                elif isinstance(balances, list):
                    for asset in balances:
                        if isinstance(asset, dict) and str(asset.get("asset", "")).upper() == "USDT":
                            return float(asset.get("availableMargin", 0.0))
                            
        except Exception as e:
            import traceback
            traceback.print_exc()
            log.error("bingx.balance_error", error=str(e))
        return 0.0

    async def get_positions(self, symbol: str) -> list[dict]:
        """Get open positions for symbol."""
        url = f"{self.base}/openApi/swap/v2/user/positions"
        params = self._signed_params({"symbol": symbol})
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params, headers=self._auth_headers())
                data = resp.json()

            if data.get("code") != 0:
                code, msg = self._code_and_msg(data)
                log.error("bingx.positions_api_error", symbol=symbol, code=code, msg=msg)
                return []

            positions = data.get("data", [])
            if not isinstance(positions, list):
                return []
            return [p for p in positions if float(p.get("positionAmt", 0)) != 0]
        except Exception as e:
            log.error("bingx.positions_error", symbol=symbol, error=str(e))
        return []

    # ── Private: Orders ───────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        url = f"{self.base}/openApi/swap/v2/trade/leverage"
        params = self._signed_params({
            "symbol": symbol,
            "leverage": leverage,
            "side": "BOTH",
        })
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, params=params, headers=self._auth_headers())
                data = resp.json()
            return data.get("code") == 0
        except Exception as e:
            log.error("bingx.leverage_error", symbol=symbol, error=str(e))
            return False

    async def place_order(
        self,
        symbol: str,
        side: str,       # "BUY" | "SELL"
        position_side: str,    # "LONG" | "SHORT"
        quantity: float,
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
        url = f"{self.base}/openApi/swap/v2/trade/order"
        
        # Determine correct positionSide and side for One-Way Mode
        # In One-Way mode, positionSide MUST be "BOTH"
        bingx_position_side = "BOTH"
        bingx_side = side
        
        params: dict = {
            "symbol": symbol,
            "side": bingx_side,
            "positionSide": bingx_position_side,
            "type": order_type,
            "quantity": quantity,
        }
        if reduce_only:
            params["reduceOnly"] = "true"

        signed = self._signed_params(params)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, params=signed, headers=self._auth_headers())
                data = resp.json()
            if data.get("code") == 0:
                log.info(
                    "bingx.order_placed",
                    symbol=symbol,
                    side=side,
                    position_side=position_side,
                    qty=quantity,
                    order_id=data["data"]["order"]["orderId"],
                )
                return data["data"]["order"]

            code, msg = self._code_and_msg(data)
            log.error("bingx.order_failed", symbol=symbol, code=code, msg=msg)
            return {}
        except Exception as e:
            log.error("bingx.order_exception", symbol=symbol, error=str(e))
            return {}

    async def close_all_positions(self, symbol: str) -> bool:
        """Close all open positions for symbol."""
        positions = await self.get_positions(symbol)
        success = True
        for pos in positions:
            amt = float(pos.get("positionAmt", 0))
            p_side = pos.get("positionSide", "LONG")
            if amt == 0:
                continue
            
            # In One-Way mode, positionAmt is positive but positionSide is still LONG/SHORT.
            # To close a SHORT position, we BUY. To close a LONG position, we SELL.
            side = "BUY" if p_side == "SHORT" else "SELL"
            
            result = await self.place_order(symbol, side, "BOTH", abs(amt), reduce_only=True)
            if not result:
                success = False
        return success

    async def set_sl(self, symbol: str, position_side: str, sl_price: float, quantity: float) -> dict:
        """Place stop-market SL order."""
        url = f"{self.base}/openApi/swap/v2/trade/order"
        side = "SELL" if position_side == "LONG" else "BUY"
        params = self._signed_params({
            "symbol": symbol,
            "side": side,
            "positionSide": "BOTH",
            "type": "STOP_MARKET",
            "stopPrice": sl_price,
            "quantity": quantity,
            "reduceOnly": "true",
        })
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, params=params, headers=self._auth_headers())
                data = resp.json()
            if data.get("code") != 0:
                code, msg = self._code_and_msg(data)
                log.error("bingx.sl_api_error", symbol=symbol, code=code, msg=msg)
            return data
        except Exception as e:
            log.error("bingx.sl_error", symbol=symbol, error=str(e))
            return {}

    async def cancel_all_orders(self, symbol: str) -> bool:
        """Cancel all open orders (SL/TP) for symbol."""
        url = f"{self.base}/openApi/swap/v2/trade/allOpenOrders"
        params = self._signed_params({"symbol": symbol})
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.delete(url, params=params, headers=self._auth_headers())
                data = resp.json()
            if data.get("code") != 0:
                code, msg = self._code_and_msg(data)
                log.error("bingx.cancel_api_error", symbol=symbol, code=code, msg=msg)
            return data.get("code") == 0
        except Exception as e:
            log.error("bingx.cancel_error", symbol=symbol, error=str(e))
            return False


# Singleton
bingx = BingXClient()
