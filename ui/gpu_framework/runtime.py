"""Blender draw-handler and modal event bridge."""

from __future__ import annotations

import copy
import textwrap
import time
import traceback
from dataclasses import dataclass

import bpy
from bpy.app.handlers import persistent

from .core import Rect, ScrollView, TextField, Theme
from .renderer import BlenderRenderer
from .motion import tween, ease_out
from .scaling import sidebar_tab_width, ui_scale, viewport_scale
from .panel_storage import load_size, save_size


_ACTIVE = {}
_UNDO_PAGES = {}
# Page, scroll and section state a finished session leaves for the next one in
# the same place: (last visible time, visible when it ended, plain state).
_SAVED_STATES = {}
# Values naming Blender data or a passing message; they end with their session.
_SESSION_ONLY_KEYS = {'particle.scene', 'particle.emitter', 'app.status'}
_PENDING = {}
_FAILED = set()
_FAILED_REASONS = {}  # runtime key -> the error that stopped its panel, shown with Retry
_TIMER_PENDING = set()
# Runtime keys whose panel is folded: clicking the open Sparkle tab collapses
# the sidebar to its tabs, as Blender does for every tab, and the panel follows.
# Kept outside the session so undo, or a stall that remounts the panel, keeps it.
_FOLDED = set()
# Blender draws its navigation gizmos this many UI pixels further from an edge
# than Sparkle's copies (5.2): the glide ends exactly where Blender's appear.
_NATIVE_NAVIGATION_NUDGE = 2.3
_REGISTERED = False
_NATIVE_DIALOG_OPEN = False
# The sidebar's category tabs are one Blender widget unit wide (UI_PANEL_CATEGORY_MARGIN_WIDTH).
# Native panels keep a small, even margin from the tabs and the region top; so does ours.
_SIDEBAR_EDGE = 5.0
_MIN_PANEL_WIDTH = 240.0
_DEFAULT_PANEL_WIDTH = _MIN_PANEL_WIDTH
_HOST_TIMEOUT = 3.0
# Blender 5.2 screen_ops.cc: TIMEOUT / TIMESTEP for region blending.
_REGION_FADE_DURATION = .22
_REGION_FADE_START = .9 - 1/60
_GENERATION = 0


def _active_session(key):
    session = _ACTIVE.get(key)
    if session is not None and (session._finished or not _pointer(session.operator)):
        session._finish(bpy.context)
        return None
    return session


def _pointer(value):
    try:
        return int(value.as_pointer())
    except (AttributeError, ReferenceError):
        return 0


def _runtime_key(window, area, region):
    return (_pointer(window), _pointer(area), _pointer(region))


def _key_text(key):
    return ":".join(str(part) for part in key)


_TOOLTIP_LINES = 5


def _wrap_words(renderer, text, width, size, weight):
    lines, line = [], ''
    for word in str(text).split():
        candidate = f'{line} {word}' if line else word
        if line and renderer.text_width(candidate, size, weight) > width:
            lines.append(line)
            line = word
        else:
            line = candidate
    return lines + [line] if line else lines


