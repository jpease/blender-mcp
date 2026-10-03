"""
Rows guarding paths inside error text, and provider network I/O off Blender's main thread.

The cause text, known paths, case-folding volumes, and Poly Haven's errors; then the
provider fetch registry, its HTTP helpers and the server half that drives a fetch.

Label prefixes: `file paths:`, `polyhaven:`, `provider fetch:`.
"""

from .common import (
    ADDON_FILE_PATHS,
    ADDON_INIT,
    ADDON_NETWORK,
    ADDON_POLYHAVEN,
    ADDON_PROVIDER_FETCHES,
    FPT,
    PFT,
    PHFT,
    PHT,
    PNETT,
    RNT,
    SERVER_CORE_TOOL,
    SERVER_PROVIDER_FETCH,
    SKFT,
    SPFT,
    SRVPHT,
    Revert,
)

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
        "polyhaven: a provider fetch's failure goes out raw",
        ADDON_PROVIDER_FETCHES,
        '        return "FAILED", None, f"{failure_label}: {sanitize_blender_error(exc)}"\n',
        '        return "FAILED", None, f"{failure_label}: {exc!s}"\n',
        (
            f"{PHT}::test_a_failure_before_any_download_reports_no_absolute_path[download]",
            f"{PHT}::test_a_failure_before_any_download_reports_no_absolute_path[categories]",
            f"{PHT}::test_a_failure_before_any_download_reports_no_absolute_path[catalog]",
            f"{PFT}::test_a_failed_job_reports_a_sanitized_failure_and_leaves_no_directory",
        ),
    ),
    # --- provider network I/O runs on a worker thread, never on Blender's main thread ---
    Revert(
        "provider fetch: a start runs its job on the calling thread",
        ADDON_PROVIDER_FETCHES,
        "        thread.start()\n",
        "        thread.run()\n",
        (
            f"{PFT}::test_start_returns_while_the_job_is_still_running_on_another_thread",
            f"{PHFT}::test_a_download_runs_on_a_worker_and_the_import_consumes_its_files",
            f"{SKFT}::test_a_download_runs_on_a_worker_and_the_import_consumes_its_files",
        ),
    ),
    Revert(
        "provider fetch: a cancelled fetch keeps downloading",
        ADDON_PROVIDER_FETCHES,
        "        if self._record.cancel.is_set():\n            raise FetchCancelledError\n",
        "        return\n",
        (
            f"{PFT}::test_cancelling_a_running_fetch_stops_its_transfer_and_removes_its_directory",
            f"{PHFT}::test_a_cancelled_download_stops_and_leaves_no_files",
            f"{SKFT}::test_cancelling_a_running_download_stops_it_and_removes_its_files",
        ),
    ),
    Revert(
        "provider fetch: a failed or cancelled fetch leaves its partial files behind",
        ADDON_PROVIDER_FETCHES,
        '            keep_directory = state == "SUCCEEDED" and not record.discarded\n',
        "            keep_directory = True\n",
        (
            f"{PFT}::test_a_failed_job_reports_a_sanitized_failure_and_leaves_no_directory",
            f"{PHFT}::test_a_failed_download_is_sanitized_and_leaves_no_files",
            f"{SKFT}::test_an_archive_with_a_traversal_member_fails_the_fetch_and_removes_its_files",
        ),
    ),
    Revert(
        "provider fetch: a fetch nobody polls any more is never cancelled",
        ADDON_PROVIDER_FETCHES,
        "                if not record.discarded and now - record.seen_at > ABANDONED_AFTER_SECONDS:\n",
        "                if False:\n",
        (f"{PFT}::test_a_finished_fetch_expires_and_an_abandoned_one_is_cancelled",),
    ),
    Revert(
        "provider fetch: any number of fetches run at once",
        ADDON_PROVIDER_FETCHES,
        "            if running >= MAX_RUNNING_FETCHES:\n",
        "            if False:\n",
        (f"{PFT}::test_only_a_bounded_number_of_fetches_run_at_once",),
    ),
    Revert(
        "provider fetch: an import can take the same download twice",
        ADDON_PROVIDER_FETCHES,
        "            else:\n                del self._fetches[fetch_id]\n        _remove_directories(expired)\n",
        "            else:\n                pass\n        _remove_directories(expired)\n",
        (
            f"{PFT}::test_a_download_keeps_its_files_for_the_import_that_takes_it_once",
            f"{PHFT}::test_a_download_runs_on_a_worker_and_the_import_consumes_its_files",
        ),
    ),
    Revert(
        "provider fetch: unregistering the add-on leaves its downloads running",
        ADDON_INIT,
        "    provider_fetches.REGISTRY.shutdown()\n",
        "",
        (f"{PFT}::test_unregistering_the_addon_cancels_its_running_fetches",),
    ),
    Revert(
        "provider fetch: a download never reports its chunks, so it cannot be cancelled mid-file",
        ADDON_NETWORK,
        "        if transfer is not None:\n            transfer.advance(len(chunk))\n",
        "",
        (
            f"{PNETT}::test_a_transfer_hears_the_declared_size_and_every_chunk",
            f"{PNETT}::test_a_transfer_that_raises_abandons_the_download_and_closes_it",
        ),
    ),
    Revert(
        "provider fetch: a cancelled tool call leaves the download running in Blender",
        SERVER_PROVIDER_FETCH,
        "    except (asyncio.CancelledError, Exception):\n        await asyncio.shield(_discard(fetch_id, notices))\n"
        "        raise\n    if status",
        "    except (asyncio.CancelledError, Exception):\n        raise\n    if status",
        (
            f"{SPFT}::test_cancelling_the_call_cancels_the_fetch_in_blender",
            f"{SRVPHT}::test_cancelling_the_call_cancels_the_download_in_blender",
        ),
    ),
    Revert(
        "provider fetch: a download's progress never reaches the client",
        SERVER_PROVIDER_FETCH,
        "    await ctx.report_progress(received, total, message=f\"Blender is {status.get('stage', 'fetching')}\")\n",
        "",
        (
            f"{SPFT}::test_a_query_is_polled_to_its_result_with_progress_and_released",
            f"{SRVPHT}::test_a_running_download_reports_progress_until_it_succeeds",
        ),
    ),
    Revert(
        "provider fetch: a query drops the notices on its start, poll and release replies",
        SERVER_PROVIDER_FETCH,
        '    result["warnings"] = [*notices, *result.get("warnings", [])]\n',
        '    result["warnings"] = result.get("warnings", [])\n',
        (
            f"{SPFT}::test_a_query_is_polled_to_its_result_with_progress_and_released",
            f"{RNT}::test_every_replys_notice_reaches_the_envelope_once_and_leaves_the_data[list_polyhaven_assets]",
            f"{RNT}::test_every_replys_notice_reaches_the_envelope_once_and_leaves_the_data[search_sketchfab_models]",
        ),
    ),
    Revert(
        "provider fetch: an import drops the notices on its download's replies",
        SERVER_PROVIDER_FETCH,
        '        result = {**result, "warnings": [*notices, *result.get("warnings", [])]}\n',
        "        result = dict(result)\n",
        (
            f"{SPFT}::test_a_finished_download_is_handed_to_its_import_by_fetch_id",
            f"{RNT}::test_every_replys_notice_reaches_the_envelope_once_and_leaves_the_data[import_polyhaven_asset]",
            f"{RNT}::test_every_replys_notice_reaches_the_envelope_once_and_leaves_the_data[import_sketchfab_model]",
        ),
    ),
    Revert(
        "provider fetch: the Sketchfab key check drops the status reply's notice",
        SERVER_CORE_TOOL,
        '    notices = [*reply.get("warnings", []), *verdict.get("warnings", [])]\n',
        '    notices = verdict.get("warnings", [])\n',
        (
            f"{RNT}::test_every_replys_notice_reaches_the_envelope_once_and_leaves_the_data"
            "[get_integration_status (sketchfab key check)]",
        ),
    ),
]
