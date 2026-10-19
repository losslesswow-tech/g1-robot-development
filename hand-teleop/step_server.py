#!/usr/bin/env python3
"""Bounded, manual, single-command trials; never declares angle calibration."""
import argparse
import json
from pathlib import Path
import secrets
import time
import uuid

from server import (ROOT, Rejected, RepositoryDriver, Workbench,
                    ThreadingHTTPServer, claim_live_process, load_mapping,
                    make_handler, six)


def load_plan(path):
    plan = json.loads(Path(path).read_text(encoding='utf-8'))
    if plan.get('schema') in (2, 3, 4, 5, 6):
        six(plan.get('hold_targets'))
        six(plan.get('reference_actual'))
        if plan['schema'] in (3, 4, 5, 6):
            if ((plan['schema'] == 4 and plan.get('multi_channels') != [0, 1, 2, 3]) or
                    (plan['schema'] in (5, 6) and plan.get('multi_channels') != list(range(6))) or
                    (plan['schema'] == 3 and 'multi_channels' in plan)):
                raise Rejected('多路控制仅允许小指、无名指、中指、食指组合')
            upper = 1000 if plan['schema'] == 6 else 500
            if ((plan.get('side') not in ('L', 'R') if plan['schema'] in (5, 6) else plan.get('side') != 'R') or plan.get('channels') != list(range(6)) or
                    plan.get('max_step') != [upper]*5+[50] or
                    plan.get('min_raw') != [0, 0, 0, 0, 0, 950] or
                    plan.get('max_raw') != [upper]*5+[1000] or
                    not plan.get('evidence') or any(not lo <= v <= hi for lo, v, hi in
                    zip(plan['min_raw'], plan['hold_targets'], plan['max_raw']))):
                raise Rejected('前五路仅支持已授权的 0–%s，旋转保持 950–1000' % upper)
            return plan
        # A commissioning window, not a claim of angle or endpoint calibration.
        if (plan.get('side') != 'R' or plan.get('channels') != list(range(6)) or
                plan.get('max_step') != 50 or not plan.get('evidence') or
                plan.get('min_raw') != [max(0, v-50) for v in plan['hold_targets']] or
                plan.get('max_raw') != [min(1000, v+50) for v in plan['hold_targets']]):
            raise Rejected('右手试控仅允许基准目标附近 50，逐路手动发送')
        return plan
    # This entry point implements only the specific tested left-index interval.
    if (plan.get('schema') != 1 or plan.get('side') != 'L' or
            plan.get('channel') != 3 or plan.get('min_raw') != 82 or
            plan.get('max_raw') != 200 or not plan.get('evidence')):
        raise Rejected('此单步程序仅支持已测试的左手食指 82–200 区间')
    six(plan.get('hold_targets'))
    six(plan.get('reference_actual'))
    if not 82 <= plan['hold_targets'][3] <= 200:
        raise Rejected('单步参考目标超出范围')
    return plan


class StepDriver:
    def __init__(self, original, plan):
        self.original, self.plan = original, plan

    def read(self, side):
        return self.original.read(side)

    def validate(self, side, values):
        values = six(values)
        if side != 'L' or not 82 <= values[3] <= 200:
            raise Rejected('仅允许左手食指 82–200；不截断目标')
        if any(values[i] != self.plan['hold_targets'][i] for i in (0, 1, 2, 4, 5)):
            raise Rejected('其他五路已锁定为设备目标，不能使用实测反馈替换')

    def guard(self, state=None):
        # Read commanded positions separately: feedback is NOT a hold target.
        commanded = six(self.original.read_targets('L'))
        self.validate('L', commanded)
        state = self.read('L') if state is None else state
        actual = six(state['actual'])
        if not 75 <= actual[3] <= 207 or any(
                abs(actual[i]-self.plan['reference_actual'][i]) >= 8
                for i in (0, 1, 2, 4, 5)):
            raise Rejected('实物位置偏离已观察姿态，未发送目标；请重新核查')
        return commanded

    def move(self, side, values):
        self.validate(side, values)
        self.guard()
        return self.original.move(side, values)


