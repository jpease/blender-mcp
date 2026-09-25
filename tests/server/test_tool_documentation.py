"""
The documentation pass must add meaning, not restate the JSON Schema.

`_documentation.py` writes a description for every parameter no docstring covers, and every byte it
writes is advertised in each session's `tools/list` payload. A description that only repeats the
name, type, default, range or enum the schema already carries is pure context cost, so the
generator must leave it out. These tests pin that rule on the generator itself; `test_bundles.py`
pins it on the payload a real server process advertises. The same pass sets each tool's destructive
hint, which a flag gating the loss of data earns whatever the tool is called.
"""

import copy

from typing import Any

import pytest

from blender_mcp.server.tools import _documentation

# Each parameter twice: with the constraint keywords a tool schema carries, and without them.
_CONSTRAINED_AND_BARE: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "mode": ({"type": "string", "enum": ["FAST", "EXACT"], "default": "FAST"}, {"type": "string"}),
    "samples": (
        {"anyOf": [{"type": "integer", "minimum": 1, "maximum": 64}, {"type": "null"}], "default": None},
        {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    ),
    "bevel_width": ({"type": "number", "exclusiveMinimum": 0.0}, {"type": "number"}),
    "object_names": (
        {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8},
        {"type": "array", "items": {"type": "string"}},
    ),
}

# Parameters whose meaning the schema cannot carry, grouped by the concept the description must state:
# the unit of an angle or a distance, the timeline a frame counts on, a datablock that must already
# exist, a refusal, and a location nothing defaults.
_CONCEPTS: dict[str, dict[str, dict[str, Any]]] = {
    "angle": {"tilt_angle": {"type": "number"}, "yaw": {"type": "number"}},
    "distance": {"bevel_width": {"type": "number"}, "radius": {"type": "number"}},
    "frame": {"start_frame": {"type": "integer"}, "end_frame": {"type": "integer"}},
    "euler rotation": {"rotation": {"type": "array", "items": {"type": "number"}}},
    "scale factors": {"scale": {"type": "array", "items": {"type": "number"}}},
    "existing object": {"cutter_object_name": {"type": "string"}, "mirror_object_name": {"type": "string"}},
    "refusal": {"confirm_reset": {"type": "boolean"}, "confirm_delete": {"type": "boolean"}},
    "explicit location": {"bake_output_path": {"type": "string"}, "scratch_directory": {"type": "string"}},
}


def _described(properties: dict[str, Any], explicit: dict[str, str] | None = None) -> dict[str, Any]:
    """
    Run the description pass over one object schema.

    Args:
        properties: The schema's `properties` mapping, copied so the caller's stays as written.
        explicit: Google-style `Args` descriptions parsed from a docstring, if any.

    Returns:
        The described `properties` mapping.

    """
    schema: dict[str, Any] = {"type": "object", "properties": copy.deepcopy(properties)}
    _documentation._describe_schema(schema, explicit=explicit)
    return schema["properties"]


def test_schema_constraints_are_not_restated_as_prose() -> None:
    """
    Enum, default, range and item-count keywords are on the wire already; prose would double them.

    So adding the keywords to a schema must not change its description at all.
    """
    constrained = _described({name: pair[0] for name, pair in _CONSTRAINED_AND_BARE.items()})
    bare = _described({name: pair[1] for name, pair in _CONSTRAINED_AND_BARE.items()})

    assert {name: schema.get("description") for name, schema in constrained.items()} == {
        name: schema.get("description") for name, schema in bare.items()
    }


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
    """
    What the schema cannot state - the unit, the frame space, that the name must already exist - stays.

    Asserted on the generator's decisions rather than its sentences: every such parameter is
    described, one concept reads the same wherever it appears, and no two concepts read alike, so an
    angle is never described as a distance.
    """
    described = _described({name: schema for members in _CONCEPTS.values() for name, schema in members.items()})
    by_concept = {
        concept: {described[name].get("description") for name in members} for concept, members in _CONCEPTS.items()
    }

    assert all(None not in descriptions for descriptions in by_concept.values()), by_concept
    assert all(len(descriptions) == 1 for descriptions in by_concept.values()), by_concept
    assert len(set().union(*by_concept.values())) == len(_CONCEPTS), by_concept


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


def test_only_an_offset_beside_a_limit_is_described_as_the_start_of_a_page() -> None:
    """
    `offset` beside `limit` is a record index, not a geometric offset, and no keyword says so.

    Alone it is neither, so it gets no page description; and a tool's own docstring still wins.
    """
    paged = {"limit": {"type": "integer"}, "offset": {"type": "integer"}}

    assert _described(paged)["offset"].get("description")
    assert "description" not in _described({"offset": {"type": "integer"}})["offset"]
    assert _described(paged, explicit={"offset": "Frames to shift."})["offset"]["description"] == "Frames to shift."


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


@pytest.mark.parametrize("flag", ["confirm_free", "confirm_overwrite"])
def test_a_flag_that_frees_a_cache_or_replaces_a_file_marks_its_tool_destructive(flag: str) -> None:
    """
    The settled names for freeing a cache and replacing a file each earn the hint on their own.

    `tally_frames` matches no destructive prefix and is not listed by name, so only the flag can
    mark it; a flag renamed without its entry in the conditional set leaves the hint off silently.
    """
    assert not _documentation._is_destructive("tally_frames", {"properties": {}})
    assert _documentation._is_destructive("tally_frames", {"properties": {flag: {"type": "boolean"}}})
