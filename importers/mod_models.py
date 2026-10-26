"""Entity models that mods build in code (Citadel), read from their JAR.

Citadel mods (Alex's Mobs, Alex's Caves, ...) describe each mob in a model
class constructor: ``new AdvancedModelBox(this, "head")``, ``setPos``,
``setTextureOffset(u, v).addBox(...)``, ``addChild``. ``java_vm`` runs that
code against Python versions of Citadel's model API, written from Citadel's
own bytecode, and this module turns the parts it built into the Bedrock
geometry dict the existing rig builder already imports.

Renderers are run the same way: the renderer's constructor passes the mob's
main model to ``MobRenderer``, and ``getTextureLocation`` returns the texture
for the default entity state (every entity getter answers 0/false).
Nothing here imports Blender.
"""

from __future__ import annotations

import io
import math
import os
import re
import threading
import zipfile
from dataclasses import dataclass, field

from .java_classes import JarClassPath, method_argument_types, method_return_type
from .java_vm import (
    NOT_HANDLED,
    EagerOpaque,
    JArray,
    JObject,
    JavaThrow,
    JavaVM,
    Opaque,
    VMError,
    f32,
    list_items,
    new_list,
    opaque_default,
)

CITADEL_MODEL = "com/github/alexthe666/citadel/client/model/"
ADVANCED_BOX = CITADEL_MODEL + "AdvancedModelBox"
BASIC_PART = CITADEL_MODEL + "basic/BasicModelPart"
ADVANCED_MODEL = CITADEL_MODEL + "AdvancedEntityModel"
BASIC_MODEL = CITADEL_MODEL + "basic/BasicEntityModel"
MODEL_ANIMATOR = CITADEL_MODEL + "ModelAnimator"
CITADEL_MODEL_BASES = (ADVANCED_MODEL, BASIC_MODEL)

MC_MODEL = "net/minecraft/client/model/Model"
MC_ENTITY_MODEL = "net/minecraft/client/model/EntityModel"
RESOURCE_LOCATION = "net/minecraft/resources/ResourceLocation"
POSE_STACK = "com/mojang/blaze3d/vertex/PoseStack"
ENTITY_RENDERER = "net/minecraft/client/renderer/entity/EntityRenderer"
LIVING_RENDERER = "net/minecraft/client/renderer/entity/LivingEntityRenderer"
MOB_RENDERER = "net/minecraft/client/renderer/entity/MobRenderer"
ENTITY_RENDERER_BASES = (ENTITY_RENDERER, LIVING_RENDERER, MOB_RENDERER)

# EntityModel/Model fields by their Forge (SRG) and Mojang names.
_ENTITY_MODEL_FLAGS = ("f_102608_", "f_102609_", "f_102610_",
                       "attackTime", "riding", "young")
# LivingEntityRenderer.model / layers.
_RENDERER_MODEL_FIELDS = ("f_115290_", "model")
_RENDERER_LAYER_FIELDS = ("f_115291_", "layers")
# EntityRenderer#getTextureLocation(Entity).
_TEXTURE_METHODS = ("m_5478_", "getTextureLocation")
_ADD_LAYER_METHODS = ("m_115326_", "addLayer")
# EntityModel#prepareMobModel, #setupAnim, #renderToBuffer; EntityRenderer#render.
_PREPARE_METHODS = ("m_6839_", "prepareMobModel")
_SETUP_ANIM_METHODS = ("m_6973_", "setupAnim")
_RENDER_TO_BUFFER_METHODS = ("m_7695_", "renderToBuffer")
_RENDER_METHODS = ("m_7392_", "render")
_SCALE_METHODS = ("m_7546_", "scale")
VERTEX_CONSUMER = "com/mojang/blaze3d/vertex/VertexConsumer"
MULTI_BUFFER_SOURCE = "net/minecraft/client/renderer/MultiBufferSource"
RENDER_TYPE = "net/minecraft/client/renderer/RenderType"
RENDER_LAYER = "net/minecraft/client/renderer/entity/layers/RenderLayer"
EYES_LAYER = "net/minecraft/client/renderer/entity/layers/EyesLayer"
_LAYER_RENDER_DESC = (f"(L{POSE_STACK};L{MULTI_BUFFER_SOURCE};ILnet/minecraft/world/entity/Entity;"
                      "FFFFFF)V")
# Render types that light themselves: eyes, emissive skins, charged auras.
GLOW_RENDER_TYPES = frozenset({"eyes", "entityTranslucentEmissive", "breezeEyes", "energySwirl",
                               "geckolibGlowmask"})
# What an "eager" stand-in entity still answers false: with these true a
# layer would hide the model instead of showing its optional parts.
EAGER_FALSE = frozenset({
    "isInvisible", "isInvisibleTo", "isBaby", "isSleeping", "isDeadOrDying", "isSpectator",
    "isPassenger", "isRemoved", "isUnderWater", "isInWater", "isInLava", "isFallFlying",
    "isAutoSpinAttack", "isSwimming", "isVisuallySwimming", "isShiftKeyDown", "isCrouching",
})
_PART_RENDER_DESC = f"(L{POSE_STACK};L{VERTEX_CONSUMER};IIFFFF)V"
_RENDER_TO_BUFFER_DESC = _PART_RENDER_DESC
_RENDER_DESC = (f"(Lnet/minecraft/world/entity/Entity;FFL{POSE_STACK};"
                f"L{MULTI_BUFFER_SOURCE};I)V")
_SETUP_ANIM_DESC = "(Lnet/minecraft/world/entity/Entity;FFFFF)V"
_PREPARE_DESC = "(Lnet/minecraft/world/entity/Entity;FFF)V"
_SCALE_DESC = f"(Lnet/minecraft/world/entity/LivingEntity;L{POSE_STACK};F)V"


class ModModelError(ValueError):
    """A mod model could not be read from its JAR."""


@dataclass
class JavaCube:
    """One ``addBox`` call, in its part's local space (Java model units)."""

    u: int
    v: int
    origin: tuple
    size: tuple
    inflate: tuple
    mirror: bool
    texture_size: tuple


@dataclass
class JavaPart:
    name: str
    pivot: tuple
    rotation: tuple  # radians, applied Z then Y then X like the game
    scale: tuple = (1.0, 1.0, 1.0)
    scale_children: bool = False
    visible: bool = True
    cubes: list = field(default_factory=list)
    children: list = field(default_factory=list)


@dataclass
class JavaEntityModel:
    class_name: str
    texture_size: tuple
    roots: list
    warnings: list = field(default_factory=list)
    # id(part object) -> JavaPart, for applying what a default draw shows.
    sources: dict = field(default_factory=dict, repr=False)

    def iter_parts(self):
        pending = list(self.roots)
        while pending:
            part = pending.pop(0)
            yield part
            pending[0:0] = part.children


# -- hosts: Citadel's model API ----------------------------------------------

def _part_state(this):
    if not isinstance(this.native, dict) or "cubes" not in this.native:
        this.native = {"cubes": [], "children": [], "parent": None, "model": None,
                       "texture_size": None}
    return this.native


def _float(value):
    return f32(value) if isinstance(value, (int, float)) else 0.0


def _init_basic_part(this, texture_width=64.0, texture_height=32.0):
    fields = this.fields
    fields.setdefault("rotationPointX", 0.0)
    fields.setdefault("rotationPointY", 0.0)
    fields.setdefault("rotationPointZ", 0.0)
    fields.setdefault("rotateAngleX", 0.0)
    fields.setdefault("rotateAngleY", 0.0)
    fields.setdefault("rotateAngleZ", 0.0)
    fields.setdefault("textureOffsetX", 0)
    fields.setdefault("textureOffsetY", 0)
    fields.setdefault("mirror", 0)
    fields["showModel"] = 1
    fields["textureWidth"] = _float(texture_width)
    fields["textureHeight"] = _float(texture_height)
    _part_state(this)


