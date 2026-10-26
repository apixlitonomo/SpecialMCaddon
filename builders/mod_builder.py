"""Blender construction for models read from a mod JAR (File > Import)."""

from __future__ import annotations

import json
import os

from .bedrock_mcblend.geometry import add_bedrock_model_layer, build_bedrock_model
from ..importers.archive_extractor import get_cache_dir
from ..importers.bedrock_assets import asset_resource_roots, load_json_file, safe_name
from ..importers.mod_jar import extract_mod_assets, resolve_resource_file
from ..importers.mod_models import (
    geometry_from_model,
    load_entity_model,
    load_layer_model,
    vanilla_model_library,
)

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Render types drawn with blending; the rest are cutout.
_BLENDED_RENDER_TYPES = ("entityTranslucent", "entityTranslucentCull", "itemEntityTranslucentCull",
                         "entityTranslucentEmissive", "eyes", "breezeEyes", "energySwirl",
                         "geckolibGlowmask")
# Textures named like a skin's glow (GeckoLib's _glowmask, and the common
# _glow / _eyes / _emissive), for geo files no renderer could be run for.
_GLOW_SUFFIXES = ("_glowmask", "_glow", "_eyes", "_emissive")


def mod_asset_root(jar_path):
    """The JAR's extracted resource files (cached by path, size and time)."""
    return extract_mod_assets(jar_path, get_cache_dir(_ADDON_DIR))


def _lookup_roots(context, mod_root):
    # Active resource packs override mod assets, and both override vanilla's
    # (mods dress models in vanilla textures too: a stray's skin), as in the game.
    from ..importers.vanilla_assets import addon_cache_dir, resolve_vanilla_root

    roots = [root for root in asset_resource_roots(context) if root]
    roots.append(mod_root)
    try:
        vanilla_root, _note = resolve_vanilla_root("", addon_cache_dir())
    except Exception:  # no or broken client JAR: vanilla textures stay missing
        vanilla_root = ""
    if vanilla_root and vanilla_root not in roots:
        roots.append(vanilla_root)
    return roots


def layer_key(layer):
    """How the import dialog names a render layer: its class and texture."""
    return f"{layer['layer']}|{layer['texture']}"


def _chosen_layers(entity_layers, optional_layers, chosen):
    """``chosen=None``: what a default entity shows; ``"*"``: every layer; else
    the layers whose keys are in it."""
    if chosen is None:
        return list(entity_layers)
    if chosen == "*":
        return list(entity_layers) + list(optional_layers)
    return [layer for layer in list(entity_layers) + list(optional_layers) if layer_key(layer) in chosen]


def _resolve_layers(roots, layers, warnings, texture_size=None):
    resolved = []
    for layer in layers:
        path = resolve_resource_file(roots, layer["texture"])
        if not path:
            warnings.append(f"{layer['name']}: texture {layer['texture']} was not found")
            continue
        if texture_size:
            path = _first_frame(path, *texture_size)
        resolved.append(dict(layer, path=path))
    return resolved


# Java Edition's entity cutout discards texels under alpha 0.1 (Bedrock: 0.5).
JAVA_CUTOUT_ALPHA = 0.1


def _material_stack(base_path, overlays, base_hidden=()):
    """Base texture under the layers drawn over it, in draw order (later on top)."""
    stack = [{"texture_paths": [base_path], "render_method": "alpha_test",
              "material": {"name": "base", "cutout_threshold": JAVA_CUTOUT_ALPHA},
              "hidden_parts": list(base_hidden)}]
    for layer in overlays:
        blended = layer["render_type"] in _BLENDED_RENDER_TYPES
        material = {"surface_mode": "blend" if blended else "cutout", "name": layer["name"],
                    "cutout_threshold": JAVA_CUTOUT_ALPHA}
        if layer["glow"]:
            material.update({"lighting": "unlit", "ignore_lighting": True})
        hidden = [] if layer.get("bones") is None else ["*"] + ["!" + bone for bone in layer["bones"]]
        stack.append({"texture_paths": [layer["path"]],
                      "render_method": "blend" if blended else "alpha_test",
                      "material": material, "hidden_parts": hidden})
    return stack


