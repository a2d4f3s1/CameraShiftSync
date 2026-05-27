"""N-Panel UI for Camera Shift Sync.

The Panel class is built dynamically so that the AddonPreferences value for
`panel_category` can be reflected without requiring a restart. See `__init__.py`
for the registration glue.

Function A operates on the camera through two N-Panel sections:

  - **Camera Position**: per-camera `move_delta_*` sliders move the camera;
    the property update callback recomputes shift to keep framing centered.
  - **Plate Transform**: `plane_origin` (Location) and `plate_rotation`
    (Rotation, delta starting at zero) move the D plane itself; the camera
    rigidly follows via the same kind of update callback.

The Rotation slider's reference value (actual world rotation =
`plate_baked_rotation @ plate_rotation`) is shown as a small greyed-out row
underneath, in degrees.
"""
from __future__ import annotations

import math

from bpy.types import Panel
from mathutils import Euler

from . import core


def build_panel(category: str):
    """Create and return a Panel class bound to the given N-panel category."""

    class CAMERA_PT_shift_sync(Panel):
        bl_label = "Camera Shift Sync"
        bl_space_type = 'VIEW_3D'
        bl_region_type = 'UI'
        bl_category = category

        @classmethod
        def poll(cls, context):
            # Show N-Panel for any active camera, regardless of its type.
            # ORTHO / PANO cameras can't really use the linkage, but we keep
            # the panel visible so the user isn't confused by it disappearing
            # — the Initialize row is disabled and the Camera section is
            # greyed out instead. See docs/spec.md > "Panel poll".
            obj = context.active_object
            return obj is not None and obj.type == 'CAMERA'

        def draw(self, context):
            layout = self.layout
            cam_obj = context.active_object
            cam = cam_obj.data
            settings = cam.camera_shift_sync
            is_persp = (cam.type == 'PERSP')

            # --- D Plane (Initialize + Re-Initialize) ---
            # Single-line: Initialize checkbox on the left, Re-Initialize
            # button on the right. Initialize is disabled for non-PERSP
            # cameras; Re-Initialize is enabled only when Initialize is ON.
            box = layout.box()
            row = box.row(align=True)
            init_row = row.row()
            init_row.enabled = is_persp
            init_row.prop(settings, "is_initialized", text="Initialize")
            reinit_row = row.row()
            reinit_row.enabled = is_persp and settings.is_initialized
            reinit_row.operator(
                "camera_shift_sync.reinit_d_plane",
                text="Re-Initialize",
                icon='FILE_REFRESH',
            )

            # --- Camera (always visible box; greyed out when not editable) ---
            # Shift X / Y, Target Distance + spoid, Focal Length.
            #
            # shift_*_proxy is the UI mirror of cam.shift_x/y with extended
            # drag range (±1000); shift_sync keeps both in sync via msgbus.
            # target_distance bidirectionally syncs with move_delta_z (edit
            # one and the other follows). cam.lens (Focal Length) is shown
            # directly so the Blender Camera Properties' Focal Length / FOV
            # display mode (Lens Type) is honored automatically; shift_sync's
            # cam.lens msgbus keeps shift_x/y in line with the new lens.
            #
            # The whole section is greyed out unless PERSP + Initialize ON, so
            # users don't accidentally edit values in a state where the
            # linkage isn't engaged. External edits (Properties Editor /
            # Python) on cam.shift_x/y / cam.lens still go through shift_sync.
            box = layout.box()
            box.enabled = is_persp and settings.is_initialized
            box.label(text="Camera", icon='VIEW_CAMERA')
            col = box.column(align=True)
            col.prop(settings, "shift_x_proxy", text="Shift X")
            col.prop(settings, "shift_y_proxy", text="Shift Y")
            col.separator()
            row = col.row(align=True)
            row.prop(settings, "target_distance", text="Target Distance")
            row.operator(
                "camera_shift_sync.get_distance_from_click",
                text="",
                icon='EYEDROPPER',
            )
            col.separator()
            # Mirror Properties Editor's Lens Unit setting (Millimeters / FOV)
            # so the N-Panel display matches whatever the user has selected
            # there. cam.lens is mm-only; cam.angle is FOV (subtype 'ANGLE'
            # displays in degrees, sensor_fit-aware).
            if cam.lens_unit == 'MILLIMETERS':
                col.prop(cam, "lens", text="Focal Length")
            else:  # 'FOV'
                col.prop(cam, "angle", text="Field of View")

            # --- Camera Position (D plane local) — Initialize-only ---
            # 4 sliders: Delta X / Y / Z (Plate-axis offsets from the
            # Initialize anchor) + Radial Distance (|P|, d-cam line).
            # Editing any one updates cam.location and back-syncs the other
            # representation, so the UI always shows both views consistently.
            # Below the sliders, a small greyed-out row shows
            # Camera_Origine_Position — the camera's current Plate-local
            # absolute coordinates (origin at d) — as a state readout.
            if settings.is_initialized:
                box = layout.box()
                box.label(text="Camera Position", icon='ORIENTATION_VIEW')
                col = box.column(align=True)
                col.prop(settings, "move_delta_x", text="Delta X")
                col.prop(settings, "move_delta_y", text="Delta Y")
                col.prop(settings, "move_delta_z", text="Delta Z")
                col.separator()
                col.prop(settings, "radial_distance", text="Radial Distance")
                # Camera_Origine_Position = current_plate_local; greyed-out
                # absolute Plate-local readout (origin at d).
                origine = core.current_plate_local(cam_obj, cam)
                ref_origine = col.row()
                ref_origine.enabled = False
                ref_origine.label(
                    text=f"[{origine.x:.3f}, {origine.y:.3f}, {origine.z:.3f}]"
                )

            # --- Plate Transform — Initialize-only ---
            # Both Location and Rotation are (0, 0, 0) at Initialize and edit
            # the D plane (with rigid camera follow) in LOCAL axes (baked
            # frame). Below each slider, a small greyed-out row shows the
            # actual world value for reference.
            if settings.is_initialized:
                box = layout.box()
                box.label(text="Plate Transform", icon='EMPTY_AXIS')
                col = box.column(align=True)

                col.prop(settings, "plate_location_delta", text="Location")
                # Reference: actual world T position.
                origin = settings.plane_origin
                ref_loc = col.row()
                ref_loc.enabled = False
                ref_loc.label(
                    text=f"[{origin[0]:.3f}, {origin[1]:.3f}, {origin[2]:.3f}]"
                )

                col.separator()
                col.prop(settings, "plate_rotation", text="Rotation")
                # Reference: actual world rotation in degrees, greyed out.
                # baked rotation is derived from Initialize snapshot via
                # core.compute_plate_baked_rotation (Initialize 後不変).
                baked_mat = core.compute_plate_baked_rotation(cam)
                delta = Euler(tuple(settings.plate_rotation), 'XYZ')
                world_mat = baked_mat @ delta.to_matrix()
                world_eul = world_mat.to_euler('XYZ')
                ref_rot = col.row()
                ref_rot.enabled = False
                ref_rot.label(
                    text=f"[{math.degrees(world_eul.x):.1f}, "
                    f"{math.degrees(world_eul.y):.1f}, "
                    f"{math.degrees(world_eul.z):.1f}]"
                )

            # --- Plate (always visible, collapsible, default closed) ---
            # Header row: collapse arrow (plate_expanded) + label + show_plate
            # toggle. The arrow and the show toggle are independent — collapsing
            # the body doesn't disable rendering; toggling show off doesn't
            # collapse the body.
            box = layout.box()
            header = box.row(align=True)
            header.prop(
                settings, "plate_expanded",
                icon='TRIA_DOWN' if settings.plate_expanded else 'TRIA_RIGHT',
                text="", emboss=False,
            )
            header.label(text="Plate", icon='MESH_PLANE')
            header.prop(settings, "show_plate", text="")
            if settings.plate_expanded:
                sub = box.column(align=True)
                sub.prop(settings, "plate_in_front", text="In Front")
                sub.prop(settings, "plate_fill_color", text="Fill")
                sub.prop(settings, "plate_edge_color", text="Edge")
                sub.prop(settings, "plate_edge_width", text="Edge Width")
                sub.operator(
                    "camera_shift_sync.reset_plate_style",
                    text="Reset Plate",
                    icon='LOOP_BACK',
                )

    return CAMERA_PT_shift_sync
