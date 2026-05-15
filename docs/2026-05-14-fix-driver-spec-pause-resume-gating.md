# Fix-Driver Spec — pause_mow / resume_mow ungating

**Dispatched by:** mower-recovery-pm (Phase 2 follow-up)
**Navigator report:** `~/.claude_temp/reports/2026-05-14-mammotion-mcp-navigator-report.md` §7a
**Driver output:** `~/.claude_temp/reports/2026-05-14-mammotion-mcp-fix-driver-report.md`
**Branch:** continue on `v1-driver-build` (NOT merge; NOT new branch)
**Scope:** TIGHT — fix ONLY the one substantive concern named below.

## Bug

`mammotion_mcp/server.py` v0 scaffold puts both `mow` + `status` registration outside the diag-gate, but puts `motion` + `diag` registration INSIDE the diag-gate:

```python
# Current (wrong) code in mammotion_mcp/server.py:
mow.register(server, ha_client=ha_client, safety=safety)
status.register(server, ha_client=ha_client)

if os.environ.get("ENABLE_DIAGNOSTIC_TOOLS", "false").lower() == "true":
    from mammotion_mcp.tools import diag, motion
    motion.register(server, ha_client=ha_client, safety=safety)
    diag.register(server, ha_client=ha_client)
```

Result: `pause_mow` + `resume_mow` (which live in `motion.py` alongside `manual_drive`) get gated behind `ENABLE_DIAGNOSTIC_TOOLS=true`. These are Tier-3 safety primitives that MUST be available without the diag flag.

Navigator verified live by calling `build_server()` with diag-flag unset — got 6 tools registered, pause/resume missing.

## Fix — two-part

### Part 1: `mammotion_mcp/server.py`

Always import + register `motion`. Move the diag-gate to either (a) the motion.register call itself OR (b) just the `diag.register` call. Pick (a) — pass `enable_diagnostic_tools` flag into motion.register so motion can decide what to expose.

Replace the registration block to read:

```python
from mammotion_mcp.tools import mow, motion, status

mow.register(server, ha_client=ha_client, safety=safety)
status.register(server, ha_client=ha_client)

enable_diag = os.environ.get("ENABLE_DIAGNOSTIC_TOOLS", "false").lower() == "true"
motion.register(server, ha_client=ha_client, safety=safety, enable_diagnostic_tools=enable_diag)

if enable_diag:
    from mammotion_mcp.tools import diag
    diag.register(server, ha_client=ha_client)
```

### Part 2: `mammotion_mcp/tools/motion.py`

Update `motion.register()` to accept `enable_diagnostic_tools: bool = False`. Always register `pause_mow` + `resume_mow`. Conditionally register `manual_drive` only when the flag is True.

```python
def register(
    server: FastMCP,
    *,
    ha_client: HAClient,
    safety: SafetyGate,
    enable_diagnostic_tools: bool = False,
) -> None:
    """Register motion tools.

    Tier-3 (pause_mow, resume_mow) always registered.
    Tier-4 (manual_drive) only registered when enable_diagnostic_tools=True.
    """

    @server.tool()
    async def pause_mow() -> dict[str, Any]:
        # ... (existing body — unchanged)
        ...

    @server.tool()
    async def resume_mow() -> dict[str, Any]:
        # ... (existing body — unchanged)
        ...

    if enable_diagnostic_tools:
        @server.tool()
        async def manual_drive(
            direction: Literal["forward", "backward", "left", "right"],
            duration_sec: float = 1.0,
            speed: float = 0.4,
        ) -> dict[str, Any]:
            # ... (existing body — unchanged)
            ...
```

## Verification

1. `python -c "import os; os.environ['ENABLE_DIAGNOSTIC_TOOLS']='false'; from mammotion_mcp.server import build_server; s = build_server(); import asyncio; tools = asyncio.run(s.list_tools()); print(sorted(t.name for t in tools))"`
   - Expected: includes `pause_mow`, `resume_mow`; does NOT include `manual_drive`
2. Same with `ENABLE_DIAGNOSTIC_TOOLS=true`:
   - Expected: includes `pause_mow`, `resume_mow`, AND `manual_drive`
3. `python -m pytest tests/` — all existing 41 tests still pass; ADD one test:
   ```python
   def test_motion_register_with_diag_off_includes_pause_resume(...):
       """pause_mow + resume_mow MUST register without diag flag."""
       ...
   def test_motion_register_with_diag_on_includes_manual_drive(...):
       """manual_drive MUST register only with diag flag on."""
       ...
   ```
   (Use mocked FastMCP or inspect registered tools)
4. `docker compose build` — still exits 0

## Discipline

- Scope = JUST the gating fix. Do NOT touch other files (mow.py, status.py, diag.py, ha_client.py, safety.py). If you see other Navigator-flagged minor concerns, document but DO NOT fix in this Driver — that's scope-creep risking the small-fix's clarity.
- Same branch `v1-driver-build`. Commit with descriptive message. NO merge to main.

## Output

`~/.claude_temp/reports/2026-05-14-mammotion-mcp-fix-driver-report.md`:

```markdown
# Fix-Driver Report — pause/resume ungating
**Branch:** v1-driver-build (continued)
**Commit:** <new sha>
**Parent commit:** 504cc99

## Diff
[2 files: server.py + motion.py. Expected ~30 lines net change.]

## Tests
[41 + N new tests; all pass]

## docker build
[exits 0]

## Verification command output
[the two python -c invocations from §Verification]

## Ready for re-verification Navigator
[yes/no]
```

## Allowed actions

- Edits to `mammotion_mcp/server.py` + `mammotion_mcp/tools/motion.py` + `tests/test_*.py` ONLY
- `git add`, `git commit`, `git push origin v1-driver-build`
- `python -m pytest`, `docker compose build`, `python -c "..."` for verification

## Forbidden actions

- NO edits to other files
- NO new modules
- NO merge to main
- NO mutating HA REST calls
- NO mower commands

## Stop conditions

Stop when:
1. Both Part 1 + Part 2 applied, tests pass, docker builds, verification commands output expected tool lists, commit pushed; OR
2. Unresolvable blocker — write report explaining

Begin work. No mid-stream chatter.
