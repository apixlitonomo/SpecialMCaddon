"""Posterize session: the source, its conversion and reversible model previews.

Conversion runs in-process on a background thread (NumPy only, no bpy), so
the interface keeps drawing while a large texture converts. Model previews
are tagged material copies, so they are found and removed even after an
Undo or a reopened file brought them back.
"""
import json
import os
from pathlib import Path
import struct
import threading
import time
import uuid
import zlib

import bpy
from bpy.app.handlers import persistent
import numpy as np


SESSION = None
ENABLED = False
POLL_BUSY = .05
POLL_IDLE = 1.
DEBOUNCE = .06
PREVIEW = 'mcbedrock_posterize_preview'  # on preview copies: the original material's name
APPLIED = 'mcbedrock_posterized'         # on materials and images Apply made


def redraw():
    """Repaint what shows Posterize: Sparkle sidebars, native 3D View sidebars
    and the Larger Preview, never every editor in every window. A live model
    preview repaints through Blender's own image and material updates."""
    from .gpu_framework import runtime
    from .posterize_page import _CANVASES
    for session in list(runtime._ACTIVE.values()):
        session.state['_tree_dirty'] = True
    for canvas in list(_CANVASES):
        canvas.tag_redraw()
    manager = bpy.context.window_manager
    for region in redraw_regions(manager.windows if manager else ()):
        region.tag_redraw()


def redraw_regions(windows):
    """The 3D View sidebars of ``windows``: Sparkle's page (which repaints its
    viewport part itself when what it draws changes) and the native panels."""
    return [region for window in windows for area in window.screen.areas if area.type == 'VIEW_3D'
            for region in area.regions if region.type == 'UI']


def settings(context=None):
    return (context or bpy.context).window_manager.mcbedrock_posterize


def forget_preview_texture(image):
    """Drop the texture Sparkle drew for ``image``: cached image textures are
    never read again, so new pixels or a removed image need this."""
    from .gpu_framework.renderer import forget_image
    try:
        forget_image(image)
    except ReferenceError:
        pass


def remove_preview_image(image):
    """Release the cached GPU texture as well as the temporary image datablock."""
    forget_preview_texture(image)
    bpy.data.images.remove(image)


def changed(self, context):
    if SESSION:
        SESSION.request()
    redraw()


def wake():
    """Check for work soon, even while the idle timer is slow."""
    if not ENABLED:
        return
    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)
    bpy.app.timers.register(tick, first_interval=.02)


def preview_changed(self, context):
    if SESSION:
        try:
            SESSION.refresh_preview()
        except (ValueError, ReferenceError, RuntimeError) as exc:
            SESSION.status = str(exc)
    redraw()


def target_changed(self, context):
    load_tint(self)
    preview_changed(self, context)


def model_root(obj):
    """Prefer a character boundary over a scene-wide grouping empty."""
    ancestors = []
    current = obj
    while current:
        ancestors.append(current)
        current = current.parent
    for candidate in ancestors:
        if candidate.get('mcbedrock_presentation_root'):
            return candidate
    for candidate in ancestors:
        if candidate.type == 'ARMATURE':
            return candidate
    for modifier in obj.modifiers:
        if modifier.type == 'ARMATURE' and modifier.object:
            return modifier.object
    return ancestors[-1]


def model_meshes(scene, obj):
    root = model_root(obj)
    members = {root, *root.children_recursive}
    if root.type == 'ARMATURE':
        members.update(candidate for candidate in scene.objects if candidate.type == 'MESH' and
                       any(m.type == 'ARMATURE' and m.object == root for m in candidate.modifiers))
    members.add(obj)
    return sorted((candidate for candidate in members if candidate.type == 'MESH' and
                   candidate.name in scene.objects), key=lambda candidate:candidate.name)


