"""Dedicated Posterize page and native fallback, sharing one editing session."""
import math
from pathlib import Path
import traceback

import bpy
from bpy_extras.io_utils import ImportHelper, ExportHelper
import gpu
from gpu_extras.batch import batch_for_shader
import numpy as np

from . import posterize_session as service
from .gpu_framework import Button, Column, Label, Rect, Row, Style, Toggle
from .gpu_framework.motion import blend
from .gpu_framework.navigation import Dropdown
from .styled_pages import actions, button, prop

# The transparency checkerboard behind both previews: 10 px cells, even ones lighter.
CHECKER = ((.34, .34, .34, 1.), (.22, .22, .22, 1.))
CHECKER_CELL = 10.
PROTECT_TINT = (.9, .3, .1, .4)
RANGE_TINT = (.15, .65, 1., .45)
# Overlay texels per side. A zoomed-out large mask pools into about the
# previews' own resolution; any protected pixel keeps its texel lit.
OVERLAY_LIMIT = 1024
# The slider track sits this far from the raised surface toward the muted
# text, so it reads against the panel (3:1) instead of nearly vanishing.
TRACK_LIFT = .6
_PROGRAMS = {}


def _program(kind):
    """The checkerboard or tinted-coverage shader and its unit quad, made on first draw."""
    if kind in _PROGRAMS:
        return _PROGRAMS[kind]
    info = gpu.types.GPUShaderCreateInfo()
    info.push_constant('MAT4', 'ModelViewProjectionMatrix')
    info.push_constant('VEC4', 'bounds')
    info.vertex_in(0, 'VEC2', 'pos')
    info.fragment_out(0, 'VEC4', 'fragColor')
    if kind == 'checker':
        info.push_constant('VEC4', 'colorEven')
        info.push_constant('VEC4', 'colorOdd')
        info.push_constant('FLOAT', 'cell')
        info.vertex_source('void main() { gl_Position = ModelViewProjectionMatrix * vec4(bounds.xy + pos * bounds.zw, 0.0, 1.0); }')
        info.fragment_source('''
            void main() {
                vec2 cellIndex = floor((gl_FragCoord.xy - bounds.xy) / cell);
                fragColor = mod(cellIndex.x + cellIndex.y, 2.0) >= 1.0 ? colorOdd : colorEven;
            }
        ''')
    else:
        interface = gpu.types.GPUStageInterfaceInfo('posterize_overlay_uv')
        interface.smooth('VEC2', 'uv')
        info.vertex_out(interface)
        info.sampler(0, 'FLOAT_2D', 'coverageMap')
        info.push_constant('VEC4', 'tint')
        info.vertex_source('void main() { uv = pos; gl_Position = ModelViewProjectionMatrix * vec4(bounds.xy + pos * bounds.zw, 0.0, 1.0); }')
        info.fragment_source('''
            void main() {
                ivec2 size = textureSize(coverageMap, 0);
                ivec2 texel = clamp(ivec2(floor(uv * vec2(size))), ivec2(0), size - ivec2(1));
                fragColor = vec4(tint.rgb, tint.a * texelFetch(coverageMap, texel, 0).r);
            }
        ''')
    try:
        shader = gpu.shader.create_from_info(info)
        _PROGRAMS[kind] = shader, batch_for_shader(shader, 'TRI_FAN', {'pos': [(0, 0), (1, 0), (1, 1), (0, 1)]})
    except Exception:  # a GPU backend that rejects it loses the checker or overlay, not the page
        traceback.print_exc()
        _PROGRAMS[kind] = None
    return _PROGRAMS[kind]


def _draws(renderer):
    """Renderers that put pixels on screen: not a record-only pass or a test recorder."""
    return (not getattr(renderer, 'record_only', False)
            and all(hasattr(renderer, name) for name in ('ui_scale', 'offset_x', 'offset_y', 'linear_output')))


def _bounds(renderer, rect):
    scale = renderer.ui_scale
    return ((rect.x+renderer.offset_x)*scale, (rect.y+renderer.offset_y)*scale, rect.width*scale, rect.height*scale)


def _shade(renderer, color):
    """``color`` as the renderer's own rects draw it: linear in viewport passes, faded with the panel."""
    rgb = tuple(max(0., c)**2.2 for c in color[:3]) if renderer.linear_output else tuple(color[:3])
    return (*rgb, color[3]*getattr(renderer, 'opacity', 1.))


def _bind(renderer, kind, rect):
    program = _program(kind)
    if program is None:
        return None, None
    shader, batch = program
    shader.bind()
    matrix = getattr(renderer, '_matrix', None)
    if matrix is None:
        matrix = gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix()
    shader.uniform_float('ModelViewProjectionMatrix', matrix)
    shader.uniform_float('bounds', _bounds(renderer, rect))
    return shader, batch


def draw_checker(renderer, rect):
    """The checkerboard behind a preview in one draw call, not one per cell."""
    if rect.width <= 0 or rect.height <= 0 or not _draws(renderer):
        return
    shader, batch = _bind(renderer, 'checker', rect)
    if shader is None:
        return
    shader.uniform_float('colorEven', _shade(renderer, CHECKER[0]))
    shader.uniform_float('colorOdd', _shade(renderer, CHECKER[1]))
    shader.uniform_float('cell', CHECKER_CELL*renderer.ui_scale)
    gpu.state.blend_set('ALPHA')
    batch.draw(shader)


def overlay_window(shape, uv):
    """The source pixels ``(x0, x1, y0, y1)`` a preview's ``uv`` window shows."""
    h, w = shape
    u0, v0, u1, v1 = uv
    return max(0, int(u0*w)), min(w, math.ceil(u1*w)), max(0, int(v0*h)), min(h, math.ceil(v1*h))