def _apply_layers(result, base_path, overlays, interpolation, base_hidden=()):
    """The skin as a Java cutout, and layers on the same geometry as passes
    mixed over it in the shader: one mesh, no z-fighting, no depth sorting."""
    if not base_path:
        return
    from .bedrock_mcblend.importer import _apply_entry_material

    _apply_entry_material(
        result["mesh"], [base_path], interpolation, "alpha_test",
        material_layers=_material_stack(base_path, overlays, base_hidden),
        backface_culling=False,  # Java entity render types never cull
        surface_override="cutout",
    )


def _first_frame(path, texture_width, texture_height):
    """An animated skin (a strip of frames with a ``.png.mcmeta``) as its first frame.

    The geometry's UVs cover one frame; the game shows the strip a frame at a
    time. The frame is written once to the cache.
    """
    if not path or not os.path.isfile(path) or not os.path.isfile(path + ".mcmeta"):
        return path
    import bpy
    import numpy

    try:
        image = bpy.data.images.load(path, check_existing=True)
    except RuntimeError:
        return path
    width, height = int(image.size[0]), int(image.size[1])
    frame = int(round(width * float(texture_height) / float(texture_width or 1)))
    if frame <= 0 or height <= frame or height % frame:
        return path
    out_dir = os.path.join(get_cache_dir(_ADDON_DIR), "mod_texture_frames")
    os.makedirs(out_dir, exist_ok=True)
    stamp = f"{os.path.getsize(path)}_{int(os.path.getmtime(path))}"
    stem = safe_name(os.path.splitext(os.path.basename(path))[0])
    out = os.path.join(out_dir, f"{stem}_{stamp}.png")
    if not os.path.isfile(out):
        channels = int(getattr(image, "channels", 4) or 4)
        pixels = numpy.empty(width * height * channels, dtype=numpy.float32)
        image.pixels.foreach_get(pixels)
        # Blender keeps rows bottom-up: the first (top) frame is the last rows.
        top = pixels.reshape(height, width, channels)[height - frame:]
        rgba = numpy.ones((frame, width, 4), dtype=numpy.float32)
        rgba[..., :min(channels, 4)] = top[..., :min(channels, 4)]
        cropped = bpy.data.images.new(f"frame_{stem}", width, frame, alpha=True)
        cropped.pixels.foreach_set(rgba.ravel())
        cropped.filepath_raw = out
        cropped.file_format = "PNG"
        cropped.save()
        bpy.data.images.remove(cropped)
    return out


def _add_layer_models(context, result, layers, interpolation, identifier, warnings):
    """Layers that draw another model (a saddle, an armor shell) as meshes on the same rig."""
    for index, layer in enumerate(layers):
        geometry, layer_warnings = geometry_from_model(
            layer["model"], identifier=f"geometry.{identifier}_layer{index}")
        warnings.extend(layer_warnings)
        try:
            add_bedrock_model_layer(
                context, result["armature"], result["collection"], geometry,
                texture_path=layer["path"], interpolation=interpolation,
                object_suffix=f"_{safe_name(layer['name'])}",
                render_method="blend" if layer["render_type"] in _BLENDED_RENDER_TYPES else "alpha_test",
                solid_backface_culling=False,
            )
        except (RuntimeError, ValueError, KeyError) as exc:
            warnings.append(f"{layer['name']}: {exc}")


def _finish(result, armature, jar_path, asset, model_class, renderer_class, texture_location,
            texture_path, layers, warnings):
    armature["mcbedrock_asset_type"] = "mod_entity"
    armature["mcbedrock_source_path"] = os.path.abspath(jar_path)
    armature["mcbedrock_mod_identifier"] = asset.identifier
    armature["mcbedrock_mod_model_class"] = model_class
    armature["mcbedrock_mod_renderer_class"] = renderer_class
    armature["mcbedrock_mod_texture"] = texture_location
    armature["mcbedrock_mod_layers"] = json.dumps([layer_key(layer) for layer in layers])
    if warnings:
        armature["mcbedrock_mod_warnings"] = json.dumps(warnings)
    result["warnings"] = warnings
    result["texture_path"] = texture_path
    return result


