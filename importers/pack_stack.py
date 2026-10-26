"""Deterministic, provenance-backed resource-pack stack resolution.

Earlier layers have higher priority. Logical identifiers are case-sensitive and
namespaces are preserved; the resolver never falls back to a basename or a
display name.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Dict, Iterable, Optional, Sequence, Tuple


DUPLICATE_ID = "duplicate_logical_id"
CASE_COLLISION = "case_collision"
REVERSE_OWNER_AMBIGUITY = "reverse_owner_ambiguity"
NOT_FOUND = "not_found"
IMAGE_EXTENSION_PRIORITY = {".tga": 0, ".png": 1, ".jpg": 2, ".jpeg": 3}
_SKIP_DIRS = {"__MACOSX", ".git", ".svn", "__pycache__", "node_modules"}


@dataclass(frozen=True)
class PackLayer:
    pack_root: str
    priority: int
    label: str = ""
    # Top-level folders to read (casefolded names); empty reads the whole pack.
    # A loose file's folder is no pack: only what its references can name.
    folders: frozenset = frozenset()


@dataclass(frozen=True)
class SourceProvenance:
    path: str
    pack_root: str
    priority: int
    json_pointer: str = ""


@dataclass(frozen=True)
class ResolverIssue:
    code: str
    resource_kind: str
    logical_id: str
    message: str
    sources: Tuple[SourceProvenance, ...] = ()


@dataclass(frozen=True)
class ResourceRecord:
    resource_kind: str
    logical_id: str
    path: str
    pack_root: str
    priority: int
    json_pointer: str = ""
    extension_rank: int = 99
    target_id: str = ""

    @property
    def provenance(self):
        return SourceProvenance(
            self.path, self.pack_root, self.priority, self.json_pointer
        )


@dataclass(frozen=True)
class ResolvedResource:
    resource_kind: str
    logical_id: str
    path: str
    pack_root: str
    priority: int
    source_pointer: str
    definition_pointer: str = ""

    def as_tuple(self):
        """Return the contract tuple: path, pack root, priority, JSON pointer."""
        return (self.path, self.pack_root, self.priority, self.source_pointer)


@dataclass(frozen=True)
class ResolutionResult:
    resource: Optional[ResolvedResource] = None
    issues: Tuple[ResolverIssue, ...] = ()

    @property
    def ok(self):
        return self.resource is not None and not self.issues

    def as_tuple(self):
        return self.resource.as_tuple() if self.resource else None


@dataclass(frozen=True)
class OwnerRecord:
    owner_id: str
    resource_kind: str
    logical_id: str
    source: SourceProvenance


@dataclass(frozen=True)
class OwnerResolutionResult:
    owner_id: str = ""
    source: Optional[SourceProvenance] = None
    issues: Tuple[ResolverIssue, ...] = ()

    @property
    def ok(self):
        return bool(self.owner_id) and not self.issues


@dataclass(frozen=True)
class OwnerMapResult:
    mapping: Dict[str, str]
    issues: Tuple[ResolverIssue, ...] = ()


def canonical_logical_id(value):
    """Normalize separators only; preserve namespace, spelling, and case."""
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def _geometry_compatibility_key(value):
    """Match punctuation-only Bedrock geometry spelling variants."""
    logical = canonical_logical_id(value)
    if ":" in logical and not logical.startswith(("geometry.", "geo.")):
        logical = logical.split(":", 1)[1]
    logical = re.sub(r"^(?:geometry|geo)[./]", "", logical)
    return re.sub(r"[^A-Za-z0-9]", "", logical)


def _json_pointer_part(value):
    return str(value).replace("~", "~0").replace("/", "~1")
def _geometry_file_rank(identifier, filepath, modern=False):
    if modern:
        return 0
    logical = canonical_logical_id(identifier)
    if logical.casefold().startswith("geometry."):
        logical = logical[len("geometry."):]
    filename = os.path.basename(filepath)
    stem = os.path.splitext(filename)[0]
    if stem.casefold().endswith(".geo"):
        stem = stem[:-4]
    normalize = lambda value: re.sub(r"[^a-z0-9]", "", value.casefold())
    return 1 if normalize(logical) == normalize(stem) else 2




def _load_json(filepath):
    with open(filepath, "r", encoding="utf-8-sig") as handle:
        content = handle.read()
    content = re.sub(r"//.*", "", content)
    content = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
    value = json.loads(content)
    return value if isinstance(value, dict) else {}


def _extract_texture_target(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("path"), str):
            return value["path"]
        variations = value.get("variations")
        if isinstance(variations, list) and variations:
            return _extract_texture_target(variations[0])
    if isinstance(value, list) and value:
        return _extract_texture_target(value[0])
    return ""


def _logical_file_id(relative_path, strip_extension=False):
    logical = canonical_logical_id(relative_path)
    parts = PurePosixPath(logical).parts
    if len(parts) >= 3 and parts[0].casefold() == "assets":
        logical = f"{parts[1]}:{'/'.join(parts[2:])}"
    if strip_extension:
        logical = os.path.splitext(logical)[0].replace("\\", "/")
    return logical


class ResourcePackStack:
    """Index and resolve an ordered set of resource-pack roots."""

    def __init__(self, layers: Sequence[PackLayer]):
        normalized = []
        seen = set()
        for layer in layers:
            root = os.path.abspath(os.fspath(layer.pack_root))
            key = os.path.normcase(root)
            if key in seen or not os.path.isdir(root):
                continue
            seen.add(key)
            normalized.append(PackLayer(root, int(layer.priority), layer.label, frozenset(layer.folders)))
        self.layers = tuple(normalized)
        self._records: Dict[str, Dict[str, list]] = {}
        self._owners = []
        self._issues = []
        self._issue_signatures = set()
        self._issues_by_key = {}
        for layer in self.layers:
            self._index_layer(layer)
        self._finalize_issues()

    @classmethod
    def from_roots(cls, roots: Iterable[str]):
        roots = [os.path.abspath(os.fspath(root)) for root in roots if root]
        count = len(roots)
        return cls(
            [PackLayer(root, count - index, os.path.basename(root)) for index, root in enumerate(roots)]
        )

    @property
    def issues(self):
        return tuple(self._issues)

    def _add(self, record):
        logical_id = canonical_logical_id(record.logical_id)
        if not logical_id:
            return
        normalized = ResourceRecord(
            record.resource_kind,
            logical_id,
            os.path.abspath(record.path),
            record.pack_root,
            record.priority,
            record.json_pointer,
            record.extension_rank,
            canonical_logical_id(record.target_id),
        )
        self._records.setdefault(record.resource_kind, {}).setdefault(logical_id, []).append(normalized)

    def _add_json_resources(self, filepath, layer, data):
        geometries = data.get("minecraft:geometry")
        if isinstance(geometries, list):
            for index, geometry in enumerate(geometries):
                description = geometry.get("description", {}) if isinstance(geometry, dict) else {}
                identifier = description.get("identifier", "") if isinstance(description, dict) else ""
                if identifier:
                    self._add(ResourceRecord(
                        "geometry", identifier, filepath, layer.pack_root, layer.priority,
                        f"/minecraft:geometry/{index}/description/identifier",
                        _geometry_file_rank(identifier, filepath, modern=True),
                    ))
        for identifier in data:
            if str(identifier).startswith("geometry."):
                child = str(identifier).partition(":")[0]
                self._add(ResourceRecord(
                    "geometry", child, filepath, layer.pack_root, layer.priority,
                    f"/{_json_pointer_part(identifier)}",
                    _geometry_file_rank(child, filepath),
                ))

        animations = data.get("animations")
        if isinstance(animations, dict):
            for identifier in animations:
                self._add(ResourceRecord(
                    "animation", identifier, filepath, layer.pack_root, layer.priority,
                    f"/animations/{_json_pointer_part(identifier)}",
                ))
        controllers = data.get("animation_controllers")
        if isinstance(controllers, dict):
            for identifier in controllers:
                self._add(ResourceRecord(
                    "controller", identifier, filepath, layer.pack_root, layer.priority,
                    f"/animation_controllers/{_json_pointer_part(identifier)}",
                ))

        basename = os.path.basename(filepath).casefold()
        if basename in {"terrain_texture.json", "item_texture.json"}:
            texture_data = data.get("texture_data", {})
            if isinstance(texture_data, dict):
                for alias, entry in texture_data.items():
                    target = _extract_texture_target(entry.get("textures")) if isinstance(entry, dict) else ""
                    if target:
                        self._add(ResourceRecord(
                            "texture_alias", alias, filepath, layer.pack_root, layer.priority,
                            f"/texture_data/{_json_pointer_part(alias)}/textures",
                            target_id=target,
                        ))

        if basename == "blocks.json":
            for block_id, definition in data.items():
                textures = definition.get("textures") if isinstance(definition, dict) else None
                faces = {"*": textures} if isinstance(textures, str) else textures
                if not isinstance(faces, dict):
                    continue
                for face, target in faces.items():
                    if isinstance(target, str) and target:
                        self._add(ResourceRecord(
                            "block_texture", str(block_id) + "/" + str(face),
                            filepath, layer.pack_root, layer.priority,
                            f"/{_json_pointer_part(block_id)}/textures",
                            target_id=target,
                        ))

        entity = data.get("minecraft:client_entity")
        entity_key = "minecraft:client_entity"
        if not isinstance(entity, dict):
            entity = data.get("minecraft:attachable")
            entity_key = "minecraft:attachable"
        if isinstance(entity, dict):
            description = entity.get("description", {})
            if isinstance(description, dict):
                owner_id = canonical_logical_id(description.get("identifier", ""))
                if owner_id:
                    owner_pointer = f"/{entity_key}/description/identifier"
                    self._add(ResourceRecord(
                        "client_entity", owner_id, filepath, layer.pack_root,
                        layer.priority, owner_pointer,
                    ))
                    for field, kind in (("geometry", "geometry"), ("textures", "texture")):
                        references = description.get(field, {})
                        if isinstance(references, str):
                            references = {"default": references}
                        if not isinstance(references, dict):
                            continue
                        for alias, logical_id in references.items():
                            if not isinstance(logical_id, str):
                                continue
                            pointer = (
                                f"/{entity_key}/description/{field}/"
                                f"{_json_pointer_part(alias)}"
                            )
                            self._owners.append(OwnerRecord(
                                owner_id, kind, canonical_logical_id(logical_id),
                                SourceProvenance(
                                    os.path.abspath(filepath), layer.pack_root,
                                    layer.priority, pointer,
                                ),
                            ))

    def _index_layer(self, layer):
        for current, dirs, files in os.walk(layer.pack_root):
            dirs[:] = sorted(
                [name for name in dirs if name not in _SKIP_DIRS and not name.startswith(".")
                 and (not layer.folders or current != layer.pack_root or name.casefold() in layer.folders)],
                key=str.casefold,
            )
            for filename in sorted(files, key=str.casefold):
                filepath = os.path.join(current, filename)
                extension = os.path.splitext(filename)[1].casefold()
                relative = os.path.relpath(filepath, layer.pack_root).replace("\\", "/")
                if extension in IMAGE_EXTENSION_PRIORITY:
                    full_id = _logical_file_id(relative)
                    stem_id = _logical_file_id(relative, strip_extension=True)
                    rank = IMAGE_EXTENSION_PRIORITY[extension]
                    for logical_id in (full_id, stem_id):
                        self._add(ResourceRecord(
                            "texture", logical_id, filepath, layer.pack_root,
                            layer.priority, extension_rank=rank,
                        ))
                elif extension == ".json":
                    try:
                        data = _load_json(filepath)
                    except (OSError, UnicodeError, json.JSONDecodeError):
                        continue
                    self._add_json_resources(filepath, layer, data)

    def _register_issue(self, issue, logical_ids):
        signature = (
            issue.code, issue.resource_kind, issue.logical_id,
            tuple((source.path, source.priority, source.json_pointer) for source in issue.sources),
        )
        # Looked up, not compared with every earlier issue: a folder holding
        # many packs has thousands, and that made indexing take minutes.
        if signature in self._issue_signatures:
            return
        self._issue_signatures.add(signature)
        self._issues.append(issue)
        for logical_id in logical_ids:
            self._issues_by_key.setdefault((issue.resource_kind, logical_id), []).append(issue)

    def _finalize_issues(self):
        for kind, by_id in self._records.items():
            folded = {}
            for logical_id in by_id:
                folded.setdefault(logical_id.casefold(), []).append(logical_id)
            for identifiers in folded.values():
                identifiers = sorted(set(identifiers))
                if len(identifiers) < 2:
                    continue
                sources = tuple(
                    record.provenance
                    for logical_id in identifiers
                    for record in by_id[logical_id]
                )
                issue = ResolverIssue(
                    CASE_COLLISION, kind, " | ".join(identifiers),
                    f"Case-colliding {kind} identifiers: {', '.join(identifiers)}",
                    sources,
                )
                self._register_issue(issue, identifiers)

            for logical_id, records in by_id.items():
                by_priority = {}
                for record in records:
                    by_priority.setdefault(record.priority, []).append(record)
                for priority, candidates in by_priority.items():
                    groups = {}
                    for record in candidates:
                        rank = record.extension_rank if kind in {"texture", "geometry"} else 0
                        groups.setdefault(rank, []).append(record)
                    best_rank = min(groups)
                    duplicates = groups[best_rank]
                    if len(duplicates) < 2:
                        continue
                    issue = ResolverIssue(
                        DUPLICATE_ID, kind, logical_id,
                        f"Equal-priority duplicate {kind} identifier {logical_id!r} at priority {priority}",
                        tuple(record.provenance for record in duplicates),
                    )
                    self._register_issue(issue, (logical_id,))

    def _select(self, kind, logical_id):
        logical_id = canonical_logical_id(logical_id)
        issues = tuple(self._issues_by_key.get((kind, logical_id), ()))
        if issues:
            return None, issues
        records = self._records.get(kind, {}).get(logical_id, ())
        if not records:
            available = self._records.get(kind, {})
            matches = [key for key in available if key.casefold() == logical_id.casefold()]
            detail = f"; exact case available: {matches[0]!r}" if len(matches) == 1 else ""
            issue = ResolverIssue(
                NOT_FOUND, kind, logical_id,
                f"Exact {kind} identifier {logical_id!r} was not found{detail}",
            )
            return None, (issue,)
        priority = max(record.priority for record in records)
        candidates = [record for record in records if record.priority == priority]
        if kind in {"texture", "geometry"}:
            rank = min(record.extension_rank for record in candidates)
            candidates = [record for record in candidates if record.extension_rank == rank]
        if len(candidates) != 1:
            issue = ResolverIssue(
                DUPLICATE_ID, kind, logical_id,
                f"Equal-priority duplicate {kind} identifier {logical_id!r}",
                tuple(record.provenance for record in candidates),
            )
            return None, (issue,)
        return candidates[0], ()

    def resolve(self, resource_kind, logical_id, source_pointer=""):
        record, issues = self._select(resource_kind, logical_id)
        if not record:
            return ResolutionResult(issues=issues)
        pointer = source_pointer or record.json_pointer
        return ResolutionResult(resource=ResolvedResource(
            resource_kind, canonical_logical_id(logical_id), record.path,
            record.pack_root, record.priority, pointer, record.json_pointer,
        ))

    def resolve_geometry_compatible(self, logical_id, source_pointer=""):
        """Resolve exact geometry first, then one unique spelling equivalent."""
        exact = self.resolve("geometry", logical_id, source_pointer)
        if exact.ok:
            return exact
        if not exact.issues or not all(
            issue.code == NOT_FOUND for issue in exact.issues
        ):
            return exact
        target = _geometry_compatibility_key(logical_id)
        if not target:
            return exact
        matches = [
            candidate
            for candidate in self._records.get("geometry", {})
            if _geometry_compatibility_key(candidate) == target
        ]
        if len(matches) != 1:
            return exact
        return self.resolve("geometry", matches[0], source_pointer)


    def resolve_texture(self, logical_id, source_pointer="", _seen=()):
        logical_id = canonical_logical_id(logical_id)
        if logical_id in _seen:
            issue = ResolverIssue(
                DUPLICATE_ID, "texture_alias", logical_id,
                f"Texture alias cycle encountered at {logical_id!r}",
            )
            return ResolutionResult(issues=(issue,))
        if "/" not in logical_id and not os.path.splitext(logical_id)[1]:
            alias, alias_issues = self._select("texture_alias", logical_id)
            if alias:
                return self.resolve_texture(
                    alias.target_id,
                    source_pointer or alias.json_pointer,
                    _seen + (logical_id,),
                )
            if alias_issues and not all(issue.code == NOT_FOUND for issue in alias_issues):
                return ResolutionResult(issues=alias_issues)
        return self.resolve("texture", logical_id, source_pointer)

    def resolve_block_texture(self, block_id, face="*"):
        """Resolve a declared block face through the shared terrain atlas."""
        for candidate in dict.fromkeys((str(block_id) + "/" + face, str(block_id) + "/*")):
            record, issues = self._select("block_texture", canonical_logical_id(candidate))
            if record:
                return self.resolve_texture(record.target_id, record.json_pointer)
            if issues and not all(issue.code == NOT_FOUND for issue in issues):
                return ResolutionResult(issues=issues)
        return ResolutionResult(issues=issues)

    def owner_for(self, resource_kind, logical_id):
        logical_id = canonical_logical_id(logical_id)
        references = [
            item for item in self._owners
            if item.resource_kind == resource_kind and item.logical_id == logical_id
        ]
        effective = []
        for owner_id in sorted({item.owner_id for item in references}):
            owned = [item for item in references if item.owner_id == owner_id]
            priority = max(item.source.priority for item in owned)
            effective.extend(item for item in owned if item.source.priority == priority)
        owners = sorted({item.owner_id for item in effective})
        if not owners:
            return OwnerResolutionResult(issues=(ResolverIssue(
                NOT_FOUND, f"{resource_kind}_owner", logical_id,
                f"No owner declares exact {resource_kind} identifier {logical_id!r}",
            ),))
        if len(owners) != 1 or len(effective) != 1:
            issue = ResolverIssue(
                REVERSE_OWNER_AMBIGUITY, f"{resource_kind}_owner", logical_id,
                f"{resource_kind.title()} identifier {logical_id!r} has ambiguous owners: {', '.join(owners)}",
                tuple(item.source for item in effective),
            )
            return OwnerResolutionResult(issues=(issue,))
        return OwnerResolutionResult(owners[0], effective[0].source)

    def unique_owner_map(self, resource_kind):
        logical_ids = sorted({
            item.logical_id for item in self._owners
            if item.resource_kind == resource_kind
        })
        mapping = {}
        issues = []
        for logical_id in logical_ids:
            result = self.owner_for(resource_kind, logical_id)
            if result.ok:
                mapping[logical_id] = result.owner_id
            else:
                issues.extend(result.issues)
        return OwnerMapResult(mapping, tuple(issues))
