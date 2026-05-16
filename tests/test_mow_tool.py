"""Tests for Tier-1 mow_area canonical sequence + v1.1 verification phase.

These tests verify:
- The ORDER of HA service calls + safety-gate enforcement (v1.0)
- The 3-phase verification logic + failure-path semantics (v1.1)

The live mow test is in test_live_mow.py and gated by an env var.

v1.1 design note: tests that focus on the canonical-sequence ORDER use
``verify=False`` to skip the verification phase (v1.0 fast path). Tests
that focus on verification behavior use ``verify=True`` with mocks that
fabricate the state-history sequence the verifier will observe.
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
    """Tracks the order of call_service invocations.

    Default behavior: simulates a successful mow cycle for v1.0 verify=False
    callers — state transitions to ``mowing`` after a couple polls, charging
    flips to ``on`` immediately. v1.1 verification-path tests should override
    ``get_state`` and ``safe_float_state`` via subclass / monkeypatch to
    fabricate richer state sequences.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.mower_entity_id = "lawn_mower.luba2_awd_1"
        self._status_states = [
            MowerStatus(
                state="docked", activity_mode="MODE_READY", charging=True,
                battery_pct=100, last_error_code=0, last_error_time=None,
                blade_used_time_hr=166.0, last_changed=None,
            ),
        ]
        self._state_poll_count = 0
        self._charging_poll_count = 0

    async def call_service(self, domain: str, service: str, **service_data) -> list:
        self.calls.append((domain, service, service_data))
        return []

    async def get_mower_status(self) -> MowerStatus:
        return self._status_states[0]

    async def get_state(self, entity_id: str | None = None) -> dict:
        eid = entity_id or self.mower_entity_id
        if "charging" in eid:
            self._charging_poll_count += 1
            return {"state": "on"}
        self._state_poll_count += 1
        if self._state_poll_count >= 2:
            return {"state": "mowing"}
        return {"state": "docked"}

    async def safe_float_state(self, entity_id: str) -> float | None:
        # Default: blade_used_time stays flat (won't satisfy Phase 3)
        if "blade_used_time" in entity_id:
            return 166.0
        return None


class _VerifyMockHAClient(_MockHAClient):
    """Stateful mock that lets tests script the verification poll sequence.

    Tests construct one of these, populate the ``state_sequence`` dict with
    lists keyed by entity-id-suffix, and the mock yields one value per
    ``get_state`` call (sticking on the last value once exhausted).
    """

    def __init__(self) -> None:
        super().__init__()
        # Each key: a list of values. As get_state is called, we pop[0]-style.
        # Sticks on last value once the list is single-item.
        self.state_sequence: dict[str, list[str]] = {
            "lawn_mower": ["mowing"],
            "activity_mode": ["MODE_WORKING"],
            "work_area": ["Not working"],
            "progress": ["0"],
        }
        self.blade_used_sequence: list[float | None] = [166.0]

    def _next(self, key: str) -> str | None:
        seq = self.state_sequence.get(key)
        if not seq:
            return None
        if len(seq) > 1:
            return seq.pop(0)
        return seq[0]

    async def get_state(self, entity_id: str | None = None) -> dict:
        eid = entity_id or self.mower_entity_id
        # charging is hit only during dock recovery / v1.0 path
        if "charging" in eid:
            return {"state": "on"}
        # Map entity id → sequence key
        if eid.startswith("lawn_mower."):
            return {"state": self._next("lawn_mower")}
        if "activity_mode" in eid:
            return {"state": self._next("activity_mode")}
        if "work_area" in eid:
            return {"state": self._next("work_area")}
        if "progress" in eid:
            return {"state": self._next("progress")}
        if "blade_used_time" in eid:
            # blade reads via get_state path — pop a value
            val = self.blade_used_sequence[0]
            if len(self.blade_used_sequence) > 1:
                val = self.blade_used_sequence.pop(0)
            return {"state": str(val) if val is not None else "unavailable"}
        return {"state": "docked"}

    async def safe_float_state(self, entity_id: str) -> float | None:
        if "blade_used_time" in entity_id:
            if not self.blade_used_sequence:
                return None
            if len(self.blade_used_sequence) > 1:
                return self.blade_used_sequence.pop(0)
            return self.blade_used_sequence[0]
        return None


