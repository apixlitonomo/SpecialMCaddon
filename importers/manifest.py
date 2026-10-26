from dataclasses import dataclass, field
from typing import List, Optional, Any, Dict

@dataclass
class ImportedAsset:
    """
    Normalized representation of a Minecraft asset (Java or Bedrock).
    """
    id: str
    name: str
    source_format: str # 'java_resource_pack', 'bedrock_resource_pack', 'bnrarchive', 'loose'
    asset_type: str    # 'model', 'texture', 'particle', 'animation', 'cutscene', 'entity', 'item', 'block'
    json_name: Optional[str] = None
    identifier: Optional[str] = None
    source_path: str = ""
    dependencies: List[str] = field(default_factory=list)
    raw_json: Optional[Dict[str, Any]] = None

@dataclass
class ImportManifest:
    """
    Contains a list of all detected assets across the entire pack.
    """
    assets: List[ImportedAsset] = field(default_factory=list)
    
    def add_asset(self, asset: ImportedAsset):
        self.assets.append(asset)
        
    def get_assets_by_type(self, asset_type: str) -> List[ImportedAsset]:
        return [a for a in self.assets if a.asset_type == asset_type]
