"""Priority-stacked Java resource-pack VFS and blockstate resolver.

A block state string such as ``minecraft:oak_stairs[facing=east,half=top]``
maps to one or more model references through either the ``variants`` form
(pick the single matching key) or the ``multipart`` form (accumulate every
entry whose ``when`` condition passes). Both forms are handled here so the
geometry layer only ever deals with a flat list of model references.

All blockstate, model and texture lookups use :class:`ResourcePackVFS`. Roots
are searched from index zero (highest priority) through every enabled pack and
then Vanilla. Each resource is resolved independently, which is what permits a
model from one pack to inherit a texture override from another pack and parent
properties from Vanilla.

Pure Python - no ``bpy`` - so it can be exercised outside Blender.
"""

import glob
import os

try:
    from .bedrock_assets import load_json_file
except ImportError:  # pragma: no cover - plain Python test path
    from importers.bedrock_assets import load_json_file


DEFAULT_NAMESPACE = "minecraft"
RESOURCE_FOLDERS = {
    "blockstate": "blockstates",
    "model": "models",
    "texture": "textures",
}
RESOURCE_EXTENSIONS = {
    "blockstate": (".json",),
    "model": (".json",),
    "texture": (".png", ".tga", ".jpg", ".jpeg"),
}


def parse_block_state(state):
    """Split ``ns:name[a=1,b=2]`` into ``("ns:name", {"a": "1", "b": "2"})``."""
    text = str(state or "").strip()
    properties = {}
    bracket = text.find("[")
    if bracket >= 0:
        body = text[bracket + 1:].rstrip()
        if body.endswith("]"):
            body = body[:-1]
        text = text[:bracket]
        for pair in body.split(","):
            if not pair:
                continue
            key, sep, value = pair.partition("=")
            if sep:
                properties[key.strip()] = value.strip()
    if ":" not in text:
        text = "%s:%s" % (DEFAULT_NAMESPACE, text)
    return text, properties


def split_namespace(identifier):
    namespace, _sep, path = str(identifier or "").partition(":")
    if not path:
        return DEFAULT_NAMESPACE, namespace
    return namespace or DEFAULT_NAMESPACE, path


def _root_key(root):
    try:
        return os.path.normcase(os.path.abspath(os.fspath(root)))
    except (OSError, TypeError, ValueError):
        return ""


def as_roots(pack_roots, vanilla_root=""):
    """Return a deduplicated priority list, with Vanilla always appended.

    ``pack_roots`` may be one path, an iterable, a :class:`ResourcePackVFS`, or
    a Blender-like scene exposing ``mcbedrock_active_packs``. The scene form is
    duck-typed deliberately so this module remains importable without Blender.
    """
    if not pack_roots:
        values = []
    elif isinstance(pack_roots, ResourcePackVFS):
        values = list(pack_roots.roots)
    elif isinstance(pack_roots, (str, bytes, os.PathLike)):
        values = [pack_roots]
    elif hasattr(pack_roots, "mcbedrock_active_packs"):
        values = [
            getattr(item, "path", "")
            for item in getattr(pack_roots, "mcbedrock_active_packs", ())
        ]
        if not any(values):
            values = [getattr(pack_roots, "mcbedrock_resource_pack_path", "")]
    else:
        values = list(pack_roots)

    if vanilla_root:
        values.append(vanilla_root)
    roots = []
    seen = set()
    for root in values:
        if not root:
            continue
        key = _root_key(root)
        if not key or key in seen:
            continue
        seen.add(key)
        roots.append(os.path.abspath(os.fspath(root)))
    return roots


