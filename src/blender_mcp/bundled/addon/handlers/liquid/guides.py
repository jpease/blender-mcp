# pyright: reportGeneralTypeIssues=false
"""Liquid domain guide-source setup handlers."""

import contextlib

import bpy

from .inspection_and_setup import (
    _ensure_collection,
    _get_domain,
    _get_object,
    _link_object,
    _read_fields,
    _reject_baked,
    _validate_rna_value,
)
from .simulation import _scene_context_for_object, _set_cache_range

_GUIDE_DOMAIN_FIELDS = {"use_guide", "guide_source", "guide_alpha", "guide_beta", "guide_vel_factor"}


class LiquidGuideHandlers:
    """Attach effector- or domain-driven guide velocities to a liquid domain."""

    def create_liquid_guide(
        self,
        domain_object_name,
        domain_modifier_name,
        guide_object_name,
        source="EFFECTOR",
        guide_modifier_name="Liquid Guide",
        existing_policy="ERROR",
        guide_mode="OVERRIDE",
        velocity_factor=1.0,
        guide_parent_domain_object_name=None,
        guide_collection_name=None,
        cache_frame_start=None,
        cache_frame_end=None,
        guide_alpha=None,
        guide_beta=None,
        guide_vel_factor=None,
    ):
        domain_obj, domain_modifier, domain = _get_domain(domain_object_name, domain_modifier_name)
        _reject_baked(domain)
        if source not in {"EFFECTOR", "DOMAIN"}:
            raise ValueError("source must be EFFECTOR or DOMAIN")
        start = domain.cache_frame_start if cache_frame_start is None else int(cache_frame_start)
        end = domain.cache_frame_end if cache_frame_end is None else int(cache_frame_end)
        if start > end:
            raise ValueError("cache_frame_start must be <= cache_frame_end")
        if source == "EFFECTOR" and guide_parent_domain_object_name is not None:
            raise ValueError("guide_parent_domain_object_name is valid only for DOMAIN guide sources")
        if source == "DOMAIN" and not guide_parent_domain_object_name:
            raise ValueError("DOMAIN guide sources require guide_parent_domain_object_name")
        old_domain = {
            name: getattr(domain, name)
            for name in (
                *_GUIDE_DOMAIN_FIELDS,
                "guide_parent",
                "effector_group",
                "cache_frame_start",
                "cache_frame_end",
            )
        }
        guide_result = None
        linked = False
        created_collection_link = False
        try:
            if source == "EFFECTOR":
                guide = _get_object(guide_object_name, {"MESH"})
                if guide_collection_name:
                    scene, _view_layer = _scene_context_for_object(domain_obj)
                    collection, _created, created_collection_link = _ensure_collection(scene, guide_collection_name)
                    linked = _link_object(collection, guide)
                    domain.effector_group = collection
                guide_result = self.add_liquid_effector(
                    object_name=guide_object_name,
                    domain_object_name=domain_object_name,
                    modifier_name=guide_modifier_name,
                    existing_policy=existing_policy,
                    effector_type="GUIDE",
                    settings={"guide_mode": guide_mode, "velocity_factor": velocity_factor},
                )
                domain.use_guide = True
                domain.guide_source = "EFFECTOR"
                domain.guide_parent = None
            else:
                parent_obj, _parent_modifier, _parent = _get_domain(guide_parent_domain_object_name)
                if parent_obj == domain_obj:
                    raise ValueError("A liquid domain cannot guide itself")
                guide_object = _get_object(guide_object_name)
                if guide_object != parent_obj:
                    raise ValueError("For DOMAIN guides, guide_object_name must identify the parent domain")
                domain.use_guide = True
                domain.guide_source = "DOMAIN"
                domain.guide_parent = parent_obj
            for name, value in (
                ("guide_alpha", guide_alpha),
                ("guide_beta", guide_beta),
                ("guide_vel_factor", guide_vel_factor),
            ):
                if value is not None:
                    _validate_rna_value(domain, name, value)
                    setattr(domain, name, value)
            _set_cache_range(domain, start, end)
            bpy.context.view_layer.update()
        except Exception:
            for name, value in old_domain.items():
                if name in {"cache_frame_start", "cache_frame_end"}:
                    continue
                with contextlib.suppress(Exception):
                    setattr(domain, name, value)
            with contextlib.suppress(Exception):
                _set_cache_range(domain, old_domain["cache_frame_start"], old_domain["cache_frame_end"])
            if linked:
                with contextlib.suppress(Exception):
                    collection.objects.unlink(guide)  # pyright: ignore[reportArgumentType]
            if created_collection_link:
                with contextlib.suppress(Exception):
                    scene.collection.children.unlink(collection)  # pyright: ignore[reportArgumentType]
            raise
        return {
            "changed_objects": sorted({domain_obj.name, guide_object_name}),
            "domain": domain_obj.name,
            "domain_modifier": domain_modifier.name,
            "source": source,
            "guide_object": guide_object_name,
            "guide_setup": guide_result,
            "settings": _read_fields(domain, _GUIDE_DOMAIN_FIELDS | {"guide_parent"}),
            "frame_range": [domain.cache_frame_start, domain.cache_frame_end],
            "required_bake_order": ["GUIDES", "DATA", "MESH/PARTICLES"],
            "invalidated_cache_stages": ["GUIDES", "DATA", "MESH", "PARTICLES"],
        }
