"""Litematica, Axiom, Sponge/WorldEdit and legacy MCEdit schematic importer.

Dependency-free: the NBT reader is hand-rolled on ``gzip`` + ``struct`` because
Blender ships a stock Python with no ``nbtlib`` / ``amulet-core`` and this add-on
is distributed as a plain zip.

This module decodes a schematic into a chunk-based spatial hash and prints a
summary to the system console. The decoded world is retained in a module-level
store (see :func:`get_loaded_world`) so a later mesh-building pass can read it
without re-parsing the file.
"""

import gzip
import io
import json
import os
import struct
import time
from collections import Counter
from functools import lru_cache

try:
    import bpy
    from bpy.types import Operator, Panel
    from bpy_extras.io_utils import ImportHelper
except ImportError:  # pragma: no cover - plain Python test path
    class _DummyProps:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    class _DummyBpy:
        props = _DummyProps()

        def __bool__(self):
            return False

    class Operator:
        pass

    class Panel:
        pass

    class ImportHelper:
        pass

    bpy = _DummyBpy()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHUNK_SIZE = 16
_CHUNK_SHIFT = 4
_CHUNK_MASK = CHUNK_SIZE - 1

_UINT64 = 0xFFFFFFFFFFFFFFFF

AIR_BLOCKS = frozenset((
    "minecraft:air",
    "minecraft:cave_air",
    "minecraft:void_air",
    "air",
    "cave_air",
    "void_air",
))

_LOG_PREFIX = "[Sparkle][Schematic]"

# Guards against a corrupt/hostile file locking up Blender.
_MAX_NBT_DEPTH = 512
_MAX_ARRAY_LEN = 1 << 28


# ---------------------------------------------------------------------------
# NBT tag ids
# ---------------------------------------------------------------------------

TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11
TAG_LONG_ARRAY = 12

_TAG_NAMES = {
    TAG_END: "End", TAG_BYTE: "Byte", TAG_SHORT: "Short", TAG_INT: "Int",
    TAG_LONG: "Long", TAG_FLOAT: "Float", TAG_DOUBLE: "Double",
    TAG_BYTE_ARRAY: "ByteArray", TAG_STRING: "String", TAG_LIST: "List",
    TAG_COMPOUND: "Compound", TAG_INT_ARRAY: "IntArray",
    TAG_LONG_ARRAY: "LongArray",
}

_S_BYTE = struct.Struct(">b")
_S_UBYTE = struct.Struct(">B")
_S_SHORT = struct.Struct(">h")
_S_USHORT = struct.Struct(">H")
_S_INT = struct.Struct(">i")
_S_LONG = struct.Struct(">q")
_S_FLOAT = struct.Struct(">f")
_S_DOUBLE = struct.Struct(">d")


class NBTError(Exception):
    """Raised when a file is not valid NBT or is truncated."""


class SchematicError(Exception):
    """Raised when a file is not a usable supported schematic."""


# ---------------------------------------------------------------------------
# NBT reader
# ---------------------------------------------------------------------------

class _NBTReader:
    """Recursive big-endian NBT reader over a single in-memory buffer."""

    __slots__ = ("data", "pos", "size")

    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.size = len(data)

    # -- primitives ---------------------------------------------------------

    def _need(self, count):
        """Raise before struct does, so the error names the real problem."""
        if self.pos + count > self.size:
            raise NBTError(
                "Truncated NBT: needed %d bytes at offset %d, file has %d"
                % (count, self.pos, self.size)
            )

    def read_byte(self):
        self._need(1)
        value = _S_BYTE.unpack_from(self.data, self.pos)[0]
        self.pos += 1
        return value

    def read_ubyte(self):
        self._need(1)
        value = _S_UBYTE.unpack_from(self.data, self.pos)[0]
        self.pos += 1
        return value

    def read_short(self):
        self._need(2)
        value = _S_SHORT.unpack_from(self.data, self.pos)[0]
        self.pos += 2
        return value

    def read_ushort(self):
        self._need(2)
        value = _S_USHORT.unpack_from(self.data, self.pos)[0]
        self.pos += 2
        return value

    def read_int(self):
        self._need(4)
        value = _S_INT.unpack_from(self.data, self.pos)[0]
        self.pos += 4
        return value

    def read_long(self):
        self._need(8)
        value = _S_LONG.unpack_from(self.data, self.pos)[0]
        self.pos += 8
        return value

    def read_float(self):
        self._need(4)
        value = _S_FLOAT.unpack_from(self.data, self.pos)[0]
        self.pos += 4
        return value

    def read_double(self):
        self._need(8)
        value = _S_DOUBLE.unpack_from(self.data, self.pos)[0]
        self.pos += 8
        return value

    def read_string(self):
        length = self.read_ushort()
        self._need(length)
        raw = self.data[self.pos:self.pos + length]
        self.pos += length
        return _decode_modified_utf8(raw)

    # -- arrays -------------------------------------------------------------

    def _read_array(self, item_size, code):
        count = self.read_int()
        if count < 0 or count > _MAX_ARRAY_LEN:
            raise NBTError("Implausible NBT array length %d at offset %d" % (count, self.pos))
        total = count * item_size
        self._need(total)
        values = list(struct.unpack_from(">%d%s" % (count, code), self.data, self.pos))
        self.pos += total
        return values

    # -- structure ----------------------------------------------------------

    def read_payload(self, tag_id, depth=0):
        if depth > _MAX_NBT_DEPTH:
            raise NBTError("NBT nesting deeper than %d levels" % _MAX_NBT_DEPTH)

        if tag_id == TAG_BYTE:
            return self.read_byte()
        if tag_id == TAG_SHORT:
            return self.read_short()
        if tag_id == TAG_INT:
            return self.read_int()
        if tag_id == TAG_LONG:
            return self.read_long()
        if tag_id == TAG_FLOAT:
            return self.read_float()
        if tag_id == TAG_DOUBLE:
            return self.read_double()
        if tag_id == TAG_STRING:
            return self.read_string()
        if tag_id == TAG_BYTE_ARRAY:
            return self._read_array(1, "b")
        if tag_id == TAG_INT_ARRAY:
            return self._read_array(4, "i")
        if tag_id == TAG_LONG_ARRAY:
            # Kept signed; masked to unsigned only where bits are unpacked.
            return self._read_array(8, "q")
        if tag_id == TAG_LIST:
            return self._read_list(depth)
        if tag_id == TAG_COMPOUND:
            return self._read_compound(depth)
        if tag_id == TAG_END:
            return None
        raise NBTError("Unknown NBT tag id %d at offset %d" % (tag_id, self.pos))

    def _read_list(self, depth):
        item_type = self.read_ubyte()
        count = self.read_int()
        if count <= 0:
            return []
        if count > _MAX_ARRAY_LEN:
            raise NBTError("Implausible NBT list length %d" % count)
        if item_type == TAG_END:
            # Malformed but seen in the wild; treat as empty rather than bailing.
            return []
        return [self.read_payload(item_type, depth + 1) for _ in range(count)]

    def _read_compound(self, depth):
        result = {}
        while True:
            tag_id = self.read_ubyte()
            if tag_id == TAG_END:
                return result
            name = self.read_string()
            result[name] = self.read_payload(tag_id, depth + 1)

    def read_root(self):
        """Read the unnamed root compound that wraps every NBT file."""
        tag_id = self.read_ubyte()
        if tag_id != TAG_COMPOUND:
            raise NBTError(
                "Root tag is %s, expected Compound - not an NBT file"
                % _TAG_NAMES.get(tag_id, "id %d" % tag_id)
            )
        self.read_string()  # Root name, conventionally empty.
        return self._read_compound(0)


