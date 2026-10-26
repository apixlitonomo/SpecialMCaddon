"""A default entity's synched data: the values a freshly spawned mob starts with.

The game keeps much of a mob's state in its ``SynchedEntityData``. Each entity
class defines the values and their defaults in ``defineSynchedData`` (1.20.5+:
on a ``SynchedEntityData.Builder``), which vanilla's ``Entity`` constructor
calls, and getters read them back (``entityData.get(VARIANT)``). A renderer
drawing a freshly spawned mob sees those defaults: MCreator's GeckoLib mobs
name their skin this way (``"textures/entities/" + getTexture()``, defined as
``"skinlessnew"``), other mods their variant, size or scale.

The stand-in entity a renderer draws (``java_vm.Opaque``) still runs none of
its own code except the getters that read its synched data, which run against
the defaults its class defines. Everything else it is asked answers its type's
default, as before. The eager stand-in (optional layers) keeps answering true
to boolean getters.

Members are matched by descriptor (Forge and Fabric name them differently);
``defineSynchedData`` reads under Mojang's name through ``intermediary_names``.
Nothing here imports Blender.
"""

from __future__ import annotations

from .java_classes import method_argument_types, method_return_type
from .java_vm import NOT_HANDLED, EagerOpaque, JavaThrow, JObject, Opaque, VMError

SYNCHED_DATA = "net/minecraft/network/syncher/SynchedEntityData"
BUILDER = SYNCHED_DATA + "$Builder"
ACCESSOR = "net/minecraft/network/syncher/EntityDataAccessor"
DEFINE_METHOD = "defineSynchedData"

_SYNCHED_DATA_DESC = f"L{SYNCHED_DATA};"
_GET_DESC = f"(L{ACCESSOR};)Ljava/lang/Object;"  # get
_DEFINE_ID_DESC = f"(Ljava/lang/Class;Lnet/minecraft/network/syncher/EntityDataSerializer;)L{ACCESSOR};"
_BUILDER_DEFINE_DESC = f"(L{ACCESSOR};Ljava/lang/Object;)L{BUILDER};"
_STORE_PREFIX = f"(L{ACCESSOR};Ljava/lang/Object;"  # define, set, set(..., force)


def _values(this):
    native = this.native if isinstance(this, JObject) else None
    return native.get("values") if isinstance(native, dict) else None


def install_entity_data(vm):
    """``SynchedEntityData`` hosts and the stand-in hooks that read it."""
    vm.entity_data = {}  # entity class -> the SynchedEntityData it spawns with
    vm.synched_getters = {}  # (class, name, desc) -> whether the method reads synched data
    vm.host_class(SYNCHED_DATA)
    vm.host_class(BUILDER)
    accessors = []

    @vm.dispatcher(SYNCHED_DATA, kind="static")
    def define_id(vm, this, name, args, desc):
        if desc != _DEFINE_ID_DESC:
            return NOT_HANDLED
        accessors.append(JObject(ACCESSOR, native={"id": len(accessors)}))
        return accessors[-1]

    @vm.dispatcher(SYNCHED_DATA)
    def synched_data(vm, this, name, args, desc):
        values = _values(this)
        if values is None:
            return NOT_HANDLED
        if desc == _GET_DESC:
            key = id(args[0])
            return values[key][1] if key in values else vm.default_for("Ljava/lang/Object;")
        if desc.startswith(_STORE_PREFIX) and desc.endswith(")V"):
            values[id(args[0])] = (args[0], args[1])  # the accessor is kept alive with its value
            return None
        return NOT_HANDLED

    @vm.dispatcher(BUILDER)
    def builder(vm, this, name, args, desc):
        data = this.native.get("data") if isinstance(this.native, dict) else None
        if data is None:
            return NOT_HANDLED
        if desc == _BUILDER_DEFINE_DESC:
            _values(data)[id(args[0])] = (args[0], args[1])
            return this
        if desc == f"(){_SYNCHED_DATA_DESC}":
            return data
        return NOT_HANDLED

    vm.field_hooks.append(_synched_data_field)
    vm.stand_in_hooks.append(_synched_getter)


