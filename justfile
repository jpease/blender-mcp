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

# Lint every tracked tree, `scripts/` and `tests/` included
lint:
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
