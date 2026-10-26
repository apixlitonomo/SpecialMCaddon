"""Read compiled Java classes (``.class``) from mod JARs, without a JVM.

Mods define entity models in code, so their geometry lives in class files.
This module only parses: the constant pool, members, ``Code`` attributes and
bootstrap methods, plus a decoder for the instruction stream. Running that
code is ``java_vm``'s job. Nothing here imports Blender.
"""

from __future__ import annotations

import re
import struct
import zipfile


class ClassFormatError(ValueError):
    """The bytes are not a readable Java class file."""


MAX_CLASS_BYTES = 8 * 1024 * 1024


def _modified_utf8(data):
    """Decode the JVM's modified UTF-8 (``\\0`` as C0 80, surrogate pairs)."""
    units = []
    index = 0
    count = len(data)
    while index < count:
        byte = data[index]
        if byte < 0x80:
            units.append(byte)
            index += 1
        elif byte & 0xE0 == 0xC0 and index + 1 < count:
            units.append(((byte & 0x1F) << 6) | (data[index + 1] & 0x3F))
            index += 2
        elif byte & 0xF0 == 0xE0 and index + 2 < count:
            units.append(
                ((byte & 0x0F) << 12)
                | ((data[index + 1] & 0x3F) << 6)
                | (data[index + 2] & 0x3F)
            )
            index += 3
        else:
            raise ClassFormatError("invalid modified UTF-8 constant")
    raw = "".join(map(chr, units))
    return raw.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


class _Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data):
        self.data = data
        self.pos = 0

    def take(self, size):
        end = self.pos + size
        if end > len(self.data):
            raise ClassFormatError("class file is truncated")
        value = self.data[self.pos:end]
        self.pos = end
        return value

    def u1(self):
        return self.take(1)[0]

    def u2(self):
        return struct.unpack(">H", self.take(2))[0]

    def u4(self):
        return struct.unpack(">I", self.take(4))[0]


class Member:
    """A field or method: access flags, name, descriptor and raw attributes."""

    __slots__ = ("access", "name", "desc", "attributes", "_code")

    def __init__(self, access, name, desc, attributes):
        self.access = access
        self.name = name
        self.desc = desc
        self.attributes = attributes
        self._code = None

    @property
    def is_static(self):
        return bool(self.access & 0x0008)

    @property
    def is_abstract(self):
        return bool(self.access & 0x0400)


class CodeAttribute:
    __slots__ = ("max_stack", "max_locals", "code", "handlers", "_decoded")

    def __init__(self, max_stack, max_locals, code, handlers):
        self.max_stack = max_stack
        self.max_locals = max_locals
        self.code = code
        # (start_pc, end_pc, handler_pc, catch type or None for "any")
        self.handlers = handlers
        self._decoded = None

    @property
    def instructions(self):
        if self._decoded is None:
            self._decoded = decode_instructions(self.code)
        return self._decoded


class NameRemapper:
    """Renames Fabric's and Forge's Minecraft references to Mojang's names on load.

    Fabric mods for 1.14 to 1.21.11 call Minecraft as ``net/minecraft/class_5607``
    and ``method_32110``, Forge 1.17 to 1.20.x mods as ``m_171419_``; the
    importer's hosts know Mojang's names (``LayerDefinition.create``). Those
    names are unique across Minecraft, so class names, member names and the
    class names inside descriptors and signatures are renamed wherever they
    appear. Classes Mojang renamed later (``Identifier``) keep the name the
    hosts use.
    """

    _CLASS = re.compile(r"net/minecraft/class_\d+(?:\$class_\d+)*")
    _MEMBER = re.compile(r"^(?:(?:method|field|comp)_\d+|[mf]_\d+_)$")

    def __init__(self, classes, members, aliases=None):
        self.classes = classes
        self.members = members
        self.aliases = dict(aliases or {})

    def _class(self, match):
        name = match.group(0)
        return self.classes.get(name, name)

    def __call__(self, text):
        if "class_" in text:
            text = self._CLASS.sub(self._class, text)
        elif text[:1] in "mfc" and self._MEMBER.match(text):
            return self.members.get(text, text)
        for old, new in self.aliases.items():
            if old in text:
                text = text.replace(old, new)
        return text


