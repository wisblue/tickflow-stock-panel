from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import pytest

from app.services import model_v4_sr013_exit as sr013


def _path(points: list[tuple[int, int, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "stock_code": "000001",
                "time_hhmm": hhmm,
                "time_hhmmss": hhmmss,
                "price": price,
                "vol": volume,
                "chrono_row_in_symbol": index,
            }
            for index, (hhmm, hhmmss, price, volume) in enumerate(points)
        ]
    )


def test_act5_waits_for_five_percent_activation() -> None:
    day = _path(
        [
            (925, 92500, 100.0, 10),
            (1000, 100059, 104.5, 10),
            (1030, 103059, 102.0, 10),
            (1031, 103100, 101.9, 10),
        ]
    )
    result = sr013._evaluate_rule(
        day, reference_price=100.0, current_hhmm=1031, completed_through_hhmm=1030
    )
    assert result["status"] == "holding"


def test_act5_profit_trail_uses_first_later_transaction() -> None:
    day = _path(
        [
            (925, 92500, 100.0, 10),
            (1000, 100059, 106.0, 10),
            (1030, 103059, 103.5, 10),
            (1031, 103101, 103.4, 10),
            (1031, 103102, 103.1, 10),
        ]
    )
    result = sr013._evaluate_rule(
        day, reference_price=100.0, current_hhmm=1031, completed_through_hhmm=1030
    )
    assert result["status"] == "sell_triggered"
    assert result["sell_reason"] == "sr013_act5_profit_trail"
    assert result["sell_time_hhmmss"] == 103101
    assert result["sell_price"] == 103.4


def test_catastrophe_guard_starts_at_1100() -> None:
    day = _path(
        [
            (925, 92500, 100.0, 10),
            (1000, 100059, 101.0, 10),
            (1030, 103059, 96.0, 10),
            (1100, 110059, 95.0, 10),
            (1101, 110101, 94.9, 10),
        ]
    )
    result = sr013._evaluate_rule(
        day, reference_price=100.0, current_hhmm=1101, completed_through_hhmm=1100
    )
    assert result["status"] == "sell_triggered"
    assert result["sell_reason"] == "confirmed_catastrophe_guard"
    assert result["sell_time_hhmmss"] == 110101
    assert result["sell_price"] == 94.9


def test_fallback_is_first_transaction_in_1445() -> None:
    day = _path(
        [
            (925, 92500, 100.0, 10),
            (1430, 143059, 101.0, 10),
            (1444, 144459, 101.2, 10),
            (1445, 144500, 101.3, 10),
            (1445, 144501, 101.1, 10),
        ]
    )
    result = sr013._evaluate_rule(
        day, reference_price=100.0, current_hhmm=1445, completed_through_hhmm=1444
    )
    assert result["status"] == "sell_triggered"
    assert result["sell_reason"] == "forced_first_fill_1445"
    assert result["sell_time_hhmmss"] == 144500
    assert result["sell_price"] == 101.3


def test_completed_signal_without_later_fill_is_pending() -> None:
    day = _path(
        [
            (925, 92500, 100.0, 10),
            (1000, 100059, 106.0, 10),
            (1030, 103059, 103.5, 10),
        ]
    )
    result = sr013._evaluate_rule(
        day, reference_price=100.0, current_hhmm=1031, completed_through_hhmm=1030
    )
    assert result["status"] == "sell_triggered_fill_pending"
    assert result["sell_reason"] == "sr013_act5_profit_trail"
    assert result["sell_price"] is None


def test_quote_previous_close_is_preferred_for_today(monkeypatch) -> None:
    today = sr013.datetime.now(sr013.TZ_SHANGHAI).strftime("%Y%m%d")
    monkeypatch.setattr(sr013, "_previous_date", lambda _: "20260720")
    value, source, entry_date, meta = sr013._t_close_reference(
        trade_date=today,
        symbol="000001",
        quote={"prev_close": 12.34},
    )
    assert value == 12.34
    assert source == "tickflow_quote.prev_close"
    assert entry_date == "20260720"
    assert meta["price_field"] == "prev_close"


def test_displayed_actual_return_deducts_both_side_fees(monkeypatch) -> None:
    monkeypatch.setattr(
        sr013,
        "_t_close_reference",
        lambda **_: (100.0, "test_t_close", "20260720", {}),
    )
    day = _path(
        [
            (925, 92500, 100.0, 10),
            (1000, 100059, 106.0, 10),
            (1030, 103059, 103.5, 10),
            (1031, 103101, 103.4, 10),
        ]
    )
    result = sr013._evaluate_one(
        symbol="000001",
        name="平安银行",
        trade_date="20260721",
        current=day,
        current_source="test",
        quote={"price": 103.4},
        current_hhmm=1031,
        completed_through_hhmm=1030,
    )
    expected_net = 103.4 * (1.0 - sr013.FEE_RATE_PER_SIDE) / (
        100.0 * (1.0 + sr013.FEE_RATE_PER_SIDE)
    ) - 1.0
    assert result["gross_return"] == pytest.approx(0.034)
    assert result["actual_return"] == pytest.approx(expected_net)
    assert result["actual_return"] < result["gross_return"]
    assert "下一可见分钟首笔成交" in result["sell_reason_description"]


