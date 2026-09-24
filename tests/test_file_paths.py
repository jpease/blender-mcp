"""
Adversarial coverage for the addon's filesystem trust boundary.

Path canonicalization fails silently, so each attack has its own test. The magic-byte cases
use real `.blend` files, because only a positive test catches a false rejection.

The Blender error strings below are copied from real `RuntimeError`s, not written by hand:
Blender's message format is not a contract, so an approximation would test the sanitizer
against the wrong text.
"""

import ast
import gzip
import os
import re
import sys

from pathlib import Path
from types import ModuleType

import pytest

from conftest import load_addon_source_module

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "blend"
FILE_PATHS_SOURCE = Path(__file__).resolve().parent.parent / "src/blender_mcp/bundled/addon/file_paths.py"

# --- captured from Blender 5.2.2 error text ---
_WORK = "/var/folders/87/ykdcq2j525x7kkhl13f1lrm80000gn/T/fp_shapes_4ucfn0pm"
_HOSTILE_WORK = "/var/folders/87/ykdcq2j525x7kkhl13f1lrm80000gn/T/fp shapes o'brien alk8jmkc"
# The capture ran from the repository root, so the empty-path shape named this checkout.
_CWD = str(Path(__file__).resolve().parent.parent)
SHAPE_1_MISSING = f'Error: Cannot read file "{_WORK}/missing.blend": No such file or directory\n'
SHAPE_2_DIRECTORY = f'Error: File format is not supported in file "{_WORK}"\n'
SHAPE_2_EMPTY_PATH = f'Error: File format is not supported in file "{_CWD}"\n'
SHAPE_3_TRUNCATED = (
    f'Error: Loading "{_WORK}/truncated.blend" failed: '
    f"Failed to read blend file '{_WORK}/truncated.blend': Missing DNA block\n"
)
SHAPE_4_SAVE = f"Error: Cannot open file {_WORK}/no/such/dir/x.blend@ for writing: No such file or directory\n"
SHAPE_5_RELOAD = f"Error: Trying to reload library 'LIgood.blend' from invalid path '{_WORK}/gone.blend'\n"
HOSTILE_SHAPE_3 = (
    f'Error: Loading "{_HOSTILE_WORK}/truncated.blend" failed: '
    f"Failed to read blend file '{_HOSTILE_WORK}/truncated.blend': Missing DNA block\n"
)
HOSTILE_SHAPE_4 = (
    f"Error: Cannot open file {_HOSTILE_WORK}/no/such/dir/x.blend@ for writing: No such file or directory\n"
)
HOSTILE_SHAPE_5 = f"Error: Trying to reload library 'LIgood.blend' from invalid path '{_HOSTILE_WORK}/gone.blend'\n"
# --- captured from Blender 5.2.2: a `Library.name` set to an absolute path, quoted with its `LI` code ---
_NAME_WORK = "/private/var/folders/87/ykdcq2j525x7kkhl13f1lrm80000gn/T/t7_handlers_d4j7t6wp"
_NAME_TAIL = f"from invalid path '{_NAME_WORK}/moved_canon.blend'\n"
HOSTILE_LIBRARY_NAME_SHAPE_5 = f"Error: Trying to reload library 'LI/Users/victim/shots/canon.blend' {_NAME_TAIL}"
# --- captured the same way: a newline inside the hostile name ---
_NEWLINE_WORK = "/private/var/folders/87/ykdcq2j525x7kkhl13f1lrm80000gn/T/t7_handlers_aeqmk5c4"
NEWLINE_LIBRARY_NAME_SHAPE_5 = (
    "Error: Trying to reload library 'LI/Users/victim/a\n/b.blend' "
    f"from invalid path '{_NEWLINE_WORK}/moved_canon_l.blend'\n"
)

# An oracle independent of the implementation's own detection: any separator or
# home marker that starts a token, and any drive-letter root. The placeholder
# and every cause phrase Blender uses contain neither.
_ABSOLUTE_TOKEN = re.compile(r"(?<![\w.])(?:[/\\~][\w.~ '-]|[A-Za-z]:[\\/])")
_LAYOUT_FRAGMENTS = ("/var/", "/Users/", "/private/", "folders", "o'brien", "fp_shapes", "fp shapes")


def _file_paths() -> ModuleType:
    """
    Load the addon's `file_paths` module straight from source.

    Returns:
        ModuleType: A freshly executed copy.

    """
    return load_addon_source_module("file_paths.py", "addon_file_paths_under_test")


def _assert_no_absolute_path(text: str, *also_absent: str) -> None:
    """
    Assert a client-facing message names no filesystem location.

    Args:
        text: The message a client would receive.
        *also_absent: Specific paths that must not appear, e.g. a tmp_path.

    """
    assert not _ABSOLUTE_TOKEN.search(text), f"an absolute path token survived: {text!r}"
    for fragment in (*_LAYOUT_FRAGMENTS, *also_absent):
        assert fragment not in text, f"{fragment!r} survived in {text!r}"


