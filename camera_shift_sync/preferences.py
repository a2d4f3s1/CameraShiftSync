"""AddonPreferences for Camera Shift Sync.

Layout follows the Blender add-on common rule: top-level `General` section.
Hosts the N-panel category, plus default style values for the D plane plate
overlay (these are copied into a camera's CSS_CameraSettings on D plane
initialization or via Reset Plate Style; the per-camera values then live in
the .blend). Keyboard shortcuts are intentionally deferred until the UI-driven
flow is complete; see docs/spec.md > "ショートカット（保留）".
"""
from __future__ import annotations

from typing import Callable, Optional

import bpy
from bpy.props import BoolProperty, FloatProperty, FloatVectorProperty, StringProperty
from bpy.types import AddonPreferences

ADDON_ID = __package__

_on_category_change: Optional[Callable[[str], None]] = None


def _category_update(self, context):
    if _on_category_change is not None:
        _on_category_change(self.panel_category)


class CSS_AddonPreferences(AddonPreferences):
    bl_idname = ADDON_ID

    panel_category: StringProperty(
        name="Category (N-Panel)",
        description="Tab category in the N-panel sidebar (bl_category)",
        default="CameraShift",
        update=_category_update,
    )

    default_plate_in_front: BoolProperty(
        name="Default In Front",
        description=(
            "Default 'In Front' state seeded into a camera's plate settings on "
            "D plane init / Reset Plate. ON = always on top, OFF = occluded by "
            "scene objects"
        ),
        default=False,
    )

    default_plate_fill_color: FloatVectorProperty(
        name="Default Plate Fill Color",
        description="Default fill color (RGBA) seeded into a camera's plate settings on D plane init / Reset Plate",
        size=4,
        subtype='COLOR',
        min=0.0,
        max=1.0,
        default=(1.0, 0.5, 0.0, 0.15),
    )

    default_plate_edge_color: FloatVectorProperty(
        name="Default Plate Edge Color",
        description="Default edge color (RGBA) seeded into a camera's plate settings on D plane init / Reset Plate",
        size=4,
        subtype='COLOR',
        min=0.0,
        max=1.0,
        default=(1.0, 0.5, 0.0, 1.0),
    )

    default_plate_edge_width: FloatProperty(
        name="Default Plate Edge Width",
        description="Default outline thickness (pixels) seeded into a camera's plate settings on D plane init / Reset Plate",
        min=0.0,
        max=10.0,
        default=2.0,
    )

    def draw(self, context):
        layout = self.layout

        box = layout.box()
        box.label(text="General")
        box.prop(self, "panel_category")

        box = layout.box()
        box.label(text="Plate Defaults (applied on D Plane init / Reset Plate)")
        col = box.column(align=True)
        col.prop(self, "default_plate_in_front", text="In Front")
        col.prop(self, "default_plate_fill_color", text="Fill Color")
        col.prop(self, "default_plate_edge_color", text="Edge Color")
        col.prop(self, "default_plate_edge_width", text="Edge Width")


def get_prefs():
    addons = bpy.context.preferences.addons
    if ADDON_ID in addons:
        return addons[ADDON_ID].preferences
    return None


def apply_plate_defaults(settings) -> None:
    """Copy AddonPreferences plate defaults into a CSS_CameraSettings instance.
    Used by Reset Plate."""
    prefs = get_prefs()
    if prefs is None:
        return
    settings.plate_in_front = prefs.default_plate_in_front
    settings.plate_fill_color = prefs.default_plate_fill_color
    settings.plate_edge_color = prefs.default_plate_edge_color
    settings.plate_edge_width = prefs.default_plate_edge_width


def register(
    on_category_change: Optional[Callable[[str], None]] = None,
) -> None:
    global _on_category_change
    _on_category_change = on_category_change
    bpy.utils.register_class(CSS_AddonPreferences)


def unregister() -> None:
    global _on_category_change
    bpy.utils.unregister_class(CSS_AddonPreferences)
    _on_category_change = None
