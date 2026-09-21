"""
Shared input model for the scene tool modules.

`scene.py` and `scene_authoring.py` belong to different bundles. If either imported the
other, a process selecting one would also register the other's tools.
"""

from pydantic import BaseModel, ConfigDict


class _StrictModel(BaseModel):
    """
    Base for scene tool inputs: rejects unknown fields and non-finite floats.

    `allow_inf_nan=False` does not appear in the JSON schema, so clients meet it only as a
    validation error.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
