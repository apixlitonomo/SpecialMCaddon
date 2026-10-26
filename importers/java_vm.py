"""A small, bounded interpreter for the Java code that builds mod models.

Mods describe entity models as code: a constructor that creates parts, places
them and adds boxes. ``JavaVM`` runs that code from a JAR's class files so the
geometry can be read back, without a JVM. It is deliberately small:

- Classes from the JAR run as written. Library classes the code calls into
  (Citadel, Minecraft, Java collections) are *host classes*: Python
  implementations registered by the caller (see ``mod_models``) plus the
  ``java.lang`` basics registered here.
- Anything else is *opaque*: calls on it return the type's default (0,
  false, an empty string, or another opaque object), so incidental calls such
  as ``Minecraft.getInstance()`` never stop a model from building.
- A step and call-depth budget bounds every run. Nothing touches the file
  system or Blender.
"""

from __future__ import annotations

import collections
import math
import struct
from decimal import Decimal

from .java_classes import (
    ClassFormatError,
    VanillaModelLibrary,
    is_wide_type,
    method_argument_types,
    method_return_type,
)


class VMError(RuntimeError):
    """The code cannot be interpreted."""


class VMLimitError(VMError):
    """The run went over its step or call-depth budget."""


class JavaThrow(Exception):
    """A Java exception in flight. ``value`` is the thrown object."""

    def __init__(self, value):
        super().__init__(getattr(value, "class_name", "java/lang/Throwable"))
        self.value = value


# -- values ------------------------------------------------------------------

class JLong(int):
    """A Java ``long`` (takes two slots, like ``double``)."""

    __slots__ = ()


class JDouble(float):
    """A Java ``double``. Plain Python floats hold Java ``float`` values."""

    __slots__ = ()


class JObject:
    """An object: its runtime class, its fields, and host-side state."""

    __slots__ = ("class_name", "fields", "native")

    def __init__(self, class_name, fields=None, native=None):
        self.class_name = class_name
        self.fields = {} if fields is None else fields
        self.native = native

    def __repr__(self):
        return f"<{self.class_name.rsplit('/', 1)[-1]} object>"


class JArray:
    __slots__ = ("component", "items")

    def __init__(self, component, items):
        self.component = component
        self.items = items


class Opaque:
    """Stand-in for an object of a class that is neither loaded nor emulated."""

    __slots__ = ("class_name",)

    def __init__(self, class_name="java/lang/Object"):
        self.class_name = class_name

    def __repr__(self):
        return f"<opaque {self.class_name.rsplit('/', 1)[-1]}>"


class EagerOpaque(Opaque):
    """A stand-in that answers ``true`` to boolean getters (saddled, tamed, ...).

    Getters named in ``false_names`` still answer false, so a layer that
    hides the model for an invisible or sleeping entity keeps drawing.
    """

    __slots__ = ("false_names",)

    def __init__(self, class_name="java/lang/Object", false_names=()):
        super().__init__(class_name)
        self.false_names = frozenset(false_names)


class JLambda:
    """A lambda or method reference from ``LambdaMetafactory``."""

    __slots__ = ("handle", "captured")

    def __init__(self, handle, captured):
        self.handle = handle
        self.captured = captured


_TOP = object()  # second local slot of a long/double
NOT_HANDLED = object()

_MASK32 = 0xFFFFFFFF
_MASK64 = 0xFFFFFFFFFFFFFFFF


def wrap_int(value):
    value = int(value) & _MASK32
    return value - 0x100000000 if value & 0x80000000 else value


def wrap_long(value):
    value = int(value) & _MASK64
    return JLong(value - 0x10000000000000000 if value & 0x8000000000000000 else value)


def f32(value):
    """Round to the nearest Java ``float``."""
    value = float(value)
    if math.isnan(value) or math.isinf(value):
        return value
    try:
        return struct.unpack("<f", struct.pack("<f", value))[0]
    except (OverflowError, struct.error):
        return math.copysign(math.inf, value)


def _float_to_int(value, bits):
    if math.isnan(value):
        return 0
    limit = (1 << (bits - 1))
    if value >= limit:
        return limit - 1
    if value <= -limit:
        return -limit
    return int(value)


def _int_divide(left, right):
    quotient = abs(left) // abs(right)
    return quotient if (left >= 0) == (right >= 0) else -quotient


def _float_divide(left, right):
    try:
        return left / right
    except ZeroDivisionError:
        if left == 0 or math.isnan(left):
            return math.nan
        return math.copysign(math.inf, left) * math.copysign(1.0, right)


def _float_remainder(left, right):
    if right == 0 or math.isinf(left) or math.isnan(left) or math.isnan(right):
        return math.nan
    if math.isinf(right):
        return left
    return math.fmod(left, right)


def _compare(left, right, nan_result):
    if math.isnan(left) or math.isnan(right):
        return nan_result
    return (left > right) - (left < right)


_BOXED_DEFAULTS = {
    "java/lang/Integer": 0, "java/lang/Short": 0, "java/lang/Byte": 0,
    "java/lang/Character": 0, "java/lang/Boolean": 0, "java/lang/Long": JLong(0),
    "java/lang/Float": 0.0, "java/lang/Double": JDouble(0.0),
}


def field_default(type_desc):
    """The value of a field or array element nobody has written yet."""
    code = type_desc[:1]
    if code in "IZBCS":
        return 0
    if code == "J":
        return JLong(0)
    if code == "F":
        return 0.0
    if code == "D":
        return JDouble(0.0)
    return None


def opaque_default(type_desc):
    """What an unknown method or field of a given type yields.

    Object results are opaque stand-ins rather than ``null``, so chains of
    incidental calls (``entity.getLevel().getGameTime()``) keep going.
    """
    code = type_desc[:1]
    if code == "V":
        return None
    if code == "L":
        name = type_desc[1:-1]
        if name == "java/lang/String":
            return ""
        if name in _BOXED_DEFAULTS:
            return _BOXED_DEFAULTS[name]
        return Opaque(name)
    if code == "[":
        return JArray(type_desc[1:], [])
    return field_default(type_desc)


def java_string(value, type_desc="Ljava/lang/Object;"):
    """``String.valueOf`` for interpreter values."""
    code = type_desc[:1]
    if code == "Z":
        return "true" if value else "false"
    if code == "C":
        return chr(int(value) & 0xFFFF)
    if code in "IBSJ":
        return str(int(value))
    if code == "F":
        return _java_float_text(float(value), single=True)
    if code == "D":
        return _java_float_text(float(value), single=False)
    if value is None:
        return "null"
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, JDouble):
        return _java_float_text(float(value), single=False)
    if isinstance(value, float):
        return _java_float_text(value, single=True)
    if isinstance(value, int):
        return str(int(value))
    if isinstance(value, JObject):
        native = value.native
        if isinstance(native, dict) and "string" in native:
            return str(native["string"])
        if isinstance(native, dict) and "enum_name" in native:
            return str(native["enum_name"])
        return value.class_name.replace("/", ".") + "@0"
    return str(value)


def _java_float_text(value, single):
    """``Float.toString`` / ``Double.toString``: shortest digits that round-trip.

    Plain notation from 10^-3 up to 10^7, otherwise ``d.dddE<n>``, always
    with a digit after the point (``1.0``, ``1.0E-5``, ``1.2345678E7``).
    """
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "-0.0" if math.copysign(1.0, value) < 0 else "0.0"
    magnitude = abs(value)
    if single:
        text = next(
            candidate for candidate in (f"{magnitude:.{places}e}" for places in range(9))
            if f32(float(candidate)) == magnitude)
    else:
        text = repr(magnitude)
    _sign, digits, exponent = Decimal(text).normalize().as_tuple()
    digits = "".join(map(str, digits))
    scientific = len(digits) - 1 + exponent
    sign = "-" if value < 0 else ""
    if 1e-3 <= magnitude < 1e7:
        if exponent >= 0:
            return f"{sign}{digits}{'0' * exponent}.0"
        whole = digits[:len(digits) + exponent] or "0"
        fraction = ("0" * max(0, -(len(digits) + exponent))) + digits[max(0, len(digits) + exponent):]
        return f"{sign}{whole}.{fraction}"
    return f"{sign}{digits[0]}.{digits[1:] or '0'}E{scientific}"


def java_hash(text):
    """``String.hashCode`` over UTF-16 code units."""
    encoded = str(text).encode("utf-16-be", "surrogatepass")
    result = 0
    for unit in struct.unpack(f">{len(encoded) // 2}H", encoded):
        result = (31 * result + unit) & _MASK32
    return wrap_int(result)


