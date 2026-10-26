"""What a mod JAR offers for import: mobs, blocks and items.

A JAR picked in File > Import > Minecraft Mod is read on its own. It never
joins the resource-pack list: its assets are extracted to the archive cache
(resource files only) and layered under the active packs for that import,
the same order the game uses (resource packs override mod assets).

- Mobs come from code. Each entity renderer names its model class (Citadel
  models are supported, see ``mod_models``); other Citadel model classes the
  mod ships (block-entity and item models) are listed too.
- Blocks and items are the mod's Java JSON models, imported by the existing
  Java model builder unchanged.
Nothing here imports Blender.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import zipfile
import zlib
from dataclasses import asdict, dataclass, field

from .archive_inputs import extract_archive_result, inspect_archive, is_mod_jar, mod_logo  # noqa: F401
from .java_classes import ClassFormatError, JarClassPath
from .java_vm import JObject
from .mod_layer_models import factory_display_name, layer_factories
from .mod_models import (
    LIVING_RENDERER,
    MOB_RENDERER,
    is_citadel_model_class,
    is_entity_renderer_class,
    renderer_entity_class,
)

CATALOG_VERSION = 1
_TEXTURE_SUFFIXES = (".png",)


@dataclass
class ModAsset:
    key: str
    kind: str  # "entity", "model", "block" or "item"
    name: str
    identifier: str
    renderer_class: str = ""
    model_class: str = ""
    model_path: str = ""  # assets/<namespace>/models/... for blocks and items, geo/... for GeckoLib
    layer_method: str = ""  # name + descriptor of a LayerDefinition factory
    textures: list = field(default_factory=list)
    animation_path: str = ""  # GeckoLib: assets/<namespace>/animations/....animation.json

    @property
    def geckolib(self):
        return self.key.startswith("geo:")


@dataclass
class ModCatalog:
    path: str
    name: str
    mod_id: str
    namespaces: list
    assets: list
    unsupported: list = field(default_factory=list)
    # Item models with no geometry: the game draws them in code (builtin/entity).
    code_drawn: list = field(default_factory=list)
    # Block/item models left out, by reason (see ``model_issue``).
    skipped: dict = field(default_factory=dict)

    def get(self, key):
        return next((asset for asset in self.assets if asset.key == key), None)


def _humanize(text):
    text = re.sub(r"^(Model|Render|Entity)(?=[A-Z])", "", text)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return text.replace("_", " ").strip().title() if "_" in text else text.strip()


def _snake(text):
    text = re.sub(r"^(Entity|Model|Render)(?=[A-Z])", "", text)
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text).lower()


def _language(archive, namespaces):
    names = {}
    for namespace in namespaces:
        try:
            raw = archive.read(f"assets/{namespace}/lang/en_us.json")
        except KeyError:
            continue
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeError, ValueError):
            continue
        if isinstance(data, dict):
            names.update({str(key): str(value) for key, value in data.items()})
    return names


def _created_models(class_path, class_name, methods=("<init>", "<clinit>")):
    """Citadel model classes a class constructs, in bytecode order."""
    parsed = class_path.get(class_name)
    found = []
    if parsed is None:
        return found
    for method in parsed.methods:
        if methods is not None and method.name not in methods:
            continue
        code = parsed.code(method)
        if code is None:
            continue
        try:
            instructions = code.instructions
        except ValueError:
            continue
        for pc in sorted(instructions):
            name, operands, _next_pc = instructions[pc]
            if name != "new":
                continue
            created = parsed.class_name(operands[0])
            if created not in found and is_citadel_model_class(class_path, created):
                found.append(created)
    return found


def _texture_strings(class_path, class_name):
    parsed = class_path.get(class_name)
    if parsed is None:
        return []
    return [value for value in parsed.string_constants()
            if "textures/" in value and value.lower().endswith(_TEXTURE_SUFFIXES)]


def _tokens(text):
    # CamelCase and acronyms: "AMElytra" -> am, elytra.
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", str(text))
    return {token for token in re.split(r"[^a-z0-9]+", text.lower()) if token}


def _compound_tokens(text):
    """``_tokens`` plus each pair of neighbours written as one word (the_spawn: thespawn)."""
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", str(text))
    parts = [token for token in re.split(r"[^a-z0-9]+", text.lower()) if token]
    return set(parts) | {first + second for first, second in zip(parts, parts[1:])}


_NAME_NOISE = {"model", "renderer", "render", "tile", "block", "entity", "layer", "mesh",
               "create", "base", "client", "registry", "texture", "textures"}


def rank_textures(model_class, textures):
    """Order textures by how well their file name matches the model's name."""
    wanted = _tokens(model_class.rsplit("/", 1)[-1]) - {"model"}

    def score(texture):
        path = texture.split(":", 1)[-1]
        stem_tokens = _tokens(os.path.splitext(path.rsplit("/", 1)[-1])[0])
        path_tokens = _tokens(path) - {"textures", "entity", "png"}
        return (-len(wanted & path_tokens), len(stem_tokens - wanted), textures.index(texture))

    return sorted(textures, key=score)


def _rank_by_tokens(wanted, textures):
    def score(texture):
        stem = _tokens(texture.split(":", 1)[-1].rsplit("/", 1)[-1].rsplit(".", 1)[0])
        return (-len(wanted & stem), len(stem - wanted), textures.index(texture))
    return sorted(textures, key=score)


