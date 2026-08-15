"""The vendor's per-article sentiment as a scoring backend (the free baseline).

Fills only the fields it can honestly derive: ``sentiment`` from EODHD's
VADER-style ``polarity`` (already in [-1, 1]) and ``confidence`` from how far the
score sits from neutral. Everything else stays ``None``. Useful for agreement
checks against every other backend, and it costs nothing.
"""

from __future__ import annotations

import time

from scoring.backends.base import Estimate, Item, Result
from scoring.schema import Schema


class VendorBackend:
    id = "vendor"
    kind = "record"
    model = "eodhd-vader"

    def estimate(self, items: list[Item], schema: Schema) -> Estimate:
        return Estimate(n_items=len(items), seconds=0.0, cost_usd=0.0, note="free")

    def score(self, items: list[Item], schema: Schema) -> list[Result]:
        names = set(schema.field_names())
        out: list[Result] = []
        for it in items:
            t0 = time.perf_counter()
            article: dict = {}
            if it.vendor_polarity is None:
                out.append(
                    Result(
                        it.article_id,
                        it.date,
                        "skipped",
                        model=self.model,
                        error="no vendor polarity",
                    )
                )
                continue
            pol = max(-1.0, min(1.0, float(it.vendor_polarity)))
            if "sentiment" in names:
                article["sentiment"] = pol
            if "confidence" in names:
                article["confidence"] = round(abs(pol), 4)
            symbols = {s: {} for s in it.target_symbols} if schema.per_symbol else {}
            out.append(
                Result(
                    it.article_id,
                    it.date,
                    "ok",
                    article=article,
                    symbols=symbols,
                    model=self.model,
                    prompt_hash="vendor",
                    seconds=time.perf_counter() - t0,
                )
            )
        return out
