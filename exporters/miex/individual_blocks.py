"""Merge MiEx's repeated individual-block models before Blender imports an export.

With Individual blocks (MiEx's ``onlyIndividualBlocks``) every block is one point of
a USD PointInstancer, and blocks whose shape depends on their neighbours (water and
lava above all) get a model of their own per position: 95,637 models for 128 x 128
blocks of a test world, of which 411 differ. Blender makes a collection and objects
per model, which took about two minutes. ``merge`` writes a copy beside the export
with each distinct model once and every instancer's ``protoIndices`` pointed at it,
so each block keeps its position and look; MiEx's own files are not changed.

MiEx writes ``world.usd`` as text in a fixed layout (``USDWriter``: four-space
indents, one prim per ``def`` line, one attribute per line, LF line ends). Models are
compared as their exact text without their name, so only byte-identical models merge.
Only the small remainder is parsed with Sdf; the instancer arrays are parsed with
numpy. Anything unexpected returns ``None`` and the original export is imported.
"""
from pathlib import Path
import re
import time
import uuid

import numpy as np

SCOPE = b'\n    def Scope "individualBlocksBaseMeshes"\n    {'
SCOPE_END = b'\n    }\n'
MODEL = b'\n        def '
NESTED = b'\n            def '
BASE = '/world/individualBlocksBaseMeshes/'
PRIM = re.compile(rb'( *)(?:def|over|class)(?: [A-Za-z_]\w*)? "([^"]+)"\(?')
VARIANT = re.compile(rb'( *)(?:variantSet "[^"]*" = |"[^"]*" )\{')
TARGET = re.compile(rb'<([^>]*)>')
BLOCK = re.compile(r'_class_(.+?)\d+_N?\d+_N?\d+_N?\d+_\d+')
ROWS = (b'rel prototypes = [', b'point3f[] positions = [', b'int[] protoIndices = [')


class Merged:
    """The merged copy: ``instancers[prim path] = (labels, protoIndices)``, one label per kept model.

    prototypes: models MiEx wrote; models: distinct ones; kept: distinct ones in use.
    """

    def __init__(self, path, prototypes, models, kept, instancers, seconds):
        self.path, self.prototypes, self.models, self.kept = path, prototypes, models, kept
        self.instancers, self.seconds = instancers, seconds


def label(name, text):
    """Readable model name: its block without ``minecraft_``, or ``empty`` for a model with no mesh."""
    if NESTED not in text:
        return 'empty'
    match = BLOCK.fullmatch(name)
    block = match.group(1) if match else name
    return block[10:] if block.startswith('minecraft_') else block


def instancers(lines):
    """PointInstancers outside variants: {prim path: (definition line, closing line)}."""
    stack, found, current = [], {}, None
    for number, line in enumerate(lines):
        if current and line == current[1]:
            found[current[0]] = (current[2], number)
            current = None
            continue
        prim = PRIM.fullmatch(line)
        variant = None if prim else VARIANT.fullmatch(line)
        if not (prim or variant):
            continue
        indent = len((prim or variant).group(1))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, prim.group(2).decode() if prim else None))
        if prim and b' PointInstancer "' in line and all(name for _, name in stack):
            current = ('/' + '/'.join(name for _, name in stack), b' ' * indent + b'}', number)
    return found


def array(line):
    """The values of a one-line ``type name = [...]`` attribute (``int[]`` has its own bracket)."""
    return line[line.index(b'= [') + 3:line.rindex(b']')]


