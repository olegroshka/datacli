"""Versioned schema for the EODHD datasets.

The tool's source of truth for the columns it relies on, so queries stay stable as
the data evolves. ``SCHEMA_VERSION`` bumps whenever the canonical columns change.

Robustness to evolution comes from two things:
- **Projected views** (:func:`projection`): each dataset view guarantees the
  canonical columns exist -- aliasing a known rename or NULL-filling a missing one
  -- while passing every other column through untouched. So data written under an
  older (or newer) schema still queries cleanly.
- The **``schema``** command diffs this declared schema against the on-disk data.

Only the columns the verbs/queries actually reference are declared here; the many
extra fundamentals fields ride along via ``SELECT *``.
"""

from __future__ import annotations

SCHEMA_VERSION = 4  # v2: + news; v3: + news_daily; v4: + issuer_map, news_issuer_daily

# canonical column -> role, per dataset
SCHEMAS: dict[str, dict[str, str]] = {
    "prices": {
        "ticker": "key",
        "exchange": "key",
        "date": "date",
        "open": "value",
        "high": "value",
        "low": "value",
        "close": "value",
        "adjusted_close": "value",
        "volume": "value",
    },
    "dividends": {
        "ticker": "key",
        "exchange": "key",
        "ex_date": "date",
        "dividend": "value",
        "unadjusted_dividend": "value",
        "currency": "value",
        "declaration_date": "value",
        "record_date": "value",
        "payment_date": "value",
        "period": "value",
    },
    "splits": {
        "ticker": "key",
        "exchange": "key",
        "ex_date": "date",
        "split_factor": "value",
        "numerator": "value",
        "denominator": "value",
        "split_ratio": "value",
    },
    "fundamentals": {
        "ticker": "key",
        "exchange": "key",
        "statement": "key",
        "date": "date",
        "filing_date": "value",
        "currency": "value",
    },
    # Derived daily panel from the news corpus: one row per (date, ticker,
    # exchange); share_of_day is the volume feature (normalised by the day's
    # global article count), polarity_* are vendor-sentiment aggregates.
    "news_daily": {
        "ticker": "key",
        "exchange": "key",
        "date": "date",
        "symbol": "value",
        "n_articles": "value",
        "n_articles_day": "value",
        "share_of_day": "value",
        "n_sources": "value",
        "n_solo": "value",
        "polarity_mean": "value",
        "polarity_std": "value",
        "pos_share": "value",
        "neg_share": "value",
        "first_published_at": "value",
        "last_published_at": "value",
    },
    # Symbol line -> issuer (vendor LEI/ISIN/PrimaryTicker/Listings + corpus
    # co-tagging). Not ticker-keyed (one row per symbol string).
    "issuer_map": {
        "symbol": "key",
        "ticker": "value",
        "exchange": "value",
        "issuer_id": "value",
        "issuer_name": "value",
        "primary_symbol": "value",
        "evidence": "value",
        "cotag_p": "value",
        "n_articles": "value",
        "lane": "value",
        "in_universe": "value",
    },
    # Issuer-grain daily news panel replicated onto every covered member ticker.
    "news_issuer_daily": {
        "ticker": "key",
        "exchange": "key",
        "date": "date",
        "issuer_id": "value",
        "issuer_name": "value",
        "primary_symbol": "value",
        "n_articles": "value",
        "n_articles_day": "value",
        "share_of_day": "value",
        "n_sources": "value",
        "n_solo": "value",
        "n_symbols": "value",
        "polarity_mean": "value",
        "polarity_std": "value",
        "pos_share": "value",
        "neg_share": "value",
        "first_published_at": "value",
        "last_published_at": "value",
    },
    # Article-level corpus (news lane); ``symbols``/``tags`` are list<string>
    # columns -- unnest them for symbol- or tag-level views.
    "news": {
        "article_id": "key",
        "date": "date",
        "published_at": "value",
        "title": "value",
        "content": "value",
        "link": "value",
        "source": "value",
        "symbols": "value",
        "tags": "value",
        "polarity": "value",
        "neg": "value",
        "neu": "value",
        "pos": "value",
    },
}

# Migration hook: {dataset: {old_column: canonical_column}}. When the on-disk data
# still uses an old name, the projected view aliases it to the canonical name.
# Empty at v1 -- this is the extension point for future renames.
RENAMES: dict[str, dict[str, str]] = {}


def datasets() -> tuple[str, ...]:
    return tuple(SCHEMAS.keys())


def canonical_columns(dataset: str) -> list[str]:
    return list(SCHEMAS.get(dataset, {}).keys())


def projection(dataset: str, available: list[str], lane: str) -> str:
    """SELECT-list that guarantees the canonical columns (rename-aliased or NULL)
    while passing through everything else, plus a ``lane`` literal.

    Returns the part between ``SELECT`` and ``FROM`` (caller appends the source).
    """
    avail = {c.lower() for c in available}
    renames = RENAMES.get(dataset, {})
    extras: list[str] = []
    for col in canonical_columns(dataset):
        if col.lower() in avail:
            continue
        src = next(
            (
                old
                for old, new in renames.items()
                if new == col and old.lower() in avail
            ),
            None,
        )
        extras.append(f'{src} AS "{col}"' if src else f'NULL AS "{col}"')
    lane_lit = lane.replace("'", "''")
    tail = (", " + ", ".join(extras)) if extras else ""
    return f"*{tail}, '{lane_lit}' AS lane"


def diff(dataset: str, available: list[str]) -> dict[str, list[str]]:
    """Compare declared canonical columns against on-disk columns."""
    avail = {c.lower() for c in available}
    canon = canonical_columns(dataset)
    canon_lower = {c.lower() for c in canon}
    missing = [c for c in canon if c.lower() not in avail]
    extra = [c for c in available if c.lower() not in canon_lower]
    present = [c for c in canon if c.lower() in avail]
    return {"present": present, "missing": missing, "extra": extra}
