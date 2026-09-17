"""
Re-run every probe in `scripts/blender_probes/` against a Blender binary and diff the transcripts.

Every Blender API fact this repository encodes came from one of those probes, so a new
Blender is validated by running them again and reading what changed. The probes print
observations, not assertions: a difference here is a fact to investigate, not a failure.

Usage::

    .venv/bin/python scripts/validate_blender_release.py [BLENDER]
    .venv/bin/python scripts/validate_blender_release.py --record   # after reading the diffs
    .venv/bin/python scripts/validate_blender_release.py --only linking --verbose

The binary comes from the argument, then `BLENDERMCP_BLENDER`, then the Homebrew path.
Baselines live in `scripts/blender_probes/baselines/<probe>.txt` and are only ever
written by `--record`, so an accidental run cannot erase the comparison.

Needs no `bpy` and no repository venv: probes run in Blender's own interpreter, and this
driver is stdlib-only, so a Blender install is the whole requirement. The GUI rig
(`scripts/blender_rig.py`) is not exercised here; validate it separately.
"""

import argparse
import difflib
import os
import re
import subprocess
import sys
import time

from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PROBE_DIR = _REPO_ROOT / "scripts" / "blender_probes"
_BASELINE_DIR = _PROBE_DIR / "baselines"
_DEFAULT_BLENDER = Path("/opt/homebrew/bin/blender")
_BLENDER_ENV_VAR = "BLENDERMCP_BLENDER"

# A probe whose transcript no normalisation can stabilise. Recording one would commit
# noise that fails every later run, so it is named here with the reason instead.
_UNREPRODUCIBLE: dict[str, str] = {}

_PROBE_TIMEOUT_SECONDS = 300
_VERSION_TIMEOUT_SECONDS = 120
_STDERR_TAIL_LINES = 20


@dataclass(frozen=True)
class ProbeRun:
    """One probe's captured result."""

    name: str
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ProbeVerdict:
    """What comparing one probe against its baseline established."""

    name: str
    state: str
    detail: str


def _blender_path(argument: str | None) -> Path:
    """
    Resolve which Blender to probe.

    Args:
        argument: The path given on the command line, or None.

    Returns:
        Path: The binary to run.

    """
    return Path(argument or os.environ.get(_BLENDER_ENV_VAR) or _DEFAULT_BLENDER)


def _blender_version(binary: Path) -> str:
    """
    Ask the binary for its own version string.

    Reported in the output and used by normalisation, so a patch release does not
    diff against every baseline on its version banner alone.

    Args:
        binary: The Blender to interrogate.

    Returns:
        str: `bpy.app.version_string`, e.g. `5.2.2 LTS`.

    Raises:
        SystemExit: If the binary will not run or prints no marked version.

    """
    marker = "BLENDERMCP_VERSION="
    try:
        completed = subprocess.run(
            [
                str(binary),
                "--background",
                "--factory-startup",
                "--python-expr",
                f"import bpy; print('{marker}' + bpy.app.version_string)",
            ],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"cannot run {binary}: {exc}") from exc
    for line in completed.stdout.splitlines():
        if line.startswith(marker):
            return line[len(marker) :].strip()
    raise SystemExit(f"{binary} did not report a version; exit {completed.returncode}")


def _probe_paths(only: str | None) -> list[Path]:
    """
    List the probes to run, in a stable order.

    Args:
        only: A substring a probe name must contain, or None for all of them.

    Returns:
        list[Path]: Matching probe scripts, excluding the unreproducible ones.

    """
    paths = sorted(p for p in _PROBE_DIR.glob("*.py") if p.stem not in _UNREPRODUCIBLE)
    return [p for p in paths if only is None or only in p.stem]


