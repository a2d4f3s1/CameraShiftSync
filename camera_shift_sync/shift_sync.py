"""Sync the D plane origin T and shift_x/y to follow `cam.shift_x` /
`cam.shift_y` / `cam.lens` edits, and force De-Init on `cam.type` switches
away from PERSP.

Uses `bpy.msgbus.subscribe_rna` to detect changes from any source — this
addon's N-Panel slider (which displays `cam.shift_x` / `cam.lens` directly),
Blender's standard Properties Editor, or Python scripts. Four properties
are watched:

  - `shift_x` / `shift_y`: T moves along the new shift view direction at
    the current perpendicular depth; the camera location is NOT touched.
  - `lens` / `angle` / `angle_x` / `angle_y`: shift_x/y are recomputed via
    `shift_from_target` so the framing center stays on T at the new focal
    length / FOV (= zoom centered on d). T itself is NOT moved; only
    shift adjusts. All four are subscribed because Properties Editor's
    "Lens Type" dropdown (Millimeters writes cam.lens; Field of View
    writes cam.angle / angle_x / angle_y depending on sensor_fit).

    The lens callback writes cam.shift_x/y IMMEDIATELY (no timer defer)
    so lens and shift land in the same viewport draw — eliminating the
    1-frame "ガタガタ" gap. To prevent the deferred shift_x/y msgbus that
    Blender queues from re-triggering plane_origin updates, the lens
    callback transiently clears the shift_x/y msgbus subscriptions
    (`bpy.msgbus.clear_by_owner(_owner_shift)`), writes shift, then
    re-subscribes. msgbus key-level unsubscribe doesn't exist; owner-
    level does, hence the owner split (`_owner_shift` vs `_owner_other`).
  - `type`: Function A is PERSP-only. If the camera is switched to ORTHO
    or PANO while Initialize is ON, the linkage is now incoherent with the
    camera's projection mode, so we force De-Init (Bake 確定 pattern, same
    as external transform edits / deactivation / save). The switch itself
    proceeds — only the Initialize state is invalidated.

Move operations in `properties.py::_apply_camera_position` (called by the
Camera Position Delta XYZ / Radial Distance callbacks) also write to
`cam.shift_x` (for the shift-follows-camera direction). Those writes hold
`set_suppressed(True)` so this module's msgbus callback is a no-op during a
Move, preventing the two callbacks from ping-ponging.

Re-subscription on file load is required by Blender (the message bus loses
its subscriptions on `wm.read_factory_settings()` / opening a new .blend);
we register a persistent `load_post` handler that calls `_subscribe()`.
"""
from __future__ import annotations

import bpy
from mathutils import Vector


# Two separate msgbus owner tokens so the shift_x/y subscriptions can be
# transiently cleared (`bpy.msgbus.clear_by_owner(_owner_shift)`) inside the
# lens callback without losing the other subscriptions. `clear_by_owner` is
# owner-granular, hence the split.
_owner_shift = object()  # for shift_x / shift_y subscriptions only
_owner_other = object()  # for lens / angle / angle_x / angle_y / type

_suppress = False  # ping-pong guard, toggled by _apply_camera_position during Move


def is_suppressed() -> bool:
    return _suppress


def set_suppressed(value: bool) -> None:
    global _suppress
    _suppress = value


