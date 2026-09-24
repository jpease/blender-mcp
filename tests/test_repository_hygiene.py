"""
Keep private production material out of a public repository.

The add-on is developed against a real studio's assets, and their names and the models
themselves are not ours to publish. Rig *prefixes* such as `CHAR1_` are agreed to be fine -
they name a convention, not a character - but the words behind them are not, and a `.blend`
of somebody's character never is.

The forbidden words are not in this repository in any form. Hashing them here would not have
helped: a SHA-256 of a short word is reversible with a wordlist in milliseconds, and naming
the *category* beside it supplies the wordlist. They are read at run time from
`BLENDERMCP_PRIVATE_TERMS` (comma-separated) or from an untracked `.private-terms` file at
the repository root, one term per line; with neither present the word check skips and the
asset checks still run, which is what happens in CI.
"""

from __future__ import annotations

import os
import re
import subprocess

from pathlib import Path

import pytest

from conftest import REPO_ROOT

# Where the private terms come from, neither of which is tracked.
_TERMS_ENV_VAR = "BLENDERMCP_PRIVATE_TERMS"
_TERMS_FILE = REPO_ROOT / ".private-terms"


def _private_terms() -> set[str]:
    """
    Read the words that must not appear, from outside the repository.

    Returns:
        set[str]: Lowercase terms, empty when none are configured on this machine.

    """
    raw = os.environ.get(_TERMS_ENV_VAR, "")
    if not raw and _TERMS_FILE.is_file():
        raw = _TERMS_FILE.read_text(encoding="utf-8").replace("\n", ",")
    return {term.strip().lower() for term in raw.split(",") if term.strip()}


# Extensions that carry model or image data. Anything tracked under one of these has to be
# named here with a reason, so committing an asset is a decision rather than an accident.
_ASSET_SUFFIXES = frozenset(
    {
        ".abc",
        ".blend",
        ".blend1",
        ".exr",
        ".fbx",
        ".jpeg",
        ".jpg",
        ".ma",
        ".mb",
        ".mov",
        ".mp4",
        ".obj",
        ".png",
        ".psd",
        ".tif",
        ".tiff",
        ".usd",
        ".usda",
        ".usdc",
    }
)
_ALLOWED_ASSETS = {
    # Two empty .blend files, one per compression, for the header and load-path checks.
    "tests/fixtures/blend/empty_gzip.blend",
    "tests/fixtures/blend/empty_zstd.blend",
    # Screenshots of this add-on's own UI, for the README.
    "assets/addon-instructions.png",
    "assets/hammer-icon.png",
}


def _term_pattern(terms: set[str]) -> re.Pattern[str]:
    """
    Match any term as a whole word, including a term that is not purely letters (`a-b`).

    Args:
        terms: Lowercase terms from `_private_terms`.

    Returns:
        re.Pattern[str]: A pattern to search lowercased text with.

    """
    alternatives = "|".join(re.escape(term) for term in sorted(terms))
    return re.compile(rf"(?<![a-z])(?:{alternatives})(?![a-z])")


# A .blend of a production character is megabytes; the empty fixtures are ~85 KB. Anything
# tracked above this is an asset that slipped in, whatever its extension claims.
_MAX_TRACKED_BYTES = 512 * 1024


def _tracked_files() -> list[str]:
    """
    List the repository's tracked paths.

    Returns:
        list[str]: Repository-relative paths, as git records them.

    """
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [path for path in listing.stdout.split("\0") if path]


def test_no_tracked_file_names_private_production_material() -> None:
    """A character's name in a public test fixture is a leak, however harmless the test is."""
    terms = _private_terms()
    if not terms:
        pytest.skip(f"no private terms configured; set {_TERMS_ENV_VAR} or write {_TERMS_FILE.name}")
    pattern = _term_pattern(terms)
    offenders: list[str] = []
    for path in _tracked_files():
        full = REPO_ROOT / path
        if not full.is_file() or full.suffix.lower() in _ASSET_SUFFIXES:
            continue
        try:
            text = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if pattern.search(text.lower()):
            offenders.append(path)
    assert not offenders, f"tracked files name private production material: {sorted(offenders)}"


def test_no_commit_message_names_private_production_material() -> None:
    """A commit message is published with the history, so it leaks exactly as a file does."""
    terms = _private_terms()
    if not terms:
        pytest.skip(f"no private terms configured; set {_TERMS_ENV_VAR} or write {_TERMS_FILE.name}")
    pattern = _term_pattern(terms)
    history = subprocess.run(
        ["git", "log", "HEAD", "--format=%h%x00%B%x1e"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    offenders = [
        commit
        for record in history.stdout.split("\x1e")
        if record.strip()
        for commit, _, message in [record.strip().partition("\0")]
        if pattern.search(message.lower())
    ]
    assert not offenders, f"commit messages name private production material: {offenders}"


def test_the_only_tracked_assets_are_the_ones_named_here() -> None:
    """Nobody's model ships by accident: a new tracked asset has to be admitted deliberately."""
    tracked_assets = {path for path in _tracked_files() if Path(path).suffix.lower() in _ASSET_SUFFIXES}

    assert tracked_assets == _ALLOWED_ASSETS, (
        f"unexpected tracked assets: {sorted(tracked_assets - _ALLOWED_ASSETS)}; "
        f"missing: {sorted(_ALLOWED_ASSETS - tracked_assets)}"
    )


def test_no_tracked_file_is_large_enough_to_be_an_asset() -> None:
    """The extension list cannot catch a renamed or extensionless export; the size does."""
    oversized = {
        path: (REPO_ROOT / path).stat().st_size
        for path in _tracked_files()
        if (REPO_ROOT / path).is_file() and (REPO_ROOT / path).stat().st_size > _MAX_TRACKED_BYTES
    }

    assert not oversized, f"tracked files are asset-sized: {oversized}"
