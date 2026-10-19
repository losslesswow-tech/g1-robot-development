"""Replace only our idle scoped hand service, with no actuator writes."""
import base64
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

from start_hand_single_step import remote

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--builtin-connection', action='store_true')
    parser.add_argument('--range500', action='store_true')
    parser.add_argument('--four-fingers', action='store_true')
    parser.add_argument('--all-channels', action='store_true')
    parser.add_argument('--range1000', action='store_true')
    args = parser.parse_args()
    if args.range1000:
        args.all_channels = True
    if args.range500 or args.four_fingers or args.all_channels:
        args.builtin_connection = True
    suffix = 'range1000' if args.range1000 else 'all-channels' if args.all_channels else 'four-fingers' if args.four_fingers else 'range500' if args.range500 else 'builtin-connection' if args.builtin_connection else 'right-controls'
    date = '20261013' if args.range1000 else '20261012'
    previous_name = 'hand-all-channels-runtime-20261012.json' if args.range1000 else 'hand-four-fingers-runtime-20261012.json' if args.all_channels else 'hand-range500-runtime-20261012.json' if args.four_fingers else 'hand-builtin-connection-runtime-20261012.json' if args.range500 else 'hand-right-controls-runtime-20261012.json'
    previous = json.loads((ROOT/'artifacts'/previous_name).read_text(encoding='utf-8')) if args.builtin_connection else None
    right_plan = 'manual-range1000-right.json' if args.range1000 else 'manual-all-right.json' if args.all_channels else 'single-step-right-four-fingers.json' if args.four_fingers else 'single-step-right-range500.json' if args.range500 else 'single-step-right-fingers.json'
    left_plan = 'manual-range1000-left.json' if args.range1000 else 'manual-all-left.json' if args.all_channels else 'single-step-left-index.json'
    source = ROOT/'hand-teleop'
    files = {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob('*')
             if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'
             and p.name != 'RELEASE-MANIFEST.json' and not p.name.startswith('calibration.local')}
    manifest = {'version': date+'-'+suffix,
                'sha256': {n: hashlib.sha256(v).hexdigest() for n, v in files.items()}}
    package = ROOT/('artifacts/g1-hand-'+suffix+'-'+date+'.zip')
    with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr('hand-teleop/'+name, data)
        archive.writestr('hand-teleop/RELEASE-MANIFEST.json', json.dumps(manifest, indent=2))
    raw = package.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    prefix = '/tmp/g1-hand-right-'+digest[:12]
    chunks = [raw[i:i+30000] for i in range(0, len(raw), 30000)]
    for index, data in enumerate(chunks):
        remote('''import base64,json,pathlib
p=pathlib.Path(PATH);data=base64.b64decode(DATA)
if p.exists():assert not p.is_symlink() and p.read_bytes()==data
else:
 with p.open('xb') as output:output.write(data)
print(json.dumps({'marker':'HAND_STEP_STAGE','chunk':INDEX}))
'''.replace('PATH', repr(prefix+'.part%03d'%index))
               .replace('DATA', repr(base64.b64encode(data).decode())).replace('INDEX', str(index)))
    receipt = remote('''import hashlib,io,json,pathlib,subprocess,sys,tempfile,zipfile
data=b''.join(pathlib.Path(PREFIX+'.part%03d'%i).read_bytes() for i in range(COUNT))
assert hashlib.sha256(data).hexdigest()==SHA
folder=pathlib.Path(tempfile.mkdtemp(prefix='g1-hand-web-right-20261012-'))
with zipfile.ZipFile(io.BytesIO(data)) as archive:
 for item in archive.infolist():assert folder.resolve() in (folder/item.filename).resolve().parents
 archive.extractall(folder)
work=folder/'hand-teleop';manifest=json.loads((work/'RELEASE-MANIFEST.json').read_text())
for name,digest in manifest['sha256'].items():assert hashlib.sha256((work/name).read_bytes()).hexdigest()==digest,name
tests=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p','test_*.py','-v'],cwd=str(work),capture_output=True,text=True,timeout=30)
assert tests.returncode==0,tests.stdout+tests.stderr
print(json.dumps({'marker':'HAND_STEP_STAGE','directory':str(folder),'app_directory':str(work),'offline_tests':tests.stdout+tests.stderr,'sha256':SHA,'hardware_writes':0}))
'''.replace('PREFIX', repr(prefix)).replace('COUNT', str(len(chunks))).replace('SHA', repr(digest)), timeout=45)
    (ROOT/('artifacts/hand-'+suffix+'-staged-'+date+'.json')).write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    result = remote('''import json,os,pathlib,signal,subprocess,sys,time,urllib.request
work=pathlib.Path(WORK);sys.path.insert(0,str(work));import step_server as step
old=pathlib.Path(OLD_DIRECTORY)
metadata=json.loads((old/'runtime.json').read_text());pid=metadata['pid']
assert pid==EXPECTED_PID and metadata['url']=='http://192.0.2.5:8769'
assert pathlib.Path(os.readlink('/proc/%s/cwd'%pid))==old/'hand-teleop'
cmd=pathlib.Path('/proc/%s/cmdline'%pid).read_bytes().split(b'\\0')
assert b'step_server.py' in cmd and b'8769' in cmd and EXPECTED_PLAN in cmd
token=(old/'connection.token').read_text().strip() if (old/'connection.token').is_file() else [v.split(': ',1)[1] for v in (old/'step-server.log').read_text().splitlines() if v.startswith('Connection token: ')][0]
for side in ('L','R'):
 request=urllib.request.Request('http://192.0.2.5:8769/api/status/'+side,headers={'X-Hand-Token':token})
 with urllib.request.urlopen(request,timeout=5) as response:status=json.load(response)
 assert not status['armed'] and not status['busy'],'Existing control must be idle before replacement'
original=step.RepositoryDriver(work/'vendor/repository/inspire_hand_ctl.py')
plans=[step.load_plan(work/name) for name in (RIGHT_PLAN,LEFT_PLAN)]
right=step.RightTrialDriver(original,plans[0]);left=step.RightTrialDriver(original,plans[1]) if plans[1]['schema'] in (5,6) else step.StepDriver(original,plans[1])
targets={'R':right.guard(),'L':left.guard()}
os.kill(pid,signal.SIGTERM)
for _ in range(30):
 try:
  state=pathlib.Path('/proc/%s/stat'%pid).read_text().split()[2]
  if state=='Z':break
 except FileNotFoundError:break
 time.sleep(.1)
else:raise RuntimeError('Own old service did not exit; no new service started')
log=work.parent/'step-server.log';handle=log.open('xb');os.chmod(str(log),0o600)
if BUILTIN:
 credential=work.parent/'connection.token'
 with credential.open('x') as output:output.write(token)
 os.chmod(str(credential),0o600)
process=subprocess.Popen([sys.executable,'-u','step_server.py','--bind','192.0.2.5','--port','8769','--plan',RIGHT_PLAN,'--plan',LEFT_PLAN]+EXTRA_FLAGS,cwd=str(work),stdin=subprocess.DEVNULL,stdout=handle,stderr=subprocess.STDOUT,start_new_session=True)
handle.close();config=None
for _ in range(30):
 if process.poll() is not None:raise RuntimeError('Own updated service failed; log retained at '+str(log))
 try:
  with urllib.request.urlopen('http://192.0.2.5:8769/api/config',timeout=1) as response:config=json.load(response)
  break
 except (OSError,ValueError):time.sleep(.1)
assert config['single_step']['side']=='R' and set(config['single_step_scopes'])=={'L','R'}
assert config['single_step_scopes']=={plan['side']:plan for plan in plans}
assert config.get('builtin_connection',False)==BUILTIN
assert config['driver_sha256']=='d44845c8b28fea7ea87dc3e2df878e060468bb5c91e6436bbf62835d546b0df8'
result={'marker':'HAND_STEP_STAGE','pid':process.pid,'directory':str(work.parent),'url':'http://192.0.2.5:8769','prior_pid':pid,'prior_directory':str(old),'device_targets':targets,'right_plan':plans[0],'left_plan':plans[1],'hardware_writes':0,'armed':False,'builtin_connection':config.get('builtin_connection',False),'vendor_sha256':config['driver_sha256']}
p=work.parent/'runtime.json'
with p.open('x') as output:json.dump(result,output,indent=2)
os.chmod(str(p),0o600)
print(json.dumps(result))
'''.replace('WORK', repr(receipt['app_directory']))
       .replace('OLD_DIRECTORY', repr(previous['directory'] if previous else '/tmp/g1-hand-web-step-20261012-57hcnho0'))
       .replace('EXPECTED_PID', str(previous['pid'] if previous else 531391))
       .replace('EXPECTED_PLAN', repr(b'manual-all-left.json' if args.range1000 else b'single-step-left-index.json'))
       .replace('EXTRA_FLAGS', repr(['--builtin-connection','--token-file','../connection.token'] if args.builtin_connection else []))
       .replace('RIGHT_PLAN', repr(right_plan))
       .replace('LEFT_PLAN', repr(left_plan))
       .replace('BUILTIN', repr(args.builtin_connection)), timeout=40)
    result.update(package=str(package), package_sha256=digest, offline_tests_passed=62 if args.range1000 else 57 if args.all_channels else 51 if args.four_fingers else 46 if args.range500 else 42 if args.builtin_connection else 37)
    (ROOT/('artifacts/hand-'+suffix+'-runtime-'+date+'.json')).write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
