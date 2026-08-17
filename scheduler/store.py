"""Profile-local desired state, drafts, and immutable definition snapshots."""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from .commands import CommandRegistry, ValidationContext
from .locks import LockManager, canonical_path, default_state_root
from .model import (
    COMMAND_CONTRACT_VERSION,
    DesiredPointer,
    JobDraft,
    JobSpec,
    RuntimeBinding,
    utc_now,
    validate_job_id,
)


class StoreError(RuntimeError):
    pass


class GenerationConflict(StoreError):
    pass


class NotFound(StoreError):
    pass


class CorruptStore(StoreError):
    pass


def _secure(path: Path, *, directory: bool = False) -> None:
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700 if directory else 0o600)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _secure(path.parent, directory=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            _secure(temporary)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _secure(path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise CorruptStore(f"cannot read scheduler state {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CorruptStore(f"scheduler state is not a JSON object: {path}")
    return value


@dataclass(frozen=True)
class Profile:
    profile_id: str
    label: str
    repo_root: str
    interpreter: str
    config_path: str | None
    created_at: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Profile":
        return cls(
            profile_id=str(value["profile_id"]),
            label=str(value["label"]),
            repo_root=str(value["repo_root"]),
            interpreter=str(value["interpreter"]),
            config_path=(
                str(value["config_path"])
                if value.get("config_path") is not None
                else None
            ),
            created_at=str(value["created_at"]),
        )


class ProfileRegistry:
    """Discover generated profile UUIDs without deriving identity from paths."""

    def __init__(self, state_root: Path | None = None) -> None:
        self.state_root = (state_root or default_state_root()).resolve()
        self.index_path = self.state_root / "profiles.json"
        self.lock_manager = LockManager(self.state_root / "locks")

    def _load(self) -> dict[str, Profile]:
        if not self.index_path.exists():
            return {}
        value = _read_json(self.index_path)
        profiles = value.get("profiles", {})
        if not isinstance(profiles, dict):
            raise CorruptStore("profile index has invalid profiles field")
        return {str(key): Profile.from_dict(item) for key, item in profiles.items()}

    def list(self) -> tuple[Profile, ...]:
        return tuple(
            sorted(self._load().values(), key=lambda p: (p.label, p.profile_id))
        )

    def get(self, profile_id: str) -> Profile:
        try:
            return self._load()[profile_id]
        except KeyError as exc:
            raise NotFound(f"unknown scheduler profile: {profile_id}") from exc

    def find(self, repo_root: Path, config_path: Path | None = None) -> Profile | None:
        repo_key = canonical_path(repo_root)
        config_key = canonical_path(config_path) if config_path else None
        for profile in self._load().values():
            if canonical_path(profile.repo_root) != repo_key:
                continue
            existing_config = (
                canonical_path(profile.config_path) if profile.config_path else None
            )
            if existing_config == config_key:
                return profile
        return None

    def ensure(
        self,
        repo_root: Path,
        interpreter: Path,
        config_path: Path | None,
        *,
        label: str | None = None,
    ) -> Profile:
        repo = repo_root.resolve()
        interpreter = interpreter.resolve()
        config = config_path.resolve() if config_path else None
        owner = {"pid": os.getpid(), "operation": "profile ensure"}
        from .model import ResourceClaim

        with self.lock_manager.acquire_many(
            [ResourceClaim("scheduler:profile-index", "exclusive")], owner=owner
        ):
            existing = self.find(repo, config)
            if existing is not None:
                return existing
            profile = Profile(
                profile_id=str(uuid.uuid4()),
                label=(label or repo.name or "datacli").strip(),
                repo_root=str(repo),
                interpreter=str(interpreter),
                config_path=str(config) if config else None,
                created_at=utc_now(),
            )
            profiles = self._load()
            profiles[profile.profile_id] = profile
            _atomic_json(
                self.index_path,
                {
                    "schema_version": 1,
                    "profiles": {
                        key: asdict(value) for key, value in sorted(profiles.items())
                    },
                },
            )
            profile_root = self.state_root / "profiles" / profile.profile_id
            profile_root.mkdir(parents=True, exist_ok=True)
            _secure(profile_root, directory=True)
            _atomic_json(profile_root / "profile.json", asdict(profile))
            return profile


class JobStore:
    def __init__(self, profile: Profile, state_root: Path | None = None) -> None:
        self.profile = profile
        self.state_root = (state_root or default_state_root()).resolve()
        self.root = self.state_root / "profiles" / profile.profile_id
        self.desired_root = self.root / "desired"
        self.snapshots_root = self.root / "snapshots"
        self.drafts_root = self.root / "drafts"
        self.lock_manager = LockManager(self.state_root / "locks")
        self.root.mkdir(parents=True, exist_ok=True)
        _secure(self.root, directory=True)

    def _management_claim(self, job_id: str):
        from .model import ResourceClaim

        return ResourceClaim(
            f"scheduler:management:{self.profile.profile_id}:{job_id}", "exclusive"
        )

    def _pointer_path(self, job_id: str) -> Path:
        validate_job_id(job_id)
        return self.desired_root / f"{job_id}.json"

    def _snapshot_path(self, job_id: str, generation: int, digest: str) -> Path:
        validate_job_id(job_id)
        if not digest or not all(char in "0123456789abcdef" for char in digest):
            raise StoreError("invalid definition digest")
        return self.snapshots_root / job_id / f"{generation}-{digest}.json"

    def _draft_path(self, draft_id: str) -> Path:
        safe = validate_job_id(draft_id)
        return self.drafts_root / f"{safe}.json"

    def get_current(self, job_id: str) -> DesiredPointer | None:
        path = self._pointer_path(job_id)
        if not path.exists():
            return None
        return DesiredPointer.from_dict(_read_json(path))

    def get_current_spec(self, job_id: str) -> JobSpec:
        pointer = self.get_current(job_id)
        if pointer is None or pointer.state != "active" or pointer.digest is None:
            raise NotFound(f"job is not active desired state: {job_id}")
        return self.get_snapshot(job_id, pointer.generation, pointer.digest)

    def get_snapshot(self, job_id: str, generation: int, digest: str) -> JobSpec:
        path = self._snapshot_path(job_id, generation, digest)
        if not path.exists():
            raise NotFound(
                f"definition snapshot is missing: {job_id} generation {generation} {digest}"
            )
        spec = JobSpec.from_dict(_read_json(path))
        if (
            spec.generation != generation
            or spec.job_id != job_id
            or spec.digest != digest
        ):
            raise CorruptStore(f"definition snapshot identity/digest mismatch: {path}")
        return spec

    def snapshot_ref(self, spec: JobSpec) -> str:
        return str(self._snapshot_path(spec.job_id, spec.generation, spec.digest))

    def list(
        self, *, include_tombstones: bool = False
    ) -> tuple[JobSpec | DesiredPointer, ...]:
        if not self.desired_root.exists():
            return ()
        values: list[JobSpec | DesiredPointer] = []
        for path in sorted(self.desired_root.glob("*.json")):
            pointer = DesiredPointer.from_dict(_read_json(path))
            if pointer.state == "tombstone":
                if include_tombstones:
                    values.append(pointer)
                continue
            assert pointer.digest is not None
            values.append(
                self.get_snapshot(pointer.job_id, pointer.generation, pointer.digest)
            )
        return tuple(values)

    def commit_validated(
        self, spec: JobSpec, *, expected_generation: int | None
    ) -> DesiredPointer:
        if spec.profile_id != self.profile.profile_id:
            raise StoreError("JobSpec belongs to another profile")
        owner = {"pid": os.getpid(), "operation": "commit", "job_id": spec.job_id}
        with self.lock_manager.acquire_many(
            [self._management_claim(spec.job_id)], owner=owner
        ):
            current = self.get_current(spec.job_id)
            current_generation = current.generation if current else 0
            expected = 0 if expected_generation is None else expected_generation
            if current_generation != expected:
                raise GenerationConflict(
                    f"job {spec.job_id} is generation {current_generation}, expected {expected}"
                )
            if spec.generation != current_generation + 1:
                raise GenerationConflict(
                    f"new generation must be {current_generation + 1}, got {spec.generation}"
                )
            snapshot = self._snapshot_path(spec.job_id, spec.generation, spec.digest)
            if snapshot.exists():
                existing = JobSpec.from_dict(_read_json(snapshot))
                if existing != spec:
                    raise CorruptStore(f"immutable snapshot collision: {snapshot}")
            else:
                _atomic_json(snapshot, spec.to_dict())
            pointer = DesiredPointer(
                profile_id=spec.profile_id,
                job_id=spec.job_id,
                generation=spec.generation,
                digest=spec.digest,
                state="active",
                updated_at=utc_now(),
            )
            _atomic_json(self._pointer_path(spec.job_id), pointer.to_dict())
            return pointer

    def put_draft(self, draft: JobDraft) -> JobDraft:
        if draft.profile_id != self.profile.profile_id:
            raise StoreError("draft belongs to another profile")
        with self.lock_manager.acquire_many(
            [self._management_claim(draft.job_id)],
            owner={
                "pid": os.getpid(),
                "operation": "put draft",
                "job_id": draft.job_id,
            },
        ):
            updated = replace(draft, updated_at=utc_now())
            _atomic_json(self._draft_path(updated.draft_id), updated.to_dict())
            return updated

    def get_draft(self, draft_id: str) -> JobDraft:
        path = self._draft_path(draft_id)
        if not path.exists():
            raise NotFound(f"unknown draft: {draft_id}")
        return JobDraft.from_dict(_read_json(path))

    def list_drafts(self) -> tuple[JobDraft, ...]:
        if not self.drafts_root.exists():
            return ()
        return tuple(
            JobDraft.from_dict(_read_json(path))
            for path in sorted(self.drafts_root.glob("*.json"))
        )

    def discard_draft(self, draft_id: str) -> None:
        draft = self.get_draft(draft_id)
        with self.lock_manager.acquire_many(
            [self._management_claim(draft.job_id)],
            owner={
                "pid": os.getpid(),
                "operation": "discard draft",
                "job_id": draft.job_id,
            },
        ):
            self._draft_path(draft_id).unlink(missing_ok=True)

    def commit_draft(
        self,
        draft_id: str,
        registry: CommandRegistry,
        context: ValidationContext,
        *,
        enabled: bool = True,
    ) -> tuple[DesiredPointer, JobSpec]:
        draft = self.get_draft(draft_id)
        if not draft.steps:
            raise StoreError("an empty draft cannot become desired state")
        current = self.get_current(draft.job_id)
        current_generation = current.generation if current else 0
        expected = draft.base_generation or 0
        if current_generation != expected:
            raise GenerationConflict(
                f"draft is based on generation {expected}, current is {current_generation}"
            )
        validated_steps = []
        all_bindings: dict[str, RuntimeBinding] = {}
        for step in draft.steps:
            validated = registry.validate(step.family, step.verb, step.argv, context)
            validated_steps.append(validated.spec)
            for binding in validated.bindings:
                prior = all_bindings.get(binding.name)
                if prior is not None and prior != binding:
                    raise StoreError(f"workflow binding conflict: {binding.name}")
                all_bindings[binding.name] = binding
        created = utc_now()
        if current and current.state == "active" and current.digest:
            created = self.get_snapshot(
                draft.job_id, current.generation, current.digest
            ).created_at
        spec = JobSpec(
            profile_id=self.profile.profile_id,
            job_id=draft.job_id,
            generation=current_generation + 1,
            display_name=draft.display_name,
            enabled=enabled,
            repo_root=str(context.repo_root.resolve()),
            interpreter=str(context.interpreter.resolve()),
            config_path=(
                str(context.config_path.resolve()) if context.config_path else None
            ),
            runtime_bindings=tuple(sorted(all_bindings.values(), key=lambda x: x.name)),
            command_contract_version=COMMAND_CONTRACT_VERSION,
            trigger=draft.trigger,
            steps=tuple(validated_steps),
            policy=draft.policy,
            created_at=created,
            updated_at=utc_now(),
        )
        pointer = self.commit_validated(spec, expected_generation=current_generation)
        self._draft_path(draft_id).unlink(missing_ok=True)
        return pointer, spec

    def tombstone(
        self, job_id: str, *, expected_generation: int | None
    ) -> DesiredPointer:
        with self.lock_manager.acquire_many(
            [self._management_claim(job_id)],
            owner={"pid": os.getpid(), "operation": "tombstone", "job_id": job_id},
        ):
            current = self.get_current(job_id)
            if current is None:
                raise NotFound(f"unknown job: {job_id}")
            if (
                expected_generation is not None
                and current.generation != expected_generation
            ):
                raise GenerationConflict(
                    f"job {job_id} is generation {current.generation}, expected {expected_generation}"
                )
            if current.state == "tombstone":
                return current
            pointer = DesiredPointer(
                profile_id=self.profile.profile_id,
                job_id=job_id,
                generation=current.generation + 1,
                digest=None,
                state="tombstone",
                updated_at=utc_now(),
            )
            _atomic_json(self._pointer_path(job_id), pointer.to_dict())
            return pointer

    def clone_to_draft(self, job_id: str, *, draft_id: str | None = None) -> JobDraft:
        spec = self.get_current_spec(job_id)
        draft = JobDraft(
            draft_id=draft_id or job_id,
            profile_id=spec.profile_id,
            job_id=spec.job_id,
            display_name=spec.display_name,
            trigger=spec.trigger,
            steps=spec.steps,
            policy=spec.policy,
            base_generation=spec.generation,
        )
        return self.put_draft(draft)

    def purge_unreferenced_snapshots(
        self, job_id: str, referenced_digests: Iterable[str]
    ) -> tuple[str, ...]:
        referenced = set(referenced_digests)
        current = self.get_current(job_id)
        if current and current.digest:
            referenced.add(current.digest)
        root = self.snapshots_root / validate_job_id(job_id)
        removed: list[str] = []
        if not root.exists():
            return ()
        for path in root.glob("*.json"):
            digest = path.stem.partition("-")[2]
            if digest and digest not in referenced:
                path.unlink()
                removed.append(digest)
        return tuple(sorted(removed))