def _add_cube(this, u, v, x, y, z, width, height, depth, dx, dy, dz, mirror):
    state = _part_state(this)
    texture_size = state["texture_size"] or (
        this.fields.get("textureWidth", 64.0), this.fields.get("textureHeight", 32.0))
    state["cubes"].append(JavaCube(
        int(u), int(v),
        (_float(x), _float(y), _float(z)),
        (_float(width), _float(height), _float(depth)),
        (_float(dx), _float(dy), _float(dz)),
        bool(mirror),
        (float(texture_size[0]), float(texture_size[1])),
    ))


def _add_box_call(vm, this, args, desc):
    """Every public Citadel ``addBox`` overload (BasicModelPart and AdvancedModelBox)."""
    types = method_argument_types(desc)
    fields = this.fields
    mirror = fields.get("mirror", 0)
    if types and types[0] == "Ljava/lang/String;":
        # addBox(name, x, y, z, int w, int h, int d, delta, texX, texY)
        _name, x, y, z, width, height, depth, delta, tex_x, tex_y = args
        fields["textureOffsetX"] = int(tex_x)
        fields["textureOffsetY"] = int(tex_y)
        deltas = (delta, delta, delta)
    else:
        x, y, z, width, height, depth = args[:6]
        rest = args[6:]
        rest_types = types[6:]
        if rest_types == ["Z"]:
            deltas = (0.0, 0.0, 0.0)
            mirror = rest[0]
        elif rest_types == ["F"]:
            deltas = (rest[0],) * 3
        elif rest_types == ["F", "Z"]:
            deltas = (rest[0],) * 3
            mirror = rest[1]
        elif rest_types == ["F", "F", "F"]:
            deltas = tuple(rest)
        else:
            deltas = (0.0, 0.0, 0.0)
    _add_cube(this, fields.get("textureOffsetX", 0), fields.get("textureOffsetY", 0),
              x, y, z, width, height, depth, *deltas, mirror)
    return this if method_return_type(desc) != "V" else None


def _set_texture_offset(vm, this, args, desc):
    this.fields["textureOffsetX"] = int(args[0])
    this.fields["textureOffsetY"] = int(args[1])
    return this


def _set_rotation_point(vm, this, args, desc):
    this.fields["rotationPointX"] = _float(args[0])
    this.fields["rotationPointY"] = _float(args[1])
    this.fields["rotationPointZ"] = _float(args[2])


def _add_child(vm, this, args, desc):
    child = args[0]
    _part_state(this)["children"].append(child)
    if isinstance(child, JObject) and vm.is_instance(child, ADVANCED_BOX):
        _part_state(child)["parent"] = this


def _model_texture_size(model, wide=True):
    """Citadel's ``texWidth``/``texHeight`` (AdvancedEntityModel), else BasicEntityModel's."""
    if not isinstance(model, JObject):
        return (64.0, 32.0)
    if wide:
        return (float(model.fields.get("texWidth", 32)), float(model.fields.get("texHeight", 32)))
    return (float(model.fields.get("textureWidth", 64)), float(model.fields.get("textureHeight", 32)))


def _render_arguments(args, consumer=None):
    pose = args[0] if args else None
    return [pose, consumer if consumer is not None else Opaque(VERTEX_CONSUMER),
            0, 0, 1.0, 1.0, 1.0, 1.0]


def _record_draw(vm, this, args, desc):
    """``BasicModelPart.render``: a hidden part skips itself and its children.

    A layer may draw single parts itself (``model.head.render(...)``); that
    counts as a draw of the part's model with only those parts.
    """
    if not isinstance(this, JObject) or not this.fields.get("showModel", 1):
        return None
    started = None
    if not getattr(vm, "drawing_model", False):
        owner = _part_state(this)["model"]
        started = begin_draw(vm, owner if isinstance(owner, JObject) else this,
                             args[1] if len(args) > 1 else None)
    try:
        drawn = getattr(vm, "drawn_parts", None)
        if drawn is None:
            return None
        drawn.add(id(this))
        for child in _part_state(this)["children"]:
            if isinstance(child, JObject):
                vm.invoke_virtual(child.class_name, "render", _PART_RENDER_DESC, child,
                                  _render_arguments(args, args[1] if len(args) > 1 else None))
    finally:
        end_draw(vm, started)
    return None


def _draw_parts(vm, this, args, desc):
    """Citadel's ``BasicEntityModel.renderToBuffer``: render every ``parts()`` root."""
    consumer = args[1] if len(args) > 1 else None
    started = begin_draw(vm, this, consumer)
    try:
        parts = vm.invoke_virtual(this.class_name, "parts", "()Ljava/lang/Iterable;", this, [])
        for part in list_items(parts) or ():
            if isinstance(part, JObject):
                vm.invoke_virtual(part.class_name, "render", _PART_RENDER_DESC, part,
                                  _render_arguments(args, consumer))
    finally:
        end_draw(vm, started)


# -- recording what render layers draw ----------------------------------------

@dataclass
class LayerDraw:
    """One model a render layer drew: the model object, texture, render type and parts."""

    layer: str
    model: object
    texture: str
    render_type: str
    parts: set = field(default_factory=set)

    @property
    def glow(self):
        return self.render_type in GLOW_RENDER_TYPES


def render_type_object(name, texture):
    return JObject(RENDER_TYPE, native={"type": str(name), "texture": str(texture or "")})


def _consumer(render_type):
    return JObject(VERTEX_CONSUMER, native={"render_type": render_type})


def begin_draw(vm, model, consumer):
    """Start recording a model draw into ``vm.layer_draws``; ``None`` when not recording.

    Only draws through a buffer whose render type is known are recorded (a
    layer's ``bufferSource.getBuffer(RenderType.eyes(texture))``); the
    renderer's own pass uses an opaque buffer.
    """
    draws = getattr(vm, "layer_draws", None)
    native = consumer.native if isinstance(consumer, JObject) and isinstance(consumer.native, dict) else {}
    render_type = native.get("render_type")
    if draws is None or getattr(vm, "drawing_model", False) or not isinstance(render_type, JObject):
        return None
    type_native = render_type.native if isinstance(render_type.native, dict) else {}
    draw = LayerDraw(getattr(vm, "current_layer", "") or "", model,
                     type_native.get("texture", ""), type_native.get("type", ""))
    draws.append(draw)
    started = (draw, vm.drawn_parts)
    vm.drawn_parts = set()
    vm.drawing_model = True
    return started


def end_draw(vm, started):
    if started is not None:
        draw, outer = started
        draw.parts = vm.drawn_parts or set()
        vm.drawn_parts = outer
        vm.drawing_model = False


def _model_render_desc(vm, model):
    """The ``renderToBuffer`` descriptor the model's own class chain declares (1.20 or 1.21)."""
    for name in vm.class_path.superclasses(model.class_name):
        parsed = vm.class_path.get(name)
        for method in (parsed.methods if parsed is not None else ()):
            if method.name in _RENDER_TO_BUFFER_METHODS and method.desc.startswith(
                    f"(L{POSE_STACK};L{VERTEX_CONSUMER};"):
                return method.desc
    return _RENDER_TO_BUFFER_DESC


def draw_model(vm, model, consumer, pose=None):
    """``model.renderToBuffer(pose, consumer, light, overlay, ...)`` for a layer."""
    if not isinstance(model, JObject):
        return
    desc = _model_render_desc(vm, model)
    tail = [0xF00000, 0] + ([1.0, 1.0, 1.0, 1.0] if desc.endswith("FFFF)V") else [-1])
    vm.invoke_virtual(model.class_name, "renderToBuffer", desc, model,
                      [pose if isinstance(pose, JObject) else _new_pose(), consumer] + tail)


def _layer_parent_model(vm, layer):
    native = layer.native if isinstance(layer.native, dict) else {}
    parent = native.get("parent")
    if isinstance(parent, JObject):
        return vm.invoke_virtual(parent.class_name, "getModel", f"()L{MC_ENTITY_MODEL};", parent, [])
    return None


