"""Fixed-target DreamControl UB evaluation, with no robot transport.

Observation/PD equations follow GenRobo/DreamControl at the pinned revision.
Upstream source and policy retain vendor/DreamControl/LICENSE (research only).
This entry point deliberately has no hardware mode or SDK/DDS dependency.
"""
import argparse
import csv
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import socket
import struct
import time
import zlib


ACTIVE_JOINTS = ['waist_yaw_joint'] + [
    f'{side}_{part}_joint' for side in ['left', 'right'] for part in [
        'shoulder_pitch', 'shoulder_roll', 'shoulder_yaw', 'elbow',
        'wrist_roll', 'wrist_pitch', 'wrist_yaw']]


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_png(path, rgb):
    """Encode a renderer RGB frame, without an extra image dependency."""
    h, w, channels = rgb.shape
    assert channels == 3
    def chunk(kind, content):
        return (struct.pack('!I', len(content)) + kind + content
                + struct.pack('!I', zlib.crc32(kind+content) & 0xffffffff))
    rows = b''.join(b'\x00'+rgb[row].tobytes() for row in range(h))
    path.write_bytes(b'\x89PNG\r\n\x1a\n'
                     + chunk(b'IHDR', struct.pack('!2I5B', w, h, 8, 2, 0, 0, 0))
                     + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config/target-z031.json')
    parser.add_argument('--output', default='results/z031')
    parser.add_argument('--render', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    task = json.loads((root/args.config).read_text())
    if task['mode'] != 'offline_simulation_only':
        raise SystemExit('Only offline_simulation_only is supported.')
    target = task['target_xyz_m']
    if (len(target) != 3 or not all(math.isfinite(v) for v in target)
            or target[2] not in (0.31, 0.50) or task['target_randomization']):
        raise SystemExit('Use a fixed reviewed test profile: z=0.31 or z=0.50 m.')
    policy_path = root/'assets/Button_Press_UB.pt'
    if (policy_path.stat().st_size != task['policy_size_bytes']
            or sha256(policy_path) != task['policy_sha256']):
        raise SystemExit('Policy checksum/length mismatch.')
    # This environment should have no robot SDK. Refuse accidental reuse of one.
    for package in ['unitree_sdk2py', 'cyclonedds']:
        if importlib.util.find_spec(package) is not None:
            raise SystemExit(f'Use an isolated environment without {package}.')

    os.environ.setdefault('MUJOCO_GL', 'egl')
    import mujoco
    import numpy as np
    import torch
    import yaml
    torch.set_num_threads(1)
    torch.manual_seed(0)

    # Network creation is also blocked at the Python layer for this run.
    def deny_socket(*unused, **unused_kw):
        raise RuntimeError('Network/robot transport is disabled in offline_run.')
    socket.socket = deny_socket

    vendor = root/'vendor/DreamControl/Sim2Real'
    config_path = vendor/'deploy/deploy_mujoco/configs/g1.yaml'
    cfg = yaml.safe_load(config_path.read_text())
    xml = vendor/'resources/robots/g1_description/scene_fixed_base_button_press.xml'
    model = mujoco.MjModel.from_xml_path(str(xml))
    data = mujoco.MjData(model)
    dt = task['simulation_dt_s']
    control_dt = task['control_dt_s']
    decimation = round(control_dt/dt)
    assert abs(decimation*dt-control_dt) < 1e-10
    model.opt.timestep = dt
    names = cfg['joint_names']
    # Upstream YAML lists finger body-link names; XML joint names use _joint.
    model_names = [n[:-5]+'_joint' if '_hand_' in n and n.endswith('_link') else n
                   for n in names]
    joint_ids = np.array([mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, n)
                          for n in model_names])
    if (joint_ids < 0).any():
        raise RuntimeError('Model joint names do not match upstream config.')
    assert len(set(joint_ids)) == len(names) == model.nq == model.nv
    qadr = model.jnt_qposadr[joint_ids]
    vadr = model.jnt_dofadr[joint_ids]
    defaults = np.asarray(cfg['default_angles'], dtype=np.float64)
    kp, kd = np.asarray(cfg['kps']), np.asarray(cfg['kds'])
    assert len(defaults) == len(names) == len(kp) == len(kd)
    actuator_ids = []
    for joint_id in joint_ids:
        candidates = np.flatnonzero(model.actuator_trnid[:, 0] == joint_id)
        if len(candidates) != 1:
            raise RuntimeError('Expected one joint actuator for each configured joint.')
        actuator_ids.append(candidates[0])
    actuator_ids = np.asarray(actuator_ids)
    active = np.asarray([names.index(n) for n in ACTIVE_JOINTS])
    left_hand = np.asarray([names.index(n) for n in cfg['left_hand_joint_names']])
    right_hand = np.asarray([names.index(n) for n in cfg['right_hand_joint_names']])
    data.qpos[qadr] = defaults
    data.qvel[:] = 0
    mujoco.mj_forward(model, data)
    pelvis_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, 'pelvis')
    object_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, 'object')
    wrist_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, 'right_wrist_yaw_link')
    if min(pelvis_id, object_id, wrist_id) < 0:
        raise RuntimeError('Required pelvis/object/right wrist body is missing.')
    pelvis_world = data.xpos[pelvis_id].copy()
    # Preserve the upstream simulator's object offset, and report it explicitly.
    object_world = np.asarray(target) + np.array([0.03, 0, 0.753])
    model.body_pos[object_id] = object_world
    mujoco.mj_forward(model, data)
    policy = torch.jit.load(str(policy_path), map_location='cpu').eval()
    action = np.zeros(cfg['num_actions'], dtype=np.float32)
    desired = defaults.copy()
    output = root/args.output
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    observations = []
    measured, commanded = [], []
    violations = []
    contacts = []
    self_contacts = []
    self_contact_last = {}
    checkpoints = [0, 1, task['button_press_time_s'], 5, 10, 12]
    snapshots = {}
    policy_steps = round(task['policy_duration_s']/dt)
    return_steps = round(task['return_duration_s']/dt)
    return_from = None
    start = time.monotonic()
    object_geom_ids = set(np.flatnonzero(model.geom_bodyid == object_id).tolist())
    initial_wrist = data.xpos[wrist_id].copy()

    with torch.inference_mode():
        for step in range(policy_steps+return_steps+1):
            t = step*dt
            phase = 'policy' if step < policy_steps else 'return'
            if step < policy_steps and step % decimation == 0:
                obs = np.zeros(cfg['num_obs'], dtype=np.float32)
                obs[:3] = [0, 0, -1]  # fixed-base model, same upstream identity IMU
                obs[3:18] = (data.qpos[qadr[active]]-defaults[active])*cfg['dof_pos_scale']
                obs[18:33] = data.qvel[vadr[active]]*cfg['dof_vel_scale']
                obs[33:50] = action
                phase_fraction = min(t, 10)/10
                obs[50:52] = [phase_fraction, np.sin(2*np.pi*phase_fraction)]
                obs[52:55] = target
                press_time = task['button_press_time_s']
                obs[55] = float(t > press_time)
                obs[56] = float(press_time-0.7 < t < press_time+0.7)
                assert np.array_equal(obs[52:55], np.asarray(target, dtype=np.float32))
                action = policy(torch.from_numpy(obs).unsqueeze(0)).numpy().reshape(-1)
                if action.shape != (17,) or not np.isfinite(action).all():
                    raise RuntimeError('Policy output shape/finite check failed.')
                desired[active] = action[:-2]*cfg['action_scale']+defaults[active]
                # Upstream simulator uses the closed pose for both blend endpoints.
                # The last two right finger joints are held extended in its button task.
                desired[left_hand] = np.asarray(cfg['left_hand_closed_joint_angles'])
                right_fingers = np.asarray(cfg['right_hand_closed_joint_angles']).copy()
                right_fingers[-2:] = 0
                desired[right_hand] = right_fingers
                observations.append(obs.tolist())
            elif step >= policy_steps:
                if return_from is None:
                    return_from = data.qpos[qadr].copy()
                alpha = min((step-policy_steps)/max(return_steps, 1), 1)
                # Same 2-second linear default return as upstream, simulation only.
                desired = return_from*(1-alpha)+defaults*alpha

            if step % decimation == 0:
                q = data.qpos[qadr].copy()
                dq = data.qvel[vadr].copy()
                if not np.isfinite(q).all() or not np.isfinite(dq).all():
                    raise RuntimeError('Nonfinite simulation state.')
                measured.append(q[active])
                commanded.append(desired[active].copy())
                wrist = data.xpos[wrist_id].copy()
                distance = float(np.linalg.norm(wrist-object_world))
                rows.append([t,phase,*target,*wrist,distance,*q[active],*desired[active],*dq[active]])
                bounded = model.jnt_limited[joint_ids[active]].astype(bool)
                ranges = model.jnt_range[joint_ids[active]]
                outside = bounded & ((desired[active] < ranges[:,0]) | (desired[active] > ranges[:,1]))
                if outside.any():
                    violations.append({'time_s':t,'joints':[ACTIVE_JOINTS[i] for i in np.flatnonzero(outside)]})
            for checkpoint in checkpoints:
                if checkpoint not in snapshots and t >= checkpoint:
                    snapshots[checkpoint] = data.qpos.copy()
            for i in range(data.ncon):
                contact = data.contact[i]
                if (int(contact.geom1) in object_geom_ids or int(contact.geom2) in object_geom_ids):
                    if not contacts or t-contacts[-1]['time_s'] >= 0.02:
                        contacts.append({'time_s':t, 'geom1':int(contact.geom1),
                                         'geom2':int(contact.geom2), 'distance_m':float(contact.dist)})
                b1=int(model.geom_bodyid[contact.geom1]);b2=int(model.geom_bodyid[contact.geom2])
                if b1>0 and b2>0 and object_id not in (b1,b2):
                    pair=tuple(sorted((b1,b2)))
                    if pair not in self_contact_last or t-self_contact_last[pair]>=0.02:
                        self_contact_last[pair]=t
                        self_contacts.append({'time_s':t,'bodies':[
                            mujoco.mj_id2name(model,mujoco.mjtObj.mjOBJ_BODY,b1),
                            mujoco.mj_id2name(model,mujoco.mjtObj.mjOBJ_BODY,b2)],
                            'distance_m':float(contact.dist)})
            torques = kp*(desired-data.qpos[qadr])-kd*data.qvel[vadr]
            data.ctrl[actuator_ids] = torques
            if step < policy_steps+return_steps:
                mujoco.mj_step(model, data)

    header = ['time_s','phase','target_x_m','target_y_m','target_z_m',
              'wrist_world_x_m','wrist_world_y_m','wrist_world_z_m','wrist_target_distance_m']
    header += ['q_'+n for n in ACTIVE_JOINTS]
    header += ['target_'+n for n in ACTIVE_JOINTS]
    header += ['dq_'+n for n in ACTIVE_JOINTS]
    with (output/'trajectory.csv').open('w',newline='') as stream:
        writer=csv.writer(stream); writer.writerow(header); writer.writerows(rows)
    np.savez_compressed(output/'observations.npz', observations=np.asarray(observations))
    q=np.asarray(measured); commands=np.asarray(commanded)
    q0=defaults[active]
    distances=np.asarray([r[8] for r in rows])
    peak_travel=np.max(np.abs(q-q0),axis=0)*180/np.pi
    peak_desired=np.max(np.abs(commands-q0),axis=0)*180/np.pi
    command_step=np.max(np.abs(np.diff(commands,axis=0)),axis=0)*180/np.pi
    preview_errors=[]
    self_pairs={}
    for contact in self_contacts:
        pair=tuple(sorted(contact['bodies']))
        item=self_pairs.setdefault(pair,{'bodies':list(pair),'first_s':contact['time_s'],
                                        'last_s':contact['time_s'],'samples':0,'min_distance_m':0.})
        item['last_s']=contact['time_s'];item['samples']+=1
        item['min_distance_m']=min(item['min_distance_m'],contact['distance_m'])
    rendered=[]
    if args.render:
        try:
            model.vis.global_.offwidth=720
            model.vis.global_.offheight=540
            render_data=mujoco.MjData(model)
            camera=mujoco.MjvCamera()
            camera.lookat[:]=[0.1,0,0.85]
            camera.distance=2.7; camera.azimuth=135; camera.elevation=-12
            with mujoco.Renderer(model,height=540,width=720) as renderer:
                for t,qpos in snapshots.items():
                    render_data.qpos[:]=qpos
                    mujoco.mj_forward(model,render_data)
                    renderer.update_scene(render_data,camera=camera)
                    name=f'frame-{t:06.3f}.png'
                    save_png(output/name,renderer.render())
                    rendered.append(name)
        except Exception as exc:
            preview_errors.append(str(exc))
    report={
        'mode':'offline_simulation_only','revision':task['revision'],
        'target_xyz_m':target,'frame':task['coordinate_frame'],
        'target_fixed_for_entire_run':True,'policy_observation_shape':[len(observations),57],
        'policy_action_size':17,'policy_sha256':sha256(policy_path),
        'simulation_dt_s':dt,'control_dt_s':control_dt,
        'policy_duration_s':task['policy_duration_s'],'return_duration_s':task['return_duration_s'],
        'elapsed_wall_s':time.monotonic()-start,
        'versions':{'torch':torch.__version__,'mujoco':mujoco.__version__,'numpy':np.__version__},
        'joint_name_aliases':{a:b for a,b in zip(names,model_names) if a!=b},
        'simulator_pelvis_world_m':pelvis_world.tolist(),
        'simulator_object_world_m':object_world.tolist(),
        'initial_wrist_world_m':initial_wrist.tolist(),
        'minimum_wrist_object_center_distance_m':float(distances.min()),
        'minimum_distance_time_s':float(rows[int(distances.argmin())][0]),
        'object_contacts':contacts,'joint_target_limit_violations':violations,
        'robot_self_contacts':self_contacts,
        'robot_self_contact_pairs':list(self_pairs.values()),
        'peak_measured_travel_deg':dict(zip(ACTIVE_JOINTS,peak_travel.tolist())),
        'peak_commanded_travel_deg':dict(zip(ACTIVE_JOINTS,peak_desired.tolist())),
        'peak_single_command_delta_deg':dict(zip(ACTIVE_JOINTS,command_step.tolist())),
        'return_final_error_deg':dict(zip(ACTIVE_JOINTS,((q[-1]-q0)*180/np.pi).tolist())),
        'rendered_frames':rendered,'render_errors':preview_errors,
        'dds_initialized':False,'robot_sdk_installed':False,'motion_commands_sent':0,
        'hardware_trial_ready':False,
        'limitations':['fixed pelvis and locked waist roll/pitch; does not verify whole-body balance',
                       'static cube target; no moving button or electrical success signal',
                       'x/y and camera/base transformations are not calibrated to the real scene',
                       'upstream simulator uses 25 Hz; this evaluation uses hardware-entry 50 Hz',
                       'physical acquisition, waist commands and handback still need separate verification']}
    (output/'report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({k:report[k] for k in ['target_xyz_m','policy_observation_shape',
          'minimum_wrist_object_center_distance_m','minimum_distance_time_s',
          'rendered_frames','render_errors','motion_commands_sent','hardware_trial_ready']}))


if __name__ == '__main__':
    main()
