"""Only connect/read/QUIT. Never calls acquire, set, stop-service or release."""
import json
import os
from pathlib import Path
import time
from backend import ROOT,SSHDriver

runtime=json.loads((ROOT/'runtime.example.json').read_text(encoding='utf-8'))
key=Path(os.environ['TEMP'])/'g1-migration-20261013/id_ed25519'
driver=SSHDriver(key,runtime['worker_path'])
try:
    driver.connect();time.sleep(.4);state=driver.state()
    assert state['fresh'] and state['phase']=='readonly' and state['command_frames']==0
    assert len(state['joints'])==14 and state['fsm_id']>=0
    report={'result':'readonly_verified','motion_commands_sent':0,'acquire_called':False,'set_called':False,'state':state}
finally:
    process=driver.process;driver.close()
if process:report['worker_exitcode']=process.returncode
assert report['worker_exitcode']==0
path=ROOT.parent/'artifacts/arm-debug-20261014/readonly-verification.json'
path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in report.items() if k!='state'},ensure_ascii=False))
print(json.dumps({'fsm_id':state['fsm_id'],'fsm_mode':state['fsm_mode'],'fresh':state['fresh'],'phase':state['phase'],
                  'command_frames':state['command_frames'],'right_q_rad':[j['q_rad'] for j in state['joints'] if j['index']>=22]}))