class _Frame:
    __slots__ = ("class_file", "method", "code", "locals", "stack")

    def __init__(self, class_file, method, code, arguments):
        self.class_file = class_file
        self.method = method
        self.code = code
        # Slot sizes come from the descriptor: a boxed Double or Long is held
        # as a JDouble/JLong value but takes one slot, like any reference.
        types = method_argument_types(method.desc)
        if not method.is_static:
            types = ["L"] + types
        self.locals = [None] * max(code.max_locals, len(arguments) * 2)
        index = 0
        for position, value in enumerate(arguments):
            self.locals[index] = value
            index += 1
            wide = (types[position] in ("J", "D") if position < len(types)
                    else isinstance(value, (JLong, JDouble)))
            if wide:
                self.locals[index] = _TOP
                index += 1
        self.stack = []


class _Return:
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


# Exceptions the interpreter itself raises, and their ancestors.
_THROWABLE_PARENTS = {
    "java/lang/Throwable": "java/lang/Object",
    "java/lang/Exception": "java/lang/Throwable",
    "java/lang/Error": "java/lang/Throwable",
    "java/lang/RuntimeException": "java/lang/Exception",
    "java/lang/NullPointerException": "java/lang/RuntimeException",
    "java/lang/ArithmeticException": "java/lang/RuntimeException",
    "java/lang/ClassCastException": "java/lang/RuntimeException",
    "java/lang/IllegalArgumentException": "java/lang/RuntimeException",
    "java/lang/IllegalStateException": "java/lang/RuntimeException",
    "java/lang/UnsupportedOperationException": "java/lang/RuntimeException",
    "java/lang/IndexOutOfBoundsException": "java/lang/RuntimeException",
    "java/lang/ArrayIndexOutOfBoundsException": "java/lang/IndexOutOfBoundsException",
    "java/lang/NegativeArraySizeException": "java/lang/RuntimeException",
    "java/lang/LinkageError": "java/lang/Error",
    "java/lang/IncompatibleClassChangeError": "java/lang/LinkageError",
    "java/lang/NoSuchFieldError": "java/lang/IncompatibleClassChangeError",
    "java/lang/NoSuchMethodError": "java/lang/IncompatibleClassChangeError",
}