def overlay_coverage(values, window, match=None, limit=OVERLAY_LIMIT):
    """Coverage of the visible ``window`` of a mask (or of ``values == match``).

    Returns ``(coverage, factor)``: float texels, each standing for
    ``factor`` × ``factor`` source pixels; a texel is covered when any of
    its pixels is, so a single protected pixel still shows when zoomed out.
    """
    x0, x1, y0, y1 = window
    part = values[y0:y1, x0:x1]
    part = np.asarray(part == match if match is not None else part, bool)
    height, width = part.shape
    factor = max(1, math.ceil(max(height, width, 1)/limit))
    if factor > 1:
        padded = np.zeros((math.ceil(height/factor)*factor, math.ceil(width/factor)*factor), bool)
        padded[:height, :width] = part
        part = padded.reshape(padded.shape[0]//factor, factor, padded.shape[1]//factor, factor).any(axis=(1, 3))
    return part.astype(np.float32), factor


def overlay_request(session, s):
    """What tints the previews: protection while a brush is chosen, else the highlighted range."""
    if s.tool in {'PROTECT', 'ERASE'}:
        return 'mask', session.mask_version, PROTECT_TINT
    if s.highlight_range and s.lock_groups and session.ready and session.group_map is not None:
        return 'range', (session.result_version, s.range_index), RANGE_TINT
    return None


def _overlay_texture(session, key, build):
    """One texture per overlay state, shared by both previews and every view of them."""
    cache = session.overlays
    if key in cache:
        cache[key] = cache.pop(key)  # most recent last
        return cache[key]
    coverage, factor = build()
    entry = None
    if coverage.size:
        height, width = coverage.shape
        data = np.ascontiguousarray(coverage, np.float32).ravel()
        try:
            texture = gpu.types.GPUTexture((width, height), format='R8',
                                           data=gpu.types.Buffer('FLOAT', data.size, data))
            entry = texture, width, height, factor
        except (RuntimeError, TypeError, ValueError):
            traceback.print_exc()
    cache[key] = entry
    while len(cache) > 4:
        cache.pop(next(iter(cache)))
    return entry


def draw_overlay(renderer, session, rect, uv, kind, version, tint):
    """Tint protected pixels, or the highlighted range, over a preview as one texture."""
    if not _draws(renderer):
        return
    s = service.settings()
    shape = session.mask.shape
    window = overlay_window(shape, uv)
    x0, x1, y0, y1 = window
    if x1 <= x0 or y1 <= y0:
        return
    if kind == 'mask':
        build = lambda: overlay_coverage(session.mask, window)
    else:
        build = lambda: overlay_coverage(session.group_map, window, match=s.range_index)
    entry = _overlay_texture(session, (kind, version, window), build)
    if entry is None:
        return
    texture, width, height, factor = entry
    h, w = shape
    u0, v0, u1, v1 = uv
    sx, sy = rect.width/max(1e-9, (u1-u0)*w), rect.height/max(1e-9, (v1-v0)*h)
    quad = Rect(rect.x+(x0-u0*w)*sx, rect.y+(y0-v0*h)*sy, width*factor*sx, height*factor*sy)
    shader, batch = _bind(renderer, 'overlay', quad)
    if shader is None:
        return
    shader.uniform_float('tint', _shade(renderer, tint))
    shader.uniform_sampler('coverageMap', texture)
    gpu.state.blend_set('ALPHA')
    batch.draw(shader)


def _luminance(color):
    def channel(value):
        value = max(0., float(value))
        return value/12.92 if value <= .04045 else ((value+.055)/1.055)**2.4
    r, g, b = (channel(value) for value in color[:3])
    return .2126*r+.7152*g+.0722*b


def contrast(first, second):
    """WCAG contrast ratio of two sRGB colors."""
    light, dark = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (light+.05)/(dark+.05)


class PosterizeSlider(Button):
    def __init__(self, name, label):
        self.name, self.label = name, label
        super().__init__('posterize.'+name, '', lambda ctx: None, style=Style(height=42))

    def draw(self, renderer, state, theme):
        value = getattr(service.settings(), self.name)
        r = self.rect
        renderer.text(Rect(r.x, r.y+19, r.width, 23), f'{self.label}: {value}', theme.text, size=11)
        renderer.rect(Rect(r.x+5, r.y+9, r.width-10, 4), blend(theme.surface_raised, theme.text_muted, TRACK_LIFT), radius=2)
        renderer.rect(Rect(r.x+5, r.y+9, (r.width-10)*value/100, 4), theme.accent, radius=2)
        renderer.rect(Rect(r.x+(r.width-10)*value/100, r.y+6, 10, 10), theme.text, radius=5)

    def pointer(self, x):
        set_percent(self.name, round(100*max(0, min(1, (x-self.rect.x-5)/max(1,self.rect.width-10)))))


def set_percent(name, value):
    """Set a slider's setting only when it changes: every set converts and redraws."""
    s = service.settings()
    if getattr(s, name) != value:
        setattr(s, name, value)


class PreviewCanvas(Button):
    """Shared viewport transform and image-space painting for either preview."""
    def __init__(self, result=False):
        self.result = result
        super().__init__('posterize.preview.'+('result' if result else 'original'), '', lambda ctx: None,
                         style=Style(height=190, flex=1))

    def image_rect(self):
        r = self.rect
        area = Rect(r.x+2, r.y+2, max(1, r.width-4), max(1, r.height-24))
        session = service.SESSION
        if not session:
            return area
        h, w = session.source.shape[:2]
        ratio = min(area.width/w, area.height/h)
        return Rect(area.x+(area.width-w*ratio)/2, area.y+(area.height-h*ratio)/2, w*ratio, h*ratio)

    def uv(self):
        s = service.settings()
        span = 1/s.zoom
        x = max(span/2, min(1-span/2, s.pan_x))
        y = max(span/2, min(1-span/2, s.pan_y))
        return x-span/2, y-span/2, x+span/2, y+span/2

    def pixel(self, x, y):
        r = self.image_rect()
        if not r.contains(x, y) or not service.SESSION:
            return None
        u0, v0, u1, v1 = self.uv()
        h, w = service.SESSION.source.shape[:2]
        return (min(w-1, max(0, int((u0+(x-r.x)/r.width*(u1-u0))*w))),
                min(h-1, max(0, int((v0+(y-r.y)/r.height*(v1-v0))*h))))

    def draw(self, renderer, state, theme):
        renderer.rect(self.rect, theme.surface, radius=3)
        renderer.text(Rect(self.rect.x, self.rect.top-22, self.rect.width, 22),
                      'After' if self.result else 'Before', theme.text, size=11, align='CENTER')
        session = service.SESSION
        if not session:
            return
        r = self.image_rect()
        uv = self.uv()
        renderer.push_clip(r)
        try:
            draw_checker(renderer, r)
            image = session.result_image if self.result else session.original_image
            if image:
                renderer.image(r, image, nearest=True, uv_rect=uv)
            else:
                renderer.text(r, 'Processing…', theme.text, size=11, align='CENTER')
            overlay = overlay_request(session, service.settings())
            if overlay:
                draw_overlay(renderer, session, r, uv, *overlay)
            if getattr(renderer, 'capture', False):
                # New pixels, zoom, pan or overlay change what this preview
                # shows without changing the commands above: let the sidebar see it.
                renderer.commands.append(('posterize.preview', self.key, r, uv,
                                          session.result_version if self.result else 0,
                                          overlay[:2] if overlay else None))
        finally:
            renderer.pop_clip()


class LockedSwatch(Button):
    def __init__(self,index,color):
        self.color = color
        super().__init__('posterize.lock.'+str(index),'',self.unlock,
                         style=Style(width=24,height=24),tooltip='Click to release this source color')

    def unlock(self,context):
        if service.SESSION:
            service.SESSION.locked.discard(self.color)
            service.SESSION.request()

    def draw(self,renderer,state,theme):
        renderer.rect(self.rect,(*self.color,1),radius=3,border=theme.text_muted)
        if state.get('_runtime_hover') == self.key:
            # Light or dark, whichever stands out on this kept color.
            cross = max((theme.text, theme.canvas), key=lambda ink: contrast(ink, self.color))
            renderer.text(self.rect,'×',cross,size=14,align='CENTER')


def paint(canvas, x, y, previous=None):
    session = service.SESSION
    point = canvas.pixel(x,y)
    if not session or point is None:
        return point
    s = service.settings()
    if s.tool == 'GROUP':
        px,py = point
        if session.source[py,px,3] > 0:
            from ..builders.posterize import hsv_values
            hue,saturation,value = (float(v) for v in hsv_values(session.source[py,px,:3]))
            bpy.ops.mcbedrock.posterize_range('INVOKE_DEFAULT',action='ADD',label='Sampled range',
                hue_min=(hue-8)%360,hue_max=(hue+8)%360,
                sat_min=max(0,saturation-20),sat_max=min(100,saturation+20),
                value_min=max(0,value-35),value_max=min(100,value+35),shades=1)
    elif s.tool == 'LOCK':
        px,py = point
        if session.source[py,px,3] > 0:
            color = tuple(float(c) for c in session.source[py,px,:3])
            if color in session.locked:
                session.locked.remove(color)
            else:
                session.locked.add(color)
            session.request()
    elif s.tool in {'PROTECT','ERASE'}:
        # Interpolate brush positions to avoid gaps on fast pointer movement.
        start = previous or point
        steps = max(abs(point[0]-start[0]), abs(point[1]-start[1]), 1)
        h,w = session.mask.shape
        radius = s.radius
        for t in np.linspace(0,1,steps+1):
            px,py = (round(start[i]+(point[i]-start[i])*t) for i in range(2))
            x0,x1 = max(0,px-radius+1), min(w,px+radius)
            y0,y1 = max(0,py-radius+1), min(h,py+radius)
            yy,xx = np.ogrid[y0:y1,x0:x1]
            disk = (xx-px)**2+(yy-py)**2 < radius**2
            view = session.mask[y0:y1,x0:x1]
            view[disk] = s.tool == 'PROTECT'
        session.mask_edited()
    return point


def handle_event(context, event, state, x, y, tree):
    if state.get('_menu') or not service.SESSION:
        return False
    drag = state.get('_posterize_drag')
    if drag:
        widget = next((w for w in tree.walk() if w.key == drag['key']), None)
        if widget is None:
            state.pop('_posterize_drag', None)
            return False
        if event.type in {'ESC','RIGHTMOUSE'}:
            if isinstance(widget,PosterizeSlider):
                set_percent(widget.name,drag['value'])
            elif 'mask' in drag:
                service.SESSION.mask[:] = drag['mask']
                service.SESSION.mask_edited()
            state.pop('_posterize_drag',None)
            return True
        if event.type == 'MOUSEMOVE':
            if isinstance(widget, PosterizeSlider):
                widget.pointer(x)
            elif service.settings().tool == 'PAN':
                r = widget.image_rect()
                s = service.settings()
                half = .5/s.zoom
                s.pan_x = max(half,min(1-half,drag['pan'][0]-(x-drag['mouse'][0])/r.width/s.zoom))
                s.pan_y = max(half,min(1-half,drag['pan'][1]-(y-drag['mouse'][1])/r.height/s.zoom))
            else:
                drag['last'] = paint(widget,x,y,drag.get('last'))
            return True
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            state.pop('_posterize_drag',None)
            return True
        return False
    hit = tree.hit_test(x,y)
    if isinstance(hit,PreviewCanvas) and event.type in {'WHEELUPMOUSE','WHEELDOWNMOUSE'}:
        s = service.settings()
        s.zoom = max(1,min(32,s.zoom*(1.25 if event.type=='WHEELUPMOUSE' else .8)))
        return True
    if event.type in {'LEFT_ARROW','RIGHT_ARROW','HOME','END'} and event.value == 'PRESS':
        focused = next((w for w in tree.walk() if w.key==state.get('_runtime_focus')),None)
        if isinstance(focused,PosterizeSlider):
            value = getattr(service.settings(),focused.name)+(1 if event.type=='RIGHT_ARROW' else -1)
            set_percent(focused.name,0 if event.type=='HOME' else 100 if event.type=='END' else max(0,min(100,value)))
            return True
    if event.type != 'LEFTMOUSE' or event.value != 'PRESS':
        return False
    if isinstance(hit,PosterizeSlider):
        state['_runtime_focus'] = hit.key
        state['_posterize_drag'] = dict(key=hit.key,value=getattr(service.settings(),hit.name))
        hit.pointer(x)
        return True
    if isinstance(hit,PreviewCanvas):
        s = service.settings()
        drag = dict(key=hit.key,mouse=(x,y),pan=(s.pan_x,s.pan_y))
        if s.tool in {'PROTECT','ERASE'}:
            drag['mask'] = service.SESSION.mask.copy()
        drag['last'] = paint(hit,x,y)
        if s.tool not in {'LOCK','GROUP'}:
            state['_posterize_drag'] = drag
        return True
    return False


def command(action, text=None, enabled=True):
    return button('posterize.action.'+action,text or action.title(),'mcbedrock.posterize_action',
                  action=action,enabled=enabled,invoke_default=False)


def range_button(action,text):
    return button('posterize.range.'+action,text,'mcbedrock.posterize_range',action=action,
                  invoke_default=action in {'ADD','EDIT'})


def range_controls(s,session):
    children = [Toggle('posterize.range_lock','Lock color ranges',lambda ctx:s.lock_groups,
                       lambda ctx,v:setattr(s,'lock_groups',v)),
                Toggle('posterize.range_advanced','Advanced color ranges',lambda ctx:s.advanced_ranges,
                       lambda ctx,v:setattr(s,'advanced_ranges',v))]
    if not s.lock_groups:
        children.append(Label('posterize.range_warning','Unlocked: colors may mix across hues.',muted=True))
    if not s.advanced_ranges:
        return children
    children.extend([Label('posterize.range_priority','First matching group wins overlaps.',muted=True),
                     Label('posterize.range_unmatched','Unmatched colors stay unchanged.',muted=True)])
    if s.ranges:
        index = min(s.range_index,len(s.ranges)-1)
        group = s.ranges[index]
        children.append(Toggle('posterize.range_highlight','Highlight selected range',lambda ctx:s.highlight_range,
                               lambda ctx,v:setattr(s,'highlight_range',v)))
        children.append(Dropdown('posterize.range_select',lambda ctx:str(s.range_index),
            lambda ctx,v:setattr(s,'range_index',int(v)),[(str(i),g.label) for i,g in enumerate(s.ranges)],style=Style(height=32)))
        children.append(Label('posterize.range_hue',f'Hue {group.hue_min:g}° → {group.hue_max:g}°',muted=True))
        children.append(Label('posterize.range_sv',f'S {group.sat_min:g}–{group.sat_max:g}% · Brightness {group.value_min:g}–{group.value_max:g}%',muted=True))
        stats = session.stats.get('groups',[]) if session.ready else []
        if index<len(stats):
            g = stats[index]
            children.append(Label('posterize.range_stats',f"{g['pixels']} pixels · {g['source']} → {g['result']} colors",muted=True))
        children.extend([actions('posterize.range_edit',[range_button('EDIT','Edit Range'),range_button('REMOVE','Remove')]),
                         actions('posterize.range_order',[range_button('UP','Move Up'),range_button('DOWN','Move Down')])])
    children.extend([actions('posterize.range_add',[range_button('ADD','Add Range'),
                      Button('posterize.range_sample','Sample Pixel',lambda ctx:setattr(s,'tool','GROUP'),style=Style(height=32,align='CENTER'))]),
                     range_button('RESET','Reset Ranges')])
    return children


def build_posterize(context,state):
    s = service.settings(context)
    session = service.SESSION
    children = [Label('posterize.heading','Posterize',style=Style(height=28,font_size=15,font_weight='SemiBold')),
        actions('posterize.sources',[button('posterize.open','Open Image','mcbedrock.posterize_open'),
                                    command('PACK','Choose Pack Texture')]),
        actions('posterize.modelsource',[button('posterize.model','Use Model Texture','mcbedrock.posterize_target',use_source=True),
            button('posterize.existing','Blender Image','mcbedrock.posterize_image')])]
    if not session:
        children.append(Label('posterize.empty','Choose a static sRGB color image to begin.',muted=True))
        return Column('posterize.content',children,style=Style(gap=8))
    status = Label('posterize.status',session.status,muted=True)
    status.tooltip = session.status
    children.extend([Label('posterize.source',session.source_name,muted=True),
        Row('posterize.previews',[PreviewCanvas(),PreviewCanvas(True)],style=Style(height=190,gap=6)),
        actions('posterize.view',[button('posterize.large','Larger Preview','mcbedrock.posterize_canvas'),command('FIT','Fit')]),
        Dropdown('posterize.tool',lambda ctx:s.tool,lambda ctx,v:setattr(s,'tool',v),
            [('PAN','Pan previews'),('PROTECT','Protect details'),('ERASE','Erase protection'),('LOCK','Keep color: click pixel'),('GROUP','Sample color range')],
            style=Style(height=32)),
        prop('posterize.radius',s,'radius','Brush radius'),
        Label('posterize.mask',f'{int(session.mask.sum())} protected pixels · {len(session.locked)} kept colors',muted=True),
        *[Row('posterize.locks.'+str(start),[LockedSwatch(i,color) for i,color in enumerate(sorted(session.locked)[start:start+7],start)],
              style=Style(height=24,gap=4)) for start in range(0,len(session.locked),7)],
        actions('posterize.clear',[command('CLEAR_MASK','Clear Protection'),command('CLEAR_LOCKS','Clear Color Locks')]),
        PosterizeSlider('strength','Posterization'),PosterizeSlider('cleanup','Detail cleanup'),
        *range_controls(s,session),
        Toggle('posterize.tileable','Tileable',lambda ctx:s.tileable,lambda ctx,v:setattr(s,'tileable',v)),
        status,
        button('posterize.target','Choose Model Target','mcbedrock.posterize_target'),
        Label('posterize.targetname',f'{s.target.name} / slot {s.slot} / {s.node}' if s.target else 'No model target',muted=True),
        Toggle('posterize.layers','All model layers',lambda ctx:s.model_layers,lambda ctx,v:setattr(s,'model_layers',v)),
        Toggle('posterize.live','Live model preview',lambda ctx:s.live,lambda ctx,v:setattr(s,'live',v)),
        Toggle('posterize.original','Show original on model',lambda ctx:s.original,lambda ctx,v:setattr(s,'original',v)),
        actions('posterize.commit',[Button('posterize.apply','Apply',lambda ctx:bpy.ops.mcbedrock.posterize_apply('EXEC_DEFAULT',True),
                                        style=Style(height=32,align='CENTER',enabled=session.ready and bool(s.target and s.node))),
                                    button('posterize.save','Save PNG','mcbedrock.posterize_save',enabled=session.ready)]),
        actions('posterize.finish',[command('RESET','Reset'),command('CANCEL','Cancel')])])
    return Column('posterize.content',children,style=Style(gap=7))


class MCBEDROCK_OT_posterize_open(bpy.types.Operator,ImportHelper):
    bl_idname = 'mcbedrock.posterize_open'
    bl_label = 'Open Image in Posterize'
    filter_glob: bpy.props.StringProperty(default='*.png;*.jpg;*.jpeg;*.tga;*.bmp;*.tif;*.tiff;*.webp',options={'HIDDEN'})

    def execute(self,context):
        image = None
        try:
            image = bpy.data.images.load(self.filepath,check_existing=False)
            service.open_source(image,context)
            return {'FINISHED'}
        except (ValueError,RuntimeError,OSError) as exc:
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}
        finally:
            if image and image.users == 0:
                bpy.data.images.remove(image)


