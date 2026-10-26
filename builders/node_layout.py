"""Readable layouts for the node trees Sparkle builds.

Builders create nodes at the origin, so an opened Sparkle material, world,
group or compositor setup used to show every node stacked in one spot. The
first time such a tree appears in a node editor it is laid out in columns:
outputs on the right, each node beside what it feeds. Nodes someone already
placed stay where they are; Sparkle's unplaced nodes then go in a frame
below them.
"""

import bpy

GAP_X = 80.
GAP_Y = 30.
FRAME_LABEL = 'Sparkle'
_PREFIXES = ('mc ', 'mc_', 'mcbedrock', 'sparkle')


def _at_origin(node):
    return abs(node.location.x) < 1e-3 and abs(node.location.y) < 1e-3


def _sparkle_node(node):
    return node.name.lower().startswith(_PREFIXES) or node.label.lower().startswith(_PREFIXES)


def _layout_nodes(tree):
    return [node for node in tree.nodes if node.bl_idname != 'NodeFrame']


def stacked(tree):
    """Sparkle's nodes (and unplaced ones) when two or more share one spot."""
    spots = {}
    for node in _layout_nodes(tree):
        if node.parent is None and (_at_origin(node) or _sparkle_node(node)):
            spots.setdefault((round(node.location.x), round(node.location.y)), []).append(node)
    if not any(len(nodes) > 1 for nodes in spots.values()):
        return []
    return [node for nodes in spots.values() for node in nodes]


def _size(node, scale):
    """Width and height in node-editor units. Drawn nodes know their size."""
    width, height = node.dimensions
    if width > 1 and height > 1:
        return width / scale, height / scale
    if node.bl_idname == 'NodeReroute':
        return 16., 16.
    if node.hide:
        return node.width, 32.
    rows = sum(1 for socket in node.outputs if socket.enabled and not socket.hide)
    rows += sum(1 for socket in node.inputs if socket.enabled and not socket.hide)
    extra = {'ShaderNodeValToRGB': 130, 'ShaderNodeTexImage': 170, 'CompositorNodeImage': 170,
             'ShaderNodeRGBCurve': 220, 'ShaderNodeVectorCurve': 220, 'ShaderNodeFloatCurve': 220,
             'CompositorNodeCurveRGB': 220, 'ShaderNodeRGB': 170, 'ShaderNodeValue': 10,
             'ShaderNodeMix': 30, 'ShaderNodeMixRGB': 30, 'ShaderNodeMath': 20, 'ShaderNodeVectorMath': 20}
    return node.width, 44. + 22. * rows + extra.get(node.bl_idname, 0)


def _graph(tree, nodes):
    members = set(nodes)
    consumers = {node: [] for node in nodes}
    sources = {node: [] for node in nodes}
    linked = set()
    for link in tree.links:
        a, b = link.from_node, link.to_node
        linked.update((a, b))
        if a == b or a not in members or b not in members:
            continue
        index = next((i for i, socket in enumerate(b.inputs) if socket == link.to_socket), 0)
        consumers[a].append((b, index / max(1, len(b.inputs))))
        sources[b].append(a)
    return consumers, sources, linked


def _ranks(nodes, consumers, linked):
    """Column of each node, counted from the outputs: one more than the
    farthest column of anything it feeds."""
    rank = {}
    for node in nodes:
        stack = [(node, False)]
        while stack:
            current, done = stack.pop()
            if current in rank:
                continue
            pending = [d for d, _ in consumers[current] if d not in rank]
            if done or not pending:
                rank[current] = max((rank.get(d, -1) for d, _ in consumers[current]), default=-1) + 1
                continue
            stack.append((current, True))
            # A broken (cyclic) tree must not loop forever.
            if len(stack) < 4 * len(nodes):
                stack.extend((d, False) for d in pending)
    lonely = [node for node in nodes if node not in linked]
    if lonely and len(lonely) < len(nodes):
        # Unconnected helpers sit in their own column at the far left.
        far = max(rank.values()) + 1
        for node in lonely:
            rank[node] = far
    return rank


def _place(nodes, consumers, linked, scale):
    """Locations for nodes, outputs at x=0 and the inputs to their left."""
    consumers_rank = _ranks(nodes, consumers, linked)
    columns = {}
    for node in nodes:
        columns.setdefault(consumers_rank[node], []).append(node)
    sizes = {node: _size(node, scale) for node in nodes}
    order = {node: index for index, node in enumerate(nodes)}
    top, left = {}, {}
    x = 0.
    for column in sorted(columns):
        members = columns[column]
        width = max(sizes[node][0] for node in members)
        x -= width + (GAP_X if column else 0.)
        # Line nodes up with the sockets they feed; leave the rest in build order.
        def wanted(node):
            spots = [top[d] - (fraction * sizes[d][1]) for d, fraction in consumers[node] if d in top]
            return sum(spots) / len(spots) if spots else None
        desire = {node: wanted(node) for node in members}
        members.sort(key=lambda node: (-desire[node] if desire[node] is not None else float('inf'), order[node]))
        floor = None
        for node in members:
            goal = desire[node] if desire[node] is not None else (floor if floor is not None else 0.)
            if floor is not None:
                goal = min(goal, floor)
            top[node] = goal
            left[node] = x + (width - sizes[node][0])
            floor = goal - sizes[node][1] - GAP_Y
    return {node: (left[node], top[node]) for node in nodes}, sizes


