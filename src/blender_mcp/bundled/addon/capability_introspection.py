"""
Report which keyword parameters each dispatchable command handler accepts.

get_addon_info's handshake publishes this so connection.py's preflight gate can refuse a
call before it ever reaches the addon, when a parameter was added to a tool's schema after
this copy of the addon was installed - the exact gap that let a stale save_shot crash on an
unrecognized write_provenance keyword instead of failing predictably. Free of `bpy`: takes
plain callables, the same shape _build_command_handlers() already builds for dispatch.
"""

import inspect

from collections.abc import Callable, Mapping

# Reported instead of a name list for a handler whose accepted keywords cannot be enumerated
# - either it takes **kwargs, or its signature could not be read at all. Either way, filtering
# a caller's parameters against it would be a guess; connection.py's gate skips filtering
# entirely when it sees this sentinel, exactly as it does when a command is altogether absent
# from an older addon's capability_params.
ACCEPTS_ANY_KEYWORD = "*"

_KEYWORD_KINDS = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)


def capability_params(handlers: Mapping[str, Callable[..., object]]) -> dict[str, list[str] | str]:
    """
    Map each handler's accepted keyword names, or ACCEPTS_ANY_KEYWORD when unknowable.

    Args:
        handlers: Command name to bound handler method, the mapping _build_command_handlers()
            already builds for dispatch.

    Returns:
        dict[str, list[str] | str]: Command name to a sorted list of accepted keyword names,
        or ACCEPTS_ANY_KEYWORD for a handler that takes **kwargs, whose signature could not
        be read, or that is not callable at all.

    """
    result: dict[str, list[str] | str] = {}
    for name, handler in handlers.items():
        try:
            parameters = inspect.signature(handler).parameters.values()
        except (TypeError, ValueError):
            result[name] = ACCEPTS_ANY_KEYWORD
            continue
        if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
            result[name] = ACCEPTS_ANY_KEYWORD
        else:
            result[name] = sorted(parameter.name for parameter in parameters if parameter.kind in _KEYWORD_KINDS)
    return result
