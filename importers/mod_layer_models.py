"""Models built with vanilla's ``LayerDefinition`` API, read from a mod JAR.

Since 1.17 most mods describe block-entity and entity models the way vanilla
does: a static ``createBodyLayer()`` adds ``CubeListBuilder`` boxes to a
``MeshDefinition`` at ``PartPose`` offsets and returns
``LayerDefinition.create(mesh, width, height)``. The game bakes that into
``ModelPart``s, which draw like Citadel's boxes (``ModelPart.Cube`` keeps
fractional box-UV sizes, parts turn Z then Y then X), so the parts land in
``mod_models``' part tree and import the same way. One difference: a
``ModelPart``'s scale also applies to its children.

Builder calls are matched by descriptor, so Mojang names (NeoForge, 26.x)
and Forge's SRG names both work. Only ``PartPose``'s two (float, float,
float) factories, ``offset`` and ``rotation``, are told apart by name.
Nothing here imports Blender.
"""

from __future__ import annotations

import re

from .java_classes import method_argument_types, method_return_type
from .java_vm import JObject, NOT_HANDLED, Opaque, f32, opaque_default

GEOM = "net/minecraft/client/model/geom/"
BUILDERS = GEOM + "builders/"
MESH = BUILDERS + "MeshDefinition"
PART = BUILDERS + "PartDefinition"
CUBES = BUILDERS + "CubeListBuilder"
DEFORMATION = BUILDERS + "CubeDeformation"
LAYER = BUILDERS + "LayerDefinition"
POSE = GEOM + "PartPose"
LAYER_TYPE = f"L{LAYER};"

_POSE_ROTATION_NAMES = {"rotation", "m_171430_"}
_POSE_FIELDS = ("x", "y", "z", "xRot", "yRot", "zRot", "xScale", "yScale", "zScale")
_STRING = "Ljava/lang/String;"


def _pose(x=0.0, y=0.0, z=0.0, x_rot=0.0, y_rot=0.0, z_rot=0.0, x_scale=1.0, y_scale=1.0, z_scale=1.0):
    values = (x, y, z, x_rot, y_rot, z_rot, x_scale, y_scale, z_scale)
    return JObject(POSE, fields={name: f32(value) for name, value in zip(_POSE_FIELDS, values)})


def _pose_values(pose):
    if not isinstance(pose, JObject) or pose.class_name != POSE:
        return _pose().fields
    return pose.fields


def _part(cubes=(), pose=None):
    return JObject(PART, native={"cubes": list(cubes), "pose": pose or _pose(), "children": {}})


def _grow(deformation):
    if isinstance(deformation, JObject) and isinstance(deformation.native, dict):
        return deformation.native.get("grow", (0.0, 0.0, 0.0))
    return (0.0, 0.0, 0.0)


def _deformation(x, y, z):
    return JObject(DEFORMATION, native={"grow": (f32(x), f32(y), f32(z))})


def _builder_state(this):
    if not isinstance(this.native, dict):
        this.native = {"cubes": [], "u": 0, "v": 0, "mirror": False}
    return this.native


def _add_box(vm, this, args, types):
    """Every ``CubeListBuilder.addBox`` overload, told apart by its arguments."""
    state = _builder_state(this)
    if types and types[0] == _STRING:
        args, types = args[1:], types[1:]
    u, v, mirror = state["u"], state["v"], state["mirror"]
    grow = (0.0, 0.0, 0.0)
    scale = (1.0, 1.0)
    x, y, z, width, height, depth = (float(value) for value in args[:6])
    rest, rest_types = args[6:], types[6:]
    if rest_types[:1] == ["Z"]:
        mirror = bool(rest[0])
    elif rest_types[:1] == [f"L{DEFORMATION};"]:
        grow = _grow(rest[0])
        if rest_types[1:3] == ["F", "F"]:
            scale = (float(rest[1]), float(rest[2]))
        elif rest_types[1:3] == ["I", "I"]:
            # addBox(name, x, y, z, int w, int h, int d, deformation, u, v)
            u, v = int(rest[1]), int(rest[2])
    state["cubes"].append((int(u), int(v), (f32(x), f32(y), f32(z)),
                           (f32(width), f32(height), f32(depth)), grow, bool(mirror), scale))
    return this