def _refusal(module: ModuleType, raw: object, *, must_exist: bool, match: str, tmp_path: Path) -> None:
    """
    Assert `resolve_blend_path` refuses `raw` for the stated reason, without leaking a path.

    Args:
        module: The loaded `file_paths` module.
        raw: The candidate path.
        must_exist: Passed through.
        match: Regex the refusal message must match.
        tmp_path: The test's directory, which must not appear in the message.

    """
    with pytest.raises(ValueError, match=match) as caught:
        module.resolve_blend_path(raw, roots=[], must_exist=must_exist)
    _assert_no_absolute_path(str(caught.value), str(tmp_path), os.path.realpath(tmp_path))


def _uncompressed_blend_bytes() -> bytes:
    """
    Recover a real uncompressed 5.x `.blend` from the committed gzip fixture.

    Returns:
        bytes: The file Blender wrote with `compress=False`.

    """
    return gzip.decompress((FIXTURES / "empty_gzip.blend").read_bytes())


def _write(path: Path, content: bytes) -> Path:
    """
    Write bytes to a path, creating parents.

    Args:
        path: Destination.
        content: Bytes to write.

    Returns:
        Path: The same path.

    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------------------
# The module stays free of bpy
# ---------------------------------------------------------------------------


def test_file_paths_imports_no_bpy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pure functions only; the `//` expansion happens at the call site, where `bpy` is."""
    tree = ast.parse(FILE_PATHS_SOURCE.read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names} | {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not any(name == "bpy" or name.startswith("bpy.") for name in imported), imported

    monkeypatch.setitem(sys.modules, "bpy", None)  # any `import bpy` now raises ImportError
    assert _file_paths().resolve_blend_path


# ---------------------------------------------------------------------------
# resolve_blend_path: input shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, 7, b"shot.blend", ["shot.blend"], Path("shot.blend")], ids=repr)
def test_a_non_string_path_is_refused(raw: object, tmp_path: Path) -> None:
    """A `Path` or bytes is not what the socket's JSON can carry; anything else is hostile."""
    _refusal(_file_paths(), raw, must_exist=False, match="must be a string", tmp_path=tmp_path)


def test_an_empty_path_is_refused(tmp_path: Path) -> None:
    """Blender resolves an empty open to the process CWD and leaks it (shape 2)."""
    _refusal(_file_paths(), "", must_exist=True, match="must not be empty", tmp_path=tmp_path)


def test_a_whitespace_only_path_is_refused(tmp_path: Path) -> None:
    """Whitespace is empty in every sense a caller could mean."""
    _refusal(_file_paths(), " \t ", must_exist=True, match="must not be empty", tmp_path=tmp_path)


def test_a_nul_byte_is_refused(tmp_path: Path) -> None:
    """A NUL truncates the path in C; refuse it before any filesystem call sees it."""
    _refusal(_file_paths(), f"{tmp_path}/shot\x00.blend", must_exist=False, match="NUL", tmp_path=tmp_path)


def test_an_unexpanded_blender_relative_prefix_is_refused(tmp_path: Path) -> None:
    """`//` reaching here means the call site forgot `bpy.path.abspath`; POSIX would read it as root."""
    _refusal(_file_paths(), "//shot.blend", must_exist=False, match="Blender-relative", tmp_path=tmp_path)


# ---------------------------------------------------------------------------
# resolve_blend_path: the suffix
# ---------------------------------------------------------------------------


def test_a_non_blend_extension_is_refused(tmp_path: Path) -> None:
    """Only `.blend` files go through this boundary."""
    _refusal(_file_paths(), str(tmp_path / "shot.txt"), must_exist=False, match=r"\.blend", tmp_path=tmp_path)


def test_a_trailing_dot_after_the_blend_suffix_is_refused(tmp_path: Path) -> None:
    """Windows strips a trailing dot and macOS does not, so `x.blend.` names two different files."""
    _refusal(_file_paths(), str(tmp_path / "shot.blend."), must_exist=False, match=r"\.blend", tmp_path=tmp_path)


def test_a_trailing_space_after_the_blend_suffix_is_refused(tmp_path: Path) -> None:
    """The same inconsistency as the trailing dot, for a space."""
    _refusal(_file_paths(), str(tmp_path / "shot.blend "), must_exist=False, match=r"\.blend", tmp_path=tmp_path)


def test_the_blend_suffix_is_matched_case_insensitively(tmp_path: Path) -> None:
    """`SHOT.BLEND` is a real file on a case-insensitive volume and a legitimate name everywhere."""
    resolved = _file_paths().resolve_blend_path(str(tmp_path / "SHOT.BLEND"), roots=[], must_exist=False)

    assert resolved == os.path.join(os.path.realpath(tmp_path), "SHOT.BLEND")


# ---------------------------------------------------------------------------
# resolve_blend_path: canonical form
# ---------------------------------------------------------------------------


def test_a_bare_relative_path_comes_out_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`bpy.path.abspath` passes a bare relative path through unchanged, so this function must absolutize it."""
    monkeypatch.chdir(tmp_path)

    resolved = _file_paths().resolve_blend_path("relative.blend", roots=[], must_exist=False)

    assert os.path.isabs(resolved)
    assert resolved == os.path.join(os.path.realpath(tmp_path), "relative.blend")


def test_tilde_expands_to_the_home_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`~` must mean home, not a directory literally named `~` under the process CWD."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)

    resolved = _file_paths().resolve_blend_path("~/shot.blend", roots=[], must_exist=False)

    assert resolved == os.path.join(os.path.realpath(home), "shot.blend")


def test_blenders_relative_form_resolves_inside_the_blend_directory(tmp_path: Path) -> None:
    """
    A `//` expansion under a symlinked directory resolves to its canonical form.

    On macOS the temp dir `/var/...` is a symlink to `/private/var/...`. A path left in the
    linked form would be compared against roots under a name the file does not really have.
    """
    (tmp_path / "shots" / "fx" / "sub").mkdir(parents=True)
    (tmp_path / "linked_shots").symlink_to(tmp_path / "shots", target_is_directory=True)
    expanded = f"{tmp_path}/linked_shots/fx/sub/shot.blend"

    resolved = _file_paths().resolve_blend_path(expanded, roots=[], must_exist=False)

    assert resolved == os.path.join(os.path.realpath(tmp_path / "shots"), "fx", "sub", "shot.blend")


def test_a_relative_form_climbing_out_of_the_blend_directory_is_normalised(tmp_path: Path) -> None:
    """`bpy.path.abspath('//../escape.blend')` keeps the literal `..`, so resolution must remove it."""
    module = _file_paths()
    blend_dir = tmp_path / "fx"
    blend_dir.mkdir()
    climbing = f"{blend_dir}/../escape.blend"

    resolved = module.resolve_blend_path(climbing, roots=[], must_exist=False)

    assert resolved == os.path.join(os.path.realpath(tmp_path), "escape.blend")
    with pytest.raises(ValueError, match="outside"):
        module.resolve_blend_path(climbing, roots=[str(blend_dir)], must_exist=False)


def test_dotdot_traversal_is_normalised_before_the_containment_check(tmp_path: Path) -> None:
    """`<root>/sub/../../outside.blend` shares the root's prefix textually and lies outside it."""
    module = _file_paths()
    root = tmp_path / "output"
    (root / "sub").mkdir(parents=True)
    escaping = f"{root}/sub/../../outside.blend"

    resolved = module.resolve_blend_path(escaping, roots=[], must_exist=False)

    assert ".." not in resolved
    with pytest.raises(ValueError, match="outside") as caught:
        module.resolve_blend_path(escaping, roots=[str(root)], must_exist=False)
    _assert_no_absolute_path(str(caught.value), str(tmp_path))


def test_a_symlink_inside_a_root_pointing_outside_it_is_refused(tmp_path: Path) -> None:
    """`abspath` keeps the link's own name, which sits inside the root; only `realpath` sees where it goes."""
    module = _file_paths()
    root = tmp_path / "output"
    root.mkdir()
    target = _write(tmp_path / "outside" / "secret.blend", (FIXTURES / "empty_zstd.blend").read_bytes())
    link = root / "innocent.blend"
    link.symlink_to(target)

    resolved = module.resolve_blend_path(str(link), roots=[], must_exist=True)

    assert resolved == os.path.realpath(target)
    with pytest.raises(ValueError, match="outside") as caught:
        module.resolve_blend_path(str(link), roots=[str(root)], must_exist=True)
    _assert_no_absolute_path(str(caught.value), str(tmp_path))


def test_a_symlinked_parent_directory_is_refused(tmp_path: Path) -> None:
    """A save target whose *directory* is a link out of the root escapes it just as a file link does."""
    module = _file_paths()
    root = tmp_path / "output"
    root.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (root / "shots").symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    resolved = module.resolve_blend_path(str(root / "shots" / "new.blend"), roots=[], must_exist=False)

    assert resolved == os.path.join(os.path.realpath(tmp_path / "elsewhere"), "new.blend")
    with pytest.raises(ValueError, match="outside"):
        module.resolve_blend_path(str(root / "shots" / "new.blend"), roots=[str(root)], must_exist=False)


# ---------------------------------------------------------------------------
# resolve_blend_path: what must exist
# ---------------------------------------------------------------------------


def test_a_directory_where_a_file_is_expected_is_refused(tmp_path: Path) -> None:
    """A directory named `x.blend` passes the suffix check; Blender answers it with shape 2."""
    (tmp_path / "looks_like.blend").mkdir()

    _refusal(_file_paths(), str(tmp_path / "looks_like.blend"), must_exist=True, match="directory", tmp_path=tmp_path)


def test_a_directory_where_a_save_target_is_expected_is_refused(tmp_path: Path) -> None:
    """Saving over a directory is never the caller's intent."""
    (tmp_path / "looks_like.blend").mkdir()

    _refusal(_file_paths(), str(tmp_path / "looks_like.blend"), must_exist=False, match="directory", tmp_path=tmp_path)


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    """Refused here with no path in the text, instead of by Blender with one (shape 1)."""
    _refusal(_file_paths(), str(tmp_path / "absent.blend"), must_exist=True, match="does not exist", tmp_path=tmp_path)


def test_a_mistyped_directory_and_a_mistyped_filename_are_not_the_same_refusal(tmp_path: Path) -> None:
    """
    "file does not exist" alone made a typo'd folder and a typo'd filename one sentence.

    The path itself stays out of the message - naming it would hand a client this machine's
    layout, which every other refusal here refuses to do - so the half that is wrong is named
    instead of the location that is wrong.
    """
    module = _file_paths()
    (tmp_path / "shots").mkdir()

    _refusal(
        module,
        str(tmp_path / "shots" / "sh040.blend"),
        must_exist=True,
        match="does not exist, though the directory named in its path does",
        tmp_path=tmp_path,
    )
    _refusal(
        module,
        str(tmp_path / "shotz" / "sh040.blend"),
        must_exist=True,
        match="does not exist, and neither does the directory named in its path",
        tmp_path=tmp_path,
    )


def test_a_file_whose_magic_bytes_are_not_a_blend_is_refused(tmp_path: Path) -> None:
    """A `.blend` name on a zip is exactly what a hostile download would look like."""
    fake = _write(tmp_path / "fake.blend", b"PK\x03\x04" + b"\x00" * 64)

    _refusal(_file_paths(), str(fake), must_exist=True, match="not a .blend", tmp_path=tmp_path)


def test_an_unreadable_file_is_refused_without_naming_it(tmp_path: Path) -> None:
    """`OSError`'s own text carries the path, so it must not be interpolated into the refusal."""
    locked = _write(tmp_path / "locked.blend", (FIXTURES / "empty_zstd.blend").read_bytes())
    locked.chmod(0o000)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("running as a user that ignores file modes")
        _refusal(_file_paths(), str(locked), must_exist=True, match="could not be read", tmp_path=tmp_path)
    finally:
        locked.chmod(0o600)


def test_a_save_target_whose_directory_does_not_exist_is_refused(tmp_path: Path) -> None:
    """Blender would fail later with shape 4 and its derived `@` path."""
    _refusal(
        _file_paths(),
        str(tmp_path / "no" / "such" / "x.blend"),
        must_exist=False,
        match="directory does not exist",
        tmp_path=tmp_path,
    )


def test_a_missing_save_directory_is_accepted_and_created_only_on_opt_in(tmp_path: Path) -> None:
    """`create_directories` defers a missing directory to `create_save_directory`, which makes every level."""
    module = _file_paths()
    target = tmp_path / "canon" / "shots" / "sh010.blend"

    resolved = module.resolve_blend_path(str(target), roots=[], must_exist=False, create_directories=True)

    assert not target.parent.exists(), "resolving must not create anything"
    assert module.create_save_directory(resolved) is True
    assert target.parent.is_dir()
    assert module.create_save_directory(resolved) is False


def test_a_save_directory_blocked_by_a_file_is_refused_without_naming_it(tmp_path: Path) -> None:
    """A file where a directory must go is a clean refusal whose message names no path."""
    (tmp_path / "canon").write_text("not a directory")
    target = str(tmp_path / "canon" / "shots" / "sh010.blend")

    with pytest.raises(ValueError, match="could not be created") as refusal:
        _file_paths().create_save_directory(target)

    assert str(tmp_path) not in str(refusal.value)


def test_a_save_target_in_a_read_only_directory_is_refused(tmp_path: Path) -> None:
    """Existing is not enough for a save; the process must be able to write there."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        if os.access(locked, os.W_OK):
            pytest.skip("running as a user that ignores the write bit")
        _refusal(_file_paths(), str(locked / "x.blend"), must_exist=False, match="not writable", tmp_path=tmp_path)
    finally:
        locked.chmod(0o700)


# ---------------------------------------------------------------------------
# The three magic prefixes, proven against real files (false rejection)
# ---------------------------------------------------------------------------


def test_an_uncompressed_blend_is_accepted(tmp_path: Path) -> None:
    """Blender 5.x with `compress=False` writes `BLENDER17-01v050`."""
    path = _write(tmp_path / "plain.blend", _uncompressed_blend_bytes())
    assert path.read_bytes().startswith(b"BLENDER17-01v050")

    assert _file_paths().resolve_blend_path(str(path), roots=[], must_exist=True) == os.path.realpath(path)


def test_a_zstd_compressed_blend_is_accepted() -> None:
    """The 5.x compressed form; a `BLENDER`-only check rejects every canon file saved with compression."""
    path = FIXTURES / "empty_zstd.blend"
    assert path.read_bytes().startswith(b"\x28\xb5\x2f\xfd")

    assert _file_paths().resolve_blend_path(str(path), roots=[], must_exist=True) == os.path.realpath(path)


def test_a_gzip_blend_written_without_an_fname_is_accepted() -> None:
    """FLG is zero in a normal gzip `.blend`; a 4-byte constant pins it to 8 (FNAME set) and rejects it."""
    path = FIXTURES / "empty_gzip.blend"
    assert path.read_bytes()[:4] == b"\x1f\x8b\x08\x00"

    assert _file_paths().resolve_blend_path(str(path), roots=[], must_exist=True) == os.path.realpath(path)


def test_a_pre_5x_blend_header_is_accepted(tmp_path: Path) -> None:
    """`BLENDER-v293` is what a 12-byte `BLENDER17-01` constant rejects."""
    body = _uncompressed_blend_bytes()
    path = _write(tmp_path / "legacy.blend", b"BLENDER-v293" + body[17:])

    assert _file_paths().resolve_blend_path(str(path), roots=[], must_exist=True) == os.path.realpath(path)


# ---------------------------------------------------------------------------
# is_blend_header, the pure predicate the file check is built on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        pytest.param(b"BLENDER17-01v050", id="5x-uncompressed"),
        pytest.param(b"BLENDER-v293", id="pre-5x-uncompressed"),
        pytest.param(b"\x28\xb5\x2f\xfd\x60\x38", id="zstd"),
        pytest.param(b"\x1f\x8b\x08\x08\x00\x00", id="gzip-with-an-fname"),
    ],
)
def test_every_header_form_blender_writes_is_recognised(header: bytes) -> None:
    """Each prefix is only as long as it must be: the version digits and gzip's FLG byte vary."""
    assert _file_paths().is_blend_header(header) is True


@pytest.mark.parametrize(
    "header",
    [
        pytest.param(b"", id="empty-file"),
        pytest.param(b"BLEND", id="truncated-to-inside-the-magic"),
        pytest.param(b"blender17-01v050", id="lowercased-near-miss"),
        pytest.param(b"\x28\xb5\x2f\xfc\x60\x38", id="one-byte-off-zstd"),
    ],
)
def test_anything_that_is_not_a_header_is_rejected(header: bytes) -> None:
    """A short read must not be generous: a truncated or empty file is not a `.blend`."""
    assert _file_paths().is_blend_header(header) is False


# ---------------------------------------------------------------------------
# contains, the pure spelling half of the containment check
# ---------------------------------------------------------------------------


def test_a_root_contains_itself_and_what_lies_under_it() -> None:
    """Saving into the root directory itself is inside it, as is any depth below."""
    module = _file_paths()

    assert module.contains("/output", "/output") is True
    assert module.contains("/output", "/output/shots/fx/x.blend") is True


def test_a_sibling_sharing_the_roots_spelling_is_not_contained() -> None:
    """The `startswith` bug, at the predicate: `/output-evil` is not under `/output`."""
    assert _file_paths().contains("/output", "/output-evil/x.blend") is False


def test_a_root_on_another_windows_drive_contains_nothing() -> None:
    """Two drive letters have no common path; on posix they simply share no component."""
    assert _file_paths().contains("C:\\output", "D:\\output\\x.blend") is False


def test_paths_that_cannot_be_compared_are_reported_as_not_contained() -> None:
    """`commonpath` raises on an absolute and a relative path; that must not escape as the refusal."""
    assert _file_paths().contains("/output", "relative/x.blend") is False


# ---------------------------------------------------------------------------
# The verdicts, decided from facts alone: no filesystem, no Blender
# ---------------------------------------------------------------------------


def test_a_path_is_authorized_by_any_one_root_and_by_no_roots_at_all() -> None:
    """Unset roots are the local-artist default; configured ones are checked as a set, not in order."""
    module = _file_paths()

    assert module.inside_roots("/anywhere/x.blend", []) is True
    assert module.inside_roots("/output/x.blend", ["/canon", "/output"]) is True
    assert module.inside_roots("/output-evil/x.blend", ["/canon", "/output"]) is False


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        pytest.param(
            {"is_directory": True, "exists": False, "readable": True, "header": b"", "directory_exists": True},
            "path is a directory, not a .blend file",
            id="a directory named x.blend is not a file",
        ),
        pytest.param(
            {"is_directory": False, "exists": False, "readable": True, "header": b"", "directory_exists": True},
            "file does not exist, though the directory named in its path does",
            id="a mistyped filename",
        ),
        pytest.param(
            {"is_directory": False, "exists": False, "readable": True, "header": b"", "directory_exists": False},
            "file does not exist, and neither does the directory named in its path",
            id="a mistyped directory - the same sentence until this split them",
        ),
        pytest.param(
            {"is_directory": False, "exists": True, "readable": False, "header": b"", "directory_exists": True},
            "file could not be read",
            id="unreadable is refused, not treated as a bad header",
        ),
        pytest.param(
            {
                "is_directory": False,
                "exists": True,
                "readable": True,
                "header": b"PK\x03\x04",
                "directory_exists": True,
            },
            "file is not a .blend file (unrecognised header)",
            id="a zip renamed .blend",
        ),
        pytest.param(
            {
                "is_directory": False,
                "exists": True,
                "readable": True,
                "header": b"BLENDER17-01",
                "directory_exists": True,
            },
            None,
            id="a real .blend",
        ),
    ],
)
def test_the_open_verdict_is_decided_from_facts_alone(facts: dict, expected: str | None) -> None:
    """Each refusal names the first thing that is wrong, so an unreadable file never reads as the wrong format."""
    assert _file_paths().blend_file_refusal(**facts) == expected


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        pytest.param(
            {"is_directory": True, "directory_exists": True, "directory_writable": True},
            "path is a directory, not a .blend file",
            id="saving over a directory",
        ),
        pytest.param(
            {"is_directory": False, "directory_exists": False, "directory_writable": False},
            None,
            id="a missing directory the caller opted into creating",
        ),
        pytest.param(
            {"is_directory": False, "directory_exists": True, "directory_writable": False},
            "target directory is not writable",
            id="create_directories does not excuse an unwritable existing directory",
        ),
    ],
)
def test_the_save_verdict_holds_when_the_caller_opted_into_creating_directories(
    facts: dict, expected: str | None
) -> None:
    """`create_directories` waives exactly one refusal, and the waiver must not spread to the others."""
    assert _file_paths().save_target_refusal(**facts, create_directories=True) == expected


