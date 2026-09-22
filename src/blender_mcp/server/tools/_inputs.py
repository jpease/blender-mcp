"""
The one base every public tool input model derives from, and the one way to serialize it.

Seven domains each wrote their own `_StrictModel` and their own model-to-dict helper under
three different names (`_dump`, `dump_input`, `explicit_fields`). Nothing kept the copies in
step, and they drifted: `cloth` and `liquid` set `extra="forbid"` without `allow_inf_nan=False`,
so `ClothPinningPatch(pin_stiffness=inf)` validated while the identical shape in every other
domain was refused - a non-finite float reaching a Blender property that clamps it silently.
One base is what stops that from being possible again.

This module registers no tool, so every bundle may import it without pulling another bundle's
tools into the process - the constraint the scene tools live under, where `scene.py` and
`scene_authoring.py` are in different bundles and must not import each other.
"""

import math

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, model_validator


class StrictModel(BaseModel):
    """
    Base for public tool inputs: reject unknown fields and non-finite floats.

    `extra="forbid"` is what makes a misspelled field an error naming the key rather than a
    silently dropped argument and a healthy-looking envelope. `allow_inf_nan=False` does not
    appear in the JSON schema, so a client meets it only as a validation error.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class OpenPayloadModel(StrictModel):
    """
    Base for inputs carrying open JSON: also rejects non-finite floats nested inside them.

    `allow_inf_nan=False` is applied per declared field, so it cannot reach a value inside an
    `Any`, a `dict[str, Any]` or a list of them - exactly the shape a node-graph property bag or
    a socket value has. Those payloads are forwarded to Blender properties that silently clamp a
    non-finite value, so the walk below is the only place it can be refused.

    Two identical copies of this validator lived in `_node_graph.py` and
    `geometry_nodes/_shared.py`; a third would have been written the next time a tool took an
    open payload.
    """

    @model_validator(mode="after")
    def reject_nested_nonfinite_values(self) -> "OpenPayloadModel":
        """
        Walk this model's serialized form and refuse any non-finite float in it.

        Returns:
            OpenPayloadModel: This model, unchanged.

        Raises:
            ValueError: If any float anywhere in the payload is NaN or an infinity.

        """

        # `object`, not `Any`: the walk only narrows by isinstance, so it needs no escape hatch.
        def validate(value: object) -> None:
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("numeric values must be finite")
            if isinstance(value, dict):
                for nested in value.values():
                    validate(nested)
            elif isinstance(value, (list, tuple)):
                for nested in value:
                    validate(nested)

        validate(self.model_dump())
        return self


def dump_input(model: BaseModel | None) -> dict | None:
    """
    Serialize only the fields an agent explicitly supplied on an optional patch model.

    Both exclusions matter and mean different things: `exclude_unset` drops what the caller
    never mentioned, and `exclude_none` drops an explicit null. A patch that carried either
    would overwrite a Blender property with a default the caller never asked for.

    Args:
        model: The patch model, or None when the caller supplied no patch at all.

    Returns:
        dict | None: The supplied fields, or None when there was no model.

    """
    return model.model_dump(exclude_none=True, exclude_unset=True) if model is not None else None


def dump_inputs(items: Sequence[BaseModel]) -> list[dict]:
    """
    Serialize a list of required records for the wire.

    Unlike `dump_input`, an unset field here keeps its default: these are whole records the
    handler reads positionally, not patches layered over existing Blender state.

    Args:
        items: The validated records, in the order the caller gave them.

    Returns:
        list[dict]: One JSON-serializable dict per record.

    """
    return [item.model_dump(exclude_none=True) for item in items]