def _run_probe(binary: Path, probe: Path) -> ProbeRun:
    """
    Run one probe the way a human runs it, from the repository root.

    The cwd is fixed because probes reach for `tests/fixtures/` relatively and print the
    cwd itself, so a run from elsewhere would differ for no Blender-related reason.

    Args:
        binary: The Blender to run.
        probe: The probe script.

    Returns:
        ProbeRun: The exit code and both captured streams.

    """
    try:
        completed = subprocess.run(
            [str(binary), "--background", "--factory-startup", "--python", str(probe)],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        tail = (exc.stderr or b"").decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return ProbeRun(probe.stem, -1, "", f"timed out after {_PROBE_TIMEOUT_SECONDS}s\n{tail}")
    return ProbeRun(probe.stem, completed.returncode, completed.stdout, completed.stderr)


def _mask_temp_paths(text: str) -> str:
    """
    Replace the temp root and the random tail of each temp directory under it.

    Blender reports a temp path both as it was given and as it resolves, and which one
    it uses is itself an observation, so the two forms keep distinct placeholders. Each
    directory's `mkdtemp` prefix survives, because it says which probe stage made it.

    Args:
        text: One captured stream, version already masked.

    Returns:
        str: The same text with temp paths made comparable.

    """
    # $TMPDIR first: it is the form the probes were handed, and its resolved spelling
    # contains the raw one as a suffix.
    tmpdir = os.environ.get("TMPDIR", "").rstrip("/")
    if tmpdir:
        text = text.replace(str(Path(tmpdir).resolve()), "<real-tmp>").replace(tmpdir, "<tmp>")
    text = re.sub(r"/private/var/folders/[^/\s]+/[^/\s]+/T", "<real-tmp>", text)
    text = re.sub(r"/var/folders/[^/\s]+/[^/\s]+/T", "<tmp>", text)
    text = re.sub(r"(?<![\w/])/tmp(?=/)", "<tmp>", text)
    # `mkdtemp` appends exactly 8 characters to its prefix. Each directory is masked by
    # name rather than in place, because a probe may also print the bare basename, where
    # no surrounding path anchors it. Longest first, so no name is a prefix of another.
    directories = set(re.findall(r"<(?:real-)?tmp>/([^/\n]*?[a-z0-9_]{8})(?=[/\s'\"]|$)", text))
    for name in sorted(directories, key=len, reverse=True):
        text = text.replace(name, f"{name[:-8]}<rand>")
    return text


def _normalise(text: str, version: str) -> str:
    """
    Replace what varies between two runs of the same Blender, and nothing else.

    Only machine identity is erased: the version banner, absolute paths, temp directory
    names, session uids, `bpy_struct` addresses, epoch timestamps, the byte size of a
    written `.blend` (Blender does not write one byte-identically twice) and Blender's
    own log timestamps. Everything a probe prints on purpose -- error wording, enum
    values, handler order, counts, the small byte sizes it truncates to, the session-uid
    *sequence* -- is left alone, because that is what a new Blender is checked against.

    Args:
        text: One captured stream.
        version: The probing Blender's `version_string`.

    Returns:
        str: The comparable form.

    """
    text = re.sub(r"Blender \d+\.\d+\.\d+.*? \(hash \w+ built [^)]*\)", "Blender <version> (hash <build>)", text)
    text = text.replace(version, "<version>")
    text = _mask_temp_paths(text)
    text = text.replace(str(_REPO_ROOT), "<repo>").replace(str(Path.home()), "<home>")
    text = re.sub(r"0x[0-9a-f]{6,}", "0x<addr>", text)
    text = re.sub(r"\b[0-9a-f]{32}\b", "<uid>", text)
    text = re.sub(r"\b\d{13,20}\b", "<epoch>", text)
    text = re.sub(r"\b\d{5,12}\b", "<size>", text)
    return re.sub(r"(?m)^\d\d:\d\d\.\d{3}", "<t>", text)


def _transcript(run: ProbeRun, version: str) -> str:
    """
    Render one run as the text a baseline stores.

    The streams are kept apart rather than interleaved: both carry facts (Blender's
    driver-security refusals arrive on stderr), but their relative order is a buffering
    artefact that would diff for no reason.

    Args:
        run: The captured run.
        version: The probing Blender's `version_string`.

    Returns:
        str: The normalised transcript.

    """
    body = f"exit: {run.returncode}\n--- stdout ---\n{run.stdout}\n--- stderr ---\n{run.stderr}"
    return _normalise(body, version)


def _stderr_tail(run: ProbeRun) -> str:
    """
    Quote the end of stderr, which is where a crash says why.

    Args:
        run: The captured run.

    Returns:
        str: The indented tail, or a note that stderr was empty.

    """
    lines = run.stderr.strip().splitlines()[-_STDERR_TAIL_LINES:]
    return "\n".join(f"    {line}" for line in lines) if lines else "    (stderr was empty)"


def _compare(run: ProbeRun, version: str) -> ProbeVerdict:
    """
    Judge one run against its recorded baseline.

    Args:
        run: The captured run.
        version: The probing Blender's `version_string`.

    Returns:
        ProbeVerdict: `match`, `differs`, `crashed` or `no baseline`, with the evidence.

    """
    if run.returncode != 0:
        return ProbeVerdict(run.name, "crashed", f"exit {run.returncode}\n{_stderr_tail(run)}")
    baseline_path = _BASELINE_DIR / f"{run.name}.txt"
    if not baseline_path.is_file():
        return ProbeVerdict(run.name, "no baseline", f"nothing recorded at {baseline_path.relative_to(_REPO_ROOT)}")
    current = _transcript(run, version)
    baseline = baseline_path.read_text()
    if current == baseline:
        return ProbeVerdict(run.name, "match", "")
    diff = difflib.unified_diff(
        baseline.splitlines(keepends=True),
        current.splitlines(keepends=True),
        fromfile=f"baseline/{run.name}.txt",
        tofile=f"observed/{run.name}.txt",
    )
    return ProbeVerdict(run.name, "differs", "".join(diff))


def _record(run: ProbeRun, version: str) -> ProbeVerdict:
    """
    Write one run's transcript as the new baseline.

    Args:
        run: The captured run.
        version: The probing Blender's `version_string`.

    Returns:
        ProbeVerdict: `recorded`, or `crashed` for a run too broken to be a baseline.

    """
    if run.returncode != 0:
        return ProbeVerdict(run.name, "crashed", f"exit {run.returncode}; not recorded\n{_stderr_tail(run)}")
    _BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    (_BASELINE_DIR / f"{run.name}.txt").write_text(_transcript(run, version))
    return ProbeVerdict(run.name, "recorded", "")


def _parse_args() -> argparse.Namespace:
    """
    Read the command line.

    Returns:
        argparse.Namespace: The parsed options.

    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "blender", nargs="?", help=f"Blender binary (default: ${_BLENDER_ENV_VAR} or {_DEFAULT_BLENDER})"
    )
    parser.add_argument("--record", action="store_true", help="overwrite the baselines with this run")
    parser.add_argument("--only", metavar="SUBSTRING", help="run only probes whose name contains this")
    parser.add_argument("--verbose", action="store_true", help="print each probe as it starts")
    return parser.parse_args()


def main() -> int:
    """
    Run the probes and report what the recorded baselines say about them.

    Returns:
        int: 0 when every probe matched (or was recorded), 1 otherwise.

    """
    args = _parse_args()
    binary = _blender_path(args.blender)
    probes = _probe_paths(args.only)
    if not probes:
        print(f"no probes matched --only {args.only!r} in {_PROBE_DIR.relative_to(_REPO_ROOT)}")
        return 1

    version = _blender_version(binary)
    print(f"blender  : {binary}")
    print(f"version  : {version}")
    print(f"probes   : {len(probes)}")
    print(f"mode     : {'recording baselines' if args.record else 'comparing against baselines'}\n")
    for name, reason in sorted(_UNREPRODUCIBLE.items()):
        print(f"skipped  : {name} -- {reason}")

    started = time.monotonic()
    verdicts: list[ProbeVerdict] = []
    for probe in probes:
        if args.verbose:
            print(f"running {probe.stem} ...", flush=True)
        run = _run_probe(binary, probe)
        verdict = _record(run, version) if args.record else _compare(run, version)
        verdicts.append(verdict)
        print(f"{verdict.state:>12}  {verdict.name}")
        if verdict.detail:
            print(verdict.detail if verdict.detail.endswith("\n") else verdict.detail + "\n")
    elapsed = time.monotonic() - started

    unclean = [v for v in verdicts if v.state not in {"match", "recorded"}]
    print(f"\n{len(verdicts) - len(unclean)}/{len(verdicts)} clean in {elapsed:.0f}s against Blender {version}")
    if not unclean:
        return 0
    print("A difference is a fact to investigate, not necessarily a failure: these probes")
    print("observe Blender rather than assert about it, so read each diff and decide whether")
    print("Blender changed, this repository's assumption changed, or the baseline is stale.")
    print("Once each one is understood, re-record with --record.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
