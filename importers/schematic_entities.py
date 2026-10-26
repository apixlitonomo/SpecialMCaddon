"""Schematic entity records and static Java item model selection (no Blender)."""
import math
import os

from .blockstate_resolver import as_roots, resolve_model_path, split_namespace
from .bedrock_assets import load_json_file


def finite_vector(value, size, default):
    if value is None:
        return tuple(default)
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise ValueError('Expected a %d-component vector' % size)
    result = tuple(float(v) for v in value)
    if not all(math.isfinite(v) for v in result):
        raise ValueError('Entity transform contains a non-finite number')
    return result


def normalize_entity(raw):
    if not isinstance(raw, dict):
        raise ValueError('Entity record must be a compound')
    # Sponge v3 wraps extra NBT in Data; early Litematics used EntityData.
    data = dict(raw.get('Data', raw.get('EntityData', raw)))
    identifier = raw.get('Id') or data.get('id') or data.get('Id') or ''
    if not isinstance(identifier, str) or not identifier:
        raise ValueError('Entity has no identifier')
    data['id'] = identifier if ':' in identifier else 'minecraft:' + identifier
    position = raw.get('Pos', data.get('Pos'))
    if 'EntityData' in raw and position is None:
        position = [raw.get(axis, 0) for axis in ('x', 'y', 'z')]
    data['Pos'] = finite_vector(position, 3, (0, 0, 0))
    return data


def iter_entity_records(world):
    """Yield (raw NBT, region offset); passengers retain region-relative positions."""
    def walk(raw, offset, depth=0):
        if depth > 64:
            return
        yield raw, offset
        if isinstance(raw, dict):
            data = raw.get('Data', raw.get('EntityData', raw))
            if isinstance(data, dict):
                for passenger in data.get('Passengers', []) or []:
                    yield from walk(passenger, offset, depth + 1)
    for region in world.regions:
        offset = getattr(region, 'entity_origin', region.origin)
        for raw in region.entities:
            yield from walk(raw, offset)


def _item_property(item, prop, index=0, context='none', value_kind='floats'):
    components = item.get('components', {})
    tag = item.get('tag', {})
    prop = str(prop).split(':')[-1]
    if prop == 'display_context':
        return context
    if prop == 'custom_model_data':
        value = components.get('minecraft:custom_model_data', tag.get('CustomModelData', 0))
        if isinstance(value, dict):
            values = value.get(value_kind, [])
            return values[index] if 0 <= index < len(values) else ('' if value_kind == 'strings' else 0)
        return value
    if prop == 'damage':
        value = components.get('minecraft:damage', tag.get('Damage', 0))
        maximum = components.get('minecraft:max_damage', 1)
        return float(value) / max(1, float(maximum))
    if prop == 'damaged':
        return bool(components.get('minecraft:damage', tag.get('Damage', 0)))
    if prop in ('pull', 'pulling', 'blocking', 'charged', 'firework', 'lefthanded', 'cast'):
        return 0
    raise ValueError('Unsupported item property: ' + prop)


def item_model_paths(roots, item, context='none'):
    """Resolve static model/composite/CMD branches and legacy model overrides."""
    if not isinstance(item, dict):
        raise ValueError('Missing item stack')
    identifier = item.get('id', '')
    if not identifier or identifier == 'minecraft:air':
        return []
    roots = as_roots(roots)
    components = item.get('components', {})
    model_id = components.get('minecraft:item_model', identifier)
    namespace, name = split_namespace(model_id)
    if any(part in ('..', '') for part in name.split('/')) or '\\' in name:
        raise ValueError('Invalid item model identifier')
    definition = next((os.path.join(root, 'assets', namespace, 'items', name + '.json')
                       for root in roots if os.path.isfile(os.path.join(root, 'assets', namespace, 'items', name + '.json'))), '')
    def model_path(ref):
        path = resolve_model_path(roots, ref)
        if not path:
            raise ValueError('Missing item model: ' + str(ref))
        return path
    def select(node, depth=0):
        if depth > 32 or not isinstance(node, dict):
            raise ValueError('Invalid item model tree')
        kind = str(node.get('type', '')).split(':')[-1]
        if kind == 'empty':
            return []
        if kind == 'model':
            return [model_path(node.get('model', ''))]
        if kind == 'composite':
            return [path for child in node.get('models', []) for path in select(child, depth + 1)]
        if kind == 'range_dispatch':
            value = float(_item_property(item, node.get('property'), int(node.get('index', 0)), context)) * float(node.get('scale', 1))
            matches = [e for e in node.get('entries', []) if float(e.get('threshold', 0)) <= value]
            chosen = max(matches, key=lambda e: float(e.get('threshold', 0)))['model'] if matches else node.get('fallback')
            return select(chosen, depth + 1)
        if kind in ('condition', 'select'):
            value = _item_property(item, node.get('property'), int(node.get('index', 0)), context,
                                   'flags' if kind == 'condition' else 'strings')
            if kind == 'condition':
                return select(node.get('on_true' if value else 'on_false'), depth + 1)
            for case in node.get('cases', []):
                when = case.get('when')
                if value in (when if isinstance(when, list) else [when]):
                    return select(case.get('model'), depth + 1)
            return select(node.get('fallback'), depth + 1)
        raise ValueError('Unsupported item model type: ' + kind)
    if definition:
        return select(load_json_file(definition).get('model'))
    path = model_path(namespace + ':item/' + name)
    data = load_json_file(path)
    # Legacy overrides use the last matching predicate in the authored order.
    for override in data.get('overrides', []):
        if all(float(_item_property(item, key, context=context)) >= float(value)
               for key, value in override.get('predicate', {}).items()):
            path = model_path(override.get('model', ''))
    return [path]