class JavaVM:
    """Interpret methods of one JAR's classes against registered hosts."""

    def __init__(self, class_path, max_steps=4_000_000, max_depth=96, library=None):
        self.class_path = class_path
        # Optional ``java_classes.VanillaModelLibrary`` for vanilla mesh
        # factories, or a function giving one: called when the VM first needs
        # it, as opening a client JAR takes a while and most mods never do.
        self._library = library
        self.max_steps = int(max_steps)
        self.max_depth = int(max_depth)
        self.steps = 0
        self.depth = 0
        self.statics = {}
        self._initialized = set()
        self.host_parents = dict(_THROWABLE_PARENTS)
        self.host_interfaces = {}
        self._host_methods = {}
        self._host_statics = {}
        self._host_fields = {}
        # Per-class handlers that see the member name and descriptor, for
        # APIs whose names differ between loaders (SRG and Mojang names).
        self._dispatch = {}
        self._static_dispatch = {}
        self._field_dispatch = {}
        # (owner, member) of calls that fell through to opaque defaults.
        self.notes = collections.Counter()
        # ``hook(vm, owner, name, desc, this, args)`` for calls no code or host
        # answers (and calls on stand-ins); return ``NOT_HANDLED`` to pass.
        self.call_hooks = []
        # ``hook(vm, this, owner, name, desc, args)`` for virtual calls on
        # stand-ins, and ``hook(vm, target, owner, name, desc)`` for fields
        # read from them, before they answer their type's default (a default
        # entity's synched data, ``mod_entity_data``).
        self.stand_in_hooks = []
        self.field_hooks = []
        # Calls and field reads on null answer defaults instead of throwing,
        # and calls below ``lenient_depth`` that throw answer their default.
        self.lenient_nulls = False
        self.lenient_depth = 0
        _install_java_lang(self)

    @property
    def library(self):
        if callable(self._library):
            self._library = self._library()
        return self._library

    # -- registration --------------------------------------------------------

    def host_class(self, name, parent="java/lang/Object", interfaces=()):
        self.host_parents[name] = parent
        if interfaces:
            self.host_interfaces[name] = tuple(interfaces)

    def is_host_class(self, name):
        return name in self.host_parents

    def method(self, owner, name, desc=None):
        """Decorator: implement ``owner.name`` (any descriptor when ``None``)."""
        def register(function):
            self._host_methods[(owner, name, desc)] = function
            return function
        return register

    def static(self, owner, name, desc=None):
        def register(function):
            self._host_statics[(owner, name, desc)] = function
            return function
        return register

    def static_field(self, owner, name, value):
        self._host_fields[(owner, name)] = value

    def dispatcher(self, owner, kind="method"):
        """Decorator: ``function(vm, this, name, args, desc)`` for ``owner``.

        ``kind`` is ``"method"``, ``"static"`` (no ``this``) or ``"field"``
        (``function(vm, name, desc)`` for static fields). Return
        ``NOT_HANDLED`` to fall through to the usual lookup.
        """
        table = {"method": self._dispatch, "static": self._static_dispatch,
                 "field": self._field_dispatch}[kind]

        def register(function):
            table[owner] = function
            return function
        return register

    # -- object model --------------------------------------------------------

    def parent_of(self, name):
        parsed = self.class_path.get(name) if name in self.class_path else None
        if parsed is not None:
            return parsed.super_name
        if name == "java/lang/Object":
            return None
        return self.host_parents.get(name)

    def _interfaces_of(self, name):
        parsed = self.class_path.get(name) if name in self.class_path else None
        if parsed is not None:
            return parsed.interfaces
        return self.host_interfaces.get(name, ())

    def ancestors(self, name):
        """Every class and interface ``name`` is assignable to that we know of."""
        result = []
        seen = set()
        pending = [name]
        while pending:
            current = pending.pop(0)
            if not current or current in seen:
                continue
            seen.add(current)
            result.append(current)
            parent = self.parent_of(current)
            if parent:
                pending.append(parent)
            pending.extend(self._interfaces_of(current))
        if "java/lang/Object" not in seen:
            result.append("java/lang/Object")
        return result

    def is_instance(self, value, class_name):
        if value is None:
            return False
        if class_name == "java/lang/Object":
            return True
        if isinstance(value, str):
            return class_name in ("java/lang/String", "java/lang/CharSequence",
                                  "java/lang/Comparable", "java/io/Serializable")
        if isinstance(value, JArray):
            return class_name.startswith("[") or class_name in (
                "java/lang/Cloneable", "java/io/Serializable")
        if isinstance(value, (JObject, Opaque)):
            return class_name in self.ancestors(value.class_name)
        if isinstance(value, JLambda):
            return True
        if isinstance(value, bool):
            return class_name == "java/lang/Boolean"
        if isinstance(value, JDouble):
            return class_name in ("java/lang/Double", "java/lang/Number")
        if isinstance(value, float):
            return class_name in ("java/lang/Float", "java/lang/Number")
        if isinstance(value, int):
            return class_name in ("java/lang/Integer", "java/lang/Long",
                                  "java/lang/Number", "java/lang/Short",
                                  "java/lang/Byte", "java/lang/Character")
        return False

    def default_for(self, type_desc):
        """``opaque_default``, except a JAR enum answers its first constant.

        A default entity is variant 0 everywhere: ``getVariant()`` gives 0 and
        ``getTurtleType()`` gives the type enum's first value.
        """
        if type_desc.startswith("L") and type_desc[1:-1] in self.class_path:
            name = type_desc[1:-1]
            parsed = self.class_path.get(name)
            if parsed is not None and parsed.super_name == "java/lang/Enum":
                self.initialize(name)
                constants = [
                    value for value in self.statics.get(name, {}).values()
                    if isinstance(value, JObject) and isinstance(value.native, dict)
                    and "ordinal" in value.native
                ]
                if constants:
                    return min(constants, key=lambda value: value.native["ordinal"])
        return opaque_default(type_desc)

    def throw(self, class_name, message=""):
        raise JavaThrow(JObject(class_name, {"detailMessage": message}))

    def new_object(self, class_name, desc="()V", arguments=()):
        """Allocate ``class_name`` and run its constructor."""
        self.initialize(class_name)
        instance = JObject(class_name)
        self.invoke_special(class_name, "<init>", desc, instance, list(arguments))
        return instance

    # -- classes -------------------------------------------------------------

    def initialize(self, name):
        """Run a JAR class's static initializer once (supers first)."""
        if name in self._initialized or name not in self.class_path:
            return
        self._initialized.add(name)
        parsed = self.class_path.get(name)
        if parsed is None:
            return
        if parsed.super_name:
            self.initialize(parsed.super_name)
        statics = self.statics.setdefault(name, {})
        for field in parsed.fields:
            if field.is_static:
                constant = parsed.constant_value(field)
                if constant is not None:
                    if field.desc == "F":
                        constant = f32(constant)
                    elif field.desc == "D":
                        constant = JDouble(constant)
                    elif field.desc == "J":
                        constant = JLong(constant)
                    statics[field.name] = constant
                else:
                    statics.setdefault(field.name, field_default(field.desc))
        clinit = parsed.find_method("<clinit>", "()V")
        if clinit is not None:
            try:
                self._execute(parsed, clinit, [])
            except JavaThrow as thrown:
                self.notes[(name, "<clinit> threw " + thrown.value.class_name)] += 1

    def _find_code_method(self, class_name, name, desc):
        parsed = self.class_path.get(class_name) if class_name in self.class_path else None
        if parsed is None:
            return None, None
        method = parsed.find_method(name, desc)
        if method is None or method.is_abstract:
            return parsed, None
        return parsed, method

    def _static_field_owner(self, owner, name):
        current = owner
        seen = set()
        while current and current not in seen:
            seen.add(current)
            parsed = self.class_path.get(current) if current in self.class_path else None
            if parsed is None:
                return None
            field = parsed.find_field(name)
            if field is not None and field.is_static:
                return current
            for interface in parsed.interfaces:
                found = self._static_field_owner(interface, name)
                if found:
                    return found
            current = parsed.super_name
        return None

    def get_static(self, owner, name, desc):
        declaring = self._static_field_owner(owner, name)
        if declaring:
            self.initialize(declaring)
            statics = self.statics.setdefault(declaring, {})
            return statics.get(name, field_default(desc))
        for ancestor in self.ancestors(owner):
            if (ancestor, name) in self._host_fields:
                value = self._host_fields[(ancestor, name)]
                return value(self) if callable(value) else value
            dispatch = self._field_dispatch.get(ancestor)
            if dispatch is not None:
                value = dispatch(self, name, desc)
                if value is not NOT_HANDLED:
                    return value
        self.notes[(owner, name)] += 1
        return self.default_for(desc)

    def put_static(self, owner, name, value):
        declaring = self._static_field_owner(owner, name)
        if declaring:
            self.initialize(declaring)
            self.statics.setdefault(declaring, {})[name] = value

    # -- invocation ----------------------------------------------------------

    def _host_lookup(self, table, owner, name, desc):
        function = table.get((owner, name, desc))
        if function is None:
            function = table.get((owner, name, None))
        return function

    def _catch_all(self, table, classes, name):
        """A host registered with ``name=None`` answers calls nothing else did.

        It is consulted only after the whole class chain, so a real method
        on a superclass always wins over a subclass's catch-all.
        """
        if name == "<init>":
            return None
        for owner in classes:
            function = table.get((owner, None, None))
            if function is not None:
                return function
        return None

    def invoke_static(self, owner, name, desc, arguments):
        current = owner
        seen = []
        while current and current not in seen:
            seen.append(current)
            parsed, method = self._find_code_method(current, name, desc)
            if method is not None:
                self.initialize(current)
                return self._execute(parsed, method, arguments)
            function = self._host_lookup(self._host_statics, current, name, desc)
            if function is not None:
                return function(self, arguments, desc)
            dispatch = self._static_dispatch.get(current)
            if dispatch is not None:
                result = dispatch(self, None, name, arguments, desc)
                if result is not NOT_HANDLED:
                    return result
            if parsed is None:
                break
            current = parsed.super_name
        function = self._catch_all(self._host_statics, seen, name)
        if function is not None:
            return function(self, arguments, desc)
        # The library serves vanilla's model package only: other statics
        # (an entity's ``Ingredient.of``) never open the client JAR.
        if (owner not in self.class_path and owner.startswith(VanillaModelLibrary.PREFIX)
                and self.library is not None):
            found = self.library.static_method(owner, name, desc)
            if found is not None:
                return self._execute(found[0], found[1], arguments)
        hooked = self._hooked(owner, name, desc, None, arguments)
        if hooked is not NOT_HANDLED:
            return hooked
        self.notes[(owner, name + desc)] += 1
        return self.default_for(method_return_type(desc))

    def _hooked(self, owner, name, desc, this, arguments):
        for hook in self.call_hooks:
            result = hook(self, owner, name, desc, this, arguments)
            if result is not NOT_HANDLED:
                return result
        return NOT_HANDLED

    def invoke_special(self, owner, name, desc, this, arguments):
        """Constructors, private methods and ``super.`` calls: no override."""
        if this is None:
            self.throw("java/lang/NullPointerException", f"{owner}.{name}")
        return self._invoke_from(owner, name, desc, this, arguments)

    def invoke_virtual(self, owner, name, desc, this, arguments):
        if this is None:
            if self.lenient_nulls:
                # Set-up code run only to see what it registers: a library
                # call that answered null must not end the run early.
                hooked = self._hooked(owner, name, desc, None, arguments)
                return hooked if hooked is not NOT_HANDLED else self.default_for(method_return_type(desc))
            self.throw("java/lang/NullPointerException", f"{owner}.{name}")
        if isinstance(this, Opaque):
            # Stand-ins never run code, even when their class is in the JAR:
            # every getter answers its type's default (a default entity),
            # unless a stand-in hook answers it.
            hooked = self._hooked(owner, name, desc, this, arguments)
            if hooked is not NOT_HANDLED:
                return hooked
            for hook in self.stand_in_hooks:
                hooked = hook(self, this, owner, name, desc, arguments)
                if hooked is not NOT_HANDLED:
                    return hooked
            code = method_return_type(desc)
            if code == "Z" and isinstance(this, EagerOpaque) and name not in this.false_names:
                return 1
            return self.default_for(code)
        if isinstance(this, JLambda) and name not in ("equals", "hashCode", "toString", "getClass"):
            return self._call_lambda(this, arguments, desc)
        if isinstance(this, (JObject, Opaque)):
            start = this.class_name
        elif isinstance(this, str):
            start = "java/lang/String"
        elif isinstance(this, JArray):
            start = "[array"
        elif isinstance(this, bool):
            start = "java/lang/Boolean"
        elif isinstance(this, JDouble):
            start = "java/lang/Double"
        elif isinstance(this, float):
            start = "java/lang/Float"
        elif isinstance(this, JLong):
            start = "java/lang/Long"
        elif isinstance(this, int):
            start = "java/lang/Integer"
        else:
            start = owner
        return self._invoke_from(start, name, desc, this, arguments, declared=owner)

    def _invoke_from(self, start, name, desc, this, arguments, declared=None):
        current = start
        seen = []
        while current and current not in seen:
            seen.append(current)
            parsed, method = self._find_code_method(current, name, desc)
            if method is not None:
                if method.is_static:
                    return self._execute(parsed, method, arguments)
                return self._execute(parsed, method, [this] + list(arguments))
            function = self._host_lookup(self._host_methods, current, name, desc)
            if function is not None:
                return function(self, this, arguments, desc)
            dispatch = self._dispatch.get(current)
            if dispatch is not None:
                result = dispatch(self, this, name, arguments, desc)
                if result is not NOT_HANDLED:
                    return result
            current = self.parent_of(current)
        if declared and declared != start:
            # Interfaces and abstract declarations: try the declared owner's
            # host implementation (e.g. ``List.add`` on a host list).
            for ancestor in self.ancestors(declared):
                function = self._host_lookup(self._host_methods, ancestor, name, desc)
                if function is not None:
                    return function(self, this, arguments, desc)
        hooked = self._hooked(declared or start, name, desc, this, arguments)
        if hooked is not NOT_HANDLED:
            return hooked
        function = self._catch_all(self._host_methods, seen, name)
        if function is not None:
            return function(self, this, arguments, desc)
        if name == "<init>":
            return None
        self.notes[((declared or start), name + desc)] += 1
        return self.default_for(method_return_type(desc))

    def _call_lambda(self, function, arguments, desc):
        ref_kind, owner, name, impl_desc = function.handle
        values = list(function.captured) + list(arguments)
        if ref_kind == 6:  # invokestatic
            return self.invoke_static(owner, name, impl_desc, values)
        if ref_kind == 8:  # newInvokeSpecial
            return self.new_object(owner, impl_desc, values)
        if not values:
            return opaque_default(method_return_type(desc))
        receiver, rest = values[0], values[1:]
        if ref_kind == 7:
            return self.invoke_special(owner, name, impl_desc, receiver, rest)
        return self.invoke_virtual(owner, name, impl_desc, receiver, rest)

    def call(self, function, *arguments):
        """Call a functional object (lambda) from host code."""
        if isinstance(function, JLambda):
            return self._call_lambda(function, list(arguments), "()V")
        return None

    def _execute(self, class_file, method, arguments):
        code = class_file.code(method)
        if code is None:
            return opaque_default(method_return_type(method.desc))
        if self.depth >= self.max_depth:
            raise VMLimitError(f"call depth over {self.max_depth} in {class_file.name}.{method.name}")
        self.depth += 1
        try:
            frame = _Frame(class_file, method, code, arguments)
            if self.lenient_nulls and self.depth > self.lenient_depth + 1:
                try:
                    return self._run(frame)
                except JavaThrow:
                    # A helper that failed answers its default; the set-up
                    # code that called it keeps going.
                    return opaque_default(method_return_type(method.desc))
            return self._run(frame)
        finally:
            self.depth -= 1

    # -- the interpreter loop -----------------------------------------------

    def _run(self, frame):
        try:
            instructions = frame.code.instructions
        except ClassFormatError as exc:
            raise VMError(str(exc)) from exc
        handlers = _HANDLERS
        pc = 0
        while True:
            self.steps += 1
            if self.steps > self.max_steps:
                raise VMLimitError(f"step budget of {self.max_steps} exceeded")
            entry = instructions.get(pc)
            if entry is None:
                raise VMError(f"jump to {pc} outside {frame.class_file.name}.{frame.method.name}")
            name, operands, next_pc = entry
            try:
                result = handlers[name](self, frame, operands)
            except JavaThrow as thrown:
                target = self._exception_target(frame, pc, thrown.value)
                if target is None:
                    raise
                frame.stack[:] = [thrown.value]
                pc = target
                continue
            except RecursionError as exc:
                raise VMLimitError("Python recursion limit reached") from exc
            if result is None:
                pc = next_pc
            elif result.__class__ is int:
                pc = result
            else:
                return result.value

    def _exception_target(self, frame, pc, value):
        for start, end, handler, catch in frame.code.handlers:
            if start <= pc < end and (catch is None or self.is_instance(value, catch)):
                return handler
        return None


