"""Sidecar storage for scores and embeddings, plus per-day run state.

Layout under the data root::

    news/scores/<schema>@<v>/<backend-id>/YYYY-MM-DD.parquet   # one row per (article_id, symbol)
    news/scores/<schema>@<v>/<backend-id>/state.csv            # one row per day
    news/embeddings/<model-id>/YYYY-MM-DD.parquet              # one row per article_id
    news/embeddings/<model-id>/state.csv

Score rows are *wide per schema version*: every article-level field is a column,
every symbol-level field is a column, ``symbol`` is NULL on the article-level
row and set on the per-symbol rows (which repeat the article-level values so a
``WHERE symbol = 'AAPL.US'`` needs no join). Fixed provenance columns follow.
Partitions are upserted on ``(article_id, symbol)``; the raw corpus is never
written.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from scoring.backends.base import Result


def _write_table_atomic(table: pa.Table, path: Path, **kwargs: Any) -> None:
    """Write via ``<name>.tmp`` + ``os.replace`` so readers never see a torn file."""
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp, **kwargs)
    os.replace(tmp, path)


from scoring.schema import Field, Schema

PROVENANCE_COLUMNS = [
    "status",
    "problems",
    "schema",
    "schema_version",
    "backend",
    "model",
    "prompt_hash",
    "temperature",
    "prompt_tokens",
    "completion_tokens",
    "cost_usd",
    "cached",
    "seconds",
    "scored_at",
]

STATE_COLUMNS = [
    "date",
    "status",
    "n_target",
    "n_ok",
    "n_invalid",
    "n_error",
    "n_skipped",
    "seconds",
    "cost_usd",
    "model",
    "prompt_hash",
    "scored_at",
    "detail",
]


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #
def scores_root(data_root: Path) -> Path:
    return data_root / "news" / "scores"


def embeddings_root(data_root: Path) -> Path:
    return data_root / "news" / "embeddings"


def sidecar_dir(data_root: Path, schema: Schema, backend_id: str, kind: str) -> Path:
    if kind == "vector":
        return embeddings_root(data_root) / backend_id
    return scores_root(data_root) / schema.key / backend_id


def partition_path(directory: Path, day: str) -> Path:
    return directory / f"{day}.parquet"


def state_path(directory: Path) -> Path:
    return directory / "state.csv"


# --------------------------------------------------------------------------- #
# arrow schema for score sidecars
# --------------------------------------------------------------------------- #
def _arrow_type(f: Field) -> pa.DataType:
    return {
        "float": pa.float64(),
        "int": pa.int64(),
        "bool": pa.bool_(),
        "string": pa.string(),
        "enum": pa.string(),
    }[f.type]


def score_arrow_schema(schema: Schema) -> pa.Schema:
    cols: list[tuple[str, pa.DataType]] = [
        ("article_id", pa.string()),
        ("date", pa.date32()),
        ("symbol", pa.string()),
    ]
    for f in schema.fields:
        cols.append((f.name, _arrow_type(f)))
        if f.derived_numeric:
            cols.append((f.derived_numeric, pa.float64()))
    for f in schema.symbol_fields:
        cols.append((f.name, _arrow_type(f)))
        if f.derived_numeric:
            cols.append((f.derived_numeric, pa.float64()))
    cols += [
        ("status", pa.string()),
        ("problems", pa.string()),
        ("schema", pa.string()),
        ("schema_version", pa.int32()),
        ("backend", pa.string()),
        ("model", pa.string()),
        ("prompt_hash", pa.string()),
        ("temperature", pa.float64()),
        ("prompt_tokens", pa.int64()),
        ("completion_tokens", pa.int64()),
        ("cost_usd", pa.float64()),
        ("cached", pa.bool_()),
        ("seconds", pa.float64()),
        ("scored_at", pa.timestamp("us", tz="UTC")),
    ]
    return pa.schema(cols)


VECTOR_SCHEMA_COLUMNS = [
    "article_id",
    "date",
    "vector",
    "dims",
    "model",
    "prompt_hash",
    "status",
    "seconds",
    "cached",
    "scored_at",
]


def vector_arrow_schema() -> pa.Schema:
    return pa.schema(
        [
            ("article_id", pa.string()),
            ("date", pa.date32()),
            ("vector", pa.list_(pa.float32())),
            ("dims", pa.int32()),
            ("model", pa.string()),
            ("prompt_hash", pa.string()),
            ("status", pa.string()),
            ("seconds", pa.float64()),
            ("cached", pa.bool_()),
            ("scored_at", pa.timestamp("us", tz="UTC")),
        ]
    )


# --------------------------------------------------------------------------- #
# results -> frames
# --------------------------------------------------------------------------- #
def results_to_frame(
    results: list[Result], schema: Schema, backend_id: str, scored_at: str
) -> pd.DataFrame:
    """Score results -> wide rows (article-level + per-symbol)."""
    rows: list[dict[str, Any]] = []
    for r in results:
        if r.status == "skipped":
            continue
        article_cols: dict[str, Any] = {}
        for f in schema.fields:
            value = r.article.get(f.name)
            article_cols[f.name] = value
            if f.derived_numeric:
                article_cols[f.derived_numeric] = f.to_number(value)
        symbol_blanks: dict[str, Any] = {}
        for f in schema.symbol_fields:
            symbol_blanks[f.name] = None
            if f.derived_numeric:
                symbol_blanks[f.derived_numeric] = None
        base = {
            "article_id": r.article_id,
            "date": r.date,
            "symbol": None,
            **article_cols,
            **symbol_blanks,
            "status": r.status,
            "problems": "; ".join(r.problems) if r.problems else (r.error or None),
            "schema": schema.name,
            "schema_version": schema.version,
            "backend": backend_id,
            "model": r.model,
            "prompt_hash": r.prompt_hash,
            "temperature": r.temperature,
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "cost_usd": r.cost_usd,
            "cached": bool(r.cached),
            "seconds": r.seconds,
            "scored_at": scored_at,
        }
        rows.append(base)
        for sym, rec in (r.symbols or {}).items():
            row = dict(base)
            row["symbol"] = sym
            for f in schema.symbol_fields:
                value = rec.get(f.name)
                row[f.name] = value
                if f.derived_numeric:
                    row[f.derived_numeric] = f.to_number(value)
            rows.append(row)
    cols = [f.name for f in schema_arrow_fields(schema)]
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(rows)
    return df[cols]


def schema_arrow_fields(schema: Schema) -> list[pa.Field]:
    return list(score_arrow_schema(schema))


def vectors_to_frame(
    results: list[Result], backend_id: str, scored_at: str
) -> pd.DataFrame:
    rows = []
    for r in results:
        if r.status == "skipped":
            continue
        rows.append(
            {
                "article_id": r.article_id,
                "date": r.date,
                "vector": r.vector,
                "dims": len(r.vector) if r.vector else 0,
                "model": r.model,
                "prompt_hash": r.prompt_hash,
                "status": r.status,
                "seconds": r.seconds,
                "cached": bool(r.cached),
                "scored_at": scored_at,
            }
        )
    if not rows:
        return pd.DataFrame(columns=VECTOR_SCHEMA_COLUMNS)
    return pd.DataFrame(rows)[VECTOR_SCHEMA_COLUMNS]


# --------------------------------------------------------------------------- #
# partitions
# --------------------------------------------------------------------------- #
def _prepare(df: pd.DataFrame, arrow: pa.Schema) -> pa.Table:
    work = df.copy()
    if "date" in work.columns:
        work["date"] = pd.to_datetime(work["date"], errors="coerce").dt.date
    if "scored_at" in work.columns:
        work["scored_at"] = pd.to_datetime(work["scored_at"], utc=True, errors="coerce")
    for name in arrow.names:
        if name not in work.columns:
            work[name] = None
    work = work[arrow.names]
    return pa.Table.from_pandas(work, schema=arrow, preserve_index=False)


def read_partition(path: Path) -> pd.DataFrame | None:
    return pd.read_parquet(path) if path.exists() else None


def upsert_partition(
    df: pd.DataFrame, path: Path, arrow: pa.Schema, keys: list[str]
) -> int:
    """Merge ``df`` into the partition at ``path`` (last write wins on ``keys``).

    Returns the number of rows on disk afterwards.
    """
    existing = read_partition(path)
    if existing is not None and not existing.empty:
        merged = pd.concat([existing, df], ignore_index=True)
    else:
        merged = df
    if merged.empty:
        return 0
    merged = merged.drop_duplicates(subset=keys, keep="last")
    merged = merged.sort_values(keys).reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_table_atomic(_prepare(merged, arrow), path, compression="zstd")
    return int(len(merged))


def scored_ids(path: Path, *, ok_only: bool = True) -> set[str]:
    """Article ids already present in a partition (``ok`` rows by default)."""
    if not path.exists():
        return set()
    cols = ["article_id", "status"]
    try:
        table = pq.read_table(path, columns=cols)
    except Exception:
        return set()
    df = table.to_pandas()
    if ok_only and "status" in df.columns:
        df = df[df["status"] == "ok"]
    return set(df["article_id"].astype(str))


def partition_status_counts(path: Path) -> dict[str, int]:
    """Cumulative ``{ok, invalid, error}`` article counts on disk for a day
    (article-level rows only, i.e. ``symbol IS NULL``; vector sidecars have no
    ``symbol`` column and count every row)."""
    counts = {"ok": 0, "invalid": 0, "error": 0}
    if not path.exists():
        return counts
    try:
        names = pq.read_schema(path).names
        cols = ["article_id", "status"] + (["symbol"] if "symbol" in names else [])
        df = pq.read_table(path, columns=cols).to_pandas()
    except Exception:
        return counts
    if "symbol" in df.columns:
        df = df[df["symbol"].isna()]
    for status, n in df["status"].value_counts().items():
        if status in counts:
            counts[status] = int(n)
    return counts


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #
def read_state(directory: Path) -> pd.DataFrame | None:
    p = state_path(directory)
    return pd.read_csv(p, dtype=str) if p.exists() else None


def state_lookup(state: pd.DataFrame | None) -> dict[str, dict[str, Any]]:
    if state is None or state.empty or "date" not in state.columns:
        return {}
    return {str(r["date"]): r for r in state.to_dict(orient="records")}


def merge_state(
    existing: pd.DataFrame | None, rows: list[dict[str, Any]]
) -> pd.DataFrame:
    new = pd.DataFrame(rows, columns=STATE_COLUMNS)
    merged = (
        pd.concat([existing, new], ignore_index=True) if existing is not None else new
    )
    merged = merged.drop_duplicates(subset=["date"], keep="last")
    return merged.sort_values("date").reset_index(drop=True)


def write_state(
    directory: Path, existing: pd.DataFrame | None, rows: list[dict[str, Any]]
) -> pd.DataFrame:
    merged = merge_state(existing, rows)
    directory.mkdir(parents=True, exist_ok=True)
    merged.to_csv(state_path(directory), index=False)
    return merged


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# discovery (for `score status`)
# --------------------------------------------------------------------------- #
def discover(data_root: Path) -> list[dict[str, Any]]:
    """Every score/embedding sidecar set with its state summary."""
    out: list[dict[str, Any]] = []
    for kind, root in (
        ("record", scores_root(data_root)),
        ("vector", embeddings_root(data_root)),
    ):
        if not root.exists():
            continue
        if kind == "record":
            dirs = [
                d
                for s in sorted(root.iterdir())
                if s.is_dir()
                for d in sorted(s.iterdir())
                if d.is_dir()
            ]
        else:
            dirs = [d for d in sorted(root.iterdir()) if d.is_dir()]
        for d in dirs:
            state = read_state(d)
            parts = sorted(d.glob("*.parquet"))
            rec: dict[str, Any] = {
                "kind": kind,
                "schema": d.parent.name if kind == "record" else "-",
                "backend": d.name,
                "days": len(parts),
                "first_day": parts[0].stem if parts else None,
                "last_day": parts[-1].stem if parts else None,
                "n_ok": 0,
                "n_invalid": 0,
                "n_error": 0,
                "seconds": 0.0,
                "cost_usd": 0.0,
                "model": None,
                "last_scored_at": None,
            }
            if state is not None and not state.empty:
                num = state.apply(pd.to_numeric, errors="coerce")
                for col in ("n_ok", "n_invalid", "n_error", "seconds", "cost_usd"):
                    if col in num.columns:
                        rec[col] = float(num[col].fillna(0).sum())
                rec["n_ok"] = int(rec["n_ok"])
                rec["n_invalid"] = int(rec["n_invalid"])
                rec["n_error"] = int(rec["n_error"])
                if "model" in state.columns:
                    rec["model"] = (
                        state["model"].dropna().iloc[-1]
                        if state["model"].notna().any()
                        else None
                    )
                if "scored_at" in state.columns:
                    rec["last_scored_at"] = state["scored_at"].max()
            out.append(rec)
    return out
