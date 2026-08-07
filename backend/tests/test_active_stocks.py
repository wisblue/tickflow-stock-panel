from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from app.services import active_stocks


def _use_temp_store(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(active_stocks, "_dir", lambda: tmp_path)


def test_positions_source_survives_stock_analysis_registration(tmp_path, monkeypatch) -> None:
    _use_temp_store(tmp_path, monkeypatch)

    active_stocks.add("300166", "东方国信", "positions")
    rows = active_stocks.add("300166", source="stock-analysis")

    assert len(rows) == 1
    assert rows[0]["source"] == "positions"
    assert rows[0]["sources"] == ["positions", "stock-analysis"]
    assert rows[0]["name"] == "东方国信"


def test_sync_source_is_exact_without_removing_other_sources(tmp_path, monkeypatch) -> None:
    _use_temp_store(tmp_path, monkeypatch)
    active_stocks.add("000001", source="positions")
    active_stocks.add("000001", source="stock-analysis")
    active_stocks.add("600000", source="positions")
    active_stocks.add("300166", source="watchlist")

    rows = active_stocks.sync_source(["000001", "300166"], "positions")
    by_symbol = {row["symbol"]: row for row in rows}

    assert "600000" not in by_symbol
    assert by_symbol["000001"]["sources"] == ["positions", "stock-analysis"]
    assert set(by_symbol["300166"]["sources"]) == {"watchlist", "positions"}
    assert by_symbol["300166"]["source"] == "positions"

    rows = active_stocks.sync_source([], "positions")
    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["000001"]["source"] == "stock-analysis"
    assert by_symbol["300166"]["source"] == "watchlist"
    assert all(not active_stocks.has_source(row, "positions") for row in rows)


def test_concurrent_adds_do_not_lose_or_corrupt_rows(tmp_path, monkeypatch) -> None:
    _use_temp_store(tmp_path, monkeypatch)
    symbols = [f"{index:06d}" for index in range(1, 41)]

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda symbol: active_stocks.add(symbol, source="positions"), symbols))

    rows = active_stocks.list_symbols()
    assert {row["symbol"] for row in rows} == set(symbols)
    persisted = json.loads((tmp_path / "active_stocks.json").read_text(encoding="utf-8"))
    assert len(persisted) == len(symbols)