# -- instruction handlers ------------------------------------------------------

def _push_constant(value):
    def handler(vm, frame, operands):
        frame.stack.append(value)
    return handler


_HANDLERS = {}
_HANDLERS["nop"] = lambda vm, frame, operands: None
_HANDLERS["aconst_null"] = _push_constant(None)
for _value in range(-1, 6):
    _HANDLERS["iconst_" + ("m1" if _value < 0 else str(_value))] = _push_constant(_value)
_HANDLERS["lconst_0"] = _push_constant(JLong(0))
_HANDLERS["lconst_1"] = _push_constant(JLong(1))
_HANDLERS["fconst_0"] = _push_constant(0.0)
_HANDLERS["fconst_1"] = _push_constant(1.0)
_HANDLERS["fconst_2"] = _push_constant(2.0)
_HANDLERS["dconst_0"] = _push_constant(JDouble(0.0))
_HANDLERS["dconst_1"] = _push_constant(JDouble(1.0))


def _bipush(vm, frame, operands):
    frame.stack.append(operands[0])


_HANDLERS["bipush"] = _bipush
_HANDLERS["sipush"] = _bipush


def _ldc(vm, frame, operands):
    kind, value = frame.class_file.loadable(operands[0])
    if kind == "Float":
        value = f32(value)
    elif kind == "Double":
        value = JDouble(value)
    elif kind == "Long":
        value = JLong(value)
    elif kind == "Class":
        value = JObject("java/lang/Class", native={"class": value})
    elif kind in ("MethodType", "MethodHandle"):
        value = Opaque("java/lang/invoke/" + kind)
    frame.stack.append(value)


_HANDLERS["ldc"] = _ldc
_HANDLERS["ldc_w"] = _ldc
_HANDLERS["ldc2_w"] = _ldc


def _load(index):
    def handler(vm, frame, operands):
        frame.stack.append(frame.locals[index])
    return handler


def _load_operand(vm, frame, operands):
    frame.stack.append(frame.locals[operands[0]])


def _store(index):
    def handler(vm, frame, operands):
        _set_local(frame, index, frame.stack.pop())
    return handler


def _set_local(frame, index, value):
    wide = isinstance(value, (JLong, JDouble))
    needed = index + (2 if wide else 1)
    if needed > len(frame.locals):
        frame.locals.extend([None] * (needed - len(frame.locals)))
    frame.locals[index] = value
    if wide:
        frame.locals[index + 1] = _TOP


def _store_operand(vm, frame, operands):
    _set_local(frame, operands[0], frame.stack.pop())


for _type in "ilfda":
    _HANDLERS[_type + "load"] = _load_operand
    _HANDLERS[_type + "store"] = _store_operand
    for _index in range(4):
        _HANDLERS[f"{_type}load_{_index}"] = _load(_index)
        _HANDLERS[f"{_type}store_{_index}"] = _store(_index)


def _array_and_index(vm, frame):
    index = frame.stack.pop()
    array = frame.stack.pop()
    if array is None:
        vm.throw("java/lang/NullPointerException", "array")
    if not isinstance(array, JArray):
        raise VMError("array access on a non-array value")
    if not 0 <= index < len(array.items):
        vm.throw("java/lang/ArrayIndexOutOfBoundsException", str(index))
    return array, index


def _array_load(vm, frame, operands):
    array, index = _array_and_index(vm, frame)
    frame.stack.append(array.items[index])


def _array_store(converter):
    def handler(vm, frame, operands):
        value = frame.stack.pop()
        array, index = _array_and_index(vm, frame)
        array.items[index] = converter(value) if converter else value
    return handler


for _name in ("iaload", "laload", "faload", "daload", "aaload", "baload", "caload", "saload"):
    _HANDLERS[_name] = _array_load
_HANDLERS["iastore"] = _array_store(wrap_int)
_HANDLERS["lastore"] = _array_store(wrap_long)
_HANDLERS["fastore"] = _array_store(f32)
_HANDLERS["dastore"] = _array_store(lambda value: JDouble(value))
_HANDLERS["aastore"] = _array_store(None)
_HANDLERS["bastore"] = _array_store(lambda value: ((int(value) & 0xFF) ^ 0x80) - 0x80)
_HANDLERS["castore"] = _array_store(lambda value: int(value) & 0xFFFF)
_HANDLERS["sastore"] = _array_store(lambda value: ((int(value) & 0xFFFF) ^ 0x8000) - 0x8000)


def _is_wide(value):
    return isinstance(value, (JLong, JDouble))


def _pop(vm, frame, operands):
    frame.stack.pop()


def _pop2(vm, frame, operands):
    if _is_wide(frame.stack.pop()):
        return
    frame.stack.pop()


def _dup(vm, frame, operands):
    frame.stack.append(frame.stack[-1])


def _dup_x1(vm, frame, operands):
    stack = frame.stack
    value = stack[-1]
    stack.insert(len(stack) - 2, value)


def _dup_x2(vm, frame, operands):
    stack = frame.stack
    value = stack[-1]
    depth = 2 if _is_wide(stack[-2]) else 3
    stack.insert(len(stack) - depth, value)


def _dup2(vm, frame, operands):
    stack = frame.stack
    if _is_wide(stack[-1]):
        stack.append(stack[-1])
    else:
        stack.extend(stack[-2:])


def _dup2_x1(vm, frame, operands):
    stack = frame.stack
    if _is_wide(stack[-1]):
        stack.insert(len(stack) - 2, stack[-1])
    else:
        values = stack[-2:]
        stack[len(stack) - 3:len(stack) - 3] = values


def _dup2_x2(vm, frame, operands):
    stack = frame.stack
    top_wide = _is_wide(stack[-1])
    if top_wide:
        depth = 2 if _is_wide(stack[-2]) else 3
        stack.insert(len(stack) - depth, stack[-1])
    else:
        values = stack[-2:]
        depth = 3 if _is_wide(stack[-3]) else 4
        stack[len(stack) - depth:len(stack) - depth] = values


def _swap(vm, frame, operands):
    stack = frame.stack
    stack[-1], stack[-2] = stack[-2], stack[-1]


_HANDLERS.update({
    "pop": _pop, "pop2": _pop2, "dup": _dup, "dup_x1": _dup_x1, "dup_x2": _dup_x2,
    "dup2": _dup2, "dup2_x1": _dup2_x1, "dup2_x2": _dup2_x2, "swap": _swap,
})


def _binary(function):
    def handler(vm, frame, operands):
        right = frame.stack.pop()
        left = frame.stack.pop()
        frame.stack.append(function(vm, left, right))
    return handler


def _unary(function):
    def handler(vm, frame, operands):
        frame.stack.append(function(frame.stack.pop()))
    return handler


def _idiv(vm, left, right):
    if right == 0:
        vm.throw("java/lang/ArithmeticException", "/ by zero")
    return wrap_int(_int_divide(left, right))


def _irem(vm, left, right):
    if right == 0:
        vm.throw("java/lang/ArithmeticException", "/ by zero")
    return wrap_int(left - right * _int_divide(left, right))


def _ldiv(vm, left, right):
    if right == 0:
        vm.throw("java/lang/ArithmeticException", "/ by zero")
    return wrap_long(_int_divide(left, right))


def _lrem(vm, left, right):
    if right == 0:
        vm.throw("java/lang/ArithmeticException", "/ by zero")
    return wrap_long(left - right * _int_divide(left, right))


