"""Discover Minecraft resource-pack locations on a Windows workstation."""

import os
from typing import Dict, Iterable, List

from .archive_inputs import DISCOVERABLE_ARCHIVE_EXTENSIONS


ARCHIVE_EXTENSIONS = DISCOVERABLE_ARCHIVE_EXTENSIONS
SKIP_DIRS = {
    "__MACOSX",
    ".git",
    ".svn",
    ".hg",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
    "server-resource-packs",
    "server_resource_packs",
    "resource_pack_cache",
    "behavior_packs",
    "development_behavior_packs",
    "world_behavior_packs",
    "world_resource_packs",
    "downloads", "cache", "caches", "minecraftworlds", "saves",
    "mods", "libraries", "assets",
}


def _expand_path(path: str) -> str:
    """Expand environment variables and user markers in a candidate path."""
    return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))


def _existing_dir(path: str) -> str:
    """Return an expanded directory path only if it exists."""
    expanded = _expand_path(path)
    return expanded if os.path.isdir(expanded) else ""


def _dedupe_roots(items: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    """Preserve order while removing duplicate root paths."""
    result = []
    seen = set()
    for item in items:
        root = item.get("root", "")
        if not root:
            continue
        key = os.path.normcase(os.path.abspath(root))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def discover_java_launcher_roots() -> List[Dict[str, str]]:
    """Find likely Java Edition resource-pack folders from common launchers."""
    candidates = [
        ("Minecraft Launcher", r"%APPDATA%\.minecraft\resourcepacks"),
        ("Prism Launcher", r"%APPDATA%\PrismLauncher\instances"),
        ("MultiMC", r"%APPDATA%\MultiMC\instances"),
        ("CurseForge", r"%USERPROFILE%\curseforge\minecraft\Instances"),
        ("CurseForge", r"%USERPROFILE%\Documents\CurseForge\Minecraft\Instances"),
        ("CurseForge", r"%APPDATA%\CurseForge\minecraft\Instances"),
        ("Modrinth", r"%APPDATA%\ModrinthApp\profiles"),
        ("Modrinth", r"%USERPROFILE%\AppData\Roaming\ModrinthApp\profiles"),
        ("Lunar Client", r"%USERPROFILE%\.lunarclient\offline\multiver"),
    ]
    roots = []
    for launcher, raw_path in candidates:
        root = _existing_dir(raw_path)
        if root:
            roots.append({"launcher": launcher, "root": root, "kind": "java"})
    installed = []
    for item in roots:
        for folder in _installed_pack_folders(item['root'], {'resourcepacks'}):
            installed.append(dict(item, root=folder))
    return _dedupe_roots(installed)


def _installed_pack_folders(root, names, max_depth=6):
    """Walk instance containers only until an installed resource-pack folder."""
    skipped = {name.lower() for name in SKIP_DIRS}
    for current, dirs, _files in os.walk(root):
        if os.path.basename(current).lower() in names:
            yield current
            dirs[:] = []
            continue
        rel = os.path.relpath(current, root)
        depth = 0 if rel == '.' else rel.count(os.sep)+1
        dirs[:] = [] if depth >= max_depth else [d for d in dirs if d.lower() not in skipped]


def discover_bedrock_roots() -> List[Dict[str, str]]:
    """Find likely Bedrock Edition resource-pack folders."""
    base = (
        r"%LOCALAPPDATA%\Packages\Microsoft.MinecraftUWP_8wekyb3d8bbwe"
        r"\LocalState\games\com.mojang"
    )
    candidates = [
        ("Minecraft Bedrock", base + r"\resource_packs"),
        ("Minecraft Bedrock Development", base + r"\development_resource_packs"),
    ]
    roots = []
    for launcher, raw_path in candidates:
        root = _existing_dir(raw_path)
        if root:
            roots.append({'launcher': launcher, 'root': root, 'kind': 'bedrock'})
    containers = [
        ('Minecraft Bedrock', r'%APPDATA%\Minecraft Bedrock\Users'),
        ('Minecraft Preview', r'%APPDATA%\Minecraft Bedrock Preview\Users'),
        ('Bedrock Launcher', r'%APPDATA%\BedrockLauncher\instances'),
        ('Bedrock Launcher', r'%LOCALAPPDATA%\BedrockLauncher\instances'),
    ]
    packages = _existing_dir(r'%LOCALAPPDATA%\Packages')
    if packages:
        try:
            package_names = os.listdir(packages)
        except OSError:
            package_names = []
        for name in package_names:
            if name.lower().startswith(('microsoft.minecraftuwp_', 'microsoft.minecraftwindowsbeta_')):
                containers.append(('Minecraft Bedrock', os.path.join(packages,name,'LocalState','games','com.mojang')))
    for launcher, candidate in containers:
        root = _existing_dir(candidate)
        if root:
            for folder in _installed_pack_folders(root, {'resource_packs', 'development_resource_packs'}):
                roots.append({'launcher': launcher, 'root': folder, 'kind': 'bedrock'})
    return _dedupe_roots(roots)


def _iter_files_limited(root: str, max_depth: int = 3):
    """Yield files under root without walking arbitrarily deep trees."""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return
    for current, dirs, files in os.walk(root):
        rel = os.path.relpath(current, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth >= max_depth:
            dirs[:] = []
        else:
            dirs[:] = [
                d for d in dirs
                if d.lower() not in {name.lower() for name in SKIP_DIRS} and not d.startswith(".")
            ]
        for filename in files:
            yield os.path.join(current, filename)


def discover_archive_files(extra_roots: Iterable[str] = ()) -> List[str]:
    """Find supported inputs plus project/structure files that need clear rejection."""
    roots = list(extra_roots or [])

    result = []
    seen = set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for filepath in _iter_files_limited(root, max_depth=1):
            if os.path.splitext(filepath.lower())[1] not in ARCHIVE_EXTENSIONS:
                continue
            key = os.path.normcase(os.path.abspath(filepath))
            if key in seen:
                continue
            seen.add(key)
            result.append(filepath)
    return result


def discover_all_roots_and_archives() -> Dict[str, List]:
    """Return discovered pack roots and archives in one payload."""
    roots = discover_java_launcher_roots() + discover_bedrock_roots()
    archives = discover_archive_files(item["root"] for item in roots)
    # JAR discovery is restricted to installed game versions, never mods/.
    from .vanilla_assets import discover_vanilla_jars
    archives = [path for path in archives if not path.lower().endswith('.jar')]
    archives.extend(path for _version, path in discover_vanilla_jars())
    return {"roots": _dedupe_roots(roots), "archives": archives}
