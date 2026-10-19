"""Explicit supervised wired/Wi-Fi sessions. Default invocation only prints a plan."""
import argparse
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT.parent/'wifi_access'))
import network_probe as network
from profiles import plan, profile, peers, dds_xml

CONTAINER_ROOT='/opt/g1-arrival-voice/network-modes-20261010'
ROBOT='operator@192.0.2.18'


def execute(command, capture=False, timeout=30, input_text=None):
    result=subprocess.run(command,check=True,timeout=timeout,text=True,input=input_text,
                          stdout=subprocess.PIPE if capture else None)
    return result.stdout if capture else ''


def private_create(path):
    fd=os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    os.close(fd)


def touch_private(path):
    if not network.heartbeat_alive(path,os.getuid(),path.lstat().st_mtime):
        raise ValueError('Heartbeat ownership/type changed')
    os.utime(str(path),None,follow_symlinks=False)


def wired_ready(snapshot):
    if '192.0.2.6' not in network.addresses(snapshot,'enp132s0'):
        raise ValueError('Wired mode requires .123.10 on enp132s0; no address will be installed automatically')
    if '192.0.2.6' in network.addresses(snapshot,'wlp131s0f0'):
        raise ValueError('Wi-Fi alias remains active; finish its rollback before selecting wired')
    if any(network.route_exists(snapshot,ip) for ip in network.DESTINATIONS):
        raise ValueError('Explicit internal /32 route remains; inspect and roll back before wired use')


