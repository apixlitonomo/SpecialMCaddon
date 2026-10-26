import bpy
import os
from math import radians

PARTICLE_MATERIAL_SCHEMA = 2


def _set_material_metadata(
    material,
    texture_path,
    material_type,
    is_lit,
    interpolation,
    attribute_source="OBJECT",
):
    """Record enough state to replace textures without changing shading."""
    material["mcbedrock_particle_material_schema"] = PARTICLE_MATERIAL_SCHEMA
    material["mcbedrock_texture_path"] = str(texture_path or "")
    material["mcbedrock_texture_bound"] = False
    material["mcbedrock_material_type"] = str(
        material_type or "particles_alpha"
    )
    material["mcbedrock_is_lit"] = bool(is_lit)
    material["mcbedrock_interpolation"] = str(
        interpolation or "Closest"
    )
    material["mcbedrock_attribute_source"] = str(
        attribute_source or "OBJECT"
    )


def set_global_tint(material, tint):
    """Update the stable Global_Tint contract on one particle material."""
    if not material or not material.use_nodes:
        return False
    tint_node = material.node_tree.nodes.get("Global_Tint")
    if tint_node is None or "Color2" not in tint_node.inputs:
        return False
    values = tuple(float(value) for value in tint[:4])
    if len(values) < 4:
        values += (1.0,) * (4 - len(values))
    tint_node.inputs["Color2"].default_value = values
    return True


def replace_particle_texture(objects, texture_path, interpolation=None):
    """Replace textures without discarding UV, tint, or material mode."""
    texture_path = os.path.abspath(str(texture_path or ""))
    if not os.path.isfile(texture_path):
        raise FileNotFoundError(texture_path)

    image = bpy.data.images.load(texture_path, check_existing=True)
    updated_materials = set()
    rebuilt_data = set()

    for obj in tuple(objects or ()):
        data = getattr(obj, "data", None)
        materials = getattr(data, "materials", None)
        if materials is None:
            continue

        material = materials[0] if materials else None
        material_key = material.as_pointer() if material else None
        if material and material_key not in updated_materials:
            texture_nodes = (
                tuple(
                    node
                    for node in material.node_tree.nodes
                    if node.type == "TEX_IMAGE"
                )
                if material.use_nodes
                else ()
            )
            if texture_nodes:
                resolved_interpolation = str(
                    interpolation
                    or material.get("mcbedrock_interpolation", "Closest")
                )
                for node in texture_nodes:
                    node.image = image
                    node.interpolation = resolved_interpolation
                _set_material_metadata(
                    material,
                    texture_path,
                    material.get(
                        "mcbedrock_material_type", "particles_alpha"
                    ),
                    material.get("mcbedrock_is_lit", False),
                    resolved_interpolation,
                    material.get("mcbedrock_attribute_source", "OBJECT"),
                )
                material["mcbedrock_texture_bound"] = True
                updated_materials.add(material_key)
                continue

        # Missing-texture placeholders have no image node. Rebuild once per
        # shared mesh/curve datablock from recorded material settings.
        data_key = data.as_pointer()
        if data_key in rebuilt_data:
            continue
        old_tint = (1.0, 1.0, 1.0, 1.0)
        material_type = "particles_alpha"
        is_lit = False
        resolved_interpolation = str(interpolation or "Closest")
        if material:
            material_type = str(
                material.get("mcbedrock_material_type", material_type)
            )
            is_lit = bool(material.get("mcbedrock_is_lit", is_lit))
            resolved_interpolation = str(
                interpolation
                or material.get(
                    "mcbedrock_interpolation", resolved_interpolation
                )
            )
            tint_node = (
                material.node_tree.nodes.get("Global_Tint")
                if material.use_nodes
                else None
            )
            if tint_node and "Color2" in tint_node.inputs:
                old_tint = tuple(
                    tint_node.inputs["Color2"].default_value
                )
        replacement = create_particle_material(
            obj,
            texture_path,
            material_type,
            is_lit=is_lit,
            interpolation=resolved_interpolation,
            attribute_source=(
                material.get("mcbedrock_attribute_source", "OBJECT")
                if material else "OBJECT"
            ),
        )
        set_global_tint(replacement, old_tint)
        rebuilt_data.add(data_key)
        updated_materials.add(replacement.as_pointer())

    return len(updated_materials)


