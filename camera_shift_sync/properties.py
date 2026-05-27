"""Per-camera settings stored on bpy.types.Camera via PointerProperty.

The D plane is the addon's world-fixed invariant target with two operations:

  - **Camera Position**: child-only motion. `move_delta_x/y/z` (Plate-axis
    offsets from the Initialize anchor) and `radial_distance` (d-cam line
    distance) sliders move the camera; D plane is unchanged; `shift_x/y`
    recomputes to keep framing on T. The two slider sets are bidirectionally
    synced views of the camera's Plate-local position P.
  - **Plate Transform**: parent motion. `plane_origin` (Location) and
    `plate_rotation` (Rotation) edits move the D plane itself; the camera
    rigidly follows so the camera-to-plane relationship is unchanged
    (`shift_x/y` unchanged, camera rotation follows in world space).

The D plane's orientation is split in two Euler triples:

  - `plate_baked_rotation`: world rotation derived from `init_cam_rotation`
    (the camera's world rotation snapshotted at Initialize). Computed via
    `core.compute_plate_baked_rotation`; not stored as an independent
    property. Initialize 後不変。Shown as a small greyed-out reference under
    the Rotation slider.
  - `plate_rotation`: user-editable delta on top of `plate_baked_rotation`.
    Starts at (0, 0, 0) on every Initialize so the slider always reads zero
    at the moment of init. Effective orientation = baked @ delta.

`init_cam_location` is the rigid anchor for Camera Position (replaces the
old `base_camera_location`). It stores cam.location at Initialize time and
is rigid-followed by Plate Transform edits (so the Delta XYZ readout stays
invariant under Plate Transform).

`target_distance` is the addon's own perpendicular-depth property. It is the
restore seed for Initialize (Initialize checkbox ON computes T from this
value) and is also kept in sync with the live `d` while the camera moves —
edits to it move T, edits to the camera (via Move sliders) update its
displayed value via a suppress-guarded write. It is intentionally independent
from Blender's `cam.dof.focus_*`.

Plate appearance (in_front, fill color, edge color, edge width) is per-camera
too, so artists can dial it in for each shot; defaults live in
AddonPreferences and are pushed back into a camera's settings only by the
explicit Reset Plate button.
"""
from __future__ import annotations

import bpy
from bpy.props import BoolProperty, FloatProperty, FloatVectorProperty, PointerProperty
from bpy.types import PropertyGroup
from mathutils import Euler, Matrix, Vector


_shift_proxy_suppress = False


def set_shift_proxy_suppress(value: bool) -> None:
    """Used by shift_sync.py: while it copies cam.shift_x/y back into the proxy,
    set this True so the proxy's own update callback doesn't fire and write to
    cam.shift_x/y again (which would re-fire the msgbus, ping-pong)."""
    global _shift_proxy_suppress
    _shift_proxy_suppress = value


_is_initialized_suppress = False


def set_is_initialized_suppress(value: bool) -> None:
    """Used by init_logic.bake_d_plane and external_edit_watch.clear_initialize
    while they write `is_initialized`. The flag has an update callback that
    runs the user-tick path (init_from_dof / clear) — without this guard the
    internal writes would recurse back into themselves."""
    global _is_initialized_suppress
    _is_initialized_suppress = value


def _is_initialized_update(self, context):
    """User-tick path for the Initialize checkbox in the N-Panel.

    True  -> run Initialize from target_distance (compute T along the
             current shift view direction at target_distance, then bake).
    False -> clear the Initialize state (target_distance is kept).

    Internal writes from bake_d_plane / clear_initialize set the suppress
    flag, so this callback only runs when the user toggles the checkbox or
    when something else writes the flag from outside the addon (rare)."""
    if _is_initialized_suppress:
        return
    cam_data = self.id_data
    cam_obj = context.active_object
    if cam_obj is None or cam_obj.type != 'CAMERA' or cam_obj.data is not cam_data:
        cam_obj = _find_camera_object(cam_data)
        if cam_obj is None:
            return

    if self.is_initialized:
        # User just ticked Initialize on. Run init_from_target_distance;
        # bake_d_plane will re-write is_initialized=True under suppression
        # (no recursion).
        from . import init_logic
        warning = init_logic.init_from_target_distance(cam_obj, context.scene, self)
        if warning is not None:
            # The bake didn't happen; undo the user-visible flip back to off.
            set_is_initialized_suppress(True)
            try:
                self.is_initialized = False
            finally:
                set_is_initialized_suppress(False)
    else:
        # User just unticked Initialize. clear_initialize handles the rest.
        from . import external_edit_watch
        external_edit_watch.clear_initialize(cam_data)


