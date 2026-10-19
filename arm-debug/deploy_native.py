"""Build an independent debug worker; self-test only, no DDS initialization.

Uses the existing migration transport and read-only development mount. Does not
restart containers, launch the worker, switch services or send motion commands.
"""
import base64
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT.parent/'artifacts/migration-20261013'))
import remote_ops
files={p.name:base64.b64encode(p.read_bytes()).decode() for p in (ROOT/'native').iterdir() if p.is_file()}
version=hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()[:16]
payload={'files':files,'version':version}
SOURCE=r'''
import base64,hashlib,json,pathlib,subprocess
p=PAYLOAD
root=pathlib.Path('/home/operator/g1-migration-20261013/cutover/development/arm-debug/releases')/p['version']
source=root/'source';source.mkdir(parents=True,exist_ok=True)
for name,data in p['files'].items():
    content=base64.b64decode(data);path=source/name
    if path.exists() and path.read_bytes()!=content:raise RuntimeError('immutable source mismatch')
    path.write_bytes(content)
container='robot-navigation-example'
build='/var/tmp/g1-arm-debug-'+p['version']
container_source='/opt/g1-development/arm-debug/releases/'+p['version']+'/source'
def run(args,timeout=60):
    r=subprocess.run(args,text=True,capture_output=True,timeout=timeout)
    return {'returncode':r.returncode,'stdout':r.stdout,'stderr':r.stderr}
r={'version':p['version'],'motion_commands_sent':0,'services_restarted':False,'worker_started':False}
r['build']=run(['docker','exec',container,'bash','-c','set -eu\ncmake -S "$1" -B "$2" -DG1_ARM_DEBUG_ENABLE_CONTROL=ON -DCMAKE_BUILD_TYPE=Release\ncmake --build "$2" --parallel 2','_',container_source,build])
if r['build']['returncode']==0:
    r['self_test']=run(['docker','exec',container,build+'/g1_arm_debug','--self-test'])
    if r['self_test']['returncode']==0:
        data=subprocess.check_output(['docker','exec',container,'cat',build+'/g1_arm_debug'])
        binary=root/'g1_arm_debug';binary.write_bytes(data);binary.chmod(0o755)
        r['binary_sha256']=hashlib.sha256(data).hexdigest()
        r['worker_path']='/opt/g1-development/arm-debug/releases/'+p['version']+'/g1_arm_debug'
(root/'build-result.json').write_text(json.dumps(r,indent=2))
print(json.dumps(r))
'''
p=remote_ops.python('192.0.2.1','PAYLOAD='+repr(payload)+'\n'+SOURCE,timeout=85)
artifacts=ROOT.parent/'artifacts/arm-debug-20261014';artifacts.mkdir(exist_ok=True)
(artifacts/'native-build.json').write_text(p.stdout,encoding='utf-8')
r=json.loads(p.stdout)
print(json.dumps(r,ensure_ascii=False))
if r['build']['returncode'] or r.get('self_test',{}).get('returncode',1):raise SystemExit(1)
(ROOT/'runtime.example.json').write_text(json.dumps({'worker_path':r['worker_path'],'binary_sha256':r['binary_sha256']},indent=2),encoding='utf-8')
