"""Static schematic display/entity imports, using the same resource stack as blocks.

Display NBT: https://www.minecraft.net/en-us/article/minecraft-java-edition-1-19-4
Unsupported visuals remain named empties with their NBT and a reported reason.
"""
import json
import math
from types import SimpleNamespace

import bpy
from mathutils import Matrix, Quaternion, Vector

from ..importers.schematic_entities import finite_vector, iter_entity_records, normalize_entity, item_model_paths
from ..importers.bedrock_assets import build_resource_pack_stack
from ..ui.schematic_importer import block_state_string
from .java_builder import build_java_model_from_file
from .java_resolver import _merge_java_model

# Minecraft blocks -> Blender metres: exchange vertical Y with Z.
AXES = Matrix(((1, 0, 0, 0), (0, 0, 1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))


def _rotation(value):
    if isinstance(value, dict):
        axis = Vector(finite_vector(value.get('axis'), 3, (0, 1, 0)))
        angle = float(value.get('angle', 0))
        if not math.isfinite(angle) or axis.length == 0:
            raise ValueError('Invalid display rotation')
        return Quaternion(axis.normalized(), angle).to_matrix().to_4x4()
    x, y, z, w = finite_vector(value, 4, (0, 0, 0, 1))
    q = Quaternion((w, x, y, z))
    if q.magnitude == 0:
        raise ValueError('Zero display quaternion')
    return q.normalized().to_matrix().to_4x4()


def display_transform(value):
    if value is None:
        return Matrix.Identity(4)
    if isinstance(value, (list, tuple)):
        values = finite_vector(value, 16, ())
        return Matrix(tuple(values[i:i + 4] for i in range(0, 16, 4)))
    if not isinstance(value, dict):
        raise ValueError('Invalid display transformation')
    translation = finite_vector(value.get('translation'), 3, (0, 0, 0))
    scale = finite_vector(value.get('scale'), 3, (1, 1, 1))
    return (Matrix.Translation(translation) @ _rotation(value.get('left_rotation'))
            @ Matrix.Diagonal((*scale, 1)) @ _rotation(value.get('right_rotation')))


def entity_matrix(data, offset):
    position = Vector(finite_vector(data.get('Pos'), 3, (0, 0, 0))) + Vector(offset)
    yaw, pitch = finite_vector(data.get('Rotation'), 2, (0, 0))
    transform = (Matrix.Translation(position)
                 @ Matrix.Rotation(math.radians(-yaw), 4, 'Y')
                 @ Matrix.Rotation(math.radians(pitch), 4, 'X'))
    if data['id'] in ('minecraft:block_display', 'minecraft:item_display', 'minecraft:text_display'):
        transform @= display_transform(data.get('transformation'))
    return AXES @ transform @ AXES


def item_display_matrix(data, display_context):
    display = data.get('display', {}).get(display_context, {})
    translation = Vector(finite_vector(display.get('translation'), 3, (0, 0, 0))) / 16
    angles = finite_vector(display.get('rotation'), 3, (0, 0, 0))
    scale = finite_vector(display.get('scale'), 3, (1, 1, 1))
    matrix = Matrix.Translation(translation)
    for axis, angle in zip('XYZ', angles):
        matrix @= Matrix.Rotation(math.radians(angle), 4, axis)
    return AXES @ matrix @ Matrix.Diagonal((*scale, 1)) @ AXES


class _Proxy:
    def __init__(self, original, **overrides):
        self._original = original
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self._original, name)


