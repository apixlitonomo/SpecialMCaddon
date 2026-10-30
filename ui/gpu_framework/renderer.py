"""Blender GPU/blf renderer for the framework primitives."""

from __future__ import annotations

import math
import os
import time
from collections import OrderedDict
from functools import lru_cache

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from .core import Rect

_FONTS = {}
_FONT_PATHS = set()
_FONT_PROBES = {}
_SHADERS = {}
# Image files drawn by every Sparkle view: path -> [last checked, mtime, size, texture].
# A texture lives only here, so dropping the entry frees it.
_IMAGES = OrderedDict()
_IMAGE_LIMIT = 256
_BROKEN = {}            # path -> mtime of a file that would not load: not tried each frame
_LOAD_BUDGET = .008     # seconds of image loading per drawn frame; the rest come next frame
# A fixed measurement identifies each loaded font, so a reused or freed id shows up.
_PROBE_TEXT = 'Sparkle Hg 0123'
_PROBE_SIZE = 32


def _ui_shader(kind):
    if kind in _SHADERS:
        return _SHADERS[kind]
    info = gpu.types.GPUShaderCreateInfo()
    info.push_constant('MAT4', 'ModelViewProjectionMatrix')
    info.vertex_in(0, 'VEC2', 'pos')
    info.fragment_out(0, 'VEC4', 'fragColor')
    info.vertex_source('void main() { gl_Position = ModelViewProjectionMatrix * vec4(pos, 0.0, 1.0); }')
    if kind == 'rect':
        for name in ['bounds', 'fillColor', 'borderColor']:
            info.push_constant('VEC4', name)
        info.push_constant('FLOAT', 'cornerRadius')
        info.push_constant('FLOAT', 'borderWidth')
        info.vertex_source('void main() { vec2 p = bounds.xy - vec2(1.0) + pos * (bounds.zw + vec2(2.0)); gl_Position = ModelViewProjectionMatrix * vec4(p, 0.0, 1.0); }')
        info.fragment_source('''
            void main() {
                vec2 halfSize = bounds.zw * 0.5;
                vec2 q = abs(gl_FragCoord.xy - bounds.xy - halfSize) - halfSize + cornerRadius;
                float d = length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - cornerRadius;
                float aa = max(fwidth(d), 0.75);
                float coverage = 1.0 - smoothstep(-aa*0.5, aa*0.5, d);
                float edge = borderWidth > 0.0 ? smoothstep(-aa*0.5, aa*0.5, d+borderWidth) : 0.0;
                fragColor = mix(fillColor, borderColor, edge);
                fragColor.a *= coverage;
            }
        ''')
    elif kind == 'dots':
        info.push_constant('VEC4', 'bounds')
        info.push_constant('VEC4', 'dotColor')
        info.push_constant('FLOAT', 'spacing')
        info.push_constant('FLOAT', 'dotRadius')
        info.vertex_source('void main() { vec2 p = bounds.xy + pos * bounds.zw; gl_Position = ModelViewProjectionMatrix * vec4(p, 0.0, 1.0); }')
        info.fragment_source('''
            void main() {
                vec2 cell = mod(gl_FragCoord.xy - bounds.xy, spacing) - spacing * 0.5;
                float d = length(cell);
                float coverage = 1.0 - smoothstep(dotRadius - 0.5, dotRadius + 0.5, d);
                fragColor = vec4(dotColor.rgb, dotColor.a * coverage);
            }
        ''')
    else:
        # One unit quad for every image: the rectangle, crop, corner radius and
        # turn are uniforms, so a moving image never builds new geometry.
        interface = gpu.types.GPUStageInterfaceInfo('mcassets_image_uv')
        interface.smooth('VEC2','uv')
        interface.smooth('VEC2','local')
        info.vertex_out(interface)
        info.sampler(0,'FLOAT_2D','image')
        for name in ['bounds', 'uvRect', 'iconTint']:
            info.push_constant('VEC4', name)
        info.push_constant('FLOAT','cornerRadius')
        info.push_constant('FLOAT','rotation')
        info.push_constant('FLOAT','opacity')
        info.push_constant('BOOL','linearOutput')
        info.push_constant('BOOL','nearestSampling')
        info.vertex_source('''
            void main() {
                local = pos * bounds.zw;
                uv = mix(uvRect.xy, uvRect.zw, pos);
                vec2 offset = local - bounds.zw * 0.5;
                float c = cos(rotation), s = sin(rotation);
                vec2 turned = vec2(offset.x * c - offset.y * s, offset.x * s + offset.y * c);
                gl_Position = ModelViewProjectionMatrix * vec4(bounds.xy + bounds.zw * 0.5 + turned, 0.0, 1.0);
            }
        ''')
        info.fragment_source('''
            void main() {
                ivec2 dimensions = textureSize(image, 0);
                ivec2 pixel = clamp(ivec2(floor(uv * vec2(dimensions))), ivec2(0), dimensions - ivec2(1));
                vec4 color = nearestSampling ? texelFetch(image, pixel, 0) : texture(image, uv);
                vec3 lo = color.rgb * 12.92;
                vec3 hi = 1.055 * pow(max(color.rgb, vec3(0.0)), vec3(1.0/2.4)) - 0.055;
                float coverage = 1.0;
                if (cornerRadius > 0.0) {
                    vec2 halfSize = bounds.zw * 0.5;
                    vec2 q = abs(local - halfSize) - halfSize + cornerRadius;
                    float d = length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - cornerRadius;
                    coverage = 1.0 - smoothstep(-0.5, 0.5, d);
                }
                fragColor = vec4(linearOutput ? color.rgb : mix(lo, hi, step(vec3(0.0031308), color.rgb)), color.a * opacity * coverage);
                if (iconTint.a >= 0.0) fragColor = vec4(iconTint.rgb, color.a * opacity * coverage * iconTint.a);
            }
        ''')
    _SHADERS[kind] = gpu.shader.create_from_info(info)
    return _SHADERS[kind]


