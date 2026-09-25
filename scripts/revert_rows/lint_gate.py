"""
Rows guarding the changed-line lint gate's ratchet on whole-scope metrics.

`scripts/lint_changed.py` owns a function's size metrics once the branch writes any line in
it, and then forgives the ones the same function already carried at the base at the same or
a higher value. Both directions are silent when wrong: forgive too much and a branch bloats a
legacy function unnoticed; forgive nothing and a one-word rename inside it fails the gate.

Label prefix: `lint gate:`.
"""

from .common import LINT_CHANGED, LINTT, Revert

ROWS: list[Revert] = [
    Revert(
        "lint gate: a metric the branch raised is forgiven because the base already carried one",
        LINT_CHANGED,
        "    return value <= base[key]\n",
        "    return True\n",
        (f"{LINTT}::test_a_legacy_metric_the_branch_raised_is_owned",),
    ),
    Revert(
        # The control: the ratchet itself is what keeps a rename inside a legacy function out.
        "lint gate: control: an untouched legacy metric is owned by a branch that renamed one call in it",
        LINT_CHANGED,
        "    return value <= base[key]\n",
        "    return False\n",
        (f"{LINTT}::test_a_legacy_metric_the_branch_only_touched_stays_in_the_backlog",),
    ),
    Revert(
        "lint gate: a method's metric is keyed on the class around it",
        LINT_CHANGED,
        '    return str(finding["code"]), min(holding)[1]\n',
        '    return str(finding["code"]), max(holding)[1]\n',
        (f"{LINTT}::test_a_metric_is_keyed_on_the_innermost_definition_holding_it",),
    ),
    Revert(
        "lint gate: metrics are matched by code alone, so a renamed function inherits its old name's reading",
        LINT_CHANGED,
        '    return str(finding["code"]), min(holding)[1]\n',
        '    return str(finding["code"]), ""\n',
        (f"{LINTT}::test_a_renamed_function_has_no_base_reading_and_is_owned",),
    ),
]
