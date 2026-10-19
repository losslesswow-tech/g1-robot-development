"""Isolated z=0.50 experiment using installed CPU simulator; no robot control."""
import base64
import hashlib
import json
from pathlib import Path
import sys

root=Path(__file__).resolve().parent
workspace=root.parent
artifact=workspace/'artifacts/dreamcontrol-z050-20261015'
artifact.mkdir(exist_ok=True)
sys.path.insert(0,str(workspace/'artifacts/migration-20261013'))
import remote_ops
runner=(root/'offline_run.py').read_bytes()
config=(root/'config/target-z050.json').read_bytes()
version=hashlib.sha256(runner+config).hexdigest()[:16]
payload={'version':version,'runner':base64.b64encode(runner).decode(),'config':base64.b64encode(config).decode()}
source='''import base64,hashlib,json,shutil,subprocess,tarfile
from pathlib import Path
root=Path('/home/operator/g1-dreamcontrol-offline-20261015')
experiment=root/'experiments'/('z050-'+payload['version'])
app=experiment/'app'
if not app.exists():
    shutil.copytree(root/'app',app,ignore=shutil.ignore_patterns('results','__pycache__'))
(app/'offline_run.py').write_bytes(base64.b64decode(payload['runner']))
(app/'config/target-z050.json').write_bytes(base64.b64decode(payload['config']))
baseline=subprocess.run([str(root/'.venv/bin/python'),str(app/'offline_run.py'),
    '--config','config/target-z031.json','--output','results/z031-baseline'],
    capture_output=True,text=True,timeout=35)
if baseline.returncode:raise RuntimeError(baseline.stderr)
result=subprocess.run([str(root/'.venv/bin/python'),str(app/'offline_run.py'),
    '--config','config/target-z050.json','--output','results/z050','--render'],
    capture_output=True,text=True,timeout=35)
record={'experiment':str(experiment),'version':payload['version'],'returncode':result.returncode,
        'stdout':result.stdout,'stderr':result.stderr,'motion_commands_sent':0}
if result.returncode==0:
    report=json.loads((app/'results/z050/report.json').read_text())
    record['report']=report
    record['baseline_report']=json.loads((app/'results/z031-baseline/report.json').read_text())
manifest={'runner_sha256':hashlib.sha256((app/'offline_run.py').read_bytes()).hexdigest(),
          'config_sha256':hashlib.sha256((app/'config/target-z050.json').read_bytes()).hexdigest(),
          'motion_commands_sent':0}
(experiment/'experiment.json').write_text(json.dumps(record,indent=2))
(experiment/'manifest.json').write_text(json.dumps(manifest,indent=2))
with tarfile.open(experiment/'evidence.tar.gz','w:gz') as stream:
    for name in ['experiment.json','manifest.json']:
        stream.add(experiment/name,arcname=name)
    if (app/'results/z050').exists():stream.add(app/'results/z050',arcname='results')
print(json.dumps(record))
'''
response=remote_ops.python('192.0.2.1','payload='+repr(payload)+'\n'+source,timeout=60)
(artifact/'offline-experiment.json').write_text(response.stdout)
record=json.loads(response.stdout)
summary={k:record[k] for k in ['version','experiment','returncode','stdout','stderr','motion_commands_sent']}
if 'report' in record:
    report=record['report']
    summary['target_xyz_m']=report['target_xyz_m']
    summary['contact_samples']=len(report['object_contacts'])
    summary['robot_self_contact_samples']=len(report.get('robot_self_contacts',[]))
    summary['robot_self_contacts']=report.get('robot_self_contacts',[])[:12]
    summary['self_contact_pairs_z050']=report.get('robot_self_contact_pairs',[])
    summary['self_contact_pairs_z031']=record['baseline_report'].get('robot_self_contact_pairs',[])
    summary['joint_target_limit_violations']=report['joint_target_limit_violations'][:12]
    summary['joint_target_limit_violation_count']=len(report['joint_target_limit_violations'])
    summary['peak_commanded_travel_deg']=report['peak_commanded_travel_deg']
    summary['peak_measured_travel_deg']=report['peak_measured_travel_deg']
print(json.dumps(summary))
if record['returncode']:raise SystemExit(1)
