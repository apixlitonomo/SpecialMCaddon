"""Schema-aware, resource-only Minecraft archive handling.

The Minecraft container formats are ZIP files, but an add-on, world, or world
template can also contain behavior packs and world data.  This module exposes
only manifests whose module type is a visual resource pack and recursively
inspects nested ``.mcpack`` modules.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import zipfile
from pathlib import PurePosixPath


SUPPORTED_ARCHIVE_EXTENSIONS = frozenset(
    {".mcpack", ".mcaddon", ".mcworld", ".mctemplate", ".zip", ".jar"}
)
REJECTED_ARCHIVE_EXTENSIONS = frozenset({".mcproject", ".mcstructure"})
DISCOVERABLE_ARCHIVE_EXTENSIONS = (
    SUPPORTED_ARCHIVE_EXTENSIONS | REJECTED_ARCHIVE_EXTENSIONS
)
EXTRACTION_FORMAT_VERSION = 3
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_ICON_BYTES = 8 * 1024 * 1024
MAX_NESTED_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_NESTED_DEPTH = 6
PACK_ICON_NAMES = ("pack_icon.png", "pack.png")


class ArchiveInputError(ValueError):
    """An archive cannot safely provide resource-pack assets."""


def archive_extension(filepath):
    return os.path.splitext(str(filepath or "").lower())[1]


def archive_rejection_reason(filepath):
    """Return a clear user-facing reason for intentionally unsupported inputs."""
    ext = archive_extension(filepath)
    if ext == ".mcproject":
        return (
            ".mcproject is a Minecraft project container, not a resource pack; "
            "export its resource pack as .mcpack first."
        )
    if ext == ".mcstructure":
        return (
            ".mcstructure stores world structure data, not visual resource-pack "
            "assets, and cannot be imported as a pack."
        )
    if ext and ext not in SUPPORTED_ARCHIVE_EXTENSIONS:
        return f"Unsupported archive type: {ext}"
    return ""


def _safe_name(value):
    from .utils import sanitize_pack_name

    stem = os.path.splitext(os.path.basename(value or "pack"))[0]
    stem = sanitize_pack_name(stem)
    stem = re.sub(r"[^0-9A-Za-z._ -]+", "_", stem).strip(" ._-")
    return stem or "pack"


def _archive_hash(filepath):
    info = os.stat(filepath)
    value = f"{os.path.abspath(filepath)}:{info.st_size}:{info.st_mtime_ns}"
    return hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()


def _output_dir(archive_path, cache_dir):
    return os.path.join(
        cache_dir, f"{_safe_name(archive_path)}_{_archive_hash(archive_path)[:12]}"
    )


def _marker_value(archive_path):
    return f"v{EXTRACTION_FORMAT_VERSION}:{_archive_hash(archive_path)}"


def _cached_output(archive_path, cache_dir):
    if not archive_path or not os.path.isfile(archive_path):
        return ""
    output = _output_dir(archive_path, cache_dir)
    marker = os.path.join(output, ".extracted_hash")
    try:
        with open(marker, "r", encoding="utf-8") as handle:
            return output if handle.read().strip() == _marker_value(archive_path) else ""
    except OSError:
        return ""


def cached_archive_output(archive_path, cache_dir):
    """Return a current resource-only cache folder, or ``None``."""
    return _cached_output(archive_path, cache_dir) or None


def _member_name(info):
    """Normalize one member and reject traversal, drives, links, and ADS paths."""
    raw = info.filename
    if not raw or "\x00" in raw:
        raise ArchiveInputError("Archive contains an invalid empty member name.")
    path = PurePosixPath(raw.replace("\\", "/"))
    parts = tuple(part for part in path.parts if part not in {"", "."})
    if path.is_absolute() or any(part == ".." for part in parts):
        raise ArchiveInputError(f"Unsafe archive member: {raw}")
    if not parts or re.match(r"^[A-Za-z]:", parts[0]) or any(":" in p for p in parts):
        raise ArchiveInputError(f"Unsafe archive member: {raw}")
    mode = info.external_attr >> 16
    if mode and stat.S_IFMT(mode) == stat.S_IFLNK:
        raise ArchiveInputError(f"Archive symlinks are not supported: {raw}")
    return "/".join(parts)


def _validated_infos(archive):
    result = {}
    folded = set()
    for info in archive.infolist():
        name = _member_name(info)
        key = name.casefold()
        if name in result or key in folded:
            raise ArchiveInputError(f"Archive contains a duplicate member: {info.filename}")
        result[name] = info
        folded.add(key)
    return result


def _read_limited(archive, info, limit):
    if info.file_size < 0 or info.file_size > limit:
        raise ArchiveInputError(f"Archive member is too large: {info.filename}")
    with archive.open(info, "r") as handle:
        value = handle.read(limit + 1)
    if len(value) > limit:
        raise ArchiveInputError(f"Archive member is too large: {info.filename}")
    return value


def _read_json(archive, info):
    try:
        value = json.loads(
            _read_limited(archive, info, MAX_METADATA_BYTES).decode("utf-8-sig")
        )
        return value if isinstance(value, dict) else {}
    except (ArchiveInputError, UnicodeError, json.JSONDecodeError):
        return {}


def _is_resource_manifest(data):
    modules = data.get("modules", []) if isinstance(data, dict) else []
    return isinstance(modules, list) and any(
        isinstance(module, dict)
        and str(module.get("type", "")).strip().lower()
        in {"resources", "client_data"}
        for module in modules
    )


_MOD_METADATA = ("fabric.mod.json", "quilt.mod.json", "META-INF/mods.toml", "META-INF/neoforge.mods.toml")


def is_mod_jar(path):
    """A loader's mod (Fabric, Quilt, Forge, NeoForge), not a client JAR or plain archive."""
    if not str(path).lower().endswith(".jar"):
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return False
    return bool(names.intersection(_MOD_METADATA))