_HANDLERS.update({
    "iadd": _binary(lambda vm, a, b: wrap_int(a + b)),
    "isub": _binary(lambda vm, a, b: wrap_int(a - b)),
    "imul": _binary(lambda vm, a, b: wrap_int(a * b)),
    "idiv": _binary(_idiv),
    "irem": _binary(_irem),
    "ineg": _unary(lambda a: wrap_int(-a)),
    "ladd": _binary(lambda vm, a, b: wrap_long(a + b)),
    "lsub": _binary(lambda vm, a, b: wrap_long(a - b)),
    "lmul": _binary(lambda vm, a, b: wrap_long(a * b)),
    "ldiv": _binary(_ldiv),
    "lrem": _binary(_lrem),
    "lneg": _unary(lambda a: wrap_long(-a)),
    "fadd": _binary(lambda vm, a, b: f32(a + b)),
    "fsub": _binary(lambda vm, a, b: f32(a - b)),
    "fmul": _binary(lambda vm, a, b: f32(a * b)),
    "fdiv": _binary(lambda vm, a, b: f32(_float_divide(a, b))),
    "frem": _binary(lambda vm, a, b: f32(_float_remainder(a, b))),
    "fneg": _unary(lambda a: f32(-a)),
    "dadd": _binary(lambda vm, a, b: JDouble(a + b)),
    "dsub": _binary(lambda vm, a, b: JDouble(a - b)),
    "dmul": _binary(lambda vm, a, b: JDouble(a * b)),
    "ddiv": _binary(lambda vm, a, b: JDouble(_float_divide(a, b))),
    "drem": _binary(lambda vm, a, b: JDouble(_float_remainder(a, b))),
    "dneg": _unary(lambda a: JDouble(-a)),
    "ishl": _binary(lambda vm, a, b: wrap_int(a << (b & 31))),
    "ishr": _binary(lambda vm, a, b: wrap_int(a >> (b & 31))),
    "iushr": _binary(lambda vm, a, b: wrap_int((a & _MASK32) >> (b & 31))),
    "lshl": _binary(lambda vm, a, b: wrap_long(a << (b & 63))),
    "lshr": _binary(lambda vm, a, b: wrap_long(a >> (b & 63))),
    "lushr": _binary(lambda vm, a, b: wrap_long((a & _MASK64) >> (b & 63))),
    "iand": _binary(lambda vm, a, b: wrap_int(a & b)),
    "ior": _binary(lambda vm, a, b: wrap_int(a | b)),
    "ixor": _binary(lambda vm, a, b: wrap_int(a ^ b)),
    "land": _binary(lambda vm, a, b: wrap_long(a & b)),
    "lor": _binary(lambda vm, a, b: wrap_long(a | b)),
    "lxor": _binary(lambda vm, a, b: wrap_long(a ^ b)),
    "i2l": _unary(lambda a: JLong(a)),
    "i2f": _unary(lambda a: f32(a)),
    "i2d": _unary(lambda a: JDouble(a)),
    "l2i": _unary(wrap_int),
    "l2f": _unary(lambda a: f32(int(a))),
    "l2d": _unary(lambda a: JDouble(int(a))),
    "f2i": _unary(lambda a: _float_to_int(a, 32)),
    "f2l": _unary(lambda a: JLong(_float_to_int(a, 64))),
    "f2d": _unary(lambda a: JDouble(a)),
    "d2i": _unary(lambda a: _float_to_int(a, 32)),
    "d2l": _unary(lambda a: JLong(_float_to_int(a, 64))),
    "d2f": _unary(lambda a: f32(a)),
    "i2b": _unary(lambda a: ((int(a) & 0xFF) ^ 0x80) - 0x80),
    "i2c": _unary(lambda a: int(a) & 0xFFFF),
    "i2s": _unary(lambda a: ((int(a) & 0xFFFF) ^ 0x8000) - 0x8000),
    "lcmp": _binary(lambda vm, a, b: (a > b) - (a < b)),
    "fcmpl": _binary(lambda vm, a, b: _compare(a, b, -1)),
    "fcmpg": _binary(lambda vm, a, b: _compare(a, b, 1)),
    "dcmpl": _binary(lambda vm, a, b: _compare(a, b, -1)),
    "dcmpg": _binary(lambda vm, a, b: _compare(a, b, 1)),
})


def _iinc(vm, frame, operands):
    index, delta = operands
    frame.locals[index] = wrap_int(frame.locals[index] + delta)


_HANDLERS["iinc"] = _iinc


def _branch1(test):
    def handler(vm, frame, operands):
        if test(frame.stack.pop()):
            return operands[0]
        return None
    return handler


def _branch2(test):
    def handler(vm, frame, operands):
        right = frame.stack.pop()
        left = frame.stack.pop()
        if test(left, right):
            return operands[0]
        return None
    return handler


_HANDLERS.update({
    "ifeq": _branch1(lambda a: a == 0),
    "ifne": _branch1(lambda a: a != 0),
    "iflt": _branch1(lambda a: a < 0),
    "ifge": _branch1(lambda a: a >= 0),
    "ifgt": _branch1(lambda a: a > 0),
    "ifle": _branch1(lambda a: a <= 0),
    "if_icmpeq": _branch2(lambda a, b: a == b),
    "if_icmpne": _branch2(lambda a, b: a != b),
    "if_icmplt": _branch2(lambda a, b: a < b),
    "if_icmpge": _branch2(lambda a, b: a >= b),
    "if_icmpgt": _branch2(lambda a, b: a > b),
    "if_icmple": _branch2(lambda a, b: a <= b),
    "if_acmpeq": _branch2(lambda a, b: a is b or (_is_value(a) and a == b)),
    "if_acmpne": _branch2(lambda a, b: not (a is b or (_is_value(a) and a == b))),
    "ifnull": _branch1(lambda a: a is None),
    "ifnonnull": _branch1(lambda a: a is not None),
})


def _is_value(value):
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def _goto(vm, frame, operands):
    return operands[0]


def _jsr(vm, frame, operands):
    frame.stack.append(("return-address", operands[0]))
    return operands[0]


def _ret(vm, frame, operands):
    raise VMError("jsr/ret subroutines are not supported")


def _tableswitch(vm, frame, operands):
    default, low, targets = operands
    key = frame.stack.pop()
    offset = key - low
    if 0 <= offset < len(targets):
        return targets[offset]
    return default


def _lookupswitch(vm, frame, operands):
    default, table = operands
    return table.get(frame.stack.pop(), default)


_HANDLERS.update({
    "goto": _goto, "goto_w": _goto, "jsr": _jsr, "jsr_w": _jsr, "ret": _ret,
    "tableswitch": _tableswitch, "lookupswitch": _lookupswitch,
})


def _return_value(vm, frame, operands):
    return _Return(frame.stack.pop())


def _return_void(vm, frame, operands):
    return _Return(None)


for _name in ("ireturn", "lreturn", "freturn", "dreturn", "areturn"):
    _HANDLERS[_name] = _return_value
_HANDLERS["return"] = _return_void


def _coerce_field(value, desc):
    code = desc[:1]
    if code == "F" and isinstance(value, (int, float)):
        return f32(value)
    if code == "D" and isinstance(value, (int, float)):
        return JDouble(value)
    if code == "J" and isinstance(value, int):
        return JLong(value)
    return value


def _getstatic(vm, frame, operands):
    owner, name, desc = frame.class_file.member_ref(operands[0])
    frame.stack.append(vm.get_static(owner, name, desc))


def _putstatic(vm, frame, operands):
    owner, name, desc = frame.class_file.member_ref(operands[0])
    vm.put_static(owner, name, _coerce_field(frame.stack.pop(), desc))


def _getfield(vm, frame, operands):
    owner, name, desc = frame.class_file.member_ref(operands[0])
    target = frame.stack.pop()
    if target is None and not vm.lenient_nulls:
        vm.throw("java/lang/NullPointerException", name)
    if isinstance(target, JObject):
        if name in target.fields:
            frame.stack.append(target.fields[name])
        else:
            frame.stack.append(field_default(desc))
        return
    if target is not None:
        for hook in vm.field_hooks:
            value = hook(vm, target, owner, name, desc)
            if value is not NOT_HANDLED:
                frame.stack.append(value)
                return
    frame.stack.append(vm.default_for(desc))


def _putfield(vm, frame, operands):
    owner, name, desc = frame.class_file.member_ref(operands[0])
    value = frame.stack.pop()
    target = frame.stack.pop()
    if target is None and not vm.lenient_nulls:
        vm.throw("java/lang/NullPointerException", name)
    if isinstance(target, JObject):
        target.fields[name] = _coerce_field(value, desc)


_HANDLERS.update({
    "getstatic": _getstatic, "putstatic": _putstatic,
    "getfield": _getfield, "putfield": _putfield,
})