def _split_resource_identifier(identifier, resource_type,
                               default_namespace=DEFAULT_NAMESPACE):
    """Normalize namespace, logical path, and an optional authored extension."""
    if resource_type not in RESOURCE_FOLDERS:
        raise ValueError("Unsupported resource type: %s" % resource_type)
    raw = str(identifier or "").strip().replace("\\", "/")
    namespace = default_namespace or DEFAULT_NAMESPACE

    if raw.startswith("assets/"):
        parts = raw.split("/")
        if len(parts) >= 4:
            namespace = parts[1] or namespace
            raw = "/".join(parts[2:])
    elif ":" in raw:
        namespace, raw = raw.split(":", 1)
        namespace = namespace or default_namespace or DEFAULT_NAMESPACE

    raw = raw.lstrip("/")
    folder = RESOURCE_FOLDERS[resource_type]
    if raw.startswith(folder + "/"):
        raw = raw[len(folder) + 1:]

    extension = ""
    lower = raw.lower()
    for candidate in RESOURCE_EXTENSIONS[resource_type]:
        if lower.endswith(candidate):
            raw = raw[:-len(candidate)]
            extension = candidate
            break
    return namespace, raw.strip("/"), extension


def _resource_candidates(pack_root, resource_type, identifier,
                         default_namespace=DEFAULT_NAMESPACE):
    namespace, path, authored_extension = _split_resource_identifier(
        identifier, resource_type, default_namespace
    )
    if not path:
        return []
    folder = RESOURCE_FOLDERS[resource_type]
    base = os.path.join(
        os.fspath(pack_root), "assets", namespace, folder,
        path.replace("/", os.sep),
    )
    extensions = (authored_extension,) if authored_extension else \
        RESOURCE_EXTENSIONS[resource_type]
    return [base + extension for extension in extensions]


# Vanilla textures Mojang renamed: (old path, new path). A pack or mod made
# for an older game still names the old file.
VANILLA_TEXTURE_RENAMES = {
    "block/grass": "block/short_grass",  # 1.20.3
    "item/grass": "item/short_grass",
    "block/chain": "block/iron_chain",  # 1.21.9
    "item/chain": "item/iron_chain",
}


def resolve_resource_path(pack_roots, resource_type, identifier,
                          default_namespace=DEFAULT_NAMESPACE):
    """Resolve one logical resource; pack priority outranks extension order."""
    for pack_root in as_roots(pack_roots):
        for candidate in _resource_candidates(
            pack_root, resource_type, identifier, default_namespace
        ):
            if os.path.isfile(candidate):
                return candidate
    if resource_type == "texture":
        # Only consulted when nothing answers under the authored name.
        namespace, path, _extension = _split_resource_identifier(
            identifier, resource_type, default_namespace
        )
        renamed = VANILLA_TEXTURE_RENAMES.get(path) if namespace == DEFAULT_NAMESPACE else None
        if renamed:
            return resolve_resource_path(
                pack_roots, resource_type, f"{DEFAULT_NAMESPACE}:{renamed}", default_namespace
            )
    return ""


def texture_slot_reference(value):
    """A model texture slot's sprite reference.

    Minecraft 26.x may write a slot as ``{"sprite": ..., "force_translucent":
    ...}`` instead of a bare string; both name the same texture.
    """
    if isinstance(value, dict):
        value = value.get("sprite", "")
    return value if isinstance(value, str) else ""


def _resolve_texture_reference(texture_ref, textures):
    """Follow Java ``#alias`` references in a merged model texture table.

    A face may name its slot without the ``#`` (``"texture": "0"``); the
    game reads that as the same slot.
    """
    reference = texture_slot_reference(texture_ref)
    seen = set()
    textures = textures if isinstance(textures, dict) else {}
    if reference and not reference.startswith("#") and reference in textures:
        reference = "#" + reference
    while reference.startswith("#"):
        key = reference[1:]
        if key in seen:
            return ""
        seen.add(key)
        reference = texture_slot_reference(textures.get(key, ""))
    return reference


def resolve_model_path(pack_roots, model_ref,
                       default_namespace=DEFAULT_NAMESPACE):
    """First model JSON supplied by the ordered pack stack."""
    if not model_ref or str(model_ref).startswith("builtin/"):
        return ""
    return resolve_resource_path(
        pack_roots, "model", model_ref, default_namespace
    )


