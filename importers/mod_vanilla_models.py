"""Mobs drawn the vanilla way: a renderer bakes a registered ``LayerDefinition``.

Most mods since 1.17 (NeoForge, Forge and Fabric alike) give each mob a
``LayerDefinition`` factory and register it under a ``ModelLayerLocation``
(``event.registerLayerDefinition``, Fabric's ``EntityModelLayerRegistry``,
or a mod's own helper). The renderer's constructor then calls
``context.bakeLayer(location)`` and hands the baked ``ModelPart`` tree to its
model class (``HierarchicalModel``, ``AgeableListModel``, ``HumanoidModel``
...). This module emulates those pieces for ``java_vm``:

- registrations are found by running the mod's registration methods: any
  call that passes a ``ModelLayerLocation`` with a factory records it;
- ``bakeLayer`` runs the factory (``mod_layer_models``) and bakes the parts,
  which record what a draw shows, like Citadel's parts do;
- vanilla's model base classes render their parts the way the game does.

The geometry imported is the factory's own (the rest pose); the renderer
run supplies the texture, the size, which parts a default mob shows, and
its render layers. Nothing here imports Blender.
"""

from __future__ import annotations

import re

from .java_classes import method_argument_types, method_return_type
from .java_vm import JLambda, JObject, NOT_HANDLED, Opaque, list_items, new_list, opaque_default

GEOM = "net/minecraft/client/model/geom/"
MODEL_PART = GEOM + "ModelPart"
LAYER_LOCATION = GEOM + "ModelLayerLocation"
MODEL_SET = GEOM + "EntityModelSet"
MODEL_LAYERS = GEOM + "ModelLayers"
MATERIAL = "net/minecraft/client/resources/model/Material"
CONTEXT = "net/minecraft/client/renderer/entity/EntityRendererProvider$Context"
BLOCK_CONTEXT = "net/minecraft/client/renderer/blockentity/BlockEntityRendererProvider$Context"
MODEL = "net/minecraft/client/model/Model"
ENTITY_MODEL = "net/minecraft/client/model/EntityModel"
HIERARCHICAL = "net/minecraft/client/model/HierarchicalModel"
AGEABLE_LIST = "net/minecraft/client/model/AgeableListModel"
LIST_MODEL = "net/minecraft/client/model/ListModel"
QUADRUPED = "net/minecraft/client/model/QuadrupedModel"
HUMANOID = "net/minecraft/client/model/HumanoidModel"
_PART_FIELDS = ("x", "y", "z", "xRot", "yRot", "zRot", "xScale", "yScale", "zScale")
# Vanilla models that pick their parts out of the baked root by name.
_NAMED_PARTS = {
    QUADRUPED: (("head", "head"), ("body", "body"), ("rightHindLeg", "right_hind_leg"),
                ("leftHindLeg", "left_hind_leg"), ("rightFrontLeg", "right_front_leg"),
                ("leftFrontLeg", "left_front_leg")),
    HUMANOID: (("head", "head"), ("hat", "hat"), ("body", "body"), ("rightArm", "right_arm"),
               ("leftArm", "left_arm"), ("rightLeg", "right_leg"), ("leftLeg", "left_leg")),
}
_HEAD_PARTS = {QUADRUPED: ("head",), HUMANOID: ("head",)}


def layer_location_text(value):
    if isinstance(value, JObject) and value.class_name == LAYER_LOCATION and isinstance(value.native, dict):
        return value.native.get("text", "")
    return ""


# -- baking ------------------------------------------------------------------------