def test_redis_batch_uses_pipeline_and_rejects_other_dates(monkeypatch) -> None:
    target_date = "20260721"
    target_ts = datetime(2026, 7, 21, 9, 30, tzinfo=sr013.TZ_SHANGHAI).timestamp()
    stale_ts = datetime(2026, 7, 20, 14, 45, tzinfo=sr013.TZ_SHANGHAI).timestamp()

    class FakePipeline:
        def __init__(self) -> None:
            self.keys: list[str] = []

        def exists(self, key: str) -> None:
            self.keys.append(key)

        def execute(self) -> list[int]:
            return [0, 1]

    class FakeRedis:
        def __init__(self) -> None:
            self.pipeline_instance = FakePipeline()
            self.mget_keys: list[str] = []

        def pipeline(self, transaction: bool = False) -> FakePipeline:
            assert transaction is False
            return self.pipeline_instance

        def mget(self, keys: list[str]) -> list[str]:
            self.mget_keys = keys
            return [
                json.dumps({"symbol": "000001", "timestamp": stale_ts, "price": 10, "vol": 1}),
                json.dumps({"symbol": "600000", "timestamp": target_ts, "price": 11, "vol": 2}),
            ]

    monkeypatch.setattr(sr013.settings, "tdx_redis_key_prefix", "tdx:trans")
    client = FakeRedis()
    got = sr013._redis_rows_batch(client, target_date, ["000001", "600000"])
    assert client.mget_keys == ["tdx:trans:000001", "tdx:trans:20260721:600000"]
    assert got["000001"].empty
    assert len(got["600000"]) == 1
    assert int(got["600000"].iloc[0]["time_hhmm"]) == 930


def test_redis_rows_preserve_distinct_same_minute_trades() -> None:
    target_ts = datetime(2026, 7, 21, 9, 30, tzinfo=sr013.TZ_SHANGHAI).timestamp()
    first = {
        "symbol": "600000",
        "timestamp": target_ts,
        "price": 11,
        "vol": 2,
        "num": 1,
        "buy_or_sell": 0,
        "action": "BUY",
    }
    second = {**first, "num": 2, "buy_or_sell": 1, "action": "SELL"}
    raw = "\n".join([json.dumps(first), json.dumps(first), json.dumps(second)])

    got = sr013._parse_redis_rows(raw, "20260721", "600000")

    assert len(got) == 2
    assert got["trade_count"].tolist() == [1, 2]
    assert got["bs_flag"].tolist() == [0, 1]


def test_previous_date_prefers_s152_strict_basis(tmp_path, monkeypatch) -> None:
    root = tmp_path / "s152"
    state = root / "20260721" / "pipeline_state.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps({"trade_date": "20260721", "basis_date": "20260720"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(sr013, "S152_PIPELINE_ROOT", root)
    monkeypatch.setattr(sr013, "_transaction_dates", lambda: ("20260717",))
    assert sr013._previous_date("20260721") == "20260720"


def test_transaction_dates_refresh_when_new_daily_file_arrives(tmp_path, monkeypatch) -> None:
    root = tmp_path / "transactions"
    first = root / "2026" / "07" / "17.parquet"
    first.parent.mkdir(parents=True)
    first.touch()
    monkeypatch.setattr(sr013, "TX_ROOT", root)

    assert sr013._transaction_dates() == ("20260717",)

    (first.parent / "20.parquet").touch()
    assert sr013._transaction_dates() == ("20260717", "20260720")


def test_historical_close_map_reads_all_positions_once_and_invalidates(tmp_path, monkeypatch) -> None:
    root = tmp_path / "transactions"
    path = root / "2026" / "07" / "20.parquet"
    path.parent.mkdir(parents=True)
    frame = pd.DataFrame(
        [
            {"trade_date": 20260720, "stock_code": "000001", "time_hhmm": 1459, "time_hhmmss": 145959, "price": 10.0, "chrono_row_in_symbol": 0},
            {"trade_date": 20260720, "stock_code": "000001", "time_hhmm": 1500, "time_hhmmss": 150001, "price": 10.2, "chrono_row_in_symbol": 1},
            {"trade_date": 20260720, "stock_code": "600000", "time_hhmm": 1500, "time_hhmmss": 150000, "price": 12.3, "chrono_row_in_symbol": 0},
            {"trade_date": 20260717, "stock_code": "600000", "time_hhmm": 1500, "time_hhmmss": 150000, "price": 99.0, "chrono_row_in_symbol": 0},
        ]
    )
    frame.to_parquet(path, index=False)
    monkeypatch.setattr(sr013, "TX_ROOT", root)
    sr013._load_historical_close_map_cached.cache_clear()

    got = sr013._load_historical_close_map("20260720", ["600000", "000001"])

    assert got["000001"]["price"] == pytest.approx(10.2)
    assert got["000001"]["meta"]["price_time_hhmmss"] == 150001
    assert got["600000"]["price"] == pytest.approx(12.3)

    frame.loc[
        (frame["trade_date"] == 20260720) & (frame["stock_code"] == "600000"),
        "price",
    ] = 12.5
    frame.to_parquet(path, index=False)
    refreshed = sr013._load_historical_close_map("20260720", ["600000", "000001"])
    assert refreshed["600000"]["price"] == pytest.approx(12.5)