def test_a_missing_save_directory_is_refused_without_the_opt_in() -> None:
    """The same facts, the other way round: without the flag a missing directory is the refusal."""
    refusal = _file_paths().save_target_refusal(
        is_directory=False, directory_exists=False, directory_writable=False, create_directories=False
    )

    assert refusal == "target directory does not exist; pass create_directories=true to create it"


# ---------------------------------------------------------------------------
# enforce_roots
# ---------------------------------------------------------------------------


def test_no_configured_roots_enforces_nothing(tmp_path: Path) -> None:
    """Permissive when unset: the local-artist case, observable in the handshake."""
    assert _file_paths().enforce_roots(str(tmp_path / "anywhere.blend"), []) is None


def test_a_path_inside_a_root_is_accepted_even_when_the_root_has_a_trailing_separator(tmp_path: Path) -> None:
    """`/output/` is how an operator writes a directory; it must still contain `/output/x.blend`."""
    root = tmp_path / "output"
    root.mkdir()

    assert _file_paths().enforce_roots(str(root / "x.blend"), [f"{root}{os.sep}"]) is None


def test_a_sibling_directory_sharing_the_roots_prefix_is_refused(tmp_path: Path) -> None:
    """The `startswith` bug: `/output-evil/x.blend` starts with `/output`."""
    module = _file_paths()
    (tmp_path / "output").mkdir()
    (tmp_path / "output-evil").mkdir()

    with pytest.raises(ValueError, match="outside") as caught:
        module.enforce_roots(str(tmp_path / "output-evil" / "x.blend"), [str(tmp_path / "output")])
    _assert_no_absolute_path(str(caught.value), str(tmp_path))


