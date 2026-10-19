"""DreamControl Button_Press_UB via the verified native G1 HG transport.

Explicit --execute is required. --observe writes no P frames. The learned
policy, observations, gains, 50 Hz and default transitions follow upstream;
acquisition and handback use explicit weights and complete upper-body fields.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time

import numpy as np
import torch
import yaml

SDK_NAMES=[f'{side}_{part}_joint' for side in ['left','right'] for part in
           ['hip_pitch','hip_roll','hip_yaw','knee','ankle_pitch','ankle_roll']]
SDK_NAMES+=['waist_yaw_joint','waist_roll_joint','waist_pitch_joint']
SDK_NAMES += [f'{side}_{part}_joint' for side in ['left','right'] for part in
              ['shoulder_pitch','shoulder_roll','shoulder_yaw','elbow','wrist_roll','wrist_pitch','wrist_yaw']]
ACTIVE=np.asarray([12]+list(range(15,29)))


def observation(state, previous_action, elapsed, defaults, target):
    qw,qx,qy,qz=state['quaternion']
    obs=np.zeros(57,dtype=np.float32)
    obs[:3]=[2*(-qz*qx+qw*qy),-2*(qz*qy+qw*qx),1-2*(qw*qw+qz*qz)]
    obs[3:18]=np.asarray(state['q'])[ACTIVE]-defaults[ACTIVE]
    obs[18:33]=np.asarray(state['dq'])[ACTIVE]
    obs[33:50]=previous_action
    phase=min(elapsed,10)/10
    obs[50:52]=[phase,np.sin(2*np.pi*phase)]
    obs[52:55]=target
    obs[55]=float(elapsed>2.925)
    obs[56]=float(2.925-.7<elapsed<2.925+.7)
    if not np.isfinite(obs).all():raise RuntimeError('nonfinite_policy_observation')
    return obs


class Bridge:
    def __init__(self,runtime,execute,log):
        argv=['docker','exec','-i',runtime['container'],runtime['native_path'],
              '--live' if execute else '--observe','eno1']
        if execute and runtime.get('native_deadline_s') is not None:
            seconds=runtime['native_deadline_s']
            if seconds != 90:raise ValueError('invalid_interactive_deadline')
            argv += ['--max-seconds',str(seconds)]
        self.process=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                      stderr=(log.parent/'transport-stderr.log').open('w'),
                                      text=True,bufsize=1)
        self.lock=threading.Lock();self.state=None;self.updated=0;self.finished=None
        self.events=log.open('w',buffering=1)
        self.thread=threading.Thread(target=self.reader,daemon=True);self.thread.start()

    def reader(self):
        for line in self.process.stdout:
            try:
                data=json.loads(line)
                data['received_monotonic']=time.monotonic()
                self.events.write(json.dumps(data)+'\n')
                with self.lock:
                    if data['type']=='state':self.state=data;self.updated=time.monotonic()
                    if data['type']=='finished':self.finished=data
            except Exception as exc:
                self.events.write(json.dumps({'type':'parse_error','error':str(exc),'line':line})+'\n')

    def snapshot(self):
        with self.lock:
            if self.finished:raise RuntimeError('native_transport_finished:'+self.finished['reason'])
            if self.state is None or time.monotonic()-self.updated>.2:raise RuntimeError('transport_state_stale')
            data=dict(self.state)
        if data['age_s']>.15 or data['tick_age_s']>.15:raise RuntimeError('lowstate_stale')
        return data

    def wait(self):
        deadline=time.monotonic()+6
        while time.monotonic()<deadline:
            try:
                data=self.snapshot()
                if data['fsm']==501 and data['rpc_age_s']<2 and data['api_match']:return data
            except RuntimeError:pass
            if self.process.poll() is not None:break
            time.sleep(.02)
        raise RuntimeError('native_readiness_timeout')

    def write(self,line):
        if self.process.poll() is not None:raise RuntimeError('native_transport_not_running')
        self.process.stdin.write(line+'\n');self.process.stdin.flush()

    def send(self,q,weight):
        self.write('P '+format(weight,'.9g')+' '+' '.join(format(float(v),'.9g') for v in q))

    def close(self):
        try:
            if self.process.poll() is None:self.write('RELEASE')
        except Exception:pass
        try:self.process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            # Closing stdin triggers native handback even when the parent fails.
            self.process.stdin.close();self.process.wait(timeout=4)
        self.thread.join(timeout=2)
        self.events.close()
        return self.finished


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',required=True)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--self-test',action='store_true')
    group.add_argument('--observe',action='store_true')
    group.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    runtime=json.loads(Path(args.runtime).read_text())
    policy_path=Path(runtime['policy_path'])
    if hashlib.sha256(policy_path.read_bytes()).hexdigest()!=runtime['policy_sha256']:
        raise RuntimeError('policy_checksum_mismatch')
    binary=subprocess.check_output(['docker','exec',runtime['container'],'cat',runtime['native_path']])
    if hashlib.sha256(binary).hexdigest()!=runtime['binary_sha256']:
        raise RuntimeError('native_binary_checksum_mismatch')
    cfg=yaml.safe_load(Path(runtime['author_config']).read_text())
    lookup=[cfg['joint_names'].index(name) for name in SDK_NAMES]
    defaults=np.asarray(cfg['default_angles'],dtype=np.float64)[lookup]
    assert cfg['num_obs']==57 and cfg['num_actions']==17 and cfg['control_dt']==.02
    assert cfg['action_scale']==.5 and cfg['dof_pos_scale']==cfg['dof_vel_scale']==1
    configured_target=runtime.get('target_xyz_m',[.5,-.23,.31])
    if (len(configured_target)!=3 or configured_target[:2]!=[.5,-.23]
            or configured_target[2] not in (.31,.50)):
        raise RuntimeError('unreviewed_target_profile')
    target=np.asarray(configured_target,dtype=np.float32)
    torch.set_num_threads(1)
    policy=torch.jit.load(str(policy_path),map_location='cpu').eval()
    fixture={'q':defaults.tolist(),'dq':[0.]*29,'quaternion':[1.,0.,0.,0.]}
    with torch.inference_mode():
        for unused in range(4):
            obs=observation(fixture,np.zeros(17,dtype=np.float32),0,defaults,target)
            result=policy(torch.from_numpy(obs).unsqueeze(0)).numpy().reshape(-1)
            if result.shape!=(17,) or not np.isfinite(result).all():raise RuntimeError('invalid_policy_output')
    if args.self_test:
        print(json.dumps({'self_test':True,'target_xyz_m':target.tolist(),
              'observation_size':57,'action_size':17,'dds_initialized':False,'sent':0}))
        return
    run_dir=Path(runtime['log_root'])/('trial-' if args.execute else 'observe-')
    run_dir=Path(str(run_dir)+str(time.time_ns()));run_dir.mkdir(parents=True)
    journal=(run_dir/'policy.jsonl').open('w',buffering=1)
    bridge=Bridge(runtime,args.execute,run_dir/'transport.jsonl')
    initial=None;final=None;error=None;completed=False;phase='initializing'
    def log(event,**fields):
        journal.write(json.dumps({'event':event,'phase':phase,'monotonic':time.monotonic(),**fields})+'\n')
    def announce(name):
        nonlocal phase
        phase=name;log('phase');print('PHASE '+name,flush=True)
    def transition(begin,end,seconds,weight_begin,weight_end):
        start=time.monotonic();n=round(seconds/.02)
        for i in range(n+1):
            bridge.snapshot()
            alpha=i/n
            q=begin*(1-alpha)+end*alpha
            weight=weight_begin*(1-alpha)+weight_end*alpha
            bridge.send(q,weight)
            time.sleep(max(0,start+(i+1)*.02-time.monotonic()))
    try:
        initial=bridge.wait();log('initial_state',state=initial)
        if not args.execute:
            announce('readonly_observation')
            time.sleep(2)
            final=bridge.snapshot()
            completed=True
        else:
            if not initial['service_stopped']:raise RuntimeError('builtin_arm_service_not_stopped')
            if initial['foreign_age_s']<1:raise RuntimeError('another_arm_sdk_writer')
            seed=np.asarray(initial['q'][12:29])
            nominal=defaults[12:29]
            announce('acquire_from_measured_pose')
            transition(seed,seed,2,0,1)
            announce('dreamcontrol_default_pose')
            transition(seed,nominal,2,1,1)
            announce('dreamcontrol_policy_z'+str(configured_target[2]))
            action=np.zeros(17,dtype=np.float32)
            start=time.monotonic()
            with torch.inference_mode():
                for i in range(500):
                    state=bridge.snapshot()
                    elapsed=time.monotonic()-start
                    obs=observation(state,action,elapsed,defaults,target)
                    action=policy(torch.from_numpy(obs).unsqueeze(0)).numpy().reshape(-1)
                    if action.shape!=(17,) or not np.isfinite(action).all():raise RuntimeError('invalid_policy_output')
                    q=nominal.copy()
                    for j,sdk_id in enumerate(ACTIVE):q[sdk_id-12]=float(action[j])*.5+defaults[sdk_id]
                    bridge.send(q,1)
                    log('policy_sample',elapsed_s=elapsed,observation=obs.tolist(),action=action.tolist(),target_q_upper=q.tolist(),state_tick=state['tick'])
                    time.sleep(max(0,start+(i+1)*.02-time.monotonic()))
            announce('dreamcontrol_return_default')
            state=bridge.snapshot()
            transition(np.asarray(state['q'][12:29]),nominal,2,1,1)
            final=bridge.snapshot()
            completed=True
    except BaseException as exc:
        error=str(exc);log('exception',error=error)
        print('STOP '+error,flush=True)
    finally:
        announce('handback_zero_weight')
        native=bridge.close()
        log('native_finished',result=native)
        journal.close()
    summary={'executed':args.execute,'policy_completed':completed,'error':error,
             'target_xyz_m':configured_target,'mode':'DreamControl Button_Press_UB',
             'initial':initial,'last_before_handback':final,'native_finished':native,
             'after_handback':bridge.state,
             'physical_button_success':None,'log_dir':str(run_dir),
             'legacy_debug_angle_and_velocity_limits_applied':False,
             'hand_control_commands_sent':0}
    (run_dir/'summary.json').write_text(json.dumps(summary,indent=2))
    Path(runtime['latest_result']).write_text(json.dumps(summary,indent=2))
    print(json.dumps({'executed':args.execute,'policy_completed':completed,'error':error,
                     'native_finished':native,'log_dir':str(run_dir)}),flush=True)
    if error or not native or native.get('returncode')!=0:raise SystemExit(3)


if __name__=='__main__':
    main()