def scan_mod_jar(jar_path):
    """List the mod's importable mobs, models, blocks and items (no code runs)."""
    inspection = inspect_archive(jar_path)
    packs = inspection.get("resource_packs", [])
    record = packs[0] if packs else {}
    namespaces = list(record.get("namespaces", ()))
    with zipfile.ZipFile(jar_path, "r") as archive:
        class_path = JarClassPath(archive)
        language = _language(archive, namespaces)
        assets = []
        unsupported = []
        vanilla_renderers = []
        covered = set()
        mod_namespace = record.get("uuid") or (namespaces[0] if namespaces else "")
        for name in class_path.names():
            if not is_entity_renderer_class(class_path, name):
                continue
            models = _created_models(class_path, name)
            if not models:
                vanilla_renderers.append(name)
                continue
            model_class = models[0]
            covered.add(model_class)
            entity_class = renderer_entity_class(class_path, name)
            entity_id = _snake((entity_class or name).rsplit("/", 1)[-1])
            display = (language.get(f"entity.{mod_namespace}.{entity_id}")
                       or _humanize(name.rsplit("/", 1)[-1]))
            assets.append(ModAsset(
                key=f"entity:{name}",
                kind="entity",
                name=display,
                identifier=f"{mod_namespace}:{entity_id}",
                renderer_class=name,
                model_class=model_class,
                textures=rank_textures(model_class, _texture_strings(class_path, name)),
            ))
        # Citadel models no entity renderer uses as its main model.
        users = {}
        for name in class_path.names():
            for created in _created_models(class_path, name, methods=None):
                users.setdefault(created, []).append(name)
        for model_class, owners in sorted(users.items()):
            if model_class in covered:
                continue
            textures = []
            for owner in owners:
                textures.extend(value for value in _texture_strings(class_path, owner)
                                if value not in textures)
            assets.append(ModAsset(
                key=f"model:{model_class}",
                kind="model",
                name=_humanize(model_class.rsplit("/", 1)[-1]),
                identifier=f"{mod_namespace}:{_snake(model_class.rsplit('/', 1)[-1])}",
                model_class=model_class,
                textures=rank_textures(model_class, textures),
            ))
        # Vanilla-style mobs: the renderer bakes a registered LayerDefinition.
        all_textures = sorted(
            f"{match.group(1)}:{match.group(2)}"
            for match in (re.match(r"^assets/([^/]+)/(textures/entity/.+\.png)$", name)
                          for name in archive.namelist()) if match)
        baked_factories = set()
        invisible = []
        for asset, factory_key in _vanilla_mob_assets(class_path, vanilla_renderers, language,
                                                      mod_namespace, all_textures, unsupported,
                                                      archive, invisible):
            if asset is not None:
                assets.append(asset)
            if factory_key:
                baked_factories.add(factory_key)
        members_set = set(archive.namelist())
        # Vanilla-style models: static LayerDefinition factories.
        jar_textures = [
            f"{match.group(1)}:{match.group(2)}"
            for match in (re.match(r"^assets/([^/]+)/(textures/.+\.png)$", name)
                          for name in archive.namelist())
            if match and not _NOT_MODEL_TEXTURES.match(match.group(2))
        ]
        factories = layer_factories(class_path)
        constants = _texture_constants(class_path, members_set) if factories else []
        for class_name, method in factories:
            if f"{class_name}.{method.name}{method.desc}" in baked_factories:
                continue  # listed as the mob that wears it
            label = factory_display_name(class_name, method.name)
            wanted = (_tokens(label) | _tokens(method.name)) - _NAME_NOISE
            textures = [value for value in _texture_strings(class_path, class_name)]
            # Textures the mod names in constants (ModTextures.GLOBE_EARTH_TEXTURE),
            # matched by the constant's name, in the order the mod declares them.
            label_tokens = _tokens(label) - _NAME_NOISE
            # The one constant the model's own class draws with (PartyHatLayer reads
            # ModTextures.PARTY_CREEPER) comes first; a class that picks among
            # several by state (a merchant's colours) leaves it to the names.
            read = [location for location in _read_constants(class_path, class_name, constants)
                    if location not in textures]
            read = read if len(read) == 1 and not _OVERLAY_TEXTURE.search(_stem(read[0])) else []
            named = [location for _owner, field_name, location in constants
                     if label_tokens & _tokens(field_name) and location not in textures]
            # Closest file name first; the mod's own order breaks ties (its
            # default globe is declared before the sepia and special ones).
            named = sorted(dict.fromkeys(named), key=lambda location: (
                -len(label_tokens & _tokens(_stem(location))), named.index(location)))
            if not textures:
                textures = [value for value in jar_textures
                            if wanted & _tokens(value.split(":", 1)[-1].rsplit("/", 1)[-1])]
            others = [value for value in _rank_by_tokens(wanted, textures) if value not in named]
            # One ranking: the closest file name first, a named constant on ties.
            textures = sorted(named + others, key=lambda location: (
                -len(label_tokens & _tokens(_stem(location))),
                bool(_OVERLAY_TEXTURE.search(_stem(location))), location not in named,
                (named + others).index(location)))
            textures = read + [value for value in textures if value not in read]
            assets.append(ModAsset(
                key=f"layer:{class_name}.{method.name}{method.desc}",
                kind="model",
                name=label,
                identifier=f"{mod_namespace}:{_snake(label.replace(' ', '').replace('(', '_').replace(')', ''))}",
                model_class=class_name,
                layer_method=method.name + method.desc,
                textures=textures[:12],
            ))
        geo_files = sorted(name for name in archive.namelist()
                           if re.match(r"^assets/[^/]+/geo/.+\.json$", name) and _geo_has_cubes(archive, name))
        if geo_files:
            members = set(archive.namelist())
            # GeckoLib armor and items wear item textures too.
            geo_textures = [
                f"{match.group(1)}:{match.group(2)}"
                for match in (re.match(r"^assets/([^/]+)/(textures/.+\.png)$", name)
                              for name in archive.namelist())
                if match and not _NOT_GEO_TEXTURES.match(match.group(2))
            ]
            assets.extend(_geckolib_assets(class_path, members, geo_files, language,
                                           mod_namespace, geo_textures, archive, invisible))
        # Pieces of another mob (an anaconda's segments) are not mobs of their own.
        parts = _body_parts(class_path, archive, language, assets)
        skipped = {INVISIBLE: sorted(invisible)} if invisible else {}
        if parts:
            skipped[PART_OF_ANOTHER_MOB] = sorted(asset.identifier for asset in assets if asset.key in parts)
            assets = [asset for asset in assets if asset.key not in parts]
        assets.sort(key=lambda asset: (asset.kind != "entity", asset.name.casefold()))
        _disambiguate(assets)
        json_models = []
        code_drawn = []
        parents = {}
        documents = {}
        members = set(archive.namelist())
        for info in archive.infolist():
            match = re.match(r"^assets/([^/]+)/models/(block|item)/(.+)\.json$", info.filename)
            if not match or info.is_dir():
                continue
            namespace, kind, path = match.groups()
            identifier = f"{namespace}:{kind}/{path}"
            if _drawn_in_code(archive, info.filename, parents):
                code_drawn.append(identifier)
                continue
            issue = model_issue(archive, members, info.filename, set(namespaces), documents)
            if issue:
                skipped.setdefault(issue, []).append(identifier)
                continue
            display = language.get(f"{kind}.{namespace}.{path.replace('/', '.')}") or _humanize(
                path.rsplit("/", 1)[-1])
            json_models.append(ModAsset(
                key=f"{kind}:{identifier}",
                kind=kind,
                name=display,
                identifier=identifier,
                model_path=info.filename,
            ))
        json_models.sort(key=lambda asset: (asset.kind != "block", asset.name.casefold()))
        assets.extend(json_models)
        _disambiguate(json_models)
        display_name = _display_name(archive) or record.get("name")
    return ModCatalog(
        path=os.path.abspath(jar_path),
        name=str(display_name or os.path.splitext(os.path.basename(jar_path))[0]),
        mod_id=str(mod_namespace),
        namespaces=namespaces,
        assets=assets,
        unsupported=unsupported,
        code_drawn=code_drawn,
        skipped=skipped,
    )