def _decode_modified_utf8(raw):
    """Decode Java's modified UTF-8 (NUL as 0xC0 0x80, CESU-8 surrogates)."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.replace(b"\xc0\x80", b"\x00").decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace")


def load_nbt(path):
    """Load an NBT file, gzip-compressed or raw, and return the root compound."""
    try:
        with gzip.open(path, "rb") as handle:
            data = handle.read()
    except (OSError, EOFError):
        # Some tools write .litematic uncompressed; fall back to raw bytes.
        with open(path, "rb") as handle:
            data = handle.read()
    if not data:
        raise NBTError("File is empty: %s" % path)
    return _NBTReader(data).read_root()


# ---------------------------------------------------------------------------
# Bit unpacking
# ---------------------------------------------------------------------------

def spanning_long_count(volume, bits):
    """Longs needed when entries pack solid, straddling long boundaries."""
    return -(-(volume * bits) // 64)


def padded_long_count(volume, bits):
    """Longs needed when each long pads its tail bits (vanilla 1.16+ chunks)."""
    per_long = 64 // bits
    return -(-volume // per_long) if per_long else 0


def detect_packing(long_count, volume, bits):
    """Pick the unpacker from the array length. Returns (padded, note).

    Litematica always uses the spanning layout - its ``LitematicaBitArray`` is
    unchanged across Minecraft versions, and the format author describes the
    data as packed "such that there are no unused bits left anywhere except
    possibly at the end of the last long value in the array". The 1.16 padding
    change applies to vanilla's chunk PalettedContainer, NOT to this format, so
    branching on MinecraftDataVersion decodes every modern schematic to garbage.

    The two layouts need different numbers of longs whenever ``bits`` does not
    divide 64, so the array length identifies the layout outright instead of
    guessing from a version number.
    """
    spanning = spanning_long_count(volume, bits)
    padded = padded_long_count(volume, bits)
    if 64 % bits == 0:
        # No bits are ever wasted, so the two readers are bit-identical here.
        return False, "spanning (readers identical at %d bits)" % bits
    if spanning == padded:
        # Lengths collide for this volume even though the readers differ; the
        # documented Litematica layout wins and the id validator double-checks.
        return False, "spanning (length %d ambiguous, format default)" % spanning
    if long_count == spanning:
        return False, "spanning"
    if long_count == padded:
        return True, "padded (unusual for .litematic - vanilla chunk layout)"
    return False, (
        "spanning (assumed: %d longs matches neither spanning=%d nor padded=%d)"
        % (long_count, spanning, padded)
    )


def bits_per_entry(palette_size):
    """Litematica uses at least 2 bits per entry, else ceil(log2(size))."""
    if palette_size < 2:
        return 2
    return max(2, (palette_size - 1).bit_length())


def _iter_padded(longs, bits, count):
    """Yield palette ids where entries never cross a long boundary (1.16+)."""
    mask = (1 << bits) - 1
    per_long = 64 // bits
    long_count = len(longs)
    for index in range(count):
        arr_index = index // per_long
        if arr_index >= long_count:
            yield 0  # Truncated BlockStates: report air rather than crashing.
            continue
        yield (longs[arr_index] >> ((index % per_long) * bits)) & mask


def _iter_spanning(longs, bits, count):
    """Yield palette ids where entries may straddle two longs (pre-1.16)."""
    mask = (1 << bits) - 1
    long_count = len(longs)
    for index in range(count):
        start_offset = index * bits
        start = start_offset >> 6
        end = ((index + 1) * bits - 1) >> 6
        offset = start_offset & 63
        if start >= long_count:
            yield 0
            continue
        if start == end:
            yield (longs[start] >> offset) & mask
        elif end >= long_count:
            yield (longs[start] >> offset) & mask
        else:
            yield ((longs[start] >> offset) | (longs[end] << (64 - offset))) & mask


def iter_block_ids(block_states, bits, count, padded):
    """Yield ``count`` palette ids from a Litematica BlockStates long array."""
    longs = mask_longs(block_states)
    if padded:
        return _iter_padded(longs, bits, count)
    return _iter_spanning(longs, bits, count)


def mask_longs(block_states):
    """NBT longs are signed; bit maths needs them unsigned."""
    return [value & _UINT64 for value in block_states]


def _get_padded(longs, bits, index):
    per_long = 64 // bits
    arr_index = index // per_long
    if arr_index >= len(longs):
        return 0
    return (longs[arr_index] >> ((index % per_long) * bits)) & ((1 << bits) - 1)


def _get_spanning(longs, bits, index):
    mask = (1 << bits) - 1
    start_offset = index * bits
    start = start_offset >> 6
    end = ((index + 1) * bits - 1) >> 6
    offset = start_offset & 63
    if start >= len(longs):
        return 0
    if start == end or end >= len(longs):
        return (longs[start] >> offset) & mask
    return ((longs[start] >> offset) | (longs[end] << (64 - offset))) & mask


def count_invalid_ids(longs, bits, volume, padded, palette_size, sample=8192):
    """Count sampled ids that fall outside the palette.

    A correct decode can never produce an id >= palette size, so a non-zero
    count is proof the wrong layout was chosen.
    """
    if volume <= 0 or palette_size <= 0:
        return 0
    step = max(1, volume // sample)
    getter = _get_padded if padded else _get_spanning
    invalid = 0
    for index in range(0, volume, step):
        if getter(longs, bits, index) >= palette_size:
            invalid += 1
    return invalid


def resolve_packing(block_states, bits, volume, palette_size):
    """Choose the unpacker for one region. Returns (longs, padded, note, invalid).

    Detection is by array length; the sampled out-of-range check is a second
    opinion that flips the choice rather than silently emitting garbage.
    """
    longs = mask_longs(block_states)
    padded, note = detect_packing(len(longs), volume, bits)
    invalid = count_invalid_ids(longs, bits, volume, padded, palette_size)
    if invalid:
        alternative = not padded
        alternative_invalid = count_invalid_ids(
            longs, bits, volume, alternative, palette_size
        )
        if alternative_invalid < invalid:
            note = "%s -> switched to %s (%d out-of-range ids)" % (
                note, "padded" if alternative else "spanning", invalid,
            )
            padded, invalid = alternative, alternative_invalid
    return longs, padded, note, invalid


# ---------------------------------------------------------------------------
# Block state strings
# ---------------------------------------------------------------------------

def _property_value(value):
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def block_state_string(entry):
    """Render one palette compound as ``minecraft:note_block[note=12,...]``."""
    if not isinstance(entry, dict):
        return "minecraft:air"
    name = entry.get("Name") or "minecraft:air"
    properties = entry.get("Properties")
    if not properties:
        return name
    body = ",".join(
        "%s=%s" % (key, _property_value(properties[key]))
        for key in sorted(properties)
    )
    return "%s[%s]" % (name, body)


def _state_base_name(state):
    """Name without the property suffix, so stateful air is still culled."""
    bracket = state.find("[")
    return state if bracket < 0 else state[:bracket]


# ---------------------------------------------------------------------------
# Decoded data model
# ---------------------------------------------------------------------------

class RegionInfo:
    """Per-region decode result and statistics."""

    __slots__ = (
        "name", "origin", "entity_origin", "size", "volume", "palette_size", "bits",
        "solid_blocks", "tile_entities", "entities",
        "padded", "packing_note", "long_count", "expected_longs", "invalid_ids",
    )

    def __init__(self, name):
        self.name = name
        self.origin = (0, 0, 0)
        self.entity_origin = (0, 0, 0)
        self.size = (0, 0, 0)
        self.volume = 0
        self.palette_size = 0
        self.bits = 0
        self.solid_blocks = 0
        self.tile_entities = []
        self.entities = []
        self.padded = False
        self.packing_note = ""
        self.long_count = 0
        self.expected_longs = 0
        self.invalid_ids = 0


class SchematicWorld:
    """Chunk-based spatial hash of every non-air block in a schematic.

    ``chunks`` maps a 16x16x16 chunk key to ``{(lx, ly, lz): state_string}``.
    Chunk keys use arithmetic shift so negative world coordinates floor
    correctly (``-1 >> 4 == -1``) and locals stay in 0..15 (``-1 & 15 == 15``).
    """

    __slots__ = (
        "filepath", "name", "author", "description", "data_version",
        "schematic_version", "padded", "regions", "chunks", "palette_counts",
        "total_blocks", "entity_count", "tile_entity_count", "bounds_min",
        "bounds_max", "parse_seconds", "packing_notes", "invalid_ids",
    )

    def __init__(self, filepath=""):
        self.filepath = filepath
        self.name = ""
        self.author = ""
        self.description = ""
        self.data_version = 0
        self.schematic_version = 0
        self.padded = False
        self.packing_notes = []
        self.invalid_ids = 0
        self.regions = []
        self.chunks = {}
        self.palette_counts = Counter()
        self.total_blocks = 0
        self.entity_count = 0
        self.tile_entity_count = 0
        self.bounds_min = None
        self.bounds_max = None
        self.parse_seconds = 0.0

    # -- writes -------------------------------------------------------------

    def note_packing(self, padded, note, invalid):
        """Record which unpacker a region used, for the console summary."""
        self.padded = padded
        self.invalid_ids += invalid
        if note and note not in self.packing_notes:
            self.packing_notes.append(note)

    def set_block(self, x, y, z, state):
        key = (x >> _CHUNK_SHIFT, y >> _CHUNK_SHIFT, z >> _CHUNK_SHIFT)
        chunk = self.chunks.get(key)
        if chunk is None:
            chunk = self.chunks[key] = {}
        chunk[(x & _CHUNK_MASK, y & _CHUNK_MASK, z & _CHUNK_MASK)] = state
        self.total_blocks += 1
        if self.bounds_min is None:
            self.bounds_min = [x, y, z]
            self.bounds_max = [x, y, z]
        else:
            low, high = self.bounds_min, self.bounds_max
            if x < low[0]:
                low[0] = x
            elif x > high[0]:
                high[0] = x
            if y < low[1]:
                low[1] = y
            elif y > high[1]:
                high[1] = y
            if z < low[2]:
                low[2] = z
            elif z > high[2]:
                high[2] = z

    # -- reads --------------------------------------------------------------

    def get_block(self, x, y, z):
        """Return the state string at a world coordinate, or None for air."""
        chunk = self.chunks.get(
            (x >> _CHUNK_SHIFT, y >> _CHUNK_SHIFT, z >> _CHUNK_SHIFT)
        )
        if not chunk:
            return None
        return chunk.get((x & _CHUNK_MASK, y & _CHUNK_MASK, z & _CHUNK_MASK))

    def iter_blocks(self):
        """Yield ``(x, y, z, state)`` in world coordinates, chunk by chunk."""
        for (cx, cy, cz), chunk in self.chunks.items():
            base_x = cx * CHUNK_SIZE
            base_y = cy * CHUNK_SIZE
            base_z = cz * CHUNK_SIZE
            for (lx, ly, lz), state in chunk.items():
                yield base_x + lx, base_y + ly, base_z + lz, state

    @property
    def chunk_count(self):
        return len(self.chunks)

    # -- reporting ----------------------------------------------------------

    def summary_lines(self):
        lines = []
        lines.append("=" * 68)
        lines.append("%s Loaded %s" % (_LOG_PREFIX, os.path.basename(self.filepath)))
        lines.append("=" * 68)
        if self.name:
            lines.append("  Name              : %s" % self.name)
        if self.author:
            lines.append("  Author            : %s" % self.author)
        if self.description:
            lines.append("  Description       : %s" % self.description)
        lines.append("  DataVersion       : %d (schematic version %d)"
                     % (self.data_version, self.schematic_version))
        lines.append("  Bit packing       : %s"
                     % ("; ".join(self.packing_notes) or "n/a"))
        if self.invalid_ids:
            lines.append("  !! OUT-OF-RANGE   : %d sampled ids exceed the palette - "
                         "the decode is WRONG" % self.invalid_ids)
        lines.append("  Parse time        : %.3fs" % self.parse_seconds)
        lines.append("")
        lines.append("  Regions processed : %d" % len(self.regions))
        for region in self.regions:
            lines.append(
                "    - %-24s origin=%s size=%dx%dx%d palette=%d bits=%d blocks=%d"
                % (
                    region.name[:24],
                    "(%d,%d,%d)" % region.origin,
                    region.size[0], region.size[1], region.size[2],
                    region.palette_size, region.bits, region.solid_blocks,
                )
            )
            lines.append(
                "      longs=%d expected=%d layout=%s%s"
                % (
                    region.long_count, region.expected_longs,
                    "padded" if region.padded else "spanning",
                    "" if region.long_count == region.expected_longs
                    else "  <-- LENGTH MISMATCH",
                )
            )
        lines.append("")
        lines.append("  Active chunks     : %d (16x16x16)" % len(self.chunks))
        lines.append("  Blocks stored     : %d (air culled)" % self.total_blocks)
        if self.bounds_min is not None:
            lines.append("  Bounds min        : (%d, %d, %d)" % tuple(self.bounds_min))
            lines.append("  Bounds max        : (%d, %d, %d)" % tuple(self.bounds_max))
        lines.append("")
        lines.append("  Block palette     : %d unique states" % len(self.palette_counts))
        for state, count in self.palette_counts.most_common():
            lines.append("    %8d  %s" % (count, state))
        lines.append("")
        lines.append("  Tile entities     : %d" % self.tile_entity_count)
        lines.append("  Entities          : %d" % self.entity_count)
        lines.append("=" * 68)
        return lines

    def print_summary(self):
        for line in self.summary_lines():
            print(line)


# ---------------------------------------------------------------------------
# Schematic decoding
# ---------------------------------------------------------------------------

def _vector(compound, default=(0, 0, 0)):
    if not isinstance(compound, dict):
        return default
    return (
        int(compound.get("x", default[0]) or 0),
        int(compound.get("y", default[1]) or 0),
        int(compound.get("z", default[2]) or 0),
    )


def _normalize_axis(position, size):
    """Return ``(origin, extent)``; a negative size grows the other way."""
    if size < 0:
        return position + size + 1, -size
    return position, size


def _decode_region(name, region, world):
    """Decode one selection box into the world's spatial hash."""
    info = RegionInfo(name)
    if not isinstance(region, dict):
        return info

    position = _vector(region.get("Position"))
    raw_size = _vector(region.get("Size"))
    origin_x, size_x = _normalize_axis(position[0], raw_size[0])
    origin_y, size_y = _normalize_axis(position[1], raw_size[1])
    origin_z, size_z = _normalize_axis(position[2], raw_size[2])

    info.origin = (origin_x, origin_y, origin_z)
    info.entity_origin = tuple(position)
    info.size = (size_x, size_y, size_z)
    info.volume = size_x * size_y * size_z

    tile_entities = region.get("TileEntities") or []
    entities = region.get("Entities") or []
    info.tile_entities = tile_entities if isinstance(tile_entities, list) else []
    info.entities = entities if isinstance(entities, list) else []
    world.tile_entity_count += len(info.tile_entities)
    world.entity_count += len(info.entities)

    palette = region.get("BlockStatePalette") or []
    if not isinstance(palette, list) or not palette:
        return info
    info.palette_size = len(palette)

    if info.volume <= 0:
        return info

    block_states = region.get("BlockStates") or []
    if not isinstance(block_states, list) or not block_states:
        return info

    # Resolve the palette once: per-block work is then a list index.
    states = [block_state_string(entry) for entry in palette]
    is_air = [_state_base_name(state) in AIR_BLOCKS for state in states]

    info.bits = bits_per_entry(len(palette))
    longs, padded, note, invalid = resolve_packing(
        block_states, info.bits, info.volume, len(palette)
    )
    info.padded = padded
    info.packing_note = note
    info.long_count = len(longs)
    info.expected_longs = (
        padded_long_count(info.volume, info.bits) if padded
        else spanning_long_count(info.volume, info.bits)
    )
    info.invalid_ids = invalid
    world.note_packing(padded, note, invalid)
    ids = _iter_padded(longs, info.bits, info.volume) if padded \
        else _iter_spanning(longs, info.bits, info.volume)

    next_id = ids.__next__
    set_block = world.set_block
    counts = world.palette_counts
    palette_len = len(states)
    solid = 0

    for y in range(size_y):
        world_y = origin_y + y
        for z in range(size_z):
            world_z = origin_z + z
            for x in range(size_x):
                index = next_id()
                if index >= palette_len or index < 0:
                    continue  # Corrupt id; skip rather than mis-place a block.
                if is_air[index]:
                    continue
                state = states[index]
                set_block(origin_x + x, world_y, world_z, state)
                counts[state] += 1
                solid += 1

    info.solid_blocks = solid
    return info