def same_texture(left, right):
    if left == right:
        return left is not None
    if not left or not right:
        return False
    # Separate loads of one unedited file are also the same texture. Packed or
    # edited copies may have diverged, even when their file paths still match.
    if any(image.source != 'FILE' or image.is_dirty or image.packed_file or not image.filepath
           for image in (left,right)):
        return False
    return (left.colorspace_settings.name == right.colorspace_settings.name and
            left.alpha_mode == right.alpha_mode and
            Path(bpy.path.abspath(left.filepath,library=left.library)).resolve() ==
            Path(bpy.path.abspath(right.filepath,library=right.library)).resolve())


def srgb_to_linear(color):
    return tuple(c/12.92 if c <= .04045 else ((c+.055)/1.055)**2.4 for c in color[:3])


def linear_to_srgb(color):
    return tuple(c*12.92 if c <= .0031308 else 1.055*c**(1/2.4)-.055 for c in color[:3])


def tint_sockets(scene, obj):
    """Tint colour inputs of every material (and live preview copy) on ``obj``'s
    model whose faces a server colours at runtime. Java models imported by
    Sparkle mark those materials; others have none."""
    from ..builders.java_builder import TINT_ATTRIBUTE
    from ..builders.java_materials import tint_socket
    sockets, seen = [], set()
    for mesh_obj in model_meshes(scene, obj) if obj else ():
        for slot in mesh_obj.material_slots:
            for material in (slot.material, original_material(slot.material)):
                if material is None or material.name in seen or material.get(TINT_ATTRIBUTE) is None:
                    continue
                seen.add(material.name)
                socket = tint_socket(material)
                if socket is not None:
                    sockets.append(socket)
    return sockets


_SYNCING = False


def tint_changed(self, context):
    """Colour the model's server-tinted parts, as the game multiplies a tint."""
    if _SYNCING or not self.target:
        return
    color = (*srgb_to_linear(self.tint_color), 1.)
    for socket in tint_sockets(context.scene, self.target):
        socket.default_value = color
    redraw()


def load_tint(s):
    """Show the target model's current server tint in the picker."""
    global _SYNCING
    sockets = tint_sockets(bpy.context.scene, s.target) if s.target else []
    if sockets:
        _SYNCING = True
        try:
            s.tint_color = linear_to_srgb(sockets[0].default_value)
        finally:
            _SYNCING = False


class PosterizeSettings(bpy.types.PropertyGroup):
    colors: bpy.props.IntProperty(name='Colors', default=0, min=0, max=64, update=changed,
        description='How many colors the texture keeps. 0 keeps them all')
    specks: bpy.props.IntProperty(name='Remove Specks', default=0, min=0, max=100, update=changed,
        description='Replace lone pixels with the color around them')
    tileable: bpy.props.BoolProperty(name='Seamless Edges', default=False, update=changed,
        description='Treat opposite edges as touching, for textures that tile')
    recolor: bpy.props.BoolProperty(name='Recolor', default=False, update=changed,
        description='Color part of the texture, keeping its shading, like a server tint or a Blockbench color adjust')
    recolor_color: bpy.props.FloatVectorProperty(name='New Color', subtype='COLOR_GAMMA', size=3, min=0, max=1,
        default=(.8, .2, .2), update=changed)
    recolor_select: bpy.props.EnumProperty(name='Recolor', update=changed, items=[
        ('WHITES', 'Whites and light grays', 'Every white or light gray pixel'),
        ('PICKED', 'Picked color', 'Shades near a color picked from the texture')], default='WHITES')
    tint_color: bpy.props.FloatVectorProperty(name='Server Tint', subtype='COLOR_GAMMA', size=3, min=0, max=1,
        default=(1, 1, 1), update=tint_changed,
        description='The color a server gives this model in game. Changes the model directly; Ctrl+Z undoes it')
    recolor_pick: bpy.props.FloatVectorProperty(name='Picked Color', subtype='COLOR_GAMMA', size=3, min=0, max=1,
        default=(1, 1, 1), update=changed)
    recolor_tolerance: bpy.props.IntProperty(name='Tolerance', default=25, min=0, max=100, update=changed,
        description='How far a shade may be from white or the picked color and still change')
    recolor_mode: bpy.props.EnumProperty(name='Shading', update=changed, items=[
        ('TINT', 'Tint like the game', 'Multiply: white becomes the color, grays become darker shades of it'),
        ('MATCH', 'Brightest becomes the color', 'The lightest selected shade becomes exactly the color')], default='TINT')
    live: bpy.props.BoolProperty(name='Show on Model', default=True, update=preview_changed,
        description='Show the result on the model while editing')
    original: bpy.props.BoolProperty(name='Show Original', default=False, update=preview_changed,
        description='Compare: show the untouched texture on the model')
    target: bpy.props.PointerProperty(name='Object', type=bpy.types.Object, update=target_changed)
    slot: bpy.props.IntProperty(name='Material Slot', default=0, min=0, update=target_changed)
    node: bpy.props.StringProperty(name='Image Node', update=target_changed)
    tool: bpy.props.EnumProperty(name='Tool', items=[
        ('PAN', 'Pan', 'Drag to move both previews (middle-drag always pans)'),
        ('PROTECT', 'Protect', 'Paint pixels that must not change'),
        ('ERASE', 'Erase Protection', 'Remove protection'),
        ('KEEP', 'Keep Color', 'Click a pixel to keep its color exactly'),
        ('RECOLOR', 'Pick Recolor', 'Click the texture to choose the color to recolor')], default='PAN')
    radius: bpy.props.IntProperty(name='Brush Size', default=1, min=1, max=64)
    zoom: bpy.props.FloatProperty(name='Zoom', default=1, min=1, max=32)
    pan_x: bpy.props.FloatProperty(default=.5, min=0, max=1)
    pan_y: bpy.props.FloatProperty(default=.5, min=0, max=1)


