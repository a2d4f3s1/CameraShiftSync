"""Operators for Camera Shift Sync.

Function A is driven by the per-camera move deltas in `CSS_CameraSettings`
(see `properties.py` for the update callback that does the actual camera move
+ shift recompute). Initialize / De-Initialize are not operators — they are
done by toggling `is_initialized` in the N-Panel (its update callback runs
Initialize from DOF or `clear_initialize` as appropriate). The operators
here only:

  - `camera_shift_sync.get_distance_from_click`: modal raycast picker.
    Initializes the D plane along the camera's current shift view direction
    at the picked perpendicular depth. Direction is from the shift view, not
    the click — only the distance comes from the click.
  - `camera_shift_sync.reset_plate_style`: copy AddonPreferences plate
    defaults (in_front / fill / edge / edge width) back into the active
    camera. UI label is "Reset Plate"; bl_idname kept for compatibility.

`camera_shift_sync.pick_d_plane` (the legacy hit-point-as-target picker) is
still defined below as a class but intentionally not registered; it's parked
for the Function B work where its behavior may diverge from
Get Distance from Click.
"""
from __future__ import annotations

import bpy
from bpy.types import Operator

from . import core, init_logic, preferences as prefs_mod


def _camera_obj_poll(context) -> bool:
    """Active object is a camera (any type). Used by plate-style operators
    that don't depend on the projection mode."""
    obj = context.active_object
    return obj is not None and obj.type == 'CAMERA'


def _camera_poll(context) -> bool:
    """Active object is a perspective camera. Used by operators that require
    Initialize-state semantics (which only make sense for PERSP). Orthographic
    shift is equivalent to plain XY translation and the shift/position
    coupling loses its meaning; panoramic projection is unsupported.
    See docs/spec.md > "対象カメラ"."""
    obj = context.active_object
    if obj is None or obj.type != 'CAMERA':
        return False
    return obj.data.type == 'PERSP'