def arrange(tree, scale=None):
    """Lay out a tree Sparkle built. Returns how many nodes moved."""
    if scale is None:
        scale = getattr(bpy.context.preferences.system, 'ui_scale', 1.) or 1.
    loose = stacked(tree)
    if not loose:
        return 0
    frame = tree.nodes.get(FRAME_LABEL)
    if frame is not None and frame.bl_idname == 'NodeFrame':
        # Lay out earlier and new Sparkle nodes together in the one frame.
        for node in _layout_nodes(tree):
            if node.parent == frame:
                node.parent = None
                loose.append(node)
    everything = [node for node in _layout_nodes(tree) if node.parent is None]
    whole = len(loose) >= .8 * len(everything)
    nodes = everything if whole else loose
    moving = set(nodes)
    placed = [node for node in everything if node not in moving]
    consumers, _sources, linked = _graph(tree, nodes)
    spots, sizes = _place(nodes, consumers, linked, scale)
    if whole or not placed:
        for node, (x, y) in spots.items():
            node.location = (x, y)
        if frame is not None and frame.bl_idname == 'NodeFrame' and not any(n.parent == frame for n in tree.nodes):
            tree.nodes.remove(frame)
        return len(nodes)
    # Keep the placed nodes; put Sparkle's in a labelled frame underneath them.
    def bounds(points):
        return (min(p[0] for p in points), max(p[0] for p in points),
                min(p[1] for p in points), max(p[1] for p in points))
    corners = [(n.location.x, n.location.y) for n in placed]
    corners += [(n.location.x + _size(n, scale)[0], n.location.y - _size(n, scale)[1]) for n in placed]
    _, right, bottom, _ = bounds(corners)
    block = [(x, y) for x, y in spots.values()] + [(x + sizes[n][0], y - sizes[n][1]) for n, (x, y) in spots.items()]
    _, block_right, _, block_top = bounds(block)
    shift = (right - block_right, bottom - 120. - block_top)
    if frame is None or frame.bl_idname != 'NodeFrame':
        frame = tree.nodes.new('NodeFrame')
        frame.label = frame.name = FRAME_LABEL
    frame.location = (0., 0.)
    frame.shrink = True
    for node, (x, y) in spots.items():
        node.location = (x + shift[0], y + shift[1])
        node.parent = frame
    return len(nodes)


def _owned(tree, owner):
    """Only trees Sparkle made or extended are rearranged."""
    def marked(item):
        name = getattr(item, 'name', '').lower()
        return name.startswith(_PREFIXES) or any(key.lower().startswith(_PREFIXES) for key in item.keys())
    if marked(tree) or (owner is not None and owner != tree and marked(owner)):
        return True
    return any(node.name.lower().startswith(_PREFIXES) for node in stacked(tree))


_checked = {}   # tree pointer -> node count when last looked at
_queue = []


def _tidy_queued():
    while _queue:
        tree = _queue.pop()
        try:
            if arrange(tree):
                for window in bpy.context.window_manager.windows:
                    for area in window.screen.areas:
                        if area.type == 'NODE_EDITOR':
                            area.tag_redraw()
        except ReferenceError:
            pass
    return None


def _look(*_args):
    """Node editor draw: lay out a cluttered Sparkle tree once it is shown.

    Arranging waits for a timer: drawing must not change data, and by then
    Blender knows each node's drawn size.
    """
    space = bpy.context.space_data
    tree = getattr(space, 'edit_tree', None)
    if tree is None:
        return
    key = tree.as_pointer()
    if _checked.get(key) == len(tree.nodes):
        return
    _checked[key] = len(tree.nodes)
    if stacked(tree) and _owned(tree, getattr(space, 'id', None)) and tree not in _queue:
        _queue.append(tree)
        if not bpy.app.timers.is_registered(_tidy_queued):
            bpy.app.timers.register(_tidy_queued, first_interval=0.)


_handle = None


@bpy.app.handlers.persistent
def _forget(*_args):
    _checked.clear()
    _queue.clear()


def register():
    global _handle
    if _handle is None:
        _handle = bpy.types.SpaceNodeEditor.draw_handler_add(_look, (), 'WINDOW', 'POST_PIXEL')
    for handlers in (bpy.app.handlers.load_post, bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _forget not in handlers:
            handlers.append(_forget)


def unregister():
    global _handle
    if _handle is not None:
        bpy.types.SpaceNodeEditor.draw_handler_remove(_handle, 'WINDOW')
        _handle = None
    for handlers in (bpy.app.handlers.load_post, bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _forget in handlers:
            handlers.remove(_forget)
    if bpy.app.timers.is_registered(_tidy_queued):
        bpy.app.timers.unregister(_tidy_queued)
    _forget()
