"""Drag Sparkle's sun and moon in the viewport, keeping the day cycle intact."""
import math

import blf
import bpy
from bpy.app.handlers import persistent
from bpy_extras.view3d_utils import region_2d_to_origin_3d, region_2d_to_vector_3d
from mathutils import Vector

from .compatibility import SkyOperator, supports_sky


def selected_body(context):
    from . import sky
    obj = context.view_layer.objects.active
    settings = getattr(context.scene, 'mcbedrock_sky', None)
    if not settings or not settings.active or obj is None or not obj.select_get():
        return None
    return next((name for name in ('Sun', 'Moon') if obj == sky.owned(context.scene, name)), None)


def sky_view(space):
    shading = space.shading
    return (space.show_gizmo and space.overlay.show_overlays and
            space.region_3d and space.region_3d.is_perspective and
            ((shading.type == 'RENDERED' and shading.use_scene_world_render) or
             (shading.type == 'MATERIAL' and shading.use_scene_world)))


_overlay_state = {}
_overlay_suspended = False
_keymaps = []
_reported = set()


def _report_once(what):
    """Print a background error once. A timer that raises is unregistered for
    the rest of the session, so the caller keeps running instead."""
    import traceback
    message = traceback.format_exc()
    if message not in _reported:
        _reported.add(message)
        print(f'Sparkle: {what} hit an error and keeps running:\n{message}')


@persistent
def restore_overlays(*args):
    for space, size, look_at in _overlay_state.values():
        try:
            space.show_gizmo_light_size = size
            space.show_gizmo_light_look_at = look_at
        except ReferenceError:
            pass
    _overlay_state.clear()


@persistent
def suspend_overlays(*args):
    global _overlay_suspended
    _overlay_suspended = True
    restore_overlays()


@persistent
def resume_overlays(*args):
    global _overlay_suspended
    _overlay_suspended = False


def update_overlays():
    """Hide Blender's light gizmos only while the selected sky handle replaces
    them. The Extras overlay (cameras, empties, lights) is never touched: it
    hid every camera and empty in the view while the sun was selected."""
    if _overlay_suspended:
        return .1
    try:
        _update_overlays()
    except Exception:
        _report_once('Sun and moon handles')
    return .1


def _update_overlays():
    visible = set()
    for window in bpy.context.window_manager.windows:
        with bpy.context.temp_override(window=window):
            body = selected_body(bpy.context)
        for area in window.screen.areas:
            if area.type != 'VIEW_3D': continue
            space = area.spaces.active
            key = space.as_pointer()
            if not body or not sky_view(space) or window.screen.is_animation_playing:
                continue
            visible.add(key)
            if key not in _overlay_state:
                _overlay_state[key] = (space, space.show_gizmo_light_size, space.show_gizmo_light_look_at)
                space.show_gizmo_light_size = False
                space.show_gizmo_light_look_at = False
                area.tag_redraw()
    for key in set(_overlay_state)-visible:
        space, size, look_at = _overlay_state.pop(key)
        try:
            space.show_gizmo_light_size = size
            space.show_gizmo_light_look_at = look_at
        except ReferenceError:
            pass


_HANDLERS = tuple((name, suspend_overlays) for name in ('save_pre', 'load_pre', 'undo_pre', 'redo_pre')) + tuple(
    (name, resume_overlays) for name in ('save_post', 'save_post_fail', 'load_post', 'load_post_fail', 'undo_post', 'redo_post'))


def register():
    resume_overlays()
    if not bpy.app.timers.is_registered(update_overlays):
        bpy.app.timers.register(update_overlays, first_interval=.1, persistent=True)
    for name, callback in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name, None)
        if handlers is not None and callback not in handlers: handlers.append(callback)
    config = bpy.context.window_manager.keyconfigs.addon
    if config:
        keymap = config.keymaps.new(name='Object Mode', space_type='EMPTY')
        item = keymap.keymap_items.new('mcbedrock.sky_body_menu', 'RIGHTMOUSE', 'PRESS', head=True)
        _keymaps.append((keymap, item))


def unregister():
    if bpy.app.timers.is_registered(update_overlays): bpy.app.timers.unregister(update_overlays)
    restore_overlays()
    resume_overlays()
    for name, callback in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name, None)
        if handlers is not None and callback in handlers: handlers.remove(callback)
    for keymap, item in _keymaps:
        keymap.keymap_items.remove(item)
    _keymaps.clear()


