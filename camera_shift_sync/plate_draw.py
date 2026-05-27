"""GPU draw handler for the D plane plate overlay in the 3D Viewport.

The plate is drawn whenever the active perspective camera has
`show_plate = True`, regardless of `is_initialized`. Two modes:

  - **Baked mode** (`is_initialized = True`): T is the stored `plane_origin`
    (world-fixed); axes come from `plate_baked_rotation @ plate_rotation`.
    The plate stays put in world space while the camera moves via Camera
    Position deltas.

  - **Live mode** (`is_initialized = False`): T is computed each frame from
    the camera's current shift view direction at `target_distance`; axes
    come from the camera's current `matrix_world`. The plate follows the
    camera as it moves, giving a real-time preview without committing to a
    baked D plane. Pressing Initialize freezes this live view.

The per-camera `plate_in_front` flag selects between always-on-top drawing
(no depth test) and depth-occluded drawing (`LESS_EQUAL` against the scene
depth buffer); depth_mask is off in both modes so the translucent plate
never writes Z and clobbers anything drawn after it.

The draw handler is registered once at addon register and removed at
unregister. The per-camera `show_plate` flag is read inside the callback (so
toggling it on a different camera doesn't disturb the handler lifecycle); its
update callback just triggers a viewport redraw via `area.tag_redraw()`.

Plate geometry:
  - Center: T (world).
  - Axes:   D plane X / Y in world (from `core.plane_axes_from_rotation` in
            baked mode, from `cam_obj.matrix_world` in live mode).
  - Size:   half-extents derived from Camera.view_frame() at its reported
            depth, then scaled by (d / depth_view_frame) where d is the
            perpendicular depth from the camera to T. This correctly tracks
            Focal Length / sensor_fit / aspect changes (Blender already
            embeds them into view_frame).
"""
from __future__ import annotations

from typing import Optional, Tuple

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

from . import core


_draw_handle = None  # opaque handle returned by draw_handler_add


def _live_axes(cam_obj) -> Tuple[Vector, Vector, Vector]:
    """Axes from the camera's current world rotation, for live-mode plate
    drawing when no D plane has been baked yet."""
    mw3 = cam_obj.matrix_world.to_3x3()
    x = (mw3 @ Vector((1.0, 0.0, 0.0))).normalized()
    y = (mw3 @ Vector((0.0, 1.0, 0.0))).normalized()
    n = (mw3 @ Vector((0.0, 0.0, 1.0))).normalized()
    return x, y, n


def _compute_plate_corners(cam_obj, scene, T_world: Vector, axes: Tuple[Vector, Vector, Vector]) -> Optional[Tuple[Vector, Vector, Vector, Vector]]:
    """Compute the 4 plate corners in world space (CCW order), or None if the
    target is not in front of the camera (perpendicular depth <= 0)."""
    cam = cam_obj.data

    corners_local = cam.view_frame(scene=scene)
    xs = [c.x for c in corners_local]
    ys = [c.y for c in corners_local]
    half_w_at_vf = (max(xs) - min(xs)) / 2.0
    half_h_at_vf = (max(ys) - min(ys)) / 2.0
    depth_vf = abs(corners_local[0].z)
    if depth_vf <= 0.0:
        return None

    d = core.perpendicular_depth(cam_obj, T_world)
    if d <= 0.0:
        return None

    scale = d / depth_vf
    half_w = half_w_at_vf * scale
    half_h = half_h_at_vf * scale

    x_axis_w, y_axis_w, _n = axes

    # CCW from top-right when viewed from the camera side
    return (
        T_world + half_w * x_axis_w + half_h * y_axis_w,  # 0 TR
        T_world - half_w * x_axis_w + half_h * y_axis_w,  # 1 TL
        T_world - half_w * x_axis_w - half_h * y_axis_w,  # 2 BL
        T_world + half_w * x_axis_w - half_h * y_axis_w,  # 3 BR
    )


def _draw_callback():
    """SpaceView3D POST_VIEW callback: draw the active camera's D plane plate.

    Two modes:
      - Baked (is_initialized=True): T and axes from stored plate state.
      - Live (is_initialized=False): T and axes computed from the camera's
        current matrix_world and target_distance, so the user can preview
        the plate before committing with Initialize.
    """
    context = bpy.context
    obj = context.active_object
    if obj is None or obj.type != 'CAMERA':
        return
    cam = obj.data
    if cam.type != 'PERSP':
        return
    settings = cam.camera_shift_sync
    if not settings.show_plate:
        return

    if settings.is_initialized:
        T = Vector(settings.plane_origin)
        axes = core.plane_axes_from_rotation(cam)
    else:
        # Live mode: T from current shift view direction at target_distance,
        # axes from the camera's current world rotation.
        T = core.shift_view_target_world(obj, context.scene, settings.target_distance)
        if T is None:
            return
        axes = _live_axes(obj)

    corners = _compute_plate_corners(obj, context.scene, T, axes)
    if corners is None:
        return

    fill_color = tuple(settings.plate_fill_color)
    edge_color = tuple(settings.plate_edge_color)
    edge_width = float(settings.plate_edge_width)
    in_front = bool(settings.plate_in_front)

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')

    # Depth state:
    #   in_front=True  -> no depth test, plate draws on top
    #   in_front=False -> LESS_EQUAL test so scene geometry occludes the plate.
    #                     depth_mask off because the plate is translucent — a
    #                     fully drawn quad shouldn't write Z and clip whatever
    #                     gets drawn after it in the same POST_VIEW pass.
    if in_front:
        gpu.state.depth_test_set('NONE')
        gpu.state.depth_mask_set(False)
    else:
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.depth_mask_set(False)

    try:
        # --- Translucent fill (two triangles) ---
        if fill_color[3] > 0.0:
            fill_coords = [
                corners[0], corners[1], corners[2],
                corners[0], corners[2], corners[3],
            ]
            fill_batch = batch_for_shader(shader, 'TRIS', {"pos": fill_coords})
            gpu.state.blend_set('ALPHA')
            shader.uniform_float("color", fill_color)
            fill_batch.draw(shader)
            gpu.state.blend_set('NONE')

        # --- Edge (line loop) ---
        if edge_color[3] > 0.0 and edge_width > 0.0:
            edge_coords = list(corners) + [corners[0]]
            edge_batch = batch_for_shader(shader, 'LINE_STRIP', {"pos": edge_coords})
            gpu.state.blend_set('ALPHA')
            gpu.state.line_width_set(edge_width)
            shader.uniform_float("color", edge_color)
            edge_batch.draw(shader)
            gpu.state.line_width_set(1.0)
            gpu.state.blend_set('NONE')
    finally:
        # Reset to Blender's overlay defaults so subsequent POST_VIEW handlers
        # don't inherit our state.
        gpu.state.depth_test_set('NONE')
        gpu.state.depth_mask_set(False)


def trigger_redraw():
    """Tag all 3D Viewport areas for redraw; used by plate-related property updates."""
    wm = bpy.context.window_manager
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def register() -> None:
    global _draw_handle
    if _draw_handle is None:
        _draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_callback, (), 'WINDOW', 'POST_VIEW'
        )


def unregister() -> None:
    global _draw_handle
    if _draw_handle is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, 'WINDOW')
        except (ValueError, RuntimeError):
            pass
        _draw_handle = None
