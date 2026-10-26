"""Posterize session, worker ownership, and reversible Blender assignments."""
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import time
import uuid
import zlib

import bpy
from bpy.app.handlers import persistent
import numpy as np


SESSION = None
ENABLED = False
# Results show within a tenth of a second while a job is out; otherwise the
# timer only keeps the worker's heartbeat and notices scene switches.
POLL_BUSY = .1
POLL_IDLE = 1.
HEARTBEAT = 1.  # seconds between heartbeats; the worker gives up after 120
_RESULT_FILE = re.compile(r'result-(\d+)\.npz')
_MASK_FILE = re.compile(r'mask-(\d+)\.npy')


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
    """Check for work soon: a new job goes out after its short debounce even
    while the idle timer is slow."""
    if not ENABLED:
        return
    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)
    bpy.app.timers.register(tick, first_interval=.05)


def preview_changed(self, context):
    if SESSION:
        try:
            SESSION.restore()
            if self.live and not self.original and SESSION.ready:
                SESSION.preview()
        except (ValueError, ReferenceError, RuntimeError) as exc:
            SESSION.status = str(exc)
    redraw()


def target_changed(self, context):
    if SESSION:
        SESSION.restore()
    redraw()


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


RANGE_FIELDS = ('label','enabled','hue_min','hue_max','sat_min','sat_max',
                'value_min','value_max','shades','preserve','separate_muted')


def range_properties(update=None):
    callbacks = dict(update=update) if update else {}
    result = dict(label=bpy.props.StringProperty(name='Group name',default='Custom range',**callbacks),
                  enabled=bpy.props.BoolProperty(name='Enabled',default=True,**callbacks),
                  shades=bpy.props.IntProperty(name='Shades (0 = slider)',default=0,min=0,max=256,**callbacks),
                  preserve=bpy.props.BoolProperty(name='Keep original pixels',default=False,**callbacks),
                  separate_muted=bpy.props.BoolProperty(name='Separate muted shades',default=False,**callbacks))
    for key,label,default,maximum in (
        ('hue_min','Hue start',0,360),('hue_max','Hue end',360,360),
        ('sat_min','Saturation min %',0,100),('sat_max','Saturation max %',100,100),
        ('value_min','Brightness min %',0,100),('value_max','Brightness max %',100,100)):
        result[key] = bpy.props.FloatProperty(name=label,default=default,min=0,max=maximum,**callbacks)
    return result


class PosterizeRange(bpy.types.PropertyGroup):
    __annotations__ = range_properties(changed)


def range_configuration(s):
    return [dict(name=group.label,**{key:getattr(group,key) for key in RANGE_FIELDS if key!='label'}) for group in s.ranges]


def reset_ranges(s):
    from ..builders.posterize import DEFAULT_GROUPS
    s.ranges.clear()
    for config in DEFAULT_GROUPS:
        group = s.ranges.add()
        for key,value in config.items():
            setattr(group,'label' if key=='name' else key,value)
        group.name = group.label
    s.ranges_initialized = True
    s.range_index = 0
    if SESSION:
        SESSION.request()


