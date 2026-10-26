import os
import json
from .manifest import ImportManifest, ImportedAsset

class BedrockPackReader:
    def __init__(self, pack_dir: str):
        self.pack_dir = pack_dir
        self.manifest = ImportManifest()
        
    def parse(self) -> ImportManifest:
        # Scan for models/entity
        self._scan_folder("models/entity", "model")
        # Scan for particles
        self._scan_folder("particles", "particle")
        # Scan for animations
        self._scan_folder("animations", "animation")
        # Scan for animation controllers
        self._scan_folder("animation_controllers", "animation_controller")
        # Scan for entities
        self._scan_folder("entity", "entity")
        
        return self.manifest
        
    def _scan_folder(self, folder_name: str, asset_type: str):
        target_dir = os.path.join(self.pack_dir, folder_name)
        if not os.path.exists(target_dir):
            return
            
        for root, _, files in os.walk(target_dir):
            for file in files:
                if file.endswith('.json'):
                    filepath = os.path.join(root, file)
                    rel_path = os.path.relpath(filepath, self.pack_dir)
                    
                    # Basic JSON identifier extraction could happen here
                    asset = ImportedAsset(
                        id=f"bedrock_{asset_type}_{file}",
                        name=file,
                        source_format="bedrock_resource_pack",
                        asset_type=asset_type,
                        json_name=file,
                        source_path=filepath
                    )
                    self.manifest.add_asset(asset)