DEFAULTS = ('colors', 'specks', 'tileable', 'recolor', 'recolor_color', 'recolor_select', 'recolor_pick',
            'recolor_tolerance', 'recolor_mode', 'original', 'tool', 'radius', 'zoom', 'pan_x', 'pan_y')


def reset_settings(s):
    for name in DEFAULTS:
        s.property_unset(name)


def image_pixels(image):
    if image.source not in {'FILE', 'GENERATED'} or image.type not in {'IMAGE', 'UV_TEST'}:
        raise ValueError('Choose a single static color image; animations and UDIMs are not supported')
    if image.is_float:
        raise ValueError('Choose a standard 8-bit color image; float/HDR images are not supported')
    if image.colorspace_settings.is_data:
        raise ValueError('Normal, roughness, and other data maps cannot be posterized')
    if image.colorspace_settings.name != 'sRGB':
        raise ValueError('Choose an sRGB color texture to preserve its source colors exactly')
    path = Path(bpy.path.abspath(image.filepath)) if image.filepath else None
    if path and Path(str(path)+'.mcmeta').is_file():
        try:
            if 'animation' in json.loads(Path(str(path)+'.mcmeta').read_text()):
                raise ValueError('Animated Minecraft textures are not supported yet')
        except json.JSONDecodeError as exc:
            raise ValueError('Texture animation metadata is unreadable') from exc
    if path and path.suffix.lower() in {'.gif', '.apng'}:
        raise ValueError('Animated image formats are not supported yet')
    if path and path.is_file():
        if path.suffix.lower() == '.png':
            with path.open('rb') as stream:
                if stream.read(8) == b'\x89PNG\r\n\x1a\n':
                    while True:
                        header = stream.read(8)
                        if len(header) != 8:
                            break
                        size,kind = struct.unpack('>I4s',header)
                        if kind == b'acTL':
                            raise ValueError('Animated PNG textures are not supported yet')
                        if kind in {b'IDAT',b'IEND'}:
                            break
                        stream.seek(size+4,1)
        textures = next((parent for parent in path.parents if parent.name.lower()=='textures'),None)
        if textures and (textures/'flipbook_textures.json').is_file():
            try:
                entries = json.loads((textures/'flipbook_textures.json').read_text(encoding='utf-8-sig'))
                for entry in entries if isinstance(entries,list) else []:
                    name = entry.get('flipbook_texture','') if isinstance(entry,dict) else ''
                    if name and (textures.parent/name).with_suffix(path.suffix).resolve() == path.resolve():
                        raise ValueError('Animated Bedrock flipbook textures are not supported yet')
            except json.JSONDecodeError as exc:
                raise ValueError('Texture animation metadata is unreadable') from exc
    width, height = image.size
    if not width or not height or not image.has_data:
        raise ValueError('Image pixels are unavailable')
    # Bound peak memory for histogram, hierarchy and worker transfer.
    if width * height > 4096 * 4096:
        raise ValueError('Images up to 4096 × 4096 pixels are supported')
    values = np.empty(width*height*4, dtype=np.float32)
    image.pixels.foreach_get(values)
    values = values.reshape(height, width, 4)
    if image.alpha_mode == 'PREMUL':
        alpha = values[..., 3:4]
        np.divide(values[..., :3], alpha, out=values[..., :3], where=alpha > 0)
        values[..., :3] = np.clip(values[..., :3], 0, 1)
        values = np.rint(values*255).astype(np.float32)/255
    if not np.isfinite(values).all() or values.min() < 0 or values.max() > 1:
        raise ValueError('Source pixels must be finite and in the standard color range')
    return values