class PosterizeSettings(bpy.types.PropertyGroup):
    lock_groups: bpy.props.BoolProperty(name='Lock Color Ranges',default=True,update=changed)
    advanced_ranges: bpy.props.BoolProperty(name='Advanced Color Ranges',default=False)
    highlight_range: bpy.props.BoolProperty(name='Highlight Selected Range',default=False)
    ranges_initialized: bpy.props.BoolProperty(default=False)
    ranges: bpy.props.CollectionProperty(type=PosterizeRange)
    range_index: bpy.props.IntProperty(default=0,min=0)
    strength: bpy.props.IntProperty(name='Posterization', default=50, min=0, max=100, update=changed)
    cleanup: bpy.props.IntProperty(name='Detail Cleanup', default=0, min=0, max=100, update=changed)
    tileable: bpy.props.BoolProperty(name='Tileable', default=False, update=changed)
    live: bpy.props.BoolProperty(name='Live Model Preview', default=False, update=preview_changed)
    original: bpy.props.BoolProperty(name='Show Original on Model', default=False, update=preview_changed)
    model_layers: bpy.props.BoolProperty(name='All Model Layers', default=True,
        description='Update matching textures on child meshes and sibling layers in this model', update=preview_changed)
    target: bpy.props.PointerProperty(name='Object', type=bpy.types.Object, update=target_changed)
    slot: bpy.props.IntProperty(name='Material Slot', default=0, min=0, update=target_changed)
    node: bpy.props.StringProperty(name='Image Node', update=target_changed)
    tool: bpy.props.EnumProperty(name='Preview Tool', items=[('PAN','Pan','Drag to pan both previews'),
        ('PROTECT','Protect','Paint pixels to preserve'), ('ERASE','Erase','Erase protection'),
        ('LOCK','Keep Color','Click a source pixel to toggle its color lock'),
        ('GROUP','Sample Range','Click the source texture to define a color range')], default='PAN')
    radius: bpy.props.IntProperty(name='Brush Radius (pixels)', default=1, min=1, max=64)
    zoom: bpy.props.FloatProperty(name='Zoom', default=1, min=1, max=32)
    pan_x: bpy.props.FloatProperty(default=.5, min=0, max=1)
    pan_y: bpy.props.FloatProperty(default=.5, min=0, max=1)


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


