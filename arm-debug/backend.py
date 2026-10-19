"""Bounded right-arm debug backends. The SSH worker starts in read-only mode.

Python 3.8+ stdlib; credentials are command-line paths, never HTTP data or exports.
"""
import copy
import json
import math
from pathlib import Path
import shlex
import subprocess
import threading
import time
import uuid

ROOT=Path(__file__).resolve().parent
GEOMETRY=ROOT.parent/'arm-button/web'
MODEL=json.loads((GEOMETRY/'model.mjs').read_text(encoding='utf-8').split('=',1)[1].strip().rstrip(';'))
LABELS=['肩前后摆','肩侧摆','肩旋转','肘弯曲','腕旋转','腕俯仰','腕偏转']
JOINTS=[dict(index=22+i,label=LABELS[i],name=j['name'],lower_rad=j['lower'],upper_rad=j['upper']) for i,j in enumerate(MODEL['joints'])]
MAX_STEP=math.radians(20)
WINDOW=math.radians(50)
LIMITS=dict(step_deg=20,session_deg=50,left_hold_deg=3,session_seconds=30,heartbeat_seconds=3,preview_rate_deg_s=5,settle_budget_seconds=2)

class Rejected(Exception):
    pass

def target_error(index,value,target,seed):
    if type(index) is not int or not 22<=index<=28:raise Rejected('right_arm_only')
    if type(value) not in (int,float) or not math.isfinite(value):raise Rejected('nonfinite_target')
    j=JOINTS[index-22]
    if not j['lower_rad']<=value<=j['upper_rad']:raise Rejected('model_joint_limit')
    if abs(value-target)>MAX_STEP+1e-7:raise Rejected('step_exceeds_20_degrees')
    if abs(value-seed)>WINDOW+1e-7:raise Rejected('session_exceeds_50_degrees')

class SimulationDriver:
    """Deterministic UI/test fixture; all samples are explicitly synthetic."""
    def __init__(self):
        self.connected=False;self.phase='readonly';self.fault='';self.builtin=True
        self.q=[.293,.206,-.028,.994,.103,.020,-.011,.296,-.205,.028,.99,-.129,.040,.043]
        self.seed=self.q[:];self.target=self.q[:];self.begin=0.;self.heartbeat_at=0.;self.session_at=0.
        self.frames=0;self.lock=threading.RLock();self.changed_commands=0

    def connect(self):
        with self.lock:self.connected=True;self.phase='readonly';self.fault=''

    def _advance(self):
        now=time.monotonic()
        if self.phase in ('holding','moving','settling','acquiring'):
            if now-self.heartbeat_at>3 or now-self.session_at>30:
                self.phase='releasing';self.fault='browser_heartbeat_lost' if now-self.heartbeat_at>3 else 'session_timeout';self.begin=now
        if self.phase=='acquiring' and now-self.begin>=.15:self.phase='holding'
        if self.phase=='moving':
            t=min(1,(now-self.begin)/self.duration);s=t**3*(10+t*(-15+6*t))
            self.q=[a+(b-a)*s for a,b in zip(self.previous,self.target)]
            if t>=1:self.phase='settling';self.begin=now
        if self.phase=='settling':
            if now-self.begin>=.3 and abs(self.q[self.moving_joint]-self.target[self.moving_joint])<=math.radians(1):self.phase='holding'
            elif now-self.begin>1.5:self.phase='releasing';self.fault='target_not_reached';self.begin=now
        if self.phase=='releasing' and now-self.begin>=.2:self.phase='fault' if self.fault else 'readonly'

    def state(self):
        with self.lock:
            self._advance()
            return dict(connected=self.connected,fresh=self.connected,age_ms=0 if self.connected else -1,
                        phase=self.phase,fault=self.fault,simulation=True,fsm_id=501,fsm_mode=0,
                        builtin_arm_stopped=self.builtin,api_versions_match=True,control_available=True,
                        weight=1 if self.phase in ('holding','moving','settling') else 0,
                        remaining_s=max(0,30-(time.monotonic()-self.session_at)) if self.phase in ('holding','moving','settling','acquiring') else 0,
                        command_frames=self.frames,crc_rejected=0,limits=LIMITS.copy(),
                        joints=[dict(index=15+i,q_rad=q,dq_rad_s=0,motorstate=0,target_rad=self.target[i],seed_rad=self.seed[i]) for i,q in enumerate(self.q)],
                        imu_rpy_rad=[0,0,0],imu_gyro_rad_s=[0,0,0])

    def command(self,op,index=None,value=None):
        with self.lock:
            self._advance();now=time.monotonic()
            if not self.connected:raise Rejected('not_connected')
            if op=='ACQUIRE':
                if self.phase!='readonly':raise Rejected('not_readonly')
                if not self.builtin:raise Rejected('builtin_arm_service_not_stopped')
                self.seed=self.q[:];self.target=self.q[:];self.phase='acquiring';self.session_at=self.begin=self.heartbeat_at=now
            elif op=='SET':
                if self.phase!='holding':raise Rejected('not_ready_or_busy')
                target_error(index,value,self.target[index-15] if type(index) is int and 15<=index<=28 else 0,self.seed[index-15] if type(index) is int and 15<=index<=28 else 0)
                duration=max(1,1.875*abs(value-self.target[index-15])/math.radians(LIMITS['preview_rate_deg_s']))
                if now-self.session_at+duration+LIMITS['settle_budget_seconds']>LIMITS['session_seconds']:raise Rejected('insufficient_session_time')
                self.previous=self.q[:];self.moving_joint=index-15;self.target[index-15]=value;self.phase='moving';self.begin=self.heartbeat_at=now
                self.duration=duration;self.changed_commands+=1
            elif op=='PING':self.heartbeat_at=now
            elif op=='RELEASE':
                if self.phase in ('acquiring','holding','moving','settling'):self.phase='releasing';self.begin=now
            elif op=='QUIT':self.close()
            else:raise Rejected('unknown_command')
            return {'ok':True,'phase':self.phase}

    def stop_service(self):
        with self.lock:
            if self.phase!='readonly':raise Rejected('release_before_service_change')
            self.builtin=True;return {'ok':True,'simulation':True}

    def close(self):
        with self.lock:self.connected=False;self.phase='readonly'

