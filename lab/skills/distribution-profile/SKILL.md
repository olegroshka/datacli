---
name: distribution-profile
summary: Distribution, missingness and outlier profile for a dataset's numeric fields.
inputs: [dataset, lane]
tier: cheap
---

Goal
Profile the numeric columns of a dataset so the researcher knows their shape,
missingness, and outlier exposure BEFORE any feature is built.

Approach (read-only SQL; adapt to the dataset)
- Row count, distinct pairs, date range.
- Per numeric column: count, count of NULLs, min, approx median (quantile), max,
  and a count of non-positive or zero values where that would be suspicious
  (e.g. prices <= 0, volume = 0).
    SELECT count(*) AS n,
           count(*) FILTER (WHERE close IS NULL) AS null_close,
           min(close) AS lo, quantile_cont(close, 0.5) AS med, max(close) AS hi,
           count(*) FILTER (WHERE close <= 0) AS nonpos_close
    FROM prices WHERE lane = '<lane>'
- Note heavy skew or fat tails (compare median to max) as winsorization candidates.

Output
A compact per-column profile (n, nulls, min/median/max, suspicious counts) and a
short note on which fields would need cleaning/winsorizing. Label any distributional
observation that suggests a signal as a HYPOTHESIS to test in btest.
