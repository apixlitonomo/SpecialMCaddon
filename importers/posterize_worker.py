"""One reusable headless Blender worker for a Posterize editing session.

Uses an atomically replaced job file and numeric NPZ arrays (never pickle).
Only the owning UI process touches its scene. A heartbeat prevents
orphan processes after a crash; source hierarchy survives settings changes.
A worker with nothing to do for ten minutes quits; the session starts
another for its next job.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import traceback

import numpy as np

IDLE_EXIT = 600.  # seconds without a new job
STALE_HEARTBEAT = 120.


def publish(source, target, attempts=40):
    """Replace ``target`` with ``source``. Windows refuses while the editor is
    reading the old file, which takes moments: try again instead of failing."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return True
        except PermissionError:
            if attempt + 1 < attempts:
                time.sleep(.025)
    return False


def run(directory):
    root = Path(directory)
    spec = importlib.util.spec_from_file_location('posterize_core', Path(__file__).resolve().parents[1]/'builders/posterize.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    hierarchy, source_id, previous, configuration = None, None, None, None
    unpublished = False
    idle_since = time.monotonic()
    while root.exists() and not (root/'stop').exists():
        try:
            beat = (root/'heartbeat').stat().st_mtime
        except OSError:
            break
        if time.time()-beat > STALE_HEARTBEAT:
            break
        if unpublished:
            unpublished = not publish(root/'result.tmp', root/'result.json', attempts=1)
        try:
            job = json.loads((root/'job.json').read_text())
            generation = job['generation']
        except (OSError, ValueError, KeyError, TypeError):
            time.sleep(.05)
            continue
        if generation == previous:
            if time.monotonic()-idle_since > IDLE_EXIT:
                break
            time.sleep(.05)
            continue
        previous = generation
        try:
            options = (job.get('groups'),job.get('lock_groups',True))
            if job['source'] != source_id or options != configuration:
                hierarchy = module.PaletteHierarchy(np.load(root/job['source'], allow_pickle=False),
                                                     groups=options[0],lock_groups=options[1])
                source_id = job['source']
                configuration = options
            mask = np.load(root/job['mask'], allow_pickle=False)
            output, stats = hierarchy.convert(job['strength'], job['cleanup'], job['locked'], mask, job['tileable'])
            path = root/f'result-{generation}.npz'
            temporary = root/f'result-{generation}.tmp.npz'
            group_map = np.full(mask.shape,-1,dtype=np.int32)
            group_map[hierarchy.visible] = hierarchy.owners[hierarchy.labels[hierarchy.visible]]
            np.savez(temporary, pixels=output, group_map=group_map)
            os.replace(temporary, path)
            result = dict(generation=generation, file=path.name, stats=stats)
        except Exception:
            result = dict(generation=generation, error=traceback.format_exc())
        try:
            (root/'result.tmp').write_text(json.dumps(result))
        except OSError:
            break  # the session's folder is gone or full; it reports the stopped worker
        unpublished = not publish(root/'result.tmp', root/'result.json')
        idle_since = time.monotonic()


if __name__ == '__main__':
    run(sys.argv[sys.argv.index('--')+1])
