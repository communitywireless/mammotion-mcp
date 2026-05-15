"""Tests for Tier-1 mow_area canonical sequence.

These tests verify the ORDER of HA service calls + safety-gate enforcement.
The live mow test is in test_live_mow.py and gated by an env var.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

from mammotion_mcp import area_resolver
from mammotion_mcp.ha_client import HAClient, MowerStatus
from mammotion_mcp.safety import HST, SafetyGate, SafetyViolation
from mammotion_mcp.tools import mow as mow_module

DATA_PATH = str(Path(__file__).parent.parent / "mammotion_mcp" / "data" / "area-mapping.json")


class _MockHAClient:
    """Tracks the order of call_service invocations."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.mower_entity_id = "lawn_mower.luba2_awd_1"
        # State machine: status reads return increasing battery + state
        self._status_states = [
            MowerStatus(
                state="docked", activity_mode="MODE_READY", charging=True,
                battery_pct=100, last_error_code=0, last_error_time=None,
                blade_used_time_hr=166.0, last_changed=None,
            ),
        ]
        # Track number of state polls so we can simulate state→mowing transition
        self._state_poll_count = 0
        self._charging_poll_count = 0

    async def call_service(self, domain: str, service: str, **service_data) -> list:
        self.calls.append((domain, service, service_data))
        return []

    async def get_mower_status(self) -> MowerStatus:
        return self._status_states[0]

    async def get_state(self, entity_id: str | None = None) -> dict:
        # First few polls return docked, then transition to mowing
        eid = entity_id or self.mower_entity_id
        if "charging" in eid:
            self._charging_poll_count += 1
            # Return on=charging immediately for fast tests
            return {"state": "on"}
        self._state_poll_count += 1
        if self._state_poll_count >= 2:
            return {"state": "mowing"}
        return {"state": "docked"}


class _FakeServer:
    """Captures tool decorators so we can call them directly."""

    def __init__(self) -> None:
        self.tools: dict[str, callable] = {}

    def tool(self, *_a, **_kw):  # mimic FastMCP.tool decorator
        def _wrap(fn):
            self.tools[fn.__name__] = fn
            return fn
        return _wrap


