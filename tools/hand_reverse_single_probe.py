"""Individually authorized, single-use left index probes with the original driver."""
import argparse
import json
from pathlib import Path
import shlex
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('phase', choices=('preflight', 'execute'))
parser.add_argument('--case', choices=('plus50', 'target200'), default='plus50')
args = parser.parse_args()
PHASE = args.phase
suffix = '-target200' if args.case == 'target200' else ''
baseline_path = ROOT / ('artifacts/hand-reverse-preflight'+suffix+'-20261012.json')
baseline = json.loads(baseline_path.read_text())['actual'] if PHASE == "execute" else None
source = r'''
import fcntl,hashlib,importlib.util,json,pathlib,subprocess,sys,time,urllib.request
root=pathlib.Path('/tmp/g1-hand-web-readonly-20261012-kyg5wk9d/hand-teleop')
driver=root/'vendor/repository/inspire_hand_ctl.py'
digest=hashlib.sha256(driver.read_bytes()).hexdigest()
assert digest=='d44845c8b28fea7ea87dc3e2df878e060468bb5c91e6436bbf62835d546b0df8','Original driver differs'
with urllib.request.urlopen('http://192.0.2.5:8768/api/config',timeout=5) as response:config=json.load(response)
assert config['mode']=='readonly','Unexpected active web mode'
lock=open('/tmp/g1-hand-workbench.lock','a+b');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
spec=importlib.util.spec_from_file_location('original_hand',str(driver));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
actual=m.read_regs(m.HANDS['L'],m.ANGLE_ACT,6)
assert len(actual)==6 and all(type(v)==int and 0<=v<=1000 for v in actual),'Invalid position sample'
target=list(actual)
if EXACT_TARGET is None:target[3]+=50
else:
 assert 75<=actual[3]<=89,'Index posture differs from the approved 82-to-200 test'
 target[3]=EXACT_TARGET
assert target[3]<=1000,'Authorized increment exceeds raw range; no clipping'
force=m.read_regs(m.HANDS['L'],m.FORCE_ACT,6)
result={'marker':'HAND_REVERSE_PROBE','phase':PHASE,'side':'L','channel':3,'name':'index','actual':actual,'force_raw_before':force,'target':target,'delta':target[3]-actual[3],'speed':300,'driver_sha256':digest,'hardware_target_commands':0,'web_mode':'readonly'}
if PHASE=='execute':
 assert max(abs(a-b) for a,b in zip(actual,BASELINE))<8,'Posture changed since preflight; no command sent'
 journal=root.parent/JOURNAL
 with journal.open('x') as output:json.dump(dict(result,state='intent-recorded'),output)
 program="import importlib.util,json,sys;s=importlib.util.spec_from_file_location('original_hand',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);a=m.set_angles(m.HANDS['L'],json.loads(sys.argv[2]));print(json.dumps(a))"
 result['hardware_target_commands']=1
 try:
  action=subprocess.run([sys.executable,'-u','-c',program,str(driver),json.dumps(target)],capture_output=True,text=True,timeout=22)
  assert action.returncode==0,action.stderr
  feedback=json.loads(action.stdout)
  assert len(feedback)==6
  result['actual_after']=feedback
  result['errors']=[abs(a-b) for a,b in zip(feedback,target)]
  result['reached']=max(result['errors'])<8
  result['force_raw_after']=m.read_regs(m.HANDS['L'],m.FORCE_ACT,6)
  result['state']='completed' if result['reached'] else 'not-reached-no-retry'
 except Exception as exc:
  result['state']='uncertain-no-retry';result['error']=str(exc)
 journal.write_text(json.dumps(result,indent=2))
print(json.dumps(result))
'''.replace("PHASE", repr(PHASE)).replace("BASELINE", repr(baseline)).replace("EXACT_TARGET", repr(200 if args.case == 'target200' else None)).replace("JOURNAL", repr('left-index-'+args.case+'-20261012-once.json'))
request = urllib.request.Request("http://192.0.2.5:9000/api/system/execute_cmd", data=json.dumps({"command": "python3 -c " + shlex.quote(source)}).encode(), headers={"Content-Type": "application/json"})
with urllib.request.urlopen(request, timeout=40) as response:
    envelope = json.load(response)
if not envelope.get("success") or envelope.get("exit_code", 0) != 0:
    raise RuntimeError(str(envelope))
receipt = json.loads(envelope["output"])
assert receipt['marker'] == 'HAND_REVERSE_PROBE'
path = baseline_path if PHASE == 'preflight' else ROOT / ('artifacts/hand-reverse-left-index-result'+suffix+'-20261012.json')
path.write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(receipt,ensure_ascii=False,indent=2))