def _pop_arguments(frame, desc):
    count = len(method_argument_types(desc))
    if not count:
        return []
    arguments = frame.stack[-count:]
    del frame.stack[-count:]
    return arguments


def _push_result(frame, desc, value):
    if method_return_type(desc) != "V":
        frame.stack.append(_coerce_field(value, method_return_type(desc)))


def _invokevirtual(vm, frame, operands):
    owner, name, desc = frame.class_file.member_ref(operands[0])
    arguments = _pop_arguments(frame, desc)
    this = frame.stack.pop()
    _push_result(frame, desc, vm.invoke_virtual(owner, name, desc, this, arguments))


def _invokespecial(vm, frame, operands):
    owner, name, desc = frame.class_file.member_ref(operands[0])
    arguments = _pop_arguments(frame, desc)
    this = frame.stack.pop()
    _push_result(frame, desc, vm.invoke_special(owner, name, desc, this, arguments))


def _invokestatic(vm, frame, operands):
    owner, name, desc = frame.class_file.member_ref(operands[0])
    arguments = _pop_arguments(frame, desc)
    _push_result(frame, desc, vm.invoke_static(owner, name, desc, arguments))


def _invokeinterface(vm, frame, operands):
    owner, name, desc = frame.class_file.member_ref(operands[0])
    arguments = _pop_arguments(frame, desc)
    this = frame.stack.pop()
    _push_result(frame, desc, vm.invoke_virtual(owner, name, desc, this, arguments))


def _invokedynamic(vm, frame, operands):
    class_file = frame.class_file
    bootstrap_index, name, desc = class_file.invoke_dynamic(operands[0])
    arguments = _pop_arguments(frame, desc)
    try:
        handle, static_arguments = class_file.bootstrap_method(bootstrap_index)
    except (ClassFormatError, IndexError):
        _push_result(frame, desc, opaque_default(method_return_type(desc)))
        return
    _ref_kind, bootstrap_owner, bootstrap_name, _bootstrap_desc = handle
    result = None
    if bootstrap_owner == "java/lang/invoke/StringConcatFactory":
        types = method_argument_types(desc)
        if bootstrap_name == "makeConcatWithConstants" and static_arguments:
            recipe = str(static_arguments[0][1])
            constants = [value for _kind, value in static_arguments[1:]]
            parts = []
            argument_index = 0
            constant_index = 0
            for character in recipe:
                if character == "\x01":
                    parts.append(java_string(arguments[argument_index], types[argument_index]))
                    argument_index += 1
                elif character == "\x02":
                    parts.append(java_string(constants[constant_index]))
                    constant_index += 1
                else:
                    parts.append(character)
            result = "".join(parts)
        else:
            result = "".join(
                java_string(value, type_desc) for value, type_desc in zip(arguments, types))
    elif bootstrap_owner == "java/lang/invoke/LambdaMetafactory" and len(static_arguments) >= 2:
        kind, implementation = static_arguments[1]
        if kind == "MethodHandle":
            result = JLambda(implementation, arguments)
        else:
            result = opaque_default(method_return_type(desc))
    else:
        vm.notes[(bootstrap_owner, bootstrap_name)] += 1
        result = opaque_default(method_return_type(desc))
    _push_result(frame, desc, result)


_HANDLERS.update({
    "invokevirtual": _invokevirtual, "invokespecial": _invokespecial,
    "invokestatic": _invokestatic, "invokeinterface": _invokeinterface,
    "invokedynamic": _invokedynamic,
})


def _new(vm, frame, operands):
    name = frame.class_file.class_name(operands[0])
    vm.initialize(name)
    if name in vm.class_path or vm.is_host_class(name):
        frame.stack.append(JObject(name))
    else:
        vm.notes[(name, "<new>")] += 1
        frame.stack.append(Opaque(name))


_ARRAY_TYPES = {4: "Z", 5: "C", 6: "F", 7: "D", 8: "B", 9: "S", 10: "I", 11: "J"}
MAX_ARRAY_LENGTH = 1 << 20


def _array_length_checked(vm, length):
    if length < 0:
        vm.throw("java/lang/NegativeArraySizeException", str(length))
    if length > MAX_ARRAY_LENGTH:
        raise VMLimitError(f"array of {length} elements is too large")
    return length


def _newarray(vm, frame, operands):
    component = _ARRAY_TYPES.get(operands[0], "I")
    length = _array_length_checked(vm, frame.stack.pop())
    frame.stack.append(JArray(component, [field_default(component)] * length))


def _anewarray(vm, frame, operands):
    component = frame.class_file.class_name(operands[0])
    length = _array_length_checked(vm, frame.stack.pop())
    frame.stack.append(JArray(component if component.startswith("[") else f"L{component};",
                              [None] * length))


def _multianewarray(vm, frame, operands):
    array_type = frame.class_file.class_name(operands[0])
    dimensions = operands[1]
    counts = frame.stack[-dimensions:]
    del frame.stack[-dimensions:]

    def build(type_desc, level):
        length = _array_length_checked(vm, counts[level])
        component = type_desc[1:]
        if level + 1 < len(counts):
            return JArray(component, [build(component, level + 1) for _ in range(length)])
        return JArray(component, [field_default(component)] * length)

    frame.stack.append(build(array_type, 0))


def _arraylength(vm, frame, operands):
    array = frame.stack.pop()
    if array is None:
        vm.throw("java/lang/NullPointerException", "arraylength")
    if not isinstance(array, JArray):
        raise VMError("arraylength on a non-array value")
    frame.stack.append(len(array.items))


def _athrow(vm, frame, operands):
    value = frame.stack.pop()
    if value is None:
        vm.throw("java/lang/NullPointerException", "throw null")
    raise JavaThrow(value)


def _checkcast(vm, frame, operands):
    target = frame.class_file.class_name(operands[0])
    value = frame.stack[-1]
    if isinstance(value, JObject) and not target.startswith("["):
        known = target in vm.class_path or vm.is_host_class(target)
        if known and value.class_name in vm.class_path and not vm.is_instance(value, target):
            chain = vm.ancestors(value.class_name)
            # Only refuse when every ancestor is known: then the cast fails in Java too.
            if all(name in vm.class_path or vm.is_host_class(name)
                   or name == "java/lang/Object" for name in chain):
                vm.throw("java/lang/ClassCastException", f"{value.class_name} -> {target}")


def _instanceof(vm, frame, operands):
    target = frame.class_file.class_name(operands[0])
    value = frame.stack.pop()
    frame.stack.append(1 if vm.is_instance(value, target) else 0)


def _monitor(vm, frame, operands):
    frame.stack.pop()


_HANDLERS.update({
    "new": _new, "newarray": _newarray, "anewarray": _anewarray,
    "multianewarray": _multianewarray, "arraylength": _arraylength,
    "athrow": _athrow, "checkcast": _checkcast, "instanceof": _instanceof,
    "monitorenter": _monitor, "monitorexit": _monitor,
})


# -- java.lang and java.util basics ------------------------------------------

LIST_CLASSES = (
    "java/util/ArrayList", "java/util/LinkedList", "java/util/List",
    "java/util/Collection", "java/lang/Iterable", "java/util/AbstractList",
    "com/google/common/collect/ImmutableList", "com/google/common/collect/ImmutableCollection",
    "it/unimi/dsi/fastutil/objects/ObjectArrayList", "it/unimi/dsi/fastutil/objects/ObjectList",
    "java/util/Set", "java/util/HashSet", "java/util/LinkedHashSet",
    "com/google/common/collect/ImmutableSet",
)


def new_list(items=(), class_name="java/util/ArrayList"):
    return JObject(class_name, native={"items": list(items)})


def list_items(value):
    """Items of a host list or array, else ``None``."""
    if isinstance(value, JObject) and isinstance(value.native, dict) and "items" in value.native:
        return value.native["items"]
    if isinstance(value, JArray):
        return value.items
    return None


def _flatten_varargs(arguments, desc):
    items = []
    types = method_argument_types(desc)
    for value, type_desc in zip(arguments, types):
        if type_desc.startswith("[") and isinstance(value, JArray):
            items.extend(value.items)
        else:
            items.append(value)
    return items


