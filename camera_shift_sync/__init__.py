"""Camera Shift Sync - Blender 4.2+ extension entry point."""
from __future__ import annotations

import bpy

from . import preferences, properties, operators, ui, plate_draw, shift_sync, external_edit_watch

_panel_cls = None


def _reregister_panel(category: str) -> None:
    global _panel_cls
    if _panel_cls is not None:
        try:
            bpy.utils.unregister_class(_panel_cls)
        except RuntimeError:
            pass
    _panel_cls = ui.build_panel(category)
    bpy.utils.register_class(_panel_cls)


def register() -> None:
    preferences.register(
        on_category_change=_reregister_panel,
    )
    properties.register()
    operators.register()
    plate_draw.register()
    shift_sync.register()
    external_edit_watch.register()

    prefs = preferences.get_prefs()
    category = prefs.panel_category if prefs is not None else "CameraShift"
    _reregister_panel(category)


def unregister() -> None:
    global _panel_cls

    external_edit_watch.unregister()
    shift_sync.unregister()
    plate_draw.unregister()

    if _panel_cls is not None:
        try:
            bpy.utils.unregister_class(_panel_cls)
        except RuntimeError:
            pass
        _panel_cls = None

    operators.unregister()
    properties.unregister()
    preferences.unregister()