def test_a_root_reached_through_a_symlink_still_contains_its_files(tmp_path: Path) -> None:
    """Both sides are canonicalized, or a symlinked mount rejects every file inside it."""
    real = tmp_path / "real_output"
    real.mkdir()
    (tmp_path / "output").symlink_to(real, target_is_directory=True)

    assert _file_paths().enforce_roots(str(real / "x.blend"), [str(tmp_path / "output")]) is None


def test_the_containment_refusal_names_the_policy_not_a_path(tmp_path: Path) -> None:
    """The client can read the roots from the handshake; the message points there instead of echoing paths."""
    module = _file_paths()
    root = tmp_path / "output"
    root.mkdir()

    with pytest.raises(ValueError, match="file_roots") as caught:
        module.enforce_roots(str(tmp_path / "elsewhere.blend"), [str(root)])
    _assert_no_absolute_path(str(caught.value), str(tmp_path), str(root))


def test_a_relative_path_outside_the_roots_is_told_where_it_resolved(tmp_path: Path, monkeypatch) -> None:
    """
    A shots-relative spelling resolves against Blender's working directory, which the refusal must say.

    Without it a caller retries every relative spelling it can think of and gets the same sentence each
    time; an absolute path to the same place outside the roots gets no such note, since it was meant.
    """
    module = _file_paths()
    root = tmp_path / "project"
    (root / "shots").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(module.PathOutsideRootsError, match="working directory") as relative:
        module.resolve_blend_path("shots/sh030.blend", roots=[str(root)], must_exist=False)
    _assert_no_absolute_path(str(relative.value), str(tmp_path))
    with pytest.raises(module.PathOutsideRootsError) as absolute:
        module.resolve_blend_path(str(tmp_path / "shots" / "sh030.blend"), roots=[str(root)], must_exist=False)
    assert "working directory" not in str(absolute.value)
    assert module.resolve_blend_path(str(root / "shots" / "sh030.blend"), roots=[str(root)], must_exist=False)


