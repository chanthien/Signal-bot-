"""executor_api.py — dedicated execution service for private BingX actions."""

from fastapi import FastAPI
from pydantic import BaseModel

from exchange.bingx import bingx

app = FastAPI(title="Signal Bot Executor", version="1.0.0")


class SymbolReq(BaseModel):
    symbol: str


class LeverageReq(BaseModel):
    symbol: str
    leverage: int


class OrderReq(BaseModel):
    symbol: str
    side: str
    position_side: str
    quantity: float
    order_type: str = "MARKET"
    reduce_only: bool = False


class SLReq(BaseModel):
    symbol: str
    position_side: str
    sl_price: float
    quantity: float


@app.get("/health")
async def health():
    return {"status": "ok", "service": "executor"}


@app.post("/balance")
async def balance():
    return {"balance": await bingx.get_balance()}


@app.post("/positions")
async def positions(req: SymbolReq):
    return {"positions": await bingx.get_positions(req.symbol.upper())}


@app.post("/set-leverage")
async def set_leverage(req: LeverageReq):
    ok = await bingx.set_leverage(req.symbol.upper(), req.leverage)
    return {"ok": ok}


@app.post("/place-order")
async def place_order(req: OrderReq):
    order = await bingx.place_order(
        req.symbol.upper(), req.side, req.position_side,
        req.quantity, req.order_type, req.reduce_only,
    )
    return {"order": order}


@app.post("/close-all")
async def close_all(req: SymbolReq):
    ok = await bingx.close_all_positions(req.symbol.upper())
    return {"ok": ok}


@app.post("/set-sl")
async def set_sl(req: SLReq):
    result = await bingx.set_sl(req.symbol.upper(), req.position_side, req.sl_price, req.quantity)
    return {"result": result}


@app.post("/cancel-all")
async def cancel_all(req: SymbolReq):
    ok = await bingx.cancel_all_orders(req.symbol.upper())
    return {"ok": ok}