def mod_logo(archive):
    """The member holding a mod's own logo (``fabric.mod.json`` icon, ``logoFile``), or ''."""
    names = set(archive.namelist())
    candidates = []
    for metadata in ("fabric.mod.json", "quilt.mod.json"):
        if metadata in names:
            try:
                data = json.loads(archive.read(metadata).decode("utf-8-sig"))
            except (UnicodeError, ValueError):
                continue
            icon = data.get("icon") if isinstance(data, dict) else None
            if isinstance(data, dict) and isinstance(data.get("quilt_loader"), dict):
                icon = icon or (data["quilt_loader"].get("metadata") or {}).get("icon")
            if isinstance(icon, dict) and icon:  # sizes -> paths: take the largest
                icon = icon[max(icon, key=lambda size: int(size) if str(size).isdigit() else 0)]
            if isinstance(icon, str):
                candidates.append(icon.lstrip("/"))
    for metadata in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
        if metadata in names:
            logo = _mods_toml_entry(archive.read(metadata).decode("utf-8-sig", errors="ignore")).get("logoFile")
            if isinstance(logo, str):
                candidates.append(logo.lstrip("/"))
    return next((name for name in candidates if name in names and name.lower().endswith(".png")), "")


def _mods_toml_entry(raw):
    """The first ``[[mods]]`` table of a Forge or NeoForge ``mods.toml``."""
    try:
        import tomllib

        data = tomllib.loads(raw)
        mods = data.get("mods")
        entry = dict(mods[0]) if isinstance(mods, list) and mods and isinstance(mods[0], dict) else {}
        # Keys above [[mods]] fill the gaps: some mods name their logo there.
        for key, value in data.items():
            if isinstance(value, str):
                entry.setdefault(key, value)
        return entry
    except ValueError:  # not valid TOML: read the keys line by line
        pass
    entry = {}
    for key in ("displayName", "description", "modId", "authors", "logoFile"):
        # Either quote, or triple quotes across lines: "Alex's Mobs" keeps its apostrophe.
        match = re.search(rf"(?ms)^\s*{key}\s*=\s*('''|\"\"\"|\"|')(.*?)\1", raw)
        if match:
            entry[key] = match.group(2)
    return entry


def _author_names(authors):
    """Fabric lists authors as names or ``{"name": ...}``; Quilt as ``{name: role}``."""
    if isinstance(authors, dict):
        return [str(name) for name in authors]
    if isinstance(authors, list):
        return [str(author.get("name", "")) if isinstance(author, dict) else str(author)
                for author in authors if author]
    return [str(authors)] if authors else []