class MCBEDROCK_OT_posterize_image(bpy.types.Operator):
    bl_idname = 'mcbedrock.posterize_image'
    bl_label = 'Use Blender Image'
    image: bpy.props.StringProperty(name='Image')

    def invoke(self,context,event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self,context):
        self.layout.prop_search(self,'image',bpy.data,'images')

    def execute(self,context):
        image = bpy.data.images.get(self.image)
        if not image:
            self.report({'ERROR'},'Choose an existing image')
            return {'CANCELLED'}
        try:
            service.open_source(image,context)
            return {'FINISHED'}
        except ValueError as exc:
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}


class MCBEDROCK_OT_posterize_range(bpy.types.Operator):
    bl_idname = 'mcbedrock.posterize_range'
    bl_label = 'Posterize Color Range'
    __annotations__ = service.range_properties()
    action: bpy.props.StringProperty(default='EDIT',options={'HIDDEN'})

    def invoke(self,context,event):
        s = service.settings(context)
        if self.action == 'EDIT':
            if not 0<=s.range_index<len(s.ranges):
                return {'CANCELLED'}
            for key in service.RANGE_FIELDS:
                setattr(self,key,getattr(s.ranges[s.range_index],key))
        from .gpu_framework import runtime
        runtime._NATIVE_DIALOG_OPEN = True
        return context.window_manager.invoke_props_dialog(self,width=400)

    def draw(self,context):
        layout = self.layout
        layout.prop(self,'label')
        layout.prop(self,'enabled')
        for low,high in [('hue_min','hue_max'),('sat_min','sat_max'),('value_min','value_max')]:
            row = layout.row(align=True)
            row.prop(self,low)
            row.prop(self,high)
        layout.label(text='Hue start > end wraps through red (0°).')
        layout.prop(self,'shades')
        layout.prop(self,'preserve')
        layout.prop(self,'separate_muted')
        layout.label(text='Only source colors in this range can replace each other.')
        layout.label(text='New ranges are inserted first. Unmatched pixels are kept.')

    def execute(self,context):
        from .gpu_framework import runtime
        runtime._NATIVE_DIALOG_OPEN = False
        s = service.settings(context)
        index = min(s.range_index,max(0,len(s.ranges)-1))
        if self.action in {'ADD','EDIT'}:
            if self.sat_min>self.sat_max or self.value_min>self.value_max:
                self.report({'ERROR'},'Minimum saturation/brightness must not exceed maximum')
                return {'CANCELLED'}
            if self.action == 'EDIT' and not s.ranges:
                return {'CANCELLED'}
            group = s.ranges.add() if self.action=='ADD' else s.ranges[index]
            for key in service.RANGE_FIELDS:
                setattr(group,key,getattr(self,key))
            group.name = group.label
            if self.action=='ADD':
                s.ranges.move(len(s.ranges)-1,0)
                s.range_index = 0
            s.advanced_ranges = True
        elif self.action == 'RESET':
            service.reset_ranges(s)
        elif s.ranges and self.action == 'REMOVE':
            s.ranges.remove(index)
            s.range_index = min(index,max(0,len(s.ranges)-1))
        elif s.ranges and self.action in {'UP','DOWN'}:
            new = max(0,min(len(s.ranges)-1,index+(-1 if self.action=='UP' else 1)))
            s.ranges.move(index,new)
            s.range_index = new
        if service.SESSION:
            service.SESSION.request()
        service.redraw()
        return {'FINISHED'}

    def cancel(self,context):
        from .gpu_framework import runtime
        runtime._NATIVE_DIALOG_OPEN = False


