"""Composition root for the character-rigging handler surface."""

from .constraints import PoseConstraintHandlersMixin
from .controls import ControlRigHandlersMixin
from .deformation import DeformationHandlersMixin
from .inspection import RigInspectionHandlersMixin
from .posing import PoseAnimationHandlersMixin
from .reach import BoneReachHandlersMixin
from .skinning import SkinningHandlersMixin
from .structure import ArmatureStructureHandlersMixin


class CharacterRiggingHandlersMixin(
    RigInspectionHandlersMixin,
    ArmatureStructureHandlersMixin,
    SkinningHandlersMixin,
    PoseConstraintHandlersMixin,
    DeformationHandlersMixin,
    ControlRigHandlersMixin,
    PoseAnimationHandlersMixin,
    BoneReachHandlersMixin,
):
    """Provide the complete structured character-rigging command surface."""
