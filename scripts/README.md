# scripts/

The `justfile` at the repository root wraps everything here: run `just` for the list.
Development tooling for this repository. Nothing here ships in the package: `src/blender_mcp/`
is the only packaging root, and `tests/test_blender_rig.py` fails if a `scripts/__init__.py`
appears, because that would make the rig importable as part of the installed package.

Two kinds of thing live here. The first kind the test suite depends on, so it has to keep
working. The second kind records what a real Blender does; you re-run those by hand when
Blender changes, and read the output.

## Depended on by the test suite

| Script | What it does |
|---|---|
| `blender_rig.py` | Drives a live GUI Blender over the add-on's own socket. The add-on will not start under `blender --background`, where timers never fire, so anything that needs a real event loop goes through here. macOS only; on Linux use `docker/blender/docker-compose.yml`. Covered by `tests/test_blender_rig.py`. |
| `rig_scenarios/` | The scenarios the rig runs. See below. |
| `measure_catalog.py` | Measures the advertised `tools/list` payload for a `BLENDER_MCP_TOOLSETS` value. The byte ceilings in `tests/server/test_bundles.py` come from it. Covered by `tests/test_measure_catalog.py`. |
| `quiet_box.py` | Stamps the machine's load into an artifact, so a contended run says so itself. The threading tests carry wall-clock bounds, and this is how a failure caused by a busy machine is told apart from a real one. Covered by `tests/test_quiet_box.py`. |
| `revert_matrix.py` | Reverts the behaviour each test names and checks the test then fails. A test that still passes with its fix reverted proves nothing. `--only <prefix>` selects a group (`linking`, `file paths`, `barrier`, `session`, …), `--list` prints them all. A full run takes several minutes on a quiet machine. |
| `revert_rows/` | The matrix's rows, one module per stretch of the table, named for the area most of its rows guard; each module's docstring lists the label prefixes it holds. `common.py` holds the `Revert` type and the files and test nodes rows cite. A new row goes in the module for its prefix. |
| `check_revert_anchors.py` | Reports revert-matrix rows that no longer apply to their target file, and exits 1 on a broken or unparseable one; `just check` runs it. The matrix quotes source text, so editing a quoted line silently breaks a row. **Run this after editing any file the matrix reverts.** Covered by `tests/test_check_revert_anchors.py`. |
| `lint_changed.py` | The enforced lint gate (`just lint`). Runs ruff over the files a branch touches and reports only the findings that land on lines the branch introduced, so the inherited upstream backlog stays out of the way without letting new work add to it. Function-size metrics are ratcheted per function against the base, so editing inside a legacy oversized function is not a finding but raising its measured value is. `just lint-all` is the whole backlog. Covered by `tests/test_lint_changed.py`. |

`tests/blender_*_smoke.py` are the other half of this: scripts that drive the
add-on's handlers against a real headless Blender. pytest never collects them
(they are scripts, not `test_*.py`) and CI has no Blender, so `just smoke` is the
only thing that runs them, well under a minute for the whole set. Each loads the
add-on through `tests/smoke_addon.py`.

### rig_scenarios/

A scenario defines `run(rig)` and drives the socket through `rig.send()`. It fails by raising;
a clean return is a pass. Work that must happen inside Blender, rather than arrive as a queued
socket command, goes in a `--blender-script`.

| File | What it does |
|---|---|
| `scenario_phase2_gate.py` | The full gate: file lifecycle, linking and the swap barrier against a live Blender. `tests/test_phase2_gate.py` shells out to it. |
| `scenario_file_lifecycle.py` | Drives the real `open_shot`, `save_shot` and `reset_session` over the socket. |
| `scenario_file_swap_barrier.py` | Checks the file-swap barrier and the session epoch. |
| `scenario_timer_survives_file_swap.py` | Checks that the add-on keeps serving across a live `wm.open_mainfile`. |
| `in_blender_open_mainfile.py` | `--blender-script`: which timers survive a main-thread `wm.open_mainfile`. |
| `in_blender_open_shot_spike.py` | `--blender-script`: grafts an `open_shot`-shaped command onto the running add-on. |
| `make_fixture.py` | Builds the swap fixture: one object named `RigFixtureCube`. |
| `make_phase2_gate_fixtures.py` | Builds the two `.blend` fixtures the gate scenario needs. |

