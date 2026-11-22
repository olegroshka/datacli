"""A partition-per-file store with a manifest of what each partition was built from.

Layout for dataset ``<name>`` under the linkage root::

    <root>/<name>/<key>.parquet     one file per partition (a month end, or a year)
    <root>/<name>/manifest.json     {"params": digest, "partitions": {key: entry}}

Each manifest entry records the digest of the inputs the partition was built
from (``inputs``), its row count and when it was written. A build compares
the digest it would use with the stored one: equal means unchanged, different
means restated (the vendor history or the parameters moved), absent means new.
That makes incremental builds honest without keeping vintages: a partition is
always what today's inputs give for its own window, never more.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

NEW, RESTATED, UNCHANGED = "new", "restated", "unchanged"


@dataclass(frozen=True)
class Entry:
    inputs: str
    rows: int
    built_at: str


class PartitionStore:
    def __init__(self, root: Path, name: str, schema: pa.Schema) -> None:
        self.root = Path(root)
        self.name = name
        self.schema = schema
        self.dir = self.root / name
        self.manifest_path = self.dir / "manifest.json"

    def path(self, key: str) -> Path:
        return self.dir / f"{key}.parquet"

    def keys_on_disk(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        return sorted(p.stem for p in self.dir.glob("*.parquet"))

    # ----- manifest -------------------------------------------------------- #
    def manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {"params": "", "partitions": {}}
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"params": "", "partitions": {}}
        data.setdefault("params", "")
        data.setdefault("partitions", {})
        return data

    def entries(self) -> dict[str, Entry]:
        return {k: Entry(**v) for k, v in self.manifest()["partitions"].items()}

    def classify(self, key: str, inputs: str, params: str) -> str:
        """``new`` / ``restated`` / ``unchanged`` for a partition built from ``inputs``."""
        manifest = self.manifest()
        entry = manifest["partitions"].get(key)
        if entry is None or not self.path(key).exists():
            return NEW
        if manifest["params"] != params or entry.get("inputs") != inputs:
            return RESTATED
        return UNCHANGED

    def _write_manifest(self, params: str, partitions: Mapping[str, Entry]) -> None:
        import _atomic  # type: ignore[import-not-found]

        payload = {
            "params": params,
            "partitions": {k: vars(partitions[k]) for k in sorted(partitions)},
        }
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        _atomic.replace_with_retry(tmp, self.manifest_path)

    # ----- data ------------------------------------------------------------ #
    def write(self, key: str, frame: pd.DataFrame, *, inputs: str, params: str) -> int:
        """Write one partition and record it; partitions built under other params are dropped."""
        import _atomic  # type: ignore[import-not-found]

        table = pa.Table.from_pandas(
            frame[list(self.schema.names)], schema=self.schema, preserve_index=False
        )
        _atomic.write_table(table, self.path(key), compression="zstd")
        manifest = self.manifest()
        partitions = (
            {k: Entry(**v) for k, v in manifest["partitions"].items()}
            if manifest["params"] == params
            else {}
        )
        partitions[key] = Entry(
            inputs=inputs,
            rows=table.num_rows,
            built_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        )
        self._write_manifest(params, partitions)
        return table.num_rows

    def drop(self, key: str) -> None:
        """Remove a partition that the inputs no longer support."""
        path = self.path(key)
        if path.exists():
            path.unlink()
        manifest = self.manifest()
        partitions = {
            k: Entry(**v) for k, v in manifest["partitions"].items() if k != key
        }
        self._write_manifest(manifest["params"], partitions)

    def read(self, key: str) -> pd.DataFrame:
        return pq.read_table(self.path(key)).to_pandas()

    def read_all(self) -> pd.DataFrame:
        keys = self.keys_on_disk()
        if not keys:
            return self.schema.empty_table().to_pandas()
        import pyarrow.dataset as pads

        files = [
            str(self.path(key)) for key in keys
        ]  # never the manifest or a stray temp file
        return (
            pads.dataset(files, schema=self.schema, format="parquet")
            .to_table()
            .to_pandas()
        )