_target_distance_suppress = False


def set_target_distance_suppress(value: bool) -> None:
    """Used by _apply_camera_position / bake_d_plane while they write
    `target_distance` to keep it in sync with the live `d`. Without this
    guard, the user-edit path (recompute T) would fire on every camera
    movement and create a feedback loop."""
    global _target_distance_suppress
    _target_distance_suppress = value


def _target_distance_update(self, context):
    """User edited `target_distance` via the N-Panel slider. Move T (the D
    plane origin) along the camera's current shift view direction at the
    new distance. Camera location / shift / Delta XYZ are unchanged (T-
    cancellation in `Delta = current_P - init_PL` preserves Delta when T
    moves). Radial Distance DOES change because the camera-to-d distance
    changes; we auto-sync the slider after writing T.

    In live mode (is_initialized=False) there's no baked T to move, but the
    live plate computes its T from target_distance each frame, so we trigger
    a viewport redraw so the live plate follows the new value."""
    if _target_distance_suppress:
        return
    if not self.is_initialized:
        # Live mode: nudge the viewport so the live plate follows the new
        # distance. shift / camera position are untouched.
        if self.show_plate:
            from . import plate_draw
            plate_draw.trigger_redraw()
        return

    cam_data = self.id_data
    cam_obj = context.active_object
    if cam_obj is None or cam_obj.type != 'CAMERA' or cam_obj.data is not cam_data:
        cam_obj = _find_camera_object(cam_data)
        if cam_obj is None:
            return

    from . import core, shift_sync, plate_draw
    T_world = core.shift_view_target_world(cam_obj, context.scene, self.target_distance)
    if T_world is None:
        return
    # Suppress:
    #   - shift_sync msgbus so the plane_origin write doesn't cascade back
    #     through cam.shift_x/y -> _on_camera_shift_changed -> plane_origin.
    #   - Plate Transform update so the plane_origin write isn't mistaken
    #     for a user Plate Transform Location edit (which would rigid-
    #     translate the camera).
    shift_sync.set_suppressed(True)
    set_plate_transform_suppress(True)
    try:
        self.plane_origin = T_world
        # Re-seed Plate Transform baseline so subsequent Location edits
        # compute delta from this new T, not the pre-distance one.
        seed_plate_transform_baseline(cam_data)
    finally:
        set_plate_transform_suppress(False)
        shift_sync.set_suppressed(False)
    # Auto-sync radial_distance: T moved but cam stayed, so |current_P|
    # (= camera-to-d distance) changed. Without this sync, the Radial
    # Distance slider keeps showing the pre-edit value, and the next user
    # touch of it would jump the camera by the desync amount.
    new_radial = core.current_plate_local(cam_obj, cam_data).length
    if new_radial > 0.0 and abs(self.radial_distance - new_radial) > 1.0e-7:
        set_move_position_suppress(True)
        try:
            self.radial_distance = new_radial
        finally:
            set_move_position_suppress(False)
    plate_draw.trigger_redraw()


_plate_transform_suppress = False


def set_plate_transform_suppress(value: bool) -> None:
    """Used by init_logic.bake_d_plane / external_edit_watch / Plate Transform
    update callbacks themselves when they need to write
    `plate_location_delta`, `plate_baked_rotation`, or `plate_rotation`
    without triggering the rigid-follow update logic. Without this guard,
    internal writes (Initialize bake, restore-from-snapshot, the secondary
    update for the *other* axis of the same edit) would re-enter the update
    callback in a loop."""
    global _plate_transform_suppress
    _plate_transform_suppress = value


# Module-local "last applied" snapshots, per camera-data name_full. We need
# these because Blender's property update callback only delivers the new value;
# computing the delta requires us to remember what the old value was. Updated
# at the end of each non-suppressed update, and at bake / restore time via
# `seed_plate_transform_baseline`.
_plate_location_delta_baseline: dict[str, tuple[float, float, float]] = {}
_plate_rotation_baseline: dict[str, tuple[float, float, float]] = {}


