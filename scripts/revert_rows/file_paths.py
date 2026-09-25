"""
Rows guarding the filesystem trust boundary.

Label prefixes: `file paths:`, `file paths control:`, `create_directories:`, `polyhaven:`,
`file roots:`, `handshake:`.
"""

from .common import (
    ADDON_FILE_PATHS,
    ADDON_MANAGER,
    ADDON_OUTPUT_ROOTS,
    ADDON_POLYHAVEN,
    ADDON_SERVER_CORE,
    AMT,
    FLT,
    FPT,
    PHT,
    ROOTST,
    Revert,
)

# The two table tests over `file_paths`' pure verdicts: their parameter ids are readable,
# which makes the full node ids too long to inline.
_OPEN_VERDICT = f"{FPT}::test_the_open_verdict_is_decided_from_facts_alone"
_SAVE_VERDICT = f"{FPT}::test_the_save_verdict_holds_when_the_caller_opted_into_creating_directories"

# Appended by the two canonicalization rows. `_has_ancestor_directory`'s device/inode
# check also accepts the symlinked-root and trailing-separator cases, so reverting
# canonicalization alone would leave those nodes passing; this disables it too.
NO_SAME_DIRECTORY_FALLBACK = """

def _has_ancestor_directory(candidate, root):
    return False
"""