def _on_camera_shift_changed():
    """msgbus callback: sync the UI proxy (settings.shift_x/y_proxy) back from
    cam.shift_x/y, and — if Initialized — re-place T along the new shift view
    direction at the current perpendicular depth."""
    global _suppress
    if _suppress:
        return

    from . import core, properties  # lazy to avoid circulars at module-load time

    context = bpy.context
    obj = context.active_object
    if obj is None or obj.type != 'CAMERA':
        return
    cam = obj.data
    if cam.type != 'PERSP':
        return
    settings = cam.camera_shift_sync

    # Always mirror cam.shift_x/y back into the UI proxy so the N-Panel slider
    # value stays consistent with cam.shift even when the edit came from the
    # Properties Editor or a Move-delta-driven shift recompute. The proxy
    # update callback is suppressed during this write so we don't ping-pong
    # back into cam.shift.
    properties.set_shift_proxy_suppress(True)
    try:
        if settings.shift_x_proxy != cam.shift_x:
            settings.shift_x_proxy = cam.shift_x
        if settings.shift_y_proxy != cam.shift_y:
            settings.shift_y_proxy = cam.shift_y
    finally:
        properties.set_shift_proxy_suppress(False)

    if not settings.is_initialized:
        return

    T_current = Vector(settings.plane_origin)
    d = core.perpendicular_depth(obj, T_current)
    if d <= 0.0:
        return
    new_T = core.shift_view_target_world(obj, context.scene, d)
    if new_T is None:
        return

    # Suppress both this module's msgbus (so the plane_origin write doesn't
    # cascade back through cam.shift_x/y) and the Plate Transform update
    # callback (so the write isn't mistaken for a user Plate Transform Location
    # edit, which would rigid-translate the camera).
    _suppress = True
    properties.set_plate_transform_suppress(True)
    try:
        settings.plane_origin = new_T
        # Re-seed the Plate Transform baseline so subsequent user edits to
        # Location compute their delta from this new T, not the pre-shift one.
        properties.seed_plate_transform_baseline(cam)
    finally:
        properties.set_plate_transform_suppress(False)
        _suppress = False

    # Auto-sync radial_distance: T moved but cam stayed, so |current_P|
    # (= camera-to-d distance) changed. Without this sync, the Radial
    # Distance slider keeps showing the pre-shift value, and the next user
    # touch of it would jump the camera by the desync amount.
    new_radial = core.current_plate_local(obj, cam).length
    if new_radial > 0.0 and abs(settings.radial_distance - new_radial) > 1.0e-7:
        properties.set_move_position_suppress(True)
        try:
            settings.radial_distance = new_radial
        finally:
            properties.set_move_position_suppress(False)


def _on_camera_lens_changed():
    """msgbus callback for cam.lens / angle / angle_x / angle_y. Recomputes
    shift_x/y IMMEDIATELY so framing stays centered on T at the new focal
    length / FOV — no timer defer, so lens and shift land in the same
    viewport draw (no "ガタガタ" gap).

    To prevent the deferred shift_x/y msgbus that Blender queues after
    we write cam.shift_x/y from re-running the plane_origin / Plate
    baseline / radial code path, we transiently clear the shift_x/y
    msgbus subscriptions before writing and re-subscribe right after.
    The `_suppress` flag alone isn't safe here: msgbus callbacks run in
    Blender's re-entry-protected dispatch context, which DEFERS the
    shift msgbus until after this callback returns — by which time we
    can no longer guarantee `_suppress` is still True. Owner-level
    clear is the only deterministic way to silence the deferred fire."""
    global _suppress
    if _suppress:
        return

    from . import core, properties

    context = bpy.context
    obj = context.active_object
    if obj is None or obj.type != 'CAMERA':
        return
    cam = obj.data
    if cam.type != 'PERSP':
        return
    settings = cam.camera_shift_sync

    if settings.is_initialized:
        T_world = Vector(settings.plane_origin)
    else:
        T_world = core.shift_view_target_world(obj, context.scene, settings.target_distance)
        if T_world is None:
            return

    shift_x, shift_y = core.shift_from_target(obj, T_world, context.scene)
    if shift_x is None:
        return

    # Clear shift msgbus subscriptions so the deferred shift_x/y fires that
    # Blender queues for our writes below are dropped entirely. Re-subscribe
    # immediately after so subsequent user-driven shift edits resume.
    bpy.msgbus.clear_by_owner(_owner_shift)
    properties.set_shift_proxy_suppress(True)
    try:
        if cam.shift_x != shift_x:
            cam.shift_x = shift_x
        if cam.shift_y != shift_y:
            cam.shift_y = shift_y
        if settings.shift_x_proxy != shift_x:
            settings.shift_x_proxy = shift_x
        if settings.shift_y_proxy != shift_y:
            settings.shift_y_proxy = shift_y
    finally:
        properties.set_shift_proxy_suppress(False)
        _subscribe_shift()


