"""Offline Mojang MoLang function/query metadata registry.

The JSON snapshot is from Mojang's ``bedrock-samples`` repository at commit
921fafb05c93abeae56c2f2868cd8f942bdbcc0f (v1.26.30.5, 2026-06-16).
It is data only and is never fetched at add-on runtime.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Dict, Iterable, Optional, Tuple


SNAPSHOT_COMMIT = "921fafb05c93abeae56c2f2868cd8f942bdbcc0f"
SNAPSHOT_VERSION = "1.26.30.5"
SNAPSHOT_PATH = Path(__file__).with_name("data") / "mojang-molang-queries.json"


def version_tuple(value) -> Tuple[int, int, int]:
    if value is None:
        return (1, 26, 30)
    parts = value.split(".") if isinstance(value, str) else tuple(value)
    result = []
    for part in parts[:3]:
        match = re.match(r"\d+", str(part))
        result.append(int(match.group()) if match else 0)
    return tuple((result + [0, 0, 0])[:3])


@dataclass(frozen=True)
class VersionRange:
    first: Tuple[int, int, int]
    last: Optional[Tuple[int, int, int]]
    query_sets: Tuple[str, ...]

    def contains(self, version, query_set=None) -> bool:
        resolved = version_tuple(version)
        if resolved < self.first or (self.last is not None and resolved > self.last):
            return False
        if query_set is None or not self.query_sets:
            return True
        return str(query_set) in self.query_sets


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    min_args: int
    max_args: Optional[int]
    return_type: str = "float"
    description: str = ""
    version_ranges: Tuple[VersionRange, ...] = ()

    def is_available(self, version=None, query_set=None) -> bool:
        return not self.version_ranges or any(
            item.contains(version, query_set) for item in self.version_ranges
        )

    def accepts(self, argument_count: int) -> bool:
        return self.min_args <= int(argument_count) and (self.max_args is None or int(argument_count) <= self.max_args)


class MetadataError(ValueError):
    pass


class MoLangMetadataRegistry:
    def __init__(self, math_functions: Iterable[FunctionSpec], queries: Iterable[FunctionSpec]):
        self.math_functions: Dict[str, FunctionSpec] = {
            item.name.casefold(): item for item in math_functions
        }
        self.queries: Dict[str, FunctionSpec] = {
            item.name.casefold(): item for item in queries
        }

    @classmethod
    def load(cls, path=SNAPSHOT_PATH):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))

        def convert(item):
            ranges = []
            for version_range in item.get("version_ranges", ()):
                last = version_range.get("last_version")
                ranges.append(
                    VersionRange(
                        version_tuple(version_range.get("first_version", "0.0.0")),
                        version_tuple(last) if last else None,
                        tuple(str(value) for value in version_range.get("query_sets", ())),
                    )
                )
            minimum = int(item.get("min_args", 0))
            maximum = int(item["max_args"]) if "max_args" in item else None
            return FunctionSpec(
                str(item["name"]).casefold(),
                minimum,
                maximum,
                str(item.get("return_type", "float")),
                str(item.get("description", "")),
                tuple(ranges),
            )

        return cls(
            (convert(item) for item in payload.get("math_functions", ())),
            (convert(item) for item in payload.get("queries", ())),
        )

    def lookup(self, name, *, version=None, query_set=None):
        normalized = str(name).casefold()
        if normalized.startswith("q."):
            normalized = "query." + normalized[2:]
        table = self.math_functions if normalized.startswith("math.") else self.queries
        spec = table.get(normalized)
        if spec is None or not spec.is_available(version, query_set):
            return None
        return spec

    def definition(self, name):
        """Return a definition without applying version/query-set filtering."""
        normalized = str(name).casefold()
        if normalized.startswith("q."):
            normalized = "query." + normalized[2:]
        table = self.math_functions if normalized.startswith("math.") else self.queries
        return table.get(normalized)

    def validate_call(self, name, argument_count, *, version=None, query_set=None):
        spec = self.lookup(name, version=version, query_set=query_set)
        if spec is None:
            raise MetadataError("{} is unavailable for engine {}".format(name, version_tuple(version)))
        if not spec.accepts(argument_count):
            maximum = "unbounded" if spec.max_args is None else str(spec.max_args)
            raise MetadataError(
                "{} expects {}-{} arguments, got {}".format(
                    spec.name, spec.min_args, maximum, argument_count
                )
            )
        return spec


DEFAULT_MOLANG_METADATA = MoLangMetadataRegistry.load()


__all__ = [
    "DEFAULT_MOLANG_METADATA",
    "FunctionSpec",
    "MetadataError",
    "MoLangMetadataRegistry",
    "SNAPSHOT_COMMIT",
    "SNAPSHOT_PATH",
    "SNAPSHOT_VERSION",
    "VersionRange",
    "version_tuple",
]