class Session:
    def __init__(self,mode,motion):
        self.mode,self.motion=mode,motion
        self.token=uuid.uuid4().hex
        self.directory=Path.home()/('g1-network-session-'+self.token)
        self.directory.mkdir(mode=0o700)
        self.socket=str(self.directory/'ssh')
        self.heartbeat=Path('/tmp/g1-network-heartbeat-'+self.token)
        self.remote_probe='/tmp/g1-network-probe-'+self.token+'.py'
        self.remote_discovery='/tmp/g1-network-discovery-'+self.token+'.py'
        self.remote_peers='/tmp/g1-network-peers-'+self.token+'.json'
        self.container_heartbeat='/tmp/g1-network-heartbeat-'+self.token
        self.container_stop='/tmp/g1-network-stop-'+self.token
        self.xml='/tmp/g1-network-'+self.token+'.xml'
        self.node='g1_network_bridge_'+self.token[:12]
        self.robot_active=self.host_active=self.ssh_active=False
        self.child=None

    def ssh(self,command,capture=False,tty=False,timeout=30):
        return execute(['ssh']+(['-tt'] if tty else ['-T'])+['-S',self.socket,'-o','BatchMode=yes',ROBOT,command],capture,timeout)

    def container_file(self,path,content='',touch=False):
        if touch:
            source='import os,stat; p='+repr(path)+'; s=os.lstat(p); assert stat.S_ISREG(s.st_mode) and s.st_nlink==1 and s.st_uid==os.getuid() and stat.S_IMODE(s.st_mode)==0o600; os.utime(p,None,follow_symlinks=False)'
        else:
            source='import os,sys; f=os.open('+repr(path)+',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600); os.write(f,sys.stdin.buffer.read()); os.close(f)'
        execute(['docker','exec','-i','nav_robot','python3','-c',source],timeout=8,input_text=content)

    def heartbeat_remote(self,create=False):
        path=str(self.heartbeat)
        if create:
            code='import os; f=os.open('+repr(path)+',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600); os.close(f)'
        else:
            code='import os,stat; p='+repr(path)+'; s=os.lstat(p); assert stat.S_ISREG(s.st_mode) and s.st_nlink==1 and s.st_uid==os.getuid() and stat.S_IMODE(s.st_mode)==0o600; os.utime(p,None,follow_symlinks=False)'
        self.ssh('python3 -c '+shlex.quote(code),timeout=8)

    def prepare_wifi(self):
        execute(['ssh','-M','-S',self.socket,'-o','ControlMaster=yes','-o','ControlPersist=3600',
                 '-o','ServerAliveInterval=5','-o','ServerAliveCountMax=2','-fNT',ROBOT],timeout=120)
        self.ssh_active=True
        for name,target in [('network_probe.py',self.remote_probe),('dds_discovery.py',self.remote_discovery)]:
            execute(['scp','-o','ControlPath='+self.socket,str(ROOT.parent/'wifi_access'/name),ROBOT+':'+target])
        print('Receiving DDS discovery for 35 seconds with temporary 239.255.0.1 membership; kernel IGMP is possible, no application DDS/motion sends.',flush=True)
        discovery_error=None
        try:
            self.ssh('umask 077; sudo /usr/bin/python3 '+self.remote_discovery+' --join-spdp --seconds 35 > '+self.remote_peers,tty=True,timeout=120)
        except subprocess.CalledProcessError as error:
            discovery_error=error.returncode
        report=json.loads(self.ssh('cat '+self.remote_peers,capture=True))
        (self.directory/'discovery.json').write_text(json.dumps(report,indent=2))
        if discovery_error is not None:
            raise RuntimeError('DDS discovery failed before network apply (exit '+str(discovery_error)+'): '+json.dumps(report))
        peer_list=peers(report)
        self.container_file(self.xml,dds_xml(report))
        self.heartbeat_remote(create=True)
        self.robot_active=True  # Also clean up if apply reports an uncertain result.
        self.ssh('sudo /usr/bin/python3 '+self.remote_probe+' robot --apply --stationary-confirmed --session-lease --token '+self.token+' --seconds 180',tty=True,timeout=120)
        private_create(self.heartbeat)
        self.host_active=True
        execute(['sudo','python3',str(ROOT.parent/'wifi_access/network_probe.py'),'host','--apply',
                 '--stationary-confirmed','--preserve-source','--session-lease','--token',self.token,'--seconds','180'],timeout=120)
        # Begin heartbeats before bounded read-only readiness checks; setup grace is 180s.
        self.heartbeat_remote(); touch_private(self.heartbeat)
        return ['--peers']+peer_list

    def start(self):
        state=network.inventory()
        if state.get('bridges'):
            raise ValueError('Legacy g1_ros1_sdk is still running. This candidate never stops/restarts it; controlled handover is required after charging')
        network.ensure_idle()
        if self.mode=='wired':
            wired_ready(state); query_args=['--wired']
        else:
            network.plan('host',state); query_args=self.prepare_wifi()
        execute(['docker','exec','nav_robot','timeout','25',CONTAINER_ROOT+'/wifi_access/build/g1_wifi_readonly']+query_args,timeout=28)
        command='source /catkin_ws/devel/setup.bash && export ROS_MASTER_URI=http://192.0.2.5:11311 ROS_IP=192.0.2.5 && timeout 18 python3 '+CONTAINER_ROOT+'/wifi_access/sensor_probe.py'
        execute(['docker','exec','nav_robot','bash','-c',command],timeout=22)
        self.container_file(self.container_heartbeat)
        data=profile(self.mode)
        args=[CONTAINER_ROOT+'/network_modes/build/g1_network_bridge','__name:='+self.node,
              '_mode:='+self.mode,'_networkInterface:='+data['interface'],
              '_allow_motion:='+str(self.motion).lower(),
              '_imu_topic:='+('/sdk/imu' if self.motion else '/g1_network/imu'),
              '_heartbeat_file:='+self.container_heartbeat,'_stop_file:='+self.container_stop,
              '_session_seconds:='+str(data['session_seconds'])]
        if self.mode=='wifi': args.append('_dds_config:='+self.xml)
        env=['-e','ROS_MASTER_URI='+data['ros_master_uri'],'-e','ROS_IP='+data['ros_ip']]
        self.child=subprocess.Popen(['docker','exec']+env+['nav_robot']+args)
        print(json.dumps({'mode':self.mode,'motion_capable':self.motion,'armed':False,
                          'arm_service':'/'+self.node+'/arm','status_topic':'/'+self.node+'/status',
                          'hard_limit_seconds':data['session_seconds'],'token':self.token,'evidence_directory':str(self.directory)}),flush=True)
        deadline=time.monotonic()+data['session_seconds']
        while time.monotonic()<deadline and self.child.poll() is None:
            self.container_file(self.container_heartbeat,touch=True)
            if self.mode=='wifi':
                touch_private(self.heartbeat); self.heartbeat_remote()
            time.sleep(3)
        if self.child.poll() not in (None,0): raise RuntimeError('Candidate bridge exited with an error')

    def close(self):
        errors=[]
        # Stop the finite-duration bridge before removing any network resources.
        if self.child:
            try:
                self.container_file(self.container_stop)
                self.child.wait(timeout=5)
            except Exception as error:
                errors.append('Bridge stop not confirmed: '+str(error)+'; heartbeat expires within 15s, use remote stop')
        if self.host_active:
            try:
                execute(['sudo','python3',str(ROOT.parent/'wifi_access/network_probe.py'),'rollback','--token',self.token],timeout=30)
                path=Path('/tmp/g1-wifi-result-'+self.token+'.json')
                receipt=json.loads(path.read_text())
                (self.directory/'host-rollback.json').write_text(json.dumps(receipt,indent=2))
                if receipt.get('phase')!='rolled_back' or receipt.get('remaining'): raise RuntimeError(str(receipt))
            except Exception as error: errors.append('Host rollback not confirmed: '+str(error))
        if self.robot_active:
            try:
                self.ssh('touch /tmp/g1-wifi-stop-'+self.token,timeout=8)
                receipt=json.loads(self.ssh('for n in 1 2 3 4 5 6 7 8 9 10; do if test -f /tmp/g1-wifi-result-'+self.token+'.json; then cat /tmp/g1-wifi-result-'+self.token+'.json; exit 0; fi; sleep 1; done; exit 1',capture=True,timeout=15))
                (self.directory/'robot-rollback.json').write_text(json.dumps(receipt,indent=2))
                if receipt.get('phase')!='rolled_back' or receipt.get('remaining'): raise RuntimeError(str(receipt))
            except Exception as error: errors.append('Robot rollback not confirmed: '+str(error)+'; independent heartbeat expiry remains active')
        if self.ssh_active:
            try: execute(['ssh','-S',self.socket,'-O','exit',ROBOT],timeout=5)
            except Exception: pass
        print(json.dumps({'session_ended':True,'rollback_errors':errors,'evidence_directory':str(self.directory)}),flush=True)
        return not errors


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['wired','wifi'])
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--motion',action='store_true',help='Allow explicit arm service; does not arm at startup')
    args=parser.parse_args()
    print(json.dumps(plan(args.mode),ensure_ascii=False,indent=2),flush=True)
    if not args.run: return 0
    if sys.platform!='linux' or os.geteuid()==0: raise ValueError('Run as the normal navigation-host user, not root/Windows')
    import fcntl
    lock_path=Path('/tmp/g1-network-session.lock')
    fd=os.open(str(lock_path),os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    lock=os.fdopen(fd,'w')
    if not network.heartbeat_alive(lock_path,os.getuid(),lock_path.lstat().st_mtime):
        raise ValueError('Session lock ownership/type is invalid')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)  # Keep lock inode across sessions.
    print('Cancel navigation/tasks. Robot must be stationary, clear area, supervised, hardware remote stop ready. Type STATIONARY:')
    if input().strip()!='STATIONARY': return 1
    session=Session(args.mode,args.motion)
    status=0
    def stop(signum,frame): raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stop)
    try: session.start()
    except KeyboardInterrupt: pass
    except Exception as error: print(json.dumps({'error':str(error)}),file=sys.stderr); status=1
    finally:
        if not session.close(): status=1
    return status


if __name__=='__main__': raise SystemExit(main())
