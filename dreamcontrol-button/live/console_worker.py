"""JSON-line action console. Startup/self-test never create a DDS publisher."""
import argparse
import hashlib
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from action_flow import ActionFlow, DEFAULT_TARGET, NODES, validate_target


class MockBridge:
    """UI/protocol fixture, not a physics simulation."""
    def __init__(self, defaults):
        self.q = list(defaults)
        self.sent = self.zeros = 0
        self.state = self.snapshot()

    def snapshot(self):
        self.state = dict(q=list(self.q), dq=[0.] * 29, quaternion=[1., 0., 0., 0.],
                          fsm=501, fsm_mode=int(self.sent > 0), weight=int(self.sent > 0),
                          service_stopped=True, foreign_age_s=999, tick=self.sent,
                          rpy=[0., 0., 0.], gyro=[0., 0., 0.])
        return dict(self.state)

    def wait(self):
        value = self.snapshot()
        value['fsm_mode'] = 0
        return value

    def send(self, q, weight):
        self.q[12:29] = list(q)
        self.sent += 1
        self.zeros += int(weight == 0)

    def close(self):
        if self.sent:
            self.sent += 60
            self.zeros += 11
        self.state = self.snapshot()
        self.state.update(weight=0, fsm_mode=0)
        return dict(type='finished', reason='RELEASE', sent=self.sent,
                    zero_weight_frames=self.zeros, final_weight=0, returncode=0,
                    simulated=True, physical_commands_sent=0)


def resources(runtime, mock):
    if mock:
        import math
        defaults = [0.] * 29
        defaults[15:29] = [.35, .16, 0., .87, 0., 0., 0., .35, -.16, 0., .87, 0., 0., 0.]
        def policy(state, previous, elapsed, target):
            q = list(defaults[12:29])
            a = max(0., math.sin(min(elapsed, 10) / 10 * math.pi))
            q[10] -= a * 1.3
            q[13] -= a * .7
            return q, [0.] * 17, [0.] * 57
        return defaults, policy, lambda execute, directory: MockBridge(defaults)
    import numpy as np
    import torch
    import yaml
    from controller import ACTIVE, Bridge, SDK_NAMES, observation
    policy_path = Path(runtime['policy_path'])
    if hashlib.sha256(policy_path.read_bytes()).hexdigest() != runtime['policy_sha256']:
        raise RuntimeError('policy_checksum_mismatch')
    binary = subprocess.check_output(['docker', 'exec', runtime['container'], 'cat', runtime['native_path']], timeout=10)
    if hashlib.sha256(binary).hexdigest() != runtime['binary_sha256']:
        raise RuntimeError('native_binary_checksum_mismatch')
    cfg = yaml.safe_load(Path(runtime['author_config']).read_text())
    assert (cfg['num_obs'], cfg['num_actions'], cfg['control_dt'], cfg['action_scale']) == (57, 17, .02, .5)
    lookup = [cfg['joint_names'].index(name) for name in SDK_NAMES]
    defaults = np.asarray(cfg['default_angles'], dtype=float)[lookup]
    torch.set_num_threads(1)
    model = torch.jit.load(str(policy_path), map_location='cpu').eval()
    def policy(state, previous, elapsed, target):
        obs = observation(state, np.asarray(previous, dtype=np.float32), elapsed,
                          defaults, np.asarray(target, dtype=np.float32))
        with torch.inference_mode():
            action = model(torch.from_numpy(obs).unsqueeze(0)).numpy().reshape(-1)
        if action.shape != (17,) or not np.isfinite(action).all():
            raise RuntimeError('invalid_policy_output')
        q = defaults[12:29].copy()
        q[ACTIVE - 12] = action[:15] * .5 + defaults[ACTIVE]
        return q.tolist(), action.tolist(), obs.tolist()
    fixture = dict(q=defaults.tolist(), dq=[0.] * 29, quaternion=[1., 0., 0., 0.])
    for target in (DEFAULT_TARGET, [.5, -.23, .5], [.47, -.20, .25]):
        for elapsed in (0, 2.24, 2.925, 3.64, 9.98):
            policy(fixture, [0.] * 17, elapsed, target)
    return defaults.tolist(), policy, lambda execute, directory: Bridge(runtime, execute, directory / 'transport.jsonl')


