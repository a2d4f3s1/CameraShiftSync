"""Detect external edits / deactivation / save and clear Initialize state.

The Move (Delta) sliders and Lens Shift sliders only make sense while a camera
is in the Initialize state: `init_cam_location` is the rigid anchor and the
deltas are measured from it. We auto-clear the linkage on two triggers, both
handled in `depsgraph_update_post`:

  1. External camera transform edits — anything outside this addon (Properties
     Editor transform fields, G / R / S shortcuts, drivers, Python scripts,
     ...) that changes the camera's local transform invalidates the
     `init_cam_location` anchor and the plane's baked orientation. We detect
     this by snapshotting `cam_obj.matrix_basis` on every internal write and
     comparing it against the current value on each depsgraph update;
     a mismatch in any element beyond `_MATCH_EPS` is treated as external.
     Using `matrix_basis` covers location, rotation (in any rotation_mode),
     and scale uniformly.

  2. Deactivation — the N-Panel and operators only target the active object, so
     a non-active camera in Initialize state is invisible / inaccessible. When
     the active object changes away from an Initialize-state camera (to a
     different object, a different camera, or nothing), we clear it too. This
     also gives the user an easy explicit way to "drop" a linkage: select away.

We deliberately do NOT rely on `DepsgraphUpdate.is_updated_transform`: Blender
4.2 routes Camera transform updates inconsistently between the Object and the
Camera datablock (Object often shows `transform=False, geometry=True` while the
Camera data shows `transform=True`), so a flag-based filter misses the edit.
Location comparison sidesteps that entirely.

Clearing zeroes `is_initialized`, `move_delta_*`, `radial_distance`, plane
geometry, and `show_plate`. The camera's current location / shift / plate
style are left alone — those are the "baked" result of whatever the user did.
The user must press Initialize / Get Distance from Click again to re-engage.

`clear_initialize(cam_data)` is also called by:
  - the `is_initialized` checkbox in the N-Panel (via the property update
    callback in `properties.py`), and
  - the save-time clear / restore handlers below.

Those paths bypass the depsgraph handler so they can write directly without
the timer indirection that `_schedule_clear` needs.

The save-time handlers snapshot Initialize state before save and restore it
after save: the .blend file is written clean (no persisted Initialize state)
but the user's in-memory work continues seamlessly.
"""
from __future__ import annotations

import bpy


# Camera-data name_full -> last known "internal" (this addon wrote it)
# matrix_basis as a 16-element tuple. Internal writes (Initialize, Move slider)
# call `remember_internal_transform` so the subsequent depsgraph notification
# matches and is ignored.
_internal_transforms: dict[str, tuple[float, ...]] = {}

# Tolerance for "matches the internal write" comparison. Move-driven writes
# go through view_layer.update() so any float drift would be deterministic
# rather than additive — 1e-5 is comfortably above that and well below any
# real user-visible edit (in both meters and radians, and as a scale ratio).
_MATCH_EPS = 1.0e-5


def _matrix_basis_tuple(cam_obj) -> tuple[float, ...]:
    """Flatten cam_obj.matrix_basis (the local transform, before parent /
    constraint resolution) to a 16-element tuple suitable for snapshotting."""
    return tuple(v for row in cam_obj.matrix_basis for v in row)


def remember_internal_transform(cam_obj) -> None:
    """Record the camera's current matrix_basis as an internal write so the
    next depsgraph notification on it is treated as a self-trigger."""
    _internal_transforms[cam_obj.data.name_full] = _matrix_basis_tuple(cam_obj)


def _resolve_camera(update_id):
    """Return (cam_obj, cam_data) for an update whose id is either a Camera
    Object or a Camera datablock. Returns (None, None) if the update isn't
    camera-related or we can't find a backing Object."""
    if isinstance(update_id, bpy.types.Object) and update_id.type == 'CAMERA':
        return update_id, update_id.data
    if isinstance(update_id, bpy.types.Camera):
        for obj in bpy.data.objects:
            if obj.type == 'CAMERA' and obj.data == update_id:
                return obj, update_id
        return None, update_id
    return None, None


