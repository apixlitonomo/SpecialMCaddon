"""Safe, pack-aware source identity and virtual file-system primitives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, Mapping, Optional, Protocol, Tuple

from .diagnostics import SourceRef


class VfsError(RuntimeError):
    pass


class UnsafeVirtualPath(VfsError, ValueError):
    pass


_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_NAMESPACE = re.compile(r"^[a-z0-9_.-]+$")
_RESOURCE_PATH = re.compile(r"^[a-z0-9_./-]+$")


def normalize_virtual_path(value: object) -> str:
    """Return a canonical POSIX path, rejecting traversal and absolute paths."""

    if not isinstance(value, str):
        raise UnsafeVirtualPath("Virtual path must be a string")
    if "\x00" in value:
        raise UnsafeVirtualPath("Virtual path contains a NUL byte")
    raw = value.replace("\\", "/")
    if raw.startswith("/") or _DRIVE_PREFIX.match(raw):
        raise UnsafeVirtualPath("Absolute virtual paths are not allowed: %r" % value)
    parts = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise UnsafeVirtualPath("Path traversal is not allowed: %r" % value)
        parts.append(part)
    if not parts:
        raise UnsafeVirtualPath("Virtual path is empty")
    return "/".join(parts)


@dataclass(frozen=True, order=True)
class ResourceLocation:
    """A Bedrock resource identifier, retaining namespace semantics."""

    namespace: str
    path: str

    @classmethod
    def parse(
        cls, value: str, default_namespace: str = "minecraft"
    ) -> "ResourceLocation":
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Resource identifier must be a non-empty string")
        value = value.strip().lower()
        if ":" in value:
            namespace, resource_path = value.split(":", 1)
        else:
            namespace, resource_path = default_namespace, value
        if not _NAMESPACE.fullmatch(namespace):
            raise ValueError("Invalid resource namespace: %r" % namespace)
        if (
            not resource_path
            or resource_path.startswith("/")
            or ".." in resource_path.split("/")
            or not _RESOURCE_PATH.fullmatch(resource_path)
        ):
            raise ValueError("Invalid resource path: %r" % resource_path)
        return cls(namespace, resource_path)

    def __str__(self) -> str:
        return "%s:%s" % (self.namespace, self.path)


@dataclass(frozen=True)
class SourceIdentity:
    """Collision-resistant identity for a resource inside a provider."""

    provider_id: str
    virtual_path: str
    content_digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "virtual_path", normalize_virtual_path(self.virtual_path)
        )
        if not self.provider_id:
            raise ValueError("provider_id must not be empty")

    @property
    def stable_key(self) -> str:
        encoded = json.dumps(
            [self.provider_id, self.virtual_path, self.content_digest],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def uri(self) -> str:
        return "particle-vfs://%s/%s" % (self.provider_id, self.virtual_path)

    def source_ref(self, pointer: str = "") -> SourceRef:
        return SourceRef(self.uri, pointer)


@dataclass(frozen=True)
class SafeArchiveIdentity:
    """Identity for an exact member in an exact archive.

    The archive content digest prevents two same-named downloads from aliasing.
    The member path is normalized before it can enter a cache key.  ``layers``
    represents nested archives without using unsafe ``!`` string parsing.
    """

    archive_digest: str
    member_path: str
    layers: Tuple[str, ...] = ()
    member_crc32: Optional[int] = None
    member_size: Optional[int] = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.archive_digest):
            raise ValueError("archive_digest must be a lowercase SHA-256 digest")
        object.__setattr__(
            self, "member_path", normalize_virtual_path(self.member_path)
        )
        object.__setattr__(
            self,
            "layers",
            tuple(normalize_virtual_path(layer) for layer in self.layers),
        )

    @classmethod
    def from_bytes(
        cls,
        archive_bytes: bytes,
        member_path: str,
        layers: Iterable[str] = (),
        member_crc32: Optional[int] = None,
        member_size: Optional[int] = None,
    ) -> "SafeArchiveIdentity":
        return cls(
            hashlib.sha256(archive_bytes).hexdigest(),
            member_path,
            tuple(layers),
            member_crc32,
            member_size,
        )

    @classmethod
    def from_file(
        cls,
        archive_path: os.PathLike[str] | str,
        member_path: str,
        layers: Iterable[str] = (),
        member_crc32: Optional[int] = None,
        member_size: Optional[int] = None,
    ) -> "SafeArchiveIdentity":
        digest = hashlib.sha256()
        with open(archive_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return cls(
            digest.hexdigest(),
            member_path,
            tuple(layers),
            member_crc32,
            member_size,
        )

    @property
    def stable_key(self) -> str:
        encoded = json.dumps(
            [
                self.archive_digest,
                list(self.layers),
                self.member_path,
                self.member_crc32,
                self.member_size,
            ],
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def uri(self) -> str:
        chain = "/".join(self.layers + (self.member_path,))
        return "particle-archive://%s/%s" % (self.archive_digest[:24], chain)


class VirtualFileSystem(Protocol):
    provider_id: str

    def exists(self, virtual_path: str) -> bool:
        ...

    def read_bytes(self, virtual_path: str) -> bytes:
        ...

    def identity(self, virtual_path: str) -> SourceIdentity:
        ...

    def iter_paths(self, prefix: str = "") -> Iterator[str]:
        ...


class MemoryVFS:
    """Small immutable VFS useful for tests and in-memory pack adapters."""

    def __init__(
        self, files: Mapping[str, bytes | str], provider_id: str = "memory"
    ) -> None:
        normalized: Dict[str, bytes] = {}
        for path, payload in files.items():
            key = normalize_virtual_path(path)
            if key in normalized:
                raise VfsError("Duplicate normalized path: %s" % key)
            normalized[key] = (
                payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
            )
        self._files = normalized
        self.provider_id = provider_id

    def exists(self, virtual_path: str) -> bool:
        return normalize_virtual_path(virtual_path) in self._files

    def read_bytes(self, virtual_path: str) -> bytes:
        key = normalize_virtual_path(virtual_path)
        try:
            return self._files[key]
        except KeyError as exc:
            raise FileNotFoundError(key) from exc

    def identity(self, virtual_path: str) -> SourceIdentity:
        key = normalize_virtual_path(virtual_path)
        payload = self.read_bytes(key)
        return SourceIdentity(
            self.provider_id, key, hashlib.sha256(payload).hexdigest()
        )

    def iter_paths(self, prefix: str = "") -> Iterator[str]:
        if prefix:
            prefix = normalize_virtual_path(prefix).rstrip("/") + "/"
        for path in sorted(self._files):
            if not prefix or path.startswith(prefix):
                yield path


class DirectoryVFS:
    """Read-only directory adapter that contains symlink resolution."""

    def __init__(
        self, root: os.PathLike[str] | str, provider_id: Optional[str] = None
    ) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise VfsError("Directory VFS root does not exist: %s" % self.root)
        self.provider_id = provider_id or (
            "dir-" + hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:24]
        )

    def _resolve(self, virtual_path: str) -> Tuple[str, Path]:
        key = normalize_virtual_path(virtual_path)
        target = (self.root / Path(*key.split("/"))).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise UnsafeVirtualPath(
                "Resolved path escapes VFS root: %s" % key
            ) from exc
        return key, target

    def exists(self, virtual_path: str) -> bool:
        _, target = self._resolve(virtual_path)
        return target.is_file()

    def read_bytes(self, virtual_path: str) -> bytes:
        _, target = self._resolve(virtual_path)
        return target.read_bytes()

    def identity(self, virtual_path: str) -> SourceIdentity:
        key, target = self._resolve(virtual_path)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        return SourceIdentity(self.provider_id, key, digest)

    def iter_paths(self, prefix: str = "") -> Iterator[str]:
        prefix_value = normalize_virtual_path(prefix) if prefix else ""
        base = self._resolve(prefix_value)[1] if prefix_value else self.root
        if not base.exists():
            return
        for target in sorted(item for item in base.rglob("*") if item.is_file()):
            resolved = target.resolve()
            try:
                relative = resolved.relative_to(self.root)
            except ValueError:
                continue
            yield relative.as_posix()


class ArchiveVFS:
    """Read-only ZIP VFS with traversal, ambiguity and zip-bomb limits."""

    def __init__(
        self,
        archive_path: os.PathLike[str] | str,
        *,
        max_members: int = 100_000,
        max_member_size: int = 256 * 1024 * 1024,
        max_total_size: int = 2 * 1024 * 1024 * 1024,
        max_compression_ratio: float = 1000.0,
    ) -> None:
        self.archive_path = Path(archive_path).resolve()
        digest = hashlib.sha256()
        with self.archive_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        self.archive_digest = digest.hexdigest()
        self.provider_id = "archive-" + self.archive_digest[:24]
        self._members: Dict[str, zipfile.ZipInfo] = {}
        total_size = 0
        with zipfile.ZipFile(self.archive_path, "r") as archive:
            infos = archive.infolist()
            if len(infos) > max_members:
                raise VfsError("Archive exceeds member budget")
            for info in infos:
                if info.is_dir():
                    continue
                key = normalize_virtual_path(info.filename)
                if key in self._members:
                    raise VfsError("Duplicate normalized archive member: %s" % key)
                mode = (info.external_attr >> 16) & 0xFFFF
                if mode and stat.S_ISLNK(mode):
                    raise VfsError("Archive symlink is not allowed: %s" % key)
                if info.file_size > max_member_size:
                    raise VfsError("Archive member exceeds size budget: %s" % key)
                total_size += info.file_size
                if total_size > max_total_size:
                    raise VfsError("Archive exceeds total uncompressed size budget")
                if info.file_size:
                    ratio = info.file_size / max(info.compress_size, 1)
                    if ratio > max_compression_ratio:
                        raise VfsError(
                            "Archive member exceeds compression ratio budget: %s" % key
                        )
                self._members[key] = info

    def exists(self, virtual_path: str) -> bool:
        return normalize_virtual_path(virtual_path) in self._members

    def read_bytes(self, virtual_path: str) -> bytes:
        key = normalize_virtual_path(virtual_path)
        info = self._members.get(key)
        if info is None:
            raise FileNotFoundError(key)
        with zipfile.ZipFile(self.archive_path, "r") as archive:
            return archive.read(info)

    def archive_identity(self, virtual_path: str) -> SafeArchiveIdentity:
        key = normalize_virtual_path(virtual_path)
        info = self._members.get(key)
        if info is None:
            raise FileNotFoundError(key)
        return SafeArchiveIdentity(
            self.archive_digest,
            key,
            member_crc32=info.CRC,
            member_size=info.file_size,
        )

    def identity(self, virtual_path: str) -> SourceIdentity:
        archive_identity = self.archive_identity(virtual_path)
        payload = self.read_bytes(virtual_path)
        return SourceIdentity(
            self.provider_id,
            archive_identity.member_path,
            hashlib.sha256(payload).hexdigest(),
        )

    def iter_paths(self, prefix: str = "") -> Iterator[str]:
        if prefix:
            prefix = normalize_virtual_path(prefix).rstrip("/") + "/"
        for path in sorted(self._members):
            if not prefix or path.startswith(prefix):
                yield path


class OverlayVFS:
    """Ordered pack stack; the first provider has highest precedence."""

    def __init__(
        self, providers: Iterable[VirtualFileSystem], provider_id: str = "overlay"
    ) -> None:
        self.providers = tuple(providers)
        self.provider_id = provider_id

    def _provider_for(self, virtual_path: str) -> VirtualFileSystem:
        key = normalize_virtual_path(virtual_path)
        for provider in self.providers:
            if provider.exists(key):
                return provider
        raise FileNotFoundError(key)

    def exists(self, virtual_path: str) -> bool:
        try:
            self._provider_for(virtual_path)
        except FileNotFoundError:
            return False
        return True

    def read_bytes(self, virtual_path: str) -> bytes:
        provider = self._provider_for(virtual_path)
        return provider.read_bytes(virtual_path)

    def identity(self, virtual_path: str) -> SourceIdentity:
        return self._provider_for(virtual_path).identity(virtual_path)

    def iter_paths(self, prefix: str = "") -> Iterator[str]:
        seen = set()
        for provider in self.providers:
            for path in provider.iter_paths(prefix):
                if path not in seen:
                    seen.add(path)
                    yield path
