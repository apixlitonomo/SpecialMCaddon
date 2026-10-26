"""Blender construction for native Blockbench project files."""

from __future__ import annotations

import base64
import json
import os
import tempfile

import bpy
from mathutils import Vector

from .selection import deselect_all_objects
from .bedrock_mcblend.animation import (
    apply_bedrock_animations,
    arrange_nla_tracks_for_preview,
)
from .bedrock_mcblend.geometry import (
    bedrock_to_blender,
    build_bedrock_model,
    ensure_unique_collection,
)
from .bedrock_mcblend.importer import _create_locator_objects
from ..importers.bbmodel import (
    convert_animations,
    convert_geometry,
    load_bbmodel,
    project_summary,
)


def _decode_data_url(source):
    if not isinstance(source, str) or not source.startswith("data:"):
        return b"", ".png"
    header, separator, payload = source.partition(",")
    if not separator:
        return b"", ".png"
    extension = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/tga": ".tga",
        "image/webp": ".webp",
    }.get(header[5:].split(";", 1)[0].lower(), ".png")
    try:
        if ";base64" in header.lower():
            return base64.b64decode(payload, validate=False), extension
        from urllib.parse import unquote_to_bytes
        return unquote_to_bytes(payload), extension
    except (ValueError, TypeError):
        return b"", extension


def _texture_paths(data, filepath, temp_dir):
    paths = []
    embedded = []
    project_dir = os.path.dirname(os.path.abspath(filepath))
    for index, texture in enumerate(
        data.get("textures", []) if isinstance(data.get("textures"), list) else []
    ):
        if not isinstance(texture, dict):
            continue
        candidates = []
        relative = texture.get("relative_path")
        if isinstance(relative, str) and relative:
            candidates.append(os.path.normpath(os.path.join(project_dir, relative)))
        authored = texture.get("path")
        if isinstance(authored, str) and authored:
            candidates.append(
                authored if os.path.isabs(authored)
                else os.path.normpath(os.path.join(project_dir, authored))
            )
        resolved = next((candidate for candidate in candidates if os.path.isfile(candidate)), "")
        if not resolved:
            payload, extension = _decode_data_url(texture.get("source"))
            if payload:
                name = str(texture.get("name") or texture.get("id") or f"texture_{index}")
                safe = "".join(
                    character if character.isalnum() or character in "._-" else "_"
                    for character in name
                ).strip("._") or f"texture_{index}"
                if not os.path.splitext(safe)[1]:
                    safe += extension
                resolved = os.path.join(temp_dir, safe)
                with open(resolved, "wb") as handle:
                    handle.write(payload)
                embedded.append(resolved)
        if resolved and resolved not in paths:
            paths.append(resolved)
    return paths, embedded


def _has_renderable_geometry(geometry):
    for bone in geometry.get("bones", []):
        if not isinstance(bone, dict):
            continue
        if bone.get("cubes") or bone.get("poly_mesh") or bone.get("texture_meshes"):
            return True
    return False


def _is_java_project(data):
    meta = data.get("meta", {}) if isinstance(data, dict) else {}
    model_format = str(meta.get("model_format", "") if isinstance(meta, dict) else "")
    return model_format.lower().startswith("java")


def _build_empty_rig(context, geometry, display_name):
    name = display_name or geometry.get("description", {}).get("identifier", "BBModel")
    name = str(name).split(".")[-1] or "BBModel"
    collection = ensure_unique_collection(context, f"MC_Model_{name}")
    armature_data = bpy.data.armatures.new(f"{name}_Armature")
    armature = bpy.data.objects.new(name, armature_data)
    context.collection.objects.link(armature)
    collection.objects.link(armature)
    for owner in list(armature.users_collection):
        if owner != collection:
            owner.objects.unlink(armature)
    deselect_all_objects(context)
    armature.select_set(True)
    context.view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode="EDIT")
    bones = geometry.get("bones", [])
    source_bones = [bone for bone in bones if isinstance(bone, dict)] or [
        {"name": "model_root", "pivot": (0.0, 0.0, 0.0)}
    ]
    created = {}
    for source in source_bones:
        bone_name = str(source.get("name") or "bone")
        edit_bone = armature_data.edit_bones.new(bone_name)
        edit_bone.head = bedrock_to_blender(source.get("pivot", (0.0, 0.0, 0.0)))
        edit_bone.tail = edit_bone.head + Vector((0.0, 0.125, 0.0))
        created[bone_name] = edit_bone
    for source in source_bones:
        bone = created.get(str(source.get("name") or "bone"))
        parent = created.get(str(source.get("parent") or ""))
        if bone and parent and bone != parent:
            bone.parent = parent
    bpy.ops.object.mode_set(mode="OBJECT")
    return {
        "armature": armature,
        "mesh": None,
        "collection": collection,
        "origin_offset": Vector((0.0, 0.0, 0.0)),
    }


def _pack_embedded_images(paths):
    normalized = {os.path.normcase(os.path.abspath(path)) for path in paths}
    for image in bpy.data.images:
        image_path = bpy.path.abspath(getattr(image, "filepath", "") or "")
        if image_path and os.path.normcase(os.path.abspath(image_path)) in normalized:
            try:
                image.pack()
            except RuntimeError:
                pass


