"""Build native G1 transport and run a no-DDS self-test; never send commands."""
import base64
import argparse
import hashlib
import json
from pathlib import Path
import sys

root=Path(__file__).resolve().parent
workspace=root.parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--artifact',type=Path,default=workspace/'artifacts/dreamcontrol-button-live-20261015')
args=parser.parse_args()
sys.path.insert(0,str(workspace/'artifacts/migration-20261013'))
import remote_ops
files={name:base64.b64encode((root/name).read_bytes()).decode()
       for name in ['CMakeLists.txt','native_transport.cpp']}
version=hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()[:16]
source='''import base64,hashlib,json,subprocess
from pathlib import Path
root=Path('/home/operator/g1-migration-20261013/cutover/development/dreamcontrol-live/releases')/payload['version']
directory=root/'source';directory.mkdir(parents=True,exist_ok=True)
for name,encoded in payload['files'].items():
    data=base64.b64decode(encoded);target=directory/name
    if target.exists() and target.read_bytes()!=data:raise RuntimeError('Immutable source mismatch')
    target.write_bytes(data)
container='robot-navigation-example'
c_source='/opt/g1-development/dreamcontrol-live/releases/'+payload['version']+'/source'
build='/var/tmp/g1-dreamcontrol-'+payload['version']
commands=[['docker','exec',container,'cmake','-S',c_source,'-B',build,'-DCMAKE_BUILD_TYPE=Release'],
          ['docker','exec',container,'cmake','--build',build,'--parallel','2'],
          ['docker','exec',container,build+'/g1_dreamcontrol_transport','--self-test']]
results=[]
for argv in commands:
    p=subprocess.run(argv,capture_output=True,text=True,timeout=80)
    results.append({'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
    if p.returncode:break
record={'version':payload['version'],'container':container,'native_path':build+'/g1_dreamcontrol_transport',
        'results':results,'sent':0,'dds_initialized':False,'success':len(results)==3 and all(r['returncode']==0 for r in results)}
if record['success']:
    binary=subprocess.check_output(['docker','exec',container,'cat',record['native_path']])
    record['binary_sha256']=hashlib.sha256(binary).hexdigest()
(root/'build-result.json').write_text(json.dumps(record,indent=2))
print(json.dumps(record))
'''
result=remote_ops.python('192.0.2.1','payload='+repr({'version':version,'files':files})+'\n'+source,timeout=110)
artifact=args.artifact
artifact.mkdir(parents=True,exist_ok=True)
(artifact/'native-build.json').write_text(result.stdout)
print(result.stdout)
if not json.loads(result.stdout)['success']:raise SystemExit(1)
