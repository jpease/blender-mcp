"""Typed tools for IK systems, rig drivers, custom shapes, and shape-key controls."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from pydantic import Field, model_validator

from ...app import mcp
from .._dispatch import call_blender
from ._shared import _StrictModel


class ControlBoneDefinition(_StrictModel):
    """A non-deforming control bone defined in armature-local space."""

    name: str = Field(min_length=1, max_length=63)
    head: tuple[float, float, float]
    tail: tuple[float, float, float]
    collection: str = Field(default="CTRL", min_length=1, max_length=63)


class PoleControlDefinition(ControlBoneDefinition):
    """An optional pole control and its explicit IK pole angle."""

    pole_angle: float = 0.0


class CustomShapeAssignment(_StrictModel):
    """Display-shape settings for one pose bone."""

    bone_name: str = Field(min_length=1, max_length=63)
    shape_object_name: str = Field(min_length=1)
    transform_bone_name: str | None = Field(default=None, max_length=63)
    translation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation_euler: tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    wire_width: float = Field(default=1.0, gt=0)
    use_bone_size: bool = True


class DrivenChannel(_StrictModel):
    """An allowlisted destination channel for a rig property driver."""

    owner: Literal["POSE_BONE", "CONSTRAINT", "SHAPE_KEY", "MODIFIER"]
    object_name: str = Field(min_length=1)
    bone_name: str | None = None
    constraint_name: str | None = None
    shape_key_name: str | None = None
    modifier_name: str | None = None
    property_name: Literal[
        "location",
        "rotation_euler",
        "rotation_quaternion",
        "rotation_axis_angle",
        "scale",
        "ik_stretch",
        "influence",
        "value",
        "levels",
        "render_levels",
        "strength",
        "factor",
        "width",
        "thickness",
        "show_viewport",
        "show_render",
    ]
    array_index: int | None = Field(default=None, ge=0, le=3)
    existing_policy: Literal["ERROR", "REPLACE"] = "ERROR"

    @model_validator(mode="after")
    def validate_owner_identity(self) -> "DrivenChannel":
        required = {
            "POSE_BONE": self.bone_name,
            "CONSTRAINT": self.bone_name and self.constraint_name,
            "SHAPE_KEY": self.shape_key_name,
            "MODIFIER": self.modifier_name,
        }[self.owner]
        if not required:
            raise ValueError(f"Missing identity field for {self.owner} destination")
        return self


class DirectShapeKeyControl(_StrictModel):
    """Map one bounded rig property directly to one shape key."""

    mode: Literal["DIRECT"] = "DIRECT"
    shape_key_name: str = Field(min_length=1)
    property_name: str = Field(min_length=1)
    minimum: float = 0.0
    maximum: float = 1.0
    default: float = 0.0
    factor: float = 1.0
    offset: float = 0.0
    existing_driver_policy: Literal["ERROR", "REPLACE"] = "ERROR"

    @model_validator(mode="after")
    def validate_range(self) -> "DirectShapeKeyControl":
        if self.minimum >= self.maximum:
            raise ValueError("minimum must be less than maximum")
        if not self.minimum <= self.default <= self.maximum:
            raise ValueError("default must be inside [minimum, maximum]")
        return self


class SignedShapeKeyControl(_StrictModel):
    """Split the positive and negative sides of one signed property across two shape keys."""

    mode: Literal["SIGNED"] = "SIGNED"
    positive_shape_key_name: str = Field(min_length=1)
    negative_shape_key_name: str = Field(min_length=1)
    property_name: str = Field(min_length=1)
    default: float = Field(default=0.0, ge=-1.0, le=1.0)
    factor: float = Field(default=1.0, gt=0)
    existing_driver_policy: Literal["ERROR", "REPLACE"] = "ERROR"

    @model_validator(mode="after")
    def validate_shape_keys(self) -> "SignedShapeKeyControl":
        if self.positive_shape_key_name == self.negative_shape_key_name:
            raise ValueError("positive and negative shape keys must differ")
        return self


class CorrectivePropertyInput(_StrictModel):
    """One bounded custom-property input to a corrective shape-key formula."""

    property_name: str = Field(min_length=1)
    minimum: float = 0.0
    maximum: float = 1.0
    default: float = 0.0

    @model_validator(mode="after")
    def validate_range(self) -> "CorrectivePropertyInput":
        if self.minimum >= self.maximum:
            raise ValueError("minimum must be less than maximum")
        if not self.minimum <= self.default <= self.maximum:
            raise ValueError("default must be inside [minimum, maximum]")
        return self


class CorrectiveShapeKeyControl(_StrictModel):
    """Drive one corrective shape from an allowlisted multi-property formula."""

    mode: Literal["CORRECTIVE"] = "CORRECTIVE"
    shape_key_name: str = Field(min_length=1)
    inputs: Annotated[list[CorrectivePropertyInput], Field(min_length=2, max_length=8)]
    operation: Literal["MULTIPLY", "MINIMUM", "MAXIMUM", "AVERAGE"] = "MULTIPLY"
    factor: float = 1.0
    offset: float = 0.0
    existing_driver_policy: Literal["ERROR", "REPLACE"] = "ERROR"

    @model_validator(mode="after")
    def validate_inputs(self) -> "CorrectiveShapeKeyControl":
        names = [item.property_name for item in self.inputs]
        if len(names) != len(set(names)):
            raise ValueError("Corrective input property names must be unique")
        return self


ShapeKeyControl = Annotated[
    DirectShapeKeyControl | SignedShapeKeyControl | CorrectiveShapeKeyControl,
    Field(discriminator="mode"),
]


@mcp.tool()
async def create_ik_chain(
    ctx: Context,
    armature_object_name: str,
    chain_bone_names: Annotated[list[str], Field(min_length=1, max_length=64)],
    target_control: ControlBoneDefinition,
    pole_control: PoleControlDefinition | None = None,
    constraint_name: Annotated[str, Field(min_length=1, max_length=63)] = "IK",
    iterations: Annotated[int, Field(ge=1, le=10_000)] = 500,
    use_stretch: bool = False,
) -> dict:
    """
    Create non-deforming target/pole controls and an IK constraint on a contiguous chain.

    All bones named in chain_bone_names, and armature_object_name itself, must already exist.
    chain_bone_names must be ordered from the chain root to its tip and form an unbroken
    parent/child chain; the IK constraint is added to the tip bone. target_control (and
    pole_control, when given) describe new non-deforming control bones this call creates - use
    create_ik_fk_limb instead when the goal is a switchable FK/IK blend rather than IK-only.
    """
    return await call_blender(
        "create_ik_chain",
        {
            "armature_object_name": armature_object_name,
            "chain_bone_names": chain_bone_names,
            "target_control": target_control.model_dump(),
            "pole_control": pole_control.model_dump() if pole_control else None,
            "constraint_name": constraint_name,
            "iterations": iterations,
            "use_stretch": use_stretch,
        },
        changed_objects=[armature_object_name],
    )


@mcp.tool()
async def create_ik_fk_limb(
    ctx: Context,
    armature_object_name: str,
    deform_bone_names: Annotated[list[str], Field(min_length=2, max_length=16)],
    property_bone_name: Annotated[str, Field(min_length=1, max_length=63)],
    property_name: Annotated[str, Field(min_length=1)] = "ik_fk",
    fk_prefix: Annotated[str, Field(min_length=1, max_length=32)] = "FK-",
    ik_prefix: Annotated[str, Field(min_length=1, max_length=32)] = "IK-",
    mechanism_collection: Annotated[str, Field(min_length=1, max_length=63)] = "MCH",
    control_collection: Annotated[str, Field(min_length=1, max_length=63)] = "CTRL",
    ik_target: ControlBoneDefinition | None = None,
    pole_control: PoleControlDefinition | None = None,
) -> dict:
    """
    Duplicate explicit DEF bones into FK/IK chains and drive a bounded IK/FK blend.

    deform_bone_names must be an existing, unbroken, root-to-tip parent chain of deforming
    bones on armature_object_name; each is duplicated (not modified) into an FK copy and an IK
    copy named with fk_prefix/ik_prefix, which must differ. property_bone_name/property_name
    identify a custom property (created at 0=FK, 1=IK) whose drivers blend the DEF bones between
    the two duplicated chains - read it back with get_character_rig_info or similar inspection
    tools before assuming a starting pose. Use create_ik_chain instead for a plain, non-switchable
    IK setup.
    """
    if fk_prefix == ik_prefix:
        raise ValueError("fk_prefix and ik_prefix must differ")
    return await call_blender(
        "create_ik_fk_limb",
        {
            "armature_object_name": armature_object_name,
            "deform_bone_names": deform_bone_names,
            "property_bone_name": property_bone_name,
            "property_name": property_name,
            "fk_prefix": fk_prefix,
            "ik_prefix": ik_prefix,
            "mechanism_collection": mechanism_collection,
            "control_collection": control_collection,
            "ik_target": ik_target.model_dump() if ik_target else None,
            "pole_control": pole_control.model_dump() if pole_control else None,
        },
        changed_objects=[armature_object_name],
    )


@mcp.tool()
async def create_spline_ik_rig(
    ctx: Context,
    armature_object_name: str,
    chain_bone_names: Annotated[list[str], Field(min_length=2, max_length=256)],
    constraint_name: Annotated[str, Field(min_length=1, max_length=63)] = "Spline IK",
    curve_object_name: str | None = None,
    new_curve_name: Annotated[str | None, Field(min_length=1, max_length=63)] = None,
    curve_points: Annotated[list[tuple[float, float, float]], Field(min_length=2, max_length=256)] | None = None,
    curve_collection_name: Annotated[str | None, Field(min_length=1, max_length=63)] = None,
    use_even_divisions: bool = True,
    y_scale_mode: Literal["NONE", "FIT_CURVE", "BONE_ORIGINAL"] = "FIT_CURVE",
    xz_scale_mode: Literal["NONE", "BONE_ORIGINAL", "INVERSE_PRESERVE", "VOLUME_PRESERVE"] = "VOLUME_PRESERVE",
    use_curve_radius: bool = True,
) -> dict:
    """
    Add Spline IK to a contiguous chain using an existing or newly-created curve.

    chain_bone_names must be an existing, unbroken, root-to-tip parent chain on
    armature_object_name. Supply exactly one of: curve_object_name (an existing curve object to
    reuse as-is), or all three of new_curve_name/curve_points/curve_collection_name together (to
    create a new curve) - mixing or omitting both raises a validation error before anything is
    changed. New curve points are armature-local, and the curve object receives the armature's
    world transform.
    """
    existing = curve_object_name is not None
    creating = new_curve_name is not None or curve_points is not None or curve_collection_name is not None
    if existing == creating:
        raise ValueError("Supply either curve_object_name or all new-curve fields")
    if creating and not (new_curve_name and curve_points and curve_collection_name):
        raise ValueError("new_curve_name, curve_points, and curve_collection_name are required together")
    return await call_blender(
        "create_spline_ik_rig",
        {
            "armature_object_name": armature_object_name,
            "chain_bone_names": chain_bone_names,
            "constraint_name": constraint_name,
            "curve_object_name": curve_object_name,
            "new_curve_name": new_curve_name,
            "curve_points": curve_points,
            "curve_collection_name": curve_collection_name,
            "use_even_divisions": use_even_divisions,
            "y_scale_mode": y_scale_mode,
            "xz_scale_mode": xz_scale_mode,
            "use_curve_radius": use_curve_radius,
        },
        changed_objects=[armature_object_name],
    )


@mcp.tool()
async def create_rig_property_driver(
    ctx: Context,
    armature_object_name: str,
    property_owner: Literal["OBJECT", "POSE_BONE"],
    property_name: Annotated[str, Field(min_length=1)],
    destinations: Annotated[list[DrivenChannel], Field(min_length=1, max_length=100)],
    property_bone_name: Annotated[str | None, Field(min_length=1, max_length=63)] = None,
    default: float = 0.0,
    minimum: float = 0.0,
    maximum: float = 1.0,
    soft_minimum: float | None = None,
    soft_maximum: float | None = None,
    factor: float = 1.0,
    offset: float = 0.0,
) -> dict:
    """
    Create one bounded custom property and drive allowlisted destination channels from it.

    property_bone_name is required (and must name an existing pose bone) when property_owner is
    POSE_BONE, and is ignored for OBJECT. minimum/maximum/soft_minimum/soft_maximum/default must
    satisfy minimum <= soft_minimum <= soft_maximum <= maximum and minimum <= default <= maximum,
    or this raises before anything is changed. Each destination's owner-specific identity field
    (bone_name, constraint_name, shape_key_name, or modifier_name) must reference an object,
    bone, constraint, shape key, or modifier that already exists on the named object.
    """
    if minimum >= maximum or not minimum <= default <= maximum:
        raise ValueError("Require minimum < maximum and default inside that range")
    if soft_minimum is not None and not minimum <= soft_minimum <= maximum:
        raise ValueError("soft_minimum must be inside [minimum, maximum]")
    if soft_maximum is not None and not minimum <= soft_maximum <= maximum:
        raise ValueError("soft_maximum must be inside [minimum, maximum]")
    if soft_minimum is not None and soft_maximum is not None and soft_minimum > soft_maximum:
        raise ValueError("soft_minimum must not exceed soft_maximum")
    if property_owner == "POSE_BONE" and not property_bone_name:
        raise ValueError("property_bone_name is required for a POSE_BONE property")
    return await call_blender(
        "create_rig_property_driver",
        {
            "armature_object_name": armature_object_name,
            "property_owner": property_owner,
            "property_bone_name": property_bone_name,
            "property_name": property_name,
            "destinations": [item.model_dump(exclude_none=True) for item in destinations],
            "default": default,
            "minimum": minimum,
            "maximum": maximum,
            "soft_minimum": soft_minimum,
            "soft_maximum": soft_maximum,
            "factor": factor,
            "offset": offset,
        },
        changed_objects=[armature_object_name, *sorted({item.object_name for item in destinations})],
    )


@mcp.tool()
async def assign_bone_custom_shapes(
    ctx: Context,
    armature_object_name: str,
    assignments: Annotated[list[CustomShapeAssignment], Field(min_length=1, max_length=500)],
    widget_collection_name: Annotated[str | None, Field(min_length=1, max_length=63)] = None,
    hide_widgets_from_render: bool = True,
) -> dict:
    """
    Assign reusable widget objects to pose bones without duplicating their geometry.

    Each assignment's bone_name must name an existing pose bone on armature_object_name, and
    shape_object_name must name an existing mesh or curve object already in the scene - this
    tool references that object as the bone's display shape, it does not create one.
    transform_bone_name, if given, must also name an existing bone whose transform offsets the
    displayed shape. widget_collection_name, if given, moves referenced widget objects into that
    collection (created if missing).
    """
    return await call_blender(
        "assign_bone_custom_shapes",
        {
            "armature_object_name": armature_object_name,
            "assignments": [item.model_dump(exclude_none=True) for item in assignments],
            "widget_collection_name": widget_collection_name,
            "hide_widgets_from_render": hide_widgets_from_render,
        },
        changed_objects=[armature_object_name],
    )


@mcp.tool()
async def create_shape_key_controls(
    ctx: Context,
    mesh_object_name: str,
    armature_object_name: str,
    property_owner: Literal["OBJECT", "POSE_BONE"],
    controls: Annotated[list[ShapeKeyControl], Field(min_length=1, max_length=200)],
    property_bone_name: Annotated[str | None, Field(min_length=1, max_length=63)] = None,
) -> dict:
    """
    Drive existing shape keys from new bounded armature or pose-bone custom properties.

    Every shape key named in controls (shape_key_name, or positive_shape_key_name/
    negative_shape_key_name for a SIGNED control) must already exist on mesh_object_name's mesh -
    this tool only creates the driving custom properties and drivers, not the shape keys
    themselves. property_bone_name is required (and must name an existing pose bone) when
    property_owner is POSE_BONE, and is ignored for OBJECT.
    """
    if property_owner == "POSE_BONE" and not property_bone_name:
        raise ValueError("property_bone_name is required for a POSE_BONE property")
    return await call_blender(
        "create_shape_key_controls",
        {
            "mesh_object_name": mesh_object_name,
            "armature_object_name": armature_object_name,
            "property_owner": property_owner,
            "property_bone_name": property_bone_name,
            "controls": [item.model_dump() for item in controls],
        },
        changed_objects=[mesh_object_name, armature_object_name],
    )