def seed_plate_transform_baseline(cam_data) -> None:
    """Record the current `plate_location_delta` / `plate_rotation` as the
    baseline for delta computation. Call after any internal write that should
    NOT be interpreted as a user edit (Initialize bake, save-time restore)."""
    settings = cam_data.camera_shift_sync
    _plate_location_delta_baseline[cam_data.name_full] = tuple(settings.plate_location_delta)
    _plate_rotation_baseline[cam_data.name_full] = tuple(settings.plate_rotation)


def forget_plate_transform_baseline(cam_data_name_full: str) -> None:
    _plate_location_delta_baseline.pop(cam_data_name_full, None)
    _plate_rotation_baseline.pop(cam_data_name_full, None)


def _plate_location_delta_update(self, context):
    """User edited Plate Transform Location. The slider value is a delta in
    D-plane LOCAL axes (baked frame). Convert to world:

        delta_world = baked_rotation_matrix @ (new_local - old_local)

    and translate the camera + Move base + T (plane_origin) by that world
    delta. Camera rotation is unchanged because we're only translating; shift
    is also unchanged because the camera-to-plane offset is preserved."""
    if _plate_transform_suppress:
        return
    if not self.is_initialized:
        # Pre-Initialize state: just a stored value with no rig.
        _plate_location_delta_baseline[self.id_data.name_full] = tuple(self.plate_location_delta)
        return

    cam_data = self.id_data
    cam_obj = context.active_object
    if cam_obj is None or cam_obj.type != 'CAMERA' or cam_obj.data is not cam_data:
        cam_obj = _find_camera_object(cam_data)
        if cam_obj is None:
            return

    old = _plate_location_delta_baseline.get(cam_data.name_full)
    new = tuple(self.plate_location_delta)
    if old is None:
        _plate_location_delta_baseline[cam_data.name_full] = new
        return
    delta_local = Vector((new[0] - old[0], new[1] - old[1], new[2] - old[2]))
    if delta_local.length_squared == 0.0:
        return

    from . import core, shift_sync, external_edit_watch, plate_draw
    baked_mat = core.compute_plate_baked_rotation(cam_data)
    delta_world = baked_mat @ delta_local
    # Suppress shift_sync so the plane_origin write doesn't trigger T-follow
    # via the msgbus, and Plate Transform suppress so the plane_origin write
    # below doesn't recurse here.
    shift_sync.set_suppressed(True)
    set_plate_transform_suppress(True)
    try:
        cam_obj.location = Vector(cam_obj.location) + delta_world
        external_edit_watch.remember_internal_transform(cam_obj)
        # init_cam_location rigid-follows Plate Transform Location so that
        # `init_plate_local = plate_rot.inv @ (init_cam - T)` is invariant
        # under this edit (cam and T both shift by delta_world; init_cam
        # must shift too for the Delta XYZ readout to stay unchanged).
        self.init_cam_location = Vector(self.init_cam_location) + delta_world
        self.plane_origin = Vector(self.plane_origin) + delta_world
        context.view_layer.update()
    finally:
        set_plate_transform_suppress(False)
        shift_sync.set_suppressed(False)
    _plate_location_delta_baseline[cam_data.name_full] = new
    plate_draw.trigger_redraw()


