import os
try:
    from ..importers.bedrock_assets import load_json_file
    from ..importers.blockstate_resolver import ResourcePackVFS, texture_slot_reference
except ImportError:  # Plain-Python unit tests without importing the bpy package root.
    from importers.bedrock_assets import load_json_file
    from importers.blockstate_resolver import ResourcePackVFS, texture_slot_reference

def _model_id_from_path(filepath):
    rel = filepath.replace("\\", "/")
    marker = "/assets/"
    lower = rel.lower()
    if marker not in lower:
        return os.path.splitext(os.path.basename(filepath))[0]
    idx = lower.rfind(marker)
    tail = rel[idx + len(marker):]
    parts = tail.split("/")
    if len(parts) >= 3 and parts[1] == "models":
        namespace = parts[0]
        model_path = "/".join(parts[2:])
        return f"{namespace}:{os.path.splitext(model_path)[0]}"
    return os.path.splitext(os.path.basename(filepath))[0]


def _find_pack_root(filepath):
    current = os.path.dirname(os.path.abspath(filepath))
    for _ in range(12):
        if os.path.isdir(os.path.join(current, "assets")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return os.path.dirname(os.path.abspath(filepath))


def _resolve_model_path(model_ref, pack_root, current_namespace="minecraft"):
    return ResourcePackVFS(pack_root).model(model_ref, current_namespace)


def _namespace_from_model(filepath):
    rel = filepath.replace("\\", "/")
    lower = rel.lower()
    marker = "/assets/"
    if marker in lower:
        tail = rel[lower.rfind(marker) + len(marker):]
        parts = tail.split("/")
        if parts:
            return parts[0]
    return "minecraft"


def _game_namespace_applies(reference, namespace):
    """Whether a bare reference in a non-vanilla model should try ``minecraft:``.

    The game reads ``block/chain`` as ``minecraft:block/chain`` whatever
    namespace the model is in (mod models use vanilla textures and parents
    that way). The model's own namespace still answers when nothing exists
    under ``minecraft:``, as Sparkle always resolved it.
    """
    return (bool(reference) and isinstance(reference, str) and namespace != "minecraft"
            and ":" not in reference and not reference.startswith(("#", "builtin/")))


def _merge_java_model(filepath, pack_root, seen=None):
    seen = seen or set()
    filepath = os.path.abspath(filepath)
    if filepath in seen:
        return {}
    seen.add(filepath)

    data = load_json_file(filepath)
    if not isinstance(data, dict):
        return {}
    loader = str(data.get("loader") or "")
    if loader.endswith(("separate_transforms", "separate-perspective")) and isinstance(data.get("base"), dict):
        # (Neo)Forge's per-view wrapper: ``base`` is the model held in hand
        # and placed in the world.
        base = data["base"]
        wrapper = data
        data = dict(base, **{key: wrapper[key] for key in ("display", "gui_light") if key in wrapper})
        if isinstance(wrapper.get("textures"), dict):
            data["textures"] = dict(wrapper["textures"], **(base.get("textures") or {}))

    namespace = _namespace_from_model(filepath)
    vfs = ResourcePackVFS(pack_root)
    parent_data = {}
    parent_ref = data.get("parent", "")
    parent_path = ""
    if _game_namespace_applies(parent_ref, namespace):
        parent_path = vfs.model(parent_ref, "minecraft")
    if not parent_path:
        parent_path = _resolve_model_path(parent_ref, pack_root, namespace)
    if parent_path:
        parent_data = _merge_java_model(parent_path, pack_root, seen)

    merged = dict(parent_data)
    textures = dict(parent_data.get("textures", {}) if isinstance(parent_data.get("textures"), dict) else {})
    for texture_key, reference in (data.get("textures", {}) if isinstance(data.get("textures"), dict) else {}).items():
        reference = texture_slot_reference(reference)
        if reference and not reference.startswith("#") and ":" not in reference:
            if _game_namespace_applies(reference, namespace) and vfs.texture("minecraft:" + reference):
                reference = "minecraft:" + reference
            else:
                reference = namespace + ":" + reference
        textures[texture_key] = reference
    if textures:
        merged["textures"] = textures
    for key, value in data.items():
        if key == "textures":
            continue
        if key == "elements" and isinstance(value, list):
            merged[key] = value
        elif key not in merged or key in {"display", "gui_light", "overrides", "parent"}:
            merged[key] = value
    return merged


def _resolve_texture_ref(texture_ref, textures):
    ref = texture_slot_reference(texture_ref)
    seen = set()
    while ref.startswith("#"):
        key = ref[1:]
        if key in seen:
            return ""
        seen.add(key)
        ref = texture_slot_reference(textures.get(key, ""))
    return ref


def _resolve_texture_path(texture_ref, textures, pack_root, default_namespace="minecraft"):
    # Resolve the exact shared reference across the whole stack before trying
    # legacy loose-pack paths. A same-named image must not mask a real match.
    vfs = ResourcePackVFS(pack_root)
    resolved = vfs.texture(texture_ref, textures, default_namespace)
    if resolved:
        return resolved
    for root in vfs.roots:
        if os.path.isdir(os.path.join(root, "assets")):
            continue
        resolved = _resolve_texture_path_legacy(texture_ref, textures, root, default_namespace)
        if resolved:
            return resolved
    return ""


def _resolve_texture_path_legacy(texture_ref, textures, pack_root, default_namespace="minecraft"):
    ref = _resolve_texture_ref(texture_ref, textures)
    if not ref:
        return ""
    if ":" in ref:
        namespace, path = ref.split(":", 1)
    else:
        namespace, path = default_namespace, ref
    path = path.replace("\\", "/")
    search_paths = [path]
    if path.startswith("textures/"):
        search_paths.append(path[len("textures/"):])
    elif path.startswith(("block/", "item/", "entity/", "custom/")):
        search_paths.append(path)

    namespaces = [namespace]
    assets_root = os.path.join(pack_root, "assets")
    if os.path.isdir(assets_root):
        for entry in os.listdir(assets_root):
            if entry not in namespaces and os.path.isdir(os.path.join(assets_root, entry)):
                namespaces.append(entry)

    for ns in namespaces:
        for rel_path in search_paths:
            roots = [os.path.join(pack_root, "assets", ns, "textures")]
            if rel_path.startswith("textures/"):
                roots.append(os.path.join(pack_root, "assets", ns))
            for root in roots:
                for ext in (".png", ".tga", ".jpg", ".jpeg", ""):
                    candidate = os.path.join(root, rel_path.replace("/", os.sep) + ext)
                    if os.path.isfile(candidate):
                        return candidate

    wanted = os.path.splitext(os.path.basename(path))[0].lower()
    if wanted and os.path.isdir(assets_root):
        for ns in namespaces:
            tex_root = os.path.join(assets_root, ns, "textures")
            if not os.path.isdir(tex_root):
                continue
            for root, _dirs, files in os.walk(tex_root):
                for filename in files:
                    stem, ext = os.path.splitext(filename)
                    if stem.lower() == wanted and ext.lower() in {".png", ".tga", ".jpg", ".jpeg"}:
                        return os.path.join(root, filename)
    return ""

_BUILTIN_ENTITY_TEXTURES = {
    "shield": "minecraft:entity/shield_base_nopattern",
    "shield_blocking": "minecraft:entity/shield_base_nopattern",
    "trident": "minecraft:entity/trident",
    "trident_in_hand": "minecraft:entity/trident",
    "trident_throwing": "minecraft:entity/trident",
    "spyglass": "minecraft:item/spyglass",
    "spyglass_in_hand": "minecraft:item/spyglass",
}


def resolve_builtin_entity_texture(data, filepath, pack_root, namespace="minecraft"):
    """Resolve textures for Java builtin/entity items that have no layer0 entry."""
    data = data if isinstance(data, dict) else {}
    parent = str(data.get("parent", "") or "").lower()
    stem = os.path.splitext(os.path.basename(filepath))[0].lower()
    if not parent.startswith("builtin/entity") and stem not in _BUILTIN_ENTITY_TEXTURES:
        return ""
    candidates = []
    mapped = _BUILTIN_ENTITY_TEXTURES.get(stem)
    if mapped:
        candidates.append(mapped)
    base_stem = stem
    for suffix in ("_blocking", "_in_hand", "_throwing"):
        if base_stem.endswith(suffix):
            base_stem = base_stem[:-len(suffix)]
            break
    candidates.extend((
        f"{namespace}:entity/{base_stem}",
        f"{namespace}:item/{base_stem}",
    ))
    for texture_ref in candidates:
        resolved = _resolve_texture_path(texture_ref, {}, pack_root, namespace)
        if resolved:
            return resolved
    return ""
