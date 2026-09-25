"""
What every revert-matrix row cites: the `Revert` type, and the files and nodes rows name.

`revert_matrix.py` names the same test files in its coverage lists, so each is spelled
once, here.
"""

import pathlib

from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parents[2]
RIG = ROOT / "scripts/blender_rig.py"
LINT_CHANGED = ROOT / "scripts/lint_changed.py"
ADDON_MANAGER = ROOT / "src/blender_mcp/addon_manager.py"
ADDON_OUTPUT_ROOTS = ROOT / "src/blender_mcp/bundled/addon/output_roots.py"
ADDON_FILE_PATHS = ROOT / "src/blender_mcp/bundled/addon/file_paths.py"
ADDON_LIBRARY_DIGEST = ROOT / "src/blender_mcp/bundled/addon/library_digest.py"
ADDON_POLYHAVEN = ROOT / "src/blender_mcp/bundled/addon/handlers/polyhaven.py"
ADDON_SERVER_CORE = ROOT / "src/blender_mcp/bundled/addon/server_core.py"
# What `server_core.py` was split into: the socket transport and its framing, the command
# registry with every classification and gate read from it, and the scene-inspection handlers.
ADDON_SOCKET_TRANSPORT = ROOT / "src/blender_mcp/bundled/addon/socket_transport.py"
ADDON_COMMAND_REGISTRY = ROOT / "src/blender_mcp/bundled/addon/command_registry.py"
ADDON_SCENE_INSPECTION = ROOT / "src/blender_mcp/bundled/addon/handlers/scene_inspection.py"
ADDON_CAPABILITY_INTROSPECTION = ROOT / "src/blender_mcp/bundled/addon/capability_introspection.py"
ADDON_KEY_STYLE = ROOT / "src/blender_mcp/bundled/addon/handlers/key_style.py"
SERVER_CORE_TOOL = ROOT / "src/blender_mcp/server/tools/core.py"
SERVER_CONNECTION = ROOT / "src/blender_mcp/server/connection.py"
ADDON_SESSION = ROOT / "src/blender_mcp/bundled/addon/session.py"
ADDON_TRANSACTION = ROOT / "src/blender_mcp/bundled/addon/transaction.py"
ADDON_AUTHORED = ROOT / "src/blender_mcp/bundled/addon/authored.py"
ADDON_OBJECT_STATE = ROOT / "src/blender_mcp/bundled/addon/object_state.py"
ADDON_TEXT_HYGIENE = ROOT / "src/blender_mcp/bundled/addon/text_hygiene.py"
SERVER_TEXT_HYGIENE = ROOT / "src/blender_mcp/text_hygiene.py"
ADDON_FILE_LIFECYCLE = ROOT / "src/blender_mcp/bundled/addon/handlers/file_lifecycle.py"
# What every command touching a `.blend` on disk shares (paths, flags, library summaries,
# operator failures), and the provenance block a save stamps and a delivery scan reads back.
ADDON_BLEND_FILES = ROOT / "src/blender_mcp/bundled/addon/handlers/blend_files.py"
ADDON_PROVENANCE = ROOT / "src/blender_mcp/bundled/addon/handlers/provenance.py"
ADDON_LINKING = ROOT / "src/blender_mcp/bundled/addon/handlers/linking.py"
ADDON_VIEWPORT = ROOT / "src/blender_mcp/bundled/addon/handlers/viewport.py"
# Server-side tool wrappers and their registration/documentation surface.
SERVER_FILE_LIFECYCLE_TOOL = ROOT / "src/blender_mcp/server/tools/file_lifecycle.py"
# The one socket round trip every server-side tool makes.
SERVER_DISPATCH = ROOT / "src/blender_mcp/server/tools/_dispatch.py"
# `save_shot.create_directories` and name resolution after a library override.
ADDON_OBJECT_LOOKUP = ROOT / "src/blender_mcp/bundled/addon/object_lookup.py"
ADDON_CANDIDATES = ROOT / "src/blender_mcp/bundled/addon/candidates.py"
SERVER_APP = ROOT / "src/blender_mcp/server/app.py"
ADDON_SCENE = ROOT / "src/blender_mcp/bundled/addon/handlers/scene.py"
# The scene tool wrapper: the preflight's paging arguments and `remove_scene_objects`, which
# moved onto the core surface from `scene_authoring.py`.
SERVER_SCENE_TOOL = ROOT / "src/blender_mcp/server/tools/scene.py"
ADDON_ANIMATION = ROOT / "src/blender_mcp/bundled/addon/handlers/animation.py"
SERVER_ANIMATION_TOOL = ROOT / "src/blender_mcp/server/tools/animation.py"
# The object-keyframing handler and the action-assignment module it and the posing handler
# both key through: one ID holds one action, so both tools make the same mistake.
ADDON_OBJECT_ANIMATION = ROOT / "src/blender_mcp/bundled/addon/handlers/object_animation.py"
ADDON_ACTION_ASSIGNMENT = ROOT / "src/blender_mcp/bundled/addon/handlers/action_assignment.py"
SERVER_ENVELOPE = ROOT / "src/blender_mcp/server/tools/envelope.py"
# The lighting, posing and render-settings handlers the reply-shape work reshaped, and the
# server-side wrappers that carry their `detail` flag across the socket.
ADDON_LIGHTING_SHARED = ROOT / "src/blender_mcp/bundled/addon/handlers/lighting/_shared.py"
ADDON_LIGHTING_INSPECTION = ROOT / "src/blender_mcp/bundled/addon/handlers/lighting/inspection.py"
ADDON_LIGHTING_RENDERING = ROOT / "src/blender_mcp/bundled/addon/handlers/lighting/rendering.py"
SERVER_LIGHTING_INSPECTION_TOOL = ROOT / "src/blender_mcp/server/tools/lighting/inspection.py"
SERVER_LIGHTING_RENDERING_TOOL = ROOT / "src/blender_mcp/server/tools/lighting/rendering.py"
SERVER_LIGHTING_CONSTRUCTION_TOOL = ROOT / "src/blender_mcp/server/tools/lighting/construction.py"
ADDON_CR_PRIMITIVES = ROOT / "src/blender_mcp/bundled/addon/handlers/character_rigging/primitives.py"
ADDON_POSING = ROOT / "src/blender_mcp/bundled/addon/handlers/character_rigging/posing.py"
ADDON_CR_INSPECTION = ROOT / "src/blender_mcp/bundled/addon/handlers/character_rigging/inspection.py"
# The add-on's shared helpers: one definition of "this mesh is deformed by that rig", read by
# both camera framing and the posing surface.
ADDON_HELPERS = ROOT / "src/blender_mcp/bundled/addon/helpers.py"
ADDON_AXES = ROOT / "src/blender_mcp/bundled/addon/handlers/character_rigging/axes.py"
ADDON_REACH = ROOT / "src/blender_mcp/bundled/addon/handlers/character_rigging/reach.py"
SERVER_POSING_TOOL = ROOT / "src/blender_mcp/server/tools/character_rigging/posing.py"
# Camera aiming, on both sides of the socket: the server wrapper preflights a placement it can
# resolve without a round trip, and the handler repeats the check against the resolved target.
ADDON_CAMERA_TARGETING = ROOT / "src/blender_mcp/bundled/addon/handlers/camera/targeting.py"
SERVER_CAMERA_TARGETING_TOOL = ROOT / "src/blender_mcp/server/tools/camera/targeting.py"
# Camera creation on the server side, where a bone qualifies the object it names.
SERVER_CAMERA_CORE_TOOL = ROOT / "src/blender_mcp/server/tools/camera/core.py"
# The two camera modules that leave timeline markers behind: the shared resolution that decides
# whether the earliest one claims the frames before it, and the scene-camera assignment that has
# to report that in the same words create_camera_markers does.
ADDON_CAMERA_SHARED = ROOT / "src/blender_mcp/bundled/addon/handlers/camera/_shared.py"
ADDON_CAMERA_CORE = ROOT / "src/blender_mcp/bundled/addon/handlers/camera/core.py"
ADDON_RENDERING = ROOT / "src/blender_mcp/bundled/addon/handlers/rendering.py"
ADDON_DELIVERY = ROOT / "src/blender_mcp/bundled/addon/handlers/delivery.py"
# Every geometry-nodes builder, and which space each reads the objects it references in.
ADDON_GN_WORKFLOWS = ROOT / "src/blender_mcp/bundled/addon/handlers/geometry_nodes/workflows.py"
# Which optional integration gates which tool, and whether the handshake shows it enabled.
SERVER_INTEGRATIONS = ROOT / "src/blender_mcp/server/integrations.py"
RENDER_COVERAGE_SCRIPT = ROOT / "scripts/render_coverage.py"
SERVER_RENDERING_TOOL = ROOT / "src/blender_mcp/server/tools/rendering.py"
SERVER_DOCUMENTATION = ROOT / "src/blender_mcp/server/tools/_documentation.py"
# Where the tool catalog is registered, and where the hardening pass that follows the
# registration imports is called from.
SERVER_TOOLS_INIT = ROOT / "src/blender_mcp/server/tools/__init__.py"
# The hardening pass itself: which config every generated argument model is given.
SERVER_STRICT_ARGS = ROOT / "src/blender_mcp/server/tools/_strict_args.py"
SERVER_BUNDLES = ROOT / "src/blender_mcp/server/bundles.py"
TEST_BUNDLES_FILE = ROOT / "tests/server/test_bundles.py"
# `scripts/update_addon_surface.py` imports the snapshot's builder and serializer from this
# test module, so the generator and the assertion are one code path and a row reverting it
# reverts what `just addon-surface` writes.
TEST_ADDON_SURFACE_FILE = ROOT / "tests/test_addon_surface.py"
ADDON_INIT = ROOT / "src/blender_mcp/bundled/addon/__init__.py"
TEST_THREADING_FILE = ROOT / "tests/server/test_threading.py"
SERVER_CLI = ROOT / "src/blender_mcp/server/cli.py"
PACKAGE_INIT = ROOT / "src/blender_mcp/__init__.py"
SCRIPTS_INIT = ROOT / "scripts/__init__.py"
QUIET_BOX = ROOT / "scripts/quiet_box.py"
PYPROJECT = ROOT / "pyproject.toml"
DOCKERFILE = ROOT / "docker/blender/Dockerfile"
DOCKERIGNORE = ROOT / "docker/blender/Dockerfile.dockerignore"
COMPOSE = ROOT / "docker/blender/docker-compose.yml"
ENTRYPOINT = ROOT / "docker/blender/entrypoint.sh"
HEALTHCHECK = ROOT / "docker/blender/healthcheck.py"
DOCKER_START = ROOT / "docker/blender/start_server.py"
# The RNA patch helpers the simulation handlers share, the cache frame-range write, and the two
# domain modules that set a cache range through it.
ADDON_RNA_PATCH = ROOT / "src/blender_mcp/bundled/addon/handlers/rna_patch.py"
ADDON_SIMULATION_CACHE = ROOT / "src/blender_mcp/bundled/addon/handlers/simulation_cache.py"
ADDON_LIQUID_INSPECTION = ROOT / "src/blender_mcp/bundled/addon/handlers/liquid/inspection_and_setup.py"
ADDON_RIGID_BODY_INSPECTION = ROOT / "src/blender_mcp/bundled/addon/handlers/rigid_body/inspection_and_setup.py"
# The simulation handlers whose reply could name every body, helper or duplicated member of a
# setup, and now counts them beside the one object or scene the caller acts on next.
ADDON_RIGID_BODY_SIMULATION = ROOT / "src/blender_mcp/bundled/addon/handlers/rigid_body/simulation.py"
ADDON_RIGID_BODY_LIFECYCLE = ROOT / "src/blender_mcp/bundled/addon/handlers/rigid_body/lifecycle.py"
ADDON_CLOTH_VARIANTS = ROOT / "src/blender_mcp/bundled/addon/handlers/cloth/variants.py"
ADDON_LIQUID_DELIVERY = ROOT / "src/blender_mcp/bundled/addon/handlers/liquid/delivery.py"
# The three handlers whose edit reaches every object sharing one datablock - an armature, a
# light, a Geometry Nodes group - and counts those users instead of naming each as changed.
ADDON_CR_STRUCTURE = ROOT / "src/blender_mcp/bundled/addon/handlers/character_rigging/structure.py"
ADDON_LIGHTING_CONSTRUCTION = ROOT / "src/blender_mcp/bundled/addon/handlers/lighting/construction.py"
ADDON_GN_AUTHORING = ROOT / "src/blender_mcp/bundled/addon/handlers/geometry_nodes/authoring.py"
# The ND cleanup handler and the ND and Poly Haven tool wrappers, whose replies could name every
# object a scene-wide cleanup or a model import touched, and now count them.
ADDON_ND = ROOT / "src/blender_mcp/bundled/addon/handlers/nd.py"
SERVER_ND_TOOL = ROOT / "src/blender_mcp/server/tools/nd.py"
SERVER_POLYHAVEN_TOOL = ROOT / "src/blender_mcp/server/tools/polyhaven.py"