_DEFAULT_REMAPPER = None


def default_remapper():
    """Intermediary names to Mojang's (see ``intermediary_names``), built once."""
    global _DEFAULT_REMAPPER
    if _DEFAULT_REMAPPER is None:
        from .intermediary_names import CLASSES, MEMBERS

        _DEFAULT_REMAPPER = NameRemapper(
            {"net/minecraft/" + key: value for key, value in CLASSES.items()},
            MEMBERS,
            # 1.21.11 renamed ResourceLocation; 26.x mods use the new name.
            {"net/minecraft/resources/Identifier": "net/minecraft/resources/ResourceLocation"},
        )
    return _DEFAULT_REMAPPER


class ClassFile:
    """One parsed class. Names use the JVM's internal ``a/b/C`` form.

    ``remap`` renames every string in the constant pool (``NameRemapper``).
    """

    def __init__(self, data, remap=None):
        if len(data) > MAX_CLASS_BYTES:
            raise ClassFormatError("class file is too large")
        reader = _Reader(data)
        if reader.take(4) != b"\xca\xfe\xba\xbe":
            raise ClassFormatError("not a Java class file")
        self.minor_version = reader.u2()
        self.major_version = reader.u2()
        self.constants = self._read_constants(reader, remap)
        self.access = reader.u2()
        self.name = self.class_name(reader.u2())
        super_index = reader.u2()
        self.super_name = self.class_name(super_index) if super_index else None
        self.interfaces = tuple(
            self.class_name(reader.u2()) for _ in range(reader.u2())
        )
        self.fields = self._read_members(reader)
        self.methods = self._read_members(reader)
        self.attributes = self._read_attributes(reader)
        self._bootstrap = None

    # -- constant pool -------------------------------------------------------

    def _read_constants(self, reader, remap=None):
        count = reader.u2()
        constants = [None] * count
        index = 1
        while index < count:
            tag = reader.u1()
            if tag == 1:
                value = _modified_utf8(reader.take(reader.u2()))
                constants[index] = ("Utf8", remap(value) if remap is not None else value)
            elif tag == 3:
                constants[index] = ("Integer", struct.unpack(">i", reader.take(4))[0])
            elif tag == 4:
                constants[index] = ("Float", struct.unpack(">f", reader.take(4))[0])
            elif tag == 5:
                constants[index] = ("Long", struct.unpack(">q", reader.take(8))[0])
                index += 1
            elif tag == 6:
                constants[index] = ("Double", struct.unpack(">d", reader.take(8))[0])
                index += 1
            elif tag in (7, 8, 16, 19, 20):
                kind = {7: "Class", 8: "String", 16: "MethodType",
                        19: "Module", 20: "Package"}[tag]
                constants[index] = (kind, reader.u2())
            elif tag in (9, 10, 11, 12, 17, 18):
                kind = {9: "Fieldref", 10: "Methodref", 11: "InterfaceMethodref",
                        12: "NameAndType", 17: "Dynamic", 18: "InvokeDynamic"}[tag]
                constants[index] = (kind, reader.u2(), reader.u2())
            elif tag == 15:
                constants[index] = ("MethodHandle", reader.u1(), reader.u2())
            else:
                raise ClassFormatError(f"unknown constant pool tag {tag}")
            index += 1
        return constants

    def _entry(self, index, *kinds):
        try:
            entry = self.constants[index]
        except (IndexError, TypeError):
            entry = None
        if entry is None or (kinds and entry[0] not in kinds):
            raise ClassFormatError(f"bad constant pool reference {index}")
        return entry

    def utf8(self, index):
        return self._entry(index, "Utf8")[1]

    def class_name(self, index):
        return self.utf8(self._entry(index, "Class")[1])

    def name_and_type(self, index):
        _kind, name_index, desc_index = self._entry(index, "NameAndType")
        return self.utf8(name_index), self.utf8(desc_index)

    def member_ref(self, index):
        """``(owner, name, descriptor)`` of a field or method reference."""
        _kind, class_index, nat_index = self._entry(
            index, "Fieldref", "Methodref", "InterfaceMethodref")
        name, desc = self.name_and_type(nat_index)
        return self.class_name(class_index), name, desc

    def loadable(self, index):
        """Decode an ``ldc`` operand: ``(kind, value)``."""
        entry = self._entry(index)
        kind = entry[0]
        if kind in ("Integer", "Float", "Long", "Double"):
            return kind, entry[1]
        if kind == "String":
            return kind, self.utf8(entry[1])
        if kind == "Class":
            return kind, self.utf8(entry[1])
        if kind == "MethodType":
            return kind, self.utf8(entry[1])
        if kind == "MethodHandle":
            return kind, self.method_handle(index)
        return kind, None

    def method_handle(self, index):
        _kind, ref_kind, ref_index = self._entry(index, "MethodHandle")
        owner, name, desc = self.member_ref(ref_index)
        return ref_kind, owner, name, desc

    def string_constants(self):
        return [
            self.utf8(entry[1]) for entry in self.constants
            if entry is not None and entry[0] == "String"
        ]

    def class_references(self):
        return {
            self.utf8(entry[1]) for entry in self.constants
            if entry is not None and entry[0] == "Class"
        }

    # -- members and attributes ----------------------------------------------

    def _read_attributes(self, reader):
        attributes = {}
        for _ in range(reader.u2()):
            name = self.utf8(reader.u2())
            payload = reader.take(reader.u4())
            attributes.setdefault(name, payload)
        return attributes

    def _read_members(self, reader):
        members = []
        for _ in range(reader.u2()):
            access = reader.u2()
            name = self.utf8(reader.u2())
            desc = self.utf8(reader.u2())
            members.append(Member(access, name, desc, self._read_attributes(reader)))
        return members

    def find_method(self, name, desc=None):
        for method in self.methods:
            if method.name == name and (desc is None or method.desc == desc):
                return method
        return None

    def find_field(self, name):
        for field in self.fields:
            if field.name == name:
                return field
        return None

    def code(self, method):
        """The method's ``CodeAttribute``, or ``None`` (abstract/native)."""
        if method._code is not None:
            return method._code
        raw = method.attributes.get("Code")
        if raw is None:
            return None
        reader = _Reader(raw)
        max_stack = reader.u2()
        max_locals = reader.u2()
        code = reader.take(reader.u4())
        handlers = []
        for _ in range(reader.u2()):
            start, end, handler, catch_index = (
                reader.u2(), reader.u2(), reader.u2(), reader.u2())
            catch = self.class_name(catch_index) if catch_index else None
            handlers.append((start, end, handler, catch))
        method._code = CodeAttribute(max_stack, max_locals, code, tuple(handlers))
        return method._code

    def constant_value(self, field):
        """A static final field's ``ConstantValue``, or ``None``."""
        raw = field.attributes.get("ConstantValue")
        if raw is None or len(raw) < 2:
            return None
        return self.loadable(struct.unpack(">H", raw[:2])[0])[1]

    def bootstrap_method(self, index):
        """``(method handle, [static arguments])`` for invokedynamic."""
        if self._bootstrap is None:
            self._bootstrap = []
            raw = self.attributes.get("BootstrapMethods")
            if raw:
                reader = _Reader(raw)
                for _ in range(reader.u2()):
                    handle = reader.u2()
                    arguments = [reader.u2() for _ in range(reader.u2())]
                    self._bootstrap.append((handle, arguments))
        handle, arguments = self._bootstrap[index]
        return (
            self.method_handle(handle),
            [self.loadable(argument) for argument in arguments],
        )

    def invoke_dynamic(self, index):
        """``(bootstrap index, name, descriptor)`` of an InvokeDynamic entry."""
        _kind, bootstrap_index, nat_index = self._entry(index, "InvokeDynamic", "Dynamic")
        name, desc = self.name_and_type(nat_index)
        return bootstrap_index, name, desc

    @property
    def signature(self):
        """The generic ``Signature`` attribute, or ``""``."""
        raw = self.attributes.get("Signature")
        if not raw or len(raw) < 2:
            return ""
        return self.utf8(struct.unpack(">H", raw[:2])[0])