def bake(vm, layer, factory=None):
    """A ``LayerDefinition`` host baked into ``ModelPart`` hosts; returns the root."""
    from .mod_layer_models import _pose_values

    if not isinstance(layer, JObject) or not isinstance(layer.native, dict) or "mesh" not in layer.native:
        return None
    mesh = layer.native["mesh"]
    if not isinstance(mesh, JObject) or not isinstance(mesh.native, dict):
        return None

    def make(name, definition):
        pose = _pose_values(definition.native["pose"])
        values = {field: float(pose.get(field, 1.0 if field.endswith("Scale") else 0.0))
                  for field in _PART_FIELDS}
        part = JObject(MODEL_PART, fields=dict(values, visible=1, skipDraw=0),
                       native={"name": name, "children": {}, "definition": definition,
                               "initial": values})
        for child_name, child in definition.native["children"].items():
            part.native["children"][child_name] = make(child_name, child)
        return part

    root = make("root", mesh.native["root"])
    vm.baked_layers[id(root)] = (root, layer, factory)
    return root


def _all_parts(part):
    found = [part]
    for child in part.native["children"].values():
        found.extend(_all_parts(child))
    return found


def _render_part(vm, part, args):
    """``ModelPart.render``: a hidden part skips itself and its children; a
    ``skipDraw`` part only skips its own cubes."""
    from .mod_models import begin_draw, end_draw

    if not isinstance(part, JObject) or not part.fields.get("visible", 1):
        return None
    started = None
    if not getattr(vm, "drawing_model", False):
        owner = vm.part_models.get(id(part))
        started = begin_draw(vm, owner if owner is not None else part, args[1] if len(args) > 1 else None)
    try:
        drawn = getattr(vm, "drawn_parts", None)
        if drawn is not None and not part.fields.get("skipDraw", 0):
            drawn.add(id(part))
        for child in part.native["children"].values():
            _render_part(vm, child, args)
    finally:
        end_draw(vm, started)
    return None