class RightTrialDriver:
    # Historical name; schemas 5/6 support either hand with independent state.
    def __init__(self, original, plan):
        self.original, self.plan = original, plan
        self.side = plan['side']
        self.expected_targets = six(plan['hold_targets'])

    def read(self, side):
        return self.original.read(side)

    def validate(self, side, values):
        values = six(values)
        if side != self.side or any(not low <= value <= high for low, value, high in
                             zip(self.plan['min_raw'], values, self.plan['max_raw'])):
            raise Rejected('目标超出本次试控范围；不截断目标')
        changed = [i for i in range(6) if values[i] != self.expected_targets[i]]
        if not changed:
            raise Rejected('没有新的编辑目标，未发送动作')
        if len(changed) > 1 and not (self.plan['schema'] in (4, 5, 6) and
                all(i in self.plan['multi_channels'] for i in changed)):
            raise Rejected('仅四指可以组合发送；拇指弯曲和旋转须单独控制')
        for index in changed:
            limit = self.plan['max_step'][index] if isinstance(self.plan['max_step'], list) else self.plan['max_step']
            if abs(values[index]-self.expected_targets[index]) > limit:
                raise Rejected('此路单次幅度不能超过 %s；不截断目标' % limit)

    def guard(self, state=None):
        commanded = six(self.original.read_targets(self.side))
        if commanded != self.expected_targets:
            raise Rejected('设备目标已被其他程序改变，未发送动作；请重新核查')
        state = self.read(self.side) if state is None else state
        actual = six(state['actual'])
        expected = [a+t-b for a, t, b in zip(self.plan['reference_actual'],
                                            commanded, self.plan['hold_targets'])]
        if any(abs(a-b) >= 8 for a, b in zip(actual, expected)):
            raise Rejected('反馈偏离本次参考姿态，未发送动作；请核查实物')
        return commanded

    def move(self, side, values):
        self.validate(side, values)
        self.guard()
        actual = self.original.move(side, values)
        # Keep the literal commanded target, even if feedback has an offset.
        self.expected_targets = six(values)
        return actual


class ScopedDrivers:
    def __init__(self, original, plans):
        self.original = original
        self.scopes = {p['side']: (RightTrialDriver(original, p) if p['schema'] in (2, 3, 4, 5, 6)
                                  else StepDriver(original, p)) for p in plans}

    def read(self, side):
        return self.original.read(side)

    def validate(self, side, values):
        if side not in self.scopes:
            raise Rejected('此手没有开放试控')
        self.scopes[side].validate(side, values)

    def guard(self, side, state):
        return self.scopes[side].guard(state)

    def move(self, side, values):
        self.validate(side, values)
        return self.scopes[side].move(side, values)


