"""Load the agent roster from files: personas (TOML) and skills (SKILL.md).

Registry-driven and hot-loadable, matching datacli's ethos -- add a persona or a
skill by dropping a file in, no code change. Personas are ``lab/personas/*.toml``;
skills are ``lab/skills/<name>/SKILL.md`` with a small frontmatter block.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PERSONA_DIR = Path(__file__).resolve().parent / "personas"
SKILL_DIR = Path(__file__).resolve().parent / "skills"


@dataclass(frozen=True)
class Persona:
    name: str
    description: str = ""
    model: str = "mid"  # a tier name (resolved via [lab.models]) or a raw id
    temperature: float = 0.0
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    role: str = ""


@dataclass(frozen=True)
class Skill:
    name: str
    summary: str = ""
    inputs: list[str] = field(default_factory=list)
    tier: str = "cheap"
    body: str = ""


def _as_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    text = str(value).strip().strip("[]")
    return [t.strip() for t in text.split(",") if t.strip()]


def load_personas(directory: Path = PERSONA_DIR) -> dict[str, Persona]:
    personas: dict[str, Persona] = {}
    if not directory.exists():
        return personas
    for path in sorted(directory.glob("*.toml")):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        name = str(data.get("name", path.stem))
        personas[name] = Persona(
            name=name,
            description=str(data.get("description", "")),
            model=str(data.get("model", "mid")),
            temperature=float(data.get("temperature", 0.0)),
            tools=_as_list(data.get("tools", [])),
            skills=_as_list(data.get("skills", [])),
            role=str(data.get("role", "")),
        )
    return personas


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a ``---``-fenced frontmatter block from the body."""
    if text.lstrip().startswith("---"):
        parts = text.lstrip().split("---", 2)
        if len(parts) >= 3:
            meta: dict[str, str] = {}
            for line in parts[1].splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    meta[key.strip()] = value.strip()
            return meta, parts[2].strip()
    return {}, text.strip()


def load_skills(directory: Path = SKILL_DIR) -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    if not directory.exists():
        return skills
    for path in sorted(directory.glob("*/SKILL.md")):
        try:
            meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        name = meta.get("name", path.parent.name)
        skills[name] = Skill(
            name=name,
            summary=meta.get("summary", ""),
            inputs=_as_list(meta.get("inputs", "")),
            tier=meta.get("tier", "cheap"),
            body=body,
        )
    return skills