def import_mod_entity(context, jar_path, asset, texture="", interpolation="Closest",
                      display_name="", use_render_scale=True, layers=None):
    """Build a mod mob (or other code model) as a rigged Bedrock-style model.

    ``layers``: keys (``layer_key``) of the render layers to draw; ``None``
    draws what a default entity shows (glowing eyes, but no saddle).
    """
    if asset.geckolib:
        return import_mod_geo(context, jar_path, asset, texture=texture, interpolation=interpolation,
                              display_name=display_name, use_render_scale=use_render_scale,
                              layers=layers)
    if asset.layer_method:
        # Models built on vanilla meshes (HumanoidModel.createMesh) run
        # vanilla's own factory from an installed 26.1+ client JAR.
        entity = load_layer_model(jar_path, asset.model_class, asset.layer_method,
                                  library=vanilla_model_library)
    else:
        entity = load_entity_model(
            jar_path, renderer_class=asset.renderer_class, model_class=asset.model_class,
            library=vanilla_model_library)  # opened only if the model calls vanilla code
    warnings = []
    mod_root = mod_asset_root(jar_path)
    roots = _lookup_roots(context, mod_root)
    chosen = _resolve_layers(roots, _chosen_layers(entity.layers, entity.optional_layers, layers), warnings,
                             entity.model.texture_size)
    same_model = [layer for layer in chosen if layer["model"] is None]
    other_models = [layer for layer in chosen if layer["model"] is not None]
    # Parts only a chosen layer draws (a saddle the default draw hides) are built too,
    # under the layer's texture only.
    shown = {part.name for part in entity.model.iter_parts() if part.visible}
    extra = set()
    for layer in same_model:
        for part in entity.model.iter_parts():
            if layer["bones"] is not None and part.name in layer["bones"] and not part.visible:
                part.visible = True
                extra.add(part.name)
    name = display_name or asset.name
    identifier = safe_name(asset.identifier.split(":")[-1])
    geometry, model_warnings = geometry_from_model(
        entity.model, identifier="geometry." + identifier, scale=entity.render_scale)
    warnings.extend(model_warnings)
    texture_location = texture or entity.texture or (asset.textures[0] if asset.textures else "")
    texture_path = resolve_resource_file(roots, texture_location) if texture_location else ""
    if not texture_path:
        warnings.append(f"texture {texture_location or '(none)'} was not found")
    texture_path = _first_frame(texture_path, *entity.model.texture_size)
    geometry["_mcbedrock_visibility_alpha"] = JAVA_CUTOUT_ALPHA
    if same_model and texture_path:
        geometry["_mcbedrock_visibility_textures"] = [texture_path] + [layer["path"] for layer in same_model]
    result = build_bedrock_model(
        context,
        geometry,
        texture_path=texture_path,
        texture_paths=[texture_path] if texture_path else [],
        source_path=os.path.abspath(jar_path),
        interpolation=interpolation,
        display_name=name,
        render_method="alpha_test",
        # Java entity render types draw without culling.
        solid_backface_culling=False,
    )
    _apply_layers(result, texture_path, same_model, interpolation, sorted(extra - shown))
    _add_layer_models(context, result, other_models, interpolation, identifier, warnings)
    armature = result["armature"]
    if use_render_scale and entity.render_scale != (1.0, 1.0, 1.0):
        # The renderer's own scale(), applied about the feet like the game.
        armature.scale = entity.render_scale
    return _finish(result, armature, jar_path, asset, entity.model_class, entity.renderer_class,
                   texture_location, texture_path, chosen, warnings)


def geo_layers(jar_path, asset):
    """``(default, optional)`` layer records of a GeckoLib asset's renderer."""
    from ..importers.mod_geckolib import load_geo_renderer
    from ..importers.mod_models import ModModelError, layer_passes

    if not asset.renderer_class:
        return None, [], []
    try:
        result = load_geo_renderer(jar_path, asset.renderer_class)
    except ModModelError:
        return None, [], []
    return (result,
            layer_passes(None, result.layers, "geo", None),
            layer_passes(None, result.optional_layers, "geo", None, optional=True))


