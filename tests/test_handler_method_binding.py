"""
Every add-on handler method must be callable the way its call sites call it.

The add-on's handler mixins cannot be imported without a live `bpy`, so nothing
in this suite executes them. That let `ClothMaterialAndSolverHandlers._scale_warnings`
ship declared as `def _scale_warnings(obj)`: seven `self._scale_warnings(obj)`
call sites across four cloth modules, every one of them a guaranteed
`TypeError: takes 1 positional argument but 2 were given` the moment Blender
reaches it. The type checker saw it; the tests could not.

This checks the shape instead of the behaviour, which is the only thing
reachable without Blender, and it generalises: a method that forgets `self`
anywhere under `src/` fails here.
"""

from __future__ import annotations

import ast

from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
BOUND_FIRST_PARAMETERS = frozenset({"self", "cls"})
UNBOUND_DECORATORS = frozenset({"staticmethod", "classmethod"})


def _decorator_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """
    Name every decorator applied to a function, however it is spelled.

    Args:
        function: The decorated function node.

    Returns:
        set[str]: Bare names for `@name` and trailing attributes for `@mod.name`.

    """
    names: set[str] = set()
    for decorator in function.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def _unbindable_methods() -> list[str]:
    """
    Find instance methods whose first parameter cannot receive the instance.

    Returns:
        list[str]: `path:line class.method(params)` for each offender.

    """
    offenders: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for method in node.body:
                if not isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                if _decorator_names(method) & UNBOUND_DECORATORS:
                    continue
                parameters = [argument.arg for argument in method.args.posonlyargs + method.args.args]
                if parameters[:1] and parameters[0] in BOUND_FIRST_PARAMETERS:
                    continue
                location = f"{path.relative_to(SOURCE_ROOT.parent)}:{method.lineno}"
                offenders.append(f"{location} {node.name}.{method.name}({', '.join(parameters)})")
    return offenders


def test_every_instance_method_can_receive_its_instance() -> None:
    """A method missing `self` raises TypeError at every call site that binds it."""
    offenders = _unbindable_methods()

    assert offenders == [], "instance methods that cannot be called on an instance:\n  " + "\n  ".join(offenders)
