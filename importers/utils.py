import re
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager


_WALK_STATE = threading.local()


@contextmanager
def shared_walks():
    """Read each folder once for every ``walk`` in this block (this thread only).

    Indexing a pack walks the same tree for language files, pack roots,
    assets and archives. Inside one scan those walks reuse the first listing,
    the same snapshot they would read a moment apart.
    """
    outer = getattr(_WALK_STATE, "listings", None)
    if outer is None:
        _WALK_STATE.listings = {}
    try:
        yield
    finally:
        if outer is None:
            _WALK_STATE.listings = None


def walk(top):
    """``os.walk(top)``, reusing folder listings inside ``shared_walks()``."""
    listings = getattr(_WALK_STATE, "listings", None)
    if listings is None:
        return os.walk(top)
    return _shared_walk(os.fspath(top), listings)


def _list_folder(top):
    dirs, files, links = [], [], set()
    try:
        with os.scandir(top) as listing:
            entries = list(listing)
    except OSError:
        return None
    for entry in entries:
        try:
            is_dir = entry.is_dir()
        except OSError:
            is_dir = False
        if not is_dir:
            files.append(entry.name)
            continue
        dirs.append(entry.name)
        try:
            if entry.is_symlink():
                links.add(entry.name)
        except OSError:
            pass
    return dirs, files, links


def _shared_walk(top, listings):
    listing = listings.get(top, False)
    if listing is False:
        listing = listings[top] = _list_folder(top)
    if listing is None:
        return
    dirs, files, links = listing
    # Callers prune ``dirs`` in place, as with os.walk; keep the cache intact.
    dirs = list(dirs)
    yield top, dirs, list(files)
    for name in dirs:
        if name not in links:  # os.walk does not follow folder symlinks
            yield from _shared_walk(os.path.join(top, name), listings)


def read_ahead(paths, read, window=64, workers=8):
    """Yield ``(path, result, error)`` in order while later files read on threads.

    Opening thousands of small files is mostly waiting on the disk (OneDrive
    especially), so a bounded window of upcoming reads runs in parallel.
    """
    paths = list(paths)
    if not paths:
        return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = [pool.submit(read, path) for path in paths[:window]]
        try:
            for index, path in enumerate(paths):
                future = pending[index]
                if index + window < len(paths):
                    pending.append(pool.submit(read, paths[index + window]))
                pending[index] = None
                try:
                    yield path, future.result(), None
                except Exception as exc:
                    yield path, None, exc
        finally:
            for future in pending:
                if future is not None:
                    future.cancel()


def fast_relpath(path, root):
    """``os.path.relpath`` for paths walked from ``root``.

    On Windows ``os.path.relpath`` normalizes case for every component, which
    cost seconds when indexing a 20k-file pack. Paths produced by walking
    ``root`` already start with it, so slicing gives the same answer.
    """
    root = str(root).rstrip("\\/")
    for separator in (os.sep, "/"):
        prefix = root + separator
        if path.startswith(prefix) and ".." not in path[len(prefix):].replace("\\", "/").split("/"):
            return path[len(prefix):]
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path

def undo_mojibake(text: str) -> str:
    """Repair UTF-8 text that was decoded as Windows-1252 ("Ã©" -> "é", "Â§" -> "§").

    Real words such as "Âme" fail the round trip and stay as they are.
    """
    if not text or ("Â" not in text and "Ã" not in text):
        return text
    try:
        return text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def strip_formatting_codes(text: str) -> str:
    """Drop Minecraft "§" color and style codes from display text."""
    return re.sub(r"§.?", "", undo_mojibake(text))


def sanitize_pack_name(raw_name: str) -> str:
    if not raw_name:
        return ""
    
    name = raw_name
    
    # 2. Strips Minecraft formatting codes: remove any section sign followed by a single character
    name = re.sub(r"(?:\xa7|§).", "", name)
    
    # 3. Removes common UI glyphs that Bedrock packs use in titles
    name = re.sub(r"[\u2600-\u26FF\u2700-\u27BF\u25A0-\u25FF\uE000-\uF8FF]+", "", name)
    
    # 1. Removes trailing version numbers like "v1.2.3", "1.2.3", "V1.2.3", "version 1.2.3"
    name = re.sub(r"(?i)\s*(?:v|version|-)?\s*\d+(?:\.\d+)+\s*$", "", name)
    
    # 4. Strips leading/trailing whitespace and collapses multiple spaces.
    name = re.sub(r"\s+", " ", name).strip()
    
    # 5. Replaces any remaining characters that are not allowed in filenames with underscores.
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    
    # 6. If the resulting name is just a UUID (common in auto-generated manifests), strip it
    if re.match(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$", name):
        return ""
        
    return name