# -- descriptors ---------------------------------------------------------------

def method_argument_types(desc):
    """Split a method descriptor into its argument type descriptors."""
    if not desc.startswith("("):
        raise ClassFormatError(f"bad method descriptor {desc}")
    types = []
    index = 1
    while desc[index] != ")":
        start = index
        while desc[index] == "[":
            index += 1
        if desc[index] == "L":
            index = desc.index(";", index)
        index += 1
        types.append(desc[start:index])
    return types


def method_return_type(desc):
    return desc[desc.index(")") + 1:]


def is_wide_type(type_desc):
    return type_desc in ("J", "D")


# -- instructions --------------------------------------------------------------

_OPCODES = {}


def _define(code, name, operands=""):
    _OPCODES[code] = (name, operands)


def _define_run(start, names, operands=""):
    for offset, name in enumerate(names.split()):
        _define(start + offset, name, operands)


_define_run(0x00, "nop aconst_null iconst_m1 iconst_0 iconst_1 iconst_2 iconst_3 "
                  "iconst_4 iconst_5 lconst_0 lconst_1 fconst_0 fconst_1 fconst_2 "
                  "dconst_0 dconst_1")
_define(0x10, "bipush", "s1")
_define(0x11, "sipush", "s2")
_define(0x12, "ldc", "u1")
_define(0x13, "ldc_w", "u2")
_define(0x14, "ldc2_w", "u2")
_define_run(0x15, "iload lload fload dload aload", "u1")
_define_run(0x1A, " ".join(f"{t}load_{n}" for t in "ilfda" for n in range(4)))
_define_run(0x2E, "iaload laload faload daload aaload baload caload saload")
_define_run(0x36, "istore lstore fstore dstore astore", "u1")
_define_run(0x3B, " ".join(f"{t}store_{n}" for t in "ilfda" for n in range(4)))
_define_run(0x4F, "iastore lastore fastore dastore aastore bastore castore sastore")
_define_run(0x57, "pop pop2 dup dup_x1 dup_x2 dup2 dup2_x1 dup2_x2 swap")
_define_run(0x60, " ".join(t + op for op in ("add", "sub", "mul", "div", "rem", "neg")
                           for t in "ilfd"))