@pytest.fixture(autouse=True)
def patch_sleep(monkeypatch):
    """Patch asyncio.sleep in the mow module so tests don't actually wait."""
    import asyncio

    real_sleep = asyncio.sleep  # bind original BEFORE patching

    async def fast_sleep(_s: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(mow_module.asyncio, "sleep", fast_sleep)


@pytest.fixture
def gate(tmp_path) -> SafetyGate:
    return SafetyGate(
        quiet_hours_start_hst=21,
        quiet_hours_end_hst=8,
        min_battery_pct=30,
        lock_file_path=str(tmp_path / "test.lock"),
    )


@pytest.fixture
def mapping_env(monkeypatch):
    monkeypatch.setenv("AREA_MAPPING_PATH", DATA_PATH)
    area_resolver.clear_cache()


@pytest.fixture
def daytime_now():
    """Mock datetime.now to 10am HST (outside quiet hours)."""
    with mock.patch("mammotion_mcp.safety.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 14, 10, 0, tzinfo=HST)
        yield mock_dt


@pytest.fixture
def nighttime_now():
    """Mock datetime.now to 22:00 HST (inside quiet hours)."""
    with mock.patch("mammotion_mcp.safety.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 14, 22, 0, tzinfo=HST)
        yield mock_dt


# --- Canonical sequence order -------------------------------------------


@pytest.mark.asyncio
async def test_mow_area_fires_5_step_sequence(daytime_now, gate, mapping_env) -> None:
    """Verify the 5-step canonical order: cancel → blades → start_mow → dock → cancel."""
    server = _FakeServer()
    ha = _MockHAClient()
    mow_module.register(server, ha_client=ha, safety=gate)

    result = await server.tools["mow_area"]("Area 6", blade_height_mm=55)

    # Pull just the service tuples
    sequence = [(d, s) for d, s, _data in ha.calls]
    assert sequence == [
        ("mammotion", "cancel_job"),         # step 1
        ("mammotion", "start_stop_blades"),  # step 2
        ("mammotion", "start_mow"),          # step 3
        ("lawn_mower", "dock"),              # step 4
        ("mammotion", "cancel_job"),         # step 5
    ], f"unexpected sequence: {sequence}"

    assert result["result"] == "mow_complete"
    assert result["area_resolved"] == "switch.luba2_awd_1_area_3439157731089703234"
    assert result["blade_height_mm"] == 55
    assert result["protocol_version"] == 1


@pytest.mark.asyncio
async def test_mow_area_default_blade_height_is_55(
    daytime_now, gate, mapping_env
) -> None:
    """The default blade_height_mm is 55 (Joshua's operational value)."""
    server = _FakeServer()
    ha = _MockHAClient()
    mow_module.register(server, ha_client=ha, safety=gate)

    await server.tools["mow_area"]("Area 6")  # no blade_height_mm — use default

    # Find the start_stop_blades + start_mow calls and verify blade_height=55
    start_blades = next(c for c in ha.calls if c[1] == "start_stop_blades")
    start_mow = next(c for c in ha.calls if c[1] == "start_mow")
    assert start_blades[2]["blade_height"] == 55
    assert start_mow[2]["blade_height"] == 55


@pytest.mark.asyncio
async def test_mow_area_passes_resolved_switch_entity_to_start_mow(
    daytime_now, gate, mapping_env
) -> None:
    server = _FakeServer()
    ha = _MockHAClient()
    mow_module.register(server, ha_client=ha, safety=gate)

    await server.tools["mow_area"]("Area 6")

    start_mow = next(c for c in ha.calls if c[1] == "start_mow")
    assert start_mow[2]["areas"] == ["switch.luba2_awd_1_area_3439157731089703234"]


# --- Safety enforcement -------------------------------------------------


@pytest.mark.asyncio
async def test_mow_area_blocked_during_quiet_hours(
    nighttime_now, gate, mapping_env
) -> None:
    server = _FakeServer()
    ha = _MockHAClient()
    mow_module.register(server, ha_client=ha, safety=gate)

    with pytest.raises(SafetyViolation, match="Quiet hours"):
        await server.tools["mow_area"]("Area 6")

    # NO HA calls should have been made
    assert ha.calls == []


@pytest.mark.asyncio
async def test_mow_area_override_quiet_hours(
    nighttime_now, gate, mapping_env
) -> None:
    """override_quiet_hours=True allows mow at night."""
    server = _FakeServer()
    ha = _MockHAClient()
    mow_module.register(server, ha_client=ha, safety=gate)

    await server.tools["mow_area"]("Area 6", override_quiet_hours=True)
    # Sequence should have fired
    assert len(ha.calls) == 5


@pytest.mark.asyncio
async def test_mow_area_battery_below_min_blocks(
    daytime_now, gate, mapping_env
) -> None:
    server = _FakeServer()
    ha = _MockHAClient()
    # Patch status to return low battery
    ha._status_states = [  # noqa: SLF001
        MowerStatus(
            state="docked", activity_mode="MODE_READY", charging=True,
            battery_pct=20, last_error_code=0, last_error_time=None,
            blade_used_time_hr=166.0, last_changed=None,
        ),
    ]
    mow_module.register(server, ha_client=ha, safety=gate)

    with pytest.raises(SafetyViolation, match="battery"):
        await server.tools["mow_area"]("Area 6")
    # cancel_job + start_stop_blades + start_mow should NOT have fired
    assert all(c[1] != "start_mow" for c in ha.calls)


@pytest.mark.asyncio
async def test_mow_area_blade_height_out_of_bounds(
    daytime_now, gate, mapping_env
) -> None:
    server = _FakeServer()
    ha = _MockHAClient()
    mow_module.register(server, ha_client=ha, safety=gate)

    with pytest.raises(SafetyViolation, match="bounds"):
        await server.tools["mow_area"]("Area 6", blade_height_mm=10)


@pytest.mark.asyncio
async def test_mow_area_unknown_area_raises(daytime_now, gate, mapping_env) -> None:
    server = _FakeServer()
    ha = _MockHAClient()
    mow_module.register(server, ha_client=ha, safety=gate)

    with pytest.raises(ValueError, match="Unknown area"):
        await server.tools["mow_area"]("Bogus Area")
    assert ha.calls == []


# --- return_to_dock semantics -------------------------------------------


@pytest.mark.asyncio
async def test_mow_area_no_dock_returns_after_mowing(
    daytime_now, gate, mapping_env
) -> None:
    """return_to_dock=False → no dock + no post-dock cancel."""
    server = _FakeServer()
    ha = _MockHAClient()
    mow_module.register(server, ha_client=ha, safety=gate)

    result = await server.tools["mow_area"]("Area 6", return_to_dock=False)
    # No lawn_mower.dock fired
    assert all(c != ("lawn_mower", "dock") for c in [(c[0], c[1]) for c in ha.calls])
    # Only ONE mammotion.cancel_job fired (the pre-mow one, not the post-dock one)
    cancels = [c for c in ha.calls if c[1] == "cancel_job"]
    assert len(cancels) == 1
    assert result["result"] == "mowing_started"


@pytest.mark.asyncio
async def test_cancel_job_standalone(daytime_now, gate, mapping_env) -> None:
    server = _FakeServer()
    ha = _MockHAClient()
    mow_module.register(server, ha_client=ha, safety=gate)

    result = await server.tools["cancel_job"]()
    assert any(c[:2] == ("mammotion", "cancel_job") for c in ha.calls)
    assert result["result"] == "job_cancelled"


@pytest.mark.asyncio
async def test_dock_and_clear(daytime_now, gate, mapping_env) -> None:
    server = _FakeServer()
    ha = _MockHAClient()
    mow_module.register(server, ha_client=ha, safety=gate)

    result = await server.tools["dock_and_clear"]()
    sequence = [(d, s) for d, s, _ in ha.calls]
    assert sequence == [("lawn_mower", "dock"), ("mammotion", "cancel_job")]
    assert result["result"] == "docked_and_cleared"
