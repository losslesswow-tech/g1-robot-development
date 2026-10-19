"""Evaluate the console flow with MuJoCo and the real policy, never DDS."""
import importlib.util
import json
from pathlib import Path
import socket
import sys

import mujoco
import numpy as np
import yaml

root = Path('/home/operator/g1-dreamcontrol-offline-20261015')
deployment = json.loads((root / 'console/deployment.json').read_text())
sys.path.insert(0, str(Path(deployment['worker_path']).parent))
from action_flow import ActionFlow, NODES
from console_worker import resources
from controller import SDK_NAMES

runtime = json.loads(Path(deployment['runtime_path']).read_text())
defaults, policy, unused = resources(runtime, False)
for package in ('unitree_sdk2py', 'cyclonedds'):
    assert importlib.util.find_spec(package) is None
def no_socket(*args, **kwargs):
    raise RuntimeError('offline_network_disabled')
socket.socket = no_socket
vendor = root / 'app/vendor/DreamControl/Sim2Real'
cfg = yaml.safe_load((vendor / 'deploy/deploy_mujoco/configs/g1.yaml').read_text())


class Clock:
    value = 0.
    def __call__(self):
        return self.value


class Plant:
    def __init__(self, target):
        self.model = model = mujoco.MjModel.from_xml_path(str(vendor / 'resources/robots/g1_description/scene_fixed_base_button_press.xml'))
        self.data = data = mujoco.MjData(model)
        model.opt.timestep = .002
        names = cfg['joint_names']
        xml_names = [n[:-5] + '_joint' if '_hand_' in n and n.endswith('_link') else n for n in names]
        joints = np.array([mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, n) for n in xml_names])
        assert (joints >= 0).all()
        self.qadr, self.vadr = model.jnt_qposadr[joints], model.jnt_dofadr[joints]
        self.sdk = np.array([names.index(n) if n in names else -1 for n in SDK_NAMES])
        self.present = self.sdk >= 0
        assert set(np.flatnonzero(~self.present)) == {13, 14}
        self.upper_present = self.present[12:29]
        self.actuators = np.array([np.flatnonzero(model.actuator_trnid[:, 0] == j)[0] for j in joints])
        self.desired = np.array(cfg['default_angles'], dtype=float)
        self.kp, self.kd = np.array(cfg['kps']), np.array(cfg['kds'])
        # Match the upper-body gains used by the actual native adapter.
        self.kp[self.sdk[12]], self.kd[self.sdk[12]] = 200, 5
        self.kp[self.sdk[15:29]], self.kd[self.sdk[15:29]] = 40, 10
        data.qpos[self.qadr] = self.desired
        self.limits = np.tile([-.52, .52], (17, 1))
        self.limits[self.upper_present] = model.jnt_range[joints[self.sdk[12:29][self.upper_present]]]
        obj = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, 'object')
        model.body_pos[obj] = np.asarray(target) + [.03, 0, .753]
        mujoco.mj_forward(model, data)
        self.sent = self.violations = 0
        self.weight = 0
        self.state = self.snapshot()

    def snapshot(self):
        q = np.asarray(defaults).copy()
        dq = np.zeros(29)
        q[self.present] = self.data.qpos[self.qadr[self.sdk[self.present]]]
        dq[self.present] = self.data.qvel[self.vadr[self.sdk[self.present]]]
        if not np.isfinite(q).all() or not np.isfinite(dq).all():
            raise RuntimeError('nonfinite_simulation_state')
        self.state = dict(q=q.tolist(), dq=dq.tolist(), quaternion=[1.,0.,0.,0.],
                          fsm=501, fsm_mode=int(self.sent > 0), service_stopped=True,
                          foreign_age_s=999, tick=self.sent, weight=self.weight,
                          rpy=[0.,0.,0.], gyro=[0.,0.,0.])
        return self.state

    def wait(self):
        return self.snapshot()

    def send(self, q, weight):
        values = np.asarray(q)
        if not np.isfinite(values).all() or ((values < self.limits[:,0]) | (values > self.limits[:,1])).any():
            self.violations += 1
            raise RuntimeError('joint_target_limit_violation')
        self.desired[self.sdk[12:29][self.upper_present]] = values[self.upper_present]
        self.weight = weight
        for unused in range(10):
            torque = self.kp * (self.desired - self.data.qpos[self.qadr]) - self.kd * self.data.qvel[self.vadr]
            self.data.ctrl[self.actuators] = torque
            mujoco.mj_step(self.model, self.data)
        self.sent += 1

    def close(self):
        self.weight = 0
        self.state = self.snapshot()
        self.state['fsm_mode'] = 0
        return dict(returncode=0, final_weight=0, reason='offline_release', physical_commands_sent=0)


results = []
for target, stepped in [([.5,-.23,.31],False),([.5,-.23,.5],False),
                        ([.47,-.20,.25],False),([.5,-.23,.31],True),([.5,-.23,.5],True)]:
    clock = Clock()
    plant = Plant(target)
    flow = ActionFlow(clock)
    flow.start(plant, policy, defaults, target, 'prepared' if stepped else 'released')
    for unused in range(4400):
        flow.heartbeat()
        if flow.phase == 'holding' and clock.value - flow.hold_started >= 1:
            node = NODES[NODES.index(flow.node) + 1]
            flow.command('next', node)
        if not flow.active:
            break
        try:
            flow.tick()
        except Exception as exc:
            flow.finish('offline_error', str(exc))
        clock.value += .02
    record = dict(target_xyz_m=target, stepped=stepped, phase=flow.phase, error=flow.error,
                  policy_samples=flow.policy_samples, simulated_seconds=clock.value,
                  joint_target_limit_violations=plant.violations, physical_commands_sent=0)
    results.append(record)
    print(json.dumps(record), flush=True)
assert all(r['phase'] == 'finished' and r['policy_samples'] == 500 and r['joint_target_limit_violations'] == 0 for r in results)
print(json.dumps(dict(success=True, runs=results, dds_initialized=False, physical_commands_sent=0,
                     limitation='Fixed pelvis; no real balance, contact success or held-pose hardware validation.')))