def create_billboard_instance(context, identifier, width=0.1, height=0.1, facing_mode="rotate_xyz"):
    """
    Creates a plane sized to the particle's billboard dimensions.
    Applies the correct camera-facing constraint based on the facing_mode from the particle JSON.

    facing_mode values:
      - rotate_xyz / lookat_xyz : Full camera-facing Track-To (default Bedrock behaviour)
      - rotate_y  / lookat_y    : Camera-facing locked to vertical (Z) axis only
      - direction_x/y/z         : No constraint; rotation baked per-frame from velocity in builder
      - emitter_transform_xy/xz/yz : No billboard; particle lives in emitter local space
    """
    short_name = identifier.split(":")[-1] if ":" in identifier else identifier
    name = f"Particle_{short_name}"

    mesh = bpy.data.meshes.new(f"{name}_Mesh")
    w, h = width / 2.0, height / 2.0
    mesh.from_pydata(
        [(-w, -h, 0), (w, -h, 0), (w, h, 0), (-w, h, 0)],
        [], 
        [(0, 1, 2, 3)]
    )
    uv_layer = mesh.uv_layers.new(name="UVMap")
    uvs = [(0, 0), (1, 0), (1, 1), (0, 1)]
    for i, loop in enumerate(mesh.loops):
        uv_layer.data[loop.index].uv = uvs[i]
    mesh.update()
    
    plane = bpy.data.objects.new(name, mesh)
    context.collection.objects.link(plane)

    # Store the facing mode so the builder can reference it during bake
    plane["mc_billboard_mode"] = facing_mode

    cam = next((obj for obj in context.scene.objects if obj.type == 'CAMERA'), None)

    if facing_mode in ("rotate_xyz", "lookat_xyz"):
        # Full camera-facing — exactly the original behaviour
        if cam:
            tt = plane.constraints.new(type='COPY_ROTATION')
            tt.target = cam
            tt.mix_mode = 'BEFORE'
            tt.target_space = 'WORLD'
            tt.owner_space = 'WORLD'
        else:
            # Stand it up vertically so it faces the front by default
            plane.rotation_euler[0] = radians(90)

    elif facing_mode in ("rotate_y", "lookat_y"):
        # Face the camera but only rotate around the world-up (Z) axis — like a billboard on a pole.
        # TRACK_TO with use_target_z=True locks the tilt so only yaw is affected.
        if cam:
            tt = plane.constraints.new(type='TRACK_TO')
            tt.target = cam
            tt.track_axis = 'TRACK_Z'
            tt.up_axis = 'UP_Y'
            # use_target_z makes the constraint keep the plane's up aligned to world Z,
            # preventing it from tilting toward the camera vertically.
            if hasattr(tt, 'use_target_z'):
                tt.use_target_z = True
        # Stand plane upright so it is vertical in world space
        plane.rotation_euler[0] = radians(90)

    elif facing_mode.startswith("direction_") or facing_mode == "lookat_direction":
        # The rotation baked in builder.py will align a specific axis (X, Y, or Z) to the velocity.
        # Here we add a LOCKED_TRACK to force the Z axis (face of plane) to point to the camera,
        # while keeping the velocity axis locked.
        if cam:
            tt = plane.constraints.new(type='LOCKED_TRACK')
            tt.target = cam
            tt.track_axis = 'TRACK_Z'
            
            # The lock axis depends on which axis builder.py aligned to the velocity.
            # We will configure builder.py to align the matching axis.
            if facing_mode == "direction_x" or facing_mode == "lookat_direction":
                tt.lock_axis = 'LOCK_X'
            elif facing_mode == "direction_y":
                tt.lock_axis = 'LOCK_Y'
            elif facing_mode == "direction_z":
                tt.lock_axis = 'LOCK_Z'
            else:
                tt.lock_axis = 'LOCK_Y'
        else:
            plane.rotation_euler[0] = radians(90)
            
    elif facing_mode.startswith("emitter_transform"):
        plane.rotation_euler[0] = radians(90)

    else:
        # Unknown / future mode — fall back to full camera-facing
        if cam:
            tt = plane.constraints.new(type='COPY_ROTATION')
            tt.target = cam
            tt.mix_mode = 'BEFORE'
            tt.target_space = 'WORLD'
            tt.owner_space = 'WORLD'
        else:
            plane.rotation_euler[0] = radians(90)

    return plane