def parse_litematic(filepath):
    """Parse a ``.litematic`` file into a :class:`SchematicWorld`."""
    started = time.time()
    root = load_nbt(filepath)
    if not isinstance(root, dict):
        raise SchematicError("Root NBT tag is not a compound")

    regions = root.get("Regions")
    if not isinstance(regions, dict) or not regions:
        raise SchematicError(
            "No 'Regions' compound found - not a Litematica schematic"
        )

    world = SchematicWorld(filepath)
    world.data_version = int(root.get("MinecraftDataVersion") or 0)
    world.schematic_version = int(root.get("Version") or 0)

    metadata = root.get("Metadata")
    if isinstance(metadata, dict):
        world.name = str(metadata.get("Name") or "")
        world.author = str(metadata.get("Author") or "")
        world.description = str(metadata.get("Description") or "")
    if not world.name:
        world.name = os.path.splitext(os.path.basename(filepath))[0]

    for region_name in sorted(regions):
        world.regions.append(_decode_region(region_name, regions[region_name], world))

    world.parse_seconds = time.time() - started
    return world


# ---------------------------------------------------------------------------
# Axiom blueprints
# ---------------------------------------------------------------------------

def parse_axiom_blueprint(filepath):
    """Decode Axiom's framed NBT container and padded 16-cube block sections.

    Format reference (container fields and section packing):
    https://github.com/PiTheGuy/SchemConvert/blob/master/src/main/java/
    pitheguy/schemconvert/converter/formats/AxiomSchematicFormat.java
    """
    started = time.time()
    with open(filepath, 'rb') as handle:
        file_size = os.fstat(handle.fileno()).st_size

        def read_exact(length):
            if length < 0 or length > _MAX_ARRAY_LEN or length > file_size - handle.tell():
                raise SchematicError('Invalid or truncated Axiom blueprint section')
            data = handle.read(length)
            if len(data) != length:
                raise SchematicError('Truncated Axiom blueprint')
            return data

        def read_section():
            return read_exact(struct.unpack('>i', read_exact(4))[0])

        if read_exact(4) != b'\x0a\xe5\xbb\x36':
            raise SchematicError('Not an Axiom .bp blueprint (incorrect header)')
        header_data = read_section()
        header = _NBTReader(header_data).read_root() if header_data else {}
        read_section()  # Embedded thumbnail is not needed for building geometry.
        compressed = read_section()

    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            payload = stream.read(_MAX_ARRAY_LEN + 1)
    except (OSError, EOFError) as exc:
        raise SchematicError('Cannot decompress Axiom blueprint block data') from exc
    if len(payload) > _MAX_ARRAY_LEN:
        raise SchematicError('Axiom blueprint block data exceeds the supported size')
    root = _NBTReader(payload).read_root()
    regions = root.get('BlockRegion')
    if not isinstance(regions, list):
        raise SchematicError("Axiom blueprint has no 'BlockRegion' list")

    world = SchematicWorld(filepath)
    world.name = str(header.get('Name') or os.path.splitext(os.path.basename(filepath))[0])
    world.author = str(header.get('Author') or '')
    world.description = str(header.get('Description') or '')
    world.data_version = int(root.get('DataVersion') or 0)
    world.schematic_version = int(header.get('Version') or 0)
    seen = set()
    for section in regions:
        if not isinstance(section, dict) or any(type(section.get(axis)) is not int for axis in 'XYZ'):
            raise SchematicError('Axiom blueprint has invalid section coordinates')
        coords = tuple(section[axis] for axis in 'XYZ')
        if coords in seen:
            raise SchematicError('Axiom blueprint contains a duplicate block section')
        seen.add(coords)
        block_states = section.get('BlockStates')
        palette = block_states.get('palette') if isinstance(block_states, dict) else None
        if not isinstance(palette, list) or not 1 <= len(palette) <= 4096:
            raise SchematicError('Axiom blueprint has a missing or invalid block palette')
        if any(not isinstance(entry, dict) or not isinstance(entry.get('Name'), str)
               or not entry['Name'] or not isinstance(entry.get('Properties', {}), dict)
               for entry in palette):
            raise SchematicError('Axiom blueprint contains an invalid block state')
        states = [block_state_string(entry) for entry in palette]
        info = RegionInfo('Section %d, %d, %d' % coords)
        info.origin = tuple(value * 16 for value in coords)
        info.size = (16, 16, 16)
        info.volume = 4096
        info.palette_size = len(states)
        info.padded = True
        info.packing_note = 'Axiom padded sections'
        if len(states) == 1:
            ids = (0 for _ in range(4096))
        else:
            info.bits = max(4, (len(states) - 1).bit_length())
            data = block_states.get('data')
            info.expected_longs = padded_long_count(4096, info.bits)
            if not isinstance(data, list) or len(data) != info.expected_longs or any(type(n) is not int for n in data):
                raise SchematicError('Axiom blueprint has missing or truncated block states')
            info.long_count = len(data)
            ids = _iter_padded(data, info.bits, 4096)
        for index, palette_id in enumerate(ids):
            if palette_id >= len(states):
                raise SchematicError('Axiom blueprint contains an out-of-range block state')
            state = states[palette_id]
            if _state_base_name(state) in AIR_BLOCKS or _state_base_name(state) in {'minecraft:structure_void', 'structure_void'}:
                continue  # Unselected positions in Axiom use structure_void.
            x, y, z = info.origin
            world.set_block(x + (index & 15), y + (index >> 8), z + ((index >> 4) & 15), state)
            world.palette_counts[state] += 1
            info.solid_blocks += 1
        world.regions.append(info)
        world.note_packing(True, info.packing_note, 0)
    # Blueprint entity positions are relative to the whole blueprint, not a section.
    for field, attribute, count_attribute in (
        ('BlockEntities', 'tile_entities', 'tile_entity_count'),
        ('Entities', 'entities', 'entity_count'),
    ):
        entities = root.get(field, [])
        if not isinstance(entities, list):
            raise SchematicError('Axiom blueprint has an invalid %s list' % field)
        setattr(world, count_attribute, len(entities))
        if entities and not world.regions:
            world.regions.append(RegionInfo('Entities'))
        if world.regions:
            setattr(world.regions[0], attribute, entities)
    world.parse_seconds = time.time() - started
    return world