def resolve_texture_path(pack_roots, texture_ref, textures=None,
                         default_namespace=DEFAULT_NAMESPACE):
    """First texture supplied by the stack after resolving model aliases.

    A sprite name an atlas gives to another texture (``"type": "single"``
    with ``"sprite"``, as Origin Realms' ``block_as_item/...``) resolves to
    that texture.
    """
    reference = _resolve_texture_reference(texture_ref, textures)
    if not reference:
        return ""
    found = resolve_resource_path(pack_roots, "texture", reference, default_namespace)
    if found:
        return found
    namespace, _, path = reference.partition(":") if ":" in reference else (default_namespace, "", reference)
    renamed = atlas_sprites(pack_roots).get(f"{namespace.lower()}:{path.lower()}")
    if renamed:
        return resolve_resource_path(pack_roots, "texture", renamed, default_namespace)
    return ""


_ATLAS_SPRITES = {}


def atlas_sprites(pack_roots):
    """``sprite name -> texture`` for atlas sources that rename a texture,
    read from every root and its version overlay folders."""
    roots = tuple(str(root) for root in as_roots(pack_roots) if root)
    cached = _ATLAS_SPRITES.get(roots)
    if cached is not None:
        return cached
    sprites = {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        folders = [root] + [os.path.join(root, name) for name in sorted(os.listdir(root))
                            if os.path.isdir(os.path.join(root, name, "assets"))]
        for folder in folders:
            for path in glob.glob(os.path.join(folder, "assets", "*", "atlases", "*.json")):
                try:
                    sources = load_json_file(path).get("sources", [])
                except Exception:  # noqa: BLE001 - a broken atlas must not stop imports
                    continue
                for source in sources if isinstance(sources, list) else ():
                    if (isinstance(source, dict) and str(source.get("type", "")).split(":")[-1] == "single"
                            and isinstance(source.get("sprite"), str) and isinstance(source.get("resource"), str)):
                        sprite = source["sprite"] if ":" in source["sprite"] else "minecraft:" + source["sprite"]
                        sprites.setdefault(sprite.lower(), source["resource"])
    _ATLAS_SPRITES[roots] = sprites
    return sprites


class ResourcePackVFS:
    """Small immutable view of a pack priority stack plus Vanilla fallback."""

    def __init__(self, pack_roots=(), vanilla_root=""):
        self.roots = tuple(as_roots(pack_roots, vanilla_root))

    def resolve(self, resource_type, identifier,
                default_namespace=DEFAULT_NAMESPACE):
        return resolve_resource_path(
            self.roots, resource_type, identifier, default_namespace
        )

    def blockstate(self, identifier):
        return self.resolve("blockstate", identifier)

    def model(self, identifier, default_namespace=DEFAULT_NAMESPACE):
        return resolve_model_path(self.roots, identifier, default_namespace)

    def texture(self, identifier, textures=None,
                default_namespace=DEFAULT_NAMESPACE):
        return resolve_texture_path(
            self.roots, identifier, textures, default_namespace
        )


# Descriptive alias for callers that prefer the longer name.
VirtualFileSystemResolver = ResourcePackVFS


def blockstate_path(pack_root, identifier):
    """Absolute path of the blockstates JSON for a block identifier."""
    candidates = _resource_candidates(pack_root, "blockstate", identifier)
    return candidates[0] if candidates else ""


def load_blockstate_definition(pack_roots, identifier):
    """Return the parsed blockstates JSON from the first root that has it.

    Roots are searched in order, so a texture-only pack listed first still
    falls through to vanilla for the definitions it does not override.
    """
    path = ResourcePackVFS(pack_roots).blockstate(identifier)
    if path:
        try:
            data = load_json_file(path)
        except Exception:
            data = None
        if isinstance(data, dict):
            return data
    return None


# ---------------------------------------------------------------------------
# variants
# ---------------------------------------------------------------------------

def _variant_key_matches(key, properties):
    """``"facing=east,half=top"`` matches only if every pair is satisfied."""
    key = str(key or "").strip()
    if not key:
        return True  # The empty key is the catch-all variant.
    for pair in key.split(","):
        if not pair:
            continue
        name, sep, value = pair.partition("=")
        if not sep:
            continue
        if str(properties.get(name.strip(), "")) != value.strip():
            return False
    return True


def _first_model(entry):
    """Weighted variant lists are random in game; take the first for determinism."""
    if isinstance(entry, list):
        for item in entry:
            if isinstance(item, dict):
                return item
        return None
    return entry if isinstance(entry, dict) else None


def _normalize_apply(entry):
    model = _first_model(entry)
    if not model or not model.get("model"):
        return None
    return {
        "model": str(model.get("model")),
        "x": float(model.get("x", 0) or 0),
        "y": float(model.get("y", 0) or 0),
        "uvlock": bool(model.get("uvlock", False)),
    }


def _select_variant(variants, properties):
    if not isinstance(variants, dict):
        return []
    best_key = None
    best_score = -1
    for key in variants:
        if not _variant_key_matches(key, properties):
            continue
        # Prefer the most specific match so "" never beats a real condition.
        score = len([p for p in str(key).split(",") if p])
        if score > best_score:
            best_key, best_score = key, score
    if best_key is None:
        return []
    applied = _normalize_apply(variants[best_key])
    return [applied] if applied else []


# ---------------------------------------------------------------------------
# multipart
# ---------------------------------------------------------------------------

def _condition_matches(condition, properties):
    """One ``when`` clause; values may list alternates as ``"north|south"``."""
    if not isinstance(condition, dict):
        return False
    for name, expected in condition.items():
        if name in ("OR", "AND"):
            continue
        actual = str(properties.get(name, ""))
        if isinstance(expected, bool):
            expected = "true" if expected else "false"
        alternatives = [part.strip() for part in str(expected).split("|")]
        if actual not in alternatives:
            return False
    return True


def _when_matches(when, properties):
    if when is None:
        return True
    if not isinstance(when, dict):
        return False
    if "OR" in when:
        clauses = when.get("OR") or []
        if not isinstance(clauses, list):
            return False
        if not any(_when_matches(clause, properties) for clause in clauses):
            return False
    if "AND" in when:
        clauses = when.get("AND") or []
        if not isinstance(clauses, list):
            return False
        if not all(_when_matches(clause, properties) for clause in clauses):
            return False
    plain = {k: v for k, v in when.items() if k not in ("OR", "AND")}
    if plain and not _condition_matches(plain, properties):
        return False
    return True


def _select_multipart(multipart, properties):
    if not isinstance(multipart, list):
        return []
    selected = []
    for part in multipart:
        if not isinstance(part, dict):
            continue
        if not _when_matches(part.get("when"), properties):
            continue
        applied = _normalize_apply(part.get("apply"))
        if applied:
            selected.append(applied)
    return selected


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def resolve_block_models(pack_roots, state, definition=None):
    """Return ``[{model, x, y, uvlock}, ...]`` for one block state string.

    An empty list means no root has a blockstates file for the block, or no
    condition matched - the caller decides whether that is a miss worth
    reporting or an intentionally invisible block.
    """
    identifier, properties = parse_block_state(state)
    if definition is None:
        definition = load_blockstate_definition(pack_roots, identifier)
    if not definition:
        return []
    if "variants" in definition:
        return _select_variant(definition.get("variants"), properties)
    if "multipart" in definition:
        return _select_multipart(definition.get("multipart"), properties)
    return []


def block_asset_context(metadata, resource_roots=(), vanilla_path=""):
    """Shared preview/import inputs; the active stack wins over card origin."""
    metadata = metadata if isinstance(metadata, dict) else {}
    roots = as_roots(resource_roots)
    if not roots:
        roots = as_roots(metadata.get("pack_root", ""))
    return roots, str(vanilla_path or metadata.get("vanilla_root") or "")
