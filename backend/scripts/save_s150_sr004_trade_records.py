#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.api.backtest import (  # noqa: E402
    S150_STATE_FILE,
    S150_TRADE_RECORD_ROOT,
    _load_s150_state,
    _s150_date,
    _s150_trade_record_path,
    _s150_trade_rows,
)


def json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_ready(v) for v in value]
    if isinstance(value, tuple):
        return [json_ready(v) for v in value]
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Save the S150-SR004 20-trade ledger for a trade date.")
    parser.add_argument("--trade-date", default="", help="YYYYMMDD; defaults to Asia/Shanghai today.")
    parser.add_argument("--limit", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    trade_date = _s150_date(args.trade_date) or now.strftime("%Y%m%d")
    state = _load_s150_state()
    rows = _s150_trade_rows(state, {}, limit=int(args.limit), before_date=trade_date)
    settled = [row.get("day_return") for row in rows if row.get("day_return") is not None]
    payload = {
        "artifact_type": "tickflow_panel.s150_sr004_trade_records",
        "generated_at": now.isoformat(timespec="seconds"),
        "trade_date": trade_date,
        "limit": int(args.limit),
        "state_file": str(S150_STATE_FILE),
        "trade_count": len(rows),
        "settled_trade_count": len(settled),
        "avg_day_return": sum(float(x) for x in settled) / len(settled) if settled else None,
        "trades": rows,
    }
    S150_TRADE_RECORD_ROOT.mkdir(parents=True, exist_ok=True)
    out = _s150_trade_record_path(trade_date)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(json_ready(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(out)
    print(f"[save-s150-trade-records] wrote {out} rows={len(rows)} settled={len(settled)}", flush=True)


if __name__ == "__main__":
    main()
