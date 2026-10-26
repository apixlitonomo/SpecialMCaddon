"""Normalized, renderer-independent particle document IR."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Optional, Sequence, Tuple

from .diagnostics import Diagnostic, DiagnosticBag, Severity, SourceRef
from .registry import (
    DEFAULT_COMPONENT_REGISTRY,
    ComponentSpec,
    Registry,
)
from .source import ResourceLocation


class FrozenDict(Mapping):
    """Compact immutable mapping used by normalized documents."""

    __slots__ = ("_items", "_lookup", "_hash")

    def __init__(self, items: Iterable[Tuple[str, Any]] = ()) -> None:
        pairs = tuple(items)
        lookup = {}
        for key, value in pairs:
            if not isinstance(key, str):
                raise TypeError("FrozenDict keys must be strings")
            if key in lookup:
                raise ValueError("Duplicate key: %s" % key)
            lookup[key] = value
        self._items = pairs
        self._lookup = lookup
        self._hash = None

    def __getitem__(self, key: str) -> Any:
        return self._lookup[key]

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __hash__(self) -> int:
        value = self._hash
        if value is None:
            value = hash(self._items)
            self._hash = value
        return value

    def __repr__(self) -> str:
        return "FrozenDict(%r)" % (dict(self._items),)


JsonScalar = type(None) | bool | int | float | str
FrozenJson = JsonScalar | Tuple["FrozenJson", ...] | FrozenDict


def freeze_json(value: Any) -> FrozenJson:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return FrozenDict((str(key), freeze_json(item)) for key, item in value.items())
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return tuple(freeze_json(item) for item in value)
    raise TypeError("Value is not JSON-compatible: %r" % (value,))


def thaw_json(value: FrozenJson) -> Any:
    if isinstance(value, FrozenDict):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class RenderParametersIR:
    material: str
    texture: str


@dataclass(frozen=True)
class ParticleDescriptionIR:
    identifier: str
    render: RenderParametersIR

    @property
    def resource_location(self) -> Optional[ResourceLocation]:
        try:
            return ResourceLocation.parse(self.identifier)
        except ValueError:
            return None


@dataclass(frozen=True)
class CurveIR:
    name: str
    curve_type: str
    input_expression: FrozenJson
    horizontal_range: FrozenJson
    nodes: FrozenJson
    source: SourceRef


@dataclass(frozen=True)
class ComponentIR:
    identifier: str
    payload: FrozenJson
    spec: Optional[ComponentSpec]
    source: SourceRef

    @property
    def known(self) -> bool:
        return self.spec is not None


@dataclass(frozen=True)
class EventIR:
    name: str
    node: FrozenJson
    source: SourceRef


@dataclass(frozen=True)
class ParticleDocumentIR:
    source: SourceRef
    format_version: str
    description: ParticleDescriptionIR
    curves: Tuple[CurveIR, ...]
    events: Tuple[EventIR, ...]
    components: Tuple[ComponentIR, ...]
    extras: FrozenDict = FrozenDict()

    def component(self, identifier: str) -> Optional[ComponentIR]:
        return next(
            (item for item in self.components if item.identifier == identifier),
            None,
        )

    def curve(self, name: str) -> Optional[CurveIR]:
        return next((item for item in self.curves if item.name == name), None)

    def event(self, name: str) -> Optional[EventIR]:
        return next((item for item in self.events if item.name == name), None)

    @property
    def unknown_components(self) -> Tuple[str, ...]:
        return tuple(item.identifier for item in self.components if not item.known)


@dataclass(frozen=True)
class NormalizationResult:
    document: Optional[ParticleDocumentIR]
    diagnostics: Tuple[Diagnostic, ...]

    @property
    def ok(self) -> bool:
        return self.document is not None and not any(
            item.severity >= Severity.ERROR for item in self.diagnostics
        )


def _as_mapping(
    value: Any,
    bag: DiagnosticBag,
    source: SourceRef,
    code: str,
    label: str,
    *,
    required: bool = False,
) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    severity = Severity.ERROR if required else Severity.WARNING
    bag.add(code, "%s must be an object" % label, severity, source)
    return {}


def _strip_json_comments(text: str) -> str:
    """Remove JSONC comments while preserving offsets, lines, and strings."""
    output = list(text)
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            index += 1
            continue
        if char == "/" and index + 1 < len(text):
            marker = text[index + 1]
            if marker == "/":
                output[index] = output[index + 1] = " "
                index += 2
                while index < len(text) and text[index] not in "\r\n":
                    output[index] = " "
                    index += 1
                continue
            if marker == "*":
                output[index] = output[index + 1] = " "
                index += 2
                while index < len(text):
                    if index + 1 < len(text) and text[index:index + 2] == "*/":
                        output[index] = output[index + 1] = " "
                        index += 2
                        break
                    if text[index] not in "\r\n":
                        output[index] = " "
                    index += 1
                continue
        index += 1
    return "".join(output)


def parse_particle_json(
    payload: bytes | str,
    source: SourceRef = SourceRef(),
    *,
    component_registry: Registry[ComponentSpec] = DEFAULT_COMPONENT_REGISTRY,
) -> NormalizationResult:
    bag = DiagnosticBag()
    try:
        text = payload.decode("utf-8-sig") if isinstance(payload, bytes) else payload
        raw = json.loads(_strip_json_comments(text))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        line = getattr(exc, "lineno", None)
        column = getattr(exc, "colno", None)
        bag.add(
            "particle.json.invalid",
            str(exc),
            Severity.FATAL,
            SourceRef(source.uri, source.pointer, line, column),
        )
        return NormalizationResult(None, bag.snapshot())
    return normalize_particle_document(
        raw, source, component_registry=component_registry, diagnostics=bag
    )


def normalize_particle_document(
    raw: Any,
    source: SourceRef = SourceRef(),
    *,
    component_registry: Registry[ComponentSpec] = DEFAULT_COMPONENT_REGISTRY,
    diagnostics: Optional[DiagnosticBag] = None,
) -> NormalizationResult:
    """Validate the document envelope and preserve every component/event payload."""

    bag = diagnostics or DiagnosticBag()
    if not isinstance(raw, Mapping):
        bag.add(
            "particle.document.type",
            "Particle document root must be an object",
            Severity.FATAL,
            source,
        )
        return NormalizationResult(None, bag.snapshot())

    raw_version = raw.get("format_version", "")
    if isinstance(raw_version, (str, int, float)) and not isinstance(
        raw_version, bool
    ):
        format_version = str(raw_version)
    else:
        format_version = ""
        bag.add(
            "particle.format_version.invalid",
            "format_version must be a string or number",
            Severity.ERROR,
            source.child("format_version"),
        )
    if not format_version:
        bag.add(
            "particle.format_version.missing",
            "format_version is required",
            Severity.ERROR,
            source.child("format_version"),
        )

    effect_source = source.child("particle_effect")
    effect = _as_mapping(
        raw.get("particle_effect"),
        bag,
        effect_source,
        "particle.effect.missing",
        "particle_effect",
        required=True,
    )
    description_source = effect_source.child("description")
    description = _as_mapping(
        effect.get("description"),
        bag,
        description_source,
        "particle.description.missing",
        "particle_effect.description",
        required=True,
    )
    identifier = description.get("identifier")
    if not isinstance(identifier, str) or not identifier.strip():
        bag.add(
            "particle.identifier.invalid",
            "description.identifier must be a non-empty string",
            Severity.ERROR,
            description_source.child("identifier"),
        )
        identifier = ""
    else:
        identifier = identifier.strip()
        try:
            ResourceLocation.parse(identifier)
        except ValueError as exc:
            bag.add(
                "particle.identifier.noncanonical",
                str(exc),
                Severity.WARNING,
                description_source.child("identifier"),
            )

    render_source = description_source.child("basic_render_parameters")
    render = _as_mapping(
        description.get("basic_render_parameters"),
        bag,
        render_source,
        "particle.render_parameters.missing",
        "description.basic_render_parameters",
        required=True,
    )
    material = render.get("material")
    texture = render.get("texture")
    if not isinstance(material, str) or not material:
        bag.add(
            "particle.material.invalid",
            "basic_render_parameters.material must be a non-empty string",
            Severity.ERROR,
            render_source.child("material"),
        )
        material = ""
    if not isinstance(texture, str) or not texture:
        bag.add(
            "particle.texture.invalid",
            "basic_render_parameters.texture must be a non-empty string",
            Severity.ERROR,
            render_source.child("texture"),
        )
        texture = ""

    curves_source = effect_source.child("curves")
    curves_raw = effect.get("curves", {})
    curves_map = _as_mapping(
        curves_raw,
        bag,
        curves_source,
        "particle.curves.type",
        "particle_effect.curves",
    )
    curves = []
    valid_curve_types = {"linear", "catmull_rom", "bezier", "bezier_chain"}
    for name, definition in curves_map.items():
        item_source = curves_source.child(name)
        if not isinstance(name, str):
            bag.add(
                "particle.curve.name",
                "Curve names must be strings",
                Severity.ERROR,
                item_source,
            )
            continue
        definition_map = _as_mapping(
            definition,
            bag,
            item_source,
            "particle.curve.type",
            "curve %s" % name,
            required=True,
        )
        curve_type = definition_map.get("type", "linear")
        if curve_type not in valid_curve_types:
            bag.add(
                "particle.curve.unsupported_type",
                "Unknown curve type %r; payload was preserved" % curve_type,
                Severity.WARNING,
                item_source.child("type"),
            )
            curve_type = str(curve_type)
        nodes = definition_map.get("nodes", ())
        expects_mapping = curve_type == "bezier_chain"
        if expects_mapping and not isinstance(nodes, Mapping):
            bag.add(
                "particle.curve.nodes",
                "bezier_chain nodes must be an object keyed by input",
                Severity.ERROR,
                item_source.child("nodes"),
            )
        elif not expects_mapping and not isinstance(nodes, Sequence):
            bag.add(
                "particle.curve.nodes",
                "%s nodes must be an array" % curve_type,
                Severity.ERROR,
                item_source.child("nodes"),
            )
        curves.append(
            CurveIR(
                name,
                curve_type,
                freeze_json(definition_map.get("input", 0.0)),
                freeze_json(definition_map.get("horizontal_range", 1.0)),
                freeze_json(nodes),
                item_source,
            )
        )

    events_source = effect_source.child("events")
    events_map = _as_mapping(
        effect.get("events", {}),
        bag,
        events_source,
        "particle.events.type",
        "particle_effect.events",
    )
    events = tuple(
        EventIR(str(name), freeze_json(node), events_source.child(name))
        for name, node in events_map.items()
    )

    components_source = effect_source.child("components")
    components_map = _as_mapping(
        effect.get("components"),
        bag,
        components_source,
        "particle.components.missing",
        "particle_effect.components",
        required=True,
    )
    components = []
    for identifier_value, payload in components_map.items():
        identifier_text = str(identifier_value)
        spec = component_registry.get(identifier_text)
        item_source = components_source.child(identifier_text)
        if spec is None:
            bag.add(
                "particle.component.unknown",
                (
                    "Unknown component %s was preserved for forward compatibility"
                    % identifier_text
                ),
                Severity.INFO,
                item_source,
            )
        components.append(
            ComponentIR(
                identifier_text,
                freeze_json(payload),
                spec,
                item_source,
            )
        )

    known_top = {"format_version", "particle_effect"}
    known_effect = {"description", "curves", "events", "components"}
    extras = {
        key: value for key, value in raw.items() if key not in known_top
    }
    effect_extras = {
        key: value for key, value in effect.items() if key not in known_effect
    }
    description_extras = {
        key: value
        for key, value in description.items()
        if key not in {"identifier", "basic_render_parameters"}
    }
    render_extras = {
        key: value
        for key, value in render.items()
        if key not in {"material", "texture"}
    }
    if render_extras:
        description_extras["basic_render_parameters"] = render_extras
    if description_extras:
        effect_extras["description"] = description_extras
    if effect_extras:
        extras["particle_effect"] = effect_extras

    document = ParticleDocumentIR(
        source=source,
        format_version=format_version,
        description=ParticleDescriptionIR(
            identifier,
            RenderParametersIR(str(material), str(texture)),
        ),
        curves=tuple(curves),
        events=events,
        components=tuple(components),
        extras=freeze_json(extras),
    )
    return NormalizationResult(document, bag.snapshot())