class MCBEDROCK_MT_sky_body(bpy.types.Menu):
    bl_label = 'Sky light'

    def draw(self, context):
        body = selected_body(context)
        if not body: return
        settings = context.scene.mcbedrock_sky
        self.layout.label(text=body)
        self.layout.prop(settings, 'sunlight' if body == 'Sun' else 'moonlight', text=body+' strength')
        self.layout.prop(settings, 'time', text='Starting time' if settings.cycle_enabled else 'Time of day')
        self.layout.prop(settings, 'azimuth', text='Sky direction')


class MCBEDROCK_OT_sky_body_menu(bpy.types.Operator):
    bl_idname = 'mcbedrock.sky_body_menu'
    bl_label = 'Sky light controls'

    @classmethod
    def poll(cls, context):
        return (context.area and context.area.type == 'VIEW_3D' and
                context.region and context.region.type == 'WINDOW' and
                selected_body(context) is not None and sky_view(context.space_data))

    def invoke(self, context, event):
        body = selected_body(context)
        settings = context.scene.mcbedrock_sky
        from . import sky
        direction = sky.time_state(sky.current_time(context.scene), settings.azimuth)[0]
        if body == 'Moon': direction.negate()
        if direction.z <= -.12: return {'PASS_THROUGH'}
        placement = body_handle(context.region, context.region_data, direction,
                                settings.sun_size if body == 'Sun' else settings.moon_size,
                                settings.azimuth, 6*context.preferences.system.ui_scale)
        point = (event.mouse_region_x, event.mouse_region_y)
        if not placement or not inside_square(placement[1], point) or foreground_hit(context, point):
            return {'PASS_THROUGH'}
        bpy.ops.wm.call_menu(name='MCBEDROCK_MT_sky_body')
        return {'FINISHED'}


def inside_square(corners, location):
    sides = [(b.x-a.x)*(location[1]-a.y)-(b.y-a.y)*(location[0]-a.x)
             for a, b in zip(corners, (*corners[1:], corners[0]))]
    return min(sides) >= 0 or max(sides) <= 0


def foreground_hit(context, location):
    origin = region_2d_to_origin_3d(context.region, context.region_data, location)
    direction = region_2d_to_vector_3d(context.region, context.region_data, location)
    return context.scene.ray_cast(context.evaluated_depsgraph_get(), origin, direction,
                                  distance=context.space_data.clip_end)[0]


def project_direction(region, view, direction):
    """Project a point at infinity, including camera shift and viewport zoom."""
    clip = view.perspective_matrix @ Vector((*direction, 0))
    if clip.w <= 1e-6:
        return None
    return Vector(((clip.x/clip.w+1)*region.width*.5,
                   (clip.y/clip.w+1)*region.height*.5))