_define_run(0x78, "ishl lshl ishr lshr iushr lushr iand land ior lor ixor lxor")
_define(0x84, "iinc", "iinc")
_define_run(0x85, "i2l i2f i2d l2i l2f l2d f2i f2l f2d d2i d2l d2f i2b i2c i2s")
_define_run(0x94, "lcmp fcmpl fcmpg dcmpl dcmpg")
_define_run(0x99, "ifeq ifne iflt ifge ifgt ifle if_icmpeq if_icmpne if_icmplt "
                  "if_icmpge if_icmpgt if_icmple if_acmpeq if_acmpne goto jsr", "b2")
_define(0xA9, "ret", "u1")
_define(0xAA, "tableswitch", "tableswitch")
_define(0xAB, "lookupswitch", "lookupswitch")
_define_run(0xAC, "ireturn lreturn freturn dreturn areturn return")
_define_run(0xB2, "getstatic putstatic getfield putfield invokevirtual "
                  "invokespecial invokestatic", "u2")
_define(0xB9, "invokeinterface", "invokeinterface")
_define(0xBA, "invokedynamic", "invokedynamic")
_define(0xBB, "new", "u2")
_define(0xBC, "newarray", "u1")
_define(0xBD, "anewarray", "u2")
_define_run(0xBE, "arraylength athrow")
_define_run(0xC0, "checkcast instanceof", "u2")
_define_run(0xC2, "monitorenter monitorexit")
_define(0xC4, "wide", "wide")
_define(0xC5, "multianewarray", "multianewarray")
_define_run(0xC6, "ifnull ifnonnull", "b2")
_define_run(0xC8, "goto_w jsr_w", "b4")