class Session:
    def __init__(self, pixels, name, filepath, scene):
        self.source = pixels
        self.source_name = name
        self.source_path = filepath
        self.scene = scene
        self.directory = Path(tempfile.mkdtemp(prefix='sparkle_posterize_'))
        self.worker = None
        self.log = None
        self.original_image = None
        self.result_image = None
        self.result = None
        self.mask = np.zeros(self.source.shape[:2], bool)
        self.locked = set()
        self.generation = 0
        self.sent = -1
        self.received = -1
        self.due = 0
        self.status = 'Preparing preview…'
        self.stats = {}
        self.group_map = None
        self.assignments = []
        self.preview_materials = []
        # Bumped when the protection mask or the result pixels change: the
        # previews' overlay textures and the sidebar's repaint follow them.
        self.mask_version = 0
        self.result_version = 0
        self.overlays = {}
        self._mask_sent = None  # (file name, copy of the mask it holds)
        self._result_stamp = None
        self._beat = float('-inf')
        try:
            np.save(self.directory/'source.npy', self.source, allow_pickle=False)
            self.original_image = make_image('Posterize Original', self.source)
        except Exception:
            self.close()
            raise
        self.request()

    @property
    def ready(self):
        return self.result is not None and self.received == self.generation

    @property
    def outstanding(self):
        """A job went to the worker and its result has not come back."""
        return self.sent >= 0 and self.sent != self.received

    @property
    def busy(self):
        """A job waits for its debounce or for a worker that is still running."""
        return self.sent != self.generation or (
            self.outstanding and self.worker is not None and self.worker.poll() is None)

    def request(self):
        self.generation += 1
        self.due = time.monotonic()+.18
        self.status = 'Updating preview…'
        wake()

    def mask_edited(self):
        """The protection mask changed: its overlay redraws and the preview converts again."""
        self.mask_version += 1
        self.request()

    def start_worker(self):
        if self.worker and self.worker.poll() is None:
            return
        if self.log:
            self.log.close()
        self.log = (self.directory/'worker.log').open('ab')
        script = Path(__file__).resolve().parents[1]/'importers/posterize_worker.py'
        self.worker = subprocess.Popen([bpy.app.binary_path, '--background', '--factory-startup',
            '--threads', '2', '--python', str(script), '--', str(self.directory)],
            stdout=self.log, stderr=self.log,
            creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS) if os.name == 'nt' else 0)

    def tick(self):
        self._ensure_files()
        now = time.monotonic()
        if now - self._beat >= HEARTBEAT:
            (self.directory/'heartbeat').touch()
            self._beat = now
        if self.sent != self.generation and now >= self.due:
            self._send()
        self._collect()
        if self.worker and self.worker.poll() is not None and self.outstanding:
            self.status = 'Preview worker stopped. Adjust a setting to retry.'

    def _ensure_files(self):
        """Temporary-file cleaners can empty the folder mid-session: write the source again."""
        if (self.directory/'source.npy').is_file():
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        np.save(self.directory/'source.npy', self.source, allow_pickle=False)
        self._mask_sent = None
        self._result_stamp = None
        self.sent = -1  # the job goes out again

    def _mask_file(self, generation):
        """The mask's file for a job, written only when the mask changed since the last one."""
        if self._mask_sent is not None and np.array_equal(self._mask_sent[1], self.mask):
            if (self.directory/self._mask_sent[0]).is_file():
                return self._mask_sent[0]
        else:
            self.mask_version += 1  # also when edited without mask_edited()
        name = f'mask-{generation}.npy'
        np.save(self.directory/name, self.mask, allow_pickle=False)
        self._mask_sent = (name, self.mask.copy())
        return name

    def _send(self):
        s = settings()
        generation = self.generation
        job = dict(generation=generation, source='source.npy', mask=self._mask_file(generation),
                   strength=s.strength, cleanup=s.cleanup, tileable=s.tileable,
                   locked=[list(c) for c in sorted(self.locked)],
                   lock_groups=s.lock_groups,groups=range_configuration(s))
        temporary = self.directory/'job.tmp'
        temporary.write_text(json.dumps(job))
        try:
            os.replace(temporary, self.directory/'job.json')
        except PermissionError:
            return  # Windows: the worker is reading the last job. Send this one next tick.
        self.start_worker()
        self.sent = generation

    def _collect(self):
        """Take the worker's newest result once; unchanged files are not read again."""
        result_file = self.directory/'result.json'
        try:
            stat = result_file.stat()
        except FileNotFoundError:
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._result_stamp:
            return
        try:
            result = json.loads(result_file.read_text())
        except (OSError, ValueError):
            return  # being replaced right now: read it next tick
        self._result_stamp = stamp
        generation = result['generation']
        if generation == self.generation and generation != self.received:
            if 'error' in result:
                self.status = 'Conversion failed: '+result['error'].strip().splitlines()[-1]
                self.received = generation
                self.result = None
                self.restore()
            else:
                with np.load(self.directory/result['file'], allow_pickle=False) as data:
                    pixels, group_map = data['pixels'], data['group_map']
                self.accept(generation, pixels, result['stats'],group_map=group_map)
            redraw()
        self._discard_files(generation)

    def _discard_files(self, generation):
        """Workers never revisit completed generations. Keep disk use bounded."""
        current = self._mask_sent[0] if self._mask_sent else None
        for path in self.directory.iterdir():
            result = _RESULT_FILE.fullmatch(path.name)
            mask = _MASK_FILE.fullmatch(path.name)
            if ((result and int(result.group(1)) <= generation)
                    or (mask and path.name != current and int(mask.group(1)) < generation)):
                path.unlink(missing_ok=True)

    def show_result(self, pixels):
        """Put ``pixels`` in the session's one result image; True when it had to be made.

        Updating it in place keeps the previews and a live model preview on
        the same datablock and material copies instead of new ones per result.
        """
        image = self.result_image
        height, width = pixels.shape[:2]
        try:
            reusable = image is not None and tuple(image.size) == (width, height)
        except ReferenceError:  # removed by hand
            image, reusable = None, False
        if reusable:
            image.pixels.foreach_set(np.asarray(pixels, np.float32).ravel())
            image.update()
            forget_preview_texture(image)
            return False
        self.result_image = make_image('Posterize Result', pixels)
        try:
            if image is not None and image.users == 0:
                remove_preview_image(image)
        except ReferenceError:
            pass
        return True

    def accept(self, generation, pixels, stats, group_map=None):
        if generation != self.generation:
            return False
        created = self.show_result(pixels)
        self.result = pixels
        self.result_version += 1
        self.received = generation
        self.stats = stats
        self.group_map = group_map
        self.status = f"{stats['colors']} colors"
        if 'families' in stats:
            self.status += f" · {stats['families']} color families"
        if stats.get('limited'):
            self.status += ' · protected pixels / color locks limit reduction'
        # A live preview already shows the result image, which now has the new pixels.
        if settings().live and not settings().original and (created or not self.assignments):
            try:
                self.preview()
            except ValueError as exc:
                self.status += ' · '+str(exc)
        return True

    def target(self):
        s = settings()
        obj = s.target
        if not obj or obj.name not in self.scene.objects or s.slot >= len(obj.material_slots):
            raise ValueError('Choose a model material target')
        if obj.library or obj.data.library:
            raise ValueError('Linked objects must be made local before applying')
        slot = obj.material_slots[s.slot]
        material = slot.material
        if not material or not material.use_nodes:
            raise ValueError('Choose a material with image nodes')
        node = material.node_tree.nodes.get(s.node)
        if not node or node.type != 'TEX_IMAGE':
            raise ValueError('Choose an image texture node')
        if node.image and node.image.colorspace_settings.is_data:
            raise ValueError('Choose a color texture target, not a material data map')
        # Only top-level image nodes are offered; nested groups may be shared.
        return obj, s.slot, material, node

    def preview(self):
        if not self.ready:
            return
        self.restore()
        self.assign_image(self.result_image, self.targets(), ' · Posterize preview')

    def targets(self):
        """Resolve all slots before changing any; unrelated image users stay put."""
        obj,index,material,node = self.target()
        if not settings().model_layers or not node.image or obj.type != 'MESH':
            return [(obj,index,material,(node.name,))]
        targets = []
        for candidate in model_meshes(self.scene,obj):
            for slot_index,slot in enumerate(candidate.material_slots):
                mat = slot.material
                if not mat or not mat.use_nodes:
                    continue
                names = tuple(n.name for n in mat.node_tree.nodes if n.type == 'TEX_IMAGE' and same_texture(n.image,node.image))
                if not names:
                    continue
                if candidate.library or candidate.data.library:
                    raise ValueError('Make all matching model layers local before applying')
                targets.append((candidate,slot_index,mat,names))
        return targets

    def assign_image(self, image, targets, suffix):
        """Keep shared materials shared within this operation and roll back failures."""
        copies = {}
        try:
            for obj,index,material,names in targets:
                key = (material.as_pointer(),names)
                if key not in copies:
                    copy = material.copy()
                    self.preview_materials.append(copy)
                    copy.name = material.name+suffix
                    for name in names:
                        copy.node_tree.nodes[name].image = image
                        copy.node_tree.nodes[name].interpolation = 'Closest'
                    copies[key] = copy
                copy = copies[key]
                slot = obj.material_slots[index]
                self.assignments.append((obj,index,slot.link,material,copy))
                # OBJECT link isolates other instances of the same mesh data.
                slot.link = 'OBJECT'
                slot.material = copy
        except Exception:
            self.restore()
            raise

    def restore(self):
        for obj, index, link, original, preview in self.assignments:
            try:
                if index < len(obj.material_slots):
                    slot = obj.material_slots[index]
                    if slot.material == preview:
                        slot.material = original
                        slot.link = link
            except (ReferenceError, RuntimeError):
                pass
        self.assignments.clear()
        for material in self.preview_materials:
            try:
                if material.users == 0:
                    bpy.data.materials.remove(material)
            except ReferenceError:
                pass
        self.preview_materials.clear()

    def apply(self):
        if not self.ready:
            raise ValueError('Wait for the current preview to finish')
        self.restore()
        targets = self.targets()
        result = make_image(self.source_name+' · Posterized', self.result)
        try:
            result.pack()
            self.assign_image(result,targets,' · Posterized')
        except Exception:
            if result.users == 0:
                bpy.data.images.remove(result)
            raise
        self.assignments.clear()
        self.preview_materials.clear()
        count = len({obj.as_pointer() for obj,_,_,_ in targets})
        self.status = f'Applied to {count} model mesh'+('es' if count != 1 else '')
        return result

    def export(self, filepath):
        if not self.ready:
            raise ValueError('Wait for the current preview to finish')
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
        try:
            (self.directory/'stop').touch()
        except OSError:
            pass  # Termination below still works if temporary storage was lost.
        if self.worker and self.worker.poll() is None:
            self.worker.terminate()
            try:
                self.worker.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.worker.kill()
                self.worker.wait(timeout=3)
        if self.log:
            self.log.close()
        self.overlays.clear()
        for image in (self.original_image, self.result_image):
            try:
                if image and image.users == 0:
                    remove_preview_image(image)
            except ReferenceError:
                pass
        # Only our tempfile.mkdtemp directory is removed.
        if self.directory.parent == Path(tempfile.gettempdir()) and self.directory.name.startswith('sparkle_posterize_'):
            shutil.rmtree(self.directory, ignore_errors=True)


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
    pixels = image_pixels(image)  # Capture before closing: source may be our preview.
    name = image.name
    filepath = bpy.path.abspath(image.filepath) if image.filepath else ''
    close()
    s = settings(context)
    if not s.ranges_initialized:
        reset_ranges(s)
    s.live = False
    s.original = False
    s.zoom, s.pan_x, s.pan_y = 1, .5, .5
    SESSION = Session(pixels, name, filepath, context.scene)
    if not bpy.app.timers.is_registered(tick):
        bpy.app.timers.register(tick, first_interval=.1)
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
        if bpy.context.scene != session.scene:
            close()
            redraw()
            return None
        session.tick()
    except Exception as exc:
        if SESSION is session:
            # A file hiccup (a folder cleaner, a locked file) leaves the model
            # preview alone; anything else puts the original back.
            if not isinstance(exc, OSError):
                session.restore()
            message = 'Preview error: '+str(exc)
            if session.status != message:
                session.status = message
                redraw()
        return POLL_IDLE
    return POLL_BUSY if session.busy else POLL_IDLE


