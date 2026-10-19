"""Deploy the thumb display-direction fix without restarting or actuating."""
import base64
import argparse
import json
from pathlib import Path
import shlex
import urllib.request

root = Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--rotation',action='store_true')
args=parser.parse_args()
names = ['web/app.js', 'web/visual-reference.mjs']
if args.rotation:names.append('web/index.html')
payload = {name: base64.b64encode((root/'hand-teleop'/name).read_bytes()).decode() for name in names}
previous = json.loads((root/'artifacts/hand-teleop-model-correction-20261012.json').read_text())
latest=json.loads((root/'artifacts/hand-thumb-display-correction-20261012.json').read_text()) if args.rotation else previous
expected = {name: (latest if name in latest['changes'] else previous)['changes'][name]['after_sha256'] for name in names}
record_name='THUMB-ROTATION-CORRECTION-MANIFEST.json' if args.rotation else 'THUMB-DISPLAY-CORRECTION-MANIFEST.json'
source = r'''
import base64,hashlib,json,os,pathlib,tempfile,urllib.request
root=pathlib.Path('/tmp/g1-hand-web-readonly-20261012-kyg5wk9d/hand-teleop')
with urllib.request.urlopen('http://192.0.2.5:8768/api/config',timeout=5) as response:config=json.load(response)
assert config['mode']=='readonly'
digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
release=json.loads((root/'RELEASE-MANIFEST.json').read_text())
protected=['server.py','calibration.example.json','vendor/repository/inspire_hand_ctl.py']
for name in protected:assert digest(root/name)==release['sha256'][name],name
payload=PAYLOAD;expected=EXPECTED
for name in payload:assert not (root/name).is_symlink() and digest(root/name)==expected[name],name
record=root/RECORD_NAME
assert not record.exists()
backup=pathlib.Path(tempfile.mkdtemp(prefix='thumb-display-backup-',dir=str(root.parent)))
changes={}
for name,encoded in payload.items():
 p=root/name;data=base64.b64decode(encoded)
 saved=backup/name;saved.parent.mkdir(parents=True,exist_ok=True);saved.write_bytes(p.read_bytes())
 temporary=p.with_name(p.name+'.thumb-display.tmp')
 with temporary.open('xb') as output:output.write(data)
 os.replace(str(temporary),str(p))
 changes[name]={'before_sha256':expected[name],'after_sha256':digest(p)}
 assert digest(p)==hashlib.sha256(data).hexdigest()
for name in protected:assert digest(root/name)==release['sha256'][name],name
result={'marker':'THUMB_DISPLAY_CORRECTED','mode':'readonly','changes':changes,'backup':str(backup),'protected_unchanged':protected,'hardware_writes':0,'backend_restarted':False}
with record.open('x') as output:json.dump(result,output,indent=2)
print(json.dumps(result))
'''.replace('PAYLOAD',repr(payload)).replace('EXPECTED',repr(expected)).replace('RECORD_NAME',repr(record_name))
request=urllib.request.Request('http://192.0.2.5:9000/api/system/execute_cmd',data=json.dumps({'command':'python3 -c '+shlex.quote(source)}).encode(),headers={'Content-Type':'application/json'})
with urllib.request.urlopen(request,timeout=30) as response:envelope=json.load(response)
assert envelope.get('success') and envelope.get('exit_code',0)==0,envelope
result=json.loads(envelope['output']);assert result['marker']=='THUMB_DISPLAY_CORRECTED'
report_name='hand-thumb-rotation-correction-20261012.json' if args.rotation else 'hand-thumb-display-correction-20261012.json'
(root/'artifacts'/report_name).write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result,indent=2))
