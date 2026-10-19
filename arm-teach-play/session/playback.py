"""Selectable staged or continuous arm playback; startup is read-only."""
import argparse
import json
import math
import os
from pathlib import Path
import threading
import time
import xml.etree.ElementTree as ET

from transport import Bridge, ROOT, WORKSPACE
from recording_store import RecordingStore, atomic_json
from local_server import serve
from service_control import BuiltinArmService
from continuous_trajectory import ContinuousTrajectory

POSITION_TOLERANCE_DEG = 2.0
SPEED_TOLERANCE_RAD_S = .15
COMMAND_PEAK_DEG_S = 20.
SETTLE_HOLD_S = .3
SETTLE_TIMEOUT_S = 5
STAGE_LIMIT_S = 120
HOLD_LIMIT_S = 120
SESSION_LIMIT_S = 900
ACTIVE = ('acquiring', 'moving', 'holding', 'approaching', 'continuous', 'tracking_wait',
          'final_settling', 'releasing', 'restoring_service')
ARM_LABELS = [side + part for side in ('左', '右') for part in
              ('肩前后摆', '肩侧摆', '肩旋转', '肘弯曲', '腕旋转', '腕俯仰', '腕偏转')]


def arrival_metrics(state, target):
    errors = [math.degrees(abs(a - b)) for a, b in zip(state['q'][15:29], target)]
    index = max(range(14), key=lambda i: errors[i])
    speed = max(abs(x) for x in state['dq'][15:29])
    return {'max_error_deg': errors[index], 'joint': ARM_LABELS[index], 'joint_index': 15 + index,
            'max_speed_rad_s': speed, 'position_tolerance_deg': POSITION_TOLERANCE_DEG,
            'speed_tolerance_rad_s': SPEED_TOLERANCE_RAD_S,
            'position_ok': errors[index] <= POSITION_TOLERANCE_DEG, 'speed_ok': speed <= SPEED_TOLERANCE_RAD_S}


def blend(x):
    x = min(1, max(0, x))
    return x*x*x*(10+x*(-15+6*x))


def duration(begin, end, rate_deg_s=COMMAND_PEAK_DEG_S):
    return max(2, 1.875*max(abs(a-b) for a, b in zip(begin, end))/math.radians(rate_deg_s))


def arm_limits():
    model = ET.parse(WORKSPACE / 'arm-button/reference/g1_29dof_rev_1_0.urdf').getroot()
    limits = {j.attrib['name']: (float(j.find('limit').attrib['lower']), float(j.find('limit').attrib['upper']))
              for j in model.findall('joint') if j.find('limit') is not None}
    names = [side + '_' + part + '_joint' for side in ('left', 'right') for part in
             ('shoulder_pitch', 'shoulder_roll', 'shoulder_yaw', 'elbow', 'wrist_roll', 'wrist_pitch', 'wrist_yaw')]
    return [limits[name] for name in names]


def load_points(recording):
    points = json.loads((recording / 'waypoints.json').read_text(encoding='utf-8'))
    if not isinstance(points, list) or not 1 <= len(points) <= 10000:
        raise ValueError('recording_requires_1_to_10000_keypoints')
    limits=arm_limits()
    for point in points:
        if not isinstance(point, dict) or not isinstance(point.get('q_arm'), list) or len(point['q_arm']) != 14:
            raise ValueError('wrong_joint_count')
        for q, (lo, hi) in zip(point['q_arm'], limits):
            if isinstance(q, bool) or not isinstance(q, (int, float)) or not math.isfinite(q) or not lo <= q <= hi:
                raise ValueError('invalid_recorded_pose')
    return points