def preflight(runtime):
    prefix = ['docker', 'exec', runtime['container']]
    path = runtime['query_binary_dir']
    result = subprocess.run(prefix + [path + 'g1_mode_query', 'eno1'], capture_output=True, text=True, timeout=10)
    mode = json.loads(result.stdout) if result.returncode == 0 else {}
    if (mode.get('fsm_id') != 501 or mode.get('fsm_mode') != 0
            or mode.get('fsm_id_return_code') != 0 or mode.get('fsm_mode_return_code') != 0):
        raise RuntimeError('regular_standing_mode_unconfirmed')
    result = subprocess.run(prefix + [path + 'g1_arm_service', '--stop', 'eno1'],
                            capture_output=True, text=True, timeout=25)
    if result.returncode:
        raise RuntimeError('builtin_arm_service_stop_unconfirmed')
    return dict(mode=mode, service_stdout=result.stdout)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runtime', type=Path)
    p.add_argument('--mock', action='store_true')
    p.add_argument('--self-test', action='store_true')
    args = p.parse_args()
    if not args.mock and not args.runtime:
        p.error('--runtime required for hardware backend')
    runtime = json.loads(args.runtime.read_text()) if args.runtime else {}
    defaults, policy, make_bridge = resources(runtime, args.mock)
    if args.self_test:
        print(json.dumps(dict(self_test=True, nodes=NODES, target_xyz_m=DEFAULT_TARGET,
                              dds_initialized=False, physical_commands_sent=0)))
        return
    inbox = queue.Queue()
    heartbeat = [time.monotonic()]
    def reader():
        try:
            for line in sys.stdin:
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError('command_object_required')
                    if value.get('action') in ('heartbeat', 'start'):
                        heartbeat[0] = time.monotonic()
                    if value.get('action') != 'heartbeat':
                        inbox.put(value)
                except (ValueError, TypeError):
                    inbox.put(dict(action='invalid'))
        finally:
            inbox.put(dict(action='quit'))
    threading.Thread(target=reader, daemon=True).start()
    journal = None
    run_dir = None
    connected = False
    last_error = None
    def emit(value):
        print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)
    def log(event, **fields):
        if journal:
            journal.write(json.dumps(dict(event=event, monotonic=time.monotonic(), **fields),
                                     ensure_ascii=False, allow_nan=False) + '\n')
    flow = ActionFlow(log=log)
    def status():
        return dict(type='status', connected=connected, mock=args.mock,
                    log_dir=str(run_dir) if run_dir else None, last_error=last_error, **flow.status())
    emit(status())
    announced_done = False
    next_emit = 0
    try:
        while True:
            tick_started = time.monotonic()
            for unused in range(32):
                try:
                    cmd = inbox.get_nowait()
                except queue.Empty:
                    break
                action = cmd.get('action')
                try:
                    if action == 'quit':
                        if flow.active:
                            flow.finish('console_disconnected', error='console_disconnected')
                        return
                    if action == 'connect':
                        if flow.active:
                            raise ValueError('release_before_connect')
                        if args.mock:
                            state = MockBridge(defaults).wait()
                        else:
                            directory = Path(runtime['log_root']) / ('readonly-' + str(time.time_ns()))
                            directory.mkdir(parents=True)
                            bridge = make_bridge(False, directory)
                            try:
                                state = bridge.wait()
                            finally:
                                bridge.close()
                        flow.last_state = state
                        connected, last_error = True, None
                        emit(dict(type='readonly_connected', physical_commands_sent=0, state=state))
                    elif action == 'start':
                        if not connected:
                            raise ValueError('connect_readonly_first')
                        if flow.active:
                            raise ValueError('action_already_active')
                        target = validate_target(cmd.get('target_xyz_m'))
                        stop = cmd.get('stop_at', 'released')
                        if stop not in NODES:
                            raise ValueError('unknown_stop_node')
                        run_dir = Path(runtime.get('log_root', str(Path.cwd() / 'mock-logs'))) / ('trial-' + str(time.time_ns()))
                        run_dir.mkdir(parents=True)
                        if journal:
                            journal.close()
                        journal = (run_dir / 'flow.jsonl').open('w', buffering=1)
                        emit(dict(type='status', active=True, phase='starting', connected=True, target_xyz_m=target))
                        if not args.mock:
                            log('preflight', **preflight(runtime))
                        bridge = make_bridge(True, run_dir)
                        try:
                            flow.start(bridge, policy, defaults, target, stop)
                        except BaseException:
                            bridge.close()
                            raise
                        last_error = None
                        announced_done = False
                    else:
                        flow.command(action, cmd.get('node'))
                        last_error = None
                    log('ui_command', command=cmd)
                    emit(dict(type='accepted', action=action, request_id=cmd.get('request_id')))
                except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
                    last_error = str(exc)
                    log('command_rejected', command=cmd, error=last_error)
                    emit(dict(type='rejected', action=action, error=last_error))
                    if action == 'start' and not flow.active:
                        flow.phase, flow.error = 'fault', last_error
                emit(status())
            if flow.active:
                flow.last_heartbeat = heartbeat[0]
                try:
                    flow.tick()
                except BaseException as exc:
                    flow.finish('transport_or_policy_error', error=str(exc))
            if not flow.active and flow.bridge and not announced_done:
                summary = status()
                summary['after_handback'] = flow.bridge.state
                (run_dir / 'summary.json').write_text(json.dumps(summary, indent=2))
                if not args.mock:
                    Path(runtime['latest_result']).write_text(json.dumps(summary, indent=2))
                emit(summary)
                announced_done = True
            if time.monotonic() >= next_emit:
                emit(status())
                next_emit = time.monotonic() + .2
            time.sleep(max(0, tick_started + .02 - time.monotonic()))
    finally:
        if flow.active:
            flow.finish('worker_exit', error='worker_exit')
        if journal:
            journal.close()


if __name__ == '__main__':
    main()
