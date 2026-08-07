"""Model-v4 realtime sell monitor API."""
from __future__ import annotations

import re

from fastapi import APIRouter, Request

from app.services.model_v4_bb_exit import evaluate_positions as evaluate_bb_positions
from app.services.model_v4_sr013_exit import evaluate_positions as evaluate_sr013_positions

router = APIRouter(prefix="/api/model-v4", tags=["model-v4"])


@router.get("/bb-realtime/positions")
def bb_realtime_positions(request: Request, trade_date: str | None = None):
    """Legacy BB monitor retained for API compatibility."""
    return evaluate_bb_positions(request, trade_date)


@router.get("/sr013-realtime/positions")
def sr013_realtime_positions(
    request: Request,
    trade_date: str | None = None,
    symbols: str | None = None,
):
    """Evaluate browser positions, falling back to the shared active set."""
    requested = (
        [value for value in re.split(r"[\s,]+", symbols) if value]
        if symbols is not None
        else None
    )
    return evaluate_sr013_positions(request, trade_date, requested)