# ---------------------------------------------------------------------------
# sanitize_blender_error, against captured shapes
# ---------------------------------------------------------------------------


def test_sanitizer_removes_the_path_from_a_missing_file_error() -> None:
    """Shape 1: a double-quoted path with a cause after it."""
    sanitized = _file_paths().sanitize_blender_error(RuntimeError(SHAPE_1_MISSING))

    _assert_no_absolute_path(sanitized)
    assert sanitized == 'Error: Cannot read file "<path>": No such file or directory'


def test_sanitizer_keeps_the_cause_when_nothing_follows_the_path() -> None:
    """Shape 2: the cause is entirely in the tokens *before* the path."""
    sanitized = _file_paths().sanitize_blender_error(RuntimeError(SHAPE_2_DIRECTORY))

    _assert_no_absolute_path(sanitized)
    assert sanitized == 'Error: File format is not supported in file "<path>"'


def test_sanitizer_removes_the_process_cwd_from_the_empty_path_shape() -> None:
    """Shape 2, empty-path variant: the leaked path is the server's CWD, which no caller sent."""
    sanitized = _file_paths().sanitize_blender_error(RuntimeError(SHAPE_2_EMPTY_PATH))

    _assert_no_absolute_path(sanitized, _CWD)
    assert "File format is not supported" in sanitized