def _font_path(weight):
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '../../assets/ui/fonts', 'Inter-' + weight + '.ttf'))


def release_resources():
    """Release BLF file handles before Windows removes the extension folder."""
    for path in tuple(_FONT_PATHS):
        blf.unload(path)
        _FONT_PATHS.discard(path)
    _FONTS.clear()
    _FONT_PROBES.clear()
    _dimensions.cache_clear()
    _SHADERS.clear()
    _IMAGES.clear()
    _BROKEN.clear()


def forget_images():
    """Cached image files were deleted (Clear Cache): load them from disk again."""
    _IMAGES.clear()
    _BROKEN.clear()


def _load_texture(path):
    """A GPU texture for an image file, and its size.

    Decoded with ImBuf: the user's file never gains an Image datablock, and
    the texture is freed with its cache entry.
    """
    import imbuf
    import numpy

    buffer = imbuf.load(path)
    try:
        if buffer.buffer_type != 'BYTE':
            return _load_through_datablock(path)
        width, height = buffer.size
        with buffer.with_buffer() as view:
            pixels = numpy.asarray(view).reshape(height, width, -1).astype(numpy.float32)
    finally:
        buffer.free()
    if pixels.shape[2] < 4:
        # Grey or RGB files: spread to RGB and add an opaque alpha.
        pixels = numpy.concatenate([numpy.repeat(pixels[:, :, :1], 3, axis=2) if pixels.shape[2] < 3
                                    else pixels[:, :, :3], numpy.full((height, width, 1), 255, numpy.float32)], axis=2)
    pixels *= 1 / 255
    # Byte pixels are sRGB-encoded: sampled linear, as Blender's own image textures are.
    texture = gpu.types.GPUTexture((width, height), format='SRGB8_A8',
                                   data=gpu.types.Buffer('FLOAT', pixels.size, pixels.ravel()))
    return (width, height), texture


def forget_image(image):
    """Drop the texture drawn for a Blender image before that image is removed."""
    _IMAGES.pop('packed:' + str(image.as_pointer()), None)


def _load_through_datablock(path):
    """Float files keep Blender's colour handling; the datablock goes straight away."""
    image = bpy.data.images.load(path, check_existing=False)
    try:
        return tuple(image.size), gpu.texture.from_image(image)
    finally:
        bpy.data.images.remove(image)


def _probe(font):
    blf.size(font, _PROBE_SIZE)
    return tuple(blf.dimensions(font, _PROBE_TEXT))