def _plain(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, (list, tuple)):
        return all(_plain(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _plain(item) for key, item in value.items())
    return False


def _page_state(state):
    """Navigation a later session can resume: plain values, never RNA or widgets."""
    return {key: copy.deepcopy(value) for key, value in state.items()
            if not key.startswith('_') and key not in _SESSION_ONLY_KEYS and _plain(value)}


def _sidebar_background_alpha(context):
    themes = getattr(context.preferences, 'themes', ())
    sidebars = getattr(getattr(themes[0], 'regions', None), 'sidebars', None) if len(themes) else None
    back = getattr(sidebars, 'back', None)
    return float(back[3]) if back is not None and len(back) > 3 else 0.0


def _parse_key(value):
    try:
        parts = tuple(int(part) for part in value.split(":"))
    except (TypeError, ValueError):
        return ()
    return parts if len(parts) == 3 else ()


def _valid_context_objects(window, area, region):
    area_pointer = _pointer(area)
    region_pointer = _pointer(region)
    try:
        live_area = next(
            (item for item in window.screen.areas if _pointer(item) == area_pointer),
            None,
        )
        return (
            window is not None
            and area is not None
            and region is not None
            and area.type == "VIEW_3D"
            and region.type == "UI"
            and live_area is not None
            and any(
                _pointer(item) == region_pointer for item in live_area.regions
            )
        )
    except (ReferenceError, AttributeError):
        return False


@dataclass
class _Pending:
    window: object
    area: object
    region: object
    builder: object


def mount_state(context) -> str:
    """Return ACTIVE, STARTING, or INACTIVE for the current sidebar region."""
    if (
        context is None
        or context.window is None
        or context.area is None
        or context.region is None
        or context.area.type != "VIEW_3D"
        or context.region.type != "UI"
    ):
        return "INACTIVE"
    key = _runtime_key(context.window, context.area, context.region)
    if key in _FAILED:
        return "FAILED"
    if _active_session(key) is not None:
        return "ACTIVE"
    if key in _PENDING or key in _TIMER_PENDING:
        return "STARTING"
    return "INACTIVE"


def request_mount(context, builder) -> None:
    """Touch an active host or defer one modal runtime until after Panel.draw."""
    if not _REGISTERED or context.area is None or context.region is None:
        return
    if context.area.type != "VIEW_3D" or context.region.type != "UI":
        return
    key = _runtime_key(context.window, context.area, context.region)
    if key in _FAILED:
        return
    if not all(key):
        return
    active = _active_session(key)
    if active is not None:
        active.touch()
        active.builder = builder
        return

    _PENDING[key] = _Pending(
        context.window,
        context.area,
        context.region,
        builder,
    )
    if key in _TIMER_PENDING:
        return
    _TIMER_PENDING.add(key)
    generation = _GENERATION

    def invoke_after_draw():
        if generation != _GENERATION or not _REGISTERED:
            return None
        _TIMER_PENDING.discard(key)
        pending = _PENDING.get(key)
        if pending is None or not _valid_context_objects(
            pending.window, pending.area, pending.region
        ):
            _PENDING.pop(key, None)
            return None
        try:
            with bpy.context.temp_override(
                window=pending.window,
                area=pending.area,
                region=pending.region,
            ):
                bpy.ops.mcbedrock.gpu_ui_runtime(
                    "INVOKE_DEFAULT",
                    runtime_key=_key_text(key),
                )
        except Exception as exc:
            traceback.print_exc()
            _FAILED.add(key)
            _FAILED_REASONS[key] = str(exc)
            _PENDING.pop(key, None)
        return None

    bpy.app.timers.register(invoke_after_draw, first_interval=0.0)


def retry_mount(context):
    key = _runtime_key(context.window, context.area, context.region)
    _FAILED.discard(key)
    _FAILED_REASONS.pop(key, None)


def failure_reason(context):
    """Why this sidebar's panel stopped, for the Retry box."""
    return _FAILED_REASONS.get(_runtime_key(context.window, context.area, context.region), "")


def cancel_all() -> None:
    global _NATIVE_DIALOG_OPEN, _GENERATION
    _GENERATION += 1
    _NATIVE_DIALOG_OPEN = False
    _FAILED.clear()
    _FAILED_REASONS.clear()
    _PENDING.clear()
    _TIMER_PENDING.clear()
    for runtime in list(_ACTIVE.values()):
        runtime.request_close = True
        runtime._finish(bpy.context)


class RuntimeSession:
    """Own resources outside Blender's disposable operator RNA wrapper."""

    def __init__(self, operator):
        self.operator = operator

    def touch(self):
        self.last_seen = time.monotonic()

    def invoke(self, context, event):
        self.window = None
        self.area = None
        self.region = None
        self.builder = None
        self.state = load_size()
        self.theme = Theme()
        self.ui_scale = 1.0
        self.tree = None
        self.renderer = None
        self._built_at = 0.0
        self._tree_context = None
        self.draw_count = 0
        self.build_count = 0
        self.draw_handler = None
        self.viewport_handler = None
        self.timer = None
        self.last_seen = 0.0
        self.request_close = False
        self._finished = False
        self._closing_fold = False
        self._navigation_from = None
        self.key = _parse_key(self.operator.runtime_key)
        pending = _PENDING.pop(self.key, None)
        if pending is None:
            return {"CANCELLED"}
        active = _active_session(self.key)
        if active is not None:
            active.touch()
            return {"CANCELLED"}
        restored_state = _UNDO_PAGES.pop(self.key, {})
        self._resume_open = bool(restored_state.pop('_resume_open', False))
        self.window = pending.window
        self.area = pending.area
        self.region = pending.region
        self.builder = pending.builder
        if not restored_state:
            self._resume_saved_state()
        self.state.update(restored_state)
        # The pack's undo snapshot can precede its asynchronous asset scan.
        # Restore that derived list when returning to the library after undo.
        scene = context.scene
        if (restored_state.get('app.active_tab') == 'ASSETS'
                and scene.mcbedrock_browser_root
                and (not scene.mcbedrock_browser_assets
                     or scene.get('_mcbedrock_loaded_browser_root') != scene.mcbedrock_browser_root)):
            bpy.ops.mcbedrock.asset_browser_refresh(
                'EXEC_DEFAULT', use_cached=True, background=True)
        self.touch()
        self.draw_handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw,
            (),
            "UI",
            "POST_PIXEL",
        )
        self.viewport_region = next(r for r in self.area.regions if r.type == 'WINDOW')
        self.viewport_handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (), 'WINDOW', 'POST_PIXEL')
        self.timer = context.window_manager.event_timer_add(
            .25,
            window=self.window,
        )
        self._timer_interval = .25
        context.window_manager.modal_handler_add(self.operator)
        _ACTIVE[self.key] = self
        self._tag_redraw()
        return {"RUNNING_MODAL"}

    def _snapshot(self):
        return (getattr(self, '_last_visible', 0.0), getattr(self, '_was_visible', False),
                _page_state(getattr(self, 'state', {})))

    def _resume_saved_state(self):
        """Resume what this place showed before its last session ended.

        Sessions end when the sidebar hides or changes tab, the workspace
        changes, or Blender stalls past the host timeout. Ctrl+Space shows the
        area under a new key, so newer state from an area that left the screen wins.
        """
        windows = {_pointer(window) for window in bpy.context.window_manager.windows}
        live = {_pointer(area) for area in self.window.screen.areas}
        def moved(key):
            return key[0] == self.key[0] and key[1] not in live
        sources = [saved for key, saved in _SAVED_STATES.items() if key == self.key or moved(key)]
        sources += [session._snapshot() for key, session in _ACTIVE.items() if moved(key)]
        for key in [key for key in _SAVED_STATES if key == self.key or moved(key) or key[0] not in windows]:
            del _SAVED_STATES[key]
        if sources:
            seen, visible, state = max(sources, key=lambda saved: saved[0])
            self.state.update(state)
            # A panel that was on screen a moment ago continues without the slide-in.
            self._resume_open = visible and time.monotonic() - seen < 2.0

    def _matches_draw_context(self):
        return (
            getattr(self.region, "active_panel_category", "Sparkle") == "Sparkle"
            and _pointer(bpy.context.window) == self.key[0]
            and _pointer(bpy.context.area) == self.key[1]
            and _pointer(bpy.context.region) in {self.key[2], _pointer(self.viewport_region)}
            and self.area.spaces.active.show_region_ui
            and self.key not in _FOLDED
        )

    def _restore_navigation(self):
        original = getattr(self, '_native_navigation', None)
        if original is not None:
            try:
                self._navigation_space.show_gizmo_navigate = original
            except (ReferenceError, AttributeError):
                pass
            self._native_navigation = None

    def _sync_navigation(self):
        # While the panel shows, Sparkle's copies stand beside it wherever the
        # sidebar region ends; handing over whenever the panel crossed the
        # region's edge moved the gizmos without the panel moving.
        space = self.area.spaces.active
        if space.show_gizmo:
            if getattr(self, '_native_navigation', None) is None:
                self._navigation_space = space
                self._native_navigation = space.show_gizmo_navigate
                space.show_gizmo_navigate = False
                self.viewport_region.tag_redraw()
        else:
            self._restore_navigation()

    def _content_rect(self):
        state = getattr(self, 'state', {})
        # Width is shared by all pages in this panel.
        # The custom left edge can extend into the viewport independently of
        # Blender's remembered N-panel width.
        tabs = getattr(self, 'tab_width', 20.0 * self.ui_scale) / self.ui_scale
        right = float(self.region.width) / self.ui_scale - tabs - _SIDEBAR_EDGE
        available = (self.region.x + right * self.ui_scale - self.area.x) / self.ui_scale - 8.0
        default_width = _DEFAULT_PANEL_WIDTH
        requested = getattr(self, 'state', {}).get('_panel_width', default_width)
        width = max(0.0, min(max(_MIN_PANEL_WIDTH, requested), available))
        return Rect(right - width, _SIDEBAR_EDGE, width,
                    max(0.0, float(self.region.height) / self.ui_scale - 2 * _SIDEBAR_EDGE))

    def _draw(self):
        if not _pointer(self.operator):
            self._finish(bpy.context)
            return
        if self._finished or self.request_close:
            return
        # Space draw handlers also run in other viewports. Those must not reset
        # the opening transition of this panel.
        if (_pointer(bpy.context.window) != self.key[0]
                or _pointer(bpy.context.area) != self.key[1]):
            return
        if getattr(self.region, 'active_panel_category', 'Sparkle') != 'Sparkle':
            _FOLDED.discard(self.key)  # another tab was chosen: picking Sparkle opens it again
        self._follow_region_collapse()
        if not self._matches_draw_context():
            self._was_visible = False
            # A hidden panel keeps no keyboard focus.
            self.state.pop('_runtime_focus', None)
            self._restore_navigation()
            return
        if not getattr(self, '_was_visible', False):
            motion = self.state.setdefault('_motion', {})
            motion.pop('runtime.open', None)
            self._was_visible = True
            self._open_prepared = False
            self._opening_progress = 0.0
            if getattr(self, '_resume_open', False):
                self._resume_open = False
                now = time.monotonic()
                self._open_prepared = True
                self._opening_progress = 1.0
                motion['runtime.open'] = [1., 1., now, .22]
                motion['make.page.enter'] = [1., 1., now, .18]
                self.state['_make_previous_tab'] = self.state.get('app.active_tab')
        if time.monotonic() - self.last_seen > _HOST_TIMEOUT:
            return
        self._last_visible = time.monotonic()
        try:
            context = bpy.context
            self.draw_count += 1
            from ..custom_ui import _addon_preferences
            preferences = _addon_preferences(context)
            from .theme_adapter import resolve_theme
            theme = resolve_theme(context.preferences, getattr(preferences, 'ui_theme', 'BLENDER'))
            if theme != self.theme:
                self.theme = theme
                self.state['_tree_dirty'] = True
            self.ui_scale = viewport_scale(context.preferences, self.region.height,
                adaptive=getattr(preferences, 'adaptive_ui_scale', True),
                multiplier=getattr(preferences, 'custom_ui_scale', 1.0))
            self.tab_width = sidebar_tab_width(context.preferences)
            self._sync_navigation()
            self.state["_runtime_context"] = context
            self.state['_motion_time'] = time.monotonic()
            self.state['_motion_active'] = False
            # One preference turns off every Sparkle animation.
            if getattr(preferences, 'reduce_motion', False):
                self.state['_motion_disabled'] = True
            elif self.state.get('_motion_disabled_by_preference'):
                self.state.pop('_motion_disabled', None)
            self.state['_motion_disabled_by_preference'] = bool(getattr(preferences, 'reduce_motion', False))
            # Start the clock only after the viewport has prepared fonts,
            # shaders and thumbnails. UI-region bookkeeping must not consume
            # the opening animation before its first visible frame.
            if context.region.type == 'WINDOW' and self._open_prepared:
                self._opening_progress = tween(self.state, 'runtime.open', 1.0,
                    .22, initial=0.0, easing=ease_out)
            opening = self._opening_progress
            if opening < 1.0:
                self.state['_motion_active'] = True
            elif context.region.type == 'WINDOW':
                self._navigation_from = None  # the gizmos reached the panel
            self._slide_x = 36.0 * (1.0 - opening)
            self.state["_content_width"] = self._content_rect().width
            signature = (_pointer(context.scene), _pointer(context.active_object),self._content_rect().width)
            now = time.monotonic()
            if (self.tree is None or self.state.pop('_tree_dirty',False)
                    or now-self._built_at >= .25 or signature != self._tree_context):
                self.tree = self.builder(context, self.state, self.theme)
                self.build_count += 1
                self._built_at, self._tree_context = now, signature
            self.tree.layout(self._content_rect(), self.state, self.theme)
            if self.renderer is None or self.renderer.ui_scale != self.ui_scale:
                self.renderer = BlenderRenderer(context.region, self.ui_scale)
            renderer = self.renderer
            renderer.offset_x = (self.region.x - context.region.x) / self.ui_scale + self._slide_x
            renderer.opacity = opening if self._open_prepared else 0.0
            closing = getattr(self, '_closing_started', None)
            if closing is not None:
                alpha = max(0.0, _REGION_FADE_START - (time.monotonic() - closing) / _REGION_FADE_DURATION)
                self._slide_x = self._closing_slide
                renderer.offset_x = (self.region.x - context.region.x) / self.ui_scale + self._slide_x
                renderer.opacity *= alpha
                self.state['_motion_active'] = True
            renderer.offset_y = (self.region.y - context.region.y) / self.ui_scale
            overlaps = self.viewport_region.x + self.viewport_region.width >= self.region.x + self.region.width
            # Blender composites an overlapping sidebar's themed background over
            # the viewport pass. Unless the theme keeps it fully transparent,
            # paint the in-region part in the sidebar, as without region overlap.
            overlaps = overlaps and _sidebar_background_alpha(context) < 1/255
            renderer.record_only = context.region.type == 'UI' and overlaps
            renderer.linear_output = context.region.type == 'WINDOW'
            renderer.capture = context.region.type == 'UI'
            renderer.begin()
            try:
                # Overlapping regions use one continuous viewport surface.
                # Non-overlapping layouts split the panel at the region boundary.
                if context.region.type == 'WINDOW' and not overlaps:
                    renderer.push_clip(Rect(self._content_rect().x - 8, 0,
                        max(0, -self._content_rect().x + 8), self.region.height / self.ui_scale))
                if not self._open_prepared:
                    self.state['_make_previous_tab'] = self.state.get('app.active_tab')
                    self.state.setdefault('_motion', {})['make.page.enter'] = [1., 1., now, .18]
                self.tree.draw(renderer, self.state, self.theme)
                self._draw_resize_handles(renderer)
                self._draw_tooltip(renderer)
            finally:
                if context.region.type == 'WINDOW' and not overlaps:
                    renderer.pop_clip()
                renderer.end()
            if context.region.type == 'WINDOW' and not self._open_prepared:
                self._open_prepared = True
                self.state.setdefault('_motion', {})['runtime.open'] = [0., 1., time.monotonic(), .22]
                self._tag_redraw()
                self.viewport_region.tag_redraw()
            if renderer.capture:
                signature = tuple(renderer.commands)
                if signature != getattr(self, '_paint_signature', None):
                    self._paint_signature = signature
                    self.viewport_region.tag_redraw()
            self._set_timer_interval(1/60 if self.state.get('_motion_active') or self.state.get('_sky_drag') else .25)
        except Exception as exc:
            traceback.print_exc()
            _FAILED.add(self.key)
            _FAILED_REASONS[self.key] = str(exc)
            self.request_close = True

    def _set_timer_interval(self, interval):
        if self._timer_interval == interval or self._finished:
            return
        manager = bpy.context.window_manager
        if self.timer is not None:
            manager.event_timer_remove(self.timer)
        self.timer = manager.event_timer_add(interval, window=self.window)
        self._timer_interval = interval

    def _draw_tooltip(self, renderer):
        if self.state.get('_tooltip_dismissed') or self.state.get('_menu') or _NATIVE_DIALOG_OPEN:
            return
        if any(self.state.get(key) for key in ('_sky_drag', '_posterize_drag', '_schematic_pack_drag', '_carousel_drag',
                '_scroll_drag', '_panel_resize', '_panel_resize_y', '_runtime_pressed')):
            return
        hover_key = self.state.get("_runtime_hover")
        if not hover_key or self.tree is None:
            return
        elapsed = time.monotonic() - self.state.get('_hover_since', time.monotonic())
        if not 1.5 <= elapsed < 5.5:
            return
        widget = self._find_by_key(self.tree, hover_key)
        if widget is None or not widget.tooltip:
            return
        x, y = self.state.get("_runtime_mouse", (0.0, 0.0))
        width = min(max(0.0, self._content_rect().width - 8), 260.0,
                    max(90.0, renderer.text_width(widget.tooltip, 11.0, 'Medium') + 20.0))
        # Wrap instead of cutting: shortcuts and hints sit at the end of tooltips.
        lines = _wrap_words(renderer, widget.tooltip, width - 16.0, 11.0, 'Medium')
        if len(lines) > _TOOLTIP_LINES:
            lines[_TOOLTIP_LINES - 1:] = [' '.join(lines[_TOOLTIP_LINES - 1:])]
        tip = Rect(
            min(x + 12.0, self._content_rect().right - width - 4.0),
            max(4.0, y - 6.0 - (12.0 + 15.0 * len(lines))),
            width,
            12.0 + 15.0 * len(lines),
        )
        renderer.rect(
            tip,
            self.theme.surface_raised,
            radius=4.0,
            border=self.theme.border,
        )
        for index, line in enumerate(lines):
            renderer.text(
                Rect(tip.x + 8.0, tip.top - 6.0 - 15.0 * (index + 1), tip.width - 16.0, 15.0),
                line,
                self.theme.text,
                size=11.0,
                weight='Medium',
                cap_center=True,
            )

    def _find_by_key(self, widget, key):
        if getattr(widget, 'key', None) == key:
            return widget
        for child in getattr(widget, 'children', ()):
            found = self._find_by_key(child, key)
            if found is not None:
                return found
        return None

    def _tag_redraw(self):
        try:
            # Hover and animation belong to the sidebar. Tagging the whole area
            # also redraws the 3D viewport, including expensive rendered scenes.
            self.region.tag_redraw()

        except (ReferenceError, AttributeError):
            pass

    def _mouse(self, event):
        try:
            return (
                float(event.mouse_x - self.region.x) / self.ui_scale - getattr(self, '_slide_x', 0.0),
                float(event.mouse_y - self.region.y) / self.ui_scale,
            )
        except (AttributeError, ReferenceError):
            return (-1.0, -1.0)

    def _hit(self, event):
        if self.tree is None:
            return None
        x, y = self._mouse(event)
        if not self._content_rect().contains(x, y):
            return None
        return self.tree.hit_test(x, y)

    def _draw_resize_handles(self, renderer):
        # Menus own the overlay, including the part extending past the panel.
        if self.state.get('_menu') or getattr(self.tree, 'menu', None):
            return
        panel = self.tree.rect
        # Dock details change the bottom edge, not the side grip's anchor.
        expanded_height = panel.height + self.state.get('_pack_shell_reduction', 0.)
        renderer.rect(Rect(panel.x + 2, panel.top - expanded_height / 2 - 18, 2, 36),
                      self.theme.accent if self.state.get('_panel_resize') else self.theme.border,
                      radius=1)
        if self._find_by_key(self.tree, "app.footer") is not None:
            return
        renderer.rect(Rect(panel.x + panel.width / 2 - 18, panel.y + 2, 36, 2),
                      self.theme.accent if self.state.get('_panel_resize_y') else self.theme.border,
                      radius=1)

    def _handle_resize(self, context, event):
        vertical = self.state.get('_panel_resize_y')
        if vertical is not None:
            if event.type == 'MOUSEMOVE':
                self.state['_panel_height'] = min(self._content_rect().height,
                    max(180,vertical[1]-(event.mouse_y-vertical[0])/self.ui_scale))
            elif event.type=='ESC' or (event.type=='LEFTMOUSE' and event.value=='RELEASE'):
                if event.type=='ESC':
                    if vertical[2] is None:
                        self.state.pop('_panel_height',None)
                    else:
                        self.state['_panel_height']=vertical[2]
                if event.type != 'ESC':
                    save_size(self.state)
                self.state.pop('_panel_resize_y',None)
                self.window.cursor_modal_restore()
            else:
                return event.type!='TIMER'
            self.state['_tree_dirty']=True
            self._tag_redraw()
            self.viewport_region.tag_redraw()
            return True
        drag = self.state.get('_panel_resize')
        if drag is not None:
            if event.type == 'MOUSEMOVE':
                delta = (event.mouse_x - drag[0]) / self.ui_scale
                self.state['_panel_width'] = max(_MIN_PANEL_WIDTH, drag[1] - delta)
                self.state['_panel_width'] = self._content_rect().width
                self.state['_tree_dirty'] = True
            elif event.type == 'ESC' or (event.type == 'LEFTMOUSE' and event.value == 'RELEASE'):
                if event.type == 'ESC':
                    self.state['_panel_width'] = drag[1]
                    if drag[2] is None:
                        self.state.pop('_panel_height', None)
                    else:
                        self.state['_panel_height'] = drag[2]
                if event.type != 'ESC':
                    save_size(self.state)
                self.state.pop('_panel_resize', None)
                self.window.cursor_modal_restore()
                self.state['_tree_dirty'] = True
            else:
                return event.type != 'TIMER'
            self._tag_redraw()
            self.viewport_region.tag_redraw()
            return True
        if (event.type != 'LEFTMOUSE' or event.value != 'PRESS'
                or self.tree is None or _NATIVE_DIALOG_OPEN
                or self.state.get('_menu') or getattr(self.tree, 'menu', None)
                or getattr(self.region, 'active_panel_category', 'Sparkle') != 'Sparkle'
                or not self.area.spaces.active.show_region_ui
                or _pointer(context.window) != self.key[0]
                or _pointer(context.area) != self.key[1]):
            return False
        x, y = self._mouse(event)
        panel = self.tree.rect
        gear = self._find_by_key(self.tree, 'app.settings')
        if gear and gear.rect.contains(x,y):
            return False
        footer = self._find_by_key(self.tree, 'app.footer')
        grip = footer and abs(x-(footer.rect.x+footer.rect.width/2)) <= 24 and abs(y-(footer.rect.y+16)) <= 8
        if grip or (panel.x <= x <= panel.right and abs(y-panel.y)<=6):
            self.state['_panel_resize_y']=(event.mouse_y,panel.height,self.state.get('_panel_height'))
            self.state.pop('_runtime_pressed',None)
            self.window.cursor_modal_set('MOVE_Y')
            return True
        if abs(x - panel.x) > 6 or not panel.y <= y <= panel.top:
            return False
        self.state['_panel_resize'] = (event.mouse_x, self._content_rect().width, self.state.get('_panel_height'))
        self.state['_panel_height'] = panel.height
        self.state.pop('_runtime_pressed', None)
        self.window.cursor_modal_set('MOVE_X')
        return True

    def _handle_text_event(self, context, event):
        focus_key = self.state.get("_runtime_focus")
        if not focus_key or self.tree is None:
            return False
        field = self._find_by_key(self.tree, focus_key)
        if not isinstance(field, TextField) or event.value != "PRESS":
            return False
        value = field.get_text(context)
        if event.type == "BACK_SPACE":
            field.set_text(context, value[:-1])
        elif event.type == "DEL":
            field.set_text(context, "")
        elif event.type in {"RET", "NUMPAD_ENTER", "ESC"}:
            self.state["_runtime_focus"] = None
        elif event.type == "A" and event.ctrl:
            field.set_text(context, "")
        elif event.type == "V" and event.ctrl:
            # One-line fields: a pasted line break would search for it.
            pasted = context.window_manager.clipboard.replace("\r", "").replace("\n", " ")
            field.set_text(context, value + pasted)
        elif event.type == "C" and event.ctrl:
            context.window_manager.clipboard = value
        elif (event.unicode and event.unicode.isprintable()
              and (not (event.ctrl or event.alt) or (event.ctrl and event.alt))):
            # Windows reports AltGr (@ \ { } € on many layouts) as Ctrl+Alt.
            field.set_text(context, value + event.unicode)
        else:
            return False
        self._tag_redraw()
        self.state['_tree_dirty'] = True
        return True

    def _dismiss_tooltip_for_event(self, event):
        if (getattr(event, 'value', '') in {'PRESS', 'DOUBLE_CLICK'}
                or event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN'}):
            if not self.state.get('_tooltip_dismissed'):
                self.state['_tooltip_dismissed'] = True
                self._tag_redraw()

    def _handle_pointer(self, context, event):
        self._dismiss_tooltip_for_event(event)
        hit = self._hit(event)
        drag=self.state.get('_scroll_drag')
        scroll=self._find_by_key(self.tree,drag[0]) if drag else (hit if isinstance(hit,ScrollView) else None)
        if scroll and scroll.pointer_scrollbar(event,*self._mouse(event),self.state):
            self.state.pop('_runtime_pressed',None)
            self._tag_redraw()
            return True
        if event.type == "MOUSEMOVE":
            key = hit.key if hit is not None else None
            changed = key != self.state.get("_runtime_hover")
            self.state["_runtime_hover"] = key
            self.state["_runtime_mouse"] = self._mouse(event)
            if changed:
                self.state['_hover_since'] = time.monotonic()
                self.state.pop('_tooltip_dismissed', None)
                self._tag_redraw()
            return hit is not None

        if event.type in {"WHEELUPMOUSE", "WHEELDOWNMOUSE", "TRACKPADPAN"}:
            if hit is None:
                return False
            scroll = hit.ancestor(ScrollView)
            if scroll is None:
                return False
            if event.type == "TRACKPADPAN":
                # Blender supplies gesture displacement in window pixels,
                # including the system's scroll direction. Do not quantize it
                # into wheel clicks or invert it a second time.
                delta = (event.mouse_y - event.mouse_prev_y) / self.ui_scale
            else:
                direction = -1.0 if event.type == "WHEELUPMOUSE" else 1.0
                delta = direction * scroll.wheel_step
            # Compact pages contain a list inside another scroll view. The
            # inner list may fit completely while the page itself overflows.
            # Let the nearest ancestor that can move handle the gesture.
            while scroll is not None:
                if scroll.scroll_by(delta, self.state):
                    self._tag_redraw()
                    break
                scroll = scroll.parent.ancestor(ScrollView) if scroll.parent else None
            return True

        # Modal handlers can receive two PRESS events instead of DOUBLE_CLICK.
        if event.type == "LEFTMOUSE" and event.value in {"PRESS", "DOUBLE_CLICK"}:
            now = time.monotonic()
            point = self._mouse(event)
            previous = self.state.pop('_double_click', None)
            if hit is not None and getattr(hit, 'detect_double_press', False):
                identity = getattr(hit, 'double_click_identity', hit.key)
                paired = (previous is not None and previous[0] == identity
                          and now - previous[1] <= 0.4
                          and abs(point[0] - previous[2][0]) <= 5
                          and abs(point[1] - previous[2][1]) <= 5)
                if paired or event.value == 'DOUBLE_CLICK':
                    self.state.pop('_runtime_pressed', None)
                    self._guarded(lambda: hit.double_action(context))
                    self.state['_tree_dirty'] = True
                    self._tag_redraw()
                    return True
                self.state['_double_click'] = (identity, now, point)

        if event.type == "LEFTMOUSE" and event.value == "DOUBLE_CLICK" and hit is not None:
            action = getattr(hit, "double_action", None)
            if action is not None:
                self._guarded(lambda: action(context))
                self.state['_tree_dirty'] = True
                self._tag_redraw()
                return True
        if event.type != "LEFTMOUSE":
            return False
        if event.value == "PRESS":
            self.state["_runtime_pressed"] = hit.key if hit is not None else None
            self.state["_focus_visible"] = False
            if isinstance(hit, TextField):
                self.state["_runtime_focus"] = hit.key
            elif hit is not None:
                self.state["_runtime_focus"] = hit.key
            else:
                self.state["_runtime_focus"] = None
            self._tag_redraw()
            return hit is not None
        if event.value == "RELEASE":
            pressed = self.state.pop("_runtime_pressed", None)
            if hit is not None and hit.key == pressed:
                changed = self._guarded(lambda: hit.activate(context, self.state))
                self.state['_tree_dirty'] = True
                self._tag_redraw()
                return changed or hit is not None
        return hit is not None

    def _guarded(self, action):
        """Run a widget handler. An error ends a drag and is reported, not the session."""
        try:
            return action()
        except Exception as exc:
            traceback.print_exc()
            for drag in ('_sky_drag', '_posterize_drag', '_schematic_pack_drag'):
                self.state.pop(drag, None)
            self.state["app.status"] = f"Error: {exc}"
            return True

    def _report_status(self, context):
        """Show a message a widget left in app.status as Blender's own popup."""
        message = self.state.pop("app.status", None)
        if not message or self._finished:
            return
        lines = textwrap.wrap(str(message), 64) or [str(message)]
        def draw(menu, _context):
            for line in lines:
                menu.layout.label(text=line)
        try:
            context.window_manager.popup_menu(
                draw, title="Sparkle", icon='ERROR' if str(message).startswith('Error') else 'INFO')
        except Exception:
            print("Sparkle:", message)

    def _panel_visible(self, context):
        return (getattr(self.region, 'active_panel_category', 'Sparkle') == 'Sparkle'
                and self.area.spaces.active.show_region_ui
                and self.key not in _FOLDED
                and _pointer(getattr(context, 'window', None)) == self.key[0])

    def _event_targets_host_region(self, context, event):
        """Capture the visible panel across both regions, never the blank tail."""
        if (getattr(self.region, 'active_panel_category', 'Sparkle') != 'Sparkle'
                or self.key in _FOLDED
                or _pointer(getattr(context, 'window', None)) != self.key[0]
                or _pointer(getattr(context, 'area', None)) != self.key[1]):
            return False
        tree = getattr(self, 'tree', None)
        if tree is None:
            return False
        x, y = self._mouse(event)
        return (tree.rect.contains(x, y)
                or (tree.menu is not None and tree.menu_rect.contains(x, y)))

    def _complete_keyboard_close(self):
        if self._finished or getattr(self, '_closing_started', None) is None:
            return
        folding = getattr(self, '_closing_fold', False)
        # Opening glides the gizmos back from where Blender's stand now.
        self._navigation_from = self._native_navigation_x(hidden=not folding)
        if folding:
            # Only the tabs stay: Sparkle draws nothing until the sidebar opens again.
            self._closing_fold = False
            _FOLDED.add(self.key)
            self.state.pop('_runtime_focus', None)
        else:
            self.area.spaces.active.show_region_ui = False
        self._closing_started = None
        self._was_visible = False
        self._restore_navigation()
        self._set_timer_interval(.25)
        self.state['_motion_active'] = False
        self.area.tag_redraw()

    def _begin_keyboard_close(self, context, event):
        if (event.type != 'N' or event.value != 'PRESS'
                or any(getattr(event, k, False) for k in ('ctrl', 'shift', 'alt', 'oskey'))
                or _NATIVE_DIALOG_OPEN
                or _pointer(context.window) != self.key[0]
                or _pointer(context.area) != self.key[1]
                or getattr(context.region, 'type', None) not in {'WINDOW', 'UI'}
                or not self.area.spaces.active.show_region_ui
                or self.key in _FOLDED
                or getattr(self.region, 'active_panel_category', '') != 'Sparkle'):
            return False
        focused = self._find_by_key(self.tree, self.state.get('_runtime_focus')) if self.tree else None
        if isinstance(focused, TextField):
            return False
        self._closing_fold = False  # N during a fold hides the sidebar, as N does
        self._start_close()
        return True

    def _start_close(self):
        """Fade the panel out; closing then hides the sidebar, or folds the panel."""
        if getattr(self, '_closing_started', None) is None:
            self._closing_started = time.monotonic()
            self._closing_slide = getattr(self, '_slide_x', 0.0)
            # A short app timer also completes closing if another Blender modal
            # handler temporarily consumes window TIMER events.
            began = self._closing_started
            def complete():
                if getattr(self, '_closing_callback', None) is complete:
                    self._closing_callback = None
                if (not self._finished and getattr(self, '_closing_started', None) == began
                        and _valid_context_objects(self.window, self.area, self.region)):
                    self._complete_keyboard_close()
                return None
            self._closing_callback = complete
            bpy.app.timers.register(complete, first_interval=_REGION_FADE_DURATION)
            self.state['_motion_active'] = True
            self._set_timer_interval(1/60)
            self._tag_redraw()
            self.viewport_region.tag_redraw()

    def _region_collapsed(self):
        """True while the sidebar shows only its tabs.

        Clicking the open tab collapses Blender's sidebar to the tab strip and
        clicking it again opens it; the region keeps its place on screen.
        """
        preferences = bpy.context.preferences
        return self.region.width <= sidebar_tab_width(preferences) + 20 * ui_scale(preferences)

    def _follow_region_collapse(self):
        """Fold the panel when the sidebar collapses to its tabs; open it again with the sidebar."""
        if (self._finished or not self.area.spaces.active.show_region_ui
                or getattr(self.region, 'active_panel_category', '') != 'Sparkle'):
            return
        collapsed = self._region_collapsed()
        closing = getattr(self, '_closing_started', None) is not None
        if collapsed and self.key not in _FOLDED and not closing:
            self.state.pop('_menu', None)
            if not getattr(self, '_was_visible', False):
                _FOLDED.add(self.key)  # already collapsed when the panel mounted: nothing to fade
                self._restore_navigation()
                return
            self._closing_fold = True
            self._start_close()
        elif not collapsed and (self.key in _FOLDED or (closing and self._closing_fold)):
            _FOLDED.discard(self.key)  # the next draw slides the panel in again
            if closing and self._closing_fold:
                # Opened again before the fade ended: stay open.
                self._closing_started = None
                self._closing_fold = False
                self._closing_callback = None
            self._tag_redraw()
            self.viewport_region.tag_redraw()

    def _native_navigation_x(self, hidden=False):
        """Window x of the edge Blender's own navigation gizmos stand beside.

        That is the sidebar region's left edge (a collapsed sidebar's is its
        tab strip), or the area's right edge once N hides the sidebar.
        """
        edge = (self.viewport_region.x + self.viewport_region.width if hidden
                else self.region.x)
        return edge + _NATIVE_NAVIGATION_NUDGE * self.ui_scale

    def navigation_edge(self, context):
        """Physical x, in ``context.region``, of the edge Sparkle's navigation gizmos hug.

        Open, they stand beside the panel. While the panel fades out they glide
        to where Blender's own appear once it is gone, and while it opens they
        glide back from there, so the hand-over never jumps.
        """
        panel = self.region.x + self._content_rect().x * self.ui_scale
        closing = getattr(self, '_closing_started', None)
        if closing is not None:
            target = self._native_navigation_x(hidden=not getattr(self, '_closing_fold', False))
            progress = ease_out(min(1.0, max(0.0, (time.monotonic() - closing) / _REGION_FADE_DURATION)))
            return panel + (target - panel) * progress - context.region.x
        start = getattr(self, '_navigation_from', None)
        opening = getattr(self, '_opening_progress', 1.0)
        if start is not None and opening < 1.0:
            return start + (panel - start) * opening - context.region.x
        return panel - context.region.x

    def _handle_asset_browser_reset(self, context, event):
        if (self.state.get('app.active_tab') not in {'ASSETS', 'PACKS'}
                or event.type != 'BACK_SPACE' or event.value != 'PRESS'
                or not event.ctrl or not event.alt
                or getattr(event, 'shift', False) or getattr(event, 'oskey', False)
                or getattr(event, 'is_repeat', False)
                or _NATIVE_DIALOG_OPEN
                or not self.area.spaces.active.show_region_ui
                or not self._event_targets_host_region(context, event)):
            return False
        from ..asset_browser import reset_browser_state
        if self.state.get('app.active_tab') == 'PACKS':
            from ..pack_manager import reset_pack_library_state
            if 'FINISHED' not in bpy.ops.mcbedrock.reset_pack_library('EXEC_DEFAULT'):
                return True
            reset_pack_library_state(self.state)
        else:
            bpy.ops.mcbedrock.asset_browser_reset('EXEC_DEFAULT')
        reset_browser_state(self.state)
        self._tag_redraw()
        return True

    def modal(self, context, event):
        result = self._modal(context, event)
        if self.state.get("app.status"):
            self._report_status(context)
        return result

    def _modal(self, context, event):
        if (
            self.request_close
            or not _valid_context_objects(self.window, self.area, self.region)
            or time.monotonic() - self.last_seen > _HOST_TIMEOUT
        ):
            self._finish(context)
            return {"CANCELLED"}

        self._follow_region_collapse()
        # Dismiss before menus, resize handles or dialogs consume the click.
        self._dismiss_tooltip_for_event(event)
        if (self.state.get('_schematic_pack_drag') or self.state.get('_sky_drag') or self.state.get('_posterize_drag')) and self.tree is not None:
            x, y = self._mouse(event)
            if self._guarded(lambda: self.tree.handle_menu_event(context, event, self.state, x, y)):
                # The dragged control paints from live values. Text that depends
                # on it rebuilds at most 15 times a second, not on every event.
                steady = (event.type in {'TIMER', 'MOUSEMOVE'}
                          and time.monotonic() - self._built_at < 1 / 15)
                if not self.state.get('_schematic_pack_drag') and not steady:
                    self.state['_tree_dirty'] = True
                self._tag_redraw()
                return {'RUNNING_MODAL'}

        if self._handle_resize(context, event):
            return {'RUNNING_MODAL'}

        if self._begin_keyboard_close(context, event):
            return {'RUNNING_MODAL'}

        if event.type == "TIMER":
            now = time.monotonic()
            closing = getattr(self, '_closing_started', None)
            if closing is not None and now - closing >= _REGION_FADE_DURATION:
                self._complete_keyboard_close()
                return {'PASS_THROUGH'}
            # Event has no timer RNA property in Blender 5.x. Rate-limit shared
            # TIMER events by elapsed time instead of rejecting our own timer.
            interval = 1/60 if self.state.get('_motion_active') else .25
            if now - self.state.get('_idle_redraw',0) >= interval:
                self.state['_idle_redraw'] = now
                self._tag_redraw()
                if self.state.get('_motion_active'):
                    self.viewport_region.tag_redraw()
            return {"PASS_THROUGH"}

        if not self._event_targets_host_region(context, event):
            # A focused text field keeps the keyboard while the pointer is off
            # the panel, as Blender's own fields do; Enter, Esc or a click ends it.
            if (event.type != 'TAB' and not _NATIVE_DIALOG_OPEN and self._panel_visible(context)
                    and self._handle_text_event(context, event)):
                return {'RUNNING_MODAL'}
            if self.state.get('_scroll_drag') and event.type in {'MOUSEMOVE','LEFTMOUSE'}:
                if self._handle_pointer(context,event):
                    return {'RUNNING_MODAL'}
            if event.type == "LEFTMOUSE" and event.value == "PRESS":
                self.state.pop("_menu", None)
                self.state.pop("_runtime_focus", None)
            changed = self.state.pop("_runtime_hover", None) is not None
            if event.type == "LEFTMOUSE" and event.value == "RELEASE":
                self.state.pop("_runtime_pressed", None)
            if changed:
                self._tag_redraw()
            return {"PASS_THROUGH"}

        if _NATIVE_DIALOG_OPEN:
            return {"PASS_THROUGH"}
        if self._handle_asset_browser_reset(context, event):
            return {'RUNNING_MODAL'}
        if self.tree is not None and hasattr(self.tree, "handle_menu_event"):
            x, y = self._mouse(event)
            if self._guarded(lambda: self.tree.handle_menu_event(context, event, self.state, x, y)):
                self.state['_tree_dirty'] = True
                self._tag_redraw()
                return {"RUNNING_MODAL"}
        if event.type == "TAB" and event.value == "PRESS" and self.tree is not None:
            widgets = [w for w in self.tree.walk() if w.interactive and w.style.enabled
                       and not isinstance(w, ScrollView)
                       and (w.clip_rect is None or w.rect.intersect(w.clip_rect).height > 0)] if hasattr(self.tree, "walk") else []
            keys = [w.key for w in widgets]
            if keys:
                old = self.state.get("_runtime_focus")
                index = keys.index(old) if old in keys else (-1 if not event.shift else 0)
                self.state["_runtime_focus"] = keys[(index + (-1 if event.shift else 1)) % len(keys)]
                self.state["_focus_visible"] = True
                self._tag_redraw()
                return {"RUNNING_MODAL"}
        if self._handle_text_event(context, event):
            return {"RUNNING_MODAL"}
        # Only Tab focus repeats a control. A click's silent focus must not turn
        # Space, Blender's play key, into a second click.
        if (event.type in {"RET", "NUMPAD_ENTER", "SPACE"} and event.value == "PRESS"
                and self.state.get("_focus_visible")):
            focused = self._find_by_key(self.tree, self.state.get("_runtime_focus")) if self.tree else None
            if focused and self._guarded(lambda: focused.activate(context, self.state)):
                self.state['_tree_dirty'] = True
                self._tag_redraw()
                return {"RUNNING_MODAL"}
        if self._handle_pointer(context, event):
            return {"RUNNING_MODAL"}
        return {"PASS_THROUGH"}

    def cancel(self, context):
        self._finish(context)

    def _finish(self, context):
        if self._finished:
            return
        self._finished = True
        if len(getattr(self, 'key', ())) == 3 and getattr(self, 'state', None) is not None:
            _SAVED_STATES[self.key] = self._snapshot()
        if getattr(self, 'state', {}).pop('_panel_resize', None) is not None:
            self.window.cursor_modal_restore()
        if getattr(self, 'state', {}).pop('_panel_resize_y', None) is not None:
            self.window.cursor_modal_restore()
        callback = getattr(self, '_closing_callback', None)
        if callback is not None and bpy.app.timers.is_registered(callback):
            bpy.app.timers.unregister(callback)
        self._closing_callback = None
        self._restore_navigation()
        if _ACTIVE.get(self.key) is self:
            _ACTIVE.pop(self.key, None)
        if self.draw_handler is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(
                    self.draw_handler,
                    "UI",
                )
            except Exception:
                pass
            self.draw_handler = None
        if getattr(self, 'viewport_handler', None) is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(self.viewport_handler, 'WINDOW')
            except (ValueError, ReferenceError):
                pass
            self.viewport_handler = None
            self.viewport_region.tag_redraw()
        if self.timer is not None:
            try:
                context.window_manager.event_timer_remove(self.timer)
            except Exception:
                pass
            self.timer = None
        self._tag_redraw()
        self.tree = None
        self.renderer = None
        self.operator = None


class MCBEDROCK_OT_gpu_ui_runtime(bpy.types.Operator):
    bl_idname = 'mcbedrock.gpu_ui_runtime'
    bl_label = 'Sparkle GPU UI Runtime'
    bl_options = {'INTERNAL'}

    runtime_key: bpy.props.StringProperty(options={'HIDDEN','SKIP_SAVE'})

    def invoke(self, context, event):
        self._session = RuntimeSession(self)
        return self._session.invoke(context,event)

    def modal(self, context, event):
        if self._session._finished:
            return {'CANCELLED'}
        return self._session.modal(context,event)

    def cancel(self, context):
        self._session._finish(context)


@persistent
def _before_data_reset(*_args):
    from .. import asset_browser
    asset_browser._stop_browser_index()
    asset_browser._stop_worker()
    cancel_all()


@persistent
def _before_undo(*_args):
    # Keep navigation, never Blender data or widget callbacks, across RNA reset.
    for key, session in _ACTIVE.items():
        _UNDO_PAGES[key] = {
            name: session.state[name]
            for name in ('app.active_tab', 'particle.return_page', 'settings.return_page')
            if isinstance(session.state.get(name), str)
        }
        _UNDO_PAGES[key]['particle.restore_page'] = True
        # Scroll offsets are navigation too; the panel stays put instead of sliding in again.
        _UNDO_PAGES[key].update({
            name: {'scroll': float(value['scroll'])} for name, value in session.state.items()
            if isinstance(value, dict) and set(value) == {'scroll'}
            and isinstance(value['scroll'], (int, float))})
        if getattr(session, '_was_visible', False):
            _UNDO_PAGES[key]['_resume_open'] = True
    _before_data_reset()
    # Saved states may name rows the undo step replaces.
    _SAVED_STATES.clear()


@persistent
def _before_load(*_args):
    _UNDO_PAGES.clear()
    _FOLDED.clear()
    _before_data_reset()
    _SAVED_STATES.clear()
    # Blender releases custom fonts on file load; cached BLF ids then go stale.
    from .renderer import release_resources
    release_resources()


from .viewport_navigation import (
    MCBEDROCK_OT_orbit_click, MCBEDROCK_GGT_panel_navigation, MCBEDROCK_GGT_panel_orbit,
)

@persistent
def _before_save(*_args):
    # Never serialize our temporary navigation visibility into the user's file.
    for session in list(_ACTIVE.values()):
        session._restore_navigation()


@persistent
def _after_save(*_args):
    for session in list(_ACTIVE.values()):
        session._tag_redraw()


classes = (MCBEDROCK_OT_gpu_ui_runtime, MCBEDROCK_OT_orbit_click,
           MCBEDROCK_GGT_panel_navigation, MCBEDROCK_GGT_panel_orbit)


def register():
    global _REGISTERED
    if _REGISTERED:
        return
    for cls in classes:
        bpy.utils.register_class(cls)
    for handlers, callback in ((bpy.app.handlers.load_pre, _before_load),
                               (bpy.app.handlers.undo_pre, _before_undo),
                               (bpy.app.handlers.redo_pre, _before_undo)):
        if callback not in handlers:
            handlers.append(callback)
    bpy.app.handlers.save_pre.append(_before_save)
    bpy.app.handlers.save_post.append(_after_save)
    _REGISTERED = True


def unregister():
    global _REGISTERED
    cancel_all()
    from .renderer import release_resources
    release_resources()
    for handlers, callback in ((bpy.app.handlers.load_pre, _before_load),
                               (bpy.app.handlers.undo_pre, _before_undo),
                               (bpy.app.handlers.redo_pre, _before_undo)):
        if callback in handlers:
            handlers.remove(callback)
    for handlers, callback in ((bpy.app.handlers.save_pre, _before_save),
                               (bpy.app.handlers.save_post, _after_save)):
        if callback in handlers:
            handlers.remove(callback)
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
    _ACTIVE.clear()
    _UNDO_PAGES.clear()
    _SAVED_STATES.clear()
    _PENDING.clear()
    _TIMER_PENDING.clear()
    _REGISTERED = False