def _install_drawing(vm):
    """RenderType, buffers and vanilla's layer base classes, recording layer draws."""
    vm.host_class(RENDER_TYPE)

    @vm.dispatcher(RENDER_TYPE, kind="static")
    def render_type_static(vm, this, name, args, desc):
        if method_return_type(desc) != f"L{RENDER_TYPE};":
            return NOT_HANDLED
        texture = next((resource_location_text(value) for value in args
                        if resource_location_text(value)), "")
        return render_type_object(name, texture)

    vm.method(RENDER_TYPE, None)(lambda vm, this, args, desc: opaque_default(method_return_type(desc)))

    vm.host_class(MULTI_BUFFER_SOURCE)
    vm.method(MULTI_BUFFER_SOURCE, "getBuffer")(lambda vm, this, args, desc: _consumer(args[0]))
    vm.method(MULTI_BUFFER_SOURCE, None)(
        lambda vm, this, args, desc: opaque_default(method_return_type(desc)))
    vm.host_class(VERTEX_CONSUMER)
    vm.method(VERTEX_CONSUMER, None)(
        lambda vm, this, args, desc: this if method_return_type(desc) == f"L{VERTEX_CONSUMER};"
        else opaque_default(method_return_type(desc)))

    vm.host_class(RENDER_LAYER)
    vm.host_class(EYES_LAYER, parent=RENDER_LAYER)

    @vm.method(RENDER_LAYER, "<init>")
    def render_layer_init(vm, this, args, desc):
        this.native = {"parent": args[0] if args else None}

    vm.method(RENDER_LAYER, "getParentModel")(
        lambda vm, this, args, desc: _layer_parent_model(vm, this))

    @vm.method(RENDER_LAYER, "getTextureLocation")
    def layer_texture(vm, this, args, desc):
        parent = (this.native or {}).get("parent") if isinstance(this.native, dict) else None
        if not isinstance(parent, JObject):
            return None
        return vm.invoke_virtual(parent.class_name, "getTextureLocation",
                                 f"(Lnet/minecraft/world/entity/Entity;)L{RESOURCE_LOCATION};",
                                 parent, args[:1])

    @vm.dispatcher(RENDER_LAYER, kind="static")
    def render_layer_static(vm, this, name, args, desc):
        if name == "renderColoredCutoutModel":
            model, texture, pose = args[0], args[1], args[2]
        elif name == "coloredCutoutModelCopyLayerRender":
            parent, model, texture, pose = args[0], args[1], args[2], args[3]
            if isinstance(parent, JObject) and isinstance(model, JObject):
                vm.invoke_virtual(parent.class_name, "copyPropertiesTo",
                                  f"(L{MC_ENTITY_MODEL};)V", parent, [model])
        else:
            return NOT_HANDLED
        draw_model(vm, model, _consumer(render_type_object(
            "entityCutoutNoCull", resource_location_text(texture))), pose)
        return None

    vm.method(RENDER_LAYER, None)(lambda vm, this, args, desc: opaque_default(method_return_type(desc)))

    @vm.method(EYES_LAYER, "render")
    def eyes_render(vm, this, args, desc):
        render_type = vm.invoke_virtual(this.class_name, "renderType", f"()L{RENDER_TYPE};", this, [])
        if isinstance(render_type, JObject):
            draw_model(vm, _layer_parent_model(vm, this), _consumer(render_type),
                       args[0] if args else None)
        return None


def _install_citadel(vm):
    vm.host_class(MC_MODEL)
    vm.host_class(MC_ENTITY_MODEL, parent=MC_MODEL)
    vm.host_class(BASIC_MODEL, parent=MC_ENTITY_MODEL)
    vm.host_class(ADVANCED_MODEL, parent=BASIC_MODEL)
    vm.host_class(BASIC_PART)
    vm.host_class(ADVANCED_BOX, parent=BASIC_PART)
    vm.host_class(MODEL_ANIMATOR)

    def model_init(vm, this, args, desc):
        for name in _ENTITY_MODEL_FLAGS:
            this.fields.setdefault(name, 0.0 if name in ("f_102608_", "attackTime") else 0)

    vm.method(MC_MODEL, "<init>")(model_init)
    vm.method(MC_ENTITY_MODEL, "<init>")(model_init)

    @vm.method(BASIC_MODEL, "<init>")
    def basic_model_init(vm, this, args, desc):
        model_init(vm, this, args, desc)
        this.fields["textureWidth"] = 64
        this.fields["textureHeight"] = 32

    @vm.method(ADVANCED_MODEL, "<init>")
    def advanced_model_init(vm, this, args, desc):
        basic_model_init(vm, this, args, desc)
        this.fields["movementScale"] = 1.0
        this.fields["texWidth"] = 32
        this.fields["texHeight"] = 32

    def each_part(vm, this, method):
        parts = vm.invoke_virtual(this.class_name, "getAllParts", "()Ljava/lang/Iterable;", this, [])
        for part in list_items(parts) or ():
            if isinstance(part, JObject):
                vm.invoke_virtual(ADVANCED_BOX, method, "()V", part, [])

    vm.method(ADVANCED_MODEL, "updateDefaultPose")(
        lambda vm, this, args, desc: each_part(vm, this, "updateDefaultPose"))
    vm.method(ADVANCED_MODEL, "resetToDefaultPose")(
        lambda vm, this, args, desc: each_part(vm, this, "resetToDefaultPose"))

    @vm.method(ADVANCED_MODEL, "setRotateAngle")
    def set_rotate_angle(vm, this, args, desc):
        box, x, y, z = args
        if isinstance(box, JObject):
            box.fields["rotateAngleX"] = _float(x)
            box.fields["rotateAngleY"] = _float(y)
            box.fields["rotateAngleZ"] = _float(z)

    vm.method(ADVANCED_MODEL, "getMovementScale")(
        lambda vm, this, args, desc: this.fields.get("movementScale", 1.0))
    vm.method(ADVANCED_MODEL, "setMovementScale")(
        lambda vm, this, args, desc: this.fields.__setitem__("movementScale", _float(args[0])))
    # Procedural animation helpers (walk, flap, chainSwing, progressRotation,
    # ...) only move parts. The import keeps the constructor's rest pose.
    vm.method(ADVANCED_MODEL, None)(
        lambda vm, this, args, desc: opaque_default(method_return_type(desc)))
    vm.method(BASIC_MODEL, None)(
        lambda vm, this, args, desc: opaque_default(method_return_type(desc)))
    vm.method(MC_ENTITY_MODEL, None)(
        lambda vm, this, args, desc: opaque_default(method_return_type(desc)))

    # BasicModelPart
    @vm.method(BASIC_PART, "<init>")
    def basic_part_init(vm, this, args, desc):
        types = method_argument_types(desc)
        if types == ["I", "I", "I", "I"]:
            _init_basic_part(this, args[0], args[1])
            _set_texture_offset(vm, this, args[2:], desc)
            return
        model = args[0] if args else None
        width, height = _model_texture_size(model, wide=False)
        _init_basic_part(this, width, height)
        _part_state(this)["model"] = model
        if types[1:] == ["I", "I"]:
            _set_texture_offset(vm, this, args[1:], desc)

    vm.method(BASIC_PART, "setTextureOffset")(_set_texture_offset)
    vm.method(BASIC_PART, "addBox")(_add_box_call)
    vm.method(BASIC_PART, "setRotationPoint")(_set_rotation_point)
    vm.method(BASIC_PART, "addChild")(
        lambda vm, this, args, desc: _part_state(this)["children"].append(args[0]))

    @vm.method(BASIC_PART, "setTextureSize")
    def set_texture_size(vm, this, args, desc):
        this.fields["textureWidth"] = _float(args[0])
        this.fields["textureHeight"] = _float(args[1])
        return this

    @vm.method(BASIC_PART, "copyModelAngles")
    def copy_model_angles(vm, this, args, desc):
        source = args[0]
        if isinstance(source, JObject):
            for name in ("rotateAngleX", "rotateAngleY", "rotateAngleZ",
                         "rotationPointX", "rotationPointY", "rotationPointZ"):
                this.fields[name] = source.fields.get(name, 0.0)

    vm.method(BASIC_PART, None)(
        lambda vm, this, args, desc: opaque_default(method_return_type(desc)))

    # AdvancedModelBox
    @vm.method(ADVANCED_BOX, "<init>")
    def advanced_box_init(vm, this, args, desc):
        model = args[0] if args else None
        basic_part_init(vm, this, [model], "(L" + BASIC_MODEL + ";)V")
        this.fields["scaleX"] = this.fields["scaleY"] = this.fields["scaleZ"] = 1.0
        this.fields["boxName"] = ""
        this.fields["scaleChildren"] = 0
        state = _part_state(this)
        # AdvancedModelBox keeps its own (private) texture size, from texWidth.
        state["texture_size"] = _model_texture_size(model, wide=True)
        types = method_argument_types(desc)
        if types[1:] == ["Ljava/lang/String;"]:
            this.fields["boxName"] = args[1] if isinstance(args[1], str) else ""
        elif types[1:] == ["I", "I"]:
            _set_texture_offset(vm, this, args[1:], desc)

    @vm.method(ADVANCED_BOX, "setTexSize")
    def set_tex_size(vm, this, args, desc):
        _part_state(this)["texture_size"] = (_float(args[0]), _float(args[1]))
        return this

    vm.method(ADVANCED_BOX, "addBox")(_add_box_call)
    vm.method(ADVANCED_BOX, "setTextureOffset")(_set_texture_offset)
    vm.method(ADVANCED_BOX, "setPos")(_set_rotation_point)
    vm.method(ADVANCED_BOX, "addChild")(_add_child)

    @vm.method(ADVANCED_BOX, "setScale")
    def set_scale(vm, this, args, desc):
        this.fields["scaleX"], this.fields["scaleY"], this.fields["scaleZ"] = (
            _float(value) for value in args)

    for axis in "XYZ":
        vm.method(ADVANCED_BOX, "setScale" + axis)(
            lambda vm, this, args, desc, axis=axis: this.fields.__setitem__(
                "scale" + axis, _float(args[0])))
    vm.method(ADVANCED_BOX, "setShouldScaleChildren")(
        lambda vm, this, args, desc: this.fields.__setitem__("scaleChildren", int(bool(args[0]))))

    @vm.method(ADVANCED_BOX, "updateDefaultPose")
    def update_default_pose(vm, this, args, desc):
        for axis in "XYZ":
            this.fields["defaultRotation" + axis] = this.fields.get("rotateAngle" + axis, 0.0)
            this.fields["defaultPosition" + axis] = this.fields.get("rotationPoint" + axis, 0.0)

    @vm.method(ADVANCED_BOX, "resetToDefaultPose")
    def reset_to_default_pose(vm, this, args, desc):
        for axis in "XYZ":
            this.fields["rotateAngle" + axis] = this.fields.get("defaultRotation" + axis, 0.0)
            this.fields["rotationPoint" + axis] = this.fields.get("defaultPosition" + axis, 0.0)

    vm.method(ADVANCED_BOX, "render")(_record_draw)
    vm.method(BASIC_PART, "render")(_record_draw)
    for name in _RENDER_TO_BUFFER_METHODS:
        vm.method(BASIC_MODEL, name)(_draw_parts)
    vm.method(ADVANCED_BOX, "getParent")(lambda vm, this, args, desc: _part_state(this)["parent"])
    vm.method(ADVANCED_BOX, "setParent")(
        lambda vm, this, args, desc: _part_state(this).__setitem__("parent", args[0]))
    vm.method(ADVANCED_BOX, "getModel")(lambda vm, this, args, desc: _part_state(this)["model"])
    vm.method(ADVANCED_BOX, None)(
        lambda vm, this, args, desc: opaque_default(method_return_type(desc)))

    vm.static(MODEL_ANIMATOR, "create")(lambda vm, args, desc: JObject(MODEL_ANIMATOR))
    vm.method(MODEL_ANIMATOR, "<init>")(lambda vm, this, args, desc: None)
    # setAnimation answers false, so keyframe code is skipped.
    vm.method(MODEL_ANIMATOR, None)(
        lambda vm, this, args, desc: opaque_default(method_return_type(desc)))


