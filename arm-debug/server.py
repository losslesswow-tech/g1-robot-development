"""Loopback right-arm workbench. Startup and connection never acquire control."""
import argparse
from collections import deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit

from backend import ROOT,GEOMETRY,JOINTS,MODEL,LIMITS,Rejected,SimulationDriver,SSHDriver

class Workbench:
    def __init__(self,driver,mode='simulation',allow_control=False,audit_path=None):
        self.driver=driver;self.mode=mode;self.allow_control=allow_control
        self.owner=None;self.lock=threading.RLock();self.seen=deque(maxlen=256)
        self.audit_path=Path(audit_path) if audit_path else None
        self.audit_lock=threading.Lock()
        if isinstance(driver,SSHDriver):driver.event_sink=self.audit

    def audit(self,event,**values):
        if self.audit_path:
            self.audit_path.parent.mkdir(parents=True,exist_ok=True)
            with self.audit_lock,self.audit_path.open('a',encoding='utf-8') as f:
                f.write(json.dumps(dict(time=time.time(),event=event,backend=self.mode,**values),ensure_ascii=False,allow_nan=False)+'\n')

    def state(self,session):
        with self.lock:
            s=self.driver.state()
            if s.get('phase') in ('fault','disconnected'):self.owner=None
            s['owned_by_this_page']=bool(self.owner and self.owner==session)
            s['another_page_owns_control']=bool(self.owner and self.owner!=session)
            s['backend']=self.mode
            return s

    def action(self,session,action,data):
        with self.lock:
            request=data.get('request_id')
            if not isinstance(request,str) or len(request)!=32 or any(c not in '0123456789abcdef' for c in request):raise Rejected('invalid_request_id')
            if request in self.seen:raise Rejected('duplicate_request_rejected')
            self.seen.append(request)
            if action=='connect':
                self.driver.connect();self.audit('connect_readonly');return self.state(session)
            if action=='disconnect':
                if self.owner and self.owner!=session:raise Rejected('another_page_owns_control')
                self.driver.close();self.owner=None;self.audit('disconnect');return {'ok':True}
            if action=='acquire':
                if not self.allow_control:raise Rejected('control_not_enabled_on_server')
                if data.get('onsite') is not True:raise Rejected('onsite_monitoring_required')
                if self.owner:raise Rejected('another_page_owns_control')
                if not self.driver.state().get('fresh'):raise Rejected('stale_lowstate')
                self.owner=session
                try:r=self.driver.command('ACQUIRE')
                except Exception:self.owner=None;raise
                self.audit('acquire_requested');return r
            if action=='stop-service':
                if not self.allow_control or data.get('onsite') is not True:raise Rejected('onsite_monitoring_required')
                if self.owner:raise Rejected('release_before_service_change')
                r=self.driver.stop_service();self.audit('stop_builtin_arm_service');return r
            if self.owner!=session:raise Rejected('this_page_does_not_own_control')
            if action=='heartbeat':return self.driver.command('PING')
            if action=='target':
                index,value=data.get('index'),data.get('q_rad')
                r=self.driver.command('SET',index,value);self.audit('target_accepted',index=index,q_rad=value);return r
            if action=='release':
                r=self.driver.command('RELEASE');self.owner=None;self.audit('release_requested');return r
            raise Rejected('unknown_action')