def _java_mod_metadata(archive, infos, source):
    """Return display metadata for a Forge/Fabric/NeoForge/Quilt mod JAR."""
    metadata = {
        "name": os.path.splitext(os.path.basename(source))[0],
        "description": "Minecraft mod",
        "uuid": "",
        "manifest": {"java_mod": True},
    }
    by_lower = {name.casefold(): (name, info) for name, info in infos.items()}
    fabric = by_lower.get("fabric.mod.json") or by_lower.get("quilt.mod.json")
    if fabric:
        data = _read_json(archive, fabric[1])
        if "quilt_loader" in data and isinstance(data.get("quilt_loader"), dict):
            quilt = data["quilt_loader"]
            details = quilt.get("metadata") if isinstance(quilt.get("metadata"), dict) else {}
            metadata["name"] = str(details.get("name") or quilt.get("id") or metadata["name"])
            metadata["description"] = str(details.get("description") or metadata["description"])
            metadata["uuid"] = str(quilt.get("id") or "")
            authors = _author_names(details.get("contributors"))
        else:
            metadata["name"] = str(data.get("name") or data.get("id") or metadata["name"])
            metadata["description"] = str(data.get("description") or metadata["description"])
            metadata["uuid"] = str(data.get("id") or "")
            authors = _author_names(data.get("authors"))
        metadata["manifest"] = dict(data, java_mod=True, authors=authors)
    else:
        toml_entry = (
            by_lower.get("meta-inf/mods.toml")
            or by_lower.get("meta-inf/neoforge.mods.toml")
        )
        if toml_entry:
            try:
                raw = _read_limited(archive, toml_entry[1], MAX_METADATA_BYTES).decode(
                    "utf-8-sig", errors="ignore"
                )
                entry = _mods_toml_entry(raw)
                metadata["name"] = str(entry.get("displayName") or metadata["name"]).strip()
                metadata["description"] = str(entry.get("description") or metadata["description"]).strip()
                metadata["uuid"] = str(entry.get("modId") or "").strip()
                metadata["manifest"] = {
                    "java_mod": True,
                    "mod_id": metadata["uuid"],
                    "authors": str(entry.get("authors") or "").strip(),
                }
            except (ArchiveInputError, UnicodeError):
                pass
    return metadata

def _pack_candidates(archive, infos, source):
    result = []
    seen = set()
    for name, info in infos.items():
        basename = PurePosixPath(name).name.lower()
        if basename not in {"manifest.json", "pack.mcmeta"}:
            continue
        data = _read_json(archive, info)
        if basename == "manifest.json":
            if not _is_resource_manifest(data):
                continue
            kind = "bedrock"
            header = data.get("header", {})
            header = header if isinstance(header, dict) else {}
            metadata = {
                "name": str(header.get("name") or ""),
                "description": str(header.get("description") or ""),
                "uuid": str(header.get("uuid") or ""),
                "manifest": data,
            }
        else:
            pack = data.get("pack")
            if not isinstance(pack, dict):
                continue
            kind = "java"
            metadata = {
                "name": "",
                "description": pack.get("description", "")
                if isinstance(pack.get("description", ""), str)
                else "",
                "uuid": "",
                "manifest": data,
            }
        root = str(PurePosixPath(name).parent)
        root = "" if root == "." else root
        key = (root.casefold(), kind)
        if key not in seen:
            seen.add(key)
            result.append({
                "root": root,
                "kind": kind,
                "metadata": metadata,
                "source": source,
            })
    if archive_extension(source) == ".jar":
        # A mod JAR may contain pack.mcmeta, but only assets/ is safe/useful.
        result = []
        asset_namespaces = sorted({
            name.split("/", 2)[1]
            for name in infos
            if name.casefold().startswith("assets/") and name.count("/") >= 2
        }, key=str.casefold)
        if asset_namespaces:
            metadata = _java_mod_metadata(archive, infos, source)
            metadata["namespaces"] = asset_namespaces
            result.append({
                "root": "",
                "kind": "java_mod",
                "metadata": metadata,
                "source": source,
                "member_prefixes": ("assets/",),
            })

    return sorted(
        result,
        key=lambda item: (
            item["root"].count("/"), item["root"].casefold(), item["kind"]
        ),
    )


