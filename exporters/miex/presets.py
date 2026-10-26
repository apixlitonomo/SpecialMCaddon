"""Restore actual MiEx USD export metadata without importing scene geometry."""
import json

REGION_FIELDS={'name':'region_name','enableLOD':'enable_lod','lodWidth':'lod_width','lodDepth':'lod_depth',
    'lodYDetail':'lod_detail','lodBaseLevel':'lod_level','onlyIndividualBlocks':'individual','chunkSize':'chunk_size'}
for axis in 'xyz':
    for prefix in ('min','max','offset'):
        REGION_FIELDS[prefix+axis.upper()]=prefix+'_'+axis


def apply_region(settings,region):
    for key,prop in REGION_FIELDS.items():
        if key in region:
            old=getattr(settings,prop)
            value=region[key]
            if isinstance(old,bool):value=bool(value)
            if prop in {'lod_width','lod_depth'}:value=max(16,value)
            setattr(settings,prop,value)
    settings.center=False
    settings.excluded='[]'
    settings.extra_exclusions=json.dumps(region.get('excludeRegions',[]))
    settings.foreground=json.dumps(region.get('fgChunks',[]))
    # MiEx disabledChunks refer to grouped export chunks, not world chunks.
    ranges=json.loads(settings.extra_exclusions)
    for point in region.get('disabledChunks',[]):
        ix,iz=(point.get('key'),point.get('value')) if isinstance(point,dict) else point
        size=region.get('chunkSize',16)*16
        x=region['minX']//16*16+ix*size;z=region['minZ']//16*16+iz*size
        ranges.append(dict(minX=x,minZ=z,minY=region['minY'],maxX=x+size-1,maxZ=z+size-1,maxY=region['maxY']))
    settings.extra_exclusions=json.dumps(ranges)


def read_usd(path):
    from pxr import Sdf
    layer=Sdf.Layer.FindOrOpen(str(path))
    metadata=layer.customLayerData.get('MiEx') if layer else None
    if not metadata:
        raise ValueError('This USD has no MiEx export settings')
    data={'world_path':metadata.get('world',''),'dimension':metadata.get('dimension','overworld')}
    mapping={'chunkSize':'chunk_size','removeCaves':'remove_caves','fillInCaves':'fill_caves',
        'runOptimiser':'run_optimiser','exportBlockAnimations':'block_animations','entityStartFrame':'entity_start',
        'entityEndFrame':'entity_end','entityFPS':'entity_fps','entityRandomSeed':'entity_seed',
        'entitySpawnDensity':'entity_density','entitySunLightLevel':'entity_light'}
    for key,prop in mapping.items():
        if key in metadata:data[prop]=metadata[key]
    for key,prop in [('entityExport','entity_types'),('entitySimulate','entity_simulate'),('entitySpawnRules','entity_spawn')]:
        data[prop]=','.join(metadata.get(key,[]))
    data['entities']=bool(data['entity_types'])
    regions=[]
    for name in (key[:-5] for key in metadata if key.startswith('region_') and key.endswith('_name')):
        region={key[len(name)+1:]:value for key,value in metadata.items() if key.startswith(name+'_')}
        region['fgChunks']=list(region.get('fgChunks',[]))
        flat=list(region.get('excludeRegions',[]))
        region['excludeRegions']=[dict(zip(('minX','minY','minZ','maxX','maxY','maxZ'),flat[i:i+6])) for i in range(0,len(flat),6)]
        flat=list(region.get('disabledChunks',[]))
        region['disabledChunks']=[flat[i:i+2] for i in range(0,len(flat),2)]
        regions.append(region)
    if not regions:raise ValueError('MiEx export contains no selection regions')
    data['region']=regions[0]
    data['extra_regions']=regions[1:]
    data['source_pack_ids']=list(metadata.get('resourcePacks',[]))
    if 'BlenderEEVEE' in data['source_pack_ids']:data['engine']='EEVEE'
    elif 'BlenderCycles' in data['source_pack_ids']:data['engine']='CYCLES'
    return data