# Search label -> (object, material slot, image node name, image), selection first.
_TARGETS = {}


def collect_targets(context):
    """Every image texture a model can be posterized through, labelled image first.

    The selection and its model come first. A model lists each material's
    image node once, however many of its meshes share that material, so a
    schematic or world of thousands of chunks stays a short list.
    """
    scene = context.scene
    selected = set(context.selected_objects)
    active = context.active_object
    if active:
        selected.add(active)
        selected.update(service.model_meshes(scene,active))
    images, seen, roots, targets = {}, set(), {}, {}
    for obj in sorted(scene.objects,key=lambda obj:(obj not in selected,obj.name)):
        for index,slot in enumerate(obj.material_slots):
            material = slot.material
            if not material or not material.use_nodes or not material.node_tree:
                continue
            nodes = images.get(material)
            if nodes is None:
                nodes = images[material] = [(node.name,node.image) for node in material.node_tree.nodes
                                            if node.type == 'TEX_IMAGE' and node.image]
            if not nodes:
                continue
            root = roots.get(obj)
            if root is None:
                root = roots[obj] = service.model_root(obj)
            for name,image in nodes:
                if (root,material,name) in seen:
                    continue
                seen.add((root,material,name))
                label = f'{image.name} · {obj.name} · {material.name} [{index}] · {name}'
                while label in targets:  # linked objects may share a name
                    label += ' '
                targets[label] = (obj,index,name,image)
    return targets


