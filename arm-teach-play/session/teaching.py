"""Repeatable named teaching sessions. Startup and New Project are read-only."""
import argparse
import json
import os
from pathlib import Path
import threading
import time

from transport import Bridge, ROOT, WORKSPACE
from recording_store import RecordingStore, atomic_json, recording_name, TERMINAL
from local_server import serve
from service_control import BuiltinArmService

ACTIVE = ('acquiring', 'softening', 'teaching', 'releasing', 'restoring_service')


class Session:
    def __init__(self, runtime, key, store, service=None):
        self.lock = threading.RLock()
        self.phase = 'connecting'
        self.error = ''
        self.waypoints = []
        self.started_at = self.ended_at = None
        self.stop = threading.Event()
        self.last_heartbeat = time.monotonic()
        self.directory = None
        self.name = ''
        self.saved = False
        self.max_seconds = 300
        self.store = store
        self.runtime = runtime
        self.service = service
        self.restore_result = None
        logdir = ROOT / 'logs' / 'teaching' / str(time.time_ns())
        logdir.mkdir(parents=True, exist_ok=False)
        self.journal = (logdir / 'session.jsonl').open('w', encoding='utf-8', buffering=1)
        self.bridge = Bridge(runtime, key, logdir, max_seconds=610)
        self.log_directory = logdir
        self.log('prepared_readonly', runtime=runtime, native_limit_s=610)
        self.phase = 'readonly'

    def log(self, event, **data):
        with self.lock:
            self.journal.write(json.dumps(dict(event=event, monotonic=time.monotonic(), **data), allow_nan=False) + '\n')

    def elapsed(self):
        if self.started_at is None:
            return 0.
        return max(0., (self.ended_at if self.ended_at is not None else time.monotonic()) - self.started_at)

    def state(self):
        try:
            data = self.bridge.state()
        except RuntimeError as exc:
            data = {'connected': False, 'transport_error': str(exc), 'sent': 0, 'weight': 0}
        with self.lock:
            elapsed = self.elapsed()
            data.update(phase=self.phase, error=self.error, waypoints=len(self.waypoints), name=self.name,
                        recording_id=self.directory.name if self.directory else None,
                        recording_directory=str(self.directory) if self.directory else '',
                        elapsed_s=max(0., elapsed - 4.), session_elapsed_s=elapsed,
                        remaining_s=max(0., self.max_seconds - max(0., elapsed - 4.)),
                        max_seconds=self.max_seconds, saved=self.saved, restore_result=self.restore_result)
        return data

    def begin(self, onsite, name, max_seconds=300):
        name = recording_name(name)
        if isinstance(max_seconds, bool) or not isinstance(max_seconds, int) or not 30 <= max_seconds <= 600:
            raise ValueError('录制时限需为 30～600 秒的整数')
        with self.lock:
            if onsite is not True:
                raise RuntimeError('onsite_support_required')
            if self.phase != 'readonly':
                raise RuntimeError('请先结束当前录制，再新建项目')
            initial = self.bridge.snapshot()
            if initial['fsm_mode'] != 0:
                raise RuntimeError('not_standing')
            self.directory = self.store.create()
            self.name = name
            self.error = ''
            self.max_seconds = max_seconds
            self.created_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            self.started_at = time.monotonic()
            self.last_heartbeat = self.started_at
            self.phase = 'acquiring'
            self.stop.clear()
            self.log('begin', initial_state=initial, name=name, directory=str(self.directory), max_seconds=max_seconds)
            try:
                self.save()
            except OSError as exc:
                self.phase = 'readonly'
                self.started_at = None
                self.error = '保存准备失败：' + str(exc)
                raise
            self.thread = threading.Thread(target=self.record, args=(initial,), daemon=True)
            self.thread.start()

    def record(self, initial):
        seed = initial['q'][12:29]
        deadline = time.monotonic()
        first = True
        raw = None
        try:
            raw = (self.directory / 'samples.jsonl').open('w', encoding='utf-8', buffering=1)
            while not self.stop.is_set():
                now = time.monotonic()
                elapsed = now - self.started_at
                if now - self.last_heartbeat > 3:
                    raise RuntimeError('browser_heartbeat_lost')
                if elapsed >= self.max_seconds + 4:
                    self.log('recording_limit_reached', max_seconds=self.max_seconds)
                    break
                state = self.bridge.snapshot()
                if elapsed < 2:
                    weight, softness, q, phase = min(1, elapsed / 2), 0., seed, 'acquiring'
                else:
                    weight, softness = 1., min(1, (elapsed - 2) / 2)
                    q = seed[:3] + state['q'][15:29]
                    phase = 'softening' if softness < 1 else 'teaching'
                with self.lock:
                    if phase != self.phase:
                        self.phase = phase
                        self.log('phase', phase=phase)
                if first:
                    weight = 0
                self.bridge.send(q, weight, softness)
                first = False
                raw.write(json.dumps({'t_s': elapsed, 'recording_t_s': max(0., elapsed - 4.),
                    'phase': phase, 'tick': state['tick'], 'q_upper': state['q'][12:29],
                    'dq_upper': state['dq'][12:29], 'rpy': state['rpy'], 'mode_machine': state['mode_machine']}) + '\n')
                deadline += .02
                time.sleep(max(0, deadline - time.monotonic()))
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
            self.log('stop_reason', reason=str(exc))
        finally:
            with self.lock:
                self.ended_at = time.monotonic()
                self.phase = 'releasing'
            try:
                self.bridge.release()
            except Exception as exc:
                self.log('release_transport_error', reason=str(exc))
            if raw is not None:
                raw.close()
            confirmed = self.bridge.handback_confirmed()
            if confirmed and self.service is not None:
                with self.lock:self.phase = 'restoring_service'
                try:
                    self.restore_result = self.service.restore_after_handback(self.bridge)
                except Exception as exc:
                    self.restore_result = {'verified': False, 'message': str(exc)}
                self.log('service_restore_after_handback', result=self.restore_result)
            with self.lock:
                finish = self.bridge.finished
                if confirmed:
                    if finish['returncode'] != 0 and not self.error:
                        self.error = 'native_exit:' + finish['reason']
                    self.phase = 'ended_with_error' if self.error else 'ended'
                    if self.restore_result and not self.restore_result['verified']:
                        self.phase = 'ended_with_error'
                        self.error = (self.error + '；' if self.error else '') + '手臂已交还，原厂服务恢复未确认：' + self.restore_result['message']
                else:
                    self.phase = 'release_unconfirmed'
                self.log('finished', transport=finish, waypoints=len(self.waypoints))
                try:
                    self.save()
                except OSError as exc:
                    self.saved = False
                    self.error = '保存失败：' + str(exc)
                    if self.phase in TERMINAL:
                        self.phase = 'ended_with_error'

    def mark(self, label):
        with self.lock:
            if self.phase != 'teaching':
                raise RuntimeError('wait_until_teaching')
            data = self.bridge.snapshot()
            elapsed = self.elapsed()
            value = {'label': str(label)[:100], 't_s': elapsed, 'recording_t_s': max(0., elapsed - 4.),
                     'q_arm': data['q'][15:29], 'q_upper': data['q'][12:29]}
            self.waypoints.append(value)
            self.log('waypoint', **value)
            self.save()
            return len(self.waypoints)

    def save(self, name=None):
        with self.lock:
            if self.directory is None:
                raise RuntimeError('尚未录制项目')
            if name is not None:
                if self.phase not in TERMINAL:
                    raise RuntimeError('请等待录制结束并交还完成后保存名称')
                self.name = recording_name(name)
            atomic_json(self.directory / 'waypoints.json', self.waypoints)
            text = str(len(self.waypoints)) + '\n14\n'
            text += ''.join(' '.join(format(q, '.9g') for q in point['q_arm']) + '\n' for point in self.waypoints)
            (self.directory / 'arm7_action.dat').write_text(text, encoding='utf-8')
            atomic_json(self.directory / 'metadata.json', {'schema_version': 2, 'name': self.name,
                'id': self.directory.name, 'created_at': self.created_at, 'robot': 'Unitree G1 29DoF',
                'arm_scope': 'both arms', 'q_unit': 'rad', 'time_unit': 's', 'sampling_target_hz': 50,
                'saved': self.phase in TERMINAL, 'recording_duration_s': max(0., self.elapsed() - 4.),
                'max_seconds': self.max_seconds, 'phase': self.phase, 'error': self.error,
                'service_restore': self.restore_result,
                'ready_for_replay': False, 'initial_state_preserved': True,
                'upstream_commit': self.runtime['upstream_commit'], 'log_directory': str(self.log_directory),
                'fingertip_height_calibrated': False, 'button_success_verified': False})
            self.saved = self.phase in TERMINAL

    def close(self):
        with self.lock:
            if self.phase in ACTIVE or self.phase == 'release_unconfirmed':
                raise RuntimeError('请先结束录制并确认交还完成')
            if self.started_at is not None and self.directory is not None and not self.saved:
                raise RuntimeError('请先成功保存当前项目，再新建项目')
            self.bridge.close()
            self.journal.close()

    def restored(self, result):
        with self.lock:
            self.restore_result = result
            if result['verified']:
                self.error = self.error.split('手臂已交还，原厂服务恢复未确认：')[0].rstrip('；')
                self.phase = 'ended_with_error' if self.error else 'ended'
            self.log('service_restore_retry', result=result)
            self.save()