_RESOURCE_LOCATION_DESC = "Lnet/minecraft/resources/ResourceLocation;"
_MATERIAL_DESC = "Lnet/minecraft/client/resources/model/Material;"


def _texture_constants(class_path, members):
    """``(field name, texture)`` for the static texture constants a mod declares
    (``ResourceLocation``s of textures, and ``Material`` sprites), in declaration
    order. Only classes that declare two or more are read: texture registries."""
    from .java_vm import JavaThrow, VMError
    from .mod_models import new_model_vm, resource_location_text

    owners = []
    for name in class_path.names():
        parsed = class_path.get(name)
        if parsed is None:
            continue
        fields = [field for field in parsed.fields
                  if field.is_static and field.desc in (_RESOURCE_LOCATION_DESC, _MATERIAL_DESC)]
        if len(fields) >= 2:
            owners.append((name, fields))
    if not owners:
        return []
    vm = new_model_vm(class_path, max_steps=400_000)
    vm.lenient_nulls = True  # set-up code: a library call answering null must not stop it
    found = []
    for name, fields in owners:
        vm.lenient_depth = vm.depth
        try:
            vm.initialize(name)
        except (JavaThrow, VMError):
            continue
        for field in fields:
            value = vm.statics.get(name, {}).get(field.name)
            location = resource_location_text(value)
            if not location and isinstance(value, JObject) and isinstance(value.native, dict):
                sprite = resource_location_text(value.native.get("texture"))  # a Material
                if sprite:
                    namespace, path = sprite.split(":", 1)
                    location = f"{namespace}:textures/{path}.png"
            # Any texture the mod names counts, item sprites too: Supplementaries'
            # party hat model wears textures/item/party_hat.png.
            if location and location.endswith(".png") and _member(location) in members:
                found.append((name, field.name, location))
    return found


def _read_constants(class_path, class_name, constants):
    """Texture constants the class's own code reads (``getstatic``), in code order."""
    parsed = class_path.get(class_name)
    if parsed is None or not constants:
        return []
    by_field = {(owner, field_name): location for owner, field_name, location in constants}
    found = []
    for method in parsed.methods:
        code = parsed.code(method)
        if code is None:
            continue
        try:
            instructions = code.instructions
        except ValueError:
            continue
        for pc in sorted(instructions):
            op, operands, _next = instructions[pc]
            if op != "getstatic":
                continue
            try:
                owner, field_name, _desc = parsed.member_ref(operands[0])
            except Exception:
                continue
            location = by_field.get((owner, field_name))
            if location and location not in found:
                found.append(location)
    return found


