# Driver Spec — area-mapping package-data fix

**Dispatched by:** mower-recovery-pm (Phase 2 — post-bounce gap surfaced 08:11 HST via Joshua relay)
**Driver output:** `~/.claude_temp/reports/2026-05-15-area-mapping-driver-report.md`
**Repo:** `communitywireless/mammotion-mcp` (public, main branch)
**Worktree:** `C:/Users/Joshua Montgomery/projects/mammotion-mcp/` is the working tree.
**Branch:** create `fix-area-mapping-package-data` off main; commit + push; do NOT merge to main inside this Driver (Navigator-then-merge follows).

## The bug

When `mammotion-mcp` is installed via `uvx --from git+https://github.com/communitywireless/mammotion-mcp` (the canonical install path sandbox-daemon uses), the `data/area-mapping.json` file is NOT bundled in the wheel because the file lives at the repo root, OUTSIDE the `mammotion_mcp/` package directory that `pyproject.toml`'s `[tool.hatch.build.targets.wheel] packages = ["mammotion_mcp"]` ships. Result: `mower__list_areas` and `mower__mow_area` both fail at FileNotFoundError when the default env `AREA_MAPPING_PATH=/app/data/area-mapping.json` doesn't resolve to an existing file.

The Docker container deployment worked because the Dockerfile's `COPY data ./data` line manually placed it at `/app/data/`. The uvx-install path has no equivalent.

## The fix

1. **Move the data into the package:** `data/area-mapping.json` → `mammotion_mcp/data/area-mapping.json`. The package directory then ships the data file as a normal package resource.
2. **Update area_resolver.py to use `importlib.resources`** when no override is passed. Keep the existing `mapping_path: str` parameter for env-override compatibility.
3. **Update server.py default** so when `AREA_MAPPING_PATH` env is unset, callers pass `None`, which triggers the package-resource path.
4. **Update tools/status.py `list_areas`** to follow the same None-means-package convention.
5. **Update Dockerfile** to remove the obsolete `COPY data ./data` (now redundant; data is inside the package and shipped via `pip install -e .`).
6. **Update tests** to exercise the package-resource path.

## Step-by-step

### Step 1: Move the file

```bash
cd "C:/Users/Joshua Montgomery/projects/mammotion-mcp"
mkdir -p mammotion_mcp/data
git mv data/area-mapping.json mammotion_mcp/data/area-mapping.json
rmdir data 2>/dev/null || true
```

### Step 2: Update `area_resolver.py`

Replace the existing `_load_mapping` function:

```python
@lru_cache(maxsize=4)
def _load_mapping(mapping_path: str | None) -> dict:
    """Load area-mapping.json. Cached by path.

    Args:
        mapping_path: Absolute path to the area-mapping.json file, or None
                      to load the package-bundled default (the canonical
                      mapping shipped inside mammotion_mcp/data/).

    Returns:
        Parsed JSON dict.

    Raises:
        FileNotFoundError: if the file is missing.
        json.JSONDecodeError: if the file is corrupt.
    """
    if mapping_path is None:
        # Load from package resources (works for uvx-installed wheels +
        # editable installs + Docker COPY-into-package layouts identically)
        from importlib.resources import files

        resource = files("mammotion_mcp").joinpath("data/area-mapping.json")
        return json.loads(resource.read_text())

    path = Path(mapping_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Area mapping not found at {mapping_path}. "
            f"Set AREA_MAPPING_PATH=<path> OR unset to use the package default."
        )
    with path.open() as f:
        return json.load(f)
```

Update `resolve()` and `list_areas()` signatures to accept `mapping_path: str | None`. The `None` value triggers the package-resource path.

### Step 3: Update `server.py`

Replace the `AREA_MAPPING_PATH` env reading at server boot. Current default in tools/mow.py + status.py is `/app/data/area-mapping.json`. Change to `None` (which triggers package-resource path):

```python
# In tools/mow.py register() and tools/status.py register():
mapping_path = os.environ.get("AREA_MAPPING_PATH")  # None if unset
# Pass mapping_path (None or a str) to area_resolver.resolve / list_areas
```

(Remove the `or "/app/data/area-mapping.json"` fallback.)

### Step 4: Update `tools/status.py` `list_areas`

The current implementation reads from the env path directly. Refactor it to delegate to `area_resolver.list_areas(mapping_path)` where `mapping_path` is either the env value or None.

### Step 5: Update `pyproject.toml`

Hatch should auto-include package-relative non-`.py` files, but be explicit to avoid drift:

```toml
[tool.hatch.build.targets.wheel]
packages = ["mammotion_mcp"]

[tool.hatch.build.targets.wheel.force-include]
"mammotion_mcp/data/area-mapping.json" = "mammotion_mcp/data/area-mapping.json"
```

(Verify after build that the wheel contains `mammotion_mcp/data/area-mapping.json`.)

