"""SEC bulk datasets (docs/samrt-money-flow-dataset-initiative, WP7).

``form13f`` stores the SEC's Form 13F data sets as published: one parquet file
per archive and table, every column a string. Typing, units and amendment
handling live in the views. The SEC's fails-to-deliver files predate this
package and stay under ``finra/sec.py`` (ADR-001 D2).
"""

import finra  # noqa: F401  (puts eodhd/ on sys.path for the shared helpers)