def target_search(self,context,edit_text):
    # Blender filters and pages these as the user types, drawing visible rows only.
    return list(_TARGETS)


def _target_for(text):
    """The target a typed label means: that label, else the shortest one containing the text."""
    if text in _TARGETS:
        return _TARGETS[text]
    lowered = text.strip().lower()
    matches = sorted((label for label in _TARGETS if lowered and lowered in label.lower()),key=len)
    return _TARGETS[matches[0]] if matches else None


class MCBEDROCK_OT_posterize_target(bpy.types.Operator):
    bl_idname = 'mcbedrock.posterize_target'
    bl_label = 'Choose Model Texture Target'
    use_source: bpy.props.BoolProperty(default=False,options={'HIDDEN'})
    texture: bpy.props.StringProperty(name='Texture',
        description='Image texture to edit. Type to search images, objects and materials; the selection and its model come first',
        search=target_search)

    def invoke(self,context,event):
        if service.SESSION:
            service.SESSION.restore()
        _TARGETS.clear()
        _TARGETS.update(collect_targets(context))
        if not _TARGETS:
            self.report({'ERROR'},'No model image textures found in this scene')
            service.preview_changed(service.settings(context),context)
            return {'CANCELLED'}
        self.texture = next(iter(_TARGETS))
        return context.window_manager.invoke_props_dialog(self,width=620)

    def draw(self,context):
        self.layout.prop(self,'texture',icon='IMAGE_DATA')

    def execute(self,context):
        if not _TARGETS:
            _TARGETS.update(collect_targets(context))
        target = _target_for(self.texture)
        try:
            alive = target is not None and target[0].name in context.scene.objects
        except ReferenceError:  # removed since the list was made
            alive = False
        if not alive:
            self.report({'ERROR'},'Choose a texture target again')
            return {'CANCELLED'}
        obj,index,node,image = target
        try:
            if self.use_source:
                service.open_source(image,context)
            s = service.settings(context)
            s.target,s.slot,s.node = obj,index,node
            service.preview_changed(s,context)
            return {'FINISHED'}
        except (ValueError,ReferenceError) as exc:
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}

    def cancel(self,context):
        service.preview_changed(service.settings(context),context)