def entity_data(vm, class_name):
    """The ``SynchedEntityData`` a freshly spawned ``class_name`` holds."""
    data = vm.entity_data.get(class_name)
    if data is None:
        data = vm.entity_data[class_name] = JObject(SYNCHED_DATA, native={"values": {}})
        _define_defaults(vm, class_name, data)
    return data


def _define_defaults(vm, class_name, data):
    """What vanilla's ``Entity`` constructor does: the class's ``defineSynchedData``
    on a new ``SynchedEntityData`` (1.20.5+: on a builder that becomes it)."""
    stand_in = Opaque(class_name)
    for desc, arguments in (("()V", []), (f"(L{BUILDER};)V", [JObject(BUILDER, native={"data": data})])):
        owner = _declaring_class(vm, class_name, DEFINE_METHOD, desc)
        if not owner:
            continue
        try:
            vm.invoke_special(owner, DEFINE_METHOD, desc, stand_in, arguments)
        except (JavaThrow, VMError) as exc:
            vm.notes[(class_name, f"{DEFINE_METHOD}: {exc}")] += 1
        return


def _declaring_class(vm, class_name, name, desc):
    """The JAR class ``class_name`` inherits a method with code from, or ``""``."""
    for current in vm.class_path.superclasses(class_name):
        parsed = vm.class_path.get(current)
        if parsed is None:
            return ""
        method = parsed.find_method(name, desc)
        if method is not None:
            return current if not method.is_abstract and not method.is_static and parsed.code(method) else ""
    return ""


def _reads_synched_data(vm, class_name, name, desc):
    key = (class_name, name, desc)
    if key not in vm.synched_getters:
        reads = False
        parsed = vm.class_path.get(class_name)
        method = parsed.find_method(name, desc) if parsed is not None else None
        code = parsed.code(method) if method is not None else None
        try:
            instructions = code.instructions if code is not None else {}
        except ValueError:
            instructions = {}
        for operation, operands, _next in instructions.values():
            if operation.startswith("invoke") and operation != "invokedynamic":
                owner, _name, member_desc = parsed.member_ref(operands[0])
                if owner == SYNCHED_DATA and member_desc == _GET_DESC:
                    reads = True
                    break
        vm.synched_getters[key] = reads
    return vm.synched_getters[key]


def _synched_data_field(vm, target, owner, name, desc):
    """``this.entityData`` on a stand-in entity: the data its class spawns with."""
    if desc != _SYNCHED_DATA_DESC or not isinstance(target, Opaque) or target.class_name not in vm.class_path:
        return NOT_HANDLED
    return entity_data(vm, target.class_name)


def _synched_getter(vm, this, owner, name, desc, args):
    """A stand-in's own getter that reads its synched data runs against the
    defaults; ``getEntityData()`` gives them."""
    if this.class_name not in vm.class_path or name.startswith("<"):
        return NOT_HANDLED
    returns = method_return_type(desc)
    if returns == _SYNCHED_DATA_DESC and not method_argument_types(desc):
        return entity_data(vm, this.class_name)
    if returns == "Z" and isinstance(this, EagerOpaque):
        return NOT_HANDLED  # the optional-layer pass keeps answering true
    declaring = _declaring_class(vm, this.class_name, name, desc)
    if not declaring or not _reads_synched_data(vm, declaring, name, desc):
        return NOT_HANDLED
    entity_data(vm, this.class_name)
    try:
        return vm.invoke_special(declaring, name, desc, this, list(args))
    except (JavaThrow, VMError) as exc:
        vm.notes[(declaring, f"{name}: {exc}")] += 1
        return NOT_HANDLED


__all__ = ["ACCESSOR", "BUILDER", "DEFINE_METHOD", "SYNCHED_DATA", "entity_data", "install_entity_data"]