def install_vanilla_models(vm):
    """Hosts for layer locations, contexts, baked parts and vanilla model bases."""
    from .mod_models import resource_location_text

    vm.baked_layers = {}   # id(root part) -> (root part, LayerDefinition host, factory)
    vm.part_models = {}    # id(part) -> the model object holding its root
    vm.layer_registry = None  # location text -> factory, built on the first bakeLayer

    vm.host_class(LAYER_LOCATION)

    @vm.method(LAYER_LOCATION, "<init>")
    def location_init(vm, this, args, desc):
        model = resource_location_text(args[0]) if args else ""
        layer = str(args[1]) if len(args) > 1 and isinstance(args[1], str) else "main"
        this.native = {"model": model, "layer": layer, "text": f"{model}#{layer}"}

    vm.method(LAYER_LOCATION, "toString")(lambda vm, this, args, desc: layer_location_text(this))
    vm.method(LAYER_LOCATION, "equals")(
        lambda vm, this, args, desc: 1 if layer_location_text(args[0]) == layer_location_text(this) else 0)
    vm.method(LAYER_LOCATION, "hashCode")(
        lambda vm, this, args, desc: hash(layer_location_text(this)) & 0x7FFFFFFF)
    vm.method(LAYER_LOCATION, None)(lambda vm, this, args, desc: opaque_default(method_return_type(desc)))

    # Vanilla's own locations (ModelLayers.SKELETON): one stable object each,
    # so a mod that registers a model under one bakes it back.
    vm.host_class(MODEL_LAYERS)
    vanilla_locations = {}

    @vm.dispatcher(MODEL_LAYERS, kind="field")
    def vanilla_location(vm, name, desc):
        if desc != f"L{LAYER_LOCATION};":
            return NOT_HANDLED
        if name not in vanilla_locations:
            text = f"minecraft:{name.lower()}#main"
            vanilla_locations[name] = JObject(LAYER_LOCATION, native={
                "model": f"minecraft:{name.lower()}", "layer": "main", "text": text})
        return vanilla_locations[name]

    def bake_layer(vm, this, args, desc):
        location = layer_location_text(args[0]) if args else ""
        factory = registry(vm).get(location) or matching_factory(vm, args[0] if args else None)
        if factory is None:
            vm.notes[(LAYER_LOCATION, f"unregistered {location}")] += 1
            return None
        layer = call_factory(vm, factory)
        return bake(vm, layer, factory)

    for name in (CONTEXT, BLOCK_CONTEXT, MODEL_SET):
        vm.host_class(name)
        vm.method(name, "bakeLayer")(bake_layer)
        vm.method(name, "getModelSet")(lambda vm, this, args, desc: JObject(MODEL_SET))
        vm.method(name, None)(lambda vm, this, args, desc: opaque_default(method_return_type(desc)))

    # Material: a sprite on an atlas (block-entity textures).
    vm.host_class(MATERIAL)

    @vm.method(MATERIAL, "<init>")
    def material_init(vm, this, args, desc):
        this.native = {"atlas": args[0] if args else None, "texture": args[1] if len(args) > 1 else None}

    vm.method(MATERIAL, "texture")(lambda vm, this, args, desc: (this.native or {}).get("texture"))
    vm.method(MATERIAL, "atlasLocation")(lambda vm, this, args, desc: (this.native or {}).get("atlas"))
    vm.method(MATERIAL, None)(lambda vm, this, args, desc: opaque_default(method_return_type(desc)))

    # ModelPart
    vm.host_class(MODEL_PART)

    @vm.method(MODEL_PART, "getChild")
    def get_child(vm, this, args, desc):
        child = this.native["children"].get(str(args[0]))
        if child is None:
            vm.throw("java/util/NoSuchElementException", f"Can't find part {args[0]}")
        return child

    vm.method(MODEL_PART, "hasChild")(
        lambda vm, this, args, desc: 1 if str(args[0]) in this.native["children"] else 0)
    vm.method(MODEL_PART, "render")(lambda vm, this, args, desc: _render_part(vm, this, args))
    vm.method(MODEL_PART, "getAllParts")(
        lambda vm, this, args, desc: (new_list(_all_parts(this))
                                      if method_return_type(desc) == "Ljava/util/List;"
                                      else JObject("java/util/stream/Stream",
                                                   native={"items": _all_parts(this)})))

    def reset_pose(vm, this, args, desc):
        this.fields.update(this.native["initial"])

    vm.method(MODEL_PART, "resetPose")(reset_pose)

    @vm.method(MODEL_PART, "copyFrom")
    def copy_from(vm, this, args, desc):
        source = args[0]
        if isinstance(source, JObject):
            for field in _PART_FIELDS:
                if field in source.fields:
                    this.fields[field] = source.fields[field]

    @vm.method(MODEL_PART, "setPos")
    def set_pos(vm, this, args, desc):
        this.fields["x"], this.fields["y"], this.fields["z"] = (float(value) for value in args[:3])

    @vm.method(MODEL_PART, "setRotation")
    def set_rotation(vm, this, args, desc):
        this.fields["xRot"], this.fields["yRot"], this.fields["zRot"] = (float(value) for value in args[:3])

    vm.method(MODEL_PART, None)(lambda vm, this, args, desc: opaque_default(method_return_type(desc)))

    # Vanilla model bases.
    for name, parent in ((MODEL, "java/lang/Object"), (ENTITY_MODEL, MODEL), (HIERARCHICAL, ENTITY_MODEL),
                         (AGEABLE_LIST, ENTITY_MODEL), (LIST_MODEL, ENTITY_MODEL),
                         (QUADRUPED, AGEABLE_LIST), (HUMANOID, AGEABLE_LIST)):
        vm.host_class(name, parent=parent)

    def model_init(vm, this, args, desc):
        for name in ("attackTime", "riding", "young"):
            this.fields.setdefault(name, 0.0 if name == "attackTime" else 0)
        this.fields.setdefault("young", 0)
        root = next((value for value in args if isinstance(value, JObject) and value.class_name == MODEL_PART), None)
        if root is not None:
            this.native = {"root": root}
            claim(vm, this, root)
            for class_name, fields in _NAMED_PARTS.items():
                if class_name in vm.ancestors(this.class_name):
                    for field, child in fields:
                        if child in root.native["children"]:
                            this.fields[field] = root.native["children"][child]

    for name in (MODEL, ENTITY_MODEL, HIERARCHICAL, AGEABLE_LIST, LIST_MODEL, QUADRUPED, HUMANOID):
        vm.method(name, "<init>")(model_init)

    def render_to_buffer(vm, this, args, desc):
        from .mod_models import begin_draw, end_draw

        started = begin_draw(vm, this, args[1] if len(args) > 1 else None)
        try:
            for part in model_parts(vm, this):
                _render_part(vm, part, args)
        finally:
            end_draw(vm, started)
        return None

    for name in (MODEL, ENTITY_MODEL, HIERARCHICAL, AGEABLE_LIST, LIST_MODEL, QUADRUPED, HUMANOID):
        vm.method(name, "renderToBuffer")(render_to_buffer)
    vm.method(MODEL, "root")(lambda vm, this, args, desc: model_root(vm, this))
    for class_name, heads in _HEAD_PARTS.items():
        vm.method(class_name, "headParts")(
            lambda vm, this, args, desc, heads=heads: new_list([this.fields[field] for field in heads
                                                               if isinstance(this.fields.get(field), JObject)]))
        vm.method(class_name, "bodyParts")(
            lambda vm, this, args, desc, class_name=class_name: new_list([
                this.fields[field] for field, _child in _NAMED_PARTS[class_name]
                if field not in _HEAD_PARTS[class_name] and isinstance(this.fields.get(field), JObject)]))
    for name in (MODEL, ENTITY_MODEL, HIERARCHICAL, AGEABLE_LIST, LIST_MODEL, QUADRUPED, HUMANOID):
        vm.method(name, None)(lambda vm, this, args, desc: opaque_default(method_return_type(desc)))

    vm.call_hooks.append(_registration_hook)