def test_sanitizer_removes_every_occurrence_of_the_path() -> None:
    """Shape 3 carries the path twice, `"..."` then `'...'`; a single replace ships the second."""
    sanitized = _file_paths().sanitize_blender_error(RuntimeError(SHAPE_3_TRUNCATED))

    assert sanitized.count(f"{_WORK}/truncated.blend") == 0
    _assert_no_absolute_path(sanitized)
    assert sanitized == "Error: Loading \"<path>\" failed: Failed to read blend file '<path>': Missing DNA block"


def test_sanitizer_removes_derived_temp_write_path() -> None:
    """Shape 4's `<abs>@` is unquoted and derived by Blender, so matching the caller's string misses it."""
    sanitized = _file_paths().sanitize_blender_error(RuntimeError(SHAPE_4_SAVE))

    _assert_no_absolute_path(sanitized, "x.blend@")
    assert sanitized == "Error: Cannot open file <path> for writing: No such file or directory"


def test_sanitizer_removes_the_path_from_a_library_reload_error() -> None:
    """Shape 5: a single-quoted path after a single-quoted library name that must survive."""
    sanitized = _file_paths().sanitize_blender_error(RuntimeError(SHAPE_5_RELOAD))

    _assert_no_absolute_path(sanitized)
    assert "from invalid path '<path>'" in sanitized


