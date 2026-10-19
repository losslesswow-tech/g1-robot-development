"""Stage the web workbench and verify offline on Linux; no host install or hand IO."""
import base64
import hashlib
import json
from pathlib import Path
import shlex
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
API = "http://192.0.2.5:9000/api/system/execute_cmd"


def remote(source, timeout=30):
    request = urllib.request.Request(API, data=json.dumps({
        "command": "python3 -c " + shlex.quote(source)}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        envelope = json.load(response)
    if not envelope.get("success") or envelope.get("exit_code", 0) != 0:
        raise RuntimeError("Remote staging failed: " + str(envelope))
    result = json.loads(envelope["output"])
    if result.get("marker") != "HAND_WEB_STAGE":
        raise RuntimeError("Unexpected staging response")
    return result


def main():
    package = ROOT / "artifacts/g1-hand-teleop-20261012.zip"
    raw = package.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    prefix = "/tmp/g1-hand-web-" + sha[:12]
    # Individual immutable chunks stay below shell argument-size limits.
    chunks = [raw[start:start+30000] for start in range(0, len(raw), 30000)]
    for index, data in enumerate(chunks):
        path = prefix + ".part%03d" % index
        source = """import base64,json,pathlib
p=pathlib.Path(PATH);data=base64.b64decode(DATA)
if p.exists() or p.is_symlink():
 assert not p.is_symlink() and p.read_bytes()==data,'Existing chunk differs'
else:
 with p.open('xb') as f:f.write(data)
print(json.dumps({'marker':'HAND_WEB_STAGE','chunk':INDEX}))
""".replace("PATH", repr(path)).replace("DATA", repr(base64.b64encode(data).decode())).replace("INDEX", str(index))
        remote(source)
    assembled = remote("""import hashlib,json,pathlib
prefix=PREFIX;p=pathlib.Path(prefix+'.zip')
data=b''.join(pathlib.Path(prefix+'.part%03d'%i).read_bytes() for i in range(COUNT))
assert hashlib.sha256(data).hexdigest()==SHA
if p.exists() or p.is_symlink():
 assert not p.is_symlink() and p.read_bytes()==data,'Existing archive differs'
else:
 with p.open('xb') as f:f.write(data)
print(json.dumps({'marker':'HAND_WEB_STAGE','package':str(p),'sha256':SHA}))
""".replace("PREFIX", repr(prefix)).replace("COUNT", str(len(chunks))).replace("SHA", repr(sha)))

    receipt = remote("""import hashlib,json,pathlib,subprocess,tempfile,zipfile,sys,platform
folder=pathlib.Path(tempfile.mkdtemp(prefix='g1-hand-web-verify-'))
with zipfile.ZipFile(PACKAGE) as z:
 for item in z.infolist():
  target=(folder/item.filename).resolve()
  assert folder.resolve() in target.parents,'Invalid archive member'
 z.extractall(folder)
work=folder/'hand-teleop'
manifest=json.loads((work/'RELEASE-MANIFEST.json').read_text())
for name,digest in manifest['sha256'].items():
 assert hashlib.sha256((work/name).read_bytes()).hexdigest()==digest,name
test=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p','test_workbench.py','-v'],cwd=str(work),capture_output=True,text=True,timeout=25)
print(json.dumps({'marker':'HAND_WEB_STAGE','verification_directory':str(folder),'python':sys.version,'platform':platform.platform(),'offline_tests_exit_code':test.returncode,'offline_tests_output':test.stdout+test.stderr,'hardware_connections':0,'hardware_writes':0}))
""".replace("PACKAGE", repr(prefix + ".zip")))
    if receipt["offline_tests_exit_code"] != 0:
        raise RuntimeError(receipt["offline_tests_output"])

    # This entry is run by operator on the host; it only launches simulation.
    bootstrap = """#!/usr/bin/env bash
set -euo pipefail
umask 077
[[ $(id -un) == operator && ${HOME:-} == /home/operator ]] || { printf '%s\n' 'Run as operator on the navigation host.' >&2; exit 1; }
[[ ! -f /.dockerenv ]] || { printf '%s\n' 'Run on the host, not inside a container.' >&2; exit 1; }
hand_web_dir=$(mktemp -d /home/operator/g1-hand-web-20261012.XXXXXXXX)
docker cp nav_robot:PACKAGE "$hand_web_dir/module.zip"
printf '%s  %s\n' SHA "$hand_web_dir/module.zip" | sha256sum -c -
python3 -m zipfile -e "$hand_web_dir/module.zip" "$hand_web_dir"
cd "$hand_web_dir/hand-teleop"
python3 -m unittest discover -s tests -p 'test_workbench.py' -v
printf '%s\n' 'Open http://192.0.2.5:8768 in your browser. This is SIMULATION ONLY.'
exec python3 server.py --mode simulation --bind 192.0.2.5 --port 8768
""".replace("PACKAGE", shlex.quote(prefix + ".zip")).replace("SHA", shlex.quote(sha))
    entry = "/tmp/g1-hand-web-install-20261012-" + sha[:12] + ".sh"
    local = ROOT / "artifacts" / Path(entry).name
    local.write_text(bootstrap, encoding="utf-8", newline="\n")
    result = remote("""import base64,json,pathlib,subprocess
data=base64.b64decode(DATA);p=pathlib.Path(ENTRY)
check=subprocess.run(['bash','-n'],input=data,capture_output=True)
assert check.returncode==0,check.stderr.decode()
if p.exists() or p.is_symlink():
 assert not p.is_symlink() and p.read_bytes()==data,'Existing entry differs'
else:
 with p.open('xb') as f:f.write(data)
print(json.dumps({'marker':'HAND_WEB_STAGE','entry':str(p),'bash_syntax_passed':True}))
""".replace("DATA", repr(base64.b64encode(bootstrap.encode()).decode())).replace("ENTRY", repr(entry)))
    receipt.update(assembled)
    receipt.update(result)
    receipt.update(host_install_performed=False, remote_server_started=False,
                   commands=["docker cp nav_robot:"+entry+" "+entry, "bash "+entry])
    (ROOT / "artifacts/hand-teleop-staged-20261012.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
