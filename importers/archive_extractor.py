"""Safe extraction helpers for Minecraft resource-pack archives."""

import hashlib
import os
import re
import shutil
import zipfile
from typing import Dict, Iterable, List, Optional
from .archive_inputs import (
    DISCOVERABLE_ARCHIVE_EXTENSIONS,
    REJECTED_ARCHIVE_EXTENSIONS,
    SUPPORTED_ARCHIVE_EXTENSIONS,
    archive_rejection_reason,
    cached_archive_output,
    extract_archive_result,
    inspect_archive,
)



def _safe_name(value: str) -> str:
    """Return a filesystem-friendly name while keeping it readable."""
    from .utils import sanitize_pack_name
    stem = os.path.splitext(os.path.basename(value or "pack"))[0]
    stem = sanitize_pack_name(stem)
    return stem or "pack"


def get_cache_dir(addon_dir: str) -> str:
    """Keep Windows archive paths short enough for Blender's file APIs."""
    local = os.environ.get("LOCALAPPDATA", "")
    if os.name == "nt" and local:
        return os.path.join(local, "MCAddon", "packs")
    return os.path.join(addon_dir, "mc_asset_cache")


def user_cache_dir(name: str) -> str:
    """Per-user cache folder that survives add-on reinstalls and updates.

    Anything stored inside the installed extension folder is deleted every
    time a new build is installed, so thumbnails and pack indexes live here.
    """
    local = os.environ.get("LOCALAPPDATA", "")
    if os.name == "nt" and local:
        base = os.path.join(local, "MCAddon")
    elif os.path.isdir(os.path.expanduser("~/Library/Caches")):
        base = os.path.expanduser("~/Library/Caches/MCAddon")
    else:
        base = os.path.join(
            os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
            "MCAddon",
        )
    return os.path.join(base, name)


def archive_hash(filepath: str) -> str:
    """Return a cheap change-detection hash for an archive path."""
    stat = os.stat(filepath)
    payload = f"{os.path.abspath(filepath)}:{stat.st_size}:{stat.st_mtime_ns}"
    return hashlib.sha1(payload.encode("utf-8", errors="ignore")).hexdigest()


def _archive_output_dir(archive_path: str, cache_dir: str) -> str:
    digest = archive_hash(archive_path)[:12]
    return os.path.join(cache_dir, f"{_safe_name(archive_path)}_{digest}")


def is_archive_extracted(archive_path: str, cache_dir: str) -> Optional[str]:
    """Return a cached extraction only when it uses the resource-only format."""
    return cached_archive_output(archive_path, cache_dir)


def _safe_extract_zip(zip_file: zipfile.ZipFile, output_dir: str) -> None:
    """Extract a zip archive while rejecting path traversal entries."""
    output_abs = os.path.abspath(output_dir)
    for info in zip_file.infolist():
        target = os.path.abspath(os.path.join(output_abs, info.filename))
        try:
            if os.path.commonpath([output_abs, target]) != output_abs:
                raise ValueError(f"Unsafe archive member: {info.filename}")
        except ValueError as exc:
            raise ValueError(f"Unsafe archive member: {info.filename}") from exc
        zip_file.extract(info, output_abs)


def extract_archive(archive_path: str, cache_dir: str) -> Optional[str]:
    """Extract only resource-pack modules and return their cache folder."""
    result = extract_archive_result(archive_path, cache_dir)
    output = result.get("output_path", "")
    return str(output) if output else None


def extract_archives(archive_paths: Iterable[str], addon_dir: str) -> List[Dict[str, object]]:
    """Extract multiple archives and return status records."""
    cache_dir = get_cache_dir(addon_dir)
    return [extract_archive_result(path, cache_dir) for path in archive_paths]
