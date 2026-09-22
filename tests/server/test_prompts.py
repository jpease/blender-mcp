"""Regression coverage for the agent strategy prompt's staged workflow."""

from blender_mcp.server.prompts import asset_creation_strategy, character_animation_strategy


def test_prompt_checks_addon_status_before_any_other_tool() -> None:
    text = asset_creation_strategy()

    assert text.index("get_addon_status") < text.index("list_scene_objects")
    assert text.index("get_addon_status") < text.index("get_integration_status")


def test_prompt_does_not_repeat_the_contradictory_fallback_example() -> None:
    text = asset_creation_strategy()

    assert "no dedicated tool has" not in text
    assert "a primitive is explicitly requested" not in text


def test_prompt_teaches_stale_index_and_envelope_gates() -> None:
    text = asset_creation_strategy()

    assert "stale" in text.lower()
    assert '"ok"' in text
    assert '"warnings"' in text
    assert "get_mesh_data" in text
    assert "cancelled" in text.lower()


def test_prompt_scopes_world_bounding_box_to_get_object_info() -> None:
    text = asset_creation_strategy()

    assert "world_bounding_box" in text
    assert "get_object_info" in text
    assert "list_scene_objects() to see what exists" in text


def test_animation_prompt_orders_the_body_before_the_feet() -> None:
    """A foot solved before the hips travel is solved against a body that has not moved yet."""
    text = character_animation_strategy()

    assert text.index("Key the body first") < text.index("Plant the feet")
    assert text.index("keyframe_object_transform") < text.index("keyframe_bone_reach")


def test_animation_prompt_names_the_actual_cause_of_a_sliding_foot() -> None:
    """The agent that reads "interpolation" here will spend the next hour on the wrong curve."""
    text = character_animation_strategy()

    assert "target moved" in text
    assert "interpolation cannot fix that" in text


def test_animation_prompt_requires_a_pole_and_a_hinge_for_a_knee() -> None:
    """Without both, the knee either stays straight or inverts - the two reported symptoms."""
    text = character_animation_strategy()

    assert "pole_target_point" in text
    assert "hinge" in text
    assert "invert the joint" in text


def test_animation_prompt_teaches_the_review_loop_and_the_loop_mode() -> None:
    """An agent that cannot move the playhead reviews frame 1 forever and calls the shot done."""
    text = character_animation_strategy()

    assert "set_scene_frame" in text
    assert "get_viewport_screenshot" in text
    assert "REPEAT_OFFSET" in text
    assert "converged" in text