def install_layer_api(vm):
    for name in (MESH, PART, CUBES, DEFORMATION, LAYER, POSE):
        vm.host_class(name)

    @vm.method(MESH, "<init>")
    def mesh_init(vm, this, args, desc):
        this.native = {"root": _part()}

    @vm.dispatcher(MESH)
    def mesh_method(vm, this, name, args, desc):
        if desc == f"()L{PART};":  # getRoot
            return this.native["root"]
        return NOT_HANDLED

    @vm.dispatcher(PART)
    def part_method(vm, this, name, args, desc):
        children = this.native["children"]
        if desc == f"({_STRING}L{CUBES};L{POSE};)L{PART};":  # addOrReplaceChild
            child_name, builder, pose = args
            cubes = _builder_state(builder)["cubes"] if isinstance(builder, JObject) else []
            child = _part(cubes, pose if isinstance(pose, JObject) else _pose())
            children[str(child_name)] = child
            return child
        if desc == f"({_STRING})L{PART};":  # getChild
            child = children.get(str(args[0]))
            if child is None:
                vm.throw("java/util/NoSuchElementException", str(args[0]))
            return child
        return NOT_HANDLED

    @vm.dispatcher(CUBES, kind="static")
    def cubes_static(vm, this, name, args, desc):
        if desc == f"()L{CUBES};":  # create
            return JObject(CUBES, native={"cubes": [], "u": 0, "v": 0, "mirror": False})
        return NOT_HANDLED

    @vm.dispatcher(CUBES)
    def cubes_method(vm, this, name, args, desc):
        if method_return_type(desc) != f"L{CUBES};":
            return NOT_HANDLED
        types = method_argument_types(desc)
        state = _builder_state(this)
        if types == ["I", "I"]:  # texOffs
            state["u"], state["v"] = int(args[0]), int(args[1])
            return this
        if types == []:  # mirror()
            state["mirror"] = True
            return this
        if types == ["Z"]:  # mirror(boolean)
            state["mirror"] = bool(args[0])
            return this
        if len([t for t in types if t in ("F", "I")]) >= 6:  # addBox
            return _add_box(vm, this, args, types)
        return NOT_HANDLED

    @vm.method(DEFORMATION, "<init>")
    def deformation_init(vm, this, args, desc):
        values = [float(value) for value in args] or [0.0]
        if len(values) == 1:
            values = values * 3
        this.native = {"grow": tuple(f32(value) for value in values[:3])}

    @vm.dispatcher(DEFORMATION)
    def deformation_method(vm, this, name, args, desc):
        if method_return_type(desc) == f"L{DEFORMATION};":  # extend(...)
            values = [float(value) for value in args]
            if len(values) == 1:
                values = values * 3
            grow = _grow(this)
            return _deformation(*(grow[axis] + values[axis] for axis in range(3)))
        return NOT_HANDLED

    @vm.dispatcher(DEFORMATION, kind="field")
    def deformation_field(vm, name, desc):
        # NONE (SRG f_171458_); any other constant is a zero deformation too.
        return _deformation(0.0, 0.0, 0.0) if desc == f"L{DEFORMATION};" else NOT_HANDLED

    @vm.dispatcher(POSE, kind="static")
    def pose_static(vm, this, name, args, desc):
        types = method_argument_types(desc)
        if method_return_type(desc) != f"L{POSE};" or any(t != "F" for t in types):
            return NOT_HANDLED
        values = [float(value) for value in args]
        if len(values) == 3:
            if name in _POSE_ROTATION_NAMES:
                return _pose(x_rot=values[0], y_rot=values[1], z_rot=values[2])
            return _pose(*values)  # offset (SRG m_171419_)
        if len(values) == 6:  # offsetAndRotation
            return _pose(*values)
        if len(values) == 9:
            return _pose(*values)
        return NOT_HANDLED

    @vm.method(POSE, "<init>")
    def pose_init(vm, this, args, desc):
        # The 1.21.2+ record constructor: offset, rotation, scale.
        this.fields.update(_pose(*[float(value) for value in args]).fields)

    @vm.dispatcher(POSE)
    def pose_method(vm, this, name, args, desc):
        values = dict(_pose_values(this))
        numbers = [float(value) for value in args if isinstance(value, (int, float))]
        if name == "translated" and len(numbers) == 3:
            return _pose(values["x"] + numbers[0], values["y"] + numbers[1], values["z"] + numbers[2],
                         values["xRot"], values["yRot"], values["zRot"],
                         values["xScale"], values["yScale"], values["zScale"])
        if name in ("withScale", "scaled") and numbers:
            factors = numbers * 3 if len(numbers) == 1 else numbers
            if name == "scaled":
                factors = [values[axis + "Scale"] * factor for axis, factor in zip("xyz", factors)]
            return _pose(values["x"], values["y"], values["z"],
                         values["xRot"], values["yRot"], values["zRot"], *factors[:3])
        return NOT_HANDLED

    @vm.dispatcher(POSE, kind="field")
    def pose_field(vm, name, desc):
        return _pose() if desc == f"L{POSE};" else NOT_HANDLED  # ZERO

    @vm.dispatcher(LAYER, kind="static")
    def layer_static(vm, this, name, args, desc):
        if desc == f"(L{MESH};II)L{LAYER};":  # create
            return JObject(LAYER, native={"mesh": args[0], "size": (float(args[1]), float(args[2]))})
        return NOT_HANDLED

    @vm.dispatcher(LAYER)
    def layer_method(vm, this, name, args, desc):
        if method_return_type(desc) == f"L{LAYER};":  # apply(MeshTransformer) and similar
            return this
        return opaque_default(method_return_type(desc))


