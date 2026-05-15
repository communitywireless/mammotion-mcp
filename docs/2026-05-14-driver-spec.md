# Driver Spec — mammotion-mcp v1.0

**Dispatched by:** mower-recovery-pm (Phase 2)
**Customer-proxy:** mycroft-desktop (Joshua AFK overnight)
**Investigator brief:** `~/.claude_temp/reports/2026-05-14-pymammotion-public-surface-investigator-report.md` — READ IN FULL
**Driver output:** `~/.claude_temp/reports/2026-05-14-mammotion-mcp-driver-report.md`
**Worktree:** `/c/Users/Joshua Montgomery/projects/mammotion-mcp/` is the working tree; create a branch `v1-driver-build` for your edits and push that branch, NOT main directly.

## Mission

Implement the tool bodies in `mammotion_mcp/` package per the v0 scaffold. v1.0 ships against HA REST only (single dependency, matches the verified canonical sequence pattern). Tier 1 + 2 + 3 always-on; Tier 4 gated behind `ENABLE_DIAGNOSTIC_TOOLS=true`. End-to-end testable from a real MCP client.

## Starting state

- Repo: `communitywireless/mammotion-mcp` @ `3698021` (main) — v0 scaffold pushed
- Local clone: `C:/Users/Joshua Montgomery/projects/mammotion-mcp/` (your working tree)
- Tool bodies are `NotImplementedError` placeholders — your job is to fill them
- HA endpoint: `http://192.168.1.201:8123` (Thor1 production HA, shared by sandbox + production tiers today)
- HA token: pulled from `/opt/infrastructure/services/ha-mcp/.env` on Thor1 (canonical transient-PM auth path) OR from `.env` if running locally for dev
- Mower entity: `lawn_mower.luba2_awd_1`
- Canonical area: `Area 6` → `switch.luba2_awd_1_area_3439157731089703234`

## Target state

`communitywireless/mammotion-mcp` branch `v1-driver-build` pushed with:

1. All `NotImplementedError` stubs replaced with working bodies per the surface below
2. Each tool wrapped with proper error handling (retry on transient HA errors; clean error responses to MCP caller)
3. Type hints + docstrings on every public method
4. Tests in `tests/` for: area resolution, safety gates, HA REST mock calls (the live mow test stays opt-in behind `MAMMOTION_MCP_LIVE_TEST=1` env var)
5. The `data/area-mapping.json` baked-in image works correctly
6. Container builds successfully: `docker compose build` returns 0
7. A README section "How to register in `.mcp.json`" with the exact stanza for `mycroft-sandbox`

## Locked defaults — DO NOT change without surfacing to mower-recovery-pm

| Setting | Value | Source |
|---|---|---|
| `DEFAULT_BLADE_HEIGHT_MM` | 55 | Joshua's operational value; HA schema default of 25 triggers Error 1202 |
| `QUIET_HOURS_START_HST` | 21 | env override allowed |
| `QUIET_HOURS_END_HST` | 8 | env override allowed |
| `MIN_BATTERY_PCT` | 30 | preflight gate |
| Concurrency | single-flight file lock | one `mow_area` at a time across all MCP clients |
| Transport | HA REST only | NO pymammotion-direct in v1.0 |

## Tool surface — implement these (Tier 1 + 2 + 3 always-on)

### Tier 1 — `mammotion_mcp/tools/mow.py`

#### `mow_area(area_name, blade_height_mm=55, mow_duration_sec=None, return_to_dock=True, override_quiet_hours=False) -> dict`

The 5-step canonical sequence per `~/projects/mower-recovery-pm/docs/2026-05-14-mower-usage-guide-for-agents.md`:

```python
async def mow_area(area_name, blade_height_mm=55, mow_duration_sec=None,
                   return_to_dock=True, override_quiet_hours=False) -> dict:
    # Safety gates
    safety.check_quiet_hours(override=override_quiet_hours)
    safety.check_blade_height(blade_height_mm)

    # Area resolution (raises ValueError if unknown name)
    switch_entity = area_resolver.resolve(area_name)

    async with safety:  # acquire single-flight lock
        # Preflight
        status = await ha_client.get_mower_status()
        safety.check_battery(status.battery_pct)

        # Step 1: cancel_job (clear stale breakpoint)
        await ha_client.call_service("mammotion", "cancel_job")
        await asyncio.sleep(10)  # let cancel settle

        # Step 2: start_stop_blades(true, blade_height) → fires DrvMowCtrlByHand
        await ha_client.call_service(
            "mammotion", "start_stop_blades",
            start_stop=True, blade_height=blade_height_mm
        )
        await asyncio.sleep(3)

        # Step 3: start_mow with explicit blade_height
        await ha_client.call_service(
            "mammotion", "start_mow",
            areas=[switch_entity], blade_height=blade_height_mm
        )

        # Poll for state=mowing (max 60s)
        mowing_reached = await _poll_state(ha_client, "mowing", timeout_s=60)
        if not mowing_reached:
            raise RuntimeError(f"Mower did not transition to mowing within 60s")

        # If mow_duration_sec specified, mow for that long
        if mow_duration_sec:
            await asyncio.sleep(mow_duration_sec)

        if not return_to_dock:
            return await ha_client.get_mower_status().to_dict()

        # Step 4: lawn_mower.dock
        await ha_client.call_service("lawn_mower", "dock")

        # Poll for charging=on (480s timeout)
        charging_reached = await _poll_charging(ha_client, timeout_s=480)
        if not charging_reached:
            raise RuntimeError("Mower did not reach charging=on within 480s")

        # Step 5: post-dock cancel_job (clean task state)
        await ha_client.call_service("mammotion", "cancel_job")
        await asyncio.sleep(5)

        return await ha_client.get_mower_status().to_dict()
```