def claim(vm, model, root):
    for part in _all_parts(root):
        vm.part_models.setdefault(id(part), model)


def model_root(vm, model):
    native = model.native if isinstance(model.native, dict) else {}
    if isinstance(native.get("root"), JObject):
        return native["root"]
    return None


def model_parts(vm, model):
    """What a vanilla model's ``renderToBuffer`` draws: ``root()``, else
    ``headParts()`` and ``bodyParts()``, else ``parts()``."""
    chain = vm.ancestors(model.class_name)
    if HIERARCHICAL in chain or MODEL in chain and not ({AGEABLE_LIST, LIST_MODEL} & set(chain)):
        root = vm.invoke_virtual(model.class_name, "root", f"()L{MODEL_PART};", model, [])
        if isinstance(root, JObject):
            return [root]
    if AGEABLE_LIST in chain:
        parts = []
        for name in ("headParts", "bodyParts"):
            parts.extend(list_items(vm.invoke_virtual(model.class_name, name, "()Ljava/lang/Iterable;",
                                                      model, [])) or ())
        return [part for part in parts if isinstance(part, JObject)]
    if LIST_MODEL in chain:
        return [part for part in list_items(vm.invoke_virtual(
            model.class_name, "parts", "()Ljava/lang/Iterable;", model, [])) or ()
            if isinstance(part, JObject)]
    root = model_root(vm, model)
    return [root] if root is not None else []


# -- the layer registry ----------------------------------------------------------

def _registration_hook(vm, owner, name, desc, this, args):
    """A call passing a ``ModelLayerLocation`` and a factory registers it.

    A library helper the JAR does not contain that returns a location
    (PuzzlesLib's ``ModelLayerFactory.register("caped_illager")``) gets a
    unique one: registration and ``bakeLayer`` share the object it lands in.
    """
    if method_return_type(desc) == f"L{LAYER_LOCATION};":
        vm.minted_locations = getattr(vm, "minted_locations", 0) + 1
        words = "/".join(str(value) for value in args if isinstance(value, str)) or name
        text = f"{owner.rsplit('/', 1)[-1]}:{words}#{vm.minted_locations}"
        return JObject(LAYER_LOCATION, native={"model": text, "layer": "main", "text": text})
    registry = getattr(vm, "_collecting_layers", None)
    if registry is None:
        return NOT_HANDLED
    location = next((layer_location_text(value) for value in args if layer_location_text(value)), "")
    factory = next((value for value in args if isinstance(value, JLambda)), None)
    if location and factory is not None:
        registry.setdefault(location, factory)
        return opaque_default(method_return_type(desc))
    return NOT_HANDLED