class MCBEDROCK_OT_posterize_action(bpy.types.Operator):
    bl_idname = 'mcbedrock.posterize_action'
    bl_label = 'Posterize Action'
    action: bpy.props.EnumProperty(items=[(x,x,x) for x in ('PACK','FIT','CLEAR_MASK','CLEAR_LOCKS','RESET','CANCEL')])

    def execute(self,context):
        s,session = service.settings(context),service.SESSION
        if self.action == 'PACK':
            from .gpu_framework import runtime
            from .make_panels import Carousel
            context.scene.mcbedrock_browser_filter = 'TEXTURE'
            context.scene.mcbedrock_browser_search = ''
            packs = context.scene.mcbedrock_active_packs
            state = next((ui.state for ui in runtime._ACTIVE.values()),{})
            if packs:
                try:
                    Carousel('assets.carousel',packs,state).load_into_browser(context,state)
                except (OSError,RuntimeError) as exc:
                    self.report({'ERROR'},str(exc))
                    return {'CANCELLED'}
            for ui in runtime._ACTIVE.values():
                ui.state['app.active_tab'] = 'ASSETS'
            self.report({'INFO'},'Select a pack texture in Assets, then choose Posterize')
        elif self.action == 'CANCEL':
            service.close()
        elif self.action == 'FIT':
            s.zoom,s.pan_x,s.pan_y = 1,.5,.5
        elif session:
            if self.action in {'CLEAR_MASK','RESET'}:
                session.mask[:] = False
                session.mask_version += 1
            if self.action in {'CLEAR_LOCKS','RESET'}:
                session.locked.clear()
            if self.action == 'RESET':
                s.strength,s.cleanup,s.tileable = 50,0,False
                s.live,s.original = False,False
                s.zoom,s.pan_x,s.pan_y = 1,.5,.5
                s.lock_groups = True
                service.reset_ranges(s)
            session.request()
        service.redraw()
        return {'FINISHED'}


class MCBEDROCK_OT_posterize_apply(bpy.types.Operator):
    bl_idname = 'mcbedrock.posterize_apply'
    bl_label = 'Apply Posterized Texture'
    bl_options = {'UNDO'}

    def execute(self,context):
        session = service.SESSION
        if not session:
            return {'CANCELLED'}
        try:
            session.apply()
            service.settings(context).live = False
            service.redraw()
            return {'FINISHED'}
        except (ValueError,RuntimeError,ReferenceError) as exc:
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}


class MCBEDROCK_OT_posterize_save(bpy.types.Operator,ExportHelper):
    bl_idname = 'mcbedrock.posterize_save'
    bl_label = 'Save Posterized PNG'
    filename_ext = '.png'
    filter_glob: bpy.props.StringProperty(default='*.png',options={'HIDDEN'})

    def invoke(self,context,event):
        if service.SESSION:
            self.filepath = Path(service.SESSION.source_name).stem+'_posterized.png'
        return ExportHelper.invoke(self,context,event)

    def execute(self,context):
        try:
            if not service.SESSION:
                raise ValueError('Open a source image first')
            service.SESSION.export(self.filepath)
            service.redraw()
            return {'FINISHED'}
        except (ValueError,OSError) as exc:
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}


_CANVASES = []
TOOL_KEYS = {'P':'PAN','B':'PROTECT','E':'ERASE','K':'LOCK'}
# Close first: a narrow viewport cuts the end of the line, never the way out.
HINT = 'Esc: close · Wheel: zoom · P: pan · B: protect · E: erase · K: keep color'
# Clicks and the wheel over the preview's plate never reach the viewport underneath.
PLATE_EVENTS = {'LEFTMOUSE','MIDDLEMOUSE','RIGHTMOUSE','BUTTON4MOUSE','BUTTON5MOUSE','BUTTON6MOUSE',
                'BUTTON7MOUSE','WHEELUPMOUSE','WHEELDOWNMOUSE','WHEELINMOUSE','WHEELOUTMOUSE',
                'TRACKPADPAN','TRACKPADZOOM','MOUSEROTATE','MOUSESMARTZOOM'}


