import os
import json
import re

from .utils import fast_relpath, read_ahead, walk

def strip_obfuscation(identifier):
    ident = str(identifier).split(":", 1)[-1]
    if ident.startswith("geometry."):
        ident = ident[9:]
    clean = re.sub(r'[\.\-]?\d{6,}$', '', ident)
    if not clean:
        return "unknown_asset"
    return clean

def _read_json_text(path):
    with open(path, 'r', encoding='utf-8-sig', errors='ignore') as f:
        return f.read()


class VirtualFileSystem:
    _instances = {}

    @classmethod
    def get(cls, target_dir):
        if not target_dir:
            return None
        target_dir = os.path.abspath(target_dir)
        if target_dir not in cls._instances:
            cls._instances[target_dir] = cls(target_dir)
        return cls._instances[target_dir]

    def __init__(self, target_dir):
        self.target_dir = target_dir
        self.logical_to_physical = {}
        self.physical_to_logical = {}
        
        self.basename_to_relpath = {}
        # One walk feeds every pass below; a pack used to be walked 3 times.
        self._files = []
        for root, dirs, files in walk(self.target_dir):
            for file in files:
                rel = fast_relpath(os.path.join(root, file), self.target_dir).replace("\\", "/")
                self._files.append((root, file, rel))
                self.basename_to_relpath[file] = rel
                self.basename_to_relpath[os.path.splitext(file)[0]] = rel

        self.build_map()

    def _add_mapping(self, logical_path, physical_key):
        logical_path = logical_path.replace("\\", "/")
        # physical_key could be an exact relative path or just a filename/stem
        phys_rel = self.basename_to_relpath.get(physical_key) or physical_key.replace("\\", "/")
        
        self.logical_to_physical[logical_path] = phys_rel
        self.logical_to_physical[logical_path.lower()] = phys_rel
        self.physical_to_logical[phys_rel] = logical_path
        
        log_stem = os.path.splitext(logical_path)[0]
        phys_stem = os.path.splitext(phys_rel)[0]
        self.logical_to_physical[log_stem] = phys_rel
        self.logical_to_physical[log_stem.lower()] = phys_rel
        self.physical_to_logical[phys_stem] = log_stem

    def build_map(self):
        # 1. Parse contents.json if available recursively
        content_files = [
            os.path.join(root, file) for root, file, _rel in self._files
            if file in ("contents.json", "content.json")
        ]

        for cp in content_files:
            try:
                with open(cp, 'r', encoding='utf-8-sig', errors='ignore') as f:
                    cdata = json.load(f)
                    if isinstance(cdata, list):
                        for item in cdata:
                            if isinstance(item, dict) and 'path' in item and 'key' in item:
                                self._add_mapping(item['path'], item['key'])
                    elif isinstance(cdata, dict):
                        if "content" in cdata and isinstance(cdata["content"], list):
                            for item in cdata["content"]:
                                if isinstance(item, dict) and 'path' in item and 'key' in item:
                                    self._add_mapping(item['path'], item['key'])
                        else:
                            for k, v in cdata.items():
                                if isinstance(v, str):
                                    if "/" in v or "." in v:
                                        self._add_mapping(v, k)
                                    else:
                                        self._add_mapping(k, v)
            except Exception:
                pass

        # 2. Fallback logic: read JSON files if not mapped. Only Bedrock packs
        # address assets by identifier; a Java pack (assets/ and no Bedrock
        # manifest) has none to map, so its thousands of models are skipped.
        texture_sets = {}
        java_layout = (
            os.path.isdir(os.path.join(self.target_dir, "assets"))
            and not os.path.isfile(os.path.join(self.target_dir, "manifest.json"))
        )
        candidates = [
            (root, file, rel_path) for root, file, rel_path in ([] if java_layout else self._files)
            if file.endswith(".json")
            and file not in ("manifest.json", "contents.json", "content.json")
            and rel_path not in self.physical_to_logical
            and os.path.splitext(file)[0] not in self.physical_to_logical
        ]
        texts = read_ahead(
            [os.path.join(root, file) for root, file, _rel in candidates], _read_json_text
        )
        for (root, file, rel_path), (_path, text, error) in zip(candidates, texts):
                stem = os.path.splitext(file)[0]

                # Earlier files in this pass can map this one too.
                if rel_path in self.physical_to_logical or stem in self.physical_to_logical:
                    continue

                if error is not None:
                    continue
                try:
                    data = json.loads(text)
                except Exception:
                    continue

                asset_type = None
                clean_name = None
                
                if not isinstance(data, dict):
                    continue
                    
                if "minecraft:geometry" in data:
                    geo_list = data["minecraft:geometry"]
                    if isinstance(geo_list, list) and len(geo_list) > 0:
                        first_geo = next((geo for geo in geo_list if isinstance(geo, dict)), {})
                        desc = first_geo.get("description", {})
                        desc = desc if isinstance(desc, dict) else {}
                        ident = desc.get("identifier", "")
                        clean_name = strip_obfuscation(ident)
                        asset_type = "models/entity"
                elif any(isinstance(k, str) and k.startswith("geometry.") for k in data):
                    for k in data:
                        if isinstance(k, str) and k.startswith("geometry."):
                            clean_name = strip_obfuscation(k)
                            asset_type = "models/entity"
                            break
                elif "particle_effect" in data:
                    effect = data["particle_effect"] if isinstance(data["particle_effect"], dict) else {}
                    desc = effect.get("description", {})
                    desc = desc if isinstance(desc, dict) else {}
                    ident = desc.get("identifier", "")
                    clean_name = strip_obfuscation(ident)
                    asset_type = "particles"
                elif "minecraft:client_entity" in data:
                    entity = data["minecraft:client_entity"] if isinstance(data["minecraft:client_entity"], dict) else {}
                    desc = entity.get("description", {})
                    desc = desc if isinstance(desc, dict) else {}
                    ident = desc.get("identifier", "")
                    clean_name = strip_obfuscation(ident)
                    asset_type = "entity"
                elif "minecraft:attachable" in data:
                    attachable = data["minecraft:attachable"] if isinstance(data["minecraft:attachable"], dict) else {}
                    desc = attachable.get("description", {})
                    desc = desc if isinstance(desc, dict) else {}
                    ident = desc.get("identifier", "")
                    clean_name = strip_obfuscation(ident)
                    asset_type = "attachables"
                elif "animations" in data:
                    keys = list(data["animations"].keys())
                    if keys:
                        ident = keys[0]
                        if ident.startswith("animation."):
                            ident = ident[10:]
                        clean_name = strip_obfuscation(ident)
                        asset_type = "animations"
                elif "minecraft:texture_set" in data:
                    texset = data["minecraft:texture_set"]
                    color = texset.get("color", "") if isinstance(texset, dict) else ""
                    if color:
                        clean_name = strip_obfuscation(color)
                        asset_type = "textures"
                        texture_sets[rel_path] = clean_name

                if clean_name and asset_type:
                    ext = ".json"
                    if asset_type == "models/entity": ext = ".geo.json"
                    elif asset_type == "entity": ext = ".entity.json"
                    elif asset_type == "textures": ext = ".texture_set.json"
                    
                    logical_path = f"{asset_type}/{clean_name}{ext}"
                    self._add_mapping(logical_path, rel_path)

        # Map PNGs for texture sets
        for root, file, rel_path in (self._files if texture_sets else []):
                if file.endswith(".png"):
                    if rel_path in self.physical_to_logical:
                        continue
                    
                    # See if there's a mapped texture_set for this PNG
                    json_rel = os.path.splitext(rel_path)[0] + ".json"
                    if json_rel in texture_sets:
                        clean_name = texture_sets[json_rel]
                        logical_path = f"textures/{clean_name}.png"
                        self._add_mapping(logical_path, rel_path)

    def resolve_physical(self, logical_path):
        """Returns the absolute physical path given a logical path string"""
        if not logical_path:
            return None
        logical_path = logical_path.replace("\\", "/")
        phys_rel = self.logical_to_physical.get(logical_path) or self.logical_to_physical.get(logical_path.lower())
        
        if not phys_rel:
            phys_rel = self.logical_to_physical.get(os.path.splitext(logical_path)[0].lower())
            
        if phys_rel:
            return os.path.join(self.target_dir, phys_rel)
        return os.path.join(self.target_dir, logical_path)

    def resolve_logical(self, physical_path):
        """Returns the logical path given an absolute physical path"""
        rel = fast_relpath(physical_path, self.target_dir).replace("\\", "/")
        log = self.physical_to_logical.get(rel)
        if log:
            return log
        return rel