def _vanilla_mob_assets(class_path, renderers, language, mod_namespace, entity_textures, unsupported,
                        archive, invisible):
    """``(asset, factory key)`` for each renderer whose model bakes a registered
    LayerDefinition; the rest go to ``unsupported`` (projectiles, item renderers).
    An invisible mob (see ``_see_through``) goes to ``invisible`` with no asset."""
    from .java_vm import JavaThrow, VMError
    from .mod_models import ModModelError, drawn_model, new_model_vm, run_renderer, vanilla_model_library
    from .mod_vanilla_models import baked_root_of

    found = []
    vm = new_model_vm(class_path, library=vanilla_model_library, max_steps=2_000_000) if renderers else None
    for name in renderers:
        try:
            result = run_renderer(vm, name)
            # The mob wears the model its draw picks; the others it holds stay models.
            result.model = drawn_model(vm, result.renderer, result.model, result.entity)
            _root, layer, factory = baked_root_of(vm, result.model)
        except (ModModelError, JavaThrow, VMError):
            layer = factory = None
        if layer is None:
            unsupported.append(name)
            continue
        entity_class = renderer_entity_class(class_path, name)
        entity_id = _snake((entity_class or name).rsplit("/", 1)[-1])
        display = (language.get(f"entity.{mod_namespace}.{entity_id}")
                   or language.get(f"entity.{mod_namespace}.{re.sub(r'_entity$', '', entity_id)}")
                   or _humanize(re.sub(r"(Entity)?Render(er)?$", "", name.rsplit("/", 1)[-1])))
        wanted = (_tokens((entity_class or name).rsplit("/", 1)[-1])
                  | _tokens(result.model.class_name.rsplit("/", 1)[-1])) - _NAME_NOISE
        factory_key = ""
        if factory is not None and factory.handle[0] == 6:  # a static method reference
            factory_key = f"{factory.handle[1]}.{factory.handle[2]}{factory.handle[3]}"
        own = result.texture if result.texture else ""
        native = result.renderer.native if isinstance(getattr(result.renderer, "native", None), dict) else {}
        if own and _see_through(archive, own) and not native.get("layers"):
            invisible.append(f"{mod_namespace}:{re.sub(r'_entity$', '', entity_id)}")
            found.append((None, factory_key))
            continue
        named = [value for value in _texture_strings(class_path, name) if value != own]
        ranked = _by_shared_tokens(wanted, [value for value in entity_textures if value != own])
        textures = ([own] if own else []) + [value for value in ranked if value not in named][:12]
        found.append((ModAsset(
            key=f"entity:{name}",
            kind="entity",
            name=display,
            identifier=f"{mod_namespace}:{re.sub(r'_entity$', '', entity_id)}",
            renderer_class=name,
            model_class=result.model.class_name,
            textures=textures,
        ), factory_key))
    return found


def _geo_has_cubes(archive, member):
    """Whether a geo file draws anything (some mods ship an empty placeholder)."""
    try:
        data = json.loads(archive.read(member).decode("utf-8-sig"))
    except (KeyError, UnicodeError, ValueError):
        return False
    geometries = data.get("minecraft:geometry") if isinstance(data, dict) else None
    if not isinstance(geometries, list):
        geometries = [value for key, value in (data or {}).items()
                      if str(key).startswith("geometry.")] if isinstance(data, dict) else []
    return any(isinstance(bone, dict) and (bone.get("cubes") or bone.get("poly_mesh"))
               for geometry in geometries if isinstance(geometry, dict)
               for bone in geometry.get("bones") or ())


def _member(location):
    """``ns:path`` -> ``assets/ns/path``."""
    namespace, _separator, path = str(location or "").partition(":")
    return f"assets/{namespace}/{path}" if path else ""


def _location(member):
    """``assets/ns/path`` -> ``ns:path``."""
    match = re.match(r"^assets/([^/]+)/(.+)$", member)
    return f"{match.group(1)}:{match.group(2)}" if match else ""


# Textures drawn over a skin, never the skin itself.
_OVERLAY_TEXTURE = re.compile(r"_(glow|glowmask|glowing|eyes?|emissive|overlay|e|lights?)$")


def _stem(location):
    return location.split(":", 1)[-1].rsplit("/", 1)[-1].rsplit(".", 1)[0]


def _by_shared_tokens(wanted, candidates, tokens=_tokens):
    """Candidates sharing the most of ``wanted``'s name tokens (the file name, then
    its folders), skins before overlays, the closest name first."""
    scored = []
    for value in candidates:
        stem = _stem(value)
        overlap = len(wanted & tokens(stem))
        folders = len(wanted & tokens(value.split(":", 1)[-1].rsplit("/", 1)[0]))
        if overlap or folders:
            scored.append((bool(_OVERLAY_TEXTURE.search(stem)), -overlap, -folders,
                           ":textures/entity/" not in value, len(tokens(stem) - wanted),
                           len(value), value))
    return [entry[-1] for entry in sorted(scored)]


