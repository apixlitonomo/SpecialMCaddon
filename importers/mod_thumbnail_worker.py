"""Render thumbnails of mod JARs' models in a headless Blender that stays warm.

blender --background --factory-startup --python mod_thumbnail_worker.py -- <control.json>

``control.json`` names the add-on folder, the image size, an order file and
an error file. The import panel rewrites the order file as it scrolls:
``{"start": <start.json>, "order": [catalog keys, the cards on screen
first]}``, where ``start.json`` names the JAR, its thumbnail folder and
every model's catalog record (so the JAR's classes are not scanned again).
Between models the worker reads the order again and renders the first model
that has no PNG yet. Before rendering one it creates a ``.claim`` file beside
its PNG, so other workers take other models. Each model is imported with the
builders File > Import uses, framed from the front left, rendered with EEVEE
on a transparent background and written under its final name in one step.
A model that fails leaves a ``.failed`` file instead, so it is not tried
again.

It starts by compiling EEVEE's shaders with an empty render while a thread
opens the vanilla client JAR (the list of installed ones comes from Blender's
UI when it has one), so the first model renders at full speed; one that stays
up then serves the next mod dropped at once. Like the browser's thumbnail worker
it loads only the importers and builders it needs, without registering the
add-on. It quits when Blender exits (or the panel closes its stdin), when the
order file is gone, or after two minutes with nothing to do.
"""

import json
import math
import os
import sys
import threading
import time
import traceback
import types
from hashlib import sha1
from importlib import import_module

IDLE_SECONDS = 120.0
PURGE_EVERY = 8
STALE_CLAIM = 30.0  # seconds: a claim this old was left by a worker that stopped mid-model


def output_path(folder, key):
    """The PNG of ``key``; ``mod_thumbnails`` names them the same way."""
    return os.path.join(folder, sha1(key.encode("utf-8")).hexdigest()[:20] + ".png")


def _bootstrap(addon_dir):
    """The add-on's modules without its ``__init__`` (no registration)."""
    addon_dir = os.path.abspath(addon_dir)
    name = os.path.basename(addon_dir)
    if os.path.dirname(addon_dir) not in sys.path:
        sys.path.insert(0, os.path.dirname(addon_dir))
    if name not in sys.modules:
        package = types.ModuleType(name)
        package.__path__ = [addon_dir]
        sys.modules[name] = package
    return name