def _plate_rotation_update(self, context):
    """User edited the Plate Transform Rotation (delta). Rotate the camera
    (position + rotation) rigidly around T so the camera-to-plane relationship
    is preserved. Shift_x/y unchanged.

    World-space rotation delta is computed directly from the old and new
    world rotations of the plate:

        old_plate_world_rot = baked @ Euler(old).to_matrix()
        new_plate_world_rot = baked @ Euler(new).to_matrix()
        delta_rot_world     = new_plate_world_rot @ old_plate_world_rot.inverted()
                            = baked @ new @ old.inverted() @ baked.inverted()

    This is the order that preserves `current_P = plate_rot.inv @ (cam - T)`
    across the edit (rigid follow). The previous form (`baked @ old.inv @
    new @ baked.inv`) is mathematically different due to non-commutativity
    of Euler rotations and caused multi-axis Plate Rotation edits to drift
    the rigid-follow invariant (論点 Y, resolved 2026-05-27).

        cam.location         = T + delta_rot_world @ (cam.location - T)
        init_cam_location    = T + delta_rot_world @ (init_cam_location - T)
        cam.matrix_basis 3x3 = delta_rot_world @ old_3x3   (via matrix_basis to
                                                            be rotation_mode-agnostic)
    """
    if _plate_transform_suppress:
        return
    if not self.is_initialized:
        _plate_rotation_baseline[self.id_data.name_full] = tuple(self.plate_rotation)
        return

    cam_data = self.id_data
    cam_obj = context.active_object
    if cam_obj is None or cam_obj.type != 'CAMERA' or cam_obj.data is not cam_data:
        cam_obj = _find_camera_object(cam_data)
        if cam_obj is None:
            return

    old = _plate_rotation_baseline.get(cam_data.name_full)
    new = tuple(self.plate_rotation)
    if old is None:
        _plate_rotation_baseline[cam_data.name_full] = new
        return
    if old == new:
        return

    from . import core, shift_sync, external_edit_watch, plate_draw
    baked_mat = core.compute_plate_baked_rotation(cam_data)
    old_world_rot = baked_mat @ Euler(old, 'XYZ').to_matrix()
    new_world_rot = baked_mat @ Euler(new, 'XYZ').to_matrix()
    delta_world = new_world_rot @ old_world_rot.inverted()

    T = Vector(self.plane_origin)
    cam_loc = Vector(cam_obj.location)
    init_cam_loc = Vector(self.init_cam_location)
    shift_sync.set_suppressed(True)
    try:
        # Rotate camera location around T
        new_cam_loc = T + (delta_world @ (cam_loc - T))
        # Rotate camera orientation via matrix_basis (rotation_mode-agnostic).
        # matrix_basis = T(loc) @ R @ S; we need to replace the rotation part
        # with delta_world @ old_R, while preserving scale.
        old_basis = cam_obj.matrix_basis.copy()
        old_loc, old_rot_quat, old_scale = old_basis.decompose()
        old_rot_mat = old_rot_quat.to_matrix()
        new_rot_mat = delta_world @ old_rot_mat
        new_basis = (
            Matrix.Translation(new_cam_loc)
            @ new_rot_mat.to_4x4()
            @ Matrix.Diagonal(old_scale).to_4x4()
        )
        cam_obj.matrix_basis = new_basis
        external_edit_watch.remember_internal_transform(cam_obj)

        # init_cam_location rigid-follows Plate Transform Rotation around T,
        # mirroring the camera's own rotation around T. This keeps
        # `init_plate_local = plate_rot.inv @ (init_cam - T)` invariant under
        # Plate Rotation (the rotation matrix change in plate_rot cancels the
        # rotation of (init_cam - T) by delta_world).
        self.init_cam_location = T + (delta_world @ (init_cam_loc - T))

        context.view_layer.update()
    finally:
        shift_sync.set_suppressed(False)
    _plate_rotation_baseline[cam_data.name_full] = new
    plate_draw.trigger_redraw()


def _shift_x_proxy_update(self, context):
    """Proxy is a UI mirror; the source of truth is cam.shift_x. Edits to the
    proxy write through to cam.shift_x, then the shift_sync msgbus callback
    handles the consequences (T move + back-sync of the proxy)."""
    if _shift_proxy_suppress:
        return
    cam_data = self.id_data
    if cam_data.shift_x != self.shift_x_proxy:
        cam_data.shift_x = self.shift_x_proxy


def _shift_y_proxy_update(self, context):
    if _shift_proxy_suppress:
        return
    cam_data = self.id_data
    if cam_data.shift_y != self.shift_y_proxy:
        cam_data.shift_y = self.shift_y_proxy


def _show_plate_update(self, context):
    """Toggle visibility by redrawing the 3D Viewport; the plate draw handler
    itself reads show_plate per draw and skips when False, so we just need to
    invalidate the view."""
    from . import plate_draw
    plate_draw.trigger_redraw()


def _plate_style_update(self, context):
    """Color / width changes only need a redraw; the draw handler reads the
    values on every frame."""
    from . import plate_draw
    plate_draw.trigger_redraw()


def _find_camera_object(cam_data):
    """Find any object that uses this Camera data block. Returns None if
    none. Used as a fallback in update callbacks when context.active_object
    doesn't match (e.g. property edited via Python from a different context)."""
    for obj in bpy.data.objects:
        if obj.type == 'CAMERA' and obj.data == cam_data:
            return obj
    return None


