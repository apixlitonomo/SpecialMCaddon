"""Blender-independent MiEx transport. Only the owning job thread calls request."""
import atexit
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time

VERSION = 'v2.0.0.experimental-28'
DOWNLOAD = 'https://github.com/BramStoutProductions/MiEx/releases/download/' + VERSION + '/MiEx.jar'
SHA256 = '30f7779c2216791863585e6251613ebbb5bf1f933eb095df355d87e80b67c406'
LOG_LINE = 4000


def bundled_runtime():
    folder = Path(__file__).resolve().parents[2] / 'assets' / 'miex'
    java = folder / 'jre' / 'bin' / 'java.exe'
    return str(folder / 'MiEx.jar'), str(java) if os.name == 'nt' else 'java'


class MiExError(RuntimeError):
    pass


def parse_line(line):
    if 'java.lang.OutOfMemoryError' in line:
        return 'error', 'MiEx ran out of Java memory. Try fewer resource packs or a smaller export, or raise Memory under More options, Runtime. See View log for details.'
    if line.startswith('[MSG] Locked world:'):
        return 'error', 'MiEx cannot access this world. Close it in Minecraft or choose a writable copy; see the log.'
    if line.strip() == '[INITIALISED]':
        return 'ready', None
    if line.startswith('[COMMAND] '):
        try:
            result = json.loads(line[10:])
        except ValueError as exc:
            raise MiExError('MiEx returned an invalid command response') from exc
        if not isinstance(result, dict) or result.get('status') not in {'success', 'error'}:
            raise MiExError('MiEx returned an unsupported command response')
        return 'result', result
    return 'log', line.rstrip()


class Session:
    def __init__(self, java, jar, folder, memory=4):
        self.jar = str(Path(jar).resolve())
        self.java = shutil.which(java) or java
        self.folder = Path(folder).resolve()
        self.memory = memory
        self.process = None
        self.events = queue.Queue()
        self.cancelled = threading.Event()
        self.log_lock = threading.Lock()
        self.lifecycle_lock = threading.Lock()
        self.log_path = self.folder / 'miex.log'
        self.readers = []
        self.in_request = False

    def _log(self, text):
        with self.log_lock:
            with self.log_path.open('a', encoding='utf-8') as stream:
                stream.write(text.rstrip() + '\n')

    def _read(self, pipe, protocol):
        try:
            for line in pipe:
                # Preview and map responses run to megabytes; the log keeps their start.
                self._log(line if len(line) <= LOG_LINE else line[:LOG_LINE] + ' … (%d characters)' % len(line))
                # JVM failures are also written to stderr, without a command
                # response. Surface them immediately instead of timing out.
                if protocol or 'java.lang.OutOfMemoryError' in line:
                    try:
                        kind, data = parse_line(line)
                        if kind != 'log':
                            self.events.put((kind, data))
                    except MiExError as exc:
                        self.events.put(('error', str(exc)))
        finally:
            pipe.close()
            if protocol:
                self.events.put(('error', 'MiEx stopped. Check Setup and View Log for details.'))

    def _wait(self, expected, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.cancelled.is_set():
                raise MiExError('Cancelled')
            try:
                kind, data = self.events.get(timeout=.1)
            except queue.Empty:
                continue
            if kind == 'error':
                raise MiExError(data)
            if kind == expected:
                if kind == 'result' and data['status'] == 'error':
                    raise MiExError(data.get('error') or 'MiEx command failed; see log')
                return data
            raise MiExError('Unexpected MiEx response; reconnect before retrying')
        raise MiExError('MiEx timed out; see log or cancel and retry')

    def start(self):
        if not Path(self.jar).is_file():
            raise MiExError('Bundled MiEx is missing. Reinstall the complete add-on ZIP or choose a MiEx.jar override')
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / 'resources').mkdir(exist_ok=True)
        self.log_path.write_text('', encoding='utf-8')
        bridge = Path(__file__).with_name('bridge.jar')
        from .worlds import launcher_roots
        roots = launcher_roots()
        environment = os.environ.copy()
        multimc = next((r['path'] for r in roots if r['kind']=='multimc'), None)
        if multimc:environment.setdefault('MIEX_MULTIMC_ROOT_DIR',multimc)
        # Exit on heap exhaustion: otherwise MiEx's Swing timer keeps a broken JVM alive,
        # holding its heap and burning CPU in garbage collection, and later requests hang.
        args = [self.java, '-Xmx%dG' % self.memory, '-XX:+ExitOnOutOfMemoryError', '-Dfile.encoding=UTF-8',
                '-cp', self.jar + os.pathsep + str(bridge), 'MCAddonMiEx',
                '-cli', '-commandStdIn', '-offlineMode', '-portableExports',
                '-storeGenTexInExport', '-rpDir', str(self.folder / 'resources'),
                '-log', str(self.folder / 'java.log')]
        with self.lifecycle_lock:
            if self.cancelled.is_set():
                raise MiExError('Cancelled')
            try:
                self.process = subprocess.Popen(args, cwd=self.folder, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                    errors='replace', bufsize=1, env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                atexit.register(self.close)
            except OSError as exc:
                raise MiExError('Cannot start Java. Install 64-bit Java 17+ or choose its executable: ' + str(exc)) from exc
        for pipe, protocol in ((self.process.stdout, True), (self.process.stderr, False)):
            thread = threading.Thread(target=self._read, args=(pipe, protocol), daemon=True)
            thread.start()
            self.readers.append(thread)
        self._wait('ready', 300)
        self.discover_worlds()

    def discover_worlds(self):
        from .worlds import launcher_roots, normalize_worlds
        return normalize_worlds(self.request('mcaddonDiscover', roots=launcher_roots()).get('worlds',[]))

    def request(self, command, timeout=180, **args):
        if self.cancelled.is_set():
            raise MiExError('Cancelled')
        payload = dict(command=command, **args)
        self.in_request = True
        try:
            try:
                self.process.stdin.write(json.dumps(payload, ensure_ascii=True) + '\n')
                self.process.stdin.flush()
            except (OSError, ValueError, AttributeError) as exc:
                raise MiExError('MiEx disconnected; retry the operation') from exc
            return self._wait('result', timeout)
        finally:
            self.in_request = False

    def close(self):
        atexit.unregister(self.close)
        self.cancelled.set()
        with self.lifecycle_lock:
            proc = self.process
            if proc is not None and proc.poll() is None:
                if not self.in_request:
                    try:
                        proc.stdin.write('{"command":"quit"}\n')
                        proc.stdin.flush()
                        proc.wait(timeout=.5)
                    except (OSError, ValueError, subprocess.TimeoutExpired):
                        pass
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
            if proc is not None and proc.stdin:
                proc.stdin.close()
        for reader in self.readers:
            reader.join(timeout=1)


def validate_region(region):
    for axis in 'XYZ':
        if region['min' + axis] > region['max' + axis]:
            raise MiExError('Minimum %s must not exceed maximum %s' % (axis, axis))
    return region
