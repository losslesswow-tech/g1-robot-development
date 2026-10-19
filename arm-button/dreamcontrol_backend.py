"""Local console process adapter; no connection or movement during construction."""
from collections import deque
import json
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'dreamcontrol-button/live'))
from action_flow import DEFAULT_TARGET, NODES, TARGET_BOUNDS, validate_target


class Rejected(ValueError):
    pass


class ConsoleDriver:
    def __init__(self, backend='mock', runtime=None, ssh_key=None, audit=None):
        self.backend, self.runtime, self.ssh_key = backend, runtime, ssh_key
        self.audit = audit or (lambda event, **data: None)
        self.process = None
        self.pending_start = False
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.value = dict(active=False, connected=False, phase='idle', target_xyz_m=DEFAULT_TARGET)

    def launch(self):
        if self.process and self.process.poll() is None:
            return
        if self.backend == 'ssh':
            config = self.runtime
            if (config.get('host'), config.get('user')) != ('192.0.2.1', 'operator'):
                raise Rejected('unexpected_robot_development_host')
            command = 'exec ' + ' '.join(shlex.quote(config[k]) for k in ('python', 'worker_path'))
            command += ' --runtime ' + shlex.quote(config['runtime_path'])
            argv = ['C:/Windows/System32/OpenSSH/ssh.exe', '-i', str(self.ssh_key),
                    '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
                    '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=8',
                    'operator@192.0.2.1', command]
        else:
            argv = [sys.executable, '-u', str(ROOT.parent / 'dreamcontrol-button/live/console_worker.py'), '--mock']
        directory = ROOT.parent / 'artifacts/dreamcontrol-console-20261015'
        directory.mkdir(parents=True, exist_ok=True)
        self.stderr = (directory / ('worker-stderr-' + str(time.time_ns()) + '.log')).open('w', encoding='utf-8')
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.stderr, text=True, encoding='utf-8', bufsize=1, cwd=directory)
        self.reader = threading.Thread(target=self.read, args=(self.process,), daemon=True)
        self.reader.start()
        self.audit('worker_launched', backend=self.backend)

    def read(self, process):
        for line in process.stdout:
            try:
                event = json.loads(line)
                with self.lock:
                    if event.get('type') == 'status':
                        if not self.pending_start or event.get('active'):
                            self.value = event
                            self.pending_start = False
                    elif event.get('type') == 'rejected':
                        self.value['last_error'] = event['error']
                        if event.get('action') == 'start':
                            self.pending_start = False
                            self.value.update(active=False, phase='fault', error=event['error'])
                self.audit('worker_event', value=event)
            except (ValueError, TypeError):
                self.audit('worker_invalid_output')
        with self.lock:
            if self.value.get('active'):
                self.value.update(active=False, phase='fault', error='worker_disconnected')
            self.pending_start = False
            self.value['connected'] = False
        self.audit('worker_exited', returncode=process.wait())

    def state(self):
        with self.lock:
            return dict(self.value)

    def send(self, value):
        if not self.process or self.process.poll() is not None:
            raise Rejected('connect_readonly_first')
        if value['action'] == 'start':
            with self.lock:
                self.pending_start = True
                self.value.update(active=True, phase='queued', last_error=None, error=None,
                                  reason=None, policy_elapsed_s=0, policy_samples=0, node=None)
        with self.write_lock:
            self.process.stdin.write(json.dumps(value, allow_nan=False) + '\n')
            self.process.stdin.flush()

    def close(self):
        if not self.process:
            return
        process = self.process
        if process.poll() is None:
            try:
                self.send(dict(action='quit'))
                process.stdin.close()
                process.wait(timeout=7)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=3)
        self.reader.join(timeout=2)
        self.stderr.close()
        self.process = None


class ActionService:
    def __init__(self, backend='mock', runtime=None, ssh_key=None, allow_control=False, audit_path=None, driver=None):
        self.backend, self.allow_control = backend, allow_control
        self.audit_path = Path(audit_path) if audit_path else None
        self.audit_lock = threading.Lock()
        self.lock = threading.RLock()
        self.target = list(DEFAULT_TARGET)
        self.owner = None
        self.seen = deque(maxlen=1024)
        self.driver = driver or ConsoleDriver(backend, runtime, ssh_key, self.audit)

    def audit(self, event, **fields):
        if self.audit_path:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_lock, self.audit_path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(dict(time=time.time(), event=event, **fields),
                                        ensure_ascii=False, allow_nan=False) + '\n')

    def config(self):
        return dict(backend=self.backend, control_enabled=self.allow_control or self.backend == 'mock',
                    target_xyz_m=self.target, target_bounds_m=TARGET_BOUNDS, nodes=NODES,
                    hold_seconds=20, heartbeat_seconds=3, session_seconds=90)

    def state(self, page=None):
        with self.lock:
            value = self.driver.state()
            if not value.get('active') and value.get('phase') not in ('queued', 'starting'):
                self.owner = None
            value.update(backend=self.backend, control_enabled=self.config()['control_enabled'],
                         owned_by_this_page=bool(page and self.owner == page),
                         another_page_owns_control=bool(self.owner and self.owner != page))
            return value

    def action(self, page, action, data):
        with self.lock:
            request = data.get('request_id')
            if not isinstance(request, str) or len(request) != 32 or any(c not in '0123456789abcdef' for c in request):
                raise Rejected('invalid_request_id')
            if request in self.seen:
                raise Rejected('duplicate_request_rejected')
            self.seen.append(request)
            state = self.state(page)
            if action == 'configure':
                if state.get('active'):
                    raise Rejected('target_locked_during_action')
                self.target = validate_target(data.get('target_xyz_m'))
                self.audit('target_configured', target_xyz_m=self.target)
                return self.config()
            if action == 'connect':
                if state.get('active'):
                    raise Rejected('release_before_connect')
                self.driver.launch()
            elif action == 'disconnect':
                if self.owner and self.owner != page:
                    raise Rejected('another_page_owns_control')
                self.driver.close()
                self.owner = None
                return self.state(page)
            elif action == 'start':
                if not self.config()['control_enabled']:
                    raise Rejected('control_not_enabled_on_server')
                if state.get('active') or self.owner:
                    raise Rejected('action_already_active')
                if not state.get('connected'):
                    raise Rejected('connect_readonly_first')
                self.target = validate_target(data.get('target_xyz_m'))
                if data.get('stop_at', 'released') not in NODES:
                    raise Rejected('unknown_stop_node')
                self.owner = page
            else:
                if action not in ('heartbeat', 'pause', 'resume', 'next', 'continue', 'return', 'release'):
                    raise Rejected('unknown_action')
                if not state.get('active'):
                    raise Rejected('no_active_action')
                if action != 'release' and self.owner != page:
                    raise Rejected('this_page_does_not_own_control')
            command = {key: data[key] for key in ('target_xyz_m', 'stop_at', 'node') if key in data}
            command.update(action=action, request_id=request)
            try:
                self.driver.send(command)
            except Exception:
                if action == 'start':
                    self.owner = None
                raise
            if action != 'heartbeat':
                self.audit('command_requested', **command)
            return dict(accepted=True, action=action, state=self.state(page))