def test_sanitizer_does_not_present_the_id_code_as_part_of_the_library_name() -> None:
    """`LIgood.blend` is the raw ID name; the library is called `good.blend`."""
    sanitized = _file_paths().sanitize_blender_error(RuntimeError(SHAPE_5_RELOAD))

    assert sanitized == "Error: Trying to reload library 'good.blend' from invalid path '<path>'"


def test_sanitizer_reduces_a_library_name_containing_a_newline() -> None:
    """Without DOTALL the name match stops at the newline and `a` plus a relative tail goes out."""
    sanitized = _file_paths().sanitize_blender_error(RuntimeError(NEWLINE_LIBRARY_NAME_SHAPE_5))

    _assert_no_absolute_path(sanitized, "victim")
    assert sanitized == "Error: Trying to reload library 'b.blend' from invalid path '<path>'"


def test_sanitizer_reduces_a_library_name_without_touching_the_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    """A library name is chosen by a `.blend` author; stat-ing it probes the CWD, or a network share on Windows."""
    module = _file_paths()

    def refuse(_path: object) -> bool:
        raise AssertionError("the library name was stat-ed")

    monkeypatch.setattr(os.path, "isdir", refuse)

    assert module.sanitize_blender_error(RuntimeError(HOSTILE_LIBRARY_NAME_SHAPE_5)) == (
        "Error: Trying to reload library 'canon.blend' from invalid path '<path>'"
    )


def test_sanitizer_reduces_a_library_name_holding_an_absolute_path_to_its_leaf() -> None:
    """The `LI` code keeps the quoted name from reading as a path, so without the name rule it goes out whole."""
    sanitized = _file_paths().sanitize_blender_error(RuntimeError(HOSTILE_LIBRARY_NAME_SHAPE_5))

    _assert_no_absolute_path(sanitized, "victim", "shots")
    assert sanitized == "Error: Trying to reload library 'canon.blend' from invalid path '<path>'"


@pytest.mark.parametrize(
    "template",
    [
        "Cannot relocate indirectly linked library '{name}'",
        "Cannot delete indirectly linked library '{name}'",
        "Path 'location' not found, from linked data-block 'OBHero' (from library '{name}')",
    ],
    ids=["relocate-indirect", "delete-indirect", "from-library"],
)
def test_sanitizer_reduces_every_quoted_library_name_shape_to_its_leaf(template: str) -> None:
    """
    Other Blender messages that quote a library name reduce it to its leaf too, not only the reload one.

    These formats were read from Blender's binary rather than captured from errors.
    """
    raw = template.format(name="LI/Users/victim/shots/canon.blend")

    sanitized = _file_paths().sanitize_blender_error(RuntimeError(raw))

    _assert_no_absolute_path(sanitized, "victim", "shots")
    assert "library 'canon.blend'" in sanitized, sanitized


def test_sanitizer_removes_quoted_paths_containing_a_space_and_an_apostrophe() -> None:
    """A work dir named `fp shapes o'brien` puts a space and an apostrophe inside a `'...'` path."""
    module = _file_paths()

    for raw in (HOSTILE_SHAPE_3, HOSTILE_SHAPE_5):
        sanitized = module.sanitize_blender_error(RuntimeError(raw))
        _assert_no_absolute_path(sanitized, "brien")
        assert sanitized.endswith(("Missing DNA block", "'<path>'")), sanitized


def test_sanitizer_removes_an_unquoted_path_containing_a_space() -> None:
    """Shape 4 in the hostile work dir: an unquoted path with a space runs past the first word."""
    sanitized = _file_paths().sanitize_blender_error(RuntimeError(HOSTILE_SHAPE_4))

    _assert_no_absolute_path(sanitized, "brien", "x.blend@")
    assert sanitized == "Error: Cannot open file <path> for writing: No such file or directory"


