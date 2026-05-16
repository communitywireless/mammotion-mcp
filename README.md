# mammotion-mcp

MCP server exposing canonical Mammotion Luba2-AWD mower control to Guard Well Farm agents.

**Status:** v1.2 (2026-05-15) — dock-semantic fix (W-003 root cause).
**Deployment tier (initial):** mycroft-sandbox on Thor2 (per `rules/sandbox-first.md`).
**Owner:** mower-recovery-pm (transient) → folds into mycroft-desktop on close.

## Dock semantics (v1.2 W-003 root-cause fix)

`mow_area` treats `mow_duration_sec` ASYMMETRICALLY with respect to the
explicit `lawn_mower.dock` recall:

| `mow_duration_sec` | `return_to_dock` | Behavior |
|---|---|---|
| `None` (default) | `True` (default) | Tool does NOT fire `lawn_mower.dock`. Mower auto-completes the area + auto-docks itself. Returns `mow_complete_autonomous` after Phase 3 verification confirms blades engaged. |
| `None` | `False` | Same — autonomous completion. `return_to_dock` has NO effect when `mow_duration_sec` is None (the mower's firmware handles the dock-return itself). |
| `N` (set) | `True` | Tool sleeps N seconds AFTER verification passes, then fires `lawn_mower.dock` (explicit recall). Returns `mow_complete`. |
| `N` (set) | `False` | Tool sleeps N seconds AFTER verification passes, then returns WITHOUT firing dock. Returns `mow_complete`. |

### Why asymmetric (the 2026-05-15 09:25 HST root cause)

v1.1 fired `lawn_mower.dock` unconditionally after Phase 3 verification
passed, regardless of whether the caller bounded the mow with
`mow_duration_sec`. On 2026-05-15 09:25 HST a `mow_area("Area 1")`
dispatch with the default `mow_duration_sec=None` reached Phase 3 PASS,
then fired `lawn_mower.dock` while the mower was still in `MODE_WORKING`.
HA's mammotion integration translates `lawn_mower.dock` during
`MODE_WORKING` into `pause_execute_task` + `return_to_dock`, which killed
the mow. The mower had driven 1.3 meters of a ~120-meter transit to Area
1 and never blade-engaged in the target area.

v1.1's three-phase verification HONESTLY REPORTED what happened — but
didn't PREVENT it. The bug was that the tool fired the killing dock
itself.

v1.2's fix: `mow_duration_sec=None` means "let the mower mow to natural
completion." The mower's firmware auto-docks when the area is finished.
The tool does NOT issue an explicit dock — it returns success after Phase
3 verification confirms blades engaged and leaves the mower to complete
the area autonomously. `mow_duration_sec=N` is the explicit-recall path
for callers who want to bound the mow regardless of area completion.

### Doctrine: prefer autonomous completion

Default callers should leave `mow_duration_sec=None` and let the mower
auto-complete + auto-dock. Use `mow_duration_sec` only when you
specifically need to bound the mow with a wall-clock timer (e.g., quiet
hours approaching, battery preservation budget, demo scenario).

## What `success` means (v1.1 W-003 fix)

`mow_area` semantics changed in v1.1 to fix a W-003 ("test the faucet,
not the plumbing") violation that bit us 2026-05-15 09:25 HST.

### v1.0 behavior (the bug)

`mow_area` returned `result="mow_complete"` after the canonical 5-step
HA-service-call sequence (cancel + start_stop_blades + start_mow + dock +
post-dock cancel) completed 200 OK and the mower's `state` briefly
transitioned to `mowing` within 60s.

What actually happened on 2026-05-15 09:25 HST: state held `mowing` for
exactly **24 seconds** then went to `paused`. `work_area` never reached
the target area's hash. `blade_used_time` was unchanged (166.49 hr at
T+0 and T+8min). **The mower never physically mowed.** But the tool
returned success — plumbing ACK without faucet proof.

### v1.1 behavior (the fix)

`mow_area` now defaults to `verify=True`, which runs a 3-phase
post-dispatch verification (per the Investigator surface report
2026-05-15 §5):

| Phase | Window | Success criterion | What it proves |
|---|---|---|---|
| 1 | 0-90s | `state=mowing` + `activity=MODE_WORKING` SUSTAINED ≥30s | Mower undocked + entered work mode (not just a flash) |
| 2 | 90-600s | `sensor.work_area` contains `"area <target_hash>"` | Mower physically arrived at the target area (RTK-GPS zone match) |
| 3 | 600-1800s | `sensor.blade_used_time` delta ≥ 0.001 hr (~3.6s blade time) | Blades **physically spun** — the irreversible faucet proof |

Phase 3 is THE proof. `blade_used_time` is the only HA signal that
conclusively proves blades physically engaged. There is **no**
real-time blade-RPM sensor in the HA surface (Investigator §1). The
counter is async (SysReport-driven, 5-17 min latency observed), so the
20-minute Phase 3 window is conservative.

### Trade-off: long-running

`verify=True` is now **long-running**: up to ~30 minutes worst case (on
a failed Phase 3 timeout). Callers MUST set their MCP timeout
accordingly — `≥ 1800s` is the recommendation.

### Result values (v1.2)

| `result` | Meaning |
|---|---|
| `mow_complete_autonomous` | v1.2 default success — verification passed, `mow_duration_sec=None`, tool did NOT fire explicit dock. Mower will continue mowing and auto-dock when the area is complete. |
| `mow_complete` | Explicit-recall success — verification passed AND `mow_duration_sec` was set, tool slept the duration and fired `lawn_mower.dock`. |
| `mow_failed_verification` | Verification failed; dock recovery attempted (when `return_to_dock=True`). The `verification` field has the phase that failed + detail. |
| `mow_dispatched_unverified` | `verify=False` opt-out path — v1.0 HA-ACK semantics. Caller verifies themselves. |
| `mowing_started` | `verify=False` AND `return_to_dock=False` — v1.0 fast-path snapshot return. |

### Opt-out (`verify=False`)

Callers who plan to handle verification themselves can pass
`verify=False`. This restores v1.0 fast-path semantics. The
`protocol_version` in the response is now `3` (v1.2 bump from 2 — the
dock-semantic change is a wire-format-compatible behavior change but
gets a version bump so callers can detect v1.2 explicitly).

### Edge case: stale-SysReport rollback

`blade_used_time` is async — on MQTT reconnect, a stale cached value
can briefly arrive (observed: -5.7 min delta, recovered within ~3 min).
`_verify_mowing` handles this: when `current_value < baseline`, the
verifier does NOT update its baseline; it keeps polling and lets the
later (correct) value re-establish the positive delta.

## Purpose

Other agents (CM, sandbox, mycroft-desktop, future Violet) need a **forcing function** for the canonical mower control sequence. Today they reach for `ha-mcp.call_service` and compose `mammotion.start_mow` themselves — which is exactly the wrong path. They hit Error 1202, dock dirty, leave the mower in "task paused, not ready" state.

This MCP server is the structural fix: one tool, `mow_area(name)`, internally fires the verified 5-step canonical sequence (cancel → start_stop_blades → start_mow → poll → dock → post-dock cancel). Agents cannot compose it wrong; they don't have the primitives.

## v1 tool surface (planned — locked after Investigator returns)

### Tier 1 — Core mow (load-bearing)
- `mow_area(area_name, blade_height_mm=55, mow_duration_sec=None, return_to_dock=True)` — full canonical sequence
- `dock_and_clear()` — recall + post-dock cancel
- `cancel_job()` — standalone task-state clear

### Tier 2 — Status reads (cheap, high-value)
- `get_mower_status()` — state + charging + battery + activity_mode + last_error
- `list_areas()` — area_name → switch_entity → hash mapping
- `get_position()` — current lat/lon/bearing/speed (telemetry capture lever)

### Tier 3 — Pause / resume
- `pause_mow()` — leaves breakpoint
- `resume_mow()` — resumes from breakpoint

### Tier 4 (gated behind `ENABLE_DIAGNOSTIC_TOOLS=true`)
- `start_stop_blades(start_stop, blade_height_mm=55)` — low-level blade motor
- `reload_integration()` — polling-stall workaround
- `manual_drive(direction, duration_sec, speed)` — nudge primitives (if pymammotion exposes)
- `goto_coord(lat, lon)` — point-to-point (if pymammotion exposes)

### Built-in safety gates
- Quiet-hours: refuse mow between 21:00–08:00 HST unless `override_quiet_hours=True`
- Blade-height bounds: 15 ≤ blade_height_mm ≤ 100
- Pre-flight battery check: require `battery_pct ≥ 30` before start
- Concurrent-call file lock: prevent overlapping `mow_area` calls

## Architecture

```
agent (CM / sandbox / mycroft-desktop)
  │
  │ MCP stdio
  ▼
mammotion-mcp (Python, this repo)
  │
  │ HTTP REST
  ▼
Home Assistant @ thor1:8123 (mammotion.* + lawn_mower.* services)
  │
  │ pymammotion → MQTT
  ▼
Aliyun cloud
  │
  ▼
Luba2-AWD physical device
```

For tools that need behaviors NOT exposed by HA (manual nudge, goto_coord), this server may need to bypass HA and talk to pymammotion directly. That call gets made after the Investigator returns the actual pymammotion surface.

## Config (env vars per `rules/config-over-env.md`)

| Var | Default | Notes |
|---|---|---|
| `HA_URL` | `http://192.168.1.201:8123` | Thor1 prod HA |
| `HA_TOKEN` | (required) | from `gwf-creds` at boot OR `.env` |
| `MOWER_ENTITY_ID` | `lawn_mower.luba2_awd_1` | |
| `AREA_MAPPING_PATH` | `/app/data/area-mapping.json` | baked in image |
| `ENABLE_DIAGNOSTIC_TOOLS` | `false` | gate Tier 4 |
| `QUIET_HOURS_START_HST` | `21` | int 0-23 |
| `QUIET_HOURS_END_HST` | `8` | int 0-23 |
| `MIN_BATTERY_PCT` | `30` | int 0-100 |
| `LOG_LEVEL` | `INFO` | |

## Deployment (sandbox-first per `rules/sandbox-first.md`)

1. Build: `docker compose build` (Dockerfile in this repo)
2. Deploy to Thor2: `docker compose up -d` from `/opt/mammotion-mcp/`
3. Register in mycroft-sandbox `.mcp.json` (see stanza below)
4. Smoke-test read-side tools (`get_mower_status`, `list_areas`, `get_position`) via mycroft-sandbox agent
5. Joshua eyes-on Tier-1 live `mow_area` test
6. After 24h clean: promote to mycroft-desktop `.mcp.json`, then CM/Mycroft-container

## How to register in `.mcp.json`

For mycroft-sandbox (Thor2) — the agent owns the MCP server process via `docker exec`:

```json
{
  "mcpServers": {
    "mammotion-mcp": {
      "command": "docker",
      "args": [
        "exec",
        "-i",
        "mammotion-mcp",
        "python",
        "-m",
        "mammotion_mcp.server"
      ],
      "env": {}
    }
  }
}
```

Pre-requisites on the host:
- `docker compose up -d` has been run from `/opt/mammotion-mcp/` so the
  `mammotion-mcp` container is up.
- `.env` file at `/opt/mammotion-mcp/.env` has at minimum:
  - `HA_TOKEN=<thor1 ha long-lived token>`
  - All other vars use the defaults baked into `.env.example`.

For local dev (no container) — point MCP directly at the Python module:

```json
{
  "mcpServers": {
    "mammotion-mcp-dev": {
      "command": "python",
      "args": ["-m", "mammotion_mcp.server"],
      "env": {
        "HA_TOKEN": "<your long-lived token>",
        "HA_URL": "http://192.168.1.201:8123",
        "AREA_MAPPING_PATH": "/c/Users/Joshua Montgomery/projects/mammotion-mcp/data/area-mapping.json",
        "LOCK_FILE_PATH": "/c/temp/mammotion-mcp.lock"
      }
    }
  }
}
```

To enable Tier-4 diagnostic tools (manual_drive, reload_integration,
start_stop_blades, etc.), set `ENABLE_DIAGNOSTIC_TOOLS=true` in `.env`
or in the `env` block above. Default is `false` — production registers
the safe subset only.

## Doctrine compliance

- `rules/sandbox-first.md` — install Thor2 first, observation window before promotion
- `rules/git-deployment-doctrine.md` — repo public-private at GitHub from inception; running production reproducible by `git clone + configure + run`
- `rules/branch-discipline-deploy-trees.md` — feature branches on worktrees, deploy tree on `main`
- `rules/config-over-env.md` — config via env vars in `.env`, not hard-coded
- `rules/hyperscale-ready-disciplines.md` — single-tenant v1, but `companion_id` parameter on every mutation in the API surface (defaults to `joshua-mont` v1)
- `rules/agent-portability.md` — service lives WITH the consumer agent (sandbox → desktop → CM); per-instance config

## Repository layout (v0 scaffold)

```
mammotion-mcp/
  ├── README.md           (this file)
  ├── pyproject.toml      (Python package metadata)
  ├── Dockerfile          (container build)
  ├── compose.yml         (deployment compose)
  ├── .env.example        (config template)
  ├── .gitignore
  ├── mammotion_mcp/
  │   ├── __init__.py
  │   ├── server.py       (MCP server entry point)
  │   ├── tools/
  │   │   ├── mow.py      (Tier 1 mow tools)
  │   │   ├── status.py   (Tier 2 status tools)
  │   │   ├── motion.py   (Tier 3 pause/resume + Tier 4 manual)
  │   │   └── diag.py     (Tier 4 diagnostics)
  │   ├── ha_client.py    (HA REST wrapper)
  │   ├── safety.py       (quiet-hours, bounds, battery, lock)
  │   └── area_resolver.py (area_name → switch_entity)
  ├── data/
  │   └── area-mapping.json (baked-in area mapping)
  └── tests/
      └── (tests TBD)
```