#### `dock_and_clear() -> dict`

```python
async def dock_and_clear() -> dict:
    """lawn_mower.dock + poll charging=on + post-dock cancel_job."""
    async with safety:
        await ha_client.call_service("lawn_mower", "dock")
        await _poll_charging(ha_client, timeout_s=480)
        await ha_client.call_service("mammotion", "cancel_job")
        await asyncio.sleep(5)
        return await ha_client.get_mower_status().to_dict()
```

#### `cancel_job() -> dict`

```python
async def cancel_job() -> dict:
    """Standalone mammotion.cancel_job — clear task state without recall."""
    await ha_client.call_service("mammotion", "cancel_job")
    await asyncio.sleep(5)
    return await ha_client.get_mower_status().to_dict()
```

### Tier 2 — `mammotion_mcp/tools/status.py`

#### `get_mower_status() -> dict`

Compose snapshot from multiple HA entities:

```python
async def get_mower_status() -> dict:
    state = await ha_client.get_state("lawn_mower.luba2_awd_1")
    charging = await ha_client.get_state("binary_sensor.luba2_awd_1_charging")
    battery = await ha_client.get_state("sensor.luba2_awd_1_battery")
    last_error = await ha_client.get_state("sensor.luba2_awd_1_last_error_code")
    last_error_time = await ha_client.get_state("sensor.luba2_awd_1_last_error_time")
    blade_used = await ha_client.get_state("sensor.luba2_awd_1_blade_used_time")
    activity_mode = state["attributes"].get("activity_mode")

    return {
        "state": state.get("state"),
        "activity_mode": activity_mode,
        "charging": charging.get("state") == "on",
        "battery_pct": _safe_int(battery.get("state")),
        "last_error_code": _safe_int(last_error.get("state")),
        "last_error_time": last_error_time.get("state"),
        "blade_used_time_hr": _safe_float(blade_used.get("state")),
        "last_changed": state.get("last_changed"),
    }
```

Driver: validate the entity IDs against `lawn_mower.luba2_awd_1`'s attributes + sibling sensors on the live Thor1 HA. If an entity ID is wrong, use the actual one HA exposes — discover via `GET /api/states` filtered to `luba2_awd_1`.

#### `list_areas() -> list[dict]`

Already partially scaffolded — verify it reads `data/area-mapping.json` correctly + returns `app_name`, `hash`, `ha_switch_entity` for each entry.

#### `get_position() -> dict`

Read mower position attributes from HA state. The pymammotion Investigator confirmed lat/lon are in `location.device.latitude/longitude` in **radians** — HA may already convert these to degrees in state attributes, OR may pass them through. Verify against live HA state and convert via `math.degrees()` if HA passes raw radians.

```python
async def get_position() -> dict:
    state = await ha_client.get_state("lawn_mower.luba2_awd_1")
    attrs = state.get("attributes", {})
    # Investigator note: HA may convert; verify against live state
    lat = attrs.get("latitude")  # or attrs.get("lat_deg") or wherever HA puts it
    lon = attrs.get("longitude")
    return {
        "latitude": lat,
        "longitude": lon,
        "heading_deg": attrs.get("orientation") or attrs.get("heading"),
        "position_type": attrs.get("position_type"),
        "pos_x_m": attrs.get("pos_x"),
        "pos_y_m": attrs.get("pos_y"),
        "activity_mode": attrs.get("activity_mode"),
    }
```

If HA doesn't expose any of these in lawn_mower attributes, also check `sensor.luba2_awd_1_position` or sibling entities. The Investigator report Section 3.1 lists all telemetry field paths — find them in HA's state surface.

### Tier 3 — `mammotion_mcp/tools/motion.py` (pause/resume only)

#### `pause_mow() -> dict`