def registry(vm):
    """``location text -> factory``, from running the mod's registration methods once."""
    if vm.layer_registry is not None:
        return vm.layer_registry
    collected = {}
    vm.layer_registry = collected
    vm._collecting_layers = collected
    vm.lenient_nulls = True
    vm.lenient_depth = vm.depth
    try:
        for class_name, method in _registration_methods(vm.class_path):
            arguments = [vm.default_for(type_desc) for type_desc in method_argument_types(method.desc)]
            try:
                if method.is_static:
                    vm.invoke_static(class_name, method.name, method.desc, arguments)
                else:
                    receiver = Opaque(class_name)
                    instance = _instance(vm, class_name)
                    vm.invoke_virtual(class_name, method.name, method.desc, instance or receiver, arguments)
            except Exception as exc:  # one broken registration must not hide the rest
                vm.notes[(class_name, f"{method.name}: {type(exc).__name__}")] += 1
    finally:
        vm._collecting_layers = None
        vm.lenient_nulls = False
    return collected


def _instance(vm, class_name):
    parsed = vm.class_path.get(class_name)
    if parsed is None or parsed.access & 0x0600:  # interface or abstract
        return None
    constructor = parsed.find_method("<init>", "()V")
    if constructor is None:
        return None
    try:
        return vm.new_object(class_name, "()V", [])
    except Exception:
        return None


def _registration_methods(class_path):
    """Methods that pass a ``ModelLayerLocation`` and a lambda to some register call."""
    found = []
    for class_name in class_path.names():
        parsed = class_path.get(class_name)
        if parsed is None:
            continue
        for method in parsed.methods:
            code = parsed.code(method)
            if code is None or method.name in ("<clinit>",):
                continue
            try:
                instructions = code.instructions
            except ValueError:
                continue
            registers = lambdas = False
            for pc in instructions:
                op, operands, _next = instructions[pc]
                if op.startswith("invoke") and op != "invokedynamic":
                    try:
                        _owner, _name, desc = parsed.member_ref(operands[0])
                    except Exception:
                        continue
                    types = method_argument_types(desc)
                    if f"L{LAYER_LOCATION};" in types and len(types) >= 2:
                        registers = True
                elif op == "invokedynamic":
                    lambdas = True
            if registers and lambdas:
                found.append((class_name, method))
    return found


_MAIN_FACTORY = re.compile(r"^(createBodyLayer|createBodyModel|createLayer|createMesh|create|"
                           r"getTexturedModelData|getModelData|createModelData)$")


def matching_factory(vm, location):
    """The factory a location goes with when its registration was not seen
    (mods that register from a loop over their own map): the LayerDefinition
    factory of the model class the location is named after (``sculk_centipede``
    -> ``SculkCentipedeModel``), ``createBodyLayer``-like for ``main``, else
    the one named after the layer (``outer`` -> ``createOuterLayer``)."""
    from .mod_layer_models import layer_factories

    if not isinstance(location, JObject) or not isinstance(location.native, dict):
        return None
    model = location.native.get("model", "")
    wanted = _name_tokens(model.split(":", 1)[-1].rsplit("/", 1)[-1])
    layer = location.native.get("layer", "main")
    if not wanted:
        return None
    if getattr(vm, "_factories", None) is None:
        vm._factories = layer_factories(vm.class_path)
    layer_tokens = _name_tokens(layer)
    factories = vm._factories
    if model.startswith("minecraft:") and vm.library is not None:
        factories = _vanilla_factories(vm, wanted)  # ModelLayers.CREEPER: vanilla's CreeperModel
    scored = []
    for class_name, method in factories:
        tokens = _name_tokens(class_name.rsplit("/", 1)[-1]) - {"model", "entity", "renderer", "layer"}
        if not wanted <= tokens:
            continue
        extra = tokens - wanted
        if layer == "main":
            # AdultCapybaraModel is the capybara; BabyCapybaraModel is not.
            if "baby" in extra or not _MAIN_FACTORY.match(method.name):
                continue
            extra -= {"adult"}
        else:
            named = extra | _name_tokens(method.name)
            if not layer_tokens <= named:
                continue
            extra -= layer_tokens
        scored.append((len(extra), class_name, method))
    if not scored:
        return None
    scored.sort(key=lambda entry: entry[0])
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None  # two equally good guesses: none
    _score, class_name, method = scored[0]
    return JLambda((6, class_name, method.name, method.desc), [])