class Replay:
    def __init__(self, recording, runtime, key, service=None, points=None):
        self.lock = threading.RLock()
        self.phase = 'readonly'
        self.error = ''
        self.completed = self.requested = 0
        self.stopped = threading.Event()
        self.last_heartbeat = time.monotonic()
        self.started = self.ended = None
        self.target = None
        self.segment_started = self.segment_duration = self.segment_elapsed = self.motion_elapsed = 0.
        self.holding_started = None
        self.arrival = None
        self.service = service
        self.restore_result = None
        self.mode = 'staged'
        self.points = load_points(recording) if points is None else points
        self.recording = recording
        self.directory = recording / 'stage-playback' / str(time.time_ns())
        self.directory.mkdir(parents=True, exist_ok=False)
        self.journal = (self.directory / 'events.jsonl').open('w', encoding='utf-8', buffering=1)
        self.bridge = Bridge(runtime, key, self.directory, max_seconds=SESSION_LIMIT_S + 10, gravity=True)
        self.log('prepared_readonly', recording=str(recording), runtime=runtime, rate_limit_deg_s=COMMAND_PEAK_DEG_S,
                 automatic_stages=False, position_tolerance_deg=POSITION_TOLERANCE_DEG,
                 speed_tolerance_rad_s=SPEED_TOLERANCE_RAD_S,
                 session_limit_s=SESSION_LIMIT_S, stage_limit_s=STAGE_LIMIT_S, hold_limit_s=HOLD_LIMIT_S)

    def log(self, event, **values):
        with self.lock:
            self.journal.write(json.dumps(dict(event=event, monotonic=time.monotonic(), **values), allow_nan=False) + '\n')

    def state(self):
        try:
            data = self.bridge.state()
        except RuntimeError as exc:
            data = {'connected': False, 'transport_error': str(exc), 'sent': 0, 'weight': 0}
        with self.lock:
            now = self.ended if self.ended is not None else time.monotonic()
            session_elapsed = max(0., now - self.started) if self.started is not None else 0.
            hold_elapsed = max(0., now - self.holding_started) if self.holding_started is not None else 0.
            data.update(phase=self.phase, mode=self.mode, error=self.error, completed=self.completed, requested=self.requested,
                        total=len(self.points), duration_s=self.segment_duration,
                        elapsed_s=self.motion_elapsed, stage_elapsed_s=self.segment_elapsed,
                        session_elapsed_s=session_elapsed, session_remaining_s=max(0., SESSION_LIMIT_S-session_elapsed),
                        hold_remaining_s=max(0., HOLD_LIMIT_S-hold_elapsed), arrival=self.arrival,
                        position_tolerance_deg=POSITION_TOLERANCE_DEG, speed_tolerance_rad_s=SPEED_TOLERANCE_RAD_S,
                        command_peak_deg_s=COMMAND_PEAK_DEG_S, restore_result=self.restore_result,
                        targets=[{'point': i+1, 'label': str(p.get('label', '')),
                                  'right_arm_deg': [round(math.degrees(v), 2) for v in p['q_arm'][7:]]}
                                 for i, p in enumerate(self.points)], directory=str(self.directory))
        return data

    def next(self, onsite):
        with self.lock:
            if onsite is not True:
                raise RuntimeError('onsite_conditions_required')
            if self.phase not in ('readonly', 'holding'):
                raise RuntimeError('not_ready_for_next_stage')
            if self.stopped.is_set():
                raise RuntimeError('replay_is_stopping')
            if self.completed >= len(self.points):
                raise RuntimeError('all_points_complete')
            now = time.monotonic()
            if self.phase == 'holding' and now - self.holding_started >= HOLD_LIMIT_S:
                raise RuntimeError('holding_time_limit')
            state = self.bridge.snapshot()
            if max(abs(v) for v in state['gyro']) > .15:
                raise RuntimeError('body_rotation_before_stage')
            if max(abs(v) for v in state['rpy'][:2]) > .175:
                raise RuntimeError('body_tilt_before_stage')
            begin_q = list(state['q'][15:29])
            target = list(self.points[self.completed]['q_arm'])
            seconds = duration(begin_q, target)
            if seconds + SETTLE_TIMEOUT_S > STAGE_LIMIT_S:
                raise RuntimeError('stage_exceeds_time_limit')
            if self.started is not None and now - self.started + seconds + SETTLE_TIMEOUT_S + 2 > SESSION_LIMIT_S:
                raise RuntimeError('insufficient_session_time_end_and_reprepare')
            self.begin_q, self.target, self.segment_duration = begin_q, target, seconds
            self.arrival = None
            self.segment_elapsed = 0.
            self.holding_started = None
            self.requested = self.completed + 1
            self.last_heartbeat = now
            self.log('stage_requested', point=self.requested, duration_s=seconds, start=state)
            if self.phase == 'readonly':
                self.seed = list(state['q'][12:29])
                self.started = now
                self.phase = 'acquiring'
                self.thread = threading.Thread(target=self.run, daemon=True)
                self.thread.start()
            else:
                self.segment_started = now
                self.phase = 'moving'

    def run(self):
        deadline = previous = time.monotonic()
        first = True
        settled_since = None
        try:
            while not self.stopped.is_set():
                now = time.monotonic()
                elapsed = now - self.started
                if elapsed >= SESSION_LIMIT_S:
                    self.log('session_limit_reached')
                    break
                if now - self.last_heartbeat > 3:
                    raise RuntimeError('browser_heartbeat_lost')
                state = self.bridge.snapshot()
                with self.lock:
                    if self.phase == 'acquiring':
                        q, weight = self.seed, min(1, elapsed / 2)
                        if elapsed >= 2:
                            self.begin_q = list(state['q'][15:29])
                            self.segment_duration = duration(self.begin_q, self.target)
                            self.segment_started = now
                            self.phase = 'moving'
                    elif self.phase == 'moving':
                        t = now - self.segment_started
                        self.segment_elapsed = t
                        self.motion_elapsed += max(0., now - max(previous, self.segment_started))
                        s = blend(t / self.segment_duration)
                        q = self.seed[:3] + [a + (b-a)*s for a, b in zip(self.begin_q, self.target)]
                        weight = 1
                        self.arrival = arrival_metrics(state, self.target)
                        if t >= self.segment_duration:
                            if self.arrival['position_ok'] and self.arrival['speed_ok']:
                                if settled_since is None:
                                    settled_since = now
                                if now - settled_since >= SETTLE_HOLD_S:
                                    self.completed = self.requested
                                    self.phase = 'holding'
                                    self.holding_started = now
                                    settled_since = None
                                    self.log('point_reached', point=self.completed, arrival=self.arrival)
                            else:
                                settled_since = None
                            if self.phase == 'moving' and t >= self.segment_duration + SETTLE_TIMEOUT_S:
                                self.log('arrival_timeout', point=self.requested, arrival=self.arrival)
                                raise RuntimeError('target_not_reached_no_next_stage')
                        if t >= STAGE_LIMIT_S:
                            raise RuntimeError('stage_time_limit')
                    elif self.phase == 'holding':
                        if now - self.holding_started >= HOLD_LIMIT_S:
                            self.log('holding_limit_reached')
                            break
                        q, weight = self.seed[:3] + self.target, 1
                        self.arrival = arrival_metrics(state, self.target)
                    else:
                        break
                if first:
                    weight = 0
                self.bridge.send(q, weight, 0)
                first = False
                previous = now
                deadline += .02
                time.sleep(max(0, deadline - time.monotonic()))
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
            self.log('stop_reason', reason=str(exc))
        finally:
            self.finish_run()

    def finish_run(self):
        with self.lock:
            self.ended = time.monotonic()
            self.phase = 'releasing'
        try:
            self.bridge.release()
        except Exception as exc:
            self.log('release_error', reason=str(exc))
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
            self.log('finished', transport=finish, points_completed=self.completed, error=self.error)

    def close(self):
        with self.lock:
            if self.phase in ACTIVE or self.phase == 'release_unconfirmed':
                raise RuntimeError('请先结束回放并确认交还完成，再选择项目')
            self.bridge.close()
            self.journal.close()

    def restored(self, result):
        with self.lock:
            self.restore_result = result
            if result['verified']:
                self.error = self.error.split('手臂已交还，原厂服务恢复未确认：')[0].rstrip('；')
                self.phase = 'ended_with_error' if self.error else 'ended'
            self.log('service_restore_retry', result=result)