def _pointer(value):
    try:
        return int(value.as_pointer())
    except (AttributeError, ReferenceError):
        return 0


def free_area(area, region, scale):
    """The part of ``region`` no toolbar, sidebar, header or asset shelf covers, in logical px."""
    left, right, bottom, top = 0., float(region.width), 0., float(region.height)
    for other in area.regions:
        if other.type == 'WINDOW' or other.width <= 1 or other.height <= 1:
            continue
        # Only regions drawn over the viewport (region overlap) take room from it.
        if not (region.x <= other.x and other.x+other.width <= region.x+region.width
                and region.y <= other.y and other.y+other.height <= region.y+region.height):
            continue
        x, y = other.x-region.x, other.y-region.y
        if other.height >= other.width:  # toolbar or sidebar
            if x+other.width/2 < region.width/2:
                left = max(left, x+other.width)
            else:
                right = min(right, x)
        elif y+other.height/2 > region.height/2:  # header, tool settings
            top = min(top, y)
        else:  # asset shelf
            bottom = max(bottom, y+other.height)
    return Rect(left/scale, bottom/scale, max(0., right-left)/scale, max(0., top-bottom)/scale)


class PreviewRow(Row):
    def walk(self):
        yield self
        yield from self.children


class LargerPreview:
    """The Larger Preview over one 3D viewport, kept outside the operator's RNA.

    Its viewport is looked up again by pointer for every event, so closing,
    joining or changing that area ends it without touching freed memory,
    and Ctrl+Space (which moves the region to a new area) keeps it.
    """

    def __init__(self, window, area, region):
        self.keys = (_pointer(window), _pointer(area), _pointer(region))
        self.window, self.area, self.region = window, area, region
        self.state = {}
        self.tree = PreviewRow('posterize.large',[PreviewCanvas(),PreviewCanvas(True)],style=Style(gap=12))
        self.renderer = None
        self.scale = 1.
        self.plate = None
        self.closed = self.failed = False
        self.handler = bpy.types.SpaceView3D.draw_handler_add(self.draw,(),'WINDOW','POST_PIXEL')
        _CANVASES.append(self)

    def valid(self):
        """Find the viewport region again in its window's current screen."""
        try:
            window = next((item for item in bpy.context.window_manager.windows
                           if _pointer(item) == self.keys[0]), None)
            if window is None:
                return False
            for area in window.screen.areas:
                if area.type != 'VIEW_3D':
                    continue
                region = next((item for item in area.regions if _pointer(item) == self.keys[2]), None)
                if region is not None:
                    self.window, self.area, self.region = window, area, region
                    return True
        except (AttributeError, ReferenceError):
            pass
        return False

    def tag_redraw(self):
        if self.valid():
            self.region.tag_redraw()

    def mouse(self, event):
        return (event.mouse_x-self.region.x)/self.scale, (event.mouse_y-self.region.y)/self.scale

    def in_area(self, event):
        area = self.area
        return area.x <= event.mouse_x < area.x+area.width and area.y <= event.mouse_y < area.y+area.height

    def draw(self):
        # Only in its own region: Quad View's other quarters and other viewports draw nothing.
        if (self.closed or self.failed or not service.SESSION or not self.keys[2]
                or _pointer(bpy.context.region) != self.keys[2]):
            return
        try:
            self.paint(bpy.context)
        except Exception:
            traceback.print_exc()
            self.failed = True  # the next event closes it

    def paint(self, context):
        from .custom_ui import _addon_preferences
        from .gpu_framework.renderer import BlenderRenderer
        from .gpu_framework.scaling import viewport_scale
        from .gpu_framework.theme_adapter import resolve_theme
        region = context.region
        preferences = _addon_preferences(context)
        theme = resolve_theme(context.preferences, getattr(preferences, 'ui_theme', 'BLENDER'))
        # Sparkle's interface scale, like the sidebar: HiDPI and Resolution Scale apply.
        self.scale = max(.5, viewport_scale(context.preferences, region.height,
                                            adaptive=getattr(preferences, 'adaptive_ui_scale', True),
                                            multiplier=getattr(preferences, 'custom_ui_scale', 1.0)))
        if self.renderer is None or self.renderer.ui_scale != self.scale:
            self.renderer = BlenderRenderer(region, self.scale)
        free = free_area(context.area, region, self.scale)
        width = max(100,min(1000,free.width-60))
        height = max(100,min(620,free.height-100))
        rect = Rect(free.x+30,free.y+50,width,height)
        self.plate = Rect(free.x+20,free.y+20,width+20,height+70)
        self.tree.layout(rect,self.state,theme)
        # Row children have a sidebar height by default; enlarge their canvases.
        for child in self.tree.children:
            child.rect = Rect(child.rect.x,rect.y,child.rect.width,rect.height)
        renderer = self.renderer
        renderer.linear_output = True  # the viewport presents through a display transform
        renderer.begin()
        try:
            renderer.rect(self.plate,theme.canvas,radius=8)
            renderer.text(Rect(rect.x,free.y+25,width,22),HINT,theme.text,size=12)
            self.tree.draw(renderer,self.state,theme)
        finally:
            renderer.end()

    def modal(self, context, event):
        if self.closed or self.failed or not service.SESSION or not self.valid():
            self.finish(context)
            return {'CANCELLED','PASS_THROUGH'}
        x,y = self.mouse(event)
        over = self.plate is not None and self.plate.contains(x,y)
        dragging = bool(self.state.get('_posterize_drag'))
        if event.type=='ESC' and event.value=='PRESS' and not dragging and self.in_area(event):
            self.finish(context)
            return {'CANCELLED'}
        if (event.type in TOOL_KEYS and event.value=='PRESS' and over
                and not (event.ctrl or event.alt or event.shift or event.oskey)):
            service.settings().tool = TOOL_KEYS[event.type]
            self.tag_redraw()
            return {'RUNNING_MODAL'}
        if (over or dragging) and handle_event(context,event,self.state,x,y,self.tree):
            self.tag_redraw()
            return {'RUNNING_MODAL'}
        if over and event.type in PLATE_EVENTS:
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}

    def finish(self, context=None):
        if self.closed:
            return
        self.closed = True
        if self.handler is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(self.handler,'WINDOW')
            except (ValueError, ReferenceError):
                pass
            self.handler = None
        if self in _CANVASES:
            _CANVASES.remove(self)
        self.tag_redraw()  # only while its viewport is still there