def clear_initialize(cam_data) -> None:
    """Drop the linkage state for `cam_data`. Camera transform and shift are
    left as-is, as is plate style. See module docstring for the rationale.

    Safe to call directly from operators and timers. MUST NOT be called
    directly from `depsgraph_update_post`: writes to ID PropertyGroups inside
    that handler do not persist. Use `_schedule_clear` from depsgraph handlers
    — it defers this call to a `bpy.app.timers` tick that runs outside the
    handler context.
    """
    # Avoid the shift_sync msgbus callback firing on plane_origin writes etc.
    # Also suppress the is_initialized update callback so the False write here
    # doesn't recurse back into this function, and the Plate Transform update
    # callbacks so the plane_origin / plate_rotation resets don't trigger
    # rigid-follow camera motion.
    from . import shift_sync, plate_draw, properties
    settings = cam_data.camera_shift_sync
    shift_sync.set_suppressed(True)
    properties.set_is_initialized_suppress(True)
    properties.set_plate_transform_suppress(True)
    properties.set_move_position_suppress(True)
    try:
        settings.is_initialized = False
        settings.move_delta_x = 0.0
        settings.move_delta_y = 0.0
        settings.move_delta_z = 0.0
        settings.radial_distance = 1.0  # default neutral (min > 0 enforced by RNA)
        settings.plane_origin = (0.0, 0.0, 0.0)
        # plate_baked_rotation は派生（init_cam_rotation から都度計算）なので
        # ここで書き込まない。
        settings.plate_rotation = (0.0, 0.0, 0.0)
        settings.plate_location_delta = (0.0, 0.0, 0.0)
        settings.show_plate = False
        # Initialize snapshot 5 values: discard on De-Initialize.
        # Bake 確定: camera self (cam.location / rotation / lens / shift_x/y)
        # is NOT touched.
        settings.init_cam_location = (0.0, 0.0, 0.0)
        settings.init_cam_rotation = (0.0, 0.0, 0.0)
        settings.init_cam_lens = 50.0
        settings.init_cam_shift_x = 0.0
        settings.init_cam_shift_y = 0.0
    finally:
        properties.set_move_position_suppress(False)
        properties.set_plate_transform_suppress(False)
        properties.set_is_initialized_suppress(False)
        shift_sync.set_suppressed(False)
    properties.forget_plate_transform_baseline(cam_data.name_full)
    _internal_transforms.pop(cam_data.name_full, None)
    plate_draw.trigger_redraw()
    # Force N-Panel / Properties Editor redraw so the UI reflects the cleared
    # state without waiting for the next mouse move.
    wm = bpy.context.window_manager
    if wm is not None:
        for window in wm.windows:
            for area in window.screen.areas:
                if area.type in {'VIEW_3D', 'PROPERTIES'}:
                    area.tag_redraw()


# Cameras (by name_full) for which a deferred clear is pending; prevents
# the same clear from being scheduled multiple times across rapid depsgraph
# ticks during a G-grab.
_pending_clears: set[str] = set()


def _schedule_clear(cam_data_name_full: str) -> None:
    if cam_data_name_full in _pending_clears:
        return
    _pending_clears.add(cam_data_name_full)

    def _do_clear():
        _pending_clears.discard(cam_data_name_full)
        cam_data = bpy.data.cameras.get(cam_data_name_full)
        if cam_data is None:
            return None
        if not cam_data.camera_shift_sync.is_initialized:
            return None
        clear_initialize(cam_data)
        return None  # one-shot

    bpy.app.timers.register(_do_clear, first_interval=0.0)


def _is_external_edit(cam_obj, cam_data) -> bool:
    """True if the camera's current matrix_basis differs from the last
    internal-write snapshot beyond `_MATCH_EPS` in any element. If we have no
    recorded snapshot (e.g. file just loaded), treat the current value as the
    baseline and return False."""
    current = _matrix_basis_tuple(cam_obj)
    expected = _internal_transforms.get(cam_data.name_full)
    if expected is None:
        _internal_transforms[cam_data.name_full] = current
        return False
    return any(abs(c - e) > _MATCH_EPS for c, e in zip(current, expected))


@bpy.app.handlers.persistent
def _depsgraph_update_post(_scene, depsgraph):
    # Track which cameras we've already processed this tick — a single edit
    # can produce both Object and Camera-data updates in the same depsgraph.
    seen: set[str] = set()
    for update in depsgraph.updates:
        cam_obj, cam_data = _resolve_camera(update.id)
        if cam_obj is None or cam_data is None:
            continue
        if cam_data.name_full in seen:
            continue
        seen.add(cam_data.name_full)
        settings = cam_data.camera_shift_sync
        if not settings.is_initialized:
            # Not in Initialize state, but keep the baseline fresh so a future
            # Initialize starts from a known internal value.
            _internal_transforms[cam_data.name_full] = _matrix_basis_tuple(cam_obj)
            continue
        if not _is_external_edit(cam_obj, cam_data):
            continue
        _schedule_clear(cam_data.name_full)

    # Deactivation pass: any Initialize-state camera that isn't the current
    # active object gets cleared. Active-object changes fire depsgraph updates
    # in Blender 4.2, so this hook is sufficient — no separate msgbus sub.
    # Cheap in practice: typically zero or one camera is in Initialize state.
    active_obj = bpy.context.active_object
    if active_obj is not None and active_obj.type == 'CAMERA':
        active_cam_data_name = active_obj.data.name_full
    else:
        active_cam_data_name = None
    for cam_data in bpy.data.cameras:
        if not cam_data.camera_shift_sync.is_initialized:
            continue
        if cam_data.name_full == active_cam_data_name:
            continue
        _schedule_clear(cam_data.name_full)


