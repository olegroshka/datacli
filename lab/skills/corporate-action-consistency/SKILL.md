---
name: corporate-action-consistency
summary: Cross-check dividends/splits against prices and their state sidecars.
inputs: [lane]
tier: mid
---

Goal
Check that corporate-action data is internally consistent -- a common source of
silent errors that corrupt returns and adjusted prices downstream.

Approach (read-only SQL)
- Multiple dividends on one ex-date are legitimate; confirm they are preserved
  (do NOT assume one row per ex-date), e.g. count rows vs. distinct
  (ticker, exchange, ex_date).
- State-vs-rows agreement: tickers whose dividends_state.status = 'empty' but which
  have rows in dividends (and the reverse).
- Split sanity: split_factor / numerator / denominator present and > 0.
- Optional: for a split ex-date, note whether the raw price shows an unadjusted
  jump (a large one-day move) -- a hint that adjusted_close may be stale.

Output
A list of specific (ticker, exchange, ex_date) inconsistencies with the exact
figures, grouped by issue type, and the recommended remediation. Never assert a
jump you did not measure with a query.