### Step 6: Update `Dockerfile`

Remove the `COPY data ./data` line. Adjust the build context if needed so the package's `data/` ships via `pip install -e .`.

Before:
```dockerfile
COPY mammotion_mcp ./mammotion_mcp
COPY data ./data
```

After:
```dockerfile
COPY mammotion_mcp ./mammotion_mcp
```

### Step 7: Update `tests/test_area_resolver.py`

The test currently reads `data/area-mapping.json` from the repo root. Update to verify both:
- The new `mammotion_mcp/data/area-mapping.json` location
- The `None` parameter path via `importlib.resources`
- An explicit-path override path still works

### Step 8: Update `.env.example`

Remove the `AREA_MAPPING_PATH=/app/data/area-mapping.json` line OR change it to a comment explaining that omitting it loads the package default:

```ini
# Override the bundled area mapping with a custom path. Unset = package default.
# AREA_MAPPING_PATH=/path/to/custom-area-mapping.json
```

### Step 9: Verify

```bash
# Test resolves with no override
python -c "from mammotion_mcp import area_resolver; print(area_resolver.resolve('Area 6', None))"
# Expected: switch.luba2_awd_1_area_3439157731089703234

# Test list_areas with no override
python -c "from mammotion_mcp import area_resolver; areas = area_resolver.list_areas(None); print(len(areas), 'areas'); print(areas[0])"
# Expected: 8 areas + first one

# Existing tests pass
python -m pytest tests/ -v

# Build wheel + confirm data file is bundled
python -m pip wheel . -w /tmp/wheel-check
unzip -l /tmp/wheel-check/mammotion_mcp-*.whl | grep area-mapping
# Expected: mammotion_mcp/data/area-mapping.json listed
```

### Step 10: Commit + push

```bash
git checkout -b fix-area-mapping-package-data
git add .
git commit -m "$(cat <<'EOF'
fix(area-mapping): ship area-mapping.json as package data

Bug: uvx-install via `uvx --from git+...` produced wheels missing the
area-mapping.json file because data/ lived at repo root, outside the
mammotion_mcp/ package directory. sandbox-daemon hit FileNotFoundError
at first mower__list_areas call (Joshua relay 2026-05-15 08:11 HST).

Fix:
- Move data/area-mapping.json → mammotion_mcp/data/area-mapping.json
- area_resolver._load_mapping(None) now uses importlib.resources.files()
  to read the bundled JSON, works for uvx wheels + editable installs +
  Docker layouts uniformly
- server defaults AREA_MAPPING_PATH unset → None → package default
- Dockerfile drops the now-redundant COPY data ./data
- pyproject.toml force-includes the data file via hatch
- Tests cover the None (package) path + explicit-path override

Backward compat: setting AREA_MAPPING_PATH=<path> still overrides; the
no-env-set case now JUST WORKS.
EOF
)"
git push -u origin fix-area-mapping-package-data
```

## Verification gates (Driver must satisfy ALL before exiting)

1. `python -m py_compile mammotion_mcp/area_resolver.py mammotion_mcp/server.py mammotion_mcp/tools/mow.py mammotion_mcp/tools/status.py` → 0
2. `python -m pytest tests/ -v` → all green
3. `python -c "from mammotion_mcp import area_resolver; print(area_resolver.resolve('Area 6', None))"` → `switch.luba2_awd_1_area_3439157731089703234`
4. `python -m pip wheel . -w /tmp/wheel-check` then `unzip -l /tmp/wheel-check/mammotion_mcp-*.whl | grep area-mapping` → shows the file
5. `git diff main --stat` shows ONLY: rename + small edits in area_resolver.py, server.py, tools/mow.py, tools/status.py, Dockerfile, pyproject.toml, tests/test_area_resolver.py, .env.example. Nothing else.
6. Branch pushed; no merge to main.

## Allowed actions

- Edits in `C:/Users/Joshua Montgomery/projects/mammotion-mcp/`
- `git`, `python`, `pip` for verification
- WriteEdit/Bash tools

## Forbidden actions

- NO edits outside the repo
- NO merge to main from Driver (Navigator follows)
- NO mutating HA REST calls
- NO mower commands
- NO touching sandbox-daemon

## Stop conditions

- All verification gates pass + commit pushed; OR
- Unresolvable blocker → write report explaining what blocked

## Output

`~/.claude_temp/reports/2026-05-15-area-mapping-driver-report.md`:

```markdown
# area-mapping Package-Data Fix Driver Report
**Branch:** fix-area-mapping-package-data
**Commit:** <sha>

## Diff summary
[git diff --stat output]

## Verification gates
[step 9 results]

## Wheel contents check
[unzip -l output excerpt]

## Anything unexpected
[items worth knowing]

## Ready for Navigator
[yes/no]
```

Begin work. No mid-stream chatter.
