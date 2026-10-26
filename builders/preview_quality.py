"""Reversible viewport quality during navigation and interactive lighting edits."""
import json
import time

import bpy
from .compatibility import supports_sky
from bpy.app.handlers import persistent

IDLE_SECONDS = .3
POLL_SECONDS = .05
_previous_views = {}
_last_motion = {}
_suspensions = set()


def interaction(scene):
    """Keep AUTO preview fast while a user drags a lighting control."""
    s = getattr(scene, 'mcbedrock_sky', None)
    if not s or not s.active or _suspensions or s.preview_quality != 'AUTO':
        return
    _last_motion[scene.as_pointer()] = time.monotonic()
    _fast(scene)


def _cycles(scene):
    # Cycles is an add-on that can be turned off; its scene settings go with it.
    return getattr(scene, 'cycles', None)


def _fast(scene):
    s = scene.mcbedrock_sky
    if s.fast_preview_state:
        return
    cycles = _cycles(scene)
    s.fast_preview_state = json.dumps(dict(
        pixel_size=scene.render.preview_pixel_size,
        eevee_samples=scene.eevee.taa_samples,
        cycles_samples=cycles.preview_samples if cycles is not None else None,
        jitter=scene.eevee.use_shadow_jitter_viewport))
    current = scene.render.preview_pixel_size
    scene.render.preview_pixel_size = str(max(2, int(current))) if current != 'AUTO' else '2'
    scene.eevee.taa_samples = min(scene.eevee.taa_samples, 16) if scene.eevee.taa_samples else 16
    if cycles is not None:
        cycles.preview_samples = min(cycles.preview_samples, 16) if cycles.preview_samples else 16
    scene.eevee.use_shadow_jitter_viewport = False


def restore(scene):
    s = getattr(scene, 'mcbedrock_sky', None)
    if s is None or not s.fast_preview_state:
        return
    try:
        saved = json.loads(s.fast_preview_state)
        pixel_size, eevee_samples, cycles_samples, jitter = (
            saved[key] for key in ('pixel_size', 'eevee_samples', 'cycles_samples', 'jitter'))
    except (ValueError, KeyError, TypeError):
        s.fast_preview_state = ''
        return
    scene.render.preview_pixel_size = pixel_size
    scene.eevee.taa_samples = eevee_samples
    cycles = _cycles(scene)
    if cycles is not None and cycles_samples is not None:
        cycles.preview_samples = cycles_samples
    scene.eevee.use_shadow_jitter_viewport = jitter
    s.fast_preview_state = ''


def toggle(scene):
    """Compatibility with the original Fast Preview operator."""
    s = scene.mcbedrock_sky
    fast = not bool(s.fast_preview_state)
    if hasattr(s, 'preview_quality'):
        s.preview_quality = 'FAST' if fast else 'FULL'
        mode_changed(s, bpy.context)
    elif fast:
        _fast(scene)
    else:
        restore(scene)


def mode_changed(self, context):
    if not supports_sky():
        return
    scene = getattr(self, 'id_data', None) or context.scene
    scene_id = scene.as_pointer()
    _last_motion.pop(scene_id, None)
    for key, (owner, _) in list(_previous_views.items()):
        if owner == scene_id:
            del _previous_views[key]
    restore(scene)
    if self.preview_quality == 'FAST' and getattr(self, 'active', True) and not _suspensions:
        _fast(scene)


def _rendered_views():
    """Snapshot real rendered viewports, including additional windows and quad view."""
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            space = area.spaces.active
            if space.shading.type != 'RENDERED':
                continue
            views = space.region_quadviews or (space.region_3d,)
            for view in views:
                # Projection changes include orthographic zoom and camera lens changes.
                signature = tuple(value for matrix in (view.view_matrix, view.perspective_matrix)
                                  for row in matrix for value in row)
                yield window.scene, (window.as_pointer(), area.as_pointer(), view.as_pointer()), signature


