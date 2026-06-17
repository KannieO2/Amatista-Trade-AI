from fastapi import FastAPI
from pydantic import BaseModel


class Exchange(BaseModel):
    id: str
    name: str
    enabled: bool
    supports_futures: bool


app = FastAPI(title="TradeOS AI Exchange Hub", version="0.1.0")

supported_exchanges = [
    Exchange(id="binance", name="Binance", enabled=True, supports_futures=True),
    Exchange(id="bitget", name="Bitget", enabled=True, supports_futures=True),
    Exchange(id="mexc", name="MEXC", enabled=True, supports_futures=True),
    Exchange(id="hyperliquid", name="Hyperliquid", enabled=False, supports_futures=True),
    Exchange(id="grvt", name="GRVT", enabled=False, supports_futures=True),
]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "exchange-hub"}


@app.get("/exchanges", response_model=list[Exchange])
async def list_exchanges() -> list[Exchange]:
    return supported_exchanges