def make_server(workbench,bind='127.0.0.1',port=8771):
    sessions={};sessions_lock=threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def send_headers(self,status,length,kind='application/json; charset=utf-8',cookie=None):
            self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(length))
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            if status>=400:self.send_header('Connection','close');self.close_connection=True
            if cookie:self.send_header('Set-Cookie',cookie)
            self.end_headers()
        def respond(self,value,status=200,cookie=None):
            raw=json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8');self.send_headers(status,len(raw),cookie=cookie);self.wfile.write(raw)
        def valid_host(self):return self.headers.get('Host') in self.server.allowed_hosts
        def session(self,create=False):
            cookie=SimpleCookie()
            try:cookie.load(self.headers.get('Cookie',''))
            except Exception:return None,None,None
            sid=cookie.get('arm_debug_session');sid=sid.value if sid else None
            with sessions_lock:
                if sid in sessions:return sid,sessions[sid],None
                if not create:return None,None,None
                if len(sessions)>64:sessions.clear()
                sid=secrets.token_hex(24);token=secrets.token_hex(24);sessions[sid]=token
            return sid,token,'arm_debug_session='+sid+'; HttpOnly; SameSite=Strict; Path=/'
        def do_GET(self):
            if not self.valid_host():self.respond({'error':'invalid_host'},403);return
            path=urlsplit(self.path).path
            if path=='/api/config':
                sid,token,cookie=self.session(True)
                self.respond({'csrf':token,'backend':workbench.mode,'control_enabled':workbench.allow_control,'joints':JOINTS,
                              'limits':LIMITS,
                              'model_sha256':MODEL['urdf_sha256'],'tool_version':'2026-10-14-right-arm-20-50'},cookie=cookie);return
            sid,_,_=self.session()
            if path=='/api/state':self.respond(workbench.state(sid));return
            assets={'/':'index.html','/index.html':'index.html','/style.css':'style.css','/app.mjs':'app.mjs','/editor.mjs':'editor.mjs','/diagnostics.mjs':'diagnostics.mjs'}
            geometry={'model.mjs','snapshot.mjs','hand-geometry.mjs','kinematics.mjs','three.module.min.js','three.core.min.js'}
            if path in assets:file=ROOT/'web'/assets[path]
            elif path.startswith('/geometry/') and path[len('/geometry/'):] in geometry:file=GEOMETRY/path[len('/geometry/'):]
            else:self.respond({'error':'not_found'},404);return
            if not file.is_file():self.respond({'error':'not_found'},404);return
            kind='text/html; charset=utf-8' if file.suffix=='.html' else 'text/css; charset=utf-8' if file.suffix=='.css' else 'text/javascript; charset=utf-8'
            raw=file.read_bytes();self.send_headers(200,len(raw),kind);self.wfile.write(raw)
        def do_POST(self):
            if not self.valid_host() or self.headers.get('Origin')!='http://'+self.headers.get('Host',''):
                self.respond({'error':'origin_rejected'},403);return
            sid,token,_=self.session()
            if not sid:self.respond({'error':'session_required'},403);return
            if self.headers.get_content_type()!='application/json':self.respond({'error':'json_required'},415);return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=16384:raise ValueError('body size')
                data=json.loads(self.rfile.read(length),parse_constant=lambda x:(_ for _ in ()).throw(ValueError(x)))
                if not isinstance(data,dict):raise ValueError('body object')
            except (ValueError,UnicodeError):self.respond({'error':'invalid_json'},400);return
            if not isinstance(data.get('csrf'),str) or not secrets.compare_digest(data['csrf'],token):self.respond({'error':'csrf_rejected'},403);return
            path=urlsplit(self.path).path
            if not path.startswith('/api/'):self.respond({'error':'not_found'},404);return
            try:self.respond(workbench.action(sid,path[len('/api/'):],data))
            except Rejected as error:
                workbench.audit('request_rejected',action=path,error=str(error));self.respond({'error':str(error)},409)
            except (OSError,TimeoutError) as error:
                workbench.audit('transport_failure',action=path,error=type(error).__name__);self.respond({'error':'backend_transport_failure'},503)
    server=ThreadingHTTPServer((bind,port),Handler)
    actual_port=server.server_address[1]
    server.allowed_hosts={'127.0.0.1:'+str(actual_port),'localhost:'+str(actual_port),bind+':'+str(actual_port)}
    return server

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--backend',choices=('simulation','ssh'),default='simulation');p.add_argument('--port',type=int,default=8771)
    p.add_argument('--ssh-key');p.add_argument('--runtime',type=Path,default=ROOT/'runtime.example.json')
    p.add_argument('--allow-control',action='store_true');p.add_argument('--audit',type=Path,default=ROOT.parent/'artifacts/arm-debug-20261014/audit.jsonl')
    args=p.parse_args()
    if args.backend=='ssh':
        if not args.ssh_key:p.error('--ssh-key is required for SSH backend')
        runtime=json.loads(args.runtime.read_text(encoding='utf-8'));driver=SSHDriver(args.ssh_key,runtime['worker_path'])
    else:driver=SimulationDriver()
    w=Workbench(driver,args.backend,args.allow_control,args.audit);server=make_server(w,port=args.port)
    print(json.dumps({'url':'http://127.0.0.1:'+str(server.server_address[1]),'backend':args.backend,'control_enabled':args.allow_control,'startup_motion_commands':0}),flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:driver.close();server.server_close()

if __name__=='__main__':main()
