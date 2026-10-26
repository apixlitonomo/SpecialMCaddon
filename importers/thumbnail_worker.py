"""Headless thumbnail render worker.

Run ONLY as a standalone Blender script -- never imported by the add-on::

    blender -b --factory-startup -noaudio -P thumbnail_worker.py -- queue.json

The queue JSON looks like::

    {
      "size": 128,
      "resource_pack": "C:/packs/vanilla",
      "items": [
        {"file_path": "...json", "asset_type": "particle", "identifier": "...",
         "geometry_index": -1, "output": "C:/.../mc_thumb_cache/<md5>.png"}
      ]
    }

Two rendering paths:

* ``asset_type == "particle"`` -- no simulation. The particle JSON is read for
  its 2D texture path and the UV rect of the effect's first frame; the texture
  is loaded, cropped to that rect, scaled and saved with Blender's native
  ``bpy.data.images`` API. Never PIL/cv2: neither ships with Blender's Python
  and importing them kills the worker.
* everything else -- the real builders import the model into a clean Eevee
  scene, an orthographic camera frames it, one still is rendered, and the
  objects are purged before the next item.
"""

import array
import hashlib
import json
import os
import time
import subprocess  # noqa: F401 - kept for parity with thumbnail_cache tooling
import sys
import types
from pathlib import Path

import bpy
from mathutils import Vector


TEXTURE_EXTENSIONS = (".png", ".tga", ".jpg", ".jpeg")
DEFAULT_SIZE = 128

# Marker written beside the missing PNG when an asset cannot be rendered, so the
# UI can show an error icon instead of a neutral one and the queue builder can
# stop retrying a broken asset on every refresh. Must match
# ``thumbnail_cache.FAILED_SUFFIX``.
FAILED_SUFFIX = ".failed-v2"


# ----------------------------------------------------------------------
# Package bootstrap
# ----------------------------------------------------------------------
def _bootstrap_package():
    """Expose the add-on's submodules without executing its ``__init__``.

    The add-on ``__init__`` imports ``simulator`` and the UI at module scope.
    The worker must not pull those in, so a stub package object is installed in
    ``sys.modules`` with ``__path__`` pointed at the add-on directory. Relative
    imports inside ``builders/`` (``from ...importers import ...``) resolve
    against that stub.
    """
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pkg_name = os.path.basename(pkg_root)
    parent = os.path.dirname(pkg_root)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    if pkg_name not in sys.modules:
        stub = types.ModuleType(pkg_name)
        stub.__path__ = [pkg_root]
        sys.modules[pkg_name] = stub
    return pkg_name


PACKAGE = _bootstrap_package()

from importlib import import_module

_preview_helpers = import_module(PACKAGE + ".importers.preview_fallback")
png_has_visible_pixels = _preview_helpers.png_has_visible_pixels
png_is_complete = _preview_helpers.png_is_complete
write_deterministic_fallback = _preview_helpers.write_deterministic_fallback


# ----------------------------------------------------------------------
# Scene setup
# ----------------------------------------------------------------------
def _set_eevee(render):
    """Select Eevee. 4.2-4.5 renamed the engine to BLENDER_EEVEE_NEXT."""
    for engine in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
        try:
            render.engine = engine
            return render.engine
        except TypeError:
            continue
    return render.engine


def setup_eevee_environment(scene, size):
    """Configure Eevee for the fastest possible flat-shaded still.

    One sample, no shadows/AO/bloom/volumetrics/motion-blur/AA. The scene has
    no lamps (see the flat-world lighting below), so this is about stripping
    every per-pixel effect Eevee would otherwise evaluate even with zero
    lights, plus keeping TAA at a single sample so nothing accumulates across
    frames.
    """
    render = scene.render
    _set_eevee(render)
    render.resolution_x = size
    render.resolution_y = size
    render.resolution_percentage = 100
    render.threads_mode = 'FIXED'
    render.threads = 2
    render.film_transparent = True
    render.image_settings.file_format = 'PNG'
    render.image_settings.color_mode = 'RGBA'
    render.image_settings.compression = 15
    render.use_motion_blur = False

    eevee = scene.eevee
    for attr, value in (
        ("taa_render_samples", 1),
        ("taa_samples", 1),
        ("use_gtao", False),
        ("use_bloom", False),
        ("use_ssr", False),
        ("use_volumetric_lights", False),
        ("use_volumetric_shadows", False),
        ("use_shadows", False),
        ("use_soft_shadows", False),
        ("use_raytracing", False),
    ):
        if hasattr(eevee, attr):
            setattr(eevee, attr, value)

    try:
        scene.view_settings.view_transform = 'Standard'
    except TypeError:
        pass

    return render