class EntityBuilder:
    def __init__(self, context, roots, collection, root, models, materials, interpolation):
        self.context, self.roots = context, roots
        self.collection, self.root = collection, root
        self.models, self.materials = models, materials
        self.interpolation = interpolation
        self.block_meshes, self.item_meshes = {}, {}
        self.stack = None

    def block(self, data, anchor):
        from .schematic_builder import _make_chunk_object
        state_data = data.get('block_state', data.get('BlockState', {}))
        if not isinstance(state_data, dict) or not state_data.get('Name'):
            raise ValueError('Block display has no block_state')
        state = block_state_string(state_data)
        model = self.models.get(state)
        if not model.resolved:
            raise ValueError('Missing block-display model: ' + state)
        if not model.faces:
            return
        mesh = self.block_meshes.get(state)
        if mesh is None:
            verts, faces, uvs, keys = [], [], [], []
            for face in model.faces:
                start = len(verts)
                verts.extend(face.verts)
                faces.append(tuple(range(start, start + 4)))
                uvs.append(face.uvs)
                keys.append((face.texture, face.tint))
            obj = _make_chunk_object(self.context, 'Display_' + state.split('[')[0],
                (0, 0, 0), verts, faces, uvs, keys, self.materials, self.collection)
            mesh = self.block_meshes[state] = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
        obj = bpy.data.objects.new('Block Display', mesh)
        self.collection.objects.link(obj)
        obj.parent = anchor

    def item(self, data, anchor):
        item = data.get('item', data.get('Item', {}))
        mode = str(data.get('item_display', 'ground' if data['id'] == 'minecraft:item' else 'none'))
        for path in item_model_paths(self.roots, item, mode):
            cached = self.item_meshes.get(path)
            if cached is None:
                merged = _merge_java_model(path, self.roots)
                # In the schematic's own mirrored axes, like its blocks.
                result = build_java_model_from_file(self.context, path,
                    resource_roots=self.roots, vanilla_path='', interpolation=self.interpolation,
                    mirrored=True)
                source = result['mesh']
                mesh = source.data
                # Direct model import uses a corner origin; item displays use a centre.
                if source.get('mcbedrock_generated_fallback'):
                    centering = Matrix.Translation((0, 0, -0.5))
                else:
                    centering = Matrix.Translation((-0.5, -0.5, -0.5))
                cached = self.item_meshes[path] = (mesh, merged, centering)
                source_collection = result.get('collection')
                bpy.data.objects.remove(source, do_unlink=True)
                if source_collection and not source_collection.objects and not source_collection.children:
                    bpy.data.collections.remove(source_collection)
            mesh, merged, centering = cached
            obj = bpy.data.objects.new('Item Display', mesh)
            self.collection.objects.link(obj)
            obj.parent = anchor
            obj.matrix_basis = item_display_matrix(merged, mode) @ centering

    def entity(self, data, anchor):
        # Pack-authored client entities use the established model/rig importer.
        # Java vanilla mobs have code-defined meshes, not models/*.json files.
        if self.stack is None:
            self.stack = build_resource_pack_stack(self.roots)
        resolved = self.stack.resolve('client_entity', data['id'])
        if not resolved.ok:
            raise ValueError('No importable entity model in active packs: ' + data['id'])
        from .bedrock_mcblend.importer import import_client_entity
        scene = _Proxy(self.context.scene,
            mcbedrock_active_packs=[SimpleNamespace(path=p) for p in self.roots])
        context = _Proxy(self.context, scene=scene)
        before = set(bpy.data.objects)
        import_client_entity(context, resolved.resource.path, animation_filter='__NONE__',
                             entity_effects='NONE', interpolation=self.interpolation)
        created = set(bpy.data.objects) - before
        if not any(o.type == 'MESH' for o in created):
            raise ValueError('Entity importer produced no mesh: ' + data['id'])
        for obj in created:
            for owner in list(obj.users_collection):
                owner.objects.unlink(obj)
            self.collection.objects.link(obj)
            if obj.parent not in created:
                obj.parent = anchor
                obj.matrix_parent_inverse = Matrix.Identity(4)

    def build(self, world):
        stats = {'entities': 0, 'entity_markers': 0, 'entity_warnings': []}
        for index, (raw, offset) in enumerate(iter_entity_records(world)):
            anchor = bpy.data.objects.new('Schematic Entity %d' % (index + 1), None)
            self.collection.objects.link(anchor)
            anchor.parent = self.root
            anchor.empty_display_size = 0.25
            anchor['mcbedrock_entity_nbt'] = json.dumps(raw, ensure_ascii=False)
            anchor['mcbedrock_asset_type'] = 'schematic_entity'
            before = set(bpy.data.objects)
            try:
                data = normalize_entity(raw)
                identifier = data['id']
                anchor.name = identifier.split(':')[-1]
                anchor['mcbedrock_entity_identifier'] = identifier
                pose = dict(data)
                pose.pop('transformation', None)
                anchor.matrix_basis = entity_matrix(pose, offset)
                if identifier in ('minecraft:block_display', 'minecraft:falling_block'):
                    self.block(data, anchor)
                elif identifier in ('minecraft:item_display', 'minecraft:item'):
                    self.item(data, anchor)
                elif identifier in ('minecraft:marker', 'minecraft:interaction', 'minecraft:area_effect_cloud'):
                    anchor['mcbedrock_invisible_entity'] = True
                else:
                    self.entity(data, anchor)
                if identifier in ('minecraft:block_display', 'minecraft:item_display'):
                    # Object TRS decomposes away shear. Bake the full affine
                    # transform into these visual meshes to retain right rotation
                    # combined with nonuniform scale, negative scale and matrix NBT.
                    transform = AXES @ display_transform(data.get('transformation')) @ AXES
                    if transform != Matrix.Identity(4):
                        for child in list(anchor.children):
                            if child.type == 'MESH':
                                child.data = child.data.copy()
                                child.data.transform(transform @ child.matrix_basis)
                                child.matrix_basis = Matrix.Identity(4)
                if data.get('billboard', 'fixed') != 'fixed':
                    stats['entity_warnings'].append(identifier + ': billboard imported at its saved orientation')
                stats['entities'] += 1
            except Exception as exc:
                # One unsupported entity must not discard the rest of the schematic.
                for obj in set(bpy.data.objects) - before:
                    bpy.data.objects.remove(obj, do_unlink=True)
                anchor.empty_display_type = 'CUBE'
                anchor.show_name = True
                anchor['mcbedrock_import_warning'] = str(exc)
                stats['entity_markers'] += 1
                stats['entity_warnings'].append('%s: %s' % (anchor.name, exc))
        return stats