def decode_instructions(code):
    """Decode bytecode into ``{pc: (name, operands, next_pc)}``.

    Branch operands are absolute target offsets. ``wide`` is folded into the
    instruction it widens.
    """
    result = {}
    pc = 0
    size = len(code)

    def signed(fmt, start, length):
        if start + length > size:
            raise ClassFormatError("truncated instruction")
        return struct.unpack(fmt, code[start:start + length])[0]

    while pc < size:
        opcode = code[pc]
        if opcode not in _OPCODES:
            raise ClassFormatError(f"unknown opcode 0x{opcode:02x} at {pc}")
        name, kind = _OPCODES[opcode]
        cursor = pc + 1
        operands = ()
        if kind == "s1":
            operands = (signed(">b", cursor, 1),)
            cursor += 1
        elif kind == "s2":
            operands = (signed(">h", cursor, 2),)
            cursor += 2
        elif kind == "u1":
            operands = (signed(">B", cursor, 1),)
            cursor += 1
        elif kind == "u2":
            operands = (signed(">H", cursor, 2),)
            cursor += 2
        elif kind == "iinc":
            operands = (signed(">B", cursor, 1), signed(">b", cursor + 1, 1))
            cursor += 2
        elif kind == "b2":
            operands = (pc + signed(">h", cursor, 2),)
            cursor += 2
        elif kind == "b4":
            operands = (pc + signed(">i", cursor, 4),)
            cursor += 4
        elif kind == "invokeinterface":
            operands = (signed(">H", cursor, 2), signed(">B", cursor + 2, 1))
            cursor += 4
        elif kind == "invokedynamic":
            operands = (signed(">H", cursor, 2),)
            cursor += 4
        elif kind == "multianewarray":
            operands = (signed(">H", cursor, 2), signed(">B", cursor + 2, 1))
            cursor += 3
        elif kind == "tableswitch":
            cursor = pc + 1 + (-(pc + 1) % 4)
            default = signed(">i", cursor, 4)
            low = signed(">i", cursor + 4, 4)
            high = signed(">i", cursor + 8, 4)
            cursor += 12
            if high < low or high - low > 65535:
                raise ClassFormatError("bad tableswitch")
            targets = tuple(
                pc + signed(">i", cursor + 4 * offset, 4)
                for offset in range(high - low + 1)
            )
            cursor += 4 * (high - low + 1)
            operands = (pc + default, low, targets)
        elif kind == "lookupswitch":
            cursor = pc + 1 + (-(pc + 1) % 4)
            default = signed(">i", cursor, 4)
            pairs = signed(">i", cursor + 4, 4)
            cursor += 8
            if pairs < 0 or pairs > 65535:
                raise ClassFormatError("bad lookupswitch")
            table = {}
            for _ in range(pairs):
                table[signed(">i", cursor, 4)] = pc + signed(">i", cursor + 4, 4)
                cursor += 8
            operands = (pc + default, table)
        elif kind == "wide":
            inner = signed(">B", cursor, 1)
            if inner not in _OPCODES:
                raise ClassFormatError("bad wide instruction")
            name = _OPCODES[inner][0]
            if name == "iinc":
                operands = (signed(">H", cursor + 1, 2), signed(">h", cursor + 3, 2))
                cursor += 5
            else:
                operands = (signed(">H", cursor + 1, 2),)
                cursor += 3
        result[pc] = (name, operands, cursor)
        pc = cursor
    return result