def _set_blend_mode(mat, is_additive, is_alpha):
    """
    Set EEVEE blend / render method on the material.
    Handles Blender 2.80–4.1 (blend_method) and Blender 4.2+ (surface_render_method).
    """
    # Blender 4.2+ uses surface_render_method instead of blend_method
    if hasattr(mat, 'surface_render_method'):
        if is_additive or is_alpha:
            mat.surface_render_method = 'BLENDED'
        else:
            mat.surface_render_method = 'DITHERED'  # closest to OPAQUE in 4.2+
    else:
        # Blender 2.80–4.1
        if is_additive:
            # 'ADDITIVE' not available on older builds; fall back to BLEND
            if hasattr(mat, 'blend_method'):
                try:
                    mat.blend_method = 'ADDITIVE'
                except TypeError:
                    mat.blend_method = 'BLEND'
        elif is_alpha:
            if hasattr(mat, 'blend_method'):
                mat.blend_method = 'BLEND'
        else:
            if hasattr(mat, 'blend_method'):
                mat.blend_method = 'CLIP'
                mat.alpha_threshold = 0.1

    if hasattr(mat, 'shadow_method') and (is_additive or is_alpha):
        mat.shadow_method = 'HASHED'
    if hasattr(mat, 'show_transparent_back'):
        mat.show_transparent_back = True


def _build_additive_material(nodes, links, tex_node, obj_info, output_node):
    """
    Build the node graph for particles_add (additive blending).

    Graph:
        Texture.Color ──┐
                        MixRGB(Multiply) ──> MixRGB(Multiply, Global_Tint) ──> Emission.Color
        ObjInfo.Color ──┘                                                       Emission.Strength = 1
                                                                                    │
        Transparent BSDF ──> Add Shader ──────────────────────────────────────> Material Output
                                 ↑
                         Emission ──────────────────────────────────────────────┘

    Alpha from the texture is NOT piped into the Transparent factor so that the
    add-blend shows bright pixels and hides black pixels naturally.
    """
    # MixRGB: Texture × Object Color  (per-particle tint)
    mix_node = nodes.new('ShaderNodeMixRGB')
    mix_node.location = (-100, 300)
    mix_node.blend_type = 'MULTIPLY'
    mix_node.inputs['Fac'].default_value = 1.0

    links.new(tex_node.outputs['Color'], mix_node.inputs['Color1'])
    links.new(obj_info.outputs['Vector'], mix_node.inputs['Color2'])

    # MixRGB: Global UI tint  (named so UI can drive it)
    tint_node = nodes.new('ShaderNodeMixRGB')
    tint_node.name = "Global_Tint"
    tint_node.location = (50, 300)
    tint_node.blend_type = 'MULTIPLY'
    tint_node.inputs['Fac'].default_value = 1.0
    tint_node.inputs['Color2'].default_value = (1.0, 1.0, 1.0, 1.0)

    links.new(mix_node.outputs['Color'], tint_node.inputs['Color1'])

    # Emission node — driven by the tinted colour
    emission = nodes.new('ShaderNodeEmission')
    emission.location = (250, 300)
    emission.inputs['Strength'].default_value = 1.0
    links.new(tint_node.outputs['Color'], emission.inputs['Color'])

    # Transparent BSDF — for the "dark = invisible" additive behaviour
    transparent = nodes.new('ShaderNodeBsdfTransparent')
    transparent.location = (250, 100)

    # Add Shader — combines transparent + emission for additive look
    add_shader = nodes.new('ShaderNodeAddShader')
    add_shader.location = (450, 200)
    links.new(transparent.outputs['BSDF'], add_shader.inputs[0])
    links.new(emission.outputs['Emission'], add_shader.inputs[1])

    links.new(add_shader.outputs['Shader'], output_node.inputs['Surface'])


