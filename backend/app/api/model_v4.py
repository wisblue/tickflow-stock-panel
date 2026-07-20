"""Model-v4 realtime sell monitor API."""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.services.model_v4_bb_exit import evaluate_positions

router = APIRouter(prefix="/api/model-v4", tags=["model-v4"])


@router.get("/bb-realtime/positions")
def bb_realtime_positions(request: Request, trade_date: str | None = None):
    """Evaluate all active ``source=positions`` symbols in one refresh."""
    return evaluate_positions(request, trade_date)
