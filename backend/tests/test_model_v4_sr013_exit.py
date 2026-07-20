from __future__ import annotations

import pandas as pd

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