## Blender API probes

`blender_probes/` holds the probes. Every Blender API fact asserted in this codebase was
established by one of them, which is why they are committed rather than thrown away: when
Blender changes, re-running them is how you find out what changed. They take no arguments:

```
/opt/homebrew/bin/blender --background --factory-startup --python scripts/blender_probes/<name>.py
```

Most print an observation transcript for a person to read; only
`shot_directories_and_override_names.py` prints `[ok]`/`[FAIL]` lines and exits non-zero.

### Vetting a new Blender release

```
just probes          # run every probe against its recorded baseline: seconds, no GUI
just probes-record   # accept the current transcripts, once every diff is understood
```

`validate_blender_release.py` runs each probe, masks the volatile parts of its output
(the version banner, temp and repo paths, `$HOME`, session uids, addresses, log timestamps)
and diffs the rest against `blender_probes/baselines/<probe>.txt`. It exits non-zero if
anything differs or crashes. A difference is a fact to investigate, not a failure: decide
per diff whether Blender changed, an assumption in this repository was wrong, or the
baseline is simply stale, then re-record.

It picks the binary from its argument, else `$BLENDERMCP_BLENDER`, else
`/opt/homebrew/bin/blender`, and prints the version it used. It needs no `bpy` and no
network. The baselines hold no machine-specific paths, but they do encode macOS temp-path
spellings, so another platform needs its own recording.

The GUI rig is separate, because timers never fire under `--background`:

```
just gate                        # the phase-2 gate scenario against a live Blender
just rig <scenario> [work] ...    # one scenario; extra args reach blender_rig.py
```

| Probe | What it shows |
|---|---|
| `session_handlers.py` | `session.py`'s handler contract against real Blender rather than a test stub. |
| `file_lifecycle_handlers_real_blender.py` | The real `open_shot`, `save_shot` and `reset_session` handlers against real files. |
| `linking_handlers_real_blender.py` | The real linking handlers against real files. |
| `transaction_library_rollback.py` | The real `transaction.py` and `session.py` through a link, a reload and a load. |
| `shot_directories_and_override_names.py` | `save_shot(create_directories=…)` and `object_lookup.find_object`. |
| `library_replace_handlers.py` | Which file handlers fire, and which `session_uid`s change, per library operation. |
| `library_handlers_and_session_uids.py` | The library handler-firing table, `session_uid` churn, `users_id` and the override routes. |
| `linking_datablock_lifecycle.py` | What linking, unlinking and purging do to datablocks. |
| `linking_override_routes.py` | What each of the three library-override routes produces. |
| `linking_reload_ruling.py` | That the `Library` data API reloads and the operators are unsuitable. |
| `linking_gate_smoke.py` | Link, override, save and reopen, on two override routes. |
| `linking_scripts_auto_execute.py` | That linking, reloading, overriding or appending a hostile `.blend` never runs its Python. |
| `override_routes.py` | `override_create`'s return value and the three override routes. |
| `override_create_return_value.py` | Tells "failed" apart from "succeeded but returns None". |
| `file_lifecycle_operator_defaults.py` | Every RNA property and default of the file operators the handlers call or avoid. |
| `file_path_error_shapes.py` | Blender's real path-bearing error texts beside `sanitize_blender_error`'s output. |
| `error_shapes_and_magic_bytes.py` | Which failures raise rather than return `{'CANCELLED'}`, and `.blend` magic bytes. |
| `compression_default_and_dna_error.py` | The save compression default, and the truncated-file error shape. |
| `introspection_api_surface.py` | The introspection API surface the add-on reads. |
| `reset_operator_side_effects.py` | What each candidate `reset_session` operator does to preferences and to an enabled add-on. |
| `timers_in_background.py` | That `bpy.app.timers` never fire under `--background`, which is why the rig needs a GUI. |

## Needs neither Blender nor the network

| Script | What it does |
|---|---|
| `text_hygiene_enumeration.py` | Enumerates the characters that could slip a path separator or a disguise past the add-on, and reports what the current rules do with each. Run it with `.venv/bin/python` when changing `text_hygiene.py`. |