# -- hosts: Minecraft resources, poses and renderers --------------------------

def resource_location_text(value):
    """``namespace:path`` of a host ``ResourceLocation``, else ``""``."""
    if isinstance(value, JObject) and isinstance(value.native, dict) and "path" in value.native:
        return f"{value.native['namespace']}:{value.native['path']}"
    return ""


def _set_resource_location(this, namespace, path):
    namespace = str(namespace or "minecraft")
    path = str(path or "")
    this.native = {"namespace": namespace, "path": path, "string": f"{namespace}:{path}"}
    return this


def _parse_resource_location(this, text):
    text = str(text or "")
    namespace, separator, path = text.partition(":")
    if not separator:
        namespace, path = "minecraft", text
    return _set_resource_location(this, namespace or "minecraft", path)


def _install_minecraft(vm):
    vm.host_class(RESOURCE_LOCATION)

    @vm.method(RESOURCE_LOCATION, "<init>")
    def resource_location_init(vm, this, args, desc):
        strings = [value for value in args if isinstance(value, str)]
        if len(strings) >= 2:
            _set_resource_location(this, strings[0], strings[1])
        elif strings:
            _parse_resource_location(this, strings[0])
        else:
            _set_resource_location(this, "minecraft", "")

    for name in ("fromNamespaceAndPath", "tryBuild"):
        vm.static(RESOURCE_LOCATION, name)(
            lambda vm, args, desc: _set_resource_location(JObject(RESOURCE_LOCATION), args[0], args[1]))
    for name in ("parse", "tryParse", "withDefaultNamespace", "m_135820_", "m_338530_"):
        vm.static(RESOURCE_LOCATION, name)(
            lambda vm, args, desc: _parse_resource_location(JObject(RESOURCE_LOCATION), args[0]))
    for name in ("m_135815_", "getPath"):
        vm.method(RESOURCE_LOCATION, name)(lambda vm, this, args, desc: this.native["path"])
    for name in ("m_135827_", "getNamespace"):
        vm.method(RESOURCE_LOCATION, name)(lambda vm, this, args, desc: this.native["namespace"])
    vm.method(RESOURCE_LOCATION, "toString")(lambda vm, this, args, desc: this.native["string"])
    vm.method(RESOURCE_LOCATION, "equals")(
        lambda vm, this, args, desc: 1 if resource_location_text(args[0]) == this.native["string"] else 0)
    vm.method(RESOURCE_LOCATION, "hashCode")(lambda vm, this, args, desc: hash(this.native["string"]) & 0x7FFFFFFF)

    # PoseStack: renderers' scale() calls are recorded for the model's size.
    vm.host_class(POSE_STACK)

    @vm.method(POSE_STACK, "<init>")
    def pose_stack_init(vm, this, args, desc):
        this.native = {"scale": [1.0, 1.0, 1.0], "depth": 0}

    for name in ("m_85841_", "scale"):
        @vm.method(POSE_STACK, name, "(FFF)V")
        def pose_scale(vm, this, args, desc):
            state = this.native or {"scale": [1.0, 1.0, 1.0], "depth": 0}
            this.native = state
            if state["depth"] == 0:
                for axis in range(3):
                    state["scale"][axis] *= float(args[axis])
    for name in ("m_85836_", "pushPose"):
        @vm.method(POSE_STACK, name)
        def pose_push(vm, this, args, desc):
            this.native = this.native or {"scale": [1.0, 1.0, 1.0], "depth": 0}
            this.native["depth"] += 1
    for name in ("m_85849_", "popPose"):
        @vm.method(POSE_STACK, name)
        def pose_pop(vm, this, args, desc):
            this.native = this.native or {"scale": [1.0, 1.0, 1.0], "depth": 0}
            this.native["depth"] = max(0, this.native["depth"] - 1)
    vm.method(POSE_STACK, None)(lambda vm, this, args, desc: opaque_default(method_return_type(desc)))

    vm.host_class(ENTITY_RENDERER)
    vm.host_class(LIVING_RENDERER, parent=ENTITY_RENDERER)
    vm.host_class(MOB_RENDERER, parent=LIVING_RENDERER)

    @vm.method(ENTITY_RENDERER, "<init>")
    def entity_renderer_init(vm, this, args, desc):
        this.native = {"model": None, "layers": [], "shadow": 0.0}
        for name in _RENDERER_LAYER_FIELDS:
            this.fields[name] = new_list()

    @vm.method(LIVING_RENDERER, "<init>")
    def living_renderer_init(vm, this, args, desc):
        entity_renderer_init(vm, this, args, desc)
        types = method_argument_types(desc)
        # The JAR reads and writes the model field under one naming (SRG
        # for Forge, Mojang's for NeoForge), so only that name is set.
        field_name = _RENDERER_MODEL_FIELDS[0 if _uses_srg_names(vm, this.class_name) else 1]
        for value, type_desc in zip(args, types):
            if isinstance(value, JObject) and type_desc.startswith("Lnet/minecraft/client/model/"):
                this.native["model"] = value
                this.fields[field_name] = value
            elif type_desc == "F":
                this.native["shadow"] = float(value)

    vm.method(MOB_RENDERER, "<init>")(living_renderer_init)
    for name in _ADD_LAYER_METHODS:
        @vm.method(LIVING_RENDERER, name)
        def add_layer(vm, this, args, desc):
            (this.native or {}).setdefault("layers", []).append(args[0])
            return 1
    for name in ("m_7200_", "getModel"):
        vm.method(LIVING_RENDERER, name)(
            lambda vm, this, args, desc: (this.native or {}).get("model"))
    for name in _RENDER_METHODS:
        vm.method(LIVING_RENDERER, name)(_living_render)
    vm.method(ENTITY_RENDERER, None)(
        lambda vm, this, args, desc: opaque_default(method_return_type(desc)))

    # Mth: only animation code uses it; the common math is answered exactly.
    mth = "net/minecraft/util/Mth"
    vm.host_class(mth)
    functions = {
        ("m_14031_", "sin"): math.sin, ("m_14089_", "cos"): math.cos,
        ("m_14116_", "sqrt"): lambda value: math.sqrt(value) if value >= 0 else math.nan,
        ("m_14154_", "abs"): abs,
        ("m_14177_", "wrapDegrees"): lambda value: _wrap_degrees(value),
        ("m_14175_",): lambda value: _wrap_degrees(value),
    }
    for names, function in functions.items():
        for name in names:
            vm.static(mth, name)(
                lambda vm, args, desc, function=function: _typed(desc, function(*args)))
    for name in ("m_14036_", "m_14008_", "m_14045_", "clamp"):
        vm.static(mth, name)(lambda vm, args, desc: _typed(desc, min(max(args[0], args[1]), args[2])))
    for name in ("m_14179_", "m_14139_", "lerp"):
        vm.static(mth, name)(lambda vm, args, desc: _typed(desc, args[1] + args[0] * (args[2] - args[1])))
    vm.static(mth, None)(lambda vm, args, desc: opaque_default(method_return_type(desc)))