def make_image(name, pixels):
    height, width = pixels.shape[:2]
    image = bpy.data.images.new(name, width=width, height=height, alpha=True, float_buffer=False)
    image.colorspace_settings.name = 'sRGB'
    image.alpha_mode = 'STRAIGHT'
    image.pixels.foreach_set(np.asarray(pixels, np.float32).ravel())
    image.update()
    return image


def write_png(path, pixels):
    """Lossless sRGB RGBA8; never use scene view transforms or save_render."""
    values = np.rint(np.clip(pixels, 0, 1)*255).astype(np.uint8)[::-1]
    height, width = values.shape[:2]
    def chunk(kind, data):
        return struct.pack('>I', len(data))+kind+data+struct.pack('>I', zlib.crc32(kind+data) & 0xffffffff)
    raw = b''.join(b'\0'+row.tobytes() for row in values)
    content = b'\x89PNG\r\n\x1a\n'
    content += chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
    content += chunk(b'sRGB', b'\0')
    content += chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')
    path = Path(path)
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def drop_previews(objects=None):
    """Put the original material back wherever a preview copy is assigned,
    then delete unused copies. Works from the tags alone."""
    for obj in (bpy.data.objects if objects is None else objects):
        for slot in getattr(obj, 'material_slots', ()):
            material = slot.material
            original = material.get(PREVIEW) if material else None
            if original is None:
                continue
            links = material.get(PREVIEW+'_link')
            link = links.get(obj.name, 'DATA') if links else 'DATA'
            slot.material = bpy.data.materials.get(original)
            if slot.link != link:
                slot.link = link
    for material in list(bpy.data.materials):
        if material.get(PREVIEW) is not None and material.users == 0:
            bpy.data.materials.remove(material)