def _direct_member(infos, root, basename):
    prefix = f"{root}/" if root else ""
    for name, info in infos.items():
        if not name.casefold().startswith(prefix.casefold()):
            continue
        relative = name[len(prefix):]
        if "/" not in relative and relative.casefold() == basename.casefold():
            return name, info
    return None


def _walk_packs(archive, source, callback, depth=0):
    if depth > MAX_NESTED_DEPTH:
        raise ArchiveInputError("Nested .mcpack depth exceeds the safety limit.")
    infos = _validated_infos(archive)
    count = 0
    for candidate in _pack_candidates(archive, infos, source):
        callback(archive, infos, candidate)
        count += 1

    # A container may wrap a pack in several .mcpack layers. Behavior modules
    # are ignored because only resource manifest types become candidates.
    for name, info in infos.items():
        if not name.lower().endswith(".mcpack") or info.is_dir():
            continue
        payload = _read_limited(archive, info, MAX_NESTED_ARCHIVE_BYTES)
        try:
            with zipfile.ZipFile(io.BytesIO(payload), "r") as nested:
                count += _walk_packs(
                    nested, f"{source}!/{name}", callback, depth=depth + 1
                )
        except zipfile.BadZipFile:
            continue
    return count


def inspect_archive(archive_path, include_icons=False):
    """Return metadata for resource modules without exposing other payloads."""
    reason = archive_rejection_reason(archive_path)
    if reason:
        return {
            "status": "Unsupported", "error": reason, "resource_packs": [],
            "archive_type": archive_extension(archive_path).lstrip("."),
        }
    if not archive_path or not os.path.isfile(archive_path):
        return {
            "status": "Unreadable", "error": "Archive file was not found.",
            "resource_packs": [], "archive_type": "",
        }
    packs = []

    def collect(archive, infos, candidate):
        record = dict(candidate["metadata"])
        record.update({
            "kind": candidate["kind"], "root": candidate["root"],
            "source": candidate["source"], "icon_name": "",
        })
        found = next(filter(None, (_direct_member(infos, candidate["root"], icon)
                                   for icon in PACK_ICON_NAMES)), None)
        if found is None and candidate["kind"] == "java_mod":
            # Mods name their logo in their metadata (the in-game Mods list shows it).
            logo = mod_logo(archive)
            found = (logo, infos[logo]) if logo in infos else None
        if found is not None:
            name, info = found
            record["icon_name"] = name
            if include_icons:
                try:
                    record["icon_bytes"] = _read_limited(archive, info, MAX_ICON_BYTES)
                except ArchiveInputError:
                    record["icon_bytes"] = b""
        packs.append(record)

    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            _walk_packs(archive, os.path.basename(archive_path), collect)
    except (OSError, zipfile.BadZipFile, ArchiveInputError, RuntimeError) as exc:
        return {
            "status": "Unreadable", "error": str(exc) or "Archive could not be opened.",
            "resource_packs": [],
            "archive_type": archive_extension(archive_path).lstrip("."),
        }
    if not packs:
        return {
            "status": "No Resource Packs",
            "error": "No resource-pack modules were found in the archive.",
            "resource_packs": [],
            "archive_type": archive_extension(archive_path).lstrip("."),
        }
    return {
        "status": "Ready", "error": "", "resource_packs": packs,
        "archive_type": archive_extension(archive_path).lstrip("."),
    }


def _target(root, relative):
    root = os.path.abspath(root)
    target = os.path.abspath(os.path.join(root, *PurePosixPath(relative).parts))
    try:
        if os.path.commonpath([root, target]) != root:
            raise ArchiveInputError(f"Unsafe archive member: {relative}")
    except ValueError as exc:
        raise ArchiveInputError(f"Unsafe archive member: {relative}") from exc
    return target