class SSHDriver:
    def __init__(self,key,worker_path,ssh=r'C:\Windows\System32\OpenSSH\ssh.exe'):
        self.key=Path(key);self.worker_path=worker_path;self.ssh=ssh
        if not self.key.is_file():raise ValueError('SSH key path does not exist')
        prefix='/opt/g1-development/arm-debug/releases/'
        parts=worker_path[len(prefix):].split('/') if worker_path.startswith(prefix) else []
        if len(parts)!=2 or parts[1]!='g1_arm_debug' or len(parts[0])!=16 or any(x not in '0123456789abcdef' for x in parts[0]):raise ValueError('invalid immutable worker path')
        self.process=None;self.current={};self.received=0;self.pending={};self.lock=threading.RLock();self.io=threading.Lock()
        self.errors=[];self.error_lock=threading.Lock()
        self.event_sink=None
        self.leg_audit_at=0;self.leg_audit_total=0

    def _ssh(self,remote):
        return [self.ssh,'-i',str(self.key),'-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8','operator@192.0.2.1',shlex.join(remote)]

    def connect(self):
        with self.lock:
            if self.process and self.process.poll() is None:return
            self.current={};self.received=0;self.leg_audit_at=0;self.leg_audit_total=0
            args=self._ssh(['docker','exec','-i','-e','LD_LIBRARY_PATH=/opt/ros/noetic/lib:/usr/local/lib','robot-navigation-example',self.worker_path,'--listen','eno1'])
            self.process=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace',bufsize=1)
            process=self.process
        threading.Thread(target=self._read,args=(process,),daemon=True).start()
        threading.Thread(target=self._read_errors,args=(process,),daemon=True).start()
        deadline=time.monotonic()+12
        while time.monotonic()<deadline:
            s=self.state()
            if s.get('fresh') and s.get('fsm_id',-1)>=0:return
            if process.poll() is not None:break
            time.sleep(.1)
        self.close();raise Rejected('connection_failed_or_no_fresh_state')

    def _read(self,process):
        for line in process.stdout:
            if len(line)>100000:continue
            try:data=json.loads(line,parse_constant=lambda x:(_ for _ in ()).throw(ValueError(x)))
            except ValueError:continue
            event=None;speed_event=None
            with self.lock:
                if process is not self.process:continue
                if data.get('type')=='state':
                    self.current=data;self.received=time.monotonic()
                    monitor=data.get('leg_speed_monitor',{})
                    total=sum(monitor.get('exceedance_samples',[]))
                    if total>self.leg_audit_total and self.received-self.leg_audit_at>=1:
                        self.leg_audit_total=total;self.leg_audit_at=self.received
                        speed_event={k:data.get(k) for k in ('phase','weight','fsm_id','fsm_mode','legs','leg_speed_monitor')}
                elif data.get('type')=='fault_event':event=data.get('snapshot')
                elif data.get('type')=='reply':
                    pending=self.pending.get(data.get('id'))
                    if pending:pending['data']=data;pending['event'].set()
            if event is not None and self.event_sink:
                self.event_sink('native_fault',snapshot=event)
            if speed_event is not None and self.event_sink:
                self.event_sink('leg_speed_observation',snapshot=speed_event)

    def _read_errors(self,process):
        for line in process.stderr:
            with self.error_lock:self.errors=(self.errors+[line.strip()])[-10:]

    def state(self):
        with self.lock:
            s=copy.deepcopy(self.current)
            alive=bool(self.process and self.process.poll() is None)
            age=max(0,time.monotonic()-self.received) if self.received else 1e9
            s['connected']=alive and bool(s)
            s['fresh']=bool(s.get('fresh') and alive and age<.75)
            s['transport_age_ms']=min(age*1000,1e9)
            if not alive:s['phase']='disconnected'
            return s

    def command(self,op,index=None,value=None):
        if op not in ('ACQUIRE','SET','PING','RELEASE','QUIT'):raise Rejected('unknown_command')
        if op=='SET':
            s=self.state();j=next((j for j in s.get('joints',[]) if j['index']==index),None)
            if not s.get('fresh') or not j:raise Rejected('stale_lowstate')
            target_error(index,value,j['target_rad'],j['seed_rad'])
        request_id=uuid.uuid4().hex;pending={'event':threading.Event()}
        with self.lock:
            p=self.process
            if not p or p.poll() is not None:raise Rejected('not_connected')
            self.pending[request_id]=pending
        line=op+' '+request_id+((' '+str(index)+' '+format(value,'.17g')) if op=='SET' else '')+'\n'
        try:
            with self.io:p.stdin.write(line);p.stdin.flush()
            if not pending['event'].wait(5):raise Rejected('worker_ack_timeout')
            result=pending['data']
            if not result.get('ok'):raise Rejected(result.get('error','worker_rejected'))
            return result
        except (OSError,ValueError):raise Rejected('worker_connection_closed')
        finally:
            with self.lock:self.pending.pop(request_id,None)

    def stop_service(self):
        if self.state().get('phase')!='readonly':raise Rejected('release_before_service_change')
        utility='/opt/g1-development/arm-button/releases/20261014-79a962b209cf2d36/bin/g1_arm_service'
        p=subprocess.run(self._ssh(['docker','exec','-e','LD_LIBRARY_PATH=/opt/ros/noetic/lib:/usr/local/lib','robot-navigation-example',utility,'--stop','eno1']),text=True,capture_output=True,timeout=25)
        if p.returncode:raise Rejected('service_stop_failed_or_not_verified')
        return {'ok':True}

    def close(self):
        with self.lock:p=self.process
        if not p:return
        if p.poll() is None:
            try:self.command('QUIT')
            except Rejected:pass
            try:p.stdin.close()
            except OSError:pass
            try:p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                # Losing the SSH input also expires the independent native lease.
                p.terminate()
                try:p.wait(timeout=3)
                except subprocess.TimeoutExpired:p.kill();p.wait(timeout=3)
        with self.lock:
            if self.process is p:self.process=None;self.current={}