class StepWorkbench(Workbench):
    TASK_ACTIONS = {'right_index_extend':1000, 'right_index_retract':0}
    def __init__(self, mapping, original, plan, extra_plans=(), **kwargs):
        plans = [plan]+list(extra_plans)
        if len({p['side'] for p in plans}) != len(plans):
            raise Rejected('同一只手不能配置两个试控方案')
        super().__init__('single-step', mapping, ScopedDrivers(original, plans), **kwargs)
        self.plan = plan
        self.plans = {p['side']: p for p in plans}

    def config(self):
        value = super().config()
        value['single_step'] = self.plan
        value['single_step_scopes'] = self.plans
        value['task_actions'] = dict(self.TASK_ACTIONS)
        value['task_completion'] = 'right_index_below8_and_targets_unchanged'
        return value

    def arm(self, side, client):
        return self._arm(side, client)

    def task_arm(self, action, client):
        if action not in self.TASK_ACTIONS or self.plans.get('R', {}).get('schema') != 6:
            raise Rejected('导航任务仅开放右手食指伸出1000和收回0')
        return self._arm('R', client, action)

    def _arm(self, side, client, task_action=None):
        if side not in self.plans or not isinstance(client, str) or not 8 <= len(client) <= 100:
            raise Rejected('此手未开放单步控制')
        with self.lock:
            self._expire()
            if self.lease or self.busy:
                raise Rejected('已有会话或动作仍在执行')
            lease = {'id': uuid.uuid4().hex, 'client': client, 'side': side,
                     'expires': self.clock()+6, 'seq': -1, 'submitted': False}
            self.lease = lease
        try:
            with self.io_lock:
                if task_action:
                    target = six(self.driver.original.read_targets(side))
                    before = six(self.driver.read(side)['actual'])
                    time.sleep(.15)
                    after_target = six(self.driver.original.read_targets(side))
                    state = self.driver.read(side)
                    actual = six(state['actual'])
                    plan = self.plans[side]
                    if any(not lo <= v <= hi for lo, v, hi in
                           zip(plan['min_raw'], target, plan['max_raw'])):
                        raise Rejected('设备目标超出已开放范围，未发送动作')
                    if target != after_target or any(abs(a-b) >= 8 for a,b in zip(before,actual)):
                        raise Rejected('设备目标或位置仍在变化，未发送动作')
                    lease.update(task_action=task_action, task_target=target[:], task_actual=actual[:])
                else:
                    state = self.driver.read(side)
                    target = self.driver.guard(side, state)
            with self.lock:
                if self.lease is not lease or self.clock() >= lease['expires']:
                    raise Rejected('准备耗时过长，未发送目标')
                self.cache[side].update(state, sampled_at=self.clock(), error=None)
                self.fault = None
            return {'lease': lease['id'], 'actual': state['actual'], 'target': target}
        except Exception:
            with self.lock:
                if self.lease is lease:
                    self.lease = None
            raise

    def pose(self, payload):
        return self._pose(payload, task=False)

    def task_pose(self, payload):
        return self._pose(payload, task=True)

    def _pose(self, payload, task):
        with self.lock:
            lease = self._owner(payload)
            if bool(lease.get('task_action')) != task:
                raise Rejected('会话类型与动作接口不符')
            if lease['submitted']:
                raise Rejected('每次准备只接受一个目标；没有排队或补发')
            if task:
                expected = lease['task_target'][:]
                expected[3] = self.TASK_ACTIONS[lease['task_action']]
                if payload.get('side') != 'R' or six(payload.get('values')) != expected:
                    raise Rejected('导航动作仅能改变本次准备的右手食指目标')
            else:
                self.driver.validate(payload.get('side'), payload.get('values'))
            lease['submitted'] = True
        try:
            result = super().pose(payload)
            result['single_step_complete'] = True
            return dict(result, session_active=False)
        finally:
            with self.lock:
                self.lease = None

    def _move_pose(self, side, values, lease):
        if not lease.get('task_action'):
            return super()._move_pose(side, values, lease)
        current = six(self.driver.original.read_targets(side))
        actual = six(self.driver.read(side)['actual'])
        if current != lease['task_target'] or any(
                abs(a-b) >= 8 for a,b in zip(actual, lease['task_actual'])):
            raise Rejected('设备在准备后发生变化，未发送动作；请重新执行任务')
        # Exact repository call, including its six-channel speed=300 write and wait.
        result = self.driver.original.move(side, values)
        self.driver.scopes[side].expected_targets = six(values)
        return result

    def _pose_result(self, actual, values, lease):
        result = super()._pose_result(actual, values, lease)
        if not lease.get('task_action'):
            return result
        passive = [abs(actual[i]-lease['task_actual'][i]) for i in (0,1,2,4,5)]
        device_target = six(self.driver.original.read_targets('R'))
        targets_unchanged = device_target == values
        return dict(result, repository_reached=result['reached'],
                    reached=result['errors'][3] < 8 and targets_unchanged,
                    passive_drift=passive, motion_sent=True,
                    device_target=device_target, targets_unchanged=targets_unchanged,
                    completion='right_index_below8_and_targets_unchanged')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8768)
    parser.add_argument('--plan', type=Path, required=True, action='append')
    parser.add_argument('--builtin-connection', action='store_true',
                        help='Remember authenticated browsers; never issue credentials to visitors')
    parser.add_argument('--token-file', type=Path,
                        help='Private server-side credential file, outside the served web directory')
    args = parser.parse_args()
    plans = [load_plan(path) for path in args.plan]
    mapping = load_mapping(ROOT/'calibration.example.json')
    original = RepositoryDriver(ROOT/'vendor/repository/inspire_hand_ctl.py')
    process_lock = claim_live_process()
    workbench = StepWorkbench(mapping, original, plans[0], extra_plans=plans[1:])
    token = args.token_file.read_text().strip() if args.token_file else secrets.token_urlsafe(24)
    if len(token) < 24 or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-' for c in token):
        raise Rejected('服务端口令无效')
    http = ThreadingHTTPServer((args.bind, args.port), make_handler(workbench, token, args.builtin_connection))
    print('Hand workbench: http://%s:%s mode=single-step' % (args.bind, http.server_port), flush=True)
    print('Connection token: '+token, flush=True)
    print('Scoped raw trials; one target group per manual preparation; no angle calibration.', flush=True)
    try:
        http.serve_forever()
    finally:
        http.server_close()
        process_lock.close()


if __name__ == '__main__':
    main()