def _copy_pack(archive, infos, candidate, output, index):
    metadata = candidate["metadata"]
    root = candidate["root"]
    fallback = PurePosixPath(root).name if root else candidate["source"]
    name = _safe_name(str(metadata.get("name") or fallback or f"pack_{index + 1}"))
    destination = os.path.join(output, "resource_packs", f"{index:03d}_{name}")
    prefix = f"{root}/" if root else ""
    copied = 0
    member_prefixes = tuple(candidate.get("member_prefixes", ()))
    for member, info in infos.items():
        if prefix and not member.casefold().startswith(prefix.casefold()):
            continue
        relative = member[len(prefix):] if prefix else member
        if member_prefixes and not any(
            relative.casefold().startswith(value.casefold())
            for value in member_prefixes
        ):
            continue
        if not relative or relative.lower().endswith(".mcpack"):
            continue
        target = _target(destination, relative)
        if info.is_dir() or info.filename.endswith(("/", "\\")):
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with archive.open(info, "r") as source, open(target, "wb") as target_file:
            shutil.copyfileobj(source, target_file, length=1024 * 256)
        copied += 1
    if candidate.get("kind") == "java_mod" and copied:
        marker = _target(destination, ".java_mod_resource.json")
        with open(marker, "w", encoding="utf-8") as handle:
            json.dump(candidate.get("metadata", {}), handle, ensure_ascii=True, indent=2)
    if not copied:
        raise ArchiveInputError(f"Resource pack {candidate['source']} contained no files.")


def _note_mod_jar(output, archive_path):
    """Name the JAR in each extracted mod pack's marker: the asset browser lists
    the mod's mobs and models through it. Caches made before this are updated."""
    if archive_extension(archive_path) != ".jar":
        return
    jar = os.path.abspath(archive_path)
    root = os.path.join(output, "resource_packs")
    for name in os.listdir(root) if os.path.isdir(root) else ():
        marker = os.path.join(root, name, ".java_mod_resource.json")
        try:
            with open(marker, encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict) or data.get("jar") == jar:
                continue
            data["jar"] = jar
            with open(marker, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=True, indent=2)
        except (OSError, ValueError):
            continue


def extract_archive_result(archive_path, cache_dir):
    """Safely extract resource modules and return a structured status record."""
    reason = archive_rejection_reason(archive_path)
    if reason:
        return {
            "archive_path": archive_path, "output_path": "", "status": "Unsupported",
            "error": reason, "resource_pack_count": 0,
        }
    if not archive_path or not os.path.isfile(archive_path):
        return {
            "archive_path": archive_path, "output_path": "", "status": "Failed",
            "error": "Archive file was not found.", "resource_pack_count": 0,
        }
    os.makedirs(cache_dir, exist_ok=True)
    cached = _cached_output(archive_path, cache_dir)
    if cached:
        root = os.path.join(cached, "resource_packs")
        count = sum(
            os.path.isdir(os.path.join(root, name)) for name in os.listdir(root)
        ) if os.path.isdir(root) else 0
        _note_mod_jar(cached, archive_path)
        return {
            "archive_path": archive_path, "output_path": cached, "status": "Extracted",
            "error": "", "resource_pack_count": count,
        }
    output = _output_dir(archive_path, cache_dir)
    if os.path.exists(output):
        shutil.rmtree(output)
    os.makedirs(output, exist_ok=True)
    try:
        count = 0

        def copy(archive, infos, candidate):
            nonlocal count
            _copy_pack(archive, infos, candidate, output, count)
            count += 1

        with zipfile.ZipFile(archive_path, "r") as archive:
            _walk_packs(archive, os.path.basename(archive_path), copy)
        if not count:
            raise ArchiveInputError("No resource-pack modules were found in the archive.")
        _note_mod_jar(output, archive_path)
        with open(os.path.join(output, ".extracted_hash"), "w", encoding="utf-8") as marker:
            marker.write(_marker_value(archive_path))
        return {
            "archive_path": archive_path, "output_path": output, "status": "Extracted",
            "error": "", "resource_pack_count": count,
        }
    except (ArchiveInputError, OSError, zipfile.BadZipFile, RuntimeError) as exc:
        shutil.rmtree(output, ignore_errors=True)
        return {
            "archive_path": archive_path, "output_path": "", "status": "Failed",
            "error": str(exc) or "Could not extract archive", "resource_pack_count": 0,
        }