def is_layer_factory(method):
    """A static method that returns a LayerDefinition and needs no real input.

    Compiler-made lambda bodies (``lambda$register$0``) only forward to the
    real factories, so they are left out.
    """
    if not method.is_static or not method.desc.endswith(")" + LAYER_TYPE):
        return False
    if method.access & 0x1000 or method.name.startswith("lambda$"):
        return False
    return all(t in ("Z", "I", "F", f"L{DEFORMATION};") for t in method_argument_types(method.desc))


def layer_factories(class_path):
    """``(class name, method)`` for every LayerDefinition factory in the JAR."""
    found = []
    for name in class_path.names():
        parsed = class_path.get(name)
        if parsed is None:
            continue
        for method in parsed.methods:
            if is_layer_factory(method):
                found.append((name, method))
    return found


def factory_display_name(class_name, method_name):
    """``GlobeBlockTileRenderer.createBaseMesh`` -> ``Globe (Base)``."""
    simple = class_name.rsplit("/", 1)[-1].rsplit("$", 1)[-1]
    owner = re.sub(r"(BlockTileRenderer|TileRenderer|BlockEntityRenderer|Renderer|Model|Layer|Registry)$",
                   "", simple) or simple
    owner = re.sub(r"^Model(?=[A-Z])", "", owner)
    owner = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", owner).strip()
    detail = re.sub(r"^(create|get|make|build)", "", method_name)
    detail = re.sub(r"(BodyLayer|Layer|Mesh|Definition|Model)$", "", detail)
    detail = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", detail).strip()
    if simple.endswith("Registry") or owner in ("Client", "Mod Client", "Client Setup"):
        return detail or owner
    return f"{owner} ({detail})" if detail else owner


def build_layer(vm, class_name, method):
    """Run a LayerDefinition factory; returns the LayerDefinition host object."""
    arguments = [vm.default_for(type_desc) if type_desc != f"L{DEFORMATION};"
                 else _deformation(0.0, 0.0, 0.0)
                 for type_desc in method_argument_types(method.desc)]
    return vm.invoke_static(class_name, method.name, method.desc, arguments)


def model_from_layer(layer, class_name):
    """The part tree of a LayerDefinition, as ``mod_models.JavaEntityModel``."""
    from .mod_models import JavaCube, JavaEntityModel, JavaPart, ModModelError

    if not isinstance(layer, JObject) or layer.class_name != LAYER:
        raise ModModelError(f"{class_name} did not return a LayerDefinition")
    mesh = layer.native["mesh"]
    if not isinstance(mesh, JObject) or not isinstance(mesh.native, dict):
        raise ModModelError(f"{class_name} built no mesh")
    width, height = layer.native["size"]
    used = set()

    def unique(name):
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "part"
        candidate, suffix = name, 2
        while candidate.casefold() in used:
            candidate, suffix = f"{name}_{suffix}", suffix + 1
        used.add(candidate.casefold())
        return candidate

    def convert(name, definition):
        pose = _pose_values(definition.native["pose"])
        part = JavaPart(
            name=unique(name),
            pivot=(float(pose["x"]), float(pose["y"]), float(pose["z"])),
            rotation=(float(pose["xRot"]), float(pose["yRot"]), float(pose["zRot"])),
            scale=(float(pose["xScale"]), float(pose["yScale"]), float(pose["zScale"])),
            # A baked ModelPart's scale also applies to its children.
            scale_children=True,
            cubes=[JavaCube(u, v, origin, size, grow, mirror, (width * scale[0], height * scale[1]))
                   for u, v, origin, size, grow, mirror, scale in definition.native["cubes"]],
        )
        part.children = [convert(child_name, child)
                         for child_name, child in definition.native["children"].items()]
        return part

    root = convert("root", mesh.native["root"])
    return JavaEntityModel(class_name=class_name, texture_size=(width, height), roots=[root])


__all__ = [
    "LAYER", "LAYER_TYPE", "build_layer", "factory_display_name", "install_layer_api",
    "is_layer_factory", "layer_factories", "model_from_layer", "Opaque",
]