def _renderer_model(renderer):
    for name in _RENDERER_MODEL_FIELDS:
        value = renderer.fields.get(name)
        if isinstance(value, JObject):
            return value
    return (renderer.native or {}).get("model") if isinstance(renderer.native, dict) else None


def _new_pose():
    return JObject(POSE_STACK, native={"scale": [1.0, 1.0, 1.0], "depth": 0})


def _pose_model(vm, model, entity, pose):
    """``prepareMobModel``, ``setupAnim`` and ``renderToBuffer`` for a default entity."""
    for name in _PREPARE_METHODS:
        vm.invoke_virtual(model.class_name, name, _PREPARE_DESC, model, [entity, 0.0, 0.0, 0.0])
    for name in _SETUP_ANIM_METHODS:
        vm.invoke_virtual(model.class_name, name, _SETUP_ANIM_DESC, model,
                          [entity, 0.0, 0.0, 0.0, 0.0, 0.0])
    for name in _RENDER_TO_BUFFER_METHODS:
        vm.invoke_virtual(model.class_name, name, _RENDER_TO_BUFFER_DESC, model,
                          _render_arguments([pose]))


def _living_render(vm, this, args, desc):
    """``LivingEntityRenderer.render`` for a default entity: scale, pose, draw.

    Renderers may swap ``this.model`` in ``scale`` (a blobfish's pressure
    states), so the model is read again afterwards.
    """
    entity = args[0] if args else Opaque()
    pose = args[3] if len(args) > 3 and isinstance(args[3], JObject) else _new_pose()
    for name in _SCALE_METHODS:
        vm.invoke_virtual(this.class_name, name, _SCALE_DESC, this, [entity, pose, 0.0])
    model = _renderer_model(this)
    if isinstance(model, JObject):
        _pose_model(vm, model, entity, pose)
    if getattr(vm, "layer_draws", None) is not None:
        render_layers(vm, this, pose, getattr(vm, "layer_entity", None) or entity)
    return None