def setup_scene(size):
    """Return a clean scene: Eevee, size x size, transparent film, flat light."""
    scene = bpy.context.window.scene
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)

    setup_eevee_environment(scene, size)

    # Flat lighting: a bright, even world instead of directional lamps, so the
    # pixel-art textures read the same as they do in game.
    world = bpy.data.worlds.get("_mcthumb_world") or bpy.data.worlds.new("_mcthumb_world")
    world.use_nodes = True
    background = world.node_tree.nodes.get("Background")
    if background:
        background.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
        background.inputs["Strength"].default_value = 1.0
    scene.world = world

    camera_data = bpy.data.cameras.get("_mcthumb_cam") or bpy.data.cameras.new("_mcthumb_cam")
    camera_data.type = 'ORTHO'
    camera_data.ortho_scale = 2.0
    camera = bpy.data.objects.new("_mcthumb_cam", camera_data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    return scene, camera


def _visible_meshes(objects):
    meshes = []
    for obj in objects:
        if getattr(obj, "type", None) != 'MESH':
            continue
        if getattr(obj, "hide_render", False) or getattr(obj, "hide_viewport", False):
            continue
        visible_get = getattr(obj, "visible_get", None)
        if callable(visible_get):
            try:
                if not visible_get():
                    continue
            except (RuntimeError, TypeError):
                pass
        data = getattr(obj, "data", None)
        if data is None or not getattr(data, "vertices", None):
            continue
        meshes.append(obj)
    return meshes


def _bounds(objects):
    """World-space center and largest dimension of visible mesh geometry."""
    objects = list(objects)
    visible_meshes = _visible_meshes(objects)
    candidates = visible_meshes or objects
    minimum = Vector((float("inf"),) * 3)
    maximum = Vector((float("-inf"),) * 3)
    found = False
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for obj in candidates:
        if getattr(obj, "type", None) == 'MESH' and getattr(obj, "data", None) and obj.data.vertices:
            try:
                evaluated = obj.evaluated_get(depsgraph)
            except (ReferenceError, RuntimeError):
                evaluated = obj
            corners = [evaluated.matrix_world @ Vector(corner) for corner in evaluated.bound_box]
        elif not visible_meshes:
            corners = [obj.matrix_world.translation]
        else:
            continue
        for corner in corners:
            found = True
            for axis in range(3):
                minimum[axis] = min(minimum[axis], corner[axis])
                maximum[axis] = max(maximum[axis], corner[axis])
    if not found:
        return Vector((0.0, 0.0, 0.0)), 2.0
    center = (minimum + maximum) / 2.0
    return center, max(max(maximum - minimum), 0.1)


def frame_camera(camera, objects):
    """Point the orthographic camera at the objects from a 3/4 angle."""
    center, size = _bounds(objects)
    camera.data.ortho_scale = max(size * 1.35, 0.1)
    offset = Vector((1.0, -1.0, 0.7)).normalized() * max(size * 3.0, 2.0)
    camera.location = center + offset
    direction = center - camera.location
    if direction.length > 1e-6:
        camera.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()


def clear_imported(before):
    """Remove everything created since ``before`` and purge the orphan datablocks."""
    for obj in list(bpy.data.objects):
        if obj not in before:
            bpy.data.objects.remove(obj, do_unlink=True)
    for collection in list(bpy.data.collections):
        if not collection.all_objects:
            bpy.data.collections.remove(collection)
    try:
        bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    except RuntimeError:
        pass


# ----------------------------------------------------------------------
# Particle path: texture crop, no simulation
# ----------------------------------------------------------------------
def _read_json(path):
    with open(path, "r", encoding="utf-8") as stream:
        return json.load(stream)


def _particle_effect(data):
    if not isinstance(data, dict):
        return {}
    effect = data.get("particle_effect") or data.get("minecraft:particle_effect") or data
    return effect if isinstance(effect, dict) else {}


def particle_texture_ref(data):
    """Pull the raw texture reference out of a Bedrock or Java particle JSON."""
    effect = _particle_effect(data)
    description = effect.get("description", {})
    if isinstance(description, dict):
        params = description.get("basic_render_parameters", {})
        if isinstance(params, dict) and params.get("texture"):
            return str(params["texture"])

    # Java particles: a "textures" (or legacy "values") list of texture ids.
    textures = data.get("textures", data.get("values")) if isinstance(data, dict) else None
    if isinstance(textures, dict):
        textures = list(textures.values())
    if isinstance(textures, str):
        textures = [textures]
    if isinstance(textures, list) and textures:
        return str(textures[0])
    return ""


def _as_number(value):
    """Return ``value`` as a float, or ``None`` if it is a MoLang expression.

    UV fields are allowed to hold MoLang. The worker never evaluates MoLang, so
    a non-numeric field means "no static crop" and the caller falls back to the
    full texture.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _numeric_pair(value):
    """Coerce a 2-element JSON list to a float pair, or return ``None``."""
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    first = _as_number(value[0])
    second = _as_number(value[1])
    if first is None or second is None:
        return None
    return first, second


def particle_uv_rect(data):
    """First-frame UV rect of a Bedrock particle, in the JSON's texture space.

    Returns ``(x, y, width, height, texture_width, texture_height)`` with the
    origin at the top-left, matching Bedrock's convention. Returns ``None`` when
    the effect has no static UV rect (no appearance component, MoLang-driven
    coordinates, or a degenerate size), in which case the full texture is used.
    """
    effect = _particle_effect(data)
    components = effect.get("components", {})
    if not isinstance(components, dict):
        return None

    appearance = components.get("minecraft:particle_appearance_billboard")
    if not isinstance(appearance, dict):
        appearance = components.get("minecraft:particle_appearance_stretched_billboard")
    if not isinstance(appearance, dict):
        return None

    uv_data = appearance.get("uv")
    if not isinstance(uv_data, dict):
        return None

    texture_width = _as_number(uv_data.get("texture_width", 0)) or 0.0
    texture_height = _as_number(uv_data.get("texture_height", 0)) or 0.0

    flipbook = uv_data.get("flipbook")
    if isinstance(flipbook, dict):
        # The first frame sits at base_UV; later frames step along stepUV.
        base = _numeric_pair(flipbook.get("base_UV"))
        size = _numeric_pair(flipbook.get("size_UV"))
        if base is None or size is None:
            return None
        rect = (base[0], base[1], size[0], size[1])
    else:
        base = _numeric_pair(uv_data.get("uv"))
        size = _numeric_pair(uv_data.get("uv_size"))
        if base is None or size is None:
            return None
        rect = (base[0], base[1], size[0], size[1])

    if rect[2] <= 0.0 or rect[3] <= 0.0:
        return None
    return rect + (texture_width, texture_height)


def java_first_frame_rect(texture_path, width, height):
    """Top square frame of a Java animated strip, or ``None`` for a plain texture.

    Animated Java textures are a vertical strip of square frames declared by a
    sibling ``.mcmeta``. Without the ``.mcmeta`` a tall texture is just a tall
    texture, so the sidecar file is required before cropping.
    """
    if not os.path.isfile(texture_path + ".mcmeta"):
        return None
    if height <= width or width <= 0 or height % width != 0:
        return None
    return (0.0, 0.0, float(width), float(width), float(width), float(height))


def crop_image(image, rect):
    """Return a new image holding ``rect`` of ``image``, cropped via ``pixels``.

    ``rect`` is ``(x, y, w, h, texture_width, texture_height)`` in the JSON's
    declared texture space with a top-left origin. It is rescaled onto the
    actual image resolution (packs ship 2x/4x textures against 16px UVs) and
    flipped, because ``image.pixels`` stores rows bottom-up.

    Returns ``None`` when the rect covers the whole image or degenerates to
    nothing, so the caller can keep using the original.
    """
    source_width, source_height = image.size
    if source_width <= 0 or source_height <= 0:
        return None

    x, y, width, height, texture_width, texture_height = rect
    scale_x = source_width / texture_width if texture_width > 0 else 1.0
    scale_y = source_height / texture_height if texture_height > 0 else 1.0

    left = int(round(x * scale_x))
    top = int(round(y * scale_y))
    crop_width = int(round(width * scale_x))
    crop_height = int(round(height * scale_y))

    # Clamp into the image; a rect that spills past the edge is a pack bug, not
    # a reason to fail the thumbnail.
    left = max(0, min(left, source_width - 1))
    top = max(0, min(top, source_height - 1))
    crop_width = max(1, min(crop_width, source_width - left))
    crop_height = max(1, min(crop_height, source_height - top))

    if crop_width == source_width and crop_height == source_height:
        return None

    # image.pixels is bottom-up; the rect is top-down.
    bottom = source_height - (top + crop_height)

    source_pixels = array.array('f', [0.0]) * (source_width * source_height * 4)
    image.pixels.foreach_get(source_pixels)

    cropped = array.array('f', [0.0]) * (crop_width * crop_height * 4)
    row_floats = crop_width * 4
    for row in range(crop_height):
        start = ((bottom + row) * source_width + left) * 4
        cropped[row * row_floats:(row + 1) * row_floats] = source_pixels[start:start + row_floats]

    result = bpy.data.images.new(
        "_mcthumb_crop",
        width=crop_width,
        height=crop_height,
        alpha=True,
    )
    # Match the source colour space so the float round-trip is lossless.
    try:
        result.colorspace_settings.name = image.colorspace_settings.name
    except TypeError:
        pass
    result.alpha_mode = 'STRAIGHT'
    result.pixels.foreach_set(cropped)
    result.update()
    return result


def render_texture(item, size):
    """Save a copy of a texture no larger than ``size`` pixels, keeping its shape.

    The browser draws texture cards from their own files; this keeps HD and RTX
    textures (up to 4096 px) from being loaded in full for a card.
    """
    image = None
    try:
        image = bpy.data.images.load(item["file_path"], check_existing=False)
        width, height = image.size
        if width <= 0 or height <= 0:
            return False
        scale = min(1.0, float(size) / max(width, height))
        if scale < 1.0:
            image.scale(max(1, int(round(width * scale))), max(1, int(round(height * scale))))
        image.filepath_raw = item["output"]
        image.file_format = 'PNG'
        image.save()
        return os.path.isfile(item["output"])
    except (RuntimeError, OSError) as exc:
        print("[thumb] texture downscale failed for %s: %s" % (item["file_path"], exc))
        return False
    finally:
        if image is not None:
            bpy.data.images.remove(image)


def render_particle(item, size, resource_pack):
    """Crop the particle's first frame out of its sheet, scale it, save it.

    Uses bpy.data.images exclusively -- PIL and cv2 are absent from Blender's
    bundled Python. No MoLang is evaluated and no simulator is loaded.
    """
    try:
        data = _read_json(item["file_path"])
    except (OSError, ValueError) as exc:
        print("[thumb] unreadable particle json %s: %s" % (item["file_path"], exc))
        return False

    reference = particle_texture_ref(data)
    texture_path = resolve_texture(reference, item["file_path"], resource_pack)
    if not texture_path:
        print("[thumb] no texture for %s (ref=%r)" % (item["file_path"], reference))
        return False

    image = None
    cropped = None
    try:
        image = bpy.data.images.load(texture_path, check_existing=False)
        width, height = image.size

        rect = particle_uv_rect(data)
        if rect is None:
            rect = java_first_frame_rect(texture_path, width, height)

        target = image
        if rect is not None:
            cropped = crop_image(image, rect)
            if cropped is not None:
                target = cropped
            else:
                print("[thumb] uv rect covers whole sheet, using full texture: %s" % texture_path)
        else:
            print("[thumb] no static uv rect for %s, using full texture" % item["file_path"])

        target.scale(size, size)
        target.filepath_raw = item["output"]
        target.file_format = 'PNG'
        target.save()
        return os.path.isfile(item["output"])
    except (RuntimeError, OSError) as exc:
        print("[thumb] texture copy failed for %s: %s" % (texture_path, exc))
        return False
    finally:
        for datablock in (cropped, image):
            if datablock is not None:
                bpy.data.images.remove(datablock)


def resolve_texture(reference, json_path, resource_pack=""):
    """Resolve a texture reference to an absolute file on disk, or ``""``."""
    if not reference:
        return ""
    if ":" in reference:
        reference = reference.split(":", 1)[1]
    reference = reference.replace("\\", "/").lstrip("/")

    candidates = [
        reference,
        "textures/" + reference,
        "textures/particle/" + reference,
        "assets/minecraft/textures/" + reference,
        "assets/minecraft/textures/particle/" + reference,
    ]

    roots = []
    if resource_pack and os.path.isdir(resource_pack):
        roots.append(resource_pack)

    # Walk upward from the JSON so packs found outside the configured root work.
    current = Path(json_path).resolve().parent
    for _ in range(6):
        roots.append(str(current))
        if current.parent == current:
            break
        current = current.parent

    for root in roots:
        for candidate in candidates:
            for extension in ("",) + TEXTURE_EXTENSIONS:
                path = os.path.normpath(os.path.join(root, candidate + extension))
                if os.path.isfile(path):
                    return path

    # Last resort: hunt for the bare filename inside the pack.
    basename = os.path.basename(reference)
    for root in roots[:1] or roots:
        for directory, _, files in os.walk(root):
            for name in files:
                stem, extension = os.path.splitext(name)
                if stem == basename and extension.lower() in TEXTURE_EXTENSIONS:
                    return os.path.join(directory, name)
    return ""


# ----------------------------------------------------------------------
# Mesh path: real builders, real render
# ----------------------------------------------------------------------
def _detect_kind(item):
    asset_type = (item.get("asset_type") or "").lower()
    try:
        from importlib import import_module
        bedrock_assets = import_module(PACKAGE + ".importers.bedrock_assets")
        kind = bedrock_assets.detect_json_asset(item["file_path"])
    except Exception:
        kind = ""

    # Content is authoritative. "Java Entity Model" is still a Java model,
    # and attachables are client entities despite their browser label.
    if kind == "java_model":
        return "java"
    if kind == "client_entity":
        return "entity"
    if kind == "model":
        return "bedrock"
    if "java" in asset_type:
        return "java"
    if "entity" in asset_type or "attachable" in asset_type:
        return "entity"
    if "bedrock" in asset_type or "geometry" in asset_type or "model" in asset_type:
        return "bedrock"
    return "bedrock"


def import_mesh(item):
    """Import the model with the add-on's existing builders."""
    from importlib import import_module

    context = bpy.context
    kind = _detect_kind(item)
    name = item.get("identifier") or os.path.splitext(os.path.basename(item["file_path"]))[0]

    if "block" in (item.get("asset_type") or "").lower():
        metadata = json.loads(item.get("metadata_json") or "{}")
        block_builder = import_module(
            PACKAGE + ".builders.schematic_builder"
        )
        resolver = import_module(PACKAGE + ".importers.blockstate_resolver")
        roots, vanilla_source = resolver.block_asset_context(
            metadata, item.get("resource_roots", ()), item.get("vanilla_path", ""),
        )
        block_builder.build_single_block(
            context,
            str(metadata.get("block_state") or name),
            roots,
            vanilla_path=vanilla_source,
            interpolation="Closest",
            vanilla_geometry=(
                metadata.get("geometry_policy") == "VANILLA"
            ),
        )
    elif kind == "entity":
        importer = import_module(PACKAGE + ".builders.bedrock_mcblend.importer")
        importer.import_client_entity(
            context,
            item["file_path"],
            display_name=name,
            pose_preset="DEFAULT_IDLE",
        )
    elif kind == "java":
        java_builder = import_module(PACKAGE + ".builders.java_builder")
        java_builder.build_java_model_from_file(
            context, item["file_path"], display_name=name,
            resource_roots=item.get("resource_roots"),
            vanilla_path=item.get("vanilla_path"),
        )
    else:
        importer = import_module(PACKAGE + ".builders.bedrock_mcblend.importer")
        # One worker renders one snapshot of the packs; read their folders once.
        importer.trust_pack_listings_for_batch()
        importer.build_bedrock_model_from_file(
            context,
            item["file_path"],
            geometry_identifier=item.get("identifier") or "",
            geometry_index=max(0, int(item.get("geometry_index", 0) or 0)),
            display_name=name,
        )


def render_mesh(item, scene, camera):
    """Import, frame, render to ``item['output']``, then clean the scene."""
    before = set(bpy.data.objects)
    try:
        import_mesh(item)
        created = [obj for obj in bpy.data.objects if obj not in before]
        if not created:
            print("[thumb] builder produced no objects for %s" % item["file_path"])
            return False

        # Anything parked in another collection would not render.
        linked = set(scene.collection.all_objects)
        for obj in created:
            if obj not in linked:
                try:
                    scene.collection.objects.link(obj)
                except RuntimeError:
                    pass

        visible_meshes = _visible_meshes(created)
        frame_camera(camera, visible_meshes or created)

        scene.render.filepath = item["output"]
        bpy.ops.render.render(write_still=True)
        return png_has_visible_pixels(item["output"])
    except Exception as exc:  # pragma: no cover - one bad asset must not kill the queue
        print("[thumb] mesh render failed for %s: %s" % (item["file_path"], exc))
        return False
    finally:
        clear_imported(before)


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
def _queue_path_from_argv():
    if "--" not in sys.argv:
        return ""
    trailing = sys.argv[sys.argv.index("--") + 1:]
    return trailing[0] if trailing else ""


def _normalise_geometry_index(value):
    """Return the record selector used by the parent thumbnail cache."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _fallback_output(item, size, cache_dir, render_schema_version=""):
    """Recompute the cache path if the queue omitted one (matches ThumbnailCache)."""
    try:
        mtime = os.path.getmtime(item["file_path"])
    except OSError:
        mtime = 0.0
    raw = "|".join((
        os.path.normcase(os.path.abspath(item["file_path"])),
        str(item.get("asset_type", "")),
        str(item.get("identifier", "")),
        str(_normalise_geometry_index(item.get("geometry_index", -1))),
        "%.6f" % mtime,
        str(size),
        str(render_schema_version or ""),
        str(item.get("dependency_fingerprint", "")),
    ))
    digest = hashlib.md5(raw.encode("utf-8", "ignore")).hexdigest()
    return os.path.join(cache_dir, digest + ".png")


def _failed_marker(output):
    """Failure-marker path for a job's output PNG (``<digest>`` + ``FAILED_SUFFIX``)."""
    return os.path.splitext(output)[0] + FAILED_SUFFIX


def _mark_failed(output, reason):
    """Record that this asset could not be rendered."""
    try:
        with open(_failed_marker(output), "w", encoding="utf-8") as stream:
            stream.write(str(reason))
    except OSError as exc:
        print("[thumb] could not write failure marker for %s: %s" % (output, exc))


def _clear_failed(output):
    """Drop a stale failure marker after a successful render."""
    marker = _failed_marker(output)
    if os.path.isfile(marker):
        try:
            os.remove(marker)
        except OSError:
            pass


def apply_settings(payload, scene):
    """Point the builders at the queue's packs; returns the resource roots.

    The worker skips add-on registration, so it defines the one Scene path
    property builders need for cross-file resource resolution.
    """
    resource_pack = payload.get("resource_pack") or ""
    roots = payload.get("resource_roots") or ([resource_pack] if resource_pack else [])
    if resource_pack:
        if not hasattr(bpy.types.Scene, "mcbedrock_resource_pack_path"):
            bpy.types.Scene.mcbedrock_resource_pack_path = bpy.props.StringProperty(
                subtype='DIR_PATH',
                options={"SKIP_SAVE"},
            )
        scene.mcbedrock_resource_pack_path = resource_pack
    scene["_mcbedrock_resource_roots"] = json.dumps(roots)
    if payload.get('browser_pack_only'):
        scene['_mcbedrock_asset_browser_root'] = resource_pack
    elif '_mcbedrock_asset_browser_root' in scene:
        del scene['_mcbedrock_asset_browser_root']
    return roots


def render_item(item, payload, scene, camera, roots, atomic=False):
    """Render one queue item to its PNG, else write a fallback and a failure marker.

    Returns True when rendered, False on a fallback, None when the source is
    gone. ``atomic`` renders beside the PNG and renames it into place, so a
    reader never sees half a file.
    """
    size = int(payload.get("size") or DEFAULT_SIZE)
    resource_pack = payload.get("resource_pack") or ""
    item["resource_roots"] = roots
    item["vanilla_path"] = payload.get("vanilla_path", "")
    if not item.get("file_path") or not os.path.isfile(item["file_path"]):
        return None
    if not item.get("output"):
        item["output"] = _fallback_output(
            item, size, payload.get("cache_dir") or "", payload.get("render_schema_version") or ""
        )
    output = item["output"]
    os.makedirs(os.path.dirname(output), exist_ok=True)
    target = dict(item, output=os.path.splitext(output)[0] + ".partial.png") if atomic else item

    if (item.get("asset_type") or "") == "Texture":
        # A large texture's card: a smaller copy, transparent pixels included.
        # Without one the browser shows the original, so no fallback bitmap.
        if render_texture(target, int(item.get("texture_size") or size)) and png_is_complete(target["output"]):
            if atomic:
                os.replace(target["output"], output)
            _clear_failed(output)
            print("[thumb] ok %s" % output)
            return True
        _mark_failed(output, item.get("file_path", ""))
        print("[thumb] FAILED texture %s" % item.get("file_path", ""))
        return False

    if (item.get("asset_type") or "").lower() == "particle":
        ok = render_particle(target, size, resource_pack)
    else:
        ok = render_mesh(target, scene, camera)

    if ok and png_has_visible_pixels(target["output"]):
        if atomic:
            os.replace(target["output"], output)
        _clear_failed(output)
        print("[thumb] ok %s" % output)
        return True
    fallback = write_deterministic_fallback(
        output,
        key="%s|%s|%s" % (
            item.get("asset_type", ""),
            item.get("identifier", ""),
            _normalise_geometry_index(item.get("geometry_index", -1)),
        ),
        kind=item.get("asset_type", "asset"),
        size=size,
    )
    # Preserve failure diagnostics while leaving a valid bitmap so a
    # failed source render never becomes a silent blank gallery card.
    _mark_failed(output, item.get("file_path", ""))
    print("[thumb] %s %s" % ("fallback" if fallback else "FAILED", item.get("file_path", "")))
    return False


def main():
    queue_path = _queue_path_from_argv()
    if "--pool" in sys.argv:
        return pool_main(queue_path)
    if not queue_path or not os.path.isfile(queue_path):
        print("[thumb] usage: blender -b -P thumbnail_worker.py -- queue.json")
        return 1

    payload = _read_json(queue_path)
    payload.setdefault("cache_dir", os.path.dirname(queue_path))
    items = payload.get("items") or []
    scene, camera = setup_scene(int(payload.get("size") or DEFAULT_SIZE))
    roots = apply_settings(payload, scene)

    rendered = 0
    failed = 0
    for item in items:
        time.sleep(.05)
        result = render_item(item, payload, scene, camera, roots)
        if result:
            rendered += 1
        elif result is False:
            failed += 1

    print("[thumb] rendered %d/%d (%d failed)" % (rendered, len(items), failed))
    return 0


# ----------------------------------------------------------------------
# Pool mode: stays up while the asset browser shows thumbnails
# ----------------------------------------------------------------------
POOL_IDLE_SECONDS = 120.0
STALE_CLAIM_SECONDS = 30.0  # a claim this old was left by a worker that stopped mid-item


def _watch_stdin(stop):
    """Blender exiting closes the worker's stdin; so does the add-on, to stop it."""
    try:
        while sys.stdin.buffer.read(4096):
            pass
    except (AttributeError, OSError, ValueError):
        pass
    stop.set()


def _claim(items, finished):
    """The first item with no PNG, failure marker or other worker on it."""
    for item in items:
        output = item.get("output") or ""
        if not output or output in finished:
            continue
        base = os.path.splitext(output)[0]
        if os.path.isfile(output) or os.path.isfile(_failed_marker(output)):
            finished.add(output)
            continue
        try:
            handle = os.open(base + ".claim", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(handle, str(os.getpid()).encode("ascii"))  # whose: a crash marks only this item
            os.close(handle)
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(base + ".claim") > STALE_CLAIM_SECONDS:
                    os.remove(base + ".claim")
            except OSError:
                pass
            continue
        except OSError:
            continue
        if os.path.isfile(output):
            os.remove(base + ".claim")
            finished.add(output)
            continue
        return item
    return None


def pool_main(control_path):
    """Render what the queue file asks for, first things first, until told to stop.

    ``control.json`` names the queue file (a ``write_queue`` payload the
    browser rewrites as cards come into view, the visible ones first) and an
    error file. Between items the worker reads the queue again, so new cards
    jump the line. It claims each item with a ``.claim`` file beside its PNG,
    so several workers share one queue. It quits when Blender exits (or the
    add-on closes its stdin), when the queue file is gone, or after two
    minutes with nothing to render.
    """
    import threading

    control = _read_json(control_path)
    stop = threading.Event()
    threading.Thread(target=_watch_stdin, args=(stop,), daemon=True).start()
    importer = import_module(PACKAGE + ".builders.bedrock_mcblend.importer")
    scene = camera = None
    size = settings = None
    roots = []
    finished = set()
    idle_since = time.monotonic()
    while not stop.is_set():
        try:
            payload = _read_json(control["order"])
        except FileNotFoundError:
            return 0
        except (OSError, ValueError):
            time.sleep(0.02)  # being replaced: read it again
            continue
        wanted = int(payload.get("size") or DEFAULT_SIZE)
        if wanted != size:
            scene, camera = setup_scene(wanted)
            if size is None:
                bpy.ops.render.render()  # compiles EEVEE's shaders once, before the first card
            size, settings = wanted, None
        signature = json.dumps([payload.get(key) for key in (
            "resource_pack", "resource_roots", "vanilla_path", "browser_pack_only", "cache_dir")])
        if signature != settings:
            roots = apply_settings(payload, scene)
            importer._BATCH_LISTINGS = None  # another pack snapshot: read its folders again
            settings = signature
        item = _claim(payload.get("items") or [], finished)
        if item is None:
            if time.monotonic() - idle_since > POOL_IDLE_SECONDS:
                return 0
            time.sleep(0.05)
            continue
        claim = os.path.splitext(item["output"])[0] + ".claim"
        try:
            render_item(item, payload, scene, camera, roots, atomic=True)
        finally:
            finished.add(item["output"])
            try:
                os.remove(claim)
            except OSError:
                pass
        idle_since = time.monotonic()
    return 0


if __name__ == "__main__":
    if "--pool" in sys.argv:
        try:
            sys.exit(main())
        except Exception as exc:  # the browser reads this and stops starting workers
            control = _read_json(_queue_path_from_argv())
            with open(control["error"], "w", encoding="utf-8") as stream:
                import traceback
                stream.write("%s: %s\n%s" % (type(exc).__name__, exc, traceback.format_exc()))
            sys.exit(1)
    sys.exit(main())