def _matching_animation(members, geo_member):
    """The animation file for a geo file: same name, else the most name tokens shared."""
    from .mod_geckolib import geo_stem

    namespace = geo_member.split("/")[1]
    candidates = sorted(name for name in members
                        if name.startswith(f"assets/{namespace}/animations/") and name.endswith(".json"))
    found = _by_shared_tokens(_tokens(geo_stem(geo_member)) - _NAME_NOISE,
                              [name.replace(".animation.json", ".json") for name in candidates])
    if not found:
        return ""
    best = found[0]
    return next(name for name in candidates if name.replace(".animation.json", ".json") == best)


def _see_through(archive, location):
    """Whether a skin in the JAR has no visible texel: a mob drawn only with it
    shows nothing in the game (a mob's invisible state)."""
    from .preview_fallback import png_data_has_visible_pixels

    try:
        payload = archive.read(_member(location))
    except (KeyError, OSError, ValueError, zipfile.BadZipFile, zlib.error):
        return False
    return not png_data_has_visible_pixels(payload)


def _geckolib_assets(class_path, members, geo_files, language, mod_namespace, jar_textures,
                     archive, invisible):
    """GeckoLib mobs and models.

    Each renderer is run for a default entity: its code names the geo,
    texture and animation files. A renderer whose geo file depends on the
    entity (a profession, a biome variant) still names its texture and
    animations, which then go with the geo files that share their name. Geo
    files no renderer reaches are listed with files matched by name. A
    renderer that draws only see-through textures goes to ``invisible``.
    """
    from .java_vm import JavaThrow, VMError
    from .mod_geckolib import geo_stem, is_geckolib_entity_renderer, is_geckolib_renderer, run_geo_renderer
    from .mod_models import ModModelError, new_model_vm

    def present(location):
        return bool(location) and (_member(location) in members or location.startswith("minecraft:"))

    def skins(wanted, own=""):
        found = _by_shared_tokens(wanted, [value for value in jar_textures if value != own])
        return ([own] if own else []) + found[:12]

    def entity_name(entity_class, stem):
        candidates = []
        if entity_class:
            simple = entity_class.rsplit("/", 1)[-1]
            candidates += [_snake(simple), _snake(re.sub(r"Entity$", "", simple))]
        candidates.append(stem)
        for entity_id in candidates:
            name = language.get(f"entity.{mod_namespace}.{entity_id}")
            if name:
                return entity_id, name
        return candidates[0], stem.replace("_", " ").title()

    assets = []
    used = set()
    hidden = {}  # geo file -> the invisible mob drawn with it
    partial = []  # (renderer, result): texture/animation known, geo depends on the entity
    # Entity renderers first: an item or armor renderer of the same geo file adds nothing.
    renderers = sorted((name for name in class_path.names() if is_geckolib_renderer(class_path, name)),
                       key=lambda name: not is_geckolib_entity_renderer(class_path, name))
    vm = new_model_vm(class_path, max_steps=2_000_000) if renderers else None
    for name in renderers:
        try:
            result = run_geo_renderer(vm, name, layers=False)
        except (ModModelError, JavaThrow, VMError):
            continue
        geo = _member(result.model_path)
        if geo not in members:
            if present(result.texture):
                partial.append((name, result))
            continue
        if geo in used:
            continue
        entity = is_geckolib_entity_renderer(class_path, name)
        stem = geo_stem(geo)
        entity_id, display = entity_name(renderer_entity_class(class_path, name) if entity else "", stem)
        own = result.texture if present(result.texture) else ""
        if own and _see_through(archive, own) and not _geo_layers_draw(vm, name):
            hidden.setdefault(geo, f"{mod_namespace}:{entity_id}")
            continue
        used.add(geo)
        animation = _member(result.animation_path)
        assets.append(ModAsset(
            key=f"geo:{name}",
            kind="entity" if entity else "model",
            name=display,
            identifier=f"{mod_namespace}:{entity_id}",
            renderer_class=name,
            model_path=geo,
            textures=skins((_tokens(stem) | _tokens(_stem(own))) - _NAME_NOISE, own),
            animation_path=animation if animation in members else _matching_animation(members, geo),
        ))
    # A geo file only an invisible mob draws is not listed under another name.
    invisible.extend(identifier for geo, identifier in hidden.items() if geo not in used)
    for geo in geo_files:
        if geo in used or geo in hidden:
            continue
        stem = geo_stem(geo)
        wanted = _tokens(stem) - _NAME_NOISE
        renderer, result = next(((name, result) for name, result in partial
                                 if wanted & _tokens(_stem(result.texture))), ("", None))
        textures = skins(wanted, result.texture if result is not None else "")
        if not textures:
            # A name written as one word still finds its skin (thespawn: the_spawn_texture).
            textures = _by_shared_tokens(_compound_tokens(stem) - _NAME_NOISE, jar_textures,
                                         _compound_tokens)[:12]
        animation = _member(result.animation_path) if result is not None else ""
        # A geo file whose skin is an entity texture (or that sits in geo/entity) is a mob.
        mob = bool(renderer) or "/geo/entity/" in geo or bool(
            textures and ":textures/entity/" in textures[0])
        assets.append(ModAsset(
            key=f"geo:{geo}",
            kind="entity" if mob else "model",
            name=stem.replace("_", " ").title(),
            identifier=f"{geo.split('/')[1]}:{stem}",
            renderer_class=renderer,
            model_path=geo,
            textures=textures,
            animation_path=animation if animation in members else _matching_animation(members, geo),
        ))
    return assets


