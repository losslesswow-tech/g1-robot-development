"""Clocked, transport-independent single-shot/stepped DreamControl flow."""
import math
import time

TARGET_BOUNDS = ((.43, .54), (-.27, -.13), (.13, .50))
DEFAULT_TARGET = [.50, -.23, .31]
NODES = ('prepared', 'raised', 'pressed', 'withdrawn', 'lowered', 'released')
NEXT_STAGE = dict(zip(NODES[:-1], ('raise', 'press', 'withdraw', 'lower', 'release')))
STAGE_NODE = {'default': 'prepared', 'raise': 'raised', 'press': 'pressed',
              'withdraw': 'withdrawn', 'lower': 'lowered'}
POLICY_END = {'raise': 112, 'press': 182, 'withdraw': 500}
HOLD_SECONDS = 20.0
LEASE_SECONDS = 3.0
SESSION_SECONDS = 90.0


def validate_target(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError('target_requires_xyz')
    result = []
    for axis, item, bounds in zip('xyz', value, TARGET_BOUNDS):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError('target_must_be_numeric')
        if not math.isfinite(item) or not bounds[0] <= item <= bounds[1]:
            raise ValueError('target_' + axis + '_out_of_range')
        result.append(float(item))
    return result


class ActionFlow:
    """A paused policy clock never advances; all holds keep the transport fed.

    Nodes partition the original policy timing, not Cartesian waypoints or
    physical button success. The bridge is created only by an explicit start.
    """
    def __init__(self, clock=time.monotonic, log=None):
        self.clock = clock
        self.log = log or (lambda event, **data: None)
        self.active = False
        self.phase = 'idle'
        self.stage = None
        self.node = None
        self.error = None
        self.reason = None
        self.native_finished = None
        self.target = list(DEFAULT_TARGET)
        self.last_state = None
        self.policy_step = 0
        self.policy_samples = 0
        self.last_heartbeat = clock()
        self.last_command = None
        self.bridge = None

    def heartbeat(self):
        self.last_heartbeat = self.clock()

    def start(self, bridge, policy, defaults, target, stop_at='released'):
        if self.active:
            raise ValueError('action_already_active')
        target = validate_target(target)
        if stop_at not in NODES:
            raise ValueError('unknown_stop_node')
        initial = bridge.wait()
        if (initial['fsm'] != 501 or initial['fsm_mode'] != 0
                or not initial['service_stopped'] or initial['foreign_age_s'] < 1):
            raise ValueError('robot_start_conditions_unconfirmed')
        self.bridge, self.policy = bridge, policy
        self.defaults = list(defaults[12:29])
        self.target, self.stop_at = target, stop_at
        self.initial, self.last_state = initial, initial
        self.seed = list(initial['q'][12:29])
        self.last_command = list(self.seed)
        self.previous_action = [0.] * 17
        self.active, self.phase, self.stage = True, 'running', 'acquire'
        self.node = self.error = self.reason = self.native_finished = None
        self.policy_step = self.policy_samples = self.step = 0
        self.started = self.clock()
        self.heartbeat()
        self.blend_start = None
        self.log('start', target_xyz_m=target, stop_at=stop_at, initial=initial)

    def _hold(self, phase):
        self.phase = phase
        self.hold_started = self.clock()
        # Capture measured posture once, so settling to a distant policy
        # command does not continue while the UI says that motion is paused.
        self.hold_q = list(self.bridge.snapshot()['q'][12:29])
        self.log('hold', kind=phase, node=self.node, policy_step=self.policy_step,
                 target_q_upper=self.hold_q)

    def _stage(self, stage):
        self.stage, self.phase, self.step = stage, 'running', 0
        self.blend_start = None
        if stage == 'lower':
            self.lower_seed = list(self.bridge.snapshot()['q'][12:29])
        self.log('stage', stage=stage, node=self.node)

    def command(self, action, node=None):
        if not self.active:
            raise ValueError('no_active_action')
        if action == 'release':
            self.finish('user_release')
            return
        if action == 'return':
            if self.stage in ('acquire', 'default') and self.phase == 'running':
                raise ValueError('use_release_during_acquisition')
            self.stop_at = 'released'
            self._stage('lower')
            return
        if action == 'pause':
            if self.phase != 'running' or self.stage in ('acquire', 'default'):
                raise ValueError('cannot_pause_this_stage')
            self._hold('paused')
            return
        if action == 'resume':
            if self.phase != 'paused':
                raise ValueError('action_not_paused')
            self.phase = 'running'
            self.blend_start, self.blend_q = self.clock(), list(self.hold_q)
            if self.stage == 'lower':
                # Rebase the remaining lower transition on the held posture.
                self.lower_seed = list(self.hold_q)
                self.step = 0
            self.log('resume', policy_step=self.policy_step)
            return
        if action in ('next', 'continue'):
            if self.phase != 'holding' or self.node not in NEXT_STAGE:
                raise ValueError('next_requires_completed_node')
            expected = NODES[NODES.index(self.node) + 1]
            if action == 'next' and node != expected:
                raise ValueError('node_order_required')
            self.stop_at = expected if action == 'next' else 'released'
            stage = NEXT_STAGE[self.node]
            if stage == 'release':
                self.finish('completed')
            else:
                hold = list(self.hold_q)
                self._stage(stage)
                self.blend_start, self.blend_q = self.clock(), hold
            return
        raise ValueError('unknown_action')

    def _node_done(self, node):
        self.node = node
        self.log('node', node=node, policy_step=self.policy_step)
        if node == self.stop_at:
            self._hold('holding')
        elif node == 'lowered':
            self.finish('completed')
        else:
            self._stage(NEXT_STAGE[node])

    def tick(self):
        if not self.active:
            return
        now = self.clock()
        if now - self.last_heartbeat > LEASE_SECONDS:
            self.finish('page_heartbeat_timeout', error='page_heartbeat_timeout')
            return
        if now - self.started > SESSION_SECONDS - 2:
            self.finish('session_deadline', error='session_deadline')
            return
        self.last_state = state = self.bridge.snapshot()
        if self.phase in ('paused', 'holding'):
            if now - self.hold_started >= HOLD_SECONDS:
                self.finish('node_hold_timeout', error='node_hold_timeout')
                return
            self.bridge.send(self.hold_q, 1)
            self.last_command = list(self.hold_q)
            return
        stage = self.stage
        if stage == 'acquire':
            q, weight = self.seed, self.step / 100
        elif stage in ('default', 'lower'):
            seed = self.seed if stage == 'default' else self.lower_seed
            a = self.step / 100
            q = [x * (1 - a) + y * a for x, y in zip(seed, self.defaults)]
            weight = 1
        else:
            elapsed = self.policy_step * .02
            q, action, obs = self.policy(state, self.previous_action, elapsed, self.target)
            self.previous_action = list(action)
            self.log('policy_sample', elapsed_s=elapsed, observation=list(obs),
                     action=list(action), target_q_upper=list(q), state_tick=state['tick'])
            self.policy_step += 1
            self.policy_samples += 1
            weight = 1
        if self.blend_start is not None:
            a = min(1., (now - self.blend_start) / .3)
            a = a * a * (3 - 2 * a)
            q = [x * (1 - a) + y * a for x, y in zip(self.blend_q, q)]
            if a == 1:
                self.blend_start = None
        self.bridge.send(q, weight)
        self.last_command = list(q)
        self.step += 1
        if stage == 'acquire' and self.step == 101:
            self._stage('default')
        elif stage in ('default', 'lower') and self.step == 101:
            self._node_done(STAGE_NODE[stage])
        elif stage in POLICY_END and self.policy_step >= POLICY_END[stage]:
            self._node_done(STAGE_NODE[stage])

    def finish(self, reason, error=None):
        if not self.active:
            return
        self.phase = 'releasing'
        self.reason, self.error = reason, error
        self.log('handback', reason=reason, error=error)
        try:
            self.native_finished = self.bridge.close()
            self.last_state = getattr(self.bridge, 'state', self.last_state)
            if not self.native_finished or self.native_finished.get('returncode') != 0:
                self.error = self.error or 'native_transport_failed'
            elif self.native_finished.get('final_weight') != 0:
                self.error = self.error or 'handback_zero_weight_unconfirmed'
        except Exception as exc:
            self.error = 'handback_failed:' + str(exc)
        self.active = False
        self.phase = 'fault' if self.error else 'finished'
        if not self.error:
            self.node = 'released'
        self.log('finished', **self.status())

    def status(self):
        result = dict(active=self.active, phase=self.phase, stage=self.stage, node=self.node,
                      error=self.error, reason=self.reason, target_xyz_m=self.target,
                      policy_elapsed_s=round(self.policy_step * .02, 3),
                      policy_samples=self.policy_samples, native_finished=self.native_finished,
                      physical_button_success=None)
        if self.active:
            result['session_remaining_s'] = round(max(0, SESSION_SECONDS - (self.clock() - self.started)), 1)
            result['hold_remaining_s'] = (round(max(0, HOLD_SECONDS - (self.clock() - self.hold_started)), 1)
                                          if self.phase in ('holding', 'paused') else None)
        if self.last_state:
            result['telemetry'] = {k: self.last_state.get(k) for k in
                                   ('fsm', 'fsm_mode', 'weight', 'tick', 'q', 'dq', 'rpy', 'gyro')}
        return result