class Session:
    def __init__(self, pixels, name, filepath, scene):
        self.source = pixels
        self.source_name = name
        self.source_path = filepath
        self.scene_name = scene.name
        self.posterizer = None
        self.mask = np.zeros(self.source.shape[:2], bool)
        self.mask_version = 0
        self.kept = set()
        self.generation = 0
        self.received = -1
        self.due = 0.
        self.status = 'Preparing preview…'
        self.stats = {}
        self.result = None
        self.result_version = 0
        self.overlays = {}
        self.original_name = make_image('Posterize Original', pixels).name
        self.result_name = None
        self._thread = None
        self._done = None
        self.request()

    # Images and the scene are found by name: an Undo replaces every datablock.
    @property
    def original_image(self):
        return bpy.data.images.get(self.original_name)

    @property
    def result_image(self):
        return bpy.data.images.get(self.result_name) if self.result_name else None

    @property
    def scene(self):
        return bpy.data.scenes.get(self.scene_name)

    @property
    def ready(self):
        return self.result is not None and self.received == self.generation

    @property
    def busy(self):
        return self.received != self.generation

    def request(self):
        self.generation += 1
        self.due = time.monotonic()+DEBOUNCE
        self.status = 'Updating preview…'
        wake()

    def mask_edited(self):
        self.mask_version += 1
        self.request()

    def job(self):
        """Everything the conversion needs, read on the main thread."""
        s = settings()
        recolor = None
        if s.recolor:
            recolor = dict(select=s.recolor_select, pick=list(s.recolor_pick), color=list(s.recolor_color),
                           tolerance=s.recolor_tolerance, mode=s.recolor_mode)
        return dict(generation=self.generation, colors=s.colors, specks=s.specks, tileable=s.tileable,
                    kept=sorted(self.kept), mask=self.mask.copy(), recolor=recolor)

    def run(self, job):
        from ..builders import posterize
        try:
            if self.posterizer is None:
                self.posterizer = posterize.Posterizer(self.source)
            pixels, stats = self.posterizer.convert(job['colors'], job['specks'], job['kept'],
                                                    job['mask'], job['tileable'])
            if job['recolor']:
                pixels, stats['recolored'] = posterize.recolor(pixels, self.source, job['mask'], job['recolor'])
            self._done = (job['generation'], pixels, stats, None)
        except Exception as exc:  # shown in the status line; a thread must not raise
            self._done = (job['generation'], None, None, str(exc) or type(exc).__name__)

    def tick(self):
        done, self._done = self._done, None
        if done:
            generation, pixels, stats, error = done
            if generation == self.generation:
                if error:
                    self.received = generation
                    self.status = 'Conversion failed: '+error
                else:
                    self.accept(generation, pixels, stats)
                redraw()
        running = self._thread is not None and self._thread.is_alive()
        if not running and self.busy and time.monotonic() >= self.due and not self._done:
            self._thread = threading.Thread(target=self.run, args=(self.job(),), daemon=True)
            self._thread.start()

    def wait(self, timeout=60):
        """Finish the pending conversion now (Apply, Save PNG, tests)."""
        deadline = time.monotonic()+timeout
        self.due = 0
        while self.busy and time.monotonic() < deadline:
            self.tick()
            if self._thread:
                self._thread.join(.05)
        return self.ready

    def show_result(self, pixels):
        """Put ``pixels`` in the session's one result image; True when it had to be made."""
        image = self.result_image
        height, width = pixels.shape[:2]
        if image is not None and tuple(image.size) == (width, height):
            image.pixels.foreach_set(np.asarray(pixels, np.float32).ravel())
            image.update()
            forget_preview_texture(image)
            return False
        self.result_name = make_image('Posterize Result', pixels).name
        if image is not None and image.users == 0:
            remove_preview_image(image)
        return True

    def accept(self, generation, pixels, stats):
        if generation != self.generation:
            return False
        created = self.show_result(pixels)
        self.result = pixels
        self.result_version += 1
        self.received = generation
        self.stats = stats
        source = stats.get('source_colors', 0)
        self.status = (f"{stats['colors']} of {source} colors" if stats['colors'] != source
                       else f'{source} colors')
        if 'recolored' in stats:
            self.status += f" · {stats['recolored']} pixels recolored"
        if created or not self.previewing():
            try:
                self.refresh_preview()
            except ValueError as exc:
                self.status += ' · '+str(exc)
        return True

    def target(self):
        s = settings()
        obj = s.target
        scene = self.scene
        if not obj or not scene or obj.name not in scene.objects or s.slot >= len(obj.material_slots):
            raise ValueError('Choose a model texture')
        if obj.library or obj.data.library:
            raise ValueError('Linked objects must be made local before applying')
        material = original_material(obj.material_slots[s.slot].material)
        if not material or not material.use_nodes:
            raise ValueError('Choose a material with image nodes')
        node = material.node_tree.nodes.get(s.node)
        if not node or node.type != 'TEX_IMAGE':
            raise ValueError('Choose an image texture node')
        if node.image and node.image.colorspace_settings.is_data:
            raise ValueError('Choose a color texture, not a material data map')
        return obj, s.slot, material, node

    def targets(self):
        """Every slot of the model that shows this texture: all its layers and parts."""
        obj, index, material, node = self.target()
        if not node.image or obj.type != 'MESH':
            return [(obj, index, material, (node.name,))]
        found = []
        for candidate in model_meshes(self.scene, obj):
            for slot_index, slot in enumerate(candidate.material_slots):
                mat = original_material(slot.material)
                if not mat or not mat.use_nodes:
                    continue
                names = tuple(n.name for n in mat.node_tree.nodes
                              if n.type == 'TEX_IMAGE' and same_texture(n.image, node.image))
                if not names:
                    continue
                if candidate.library or candidate.data.library:
                    raise ValueError('Make all parts of the model local before applying')
                found.append((candidate, slot_index, mat, names))
        return found

    def previewing(self):
        target = settings().target
        return bool(target) and any(slot.material and slot.material.get(PREVIEW) is not None
                                    for slot in target.material_slots)

    def refresh_preview(self):
        """Show the result on the model, or the original while comparing."""
        s = settings()
        self.restore()
        if not (s.live and not s.original and s.target and self.ready and self.result_image):
            return
        copies = {}
        for obj, index, material, names in self.targets():
            copy = copies.get(material.name)
            if copy is None:
                copy = copies[material.name] = material.copy()
                copy.name = material.name+' · Posterize preview'
                copy[PREVIEW] = material.name
                copy[PREVIEW+'_link'] = {}
                for name in names:
                    copy.node_tree.nodes[name].image = self.result_image
            slot = obj.material_slots[index]
            links = dict(copy[PREVIEW+'_link'])
            links[obj.name] = slot.link
            copy[PREVIEW+'_link'] = links
            # OBJECT link: other objects sharing this mesh keep their look.
            slot.link = 'OBJECT'
            slot.material = copy

    def restore(self):
        scene = self.scene
        drop_previews(scene.objects if scene else None)

    def apply(self):
        if not self.wait():
            raise ValueError('The preview is still updating')
        self.restore()
        targets = self.targets()
        result = make_image(Path(self.source_name).stem+' · Posterized', self.result)
        result[APPLIED] = True
        result.pack()
        # An earlier Apply's material changes in place. A material other
        # models also use is copied once, so only this model changes.
        shared = {}
        for _, _, material, _ in targets:
            if material.name not in shared:
                users = sum(1 for other in bpy.data.objects for slot in getattr(other, 'material_slots', ())
                            if slot.material == material)
                mine = sum(1 for t in targets if t[2] == material)
                shared[material.name] = material.get(APPLIED) is None and users > mine
        replaced = set()
        for obj, index, material, names in targets:
            target = material
            if shared[material.name]:
                target = bpy.data.materials.get(material.name+' · Posterized')
                if target is None or target.get(APPLIED) is None:
                    target = material.copy()
                    target.name = material.name+' · Posterized'
                    target[APPLIED] = True
                slot = obj.material_slots[index]
                if obj.data.users > 1:
                    slot.link = 'OBJECT'  # other objects with this mesh keep the original
                slot.material = target
            for node_name in names:
                node = target.node_tree.nodes[node_name]
                if node.image and node.image.get(APPLIED):
                    replaced.add(node.image.name)
                node.image = result
        for name in replaced:
            image = bpy.data.images.get(name)
            if image and image.users == 0:
                bpy.data.images.remove(image)
        count = len({obj.name for obj, _, _, _ in targets})
        self.status = f'Applied to {count} model part'+('s' if count != 1 else '')
        return result

    def export(self, filepath):
        if not self.wait():
            raise ValueError('The preview is still updating')
        path = Path(bpy.path.abspath(filepath)).resolve()
        if self.source_path and path == Path(self.source_path).resolve():
            raise ValueError('Choose a new filename; the source texture is preserved')
        from .pack_manager import active_pack_roots
        for root in active_pack_roots(self.scene):
            if path.is_relative_to(Path(root).resolve()):
                raise ValueError('Save outside resource packs to preserve their files')
        write_png(path, self.result)
        self.status = 'Saved '+path.name

    def close(self):
        self.restore()
        if self._thread:
            self._thread.join(5)
        self.overlays.clear()
        for image in (self.original_image, self.result_image):
            if image and image.users == 0:
                remove_preview_image(image)