def _geo_layers_draw(vm, renderer):
    """Whether a GeckoLib renderer's layers draw anything (glowing eyes on an
    invisible body), for a default or an eager entity."""
    from .java_vm import JavaThrow, VMError
    from .mod_geckolib import run_geo_renderer
    from .mod_models import ModModelError

    try:
        result = run_geo_renderer(vm, renderer)
    except (ModModelError, JavaThrow, VMError):
        return True
    return bool(result.layers or result.optional_layers)


def _drawn_in_code(archive, member, parents, depth=0):
    """Whether a model's parent chain (inside the JAR) ends at ``builtin/entity``.

    Those items have no geometry in JSON; the game renders them in code.
    """
    if depth > 16:
        return False
    if member not in parents:
        try:
            data = json.loads(archive.read(member).decode("utf-8-sig"))
        except (KeyError, UnicodeError, ValueError):
            data = {}
        parent = data.get("parent", "") if isinstance(data, dict) else ""
        parents[member] = parent if isinstance(parent, str) else ""
    parent = parents[member]
    if not parent:
        return False
    namespace, _, path = parent.partition(":") if ":" in parent else ("minecraft", "", parent)
    if path.startswith("builtin/entity"):
        return True
    if path.startswith("builtin/"):
        return False
    return _drawn_in_code(archive, f"assets/{namespace}/models/{path}.json", parents, depth + 1)


_STANDARD_LOADERS = ("separate_transforms", "separate-perspective")


def _document(archive, member, documents):
    if member not in documents:
        try:
            data = json.loads(archive.read(member).decode("utf-8-sig"))
        except (KeyError, UnicodeError, ValueError):
            data = None
        documents[member] = data if isinstance(data, dict) else None
    return documents[member]


def _reference(value, own):
    """``(namespace, path)`` of a model or texture reference."""
    value = value.get("sprite", "") if isinstance(value, dict) else str(value or "")
    if ":" in value:
        namespace, path = value.split(":", 1)
        return namespace, path
    return "minecraft", value


def model_issue(archive, members, member, own_namespaces, documents=None):
    """Why a block/item model would not render from this JAR plus vanilla, or ``""``.

    ``"custom loader"``: the mod builds the geometry in code.
    ``"needs another mod"``: a compat model using another mod's parent or textures.
    ``"generated at runtime"``: its parent or texture is not in the JAR (mods
    such as Supplementaries generate them while the game runs).
    ``"template"``: a base model whose texture slots its children fill.
    Vanilla (``minecraft:``) parents and textures count as present.
    """
    documents = {} if documents is None else documents
    textures = {}
    elements = None
    vanilla_parent = False
    current = member
    for _ in range(24):
        data = _document(archive, current, documents)
        if data is None:
            return "generated at runtime"
        loader = str(data.get("loader") or "")
        if loader:
            if loader.endswith(_STANDARD_LOADERS) and isinstance(data.get("base"), dict):
                data = data["base"]
            else:
                return "custom loader"
        for key, value in (data.get("textures") or {}).items() if isinstance(data.get("textures"), dict) else ():
            textures.setdefault(key, value)
        if elements is None and isinstance(data.get("elements"), list):
            elements = data["elements"]
        parent = str(data.get("parent") or "")
        if not parent or parent.startswith(("builtin/", "minecraft:builtin/")):
            break
        namespace, path = _reference(parent, own_namespaces)
        candidates = [f"assets/{namespace}/models/{path}.json"]
        if ":" not in parent:  # the resolver falls back to the model's own namespace
            candidates += [f"assets/{own}/models/{path}.json" for own in sorted(own_namespaces)]
        found = next((candidate for candidate in candidates if candidate in members), "")
        if found:
            current = found
            continue
        if namespace == "minecraft":
            vanilla_parent = True
            break
        return "generated at runtime" if namespace in own_namespaces else "needs another mod"
    if elements:
        used = {face.get("texture") for element in elements if isinstance(element, dict)
                for face in (element.get("faces") or {}).values()
                if isinstance(face, dict) and face.get("texture")}
    elif vanilla_parent:
        used = {"#" + key for key in textures if key != "particle"}
    else:
        used = {"#" + key for key in textures if key.startswith("layer")}
    for reference in used:
        seen = set()
        value = reference
        while isinstance(value, str) and value.startswith("#"):
            key = value[1:]
            if key in seen or key not in textures:
                value = None
                break
            seen.add(key)
            value = textures[key]
            value = value.get("sprite", "") if isinstance(value, dict) else value
        if value is None:
            # A slot nothing fills: a template for child models (vanilla
            # parents such as block/cube only alias their slots, never fill them).
            return "template"
        namespace, path = _reference(value, own_namespaces)
        if namespace == "minecraft":
            continue
        if namespace not in own_namespaces:
            return "needs another mod"
        if f"assets/{namespace}/textures/{path}.png" not in members:
            return "generated at runtime"
    return ""


# Never a model's skin: item sprites, GUI, particles, fonts and the like.
_NOT_MODEL_TEXTURES = re.compile(
    r"^textures/(item|items|gui|particle|font|mob_effect|painting|environment|colormap|map|trims"
    r"|entity/banner|entity/shield)/")


_NOT_GEO_TEXTURES = re.compile(
    r"^textures/(gui|particle|font|mob_effect|painting|environment|colormap|map|trims)/")


