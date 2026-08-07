# Model-v4 SR013 ACT5 realtime sell monitor

The global position ticker uses:

`GET /api/model-v4/sr013-realtime/positions`

The global ticker sends the browser's `positions-list` symbols explicitly,
makes one TickFlow batch quote request, and reads the current Redis transaction
stream in memory. The backend falls back to the shared active-stock set only
for older API callers that omit `symbols`. It does not persist transaction
parquet. The frontend refreshes once per minute.

The same browser list is exactly mirrored to the `positions` membership in
`data/user_data/active_stocks.json` for go-fetcher. Active rows support multiple
sources, so opening a held stock in stock analysis cannot overwrite its
`positions` membership. Updates use a process lock and atomic file replacement
to prevent concurrent page requests from truncating or losing rows.

The top scrolling ticker intentionally filters to `sell_triggered` and
`sell_triggered_fill_pending` rows only. Ordinary `holding` rows remain in the
click-through detail dialog but do not occupy the alert ticker.

## Reference price

The common rule and displayed-return reference is the official T-day close.
For today's T+1 evaluation, `quote.prev_close` (or an equivalent TickFlow
field) is preferred. If unavailable, the service uses the final exact-order
historical transaction at or before 15:00 on T day.

The strict T-1 date is resolved once per API request, preferring the current
S152 pipeline `basis_date`; the filesystem fallback is re-enumerated instead
of being cached across trading days. Missing quote close fields trigger one
Polars scan for all position symbols, not one full-parquet scan per stock. The
result cache key includes the historical file size and nanosecond mtime, so a
replaced canonical file automatically invalidates the cache.

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

`gross_return` is the current or realized price divided by T-day close minus
one. `actual_return` deducts the configured 0.05% fee on both the T-close buy
and the displayed/realized sell. The response also exposes the T-close source,
signal reason and text, MFE, giveback, and causal completion boundary for audit.

All position Redis keys are read with one `EXISTS` pipeline plus one `MGET` per
refresh. Rows whose epoch timestamp is not on the requested Shanghai trade
date are rejected in memory. Repeated realtime batches are deduplicated using
timestamp, price, volume, trade count, buy/sell flag, and action; distinct
same-minute trades with different direction or trade count remain visible.

The legacy BB endpoint remains available for compatibility, but the global
ticker does not call it.

Clicking the ticker opens the position exit detail sorted by realized sell
time. Filled exits come first, pending signals follow in signal-time order,
and holdings appear last. The dialog also renders the full rule description
returned by the API.
