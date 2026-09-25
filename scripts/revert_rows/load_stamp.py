"""
Rows guarding the load stamp that decides whether a timing result is evidence.

Label prefix: `quiet box:`.
"""

from .common import QBT, QUIET_BOX, Revert

ROWS: list[Revert] = [
    # --- the load stamp itself, which decides whether a timing result is evidence ---
    Revert(
        "quiet box: an unreadable load average reports as the quietest possible machine",
        QUIET_BOX,
        "    except (OSError, AttributeError):\n        return None",
        "    except (OSError, AttributeError):\n        return 0.0",
        (f"{QBT}::test_an_unreadable_load_average_is_unknown_not_quiet",),
    ),
    Revert(
        "quiet box: an unknown load is treated as quiet, which is the failure the stamp exists to stop",
        QUIET_BOX,
        "    return per_core is not None and per_core <= QUIET_BOX_LOAD_PER_CORE",
        "    return per_core is None or per_core <= QUIET_BOX_LOAD_PER_CORE",
        (f"{QBT}::test_an_unknown_load_is_never_reported_as_quiet_box_verified",),
    ),
    Revert(
        "quiet box: the threshold stops being inclusive at parity",
        QUIET_BOX,
        "    return per_core is not None and per_core <= QUIET_BOX_LOAD_PER_CORE",
        "    return per_core is not None and per_core < QUIET_BOX_LOAD_PER_CORE",
        (f"{QBT}::test_the_threshold_is_inclusive_at_parity_and_excludes_anything_above_it",),
    ),
    Revert(
        "quiet box: a cpu count of 0 falls into the division instead of reporting unknown",
        QUIET_BOX,
        "    if not cores:",
        "    if cores is None:",
        (f"{QBT}::test_a_cpu_count_of_zero_is_unknown_rather_than_a_division_error",),
    ),
    Revert(
        "quiet box: the 15-minute average is stamped in place of the 1-minute one",
        QUIET_BOX,
        "        one_minute = os.getloadavg()[0]",
        "        one_minute = os.getloadavg()[1]",
        (f"{QBT}::test_load_per_core_divides_the_one_minute_average_by_the_cpu_count",),
    ),
    Revert(
        "quiet box: every stamp reads verified, whatever the load was",
        QUIET_BOX,
        '    verdict = "quiet-box verified" if per_core <= QUIET_BOX_LOAD_PER_CORE else "NOT quiet-box verified"',
        '    verdict = "quiet-box verified"',
        (f"{QBT}::test_a_contended_stamp_says_so_rather_than_only_omitting_the_verdict",),
    ),
    Revert(
        "quiet box: the stamp drops the threshold it was judged against",
        QUIET_BOX,
        '    return f"QUIET BOX [{label}]: {per_core:.2f} load per core '
        '(threshold {QUIET_BOX_LOAD_PER_CORE:.2f}) - {verdict}"',
        '    return f"QUIET BOX [{label}]: {per_core:.2f} load per core - {verdict}"',
        (f"{QBT}::test_the_stamp_names_the_load_the_threshold_and_the_verdict",),
    ),
    Revert(
        "quiet box: an unavailable load average is stamped as 0.00 and verified",
        QUIET_BOX,
        '        return f"QUIET BOX [{label}]: load average unavailable - NOT quiet-box verified"',
        '        return f"QUIET BOX [{label}]: 0.00 load per core - quiet-box verified"',
        (f"{QBT}::test_the_stamp_says_not_verified_when_the_load_is_unavailable",),
    ),
    Revert(
        "quiet box: a scenario copied out of the repository gets None instead of a loud failure",
        QUIET_BOX,
        '    raise FileNotFoundError(f"scripts/quiet_box.py not found above {start}")',
        "    return None  # reverted: silently unstamped",
        (f"{QBT}::test_the_by_path_loader_refuses_a_caller_outside_the_repository",),
    ),
]