PART_OF_ANOTHER_MOB = "part of another mob"
INVISIBLE = "invisible"
_ENTITY = "net/minecraft/world/entity/Entity"
_ENTITY_TYPE = "net/minecraft/world/entity/EntityType"
# The entity class in a type field's generic signature: RegistryObject<EntityType<X>>,
# DeferredHolder<EntityType<?>, EntityType<X>> or EntityType<X>.
_TYPE_OF = re.compile(r"L" + _ENTITY_TYPE + r"<L([^;<>]+);>")
# Instructions between reading a part's type field and the call that builds it.
_BUILD_WINDOW = 12


def _spawn_egg_ids(archive, language):
    """Entity ids with a spawn egg (``anaconda_spawn_egg``, ``spawn_egg_anaconda``),
    from the mod's item models and names."""
    names = [entry.rsplit("/", 1)[-1][:-5] for entry in archive.namelist()
             if entry.endswith(".json") and "spawn_egg" in entry.rsplit("/", 1)[-1].lower()]
    names += [key.rsplit(".", 1)[-1] for key in language if "spawn_egg" in key.lower()]
    return {name.lower().replace("spawn_egg", "").strip("_") for name in names}


def _type_field_class(class_path, owner, field_name, cache):
    """The entity class a static ``EntityType`` field holds, from its generic signature."""
    key = (owner, field_name)
    if key not in cache:
        cache[key] = ""
        parsed = class_path.get(owner)
        member = next((item for item in parsed.fields if item.name == field_name), None) if parsed else None
        raw = member.attributes.get("Signature") if member is not None else None
        if raw and len(raw) >= 2:
            try:
                match = _TYPE_OF.search(parsed.utf8(struct.unpack(">H", raw[:2])[0]))
            except ClassFormatError:
                match = None
            cache[key] = match.group(1) if match else ""
    return cache[key]


def _entities_made_by(class_path, owner, cache):
    """Entity classes ``owner``'s code (and its own ancestors') makes: ``new X(...)``,
    or an entity built from X's type field (``CENTIPEDE_TAIL.get()`` handed to a
    constructor, or to an ``EntityType`` method that returns an entity)."""
    made = set()
    for name in class_path.superclasses(owner):
        parsed = class_path.get(name)
        for method in (parsed.methods if parsed is not None else ()):
            code = parsed.code(method)
            if code is None:
                continue
            read = []  # [entity class, instructions left] for recently read type fields
            for pc in sorted(code.instructions):
                op, operands, _next = code.instructions[pc]
                try:
                    if op == "new":
                        made.add(parsed.class_name(operands[0]))
                    elif op == "getstatic":
                        holder, field_name, _desc = parsed.member_ref(operands[0])
                        entity = _type_field_class(class_path, holder, field_name, cache)
                        if entity:
                            read.append([entity, _BUILD_WINDOW])
                    elif read and op in ("invokespecial", "invokevirtual", "invokeinterface", "invokestatic"):
                        holder, member, desc = parsed.member_ref(operands[0])
                        if ((member == "<init>" and desc.startswith(f"(L{_ENTITY_TYPE};"))
                                or (holder == _ENTITY_TYPE and desc.endswith(f")L{_ENTITY};"))):
                            made.update(entity for entity, _left in read)
                            read = []
                except ClassFormatError:
                    continue
                for entry in read:
                    entry[1] -= 1
                read = [entry for entry in read if entry[1] > 0]
    return made


def _body_parts(class_path, archive, language, assets):
    """Keys of mob entries that are pieces of another mob (an anaconda's body
    segments, a centipede's tail, a murmur's head).

    The game strings such a mob together in code: the mob makes its pieces
    itself, each a living entity the player cannot spawn alone. So a piece is
    drawn like a living thing (a ``LivingEntityRenderer``), has no spawn egg,
    and another of the mod's mobs makes it (``_entities_made_by``). Projectiles
    a mob throws are not living; mobs a mob turns into or summons have eggs.
    """
    by_class = {}
    for asset in assets:
        if asset.kind == "entity" and asset.renderer_class:
            entity = renderer_entity_class(class_path, asset.renderer_class)
            if entity:
                by_class.setdefault(entity, []).append(asset)
    cache, made_by_others = {}, set()
    for owner in by_class:
        ancestors = set(class_path.superclasses(owner))
        made_by_others.update(entity for entity in _entities_made_by(class_path, owner, cache)
                              if entity in by_class and entity not in ancestors)
    if not made_by_others:
        return set()
    eggs = _spawn_egg_ids(archive, language)
    return {asset.key for entity in made_by_others for asset in by_class[entity]
            if class_path.superclasses(asset.renderer_class)[-1] in (LIVING_RENDERER, MOB_RENDERER)
            and asset.identifier.split(":", 1)[-1].lower() not in eggs}


def _disambiguate(assets):
    """Several entities can share one in-game name (a worm's head and body).

    Only names within one kind clash: a block and its item may share a name.
    """
    counts = {}
    for asset in assets:
        key = (asset.kind, asset.name.casefold())
        counts[key] = counts.get(key, 0) + 1
    for asset in assets:
        if counts[(asset.kind, asset.name.casefold())] > 1:
            asset.name = f"{asset.name} ({asset.identifier.split(':', 1)[-1].rsplit('/', 1)[-1]})"
    # Still alike (the same file name in two folders): the whole path tells them apart.
    counts = {}
    for asset in assets:
        counts[(asset.kind, asset.name.casefold())] = counts.get((asset.kind, asset.name.casefold()), 0) + 1
    for asset in assets:
        if counts[(asset.kind, asset.name.casefold())] > 1:
            path = (asset.model_path or asset.identifier).split(":", 1)[-1]
            path = re.sub(r"^assets/[^/]+/(geo|models)/", "", path).rsplit(".", 1)[0]
            asset.name = f"{asset.name.split(' (', 1)[0]} ({path.replace('.geo', '')})"