_move_position_suppress = False


def set_move_position_suppress(value: bool) -> None:
    """While back-syncing Delta XYZ <-> Radial Distance (each is a derived
    view of the same camera Plate-local position P; editing one writes the
    other back as the new state), set this True so the back-write doesn't
    re-enter the originating callback in a loop."""
    global _move_position_suppress
    _move_position_suppress = value


def _apply_camera_position(self, context, cam_obj, new_P, sync_target: str) -> None:
    """Shared body of the Camera Position callbacks.

    Given a target Plate-local position `new_P`, write `cam.location =
    T + plate_rot @ new_P`, recompute shift_x/y to keep framing on T,
    auto-sync target_distance, and back-sync the OTHER UI representation:

      - `sync_target='radial'`: writes `radial_distance = |actual_P|`
      - `sync_target='delta'` : writes `move_delta_x/y/z = actual_P - init_PL`

    `_move_position_suppress` is set around the back-sync writes so the
    OTHER callback skips out (it would otherwise re-enter and ping-pong).

    Clamp: if the new position would put T behind the camera, snap the
    camera back to the Initialize anchor state and zero the UI sliders."""
    from . import core, shift_sync, external_edit_watch

    cam_data = cam_obj.data
    scene = context.scene
    T = Vector(self.plane_origin)
    plate_rot = core.plate_world_rotation_matrix(cam_data)
    init_PL = core.compute_init_plate_local(cam_data)

    P_world = T + plate_rot @ new_P

    shift_sync.set_suppressed(True)
    try:
        cam_obj.location = P_world
        external_edit_watch.remember_internal_transform(cam_obj)
        context.view_layer.update()

        if not core.is_target_in_front(cam_obj, T):
            # Clamp fallback: snap the camera back to the Initialize anchor.
            P_init_world = T + plate_rot @ init_PL
            cam_obj.location = P_init_world
            external_edit_watch.remember_internal_transform(cam_obj)
            context.view_layer.update()
            # Reset the sliders so the UI matches the snapped position.
            set_move_position_suppress(True)
            try:
                self.move_delta_x = 0.0
                self.move_delta_y = 0.0
                self.move_delta_z = 0.0
                self.radial_distance = init_PL.length
            finally:
                set_move_position_suppress(False)
            return

        shift_x, shift_y = core.shift_from_target(cam_obj, T, scene)
        if shift_x is None:
            P_init_world = T + plate_rot @ init_PL
            cam_obj.location = P_init_world
            external_edit_watch.remember_internal_transform(cam_obj)
            context.view_layer.update()
            return

        cam_data.shift_x = shift_x
        cam_data.shift_y = shift_y

        # Re-read the actual Plate-local position (may differ from new_P if
        # Blender clamped any of the writes) and back-sync the OTHER UI
        # representation accordingly.
        actual_P = core.current_plate_local(cam_obj, cam_data)
        set_move_position_suppress(True)
        try:
            if sync_target == 'radial':
                new_radial = actual_P.length
                if abs(self.radial_distance - new_radial) > 1.0e-7:
                    self.radial_distance = new_radial
            elif sync_target == 'delta':
                ax = actual_P.x - init_PL.x
                ay = actual_P.y - init_PL.y
                az = actual_P.z - init_PL.z
                if abs(self.move_delta_x - ax) > 1.0e-7:
                    self.move_delta_x = ax
                if abs(self.move_delta_y - ay) > 1.0e-7:
                    self.move_delta_y = ay
                if abs(self.move_delta_z - az) > 1.0e-7:
                    self.move_delta_z = az
        finally:
            set_move_position_suppress(False)

        # Auto-sync target_distance to the current perpendicular depth so
        # the Camera section's Target Distance slider tracks the live `d`.
        # Suppressed so this write does NOT fire _target_distance_update
        # (which would move T and create a feedback loop).
        new_d = core.perpendicular_depth(cam_obj, T)
        if new_d > 0.0 and abs(self.target_distance - new_d) > 1.0e-7:
            set_target_distance_suppress(True)
            try:
                self.target_distance = new_d
            finally:
                set_target_distance_suppress(False)
    finally:
        shift_sync.set_suppressed(False)