def _on_camera_type_changed():
    """msgbus callback: a Camera's `type` property changed. Function A is
    PERSP-only — if a previously Initialize-ON PERSP camera was switched to
    ORTHO / PANO, the Initialize state is now incoherent with the camera's
    projection mode (Camera Position math assumes a perspective frustum).
    Force De-Init on any such camera (Bake 確定): cam.location / rotation /
    lens / shift / type are left as-is, only the Initialize state, UI Delta
    sliders, Radial Distance, show_plate, plane_origin, and Initialize
    snapshot 5 values are cleared.

    Switching back to PERSP does NOT auto re-Initialize — user must
    explicitly tick Initialize. Matches existing De-Init triggers
    (external transform edits, deactivation, save) which are also
    non-reversing.

    The msgbus key `(bpy.types.Camera, 'type')` fires once per type change,
    not per Camera instance, so we iterate all cameras to find which one
    actually changed."""
    from . import external_edit_watch
    for cam_data in bpy.data.cameras:
        settings = cam_data.camera_shift_sync
        if settings.is_initialized and cam_data.type != 'PERSP':
            external_edit_watch.clear_initialize(cam_data)


def _initial_sync_proxies():
    """Copy cam.shift_x/y into the UI proxy for every camera in the file.
    Called via a `bpy.app.timers` one-shot from register, and directly from
    load_post — without this, proxies open at their default 0.0 even if a
    saved camera has shift_x = 0.5.

    Returns None so it's also safe to schedule as a `bpy.app.timers` callback
    (None tells the timer system not to reschedule)."""
    from . import properties
    properties.set_shift_proxy_suppress(True)
    try:
        for cam in bpy.data.cameras:
            settings = cam.camera_shift_sync
            settings.shift_x_proxy = cam.shift_x
            settings.shift_y_proxy = cam.shift_y
    finally:
        properties.set_shift_proxy_suppress(False)
    return None


def _subscribe_shift() -> None:
    """Subscribe shift_x/y under `_owner_shift`. Split out so the lens
    callback can clear (`clear_by_owner(_owner_shift)`) and re-subscribe
    these two specifically, without touching the other subscriptions."""
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.Camera, 'shift_x'),
        owner=_owner_shift,
        args=(),
        notify=_on_camera_shift_changed,
    )
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.Camera, 'shift_y'),
        owner=_owner_shift,
        args=(),
        notify=_on_camera_shift_changed,
    )


def _subscribe_other() -> None:
    """Subscribe lens / angle / angle_x / angle_y / type under
    `_owner_other`. These are never transiently cleared, so they live
    on their own owner."""
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.Camera, 'lens'),
        owner=_owner_other,
        args=(),
        notify=_on_camera_lens_changed,
    )
    # FOV-mode edits in Properties Editor (Lens Type = Field of View) write
    # cam.angle / cam.angle_x / cam.angle_y instead of cam.lens. Subscribe
    # to all three so the framing auto-recompute fires regardless of which
    # property Blender's UI wrote to. The callback is shared and idempotent
    # — it reads cam.lens via view_frame() which Blender keeps in sync.
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.Camera, 'angle'),
        owner=_owner_other,
        args=(),
        notify=_on_camera_lens_changed,
    )
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.Camera, 'angle_x'),
        owner=_owner_other,
        args=(),
        notify=_on_camera_lens_changed,
    )
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.Camera, 'angle_y'),
        owner=_owner_other,
        args=(),
        notify=_on_camera_lens_changed,
    )
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.Camera, 'type'),
        owner=_owner_other,
        args=(),
        notify=_on_camera_type_changed,
    )


def _subscribe() -> None:
    _subscribe_shift()
    _subscribe_other()


def _unsubscribe() -> None:
    bpy.msgbus.clear_by_owner(_owner_shift)
    bpy.msgbus.clear_by_owner(_owner_other)


@bpy.app.handlers.persistent
def _load_post_handler(_dummy):
    _subscribe()
    _initial_sync_proxies()


def register() -> None:
    _subscribe()
    # bpy.data is restricted inside register(); defer the initial sync to the
    # next timer tick so it runs in a normal context.
    bpy.app.timers.register(_initial_sync_proxies, first_interval=0.0)
    if _load_post_handler not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_load_post_handler)


def unregister() -> None:
    if _load_post_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post_handler)
    _unsubscribe()
