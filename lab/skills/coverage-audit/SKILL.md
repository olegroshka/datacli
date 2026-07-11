---
name: coverage-audit
summary: Per (lane, dataset) coverage windows, freshness, and pair-count mismatches.
inputs: [lane]
tier: cheap
---

Goal
Assess how completely each dataset covers its universe and how fresh it is, and
surface the worst gaps.

Approach (write your own read-only SQL; these are starting points)
- Coverage ceiling and freshness per lane from the state sidecars, e.g.:
    SELECT lane, max(coverage_through) AS through, max(latest_data_date) AS last_bar
    FROM prices_state GROUP BY lane
- Pair counts to spot mismatches (universe vs. state vs. output), e.g. distinct
  (ticker, exchange) in each of prices vs. prices_state.
- Status breakdowns: SELECT lane, status, count(*) FROM dividends_state GROUP BY 1,2

Output
The worst-covered (lane, dataset) pairs with their coverage_through / last_bar and
any state-vs-output mismatch, plus one line naming the single most urgent gap and
the action to take (targeted_rerun vs full_refresh).