def refresh_fonts():
    """Reload fonts Blender has dropped; return True when any were stale.

    Theme presets, text rendering options and the interface font make Blender
    unload every BLF font. Ids cached before that draw and measure nothing.
    """
    stale = [weight for weight, font in _FONTS.items()
             if font and _probe(font) != _FONT_PROBES.get(weight)]
    for weight in stale:
        # Blender already freed the font; forget it without unloading by id,
        # which another add-on may own now.
        _FONTS.pop(weight, None)
        _FONT_PROBES.pop(weight, None)
        _FONT_PATHS.discard(_font_path(weight))
    if stale:
        _dimensions.cache_clear()
    return bool(stale)


def _font(weight='Regular'):
    weight = 'SemiBold' if weight == 'Semi Bold' else weight
    weight = weight if weight in {'Regular', 'Medium', 'SemiBold', 'Bold'} else 'Regular'
    if weight not in _FONTS:
        path = _font_path(weight)
        try:
            loaded = blf.load(path)
            if loaded >= 0:
                _FONT_PATHS.add(path)
                _FONT_PROBES[weight] = _probe(loaded)
            _FONTS[weight] = max(0, loaded)
        except (RuntimeError, OSError):
            _FONTS[weight] = 0
    return _FONTS[weight]


@lru_cache(maxsize=8192)
def _dimensions(font, size, value):
    blf.size(font,size)
    return blf.dimensions(font,value)