def import_bbmodel(
    context,
    filepath,
    *,
    texture_override="",
    interpolation="Closest",
    display_name="",
    animation_filter="__ALL__",
    append_animation_nla=True,
):
    """Import all supported project content from a ``.bbmodel`` file."""
    data = load_bbmodel(filepath)
    source_name = display_name or os.path.splitext(os.path.basename(filepath))[0]
    geometry, bone_by_group = convert_geometry(data, source_name=source_name)
    animation_payload, animation_ids = convert_animations(data, bone_by_group)

    with tempfile.TemporaryDirectory(prefix="mcbedrock_bbmodel_") as temp_dir:
        texture_paths, embedded = _texture_paths(data, filepath, temp_dir)
        if texture_override:
            primary_texture = bpy.path.abspath(texture_override)
            texture_paths = [primary_texture] + [
                path for path in texture_paths if path != primary_texture
            ]
        else:
            primary_texture = texture_paths[0] if texture_paths else ""

        # Keep every project texture available in the blend, even when a
        # multi-texture generic project has no single Minecraft material map.
        for texture_path in texture_paths:
            try:
                bpy.data.images.load(texture_path, check_existing=True)
            except RuntimeError:
                pass
        if _has_renderable_geometry(geometry):
            result = build_bedrock_model(
                context,
                geometry,
                texture_path=primary_texture,
                texture_paths=[primary_texture] if primary_texture else [],
                source_path=filepath,
                interpolation=interpolation,
                display_name=source_name,
                render_method="alpha_test",
                # Entity-style projects cull like imported Bedrock models;
                # Java block/item projects keep Java imports' double-sided look.
                solid_backface_culling=not _is_java_project(data),
            )
        else:
            result = _build_empty_rig(context, geometry, source_name)

        armature = result["armature"]
        armature["mcbedrock_asset_type"] = "bbmodel"
        armature["mcbedrock_source_path"] = os.path.abspath(filepath)
        armature["mcbedrock_bbmodel_bone_by_group"] = json.dumps(
            bone_by_group, sort_keys=True
        )
        armature["mcbedrock_bbmodel_summary"] = json.dumps(
            project_summary(data), sort_keys=True
        )
        armature["mcbedrock_bbmodel_format"] = str(
            data.get("meta", {}).get("model_format", "")
        )
        armature["mcbedrock_bbmodel_textures"] = json.dumps([
            {
                "name": texture.get("name", ""),
                "uuid": texture.get("uuid", ""),
                "path": texture.get("relative_path", texture.get("path", "")),
            }
            for texture in data.get("textures", [])
            if isinstance(texture, dict)
        ])
        armature["mcbedrock_bbmodel_animation_controllers"] = json.dumps(
            data.get("animation_controllers", [])
            if isinstance(data.get("animation_controllers"), list) else []
        )
        armature["mcbedrock_bbmodel_display"] = json.dumps(
            data.get("display", {})
            if isinstance(data.get("display"), dict) else {}
        )

        locators = _create_locator_objects(
            result["collection"],
            armature,
            {
                bone.get("name", "bone"): bone
                for bone in geometry.get("bones", [])
                if isinstance(bone, dict)
            },
            result.get("origin_offset"),
        )
        result["locators"] = locators

        _pack_embedded_images(embedded)

        created_actions = []
        definitions = animation_payload.get("animations", {})
        source_by_identifier = {}
        for source_index, source_animation in enumerate(
            data.get("animations", [])
            if isinstance(data.get("animations"), list) else []
        ):
            if not isinstance(source_animation, dict):
                continue
            source_key = str(
                source_animation.get("uuid")
                or source_animation.get("name") or source_index
            )
            source_by_identifier[animation_ids.get(source_key, "")] = source_animation
        selected_identifier = animation_ids.get(str(animation_filter), "")
        if animation_filter == "__ALL__":
            selected = list(definitions)
        elif animation_filter == "__NONE__":
            selected = []
        else:
            selected = [selected_identifier] if selected_identifier else [
                animation_filter
            ]
        if selected:
            animation_path = os.path.join(temp_dir, "bbmodel.animations.json")
            with open(animation_path, "w", encoding="utf-8") as handle:
                json.dump(animation_payload, handle)
            for identifier in selected:
                if identifier not in definitions:
                    continue
                created_actions.extend(apply_bedrock_animations(
                    context,
                    armature,
                    animation_path,
                    animation_filter=identifier,
                    append_nla=bool(
                        append_animation_nla and animation_filter == "__ALL__"
                    ),
                ))
            for action in created_actions:
                action["mcbedrock_animation_source"] = os.path.abspath(filepath)
                action["mcbedrock_bbmodel_source"] = os.path.abspath(filepath)
                identifier = str(
                    action.get("mcbedrock_animation_name", "") or "")
                source_animation = source_by_identifier.get(identifier, {})
                source_uuid = str(source_animation.get("uuid", "") or "")
                if source_uuid:
                    action["mcbedrock_bbmodel_animation_uuid"] = source_uuid
                action["mcbedrock_bbmodel_clip_name"] = str(
                    source_animation.get("name", identifier) or identifier
                )
                action["mcbedrock_bbmodel_animated_bones"] = json.dumps(
                    sorted(
                        definitions.get(identifier, {}).get("bones", {}).keys()
                    )
                )
                try:
                    action["mcbedrock_bbmodel_snapping"] = max(
                        1.0,
                        float(source_animation.get(
                            "snapping",
                            action.get("mcbedrock_bake_fps", 24),
                        )),
                    )
                except (TypeError, ValueError):
                    action["mcbedrock_bbmodel_snapping"] = 24.0
            if animation_filter == "__ALL__" and append_animation_nla:
                arrange_nla_tracks_for_preview(context, armature)
        result["animations"] = created_actions

    deselect_all_objects(context)
    armature.select_set(True)
    context.view_layer.objects.active = armature
    return result
