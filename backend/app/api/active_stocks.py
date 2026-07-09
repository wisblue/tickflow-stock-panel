"""Active stocks API for realtime transaction refresh."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.services import active_stocks

router = APIRouter(prefix="/api/active-stocks", tags=["active-stocks"])


class ActiveStockRequest(BaseModel):
    symbol: str
    name: str = ""
    source: str = "manual"


class ActiveStocksRequest(BaseModel):
    symbols: list[str]
    source: str = "manual"


@router.get("")
def list_all() -> dict:
    return {
        "symbols": active_stocks.list_symbols(),
        "active_symbols_file": str(active_stocks.active_symbols_path()),
    }


@router.post("")
def add_one(req: ActiveStockRequest) -> dict:
    return {
        "symbols": active_stocks.add(req.symbol, req.name, req.source),
        "active_symbols_file": str(active_stocks.active_symbols_path()),
    }


@router.post("/batch")
def add_batch(req: ActiveStocksRequest) -> dict:
    return {
        "symbols": active_stocks.add_many(req.symbols, req.source),
        "active_symbols_file": str(active_stocks.active_symbols_path()),
    }


@router.delete("/{symbol}")
def remove_one(symbol: str) -> dict:
    return {
        "symbols": active_stocks.remove(symbol),
        "active_symbols_file": str(active_stocks.active_symbols_path()),
    }