def merge(path):
    """Write the merged copy beside ``path``; a Merged, or None to import ``path`` as it is.

    The copy (``<stem>_individual_<id>.usdc``) sits beside the export so its relative
    texture and payload paths still resolve; the importer deletes it afterwards.
    """
    from pxr import Sdf, Vt
    start = time.perf_counter()
    path = Path(path)
    data = path.read_bytes()
    if b'\r\n' in data[:4096]:
        data = data.replace(b'\r\n', b'\n')
    opened = data.find(SCOPE) if data.startswith(b'#usda') else -1
    closed = data.find(SCOPE_END, opened) if opened >= 0 else -1
    if closed < 0:
        return None
    # Only world.usd is rewritten, so no other layer may instance these models.
    for other in path.parent.rglob('*.usd*'):
        if (other != path and not other.name.startswith(path.stem + '_individual_')
                and b'individualBlocksBaseMeshes' in other.read_bytes()):
            return None
    body = opened + len(SCOPE)
    chunks = data[body:closed].split(MODEL)
    if chunks[0].strip():
        return None
    by_text, model_of, models = {}, {}, []
    for chunk in chunks[1:]:
        q1 = chunk.find(b'"') + 1
        q2 = chunk.find(b'"', q1)
        if q1 <= 0 or q2 < 0:
            return None
        name = chunk[q1:q2].decode()
        text = chunk[:q1] + chunk[q2:]
        model = by_text.get(text)
        if model is None:
            model = by_text[text] = (name, label(name, chunk), len(models), chunk)
            models.append(model)
        model_of[name] = model
    # Kept models are numbered by block, then by first appearance.
    rank = {model[0]: index for index, model in enumerate(sorted(models, key=lambda m: (m[1], m[2])))}
    head = data[:body].split(b'\n')
    lines = head + data[closed:].split(b'\n')
    arrays, used = {}, set()
    for prim_path, (begin, end) in instancers(lines).items():
        rows = {row: number for number in range(begin + 1, end)
                for row in ROWS if lines[number].lstrip(b' ').startswith(row)}
        if len(rows) != len(ROWS):
            return None
        paths = [t.decode() for t in TARGET.findall(lines[rows[ROWS[0]]])]
        if not paths:
            continue
        if any(not t.startswith(BASE) or t[len(BASE):] not in model_of for t in paths):
            return None
        targets = [t[len(BASE):] for t in paths]
        index_text = array(lines[rows[ROWS[2]]])
        if not index_text.strip():
            return None
        indices = np.fromstring(index_text, dtype=np.int64, sep=',')
        # Doubles rounded to float, as USD's own text reader does (a malformed number raises).
        points = np.array(array(lines[rows[ROWS[1]]]).translate(None, b'()').split(b','),
                          dtype=np.float64).astype(np.float32)
        if (len(indices) != index_text.count(b',') + 1 or len(points) != 3 * len(indices)
                or not 0 <= indices.min() <= indices.max() < len(targets)):
            return None
        kept = sorted({model_of[target][0] for target in targets}, key=rank.__getitem__)
        slot = {name: index for index, name in enumerate(kept)}
        remap = np.array([slot[model_of[target][0]] for target in targets], dtype=np.int32)
        arrays[prim_path] = (remap[indices], points.reshape(-1, 3), [model_of[name][1] for name in kept])
        used.update(kept)
        line = lines[rows[ROWS[0]]]
        indent = line[:len(line) - len(line.lstrip(b' '))]
        lines[rows[ROWS[0]]] = indent + ROWS[0] + b','.join(b'<' + (BASE + name).encode() + b'>' for name in kept) + b']'
        # Set below from numpy rather than parsed again as text.
        lines[rows[ROWS[1]]] = indent + ROWS[1] + b']'
        lines[rows[ROWS[2]]] = indent + ROWS[2] + b']'
    if not arrays:
        return None
    kept_models = b''.join(MODEL + model[3] for model in models if model[0] in used)
    text = b'\n'.join(lines[:len(head)]) + kept_models + b'\n'.join(lines[len(head):])
    layer = Sdf.Layer.CreateAnonymous('.usda')
    if not layer.ImportFromString(text.decode('utf-8')):
        return None
    for prim_path, (indices, points, labels) in arrays.items():
        indices_spec = layer.GetAttributeAtPath(prim_path + '.protoIndices')
        points_spec = layer.GetAttributeAtPath(prim_path + '.positions')
        if indices_spec is None or points_spec is None:
            return None
        indices_spec.default = Vt.IntArray.FromNumpy(np.ascontiguousarray(indices, dtype=np.int32))
        points_spec.default = Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(points))
    target = path.with_name('%s_individual_%s.usdc' % (path.stem, uuid.uuid4().hex[:8]))
    if not layer.Export(str(target)):
        return None
    return Merged(target, len(model_of), len(models), len(used),
                  {prim_path: (labels, indices) for prim_path, (indices, points, labels) in arrays.items()},
                  time.perf_counter() - start)
