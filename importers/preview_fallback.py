"""Small, dependency-free preview PNG helpers.

Blender's bundled Python does not guarantee Pillow is available.  These
helpers keep preview validation and deterministic fallback generation usable
both in the UI process and in the background thumbnail worker.
"""

from __future__ import annotations

import hashlib
import os
import struct
import threading
import zlib
from collections import OrderedDict


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# The IEND chunk: zero length, type, CRC. A complete PNG ends with it.
PNG_END = b"\x00\x00\x00\x00IEND\xaeB`\x82"
_VISIBLE_PNG_CACHE = OrderedDict()
_VISIBLE_PNG_CACHE_LIMIT = 4096
# Pack folders are read on helper threads while the sidebar draws.
_VISIBLE_PNG_LOCK = threading.Lock()


def _chunks(payload):
    if not payload.startswith(PNG_SIGNATURE):
        return
    offset = len(PNG_SIGNATURE)
    while offset + 12 <= len(payload):
        length = struct.unpack(">I", payload[offset:offset + 4])[0]
        end = offset + 12 + length
        if end > len(payload):
            return
        kind = payload[offset + 4:offset + 8]
        data = payload[offset + 8:offset + 8 + length]
        expected_crc = struct.unpack(">I", payload[offset + 8 + length:end])[0]
        if (zlib.crc32(kind + data) & 0xFFFFFFFF) != expected_crc:
            return
        yield kind, data
        offset = end
        if kind == b"IEND":
            return


def _read_png(path):
    try:
        with open(path, "rb") as stream:
            payload = stream.read()
    except OSError:
        return b"", ()
    return payload, tuple(_chunks(payload) or ())


def png_dimensions(path):
    """Return ``(width, height)`` for a complete PNG, otherwise ``(0, 0)``."""
    _payload, chunks = _read_png(path)
    if not chunks or chunks[0][0] != b"IHDR" or len(chunks[0][1]) != 13:
        return 0, 0
    if chunks[-1][0] != b"IEND":
        return 0, 0
    width, height = struct.unpack(">II", chunks[0][1][:8])
    if not width or not height or width > 16384 or height > 16384:
        return 0, 0
    return width, height


def _paeth(left, above, upper_left):
    estimate = left + above - upper_left
    dl = abs(estimate - left)
    da = abs(estimate - above)
    du = abs(estimate - upper_left)
    if dl <= da and dl <= du:
        return left
    if da <= du:
        return above
    return upper_left


def png_has_visible_pixels(path):
    """Validate once per file revision, including replacements by the worker.

    Only successful checks are retained: missing, incomplete and corrupt PNGs
    remain immediately retryable while a background render writes the file.
    """
    try:
        stat = os.stat(path)
    except OSError:
        with _VISIBLE_PNG_LOCK:
            _VISIBLE_PNG_CACHE.pop(os.fspath(path), None)
        return False
    key = os.fspath(path)
    signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
    with _VISIBLE_PNG_LOCK:
        if _VISIBLE_PNG_CACHE.get(key) == signature:
            _VISIBLE_PNG_CACHE.move_to_end(key)
            return True
        _VISIBLE_PNG_CACHE.pop(key, None)
    visible = _decode_png_has_visible_pixels(path)
    if visible:
        try:
            after = os.stat(path)
        except OSError:
            return False
        if signature == (after.st_mtime_ns, after.st_ctime_ns, after.st_size):
            _remember(key, signature)
    return visible


def _remember(key, signature):
    with _VISIBLE_PNG_LOCK:
        _VISIBLE_PNG_CACHE[key] = signature
        _VISIBLE_PNG_CACHE.move_to_end(key)
        while len(_VISIBLE_PNG_CACHE) > _VISIBLE_PNG_CACHE_LIMIT:
            _VISIBLE_PNG_CACHE.popitem(last=False)


def png_is_complete(path):
    """True for a whole PNG file: its signature and closing IEND chunk are present.

    Reads 20 bytes, not the image, so it is cheap enough to run per finished
    thumbnail on Blender's UI thread.
    """
    try:
        with open(path, "rb") as stream:
            if stream.read(len(PNG_SIGNATURE)) != PNG_SIGNATURE:
                return False
            stream.seek(-len(PNG_END), os.SEEK_END)
            return stream.read(len(PNG_END)) == PNG_END
    except OSError:
        return False