def _conventional_glow(roots, texture_location):
    """A ``<skin>_glow.png``-style texture beside the skin, as a glow layer."""
    namespace, _separator, path = texture_location.partition(":")
    stem, dot, extension = path.rpartition(".")
    for suffix in _GLOW_SUFFIXES:
        location = f"{namespace}:{stem}{suffix}.{extension}" if dot else ""
        if location and resolve_resource_file(roots, location):
            return [{"layer": "", "name": "Glow", "texture": location,
                     "render_type": "eyes", "glow": True, "bones": None, "model": None,
                     "optional": False}]
    return []


def import_mod_geo(context, jar_path, asset, texture="", interpolation="Closest",
                   display_name="", use_render_scale=True, layers=None):
    """Build a GeckoLib model: its geo.json, the texture its renderer picks, the
    renderer's layers (glowmasks, eyes) and every animation in its animation file."""
    from .bedrock_mcblend.animation import apply_bedrock_animations
    from ..importers.mod_geckolib import geo_geometry

    warnings = []
    mod_root = mod_asset_root(jar_path)
    roots = _lookup_roots(context, mod_root)
    renderer, default_layers, optional_layers = geo_layers(jar_path, asset)
    texture_location = texture or (asset.textures[0] if asset.textures else "")
    chosen = _chosen_layers(default_layers, optional_layers, layers)
    if renderer is None and not asset.renderer_class and texture_location:
        # No renderer to ask: a glow texture named after the skin glows.
        chosen += _conventional_glow(roots, texture_location)
    geo_file = os.path.join(mod_root, *asset.model_path.split("/"))
    identifier = safe_name(asset.identifier.split(":")[-1])
    geometry = geo_geometry(load_json_file(geo_file), identifier="geometry." + identifier)
    description = geometry.get("description", {})
    texture_size = (float(description.get("texture_width", 64) or 64),
                    float(description.get("texture_height", 64) or 64))
    chosen = _resolve_layers(roots, chosen, warnings, texture_size)
    texture_path = resolve_resource_file(roots, texture_location) if texture_location else ""
    if not texture_path:
        warnings.append(f"texture {texture_location or '(none)'} was not found")
    texture_path = _first_frame(texture_path, *texture_size)
    geometry["_mcbedrock_visibility_alpha"] = JAVA_CUTOUT_ALPHA
    if chosen and texture_path:
        geometry["_mcbedrock_visibility_textures"] = [texture_path] + [layer["path"] for layer in chosen]
    result = build_bedrock_model(
        context,
        geometry,
        texture_path=texture_path,
        texture_paths=[texture_path] if texture_path else [],
        source_path=os.path.abspath(jar_path),
        interpolation=interpolation,
        display_name=display_name or asset.name,
        render_method="alpha_test",
        # GeckoLib draws with entityCutoutNoCull.
        solid_backface_culling=False,
    )
    _apply_layers(result, texture_path, chosen, interpolation)
    armature = result["armature"]
    scale = renderer.scale if renderer is not None else (1.0, 1.0, 1.0)
    if use_render_scale and scale != (1.0, 1.0, 1.0):
        armature.scale = scale
    if asset.animation_path:
        animation_file = os.path.join(mod_root, *asset.animation_path.split("/"))
        try:
            apply_bedrock_animations(context, armature, animation_file, animation_filter="__ALL__",
                                     append_nla=True)
        except (ValueError, OSError, KeyError) as exc:
            warnings.append(f"animations: {exc}")
    return _finish(result, armature, jar_path, asset, asset.model_path, asset.renderer_class,
                   texture_location, texture_path, chosen, warnings)


def import_mod_json_model(context, jar_path, asset, interpolation="Closest", display_name=""):
    """Build a mod block or item JSON model with the Java model builder."""
    from .java_builder import build_java_model_from_file

    mod_root = mod_asset_root(jar_path)
    filepath = os.path.join(mod_root, *asset.model_path.split("/"))
    if not os.path.isfile(filepath):
        raise FileNotFoundError(asset.model_path)
    return build_java_model_from_file(
        context,
        filepath,
        interpolation=interpolation,
        display_name=display_name or asset.name,
    )