```python
async def pause_mow() -> dict:
    await ha_client.call_service("lawn_mower", "pause")
    return await ha_client.get_mower_status().to_dict()
```

#### `resume_mow() -> dict`

```python
async def resume_mow() -> dict:
    # When paused, lawn_mower.start_mowing resumes from breakpoint
    await ha_client.call_service("lawn_mower", "start_mowing")
    return await ha_client.get_mower_status().to_dict()
```

### Tier 4 — `mammotion_mcp/tools/diag.py` (gated by `ENABLE_DIAGNOSTIC_TOOLS=true`)

Implement these only when the gate is on. Each maps to an HA service or REST endpoint:

| MCP tool | HA call | Notes |
|---|---|---|
| `set_blade_height(height_mm)` | `mammotion.set_blade_height(height=...)` | validate 15-100 |
| `set_cutter_mode(mode)` | `mammotion.set_cutter_mode` (verify name) | 0/1/2 = normal/slow/fast |
| `set_speed(speed_mps)` | `mammotion.set_speed` (verify name) | 0.2-1.2 m/s |
| `set_headlight(on)` | `light.luba2_awd_1_headlight` turn_on/off | OR `mammotion.set_car_manual_light` if HA exposes it |
| `set_sidelight(on)` | `light.luba2_awd_1_sidelight` turn_on/off | OR `mammotion.read_and_set_sidelight` |
| `remote_restart()` | `mammotion.restart_mower` (verify) | soft reboot |
| `reload_integration()` | `POST /api/config/config_entries/entry/<id>/reload` | discover entry_id at startup |
| `return_to_dock()` | `lawn_mower.dock` | same as dock_and_clear without post-cancel |
| `leave_dock()` | `mammotion.leave_dock` (verify) | back out of charger |
| `get_error_code()` | read `sensor.luba2_awd_1_last_error_code` | no service call, just a read |
| `start_stop_blades(start_stop, blade_height)` | `mammotion.start_stop_blades` | already used in mow_area |

For each Tier 4 tool that maps to a `mammotion.*` HA service, **verify the service name exists** in HA by querying `GET /api/services` filtered to `mammotion` domain. If the service doesn't exist in HA, exclude that tool from v1.0 and document in the Driver report which ones were skipped + why.

### Safety gates — `mammotion_mcp/safety.py`

Implement the body of `SafetyGate.__aenter__` / `__aexit__` with `filelock`:

```python
from filelock import FileLock, Timeout

class SafetyGate:
    def __init__(self, ...):
        ...
        self._lock = FileLock(self.lock_file_path)

    async def __aenter__(self) -> "SafetyGate":
        try:
            self._lock.acquire(timeout=5)
        except Timeout:
            raise SafetyViolation(
                "Another mow_area call is in flight (lock held). "
                "Wait or call cancel_job to interrupt."
            )
        return self

    async def __aexit__(self, *exc_info) -> None:
        self._lock.release()
```

Add `filelock>=3.13` to pyproject.toml dependencies.

### HA REST client — `mammotion_mcp/ha_client.py`