class _FakeServer:
    """Captures tool decorators so we can call them directly."""

    def __init__(self) -> None:
        self.tools: dict[str, callable] = {}

    def tool(self, *_a, **_kw):
        def _wrap(fn):
            self.tools[fn.__name__] = fn
            return fn
        return _wrap


@pytest.fixture(autouse=True)
def patch_sleep(monkeypatch):
    """Patch asyncio.sleep in the mow module so tests don't actually wait."""
    import asyncio

    real_sleep = asyncio.sleep

    async def fast_sleep(_s: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(mow_module.asyncio, "sleep", fast_sleep)


@pytest.fixture(autouse=True)
def patch_monotonic(monkeypatch):
    """Patch time.monotonic so sustained-mowing thresholds tick in test time.

    Each call increments by a virtual 5 seconds — matches the natural Phase 1
    poll cadence and lets the 30s sustained threshold be reached after ~6 polls.
    """
    counter = {"t": 0.0}

    def fake_monotonic() -> float:
        counter["t"] += 5.0
        return counter["t"]

    monkeypatch.setattr(mow_module.time, "monotonic", fake_monotonic)


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


# =========================================================================
# v1.0 sequence-order tests — verify=False (kept for backward-compat coverage)
# =========================================================================


@pytest.mark.asyncio
async def test_mow_area_fires_5_step_sequence(daytime_now, gate, mapping_env) -> None:
    """Verify the 5-step canonical order: cancel → blades → start_mow → dock → cancel."""
    server = _FakeServer()
    ha = _MockHAClient()
    mow_module.register(server, ha_client=ha, safety=gate)

    result = await server.tools["mow_area"]("Area 6", blade_height_mm=55, verify=False)

    sequence = [(d, s) for d, s, _data in ha.calls]
    assert sequence == [
        ("mammotion", "cancel_job"),         # step 1
        ("mammotion", "start_stop_blades"),  # step 2
        ("mammotion", "start_mow"),          # step 3
        ("lawn_mower", "dock"),              # step 4
        ("mammotion", "cancel_job"),         # step 5
    ], f"unexpected sequence: {sequence}"

    assert result["result"] == "mow_dispatched_unverified"
    assert result["area_resolved"] == "switch.luba2_awd_1_area_3439157731089703234"
    assert result["blade_height_mm"] == 55
    assert result["protocol_version"] == 3


@pytest.mark.asyncio
async def test_mow_area_default_blade_height_is_55(
    daytime_now, gate, mapping_env
) -> None:
    """The default blade_height_mm is 55 (Joshua's operational value)."""
    server = _FakeServer()
    ha = _MockHAClient()
    mow_module.register(server, ha_client=ha, safety=gate)

    await server.tools["mow_area"]("Area 6", verify=False)  # default blade height

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

    await server.tools["mow_area"]("Area 6", verify=False)

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

    assert ha.calls == []


@pytest.mark.asyncio
async def test_mow_area_override_quiet_hours(
    nighttime_now, gate, mapping_env
) -> None:
    """override_quiet_hours=True allows mow at night."""
    server = _FakeServer()
    ha = _MockHAClient()
    mow_module.register(server, ha_client=ha, safety=gate)

    await server.tools["mow_area"](
        "Area 6", override_quiet_hours=True, verify=False
    )
    assert len(ha.calls) == 5


@pytest.mark.asyncio
async def test_mow_area_battery_below_min_blocks(
    daytime_now, gate, mapping_env
) -> None:
    server = _FakeServer()
    ha = _MockHAClient()
    ha._status_states = [  # noqa: SLF001
        MowerStatus(
            state="docked", activity_mode="MODE_READY", charging=True,
            battery_pct=20, last_error_code=0, last_error_time=None,
            blade_used_time_hr=166.0, last_changed=None,
        ),
    ]
    mow_module.register(server, ha_client=ha, safety=gate)

    with pytest.raises(SafetyViolation, match="battery"):
        await server.tools["mow_area"]("Area 6", verify=False)
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


# --- return_to_dock semantics (v1.0 path) -------------------------------


@pytest.mark.asyncio
async def test_mow_area_no_dock_returns_after_mowing(
    daytime_now, gate, mapping_env
) -> None:
    """return_to_dock=False + verify=False → no dock + no post-dock cancel."""
    server = _FakeServer()
    ha = _MockHAClient()
    mow_module.register(server, ha_client=ha, safety=gate)

    result = await server.tools["mow_area"](
        "Area 6", return_to_dock=False, verify=False
    )
    assert all(c != ("lawn_mower", "dock") for c in [(c[0], c[1]) for c in ha.calls])
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


# =========================================================================
# v1.1 verification-phase tests (Investigator §5 pseudocode)
# =========================================================================


@pytest.mark.asyncio
async def test_mow_area_verified_happy_path_autonomous(
    daytime_now, gate, mapping_env
) -> None:
    """v1.2 happy path: Phase 1 sustained → Phase 2 area arrival → Phase 3 blade delta.

    With ``mow_duration_sec=None`` (default), tool returns
    ``mow_complete_autonomous`` and does NOT fire ``lawn_mower.dock`` — mower
    auto-completes the area + auto-docks itself per v1.2 W-003 fix.
    """
    server = _FakeServer()
    ha = _VerifyMockHAClient()

    # Phase 1: state=mowing + MODE_WORKING sustained — sticks
    ha.state_sequence["lawn_mower"] = ["mowing"]
    ha.state_sequence["activity_mode"] = ["MODE_WORKING"]
    # Phase 2: work_area transitions Not working → path → area <hash>
    target_hash = 3439157731089703234
    ha.state_sequence["work_area"] = [
        "Not working", "path", "path", f"area {target_hash}",
    ]
    # Phase 3: baseline 166.0, then jumps to 166.1 (clear delta) — first read
    # is the baseline, subsequent reads return the incremented value.
    ha.blade_used_sequence = [166.0, 166.1]

    mow_module.register(server, ha_client=ha, safety=gate)
    result = await server.tools["mow_area"]("Area 6", verify=True)

    assert result["result"] == "mow_complete_autonomous"
    assert result["verification"]["verified"] is True
    assert result["verification"]["phase_reached"] == 3
    assert result["verification"]["blade_delta_hr"] is not None
    assert result["verification"]["blade_delta_hr"] >= 0.001
    assert result["protocol_version"] == 3
    assert "note" in result
    # 3-step pre-mow sequence ONLY (cancel + blades + start_mow).
    # NO dock-fire — that was the v1.1 bug. NO post-dock cancel either.
    sequence = [(d, s) for d, s, _ in ha.calls]
    assert sequence == [
        ("mammotion", "cancel_job"),
        ("mammotion", "start_stop_blades"),
        ("mammotion", "start_mow"),
    ]
    assert ("lawn_mower", "dock") not in sequence


@pytest.mark.asyncio
async def test_mow_area_phase1_preflight_abort(
    daytime_now, gate, mapping_env
) -> None:
    """state=mowing for 24s then paused with progress=0 → Phase 1 fail."""
    server = _FakeServer()
    ha = _VerifyMockHAClient()

    # Mowing observed once, then paused before sustained threshold reached.
    # First poll: mowing (sets mowing_start_time).
    # Second poll: paused (transition before 30s sustained → preflight check).
    ha.state_sequence["lawn_mower"] = [
        "mowing", "paused", "paused",
    ]
    ha.state_sequence["activity_mode"] = [
        "MODE_WORKING", "MODE_PAUSE", "MODE_PAUSE",
    ]
    ha.state_sequence["progress"] = ["0"]

    mow_module.register(server, ha_client=ha, safety=gate)
    result = await server.tools["mow_area"](
        "Area 6", verify=True, return_to_dock=False
    )

    assert result["result"] == "mow_failed_verification"
    assert result["verification"]["verified"] is False
    assert result["verification"]["phase_reached"] == 1
    assert "preflight" in result["verification"]["detail"].lower()


@pytest.mark.asyncio
async def test_mow_area_phase2_never_reached_area(
    daytime_now, gate, mapping_env
) -> None:
    """Sustained mowing OK, but work_area stays 'path' then mower docks.

    Returns ``mow_failed_verification`` with ``phase_reached=2``.
    """
    server = _FakeServer()
    ha = _VerifyMockHAClient()

    # Phase 1: sustained mowing
    ha.state_sequence["lawn_mower"] = [
        "mowing", "mowing", "mowing", "mowing", "mowing", "mowing", "mowing",
        # Then in Phase 2: docked (abort)
        "docked",
    ]
    ha.state_sequence["activity_mode"] = ["MODE_WORKING"]
    # Phase 2: work_area never reaches target
    ha.state_sequence["work_area"] = ["path", "path", "path"]
    ha.state_sequence["progress"] = ["0"]

    mow_module.register(server, ha_client=ha, safety=gate)
    result = await server.tools["mow_area"](
        "Area 6", verify=True, return_to_dock=False
    )

    assert result["result"] == "mow_failed_verification"
    assert result["verification"]["verified"] is False
    assert result["verification"]["phase_reached"] == 2


@pytest.mark.asyncio
async def test_mow_area_phase3_blades_never_engaged(
    daytime_now, gate, mapping_env
) -> None:
    """Sustained mowing + area arrival OK, blade_used_time flat 20 min → Phase 3 fail."""
    server = _FakeServer()
    ha = _VerifyMockHAClient()

    target_hash = 3439157731089703234
    ha.state_sequence["lawn_mower"] = ["mowing"]
    ha.state_sequence["activity_mode"] = ["MODE_WORKING"]
    ha.state_sequence["work_area"] = [f"area {target_hash}"]
    # blade_used_time FLAT — never increments
    ha.blade_used_sequence = [166.0]

    mow_module.register(server, ha_client=ha, safety=gate)
    result = await server.tools["mow_area"](
        "Area 6", verify=True, return_to_dock=False
    )

    assert result["result"] == "mow_failed_verification"
    assert result["verification"]["verified"] is False
    assert result["verification"]["phase_reached"] == 3
    # Detail mentions baseline and final values
    assert "baseline" in result["verification"]["detail"].lower()
    assert "166.000000" in result["verification"]["detail"]


@pytest.mark.asyncio
async def test_mow_area_phase3_stale_sysreport_rollback(
    daytime_now, gate, mapping_env
) -> None:
    """Mid-Phase-3 stale SysReport (current < baseline) → handle gracefully + recover."""
    server = _FakeServer()
    ha = _VerifyMockHAClient()

    target_hash = 3439157731089703234
    ha.state_sequence["lawn_mower"] = ["mowing"]
    ha.state_sequence["activity_mode"] = ["MODE_WORKING"]
    ha.state_sequence["work_area"] = [f"area {target_hash}"]
    # blade_used_sequence: baseline=166.1, then rollback to 166.0 (stale),
    # then recovery to 166.2 (+0.1 hr delta — well above 0.001 threshold)
    ha.blade_used_sequence = [166.1, 166.0, 166.2]

    mow_module.register(server, ha_client=ha, safety=gate)
    result = await server.tools["mow_area"](
        "Area 6", verify=True, return_to_dock=False
    )

    # Despite the rollback, Phase 3 should ultimately succeed.
    # v1.2: with mow_duration_sec=None (default), success result is
    # "mow_complete_autonomous" — mower auto-docks itself, no explicit
    # dock-fire by the tool.
    assert result["result"] == "mow_complete_autonomous"
    assert result["verification"]["verified"] is True
    assert result["verification"]["phase_reached"] == 3
    assert result["verification"]["blade_delta_hr"] >= 0.001


@pytest.mark.asyncio
async def test_mow_area_verify_false_opt_out(
    daytime_now, gate, mapping_env
) -> None:
    """verify=False returns ``mow_dispatched_unverified`` (v1.0 semantics).

    Returns immediately after start_mow ACK + dock cycle (no verification phase).
    """
    server = _FakeServer()
    ha = _MockHAClient()
    mow_module.register(server, ha_client=ha, safety=gate)

    result = await server.tools["mow_area"]("Area 6", verify=False)

    assert result["result"] == "mow_dispatched_unverified"
    assert "verification" not in result
    assert result["protocol_version"] == 3


@pytest.mark.asyncio
async def test_mow_area_dock_recovery_on_verification_failure(
    daytime_now, gate, mapping_env
) -> None:
    """Phase 2 fails AND return_to_dock=True → tool still attempts dock cleanup."""
    server = _FakeServer()
    ha = _VerifyMockHAClient()

    # Phase 1 OK, Phase 2 fails (mower returns to dock before reaching area)
    ha.state_sequence["lawn_mower"] = [
        "mowing", "mowing", "mowing", "mowing", "mowing", "mowing", "mowing",
        "docked",
    ]
    ha.state_sequence["activity_mode"] = ["MODE_WORKING"]
    ha.state_sequence["work_area"] = ["path"]
    ha.state_sequence["progress"] = ["0"]

    mow_module.register(server, ha_client=ha, safety=gate)
    result = await server.tools["mow_area"](
        "Area 6", verify=True, return_to_dock=True
    )

    assert result["result"] == "mow_failed_verification"
    assert result["verification"]["phase_reached"] == 2

    # Dock recovery: lawn_mower.dock fired, post-dock cancel_job fired
    services_called = [(d, s) for d, s, _ in ha.calls]
    assert ("lawn_mower", "dock") in services_called
    # At least 2 cancel_job calls (pre-mow + post-dock recovery)
    cancels = [s for d, s in services_called if (d, s) == ("mammotion", "cancel_job")]
    assert len(cancels) >= 2


# =========================================================================
# v1.2 dock-semantic tests (W-003 root-cause fix)
#
# These tests assert the asymmetric dock-fire behavior introduced in v1.2:
# - mow_duration_sec=None (default) → tool does NOT fire lawn_mower.dock,
#   mower auto-docks itself.
# - mow_duration_sec=N (set) → tool fires explicit dock after duration.
# =========================================================================


@pytest.mark.asyncio
async def test_mow_area_no_duration_no_explicit_dock(
    daytime_now, gate, mapping_env
) -> None:
    """v1.2 W-003 fix: mow_duration_sec=None + return_to_dock=True (defaults)
    must NEVER fire lawn_mower.dock.

    This is the load-bearing assertion. v1.1's unconditional dock-fire after
    verification killed the 2026-05-15 09:25 HST Area 1 mow — HA's mammotion
    integration translates dock-during-MODE_WORKING into pause_execute_task +
    return_to_dock. v1.2 returns autonomous after verification confirms
    blades engaged and lets the mower complete + auto-dock itself.
    """
    server = _FakeServer()
    ha = _VerifyMockHAClient()

    # Set up a passing 3-phase verification trace
    target_hash = 3439157731089703234
    ha.state_sequence["lawn_mower"] = ["mowing"]
    ha.state_sequence["activity_mode"] = ["MODE_WORKING"]
    ha.state_sequence["work_area"] = [f"area {target_hash}"]
    ha.blade_used_sequence = [166.0, 166.1]

    mow_module.register(server, ha_client=ha, safety=gate)
    # Defaults: mow_duration_sec=None, return_to_dock=True, verify=True
    result = await server.tools["mow_area"]("Area 6")

    # Result indicates autonomous completion path
    assert result["result"] == "mow_complete_autonomous"
    assert result["protocol_version"] == 3
    assert result["verification"]["verified"] is True
    assert "note" in result  # autonomous path carries the explanatory note

    # THE load-bearing assertion: lawn_mower.dock was NEVER called by the tool.
    services_called = [(d, s) for d, s, _ in ha.calls]
    assert ("lawn_mower", "dock") not in services_called, (
        f"v1.2 regression: lawn_mower.dock fired with mow_duration_sec=None. "
        f"Sequence: {services_called}"
    )

    # Only pre-mow steps fired (cancel + blades + start_mow). No post-dock cancel.
    assert services_called == [
        ("mammotion", "cancel_job"),
        ("mammotion", "start_stop_blades"),
        ("mammotion", "start_mow"),
    ]


@pytest.mark.asyncio
async def test_mow_area_with_duration_fires_dock(
    daytime_now, gate, mapping_env
) -> None:
    """v1.2: mow_duration_sec=N preserves the v1.1 explicit-recall behavior.

    When the user explicitly bounds the mow with a duration, the tool sleeps
    that duration AFTER verification passes, then fires lawn_mower.dock,
    polls charging, and fires post-dock cancel_job. Full 5-step sequence.
    """
    server = _FakeServer()
    ha = _VerifyMockHAClient()

    # Set up a passing 3-phase verification trace
    target_hash = 3439157731089703234
    ha.state_sequence["lawn_mower"] = ["mowing"]
    ha.state_sequence["activity_mode"] = ["MODE_WORKING"]
    ha.state_sequence["work_area"] = [f"area {target_hash}"]
    ha.blade_used_sequence = [166.0, 166.1]

    mow_module.register(server, ha_client=ha, safety=gate)
    result = await server.tools["mow_area"](
        "Area 6", mow_duration_sec=60, verify=True, return_to_dock=True
    )

    assert result["result"] == "mow_complete"
    assert result["protocol_version"] == 3
    assert result["verification"]["verified"] is True

    # Full 5-step canonical sequence including the explicit recall dock-fire.
    sequence = [(d, s) for d, s, _ in ha.calls]
    assert sequence == [
        ("mammotion", "cancel_job"),         # pre-mow
        ("mammotion", "start_stop_blades"),  # pre-mow
        ("mammotion", "start_mow"),          # pre-mow
        ("lawn_mower", "dock"),              # explicit recall after duration
        ("mammotion", "cancel_job"),         # post-dock cleanup
    ]


@pytest.mark.asyncio
async def test_mow_area_no_duration_return_to_dock_false(
    daytime_now, gate, mapping_env
) -> None:
    """v1.2: mow_duration_sec=None + return_to_dock=False returns autonomously
    after verification, with NO dock-fire (just like the default-return_to_dock
    path; return_to_dock has no effect when duration is None).
    """
    server = _FakeServer()
    ha = _VerifyMockHAClient()

    # Set up a passing 3-phase verification trace
    target_hash = 3439157731089703234
    ha.state_sequence["lawn_mower"] = ["mowing"]
    ha.state_sequence["activity_mode"] = ["MODE_WORKING"]
    ha.state_sequence["work_area"] = [f"area {target_hash}"]
    ha.blade_used_sequence = [166.0, 166.1]

    mow_module.register(server, ha_client=ha, safety=gate)
    result = await server.tools["mow_area"](
        "Area 6", verify=True, return_to_dock=False
    )

    # Same autonomous-completion result regardless of return_to_dock.
    assert result["result"] == "mow_complete_autonomous"
    assert result["protocol_version"] == 3
    assert result["verification"]["verified"] is True

    # No dock-fire — verified by absence in the service-call list.
    services_called = [(d, s) for d, s, _ in ha.calls]
    assert ("lawn_mower", "dock") not in services_called
    assert services_called == [
        ("mammotion", "cancel_job"),
        ("mammotion", "start_stop_blades"),
        ("mammotion", "start_mow"),
    ]