@persistent
def restore_before_change(*args):
    close()
    redraw()


def register():
    global ENABLED
    bpy.utils.register_class(PosterizeRange)
    bpy.utils.register_class(PosterizeSettings)
    bpy.types.WindowManager.mcbedrock_posterize = bpy.props.PointerProperty(type=PosterizeSettings, options={'SKIP_SAVE'})
    for handlers in (bpy.app.handlers.save_pre, bpy.app.handlers.load_pre,
                     bpy.app.handlers.undo_pre, bpy.app.handlers.redo_pre):
        if restore_before_change not in handlers:
            handlers.append(restore_before_change)
    ENABLED = True


def unregister():
    global ENABLED
    ENABLED = False
    close()
    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)
    for handlers in (bpy.app.handlers.save_pre, bpy.app.handlers.load_pre,
                     bpy.app.handlers.undo_pre, bpy.app.handlers.redo_pre):
        if restore_before_change in handlers:
            handlers.remove(restore_before_change)
    if hasattr(bpy.types.WindowManager, 'mcbedrock_posterize'):
        del bpy.types.WindowManager.mcbedrock_posterize
    try:
        bpy.utils.unregister_class(PosterizeSettings)
    except RuntimeError:
        pass
    try:
        bpy.utils.unregister_class(PosterizeRange)
    except RuntimeError:
        pass
