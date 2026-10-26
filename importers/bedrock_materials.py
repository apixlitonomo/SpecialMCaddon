"""Bedrock material-definition loading and source-backed behavior resolution.

The engine's ``.material`` files are JSONC documents whose material keys may
declare a parent as ``child:parent``.  This module deliberately has no Blender
dependency and contains no entity/model-name rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re

from .bedrock_compatibility import material_fallback_issue


def _strip_jsonc_comments(text):
    """Remove JSONC comments without damaging comment markers in strings."""
    output = []
    index = 0
    quoted = False
    escaped = False
    while index < len(text):
        char = text[index]
        if quoted:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            index += 1
            continue
        if char == '"':
            quoted = True
            output.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < len(text):
            marker = text[index + 1]
            if marker == "/":
                index += 2
                while index < len(text) and text[index] not in "\r\n":
                    index += 1
                continue
            if marker == "*":
                index += 2
                while index + 1 < len(text) and text[index:index + 2] != "*/":
                    index += 1
                index = min(len(text), index + 2)
                continue
        output.append(char)
        index += 1
    return "".join(output)


def _strip_trailing_commas(text):
    """Remove commas immediately before a JSON object/array close token."""
    output = []
    index = 0
    quoted = False
    escaped = False
    while index < len(text):
        char = text[index]
        if quoted:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            index += 1
            continue
        if char == '"':
            quoted = True
            output.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "}]":
                index += 1
                continue
        output.append(char)
        index += 1
    return "".join(output)


def loads_material_jsonc(text):
    """Parse Bedrock JSONC, including the trailing commas used by the engine."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    return json.loads(_strip_trailing_commas(_strip_jsonc_comments(str(text))))


def normalize_material_name(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")


def _string_list(value):
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if isinstance(item, (str, int, float))]


def _merge_named_list(inherited, definition, name):
    direct = definition.get(name)
    values = _string_list(direct) if direct is not None else list(inherited)
    additions = _string_list(definition.get("+" + name))
    removals = {item.casefold() for item in _string_list(definition.get("-" + name))}
    result = []
    seen = set()
    for item in values + additions:
        key = item.casefold()
        if key in removals or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


@dataclass(frozen=True)
class MaterialDefinition:
    name: str
    parent: str
    payload: dict
    source_path: str
    source_key: str


@dataclass
class MaterialCatalog:
    """Pack-ordered material definitions with deterministic inheritance."""

    definitions: dict = field(default_factory=dict)
    issues: list = field(default_factory=list)

    def add_document(self, document, source_path=""):
        materials = document.get("materials", {}) if isinstance(document, dict) else {}
        if not isinstance(materials, dict):
            return
        for source_key, payload in materials.items():
            if str(source_key).casefold() in {"version", "format_version"}:
                continue
            if not isinstance(payload, dict):
                continue
            key_text = str(source_key)
            child, separator, encoded_parent = key_text.partition(":")
            explicit_parent = payload.get("parent", payload.get("extends", ""))
            parent = str(explicit_parent or (encoded_parent if separator else ""))
            name = normalize_material_name(child)
            if not name or name in self.definitions:
                # Search roots are already pack-priority ordered; first exact
                # definition wins, matching the resource-pack stack.
                continue
            self.definitions[name] = MaterialDefinition(
                name=name,
                parent=normalize_material_name(parent),
                payload=dict(payload),
                source_path=str(source_path or ""),
                source_key=key_text,
            )

    def resolve(self, name):
        normalized = normalize_material_name(name)
        if normalized not in self.definitions:
            return None
        return self._resolve(normalized, ())

    def _resolve(self, name, visiting):
        definition = self.definitions.get(name)
        if definition is None:
            return None
        if name in visiting:
            issue = material_fallback_issue(
                f"Material inheritance cycle includes {definition.source_key!r}",
                fallback="use the non-cyclic authored properties",
                source_location=definition.source_path or "$.materials",
            ).as_dict()
            return {
                "states": [], "defines": [], "issues": [issue],
                "provenance": [definition.source_path],
            }

        inherited = {"states": [], "defines": [], "issues": [], "provenance": []}
        if definition.parent:
            parent = self.definitions.get(definition.parent)
            if parent is None:
                issue = material_fallback_issue(
                    f"Material {definition.source_key!r} inherits unresolved parent "
                    f"{definition.parent!r}",
                    fallback="use the child material's authored properties",
                    source_location=definition.source_path or "$.materials",
                ).as_dict()
                inherited["issues"].append(issue)
            else:
                inherited = self._resolve(
                    definition.parent, tuple(visiting) + (name,)
                ) or inherited

        payload = definition.payload
        resolved = {
            key: value
            for key, value in inherited.items()
            if key not in {"states", "defines", "issues", "provenance"}
        }
        for key, value in payload.items():
            if key not in {
                "states", "+states", "-states",
                "defines", "+defines", "-defines",
                "parent", "extends",
            }:
                resolved[key] = value
        resolved["states"] = _merge_named_list(inherited.get("states", ()), payload, "states")
        resolved["defines"] = _merge_named_list(inherited.get("defines", ()), payload, "defines")
        resolved["issues"] = list(inherited.get("issues", ()))
        resolved["provenance"] = list(dict.fromkeys(
            list(inherited.get("provenance", ())) + [definition.source_path]
        ))
        resolved["source_key"] = definition.source_key
        return resolved


