"""
Coverage for the per-command keyword introspection reported by get_addon_info.

Kept free of `bpy` and loaded straight from source, so these tests need no Blender - the
same pattern tests/test_output_roots.py uses for output_roots.py, another leaf module that
only feeds get_addon_info's handshake.
"""

from types import ModuleType

from conftest import load_addon_source_module


def _load_capability_introspection() -> ModuleType:
    return load_addon_source_module("capability_introspection.py", "addon_capability_introspection_under_test")


class _FakeHandlers:
    """Stand-ins shaped like the bound methods _build_command_handlers() actually returns."""

    def named(self, object_name: str, space: str = "WORLD") -> None: ...

    def flexible(self, **kwargs: object) -> None: ...

    def positional_only(self, x: int, /, y: int = 0) -> None: ...


def test_a_handler_with_named_parameters_reports_them_sorted() -> None:
    module = _load_capability_introspection()
    fakes = _FakeHandlers()

    assert module.capability_params({"named": fakes.named}) == {"named": ["object_name", "space"]}


def test_self_is_never_reported_for_a_bound_method() -> None:
    """_build_command_handlers() hands bound methods to this function; self is not a JSON parameter."""
    module = _load_capability_introspection()
    fakes = _FakeHandlers()

    assert "self" not in module.capability_params({"named": fakes.named})["named"]


def test_a_handler_taking_kwargs_reports_the_wildcard_sentinel() -> None:
    module = _load_capability_introspection()
    fakes = _FakeHandlers()

    assert module.capability_params({"flexible": fakes.flexible}) == {"flexible": module.ACCEPTS_ANY_KEYWORD}


def test_a_positional_only_parameter_is_not_reported_as_an_accepted_keyword() -> None:
    """
    A positional-only parameter can never arrive as a JSON object key.

    Reporting it as accepted would let a caller believe a keyword works that Python
    would reject.
    """
    module = _load_capability_introspection()
    fakes = _FakeHandlers()

    assert module.capability_params({"positional_only": fakes.positional_only}) == {"positional_only": ["y"]}


def test_a_value_that_is_not_callable_reports_the_wildcard_sentinel_instead_of_raising() -> None:
    """A non-function value in the handler table must not crash the handshake."""
    module = _load_capability_introspection()

    assert module.capability_params({"broken": object()}) == {"broken": module.ACCEPTS_ANY_KEYWORD}
