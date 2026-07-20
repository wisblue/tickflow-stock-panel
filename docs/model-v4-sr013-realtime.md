# Model-v4 SR013 ACT5 realtime sell monitor

The global position ticker uses:

`GET /api/model-v4/sr013-realtime/positions`

The endpoint reads `data/user_data/active_stocks.json`, keeps rows whose
`source` is `positions`, makes one TickFlow batch quote request, and reads the
current Redis transaction stream in memory. It does not persist transaction
parquet. The frontend refreshes once per minute.

## Reference price

The common rule and displayed-return reference is the official T-day close.
For today's T+1 evaluation, `quote.prev_close` (or an equivalent TickFlow
field) is preferred. If unavailable, the service uses the final exact-order
historical transaction at or before 15:00 on T day.

This differs from the fixed S152 14:45 buy-price basis used by the research
backtest, so the realtime rule is explicitly named `SR013_ACT5_TCLOSE` and the
backtest ADR must not be attributed to this changed reference without a
separate replay.

## Exit contract

- completed snapshots: `09:45, 10:00, 10:30, 11:00, 13:00, 13:30, 14:00, 14:30`;
- profit trail after 10:00: observed MFE at least 5% and giveback at least 2
  percentage points;
- catastrophe guard from 11:00: MFE below 3%, return at or below -4%, and
  price no higher than as-of VWAP;
- signal fill: first visible transaction in a later minute, capped at 14:45;
- no earlier signal: first transaction in minute 14:45;
- completed signal without a later transaction: `sell_triggered_fill_pending`.

`gross_return` and `actual_return` are both the current or realized price
divided by T-day close minus one. The response also exposes the T-close source,
signal reason, MFE, giveback, and causal completion boundary for audit.

The legacy BB endpoint remains available for compatibility, but the global
ticker does not call it.

Clicking the ticker opens the position exit detail sorted by realized sell
time. Filled exits come first, pending signals follow in signal-time order,
and holdings appear last. The dialog also renders the full rule description
returned by the API.
