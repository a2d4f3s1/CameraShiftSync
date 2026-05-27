"""Math utilities for Camera Shift Sync.

Function A keeps the framing center on a world-fixed D plane target `T` while
the user moves the camera. The helpers here solve `shift_x` / `shift_y` from
the current camera position and expose the D plane's local axes in world
space.

See docs/spec.md for the design model and verification items under
「動作確認時の検証項目」.
"""
from __future__ import annotations

from typing import Optional, Tuple

from mathutils import Euler, Matrix, Vector


_EPS = 1e-9


def view_frame_depth(camera_obj, scene) -> float:
    """Return |z| of `Camera.view_frame()` corners.

    Mathematically this equals `lens / fac`, where `fac` is the sensor
    normalization dimension Blender chose internally (HORIZONTAL: sensor_width,
    VERTICAL: sensor_height, AUTO: depends on render aspect vs sensor aspect).

    Using this lets us avoid re-implementing Blender's sensor_fit dispatch in
    our own code; see docs/spec.md > "shift の自動連動".
    """
    corners = camera_obj.data.view_frame(scene=scene)
    return abs(corners[0].z)


def shift_view_target_world(camera_obj, scene, perpendicular_depth_val: float) -> Optional[Vector]:
    """Return T in world space along the camera's current shift view direction
    at the given perpendicular depth.

    Uses `Camera.view_frame()` so the direction reflects shift_x / shift_y and
    sensor_fit / aspect dispatch automatically (Blender computes it). The
    target is placed where the camera-local coordinates are
    `(cx * scale, cy * scale, -perpendicular_depth_val)`, with `scale =
    perpendicular_depth_val / depth_vf` so the local -Z component lands on the
    requested perpendicular depth.

    Returns None if the camera's view_frame depth is degenerate.
    """
    if perpendicular_depth_val <= 0.0:
        return None
    corners = camera_obj.data.view_frame(scene=scene)
    cx = sum(c.x for c in corners) / 4.0
    cy = sum(c.y for c in corners) / 4.0
    cz = sum(c.z for c in corners) / 4.0  # negative under normal projection
    if cz >= 0.0:
        return None
    scale = perpendicular_depth_val / -cz
    T_local = Vector((cx * scale, cy * scale, -perpendicular_depth_val))
    return camera_obj.matrix_world @ T_local


def perpendicular_depth(camera_obj, point_world: Vector) -> float:
    """Return the camera-relative perpendicular depth of a world-space point.

        d = -(camera_obj.matrix_world.inverted() @ point_world).z

    Used by the D plane plate sizing and by the eyedropper depth assignment
    (avoids the "edge-of-frame inflates d" issue).
    """
    local = camera_obj.matrix_world.inverted() @ Vector(point_world)
    return -local.z


def is_target_in_front(camera_obj, target_world: Vector, eps: float = 1e-6) -> bool:
    """True if `target_world` is on the camera's -Z side (inside the viewing
    half-space). Used by Camera Shift Move to reject moves that would push the
    camera through the D plane.
    """
    local = camera_obj.matrix_world.inverted() @ Vector(target_world)
    return local.z < -eps


def shift_from_target(camera_obj, target_world: Vector, scene) -> Tuple[Optional[float], Optional[float]]:
    """Solve shift_x / shift_y so the framing center lands on `target_world`.

        V_local = camera_obj.matrix_world.inverted() @ target_world
        depth   = view_frame_depth(camera_obj, scene)
        shift_x = (V_local.x / -V_local.z) * depth
        shift_y = (V_local.y / -V_local.z) * depth

    Returns (None, None) if the target is behind the camera (V_local.z >= 0)
    so callers can clamp / reject the move.

    Verified by tools/verify_shift_unit.py (Test 1–4) for landscape and
    portrait sensors across HORIZONTAL / VERTICAL / AUTO and various render
    aspects.
    """
    V_local = camera_obj.matrix_world.inverted() @ Vector(target_world)
    if V_local.z >= 0:
        return None, None
    depth = view_frame_depth(camera_obj, scene)
    shift_x = (V_local.x / -V_local.z) * depth
    shift_y = (V_local.y / -V_local.z) * depth
    return shift_x, shift_y