# -- class paths ---------------------------------------------------------------

# Forge's SRG (m_171430_) and Fabric's intermediary (method_32011) method names.
_SRG_METHOD = re.compile(r"^(?:m_\d+_|method_\d+)$")


class VanillaModelLibrary:
    """Vanilla's model-definition code (``net/minecraft/client/model/``) from a
    Mojang-named client JAR (26.1+ ship unobfuscated), for the static mesh
    factories mods build on, such as ``HumanoidModel.createMesh``.

    A class that moved package between versions is found by its simple name;
    a Forge (SRG) method name by the class's only static method with that
    descriptor.
    """

    PREFIX = "net/minecraft/client/model/"

    def __init__(self, class_path):
        self.class_path = class_path
        self._by_simple_name = {}
        for name in class_path.names():
            if name.startswith(self.PREFIX):
                self._by_simple_name.setdefault(name.rsplit("/", 1)[-1], []).append(name)

    @classmethod
    def open(cls, path):
        return cls(JarClassPath.open(path))

    def _class(self, name):
        if not name.startswith(self.PREFIX):
            return None
        if name in self.class_path:
            return self.class_path.get(name)
        candidates = self._by_simple_name.get(name.rsplit("/", 1)[-1], [])
        return self.class_path.get(candidates[0]) if len(candidates) == 1 else None

    def static_method(self, owner, name, desc):
        """``(ClassFile, Member)`` of a static factory, or ``None``."""
        parsed = self._class(owner)
        if parsed is None:
            return None
        method = parsed.find_method(name, desc)
        if method is None and _SRG_METHOD.match(name):
            matches = [candidate for candidate in parsed.methods
                       if candidate.is_static and candidate.desc == desc]
            method = matches[0] if len(matches) == 1 else None
        if method is None or not method.is_static or parsed.code(method) is None:
            return None
        return parsed, method

class JarClassPath:
    """Lazily parsed classes of one JAR, looked up by internal name.

    Classes are read through ``remap`` (default: intermediary to Mojang
    names, a no-op for Forge, NeoForge and 26.x JARs); ``remap=None`` reads
    them as written.
    """

    _DEFAULT = object()

    def __init__(self, archive, remap=_DEFAULT):
        self.archive = archive
        self.remap = default_remapper() if remap is self._DEFAULT else remap
        self._infos = {
            (self.remap(info.filename[:-6]) if self.remap else info.filename[:-6]): info
            for info in archive.infolist()
            if info.filename.endswith(".class") and not info.is_dir()
            and not info.filename.startswith("META-INF/")
        }
        self._parsed = {}
        self.errors = {}

    @classmethod
    def open(cls, path, remap=_DEFAULT):
        return cls(zipfile.ZipFile(path, "r"), remap)

    def close(self):
        self.archive.close()

    def __contains__(self, name):
        return name in self._infos

    def names(self):
        return sorted(self._infos)

    def get(self, name):
        """The parsed class, or ``None`` when absent or unreadable."""
        if name in self._parsed:
            return self._parsed[name]
        info = self._infos.get(name)
        parsed = None
        if info is not None and info.file_size <= MAX_CLASS_BYTES:
            try:
                parsed = ClassFile(self.archive.read(info), self.remap)
            except (ClassFormatError, zipfile.BadZipFile, OSError, KeyError) as exc:
                self.errors[name] = str(exc)
        self._parsed[name] = parsed
        return parsed

    def superclasses(self, name):
        """``name`` and its loadable ancestors, then the first external one."""
        chain = []
        seen = set()
        while name and name not in seen:
            seen.add(name)
            chain.append(name)
            parsed = self.get(name)
            if parsed is None:
                break
            name = parsed.super_name
        return chain