def _studio(scene, width, height):
    import bpy

    engines = {item.identifier for item in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items}
    scene.render.engine = "BLENDER_EEVEE" if "BLENDER_EEVEE" in engines else "BLENDER_EEVEE_NEXT"
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = True
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    try:
        scene.eevee.taa_render_samples = 8  # as clean as 16 at card size, a third faster
    except AttributeError:
        pass
    world = bpy.data.worlds.new("Mod thumbnail light")
    world.use_nodes = True
    background = world.node_tree.nodes.get("Background")
    background.inputs["Color"].default_value = (0.42, 0.45, 0.5, 1.0)
    background.inputs["Strength"].default_value = 0.85
    scene.world = world
    sun_data = bpy.data.lights.new("Mod thumbnail sun", type="SUN")
    sun_data.energy = 3.2
    sun_data.angle = math.radians(12)
    sun = bpy.data.objects.new("Mod thumbnail sun", sun_data)
    sun.rotation_euler = (math.radians(50), 0.0, math.radians(-35))
    scene.collection.objects.link(sun)
    camera_data = bpy.data.cameras.new("Mod thumbnail camera")
    camera_data.type = "ORTHO"
    camera = bpy.data.objects.new("Mod thumbnail camera", camera_data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    return camera


def _points(objects):
    import bpy

    depsgraph = bpy.context.evaluated_depsgraph_get()
    points = []
    for obj in objects:
        if obj.type != "MESH":
            continue
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        try:
            points.extend(evaluated.matrix_world @ vertex.co for vertex in mesh.vertices)
        finally:
            evaluated.to_mesh_clear()
    return points


def _frame(camera, points, aspect):
    import bpy
    from mathutils import Vector

    low = Vector(tuple(min(point[axis] for point in points) for axis in range(3)))
    high = Vector(tuple(max(point[axis] for point in points) for axis in range(3)))
    center = (low + high) * 0.5
    # Front left, a little above: models face -Y after import.
    direction = Vector((-0.55, -1.0, 0.5)).normalized()
    camera.location = center + direction * max((high - low).length, 0.5) * 3.0
    camera.rotation_euler = (-direction).to_track_quat("-Z", "Y").to_euler()
    bpy.context.view_layer.update()
    inverse = camera.matrix_world.inverted()
    local = [inverse @ point for point in points]
    xs = [point.x for point in local]
    ys = [point.y for point in local]
    # Fit the wider of the model's two extents into the tile, with a margin.
    width = (max(xs) - min(xs)) * 1.18 + 1e-3
    height = (max(ys) - min(ys)) * 1.18 + 1e-3
    scale = max(width, height * aspect)
    camera.data.ortho_scale = scale
    camera.data.sensor_fit = "HORIZONTAL"
    camera.data.shift_x = (max(xs) + min(xs)) * 0.5 / scale
    camera.data.shift_y = (max(ys) + min(ys)) * 0.5 / scale


def _watch_stdin(stop):
    """Blender exiting closes the worker's stdin; so does the panel, to stop it."""
    try:
        while sys.stdin.buffer.read(4096):
            pass
    except (AttributeError, OSError, ValueError):
        pass
    stop.set()


class _Job:
    """One JAR's models, from its start file."""

    def __init__(self, path, mod_jar):
        with open(path, encoding="utf-8") as stream:
            start = json.load(stream)
        self.jar, self.folder = start["jar"], start["folder"]
        self.assets = {record["key"]: mod_jar.ModAsset(**record) for record in start["assets"]}
        self.finished = set()  # keys with a PNG or a .failed file
        self.tries = {}  # key -> attempts that hit a file problem
        os.makedirs(self.folder, exist_ok=True)

    def claim(self, order):
        """The first model in ``order`` with no PNG, ``.failed`` or other worker on it."""
        for key in order:
            if key in self.finished or key not in self.assets:
                continue
            output = output_path(self.folder, key)
            base = os.path.splitext(output)[0]
            if os.path.isfile(output) or os.path.isfile(base + ".failed"):
                self.finished.add(key)
                continue
            try:
                os.close(os.open(base + ".claim", os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            except FileExistsError:  # another worker has it, unless that worker is gone
                try:
                    if time.time() - os.path.getmtime(base + ".claim") > STALE_CLAIM:
                        os.remove(base + ".claim")
                except OSError:
                    pass
                continue
            except OSError:
                continue
            if os.path.isfile(output):  # finished just before the claim
                os.remove(base + ".claim")
                self.finished.add(key)
                continue
            return key
        return None


def is_file_problem(exc):
    """A file missing, in use or being unpacked by another worker: nothing wrong with the model."""
    return isinstance(exc, OSError) or "Archive file was not found" in str(exc)


def _render_one(job, key, camera, aspect, mod_builder):
    import bpy

    scene = bpy.context.scene
    asset = job.assets[key]
    output = output_path(job.folder, key)
    base = os.path.splitext(output)[0]
    before = set(bpy.data.objects)
    try:
        if asset.kind in ("entity", "model"):
            mod_builder.import_mod_entity(bpy.context, job.jar, asset)
        else:
            mod_builder.import_mod_json_model(bpy.context, job.jar, asset)
        points = _points([obj for obj in bpy.data.objects if obj not in before])
        if not points:
            raise ValueError("nothing to draw")
        _frame(camera, points, aspect)
        # Written aside, then renamed: the panel never loads half a PNG.
        scene.render.filepath = base + ".partial.png"
        bpy.ops.render.render(write_still=True)
        os.replace(base + ".partial.png", output)
    except Exception as exc:  # one broken model must not stop the others
        if is_file_problem(exc) or not os.path.isfile(job.jar):
            # Not the model: a file in use, the JAR gone. Try again (three times).
            job.tries[key] = job.tries.get(key, 0) + 1
            if job.tries[key] < 3:
                return
        with open(base + ".failed", "w", encoding="utf-8") as stream:
            stream.write(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        job.finished.add(key)
    else:
        job.finished.add(key)
    finally:
        for obj in [obj for obj in bpy.data.objects if obj not in before]:
            bpy.data.objects.remove(obj, do_unlink=True)
        try:
            os.remove(base + ".claim")
        except OSError:
            pass


def run(control):
    import bpy

    package = _bootstrap(control["addon_dir"])
    mod_jar = import_module(package + ".importers.mod_jar")
    mod_models = import_module(package + ".importers.mod_models")
    mod_builder = import_module(package + ".builders.mod_builder")
    if control.get("vanilla_jars") is not None:
        import_module(package + ".importers.vanilla_assets").remember_vanilla_jars(control["vanilla_jars"])
    width, height = control["size"]
    scene = bpy.context.scene
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    camera = _studio(scene, width, height)
    stop = threading.Event()
    threading.Thread(target=_watch_stdin, args=(stop,), daemon=True).start()
    # Once per worker, side by side (rendering lets the thread run): then the
    # first model renders as fast as the rest.
    mod_models.preload_vanilla_model_library()
    bpy.ops.render.render()
    jobs, start, order = {}, "", []
    rendered, idle_since = 0, time.monotonic()
    while not stop.is_set():
        try:
            with open(control["order"], encoding="utf-8") as stream:
                wanted = json.load(stream)
            start, order = wanted.get("start", ""), wanted.get("order", [])
        except FileNotFoundError:
            return  # the panel's queue is gone
        except (OSError, ValueError):
            pass  # being replaced: read it next time
        job = None
        if start:
            job = jobs.get(start)
            if job is None:
                try:
                    job = jobs[start] = _Job(start, mod_jar)
                except (OSError, ValueError, KeyError, TypeError):
                    job = None
        key = job.claim(order) if job is not None else None
        if key is None:
            if time.monotonic() - idle_since > IDLE_SECONDS:
                return
            time.sleep(0.05)
            continue
        _render_one(job, key, camera, width / height, mod_builder)
        rendered += 1
        idle_since = time.monotonic()
        if rendered % PURGE_EVERY == 0:
            bpy.data.orphans_purge(do_recursive=True)


if __name__ == "__main__":
    control_path = sys.argv[sys.argv.index("--") + 1]
    with open(control_path, encoding="utf-8") as stream:
        control = json.load(stream)
    try:
        run(control)
    except Exception as exc:
        with open(control["error"], "w", encoding="utf-8") as stream:
            stream.write(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