def test_sanitizer_removes_windows_drive_and_unc_paths() -> None:
    """Constructed in Blender's shape-1 wording; there is no Windows capture."""
    module = _file_paths()

    for path in ("C:\\Users\\artist\\shot.blend", "D:/shows/shot.blend", "\\\\farm\\canon\\shot.blend"):
        sanitized = module.sanitize_blender_error(RuntimeError(f'Error: Cannot read file "{path}": No such file'))
        assert "artist" not in sanitized and "shows" not in sanitized and "farm" not in sanitized, sanitized
        assert sanitized == 'Error: Cannot read file "<path>": No such file'


def test_sanitizer_removes_home_relative_paths() -> None:
    """A `~` path discloses an account name just as an absolute one does."""
    module = _file_paths()

    texts = ("Error: Cannot read file '~artist/shot.blend'", "Error: Cannot open file ~/shots/x.blend@ for writing")
    for text in texts:
        sanitized = module.sanitize_blender_error(RuntimeError(text))
        assert "artist" not in sanitized and "shots" not in sanitized, sanitized
        assert "<path>" in sanitized


def test_sanitizer_leaves_text_without_a_path_alone() -> None:
    """Over-removal destroys the cause; `and/or` and a ratio are not paths."""
    text = "Error: context is incorrect and/or scale 1/2 is invalid"

    assert _file_paths().sanitize_blender_error(RuntimeError(text)) == text


def test_sanitizer_names_the_exception_type_when_it_carries_no_text() -> None:
    """An empty message would reach the client as an empty error, which reads as success to some callers."""
    assert _file_paths().sanitize_blender_error(RuntimeError()) == "RuntimeError"


# ---------------------------------------------------------------------------
# Cause text, known paths, closing punctuation, case-folding volumes
# ---------------------------------------------------------------------------


def test_sanitizer_keeps_an_errno_text_that_contains_a_slash() -> None:
    """`os.strerror(EIO)` is `Input/output error`; it is the cause, not a path."""
    module = _file_paths()

    for text, cause in (
        (
            "Error: Cannot open file /tmp/a b/x.blend@ for writing: Input/output error",
            "for writing: Input/output error",
        ),
        ("Error: Cannot open file /tmp/x.blend@ for writing: read/write denied", "for writing: read/write denied"),
        ("Error: Cannot read file '/a/b.blend': Is a directory / something", "': Is a directory / something"),
    ):
        sanitized = module.sanitize_blender_error(RuntimeError(text))
        assert cause in sanitized, sanitized
        assert "/tmp" not in sanitized and "/a/b" not in sanitized and "x.blend" not in sanitized, sanitized


def test_sanitizer_replaces_a_known_path_whole_even_with_a_space_in_its_leaf() -> None:
    """Structural detection cannot tell where `Hero Char v2.blend` ends; the caller's own path can."""
    text = "load: /canon/Hero Char v2.blend failed to open blend file"

    sanitized = _file_paths().sanitize_blender_error(RuntimeError(text), known_paths=["/canon/Hero Char v2.blend"])

    assert sanitized == "load: <path> failed to open blend file"


def test_sanitizer_replaces_the_derived_temp_name_of_a_known_path() -> None:
    """Blender's `<path>@` is derived from the known path, so it is known too."""
    text = "Error: Cannot open file /shots/Hero Char.blend@ for writing: Input/output error"

    sanitized = _file_paths().sanitize_blender_error(RuntimeError(text), known_paths=["/shots/Hero Char.blend"])

    assert sanitized == "Error: Cannot open file <path> for writing: Input/output error"


def test_sanitizer_keeps_punctuation_closing_a_quoted_path() -> None:
    """A quote followed by `?`, `)` or `>` still ends the path, and the punctuation is text."""
    module = _file_paths()

    for close in ("?", ")", ">"):
        sanitized = module.sanitize_blender_error(RuntimeError(f'Error: (was "/Users/j/x.blend"{close} failed'))
        assert sanitized == f'Error: (was "<path>"{close} failed', sanitized


def _volume_folds_case(directory: Path) -> bool:
    """
    Probe whether `directory`'s filesystem treats a case-flipped name as the same entry.

    Args:
        directory: An existing, writable directory.

    Returns:
        bool: True on a case-insensitive volume such as default APFS.

    """
    probe = directory / "CaseProbe"
    probe.mkdir()
    flipped = directory / "caseprobe"
    return flipped.exists() and os.path.samestat(os.stat(probe), os.stat(flipped))


def test_a_root_spelled_in_another_case_still_contains_its_files(tmp_path: Path) -> None:
    """On APFS `/X/output/x.blend` is inside root `/X/Output`; comparing spellings refuses it."""
    if not _volume_folds_case(tmp_path):
        pytest.skip("case-sensitive filesystem: the two spellings name different directories")
    (tmp_path / "Output").mkdir()

    assert _file_paths().enforce_roots(str(tmp_path / "output" / "x.blend"), [str(tmp_path / "Output")]) is None


def test_sanitizer_leaves_punctuation_after_a_bare_path() -> None:
    """A colon or comma closing a bare path belongs to the sentence."""
    sanitized = _file_paths().sanitize_blender_error(RuntimeError("Error: at /tmp/x.blend, then /y/z.blend: gone"))

    assert sanitized == "Error: at <path>, then <path>: gone"