def original_material(material):
    """The material a preview copy stands in for (or ``material`` itself)."""
    if material is not None and material.get(PREVIEW) is not None:
        return bpy.data.materials.get(material[PREVIEW])
    return material


def close():
    global SESSION
    from .posterize_page import _CANVASES
    for canvas in list(_CANVASES):
        canvas.finish(bpy.context)
    session, SESSION = SESSION, None
    if session:
        session.close()


def open_source(image, context):
    global SESSION
    pixels = image_pixels(image)  # before closing: the source may be our preview
    name = image.name
    filepath = bpy.path.abspath(image.filepath) if image.filepath else ''
    close()
    s = settings(context)
    s.original = False
    s.zoom, s.pan_x, s.pan_y = 1, .5, .5
    SESSION = Session(pixels, name, filepath, context.scene)
    if not bpy.app.timers.is_registered(tick):
        bpy.app.timers.register(tick, first_interval=.02)
    from .gpu_framework import runtime
    for session in runtime._ACTIVE.values():
        session.state['app.active_tab'] = 'POSTERIZE'
        session.state.pop('_menu', None)
    redraw()
    return SESSION


def tick():
    if not ENABLED or SESSION is None:
        return None
    session = SESSION
    try:
        if session.scene is None or bpy.context.scene != session.scene:
            close()
            redraw()
            return None
        session.tick()
    except Exception as exc:
        if SESSION is session:
            session.restore()
            message = 'Preview error: '+str(exc)
            if session.status != message:
                session.status = message
                redraw()
        return POLL_IDLE
    return POLL_BUSY if session.busy else POLL_IDLE