def _move_delta_x_update(self, context):
    """Delta X slider edit. The X component of the camera's Plate-local
    position becomes `init_plate_local.x + move_delta_x`; Y / Z are kept
    at their current Plate-local values (pure Plate-X translation, no
    spillover into Y / Z). Radial Distance back-syncs."""
    if _move_position_suppress:
        return
    if not self.is_initialized:
        return
    cam_data = self.id_data
    cam_obj = context.active_object
    if cam_obj is None or cam_obj.type != 'CAMERA' or cam_obj.data is not cam_data:
        cam_obj = _find_camera_object(cam_data)
        if cam_obj is None:
            return
    from . import core
    init_PL = core.compute_init_plate_local(cam_data)
    current_P = core.current_plate_local(cam_obj, cam_data)
    new_P = Vector((init_PL.x + self.move_delta_x, current_P.y, current_P.z))
    _apply_camera_position(self, context, cam_obj, new_P, sync_target='radial')


def _move_delta_y_update(self, context):
    """Delta Y slider edit. Plate-Y axis translation. See _move_delta_x_update."""
    if _move_position_suppress:
        return
    if not self.is_initialized:
        return
    cam_data = self.id_data
    cam_obj = context.active_object
    if cam_obj is None or cam_obj.type != 'CAMERA' or cam_obj.data is not cam_data:
        cam_obj = _find_camera_object(cam_data)
        if cam_obj is None:
            return
    from . import core
    init_PL = core.compute_init_plate_local(cam_data)
    current_P = core.current_plate_local(cam_obj, cam_data)
    new_P = Vector((current_P.x, init_PL.y + self.move_delta_y, current_P.z))
    _apply_camera_position(self, context, cam_obj, new_P, sync_target='radial')


def _move_delta_z_update(self, context):
    """Delta Z slider edit. Plate-Z axis translation (NOT radial — radial
    movement is the Radial Distance slider). See _move_delta_x_update."""
    if _move_position_suppress:
        return
    if not self.is_initialized:
        return
    cam_data = self.id_data
    cam_obj = context.active_object
    if cam_obj is None or cam_obj.type != 'CAMERA' or cam_obj.data is not cam_data:
        cam_obj = _find_camera_object(cam_data)
        if cam_obj is None:
            return
    from . import core
    init_PL = core.compute_init_plate_local(cam_data)
    current_P = core.current_plate_local(cam_obj, cam_data)
    new_P = Vector((current_P.x, current_P.y, init_PL.z + self.move_delta_z))
    _apply_camera_position(self, context, cam_obj, new_P, sync_target='radial')


def _radial_distance_update(self, context):
    """Radial Distance slider edit. Scales the camera's Plate-local position
    along the current d-cam direction (`current_P / |current_P|`) so that
    `|new_P| = radial_distance`. Delta XYZ back-syncs.

    Degenerate case: if `|current_P|` is near zero (camera at d), the radial
    direction is undefined; we fall back to the Initialize anchor's direction
    so the radial slider still has a well-defined effect."""
    if _move_position_suppress:
        return
    if not self.is_initialized:
        return
    cam_data = self.id_data
    cam_obj = context.active_object
    if cam_obj is None or cam_obj.type != 'CAMERA' or cam_obj.data is not cam_data:
        cam_obj = _find_camera_object(cam_data)
        if cam_obj is None:
            return
    from . import core
    current_P = core.current_plate_local(cam_obj, cam_data)
    if current_P.length < 1.0e-6:
        init_PL = core.compute_init_plate_local(cam_data)
        if init_PL.length < 1.0e-6:
            return
        direction = init_PL / init_PL.length
    else:
        direction = current_P / current_P.length
    new_P = direction * self.radial_distance
    _apply_camera_position(self, context, cam_obj, new_P, sync_target='delta')


