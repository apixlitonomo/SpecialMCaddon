"""Exact loose-texture fallback from installed Bedrock game resources."""
import os
import re
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=8)
def texture_roots(configured=''):
    candidates=[]
    explicit=Path(configured) if configured else None
    if explicit and explicit.is_dir() and (explicit/'textures').is_dir():
        candidates.append(explicit)
    # Scope discovery to the standard installed game's resource directory.
    parent=Path(os.environ.get('SystemDrive','C:') + '/XboxGames/Minecraft for Windows/Content/data/resource_packs')
    if parent.is_dir():
        versions=[p for p in parent.iterdir() if p.is_dir() and re.fullmatch(r'vanilla_\d+(?:\.\d+)*',p.name)]
        versions.sort(key=lambda p:tuple(int(v) for v in p.name[8:].split('.')),reverse=True)
        candidates.extend(versions)
        candidates.extend(p for p in (parent/'vanilla',parent/'vanilla_base') if p.is_dir())
    return tuple(dict.fromkeys(str(p.resolve()) for p in candidates))


def find_texture(logical_id, configured=''):
    logical_id=logical_id.replace('\\','/')
    if logical_id.startswith('minecraft:'):
        logical_id=logical_id[len('minecraft:'):]
    if not logical_id.startswith('textures/') or any(part in ('..','.') for part in logical_id.split('/')):
        return None
    for root in texture_roots(configured):
        for extension in ('.tga','.png','.jpg','.jpeg',''):
            path=os.path.join(root,logical_id+extension)
            if os.path.isfile(path):
                return path,root
    return None
