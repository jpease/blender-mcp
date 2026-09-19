"""Blender handlers for production cloth simulation workflows."""

import os

import bpy

from ._cache_helpers import (
    _external_cache_path_status as _external_cache_path_status,
)
from ._cache_helpers import (
    _point_cache_context as _point_cache_context,
)
from ._cache_helpers import (
    _prospective_cache_identity as _prospective_cache_identity,
)
from ._cache_helpers import (
    _set_cache_frame_range as _set_cache_frame_range,
)
from ._cache_helpers import (
    _shared_cache_identity as _shared_cache_identity,
)
from ._ownership import (
    _owned_membership_record as _owned_membership_record,
)
from .animation import (
    _ANIMATABLE_FIELDS as _ANIMATABLE_FIELDS,
)
from .animation import ClothAnimationHandlers
from .attachment import ClothAttachmentHandlers
from .character_setup import ClothCharacterSetupHandlers
from .collisions import ClothCollisionHandlers
from .collisions import (
    _is_high_resolution_collider as _is_high_resolution_collider,
)
from .diagnostics import ClothDiagnosticsHandlers
from .diagnostics import (
    _validate_frames as _validate_frames,
)
from .dynamics import ClothDynamicsHandlers
from .dynamics import (
    _sewing_plan as _sewing_plan,
)
from .exporting import ClothExportingHandlers
from .exporting import (
    _validate_distinct_axes as _validate_distinct_axes,
)
from .inspection_and_setup import (
    _MATERIAL_PRESETS as _MATERIAL_PRESETS,
)
from .inspection_and_setup import (
    _WEIGHT_ROLES as _WEIGHT_ROLES,
)
from .inspection_and_setup import (
    ClothInspectionAndSetupHandlers,
)
from .inspection_and_setup import (
    _action_fcurves as _action_fcurves,
)
from .inspection_and_setup import (
    _max_keyed_location_delta as _max_keyed_location_delta,
)
from .inspection_and_setup import (
    _modifier_is_animated as _modifier_is_animated,
)
from .inspection_and_setup import (
    _patch_rna as _patch_rna,
)
from .inspection_and_setup import (
    _reject_baked as _reject_baked,
)
from .lifecycle import ClothLifecycleHandlers
from .material_and_solver import ClothMaterialAndSolverHandlers
from .pinning import ClothPinningHandlers
from .proxy_rigs import ClothProxyRigHandlers
from .render_surface import ClothRenderSurfaceHandlers
from .variants import ClothVariantHandlers


class ClothHandlersMixin(
    ClothMaterialAndSolverHandlers,
    ClothPinningHandlers,
    ClothCollisionHandlers,
    ClothDynamicsHandlers,
    ClothDiagnosticsHandlers,
    ClothAnimationHandlers,
    ClothAttachmentHandlers,
    ClothCharacterSetupHandlers,
    ClothLifecycleHandlers,
    ClothProxyRigHandlers,
    ClothVariantHandlers,
    ClothRenderSurfaceHandlers,
    ClothExportingHandlers,
    ClothInspectionAndSetupHandlers,
):
    """Expose the complete cloth-simulation command surface to the Blender server."""
