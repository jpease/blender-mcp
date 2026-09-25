"""
Rows guarding paths inside error text.

The cause text, known paths, case-folding volumes, and Poly Haven's errors.

Label prefixes: `file paths:`, `polyhaven:`.
"""

from .common import ADDON_FILE_PATHS, ADDON_POLYHAVEN, FPT, PHT, Revert

ROWS: list[Revert] = [
    # --- cause text, known paths, case-folding volumes, Poly Haven siblings ---
    Revert(
        "file paths: a bare path extends across a word ending in ':' into the cause",
        ADDON_FILE_PATHS,
        r"(?:(?:\s+\S*[^\s:;,])*?",
        r"(?:(?:\s+\S+)*?",
        (f"{FPT}::test_sanitizer_keeps_an_errno_text_that_contains_a_slash",),
    ),
    Revert(
        "file paths: a lone '/' is taken for a path",
        ADDON_FILE_PATHS,
        "{_PATH_START}(?=\\S)",
        "{_PATH_START}",
        (f"{FPT}::test_sanitizer_keeps_an_errno_text_that_contains_a_slash",),
    ),
    Revert(
        "file paths: a bare path swallows the punctuation that closes it",
        ADDON_FILE_PATHS,
        r"(?=\S)\S*?{_TRAILING}",
        r"(?=\S)\S*",
        (f"{FPT}::test_sanitizer_leaves_punctuation_after_a_bare_path",),
    ),
    # The library-name pattern closes on the same punctuation set, so the anchor carries the
    # back-reference that only the quoted-path alternative has.
    Revert(
        "file paths: a quoted path closed by '?', ')' or '>' is not recognised as quoted",
        ADDON_FILE_PATHS,
        r"(?P=quote)(?=$|[\s:;,.?!)\]>])",
        r"(?P=quote)(?=$|[\s:;,.)\]])",
        (f"{FPT}::test_sanitizer_keeps_punctuation_closing_a_quoted_path",),
    ),
    Revert(
        "file paths: known paths are ignored",
        ADDON_FILE_PATHS,
        "        text = text.replace(known, PATH_PLACEHOLDER)",
        "        pass",
        (
            f"{FPT}::test_sanitizer_replaces_a_known_path_whole_even_with_a_space_in_its_leaf",
            f"{FPT}::test_sanitizer_replaces_the_derived_temp_name_of_a_known_path",
        ),
    ),
    Revert(
        "file paths: a known path's derived '@' temp name is not known",
        ADDON_FILE_PATHS,
        '    return usable | {f"{path}@" for path in usable}',
        "    return usable",
        (f"{FPT}::test_sanitizer_replaces_the_derived_temp_name_of_a_known_path",),
    ),
    Revert(
        "file paths: known paths replaced shortest-first, leaving '<path>@'",
        ADDON_FILE_PATHS,
        "key=len, reverse=True)",
        "key=len)",
        (f"{FPT}::test_sanitizer_replaces_the_derived_temp_name_of_a_known_path",),
    ),
    # The ancestor walk is one pass over the roots now, after every root has been tried by
    # spelling, so the row deletes that pass rather than a root's turn in the first loop.
    Revert(
        "file paths: containment compares spellings only, refusing a case variant on APFS",
        ADDON_FILE_PATHS,
        "    if any(_has_ancestor_directory(candidate, canonical_root) for canonical_root in canonical_roots):\n"
        "        return\n",
        "",
        (f"{FPT}::test_a_root_spelled_in_another_case_still_contains_its_files",),
    ),
    Revert(
        "polyhaven: the HDRI setup error goes out raw",
        ADDON_POLYHAVEN,
        "Failed to set up HDRI in Blender: {sanitize_blender_error(e)}",
        "Failed to set up HDRI in Blender: {e!s}",
        (f"{PHT}::test_a_failed_hdri_setup_reports_no_absolute_path",),
    ),
    Revert(
        "polyhaven: the texture processing error goes out raw",
        ADDON_POLYHAVEN,
        "Failed to process textures: {sanitize_blender_error(e)}",
        "Failed to process textures: {e!s}",
        (f"{PHT}::test_a_failed_texture_load_reports_no_absolute_path",),
    ),
    Revert(
        "polyhaven: the asset import's outer error goes out raw",
        ADDON_POLYHAVEN,
        "Failed to download asset: {sanitize_blender_error(e)}",
        "Failed to download asset: {e!s}",
        (f"{PHT}::test_a_failure_before_any_download_reports_no_absolute_path[import_polyhaven_asset-arguments0]",),
    ),
    Revert(
        "polyhaven: the categories error goes out raw",
        ADDON_POLYHAVEN,
        '            return {"error": sanitize_blender_error(e)}\n\n    def list_polyhaven_assets',
        '            return {"error": str(e)}\n\n    def list_polyhaven_assets',
        (f"{PHT}::test_a_failure_before_any_download_reports_no_absolute_path[get_polyhaven_categories-arguments1]",),
    ),
    Revert(
        "polyhaven: the asset listing error goes out raw",
        ADDON_POLYHAVEN,
        ('            return {"error": sanitize_blender_error(e)}\n\n    def _configured_environment'),
        ('            return {"error": str(e)}\n\n    def _configured_environment'),
        (f"{PHT}::test_a_failure_before_any_download_reports_no_absolute_path[list_polyhaven_assets-arguments2]",),
    ),
]
