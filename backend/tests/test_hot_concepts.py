from __future__ import annotations

from datetime import date

import polars as pl

from app.api import hot_concepts


class _QuoteService:
    def __init__(self, rows: list[dict], quote_date: date) -> None:
        self._frame = pl.DataFrame(rows)
        self._date = quote_date

    def get_enriched_today(self):
        return self._frame, self._date

    def get_name_map(self, symbols: list[str] | None = None) -> dict[str, str]:
        names = {"600000.SH": "浦发银行"}
        return names if symbols is None else {symbol: names[symbol] for symbol in symbols if symbol in names}


def test_build_treemap_uses_current_realtime_snapshot(monkeypatch):
    today = date.today()
    service = _QuoteService(
        [
            {
                "symbol": "600000.SH",
                "close": 11.0,
                "prev_close": 10.0,
            },
            *[
                {
                    "symbol": f"0000{code:02d}.SZ",
                    "close": 10.0,
                    "prev_close": 10.0,
                }
                for code in range(1, 11)
            ],
        ],
        today,
    )
    events: list[tuple[str, int, str]] = []
    monkeypatch.setattr(
        hot_concepts,
        "_build_treemap",
        lambda frame, on_progress: [
            {"name": "测试概念", "value": len(frame), "children": []}
        ],
    )
    monkeypatch.setattr(
        hot_concepts,
        "_read_latest_prices_from_parquet",
        lambda _trade_date: (_ for _ in ()).throw(AssertionError("must not read parquet")),
    )

    result = hot_concepts.build_treemap_data(
        quote_service=service,
        on_progress=lambda stage, progress, message: events.append((stage, progress, message)),
    )

    assert result["trade_date"] == today.strftime("%Y%m%d")
    assert result["source"] == "realtime_quotes"
    assert result["unique_stocks"] == 1
    assert result["warning"] is None
    assert [stage for stage, _, _ in events] == ["prepare", "realtime", "detect", "concepts", "finalize"]


def test_empty_current_snapshot_does_not_fall_back_to_yesterday(monkeypatch):
    today = date.today()
    service = _QuoteService(
        [
            {
                "symbol": f"0000{code:02d}.SZ",
                "close": 10.0,
                "prev_close": 10.0,
            }
            for code in range(1, 12)
        ],
        today,
    )
    monkeypatch.setattr(
        hot_concepts,
        "_read_latest_prices_from_parquet",
        lambda _trade_date: (_ for _ in ()).throw(AssertionError("must not read parquet")),
    )

    result = hot_concepts.build_treemap_data(quote_service=service)

    assert result["trade_date"] == today.strftime("%Y%m%d")
    assert result["source"] == "realtime_quotes"
    assert result["unique_stocks"] == 0
    assert result["treemap_data"] == []


def test_treemap_keeps_only_top_ten_concepts(monkeypatch):
    limit_ups = pl.DataFrame({
        "股票代码": ["600000"],
        "股票名称": ["浦发银行"],
        "涨跌幅": [10.0],
    }).to_pandas()
    concepts = pl.DataFrame({
        "ths_concept": [f"概念{i:02d}" for i in range(12)],
        "code": ["600000"] * 12,
        "cnpt_code": [f"886{i:03d}" for i in range(12)],
    }).to_pandas()
    monkeypatch.setattr(hot_concepts, "_load_ths_members", lambda: concepts)

    result = hot_concepts._build_treemap(limit_ups)

    assert len(result) == 10
    assert all(
        item["children"] == [{"name": "浦发银行", "code": "600000", "value": 1}]
        for item in result
    )
