"""Adapted MiEx SetupMaterials; upstream revision 9258afcc507289a9b41c098623bb67312de4dc3d.
Authors: michael212345, Bram Stout, Mahid Sheikh.
Changes: Blender 4.5/5.2 transparency API; fail on missing textures.

BSD 3-Clause License

Copyright (c) 2024, Bram Stout Productions

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""
import bpy
import os
import math
import warnings

class SetupMaterials:

    def __init__(self, mat: bpy.types.Material, data, rootDir, options):
        self.conn_to_make = []
        self.mat = mat        # delete all nodes in material
        self.mat.use_nodes = True
        self.data = data
        self.rootDir = rootDir
        self.hasTransparency = False
        self.useBackfaceCulling = True
        self.options = options

    def setup_materials(self):
        for node in self.mat.node_tree.nodes:
            self.mat.node_tree.nodes.remove(node)

        for name, node_data in self.data['network'].items():
            try:
                self.import_node(name,node_data)
            except Exception as e:
                warnings.warn('Failed to create node: ' + name, UserWarning)
                raise e

        for conn in self.conn_to_make:
            try:
                node0, attr0 = conn[0].split(".")
                node1, attr1 = conn[1].split(".")
                
                node0 = self.get_Node_By_Name(node0)
                node1 = self.get_Node_By_Name(node1)

                if self.is_number(attr0):
                    attr0 = int(attr0)
                if self.is_number(attr1):
                    attr1 = int(attr1)

                self.mat.node_tree.links.new(node0.outputs[attr0], node1.inputs[attr1])
            except Exception as e:
                warnings.warn('Failed to make connection: ' + str(conn), UserWarning)
                raise e

        for name, attr in self.data['terminals'].items():
            try:
                inputAttr = attr
                inputAttr = inputAttr.split("/")
                inputAttr = inputAttr[len(inputAttr)-1]
                output = self.get_output_node()
                self.mat.node_tree.links.new(self.get_Node_By_Name(inputAttr.split('.')[0]).outputs[inputAttr.split('.')[-1]], output.inputs[0])
            except Exception as e:
                warnings.warn('Failed to connect terminal: ' + name, UserWarning)
                raise e
        
        self.mat.use_backface_culling = self.useBackfaceCulling
        try:
            self.mat.use_backface_culling_shadow = self.useBackfaceCulling
        except:
            pass
        if hasattr(self.mat, 'surface_render_method'):
            self.mat.surface_render_method = 'DITHERED'
        elif hasattr(self.mat, 'blend_method'):
            self.mat.blend_method = 'HASHED'

        try:
            self.layoutNodes()
        except:
            pass

    def import_node(self,name,data):
        node = self.mat.node_tree.nodes.new(data['type'])
        node.name = name
        node.label = name
        isBsdf = 'Bsdf' in data['type']


        if 'attributes' in data:
            for attrName, attrData in data['attributes'].items():
                try:
                    if isBsdf and attrName == "Alpha":
                        self.hasTransparency = True
                    if isBsdf and attrName == "Backface Culling":
                        self.useBackfaceCulling = bool(attrData['value'])
                        continue

                    if attrData["type"] == "asset":
                        imagePath = attrData['value']
                        if not os.path.exists(imagePath):
                            imagePath = os.path.join(self.rootDir, imagePath)
                            if not os.path.exists(imagePath):
                                raise FileNotFoundError('MiEx texture not found: ' + imagePath)
                        node.image = bpy.data.images.load(imagePath)
                        if 'colorspace_settings' in data['attributes']:
                            if isinstance(data['attributes']['colorspace_settings']['value'], list):
                                for colorspace in data['attributes']['colorspace_settings']['value']:
                                    try:
                                        node.image.colorspace_settings.name = colorspace
                                        break
                                    except:
                                        pass
                            else:
                                try:
                                    node.image.colorspace_settings.name = data['attributes']['colorspace_settings']['value']
                                except:
                                    pass
                        if 'alpha_mode' in data['attributes']:
                            try:
                                node.image.alpha_mode = data['attributes']['alpha_mode']['value']
                            except:
                                pass
                            
                    elif 'value' in attrData:
                        if hasattr(node, attrName):
                            setattr(node, attrName, attrData['value'])
                        elif attrName in node.inputs:
                            node.inputs[attrName].default_value = attrData['value']
                        elif self.is_number(attrName):
                            node.inputs[int(attrName)].default_value = attrData['value']
                        else:
                            node[attrName] = attrData['value']
                    elif 'connection' in attrData:
                        inputAttr = attrData["connection"]
                        inputAttr = inputAttr.split("/")
                        inputAttr = inputAttr[len(inputAttr)-1]
                        self.conn_to_make.append((inputAttr, node.name + "." + attrName))

                    elif "keyframes" in attrData:
                        keyframes = attrData["keyframes"]
                        numFrames = len(keyframes) // 2
                        i = 0
                        inputobject = node.inputs[attrName]
                        if attrData["type"] == "Float":
                            while i < numFrames:
                                
                                if i >= self.options['max_animation_frames']:
                                    break
                                
                                inputobject.default_value = keyframes[i * 2 + 1]
                                inputobject.keyframe_insert(data_path="default_value", frame=math.floor(keyframes[i * 2]*10.0)/10.0)
                                
                                i += 1  
                except Exception as e:
                    warnings.warn('Failed to set attribute: ' + attrName, UserWarning)
                    raise e

    def get_output_node(self):
        material_output = None
        for node in self.mat.node_tree.nodes:
            if node.type == "OUTPUT_MATERIAL":
                material_output = node
                break
        if material_output is None:
            material_output = self.mat.node_tree.nodes.new('ShaderNodeOutputMaterial')
        return material_output

    def is_number(self, inputString:str):
        try:
            int(inputString)
            return True
        except ValueError:
            return False
    
    def get_Node_By_Name(self, name:str):
        if(len(name) > 63):
            name = name[:63]
        for node in self.mat.node_tree.nodes:
            if node.name == name:
                return node
        raise Exception("Node not found: " + name)
    
    def layoutNodes(self):
        class Node:

            def __init__(self, node: bpy.types.Node, column:int, row:int) -> None:
                self.node : bpy.types.Node = node
                self.column = column
                self.row = row
                self.outputs : list[Node] = []
        
        class NodeGrid:

            def __init__(self) -> None:
                self.nodes : list[Node] = []
                self.minColumn = 0
                self.maxColumn = 0
            
            def getColumn(self, column: int) -> list[Node]:
                res : list[Node] = []
                for node in self.nodes:
                    if node.column == column:
                        res.insert(node.row, node)
                return res

            def getNode(self, node: bpy.types.Node) -> Node:
                for node2 in self.nodes:
                    if node2.node.name == node.name:
                        return node2
                node2 = Node(node, 0, len(self.getColumn(0)))
                self.nodes.append(node2)
                return node2
            
            def getNodeIfMissing(self, node: bpy.types.Node) -> Node|None:
                for node2 in self.nodes:
                    if node2.node.name == node.name:
                        return None
                node2 = Node(node, 0, len(self.getColumn(0)))
                self.nodes.append(node2)
                return node2
            
            def setColumn(self, node:Node, column: int):
                if node.column >= column:
                    return
                prevColumn = node.column
                prevRow = node.row
                node.row = len(self.getColumn(column))
                node.column = column
                for node in self.nodes:
                    if node.column == prevColumn and node.row > prevRow:
                        node.row -= 1
                self.minColumn = min(self.minColumn, column)
                self.maxColumn = max(self.maxColumn, column)
        
        def placeInColumns(node: bpy.types.Node, nodeGrid: NodeGrid, column: int):
            node2 = nodeGrid.getNode(node)
            nodeGrid.setColumn(node2, column)

            for i in range(len(node.inputs)): # type: ignore
                if node.inputs[i].is_linked:
                    link : bpy.types.NodeLink = node.inputs[i].links[0]
                    placeInColumns(link.from_node, nodeGrid, column + 1)
        
        def placeRemainingInColumns(nodeGrid : NodeGrid):
            column = nodeGrid.maxColumn + 1
            for node in self.mat.node_tree.nodes:
                node2 = nodeGrid.getNodeIfMissing(node)
                if node2 is not None:
                    nodeGrid.setColumn(node2, column)

        def placeNodes(nodeGrid : NodeGrid):
            xPos = 0
            for columnI in range(nodeGrid.minColumn, nodeGrid.maxColumn+1):
                column = nodeGrid.getColumn(columnI)
                yPos = 0
                maxWidth = 0

                for node in column:
                    node.node.location = (xPos, yPos)
                    yPos += max(node.node.height, node.node.bl_height_default) + 125
                    maxWidth = max(maxWidth, node.node.width, node.node.bl_width_default)
                
                xPos -= maxWidth + 125


        nodeGrid = NodeGrid()
        placeInColumns(self.get_output_node(), nodeGrid, 0)
        placeRemainingInColumns(nodeGrid)

        placeNodes(nodeGrid)
            