def _canonical_state(state):
    """Normalize palette names and property ordering without losing mod namespaces."""
    if not isinstance(state, str) or not state:
        raise SchematicError('Invalid block state in schematic palette')
    name, bracket, body = state.partition('[')
    if not name or any(c.isspace() for c in name):
        raise SchematicError('Invalid block name in schematic palette')
    if ':' not in name:
        name = 'minecraft:' + name
    properties = {}
    if bracket:
        if not body.endswith(']'):
            raise SchematicError('Invalid block properties in schematic palette')
        for pair in body[:-1].split(',') if body[:-1] else []:
            key, equals, value = pair.partition('=')
            if not equals or not key or not value or key in properties:
                raise SchematicError('Invalid block properties in schematic palette')
            properties[key] = value
    return block_state_string({'Name': name, 'Properties': properties})


def _byte_array(value, name, length=None):
    if not isinstance(value, list) or any(type(n) is not int or not -128 <= n <= 255 for n in value):
        raise SchematicError('Missing or invalid %s byte array' % name)
    if length is not None and len(value) != length:
        raise SchematicError('%s length does not match schematic dimensions' % name)
    return value


def _schematic_varints(data, count):
    """Read exactly one unsigned palette index per block, including multi-byte IDs."""
    pos = 0
    for _ in range(count):
        value = 0
        for shift in range(0, 35, 7):
            if pos >= len(data):
                raise SchematicError('Truncated schematic block data')
            byte = data[pos] & 255
            pos += 1
            value |= (byte & 127) << shift
            if not byte & 128:
                break
        else:
            raise SchematicError('Invalid schematic block index (varint too long)')
        if value > 0x7fffffff:
            raise SchematicError('Invalid schematic block index')
        yield value
    if pos != len(data):
        raise SchematicError('Schematic block data contains extra entries')