def remember_visible_png(path):
    """Accept a PNG another process already validated, so drawing never decodes it.

    The thumbnail worker checks every render (and writes opaque fallbacks) before
    a PNG appears at its final path; the path-lookup helper checks the PNGs that
    were already there. Only complete files are accepted, and the stat signature
    still invalidates the entry when the file is replaced.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return False
    if not png_is_complete(path):
        return False
    _remember(os.fspath(path), (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size))
    return True


def png_data_has_visible_pixels(payload):
    """``png_has_visible_pixels`` for PNG bytes (a texture read from an archive)."""
    return _visible_chunks(tuple(_chunks(payload)))


def _decode_png_has_visible_pixels(path):
    """Return whether a valid PNG contains something visible.

    Alpha-bearing 8-bit, non-interlaced PNGs are decoded so a fully
    transparent render is not accepted as a completed thumbnail.  Opaque or
    uncommon-but-valid PNG encodings are considered visible and are left to
    Blender's image loader.
    """
    _payload, chunks = _read_png(path)
    return _visible_chunks(chunks)


def _visible_chunks(chunks):
    if not chunks or chunks[0][0] != b"IHDR" or chunks[-1][0] != b"IEND":
        return False
    header = chunks[0][1]
    if len(header) != 13:
        return False
    width, height, depth, color_type, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", header
    )
    if not width or not height or width > 16384 or height > 16384:
        return False
    if compression != 0 or filtering != 0:
        return False
    if depth != 8 or interlace != 0 or color_type not in (4, 6):
        return True

    channels = 2 if color_type == 4 else 4
    stride = width * channels
    try:
        raw = zlib.decompress(b"".join(data for kind, data in chunks if kind == b"IDAT"))
    except (ValueError, zlib.error):
        return False
    if len(raw) != height * (stride + 1):
        return False

    previous = bytearray(stride)
    offset = 0
    alpha_index = channels - 1
    for _row_index in range(height):
        filter_type = raw[offset]
        offset += 1
        source = raw[offset:offset + stride]
        offset += stride
        if filter_type > 4:
            return False
        row = bytearray(stride)
        for index, value in enumerate(source):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                decoded = value
            elif filter_type == 1:
                decoded = value + left
            elif filter_type == 2:
                decoded = value + above
            elif filter_type == 3:
                decoded = value + ((left + above) // 2)
            else:
                decoded = value + _paeth(left, above, upper_left)
            row[index] = decoded & 0xFF
        if any(row[index] for index in range(alpha_index, stride, channels)):
            return True
        previous = row
    return False


def _png_chunk(kind, payload):
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def write_deterministic_fallback(path, key="", kind="asset", size=128):
    """Write a stable, generic RGBA placeholder and return its path.

    The palette is derived from the asset key, while the glyph only reflects
    the broad asset kind.  No entity-, block-, or pack-name rules are used.
    """
    size = max(16, min(int(size or 128), 512))
    digest = hashlib.sha256((str(kind) + "|" + str(key)).encode("utf-8", "ignore")).digest()
    base = tuple(48 + (value % 128) for value in digest[:3])
    accent = tuple(min(255, value + 70) for value in base)
    dark = tuple(max(12, value // 3) for value in base)
    pixels = bytearray(size * size * 4)

    def fill(left, top, right, bottom, color):
        left = max(0, min(size, left))
        right = max(left, min(size, right))
        top = max(0, min(size, top))
        bottom = max(top, min(size, bottom))
        rgba = bytes((*color, 255))
        for y_pos in range(top, bottom):
            start = (y_pos * size + left) * 4
            pixels[start:start + (right - left) * 4] = rgba * (right - left)

    margin = max(2, size // 12)
    fill(margin, margin, size - margin, size - margin, dark)
    fill(margin * 2, margin * 2, size - margin * 2, size - margin * 2, base)
    if "pack" in str(kind).lower():
        step = max(2, size // 10)
        fill(size // 4, size // 3, size * 3 // 4, size * 2 // 3, accent)
        fill(size // 3, size // 3 - step, size * 2 // 3, size // 3, accent)
        fill(size // 3, size * 2 // 3, size * 2 // 3, size * 2 // 3 + step, accent)
    else:
        inset = size // 4
        fill(inset, inset, size - inset, size - inset, accent)
        cut = max(2, size // 10)
        fill(inset, inset, inset + cut, inset + cut, dark)
        fill(size - inset - cut, size - inset - cut, size - inset, size - inset, dark)

    scanlines = b"".join(
        b"\x00" + pixels[row * size * 4:(row + 1) * size * 4]
        for row in range(size)
    )
    payload = (
        PNG_SIGNATURE
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(scanlines, 9))
        + _png_chunk(b"IEND", b"")
    )
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        temporary = path + ".tmp"
        with open(temporary, "wb") as stream:
            stream.write(payload)
        os.replace(temporary, path)
        return path
    except OSError:
        try:
            if os.path.isfile(path + ".tmp"):
                os.remove(path + ".tmp")
        except OSError:
            pass
        return ""