class BlenderRenderer:
    """Immediate renderer with explicit scissor and GPU state restoration."""

    def __init__(self, region, ui_scale: float = 1.0):
        self.region = region
        self.ui_scale = max(0.5, float(ui_scale or 1.0))
        self._shader = _ui_shader('rect')
        self._image_shader = _ui_shader('image')
        self._clip_stack: list[Rect] = []
        self.opacity = 1.0
        self.cap_center_default = False
        self.linear_output = False
        self.capture = False
        self.record_only = False
        self.commands = []
        self.offset_x = 0.0
        self.offset_y = 0.0
        self._rect_batch = batch_for_shader(self._shader,'TRI_FAN',{'pos':[(0,0),(1,0),(1,1),(0,1)]})
        self._image_batch = batch_for_shader(self._image_shader,'TRI_FAN',{'pos':[(0,0),(1,0),(1,1),(0,1)]})
        self._load_deadline = 0.0
        self._wants_frame = False

    def begin(self) -> None:
        refresh_fonts()
        self._load_deadline = time.perf_counter() + _LOAD_BUDGET
        self._wants_frame = False
        self.commands = []
        gpu.state.blend_set("ALPHA")
        gpu.state.scissor_test_set(False)
        self._matrix = gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix()

    def end(self) -> None:
        gpu.state.scissor_test_set(False)
        gpu.state.blend_set("NONE")
        if self._wants_frame:
            # Images over this frame's loading budget draw on the next one.
            area = bpy.context.area
            def redraw():
                try:
                    area.tag_redraw()
                except (AttributeError, ReferenceError):
                    pass
            bpy.app.timers.register(redraw, first_interval=0.0)

    def _color(self, color):
        if not self.linear_output:
            return color
        # Image Editor canvases present through a pure gamma-2.2 display pass.
        # Its exact inverse keeps dark surfaces identical to the sidebar's
        # (#141414 stays #141414 instead of lifting to #1B1B1B).
        return tuple(max(0.0, c) ** 2.2 for c in color[:3]) + (color[3],)

    def _physical_rect(self, rect):
        scale = self.ui_scale
        return Rect((rect.x + self.offset_x) * scale, (rect.y + self.offset_y) * scale, rect.width * scale, rect.height * scale)

    def rect(
        self,
        rect: Rect,
        color,
        *,
        radius: float = 0.0,
        border=None,
        border_width: float = 1.0,
    ) -> None:
        if self.capture:
            self.commands.append(('rect', rect, tuple(color), radius, border, self.opacity, self.offset_x, self.offset_y))
        if self.record_only:
            return
        color = self._color(color)
        border = self._color(border) if border else None
        r = self._physical_rect(rect)
        if r.width <= 0 or r.height <= 0:
            return
        shader = self._shader
        shader.bind()
        shader.uniform_float('ModelViewProjectionMatrix', self._matrix)
        shader.uniform_float('bounds',(r.x,r.y,r.width,r.height))
        shader.uniform_float('fillColor',(*color[:3],color[3]*self.opacity))
        edge = border or color
        shader.uniform_float('borderColor',(*edge[:3],edge[3]*self.opacity))
        shader.uniform_float('cornerRadius',max(0,min(radius*self.ui_scale,r.width/2,r.height/2)))
        shader.uniform_float('borderWidth',border_width*self.ui_scale if border else 0)
        gpu.state.blend_set('ALPHA')
        self._rect_batch.draw(shader)

    def dot_grid(self, rect: Rect, color, spacing=24.0, radius=.8) -> None:
        """One native GPU pass for a quiet, screen-aligned canvas texture."""
        if self.record_only:
            return
        shader = _ui_shader('dots')
        if not hasattr(self, '_dots_batch'):
            self._dots_batch = batch_for_shader(shader, 'TRI_FAN', {'pos':[(0,0),(1,0),(1,1),(0,1)]})
        r = self._physical_rect(rect)
        color = self._color(color)
        shader.bind()
        shader.uniform_float('ModelViewProjectionMatrix', self._matrix)
        shader.uniform_float('bounds', (r.x, r.y, r.width, r.height))
        shader.uniform_float('dotColor', (*color[:3], color[3] * self.opacity))
        shader.uniform_float('spacing', spacing * self.ui_scale)
        shader.uniform_float('dotRadius', radius * self.ui_scale)
        self._dots_batch.draw(shader)

    def text_width(self, value: str, size: float, weight='Regular') -> float:
        font = 0 if getattr(self, 'use_blender_font', False) else _font(weight)
        return float(_dimensions(font,max(6.0,size*self.ui_scale),str(value))[0]) / self.ui_scale

    def _truncate(self, value: str, width: float, size: float, weight='Regular') -> str:
        value = str(value)
        if width <= 4.0 or self.text_width(value, size, weight) <= width:
            return value
        ellipsis = "..."
        if self.text_width(ellipsis, size, weight) > width:
            return ""
        low, high = 0, len(value)
        while low < high:
            mid = (low + high + 1) // 2
            if self.text_width(value[:mid] + ellipsis, size, weight) <= width:
                low = mid
            else:
                high = mid - 1
        return value[:low] + ellipsis

    def text(
        self,
        rect: Rect,
        value: str,
        color,
        *,
        size: float,
        align: str = "LEFT",
        weight: str = "Regular",
        cap_center: bool = False,
    ) -> None:
        if self.capture:
            self.commands.append(('text', rect, str(value), tuple(color), size, align, weight, self.opacity, self.offset_x, self.offset_y))
        if self.record_only:
            return
        if rect.width <= 1.0 or rect.height <= 1.0:
            return
        font_size = max(6.0, float(size) * self.ui_scale)
        font = 0 if getattr(self, 'use_blender_font', False) else _font(weight)
        blf.size(font, font_size)
        display = self._truncate(str(value), rect.width, size, weight)
        width, height = (value / self.ui_scale for value in _dimensions(font,font_size,display))
        if cap_center or self.cap_center_default:
            # Use a stable cap line beside icons; descenders must not push the
            # baseline down for names such as Quick Import / Particle Settings.
            height = _dimensions(font, font_size, 'H')[1] / self.ui_scale
        if align == "CENTER":
            x = rect.x + max(0.0, (rect.width - width) * 0.5)
        elif align == "RIGHT":
            x = rect.right - width
        else:
            x = rect.x
        y = rect.y + max(0.0, (rect.height - height) * 0.5)
        blf.position(font, (x + self.offset_x) * self.ui_scale, (y + self.offset_y) * self.ui_scale, 0.0)
        blf.color(font, *color[:3], color[3] * self.opacity)
        blf.draw(font, display)

    def image(self, rect: Rect, path: str, radius: float = 0.0, *, fit: bool = False, cover: bool = False, nearest: bool = False, tint=None, uv_rect=(0,0,1,1), rotation: float = 0.0) -> bool:
        """Draw a Blender image datablock into a rectangle, optionally turned counterclockwise by rotation degrees about its centre."""
        if self.capture:
            # Crop, fit and turn change the picture too: a pan or zoom repaints the viewport pass.
            self.commands.append(('image', rect, str(path), radius, self.opacity, self.offset_x, self.offset_y,
                                  tuple(uv_rect), fit, cover, nearest, round(rotation, 1),
                                  tuple(tint) if tint else None))
        if self.record_only:
            return True
        rect = self._physical_rect(rect)
        datablock = path if isinstance(path,bpy.types.Image) else None
        path = ('packed:'+str(datablock.as_pointer())) if datablock else os.path.abspath(bpy.path.abspath(path))
        try:
            now = time.monotonic()
            entry = _IMAGES.get(path)
            if datablock is not None:
                # Blender's own image (the moon artwork): drawn as it is, never removed.
                if entry is None:
                    entry = [now, 0, tuple(datablock.size), gpu.texture.from_image(datablock)]
            elif entry is None or now-entry[0] >= 1.0:
                stamp = os.stat(path).st_mtime_ns
                if entry is None or stamp != entry[1]:
                    if _BROKEN.get(path) == stamp:
                        return False
                    if time.perf_counter() > self._load_deadline:
                        self._wants_frame = True
                        return False
                    try:
                        size, texture = _load_texture(path)
                    except (OSError, RuntimeError, ValueError):
                        _BROKEN[path] = stamp
                        return False
                    entry = [now, stamp, size, texture]
                entry[0] = now
            _IMAGES[path] = entry
            _IMAGES.move_to_end(path)
            if len(_IMAGES) > _IMAGE_LIMIT:
                _IMAGES.popitem(last=False)
            texture = entry[3]
            if cover and rect.width > 0 and rect.height > 0:
                width, height = entry[2]
                if width > 0 and height > 0:
                    ratio = (width / height) / (rect.width / rect.height)
                    u0, v0, u1, v1 = uv_rect
                    if ratio > 1:
                        margin = (u1-u0)*(1-1/ratio)/2
                        uv_rect = (u0+margin,v0,u1-margin,v1)
                    else:
                        margin = (v1-v0)*(1-ratio)/2
                        uv_rect = (u0,v0+margin,u1,v1-margin)
            elif fit:
                width, height = entry[2]
                if width > 0 and height > 0:
                    scale = min(rect.width / width, rect.height / height)
                    w, h = width * scale, height * scale
                    rect = Rect(rect.x + (rect.width-w)/2, rect.y + (rect.height-h)/2, w, h)
            if rect.width <= 0 or rect.height <= 0:
                return True
            corner = max(0.0, min(radius * self.ui_scale, rect.width / 2, rect.height / 2))
            self._image_shader.bind()
            self._image_shader.uniform_float('ModelViewProjectionMatrix', self._matrix)
            self._image_shader.uniform_float('bounds', (rect.x, rect.y, rect.width, rect.height))
            self._image_shader.uniform_float('uvRect', tuple(uv_rect))
            self._image_shader.uniform_float('cornerRadius', corner if corner > .5 else 0.0)
            self._image_shader.uniform_float('rotation', math.radians(round(rotation, 1) % 360))
            self._image_shader.uniform_float('opacity',self.opacity)
            self._image_shader.uniform_bool('linearOutput',self.linear_output)
            self._image_shader.uniform_bool('nearestSampling',nearest)
            icons = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../assets/ui/icons'))
            if tint is None:
                tint = getattr(self, 'icon_tint', None) if os.path.dirname(path) == icons and os.path.basename(path) not in {'java.png','bedrock.png'} else None
            self._image_shader.uniform_float('iconTint', tint or (0,0,0,-1))
            self._image_shader.uniform_sampler("image", texture)
            gpu.state.blend_set('ALPHA')
            self._image_batch.draw(self._image_shader)
            return True
        except (OSError, RuntimeError, ValueError, ReferenceError):
            _IMAGES.pop(path,None)
            return False

    def push_clip(self, rect: Rect) -> None:
        clipped = rect
        if self._clip_stack:
            clipped = self._clip_stack[-1].intersect(rect)
        self._clip_stack.append(clipped)
        clipped = self._physical_rect(clipped)
        gpu.state.scissor_set(
            int(max(0.0, clipped.x)),
            int(max(0.0, clipped.y)),
            int(max(0.0, clipped.right - max(0.0, clipped.x))),
            int(max(0.0, clipped.top - max(0.0, clipped.y))),
        )
        gpu.state.scissor_test_set(True)

    def pop_clip(self) -> None:
        if self._clip_stack:
            self._clip_stack.pop()
        if self._clip_stack:
            clipped = self._physical_rect(self._clip_stack[-1])
            gpu.state.scissor_set(
                int(max(0.0, clipped.x)),
                int(max(0.0, clipped.y)),
                int(max(0.0, clipped.right - max(0.0, clipped.x))),
                int(max(0.0, clipped.top - max(0.0, clipped.y))),
            )
            gpu.state.scissor_test_set(True)
        else:
            gpu.state.scissor_test_set(False)
