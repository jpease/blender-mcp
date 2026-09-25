"""
Rows guarding the space each geometry-nodes builder reads a referenced object in, and its radius.

Label prefixes: `geometry nodes:`.
"""

from .common import ADDON_GN_WORKFLOWS, GNSPACET, Revert

_SPACES = f"{GNSPACET}::test_each_reference_is_read_in_the_space_its_builder_promises"

ROWS: list[Revert] = [
    Revert(
        "geometry nodes: a boolean reads its cutters' local data, so it cuts at the target's origin",
        ADDON_GN_WORKFLOWS,
        'cutter_name, transform_space="RELATIVE"',
        'cutter_name, transform_space="ORIGINAL"',
        (f"{_SPACES}[boolean-object-cutter]", f"{_SPACES}[boolean-collection-cutters]"),
    ),
    Revert(
        "geometry nodes: a radial array reads its pivot's world location as a local offset",
        ADDON_GN_WORKFLOWS,
        'transform_space="RELATIVE", role="pivot_source"',
        'transform_space="ORIGINAL", role="pivot_source"',
        (f"{_SPACES}[radial-array-pivot]",),
    ),
    Revert(
        "geometry nodes: an array's copies are dragged by the source's distance from the host",
        ADDON_GN_WORKFLOWS,
        'source_name, transform_space="ORIGINAL", location=(-300, -180)',
        'source_name, transform_space="RELATIVE", location=(-300, -180)',
        (f"{_SPACES}[radial-array-pivot]", f"{_SPACES}[curve-array-path]"),
    ),
    Revert(
        "geometry nodes: a curve array ignores where its curve sits",
        ADDON_GN_WORKFLOWS,
        'transform_space="RELATIVE", role="curve_source", location=(-550, 100)',
        'transform_space="ORIGINAL", role="curve_source", location=(-550, 100)',
        (f"{_SPACES}[curve-array-path]",),
    ),
    Revert(
        "geometry nodes: a curve generator ignores where its path curve sits",
        ADDON_GN_WORKFLOWS,
        '                    transform_space="RELATIVE",\n                    role="curve_source",',
        '                    transform_space="ORIGINAL",\n                    role="curve_source",',
        (f"{_SPACES}[curve-generator-path-and-profile]",),
    ),
    Revert(
        "geometry nodes: a curve generator's profile is offset by its distance from the host",
        ADDON_GN_WORKFLOWS,
        '                    transform_space="ORIGINAL",\n                    role="profile_source",',
        '                    transform_space="RELATIVE",\n                    role="profile_source",',
        (f"{_SPACES}[curve-generator-path-and-profile]",),
    ),
    Revert(
        "geometry nodes: the curve radius never reaches Curve to Mesh, so every tube is 1 unit thick",
        ADDON_GN_WORKFLOWS,
        '    link(group, radius_field, "Radius", curve_to_mesh, "Scale")\n',
        "",
        (f"{GNSPACET}::test_curve_generator_scales_its_profile_by_the_curve_radius",),
    ),
    Revert(
        "geometry nodes: a proximity push measures to its target's local data, not where it sits",
        ADDON_GN_WORKFLOWS,
        '                        transform_space="RELATIVE",\n                        role="target_source",',
        '                        transform_space="ORIGINAL",\n                        role="target_source",',
        (f"{_SPACES}[proximity-push-target]",),
    ),
    Revert(
        "geometry nodes: scattered instances are dragged by the source's distance from the host",
        ADDON_GN_WORKFLOWS,
        'source = _source_node(group, source_type, source_name, transform_space="ORIGINAL")',
        'source = _source_node(group, source_type, source_name, transform_space="RELATIVE")',
        (f"{_SPACES}[scatter-instance-source]",),
    ),
    Revert(
        "geometry nodes: panel variants are dragged by their collection's distance from the host",
        ADDON_GN_WORKFLOWS,
        'source_collection_name, transform_space="ORIGINAL"',
        'source_collection_name, transform_space="RELATIVE"',
        (f"{_SPACES}[paneling-panel-collection]",),
    ),
]
