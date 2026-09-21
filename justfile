# Machine-local recipes (not committed). Absent in a fresh clone; `import?`
# skips it silently rather than erroring.
import? 'private.just'

# Interpreter for everything below. CLAUDE.md fixes the environment at `.venv/`,
# and ruff and basedpyright are run as its modules so a gate cannot silently
# fall through to a different install of them on PATH.
PYTHON := ".venv/bin/python"

# Recipe arguments reach the shell as $1.., never interpolated into the recipe's
# text, so a path holding shell metacharacters stays inert data (a Blender path
# or a scenario name is a value, not syntax).
set positional-arguments

# List the recipes, which is what a bare `just` should do
default:
    @just --list

# The test suite; its live-GUI-Blender gate skips itself, so this launches nothing
test:
    {{PYTHON}} -m pytest

# Lint the lines this branch introduces. This is the enforced gate: the repository
# carries an inherited ruff backlog (see `lint-all`), so whole-tree cleanliness is
# not a hand-off precondition yet, but no line you write may add to it. Touching a
# legacy file does not make you responsible for the findings already in it.
lint base="origin/main":
    {{PYTHON}} scripts/lint_changed.py --base "$@"

# The whole inherited backlog, for tracking it down over time; not a gate
lint-all:
    {{PYTHON}} -m ruff check .

# Reformat in place; `fmt-check` is the gate, this is the fix
format:
    {{PYTHON}} -m ruff format .

# Fail on anything `format` would rewrite
fmt-check:
    {{PYTHON}} -m ruff format --check .

# Type-check src, tests and scripts (see [tool.pyright] for the include list)
typecheck:
    {{PYTHON}} -m basedpyright

# The four gates CLAUDE.md requires before a hand-off, cheapest first
check: lint fmt-check typecheck test

# Report revert-matrix rows whose anchor no longer applies to its target file
anchors:
    {{PYTHON}} scripts/check_revert_anchors.py

# Rewrite the committed snapshot of the add-on's dispatch surface. Run it with the
# protocol bump that a new or re-signed command requires; test_addon_surface.py is
# what refuses to let the two drift apart.
addon-surface:
    {{PYTHON}} scripts/update_addon_surface.py

# Prove each tracked test still fails with its fix reverted; `--only <prefix>` narrows it
matrix *args:
    {{PYTHON}} scripts/revert_matrix.py "$@"

# Size the advertised tools/list payload; no argument means the default core-only process
catalog toolsets="":
    {{PYTHON}} scripts/measure_catalog.py "$@"

# Diff every Blender API probe against its recorded baseline: how a new Blender is vetted
probes *args:
    {{PYTHON}} scripts/validate_blender_release.py "$@"

alias blender-validate := probes

# Accept the current probe transcripts as the baseline, once every diff is understood
probes-record *args:
    {{PYTHON}} scripts/validate_blender_release.py --record "$@"

# Which render/eevee/cycles/view-settings properties the tool schemas reach, and which they do not
coverage *args:
    {{PYTHON}} scripts/render_coverage.py "$@"

# Drive one scenario through the GUI rig (macOS only); name WORK when the scenario needs fixtures built beside it
rig scenario work="" *args:
    #!/usr/bin/env sh
    set -e
    scenario="$1"
    work="$2"
    shift 2
    if [ -z "$work" ]; then
        work=$(mktemp -d)
    fi
    mkdir -p "$work/rig"
    echo "rig work dir: $work"
    {{PYTHON}} scripts/blender_rig.py --work-dir "$work/rig" --scenario "$scenario" "$@"

# The phase-2 gate against a live GUI Blender, which `just test` deliberately skips
gate:
    BLENDERMCP_LIVE_RIG=1 {{PYTHON}} -m pytest -m phase2_gate

# Every tests/blender_*_smoke.py against a real headless Blender. pytest never
# collects these (they are scripts, not test_*.py) and CI has no Blender, so this
# is the only thing that runs the add-on against the actual API. No GUI: each runs
# under --background --factory-startup, and the whole set takes about 20 seconds.
smoke blender="/opt/homebrew/bin/blender":
    #!/usr/bin/env sh
    set -eu
    blender="$1"
    if [ ! -x "$blender" ]; then
        echo "no Blender at $blender; pass one: just smoke /path/to/blender" >&2
        exit 1
    fi
    failed=0
    log=$(mktemp)
    trap 'rm -f "$log"' EXIT
    for script in tests/blender_*_smoke.py; do
        # Each script prints its own <NAME>_OK verdict line; exiting 0 is not enough,
        # because Blender swallows a script traceback and still quits cleanly.
        "$blender" --background --factory-startup --python "$script" >"$log" 2>&1 || true
        if grep -q '_OK$' "$log"; then
            echo "ok    $script"
        else
            echo "FAIL  $script"
            # The tail, because a GPU-backed script can fail for reasons that have
            # nothing to do with the add-on and a bare FAIL cannot be told apart.
            sed 's/^/      | /' "$log" | tail -15
            failed=1
        fi
    done
    exit "$failed"