def _display_name(archive):
    """The mod's display name from its Fabric/Quilt JSON or (Neo)Forge TOML."""
    for name in ("fabric.mod.json", "quilt.mod.json"):
        try:
            data = json.loads(archive.read(name).decode("utf-8-sig"))
        except (KeyError, UnicodeError, ValueError):
            continue
        if isinstance(data, dict):
            quilt = data.get("quilt_loader") if isinstance(data.get("quilt_loader"), dict) else {}
            value = data.get("name") or (quilt.get("metadata") or {}).get("name")
            if value:
                return str(value)
    for name in ("META-INF/mods.toml", "META-INF/neoforge.mods.toml"):
        try:
            raw = archive.read(name).decode("utf-8-sig", errors="ignore")
        except KeyError:
            continue
        match = re.search(r"""(?m)^\s*displayName\s*=\s*(?:"([^"]*)"|'([^']*)')""", raw)
        if match:
            return match.group(1) if match.group(1) is not None else match.group(2)
    return ""


_CATALOGS = {}
_DISK_CATALOGS = 64  # newest scans kept on disk


def mod_catalog(jar_path):
    """``scan_mod_jar`` cached by path, size and modification time, in memory and on disk."""
    try:
        stat = os.stat(jar_path)
    except OSError:
        return None
    key = (os.path.normcase(os.path.abspath(jar_path)), stat.st_size, stat.st_mtime_ns, CATALOG_VERSION)
    if key not in _CATALOGS:
        if len(_CATALOGS) > 8:
            _CATALOGS.clear()
        _CATALOGS[key] = _stored_scan(jar_path, key)
    return _CATALOGS[key]


def catalog_from_dict(data):
    """A ``ModCatalog`` from ``asdict(catalog)``."""
    data = dict(data)
    data["assets"] = [ModAsset(**asset) for asset in data["assets"]]
    return ModCatalog(**data)


_SCANNER = []


def _scanner_stamp():
    """The reading code's files (names, sizes, times): any change reads mods again."""
    if not _SCANNER:
        folder = os.path.dirname(os.path.abspath(__file__))
        stamp = []
        for name in sorted(os.listdir(folder)):
            if name.endswith(".py") and (name.startswith(("mod_", "java_")) or name == "intermediary_names.py"):
                stat = os.stat(os.path.join(folder, name))
                stamp.append((name, stat.st_size, stat.st_mtime_ns))
        _SCANNER.append(tuple(stamp))
    return _SCANNER[0]


def _stored_scan(jar_path, key):
    """``scan_mod_jar`` kept on disk: a big mod takes a second or two to read,
    so opening it again (in a later session too) is instant. A changed
    reader (a new add-on version) reads it again."""
    from .archive_extractor import user_cache_dir

    folder = user_cache_dir("mod_catalogs")
    digest = hashlib.sha1(repr(key + (_scanner_stamp(),)).encode("utf-8")).hexdigest()[:24]
    path = os.path.join(folder, digest + ".json")
    try:
        with open(path, encoding="utf-8") as stream:
            return catalog_from_dict(json.load(stream))
    except (OSError, ValueError, TypeError, KeyError):
        pass
    catalog = scan_mod_jar(jar_path)
    try:
        os.makedirs(folder, exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as stream:
            json.dump(asdict(catalog), stream)
        os.replace(path + ".tmp", path)
        stored = sorted((entry for entry in os.scandir(folder) if entry.name.endswith(".json")),
                        key=lambda entry: entry.stat().st_mtime, reverse=True)
        for entry in stored[_DISK_CATALOGS:]:
            os.remove(entry.path)
    except OSError:
        pass
    return catalog


def extract_mod_assets(jar_path, cache_dir):
    """Extract the JAR's resource files (cached) and return its pack root."""
    result = extract_archive_result(jar_path, cache_dir)
    if result.get("status") != "Extracted":
        raise ValueError(result.get("error") or "The mod JAR has no assets")
    container = os.path.join(result["output_path"], "resource_packs")
    for name in sorted(os.listdir(container)):
        root = os.path.join(container, name)
        if os.path.isdir(os.path.join(root, "assets")):
            return root
    raise ValueError("The mod JAR has no assets folder")


def resolve_resource_file(roots, location):
    """``namespace:path/to/file.png`` in the first root that has it."""
    namespace, _separator, path = str(location or "").partition(":")
    if not path:
        namespace, path = "minecraft", namespace
    parts = [part for part in path.replace("\\", "/").split("/") if part not in ("", ".", "..")]
    for root in roots:
        if not root:
            continue
        candidate = os.path.join(root, "assets", namespace, *parts)
        if os.path.isfile(candidate):
            return candidate
    return ""


__all__ = [
    "ModAsset", "ModCatalog", "extract_mod_assets", "mod_catalog", "rank_textures",
    "resolve_resource_file", "scan_mod_jar",
]