Implement `get_mower_status` body (use it inside tools). Add retry for transient errors (httpx.NetworkError, 502/503/504 from HA) with backoff: 3 retries, 1s/2s/4s. Do NOT retry on 4xx (4xx is the caller's bug; surface it cleanly).

```python
from httpx import HTTPStatusError, NetworkError

async def _request_with_retry(self, method, url, **kwargs):
    for attempt in range(3):
        try:
            resp = await self._client.request(method, url, **kwargs)
            if resp.status_code in (502, 503, 504):
                raise NetworkError(f"transient {resp.status_code}")
            resp.raise_for_status()
            return resp
        except (NetworkError,) as exc:
            if attempt == 2:
                raise
            await asyncio.sleep(2 ** attempt)
```

### Area resolver — new file `mammotion_mcp/area_resolver.py`

```python
import json
from functools import lru_cache
from pathlib import Path

@lru_cache(maxsize=1)
def _load(mapping_path: str) -> dict:
    with open(mapping_path) as f:
        return json.load(f)

def resolve(area_name: str, mapping_path: str) -> str:
    """Return the HA switch entity for the named area.

    Raises ValueError if name not found.
    """
    data = _load(mapping_path)
    by_app = data.get("by_app_name", {})
    if area_name not in by_app:
        valid = ", ".join(by_app.keys())
        raise ValueError(f"Unknown area: {area_name!r}. Valid: {valid}")
    return by_app[area_name]["ha_switch_entity"]
```

## Tests — `tests/`

### `test_area_resolver.py` (already exists; extend if needed)

### `test_safety.py` (new)

```python
def test_quiet_hours_block_at_22_hst():
    gate = SafetyGate(quiet_hours_start_hst=21, quiet_hours_end_hst=8, ...)
    with mock.patch("mammotion_mcp.safety.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 14, 22, 0, tzinfo=HST)
        with pytest.raises(SafetyViolation, match="Quiet hours"):
            gate.check_quiet_hours()

def test_quiet_hours_pass_at_10_hst():
    ...

def test_blade_height_out_of_bounds():
    gate = SafetyGate(...)
    with pytest.raises(SafetyViolation):
        gate.check_blade_height(10)
    with pytest.raises(SafetyViolation):
        gate.check_blade_height(150)

def test_battery_below_min():
    gate = SafetyGate(min_battery_pct=30, ...)
    with pytest.raises(SafetyViolation, match="battery_pct=20"):
        gate.check_battery(20)
```

### `test_ha_client.py` (new, uses `pytest-httpx`)

Mock HA REST endpoints. Verify:
- `get_mower_status()` composes correctly from multiple state reads
- `call_service()` posts the right body
- Retry fires on 502/503/504 but not on 400/401/404

### `test_mow_tool.py` (new, uses `pytest-httpx`)

Mock the full canonical sequence call chain. Verify:
- 5-step order: cancel_job → start_stop_blades → start_mow → dock → cancel_job
- blade_height=55 default
- mow_duration_sec=None skips the wait+dock
- override_quiet_hours=False raises during quiet hours
- Battery preflight blocks if <30%

### `test_live_mow.py` (new, opt-in via `MAMMOTION_MCP_LIVE_TEST=1`)

Skip by default. When env var set, run the real `mow_area("Area 6")` call against live HA. Requires Joshua eyes-on. Document this clearly.

## Verification before Driver finishes

1. `python -m pytest tests/` — all non-live tests pass
2. `docker compose build` — exits 0
3. `python -c "from mammotion_mcp.server import build_server; s = build_server()"` — no exceptions (will fail without HA_TOKEN; mock or skip in CI)
4. Read README "How to register" section — make sure the `.mcp.json` stanza is concrete + copy-pasteable
5. `git diff main` — review your own diff for: no NotImplementedError remaining in registered tools; no leftover TODO/FIXME

## Allowed actions

- SSH to thor1.guardwellfarm.com (BatchMode=yes, key auth) for HA introspection only
- HA REST READ-ONLY calls for discovery (`GET /api/states`, `GET /api/services`) — DO NOT make mutating calls (`POST /api/services/mammotion/start_mow` etc.) during the build
- File edits in `C:/Users/Joshua Montgomery/projects/mammotion-mcp/` only
- `git checkout -b v1-driver-build`, `git add`, `git commit`, `git push origin v1-driver-build`
- `pytest`, `docker build`, `python -c` for verification
- Read pymammotion source inside HA container for surface verification

## Forbidden actions

- NO mutating HA REST calls during the build (cancel_job, start_mow, dock, etc. — these only fire in pytest-httpx mocks OR in the live opt-in test which is NOT part of the Driver's build)
- NO HA container restart
- NO mower physical commands
- NO merge to `main` (Driver only pushes the feature branch)
- NO edits outside `~/projects/mammotion-mcp/`
- NO Aliyun MQTT direct session (v1.0 is HA REST only)

## Stop conditions

Stop when:
1. All Tier 1+2+3 tools implemented + Tier 4 implemented behind the gate + all tests pass + container builds + spec verification checklist passes; OR
2. You hit an unresolvable blocker (HA service name not what we expected, entity ID different, schema mismatch) — write the Driver report explaining what blocked + what you tried + ping mower-recovery-pm via the report.

## Output — Driver report

`~/.claude_temp/reports/2026-05-14-mammotion-mcp-driver-report.md`:

```markdown
# mammotion-mcp v1.0 Driver Report
**Branch:** v1-driver-build
**Commit:** <sha>
**Files changed:** N files, +X / -Y lines

## What was implemented
[tier-by-tier breakdown]

## HA surface verification
[which mammotion.* services exist + their signatures from GET /api/services]

## Tools excluded from v1.0
[any Tier 4 tools whose HA service didn't exist; explain]

## Tests
[pytest summary: passed/failed/skipped]

## docker compose build result
[stdout summary]

## Diff summary
[scope of changes]

## Open issues / questions for Navigator
[anything ambiguous; specific concerns to verify]

## Ready for Navigator
[yes/no + what to focus on]
```

## Discipline

- `~/.claude/rules/branch-discipline-deploy-trees.md` — feature branch only, no merge
- `~/.claude/rules/git-deployment-doctrine.md` — every commit pushed
- `~/.claude/rules/config-over-env.md` — env vars for config, not hard-coded
- `~/.claude/rules/no-forward-todos.md` — fix in scope or surface to mower-recovery-pm
- `~/.claude/rules/hyperscale-ready-disciplines.md` — note Discipline #3 (versioned wire protocols): include `"protocol_version": 1` field in tool responses for future-proofing
