"""GeckoLib models (``.geo.json``) in a mod JAR.

GeckoLib mods draw mobs, blocks and items from Bedrock-format geometry files
(``assets/<ns>/geo/**.geo.json``) and Bedrock-format animations. Which files
go together is decided in code: a mob's ``GeoEntityRenderer`` is built with a
``GeoModel`` whose ``getModelResource``, ``getTextureResource`` and
``getAnimationResource`` return them, and its render layers draw glowmasks
and other textures over the model. ``java_vm`` runs that code against Python
versions of GeckoLib's base classes, written from GeckoLib 4's bytecode, so
the importer reads the same files the game draws for a default entity.

GeckoLib draws geometry the way Bedrock does (box-UV sizes are floored) but
with Java's ``entityCutoutNoCull``: no culling, and of a flat cube's two caps
the one drawn later (east, down, south) shows, as for Java entity models.
Nothing here imports Blender.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .java_classes import method_argument_types, method_return_type
from .java_vm import NOT_HANDLED, EagerOpaque, JavaThrow, JObject, Opaque, VMError, opaque_default

GECKO = "software/bernie/geckolib/"
GEO_MODEL = GECKO + "model/GeoModel"
DEFAULTED_MODEL = GECKO + "model/DefaultedGeoModel"
DEFAULTED_SUBTYPES = {
    GECKO + "model/DefaultedEntityGeoModel": "entity",
    GECKO + "model/DefaultedBlockGeoModel": "block",
    GECKO + "model/DefaultedItemGeoModel": "item",
}
GEO_RENDERER = GECKO + "renderer/GeoRenderer"
GEO_LAYER = GECKO + "renderer/layer/GeoRenderLayer"
AUTO_GLOW_LAYER = GECKO + "renderer/layer/AutoGlowingGeoLayer"
AUTO_GLOW_TEXTURE = GECKO + "cache/texture/AutoGlowingTexture"
GEO_ANIMATABLE = GECKO + "animatable/GeoAnimatable"
BAKED_PREFIX = GECKO + "cache/object/"

# GeckoLib 3 (Minecraft 1.16 to 1.19): the same roles under other names.
GECKO3 = "software/bernie/geckolib3/"
GECKO3_MODELS = (GECKO3 + "model/AnimatedGeoModel", GECKO3 + "model/AnimatedTickingGeoModel")
GECKO3_LAYER = GECKO3 + "renderers/geo/GeoLayerRenderer"

ENTITY_RENDERERS = (
    GECKO + "renderer/GeoEntityRenderer",
    GECKO + "renderer/GeoReplacedEntityRenderer",
    GECKO3 + "renderers/geo/GeoEntityRenderer",
    GECKO3 + "renderers/geo/GeoReplacedEntityRenderer",
    GECKO3 + "renderers/geo/GeoProjectilesRenderer",
)
OTHER_RENDERERS = (
    GECKO + "renderer/GeoBlockRenderer",
    GECKO + "renderer/GeoItemRenderer",
    GECKO + "renderer/GeoArmorRenderer",
    GECKO + "renderer/GeoObjectRenderer",
    GECKO3 + "renderers/geo/GeoBlockRenderer",
    GECKO3 + "renderers/geo/GeoItemRenderer",
    GECKO3 + "renderers/geo/GeoArmorRenderer",
)
GEO_RENDERERS = ENTITY_RENDERERS + OTHER_RENDERERS
MODEL_BASES = (GEO_MODEL, DEFAULTED_MODEL, *DEFAULTED_SUBTYPES, *GECKO3_MODELS)

# GeoModel methods by role: GeckoLib 3.1+ and 4 names first, then 3.0's.
RESOURCE_METHODS = {
    "model": ("getModelResource", "getModelLocation"),
    "texture": ("getTextureResource", "getTextureLocation"),
    "animation": ("getAnimationResource", "getAnimationFileLocation"),
}
_ROLE_BY_NAME = {name: role for role, names in RESOURCE_METHODS.items() for name in names}
_DEFAULTED_PATHS = {
    "model": "geo/{subtype}/{path}.geo.json",
    "texture": "textures/{subtype}/{path}.png",
    "animation": "animations/{subtype}/{path}.animation.json",
}


def _rl(namespace, path):
    from .mod_models import RESOURCE_LOCATION, _set_resource_location

    return _set_resource_location(JObject(RESOURCE_LOCATION), namespace, path)


def _rl_text(value):
    from .mod_models import resource_location_text

    return resource_location_text(value)


def append_to_path(location, suffix):
    """``AutoGlowingTexture.appendToPath``: ``a/b.png`` + ``_glowmask`` -> ``a/b_glowmask.png``."""
    text = _rl_text(location)
    if not text:
        return None
    namespace, path = text.split(":", 1)
    stem, dot, extension = path.rpartition(".")
    return _rl(namespace, f"{stem}{suffix}.{extension}" if dot else path + suffix)


def _model_of(vm, renderer):
    native = renderer.native if isinstance(renderer, JObject) and isinstance(renderer.native, dict) else {}
    return native.get("model")


def call_resource(vm, model, role, animatable, renderer=None):
    """Ask a GeoModel for its model, texture or animation location (text, or ``""``)."""
    if not isinstance(model, JObject):
        return ""
    for name in RESOURCE_METHODS[role]:
        desc = _resource_desc(vm, model.class_name, name)
        if desc is None:
            continue
        arguments = [animatable if index == 0 else renderer
                     for index, _type in enumerate(method_argument_types(desc))]
        try:
            text = _rl_text(vm.invoke_virtual(model.class_name, name, desc, model, arguments))
        except (JavaThrow, VMError):
            continue
        if text:
            return text
    return ""


def _resource_desc(vm, class_name, name):
    """The descriptor to call ``name`` with: the JAR's bridge, its own method, or the host's."""
    found = []
    for owner in vm.class_path.superclasses(class_name):
        parsed = vm.class_path.get(owner)
        for method in (parsed.methods if parsed is not None else ()):
            if (method.name == name and not method.is_static
                    and method_return_type(method.desc).startswith("Lnet/minecraft/resources/")
                    and 1 <= len(method_argument_types(method.desc)) <= 2):
                found.append(method.desc)
    if found:
        # The bridge (GeoAnimatable / Object) accepts any stand-in; prefer one argument.
        return min(found, key=lambda desc: (len(method_argument_types(desc)),
                                            GEO_ANIMATABLE not in desc and "java/lang/Object" not in desc))
    chain = vm.ancestors(class_name)
    if any(base in chain for base in MODEL_BASES):
        return f"(L{GEO_ANIMATABLE};)Lnet/minecraft/resources/ResourceLocation;"
    return None


def _record_geo_draw(vm, render_type, consumer=None):
    """A layer re-rendering the GeckoLib model (all of its bones).

    The buffer decides how the game draws the vertices (a layer may pass the
    renderer's own render type along with ``getBuffer(RenderType.eyes(...))``),
    so its render type wins over the ``renderType`` argument.
    """
    from .mod_models import _consumer, begin_draw, end_draw

    native = consumer.native if isinstance(consumer, JObject) and isinstance(consumer.native, dict) else {}
    if not isinstance(native.get("render_type"), JObject) and isinstance(render_type, JObject):
        consumer = _consumer(render_type)
    end_draw(vm, begin_draw(vm, "geo", consumer))


def install_geckolib(vm):
    from .mod_models import ENTITY_RENDERER, RENDER_TYPE, VERTEX_CONSUMER, render_type_object

    vm.host_class(GEO_MODEL)
    for name in GECKO3_MODELS:
        vm.host_class(name)

    @vm.dispatcher(GEO_MODEL)
    def geo_model_method(vm, this, name, args, desc):
        role = _ROLE_BY_NAME.get(name)
        if role and len(args) == 2:  # (animatable, renderer) defaults to (animatable)
            text = call_resource(vm, this, role, args[0])
            return _rl(*text.split(":", 1)) if text else None
        return NOT_HANDLED

    vm.method(GEO_MODEL, None)(lambda vm, this, args, desc: opaque_default(method_return_type(desc)))

    # DefaultedGeoModel: geo/<subtype>/<path>.geo.json and its texture and animations.
    vm.host_class(DEFAULTED_MODEL, parent=GEO_MODEL)

    def set_path(vm, this, role, base):
        if not _rl_text(base):
            return
        subtype = vm.invoke_virtual(this.class_name, "subtype", "()Ljava/lang/String;", this, [])
        namespace, path = _rl_text(base).split(":", 1)
        state = this.native if isinstance(this.native, dict) else {}
        state[role] = _rl(namespace, _DEFAULTED_PATHS[role].format(subtype=subtype or "entity", path=path))
        this.native = state

    @vm.method(DEFAULTED_MODEL, "<init>")
    def defaulted_init(vm, this, args, desc):
        base = next((value for value in args if _rl_text(value)), None)
        this.native = {}
        for role in _DEFAULTED_PATHS:
            set_path(vm, this, role, base)

    for subtype_class, subtype in DEFAULTED_SUBTYPES.items():
        vm.host_class(subtype_class, parent=DEFAULTED_MODEL)
        vm.method(subtype_class, "<init>")(defaulted_init)
        vm.method(subtype_class, "subtype")(lambda vm, this, args, desc, subtype=subtype: subtype)
    vm.method(DEFAULTED_MODEL, "subtype")(lambda vm, this, args, desc: "entity")
    for role, name in (("model", "withAltModel"), ("texture", "withAltTexture"),
                       ("animation", "withAltAnimations")):
        @vm.method(DEFAULTED_MODEL, name)
        def with_alt(vm, this, args, desc, role=role):
            set_path(vm, this, role, args[0] if args else None)
            return this
    for name, role in _ROLE_BY_NAME.items():
        vm.method(DEFAULTED_MODEL, name)(
            lambda vm, this, args, desc, role=role:
            (this.native or {}).get(role) if isinstance(this.native, dict) else None)

    # Renderers keep their GeoModel, scale and layers.
    def renderer_init(vm, this, args, desc):
        model = next((value for value in args if isinstance(value, JObject)
                      and any(base in vm.ancestors(value.class_name) for base in MODEL_BASES)), None)
        this.native = {"model": model, "layers": [], "scale": [1.0, 1.0], "shadow": 0.0}

    def add_layer(vm, this, args, desc):
        if isinstance(this.native, dict):
            this.native["layers"].append(args[0])
        return this

    def with_scale(vm, this, args, desc):
        values = [float(value) for value in args]
        if isinstance(this.native, dict) and values:
            this.native["scale"] = [values[0], values[-1]]
        return this

    def texture_location(vm, this, args, desc):
        text = call_resource(vm, _model_of(vm, this), "texture", args[0] if args else Opaque(), this)
        return _rl(*text.split(":", 1)) if text else None

    def render_type(vm, this, args, desc):
        texture = next((value for value in args if _rl_text(value)), None)
        return render_type_object("entityCutoutNoCull", _rl_text(texture))

    def actually_render(vm, this, args, desc):
        render_type = next((value for value in args if isinstance(value, JObject)
                            and value.class_name == RENDER_TYPE), None)
        consumer = next((value for value in args if isinstance(value, JObject)
                         and value.class_name == VERTEX_CONSUMER), None)
        _record_geo_draw(vm, render_type, consumer)

    for name in GEO_RENDERERS:
        is_entity = name in ENTITY_RENDERERS
        vm.host_class(name, parent=ENTITY_RENDERER if is_entity else "java/lang/Object",
                      interfaces=(GEO_RENDERER,))
        vm.method(name, "<init>")(renderer_init)
        vm.method(name, "addRenderLayer")(add_layer)
        vm.method(name, "addLayer")(add_layer)  # GeckoLib 3
        vm.method(name, "withScale")(with_scale)
        vm.method(name, "getGeoModel")(lambda vm, this, args, desc: _model_of(vm, this))
        vm.method(name, "getGeoModelProvider")(lambda vm, this, args, desc: _model_of(vm, this))
        vm.method(name, "getTextureLocation")(texture_location)
        vm.method(name, "getTextureResource")(texture_location)
        vm.method(name, "getRenderType")(render_type)
        vm.method(name, "actuallyRender")(actually_render)
        vm.method(name, "reRender")(actually_render)
        vm.method(name, None)(lambda vm, this, args, desc: opaque_default(method_return_type(desc)))
    vm.host_class(GEO_RENDERER)
    vm.method(GEO_RENDERER, "actuallyRender")(actually_render)
    vm.method(GEO_RENDERER, "reRender")(actually_render)

    # Render layers.
    for name in (GEO_LAYER, GECKO3_LAYER):
        vm.host_class(name)

        @vm.method(name, "<init>")
        def layer_init(vm, this, args, desc):
            this.native = {"renderer": args[0] if args else None}

        vm.method(name, "getRenderer")(
            lambda vm, this, args, desc: (this.native or {}).get("renderer") if isinstance(this.native, dict) else None)

        @vm.method(name, "getGeoModel")
        def layer_model(vm, this, args, desc):
            renderer = (this.native or {}).get("renderer") if isinstance(this.native, dict) else None
            return _model_of(vm, renderer)

        @vm.method(name, "getTextureResource")
        def layer_texture(vm, this, args, desc):
            renderer = (this.native or {}).get("renderer") if isinstance(this.native, dict) else None
            if not isinstance(renderer, JObject):
                return None
            return texture_location(vm, renderer, args[:1], desc)

        vm.method(name, "getEntityTexture")(layer_texture)  # GeckoLib 3
        vm.method(name, None)(lambda vm, this, args, desc: opaque_default(method_return_type(desc)))

    vm.host_class(AUTO_GLOW_LAYER, parent=GEO_LAYER)

    @vm.method(AUTO_GLOW_LAYER, "render")
    def auto_glow_render(vm, this, args, desc):
        animatable = next((value for value in args if isinstance(value, Opaque)), Opaque())
        texture = vm.invoke_virtual(this.class_name, "getTextureResource",
                                    f"(L{GEO_ANIMATABLE};)Lnet/minecraft/resources/ResourceLocation;",
                                    this, [animatable])
        glow = append_to_path(texture, "_glowmask")
        _record_geo_draw(vm, render_type_object("geckolibGlowmask", _rl_text(glow)))

    vm.host_class(AUTO_GLOW_TEXTURE)
    vm.static(AUTO_GLOW_TEXTURE, "appendToPath")(
        lambda vm, args, desc: append_to_path(args[0], str(args[1] or "")))
    vm.static(AUTO_GLOW_TEXTURE, "getEmissiveResource")(
        lambda vm, args, desc: append_to_path(args[0], "_glowmask"))
    for name in ("getRenderType", "getOutlineRenderType"):
        vm.static(AUTO_GLOW_TEXTURE, name)(
            lambda vm, args, desc: render_type_object(
                "geckolibGlowmask", _rl_text(append_to_path(args[0], "_glowmask"))))
    vm.static(AUTO_GLOW_TEXTURE, None)(lambda vm, args, desc: opaque_default(method_return_type(desc)))


# -- reading a renderer ---------------------------------------------------------

def is_geckolib_renderer(class_path, name):
    chain = class_path.superclasses(name)
    if not chain or chain[-1] not in GEO_RENDERERS or name in GEO_RENDERERS:
        return False
    parsed = class_path.get(name)
    return parsed is not None and not parsed.access & 0x0400  # not abstract


def is_geckolib_entity_renderer(class_path, name):
    chain = class_path.superclasses(name)
    return bool(chain) and chain[-1] in ENTITY_RENDERERS


@dataclass
class GeoRendererResult:
    renderer_class: str
    model_path: str = ""      # namespace:geo/....geo.json
    texture: str = ""         # namespace:textures/....png
    animation_path: str = ""  # namespace:animations/....animation.json
    scale: tuple = (1.0, 1.0, 1.0)
    shadow: float = 0.0
    layers: list = field(default_factory=list)          # LayerDraws a default entity shows
    optional_layers: list = field(default_factory=list)  # LayerDraws only an "eager" entity shows
    notes: list = field(default_factory=list)


def _layer_arguments(vm, desc, entity, base_type, buffers, pose):
    from .mod_models import MULTI_BUFFER_SOURCE, POSE_STACK, RENDER_TYPE, VERTEX_CONSUMER, _consumer

    values = []
    for type_desc in method_argument_types(desc):
        if type_desc == f"L{POSE_STACK};":
            values.append(pose)
        elif type_desc == f"L{MULTI_BUFFER_SOURCE};":
            values.append(buffers)
        elif type_desc == f"L{RENDER_TYPE};":
            values.append(base_type)
        elif type_desc == f"L{VERTEX_CONSUMER};":
            values.append(_consumer(base_type))
        elif type_desc in ("F", "D"):
            values.append(0.0)
        elif type_desc in ("I", "J", "S", "B", "C"):
            values.append(0xF000F0)
        elif type_desc == "Z":
            values.append(0)
        elif type_desc.startswith("L" + BAKED_PREFIX):
            values.append(Opaque(type_desc[1:-1]))
        else:
            values.append(entity)
    return values


def _layer_render_desc(vm, layer):
    from .mod_models import POSE_STACK

    for owner in vm.class_path.superclasses(layer.class_name):
        parsed = vm.class_path.get(owner)
        for method in (parsed.methods if parsed is not None else ()):
            if method.name == "render" and method.desc.startswith(f"(L{POSE_STACK};") \
                    and not method.is_static:
                return method.desc
    return (f"(L{POSE_STACK};L{GEO_ANIMATABLE};L{BAKED_PREFIX}BakedGeoModel;"
            "Lnet/minecraft/client/renderer/RenderType;Lnet/minecraft/client/renderer/MultiBufferSource;"
            "Lcom/mojang/blaze3d/vertex/VertexConsumer;FII)V")


def run_geo_layers(vm, renderer, entity, base_type):
    """Run the renderer's GeckoLib layers once, returning the draws they recorded."""
    from .mod_models import MULTI_BUFFER_SOURCE, _new_pose

    draws = []
    vm.layer_draws = draws
    try:
        for layer in list(renderer.native.get("layers", ())):
            if not isinstance(layer, JObject):
                continue
            vm.current_layer = layer.class_name
            desc = _layer_render_desc(vm, layer)
            try:
                vm.invoke_virtual(layer.class_name, "render", desc, layer,
                                  _layer_arguments(vm, desc, entity, base_type,
                                                   JObject(MULTI_BUFFER_SOURCE), _new_pose()))
            except (JavaThrow, VMError) as exc:
                vm.notes[(layer.class_name, f"render: {exc}")] += 1
            finally:
                vm.current_layer = ""
                vm.drawing_model = False
    finally:
        vm.layer_draws = None
    return draws


def run_geo_renderer(vm, class_name, layers=True):
    """Construct a GeckoLib renderer for a default entity and read its resources."""
    from .mod_models import (
        EAGER_FALSE, ModModelError, _default_argument, _renderer_entity_class, render_type_object)

    parsed = vm.class_path.get(class_name)
    if parsed is None:
        raise ModModelError(f"{class_name} is not in the JAR")
    constructors = [method for method in parsed.methods if method.name == "<init>"]
    if not constructors:
        raise ModModelError(f"{class_name} has no constructor")
    constructor = min(constructors, key=lambda method: len(method_argument_types(method.desc)))
    arguments = [_default_argument(vm, type_desc) for type_desc in method_argument_types(constructor.desc)]
    renderer = vm.new_object(class_name, constructor.desc, arguments)
    if not isinstance(renderer.native, dict) or not isinstance(renderer.native.get("model"), JObject):
        raise ModModelError(f"{class_name} has no GeckoLib model")
    entity_class = _renderer_entity_class(parsed) or GEO_ANIMATABLE
    entity = Opaque(entity_class)
    model = renderer.native["model"]
    result = GeoRendererResult(class_name)
    result.model_path = call_resource(vm, model, "model", entity, renderer)
    result.animation_path = call_resource(vm, model, "animation", entity, renderer)
    try:
        result.texture = _rl_text(vm.invoke_virtual(
            class_name, "getTextureLocation",
            "(Lnet/minecraft/world/entity/Entity;)Lnet/minecraft/resources/ResourceLocation;",
            renderer, [entity]))
    except (JavaThrow, VMError):
        result.texture = ""
    result.texture = result.texture or call_resource(vm, model, "texture", entity, renderer)
    width, height = renderer.native.get("scale", [1.0, 1.0])
    if width > 1e-3 and height > 1e-3:
        result.scale = (float(width), float(height), float(width))
    shadow = renderer.fields.get("shadowRadius", renderer.native.get("shadow", 0.0))
    result.shadow = float(shadow) if isinstance(shadow, (int, float)) else 0.0
    if layers:
        base_type = render_type_object("entityCutoutNoCull", result.texture)
        result.layers = run_geo_layers(vm, renderer, entity, base_type)
        drawn = {draw.layer for draw in result.layers}
        eager = EagerOpaque(entity_class, EAGER_FALSE)
        result.optional_layers = [draw for draw in run_geo_layers(vm, renderer, eager, base_type)
                                  if draw.layer not in drawn]
    result.notes = sorted(f"{owner.rsplit('/', 1)[-1]}.{member}" for owner, member in vm.notes)
    return result


def load_geo_renderer(jar_path_or_class_path, renderer_class, layers=True, **limits):
    """``run_geo_renderer`` on a JAR path or an open ``JarClassPath``."""
    from .mod_models import ModModelError, new_model_vm, shared_class_path

    class_path = shared_class_path(jar_path_or_class_path)
    try:
        return run_geo_renderer(new_model_vm(class_path, **limits), renderer_class, layers=layers)
    except JavaThrow as thrown:
        raise ModModelError(f"renderer code threw {thrown.value.class_name}") from thrown
    except VMError as exc:
        raise ModModelError(str(exc)) from exc


# -- geometry ----------------------------------------------------------------------

def geo_geometry(document, identifier=""):
    """The first geometry of a ``.geo.json`` (any format version) as one dict.

    Every cube gets ``_mcbedrock_java_entity_cube``: GeckoLib draws without
    culling, a flat cube's later cap on top, and its cutout never draws a
    face with no visible texel (see ``geometry_from_model``).
    """
    if not isinstance(document, dict):
        raise ValueError("not a geometry file")
    entries = document.get("minecraft:geometry")
    if isinstance(entries, list) and entries:
        geometry = dict(entries[0])
    else:  # format 1.8.0: {"geometry.name": {...}}
        key = next((key for key in document if str(key).startswith("geometry.")), None)
        if key is None:
            raise ValueError("no geometry in the file")
        legacy = dict(document[key])
        geometry = {
            "description": {
                "identifier": key,
                "texture_width": legacy.get("texturewidth", 64),
                "texture_height": legacy.get("textureheight", 64),
            },
            "bones": legacy.get("bones", []),
        }
    description = dict(geometry.get("description") or {})
    if identifier:
        description["identifier"] = identifier
    geometry["description"] = description
    bones = []
    for bone in geometry.get("bones") or ():
        if not isinstance(bone, dict):
            continue
        bone = dict(bone)
        bone["cubes"] = [dict(cube, _mcbedrock_java_entity_cube=True)
                         for cube in bone.get("cubes") or () if isinstance(cube, dict)]
        bones.append(bone)
    geometry["bones"] = bones
    geometry["_mcbedrock_format_version"] = str(document.get("format_version", "1.12.0"))
    geometry["_mcbedrock_schema_family"] = "geckolib"
    return geometry


def geo_stem(path):
    """``assets/ns/geo/entity/ghoul.geo.json`` -> ``ghoul``."""
    name = path.rsplit("/", 1)[-1]
    return re.sub(r"(\.geo)?\.json$", "", name)


__all__ = [
    "GeoRendererResult", "append_to_path", "call_resource", "geo_geometry", "geo_stem",
    "install_geckolib", "is_geckolib_entity_renderer", "is_geckolib_renderer",
    "load_geo_renderer", "run_geo_layers", "run_geo_renderer",
]
