# Model-v4 BB20 realtime sell monitor

The panel keeps the model-v4 backtest under the prediction workspace and adds
an independent realtime monitor at:

`GET /api/model-v4/bb-realtime/positions`

The endpoint reads `data/user_data/active_stocks.json`, keeping rows whose
`source` is `positions`.  It makes one TickFlow quote batch request per
refresh, and reads the Redis transaction stream for the same symbols.  The
frontend refreshes every 60 seconds and renders the result in the global
top-of-content ticker, so the bar remains visible across routes.

The exit contract is:

- previous regular session warm-up;
- `BB(20, 2)` on one-minute closes;
- middle-line slope strictly below `0.15%`;
- close at least `0.05%` below the middle line;
- sell price is the first transaction in the minute after a completed signal
  bar;
- a limit-up trigger is valid only if that next-minute first transaction is
  still at the limit-up price; otherwise the trigger is discarded and a later
  re-trigger is evaluated.

`gross_return` and `actual_return` are both measured from the current day's
regular-session open, not from the position buy price.  Before a sell fill the
latest quote is used as the mark; after a sell fill the first transaction of the
next minute is used.  The open is taken from `quote.open`, with the first Redis
transaction at or after 09:30 as a fallback.  `open_price` and
`open_price_source` are returned for auditability.  `actual_return` is currently
the realized open-to-sell gross result; transaction fees are not applied.
