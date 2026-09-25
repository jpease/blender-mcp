"""Provide production-oriented Mantaflow liquid handlers."""

from .animation import LiquidAnimationHandlers
from .delivery import LiquidDeliveryHandlers
from .delivery import _bounds_volume as _bounds_volume
from .delivery import _validate_axes as _validate_axes
from .force_fields import LiquidForceFieldHandlers
from .guides import LiquidGuideHandlers
from .inspection_and_setup import LiquidInspectionAndSetupHandlers
from .lifecycle import LiquidLifecycleHandlers
from .mesh_and_materials import LiquidMeshAndMaterialHandlers
from .mesh_and_materials import _expand_viscosity_config as _expand_viscosity_config
from .mesh_and_materials import _particle_role as _particle_role
from .quality import LiquidQualityHandlers
from .result_validation import LiquidResultValidationHandlers
from .shot import LiquidShotHandlers
from .simulation import LiquidSimulationHandlers


class LiquidHandlersMixin(
    LiquidResultValidationHandlers,
    LiquidShotHandlers,
    LiquidDeliveryHandlers,
    LiquidQualityHandlers,
    LiquidLifecycleHandlers,
    LiquidSimulationHandlers,
    LiquidMeshAndMaterialHandlers,
    LiquidGuideHandlers,
    LiquidForceFieldHandlers,
    LiquidAnimationHandlers,
    LiquidInspectionAndSetupHandlers,
):
    """Provide production-oriented Mantaflow liquid handlers."""