def _iter_material_files(root):
    if not root or not os.path.isdir(root):
        return
    candidates = []
    basename = os.path.basename(os.path.normpath(root)).casefold()
    direct = root if basename == "materials" else os.path.join(root, "materials")
    if os.path.isdir(direct):
        candidates.append(direct)
    for directory in candidates:
        for current, folders, files in os.walk(directory):
            folders.sort(key=str.casefold)
            for filename in sorted(files, key=str.casefold):
                lowered = filename.casefold()
                if lowered.endswith(".material") or lowered.endswith(".material.json"):
                    yield os.path.join(current, filename)


def load_material_catalog(search_roots):
    catalog = MaterialCatalog()
    seen = set()
    roots = list(search_roots or ())
    canonical_root = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "assets",
    )
    if canonical_root not in roots:
        # Pack-local definitions are added first and therefore win. The
        # bundled renderer registry is source-style data, not model logic.
        roots.append(canonical_root)

    for root in roots:
        for filepath in _iter_material_files(root) or ():
            key = os.path.normcase(os.path.abspath(filepath))
            if key in seen:
                continue
            seen.add(key)
            try:
                with open(filepath, "r", encoding="utf-8-sig") as handle:
                    catalog.add_document(loads_material_jsonc(handle.read()), filepath)
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                catalog.issues.append(material_fallback_issue(
                    f"Could not parse material definition {filepath!r}: {exc}",
                    fallback="continue with canonical material behavior",
                    source_location=filepath,
                ).as_dict())
    return catalog


def behavior_fields_from_definition(definition):
    """Project resolved renderer state into importer material semantics."""
    definition = definition if isinstance(definition, dict) else {}
    states = {normalize_material_name(value) for value in definition.get("states", ())}
    defines = {normalize_material_name(value) for value in definition.get("defines", ())}
    tokens = states | defines

    if tokens & {
        "blend", "blending", "alpha_blend", "alphablend", "enable_blend",
        "enableblend",
    }:
        surface_mode = "blend"
        threshold = 0.0
    elif tokens & {
        "alpha_test", "alpha_to_coverage", "alphatest", "alphatocoverage",
    }:
        surface_mode = "cutout"
        raw_threshold = definition.get("alphaTest", definition.get("alpha_test", 0.5))
        try:
            threshold = max(0.0, min(1.0, float(raw_threshold)))
        except (TypeError, ValueError):
            threshold = 0.5
    else:
        surface_mode = "opaque"
        threshold = 0.0

    no_cull = bool(tokens & {
        "disable_culling", "disableculling", "no_cull", "nocull", "two_sided",
        "twosided",
    })
    culling = "none" if no_cull else "back"
    alpha_role = (
        "tint_mask"
        if tokens & {
            "color_mask", "colormask", "colour_mask", "colourmask",
            "multi_color_tint", "multicolor_tint", "multicolortint",
        }
        else "coverage"
        if surface_mode in {"cutout", "blend"}
        else "data"
    )
    emission = "inverse_alpha" if tokens & {
        "emissive_alpha", "emissivealpha", "use_emissive_alpha",
        "useemissivealpha", "emission_from_alpha", "emissionfromalpha",
    } else "emissive" if tokens & {
        "emissive", "use_emissive", "useemissive",
    } else "none"
    lighting = "unlit" if tokens & {
        "disable_lighting", "disablelighting", "unlit", "no_lighting",
        "nolighting",
    } else "lit"
    return {
        "surface_mode": surface_mode,
        "cutout_threshold": threshold,
        "culling": culling,
        "alpha_role": alpha_role,
        "lighting": lighting,
        "emission": emission,
        "legacy_emission_mode": "inverse_alpha" if emission == "inverse_alpha" else "none",
        "provenance": tuple(value for value in definition.get("provenance", ()) if value),
        "issues": tuple(definition.get("issues", ())),
    }