def compute_init_plate_local(camera_data) -> Vector:
    """Camera's Plate-local position **at the Initialize anchor** —
    `plate_rot.inverted() @ (init_cam_location - plane_origin)`.

    The anchor is `init_cam_location`, which is the camera's world position
    snapshotted at Initialize and **rigid-followed by Plate Transform edits**
    (so the anchor moves with the D plane under Plate Location / Rotation,
    but stays still under Shift / Target Distance / Camera Position edits).
    This is the role formerly held by `base_camera_location`.

    `Delta XYZ` slider = `current_P - init_plate_local`, and the formula
    cancels the current `plane_origin` (T) on both sides, so Delta XYZ is
    invariant under operations that only move T (Shift / Target Distance)
    and operations that translate / rotate cam and T together (Plate
    Transform).

    Does NOT use `target_distance` or `view_frame()` — earlier 3a version
    that did caused a quadratic blow-up of Delta Z under target_distance
    auto-sync (see docs/spec.md > 2026-05-27 design note).
    """
    settings = camera_data.camera_shift_sync
    plate_rot = plate_world_rotation_matrix(camera_data)
    return plate_rot.inverted() @ (
        Vector(settings.init_cam_location) - Vector(settings.plane_origin)
    )


def current_plate_local(cam_obj, camera_data) -> Vector:
    """Camera's current Plate-local position —
    `plate_rot.inverted() @ (cam.location - plane_origin)`. Used by
    Camera Position update callbacks to read the camera's current state
    in Plate axes before computing the new state."""
    settings = camera_data.camera_shift_sync
    plate_rot = plate_world_rotation_matrix(camera_data)
    return plate_rot.inverted() @ (
        Vector(cam_obj.location) - Vector(settings.plane_origin)
    )


def compute_plate_baked_rotation(camera_data) -> Matrix:
    """Return the D plane's baked rotation as a 3x3 Matrix, derived from
    Initialize snapshot's `init_cam_rotation` (XYZ Euler).

    This is the world rotation of the camera at the moment Initialize was
    called. Used as the baseline for the D plane's effective rotation
    (effective = baked @ plate_rotation_delta). Initialize 後は不変なので
    snapshot から都度導出する派生計算で source 一元化する。
    """
    settings = camera_data.camera_shift_sync
    return Euler(tuple(settings.init_cam_rotation), 'XYZ').to_matrix()


def plate_world_rotation_matrix(camera_data) -> Matrix:
    """Return the D plane's effective world-space rotation as a 3x3 Matrix.

    The D plane's orientation is the composition of:

      - `plate_baked_rotation`: world rotation derived from Initialize
        snapshot's `init_cam_rotation` (via `compute_plate_baked_rotation`).
        Initialize 後は不変。
      - `plate_rotation`: user-editable delta on top of that, exposed as the
        Plate Transform Rotation slider. Starts at (0, 0, 0) on every
        Initialize so the editable value reads as zero in the UI.

    The effective orientation is `baked @ delta`. Both use the 'XYZ' Euler
    convention.
    """
    settings = camera_data.camera_shift_sync
    baked = compute_plate_baked_rotation(camera_data)
    delta = Euler(tuple(settings.plate_rotation), 'XYZ').to_matrix()
    return baked @ delta


def plane_axes_from_rotation(camera_data) -> Tuple[Vector, Vector, Vector]:
    """Return (X_world, Y_world, normal_world) basis vectors for the D plane,
    derived from `plate_baked_rotation` and `plate_rotation`:

        normal_world = column 2 of the effective rotation matrix (local +Z)
        Y_world      = column 1 of the effective rotation matrix (local +Y)
        X_world      = Y × normal

    Used by Camera Position (move axes), Plate Transform (rotation/location
    apply), and the plate draw handler.
    """
    rot = plate_world_rotation_matrix(camera_data)
    # mathutils.Matrix columns: m.col[0]=X axis, [1]=Y axis, [2]=Z axis
    x_local = Vector((1.0, 0.0, 0.0))
    y_local = Vector((0.0, 1.0, 0.0))
    z_local = Vector((0.0, 0.0, 1.0))
    n = (rot @ z_local).normalized()
    y = (rot @ y_local).normalized()
    x = (rot @ x_local).normalized()
    return x, y, n


def plane_local_axes_world(camera_data) -> Tuple[Vector, Vector, Vector]:
    """Backwards-compatible alias for `plane_axes_from_rotation`. New code
    should call `plane_axes_from_rotation` directly; this name is kept so
    existing call sites (Camera Position update callback, plate draw) keep
    working without rename.
    """
    return plane_axes_from_rotation(camera_data)