class CAMERA_OT_get_distance_from_click(Operator):
    bl_idname = "camera_shift_sync.get_distance_from_click"
    bl_label = "Get Distance from Click"
    bl_description = (
        "Click a surface in the 3D Viewport to read its perpendicular depth, "
        "then write that value into target_distance. Equivalent to editing "
        "the Target Distance slider with the picked value: snapshot / UI "
        "Delta / Initialize state are NOT touched (no bake). If Initialize "
        "is ON, T moves to the new distance; if OFF, only the Live plate "
        "size updates. ESC or right-click to cancel."
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _camera_poll(context)

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, "Get Distance from Click: must be invoked in the 3D Viewport")
            return {'CANCELLED'}
        if context.region is None or context.region_data is None:
            self.report({'WARNING'}, "Get Distance from Click: 3D Viewport region not available")
            return {'CANCELLED'}

        context.window.cursor_modal_set('EYEDROPPER')
        context.window_manager.modal_handler_add(self)
        self.report({'INFO'}, "Click an object to read its distance; ESC / right-click to cancel.")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        # Re-assert the EYEDROPPER cursor every mouse move: Blender UI widgets
        # (N-Panel sliders / buttons) override the modal cursor on hover, so
        # without this the cursor reverts to default whenever the pointer
        # crosses another N-Panel control. Blender's own ui.eyedropper_* shows
        # the same behavior; no public API suppresses widget hover cursors
        # (see devtalk 10381). Re-setting on every MOUSEMOVE makes the cursor
        # snap back to EYEDROPPER as soon as the pointer keeps moving.
        if event.type == 'MOUSEMOVE':
            context.window.cursor_modal_set('EYEDROPPER')
            return {'PASS_THROUGH'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if self._raycast_and_set_distance(context, event):
                context.window.cursor_modal_restore()
                return {'FINISHED'}
            self.report({'WARNING'}, "No object hit. Click on a mesh, or ESC to cancel.")
            return {'RUNNING_MODAL'}

        if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            context.window.cursor_modal_restore()
            self.report({'INFO'}, "Get Distance from Click: cancelled")
            return {'CANCELLED'}

        return {'PASS_THROUGH'}

    def execute(self, context):
        return {'CANCELLED'}  # modal-only

    def _raycast_and_set_distance(self, context, event) -> bool:
        """Raycast at the click position, compute perpendicular depth, write
        it into `settings.target_distance` and let `_target_distance_update`
        do the rest (move T if Initialize ON, redraw Live plate if OFF).
        Does NOT call `bake_d_plane` — this operator is equivalent to a
        Target Distance slider edit, not an Initialize trigger."""
        from bpy_extras.view3d_utils import region_2d_to_origin_3d, region_2d_to_vector_3d

        region = context.region
        rv3d = context.region_data
        coord = (event.mouse_region_x, event.mouse_region_y)
        ray_origin = region_2d_to_origin_3d(region, rv3d, coord)
        ray_direction = region_2d_to_vector_3d(region, rv3d, coord)

        depsgraph = context.evaluated_depsgraph_get()
        result, location, _normal, _index, _hit_obj, _matrix = context.scene.ray_cast(
            depsgraph, ray_origin, ray_direction
        )
        if not result:
            return False

        cam_obj = context.active_object
        cam = cam_obj.data
        settings = cam.camera_shift_sync

        d = core.perpendicular_depth(cam_obj, location)
        if d <= 0.0:
            self.report({'WARNING'}, "Hit point is behind the camera; ignored.")
            return False

        # Write target_distance without the suppress flag so that
        # _target_distance_update fires and handles T move (Initialize ON)
        # or Live plate redraw (Initialize OFF) — same path as the user
        # editing the Target Distance slider directly.
        settings.target_distance = d
        self.report({'INFO'}, f"target_distance updated: d={d:.3f} m")
        return True


class CAMERA_OT_reinit_d_plane(Operator):
    bl_idname = "camera_shift_sync.reinit_d_plane"
    bl_label = "Re-Initialize D Plane"
    bl_description = (
        "Re-bake the D plane at the camera's current position and orientation. "
        "plate_baked_rotation is refreshed, plate_rotation / plate_location_delta "
        "/ move_delta_* are reset to (0,0,0), and target_distance is preserved"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not _camera_poll(context):
            return False
        return context.active_object.data.camera_shift_sync.is_initialized

    def execute(self, context):
        from . import init_logic
        cam_obj = context.active_object
        settings = cam_obj.data.camera_shift_sync
        warning = init_logic.init_from_target_distance(cam_obj, context.scene, settings)
        if warning is not None:
            self.report({'WARNING'}, warning)
            return {'CANCELLED'}
        self.report({'INFO'}, "D Plane re-initialized")
        return {'FINISHED'}


class CAMERA_OT_reset_plate_style(Operator):
    bl_idname = "camera_shift_sync.reset_plate_style"
    bl_label = "Reset Plate"
    bl_description = (
        "Reset this camera's plate settings (In Front / fill color / edge color "
        "/ edge width) to the defaults configured in AddonPreferences"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        # Plate style is per-camera regardless of projection mode (ORTHO /
        # PANO cameras still have a Plate section in the N-Panel).
        return _camera_obj_poll(context)

    def execute(self, context):
        cam = context.active_object.data
        settings = cam.camera_shift_sync
        prefs_mod.apply_plate_defaults(settings)
        self.report({'INFO'}, "Plate reset to AddonPreferences defaults")
        return {'FINISHED'}


# --- Parked for Function B work ---
#
# CAMERA_OT_pick_d_plane is the legacy "raycast hit point becomes T" picker.
# It's kept as a class definition (so the file diff stays small when we revive
# it) but NOT included in `_classes`, so it isn't registered. Function B may
# want a Picker with different semantics, at which point we revisit.

class CAMERA_OT_pick_d_plane(Operator):
    bl_idname = "camera_shift_sync.pick_d_plane"
    bl_label = "Pick D Plane from Click (parked)"
    bl_description = (
        "Legacy picker (parked): clicks the viewport and takes the hit point "
        "as T directly. Not registered; revived alongside Function B."
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _camera_poll(context)

    def execute(self, context):
        return {'CANCELLED'}


_classes = (
    CAMERA_OT_get_distance_from_click,
    CAMERA_OT_reinit_d_plane,
    CAMERA_OT_reset_plate_style,
)


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
