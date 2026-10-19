"""Stage, test, and start an idle scoped service; never sends motor targets."""
import base64
import hashlib
import json
from pathlib import Path
import shlex
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parents[1]
API='http://192.0.2.5:9000/api/system/execute_cmd'


def remote(source, timeout=40):
    request=urllib.request.Request(API,data=json.dumps({'command':'python3 -c '+shlex.quote(source)}).encode(),headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=timeout) as response:envelope=json.load(response)
    if not envelope.get('success') or envelope.get('exit_code',0)!=0:
        raise RuntimeError(str(envelope))
    result=json.loads(envelope['output'])
    assert result['marker']=='HAND_STEP_STAGE',result
    return result


def main():
    source=ROOT/'hand-teleop'
    files={p.relative_to(source).as_posix():p.read_bytes() for p in source.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc' and p.name!='RELEASE-MANIFEST.json' and not p.name.startswith('calibration.local')}
    manifest={'version':'2026-10-12-left-index-single-step','sha256':{name:hashlib.sha256(data).hexdigest() for name,data in files.items()}}
    package=ROOT/'artifacts/g1-hand-teleop-step-20261012.zip'
    with zipfile.ZipFile(package,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in files.items():archive.writestr('hand-teleop/'+name,data)
        archive.writestr('hand-teleop/RELEASE-MANIFEST.json',json.dumps(manifest,indent=2))
    raw=package.read_bytes();sha=hashlib.sha256(raw).hexdigest()
    prefix='/tmp/g1-hand-step-'+sha[:12]
    chunks=[raw[start:start+30000] for start in range(0,len(raw),30000)]
    for index,data in enumerate(chunks):
        remote('''import base64,json,pathlib
p=pathlib.Path(PATH);data=base64.b64decode(DATA)
if p.exists():assert not p.is_symlink() and p.read_bytes()==data
else:
 with p.open('xb') as output:output.write(data)
print(json.dumps({'marker':'HAND_STEP_STAGE','chunk':INDEX}))
'''.replace('PATH',repr(prefix+'.part%03d'%index)).replace('DATA',repr(base64.b64encode(data).decode())).replace('INDEX',str(index)))
    receipt=remote(r'''
import hashlib,json,pathlib,subprocess,sys,tempfile,urllib.request,zipfile,io
data=b''.join(pathlib.Path(PREFIX+'.part%03d'%i).read_bytes() for i in range(COUNT))
assert hashlib.sha256(data).hexdigest()==SHA
folder=pathlib.Path(tempfile.mkdtemp(prefix='g1-hand-web-step-20261012-'))
with zipfile.ZipFile(io.BytesIO(data)) as archive:
 for item in archive.infolist():assert folder.resolve() in (folder/item.filename).resolve().parents
 archive.extractall(folder)
work=folder/'hand-teleop';manifest=json.loads((work/'RELEASE-MANIFEST.json').read_text())
for name,digest in manifest['sha256'].items():assert hashlib.sha256((work/name).read_bytes()).hexdigest()==digest,name
tests=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p','test_*.py','-v'],cwd=str(work),capture_output=True,text=True,timeout=30)
assert tests.returncode==0,tests.stdout+tests.stderr
print(json.dumps({'marker':'HAND_STEP_STAGE','directory':str(folder),'app_directory':str(work),'python':sys.version,'offline_tests':tests.stdout+tests.stderr,'sha256':SHA,'hardware_writes':0}))
'''.replace('PREFIX',repr(prefix)).replace('COUNT',str(len(chunks))).replace('SHA',repr(sha)),timeout=45)
    # Preserve the test result even when the final field preflight blocks startup.
    (ROOT/'artifacts/hand-single-step-staged-20261012.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
    started=remote(r'''
import importlib.util,json,os,pathlib,socket,subprocess,sys,time,urllib.request
work=pathlib.Path(WORK);sys.path.insert(0,str(work));import step_server as step
with urllib.request.urlopen('http://192.0.2.5:8768/api/config',timeout=5) as response:old_config=json.load(response)
assert old_config['mode']=='readonly','Existing model page changed mode'
plan=step.load_plan(work/'single-step-left-index.json')
driver=step.RepositoryDriver(work/'vendor/repository/inspire_hand_ctl.py')
state=driver.read('L');target=step.StepDriver(driver,plan).guard(state)
probe=socket.socket();probe.bind(('192.0.2.5',8769));probe.close()
log=work.parent/'step-server.log'
handle=log.open('xb');os.chmod(str(log),0o600)
process=subprocess.Popen([sys.executable,'-u','step_server.py','--bind','192.0.2.5','--port','8769','--plan','single-step-left-index.json'],cwd=str(work),stdin=subprocess.DEVNULL,stdout=handle,stderr=subprocess.STDOUT,start_new_session=True)
handle.close()
config=None
for _ in range(30):
 if process.poll() is not None:raise RuntimeError('Own step service failed; retained log at '+str(log))
 try:
  with urllib.request.urlopen('http://192.0.2.5:8769/api/config',timeout=1) as response:config=json.load(response)
  break
 except (OSError,ValueError):time.sleep(.1)
assert config is not None and config['mode']=='single-step'
assert config['single_step']==plan
assert config['driver_sha256']=='d44845c8b28fea7ea87dc3e2df878e060468bb5c91e6436bbf62835d546b0df8'
result={'marker':'HAND_STEP_STAGE','pid':process.pid,'directory':str(work.parent),'url':'http://192.0.2.5:8769','mode':config['mode'],'side':'L','channel':3,'range':[82,200],'device_targets':target,'actual_before_start':state['actual'],'vendor_sha256':config['driver_sha256'],'hardware_writes':0,'armed':False,'old_model_page':'http://192.0.2.5:8768','old_model_page_mode':'readonly'}
metadata=work.parent/'runtime.json'
with metadata.open('x') as output:json.dump(result,output,indent=2)
os.chmod(str(metadata),0o600)
print(json.dumps(result))
'''.replace('WORK',repr(receipt['app_directory'])))
    started['package']=str(package);started['package_sha256']=sha
    (ROOT/'artifacts/hand-single-step-runtime-20261012.json').write_text(json.dumps(started,indent=2),encoding='utf-8')
    print(json.dumps(started,indent=2))


if __name__=='__main__':main()