class MCBEDROCK_OT_posterize_canvas(bpy.types.Operator):
    """Larger shared preview for both styled and native sidebar users."""
    bl_idname = 'mcbedrock.posterize_canvas'
    bl_label = 'Posterize Preview & Protection'

    def invoke(self,context,event):
        area = context.area
        if not service.SESSION or not area or area.type != 'VIEW_3D':
            return {'CANCELLED'}
        for canvas in list(_CANVASES):  # one Larger Preview at a time
            if canvas.valid() and _pointer(canvas.area) == _pointer(area):
                canvas.tag_redraw()
                return {'CANCELLED'}
            canvas.finish(context)
        regions = [region for region in area.regions if region.type == 'WINDOW']
        if not regions:
            return {'CANCELLED'}
        # Quad View: the quarter under the pointer, else the largest.
        region = next((region for region in regions
                       if region.x <= event.mouse_x < region.x+region.width
                       and region.y <= event.mouse_y < region.y+region.height),
                      max(regions,key=lambda region:region.width*region.height))
        self._canvas = LargerPreview(context.window,area,region)
        context.window_manager.modal_handler_add(self)
        self._canvas.tag_redraw()
        return {'RUNNING_MODAL'}

    def modal(self,context,event):
        return self._canvas.modal(context,event)

    def cancel(self,context):
        self._canvas.finish(context)


class MCBEDROCK_PT_posterize(bpy.types.Panel):
    bl_label = 'Posterize'
    bl_idname = 'MCBEDROCK_PT_posterize'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Sparkle'

    @classmethod
    def poll(cls,context):
        from .custom_ui import legacy_ui_enabled
        return legacy_ui_enabled(context)

    def draw(self,context):
        layout,s,session = self.layout,service.settings(context),service.SESSION
        layout.operator('mcbedrock.posterize_open',text='Open Image')
        layout.operator('mcbedrock.posterize_image',text='Use Blender Image')
        layout.operator('mcbedrock.posterize_target',text='Use Model Texture').use_source = True
        layout.operator('mcbedrock.posterize_action',text='Choose Pack Texture').action = 'PACK'
        # Native asset browser may be collapsed or use its gallery layout.
        index = context.scene.mcbedrock_browser_index
        assets = context.scene.mcbedrock_browser_assets
        if 0 <= index < len(assets) and assets[index].asset_type == 'Texture':
            row = layout.row()
            row.operator_context = 'EXEC_DEFAULT'
            row.operator('mcbedrock.posterize_open',text='Use Selected Pack Texture').filepath = assets[index].source_path
        if not session:
            return
        layout.label(text=session.source_name)
        layout.operator('mcbedrock.posterize_canvas',text='Before / After & Protect Details')
        layout.prop(s,'tool')
        layout.prop(s,'radius')
        layout.prop(s,'zoom')
        layout.prop(s,'strength',slider=True)
        layout.prop(s,'cleanup',slider=True)
        layout.prop(s,'lock_groups')
        layout.prop(s,'advanced_ranges')
        if s.advanced_ranges:
            layout.label(text='First matching group wins; unmatched colors stay.')
            layout.prop(s,'highlight_range')
            layout.template_list('UI_UL_list','posterize_ranges',s,'ranges',s,'range_index',rows=4)
            if s.ranges:
                group = s.ranges[min(s.range_index,len(s.ranges)-1)]
                layout.label(text=group.label)
                row = layout.row()
                row.operator('mcbedrock.posterize_range',text='Edit Range').action = 'EDIT'
                row.operator_context = 'EXEC_DEFAULT'
                row.operator('mcbedrock.posterize_range',text='Remove').action = 'REMOVE'
                row = layout.row()
                row.operator_context = 'EXEC_DEFAULT'
                row.operator('mcbedrock.posterize_range',text='Move Up').action = 'UP'
                row.operator('mcbedrock.posterize_range',text='Move Down').action = 'DOWN'
            row = layout.row()
            row.operator('mcbedrock.posterize_range',text='Add Range').action = 'ADD'
            row.operator_context = 'EXEC_DEFAULT'
            row.operator('mcbedrock.posterize_range',text='Reset Ranges').action = 'RESET'
            layout.label(text='Use Sample Range in the preview tool selector.')
        layout.prop(s,'tileable')
        layout.label(text=f'{int(session.mask.sum())} protected pixels / {len(session.locked)} kept colors')
        layout.operator('mcbedrock.posterize_action',text='Clear Protection').action = 'CLEAR_MASK'
        layout.operator('mcbedrock.posterize_action',text='Clear Color Locks').action = 'CLEAR_LOCKS'
        layout.label(text=session.status)
        layout.operator('mcbedrock.posterize_target',text='Choose Model Target')
        layout.label(text=f'{s.target.name} / {s.node}' if s.target else 'No target')
        layout.prop(s,'model_layers')
        layout.prop(s,'live')
        layout.prop(s,'original')
        row = layout.row()
        row.enabled = session.ready
        row.operator('mcbedrock.posterize_apply',text='Apply')
        row.operator('mcbedrock.posterize_save',text='Save PNG')
        row = layout.row()
        row.operator('mcbedrock.posterize_action',text='Reset').action = 'RESET'
        row.operator('mcbedrock.posterize_action',text='Cancel').action = 'CANCEL'


CLASSES = (MCBEDROCK_OT_posterize_range,MCBEDROCK_OT_posterize_open,MCBEDROCK_OT_posterize_image,MCBEDROCK_OT_posterize_target,
           MCBEDROCK_OT_posterize_action,MCBEDROCK_OT_posterize_apply,MCBEDROCK_OT_posterize_save,
           MCBEDROCK_OT_posterize_canvas,MCBEDROCK_PT_posterize)


def register():
    service.register()
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for canvas in list(_CANVASES):
        canvas.finish(bpy.context)
    service.unregister()
    _TARGETS.clear()
    _PROGRAMS.clear()
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
