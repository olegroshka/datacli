"""Report the current as-of status of every registered EODHD dataset.

This tool holds **no** hardcoded lane or dataset names. It iterates the shared
registry in ``eodhd_datasets.py``, so onboarding a new dataset is a one-entry
change there — this reporter picks it up automatically.

For each ``(lane, dataset)`` it answers "what, as of when":

- **last data**: the last real bar (prices) / last event ex-date (dividends,
  splits) / latest filing date (fundamentals) / last crawled UTC day (news)
  currently on disk,
- **coverage**: how far the incremental fetch queried (state sidecar),
- **fetched**: when the pull last ran (state sidecar, or file mtime for
  snapshots),
- **age / flag**: staleness measured against the correct freshness anchor per
  dataset kind, flagged ``STALE`` past ``--stale-days``,
- **rows / pairs / status**: volume and per-pair fetch-status health.

Freshness anchor per dataset kind: prices = last real bar; dividends/splits =
query coverage (not the last event date); fundamentals = latest filing date;
news = last crawled UTC day.

It also runs an auto-discovery guard: any lane directory on disk that is not in
the registry is reported as unmonitored. When *nothing* is on disk yet (a fresh
clone / new data root) it points at the first-fill steps instead of a wall of
``ABSENT`` rows.

Usage:
    uv run python eodhd/status_eodhd.py
    uv run python eodhd/status_eodhd.py --lane us_common --deep
    uv run python eodhd/status_eodhd.py --json
    uv run python eodhd/status_eodhd.py --write   # emit STATUS.md + STATUS.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq  # type: ignore[import-untyped]

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _render  # noqa: E402
from eodhd_datasets import RAW_EODHD  # noqa: E402
from eodhd_datasets import (
    LANES,
    NON_LANE_DIRS,
    DatasetSpec,
    LaneConfig,
    discover_lane_dirs,
    registered_lane_names,
)

DEFAULT_STALE_DAYS = 7


# --------------------------------------------------------------------------- #
# small readers
# --------------------------------------------------------------------------- #
def _parquet_files(path: Path) -> list[Path]:
    """The parquet file(s) behind an output: itself, or ``*.parquet`` inside a
    partition directory (sorted, so "first file" is deterministic)."""
    if path.is_dir():
        return sorted(path.glob("*.parquet"))
    return [path]


def parquet_num_rows(path: Path) -> int | None:
    """Row count from the parquet footer(s) only (no column data read)."""
    try:
        files = _parquet_files(path)
        if not files:
            return None
        return sum(int(pq.ParquetFile(f).metadata.num_rows) for f in files)
    except Exception:
        return None


def parquet_columns(path: Path) -> set[str]:
    """Column names from the parquet schema (first partition for directories)."""
    try:
        files = _parquet_files(path)
        if not files:
            return set()
        return set(pq.ParquetFile(files[0]).schema.names)
    except Exception:
        return set()


def parquet_max_date(path: Path, column: str) -> pd.Timestamp | None:
    """Max of a single date column, reading only that column."""
    try:
        table = pq.read_table(path, columns=[column])
    except Exception:
        return None
    series = pd.to_datetime(table.column(column).to_pandas(), errors="coerce")
    value = series.max()
    return None if pd.isna(value) else pd.Timestamp(value)


def parquet_distinct_pairs(path: Path, key_cols: tuple[str, ...]) -> int | None:
    """Distinct identifier pairs, reading only the key columns."""
    available = parquet_columns(path)
    cols = [c for c in key_cols if c in available]
    if not cols:
        return None
    try:
        table = pq.read_table(path, columns=cols)
    except Exception:
        return None
    return int(table.to_pandas().drop_duplicates().shape[0])


def _max_date(frame: pd.DataFrame, column: str) -> pd.Timestamp | None:
    if column not in frame.columns:
        return None
    series = pd.to_datetime(frame[column], errors="coerce")
    value = series.max()
    return None if pd.isna(value) else pd.Timestamp(value)


def _max_timestamp(frame: pd.DataFrame, column: str) -> pd.Timestamp | None:
    if column not in frame.columns:
        return None
    series = pd.to_datetime(frame[column], errors="coerce", utc=True)
    value = series.max()
    return None if pd.isna(value) else pd.Timestamp(value)


def _status_counts(frame: pd.DataFrame, column: str) -> dict[str, int]:
    if column not in frame.columns:
        return {}
    return {str(k): int(v) for k, v in frame[column].value_counts().items()}


def _iso_date(value: pd.Timestamp | None) -> str | None:
    return None if value is None else value.date().isoformat()


# --------------------------------------------------------------------------- #
# core: build one record per (lane, dataset)
# --------------------------------------------------------------------------- #
def staleness_days(
    freshness: pd.Timestamp | None, as_of_ts: pd.Timestamp
) -> int | None:
    """Calendar days between a freshness anchor and the reference date.

    Clamped at zero so a future announced date never reads as negative age.
    Returns ``None`` when there is no freshness signal.
    """
    if freshness is None:
        return None
    delta = (as_of_ts.normalize() - freshness.normalize()).days
    return max(delta, 0)


def collect_dataset(
    lane: LaneConfig,
    dataset: DatasetSpec,
    *,
    as_of_ts: pd.Timestamp,
    stale_days: int,
    deep: bool,
) -> dict[str, Any]:
    """Assemble the status record for a single dataset.

    Reads the fetch-state sidecar when present (cheap CSV) and the parquet footer
    for the row count. Parquet *data* columns are only read for snapshot datasets
    (no sidecar) or when ``deep`` is set.

    Args:
        lane: Owning lane.
        dataset: Dataset spec to inspect.
        as_of_ts: Reference date for staleness.
        stale_days: Age threshold (calendar days) beyond which the flag is STALE.
        deep: Read the parquet date column even for state-backed datasets to
            cross-check state-vs-output drift.

    Returns:
        A JSON-serializable record.
    """
    root = lane.resolved_root()
    out_path = root / dataset.output
    present = out_path.exists()

    record: dict[str, Any] = {
        "lane": lane.name,
        "region": lane.region,
        "asset_class": lane.asset_class,
        "dataset": dataset.display,
        "kind": dataset.kind,
        "source": "absent",
        "rows": parquet_num_rows(out_path) if present else None,
        "pairs": None,
        "last_data": None,
        "coverage": None,
        "output_max": None,
        "fetched": None,
        "freshness": None,
        "stale_days": None,
        "stale": None,
        "status": {},
    }

    freshness: pd.Timestamp | None = None
    state_path = (root / dataset.state) if dataset.state else None

    if state_path is not None and state_path.exists():
        state = pd.read_csv(state_path)
        record["source"] = "state"
        if set(dataset.key_cols).issubset(state.columns):
            record["pairs"] = int(
                state[list(dataset.key_cols)].drop_duplicates().shape[0]
            )
        else:
            record["pairs"] = int(len(state))
        record["status"] = _status_counts(state, dataset.status_col)
        record["last_data"] = _iso_date(_max_date(state, dataset.as_of_state_col))
        record["coverage"] = _iso_date(_max_date(state, dataset.coverage_col))
        record["fetched"] = _fetched_iso(_max_timestamp(state, dataset.fetched_col))
        freshness = _max_date(state, dataset.freshness_col)
        if deep and present and dataset.as_of_data_col in parquet_columns(out_path):
            record["output_max"] = _iso_date(
                parquet_max_date(out_path, dataset.as_of_data_col)
            )
    elif present:
        record["source"] = "snapshot"
        record["status"] = "snapshot"
        last = None
        if dataset.as_of_data_col and dataset.as_of_data_col in parquet_columns(
            out_path
        ):
            last = parquet_max_date(out_path, dataset.as_of_data_col)
        record["last_data"] = _iso_date(last)
        mtime = pd.Timestamp(datetime.fromtimestamp(out_path.stat().st_mtime))
        record["fetched"] = mtime.isoformat(timespec="seconds")
        freshness = last if last is not None else mtime
        if deep:
            record["pairs"] = parquet_distinct_pairs(out_path, dataset.key_cols)
    else:
        record["status"] = "absent"

    record["freshness"] = _iso_date(freshness)
    age = staleness_days(freshness, as_of_ts)
    record["stale_days"] = age
    record["stale"] = None if age is None else age > stale_days
    return record


def _fetched_iso(value: pd.Timestamp | None) -> str | None:
    if value is None:
        return None
    return value.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def build_records(
    lane_names: list[str],
    *,
    as_of_ts: pd.Timestamp,
    stale_days: int,
    deep: bool,
) -> list[dict[str, Any]]:
    """Build status records for the requested lanes, in registry order."""
    records: list[dict[str, Any]] = []
    for name in lane_names:
        lane = LANES[name]
        for dataset in lane.datasets:
            records.append(
                collect_dataset(
                    lane, dataset, as_of_ts=as_of_ts, stale_days=stale_days, deep=deep
                )
            )
    return records


# --------------------------------------------------------------------------- #
# discovery guard
# --------------------------------------------------------------------------- #
def discovery_warnings() -> list[str]:
    """Warn about on-disk lane directories that are not in the registry."""
    registered = registered_lane_names()
    warnings: list[str] = []
    for name in discover_lane_dirs():
        if name in registered or name in NON_LANE_DIRS:
            continue
        warnings.append(f"unregistered lane directory on disk: {name} (not monitored)")
    for name in sorted(registered):
        if not LANES[name].resolved_root().exists():
            warnings.append(f"registered lane has no directory on disk: {name}")
    return warnings


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def first_fill_hint(records: list[dict[str, Any]], root: Path) -> str | None:
    """Next-step hint when there is no data at all under ``root``.

    A wall of ``ABSENT`` rows is the normal state of a fresh clone or a freshly
    pointed data root, so instead of leaving the operator to guess, point at the
    first-fill steps. Returns ``None`` as soon as *any* record has data (a
    partially filled root is a per-lane story the table already tells).

    Args:
        records: Status records as built by :func:`build_records`.
        root: The resolved data root the records were collected under.
    """
    if not records or any(r.get("source") != "absent" for r in records):
        return None
    return (
        f"no data under {root} — first fill: config set data-root <path>, then "
        "refresh <lane> --run (us_common/uk_eu: add --with-fundamentals); "
        "see eodhd/README.md 'First fill'"
    )


def _flag(record: dict[str, Any]) -> str:
    if record["source"] == "absent":
        return "ABSENT"
    if record["stale"] is True:
        return "STALE"
    if record["stale"] is False:
        return "ok"
    return "-"


def _status_str(record: dict[str, Any]) -> str:
    status = record["status"]
    if isinstance(status, dict):
        if not status:
            return "-"
        return " ".join(f"{k}={v}" for k, v in status.items())
    return str(status)


def _fmt_int(value: int | None) -> str:
    return "-" if value is None else f"{value:,}"


def _fmt_fetched(value: str | None) -> str:
    if not value:
        return "-"
    return value.replace("T", " ").rstrip("Z")[:16]


EVENT_KINDS = {"dividends", "splits"}


def _fmt_fetched_short(value: str | None) -> str:
    """``2026-07-09T11:06:..`` -> ``07-09 11:06`` (drop the year to save width)."""
    full = _fmt_fetched(value)
    if full == "-" or len(full) < 16:
        return full
    return full[5:]  # strip "YYYY-"


def print_status(
    console: Any, records: list[dict[str, Any]], *, as_of: str, stale_days: int
) -> None:
    """Render the status dashboard: minimal box + lane grouping + semantic colour.

    Lane is printed once per group; each dataset carries a kind-hued ``●`` dot; the
    freshness anchor date, age and flag are coloured by staleness; the state
    breakdown is coloured per token. Numbers are right-aligned and compact.
    """
    from rich.text import Text

    # Responsive: the full 10-column table needs ~132 cols. On narrower terminals
    # drop the two least-critical columns (coverage ~ duplicates last_data for
    # prices; fetched is metadata) so the essentials never truncate. Age + flag
    # still carry the freshness signal.
    wide = getattr(console, "width", 120) >= 132

    table = _render.minimal_table(
        title=f"EODHD status {_render.DIVIDER} as-of {as_of} "
        f"{_render.DIVIDER} stale >{stale_days}d"
    )
    table.add_column("lane", no_wrap=True)
    table.add_column("dataset", no_wrap=True)
    table.add_column("last_data", no_wrap=True)
    if wide:
        table.add_column("coverage", no_wrap=True)
        table.add_column("fetched", no_wrap=True, style="dim")
    table.add_column("rows", justify="right", no_wrap=True)
    table.add_column("pairs", justify="right", no_wrap=True)
    table.add_column("age", justify="right", no_wrap=True)
    table.add_column("flag", no_wrap=True)
    table.add_column("state", no_wrap=True)

    prev_lane: str | None = None
    for r in records:
        kind = r.get("kind", "")
        anchor = "coverage" if kind in EVENT_KINDS else "last_data"

        dataset = Text()
        dataset.append_text(_render.kind_dot(kind))
        dataset.append(f" {r['dataset']}")

        def _date(field: str) -> Text:
            value = r.get(field) or "-"
            if value == "-":
                return Text("-", style="dim")
            if field == anchor:
                return Text(value, style=_render.freshness_style(r["stale_days"]))
            return Text(value, style="dim")

        lane_cell = "" if r["lane"] == prev_lane else r["lane"]
        prev_lane = r["lane"]

        cells = [lane_cell, dataset, _date("last_data")]
        if wide:
            cells += [_date("coverage"), _fmt_fetched_short(r["fetched"])]
        cells += [
            _render.fmt_compact(r["rows"]),
            _render.fmt_int(r["pairs"]),
            _render.age_cell(r["stale_days"]),
            _render.flag_cell(_flag(r)),
            _render.state_cell(r["status"]),
        ]
        table.add_row(*cells)

    console.print(table)

    fresh = sum(1 for r in records if r["stale"] is False)
    stale = sum(1 for r in records if r["stale"] is True)
    absent = sum(1 for r in records if r["source"] == "absent")
    total_rows = sum(r["rows"] or 0 for r in records)
    footer = Text()
    footer.append(f"{fresh} fresh", style="green")
    footer.append(f" {_render.DIVIDER} ", style="dim")
    footer.append(f"{stale} stale", style="yellow" if stale else "dim")
    footer.append(f" {_render.DIVIDER} ", style="dim")
    footer.append(f"{absent} absent", style="red" if absent else "dim")
    footer.append(f" {_render.DIVIDER} ", style="dim")
    footer.append(f"≈{_render.fmt_compact(total_rows)} rows", style="dim")
    footer.append(f" across {len(records)} datasets", style="dim")
    console.print(footer)

    hint = first_fill_hint(records, RAW_EODHD)
    if hint:
        console.print(Text(hint, style="dim"))


def render_markdown(
    records: list[dict[str, Any]], *, as_of: str, stale_days: int
) -> str:
    """Render the committed STATUS.md document."""
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# EODHD Data Status",
        "",
        f"_Auto-generated by `eodhd/status_eodhd.py` on {generated} "
        f"(reference as-of `{as_of}`, stale threshold {stale_days}d)._",
        "",
        "**Do not edit by hand** — regenerate with "
        "`uv run python eodhd/status_eodhd.py --write`.",
        "",
        "Freshness anchor per dataset: prices = last real bar; dividends/splits = "
        "query coverage (not last event date); fundamentals = latest filing date; "
        "news = last crawled UTC day.",
        "",
        "| lane | dataset | last data | coverage | fetched | rows | pairs | age (d) | flag | status |",
        "|------|---------|-----------|----------|---------|-----:|------:|--------:|------|--------|",
    ]
    for r in records:
        lines.append(
            "| {lane} | {dataset} | {last} | {cov} | {fetched} | {rows} | {pairs} | {age} | {flag} | {status} |".format(
                lane=r["lane"],
                dataset=r["dataset"],
                last=r["last_data"] or "-",
                cov=r["coverage"] or "-",
                fetched=_fmt_fetched(r["fetched"]),
                rows=_fmt_int(r["rows"]),
                pairs=_fmt_int(r["pairs"]),
                age="-" if r["stale_days"] is None else r["stale_days"],
                flag=_flag(r),
                status=_status_str(r),
            )
        )
    hint = first_fill_hint(records, RAW_EODHD)
    if hint:
        lines += ["", f"> **Note:** {hint}"]
    warnings = discovery_warnings()
    if warnings:
        lines += ["", "## Discovery warnings", ""]
        lines += [f"- {w}" for w in warnings]
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=os.environ.get("DATACLI_PROG") or None,
        description="Report as-of status of EODHD datasets",
    )
    parser.add_argument(
        "--lane",
        choices=[*LANES.keys(), "all"],
        default="all",
        help="Restrict the report to one registered lane (default: all)",
    )
    parser.add_argument(
        "--as-of",
        dest="as_of",
        default=datetime.now().date().isoformat(),
        help="Reference date for staleness (YYYY-MM-DD; defaults to today)",
    )
    parser.add_argument(
        "--stale-days",
        type=int,
        default=DEFAULT_STALE_DAYS,
        help="Flag datasets whose freshness anchor is older than this many days",
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Also read parquet date columns (state-vs-output drift; snapshot pair counts)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit records as JSON to stdout"
    )
    parser.add_argument(
        "--csv", default="", help="Optional path to write the records as CSV"
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write STATUS.md and STATUS.json into the resolved data root "
        f"({RAW_EODHD})",
    )
    parser.add_argument(
        "--no-discovery",
        action="store_true",
        help="Skip the unregistered-lane discovery guard",
    )
    parser.add_argument(
        "--no-color",
        dest="no_color",
        action="store_true",
        help="Disable ANSI colour (also honours the NO_COLOR env var)",
    )
    parser.add_argument(
        "--color",
        dest="force_color",
        action="store_true",
        help="Force ANSI colour even when stdout is not a TTY",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    as_of_ts = pd.Timestamp(args.as_of)
    lane_names = list(LANES) if args.lane == "all" else [args.lane]

    records = build_records(
        lane_names, as_of_ts=as_of_ts, stale_days=args.stale_days, deep=args.deep
    )

    if args.json:
        print(json.dumps(records, indent=2))
    else:
        console = _render.make_console(
            no_color=True if args.no_color else None,
            force_color=args.force_color,
        )
        print_status(console, records, as_of=args.as_of, stale_days=args.stale_days)

    # Discovery warnings flag *unexpected* directories or missing lanes on a
    # populated root; on a completely empty root they would only bury the
    # first-fill hint under one line per lane, so they are skipped there.
    if not args.no_discovery and first_fill_hint(records, RAW_EODHD) is None:
        for warning in discovery_warnings():
            print(f"[discovery] {warning}")

    if args.csv:
        flat = pd.DataFrame(records)
        flat["status"] = flat["status"].map(
            lambda s: (
                " ".join(f"{k}={v}" for k, v in s.items()) if isinstance(s, dict) else s
            )
        )
        flat.to_csv(args.csv, index=False)
        print(f"\nWrote records -> {args.csv}")

    if args.write:
        RAW_EODHD.mkdir(parents=True, exist_ok=True)
        md_path = RAW_EODHD / "STATUS.md"
        json_path = RAW_EODHD / "STATUS.json"
        md_path.write_text(
            render_markdown(records, as_of=args.as_of, stale_days=args.stale_days),
            encoding="utf-8",
        )
        payload = {
            "generated": datetime.now().isoformat(timespec="seconds"),
            "as_of": args.as_of,
            "stale_days": args.stale_days,
            "datasets": records,
        }
        json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nWrote {md_path}\nWrote {json_path}")


if __name__ == "__main__":
    main()
