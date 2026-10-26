"""Interruptible Figma ease-out transitions, independent of Blender and its timeline."""

import time


def ease_out(progress):
    """CSS/Figma cubic-bezier(0, 0, .58, 1), evaluated on the time axis."""
    progress = min(1.0, max(0.0, progress))
    low, high = 0.0, 1.0
    for _ in range(18):
        t = (low + high) / 2
        x = 3 * (1 - t) * t * t * .58 + t ** 3
        if x < progress:
            low = t
        else:
            high = t
    t = (low + high) / 2
    return 3 * (1 - t) * t * t + t ** 3


def make_ease(progress):
    """Make's cubic-bezier(.22, 1, .36, 1)."""
    low, high = 0., 1.
    for _ in range(18):
        t = (low+high)/2
        x = 3*(1-t)**2*t*.22 + 3*(1-t)*t*t*.36 + t**3
        if x < progress:
            low = t
        else:
            high = t
    return 1-(1-(low+high)/2)**3


def tween(state, key, target, duration=.12, *, initial=None, easing=ease_out):
    if state.get('_motion_disabled'):
        return target
    now = state.get('_motion_time', time.monotonic())
    tracks = state.setdefault('_motion', {})
    track = tracks.get(key)
    if track is None:
        start = target if initial is None else initial
        track = tracks[key] = [start, target, now, duration]
    start, end, began, seconds = track
    progress = min(1.0, max(0.0, (now - began) / max(seconds, .001)))
    if now - began + 1e-9 >= seconds:
        progress = 1.0
    value = end if progress >= 1 else start + (end - start) * easing(progress)
    if target != end:
        track[:] = [value, target, now, duration]
        progress = 0.0
    if progress < 1 and abs(value - target) > .0001:
        state['_motion_active'] = True
    return value


def blend(a, b, progress):
    return tuple(x + (y - x) * progress for x, y in zip(a, b))


def resize_transition(state, key, target):
    """Gently accelerate and settle automatic layout changes; drags stay direct."""
    if state.get('_panel_resize') or state.get('_panel_resize_y') or state.get('_motion_disabled'):
        state.get('_motion', {}).pop(key, None)
        return target
    return tween(state, key, target, .18, easing=lambda t: t * t * (3 - 2 * t))