ROWS: list[Revert] = [
    # --- the filesystem trust boundary ---
    Revert(
        "file paths: file_paths imports bpy",
        ADDON_FILE_PATHS,
        None,
        "\nimport bpy\n",
        (f"{FPT}::test_file_paths_imports_no_bpy",),
    ),
    Revert(
        "file paths: a non-string path reaches the string handling",
        ADDON_FILE_PATHS,
        '    if not isinstance(raw, str):\n        raise ValueError("path must be a string")\n',
        "",
        tuple(
            f"{FPT}::test_a_non_string_path_is_refused[{case}]"
            for case in ("None", "7", "b'shot.blend'", "['shot.blend']", "PosixPath('shot.blend')")
        ),
    ),
    Revert(
        "file paths: an empty or blank path is not refused (Blender opens the process CWD)",
        ADDON_FILE_PATHS,
        '    if not raw.strip():\n        raise ValueError("path must not be empty")\n',
        "",
        (f"{FPT}::test_an_empty_path_is_refused", f"{FPT}::test_a_whitespace_only_path_is_refused"),
    ),
    Revert(
        "file paths: a NUL byte is not refused",
        ADDON_FILE_PATHS,
        '    if "\\x00" in raw:\n        raise ValueError("path must not contain a NUL byte")\n',
        "",
        (f"{FPT}::test_a_nul_byte_is_refused",),
    ),
    Revert(
        "file paths: an unexpanded Blender-relative prefix is resolved as a POSIX path",
        ADDON_FILE_PATHS,
        "    if raw.startswith(BLENDER_RELATIVE_PREFIX):",
        "    if False:",
        (f"{FPT}::test_an_unexpanded_blender_relative_prefix_is_refused",),
    ),
    Revert(
        "file paths: the .blend suffix is not checked",
        ADDON_FILE_PATHS,
        "    if not (_has_blend_suffix(raw) and _has_blend_suffix(resolved)):",
        "    if False:",
        (
            f"{FPT}::test_a_non_blend_extension_is_refused",
            f"{FPT}::test_a_trailing_dot_after_the_blend_suffix_is_refused",
            f"{FPT}::test_a_trailing_space_after_the_blend_suffix_is_refused",
        ),
    ),
    Revert(
        "file paths: trailing dots and spaces are stripped before the suffix is compared",
        ADDON_FILE_PATHS,
        "    leaf = os.path.basename(path)\n",
        '    leaf = os.path.basename(path).rstrip(". ")\n',
        (
            f"{FPT}::test_a_trailing_dot_after_the_blend_suffix_is_refused",
            f"{FPT}::test_a_trailing_space_after_the_blend_suffix_is_refused",
        ),
    ),
    Revert(
        "file paths: the .blend suffix is compared case-sensitively",
        ADDON_FILE_PATHS,
        "    return leaf.lower().endswith(BLEND_SUFFIX)",
        "    return leaf.endswith(BLEND_SUFFIX)",
        (f"{FPT}::test_the_blend_suffix_is_matched_case_insensitively",),
    ),
    Revert(
        "file paths: realpath reverted to abspath, symlinks compared by name (plus the same-directory fallback)",
        ADDON_FILE_PATHS,
        "    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))",
        "    return os.path.abspath(os.path.expanduser(path))",
        (
            f"{FPT}::test_a_symlink_inside_a_root_pointing_outside_it_is_refused",
            f"{FPT}::test_a_symlinked_parent_directory_is_refused",
            f"{FPT}::test_a_root_reached_through_a_symlink_still_contains_its_files",
            f"{FPT}::test_blenders_relative_form_resolves_inside_the_blend_directory",
            f"{PHT}::test_a_downloaded_blend_resolving_outside_its_download_directory_is_never_loaded",
        ),
        also=NO_SAME_DIRECTORY_FALLBACK,
    ),
    Revert(
        "file paths: no abspath or realpath, relative and `..` paths compared as typed (plus the same-dir fallback)",
        ADDON_FILE_PATHS,
        "    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))",
        "    return os.path.expanduser(path)",
        (
            f"{FPT}::test_a_bare_relative_path_comes_out_absolute",
            f"{FPT}::test_dotdot_traversal_is_normalised_before_the_containment_check",
            f"{FPT}::test_a_relative_form_climbing_out_of_the_blend_directory_is_normalised",
            f"{FPT}::test_a_path_inside_a_root_is_accepted_even_when_the_root_has_a_trailing_separator",
        ),
        also=NO_SAME_DIRECTORY_FALLBACK,
    ),
    Revert(
        "file paths: ~ is not expanded, so it names a directory under the process CWD",
        ADDON_FILE_PATHS,
        "    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))",
        "    return os.path.realpath(os.path.abspath(path))",
        (f"{FPT}::test_tilde_expands_to_the_home_directory",),
    ),
    # The prefix test now lives in `contains`, the pure predicate `enforce_roots` calls, so
    # the row reverts the predicate and the predicate's own test notices alongside the walk's.
    Revert(
        "file paths: containment by string prefix (the /output-evil bug)",
        ADDON_FILE_PATHS,
        "        return os.path.commonpath((canonical_root, canonical_candidate)) == canonical_root",
        "        return canonical_candidate.startswith(canonical_root)",
        (
            f"{FPT}::test_a_sibling_directory_sharing_the_roots_prefix_is_refused",
            f"{FPT}::test_a_sibling_sharing_the_roots_spelling_is_not_contained",
        ),
    ),
    Revert(
        "file paths: a root no longer contains itself, so a save into the root directory is refused",
        ADDON_FILE_PATHS,
        "        return os.path.commonpath((canonical_root, canonical_candidate)) == canonical_root",
        "        return (\n"
        "            os.path.commonpath((canonical_root, canonical_candidate)) == canonical_root\n"
        "            and canonical_candidate != canonical_root\n"
        "        )",
        (f"{FPT}::test_a_root_contains_itself_and_what_lies_under_it",),
    ),
    # `commonpath` raises on two drive letters only under `ntpath`; on posix that pair is
    # two relative spellings sharing no component, and it returns "". So the row swaps the
    # module as well as the refusal: with the raise treated as containment, the drive case
    # and the absolute/relative case both come back contained. `also` adds the import
    # because a row has one anchor and cannot add one.
    Revert(
        "file paths: a comparison that cannot be made counts as contained, so the refusal fails open",
        ADDON_FILE_PATHS,
        "        return os.path.commonpath((canonical_root, canonical_candidate)) == canonical_root\n"
        "    except ValueError:\n"
        "        return False  # different drives on Windows: not contained by spelling",
        "        return ntpath.commonpath((canonical_root, canonical_candidate)) == canonical_root\n"
        "    except ValueError:\n"
        "        return True  # a comparison that cannot be made is read as containment",
        (
            f"{FPT}::test_a_root_on_another_windows_drive_contains_nothing",
            f"{FPT}::test_paths_that_cannot_be_compared_are_reported_as_not_contained",
        ),
        also="\nimport ntpath\n",
    ),
    Revert(
        "file paths: no configured roots refuses everything instead of enforcing nothing",
        ADDON_FILE_PATHS,
        (
            "    canonical_roots = [canonical_path(root) for root in roots]\n"
            "    if not canonical_roots:\n"
            "        return  # the permissive default costs no syscall\n"
            "    candidate = canonical_path(path)\n"
            "    if inside_roots(candidate, canonical_roots):\n"
            "        return\n"
        ),
        (
            "    canonical_roots = [canonical_path(root) for root in roots]\n"
            "    candidate = canonical_path(path)\n"
            "    if canonical_roots and inside_roots(candidate, canonical_roots):\n"
            "        return\n"
        ),
        (f"{FPT}::test_no_configured_roots_enforces_nothing",),
    ),
    Revert(
        "file paths: unset roots authorize nothing instead of everything, at the verdict itself",
        ADDON_FILE_PATHS,
        "    return not canonical_roots or any(contains(root, canonical_candidate) for root in canonical_roots)",
        "    return any(contains(root, canonical_candidate) for root in canonical_roots)",
        (f"{FPT}::test_a_path_is_authorized_by_any_one_root_and_by_no_roots_at_all",),
    ),
    Revert(
        "file paths: the containment refusal echoes the resolved path",
        ADDON_FILE_PATHS,
        "    raise PathOutsideRootsError(ROOTS_REFUSAL)",
        '    raise PathOutsideRootsError(f"path {candidate} is outside the allowed file roots")',
        (
            f"{FPT}::test_the_containment_refusal_names_the_policy_not_a_path",
            f"{FPT}::test_a_sibling_directory_sharing_the_roots_prefix_is_refused",
            f"{FPT}::test_dotdot_traversal_is_normalised_before_the_containment_check",
            f"{FPT}::test_a_symlink_inside_a_root_pointing_outside_it_is_refused",
        ),
    ),
    Revert(
        "file paths: the open verdict decides nothing, so a directory, a missing file and a zip all pass",
        ADDON_FILE_PATHS,
        (
            "    if is_directory:\n"
            '        return "path is a directory, not a .blend file"\n'
            "    if not exists:\n"
            "        if not directory_exists:\n"
            '            return "file does not exist, and neither does the directory named in its path"\n'
            '        return "file does not exist, though the directory named in its path does"\n'
            "    if not readable:\n"
            '        return "file could not be read"\n'
            "    if not is_blend_header(header):\n"
            '        return "file is not a .blend file (unrecognised header)"\n'
            "    return None\n"
        ),
        "    return None\n",
        (
            f"{_OPEN_VERDICT}[a directory named x.blend is not a file]",
            f"{_OPEN_VERDICT}[a mistyped filename]",
            f"{_OPEN_VERDICT}[a mistyped directory - the same sentence until this split them]",
            f"{_OPEN_VERDICT}[unreadable is refused, not treated as a bad header]",
            f"{_OPEN_VERDICT}[a zip renamed .blend]",
        ),
    ),
    Revert(
        "file paths control: the header test refuses every file, a real .blend included",
        ADDON_FILE_PATHS,
        "    if not is_blend_header(header):\n",
        "    if True:\n",
        (f"{_OPEN_VERDICT}[a real .blend]",),
    ),
    Revert(
        "file paths: a directory is read as a missing file",
        ADDON_FILE_PATHS,
        ('    if is_directory:\n        return "path is a directory, not a .blend file"\n    if not exists:\n'),
        "    if not exists:\n",
        (f"{FPT}::test_a_directory_where_a_file_is_expected_is_refused",),
    ),
    Revert(
        "file paths: a directory is accepted as a save target",
        ADDON_FILE_PATHS,
        (
            "    if is_directory:\n"
            '        return "path is a directory, not a .blend file"\n'
            "    if not directory_exists:\n"
        ),
        "    if not directory_exists:\n",
        (
            f"{FPT}::test_a_directory_where_a_save_target_is_expected_is_refused",
            f"{_SAVE_VERDICT}[saving over a directory]",
        ),
    ),
    Revert(
        "file paths: a missing file is not refused before it is opened",
        ADDON_FILE_PATHS,
        (
            "    if not exists:\n"
            "        if not directory_exists:\n"
            '            return "file does not exist, and neither does the directory named in its path"\n'
            '        return "file does not exist, though the directory named in its path does"\n'
        ),
        "",
        (f"{FPT}::test_a_missing_file_is_refused",),
    ),
    Revert(
        "file paths: a relative path outside the roots is not told it resolved against the working directory",
        ADDON_FILE_PATHS,
        '        raise PathOutsideRootsError(f"{refusal}{_RELATIVE_ROOTS_REFUSAL}") from None\n',
        "        raise\n",
        (f"{FPT}::test_a_relative_path_outside_the_roots_is_told_where_it_resolved",),
    ),
    Revert(
        "file paths: an absolute path outside the roots is told it resolved against the working directory",
        ADDON_FILE_PATHS,
        "        if os.path.isabs(os.path.expanduser(raw)):\n            raise\n",
        "",
        (f"{FPT}::test_a_relative_path_outside_the_roots_is_told_where_it_resolved",),
    ),
    Revert(
        "file paths: the magic-byte check is skipped",
        ADDON_FILE_PATHS,
        "    if not is_blend_header(header):",
        "    if False:",
        (
            f"{FPT}::test_a_file_whose_magic_bytes_are_not_a_blend_is_refused",
            f"{PHT}::test_a_downloaded_blend_whose_header_is_not_a_blend_is_never_loaded",
        ),
    ),
    Revert(
        "file paths: an unreadable file's OSError text (and its path) reaches the refusal",
        ADDON_FILE_PATHS,
        ("        except OSError as exc:\n            cause = exc\n"),
        ('        except OSError as exc:\n            raise ValueError(f"file could not be read: {exc}") from exc\n'),
        (f"{FPT}::test_an_unreadable_file_is_refused_without_naming_it",),
    ),
    Revert(
        "file paths: a save target's missing directory is not refused",
        ADDON_FILE_PATHS,
        '        return "target directory does not exist; pass create_directories=true to create it"\n',
        "        return None\n",
        (
            f"{FPT}::test_a_save_target_whose_directory_does_not_exist_is_refused",
            f"{FLT}::test_save_shot_refuses_a_missing_directory_unless_asked_to_create_it",
            f"{FPT}::test_a_missing_save_directory_is_refused_without_the_opt_in",
        ),
    ),
    Revert(
        "create_directories: the opt-in is ignored, so a missing directory is refused anyway",
        ADDON_FILE_PATHS,
        ("    if not directory_exists:\n        if create_directories:\n            return None\n"),
        ("    if not directory_exists:\n        if False:\n            return None\n"),
        (f"{_SAVE_VERDICT}[a missing directory the caller opted into creating]",),
    ),
    Revert(
        "file paths: a save target's read-only directory is not refused",
        ADDON_FILE_PATHS,
        ('    if not directory_writable:\n        return "target directory is not writable"\n'),
        "",
        (
            f"{FPT}::test_a_save_target_in_a_read_only_directory_is_refused",
            f"{_SAVE_VERDICT}[create_directories does not excuse an unwritable existing directory]",
        ),
    ),
    Revert(
        "file paths: the magic check accepts only b'BLENDER', rejecting every compressed .blend",
        ADDON_FILE_PATHS,
        "BLEND_MAGIC_PREFIXES = (BLEND_MAGIC_UNCOMPRESSED, BLEND_MAGIC_ZSTD, BLEND_MAGIC_GZIP)",
        "BLEND_MAGIC_PREFIXES = (BLEND_MAGIC_UNCOMPRESSED,)",
        (
            f"{FPT}::test_a_zstd_compressed_blend_is_accepted",
            f"{FPT}::test_a_gzip_blend_written_without_an_fname_is_accepted",
            f"{PHT}::test_a_valid_downloaded_blend_is_still_imported",
        ),
    ),
    Revert(
        "file paths: the superseded 4-byte gzip constant, pinning the FNAME flag",
        ADDON_FILE_PATHS,
        r'BLEND_MAGIC_GZIP = b"\x1f\x8b"',
        r'BLEND_MAGIC_GZIP = b"\x1f\x8b\x08\x08"',
        (f"{FPT}::test_a_gzip_blend_written_without_an_fname_is_accepted",),
    ),
    Revert(
        "file paths: the superseded 12-byte BLENDER17-01 constant, pinning 5.x's header",
        ADDON_FILE_PATHS,
        'BLEND_MAGIC_UNCOMPRESSED = b"BLENDER"',
        'BLEND_MAGIC_UNCOMPRESSED = b"BLENDER17-01"',
        (f"{FPT}::test_a_pre_5x_blend_header_is_accepted",),
    ),
    # The `max()` moved out of the `read()` call into `BLEND_HEADER_BYTES`; the read is
    # still what this row shortens.
    Revert(
        "file paths: the header read is shorter than the longest prefix",
        ADDON_FILE_PATHS,
        "BLEND_HEADER_BYTES = max(len(prefix) for prefix in BLEND_MAGIC_PREFIXES)",
        "BLEND_HEADER_BYTES = len(BLEND_MAGIC_GZIP)",
        (
            f"{FPT}::test_an_uncompressed_blend_is_accepted",
            f"{FPT}::test_a_zstd_compressed_blend_is_accepted",
            f"{FPT}::test_a_pre_5x_blend_header_is_accepted",
        ),
    ),
    # The predicate the file check is built on has its own tests, which reach it with
    # bytes rather than a file: the rows above revert the constants it reads, these four
    # revert the comparison itself.
    Revert(
        "file paths: the header must equal a magic exactly, so the version digits every real header carries reject it",
        ADDON_FILE_PATHS,
        "    return header.startswith(BLEND_MAGIC_PREFIXES)",
        "    return header in BLEND_MAGIC_PREFIXES",
        tuple(
            f"{FPT}::test_every_header_form_blender_writes_is_recognised[{case}]"
            for case in ("5x-uncompressed", "pre-5x-uncompressed", "zstd", "gzip-with-an-fname")
        ),
    ),
    Revert(
        "file paths: the header comparison runs the other way round, so a short read matches every magic",
        ADDON_FILE_PATHS,
        "    return header.startswith(BLEND_MAGIC_PREFIXES)",
        "    return any(prefix.startswith(header) for prefix in BLEND_MAGIC_PREFIXES)",
        tuple(
            f"{FPT}::test_anything_that_is_not_a_header_is_rejected[{case}]"
            for case in ("empty-file", "truncated-to-inside-the-magic")
        ),
    ),
    Revert(
        "file paths: the magic is matched case-insensitively, so a lowercased near-miss is read as a .blend",
        ADDON_FILE_PATHS,
        "    return header.startswith(BLEND_MAGIC_PREFIXES)",
        "    return header.upper().startswith(BLEND_MAGIC_PREFIXES)",
        (f"{FPT}::test_anything_that_is_not_a_header_is_rejected[lowercased-near-miss]",),
    ),
    Revert(
        "file paths: the zstd magic truncated to three bytes, so a frame one byte off it is accepted",
        ADDON_FILE_PATHS,
        r'BLEND_MAGIC_ZSTD = b"\x28\xb5\x2f\xfd"',
        r'BLEND_MAGIC_ZSTD = b"\x28\xb5\x2f"',
        (f"{FPT}::test_anything_that_is_not_a_header_is_rejected[one-byte-off-zstd]",),
    ),
    Revert(
        "file paths: the sanitizer is bypassed and Blender's text goes out raw",
        ADDON_FILE_PATHS,
        "    text = _PATH_IN_TEXT.sub(_placeholder_for, text)",
        "    text = raw",
        (
            f"{FPT}::test_sanitizer_removes_the_path_from_a_missing_file_error",
            f"{FPT}::test_sanitizer_keeps_the_cause_when_nothing_follows_the_path",
            f"{FPT}::test_sanitizer_removes_the_process_cwd_from_the_empty_path_shape",
            f"{FPT}::test_sanitizer_removes_every_occurrence_of_the_path",
            f"{FPT}::test_sanitizer_removes_derived_temp_write_path",
            f"{FPT}::test_sanitizer_removes_the_path_from_a_library_reload_error",
            f"{FPT}::test_sanitizer_does_not_present_the_id_code_as_part_of_the_library_name",
            f"{FPT}::test_sanitizer_removes_quoted_paths_containing_a_space_and_an_apostrophe",
            f"{FPT}::test_sanitizer_removes_an_unquoted_path_containing_a_space",
            f"{FPT}::test_sanitizer_removes_windows_drive_and_unc_paths",
            f"{FPT}::test_sanitizer_removes_home_relative_paths",
            f"{PHT}::test_a_failed_blend_load_reports_no_absolute_path",
        ),
    ),
    Revert(
        "file paths: the sanitizer replaces only the first path (shape 3 ships its second copy)",
        ADDON_FILE_PATHS,
        "    text = _PATH_IN_TEXT.sub(_placeholder_for, text)",
        "    text = _PATH_IN_TEXT.sub(_placeholder_for, text, count=1)",
        (
            f"{FPT}::test_sanitizer_removes_every_occurrence_of_the_path",
            f"{FPT}::test_sanitizer_removes_quoted_paths_containing_a_space_and_an_apostrophe",
        ),
    ),
    Revert(
        "file paths: only quoted paths are detected (shape 4's bare derived `<abs>@` survives)",
        ADDON_FILE_PATHS,
        "(?P<bare>{_PATH_START}",
        "(?P<bare>(?!){_PATH_START}",
        (
            f"{FPT}::test_sanitizer_removes_derived_temp_write_path",
            f"{FPT}::test_sanitizer_removes_an_unquoted_path_containing_a_space",
            f"{FPT}::test_sanitizer_removes_home_relative_paths",
        ),
    ),
    Revert(
        "file paths: a bare path stops at its first space",
        ADDON_FILE_PATHS,
        r"(?:(?:\s+\S*[^\s:;,])*?\s+\S*[/\\]\S*?{_TRAILING})*)",
        r")",
        (f"{FPT}::test_sanitizer_removes_an_unquoted_path_containing_a_space",),
    ),
    Revert(
        "file paths: a quoted path ends at the first matching quote, even an apostrophe inside it",
        ADDON_FILE_PATHS,
        r"(?P=quote)(?=$|[\s:;,.?!)\]>])",
        "(?P=quote)",
        (f"{FPT}::test_sanitizer_removes_quoted_paths_containing_a_space_and_an_apostrophe",),
    ),
    Revert(
        "file paths: only POSIX-rooted paths are detected",
        ADDON_FILE_PATHS,
        r'_PATH_START = r"(?:/|\\\\|~[\w.-]*[/\\]|[A-Za-z]:[\\/])"',
        '_PATH_START = r"(?:/)"',
        (
            f"{FPT}::test_sanitizer_removes_windows_drive_and_unc_paths",
            f"{FPT}::test_sanitizer_removes_home_relative_paths",
        ),
    ),
    Revert(
        "file paths: a bare path may start mid-word, so `and/or` is cut",
        ADDON_FILE_PATHS,
        r"""(?<![^\s"'(\[=,])""",
        "",
        (f"{FPT}::test_sanitizer_leaves_text_without_a_path_alone",),
    ),
    Revert(
        "file paths: the reload shape's LI type code is presented as part of the library name",
        ADDON_FILE_PATHS,
        '    return _LIBRARY_ID_NAME.sub(r"\\1", text)',
        "    return text",
        (f"{FPT}::test_sanitizer_does_not_present_the_id_code_as_part_of_the_library_name",),
    ),
    Revert(
        "file paths: an exception with no text sanitizes to an empty error",
        ADDON_FILE_PATHS,
        "    if not raw:\n        return type(exc).__name__\n",
        "",
        (f"{FPT}::test_sanitizer_names_the_exception_type_when_it_carries_no_text",),
    ),
    Revert(
        "polyhaven: Poly Haven loads the download without checking it is a .blend",
        ADDON_POLYHAVEN,
        "        return resolve_blend_path(path, roots=[download_dir], must_exist=True)",
        "        return path",
        (f"{PHT}::test_a_downloaded_blend_whose_header_is_not_a_blend_is_never_loaded",),
    ),
    Revert(
        "polyhaven: Poly Haven does not contain the download to its own directory",
        ADDON_POLYHAVEN,
        "        return resolve_blend_path(path, roots=[download_dir], must_exist=True)",
        "        return resolve_blend_path(path, roots=[], must_exist=True)",
        (f"{PHT}::test_a_downloaded_blend_resolving_outside_its_download_directory_is_never_loaded",),
    ),
    Revert(
        "polyhaven: Poly Haven's download is held to the deployment's file roots, breaking the import",
        ADDON_POLYHAVEN,
        "        return resolve_blend_path(path, roots=[download_dir], must_exist=True)",
        "        return resolve_blend_path(path, roots=configured_file_roots(), must_exist=True)",
        (f"{PHT}::test_a_valid_downloaded_blend_is_still_imported",),
        ("\n\nfrom ..output_roots import configured_file_roots\n"),
    ),
    Revert(
        "polyhaven: Poly Haven's import error reaches the client unsanitized",
        ADDON_POLYHAVEN,
        '{"error": f"Failed to import model: {sanitize_blender_error(e)}"}',
        '{"error": f"Failed to import model: {e!s}"}',
        (f"{PHT}::test_a_failed_blend_load_reports_no_absolute_path",),
    ),
    # All four re-anchor onto the single line `configured_file_roots` is now: one
    # `split_roots` call per variable, and the `or` between them is the fallback.
    Revert(
        "file roots: file roots ignore their own variable when the output roots are set",
        ADDON_OUTPUT_ROOTS,
        "    return split_roots(source.get(FILE_ROOTS_ENV_VAR)) or split_roots(source.get(OUTPUT_ROOTS_ENV_VAR))",
        "    return split_roots(source.get(OUTPUT_ROOTS_ENV_VAR)) or split_roots(source.get(FILE_ROOTS_ENV_VAR))",
        (f"{ROOTST}::test_configured_file_roots_read_their_own_variable_first",),
    ),
    Revert(
        "file roots: file roots do not fall back to the output roots",
        ADDON_OUTPUT_ROOTS,
        "    return split_roots(source.get(FILE_ROOTS_ENV_VAR)) or split_roots(source.get(OUTPUT_ROOTS_ENV_VAR))",
        "    return split_roots(source.get(FILE_ROOTS_ENV_VAR))",
        (
            f"{ROOTST}::test_configured_file_roots_fall_back_to_the_output_roots",
            f"{ROOTST}::test_a_blank_file_roots_variable_counts_as_unset",
        ),
    ),
    Revert(
        "file roots: a blank file-roots variable counts as set, so the deployment silently goes permissive",
        ADDON_OUTPUT_ROOTS,
        "    return split_roots(source.get(FILE_ROOTS_ENV_VAR)) or split_roots(source.get(OUTPUT_ROOTS_ENV_VAR))",
        "    return split_roots(source.get(FILE_ROOTS_ENV_VAR)) if FILE_ROOTS_ENV_VAR in source "
        "else split_roots(source.get(OUTPUT_ROOTS_ENV_VAR))",
        (f"{ROOTST}::test_a_blank_file_roots_variable_counts_as_unset",),
    ),
    Revert(
        "file roots: the enforced roots borrow the advisory home-directory default",
        ADDON_OUTPUT_ROOTS,
        "    return split_roots(source.get(FILE_ROOTS_ENV_VAR)) or split_roots(source.get(OUTPUT_ROOTS_ENV_VAR))",
        "    return split_roots(source.get(FILE_ROOTS_ENV_VAR)) or split_roots(source.get(OUTPUT_ROOTS_ENV_VAR))"
        ' or [os.path.expanduser("~")]',
        (f"{ROOTST}::test_configured_file_roots_never_include_the_advisory_defaults",),
    ),
    Revert(
        "file roots: the handshake publishes the configured roots un-canonicalized",
        ADDON_SERVER_CORE,
        "    return tuple(dict.fromkeys(canonical_path(root) for root in roots))",
        "    return tuple(dict.fromkeys(roots))",
        (f"{ROOTST}::test_get_addon_info_publishes_enforced_file_roots_in_canonical_form",),
    ),
    Revert(
        "file roots: the handshake publishes the advisory writable roots as the enforced ones",
        ADDON_SERVER_CORE,
        "        roots = list(_canonical_file_roots(tuple(configured_file_roots())))",
        "        roots = BlenderMCPServer._writable_output_roots()",
        (f"{ROOTST}::test_get_addon_info_publishes_a_permissive_policy_when_no_roots_are_configured",),
    ),
    Revert(
        "file roots: the handshake never reports the policy as enforced",
        ADDON_SERVER_CORE,
        '"file_roots_enforced": bool(roots)',
        '"file_roots_enforced": False',
        (f"{ROOTST}::test_get_addon_info_publishes_enforced_file_roots_in_canonical_form",),
    ),
    Revert(
        "handshake: the server drops the addon's file roots",
        ADDON_MANAGER,
        '            file_roots=normalized_session_text_list(info.get("file_roots")),',
        "            file_roots=[],",
        (f"{AMT}::test_handshake_surfaces_the_file_path_policy",),
    ),
    Revert(
        "handshake: the file roots cross the server boundary unnormalized",
        ADDON_MANAGER,
        '            file_roots=normalized_session_text_list(info.get("file_roots")),',
        '            file_roots=list(info.get("file_roots") or []),',
        (
            f"{AMT}::test_every_handshake_field_refuses_the_same_hostile_string[file_roots]",
            f"{AMT}::test_a_hostile_element_inside_a_list_field_is_dropped_not_published[file_roots]",
        ),
    ),
    Revert(
        "handshake: file_roots_enforced carries the payload itself rather than a verdict about it",
        ADDON_MANAGER,
        '            file_roots_enforced=info.get("file_roots_enforced") is True,',
        '            file_roots_enforced=info.get("file_roots_enforced"),',
        (
            f"{AMT}::test_every_handshake_field_refuses_the_same_hostile_string[file_roots_enforced]",
            f"{AMT}::test_handshake_reads_an_addon_that_omits_the_file_path_policy_as_permissive",
        ),
    ),
]
