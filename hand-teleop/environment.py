"""Backpack host adapter. Check/read/test/install adapter files only; no motion API."""
import argparse
import base64
import hashlib
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

ROOT = Path(__file__).resolve().parent
DEFAULT_PROFILE = ROOT/'environments/backpack-20261013.json'
DRIVER_HASH = 'd44845c8b28fea7ea87dc3e2df878e060468bb5c91e6436bbf62835d546b0df8'


def load_profile(path):
    p = json.loads(Path(path).read_text(encoding='utf-8'))
    if p.get('schema_version') != 1 or p.get('robot_model') != 'Unitree G1' or p.get('hand_model') != 'Inspire RH56DFTP':
        raise ValueError('Unsupported environment/model')
    for name in ('management_host','robot_host_ip'):
        ipaddress.IPv4Address(p[name])
    for name in ('ssh_user','navigation_container','bridge_container','robot_interface'):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',p[name]):
            raise ValueError('Invalid '+name)
    for name in ('host_development_root','container_development_root'):
        value = PurePosixPath(p[name])
        if not value.is_absolute() or '..' in value.parts or len(value.parts)<3:
            raise ValueError('Invalid '+name)
    if (p.get('hands') != {'L':'192.0.2.3','R':'192.0.2.4'} or
            p.get('hand_port') != 6000 or p.get('min_raw') != [0]*5+[950] or
            p.get('max_raw') != [1000]*6):
        raise ValueError('Profile cannot change hand endpoints or authorized raw ranges')
    if p.get('readonly_port') != 8768 or p.get('control_port') != 8769:
        raise ValueError('Unexpected workbench ports')
    return p


def adapter_files(profile):
    # Credentials and active control plans are intentionally absent.
    return {'environment.py':(ROOT/'environment.py').read_bytes(),
            'ENVIRONMENT.md':(ROOT/'ENVIRONMENT.md').read_bytes(),
            'environments/backpack-20261013.json':json.dumps(profile,indent=2).encode()}


def ssh_command(profile, identity):
    return ['ssh','-i',str(identity),'-o','IdentitiesOnly=yes','-o','BatchMode=yes',
            '-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8',
            profile['ssh_user']+'@'+profile['management_host'],'python3','-']


REMOTE = r'''
import base64,hashlib,json,pathlib,subprocess,urllib.request,os
p=PAYLOAD['profile'];container=p['navigation_container'];host_root=pathlib.Path(p['host_development_root'])
def run(args,timeout=15):
 r=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
 if r.returncode:raise RuntimeError('Command failed: '+r.stderr[-1000:])
 return r.stdout
def inspect():
 data=json.loads(run(['docker','inspect',container,p['bridge_container']]))
 return data,[{'name':x['Name'],'running':x['State']['Running'],'started_at':x['State']['StartedAt'],'network':x['HostConfig']['NetworkMode']} for x in data]
data,before=inspect()
assert all(x['running'] and x['network']=='host' for x in before),'Containers must already be running; adapter does not start them'
assert any(m['Source']==str(host_root) and m['Destination']==p['container_development_root'] and not m['RW'] for m in data[0]['Mounts']),'Persistent read-only development mount differs'
interface=run(['ip','-4','-brief','address','show','dev',p['robot_interface']])
assert p['robot_host_ip']+'/' in interface,'Robot interface/IP differs'
report={'marker':'HAND_ENVIRONMENT','action':PAYLOAD['action'],'containers_before':before,'interface':interface,'motion_commands_sent':0,'services_restarted':False}
work=p['container_development_root']+'/hand-teleop'
if PAYLOAD['action']=='install-adapter':
 dest=host_root/'hand-teleop'
 assert dest.is_dir() and not dest.is_symlink()
 for name,item in PAYLOAD['files'].items():
  assert name in ('environment.py','ENVIRONMENT.md','environments/backpack-20261013.json')
  target=dest/name
  assert not target.is_symlink()
  content=base64.b64decode(item['data']);assert hashlib.sha256(content).hexdigest()==item['sha256']
  target.parent.mkdir(exist_ok=True)
  assert target.parent.resolve()==dest.resolve() or dest.resolve() in target.parent.resolve().parents
  if target.exists() and target.read_bytes()!=content:
   backup=target.with_name(target.name+'.before-environment-20261014')
   if backup.exists():assert backup.read_bytes()==target.read_bytes(),'Backup already differs'
   else:backup.write_bytes(target.read_bytes())
  pending=target.with_name(target.name+'.environment-pending');pending.write_bytes(content);os.replace(str(pending),str(target))
  assert target.read_bytes()==content
 report['adapter_files_installed']=list(PAYLOAD['files'])
elif PAYLOAD['action']=='offline-test':
 test=subprocess.run(['docker','exec','-w',work,container,'python3','-m','unittest','discover','-s','tests','-p','test_*.py'],capture_output=True,text=True,timeout=30)
 assert test.returncode==0,test.stdout+test.stderr
 report['tests']=test.stdout+test.stderr
 report['offline_tests_passed']=True
else:
 token=(host_root/'connection.token').read_text().strip()
 base='http://'+p['management_host']+':'+str(p['control_port'])
 def get(path):
  req=urllib.request.Request(base+path,headers={'X-Hand-Token':token})
  with urllib.request.urlopen(req,timeout=6) as response:return json.load(response)
 config=get('/api/config')
 assert config['driver_sha256']==PAYLOAD['driver_hash'] and config['mode']=='single-step'
 assert config.get('builtin_connection') is True
 assert set(config['single_step_scopes'])=={'L','R'}
 for side,plan in config['single_step_scopes'].items():
  assert plan['schema']==6 and plan['side']==side and plan['min_raw']==p['min_raw'] and plan['max_raw']==p['max_raw']
 report['hands']={s:get('/api/status/'+s) for s in ('L','R')}
 assert all(not v['error'] for v in report['hands'].values()),'Hand reads are not healthy'
 child="import sys,json,platform;from pathlib import Path;sys.path.insert(0,"+repr(work)+");from server import RepositoryDriver;d=RepositoryDriver(Path("+repr(work+"/vendor/repository/inspire_hand_ctl.py")+"));print(json.dumps({'python':sys.version,'arch':platform.machine(),'targets':{s:d.read_targets(s) for s in ('L','R')}}))"
 report['container_readonly']=json.loads(run(['docker','exec',container,'python3','-c',child]))
 report['control_scopes']=config['single_step_scopes']
 report['control_url']=base+'/'
 report['readonly_url']='http://'+p['management_host']+':'+str(p['readonly_port'])+'/'
_,after=inspect();assert before==after,'Container changed during verification'
report['containers_after']=after
print(json.dumps(report))
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['check','offline-test','install-adapter'])
    parser.add_argument('--profile',type=Path,default=DEFAULT_PROFILE)
    parser.add_argument('--identity',type=Path,required=True)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    profile=load_profile(args.profile)
    payload={'profile':profile,'action':args.action,'driver_hash':DRIVER_HASH}
    if args.action=='install-adapter':
        payload['files']={name:{'data':base64.b64encode(data).decode(),'sha256':hashlib.sha256(data).hexdigest()}
                          for name,data in adapter_files(profile).items()}
    source='PAYLOAD='+repr(payload)+'\n'+REMOTE
    result=subprocess.run(ssh_command(profile,args.identity),input=source,text=True,
                          encoding='utf-8',capture_output=True,timeout=55)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:])
    report=json.loads(result.stdout)
    assert report['marker']=='HAND_ENVIRONMENT'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