def render_layers(vm, renderer, pose, entity):
    """Run each render layer the renderer added (``addLayer``), recording its draws."""
    buffers = JObject(MULTI_BUFFER_SOURCE)
    for layer in list((renderer.native or {}).get("layers", ()) if isinstance(renderer.native, dict) else ()):
        if not isinstance(layer, JObject):
            continue
        vm.current_layer = layer.class_name
        try:
            vm.invoke_virtual(layer.class_name, "render", _LAYER_RENDER_DESC, layer,
                              [pose, buffers, 0xF000F0, entity, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        except (JavaThrow, VMError) as exc:
            vm.notes[(layer.class_name, f"render: {exc}")] += 1
        finally:
            vm.current_layer = ""
            vm.drawing_model = False


def _uses_srg_names(vm, class_name):
    """Forge JARs call Minecraft through SRG names (``m_7392_``).

    Read through the default remapper, a JAR's SRG names arrive as Mojang's.
    """
    if getattr(vm.class_path, "remap", None) is not None:
        return False
    for name in vm.class_path.superclasses(class_name):
        parsed = vm.class_path.get(name)
        if parsed is not None and any(method.name.startswith("m_") for method in parsed.methods):
            return True
    return False


def default_draw(vm, renderer, model, entity, layers=None):
    """Parts a default entity's draw shows, as ``id()`` of part objects.

    Runs the renderer's own ``render`` (or the model's ``renderToBuffer``
    when there is no renderer). ``None`` when the code could not be run or
    drew nothing, so callers keep the constructor's visibility. A ``layers``
    list receives the render layers' draws (``LayerDraw``).
    """
    vm.drawn_parts = set()
    vm.layer_draws = layers
    pose = _new_pose()
    try:
        if renderer is not None:
            name = _RENDER_METHODS[0 if _uses_srg_names(vm, renderer.class_name) else 1]
            vm.invoke_virtual(renderer.class_name, name, _RENDER_DESC, renderer,
                              [entity, 0.0, 0.0, pose, Opaque(MULTI_BUFFER_SOURCE), 0])
        elif isinstance(model, JObject):
            _pose_model(vm, model, entity, pose)
    except (JavaThrow, VMError):
        return None
    finally:
        drawn, vm.drawn_parts = vm.drawn_parts, None
        vm.layer_draws = None
        vm.drawing_model = False
    return drawn or None


def optional_layer_draws(vm, renderer, entity_class, skip=()):
    """What the layers draw for an "eager" entity (saddled, tamed, charged ...),
    leaving out the layers in ``skip`` (those a default entity already shows)."""
    draws = []
    vm.layer_draws = draws
    vm.drawn_parts = set()
    try:
        render_layers(vm, renderer, _new_pose(), EagerOpaque(entity_class, EAGER_FALSE))
    except (JavaThrow, VMError):
        pass
    finally:
        vm.layer_draws = None
        vm.drawn_parts = None
        vm.drawing_model = False
    return [draw for draw in draws if draw.layer not in set(skip)]


def _wrap_degrees(value):
    value = math.fmod(value, 360.0)
    if value >= 180.0:
        value -= 360.0
    if value < -180.0:
        value += 360.0
    return value


def _typed(desc, value):
    code = method_return_type(desc)
    if code == "F":
        return f32(value)
    if code == "I":
        return int(value)
    from .java_vm import JDouble
    return JDouble(value) if code == "D" else value


_LIBRARIES = {}  # (path, size, modification time) -> VanillaModelLibrary or None
_LIBRARY_LOCK = threading.RLock()  # one opening at a time; the others wait and share it


def vanilla_model_library(jar_path=None):
    """Vanilla's model classes from the newest Mojang-named client JAR, or ``None``.

    Only 26.1+ client JARs are unobfuscated. ``jar_path`` picks one explicitly.
    Each JAR is opened once: reading a client JAR's file list takes a while.
    """
    with _LIBRARY_LOCK:
        if jar_path:
            return _vanilla_library(jar_path, require_mojang_names=False)
        try:
            from .vanilla_assets import discover_vanilla_jars
            jars = discover_vanilla_jars()
        except Exception:
            jars = []
        for _version, path in reversed(sorted(jars, key=lambda item: _version_key(item[0]))):
            library = _vanilla_library(path)
            if library is not None:
                return library
        return None


def preload_vanilla_model_library():
    """Start opening ``vanilla_model_library()`` on a thread; a model that needs
    it before it is open waits for it instead of opening it again."""
    threading.Thread(target=vanilla_model_library, daemon=True).start()


def _vanilla_library(path, require_mojang_names=True):
    from .java_classes import VanillaModelLibrary

    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = (os.path.normcase(os.path.abspath(path)), stat.st_size, stat.st_mtime_ns, require_mojang_names)
    if key not in _LIBRARIES:
        library = None
        try:
            class_path = JarClassPath.open(path)
            if not require_mojang_names or "net/minecraft/client/model/HumanoidModel" in class_path:
                library = VanillaModelLibrary(class_path)
            else:
                class_path.close()
        except (OSError, zipfile.BadZipFile):
            pass
        _LIBRARIES[key] = library
    return _LIBRARIES[key]


def _version_key(version):
    return tuple(int(part) if part.isdigit() else 0 for part in re.split(r"[.\-]", str(version)))


_CLASS_PATHS = {}  # (path, size, modification time) -> JarClassPath, newest last


def shared_class_path(jar_path_or_class_path):
    """One parsed ``JarClassPath`` per JAR build, kept between imports.

    The VMs that run a mod's classes only read them, so all its models share
    them and each class is parsed once. The JAR is held in memory rather than
    open, so the file can still be moved or deleted. The newest two stay.
    """
    if isinstance(jar_path_or_class_path, JarClassPath):
        return jar_path_or_class_path
    path = os.path.abspath(jar_path_or_class_path)
    stat = os.stat(path)
    key = (os.path.normcase(path), stat.st_size, stat.st_mtime_ns)
    class_path = _CLASS_PATHS.pop(key, None)
    if class_path is None:
        with open(path, "rb") as stream:
            class_path = JarClassPath(zipfile.ZipFile(io.BytesIO(stream.read())))
    _CLASS_PATHS[key] = class_path
    while len(_CLASS_PATHS) > 2:
        _CLASS_PATHS.pop(next(iter(_CLASS_PATHS)))
    return class_path


def new_model_vm(class_path, library=None, **limits):
    """A ``JavaVM`` with Citadel, vanilla model-builder and Minecraft hosts."""
    from .mod_layer_models import install_layer_api

    from .mod_entity_data import install_entity_data
    from .mod_geckolib import install_geckolib
    from .mod_vanilla_models import install_vanilla_models

    vm = JavaVM(class_path, library=library, **limits)
    vm.drawn_parts = None
    vm.layer_draws = None
    vm.layer_entity = None
    vm.current_layer = ""
    vm.drawing_model = False
    _install_citadel(vm)
    install_layer_api(vm)
    _install_minecraft(vm)
    _install_drawing(vm)
    install_vanilla_models(vm)
    install_geckolib(vm)
    install_entity_data(vm)
    return vm


# -- reading built models -----------------------------------------------------

def is_citadel_model_class(class_path, name):
    chain = class_path.superclasses(name)
    return bool(chain) and chain[-1] in CITADEL_MODEL_BASES and name not in CITADEL_MODEL_BASES


def is_entity_renderer_class(class_path, name):
    chain = class_path.superclasses(name)
    if not chain or chain[-1] not in ENTITY_RENDERER_BASES or name in ENTITY_RENDERER_BASES:
        return False
    parsed = class_path.get(name)
    return parsed is not None and not parsed.access & 0x0400  # not abstract


def _part_name(vm, part, model, used):
    name = part.fields.get("boxName") if isinstance(part.fields.get("boxName"), str) else ""
    if not name and isinstance(model, JObject):
        name = next((key for key, value in model.fields.items() if value is part), "")
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name or "").strip("_") or "part"
    candidate = name
    suffix = 2
    while candidate.casefold() in used:
        candidate = f"{name}_{suffix}"
        suffix += 1
    used.add(candidate.casefold())
    return candidate


def read_built_model(vm, model):
    """Collect the part tree a Citadel model constructor built (or the baked
    ``ModelPart`` tree a vanilla-style model draws, in its rest pose)."""
    from .mod_vanilla_models import baked_root_of, model_from_baked

    if not isinstance(model, JObject):
        raise ModModelError("the renderer did not create a Citadel model")
    root, layer, _factory = baked_root_of(vm, model)
    if layer is not None:
        return model_from_baked(root, layer, model.class_name)
    warnings = []
    roots = None
    try:
        roots = list_items(vm.invoke_virtual(
            model.class_name, "parts", "()Ljava/lang/Iterable;", model, []))
    except (JavaThrow, VMError) as exc:
        warnings.append(f"parts() failed: {exc}")
    if not roots:
        # Fall back to every part with no parent, in field order.
        parts = [value for value in model.fields.values()
                 if isinstance(value, JObject) and vm.is_instance(value, BASIC_PART)]
        children = {id(child) for part in parts for child in _part_state(part)["children"]}
        roots = [part for part in parts if id(part) not in children]
    used = set()
    seen = set()

    def convert(part):
        if not isinstance(part, JObject) or id(part) in seen:
            return None
        seen.add(id(part))
        state = _part_state(part)
        fields = part.fields
        result = JavaPart(
            name=_part_name(vm, part, model, used),
            pivot=tuple(float(fields.get("rotationPoint" + axis, 0.0)) for axis in "XYZ"),
            rotation=tuple(float(fields.get("rotateAngle" + axis, 0.0)) for axis in "XYZ"),
            scale=tuple(float(fields.get("scale" + axis, 1.0)) for axis in "XYZ"),
            scale_children=bool(fields.get("scaleChildren", 0)),
            visible=bool(fields.get("showModel", 1)),
            cubes=list(state["cubes"]),
        )
        sources[id(part)] = result
        for child in state["children"]:
            converted = convert(child)
            if converted is not None:
                result.children.append(converted)
        return result

    sources = {}
    converted_roots = [part for part in (convert(root) for root in roots) if part is not None]
    if not converted_roots:
        raise ModModelError(f"{model.class_name} built no model parts")
    return JavaEntityModel(
        class_name=model.class_name,
        texture_size=_model_texture_size(model, wide=vm.is_instance(model, ADVANCED_MODEL)),
        roots=converted_roots,
        warnings=warnings,
        sources=sources,
    )


def construct_model(vm, class_name, arguments=None):
    """Run a model class's constructor with defaults for any parameters."""
    parsed = vm.class_path.get(class_name)
    if parsed is None:
        raise ModModelError(f"{class_name} is not in the JAR")
    constructors = [method for method in parsed.methods if method.name == "<init>"]
    if not constructors:
        raise ModModelError(f"{class_name} has no constructor")
    constructors.sort(key=lambda method: len(method_argument_types(method.desc)))
    constructor = constructors[0]
    if arguments is None:
        arguments = [_default_argument(vm, type_desc)
                     for type_desc in method_argument_types(constructor.desc)]
    return vm.new_object(class_name, constructor.desc, arguments)


def _default_argument(vm, type_desc):
    """0/false, an empty string, the first enum constant, or an opaque object.

    A renderer's ``EntityRendererProvider.Context`` is a host, so its
    ``bakeLayer`` builds the registered model.
    """
    from .mod_vanilla_models import BLOCK_CONTEXT, CONTEXT

    if type_desc in (f"L{CONTEXT};", f"L{BLOCK_CONTEXT};"):
        return JObject(type_desc[1:-1])
    return vm.default_for(type_desc)


@dataclass
class RendererResult:
    renderer_class: str
    model: object
    texture: str
    scale: tuple
    shadow: float
    renderer: object = None
    entity: object = None


def run_renderer(vm, class_name):
    """Construct an entity renderer and ask it for the default texture."""
    parsed = vm.class_path.get(class_name)
    if parsed is None:
        raise ModModelError(f"{class_name} is not in the JAR")
    constructors = [method for method in parsed.methods if method.name == "<init>"]
    if not constructors:
        raise ModModelError(f"{class_name} has no constructor")
    constructor = min(constructors, key=lambda method: len(method_argument_types(method.desc)))
    arguments = [_default_argument(vm, type_desc)
                 for type_desc in method_argument_types(constructor.desc)]
    renderer = vm.new_object(class_name, constructor.desc, arguments)
    native = renderer.native if isinstance(renderer.native, dict) else {}
    model = native.get("model")
    if model is None:
        # Plain EntityRenderers (projectiles) keep their model in a field,
        # often a static one.
        values = list(renderer.fields.values()) + list(vm.statics.get(class_name, {}).values())
        candidates = []
        for value in values:
            if (isinstance(value, JObject) and value not in candidates
                    and is_citadel_model_class(vm.class_path, value.class_name)):
                candidates.append(value)
        model = candidates[0] if len(candidates) == 1 else None
    texture = ""
    entity = Opaque(_renderer_entity_class(parsed) or "net/minecraft/world/entity/Entity")
    for name in _TEXTURE_METHODS:
        try:
            value = vm.invoke_virtual(
                class_name, name,
                "(Lnet/minecraft/world/entity/Entity;)Lnet/minecraft/resources/ResourceLocation;",
                renderer, [entity])
        except (JavaThrow, VMError):
            continue
        texture = resource_location_text(value)
        if texture:
            break
    scale = (1.0, 1.0, 1.0)
    pose = JObject(POSE_STACK, native={"scale": [1.0, 1.0, 1.0], "depth": 0})
    for method in parsed.methods:
        # LivingEntityRenderer#scale(LivingEntity, PoseStack, float)
        if method.name in _SCALE_METHODS and method.desc.endswith("Lcom/mojang/blaze3d/vertex/PoseStack;F)V"):
            types = method_argument_types(method.desc)
            try:
                vm.invoke_virtual(class_name, method.name, method.desc, renderer,
                                  [entity if type_desc.startswith("L") and "PoseStack" not in type_desc
                                   else pose if "PoseStack" in type_desc else 0.0
                                   for type_desc in types])
                recorded = tuple(pose.native["scale"])
                # A scale read from the entity (its size, age) comes out 0 for
                # the default entity: only a real constant size is kept.
                if all(math.isfinite(value) and value > 1e-3 for value in recorded):
                    scale = recorded
            except (JavaThrow, VMError):
                pass
            break
    return RendererResult(class_name, model, texture, scale, float(native.get("shadow", 0.0)),
                          renderer, entity)


def _renderer_entity_class(parsed):
    """The entity class from ``MobRenderer<EntityX, ModelX>`` (generic signature)."""
    signature = parsed.signature
    match = re.search(r"<L([^;<]+)[;<]", signature)
    return match.group(1) if match else ""


def renderer_entity_class(class_path, name):
    parsed = class_path.get(name)
    return _renderer_entity_class(parsed) if parsed is not None else ""


# -- Bedrock geometry ----------------------------------------------------------

def _degrees(value):
    return math.degrees(value) if abs(value) > 1e-12 else 0.0


def geometry_from_model(model, identifier="geometry.mod_entity", scale=(1.0, 1.0, 1.0)):
    """Convert to the Bedrock geometry dict the rig builder imports.

    Java model space has Y pointing down with the ground at 24. Bedrock
    geometry keeps X and Z and flips Y (``y -> 24 - y``). Under that flip the
    game's Z-then-Y-then-X rotations become Bedrock bone angles with the same
    values in degrees. Box UV keeps fractional sizes, as the game does.
    """
    bones = []
    texture_width, texture_height = model.texture_size
    warnings = list(model.warnings)

    def add(part, parent_name, parent_pivot, inherited_scale):
        if not part.visible:
            return
        pivot = tuple(parent_pivot[axis] + part.pivot[axis] * inherited_scale[axis]
                      for axis in range(3))
        part_scale = tuple(inherited_scale[axis] * part.scale[axis] for axis in range(3))
        bone = {
            "name": part.name,
            "pivot": [pivot[0], 24.0 - pivot[1], pivot[2]],
            "rotation": [_degrees(value) for value in part.rotation],
            "cubes": [],
        }
        if parent_name:
            bone["parent"] = parent_name
        for cube in part.cubes:
            if cube.texture_size != (texture_width, texture_height):
                warnings.append(
                    f"{part.name}: box texture size {cube.texture_size} differs from the "
                    f"model's {model.texture_size}")
            bone["cubes"].append(_cube(cube, pivot, part_scale))
        bones.append(bone)
        child_scale = part_scale if part.scale_children else inherited_scale
        for child in part.children:
            add(child, part.name, pivot, child_scale)

    for root in model.roots:
        add(root, None, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
    return {
        "description": {
            "identifier": identifier,
            "texture_width": texture_width,
            "texture_height": texture_height,
        },
        "bones": bones,
        "_mcbedrock_format_version": "1.12.0",
        "_mcbedrock_schema_family": "java_entity",
        "_mcbedrock_render_scale": list(scale),
    }, warnings


def _cube(cube, pivot, scale):
    (x, y, z), (width, height, depth) = cube.origin, cube.size
    dx, dy, dz = cube.inflate
    result = {
        "uv": [cube.u, cube.v],
        "mirror": bool(cube.mirror),
        # Java entity cubes: box UV keeps fractional sizes, a flat box draws
        # both caps with the later one on top (bedrock_uv), and faces without
        # a visible texel never draw (geometry).
        "_mcbedrock_box_uv_float_size": True,
        "_mcbedrock_java_entity_cube": True,
    }
    uniform = abs(dx - dy) < 1e-9 and abs(dy - dz) < 1e-9
    if scale == (1.0, 1.0, 1.0) and uniform:
        result["origin"] = [pivot[0] + x, 24.0 - (pivot[1] + y + height), pivot[2] + z]
        result["size"] = [width, height, depth]
        if dx:
            result["inflate"] = dx
        return result
    # Scaled parts and per-axis inflation: bake the drawn box, keep the
    # authored sizes for the box-UV layout.
    low = [(x - dx) * scale[0], (y - dy) * scale[1], (z - dz) * scale[2]]
    high = [(x + width + dx) * scale[0], (y + height + dy) * scale[1], (z + depth + dz) * scale[2]]
    low, high = ([min(a, b) for a, b in zip(low, high)], [max(a, b) for a, b in zip(low, high)])
    result["origin"] = [pivot[0] + low[0], 24.0 - (pivot[1] + high[1]), pivot[2] + low[2]]
    result["size"] = [high[0] - low[0], high[1] - low[1], high[2] - low[2]]
    result["_mcbedrock_uv_size"] = [width, height, depth]
    return result


# -- one call for the importer --------------------------------------------------

@dataclass
class ModEntityModel:
    renderer_class: str
    model_class: str
    model: JavaEntityModel
    texture: str
    render_scale: tuple
    notes: list
    # Render layers (``layer_passes``): what a default entity shows, and the
    # optional ones an eager entity shows (saddles, chests, charge auras).
    layers: list = field(default_factory=list)
    optional_layers: list = field(default_factory=list)


def layer_display_name(layer_class, texture):
    """``LayerTigerEyes`` -> ``Tiger Eyes``; a nameless layer by its texture."""
    simple = layer_class.rsplit("/", 1)[-1].rsplit("$", 1)[-1]
    simple = re.sub(r"^(Layer|Render)(?=[A-Z])|(FeatureRenderer|Feature|Layer|Renderer|GeoLayer)$", "", simple)
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", simple).strip()
    if words:
        return words
    return texture.split(":", 1)[-1].rsplit("/", 1)[-1].rsplit(".", 1)[0].replace("_", " ").title()


def layer_passes(vm, draws, main_object, main_model, optional=False):
    """Plain records of what layers drew, for the builder.

    ``bones`` lists the main model's parts the layer drew (``None``: all of
    them); ``model`` is the part tree of another model a layer drew (a
    saddle or armor model), else ``None``.
    """
    passes = []
    seen = set()
    for draw in draws:
        key = (draw.layer, draw.texture, draw.render_type)
        if not draw.texture or key in seen:
            continue
        seen.add(key)
        # Another instance of the main model's class draws the same geometry.
        same = draw.model is main_object or draw.model == "geo" or (
            isinstance(draw.model, JObject) and isinstance(main_object, JObject)
            and draw.model.class_name == main_object.class_name)
        if same and draw.model is not main_object and isinstance(draw.model, JObject):
            draw.parts = set()  # its parts are other objects: the layer covers the whole model
        bones = None
        other = None
        if same and draw.parts and main_model is not None:
            names = {main_model.sources[part].name for part in draw.parts if part in main_model.sources}
            bones = sorted(names) or None
        elif not same and isinstance(draw.model, JObject):
            try:
                other = read_built_model(vm, draw.model)
            except (ModModelError, JavaThrow, VMError):
                continue
            if draw.parts:
                for object_id, part in other.sources.items():
                    part.visible = object_id in draw.parts
        elif not same:
            continue
        passes.append({
            "layer": draw.layer,
            "name": layer_display_name(draw.layer, draw.texture),
            "texture": draw.texture,
            "render_type": draw.render_type,
            "glow": draw.glow,
            "bones": bones,
            "model": other,
            "optional": optional,
        })
    return passes


def load_layer_model(jar_path_or_class_path, class_name, method_key, library=None, **limits):
    """Build the model a vanilla-style ``LayerDefinition`` factory describes.

    ``method_key`` is the factory's name plus descriptor
    (``createMesh()Lnet/.../LayerDefinition;``).
    """
    from .mod_layer_models import build_layer, model_from_layer

    class_path = shared_class_path(jar_path_or_class_path)
    try:
        parsed = class_path.get(class_name)
        method = next((candidate for candidate in (parsed.methods if parsed else ())
                       if candidate.name + candidate.desc == method_key), None)
        if method is None:
            raise ModModelError(f"{class_name} has no {method_key}")
        vm = new_model_vm(class_path, library=library, **limits)
        model = model_from_layer(build_layer(vm, class_name, method), class_name)
        notes = sorted(f"{owner.rsplit('/', 1)[-1]}.{member}" for owner, member in vm.notes)
        return ModEntityModel("", class_name, model, "", (1.0, 1.0, 1.0), notes)
    except JavaThrow as thrown:
        raise ModModelError(f"model code threw {thrown.value.class_name}") from thrown
    except VMError as exc:
        raise ModModelError(str(exc)) from exc


def drawn_model(vm, renderer, model, entity):
    """The model a renderer's default draw uses: a renderer holding several picks
    one by the entity's state as it draws (a blobfish's gray or pink body)."""
    if renderer is None or not isinstance(model, JObject) or not any(
            isinstance(value, JObject) and value is not model and vm.is_instance(value, MC_MODEL)
            for value in renderer.fields.values()):
        return model
    default_draw(vm, renderer, model, entity)
    current = _renderer_model(renderer)
    return current if isinstance(current, JObject) and vm.is_instance(current, MC_MODEL) else model


def _held_models(vm, renderer, main):
    """``id -> model at rest`` of the other models a renderer keeps in its fields."""
    held = {}
    for value in renderer.fields.values() if renderer is not None else ():
        if (isinstance(value, JObject) and value is not main and id(value) not in held
                and vm.is_instance(value, MC_MODEL)):
            try:
                held[id(value)] = read_built_model(vm, value)
            except (ModModelError, JavaThrow, VMError):
                continue
    return held


def _keep_drawn_ancestors(part):
    """A part the draw skipped whose children it drew (``body.render()`` called
    directly, past the root) stays as an empty holder for them."""
    holds = False
    for child in part.children:
        holds = _keep_drawn_ancestors(child) or holds
    if not part.visible and holds:
        part.visible = True
        part.cubes = []
    return part.visible


def load_entity_model(jar_path_or_class_path, renderer_class="", model_class="", library=None, **limits):
    """Build one mod entity model, from its renderer (preferred) or its class.

    ``library`` (``vanilla_model_library()``, or the function itself to open
    the client JAR only if needed) lets a renderer bake vanilla's own layers
    (``ModelLayers.CREEPER``)."""
    class_path = shared_class_path(jar_path_or_class_path)
    try:
        vm = new_model_vm(class_path, library=library, **limits)
        texture = ""
        render_scale = (1.0, 1.0, 1.0)
        model_object = None
        renderer = None
        entity = Opaque("net/minecraft/world/entity/Entity")
        if renderer_class:
            result = run_renderer(vm, renderer_class)
            renderer, entity = result.renderer, result.entity
            texture = result.texture
            render_scale = result.scale
            # Renderers may swap ``this.model`` in scale() (sizes, pressure,
            # baby/adult); after the scale pass it holds the model drawn.
            current = _renderer_model(renderer) if renderer is not None else None
            model_object = (current if isinstance(current, JObject)
                            and is_citadel_model_class(vm.class_path, current.class_name)
                            else result.model)
            if model_object is None:
                renderer = None
        if model_object is None:
            if not model_class:
                raise ModModelError(f"{renderer_class} has no Citadel model")
            model_object = construct_model(vm, model_class)
        # The rest pose is the constructor's; which parts show is what a
        # default entity's draw shows (no sleep bubble, no extra running legs).
        model = read_built_model(vm, model_object)
        # A renderer holding several models may pick one by the entity's state
        # as it draws (a blobfish's gray or pink body): each is read at rest
        # first, and the one the draw leaves in ``this.model`` is imported.
        held = _held_models(vm, renderer, model_object)
        draws = []
        drawn = default_draw(vm, renderer, model_object, entity, layers=draws)
        current = _renderer_model(renderer) if renderer is not None else None
        if isinstance(current, JObject) and id(current) in held:
            model_object, model = current, held[id(current)]
        if drawn and any(object_id in drawn for object_id in model.sources):
            for object_id, part in model.sources.items():
                part.visible = object_id in drawn
            for root in model.roots:
                _keep_drawn_ancestors(root)
        layers = layer_passes(vm, draws, model_object, model)
        optional = []
        if renderer is not None:
            entity_class = entity.class_name if isinstance(entity, Opaque) else "net/minecraft/world/entity/Entity"
            optional = layer_passes(
                vm, optional_layer_draws(vm, renderer, entity_class, {draw.layer for draw in draws}),
                model_object, model, optional=True)
        notes = sorted(f"{owner.rsplit('/', 1)[-1]}.{member}" for owner, member in vm.notes)
        return ModEntityModel(renderer_class, model.class_name, model, texture, render_scale, notes,
                              layers, optional)
    except JavaThrow as thrown:
        raise ModModelError(f"model code threw {thrown.value.class_name}") from thrown
    except VMError as exc:
        raise ModModelError(str(exc)) from exc