@lru_cache(maxsize=1)
def _legacy_block_mapping():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'worldedit-legacy.json')
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)['blocks']


def _legacy_states(root, size):
    """Convert Alpha's block ID + metadata arrays using the shared vanilla registry."""
    if root.get('Materials', 'Alpha') != 'Alpha':
        raise SchematicError('Only Alpha/MCEdit .schematic files are supported')
    width, height, length = size
    volume = width * height * length
    blocks = _byte_array(root.get('Blocks'), 'Blocks', volume)
    metadata = _byte_array(root.get('Data'), 'Data', volume)
    extra = _byte_array(root.get('AddBlocks', []), 'AddBlocks')
    if len(extra) > (volume + 1) // 2:
        raise SchematicError('AddBlocks length exceeds schematic dimensions')
    mapping = _legacy_block_mapping()

    def block_id(index):
        high = ((extra[index // 2] & 255) >> (4 * (index % 2))) & 15 if index // 2 < len(extra) else 0
        return (blocks[index] & 255) | (high << 8)

    def mapped(index):
        identifier, data = block_id(index), metadata[index] & 15
        state = mapping.get('%d:%d' % (identifier, data), mapping.get('%d:0' % identifier))
        if state is None:
            raise SchematicError('Unknown legacy block ID %d:%d. Re-export this file as a modern .schem with its original mods installed.' % (identifier, data))
        name, _, props = state.partition('[')
        return name, dict(pair.split('=', 1) for pair in props.rstrip(']').split(',') if pair)

    layer = width * length
    cache = {}
    for index in range(volume):
        key = (block_id(index), metadata[index] & 15)
        if key not in cache:
            cache[key] = mapped(index)
        name, props = cache[key]
        # Legacy vertical pairs split their properties across the two blocks.
        # Recombine by state properties, across all door and tall-plant variants.
        if props.get('half') in {'upper', 'lower'}:
            other = index + (layer if props['half'] == 'lower' else -layer)
            if 0 <= other < volume and block_id(other) == block_id(index):
                other_name, other_props = mapped(other)
                if 'hinge' in props:
                    props = dict(props)
                    for field in ('hinge', 'powered') if props['half'] == 'lower' else ('facing', 'open'):
                        if field in other_props:
                            props[field] = other_props[field]
                elif props['half'] == 'upper':
                    name = other_name
        yield block_state_string({'Name': name, 'Properties': props})


def parse_worldedit_schematic(filepath):
    """Read Sponge versions 1-3 or legacy Alpha arrays into the shared world.

    Layout: https://github.com/SpongePowered/Schematic-Specification
    Legacy registry provenance is bundled alongside data/worldedit-legacy.json.
    """
    started = time.time()
    root = load_nbt(filepath)
    if isinstance(root.get('Schematic'), dict):
        root = root['Schematic']
    size = []
    for field in ('Width', 'Height', 'Length'):
        value = root.get(field)
        if type(value) is not int or not -32768 <= value <= 65535 or not (value & 65535):
            raise SchematicError('Invalid schematic %s' % field)
        size.append(value & 65535)
    volume = size[0] * size[1] * size[2]
    if volume > _MAX_ARRAY_LEN:
        raise SchematicError('Schematic dimensions exceed the supported size')
    legacy = isinstance(root.get('Blocks'), list)
    version = root.get('Version', 0)
    if not legacy and version not in (1, 2, 3):
        raise SchematicError('Unsupported Sponge schematic version: %s' % version)
    offset = ([root.get('WEOffset' + axis, 0) for axis in 'XYZ'] if legacy
              else root.get('Offset', [0, 0, 0]))
    if not isinstance(offset, list) or len(offset) != 3 or any(type(n) is not int for n in offset):
        raise SchematicError('Invalid schematic offset')
    world = SchematicWorld(filepath)
    metadata = root.get('Metadata', {})
    if not isinstance(metadata, dict):
        raise SchematicError('Invalid schematic metadata')
    world.name = str(metadata.get('Name') or os.path.splitext(os.path.basename(filepath))[0])
    world.author = str(metadata.get('Author') or '')
    world.description = str(metadata.get('Description') or '')
    world.data_version = int(root.get('DataVersion', 1343 if legacy else 0))
    world.schematic_version = 0 if legacy else version
    info = RegionInfo(world.name)
    info.size, info.origin, info.volume = tuple(size), tuple(offset), volume
    info.entity_origin = tuple(offset)
    if legacy:
        states = _legacy_states(root, size)
        container = root
        info.packing_note = 'Legacy block IDs and metadata'
    else:
        container = root.get('Blocks') if version == 3 else root
        if not isinstance(container, dict):
            raise SchematicError('Schematic has no block container')
        palette = container.get('Palette')
        if not isinstance(palette, dict) or not palette:
            raise SchematicError('Schematic has no block palette')
        lookup = {}
        for state, identifier in palette.items():
            if type(identifier) is not int or not 0 <= identifier <= 0x7fffffff or identifier in lookup:
                raise SchematicError('Invalid or duplicate schematic palette index')
            lookup[identifier] = _canonical_state(state)
        data = _byte_array(container.get('Data' if version == 3 else 'BlockData'), 'BlockData')
        def palette_states():
            for identifier in _schematic_varints(data, volume):
                if identifier not in lookup:
                    raise SchematicError('Schematic references an unknown palette index: %d' % identifier)
                yield lookup[identifier]
        states = palette_states()
        info.palette_size = len(lookup)
        info.packing_note = 'Sponge varint block indices'
    info.tile_entities = container.get('TileEntities' if legacy or version == 1 else 'BlockEntities', [])
    info.entities = root.get('Entities', [])
    if not isinstance(info.tile_entities, list) or not isinstance(info.entities, list):
        raise SchematicError('Invalid schematic entity lists')
    world.tile_entity_count, world.entity_count = len(info.tile_entities), len(info.entities)
    world.regions.append(info)
    world.note_packing(False, info.packing_note, 0)
    width, height, length = size
    for index, state in enumerate(states):
        if _state_base_name(state) in AIR_BLOCKS or _state_base_name(state) == 'minecraft:structure_void':
            continue
        world.set_block(offset[0] + index % width, offset[1] + index // (width * length),
                        offset[2] + (index // width) % length, state)
        world.palette_counts[state] += 1
        info.solid_blocks += 1
    if legacy:
        info.palette_size = len(world.palette_counts)
    world.parse_seconds = time.time() - started
    return world


def parse_schematic(filepath):
    """Load every supported format into the same geometry-building data model."""
    extension = os.path.splitext(os.fspath(filepath))[1].lower()
    if extension == '.bp':
        return parse_axiom_blueprint(filepath)
    if extension == '.litematic':
        return parse_litematic(filepath)
    if extension in {'.schem', '.schematic'}:
        return parse_worldedit_schematic(filepath)
    raise SchematicError('Choose a .schem, .schematic, .litematic or .bp file')


# ---------------------------------------------------------------------------
# Module-level store (read by the mesh builder)
# ---------------------------------------------------------------------------

_LOADED_WORLD = None


def get_loaded_world():
    """Return the most recently imported :class:`SchematicWorld`, or None."""
    return _LOADED_WORLD


def set_loaded_world(world):
    global _LOADED_WORLD
    _LOADED_WORLD = world


def clear_loaded_world():
    global _LOADED_WORLD
    _LOADED_WORLD = None


_BUILT_WORLDS = {}


def live_schematic_collection(scene):
    if _BUILT_WORLDS.get(scene.as_pointer()) is not get_loaded_world() or get_loaded_world() is None:
        return None
    token = scene.get('_mcbedrock_live_schematic', '')
    return next((c for c in scene.collection.children_recursive
                 if token and c.get('_mcbedrock_live_schematic') == token), None)


_REFRESH_DELAY = .5
_refresh_due = []  # the scene waiting for its rebuild, when one is pending


def refresh_schematic_soon(context):
    """Rebuild the schematic once pack-order edits pause: each Move Up/Down or
    drop used to rebuild it on the spot, a multi-second freeze per click."""
    if bpy.app.background:
        refresh_schematic(context)  # scripts see the rebuilt schematic straight away
        return
    if live_schematic_collection(context.scene) is None:
        return
    _refresh_due[:] = [context.scene]
    if bpy.app.timers.is_registered(_refresh_now):
        bpy.app.timers.unregister(_refresh_now)
    bpy.app.timers.register(_refresh_now, first_interval=_REFRESH_DELAY)


def _refresh_now():
    if not _refresh_due:
        return None
    scene = _refresh_due.pop()
    window = next((w for w in bpy.context.window_manager.windows if w.scene == scene), None)
    if window is None:
        return None
    window.cursor_modal_set('WAIT')
    try:
        with bpy.context.temp_override(window=window, scene=scene):
            refresh_schematic(bpy.context)
    except (RuntimeError, ReferenceError) as exc:
        print(f"[Sparkle] {exc}")
        window.cursor_modal_restore()
        def draw(menu, _context):
            menu.layout.label(text=str(exc))
        bpy.context.window_manager.popup_menu(draw, title="Sparkle", icon='ERROR')
        return None
    window.cursor_modal_restore()
    return None


def refresh_schematic(context):
    """Apply a pack-order edit to the last built schematic in this scene."""
    collection = live_schematic_collection(context.scene)
    if collection is None:
        return
    options = json.loads(collection.get('_mcbedrock_build_options', '{}'))
    result = bpy.ops.mcbedrock.build_schematic('EXEC_DEFAULT', replace_previous=True, **options)
    if result != {'FINISHED'}:
        raise RuntimeError('Could not update the schematic; the previous geometry was kept')


# ---------------------------------------------------------------------------
# Blender operator / panel
# ---------------------------------------------------------------------------

class MCBEDROCK_OT_import_litematic(Operator, ImportHelper):
    """Load WorldEdit, MCEdit, Litematic or Axiom schematics"""
    bl_idname = "mcbedrock.import_litematic"
    bl_label = "Import Schematic"
    bl_options = {"REGISTER"}

    filename_ext = ""
    filter_glob: bpy.props.StringProperty(
        default="*.litematic;*.bp;*.schem;*.schematic", options={"HIDDEN"}, maxlen=255
    )

    def execute(self, context):
        path = getattr(self, "filepath", "")
        if not path or not os.path.isfile(path):
            self.report({"ERROR"}, "No schematic file selected")
            return {"CANCELLED"}
        try:
            world = parse_schematic(path)
        except (NBTError, SchematicError) as exc:
            print("%s FAILED: %s" % (_LOG_PREFIX, exc))
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:  # pragma: no cover - unexpected file damage
            import traceback
            traceback.print_exc()
            self.report({"ERROR"}, "Failed to read schematic: %s" % exc)
            return {"CANCELLED"}

        set_loaded_world(world)
        world.print_summary()
        self.report(
            {"INFO"},
            "Loaded %s: %d regions, %d chunks, %d blocks"
            % (world.name, len(world.regions), world.chunk_count, world.total_blocks),
        )
        return {"FINISHED"}


class MCBEDROCK_OT_build_schematic(Operator):
    """Build textured meshes using the resource-pack load order, top pack first"""
    bl_idname = "mcbedrock.build_schematic"
    bl_label = "Build Geometry"
    bl_options = {"REGISTER", "UNDO"}

    replace_previous: bpy.props.BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})

    cull_faces: bpy.props.BoolProperty(
        name="Cull Hidden Faces",
        description="Skip faces buried against a neighbouring full block",
        default=True,
    )
    use_tint: bpy.props.BoolProperty(
        name="Biome Tint",
        description=(
            "Apply a constant plains-like tint to grass, leaves and water; "
            "schematics carry no biome data"
        ),
        default=True,
    )
    interpolation: bpy.props.EnumProperty(
        name="Texture Filter",
        items=[
            ("Closest", "Closest", "Crisp Minecraft pixels"),
            ("Linear", "Linear", "Smoothed texture filtering"),
        ],
        default="Closest",
    )

    @classmethod
    def poll(cls, context):
        return get_loaded_world() is not None

    def execute(self, context):
        world = get_loaded_world()
        if world is None:
            self.report({"ERROR"}, "Import a schematic first")
            return {"CANCELLED"}

        # Imported here so this module still loads without bpy for tests.
        try:
            from ..builders.schematic_builder import (
                SchematicBuildError,
                build_schematic,
                print_build_summary,
            )
        except ImportError as exc:
            self.report({"ERROR"}, "Schematic builder unavailable: %s" % exc)
            return {"CANCELLED"}

        from .pack_manager import active_pack_roots

        previous = live_schematic_collection(context.scene) if self.replace_previous else None
        old_root = next((o for o in previous.objects if o.get('mcbedrock_asset_type') == 'java_schematic'), None) if previous else None
        pack_roots = active_pack_roots(context.scene)
        vanilla_path = getattr(context.scene, "mcbedrock_vanilla_assets_path", "")
        before_objects = set(bpy.data.objects)
        before_collections = set(bpy.data.collections)
        def discard_failed_build():
            for obj in set(bpy.data.objects) - before_objects:
                mesh = obj.data if obj.type == 'MESH' else None
                bpy.data.objects.remove(obj, do_unlink=True)
                if mesh is not None and mesh.users == 0:
                    bpy.data.meshes.remove(mesh)
            for collection in set(bpy.data.collections) - before_collections:
                bpy.data.collections.remove(collection)
        try:
            stats = build_schematic(
                context, world, pack_roots,
                cull_faces=self.cull_faces,
                use_tint=self.use_tint,
                interpolation=self.interpolation,
                vanilla_path=vanilla_path,
            )
        except SchematicBuildError as exc:
            discard_failed_build()
            print("%s BUILD FAILED: %s" % (_LOG_PREFIX, exc))
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:  # pragma: no cover - unexpected pack damage
            discard_failed_build()
            import traceback
            traceback.print_exc()
            self.report({"ERROR"}, "Build failed: %s" % exc)
            return {"CANCELLED"}

        collection = bpy.data.collections.get(stats.get('collection', ''))
        if collection is not None:
            import uuid
            token = uuid.uuid4().hex
            new_root = stats.get('root')
            if previous is not None and new_root is not None:
                if old_root is not None:
                    new_root.matrix_world = old_root.matrix_world.copy()
                old_token = previous.get('_mcbedrock_live_schematic')
                owned = [o for o in previous.objects if o.get('_mcbedrock_live_schematic') == old_token]
                # Keep user-added objects and their world transforms.
                for obj in list(previous.objects):
                    if obj not in owned and obj.parent in owned:
                        matrix = obj.matrix_world.copy()
                        obj.parent = new_root
                        obj.matrix_world = matrix
                for obj in list(collection.objects):
                    previous.objects.link(obj)
                    collection.objects.unlink(obj)
                    obj['_mcbedrock_live_schematic'] = token
                bpy.data.collections.remove(collection)
                collection = previous
                for obj in owned:
                    mesh = obj.data if obj.type == 'MESH' else None
                    bpy.data.objects.remove(obj, do_unlink=True)
                    if mesh is not None and mesh.users == 0:
                        bpy.data.meshes.remove(mesh)
                stats['collection'] = collection.name
            else:
                for obj in collection.objects:
                    obj['_mcbedrock_live_schematic'] = token
            collection['_mcbedrock_live_schematic'] = token
            collection['_mcbedrock_build_options'] = json.dumps(dict(
                cull_faces=self.cull_faces, use_tint=self.use_tint, interpolation=self.interpolation))
            context.scene['_mcbedrock_live_schematic'] = token
            _BUILT_WORLDS[context.scene.as_pointer()] = world
        print_build_summary(stats)
        message = "Built %d chunks, %d blocks, %d faces" % (
            stats["chunks"], stats["blocks"], stats["faces"],
        )
        if stats["placeholders"]:
            message += " (%d unresolved)" % stats["placeholders"]
        if stats.get("entities"):
            message += ", %d entities" % stats["entities"]
        if stats.get("entity_markers"):
            message += "; %d entities kept as markers (see console)" % stats["entity_markers"]
        if stats.get("missing_textures"):
            message += "; %d missing textures (see console)" % len(stats["missing_textures"])
        self.report({"WARNING"} if stats.get("entity_warnings") or stats.get("missing_textures") or stats["placeholders"] else {"INFO"}, message)
        return {"FINISHED"}


class MCBEDROCK_PT_schematic_importer(Panel):
    bl_label = "Schematic Importer"
    bl_idname = "MCBEDROCK_PT_schematic_importer"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Sparkle"
    bl_order = 2

    @classmethod
    def poll(cls, context):
        from .custom_ui import legacy_ui_enabled
        return legacy_ui_enabled(context)

    def draw(self, context):
        layout = self.layout
        layout.operator("mcbedrock.import_litematic", icon="IMPORT")

        world = get_loaded_world()
        if world is None:
            layout.label(text="No schematic loaded", icon="INFO")
            return

        box = layout.box()
        box.label(text=world.name or "(unnamed)", icon="MESH_GRID")
        column = box.column(align=True)
        column.label(text="Regions: %d" % len(world.regions))
        column.label(text="Chunks: %d" % world.chunk_count)
        column.label(text="Blocks: %d" % world.total_blocks)
        column.label(text="States: %d" % len(world.palette_counts))
        column.label(text="Entities: %d" % world.entity_count)
        column.label(text="Tile entities: %d" % world.tile_entity_count)
        box.label(text="Details in the system console", icon="CONSOLE")

        layout.separator()
        scene = context.scene
        from .pack_manager import active_pack_roots

        pack_roots = active_pack_roots(scene)
        pack_root = pack_roots[0] if pack_roots else ""
        vanilla_path = getattr(scene, "mcbedrock_vanilla_assets_path", "")

        assets = layout.box()
        assets.label(text="Resource Pack Load Order", icon="ASSET_MANAGER")
        assets.label(text="Top pack wins; vanilla is last", icon="SORT_DESC")
        assets.template_list("MCBEDROCK_UL_active_pack_list", "schematic",
                             scene, "mcbedrock_active_packs",
                             scene, "mcbedrock_active_pack_index", rows=4)
        order = assets.row(align=True)
        order.operator("mcbedrock.move_active_pack_up", text="Move Up")
        order.operator("mcbedrock.move_active_pack_down", text="Move Down")
        order.operator("mcbedrock.remove_active_pack", text="Remove")
        assets.label(text="Add / enable packs in Resource Packs")
        if pack_root:
            assets.label(text=os.path.basename(pack_root.rstrip("/\\")),
                         icon="FILE_FOLDER")
            if len(pack_roots) > 1:
                assets.label(text="%d active packs (priority order)" % len(pack_roots),
                             icon="SORT_DESC")
        else:
            assets.label(text="No resource pack set", icon="ERROR")
        assets.prop(scene, "mcbedrock_vanilla_assets_path", text="Vanilla")
        if not vanilla_path:
            # Texture-only packs are the common case and cannot build alone.
            assets.label(text="Auto-detects .minecraft jar", icon="INFO")

        build_column = layout.column()
        build_column.enabled = bool(pack_roots or vanilla_path)
        build_column.operator("mcbedrock.build_schematic", icon="MESH_CUBE")


classes = (
    MCBEDROCK_OT_import_litematic,
    MCBEDROCK_OT_build_schematic,
    MCBEDROCK_PT_schematic_importer,
)


def _register_impl():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.mcbedrock_vanilla_assets_path = bpy.props.StringProperty(
        name="Vanilla Assets",
        description=(
            "Folder of extracted vanilla assets, or a Minecraft client .jar. "
            "Supplies the blockstates and models that texture-only packs omit. "
            "Leave empty to auto-detect the newest .minecraft version jar"
        ),
        default="",
        subtype="FILE_PATH",
    )


def register():
    """Register transactionally, removing residue from a failed enable."""
    if not bpy:
        return
    unregister()
    try:
        _register_impl()
    except Exception:
        unregister()
        raise


def unregister():
    """Unregister the schematic importer and drop the decoded world."""
    clear_loaded_world()
    _BUILT_WORLDS.clear()
    if not bpy:
        return
    if hasattr(bpy.types.Scene, "mcbedrock_vanilla_assets_path"):
        del bpy.types.Scene.mcbedrock_vanilla_assets_path
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            registered_cls = getattr(bpy.types, cls.__name__, None)
            if registered_cls is not None and registered_cls is not cls:
                try:
                    bpy.utils.unregister_class(registered_cls)
                except Exception:
                    pass


if __name__ == "__main__":  # pragma: no cover - manual Blender text-editor run
    register()