def _build_standard_material(nodes, links, tex_node, obj_info, bsdf, is_alpha, is_lit, attribute_source):
    """
    Build the standard Principled BSDF node graph used by alpha/blend/opaque materials.
    """
    # MixRGB: Texture × Object Color  (per-particle tint)
    mix_node = nodes.new('ShaderNodeMixRGB')
    mix_node.location = (-100, 300)
    mix_node.blend_type = 'MULTIPLY'
    mix_node.inputs['Fac'].default_value = 1.0

    links.new(tex_node.outputs['Color'], mix_node.inputs['Color1'])
    links.new(obj_info.outputs['Vector'], mix_node.inputs['Color2'])

    # MixRGB: Global UI tint
    tint_node = nodes.new('ShaderNodeMixRGB')
    tint_node.name = "Global_Tint"
    tint_node.location = (50, 300)
    tint_node.blend_type = 'MULTIPLY'
    tint_node.inputs['Fac'].default_value = 1.0
    tint_node.inputs['Color2'].default_value = (1.0, 1.0, 1.0, 1.0)

    links.new(mix_node.outputs['Color'], tint_node.inputs['Color1'])
    if is_lit:
        links.new(tint_node.outputs['Color'], bsdf.inputs['Base Color'])
    else:
        bsdf.inputs['Base Color'].default_value = (0.0, 0.0, 0.0, 1.0)

    # Link alpha channel ALWAYS (Opaque uses Alpha Clip)
    math_node = None
    if 'Alpha' in bsdf.inputs:
        math_node = nodes.new('ShaderNodeMath')
        math_node.operation = 'MULTIPLY'
        math_node.location = (-100, 100)

        links.new(tex_node.outputs['Alpha'], math_node.inputs[0])
        # Use an Attribute node to read mc_alpha since Object Info drops alpha
        attr_alpha = nodes.new('ShaderNodeAttribute')
        attr_alpha.attribute_type = attribute_source
        attr_alpha.attribute_name = "mc_alpha"
        attr_alpha.location = (-300, 50)
        links.new(attr_alpha.outputs['Fac'], math_node.inputs[1])

        links.new(math_node.outputs['Value'], bsdf.inputs['Alpha'])

    # If it is lit by the scene, we DO NOT make it emissive.
    # It will react to sunlight normally using its Base Color.
    if not is_lit:
        # Mask the emission with Alpha so fully transparent corners don't glow
        if math_node:
            em_mix = nodes.new('ShaderNodeMixRGB')
            em_mix.blend_type = 'MULTIPLY'
            em_mix.location = (200, 200)
            em_mix.inputs['Fac'].default_value = 1.0
            links.new(tint_node.outputs['Color'], em_mix.inputs['Color1'])
            links.new(math_node.outputs['Value'], em_mix.inputs['Color2'])
            emission_color_socket = em_mix.outputs['Color']
        else:
            emission_color_socket = tint_node.outputs['Color']

        # Make particles unlit (shadeless) like in Minecraft
        if 'Emission Color' in bsdf.inputs:          # Blender 4.0+
            links.new(emission_color_socket, bsdf.inputs['Emission Color'])
            bsdf.inputs['Emission Strength'].default_value = 1.0
        elif 'Emission' in bsdf.inputs:              # Blender 3.x
            links.new(emission_color_socket, bsdf.inputs['Emission'])
            if 'Emission Strength' in bsdf.inputs:
                bsdf.inputs['Emission Strength'].default_value = 1.0