# Short names for the test files rows cite. Node ids carry parameter text
# verbatim, so the rows would otherwise be unreadably long lines.
RIGT = "tests/test_blender_rig.py"
LINTT = "tests/test_lint_changed.py"
DOCKT = "tests/test_docker_rig.py"
ROOTST = "tests/test_output_roots.py"
FPT = "tests/test_file_paths.py"
PHT = "tests/test_polyhaven_blend_guard.py"
FLT = "tests/test_file_lifecycle_handlers.py"
LKT = "tests/test_linking_handlers.py"
CORET = "tests/server/tools/test_core.py"
CLIT = "tests/server/test_cli_transport.py"
# Server-side tool wrapper tests. Named apart from FLT (the addon/bpy-level handler
# tests): same subject, different layer.
SFLT = "tests/server/tools/test_file_lifecycle.py"
BUNT = "tests/server/test_bundles.py"
TDT = "tests/server/test_tool_documentation.py"
ENVT = "tests/server/tools/test_envelope.py"
ANIMT = "tests/test_animation_tools.py"
OLT = "tests/test_object_lookup.py"
CANDT = "tests/test_candidates.py"
SIT = "tests/server/test_server_instructions.py"
SOIT = "tests/server/tools/test_scene_object_inspection.py"
AMT = "tests/test_addon_manager.py"
SURFT = "tests/test_addon_surface.py"
STRICTT = "tests/server/test_strict_tool_args.py"
# The reply-shape tests for lighting, posing and render settings, in files this matrix
# does not own: their nodes are listed in NEW_NODES_IN_EXISTING_FILES.
LIGHTT = "tests/server/tools/lighting/test_tools.py"
CTRLT = "tests/server/tools/character_rigging/test_controls.py"
# The domain's only evaluated readback: its nodes all turn on reading the depsgraph
# rather than the file, which is the one thing a fake `bpy` cannot make obvious.
DEFORMT = "tests/server/tools/character_rigging/test_deformed_geometry.py"
POSET = "tests/server/tools/character_rigging/test_posing.py"
REACHT = "tests/server/tools/character_rigging/test_reach.py"
LISTT = "tests/server/tools/character_rigging/test_bone_listing.py"
CRFT = "tests/server/tools/character_rigging/test_foundation.py"
RENDT = "tests/test_rendering_tools.py"
VIEWT = "tests/server/tools/test_viewport.py"
SVT = "tests/server/tools/test_scene_validate.py"
DRT = "tests/server/test_dispatch_rules.py"
RCT = "tests/test_render_coverage.py"
SCENETOOLT = "tests/test_scene_tools.py"
# Render jobs run in their own Blender; this matrix tracks only their paging node.
RJOBT = "tests/test_render_jobs.py"
# The camera, character-rigging and object-keyframing tool tests this matrix does not own
# either; the place-and-aim and action-assignment nodes are listed one by one below.
CAMT = "tests/server/tools/camera/test_tools.py"
CRTT = "tests/server/tools/character_rigging/test_tools.py"
OANIMT = "tests/server/tools/test_object_animation.py"
# Named because inline it passes the line limit, and `ruff format` rejoins a split f-string.
LIST_SCALAR = "test_a_string_where_a_list_belongs_is_not_iterated_character_by_character"
SESSIONT = "tests/test_session_state.py"
TSWAPT = "tests/test_transaction_session_swap.py"
MUTT = "tests/test_mutation_transaction.py"
# The two files this wave's command-registry work added: the dispatch/classification
# gate itself, and the bounded provenance ledger `save_shot` writes into the .blend.
REGT = "tests/test_command_registry.py"
AUTHT = "tests/test_authored_ledger.py"
QBT = "tests/test_quiet_box.py"
THREADT = "tests/server/test_threading.py"
CONNT = "tests/server/test_connection_framing.py"
CONNFAILT = "tests/server/test_connection_failure_detection.py"
CAPT = "tests/test_capability_introspection.py"
KEYSTYLET = "tests/test_key_style.py"
DISPT = "tests/server/tools/test_dispatch.py"
RNAPT = "tests/test_rna_patch.py"
LIQUIDT = "tests/server/tools/liquid/test_tools.py"
RBWT = "tests/server/tools/rigid_body/test_workflows.py"
CLOTHT = "tests/server/tools/cloth/test_tools.py"
GNT = "tests/server/tools/geometry_nodes/test_tools.py"
NDSTATUST = "tests/server/tools/test_nd_status.py"
NDOUTT = "tests/server/tools/test_nd_outcome.py"
SRVPHT = "tests/server/tools/test_polyhaven.py"
GNSPACET = "tests/test_geometry_nodes_builder_spaces.py"
INTEGT = "tests/server/test_integration_gating.py"
HOSTILE_LIB = f"{SESSIONT}::test_a_hostile_library_path_is_reduced_the_same_way_a_failure_note_is"
# The `name` half of the same table, with short ids so a row can list its nodes; the
# `filepath` half's ids run to hundreds of characters.
HOSTILE_LIB_NAME = f"{SESSIONT}::test_a_hostile_library_name_is_reduced_to_a_leaf_like_the_filepath_is"
HOSTILE_LIB_NAME_IDS = (
    "traversal out of the shot",
    "ANSI escape, relative branch",
    "ANSI escape, absolute branch",
    "500 characters, relative branch",
    "500 characters, absolute branch",
    "line separator",
    "fullwidth solidus",
    "bidi override",
    "nfkc-backslash (U+FE68)",
    "big solidus (U+29F8)",
    "rooted relative prefix",
    "zero-width-hidden traversal",
    "format character inside a component",
    "name is an absolute path",
    "name traverses out of the shot",
    "name is a Windows path",
)
EVASION = f"{THREADT}::test_the_producer_scan_catches_every_shape_that_evaded_it"
# Named because inline the node id passes the line limit.
NFKC_BACKSLASH_LIB = (
    f"{HOSTILE_LIB}[nfkc-backslash (U+FE68)-//..\\ufe68..\\ufe68clients\\ufe68acme\\ufe68canon.blend-forbidden8]"
)


@dataclass(frozen=True)
class Revert:
    """
    One reverted behaviour and the test nodes that must notice.

    Attributes:
        label: What is being undone, behind the prefix naming the area it guards.
        path: File to edit.
        old: Text to replace; None means "append `new`", creating the file if needed.
        new: Replacement text.
        nodes: Node ids expected to fail, and the only ones run.
        also: Extra text appended to the same file alongside the replacement.

    """

    label: str
    path: pathlib.Path
    old: str | None
    new: str
    nodes: tuple[str, ...]
    also: str = ""