@persistent
def before_save(*args):
    """Saved files never contain previews."""
    if SESSION:
        SESSION.restore()


@persistent
def after_save(*args):
    if SESSION:
        try:
            SESSION.refresh_preview()
        except (ValueError, ReferenceError, RuntimeError):
            pass


@persistent
def before_undo(*args):
    if SESSION:
        SESSION.restore()


@persistent
def after_undo(*args):
    """Undo brings datablocks back as they were: drop any preview it restored,
    keep the session (protection, kept colors) and show the preview again."""
    drop_previews()
    if SESSION:
        if SESSION.scene is None:
            close()
        else:
            after_save()
    redraw()


@persistent
def before_load(*args):
    close()


@persistent
def after_load(*args):
    drop_previews()


HANDLERS = (('save_pre', before_save), ('save_post', after_save), ('undo_pre', before_undo),
            ('redo_pre', before_undo), ('undo_post', after_undo), ('redo_post', after_undo),
            ('load_pre', before_load), ('load_post', after_load))


def register():
    global ENABLED
    bpy.utils.register_class(PosterizeSettings)
    bpy.types.WindowManager.mcbedrock_posterize = bpy.props.PointerProperty(type=PosterizeSettings, options={'SKIP_SAVE'})
    for name, handler in HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if handler not in handlers:
            handlers.append(handler)
    ENABLED = True


def unregister():
    global ENABLED
    ENABLED = False
    close()
    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)
    for name, handler in HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if handler in handlers:
            handlers.remove(handler)
    if hasattr(bpy.types.WindowManager, 'mcbedrock_posterize'):
        del bpy.types.WindowManager.mcbedrock_posterize
    try:
        bpy.utils.unregister_class(PosterizeSettings)
    except RuntimeError:
        pass
