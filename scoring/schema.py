"""Scoring schemas: declarative categories, loaded from TOML, enforced on output.

A schema file (``scoring/schemas/<name>_v<version>.toml``) declares article-level
``fields`` and per-symbol ``symbol_fields``; each field has a ``type`` (``float``,
``int``, ``bool``, ``string``, ``enum``) and its constraints. The same object
renders the field spec for a prompt, describes the JSON shape a backend must
return, and coerces + validates what came back -- so adding a category is a TOML
edit, never a code change.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"

FIELD_TYPES = ("float", "int", "bool", "string", "enum")


class SchemaError(ValueError):
    """A schema file is malformed."""


@dataclass(frozen=True)
class Field:
    """One scored category."""

    name: str
    type: str
    description: str = ""
    values: tuple[str, ...] = ()  # enum members
    min: float | None = None
    max: float | None = None
    max_length: int | None = None
    #: For an enum: ``{member: number}`` plus the derived float column name.
    #: Lets a schema ask the model for a *label* (which models pick reliably)
    #: while consumers still get a number -- see event_v3 and the anchor
    #: quantisation it fixes.
    numeric: dict[str, float] | None = None
    numeric_as: str | None = None

    @property
    def derived_numeric(self) -> str | None:
        """Name of the float column derived from this enum, if any."""
        if not self.numeric:
            return None
        return self.numeric_as or f"{self.name}_score"

    def to_number(self, value: Any) -> float | None:
        """Map a coerced enum value to its number (``None`` when unmapped)."""
        if not self.numeric or value is None:
            return None
        v = self.numeric.get(str(value))
        return None if v is None else float(v)

    def spec_line(self) -> str:
        """Human-readable one-liner for the prompt."""
        if self.type == "enum":
            constraint = "one of " + ", ".join(f'"{v}"' for v in self.values)
        elif self.type in ("float", "int"):
            lo = "-inf" if self.min is None else self.min
            hi = "inf" if self.max is None else self.max
            constraint = f"{self.type} in [{lo}, {hi}]"
        elif self.type == "string":
            constraint = "string" + (
                f" (max {self.max_length} chars)" if self.max_length else ""
            )
        else:
            constraint = "true or false"
        return f'- "{self.name}": {constraint}. {self.description}'.rstrip()

    def json_type(self) -> dict[str, Any]:
        """JSON-schema fragment (used by backends that support tool schemas)."""
        if self.type == "enum":
            return {"type": "string", "enum": list(self.values)}
        if self.type == "float":
            out: dict[str, Any] = {"type": "number"}
        elif self.type == "int":
            out = {"type": "integer"}
        elif self.type == "bool":
            return {"type": "boolean"}
        else:
            out = {"type": "string"}
            if self.max_length:
                out["maxLength"] = self.max_length
            return out
        if self.min is not None:
            out["minimum"] = self.min
        if self.max is not None:
            out["maximum"] = self.max
        return out

    def coerce(self, value: Any) -> tuple[Any, str | None]:
        """Coerce a raw value to the field type.

        Returns ``(value, None)`` when acceptable (numbers are clamped into
        range, strings truncated) or ``(None, reason)`` when it cannot be used.
        """
        if value is None:
            return None, "missing"
        try:
            if self.type == "float":
                v = float(value)
                if v != v:  # NaN
                    return None, "nan"
                return _clamp(v, self.min, self.max), None
            if self.type == "int":
                if isinstance(value, bool):
                    return None, "bool for int"
                v = int(round(float(value)))
                return int(_clamp(v, self.min, self.max)), None
            if self.type == "bool":
                if isinstance(value, bool):
                    return value, None
                text = str(value).strip().lower()
                if text in ("true", "yes", "1"):
                    return True, None
                if text in ("false", "no", "0"):
                    return False, None
                return None, f"not a bool: {value!r}"
            if self.type == "enum":
                text = str(value).strip()
                for member in self.values:
                    if text.lower() == member.lower():
                        return member, None
                # tolerate common punctuation variants ("M&A" -> "m_and_a")
                norm = _norm(text)
                for member in self.values:
                    if norm == _norm(member):
                        return member, None
                return None, f"not in enum: {value!r}"
            text = str(value)
            if self.max_length and len(text) > self.max_length:
                text = text[: self.max_length]
            return text, None
        except (TypeError, ValueError) as exc:
            return None, f"{type(exc).__name__}: {exc}"


def _clamp(v: float, lo: float | None, hi: float | None) -> float:
    if lo is not None and v < lo:
        return lo
    if hi is not None and v > hi:
        return hi
    return v


def _norm(text: str) -> str:
    return "".join(ch for ch in text.lower().replace("&", "and") if ch.isalnum())


@dataclass(frozen=True)
class Schema:
    """A loaded scoring schema."""

    name: str
    version: int
    description: str
    scope: str  # "article" | "article+symbol"
    max_symbols: int
    text: str  # "title+content" | "title" | "content"
    max_chars: int
    system: str
    instructions: str
    fields: tuple[Field, ...]
    symbol_fields: tuple[Field, ...] = ()
    source: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    # -- identity ------------------------------------------------------------ #
    @property
    def key(self) -> str:
        """``event@1`` -- the directory / view suffix for this schema version."""
        return f"{self.name}@{self.version}"

    @property
    def per_symbol(self) -> bool:
        return self.scope == "article+symbol" and bool(self.symbol_fields)

    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]

    def numeric_fields(self) -> list[Field]:
        """Article-level enum fields that carry a numeric mapping."""
        return [f for f in self.fields if f.derived_numeric]

    def numeric_symbol_fields(self) -> list[Field]:
        return [f for f in self.symbol_fields if f.derived_numeric]

    def symbol_field_names(self) -> list[str]:
        return [f.name for f in self.symbol_fields]

    def prompt_hash(self) -> str:
        """Hash of everything that shapes the model's task (prompt + fields)."""
        payload = {
            "system": self.system,
            "instructions": self.instructions,
            "fields": [f.__dict__ for f in self.fields],
            "symbol_fields": [f.__dict__ for f in self.symbol_fields],
            "text": self.text,
            "max_chars": self.max_chars,
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    # -- prompt rendering ---------------------------------------------------- #
    def field_spec(self) -> str:
        """The field list as prompt text."""
        lines = ['Article-level fields ("article"):']
        lines += [f.spec_line() for f in self.fields]
        if self.per_symbol:
            lines.append("")
            lines.append(
                'Per-symbol fields ("symbols": one object per listed symbol, keyed by '
                "the symbol exactly as given):"
            )
            lines += [f.spec_line() for f in self.symbol_fields]
        return "\n".join(lines)

    def json_shape(self, symbols: list[str]) -> dict[str, Any]:
        """JSON schema of the expected output for one article."""
        article_props = {f.name: f.json_type() for f in self.fields}
        shape: dict[str, Any] = {
            "type": "object",
            "properties": {
                "article": {
                    "type": "object",
                    "properties": article_props,
                    "required": list(article_props),
                }
            },
            "required": ["article"],
        }
        if self.per_symbol and symbols:
            sym_props = {f.name: f.json_type() for f in self.symbol_fields}
            shape["properties"]["symbols"] = {
                "type": "object",
                "properties": {
                    s: {
                        "type": "object",
                        "properties": sym_props,
                        "required": list(sym_props),
                    }
                    for s in symbols
                },
                "required": list(symbols),
            }
            shape["required"].append("symbols")
        return shape

    # -- output validation --------------------------------------------------- #
    def validate(
        self, payload: Any, symbols: list[str]
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[str]]:
        """Coerce a parsed model output into ``(article, symbols, problems)``.

        Missing/invalid fields are reported in ``problems`` and left ``None``;
        the caller decides whether that makes the record ``invalid``.
        """
        problems: list[str] = []
        if not isinstance(payload, dict):
            return {}, {}, ["payload is not an object"]
        raw_article = payload.get("article")
        if not isinstance(raw_article, dict):
            # tolerate a flat object with the article fields at top level
            raw_article = payload
        article: dict[str, Any] = {}
        for f in self.fields:
            value, why = f.coerce(raw_article.get(f.name))
            article[f.name] = value
            if why:
                problems.append(f"article.{f.name}: {why}")
        per_symbol: dict[str, dict[str, Any]] = {}
        if self.per_symbol and symbols:
            raw_symbols = payload.get("symbols")
            if not isinstance(raw_symbols, dict):
                raw_symbols = {}
                problems.append("symbols: missing")
            lookup = {str(k).strip().upper(): v for k, v in raw_symbols.items()}
            for sym in symbols:
                block = lookup.get(sym.upper())
                if not isinstance(block, dict):
                    problems.append(f"symbols.{sym}: missing")
                    block = {}
                rec: dict[str, Any] = {}
                for f in self.symbol_fields:
                    value, why = f.coerce(block.get(f.name))
                    rec[f.name] = value
                    if why:
                        problems.append(f"symbols.{sym}.{f.name}: {why}")
                per_symbol[sym] = rec
        return article, per_symbol, problems


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def _field(raw: dict[str, Any], where: str) -> Field:
    name = str(raw.get("name", "")).strip()
    ftype = str(raw.get("type", "")).strip()
    if not name:
        raise SchemaError(f"{where}: field without a name")
    if ftype not in FIELD_TYPES:
        raise SchemaError(f"{where}: field {name!r} has unknown type {ftype!r}")
    values = tuple(str(v) for v in raw.get("values", []) or [])
    if ftype == "enum" and not values:
        raise SchemaError(f"{where}: enum field {name!r} needs `values`")
    numeric = raw.get("numeric")
    if numeric is not None:
        if ftype != "enum":
            raise SchemaError(
                f"{where}: field {name!r} has `numeric` but is not an enum"
            )
        numeric = {str(k): float(v) for k, v in dict(numeric).items()}
        unknown = set(numeric) - set(values)
        if unknown:
            raise SchemaError(
                f"{where}: field {name!r} `numeric` has non-members {sorted(unknown)}"
            )
        missing = set(values) - set(numeric)
        if missing:
            raise SchemaError(
                f"{where}: field {name!r} `numeric` is missing {sorted(missing)}"
            )
    return Field(
        name=name,
        type=ftype,
        description=str(raw.get("description", "")).strip(),
        values=values,
        min=raw.get("min"),
        max=raw.get("max"),
        max_length=raw.get("max_length"),
        numeric=numeric,
        numeric_as=(str(raw["numeric_as"]) if raw.get("numeric_as") else None),
    )


def load_schema_file(path: Path) -> Schema:
    """Parse and validate one schema TOML file."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SchemaError(f"{path}: {exc}") from exc
    head = data.get("schema") or {}
    prompt = data.get("prompt") or {}
    name = str(head.get("name", "")).strip()
    if not name:
        raise SchemaError(f"{path}: [schema].name is required")
    try:
        version = int(head.get("version", 0))
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"{path}: [schema].version must be an integer") from exc
    if version < 1:
        raise SchemaError(f"{path}: [schema].version must be >= 1")
    fields = tuple(_field(f, f"{path} [[fields]]") for f in data.get("fields", []))
    if not fields:
        raise SchemaError(f"{path}: at least one [[fields]] entry is required")
    symbol_fields = tuple(
        _field(f, f"{path} [[symbol_fields]]") for f in data.get("symbol_fields", [])
    )
    names = [f.name for f in fields] + [f.name for f in symbol_fields]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise SchemaError(f"{path}: duplicate field names {sorted(dupes)}")
    scope = str(head.get("scope", "article+symbol" if symbol_fields else "article"))
    if scope not in ("article", "article+symbol"):
        raise SchemaError(f"{path}: scope must be 'article' or 'article+symbol'")
    return Schema(
        name=name,
        version=version,
        description=str(head.get("description", "")).strip(),
        scope=scope,
        max_symbols=int(head.get("max_symbols", 3)),
        text=str(head.get("text", "title+content")),
        max_chars=int(head.get("max_chars", 6000)),
        system=str(prompt.get("system", "")).strip(),
        instructions=str(prompt.get("instructions", "")).strip(),
        fields=fields,
        symbol_fields=symbol_fields,
        source=path,
        extra={k: v for k, v in head.items() if k not in _KNOWN_HEAD_KEYS},
    )


_KNOWN_HEAD_KEYS = {
    "name",
    "version",
    "description",
    "scope",
    "max_symbols",
    "text",
    "max_chars",
}


def list_schemas(schemas_dir: Path = SCHEMAS_DIR) -> dict[str, Schema]:
    """All schemas under ``schemas_dir`` keyed by ``name@version``."""
    out: dict[str, Schema] = {}
    for path in sorted(schemas_dir.glob("*.toml")):
        s = load_schema_file(path)
        if s.key in out:
            raise SchemaError(
                f"duplicate schema {s.key}: {path} and {out[s.key].source}"
            )
        out[s.key] = s
    return out


def load_schema(spec: str, schemas_dir: Path = SCHEMAS_DIR) -> Schema:
    """Resolve ``"event"`` (latest version) or ``"event@1"`` / ``"event_v1"``."""
    spec = spec.strip()
    if spec.endswith(".toml"):
        return load_schema_file(Path(spec))
    key = spec.replace("_v", "@") if "_v" in spec and "@" not in spec else spec
    schemas = list_schemas(schemas_dir)
    if key in schemas:
        return schemas[key]
    same_name = [s for s in schemas.values() if s.name == key]
    if not same_name:
        known = ", ".join(sorted(schemas)) or "(none)"
        raise SchemaError(f"unknown schema {spec!r}. Known: {known}")
    return max(same_name, key=lambda s: s.version)
