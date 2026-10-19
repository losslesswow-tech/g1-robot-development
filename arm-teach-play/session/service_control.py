"""Explicit stop and verified post-handback restoration of g1_arm_example."""
import json
import shlex
import subprocess
import threading
import time

from recording_store import atomic_json
from transport import ROOT, SSH

SERVICE = 'g1_arm_example'
API_VERSION = '1.0.0.2'


def checked_status(result):
    if result['returncode'] != 0:
        raise RuntimeError('原厂上肢服务查询失败')
    events = result['events']
    versions = [e for e in events if e.get('event') == 'api_version']
    if len(versions) != 1 or versions[0].get('client') != API_VERSION or versions[0].get('server') != API_VERSION:
        raise RuntimeError('设备状态 API 版本不匹配，未确认服务状态')
    lists = [e for e in events if e.get('event') == 'service_list' and e.get('return_code') == 0]
    if not lists:
        raise RuntimeError('服务列表查询未确认')
    targets = [s for s in lists[-1].get('services', []) if s.get('name') == SERVICE]
    if len(targets) != 1 or targets[0].get('status_raw') not in (0, 1):
        raise RuntimeError('原厂上肢服务状态未知')
    return targets[0]['status_raw']


class BuiltinArmService:
    def __init__(self, runtime, key):
        self.runtime, self.key = runtime, key
        self.lock = threading.RLock()
        self.busy = False
        self.result = None
        self.operation = None

    def state(self):
        with self.lock:
            return {'busy': self.busy, 'name': SERVICE, 'operation': self.operation,
                    'result': dict(self.result) if self.result else None}

    def request_stop(self, onsite, telemetry):
        if onsite is not True:
            raise RuntimeError('请先勾选现场条件：站稳、范围清空、支撑与遥控器监护就绪')
        if not telemetry.get('connected') or telemetry.get('finished'):
            raise RuntimeError('请先新建项目或重新加载，建立只读连接')
        if max(telemetry.get(k, 99) for k in ('age_s', 'tick_age_s', 'transport_age_s')) > .15:
            raise RuntimeError('状态已过期，不能停止服务')
        if (telemetry.get('fsm') != 501 or telemetry.get('fsm_mode') != 0
                or telemetry.get('rpc_age_s', 99) >= 2 or not telemetry.get('api_match')):
            raise RuntimeError('需确认常规站立模式与设备状态 API 后停止服务')
        if telemetry.get('weight') != 0 or telemetry.get('sent') != 0:
            raise RuntimeError('请先结束手臂控制并重新准备只读会话')
        config = self.runtime.get('service_control')
        if not isinstance(config, dict) or not config.get('binary_path') or not config.get('binary_sha256'):
            raise RuntimeError('服务停止程序尚未配置')
        with self.lock:
            if self.busy:
                raise RuntimeError('正在停止服务，请等待结果')
            self.busy = True
            self.operation = 'stop'
            self.result = None
            self.thread = threading.Thread(target=self.worker, daemon=True)
            try:
                self.thread.start()
            except Exception:
                self.busy = False
                raise
        return {'accepted': True}

    def run(self, command, timeout):
        prefix = [SSH, '-i', str(self.key), '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
                  '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=8', 'operator@' + self.runtime['host']]
        completed = subprocess.run(prefix + [shlex.join(command)], capture_output=True, text=True,
                                   encoding='utf-8', errors='replace', timeout=timeout)
        events = []
        for line in completed.stdout.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):events.append(value)
        return {'returncode': completed.returncode, 'stdout': completed.stdout, 'stderr': completed.stderr, 'events': events}

    def execute(self, evidence, operation='stop'):
        if operation not in ('stop', 'restore'):
            raise ValueError('invalid_service_operation')
        desired = 1 if operation == 'stop' else 0
        verb = '停止' if operation == 'stop' else '恢复'
        config = self.runtime['service_control']
        path = config['binary_path']
        prefix = ['docker', 'exec', '-e', 'LD_LIBRARY_PATH=/opt/ros/noetic/lib:/usr/local/lib', self.runtime['container']]
        checksum = self.run(prefix + ['sha256sum', path], 15)
        evidence['checksum'] = checksum
        fields = checksum['stdout'].split()
        if checksum['returncode'] or not fields or fields[0] != config['binary_sha256']:
            raise RuntimeError('服务程序校验失败，未发送切换请求')
        before = self.run(prefix + [path, '--query', self.runtime['interface']], 15)
        evidence['before'] = before
        status = checked_status(before)
        if status == desired:
            return {'verified': True, 'changed': False, 'switch_calls': 0, 'operation': operation,
                    'message': '原厂上肢服务已' + verb + '，无需重复操作'}
        # Same container-side flock as the arm transport. No motion can acquire it during this request.
        stopped = self.run(prefix + ['flock', '-n', '-E', '75', '/tmp/g1-right-arm-bounded.lock',
                                   path, '--' + operation, self.runtime['interface']], 45)
        evidence[operation] = stopped
        if stopped['returncode'] == 75:
            raise RuntimeError('手臂控制锁正在使用中，请先结束并交还控制')
        after = self.run(prefix + [path, '--query', self.runtime['interface']], 15)
        evidence['after'] = after
        status = checked_status(after)
        if stopped['returncode']:
            guards = [e.get('error') for e in stopped['events'] if e.get('event') == 'restore_guard' and e.get('error')]
            detail = '：' + guards[-1] if guards else ''
            raise RuntimeError(verb + '请求未确认（返回 ' + str(stopped['returncode']) + '）' + detail + '，请查看实时服务状态')
        if status != desired:
            raise RuntimeError(verb + '后查询仍未确认目标状态')
        count = sum(e.get('event') == 'service_' + operation for e in stopped['events'])
        return {'verified': True, 'changed': count > 0, 'switch_calls': count, 'operation': operation,
                'message': '原厂上肢服务已' + verb + '并查询确认'}

    def restore_after_handback(self, bridge):
        if not bridge.handback_confirmed():
            return {'verified': False, 'skipped': True, 'message': '交还未确认，未恢复原厂上肢服务'}
        if bridge.finished.get('reason') in ('controller_select_stop', 'machine_mode_changed', 'signal_stop'):
            return {'verified': False, 'skipped': True, 'message': '紧急停止或机型变化后保持原厂上肢服务关闭，请现场检查'}
        with self.lock:
            if self.busy:
                return {'verified': False, 'message': '其他服务操作尚未结束，未并行恢复'}
            self.busy = True
            self.operation = 'restore'
            self.result = None
        self.worker('restore')
        return self.state()['result']

    def request_restore(self, onsite, bridge, callback=None):
        if onsite is not True:
            raise RuntimeError('请先勾选现场条件再恢复原厂上肢服务')
        if not bridge.handback_confirmed():
            raise RuntimeError('需确认零权重与控制进程退出后才能恢复服务')
        with self.lock:
            if self.busy:
                raise RuntimeError('服务操作尚未结束')
            self.busy = True
            self.operation = 'restore'
            self.result = None
            self.thread = threading.Thread(target=self.worker, args=('restore', callback), daemon=True)
            try:
                self.thread.start()
            except Exception:
                self.busy = False
                raise
        return {'accepted': True}

    def worker(self, operation='stop', callback=None):
        evidence = {'service': SERVICE, 'operation': operation, 'started_at': time.time(), 'motion_commands_sent': 0}
        try:
            result = self.execute(evidence, operation)
        except Exception as exc:
            result = {'verified': False, 'operation': operation, 'message': str(exc)}
        result['finished_at'] = time.time()
        evidence['result'] = result
        try:
            directory = ROOT / 'logs' / 'service-control'
            directory.mkdir(parents=True, exist_ok=True)
            atomic_json(directory / (str(time.time_ns()) + '.json'), evidence)
        except OSError:
            result['log_error'] = '服务操作日志保存失败'
        with self.lock:
            self.result = result
        try:
            if callback is not None:callback(result)
        except Exception as exc:
            with self.lock:result['callback_error'] = str(exc)
        finally:
            with self.lock:self.busy = False
