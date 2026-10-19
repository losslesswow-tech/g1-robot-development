"""Local teaching console. Startup connects read-only; Begin is an explicit action."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import threading
import time

ROOT=Path(__file__).resolve().parent
WORKSPACE=ROOT.parents[1]
SSH=r'C:\Windows\System32\OpenSSH\ssh.exe'


class Bridge:
    def __init__(self,runtime,key,logdir,max_seconds=610,gravity=False):
        if not 10 <= max_seconds <= 1800:raise ValueError('invalid_native_time_limit')
        self.lock=threading.RLock();self.write_lock=threading.Lock()
        self.latest=None;self.received=0.;self.finished=None
        self.process=self.thread=self.error_file=None
        self.connection_error=''
        self.gravity=gravity
        self.gravity_model_sha256=runtime.get('gravity',{}).get('model_sha256')
        self.events=(logdir/'transport.jsonl').open('w',encoding='utf-8',buffering=1)
        base=[SSH,'-i',str(key),'-o','IdentitiesOnly=yes','-o','BatchMode=yes',
              '-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8','operator@'+runtime['host']]
        command=['docker','exec','-i','-e','LD_LIBRARY_PATH=/opt/ros/noetic/lib:/usr/local/lib',
                 runtime['container'],runtime['native_path'],'--session',runtime['interface'],
                 '--max-seconds',str(max_seconds)]
        if gravity:command.append('--gravity')
        try:
            binary=subprocess.check_output(base+[shlex.join(['docker','exec',runtime['container'],'cat',runtime['native_path']])],timeout=20)
            if hashlib.sha256(binary).hexdigest()!=runtime['binary_sha256']:
                raise RuntimeError('native_checksum_mismatch')
            self.error_file=(logdir/'transport-stderr.log').open('w',encoding='utf-8')
            self.process=subprocess.Popen(base+[shlex.join(command)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                          stderr=self.error_file,text=True,encoding='utf-8',errors='replace',bufsize=1)
        except (OSError,subprocess.SubprocessError,RuntimeError) as exc:
            # Keep the local project library available; a new/load action retries.
            detail=('SSH 连接或查询超时' if isinstance(exc,subprocess.TimeoutExpired) else
                    'SSH 查询失败（退出码 '+str(exc.returncode)+'）' if isinstance(exc,subprocess.CalledProcessError) else str(exc))
            self.connection_error='开发主机连接或程序校验失败，请检查连接后重新准备：'+detail
            self.events.write(json.dumps({'type':'connection_error','message':self.connection_error,'detail':str(exc)},ensure_ascii=False)+'\n')
            if self.error_file is not None:self.error_file.close()
            return
        self.process.stdin.reconfigure(newline='\n')
        self.thread=threading.Thread(target=self.reader,daemon=True);self.thread.start()

    def reader(self):
        for line in self.process.stdout:
            try:
                value=json.loads(line)
            except ValueError:
                continue
            value['received_monotonic']=time.monotonic()
            with self.lock:
                self.events.write(json.dumps(value,allow_nan=False)+'\n')
                if value.get('type')=='state':self.latest=value;self.received=time.monotonic()
                if value.get('type')=='finished':self.finished=value
        self.events.flush()

    def state(self):
        with self.lock:
            if self.connection_error:raise RuntimeError(self.connection_error)
            if self.latest is None:raise RuntimeError('waiting_for_lowstate')
            data=dict(self.latest)
            data['transport_age_s']=time.monotonic()-self.received
            data['finished']=self.finished
            data['connected']=self.process.poll() is None
            data['gravity_profile_match']=bool(self.gravity and self.gravity_model_sha256 and
                                              data.get('gravity_model_sha256')==self.gravity_model_sha256)
        return data

    def snapshot(self):
        data=self.state()
        if not data['connected'] or data['finished']:raise RuntimeError('transport_ended')
        if max(data['transport_age_s'],data['age_s'],data['tick_age_s'])>.15:raise RuntimeError('stale_state')
        if data['fsm']!=501 or data['fsm_mode'] not in (0,1) or data['rpc_age_s']>=2:
            raise RuntimeError('regular_mode_unconfirmed')
        if not data['service_stopped'] or not data['api_match']:raise RuntimeError('service_not_ready')
        if data['foreign_age_s']<1:raise RuntimeError('another_arm_writer')
        if any(data['motorstate']):raise RuntimeError('motor_state_fault')
        if self.gravity:
            if not data.get('gravity_enabled') or not self.gravity_model_sha256 or data.get('gravity_model_sha256')!=self.gravity_model_sha256:
                raise RuntimeError('gravity_profile_unconfirmed')
            if not data.get('gravity_ready'):raise RuntimeError(data.get('gravity_error') or 'gravity_not_ready')
        return data

    def write(self,line):
        with self.write_lock:
            if self.process is None:raise RuntimeError(self.connection_error or 'transport_not_connected')
            if self.process.poll() is not None:raise RuntimeError('transport_ended')
            self.process.stdin.write(line+'\n');self.process.stdin.flush()

    def send(self,q,weight,softness):
        if len(q)!=17 or not all(math.isfinite(x) for x in q):raise ValueError('bad_upper_pose')
        self.write('T '+format(weight,'.9g')+' '+format(softness,'.9g')+' '+' '.join(format(x,'.9g') for x in q))

    def close_readonly(self):
        if self.process is None:
            self.events.close()
            return
        with self.lock:data=dict(self.latest or {})
        if data.get('sent',0) or data.get('weight',0):
            raise RuntimeError('cannot_close_active_transport')
        if self.process.poll() is None:
            self.write('QUIT');self.process.wait(timeout=5)
        self.thread.join(timeout=2)
        if self.thread.is_alive():raise RuntimeError('transport_reader_not_closed')
        self.events.close();self.error_file.close()

    def release(self):
        if self.process is None:raise RuntimeError(self.connection_error or 'transport_not_connected')
        if self.process.poll() is None:self.write('RELEASE')
        self.process.wait(timeout=5);self.thread.join(timeout=2)
        if self.thread.is_alive():raise RuntimeError('transport_reader_not_closed')
        self.events.close();self.error_file.close()

    def handback_confirmed(self):
        finish=self.finished
        return bool(finish and finish.get('final_weight')==0
                    and self.process.poll()==finish.get('returncode')
                    and (finish.get('sent')==0 or finish.get('zero_weight_frames',0)>=10))

    def close(self):
        if self.process is None:
            self.events.close()
            return
        if self.process.poll() is None:
            self.close_readonly()
        else:
            self.thread.join(timeout=2)
            if self.thread.is_alive():raise RuntimeError('transport_reader_not_closed')
            self.events.close();self.error_file.close()