def create_particle_material(obj, texture_path, material_type, is_lit=False, interpolation='Closest', attribute_source='OBJECT'):
    """
    Creates a particle material suited to the Bedrock material type:

      particles_alpha  → Alpha-blended Principled BSDF (BLEND mode)
      particles_blend  → Same as particles_alpha
      particles_add    → Additive  (Transparent + Emission via Add Shader)
      particles_opaque → Opaque Principled BSDF

    Compatible with Blender 2.80 – 4.x (EEVEE Next included).
    """
    mat_name = f"Mat_{obj.name}"
    mat = bpy.data.materials.new(name=mat_name)
    mat.use_nodes = True
    mat.use_backface_culling = False
    _set_material_metadata(
        mat,
        texture_path,
        material_type,
        is_lit,
        interpolation,
        attribute_source,
    )

    is_additive = "add" in material_type
    is_alpha = (not is_additive) and ("alpha" in material_type or "blend" in material_type)

    _set_blend_mode(mat, is_additive, is_alpha)

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    # Locate the default Material Output node
    output_node = None
    for n in nodes:
        if n.type == 'OUTPUT_MATERIAL':
            output_node = n
            break

    # Locate/create the Principled BSDF (only needed for non-additive paths)
    bsdf = nodes.get("Principled BSDF")
    if bsdf is None:
        for n in nodes:
            if n.type == 'BSDF_PRINCIPLED':
                bsdf = n
                break

    if not is_additive:
        if bsdf is None:
            return  # Cannot set up standard material without a BSDF
        # Make particle textures non-reflective by default
        if "Specular" in bsdf.inputs:
            bsdf.inputs["Specular"].default_value = 0.0
        if "Specular IOR Level" in bsdf.inputs:
            bsdf.inputs["Specular IOR Level"].default_value = 0.0
        if "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = 1.0

    if os.path.isfile(texture_path):
        tex_node = nodes.new('ShaderNodeTexImage')
        tex_node.name = "Particle_Texture"
        tex_node.label = "Bedrock Particle Texture"
        tex_node.location = (-300, 300)
        # Reuse an already-loaded image with the same path to avoid duplicate datablocks.
        img = bpy.data.images.get(os.path.basename(texture_path))
        if not img or bpy.path.abspath(img.filepath) != bpy.path.abspath(texture_path):
            img = bpy.data.images.load(texture_path, check_existing=True)
        tex_node.image = img
        tex_node.interpolation = interpolation
        mat["mcbedrock_texture_bound"] = img is not None

        # ------------------------------------------------------------------
        # UV Attribute Nodes (single floats for compatibility)
        # ------------------------------------------------------------------
        attr_off_x = nodes.new('ShaderNodeAttribute')
        attr_off_x.attribute_name = "uv_offset_x"
        attr_off_x.attribute_type = attribute_source
        attr_off_x.location = (-900, 400)

        attr_off_y = nodes.new('ShaderNodeAttribute')
        attr_off_y.attribute_name = "uv_offset_y"
        attr_off_y.attribute_type = attribute_source
        attr_off_y.location = (-900, 300)

        combine_off = nodes.new('ShaderNodeCombineXYZ')
        combine_off.location = (-700, 350)
        links.new(attr_off_x.outputs['Fac'], combine_off.inputs['X'])
        links.new(attr_off_y.outputs['Fac'], combine_off.inputs['Y'])

        attr_scale_x = nodes.new('ShaderNodeAttribute')
        attr_scale_x.attribute_name = "uv_scale_x"
        attr_scale_x.attribute_type = attribute_source
        attr_scale_x.location = (-900, 100)

        attr_scale_y = nodes.new('ShaderNodeAttribute')
        attr_scale_y.attribute_name = "uv_scale_y"
        attr_scale_y.attribute_type = attribute_source
        attr_scale_y.location = (-900, 0)

        combine_scale = nodes.new('ShaderNodeCombineXYZ')
        combine_scale.location = (-700, 50)
        links.new(attr_scale_x.outputs['Fac'], combine_scale.inputs['X'])
        links.new(attr_scale_y.outputs['Fac'], combine_scale.inputs['Y'])

        # Texture Coordinate
        tex_coord = nodes.new('ShaderNodeTexCoord')
        tex_coord.location = (-700, 500)

        # Mapping Node
        mapping = nodes.new('ShaderNodeMapping')
        mapping.location = (-500, 300)

        links.new(tex_coord.outputs['UV'], mapping.inputs['Vector'])
        links.new(combine_off.outputs['Vector'], mapping.inputs['Location'])
        links.new(combine_scale.outputs['Vector'], mapping.inputs['Scale'])

        links.new(mapping.outputs['Vector'], tex_node.inputs['Vector'])

        attr_r = nodes.new('ShaderNodeAttribute')
        attr_r.attribute_name = "mc_r"
        attr_r.attribute_type = attribute_source
        attr_r.location = (-500, 100)

        attr_g = nodes.new('ShaderNodeAttribute')
        attr_g.attribute_name = "mc_g"
        attr_g.attribute_type = attribute_source
        attr_g.location = (-500, 0)

        attr_b = nodes.new('ShaderNodeAttribute')
        attr_b.attribute_name = "mc_b"
        attr_b.attribute_type = attribute_source
        attr_b.location = (-500, -100)

        obj_info = nodes.new('ShaderNodeCombineXYZ')
        obj_info.location = (-300, 100)
        links.new(attr_r.outputs['Fac'], obj_info.inputs['X'])
        links.new(attr_g.outputs['Fac'], obj_info.inputs['Y'])
        links.new(attr_b.outputs['Fac'], obj_info.inputs['Z'])

        # ------------------------------------------------------------------
        # Branch: additive vs. standard shader graph
        # ------------------------------------------------------------------
        if is_additive:
            if output_node is None:
                output_node = nodes.new('ShaderNodeOutputMaterial')
                output_node.location = (650, 200)
            _build_additive_material(nodes, links, tex_node, obj_info, output_node)
        else:
            _build_standard_material(nodes, links, tex_node, obj_info, bsdf, is_alpha, is_lit, attribute_source)

    else:
        # No texture found — show a bright magenta placeholder so it is obvious
        if not is_additive and bsdf is not None and 'Base Color' in bsdf.inputs:
            bsdf.inputs['Base Color'].default_value = (1.0, 0.0, 1.0, 1.0)

    # Assign material to object
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)
    return mat