class TeachingConsole:
    def __init__(self, runtime, key, store=None):
        self.lock = threading.RLock()
        self.runtime, self.key = runtime, key
        self.store = store or RecordingStore()
        self.service = BuiltinArmService(runtime, key)
        self.session = Session(runtime, key, self.store, self.service)

    def state(self):
        with self.lock:
            data = self.session.state()
            data['recordings'] = self.store.catalog()
            data['service_control'] = self.service.state()
            result = data['service_control'].get('result')
            if not data.get('connected') and result and result.get('verified'):
                data['service_stopped'] = result.get('operation') == 'stop'
                data['service_state_source'] = 'last_service_query'
            return data

    def new(self):
        with self.lock:
            self.session.close()
            # Keep the old session visible if preparation fails; no control is acquired here.
            self.session = Session(self.runtime, self.key, self.store, self.service)

    def action(self, name, data):
        with self.lock:
            session = self.session
            if name == 'restore-service':
                if session.phase not in TERMINAL:
                    raise RuntimeError('请先结束录制并确认交还，再恢复原厂上肢服务')
                return self.service.request_restore(data.get('onsite'), session.bridge, session.restored)
            if name == 'stop-service':
                if session.phase != 'readonly':
                    raise RuntimeError('请先结束录制并新建只读会话，再停止原厂上肢服务')
                return self.service.request_stop(data.get('onsite'), session.bridge.state())
            if name in ('begin', 'new') and self.service.state()['busy']:
                raise RuntimeError('正在停止并核对原厂上肢服务，请等待结果')
            if name == 'begin':
                return session.begin(data.get('onsite'), data.get('name'), data.get('max_seconds', 300))
            if name == 'heartbeat':
                session.last_heartbeat = time.monotonic()
            elif name == 'mark':
                return session.mark(data.get('label', ''))
            elif name == 'stop':
                session.stop.set()
            elif name == 'save':
                session.save(data.get('name'))
            elif name == 'new':
                self.new()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8783)
    parser.add_argument('--runtime', type=Path, default=ROOT / 'runtime.json')
    args = parser.parse_args()
    key = Path(os.environ['TEMP']) / 'g1-migration-20261013/id_ed25519'
    app = TeachingConsole(json.loads(args.runtime.read_text()), key)
    def ready():
        value = {'url': 'http://127.0.0.1:' + str(args.port), 'phase': 'readonly', 'commands_sent': 0, 'pid': os.getpid()}
        atomic_json(ROOT / 'console-runtime.json', value)
        print(json.dumps(value), flush=True)
    try:
        serve(args.port, (ROOT / 'record.html').read_text(encoding='utf-8'), app.state,
              {name: (lambda data, name=name: app.action(name, data)) for name in ('begin', 'heartbeat', 'mark', 'stop', 'save', 'new', 'stop-service', 'restore-service')}, ready)
    finally:
        session = app.session
        session.stop.set()
        if hasattr(session, 'thread'):
            session.thread.join(timeout=100)
        if hasattr(app.service, 'thread'):app.service.thread.join(timeout=100)
        if session.phase not in ACTIVE and session.phase != 'release_unconfirmed':
            session.close()


if __name__ == '__main__':
    main()
