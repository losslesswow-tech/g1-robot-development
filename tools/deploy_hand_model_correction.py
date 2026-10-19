"""Update only the static model display in the existing readonly workbench."""
import base64
import hashlib
import json
from pathlib import Path
import shlex
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FILES = ["web/app.js", "web/index.html", "web/visual-reference.mjs"]
payload = {name: base64.b64encode((ROOT / "hand-teleop" / name).read_bytes()).decode() for name in FILES}
source = r'''
import base64,hashlib,json,os,pathlib,tempfile,urllib.request
root=pathlib.Path('/tmp/g1-hand-web-readonly-20261012-kyg5wk9d/hand-teleop')
with urllib.request.urlopen('http://192.0.2.5:8768/api/config',timeout=5) as response:
 config=json.load(response)
assert config['mode']=='readonly','Service must remain readonly'
manifest=json.loads((root/'RELEASE-MANIFEST.json').read_text())
protected=['server.py','calibration.example.json','vendor/repository/inspire_hand_ctl.py']
digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
for name in protected: assert digest(root/name)==manifest['sha256'][name],name
payload=PAYLOAD
for name in payload:
 p=root/name
 assert not p.is_symlink()
 if name in manifest['sha256']: assert digest(p)==manifest['sha256'][name],name
 else: assert not p.exists(),name
backup=pathlib.Path(tempfile.mkdtemp(prefix='model-correction-backup-',dir=str(root.parent)))
changes={}
for name,encoded in payload.items():
 p=root/name;data=base64.b64decode(encoded)
 old=p.read_bytes() if p.exists() else None
 if old is not None:
  saved=backup/name;saved.parent.mkdir(parents=True,exist_ok=True);saved.write_bytes(old)
 temporary=p.with_name(p.name+'.model-correction.tmp')
 with temporary.open('xb') as output:output.write(data)
 os.replace(str(temporary),str(p))
 changes[name]={'before_sha256':hashlib.sha256(old).hexdigest() if old is not None else None,'after_sha256':digest(p)}
 assert changes[name]['after_sha256']==hashlib.sha256(data).hexdigest()
for name in protected: assert digest(root/name)==manifest['sha256'][name],name
receipt={'marker':'HAND_MODEL_CORRECTED','mode':'readonly','backup':str(backup),'changes':changes,'protected_unchanged':protected,'hardware_writes':0,'backend_restarted':False}
with (root/'MODEL-CORRECTION-MANIFEST.json').open('x') as output:json.dump(receipt,output,indent=2)
print(json.dumps(receipt))
'''.replace("PAYLOAD", repr(payload))
request = urllib.request.Request("http://192.0.2.5:9000/api/system/execute_cmd", data=json.dumps({"command": "python3 -c " + shlex.quote(source)}).encode(), headers={"Content-Type": "application/json"})
with urllib.request.urlopen(request, timeout=40) as response:
    envelope = json.load(response)
if not envelope.get("success") or envelope.get("exit_code", 0) != 0:
    raise RuntimeError(str(envelope))
receipt = json.loads(envelope["output"])
assert receipt["marker"] == "HAND_MODEL_CORRECTED"
(ROOT / "artifacts/hand-teleop-model-correction-20261012.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(receipt, ensure_ascii=False, indent=2))
