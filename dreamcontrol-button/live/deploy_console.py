"""Install an immutable interactive console; self-test only, no DDS/motion."""
import base64
import hashlib
import json
from pathlib import Path
import sys

directory = Path(__file__).resolve().parent
workspace = directory.parents[1]
sys.path.insert(0, str(workspace / 'artifacts/migration-20261013'))
import remote_ops

artifact = workspace / 'artifacts/dreamcontrol-console-20261015'
artifact.mkdir(parents=True, exist_ok=True)
native = json.loads((artifact / 'native-build.json').read_text(encoding='utf-8-sig'))
if not native['success']:
    raise RuntimeError('native_build_not_verified')
files = {name: base64.b64encode((directory / name).read_bytes()).decode()
         for name in ['controller.py', 'action_flow.py', 'console_worker.py']}
files['author_g1.yaml'] = base64.b64encode((directory.parent / 'vendor/DreamControl/Sim2Real/deploy/deploy_real/configs/g1.yaml').read_bytes()).decode()
version = hashlib.sha256(json.dumps(dict(files=files, native=native['binary_sha256']), sort_keys=True).encode()).hexdigest()[:16]
payload = dict(version=version, files=files, native=native)
source = '''import base64,json,subprocess
from pathlib import Path
root=Path('/home/operator/g1-dreamcontrol-offline-20261015')
release=root/'console'/payload['version'];release.mkdir(parents=True,exist_ok=True)
for name,encoded in payload['files'].items():
    data=base64.b64decode(encoded);path=release/name
    if path.exists() and path.read_bytes()!=data:raise RuntimeError('immutable_console_mismatch')
    path.write_bytes(data)
runtime={'container':payload['native']['container'],'native_path':payload['native']['native_path'],
 'binary_sha256':payload['native']['binary_sha256'],'native_deadline_s':90,
 'policy_path':str(root/'app/assets/Button_Press_UB.pt'),
 'policy_sha256':'c24af360a2252c372fbf6f1622bec5fdece1f3407838f2597cd2c133adcbceba',
 'author_config':str(release/'author_g1.yaml'),
 'query_binary_dir':'/opt/g1-development/arm-button/releases/20261014-363baa8d75466aec/bin/',
 'log_root':str(root/'console/logs'),'latest_result':str(root/'console/latest-result.json')}
(release/'runtime.json').write_text(json.dumps(runtime,indent=2))
python=str(root/'.venv/bin/python')
p=subprocess.run([python,str(release/'console_worker.py'),'--runtime',str(release/'runtime.json'),'--self-test'],capture_output=True,text=True,timeout=30)
record={'version':payload['version'],'python':python,'worker_path':str(release/'console_worker.py'),
 'runtime_path':str(release/'runtime.json'),'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr,
 'success':p.returncode==0,'physical_commands_sent':0,'dds_initialized':False}
(root/'console/deployment.json').write_text(json.dumps(record,indent=2))
print(json.dumps(record))
'''
result = remote_ops.python('192.0.2.1', 'payload=' + repr(payload) + '\n' + source, timeout=50)
(artifact / 'deployment.json').write_text(result.stdout)
record = json.loads(result.stdout)
if not record['success']:
    print(result.stdout)
    raise SystemExit(1)
local = dict(host='192.0.2.1', user='operator', python=record['python'],
             worker_path=record['worker_path'], runtime_path=record['runtime_path'],
             version=record['version'])
(workspace / 'arm-button/dreamcontrol-runtime.json').write_text(json.dumps(local, indent=2))
print(result.stdout)
