"""Deploy DreamControl controller; run self-test and read-only observation only."""
import base64
import argparse
import hashlib
import json
from pathlib import Path
import sys

directory=Path(__file__).resolve().parent
parser=argparse.ArgumentParser()
parser.add_argument('--target-z',type=float,choices=[.31,.5],default=.31)
args=parser.parse_args()
workspace=directory.parents[1]
sys.path.insert(0,str(workspace/'artifacts/migration-20261013'))
import remote_ops
native=json.loads((workspace/'artifacts/dreamcontrol-button-live-20261015/native-build.json').read_text())
artifact=workspace/('artifacts/dreamcontrol-z050-20261015' if args.target_z==.5 else 'artifacts/dreamcontrol-button-live-20261015')
controller=(directory/'controller.py').read_bytes()
author_config=(directory.parent/'vendor/DreamControl/Sim2Real/deploy/deploy_real/configs/g1.yaml').read_bytes()
version=hashlib.sha256(controller+author_config+str(args.target_z).encode()).hexdigest()[:16]
payload={'version':version,'native':native,'controller':base64.b64encode(controller).decode(),
         'target_z':args.target_z,
         'config':base64.b64encode(author_config).decode()}
source='''import base64,json,subprocess
from pathlib import Path
root=Path('/home/operator/g1-dreamcontrol-offline-20261015')
release=root/'live'/payload['version'];release.mkdir(parents=True,exist_ok=True)
for name,key in [('controller.py','controller'),('author_g1.yaml','config')]:
    data=base64.b64decode(payload[key]);path=release/name
    if path.exists() and path.read_bytes()!=data:raise RuntimeError('immutable controller mismatch')
    path.write_bytes(data)
runtime={'container':payload['native']['container'],'native_path':payload['native']['native_path'],
         'binary_sha256':payload['native']['binary_sha256'],
         'policy_path':str(root/'app/assets/Button_Press_UB.pt'),
         'policy_sha256':'c24af360a2252c372fbf6f1622bec5fdece1f3407838f2597cd2c133adcbceba',
         'author_config':str(release/'author_g1.yaml'),
         'target_xyz_m':[.5,-.23,payload['target_z']],
         'log_root':str(root/('live/logs-z050' if payload['target_z']==.5 else 'live/logs')),
         'latest_result':str(root/('live/latest-z050-result.json' if payload['target_z']==.5 else 'live/latest-result.json'))}
(release/'runtime.json').write_text(json.dumps(runtime,indent=2))
prefix=[str(root/'.venv/bin/python'),str(release/'controller.py'),'--runtime',str(release/'runtime.json')]
results=[]
for flag in ['--self-test','--observe']:
    p=subprocess.run(prefix+[flag],capture_output=True,text=True,timeout=25)
    results.append({'operation':flag,'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
    if p.returncode:break
record={'version':payload['version'],'controller_path':str(release/'controller.py'),
        'runtime_path':str(release/'runtime.json'),'python':str(root/'.venv/bin/python'),
        'results':results,'success':len(results)==2 and all(x['returncode']==0 for x in results),
        'physical_commands_sent_by_deployment':0}
(root/('live/deployment-z050.json' if payload['target_z']==.5 else 'live/deployment.json')).write_text(json.dumps(record,indent=2))
print(json.dumps(record))
'''
result=remote_ops.python('192.0.2.1','payload='+repr(payload)+'\n'+source,timeout=70)
(artifact/'controller-deploy.json').write_text(result.stdout)
print(result.stdout)
if not json.loads(result.stdout)['success']:raise SystemExit(1)