def _vanilla_factories(vm, wanted):
    """LayerDefinition factories of vanilla model classes named like ``wanted``."""
    from .mod_layer_models import is_layer_factory

    library = vm.library
    found = []
    for name in library.class_path.names():
        if not name.startswith(library.PREFIX):
            continue
        tokens = _name_tokens(name.rsplit("/", 1)[-1]) - {"model"}
        if not wanted <= tokens:
            continue
        parsed = library.class_path.get(name)
        for method in (parsed.methods if parsed is not None else ()):
            if is_layer_factory(method):
                found.append((name, method))
    return found


def _name_tokens(text):
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", str(text))
    return {token for token in re.split(r"[^a-z0-9]+", text.lower()) if token}


def call_factory(vm, factory):
    """Run a registered factory (a lambda or method reference) for its LayerDefinition."""
    from .mod_layer_models import DEFORMATION, LAYER, _deformation

    # A factory reference with parameters (createBodyLayer(CubeDeformation))
    # gets zero deformation and zeros, as the game's registrations pass.
    _kind, _owner, _name, impl_desc = factory.handle
    missing = method_argument_types(impl_desc)[len(factory.captured):]
    arguments = [_deformation(0.0, 0.0, 0.0) if type_desc == f"L{DEFORMATION};" else vm.default_for(type_desc)
                 for type_desc in missing]
    try:
        result = vm._call_lambda(factory, arguments, f"()L{LAYER};")
    except Exception as exc:
        vm.notes[(LAYER, f"factory: {type(exc).__name__}")] += 1
        return None
    return result if isinstance(result, JObject) and result.class_name == LAYER else None


# -- reading the part tree ---------------------------------------------------------

def baked_root_of(vm, model):
    """``(root part, LayerDefinition, factory)`` of the baked tree a model draws from."""
    if not isinstance(model, JObject):
        return None, None, None
    root = model_root(vm, model)
    if root is not None and id(root) in vm.baked_layers:
        return vm.baked_layers[id(root)]
    held = {id(value) for value in model.fields.values()
            if isinstance(value, JObject) and value.class_name == MODEL_PART}
    for candidate, layer, factory in vm.baked_layers.values():
        if held & {id(part) for part in _all_parts(candidate)}:
            return candidate, layer, factory
    return None, None, None


def model_from_baked(root, layer, class_name):
    """The part tree in its baked (rest) pose, as ``mod_models.JavaEntityModel``,
    with ``sources`` mapping each baked part object to its ``JavaPart``."""
    from .mod_layer_models import model_from_layer

    model = model_from_layer(layer, class_name)
    sources = {}

    def walk(part, java_part):
        sources[id(part)] = java_part
        for child, java_child in zip(part.native["children"].values(), java_part.children):
            walk(child, java_child)

    walk(root, model.roots[0])
    model.sources = sources
    return model


__all__ = [
    "bake", "baked_root_of", "call_factory", "install_vanilla_models", "layer_location_text",
    "model_from_baked", "model_parts", "registry",
]
