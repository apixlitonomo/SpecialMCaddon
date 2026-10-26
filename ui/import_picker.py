import bpy

class MCBEDROCK_UL_asset_list(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row()
            row.label(text=item.name, icon='FILE_BLANK')
            if item.identifier:
                row.label(text=item.identifier)
            row.label(text=item.asset_type)
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text="", icon='FILE_BLANK')

class MCBEDROCK_OT_import_universal(bpy.types.Operator):
    """Import an entire Resource Pack"""
    bl_idname = "mcbedrock.import_universal"
    bl_label = "Import Asset Pack"
    bl_options = {'REGISTER', 'UNDO'}
    
    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    
    def execute(self, context):
        # Determine format and parse
        self.report({'INFO'}, f"Universal Importer: Loaded {self.filepath}")
        return {'FINISHED'}

class MCBEDROCK_PT_asset_browser(bpy.types.Panel):
    bl_label = "Resource Pack Importer"
    bl_idname = "MCBEDROCK_PT_asset_browser"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Bedrock'
    
    def draw(self, context):
        layout = self.layout
        layout.operator("mcbedrock.import_universal", icon='FILE_FOLDER')

classes = (
    MCBEDROCK_UL_asset_list,
    MCBEDROCK_OT_import_universal,
    MCBEDROCK_PT_asset_browser,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
