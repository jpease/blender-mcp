# ruff: file-ignore[import-private-name]
"""
The documentation pass must add meaning, not restate the JSON Schema.

`_documentation.py` writes a description for every parameter no docstring covers, and every byte it
writes is advertised in each session's `tools/list` payload. A description that only repeats the
name, type, default, range or enum the schema already carries is pure context cost, so the
generator must leave it out. These tests pin that rule on the generator itself; `test_bundles.py`
pins it on the payload a real server process advertises.
"""

from typing import Any

from blender_mcp.server.tools import _documentation

# Prose forms of constraints the JSON Schema already carries as keywords.
_CONSTRAINT_PROSE = ("Default:", "Allowed:", "Range:", "Must be", "Must not be empty", "Requires", "Allows at most")


def _described(properties: dict[str, Any], explicit: dict[str, str] | None = None) -> dict[str, Any]:
    """
    Run the description pass over one object schema.

    Args:
        properties: The schema's `properties` mapping.
        explicit: Google-style `Args` descriptions parsed from a docstring, if any.

    Returns:
        The described `properties` mapping.

    """
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    _documentation._describe_schema(schema, explicit=explicit)
    return schema["properties"]


def test_schema_constraints_are_not_restated_as_prose() -> None:
    """Enum, default, range and item-count keywords are on the wire already; prose would double them."""
    described = _described(
        {
            "mode": {"type": "string", "enum": ["FAST", "EXACT"], "default": "FAST"},
            "samples": {"anyOf": [{"type": "integer", "minimum": 1, "maximum": 64}, {"type": "null"}], "default": None},
            "bevel_width": {"type": "number", "exclusiveMinimum": 0.0},
            "object_names": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8},
        }
    )
    for name, schema in described.items():
        description = schema.get("description", "")
        assert not any(token in description for token in _CONSTRAINT_PROSE), f"{name}: {description}"


def test_a_parameter_the_schema_already_describes_carries_no_description() -> None:
    """Name plus type is on the wire, so a sentence rephrasing them buys the agent nothing."""
    described = _described(
        {
            "threshold": {"type": "number"},
            "enabled": {"type": "boolean"},
            "use_fast_path": {"type": "boolean"},
            "entries": {"type": "array", "items": {"type": "string"}},
            "eevee_patch": {"$ref": "#/$defs/Patch"},
            "strategy_policy": {"type": "string"},
        }
    )
    assert [name for name, schema in described.items() if "description" in schema] == []


def test_units_and_datablock_semantics_survive() -> None:
    """What the schema cannot state - the unit, the frame space, that the name must already exist - stays."""
    described = _described(
        {
            "tilt_angle": {"type": "number"},
            "bevel_width": {"type": "number"},
            "start_frame": {"type": "integer"},
            "rotation": {"type": "array", "items": {"type": "number"}},
            "cutter_object_name": {"type": "string"},
            "confirm_reset": {"type": "boolean"},
            "bake_output_path": {"type": "string"},
        }
    )
    assert "radians" in described["tilt_angle"]["description"]
    assert "Blender scene units" in described["bevel_width"]["description"]
    assert "timeline frame" in described["start_frame"]["description"]
    assert "radians" in described["rotation"]["description"]
    assert "existing Blender object" in described["cutter_object_name"]["description"]
    assert "refuses" in described["confirm_reset"]["description"]
    assert "no default location" in described["bake_output_path"]["description"]


def test_a_non_numeric_parameter_is_never_labelled_with_scene_units() -> None:
    """`shadow_pool_size` is an enum of texture-pool labels; calling it scene units was simply false."""
    described = _described({"shadow_pool_size": {"type": "string", "enum": ["16", "32", "64"]}})
    assert "description" not in described["shadow_pool_size"]


def test_an_explicit_docstring_description_is_advertised_verbatim() -> None:
    """A hand-written Args entry is the authored contract; the generator must not decorate it."""
    described = _described(
        {"samples": {"type": "integer", "minimum": 1, "default": 64}},
        explicit={"samples": "Cycles samples per pixel."},
    )
    assert described["samples"]["description"] == "Cycles samples per pixel."


def test_the_pagination_offset_keeps_its_page_meaning() -> None:
    """`offset` beside `limit` is a record index, not a geometric offset, and no keyword says so."""
    described = _described({"limit": {"type": "integer"}, "offset": {"type": "integer"}})
    assert described["offset"]["description"] == "Zero-based index of the first record in this result page."


def test_a_nested_model_title_is_not_spliced_into_its_parameters() -> None:
    """
    Splicing a model title into each of its parameters produced text such as "e e v e e" and "g i".

    The title is advertised beside the parameter, so the only fix that also saves bytes is to stop
    repeating it per parameter.
    """
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"eevee": {"$ref": "#/$defs/EeveeLightingQuality"}},
        "$defs": {
            "EeveeLightingQuality": {
                "title": "EeveeLightingQuality",
                "type": "object",
                "properties": {"render_samples": {"type": "integer"}},
            }
        },
    }
    _documentation._describe_schema(schema)
    assert "description" not in schema["$defs"]["EeveeLightingQuality"]["properties"]["render_samples"]


def test_nested_models_still_reject_unknown_fields() -> None:
    """The pass also hardens nested patch models; that must outlive the description trimming."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "$defs": {"Patch": {"type": "object", "properties": {"value": {"type": "number"}}}},
    }
    _documentation._describe_schema(schema)
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["Patch"]["additionalProperties"] is False
