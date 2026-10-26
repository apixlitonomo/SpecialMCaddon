"""Validated reader for Bedrock resource ``.brarchive`` bundles."""

from pathlib import PurePosixPath
import os
import struct


MAGIC = bytes([0x7D, 0x27, 0x25, 0xB1, 0xA0, 0x52, 0x70, 0x26])
HEADER_SIZE = 256
CONTAINER_HEADER_SIZE = 16
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _read_exact(stream, size, label):
    data = stream.read(size)
    if len(data) != size:
        raise ValueError(f"Truncated .brarchive {label}")
    return data


def _validated_name(raw_name):
    name = raw_name.replace("\\", "/")
    path = PurePosixPath(name)
    parts = path.parts
    if (
        not name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or any(
            ":" in part
            or any(ord(character) < 32 for character in part)
            or part.endswith((" ", "."))
            or part.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES
            for part in parts
        )
    ):
        raise ValueError(f"Unsafe .brarchive entry name: {raw_name!r}")
    return path.as_posix()


def _read_directory(filepath):
    """Return validated entry records and the absolute payload offset."""
    archive_size = os.path.getsize(filepath)
    with open(filepath, "rb") as stream:
        if _read_exact(stream, 8, "magic") != MAGIC:
            raise ValueError("Not a valid .brarchive file")
        entry_count = struct.unpack(
            "<I", _read_exact(stream, 4, "entry count")
        )[0]
        version = struct.unpack(
            "<I", _read_exact(stream, 4, "version")
        )[0]
        if version != 1:
            raise ValueError(f"Unsupported .brarchive version: {version}")
        max_headers = max(
            0, (archive_size - CONTAINER_HEADER_SIZE) // HEADER_SIZE
        )
        if entry_count > max_headers:
            raise ValueError("Invalid .brarchive entry count")

        payload_start = CONTAINER_HEADER_SIZE + entry_count * HEADER_SIZE
        payload_size = archive_size - payload_start
        entries = []
        names = set()
        for index in range(entry_count):
            header = _read_exact(stream, HEADER_SIZE, f"entry header {index}")
            name_length = header[0]
            if name_length == 0 or name_length > HEADER_SIZE - 9:
                raise ValueError(
                    f"Invalid .brarchive name length at entry {index}"
                )
            try:
                decoded = header[1:1 + name_length].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    f"Invalid UTF-8 entry name at index {index}"
                ) from exc
            name = _validated_name(decoded)
            if name.casefold() in names:
                raise ValueError(f"Duplicate .brarchive entry: {name}")
            names.add(name.casefold())

            offset = struct.unpack("<I", header[-8:-4])[0]
            size = struct.unpack("<I", header[-4:])[0]
            if offset > payload_size or size > payload_size - offset:
                raise ValueError(
                    f"Entry payload is outside archive bounds: {name}"
                )
            entries.append(
                {
                    "name": name,
                    "offset": offset,
                    "size": size,
                    "version": version,
                }
            )

        # A logical path may not be both a file and a directory. Rejecting
        # these aliases also makes extraction independent of entry order.
        for entry in entries:
            parts = PurePosixPath(entry["name"]).parts
            for index in range(1, len(parts)):
                parent = "/".join(parts[:index]).casefold()
                if parent in names:
                    raise ValueError(
                        "Conflicting .brarchive file/directory entries: "
                        f"{entry['name']}"
                    )

        nonempty_ranges = sorted(
            (
                entry["offset"],
                entry["offset"] + entry["size"],
                entry["name"],
            )
            for entry in entries
            if entry["size"]
        )
        for previous, current in zip(nonempty_ranges, nonempty_ranges[1:]):
            if current[0] < previous[1] and current[:2] != previous[:2]:
                raise ValueError(
                    "Overlapping .brarchive payloads: "
                    f"{previous[2]} and {current[2]}"
                )
    return tuple(entries), payload_start


def get_brarchive_entries(filepath):
    """Return validated logical entry names contained in an archive."""
    entries, _payload_start = _read_directory(filepath)
    return [entry["name"] for entry in entries]


def extract_brarchive(filepath, out_directory):
    """Extract every validated entry beneath one caller-owned directory."""
    entries, payload_start = _read_directory(filepath)
    root = os.path.realpath(os.fspath(out_directory))
    os.makedirs(root, exist_ok=True)
    extracted = {}
    with open(filepath, "rb") as stream:
        for entry in entries:
            output = os.path.realpath(
                os.path.join(root, *PurePosixPath(entry["name"]).parts)
            )
            if os.path.commonpath((root, output)) != root:
                raise ValueError(
                    f"Unsafe extraction target: {entry['name']!r}"
                )
            os.makedirs(os.path.dirname(output), exist_ok=True)
            stream.seek(payload_start + entry["offset"])
            data = _read_exact(
                stream,
                entry["size"],
                f"payload for {entry['name']}",
            )
            with open(output, "wb") as destination:
                destination.write(data)
            extracted[entry["name"]] = output
    return extracted

def extract_brarchive_entry(filepath, entry_name, out_filepath):
    """Extract one validated logical entry to the caller-owned path."""
    wanted = _validated_name(str(entry_name))
    entries, payload_start = _read_directory(filepath)
    target = next(
        (entry for entry in entries if entry["name"] == wanted),
        None,
    )
    if target is None:
        raise FileNotFoundError(f"Entry {wanted} not found in archive")

    with open(filepath, "rb") as stream:
        stream.seek(payload_start + target["offset"])
        data = _read_exact(
            stream,
            target["size"],
            f"payload for {wanted}",
        )

    output = os.path.abspath(os.fspath(out_filepath))
    parent = os.path.dirname(output)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(output, "wb") as destination:
        destination.write(data)
    return output