class CSS_CameraSettings(PropertyGroup):
    """D plane invariants, plate display state, and per-camera move deltas."""

    is_initialized: BoolProperty(
        name="Initialize",
        description=(
            "Toggle the D plane Initialize state. Turning ON computes T from "
            "target_distance along the camera's current shift view direction. "
            "Turning OFF clears the linkage (target_distance is kept as the "
            "restore seed). Also set automatically by Get Distance from Click "
            "and cleared by external camera edits / deactivation / save."
        ),
        default=False,
        update=_is_initialized_update,
    )

    # Initialize snapshot 5 values (plan-x.md step 1).
    # `init_cam_location` is the Camera Position rigid anchor (replaces the
    # old `base_camera_location`): it stores cam.location at Initialize time
    # and is rigid-followed by Plate Transform edits (so Delta XYZ stays
    # invariant under Plate Transform). The other 4 values are frozen at
    # Initialize and used as documentation / for plate_baked_rotation derivation.
    # All 5 are discarded on De-Initialize (Bake 確定: camera itself is left
    # as-is).
    init_cam_location: FloatVectorProperty(
        name="Init Camera Location",
        description=(
            "Camera world position anchor for Camera Position computations. "
            "Initialized to cam.location at Initialize time, rigid-followed by "
            "Plate Transform edits. Used by core.compute_init_plate_local as "
            "the reference point that, together with plane_origin, defines "
            "init_plate_local = plate_rot.inv @ (init_cam_location - plane_origin)"
        ),
        size=3,
        subtype='TRANSLATION',
        default=(0.0, 0.0, 0.0),
    )

    init_cam_rotation: FloatVectorProperty(
        name="Init Camera Rotation",
        description="Initialize 時のカメラ rotation（XYZ Euler 保存、rotation_mode 非依存。Properties Editor 等で直感的に値が読めるよう Euler 形式）",
        size=3,
        subtype='EULER',
        default=(0.0, 0.0, 0.0),
    )

    init_cam_lens: FloatProperty(
        name="Init Camera Lens",
        description="Initialize 時の cam.lens（snapshot）",
        default=50.0,
    )

    init_cam_shift_x: FloatProperty(
        name="Init Camera Shift X",
        description="Initialize 時の cam.shift_x（snapshot）",
        default=0.0,
    )

    init_cam_shift_y: FloatProperty(
        name="Init Camera Shift Y",
        description="Initialize 時の cam.shift_y（snapshot）",
        default=0.0,
    )

    target_distance: FloatProperty(
        name="Target Distance",
        description=(
            "Perpendicular depth from the camera to the D plane target T. "
            "Editing this slider dollies the camera along Z (computes "
            "move_delta_z so the new camera position has d = target_distance). "
            "Conversely, when Move Delta Z dollies the camera, this value "
            "auto-syncs to the live d. Initialize ON restores T from this "
            "stored value. Independent of cam.dof.focus_distance. Shown as "
            "plain Float without unit so the digits don't get truncated"
        ),
        default=10.0,
        min=0.0,
        soft_min=0.0,
        soft_max=1000.0,
        precision=3,
        update=_target_distance_update,
    )

    plane_origin: FloatVectorProperty(
        name="D Plane Origin (T, world)",
        description=(
            "Internal: world-space center T of the D plane. Updated by "
            "Initialize, by shift_sync (when cam.shift_x/y is edited), and by "
            "Plate Transform Location edits. Not directly edited in the UI — "
            "shown as a greyed-out reference under Location"
        ),
        size=3,
        subtype='TRANSLATION',
        default=(0.0, 0.0, 0.0),
    )

    plate_location_delta: FloatVectorProperty(
        name="Location",
        description=(
            "Plate Transform Location: user-editable delta in D-plane LOCAL "
            "axes (baked frame). Starts at (0, 0, 0) on every Initialize. "
            "Editing translates the D plane and the camera by the same world "
            "delta (delta_world = baked_rotation @ delta_local), preserving "
            "shift_x/y. The actual world T position is shown as a greyed-out "
            "reference underneath"
        ),
        size=3,
        subtype='TRANSLATION',
        default=(0.0, 0.0, 0.0),
        unit='LENGTH',
        update=_plate_location_delta_update,
    )

    plate_rotation: FloatVectorProperty(
        name="Rotation",
        description=(
            "Plate Transform Rotation: user-editable delta on top of "
            "plate_baked_rotation. Starts at (0, 0, 0) on every Initialize. "
            "Editing rotates the D plane and the camera (position + rotation) "
            "rigidly around T, preserving shift_x/y"
        ),
        size=3,
        subtype='EULER',
        default=(0.0, 0.0, 0.0),
        update=_plate_rotation_update,
    )

    show_plate: BoolProperty(
        name="Show Plate",
        description="Display the D plane plate overlay in the 3D viewport",
        default=False,
        update=_show_plate_update,
    )

    plate_in_front: BoolProperty(
        name="In Front",
        description=(
            "When ON the plate draws always on top of the scene (no depth test). "
            "When OFF the plate is depth-tested against scene objects, so they "
            "occlude it where they intersect"
        ),
        default=False,
        update=_plate_style_update,
    )

    plate_expanded: BoolProperty(
        name="Plate Settings Expanded",
        description="UI-only: whether the Plate section is expanded in the N-Panel",
        default=False,
    )

    plate_fill_color: FloatVectorProperty(
        name="Plate Fill Color",
        description="Fill color (RGBA) of the D plane plate overlay; the alpha controls translucency",
        size=4,
        subtype='COLOR',
        min=0.0,
        max=1.0,
        default=(1.0, 0.5, 0.0, 0.15),
        update=_plate_style_update,
    )

    plate_edge_color: FloatVectorProperty(
        name="Plate Edge Color",
        description="Edge color (RGBA) of the D plane plate overlay outline",
        size=4,
        subtype='COLOR',
        min=0.0,
        max=1.0,
        default=(1.0, 0.5, 0.0, 1.0),
        update=_plate_style_update,
    )

    plate_edge_width: FloatProperty(
        name="Plate Edge Width",
        description="Outline thickness of the D plane plate overlay (pixels)",
        min=0.0,
        max=10.0,
        default=2.0,
        update=_plate_style_update,
    )

    move_delta_x: FloatProperty(
        name="Delta X",
        description=(
            "Camera Plate-local X offset from the Initialize anchor "
            "(init_plate_local). Editing translates the camera along the "
            "Plate-X axis (pure straight-line translation in D-plane local "
            "space). Bidirectionally synced with Radial Distance"
        ),
        default=0.0,
        unit='LENGTH',
        update=_move_delta_x_update,
    )

    move_delta_y: FloatProperty(
        name="Delta Y",
        description=(
            "Camera Plate-local Y offset from the Initialize anchor "
            "(init_plate_local). Plate-Y axis translation. See Delta X"
        ),
        default=0.0,
        unit='LENGTH',
        update=_move_delta_y_update,
    )

    move_delta_z: FloatProperty(
        name="Delta Z",
        description=(
            "Camera Plate-local Z offset from the Initialize anchor "
            "(init_plate_local). Plate-Z axis translation (NOT radial — "
            "radial movement toward d is the Radial Distance slider). "
            "See Delta X"
        ),
        default=0.0,
        unit='LENGTH',
        update=_move_delta_z_update,
    )

    radial_distance: FloatProperty(
        name="Radial Distance",
        description=(
            "Radial distance from D plane center d (=T) to the camera "
            "(3D Euclidean length of the Plate-local position vector). "
            "Editing scales the camera's Plate-local position along the "
            "current d-cam direction so the new |P| equals this value — "
            "i.e. moves the camera along the d-cam line. Bidirectionally "
            "synced with Delta XYZ. Hard min > 0 to avoid the camera "
            "passing through or onto d"
        ),
        default=10.0,
        min=0.001,
        soft_min=0.001,
        soft_max=1000.0,
        precision=3,
        unit='LENGTH',
        update=_radial_distance_update,
    )

    # UI proxies for cam.shift_x / shift_y. cam.shift_x/y remain the source of
    # truth; these expose a wider soft range (±1000) for slider dragging that
    # the stock RNA (±2 soft) doesn't permit. shift_sync keeps both directions
    # in sync via the msgbus on cam.shift_x/y.
    shift_x_proxy: FloatProperty(
        name="Shift X",
        description="Camera horizontal shift (UI mirror of cam.shift_x with extended drag range)",
        soft_min=-1000.0,
        soft_max=1000.0,
        default=0.0,
        update=_shift_x_proxy_update,
    )

    shift_y_proxy: FloatProperty(
        name="Shift Y",
        description="Camera vertical shift (UI mirror of cam.shift_y with extended drag range)",
        soft_min=-1000.0,
        soft_max=1000.0,
        default=0.0,
        update=_shift_y_proxy_update,
    )


def register() -> None:
    bpy.utils.register_class(CSS_CameraSettings)
    bpy.types.Camera.camera_shift_sync = PointerProperty(type=CSS_CameraSettings)


def unregister() -> None:
    del bpy.types.Camera.camera_shift_sync
    bpy.utils.unregister_class(CSS_CameraSettings)
