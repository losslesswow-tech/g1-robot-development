"""Stage/install task integration without starting or stopping any robot process."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import time

RELEASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RELEASE))
from navigation_task.patch_task import patch_task
from navigation_task.patch_frontend import patch_frontend

SCRIPTS = Path('/catkin_ws/src/x_nav_control/scripts')
ASSET = 'dist/assets/rviz-lv-JGMcm.js'
HTML = 'dist/app/rviz.html'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def replace(path, data):
    temporary = path.with_name(path.name + '.g1-task-new')
    with open(temporary, 'xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if path.exists():
        shutil.copymode(path, temporary)
    os.replace(str(temporary), str(path))


def stage(scripts=SCRIPTS):
    task = (scripts / 'task.py').read_bytes()
    expected = (RELEASE / 'navigation_task/upstream/task.py').read_bytes()
    if digest(task) != digest(expected):
        raise RuntimeError('task.py differs from reviewed source; stage aborted')
    asset = (scripts / ASSET).read_bytes()
    shim = ('import sys\nsys.path.insert(0, ' + repr(str(RELEASE)) + ')\n'
            'from arrival_voice.task_voice import TaskVoice, register_voice_routes, validate_speech_steps\n').encode()
    frontend = patch_frontend(asset.decode('utf-8')).encode('utf-8')
    html = (scripts / HTML).read_text(encoding='utf-8')
    from navigation_task.patch_task import once
    html = once(html, 'src="/assets/rviz-lv-JGMcm.js"',
                'src="/assets/rviz-lv-JGMcm.js?g1-task=' + digest(frontend)[:12] + '"')
    files = {'task.py': patch_task(task.decode('utf-8')).encode('utf-8'),
             ASSET: frontend, HTML: html.encode('utf-8'),
             'g1_task_voice.py': shim}
    prepared = RELEASE / 'prepared'
    prepared.mkdir(exist_ok=False)
    manifest = {'release': str(RELEASE), 'scripts': str(scripts), 'files': {}, 'modules': {}}
    for name, data in files.items():
        target = scripts / name
        if name == 'g1_task_voice.py' and target.exists():
            raise RuntimeError('existing g1_task_voice.py requires an explicit upgrade review')
        output = prepared / name
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
        manifest['files'][name] = {'before': digest(target.read_bytes()) if target.exists() else None,
                                   'after': digest(data)}
    for filename in (RELEASE / 'arrival_voice').rglob('*'):
        if filename.is_file() and filename.suffix in {'.py', '.js', '.html', '.css'}:
            manifest['modules'][str(filename.relative_to(RELEASE))] = digest(filename.read_bytes())
    (prepared / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return manifest


def stopped():
    try:
        connection = socket.create_connection(('127.0.0.1', 9000), timeout=1)
    except OSError:
        pass
    else:
        connection.close()
        raise RuntimeError('navigation Web API is running; controlled reload must be arranged first')
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            arguments = (process / 'cmdline').read_bytes().split(b'\0')
        except OSError:
            continue
        if b'arrival_voice.legacy_auto' in arguments:
            raise RuntimeError('passive arrival listener is running; stop only that module before installation')


def install():
    stopped()
    prepared = RELEASE / 'prepared'
    manifest = json.loads((prepared / 'manifest.json').read_text(encoding='utf-8'))
    if manifest['release'] != str(RELEASE) or manifest['scripts'] != str(SCRIPTS):
        raise RuntimeError('unexpected installation roots')
    for name, expected in manifest['modules'].items():
        if digest((RELEASE / name).read_bytes()) != expected:
            raise RuntimeError('release changed after staging: ' + name)
    for name, hashes in manifest['files'].items():
        target = SCRIPTS / name
        before = digest(target.read_bytes()) if target.exists() else None
        if before != hashes['before'] or digest((prepared / name).read_bytes()) != hashes['after']:
            raise RuntimeError('file changed after staging: ' + name)
    backup = Path('/opt/g1-arrival-voice/state/backups') / ('task-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))
    backup.mkdir(parents=True, exist_ok=False)
    shutil.copy2(prepared / 'manifest.json', backup / 'manifest.json')
    for name, hashes in manifest['files'].items():
        if hashes['before'] is not None:
            destination = backup / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SCRIPTS / name, destination)
    if Path('/nav_map/task.json').exists():
        shutil.copy2('/nav_map/task.json', backup / 'task.json.snapshot')
    changed = []
    try:
        for name in manifest['files']:
            replace(SCRIPTS / name, (prepared / name).read_bytes())
            changed.append(name)
    except Exception:
        for name in reversed(changed):
            if manifest['files'][name]['before'] is None:
                (SCRIPTS / name).unlink()
            else:
                replace(SCRIPTS / name, (backup / name).read_bytes())
        raise
    return {'installed': True, 'backup': str(backup), 'processes_started': False,
            'next': 'reload original Web process under field supervision; start updated passive listener'}


def rollback(backup):
    stopped()
    backup = Path(backup).resolve()
    base = Path('/opt/g1-arrival-voice/state/backups').resolve()
    if base not in backup.parents:
        raise RuntimeError('backup must be inside the module backup directory')
    manifest = json.loads((backup / 'manifest.json').read_text(encoding='utf-8'))
    allowed = {'task.py', ASSET, 'g1_task_voice.py'}
    if manifest['scripts'] != str(SCRIPTS) or set(manifest['files']) not in (allowed, allowed | {HTML}):
        raise RuntimeError('unexpected rollback targets')
    for name, hashes in manifest['files'].items():
        if digest((SCRIPTS / name).read_bytes()) != hashes['after']:
            raise RuntimeError('installed file changed; rollback aborted: ' + name)
        if hashes['before'] is not None and digest((backup / name).read_bytes()) != hashes['before']:
            raise RuntimeError('backup hash mismatch: ' + name)
    for name, hashes in manifest['files'].items():
        if hashes['before'] is None:
            (SCRIPTS / name).unlink()
        else:
            replace(SCRIPTS / name, (backup / name).read_bytes())
    return {'rolled_back': True, 'task_data_restored': False, 'processes_started': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['stage', 'install', 'rollback'])
    parser.add_argument('--backup')
    args = parser.parse_args()
    if args.operation == 'rollback' and not args.backup:
        parser.error('--backup is required for rollback')
    result = stage() if args.operation == 'stage' else install() if args.operation == 'install' else rollback(args.backup)
    print(json.dumps(result, ensure_ascii=False, indent=2))