def _step(now, views):
    """One navigation sample; explicit inputs also make idle timing testable."""
    current = {}
    visible = set()
    for scene, key, signature in views:
        scene_id = scene.as_pointer()
        visible.add(scene_id)
        current[key] = (scene_id, signature)
        previous = _previous_views.get(key)
        if previous is not None and previous[0] == scene_id and previous[1] != signature:
            _last_motion[scene_id] = now
    _previous_views.clear()
    _previous_views.update(current)
    alive = set()
    for scene in bpy.data.scenes:
        s = getattr(scene, 'mcbedrock_sky', None)
        if s is None or not hasattr(s, 'preview_quality'):
            continue
        scene_id = scene.as_pointer()
        alive.add(scene_id)
        if scene_id not in visible:
            _last_motion.pop(scene_id, None)
        moving = scene_id in visible and now - _last_motion.get(scene_id, -float('inf')) < IDLE_SECONDS
        if not _suspensions and getattr(s, 'active', True) and (s.preview_quality == 'FAST' or (s.preview_quality == 'AUTO' and moving)):
            _fast(scene)
        else:
            restore(scene)
    for scene_id in set(_last_motion) - alive:
        del _last_motion[scene_id]


_reported = set()


def _report_once(what):
    """Print a background error once. A timer that raises is unregistered for
    the rest of the session, so the caller keeps running instead."""
    import traceback
    message = traceback.format_exc()
    if message not in _reported:
        _reported.add(message)
        print(f'Sparkle: {what} hit an error and keeps running:\n{message}')


def _tick():
    if not _suspensions:
        try:
            _step(time.monotonic(), _rendered_views())
        except (ReferenceError, RuntimeError):
            # A window or scene can disappear during a workspace/file transition.
            _reset_quietly()
        except Exception:
            _report_once('Viewport preview quality')
            _reset_quietly()
    return POLL_SECONDS


def _reset_quietly():
    try:
        reset()
    except Exception:
        _report_once('Viewport preview quality')


@persistent
def reset(*args):
    # Blender replaces bpy.data with _RestrictData while enabling add-ons.
    # Clear Python state there; the first normal timer sample handles scenes.
    # Normal disable still restores quality before its properties are removed.
    for scene in getattr(bpy.data, 'scenes', ()):
        restore(scene)
    _previous_views.clear()
    _last_motion.clear()


def _suspend(reason):
    _suspensions.add(reason)
    reset()


def _resume(reason):
    # Undo/load can resurrect a transient saved state; restore it before sampling.
    reset()
    _suspensions.discard(reason)


@persistent
def render_pre(scene, *args):
    _suspend('render')


@persistent
def render_post(scene, *args):
    _resume('render')


@persistent
def _save_pre(*args):
    _suspend('save')


@persistent
def _save_post(*args):
    _resume('save')


@persistent
def _history_pre(*args):
    _suspend('history')


@persistent
def _history_post(*args):
    _resume('history')


@persistent
def _load_pre(*args):
    _suspend('load')


@persistent
def _load_post(*args):
    _suspensions.clear()
    reset()


_HANDLERS = (
    ('save_pre', _save_pre), ('save_post', _save_post), ('save_post_fail', _save_post),
    ('load_pre', _load_pre), ('load_post', _load_post), ('load_post_fail', _load_post),
    ('undo_pre', _history_pre), ('undo_post', _history_post),
    ('redo_pre', _history_pre), ('redo_post', _history_post),
    ('render_pre', render_pre), ('render_complete', render_post), ('render_cancel', render_post),
)


def register():
    _suspensions.clear()
    reset()
    for name, callback in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name, None)
        if handlers is not None and callback not in handlers:
            handlers.append(callback)
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=POLL_SECONDS, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    for name, callback in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name, None)
        if handlers is not None and callback in handlers:
            handlers.remove(callback)
    _suspensions.clear()
    reset()