def _install_java_lang(vm):
    object_class = "java/lang/Object"
    vm.host_class(object_class, parent=None)
    vm.method(object_class, "<init>")(lambda vm, this, args, desc: None)
    vm.method(object_class, "getClass")(
        lambda vm, this, args, desc: JObject(
            "java/lang/Class", native={"class": getattr(this, "class_name", object_class)}))
    vm.method(object_class, "hashCode")(lambda vm, this, args, desc: wrap_int(id(this)))
    vm.method(object_class, "equals")(
        lambda vm, this, args, desc: 1 if this is args[0] or (
            _is_value(this) and this == args[0]) else 0)
    vm.method(object_class, "toString")(lambda vm, this, args, desc: java_string(this))
    vm.method(object_class, "clone")(
        lambda vm, this, args, desc: JArray(this.component, list(this.items))
        if isinstance(this, JArray) else this)
    vm.host_class("[array", parent=object_class)
    vm.host_class("java/lang/Class")
    vm.method("java/lang/Class", "getName")(
        lambda vm, this, args, desc: this.native["class"].replace("/", "."))
    vm.method("java/lang/Class", "getSimpleName")(
        lambda vm, this, args, desc: this.native["class"].rsplit("/", 1)[-1].rsplit("$", 1)[-1])

    # Enums: the JAR's enum classes extend this host.
    vm.host_class("java/lang/Enum", interfaces=("java/lang/Comparable",))

    @vm.method("java/lang/Enum", "<init>")
    def _enum_init(vm, this, args, desc):
        this.native = {"enum_name": args[0] if args else "", "ordinal": args[1] if len(args) > 1 else 0}

    vm.method("java/lang/Enum", "ordinal")(
        lambda vm, this, args, desc: (this.native or {}).get("ordinal", 0))
    vm.method("java/lang/Enum", "name")(
        lambda vm, this, args, desc: (this.native or {}).get("enum_name", ""))
    vm.method("java/lang/Enum", "toString")(
        lambda vm, this, args, desc: (this.native or {}).get("enum_name", ""))
    vm.method("java/lang/Enum", "compareTo")(
        lambda vm, this, args, desc: (this.native or {}).get("ordinal", 0)
        - ((getattr(args[0], "native", None) or {}).get("ordinal", 0)))

    @vm.static("java/lang/Enum", "valueOf")
    def _enum_value_of(vm, args, desc):
        class_object, name = args
        enum_class = (class_object.native or {}).get("class", "") if isinstance(class_object, JObject) else ""
        vm.initialize(enum_class)
        for value in vm.statics.get(enum_class, {}).values():
            if isinstance(value, JObject) and (value.native or {}).get("enum_name") == name:
                return value
        vm.throw("java/lang/IllegalArgumentException", str(name))

    _install_strings(vm)
    _install_boxes(vm)
    _install_math(vm)
    _install_collections(vm)


def _install_strings(vm):
    string = "java/lang/String"
    vm.host_class(string, interfaces=("java/lang/CharSequence", "java/lang/Comparable"))
    simple = {
        "length": lambda this, args: len(this),
        "isEmpty": lambda this, args: 1 if not this else 0,
        "isBlank": lambda this, args: 1 if not this.strip() else 0,
        "hashCode": lambda this, args: java_hash(this),
        "equals": lambda this, args: 1 if isinstance(args[0], str) and args[0] == this else 0,
        "equalsIgnoreCase": lambda this, args: 1 if isinstance(args[0], str)
        and args[0].lower() == this.lower() else 0,
        "toLowerCase": lambda this, args: this.lower(),
        "toUpperCase": lambda this, args: this.upper(),
        "trim": lambda this, args: this.strip(" \t\n\r\x00"),
        "strip": lambda this, args: this.strip(),
        "intern": lambda this, args: this,
        "toString": lambda this, args: this,
        "concat": lambda this, args: this + java_string(args[0]),
        "startsWith": lambda this, args: 1 if this.startswith(java_string(args[0])) else 0,
        "endsWith": lambda this, args: 1 if this.endswith(java_string(args[0])) else 0,
        "contains": lambda this, args: 1 if java_string(args[0]) in this else 0,
    }
    for name, function in simple.items():
        vm.method(string, name)(lambda vm, this, args, desc, function=function: function(this, args))

    @vm.method(string, "charAt")
    def _char_at(vm, this, args, desc):
        index = args[0]
        if not 0 <= index < len(this):
            vm.throw("java/lang/StringIndexOutOfBoundsException", str(index))
        return ord(this[index])

    @vm.method(string, "substring")
    def _substring(vm, this, args, desc):
        start = args[0]
        end = args[1] if len(args) > 1 else len(this)
        if not 0 <= start <= end <= len(this):
            vm.throw("java/lang/StringIndexOutOfBoundsException", f"{start},{end}")
        return this[start:end]

    @vm.method(string, "indexOf")
    def _index_of(vm, this, args, desc):
        needle = chr(args[0]) if isinstance(args[0], int) else java_string(args[0])
        start = args[1] if len(args) > 1 else 0
        return this.find(needle, max(0, start))

    @vm.method(string, "replace")
    def _replace(vm, this, args, desc):
        old, new = args
        if isinstance(old, int):
            return this.replace(chr(old), chr(new))
        return this.replace(java_string(old), java_string(new))

    @vm.static(string, "valueOf")
    def _value_of(vm, args, desc):
        return java_string(args[0], method_argument_types(desc)[0])

    @vm.static(string, "format")
    def _format(vm, args, desc):
        return java_string(args[0])

    builder = "java/lang/StringBuilder"
    vm.host_class(builder, interfaces=("java/lang/CharSequence",))
    vm.host_class("java/lang/StringBuffer", parent=builder)

    @vm.method(builder, "<init>")
    def _builder_init(vm, this, args, desc):
        initial = ""
        if args and isinstance(args[0], str):
            initial = args[0]
        this.native = {"string": initial}

    @vm.method(builder, "append")
    def _append(vm, this, args, desc):
        this.native["string"] += java_string(args[0], method_argument_types(desc)[0])
        return this

    vm.method(builder, "toString")(lambda vm, this, args, desc: this.native["string"])
    vm.method(builder, "length")(lambda vm, this, args, desc: len(this.native["string"]))


def _unbox(value, convert):
    """``intValue()`` and friends; anything that is not a number reads as 0."""
    if not isinstance(value, (int, float)):
        value = 0
    if convert in (wrap_int, JLong, int):
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return convert(0)
        return convert(int(number))
    return convert(value)


def _install_boxes(vm):
    for box, primitive in (("java/lang/Integer", "I"), ("java/lang/Float", "F"),
                           ("java/lang/Double", "D"), ("java/lang/Long", "J"),
                           ("java/lang/Boolean", "Z"), ("java/lang/Short", "S"),
                           ("java/lang/Byte", "B"), ("java/lang/Character", "C")):
        vm.host_class(box, parent="java/lang/Number" if primitive not in "ZC" else "java/lang/Object")
        vm.static(box, "valueOf")(lambda vm, args, desc: args[0] if not isinstance(args[0], str) else 0)
        for getter, convert in (("intValue", wrap_int), ("floatValue", f32),
                                ("doubleValue", JDouble), ("longValue", JLong),
                                ("booleanValue", int), ("shortValue", wrap_int),
                                ("byteValue", wrap_int), ("charValue", int)):
            vm.method(box, getter)(
                lambda vm, this, args, desc, convert=convert: _unbox(this, convert))
        vm.method(box, "toString")(
            lambda vm, this, args, desc, primitive=primitive: java_string(this, primitive))
        vm.method(box, "hashCode")(lambda vm, this, args, desc: wrap_int(hash(this)))
    vm.host_class("java/lang/Number")
    vm.static("java/lang/Integer", "parseInt")(lambda vm, args, desc: wrap_int(int(str(args[0]))))
    vm.static("java/lang/Float", "parseFloat")(lambda vm, args, desc: f32(float(str(args[0]))))
    vm.static("java/lang/Float", "isNaN")(lambda vm, args, desc: 1 if math.isnan(args[0]) else 0)
    vm.static("java/lang/Float", "compare")(lambda vm, args, desc: _compare(args[0], args[1], 0))
    vm.static("java/lang/Float", "floatToIntBits")(
        lambda vm, args, desc: struct.unpack("<i", struct.pack("<f", args[0]))[0])
    vm.static("java/lang/Float", "intBitsToFloat")(
        lambda vm, args, desc: struct.unpack("<f", struct.pack("<i", wrap_int(args[0])))[0])
    for owner, field, value in (
        ("java/lang/Integer", "MAX_VALUE", 0x7FFFFFFF),
        ("java/lang/Integer", "MIN_VALUE", -0x80000000),
        ("java/lang/Float", "MAX_VALUE", f32(3.4028234663852886e38)),
        ("java/lang/Float", "MIN_VALUE", f32(1.401298464324817e-45)),
        ("java/lang/Float", "NaN", math.nan),
        ("java/lang/Float", "POSITIVE_INFINITY", math.inf),
        ("java/lang/Float", "NEGATIVE_INFINITY", -math.inf),
        ("java/lang/Double", "MAX_VALUE", JDouble(1.7976931348623157e308)),
        ("java/lang/Boolean", "TRUE", 1),
        ("java/lang/Boolean", "FALSE", 0),
    ):
        vm.static_field(owner, field, value)


def _math_result(desc, value):
    code = method_return_type(desc)
    if code == "F":
        return f32(value)
    if code == "D":
        return JDouble(value)
    if code == "I":
        return wrap_int(value)
    if code == "J":
        return wrap_long(value)
    return value