class ContinuousReplay(Replay):
    def __init__(self, recording, runtime, key, service=None, trajectory=None):
        self.trajectory = trajectory or ContinuousTrajectory(recording,arm_limits(),COMMAND_PEAK_DEG_S)
        if self.trajectory.duration_s+SETTLE_TIMEOUT_S+4 >= SESSION_LIMIT_S:
            raise ValueError('连续轨迹按 20°/s 放慢后超过单次接管时限，请拆分录制')
        endpoints=[{'label':'连续轨迹起点','q_arm':self.trajectory.poses[0]},
                   {'label':'连续轨迹终点','q_arm':self.trajectory.poses[-1]}]
        super().__init__(recording,runtime,key,service,points=endpoints)
        self.mode='continuous';self.trajectory_elapsed=0.;self.sample_index=0
        self.continuous_complete=False;self.tracking_wait_started=None
        self.log('continuous_loaded',**self.trajectory.summary())

    def state(self):
        data=super().state()
        with self.lock:
            data.update(continuous=self.trajectory.summary(),trajectory_elapsed_s=self.trajectory_elapsed,
                        recorded_elapsed_s=self.trajectory_elapsed/self.trajectory.time_scale,
                        progress=self.trajectory_elapsed/self.trajectory.duration_s,
                        sample_index=self.sample_index,continuous_complete=self.continuous_complete)
        return data

    def next(self, onsite):
        raise RuntimeError('当前加载的是连续采样轨迹，请使用整段回放按钮')

    def start(self, onsite):
        with self.lock:
            if onsite is not True:raise RuntimeError('onsite_conditions_required')
            if self.phase!='readonly' or self.stopped.is_set():raise RuntimeError('请重新加载连续轨迹后开始')
            state=self.bridge.snapshot()
            if max(abs(v) for v in state['gyro'])>.15:raise RuntimeError('body_rotation_before_stage')
            if max(abs(v) for v in state['rpy'][:2])>.175:raise RuntimeError('body_tilt_before_stage')
            self.begin_q=list(state['q'][15:29]);self.target=list(self.trajectory.poses[0])
            self.segment_duration=duration(self.begin_q,self.target)
            required=2+self.segment_duration+2*SETTLE_TIMEOUT_S+self.trajectory.duration_s+2*SETTLE_HOLD_S
            if required>=SESSION_LIMIT_S:raise RuntimeError('连续轨迹接近时间上限，请拆分录制')
            self.seed=list(state['q'][12:29]);self.started=time.monotonic()
            self.last_heartbeat=self.started;self.phase='acquiring';self.requested=1
            self.log('continuous_requested',initial_state=state,estimated_total_s=required,**self.trajectory.summary())
            self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()

    def run(self):
        deadline=previous=time.monotonic();first=True;settled_since=None
        try:
            while not self.stopped.is_set():
                now=time.monotonic();elapsed=now-self.started;step=max(0.,now-previous)
                if elapsed>=SESSION_LIMIT_S:raise RuntimeError('continuous_session_time_limit')
                if now-self.last_heartbeat>3:raise RuntimeError('browser_heartbeat_lost')
                state=self.bridge.snapshot()
                with self.lock:
                    if self.phase=='acquiring':
                        q,weight=self.seed,min(1.,elapsed/2)
                        if elapsed>=2:
                            # Continue the reference from the seed used during acquisition.
                            self.begin_q=list(self.seed[3:]);self.segment_duration=duration(self.begin_q,self.target)
                            self.segment_started=now;self.phase='approaching'
                    elif self.phase=='approaching':
                        t=now-self.segment_started;self.segment_elapsed=t;self.motion_elapsed+=step
                        s=blend(t/self.segment_duration)
                        q=self.seed[:3]+[a+(b-a)*s for a,b in zip(self.begin_q,self.target)];weight=1
                        self.arrival=arrival_metrics(state,self.target)
                        if t>=self.segment_duration:
                            if self.arrival['position_ok'] and self.arrival['speed_ok']:
                                if settled_since is None:settled_since=now
                                if now-settled_since>=SETTLE_HOLD_S:
                                    self.phase='continuous';self.completed=1;settled_since=None
                                    self.log('continuous_started',arrival=self.arrival)
                            else:settled_since=None
                            if self.phase=='approaching' and t>=self.segment_duration+SETTLE_TIMEOUT_S:
                                raise RuntimeError('continuous_start_not_reached')
                    elif self.phase in ('continuous','tracking_wait'):
                        self.motion_elapsed+=step
                        candidate_time=min(self.trajectory.duration_s,self.trajectory_elapsed+min(step,.05))
                        candidate,index=self.trajectory.at(candidate_time)
                        metrics=arrival_metrics(state,candidate)
                        if metrics['position_ok']:
                            if self.phase=='tracking_wait':self.log('tracking_resumed',trajectory_t_s=self.trajectory_elapsed)
                            self.phase='continuous';self.tracking_wait_started=None
                            self.target=candidate;self.trajectory_elapsed=candidate_time;self.sample_index=index
                            self.arrival=metrics
                        else:
                            if self.tracking_wait_started is None:
                                self.tracking_wait_started=now;self.log('tracking_paused',candidate_error=metrics,trajectory_t_s=self.trajectory_elapsed)
                            self.phase='tracking_wait';self.arrival=arrival_metrics(state,self.target)
                            if now-self.tracking_wait_started>=SETTLE_TIMEOUT_S:
                                raise RuntimeError('continuous_tracking_error_timeout')
                        q,weight=self.seed[:3]+self.target,1
                        if self.trajectory_elapsed>=self.trajectory.duration_s:
                            self.phase='final_settling';self.segment_started=now;settled_since=None
                            self.log('continuous_samples_complete')
                    elif self.phase=='final_settling':
                        self.motion_elapsed+=step;q,weight=self.seed[:3]+self.target,1
                        self.arrival=arrival_metrics(state,self.target)
                        if self.arrival['position_ok'] and self.arrival['speed_ok']:
                            if settled_since is None:settled_since=now
                            if now-settled_since>=SETTLE_HOLD_S:
                                self.continuous_complete=True;self.completed=2;self.sample_index=self.trajectory.sample_count
                                self.log('continuous_finished',arrival=self.arrival)
                                break
                        else:settled_since=None
                        if now-self.segment_started>=SETTLE_TIMEOUT_S:raise RuntimeError('continuous_final_not_reached')
                    else:break
                if first:weight=0
                self.bridge.send(q,weight,0);first=False;previous=now
                deadline+=.02;time.sleep(max(0.,deadline-time.monotonic()))
        except Exception as exc:
            with self.lock:self.error=str(exc)
            self.log('stop_reason',reason=str(exc))
        finally:self.finish_run()