def body_handle(region, view, direction, size, azimuth, padding=6):
    center = project_direction(region, view, direction)
    if center is None: return None
    # Match the world shader's texture basis, including rotation in the view.
    angle = math.radians(azimuth)
    tangent = Vector((math.sin(angle), -math.cos(angle), 0))
    up = tangent.cross(direction).normalized()
    radius = math.tan(math.radians(size*.5))
    points = [project_direction(region, view, direction+radius*(tangent*x+up*y))
              for x, y in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    if any(point is None for point in points): return None
    points = [point+(point-center).normalized()*padding for point in points]
    if (max(p.x for p in points) < 0 or min(p.x for p in points) > region.width or
            max(p.y for p in points) < 0 or min(p.y for p in points) > region.height):
        return None
    return center, points


def time_and_azimuth(direction, reference_hour, reference_azimuth):
    """Choose the equivalent orbit closest to the previous drag orientation."""
    direction = Vector(direction)
    if not all(math.isfinite(v) for v in direction) or direction.length_squared < 1e-12:
        raise ValueError('Sun direction must be a finite, nonzero vector')
    direction.normalize()
    horizontal = math.hypot(direction.x, direction.y)
    elevation = math.atan2(direction.z, horizontal)
    if horizontal < 1e-6:
        return (12. if direction.z > 0 else 0.), reference_azimuth % 360
    heading = math.degrees(math.atan2(direction.y, direction.x))
    candidates = (((6 + elevation * 24 / math.tau) % 24, heading % 360),
                  ((6 + (math.pi-elevation) * 24 / math.tau) % 24, (heading-180) % 360))
    # Holding the morning/evening branch fixed mirrors time at noon/midnight
    # and flips artwork by 180 degrees. Keep the orbit heading continuous instead.
    return min(candidates, key=lambda candidate: (
        abs((candidate[1]-reference_azimuth+180) % 360-180),
        abs((candidate[0]-reference_hour+12) % 24-12)))


def set_sun_direction(scene, direction, reference_hour, reference_azimuth):
    from . import sky
    settings = scene.mcbedrock_sky
    hour, azimuth = time_and_azimuth(direction, reference_hour, reference_azimuth)
    # Shift the starting time when a cycle is enabled. Do not stop it or alter
    # the frame range; the requested direction applies at the current frame.
    offset = sky.current_time(scene) - settings.time
    sky.BUSY.add(scene.as_pointer())
    try:
        settings.time = (hour - offset) % 24
        settings.azimuth = azimuth
    finally:
        sky.BUSY.discard(scene.as_pointer())
    sky.update(scene)


class MCBEDROCK_OT_place_sun(SkyOperator, bpy.types.Operator):
    bl_idname = 'mcbedrock.place_sun'
    bl_label = 'Move sun or moon'
    bl_description = 'Drag the sky light; Esc restores its previous position'
    bl_options = {'UNDO', 'BLOCKING'}
    body: bpy.props.EnumProperty(items=[('SUN', 'Sun', ''), ('MOON', 'Moon', '')], default='SUN')
    from_handle: bpy.props.BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        if not super().poll(context):
            return False
        settings = getattr(context.scene, 'mcbedrock_sky', None)
        if not settings or not settings.active or not settings.world:
            cls.poll_message_set('Add a Sparkle sky first')
            return False
        if not context.area or context.area.type != 'VIEW_3D':
            cls.poll_message_set('Use Move sun in a 3D viewport')
            return False
        if context.screen.is_animation_playing:
            cls.poll_message_set('Pause playback before moving the sun')
            return False
        return True

    def invoke(self, context, event):
        from . import sky
        self._area = context.area
        self._region = (context.region if context.region.type == 'WINDOW' else
                        next((r for r in self._area.regions if r.type == 'WINDOW'), None))
        if self._region is None:
            return {'CANCELLED'}
        # region_data belongs to the WINDOW region, not the sidebar invoking us.
        with context.temp_override(region=self._region):
            self._view = context.region_data
        if self._view is None:
            return {'CANCELLED'}
        self._scene = context.scene
        self._window = context.window
        self._owner = self._scene.mcbedrock_sky.owner
        settings = self._scene.mcbedrock_sky
        self._original = (settings.time, settings.azimuth)
        self._hour = sky.current_time(self._scene)
        self._reference = (self._hour, settings.azimuth)
        self._drag_start = None
        self._changed = False
        if self.from_handle:
            body_object = sky.owned(self._scene, self.body.title())
            if body_object:
                for obj in context.selected_objects:
                    obj.select_set(False)
                body_object.select_set(True)
                context.view_layer.objects.active = body_object
                update_overlays()
            self._drag_start = (event.mouse_x-self._region.x, event.mouse_y-self._region.y)
            self._start_ray = region_2d_to_vector_3d(self._region, self._view, self._drag_start)
            self._body_direction = sky.time_state(self._hour, settings.azimuth)[0]
            if self.body == 'MOON': self._body_direction.negate()
        self._window.cursor_modal_set('CROSSHAIR')
        self._area.header_text_set('Move '+self.body.lower()+': drag; release to confirm; Esc / right-click to cancel')
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _restore(self):
        from . import sky
        settings = self._scene.mcbedrock_sky
        if self._changed and settings.active and settings.owner == self._owner:
            sky.BUSY.add(self._scene.as_pointer())
            try:
                settings.time, settings.azimuth = self._original
            finally:
                sky.BUSY.discard(self._scene.as_pointer())
            sky.update(self._scene)

    def _finish(self):
        self._window.cursor_modal_restore()
        self._area.header_text_set(None)
        self._area.tag_redraw()

    def cancel(self, context):
        try:
            self._restore()
        finally:
            self._finish()

    def _place(self, event):
        from . import sky, preview_quality
        x = event.mouse_x - self._region.x
        y = event.mouse_y - self._region.y
        if not (0 <= x < self._region.width and 0 <= y < self._region.height):
            return
        if self._view.is_perspective:
            direction = region_2d_to_vector_3d(self._region, self._view, (x, y))
            if self.from_handle:
                direction = self._start_ray.rotation_difference(direction) @ self._body_direction
            if self.body == 'MOON': direction.negate()
        else:
            # Parallel orthographic rays cannot pick different sky directions.
            # Relative dragging still lets artists position lighting in this view.
            dx = (x - self._drag_start[0]) / max(1, self._region.width)
            dy = (y - self._drag_start[1]) / max(1, self._region.height)
            direction = sky.time_state(self._hour + dy * 12, self._original[1] + dx * 360)[0]
        preview_quality.interaction(self._scene)
        set_sun_direction(self._scene, direction, *self._reference)
        self._reference = (sky.current_time(self._scene), self._scene.mcbedrock_sky.azimuth)
        self._changed = True
        self._area.tag_redraw()

    def modal(self, context, event):
        if (context.scene != self._scene or self._area.type != 'VIEW_3D' or
                not self._scene.mcbedrock_sky.active or
                self._scene.mcbedrock_sky.owner != self._owner):
            self.cancel(context)
            return {'CANCELLED'}
        if event.type in {'ESC', 'RIGHTMOUSE', 'WINDOW_DEACTIVATE'}:
            self.cancel(context)
            return {'CANCELLED'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            x, y = event.mouse_x - self._region.x, event.mouse_y - self._region.y
            if 0 <= x < self._region.width and 0 <= y < self._region.height:
                self._drag_start = (x, y)
                self._place(event)
        elif event.type == 'MOUSEMOVE' and self._drag_start is not None:
            self._place(event)
        elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE' and self._drag_start is not None:
            self._place(event)
            self._finish()
            return {'FINISHED'}
        return {'RUNNING_MODAL'}


LABEL_SIZE = 12                     # points, before the interface scale
# Sparkle's dark tooltip surface, a little see-through, behind white text:
# readable on bright day skies and orange sunsets alike.
LABEL_PLATE = (.11, .11, .11, .82)
LABEL_PLATE_HOVER = (.16, .16, .16, .9)
LABEL_TEXT = (.96, .96, .96, 1.)


def visible_rect(region, area):
    """The part of a 3D view's main region that no overlapping header, tool
    settings, toolbar or sidebar covers, in region pixels: (left, bottom,
    right, top). Regions that do not overlap (Region Overlap off) cost nothing."""
    left, bottom, right, top = 0., 0., float(region.width), float(region.height)
    for other in getattr(area, 'regions', ()):
        if other.type == 'WINDOW' or other.width <= 1 or other.height <= 1:
            continue
        x0, y0 = other.x - region.x, other.y - region.y
        x1, y1 = x0 + other.width, y0 + other.height
        if x1 <= left or x0 >= right or y1 <= bottom or y0 >= top:
            continue
        side = getattr(other, 'alignment', 'NONE')
        if side == 'TOP':
            top = min(top, y0)
        elif side == 'BOTTOM':
            bottom = max(bottom, y1)
        elif side == 'LEFT':
            left = max(left, x1)
        elif side == 'RIGHT':
            right = min(right, x0)
    return left, bottom, right, top


def label_box(text_size, anchor_x, anchor_y, bounds, scale):
    """(x, y, width, height) of a name plate centered on anchor_x with its
    bottom at anchor_y, moved inside bounds (left, bottom, right, top)."""
    width, height = text_size
    box_w, box_h = width + 12 * scale, height + 8 * scale
    left, bottom, right, top = bounds
    margin = 4 * scale
    x = max(left + margin, min(right - margin - box_w, anchor_x - box_w / 2))
    y = max(bottom + margin, min(top - margin - box_h, anchor_y))
    return x, y, box_w, box_h


def _linear(color):
    # The viewport blends its overlays in linear light; the sidebar's renderer
    # converts its colors the same way.
    return tuple(max(0., c) ** 2.2 for c in color[:3]) + (color[3],)


def _rounded(x, y, width, height, radius, steps=6):
    radius = max(0., min(radius, width / 2, height / 2))
    corners = ((x + width - radius, y + radius, -math.pi / 2), (x + width - radius, y + height - radius, 0.),
               (x + radius, y + height - radius, math.pi / 2), (x + radius, y + radius, math.pi))
    return [(cx + radius * math.cos(start + math.pi / 2 * i / steps), cy + radius * math.sin(start + math.pi / 2 * i / steps))
            for cx, cy, start in corners for i in range(steps + 1)]


def _draw_plate(x, y, width, height, radius, color):
    import gpu
    from gpu_extras.batch import batch_for_shader
    outline = _rounded(x, y, width, height, radius)
    middle = (x + width / 2, y + height / 2)
    triangles = [point for a, b in zip(outline, outline[1:] + outline[:1]) for point in (middle, a, b)]
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    batch = batch_for_shader(shader, 'TRIS', {'pos': triangles})
    previous = gpu.state.blend_get()
    gpu.state.blend_set('ALPHA')
    try:
        shader.bind()
        shader.uniform_float('color', color)
        batch.draw(shader)
    finally:
        gpu.state.blend_set(previous)


def draw_label(context, text, anchor_x, anchor_y, highlight=False):
    """The body's name on a small dark plate, kept inside the part of the view
    that headers and side panels leave visible."""
    font = 0
    # Another add-on may have left rotation, clipping, a shadow or wrapping
    # switched on for Blender's font.
    for flag in ('ROTATION', 'CLIPPING', 'SHADOW', 'WORD_WRAP'):
        value = getattr(blf, flag, None)
        if value is not None:
            blf.disable(font, value)
    scale = context.preferences.system.ui_scale
    blf.size(font, LABEL_SIZE * scale)
    width = blf.dimensions(font, text)[0]
    height = blf.dimensions(font, 'H')[1]
    x, y, box_w, box_h = label_box((width, height), anchor_x, anchor_y,
                                   visible_rect(context.region, context.area), scale)
    _draw_plate(x, y, box_w, box_h, 4 * scale, _linear(LABEL_PLATE_HOVER if highlight else LABEL_PLATE))
    blf.position(font, x + (box_w - width) / 2, y + (box_h - height) / 2, 0)
    blf.color(font, *LABEL_TEXT)
    blf.draw(font, text)


class MCBEDROCK_GT_sky_body(bpy.types.Gizmo):
    """A texture-aligned square and upright name for the selected sky light."""
    bl_idname = 'MCBEDROCK_GT_sky_body'
    __slots__ = ('label', 'corners', '_shape', '_vertices')

    def setup(self):
        self.label = ''
        self.corners = ()
        self._shape = None
        self._vertices = ()
        self.use_draw_scale = False

    def draw(self, context):
        if not self.corners or not self.body_selected(context): return
        center = self.matrix_basis.translation.xy
        vertices = tuple((p.x-center.x, p.y-center.y, 0)
                         for i in range(4) for p in (self.corners[i], self.corners[(i+1)%4]))
        if vertices != self._vertices:
            self._shape = self.new_custom_shape('LINES', vertices)
            self._vertices = vertices
        self.draw_custom_shape(self._shape)
        scale = context.preferences.system.ui_scale
        draw_label(context, self.label, center.x, max(p.y for p in self.corners)+8*scale, self.is_highlight)

    def body_selected(self, context):
        return selected_body(context) == self.label

    def test_select(self, context, location):
        # Clicking the sun or moon in the sky picks it up, selected or not.
        if not self.corners: return -1
        # Pick the projected square's interior as well as its outline.
        if not inside_square(self.corners, location): return -1
        # Sky artwork lives at infinity. Let foreground geometry receive clicks
        # instead of selecting a hidden sun/moon through terrain or clouds.
        if foreground_hit(context, location):
            return -1
        return 0


class MCBEDROCK_GGT_sky_bodies(bpy.types.GizmoGroup):
    bl_idname = 'MCBEDROCK_GGT_sky_bodies'
    bl_label = 'Sparkle Sun and Moon'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'PERSISTENT', 'SCALE', 'SHOW_MODAL_ALL'}

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, 'mcbedrock_sky', None)
        space = context.space_data
        if not (supports_sky() and s and s.active and s.world and
                space.show_gizmo and space.overlay.show_overlays and
                context.region_data and context.region_data.is_perspective and
                not context.screen.is_animation_playing):
            return False
        shading = space.shading
        return ((shading.type == 'RENDERED' and shading.use_scene_world_render) or
                (shading.type == 'MATERIAL' and shading.use_scene_world))

    def setup(self, context):
        self.handles = {}
        for body in ('SUN', 'MOON'):
            gizmo = self.gizmos.new('MCBEDROCK_GT_sky_body')
            gizmo.label = body.title()
            gizmo.line_width = 1.5
            gizmo.color = (.95, .65, .22) if body == 'SUN' else (.65, .8, 1)
            gizmo.alpha = .65
            gizmo.color_highlight = (1, .85, .4) if body == 'SUN' else (.85, .95, 1)
            gizmo.alpha_highlight = 1
            gizmo.use_draw_modal = True
            gizmo.use_tooltip = False
            props = gizmo.target_set_operator('mcbedrock.place_sun')
            props.body = body
            props.from_handle = True
            self.handles[body] = gizmo

    def draw_prepare(self, context):
        from . import sky
        s = context.scene.mcbedrock_sky
        direction = sky.time_state(sky.current_time(context.scene), s.azimuth)[0]
        for body, vector, size in (('SUN', direction, s.sun_size), ('MOON', -direction, s.moon_size)):
            gizmo = self.handles[body]
            placement = body_handle(context.region, context.region_data, vector, size, s.azimuth,
                                    6*context.preferences.system.ui_scale) if vector.z > -.12 else None
            gizmo.hide = placement is None
            if placement:
                center, gizmo.corners = placement
                gizmo.matrix_basis.translation = (*center, 0)
