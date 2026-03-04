import asyncio
import os
import sys
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

from execution.gateway import gateway

async def test_short_pippin():
    symbol = "PIPPIN-USDT"
    print(f"--- Bắt đầu test giao dịch trên {symbol} ---")

    balance = await gateway.get_balance()
    print(f"Số dư (Balance): {balance} USDT")

    if balance < 2.0:
        print("LỖI: Không đủ số dư để test (cần ít nhất 2 USDT).")
        return

    leverage_ok = await gateway.set_leverage(symbol, 3)
    print(f"Set leverage 3: {'Thành công' if leverage_ok else 'Thất bại'}")

    price = await gateway.fetch_ticker(symbol)
    print(f"Giá hiện tại {symbol}: {price}")

    if price <= 0:
        print("LỖI: Không thể lấy giá hiện tại.")
        return

    qty = 6.0 / price
    if price >= 1000:
        qty = round(qty, 3)
    elif price >= 1:
        qty = round(qty, 2)
    else:
        qty = round(qty, 0)

    print(f"Tính toán số lượng đánh Short ($6 volume): {qty} coin")

    print("\nTiến hành đặt lệnh MARKET SHORT...")
    order = await gateway.place_order(
        symbol=symbol,
        side="SELL",
        position_side="SHORT",
        quantity=qty,
        order_type="MARKET"
    )

    if order:
        print(f"✅ ĐẶT LỆNH THÀNH CÔNG! Order ID: {order.get('orderId', 'N/A')}")
        print("Đợi 3 giây trước khi tự động đóng lệnh...")
        await asyncio.sleep(3)
        
        print("\nTiến hành đóng toàn bộ vị thế...")
        close_ok = await gateway.close_all_positions(symbol)
        print(f"Đóng vị thế: {'THÀNH CÔNG' if close_ok else 'THẤT BẠI'}")
    else:
        print("❌ ĐẶT LỆNH THẤT BẠI. Xem log bot_app.log (nếu có) để biết thêm chi tiết.")

if __name__ == "__main__":
    asyncio.run(test_short_pippin())