def _safe(function):
    def wrapped(*values):
        try:
            return function(*values)
        except (ValueError, OverflowError, ZeroDivisionError):
            return math.nan
    return wrapped


_MATH_FUNCTIONS = {
    "sin": _safe(math.sin), "cos": _safe(math.cos), "tan": _safe(math.tan),
    "asin": _safe(math.asin), "acos": _safe(math.acos), "atan": _safe(math.atan),
    "atan2": _safe(math.atan2), "sqrt": _safe(math.sqrt), "cbrt": _safe(lambda v: math.copysign(abs(v) ** (1 / 3), v)),
    "exp": _safe(math.exp), "log": _safe(math.log), "log10": _safe(math.log10),
    "pow": _safe(math.pow), "abs": abs, "toRadians": math.radians, "toDegrees": math.degrees,
    "floor": _safe(math.floor), "ceil": _safe(math.ceil), "signum": lambda v: (v > 0) - (v < 0),
    "hypot": _safe(math.hypot), "sinh": _safe(math.sinh), "cosh": _safe(math.cosh),
    "tanh": _safe(math.tanh), "random": lambda: 0.5,
    "max": max, "min": min,
}


def _install_math(vm):
    for owner in ("java/lang/Math", "java/lang/StrictMath"):
        vm.host_class(owner)
        for name, function in _MATH_FUNCTIONS.items():
            vm.static(owner, name)(
                lambda vm, args, desc, function=function: _math_result(desc, function(*args)))
        vm.static(owner, "round")(
            lambda vm, args, desc: _math_result(desc, math.floor(args[0] + 0.5)
                                                if not math.isnan(args[0]) else 0))
        vm.static_field(owner, "PI", JDouble(math.pi))
        vm.static_field(owner, "E", JDouble(math.e))


def _install_collections(vm):
    for name in LIST_CLASSES:
        vm.host_class(name, interfaces=("java/util/List", "java/util/Collection", "java/lang/Iterable")
                      if name not in ("java/util/List", "java/util/Collection", "java/lang/Iterable")
                      else ())

    def items_of(vm, value):
        items = list_items(value)
        if items is None:
            if isinstance(value, Opaque):
                return []
            raise VMError(f"{value!r} is not a list")
        return items

    def register(name, function):
        for owner in LIST_CLASSES:
            vm.method(owner, name)(function)

    @vm.method("java/util/ArrayList", "<init>")
    def _list_init(vm, this, args, desc):
        items = []
        if args and not isinstance(args[0], int):
            items = list(items_of(vm, args[0]))
        this.native = {"items": items}

    for owner in ("java/util/LinkedList", "it/unimi/dsi/fastutil/objects/ObjectArrayList",
                  "java/util/HashSet", "java/util/LinkedHashSet"):
        vm.method(owner, "<init>")(_list_init)

    def _add(vm, this, args, desc):
        items = items_of(vm, this)
        if len(args) == 2 and isinstance(args[0], int) and method_argument_types(desc)[0] == "I":
            items.insert(args[0], args[1])
            return None
        items.append(args[0])
        return 1

    register("add", _add)
    register("addAll", lambda vm, this, args, desc: items_of(vm, this).extend(items_of(vm, args[-1])) or 1)
    register("size", lambda vm, this, args, desc: len(items_of(vm, this)))
    register("isEmpty", lambda vm, this, args, desc: 0 if items_of(vm, this) else 1)
    register("contains", lambda vm, this, args, desc: 1 if any(
        item is args[0] or (_is_value(item) and item == args[0]) for item in items_of(vm, this)) else 0)
    register("clear", lambda vm, this, args, desc: items_of(vm, this).clear())

    def _get(vm, this, args, desc):
        items = items_of(vm, this)
        if not 0 <= args[0] < len(items):
            vm.throw("java/lang/IndexOutOfBoundsException", str(args[0]))
        return items[args[0]]

    def _set(vm, this, args, desc):
        items = items_of(vm, this)
        if not 0 <= args[0] < len(items):
            vm.throw("java/lang/IndexOutOfBoundsException", str(args[0]))
        previous = items[args[0]]
        items[args[0]] = args[1]
        return previous

    register("get", _get)
    register("set", _set)
    register("iterator", lambda vm, this, args, desc: JObject(
        "java/util/Iterator", native={"items": list(items_of(vm, this)), "position": 0}))
    register("toArray", lambda vm, this, args, desc: JArray(
        "Ljava/lang/Object;", list(items_of(vm, this))))
    register("stream", lambda vm, this, args, desc: Opaque("java/util/stream/Stream"))

    def _for_each(vm, this, args, desc):
        for item in list(items_of(vm, this)):
            vm.call(args[0], item)

    register("forEach", _for_each)
    vm.host_class("java/util/Iterator")

    @vm.method("java/util/Iterator", "hasNext")
    def _has_next(vm, this, args, desc):
        return 1 if this.native["position"] < len(this.native["items"]) else 0

    @vm.method("java/util/Iterator", "next")
    def _next(vm, this, args, desc):
        position = this.native["position"]
        if position >= len(this.native["items"]):
            vm.throw("java/util/NoSuchElementException")
        this.native["position"] = position + 1
        return this.native["items"][position]

    def _of(class_name):
        return lambda vm, args, desc: new_list(_flatten_varargs(args, desc), class_name)

    vm.static("com/google/common/collect/ImmutableList", "of")(_of("com/google/common/collect/ImmutableList"))
    vm.static("com/google/common/collect/ImmutableList", "copyOf")(
        lambda vm, args, desc: new_list(items_of(vm, args[0]), "com/google/common/collect/ImmutableList"))
    vm.static("com/google/common/collect/ImmutableSet", "of")(_of("com/google/common/collect/ImmutableSet"))
    vm.static("java/util/List", "of")(_of("com/google/common/collect/ImmutableList"))
    vm.static("java/util/Set", "of")(_of("com/google/common/collect/ImmutableSet"))
    vm.static("java/util/Arrays", "asList")(_of("java/util/ArrayList"))
    vm.static("com/google/common/collect/Lists", "newArrayList")(
        lambda vm, args, desc: new_list(
            items_of(vm, args[0]) if len(args) == 1 and list_items(args[0]) is not None
            and not method_argument_types(desc)[0].startswith("[") else _flatten_varargs(args, desc)))
    vm.static("java/util/Collections", "emptyList")(lambda vm, args, desc: new_list())
    vm.static("java/util/Collections", "singletonList")(lambda vm, args, desc: new_list([args[0]]))
    vm.static("java/util/Collections", "unmodifiableList")(lambda vm, args, desc: args[0])

    builder = "com/google/common/collect/ImmutableList$Builder"
    vm.host_class(builder)
    vm.static("com/google/common/collect/ImmutableList", "builder")(
        lambda vm, args, desc: JObject(builder, native={"items": []}))
    vm.method(builder, "add")(
        lambda vm, this, args, desc: this.native["items"].extend(_flatten_varargs(args, desc)) or this)
    vm.method(builder, "addAll")(
        lambda vm, this, args, desc: this.native["items"].extend(items_of(vm, args[0])) or this)
    vm.method(builder, "build")(
        lambda vm, this, args, desc: new_list(this.native["items"], "com/google/common/collect/ImmutableList"))

    for owner in ("java/util/HashMap", "java/util/Map", "java/util/LinkedHashMap",
                  "com/google/common/collect/Maps", "com/google/common/collect/ImmutableMap"):
        vm.host_class(owner, interfaces=("java/util/Map",) if owner != "java/util/Map" else ())

    def _map_init(vm, this, args, desc):
        this.native = {"map": {}}

    vm.method("java/util/HashMap", "<init>")(_map_init)
    vm.method("java/util/LinkedHashMap", "<init>")(_map_init)
    vm.static("com/google/common/collect/Maps", "newHashMap")(
        lambda vm, args, desc: JObject("java/util/HashMap", native={"map": {}}))

    def _key(value):
        return value if _is_value(value) else id(value)

    def _map(this):
        return this.native.setdefault("map", {}) if isinstance(this, JObject) else {}

    for owner in ("java/util/HashMap", "java/util/Map", "java/util/LinkedHashMap"):
        vm.method(owner, "put")(
            lambda vm, this, args, desc: (_map(this).pop(_key(args[0]), (None, None))[1],
                                          _map(this).__setitem__(_key(args[0]), (args[0], args[1])))[0])
        vm.method(owner, "get")(
            lambda vm, this, args, desc: _map(this).get(_key(args[0]), (None, None))[1])
        vm.method(owner, "containsKey")(
            lambda vm, this, args, desc: 1 if _key(args[0]) in _map(this) else 0)
        vm.method(owner, "size")(lambda vm, this, args, desc: len(_map(this)))