class PlaybackConsole:
    def __init__(self, runtime, key, store=None, selected_id=None, mode='staged'):
        self.lock = threading.RLock()
        self.runtime, self.key = runtime, key
        self.store = store or RecordingStore()
        self.service = BuiltinArmService(runtime, key)
        self.replay = None
        self.mode=mode
        self.selection_error=''
        if mode not in ('staged','continuous'):raise ValueError('invalid_playback_mode')
        choices = [item['id'] for item in self.store.catalog() if item['continuous_playable' if mode=='continuous' else 'playable']]
        if selected_id or choices:
            try:self.select(selected_id or choices[0])
            except (ValueError,OSError) as exc:self.selection_error=str(exc)

    def state(self):
        with self.lock:
            data = self.replay.state() if self.replay else {'phase': 'unselected', 'completed': 0,
                'requested': 0, 'total': 0, 'elapsed_s': 0., 'stage_elapsed_s': 0., 'targets': [], 'sent': 0, 'weight': 0}
            data['selected_id'] = self.replay.recording.name if self.replay else None
            data['mode']=self.mode
            if not self.replay:data['error']=self.selection_error
            data['recordings'] = self.store.catalog()
            data['service_control'] = self.service.state()
            result = data['service_control'].get('result')
            if not data.get('connected') and result and result.get('verified'):
                data['service_stopped'] = result.get('operation') == 'stop'
                data['service_state_source'] = 'last_service_query'
            return data

    def select(self, recording_id, mode=None):
        with self.lock:
            mode=self.mode if mode is None else mode
            path = self.store.playback(recording_id,mode)
            trajectory=ContinuousTrajectory(path,arm_limits(),COMMAND_PEAK_DEG_S) if mode=='continuous' else None
            if trajectory and trajectory.duration_s+SETTLE_TIMEOUT_S+4>=SESSION_LIMIT_S:
                raise ValueError('连续轨迹按 20°/s 放慢后超过单次接管时限，请拆分录制')
            if mode=='staged':load_points(path)
            if self.replay:
                self.replay.close()
            self.replay = (ContinuousReplay(path,self.runtime,self.key,self.service,trajectory) if mode=='continuous'
                           else Replay(path,self.runtime,self.key,self.service))
            self.mode=mode
            self.selection_error=''

    def action(self, name, data):
        with self.lock:
            if name == 'restore-service':
                if self.replay is None or self.replay.phase not in ('ended', 'ended_with_error'):
                    raise RuntimeError('请先结束回放并确认交还，再恢复原厂上肢服务')
                return self.service.request_restore(data.get('onsite'), self.replay.bridge, self.replay.restored)
            if name == 'stop-service':
                if self.replay is None or self.replay.phase != 'readonly':
                    raise RuntimeError('请先加载项目并进入只读会话，再停止原厂上肢服务')
                return self.service.request_stop(data.get('onsite'), self.replay.bridge.state())
            if name in ('select', 'next', 'start') and self.service.state()['busy']:
                raise RuntimeError('正在停止并核对原厂上肢服务，请等待结果')
            if name == 'select':
                return self.select(data.get('id'),data.get('mode'))
            if self.replay is None:
                raise RuntimeError('请先选择已保存的项目')
            if name == 'next':
                return self.replay.next(data.get('onsite'))
            if name=='start':
                if self.mode!='continuous':raise RuntimeError('请先加载连续采样轨迹')
                return self.replay.start(data.get('onsite'))
            if name == 'heartbeat':
                self.replay.last_heartbeat = time.monotonic()
            elif name == 'stop':
                self.replay.stopped.set()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port', type=int, default=8784)
    p.add_argument('--recording', type=Path)
    p.add_argument('--mode',choices=('staged','continuous'),default='continuous')
    args = p.parse_args()
    runtime = json.loads((ROOT / 'runtime.json').read_text())
    key = Path(os.environ['TEMP']) / 'g1-migration-20261013/id_ed25519'
    store = RecordingStore()
    if args.recording and args.recording.resolve().parent != store.root:
        raise ValueError('recording_outside_library')
    app = PlaybackConsole(runtime, key, store, args.recording.name if args.recording else None,mode=args.mode)
    def ready():
        value = {'url': 'http://127.0.0.1:' + str(args.port), 'phase': 'readonly', 'sent': 0, 'pid': os.getpid()}
        atomic_json(ROOT / 'replay-console-runtime.json', value)
        print(json.dumps(value), flush=True)
    try:
        serve(args.port, (ROOT / 'replay.html').read_text(encoding='utf-8'), app.state,
              {name: (lambda data, name=name: app.action(name, data)) for name in ('select', 'next', 'start', 'heartbeat', 'stop', 'stop-service', 'restore-service')}, ready)
    finally:
        replay = app.replay
        if replay:
            replay.stopped.set()
            if hasattr(replay, 'thread'):
                replay.thread.join(timeout=100)
            if hasattr(app.service, 'thread'):app.service.thread.join(timeout=100)
            if replay.phase not in ACTIVE and replay.phase != 'release_unconfirmed':
                replay.close()


if __name__ == '__main__':
    main()
