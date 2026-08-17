"""Canonical allowlisted command registry and legacy CLI adapters."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

from .locks import LockManager, canonical_path, path_resource
from .model import (
    COMMAND_CONTRACT_VERSION,
    CommandResult,
    CommandSpec,
    ContractError,
    ResourceClaim,
    RuntimeBinding,
    digest_json,
)


class CommandValidationError(ContractError):
    pass


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    severity: str = "error"

    @property
    def fatal(self) -> bool:
        return self.severity == "error"


@dataclass(frozen=True)
class Capability:
    family: str
    verb: str
    classification: str
    mutation: bool
    network: bool
    requires_run: bool = False

    @property
    def identity(self) -> str:
        return f"{self.family} {self.verb}"


@dataclass(frozen=True)
class ValidationContext:
    repo_root: Path
    interpreter: Path
    config_path: Path | None
    environment: Mapping[str, str]

    @classmethod
    def current(
        cls,
        repo_root: Path | None = None,
        interpreter: Path | None = None,
        config_path: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> "ValidationContext":
        repo = (repo_root or Path(__file__).resolve().parents[1]).resolve()
        config = config_path if config_path is not None else repo / "datacli.toml"
        return cls(
            repo_root=repo,
            interpreter=(interpreter or Path(sys.executable)).resolve(),
            config_path=config.resolve() if config else None,
            environment=dict(os.environ if environment is None else environment),
        )


@dataclass(frozen=True)
class ExecutionContext:
    validation: ValidationContext
    bindings: tuple[RuntimeBinding, ...]
    noninteractive: bool = True
    timeout_seconds: float | None = None

    def binding(self, name: str) -> RuntimeBinding:
        for binding in self.bindings:
            if binding.name == name:
                return binding
        raise KeyError(name)


@dataclass(frozen=True)
class ValidatedCommand:
    spec: CommandSpec
    bindings: tuple[RuntimeBinding, ...]


EODHD_LANES = {
    "us_common",
    "uk_eu",
    "us_etf",
    "index_ref",
    "uk_eu_etf",
    "uk_eu_index_ref",
    "news",
}
EODHD_KINDS = {
    "prices",
    "dividends",
    "splits",
    "fundamentals",
    "news",
    "news_daily",
    "issuer_map",
    "news_issuer_daily",
}

CAPABILITIES = (
    Capability("eodhd", "refresh", "CORE", True, True, True),
    Capability("eodhd", "reindex", "CORE", True, False),
    Capability("sync", "push", "CORE", True, True, True),
    Capability("eodhd", "status", "OPTIONAL", False, False),
    Capability("eodhd", "qc", "OPTIONAL", False, False),
    Capability("macro", "fetch", "OPTIONAL", True, True, True),
    Capability("macro", "status", "OPTIONAL", False, False),
    Capability("sync", "status", "OPTIONAL", False, False),
)


def _read_config(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _fingerprint_file(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _has_eodhd_credential(context: ValidationContext) -> bool:
    if context.environment.get("EODHD_API_KEY", "").strip():
        return True
    if os.name == "nt":
        try:
            import winreg  # type: ignore

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
                value, _ = winreg.QueryValueEx(key, "EODHD_API_KEY")
                if str(value).strip():
                    return True
        except OSError:
            pass
    for root in (context.repo_root, context.repo_root.parent):
        for relative in (
            Path("configs/local/eodhd_api_key.txt"),
            Path("local_cache/eodhd_api_key.txt"),
        ):
            path = root / relative
            try:
                if path.is_file() and path.stat().st_size > 0:
                    return True
            except OSError:
                pass
        path = root / ".env"
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                name, separator, value = line.strip().partition("=")
                if name == "EODHD_API_KEY" and separator and value.strip(" '\""):
                    return True
        except OSError:
            pass
    return False


def _macro_provider(argv: Sequence[str]) -> str:
    for index, token in enumerate(argv):
        if token.startswith("--provider="):
            return token.partition("=")[2]
        if token == "--provider" and index + 1 < len(argv):
            return argv[index + 1]
    return "all"


def _binding(
    name: str,
    value: str | Path,
    source: str,
    *,
    resource_id: str | None = None,
    fingerprint: str | None = None,
) -> RuntimeBinding:
    rendered = str(Path(value).resolve()) if isinstance(value, Path) else str(value)
    return RuntimeBinding(
        name=name,
        resource_id=resource_id or path_resource(rendered),
        resolved_value=rendered,
        source=source,
        fingerprint=fingerprint,
    )


def _contains_path(parent: Path, child: Path) -> bool:
    try:
        return os.path.commonpath(
            [canonical_path(parent), canonical_path(child)]
        ) == canonical_path(parent)
    except ValueError:
        return False


def _parse_known_options(
    argv: Sequence[str],
    *,
    boolean: set[str],
    scalar: set[str],
    multi: set[str] | None = None,
) -> tuple[list[str], dict[str, list[str]]]:
    multi = multi or set()
    positionals: list[str] = []
    options: dict[str, list[str]] = {}
    i = 0
    consuming_multi: str | None = None
    while i < len(argv):
        token = argv[i]
        if consuming_multi and not token.startswith("-"):
            options.setdefault(consuming_multi, []).append(token)
            i += 1
            continue
        consuming_multi = None
        if not token.startswith("-"):
            positionals.append(token)
            i += 1
            continue
        name, equals, inline = token.partition("=")
        if name in boolean:
            if equals:
                raise CommandValidationError(f"{name} does not take a value")
            options[name] = []
            i += 1
            continue
        if name in scalar or name in multi:
            values: list[str]
            if equals:
                if not inline:
                    raise CommandValidationError(f"{name} needs a value")
                values = [inline]
                i += 1
            else:
                if i + 1 >= len(argv) or argv[i + 1].startswith("-"):
                    raise CommandValidationError(f"{name} needs a value")
                values = [argv[i + 1]]
                i += 2
            options.setdefault(name, []).extend(values)
            if name in multi:
                consuming_multi = name
            continue
        raise CommandValidationError(
            f"unsupported option for scheduled command: {name}"
        )
    return positionals, options


def _reject_secret_arguments(argv: Sequence[str]) -> None:
    patterns = (
        re.compile(
            r"(?i)(?:api[-_]?key|password|secret|access[-_]?token|refresh[-_]?token)="
        ),
        re.compile(r"(?i)^bearer\s+"),
        re.compile(r"(?i)[?&](?:signature|sig|token|key)=.+"),
    )
    secret_flags = {
        "--api-key",
        "--apikey",
        "--password",
        "--token",
        "--secret",
        "--client-secret",
    }
    for token in argv:
        name = token.partition("=")[0].casefold()
        if name in secret_flags or any(pattern.search(token) for pattern in patterns):
            raise CommandValidationError(
                "secrets are not valid scheduled-command arguments; use the existing credential store"
            )


class CommandRegistry:
    def __init__(self) -> None:
        self._capabilities = {(c.family, c.verb): c for c in CAPABILITIES}

    @property
    def contract_version(self) -> str:
        return COMMAND_CONTRACT_VERSION

    def list_capabilities(self) -> tuple[Capability, ...]:
        return CAPABILITIES

    def validate(
        self,
        family: str,
        verb: str,
        argv: Sequence[str],
        context: ValidationContext,
        *,
        allow_dry_run: bool = False,
    ) -> ValidatedCommand:
        capability = self._capabilities.get((family, verb))
        if capability is None:
            raise CommandValidationError(
                f"command is not admitted for scheduling: {family} {verb}"
            )
        literal = tuple(str(arg) for arg in argv)
        if any("\x00" in arg for arg in literal):
            raise CommandValidationError("command arguments cannot contain NUL")
        _reject_secret_arguments(literal)
        self._validate_arguments(capability, literal, allow_dry_run=allow_dry_run)
        bindings, resources = self._resolve(capability, context)
        spec = CommandSpec(
            family=family,
            verb=verb,
            argv=literal,
            contract_version=self.contract_version,
            resources=resources,
            mutation=capability.mutation,
            network=capability.network,
        )
        return ValidatedCommand(spec=spec, bindings=bindings)

    def _validate_arguments(
        self, capability: Capability, argv: tuple[str, ...], *, allow_dry_run: bool
    ) -> None:
        identity = capability.identity
        options: dict[str, list[str]]
        positionals: list[str]
        if identity == "eodhd refresh":
            positionals, options = _parse_known_options(
                argv,
                boolean={
                    "--run",
                    "--fast",
                    "--with-fundamentals",
                    "--no-universe",
                    "--keep-going",
                    "--full-refresh",
                },
                scalar={"--datasets", "--days", "--to", "--limit"},
                multi={"--tickers"},
            )
            unknown = set(positionals) - EODHD_LANES
            if unknown:
                raise CommandValidationError(
                    f"unknown EODHD lane: {sorted(unknown)[0]}"
                )
            if "--days" in options:
                try:
                    if int(options["--days"][-1]) <= 0:
                        raise ValueError
                except ValueError as exc:
                    raise CommandValidationError(
                        "--days must be a positive integer"
                    ) from exc
            if "--limit" in options:
                try:
                    if int(options["--limit"][-1]) <= 0:
                        raise ValueError
                except ValueError as exc:
                    raise CommandValidationError(
                        "--limit must be a positive integer"
                    ) from exc
            if "--datasets" in options:
                kinds = {
                    item.strip()
                    for item in options["--datasets"][-1].split(",")
                    if item.strip()
                }
                unknown_kinds = kinds - EODHD_KINDS
                if unknown_kinds:
                    raise CommandValidationError(
                        f"unknown EODHD dataset kind: {sorted(unknown_kinds)[0]}"
                    )
            if "--to" in options:
                try:
                    date.fromisoformat(options["--to"][-1])
                except ValueError as exc:
                    raise CommandValidationError("--to must be an ISO date") from exc
            if "--days" in options and "--fast" not in options:
                raise CommandValidationError("--days applies only with --fast")
            if "--fast" in options and any(
                option in options
                for option in (
                    "--full-refresh",
                    "--to",
                    "--limit",
                    "--tickers",
                    "--no-universe",
                )
            ):
                raise CommandValidationError(
                    "--fast cannot be combined with targeted refresh flags"
                )
        elif identity == "eodhd reindex":
            if argv:
                raise CommandValidationError("eodhd reindex takes no arguments")
            options = {}
        elif identity == "eodhd status":
            positionals, options = _parse_known_options(
                argv,
                boolean={"--deep", "--json", "--no-discovery", "--no-color", "--color"},
                scalar={"--as-of", "--stale-days"},
            )
            if len(positionals) > 1 or (
                positionals and positionals[0] not in {*EODHD_LANES, "all"}
            ):
                raise CommandValidationError(
                    "eodhd status accepts at most one known lane"
                )
        elif identity == "eodhd qc":
            positionals, options = _parse_known_options(
                argv,
                boolean={"--all", "--json", "--deep", "--no-color", "--color"},
                scalar={
                    "--since",
                    "--as-of",
                    "--stale-days",
                    "--min-history-days-for-density",
                    "--min-recent-252-rows",
                    "--min-history-density",
                    "--min-recent-volume-window",
                    "--max-zero-volume-ratio",
                    "--max-flags-per-lane",
                },
            )
            if len(positionals) > 2:
                raise CommandValidationError(
                    "eodhd qc accepts lane and optional dataset"
                )
            if positionals and positionals[0] not in {*EODHD_LANES, "all"}:
                raise CommandValidationError(f"unknown EODHD lane: {positionals[0]}")
            if len(positionals) == 2 and positionals[1] not in {
                "prices",
                "dividends",
                "splits",
            }:
                raise CommandValidationError(f"unknown QC dataset: {positionals[1]}")
        elif identity == "macro fetch":
            positionals, options = _parse_known_options(
                argv,
                boolean={"--run", "--full"},
                scalar={"--provider"},
            )
            if positionals:
                raise CommandValidationError("macro fetch takes flags only")
            provider = options.get("--provider", ["all"])[-1]
            if provider not in {"fred", "eodhd", "all"}:
                raise CommandValidationError(
                    "macro provider must be fred, eodhd or all"
                )
        elif identity == "macro status":
            if argv:
                raise CommandValidationError("macro status takes no arguments")
            options = {}
        elif identity in {"sync push", "sync status"}:
            allowed_boolean = {"--with-caches"}
            if identity == "sync push":
                allowed_boolean |= {"--run", "--keep-going"}
            positionals, options = _parse_known_options(
                argv, boolean=allowed_boolean, scalar=set()
            )
            if positionals:
                raise CommandValidationError(f"{identity} takes flags only")
        else:  # pragma: no cover - registry table and validator are kept together
            raise CommandValidationError(f"no validator for {identity}")
        if capability.requires_run and "--run" not in options and not allow_dry_run:
            raise CommandValidationError(
                f"scheduled mutating command requires its own --run flag: {identity}"
            )

    def _resolve(
        self, capability: Capability, context: ValidationContext
    ) -> tuple[tuple[RuntimeBinding, ...], tuple[ResourceClaim, ...]]:
        config = _read_config(context.config_path)
        env_root = context.environment.get("EODHD_DATA_ROOT")
        configured_root = (
            config.get("eodhd", {}).get("data_root")
            if isinstance(config.get("eodhd"), dict)
            else None
        )
        if env_root:
            eodhd_root, root_source = Path(env_root), "env"
        elif configured_root:
            eodhd_root, root_source = Path(str(configured_root)), "config"
        else:
            eodhd_root, root_source = (
                context.repo_root.parent / "btest" / "data" / "raw" / "eodhd",
                "default",
            )
        eodhd_root = eodhd_root.expanduser().resolve()
        bindings: list[RuntimeBinding] = []
        claims: list[ResourceClaim] = []
        if context.config_path is not None:
            config_binding = _binding(
                "config_path",
                context.config_path,
                "profile",
                fingerprint=_fingerprint_file(context.config_path),
            )
            bindings.append(config_binding)
            claims.append(ResourceClaim(config_binding.resource_id, "shared"))

        identity = capability.identity
        if capability.family in {"eodhd", "sync"}:
            root_binding = _binding("eodhd_data_root", eodhd_root, root_source)
            bindings.append(root_binding)
            mode = (
                "exclusive"
                if identity in {"eodhd refresh", "eodhd reindex"}
                else "shared"
            )
            claims.append(ResourceClaim(root_binding.resource_id, mode))

        if capability.family == "macro":
            macro_section = (
                config.get("macro", {}) if isinstance(config.get("macro"), dict) else {}
            )
            macro_value = macro_section.get("data_root")
            macro_root = (
                Path(str(macro_value)).expanduser().resolve()
                if macro_value
                else (eodhd_root.parent / "macro").resolve()
            )
            macro_binding = _binding(
                "macro_data_root", macro_root, "config" if macro_value else "derived"
            )
            bindings.append(macro_binding)
            claims.append(
                ResourceClaim(
                    macro_binding.resource_id,
                    "exclusive" if capability.mutation else "shared",
                )
            )

        if capability.family == "sync":
            sync = (
                config.get("sync", {}) if isinstance(config.get("sync"), dict) else {}
            )
            backend = str(sync.get("backend") or "gdrive")
            if backend not in {"gdrive", "local"}:
                raise CommandValidationError(f"unknown sync backend: {backend}")
            backend_binding = RuntimeBinding(
                name="sync_backend",
                resource_id=f"sync-backend:{backend}",
                resolved_value=backend,
                source="config" if sync.get("backend") else "default",
            )
            bindings.append(backend_binding)
            manifest = eodhd_root / ".sync" / f"{backend}.json"
            manifest_binding = _binding("sync_manifest", manifest, "derived")
            bindings.append(manifest_binding)
            claims.append(
                ResourceClaim(
                    manifest_binding.resource_id,
                    "exclusive" if capability.mutation else "shared",
                )
            )
            if backend == "local":
                if not sync.get("local_dest"):
                    raise CommandValidationError(
                        "local sync backend requires [sync] local_dest"
                    )
                target = Path(str(sync["local_dest"])).expanduser().resolve()
                if _contains_path(eodhd_root, target) or _contains_path(
                    target, eodhd_root
                ):
                    raise CommandValidationError(
                        "sync source and local destination must not contain one another"
                    )
                target_binding = _binding("sync_target", target, "config")
            else:
                remote = str(sync.get("remote_root") or "datacli/eodhd").strip("/")
                target_binding = RuntimeBinding(
                    name="sync_target",
                    resource_id=f"gdrive-target:{remote.casefold()}",
                    resolved_value=f"gdrive:/{remote}",
                    source="config" if sync.get("remote_root") else "default",
                )
                token_value = sync.get("gdrive_token")
                token_path = (
                    Path(str(token_value)).expanduser().resolve()
                    if token_value
                    else (Path.home() / ".datacli" / "tokens" / "gdrive.json").resolve()
                )
                token_binding = _binding(
                    "credential_cache",
                    token_path,
                    "config" if token_value else "default",
                )
                bindings.append(token_binding)
                claims.append(
                    ResourceClaim(
                        token_binding.resource_id,
                        "exclusive" if capability.mutation else "shared",
                    )
                )
            bindings.append(target_binding)
            claims.append(
                ResourceClaim(
                    target_binding.resource_id,
                    "exclusive" if capability.mutation else "shared",
                )
            )

        merged: dict[str, ResourceClaim] = {}
        for claim in claims:
            prior = merged.get(claim.resource_id)
            if prior is None or claim.mode == "exclusive":
                merged[claim.resource_id] = claim
        return tuple(bindings), tuple(
            sorted(merged.values(), key=lambda x: x.resource_id)
        )

    def resolve_workflow_bindings(
        self, commands: Sequence[CommandSpec], context: ValidationContext
    ) -> tuple[RuntimeBinding, ...]:
        merged: dict[str, RuntimeBinding] = {}
        for command in commands:
            validated = self.validate(
                command.family, command.verb, command.argv, context
            )
            if validated.spec.contract_version != command.contract_version:
                raise CommandValidationError("command contract version drift")
            for binding in validated.bindings:
                prior = merged.get(binding.name)
                if prior is not None and prior != binding:
                    raise CommandValidationError(
                        f"workflow resolves conflicting binding {binding.name}"
                    )
                merged[binding.name] = binding
        return tuple(sorted(merged.values(), key=lambda x: x.name))

    def preflight(
        self,
        command: CommandSpec,
        phase: str,
        context: ExecutionContext,
    ) -> tuple[Finding, ...]:
        if phase not in {"static", "readiness", "runtime"}:
            raise ValueError(f"unknown preflight phase: {phase}")
        findings: list[Finding] = []
        validation = context.validation
        if phase in {"readiness", "runtime"}:
            if not validation.repo_root.is_dir():
                findings.append(
                    Finding(
                        "missing_repo", f"repository is missing: {validation.repo_root}"
                    )
                )
            if not validation.interpreter.is_file():
                findings.append(
                    Finding(
                        "missing_interpreter",
                        f"interpreter is missing: {validation.interpreter}",
                    )
                )
            for binding in context.bindings:
                if binding.name in {"eodhd_data_root", "macro_data_root"}:
                    path = Path(binding.resolved_value)
                    if command.mutation:
                        parent = path if path.exists() else path.parent
                        if not parent.exists():
                            findings.append(
                                Finding(
                                    "missing_data_parent",
                                    f"data-root parent is missing: {parent}",
                                )
                            )
                    elif not path.exists():
                        findings.append(
                            Finding(
                                "missing_data_root", f"data root is missing: {path}"
                            )
                        )
            if command.family == "sync" and command.verb == "push":
                try:
                    backend = context.binding("sync_backend").resolved_value
                except KeyError:
                    backend = ""
                if backend == "gdrive":
                    token = Path(context.binding("credential_cache").resolved_value)
                    if not token.exists():
                        findings.append(
                            Finding(
                                "gdrive_login_required",
                                "Google Drive is not signed in; run `sync login` interactively",
                            )
                        )
            if command.family == "eodhd" and command.verb == "refresh":
                if not _has_eodhd_credential(validation):
                    findings.append(
                        Finding(
                            "eodhd_key_missing",
                            "EODHD credentials are unavailable to the scheduled user",
                        )
                    )
            if command.family == "macro" and command.verb == "fetch":
                provider = _macro_provider(command.argv)
                if (
                    provider in {"fred", "all"}
                    and not validation.environment.get("FRED_API_KEY", "").strip()
                ):
                    findings.append(
                        Finding(
                            "fred_key_missing",
                            "FRED_API_KEY is unavailable to the scheduled user",
                        )
                    )
                if provider in {"eodhd", "all"} and not _has_eodhd_credential(
                    validation
                ):
                    findings.append(
                        Finding(
                            "eodhd_key_missing",
                            "EODHD credentials are unavailable to the scheduled user",
                        )
                    )
        return tuple(findings)

    def execute(self, command: CommandSpec, context: ExecutionContext) -> CommandResult:
        validation = context.validation
        env = dict(validation.environment)
        env.update(
            {
                "DATACLI_NONINTERACTIVE": "1" if context.noninteractive else "0",
                "DATACLI_LOCKS_HELD": "1",
                "NO_COLOR": "1",
            }
        )
        with contextlib.suppress(KeyError):
            env["EODHD_DATA_ROOT"] = context.binding("eodhd_data_root").resolved_value
        with contextlib.suppress(KeyError):
            env["DATACLI_MACRO_ROOT"] = context.binding(
                "macro_data_root"
            ).resolved_value
        if validation.config_path is not None:
            env["DATACLI_CONFIG_PATH"] = str(validation.config_path)

        if command.family == "eodhd":
            args = [
                str(validation.interpreter),
                str(validation.repo_root / "eodhd" / "cli.py"),
                command.verb,
                *command.argv,
            ]
        elif command.family == "macro":
            args = [
                str(validation.interpreter),
                "-m",
                "macro.cli",
                command.verb,
                *command.argv,
            ]
        elif command.family == "sync":
            args = [
                str(validation.interpreter),
                "-m",
                "storage.cli",
                command.verb,
                *command.argv,
            ]
        else:  # pragma: no cover
            raise CommandValidationError(f"no adapter for family {command.family}")

        try:
            completed, tree_controlled = _run_process_tree(
                args,
                cwd=validation.repo_root,
                env=env,
                timeout=context.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            tree_controlled = bool(getattr(exc, "tree_controlled", False))
            return CommandResult(
                outcome="timed_out" if tree_controlled else "failed",
                exit_code=None,
                failure_class=(
                    "timeout" if tree_controlled else "timeout_tree_unknown"
                ),
                effect="unknown" if command.mutation else "none",
                retry_guidance="inspect" if command.mutation else "safe",
                summary=(
                    "process tree timed out"
                    if tree_controlled
                    else "top-level process timed out; descendant state is unknown"
                ),
                stdout=_decode_timeout(exc.stdout),
                stderr=_decode_timeout(exc.stderr),
            )
        rc = completed.returncode
        if rc == 0:
            outcome = "succeeded"
            if (
                command.family == "sync"
                and command.verb == "push"
                and "Everything in sync." in completed.stdout
            ):
                outcome = "no_op"
            return CommandResult(
                outcome=outcome,
                exit_code=rc,
                effect=(
                    "none" if not command.mutation or outcome == "no_op" else "complete"
                ),
                retry_guidance="safe",
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        return CommandResult(
            outcome="failed",
            exit_code=rc,
            failure_class="command_exit",
            effect="partial" if command.mutation else "none",
            retry_guidance="inspect" if command.mutation else "safe",
            stdout=completed.stdout,
            stderr=completed.stderr,
            summary=f"command exited with {rc}",
        )


def _decode_timeout(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


class _WindowsJob:
    def __init__(self) -> None:
        self.handle = None

    def assign(self, process_handle: int) -> bool:
        if os.name != "nt":
            return False
        import ctypes
        from ctypes import wintypes

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel = ctypes.windll.kernel32
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            return False
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        ok = kernel.SetInformationJobObject(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info)
        ) and kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(process_handle))
        if not ok:
            kernel.CloseHandle(handle)
            return False
        self.handle = handle
        return True

    def terminate(self) -> bool:
        if self.handle is None:
            return False
        import ctypes

        return bool(ctypes.windll.kernel32.TerminateJobObject(self.handle, 3))

    def close(self) -> None:
        if self.handle is not None:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


def _run_process_tree(
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float | None,
) -> tuple[subprocess.CompletedProcess[str], bool]:
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        list(args),
        cwd=str(cwd),
        env=dict(env),
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    job = _WindowsJob()
    tree_controlled = bool(os.name == "nt" and job.assign(int(process._handle)))  # type: ignore[attr-defined]
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if not job.terminate():
            process.kill()
        stdout, stderr = process.communicate()
        exc.stdout = stdout  # type: ignore[assignment]
        exc.stderr = stderr  # type: ignore[assignment]
        exc.tree_controlled = tree_controlled  # type: ignore[attr-defined]
        raise
    finally:
        job.close()
    return (
        subprocess.CompletedProcess(list(args), process.returncode, stdout, stderr),
        tree_controlled,
    )


_REGISTRY = CommandRegistry()


def default_registry() -> CommandRegistry:
    return _REGISTRY


def workflow_binding_fingerprint(bindings: Sequence[RuntimeBinding]) -> str:
    return digest_json({"bindings": [vars(binding) for binding in bindings]})


@contextlib.contextmanager
def direct_mutation_lock(
    family: str,
    verb: str,
    argv: Sequence[str],
    *,
    context: ValidationContext | None = None,
    lock_manager: LockManager | None = None,
) -> Iterator[None]:
    """Give legacy direct CLIs the same resource lock path as scheduled runs."""

    if os.environ.get("DATACLI_LOCKS_HELD") == "1":
        yield
        return
    capability = _REGISTRY._capabilities.get((family, verb))
    mutating_now = bool(
        capability
        and capability.mutation
        and (not capability.requires_run or "--run" in argv)
    )
    if not mutating_now:
        yield
        return
    try:
        validated = _REGISTRY.validate(
            family,
            verb,
            argv,
            context or ValidationContext.current(),
            allow_dry_run=True,
        )
    except CommandValidationError:
        # The owning CLI remains the presentation/parser authority for direct
        # calls. Invalid/help invocations cannot mutate and need no lock.
        yield
        return
    manager = lock_manager or LockManager()
    with manager.acquire_many(
        validated.spec.resources,
        owner={
            "pid": os.getpid(),
            "interface": "direct",
            "command": f"{family} {verb}",
        },
    ):
        yield


@contextlib.contextmanager
def config_write_lock(config_path: Path) -> Iterator[None]:
    if os.environ.get("DATACLI_LOCKS_HELD") == "1":
        yield
        return
    claim = ResourceClaim(path_resource(config_path), "exclusive")
    with LockManager().acquire_many(
        [claim],
        owner={"pid": os.getpid(), "interface": "direct", "command": "config write"},
    ):
        yield
