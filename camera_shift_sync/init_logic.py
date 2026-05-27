"""Shared D plane bake / initialize logic.

The actual baking of T / normal / up / `is_initialized = True` and the
"Initialize from target_distance" flow used to live inside `operators.py`.
They're now extracted here so that:

  - `operators.py::CAMERA_OT_get_distance_from_click` can use `bake_d_plane`
    once it has computed T from a raycast hit
  - `properties.py::_is_initialized_update` can call `init_from_target_distance`
    when the user ticks the Initialize checkbox in the N-Panel

T is always computed from the addon's own `settings.target_distance` —
Blender's `cam.dof.focus_object` / `cam.dof.focus_distance` are intentionally
not consulted, so DOF edits do not silently move the D plane. See
`docs/spec.md` > "D 平面の初期化" for the rationale.

Both call sites need to suppress the `is_initialized` update callback while
writing the flag (otherwise the user-tick path recurses back through itself).
That suppression is centralized here and in `external_edit_watch.clear_initialize`.
"""
from __future__ import annotations

from typing import Optional

import bpy
from mathutils import Vector

from . import core


def bake_d_plane(cam_obj, scene, T_world: Vector, settings) -> None:
    """Write T / plate rotation / `is_initialized = True`, enable Show Plate,
    snap the move-delta base to the current camera location, and zero the
    deltas. Plate style and `target_distance` are intentionally preserved
    (the former is per-camera artist setup, the latter is the restore seed).

    Rotation is split: `plate_baked_rotation` captures the current world
    rotation of the camera (as Euler 'XYZ'); `plate_rotation` resets to
    (0, 0, 0) so the Plate Transform Rotation slider reads as zero at the
    moment of Initialize. Effective D-plane orientation = baked @ delta.
    """
    # Local imports to avoid circulars at module-load time.
    from . import properties, external_edit_watch

    baked_euler = cam_obj.matrix_world.to_3x3().to_euler('XYZ')

    # Initialize snapshot 5 values (plan-x.md step 1, 論点 X 合意).
    # Source for derived computation in steps 2+. Written in parallel with
    # the legacy independent properties below during step 1.
    cam_data = cam_obj.data
    settings.init_cam_location = tuple(cam_obj.location)
    settings.init_cam_rotation = (baked_euler.x, baked_euler.y, baked_euler.z)
    settings.init_cam_lens = cam_data.lens
    settings.init_cam_shift_x = cam_data.shift_x
    settings.init_cam_shift_y = cam_data.shift_y

    # Plate Transform writes go through the suppress flag so the update
    # callbacks (rigid-follow camera motion) don't re-fire during bake.
    # Location delta resets to (0,0,0) so the UI slider always reads as zero
    # at the moment of Initialize, matching the Rotation behavior.
    # plate_baked_rotation is derived from snapshot init_cam_rotation via
    # core.compute_plate_baked_rotation (Initialize 後不変)、独立プロパティ
    # への書き込みは不要。plane_origin は独立プロパティのまま、各操作の
    # update callback で書き換える（旧仕様維持）。
    properties.set_plate_transform_suppress(True)
    try:
        settings.plane_origin = T_world
        settings.plate_rotation = (0.0, 0.0, 0.0)
        settings.plate_location_delta = (0.0, 0.0, 0.0)
    finally:
        properties.set_plate_transform_suppress(False)
    # Seed Plate Transform baselines so the next user edit computes the right
    # delta from these freshly-baked values.
    properties.seed_plate_transform_baseline(cam_obj.data)

    # is_initialized has an update callback that re-enters this function
    # (or clear_initialize) when the user toggles the N-Panel checkbox.
    # Suppress for the internal write here.
    properties.set_is_initialized_suppress(True)
    try:
        settings.is_initialized = True
    finally:
        properties.set_is_initialized_suppress(False)
    settings.show_plate = True
    # Camera Position UI Delta sliders reset to 0 (init anchor state); Radial
    # Distance baked to |init_plate_local| (the camera's actual distance to d
    # at this moment). Suppress _move_position_suppress so the writes don't
    # fire each callback's body (this is bake, not user edit).
    properties.set_move_position_suppress(True)
    try:
        settings.move_delta_x = 0.0
        settings.move_delta_y = 0.0
        settings.move_delta_z = 0.0
        init_PL = core.compute_init_plate_local(cam_obj.data)
        settings.radial_distance = init_PL.length if init_PL.length > 0.0 else 1.0
    finally:
        properties.set_move_position_suppress(False)
    # Seed the external-edit watcher's baseline so the next depsgraph tick
    # doesn't read this Initialize as an external edit.
    external_edit_watch.remember_internal_transform(cam_obj)
    # Force N-Panel redraw so dependent sections (Lens Shift / Camera Position
    # / Plate Transform) appear immediately rather than waiting for the next
    # mouse move.
    wm = bpy.context.window_manager
    if wm is not None:
        for window in wm.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()


def init_from_target_distance(cam_obj, scene, settings) -> Optional[str]:
    """Compute T along the camera's shift view direction at the current
    `settings.target_distance`, then bake the D plane. Returns None on
    success, or a warning string if T couldn't be computed (view_frame
    degenerate — only possible on a broken camera)."""
    T_world = core.shift_view_target_world(cam_obj, scene, settings.target_distance)
    if T_world is None:
        return "Initialize D Plane: view_frame depth degenerate"
    bake_d_plane(cam_obj, scene, T_world, settings)
    return None