@bpy.app.handlers.persistent
def _load_post_handler(_dummy):
    # Stale entries from the previous file would point at unrelated cameras;
    # the next Initialize / first depsgraph tick will repopulate.
    _internal_transforms.clear()
    # Plate Transform baselines are also keyed by camera-data name_full and
    # would mismatch the new file's cameras.
    from . import properties
    properties._plate_location_delta_baseline.clear()
    properties._plate_rotation_baseline.clear()


# --- Save-time Initialize clearing ---
#
# Initialize state is intentionally NOT persisted into the .blend. Reasons:
#   - Opening a file already in Initialize state is confusing — the user can't
#     see what camera position the linkage was set up against.
#   - On open, the depsgraph baseline doesn't match the saved transform, which
#     can trip the external-edit watcher into clearing on the first tick anyway.
#
# We achieve "clean save, seamless work" by snapshotting Initialize state in
# save_pre, calling clear_initialize so the saved file is clean, then
# restoring from the snapshot in save_post.
_save_snapshots: dict[str, dict] = {}


def _snapshot_initialize_state(cam_data) -> dict:
    settings = cam_data.camera_shift_sync
    return {
        "plane_origin": tuple(settings.plane_origin),
        # plate_baked_rotation は派生量、init_cam_rotation で復元される
        "plate_rotation": tuple(settings.plate_rotation),
        "plate_location_delta": tuple(settings.plate_location_delta),
        "move_delta_x": settings.move_delta_x,
        "move_delta_y": settings.move_delta_y,
        "move_delta_z": settings.move_delta_z,
        "radial_distance": settings.radial_distance,
        "show_plate": settings.show_plate,
        # Initialize snapshot 5 values. init_cam_location is the rigid anchor
        # for Camera Position computations (replaces the old base_camera_location);
        # the other 4 are frozen Initialize-time camera state documentation.
        "init_cam_location": tuple(settings.init_cam_location),
        "init_cam_rotation": tuple(settings.init_cam_rotation),
        "init_cam_lens": settings.init_cam_lens,
        "init_cam_shift_x": settings.init_cam_shift_x,
        "init_cam_shift_y": settings.init_cam_shift_y,
    }


def _restore_initialize_state(cam_data, snap: dict) -> None:
    from . import shift_sync, properties
    settings = cam_data.camera_shift_sync
    shift_sync.set_suppressed(True)
    properties.set_is_initialized_suppress(True)
    properties.set_plate_transform_suppress(True)
    properties.set_move_position_suppress(True)
    try:
        settings.plane_origin = snap["plane_origin"]
        # plate_baked_rotation は派生量、init_cam_rotation 復元で自動的に追従
        settings.plate_rotation = snap["plate_rotation"]
        settings.plate_location_delta = snap["plate_location_delta"]
        settings.move_delta_x = snap["move_delta_x"]
        settings.move_delta_y = snap["move_delta_y"]
        settings.move_delta_z = snap["move_delta_z"]
        settings.radial_distance = snap["radial_distance"]
        settings.show_plate = snap["show_plate"]
        # Initialize snapshot 5 values. init_cam_location is the rigid anchor;
        # the other 4 are frozen Initialize-time camera state.
        settings.init_cam_location = snap["init_cam_location"]
        settings.init_cam_rotation = snap["init_cam_rotation"]
        settings.init_cam_lens = snap["init_cam_lens"]
        settings.init_cam_shift_x = snap["init_cam_shift_x"]
        settings.init_cam_shift_y = snap["init_cam_shift_y"]
        settings.is_initialized = True
    finally:
        properties.set_move_position_suppress(False)
        properties.set_plate_transform_suppress(False)
        properties.set_is_initialized_suppress(False)
        shift_sync.set_suppressed(False)
    # After restore, re-seed the Plate Transform baselines so subsequent user
    # edits compute deltas from the restored values.
    properties.seed_plate_transform_baseline(cam_data)


@bpy.app.handlers.persistent
def _save_pre_handler(_dummy):
    _save_snapshots.clear()
    for cam_data in bpy.data.cameras:
        if not cam_data.camera_shift_sync.is_initialized:
            continue
        _save_snapshots[cam_data.name_full] = _snapshot_initialize_state(cam_data)
        clear_initialize(cam_data)


@bpy.app.handlers.persistent
def _save_post_handler(_dummy):
    for name_full, snap in _save_snapshots.items():
        cam_data = bpy.data.cameras.get(name_full)
        if cam_data is None:
            continue
        _restore_initialize_state(cam_data, snap)
    _save_snapshots.clear()


def register() -> None:
    if _depsgraph_update_post not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_depsgraph_update_post)
    if _load_post_handler not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_load_post_handler)
    if _save_pre_handler not in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.append(_save_pre_handler)
    if _save_post_handler not in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.append(_save_post_handler)


def unregister() -> None:
    if _save_post_handler in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.remove(_save_post_handler)
    if _save_pre_handler in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.remove(_save_pre_handler)
    if _load_post_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post_handler)
    if _depsgraph_update_post in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_depsgraph_update_post)
    _internal_transforms.clear()
    _pending_clears.clear()
    _save_snapshots.clear()
