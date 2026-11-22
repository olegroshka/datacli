"""Timezone drift must mean a zone change, not a daylight-saving change."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scheduler.backends.base import FakeBackend
from scheduler.commands import ValidationContext
from scheduler.model import TriggerSpec
from scheduler.service import ScheduleService
from scheduler.store import JobStore, ProfileRegistry

REPO = Path(__file__).resolve().parents[1]
LONDON = ("GMT Standard Time", "GMT Summer Time")
ZONE_FINDING = "system timezone differs from the timezone recorded at validation"


def _clock(monkeypatch, tzname: tuple[str, str], *, dst: bool) -> None:
    fake = SimpleNamespace(
        tzname=tzname, localtime=lambda: SimpleNamespace(tm_isdst=int(dst))
    )
    monkeypatch.setattr("scheduler.model.time", fake)


def _service(tmp_path: Path) -> ScheduleService:
    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "datacli.toml"
    config.write_text(f'[eodhd]\ndata_root = "{data.as_posix()}"\n', encoding="utf-8")
    state = tmp_path / "state"
    profile = ProfileRegistry(state).ensure(REPO, Path(sys.executable), config)
    context = ValidationContext.current(
        REPO, Path(sys.executable), config, environment={}
    )
    return ScheduleService(
        profile, JobStore(profile, state), FakeBackend(), validation_context=context
    )


def _enable(service: ScheduleService, zone: str) -> None:
    trigger = TriggerSpec(
        kind="daily",
        local_time="06:00",
        time_basis="system_local",
        timezone_at_validation=zone,
    )
    service.create_draft("tz", trigger)
    service.add_step("tz", "eodhd", "reindex", [])
    service.enable_draft("tz")


@pytest.mark.parametrize("dst", [False, True])
def test_trigger_records_the_standard_name_all_year(monkeypatch, dst) -> None:
    _clock(monkeypatch, LONDON, dst=dst)
    assert TriggerSpec.daily("06:00").timezone_at_validation == "GMT Standard Time"


@pytest.mark.parametrize("recorded", LONDON)
@pytest.mark.parametrize("dst", [False, True])
def test_daylight_saving_is_not_drift(tmp_path, monkeypatch, recorded, dst) -> None:
    # A job validated in summer before the fix holds "GMT Summer Time".
    _clock(monkeypatch, LONDON, dst=dst)
    service = _service(tmp_path)
    _enable(service, recorded)
    status = service.status("tz")
    assert status.state == "in_sync"
    assert ZONE_FINDING not in status.findings


@pytest.mark.parametrize("recorded", LONDON)
def test_a_real_zone_change_is_still_drift(tmp_path, monkeypatch, recorded) -> None:
    _clock(monkeypatch, LONDON, dst=False)
    service = _service(tmp_path)
    _enable(service, recorded)
    _clock(monkeypatch, ("Pacific Standard Time", "Pacific Daylight Time"), dst=False)
    status = service.status("tz")
    assert status.state == "incompatible"
    assert ZONE_FINDING in status.findings
