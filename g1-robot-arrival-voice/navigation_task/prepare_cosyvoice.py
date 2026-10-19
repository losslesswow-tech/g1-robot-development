"""Package and stage the audio-only update in the existing navigation container."""
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import shlex
import urllib.request
import zipfile


FILES = ("arrival_voice/web.py", "arrival_voice/cosyvoice.py", "arrival_voice/cosyvoice.example.json",
         "navigation_task/install_cosyvoice.sh")


def package(root):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.writestr(name, (root / name).read_bytes().replace(b"\r\n", b"\n"))
    return buffer.getvalue()


def remote(api, source):
    command = "python3 -c " + shlex.quote(source)
    req = urllib.request.Request(api.rstrip("/") + "/api/system/execute_cmd",
                                 data=json.dumps({"command": command}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as response:
        result = json.load(response)
    return result.get("output", "")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", action="store_true", help="Stage files only; never run the host installer")
    parser.add_argument("--api", default="http://192.0.2.5:9000")
    parser.add_argument("--verify", action="store_true", help="Run offline tests in a separate /tmp container directory")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    raw = package(root)
    sha = hashlib.sha256(raw).hexdigest()
    artifacts = root / ".local-voice/artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    name = "g1-cosyvoice-20261010-" + sha[:12] + ".zip"
    (artifacts / name).write_bytes(raw)
    receipt = {"package": str(artifacts / name), "sha256": sha, "staged": False,
               "activation_performed": False, "robot_audio_submitted": False}
    if args.stage:
        target = "/tmp/" + name
        for offset in range(0, len(raw), 20000):
            chunk = raw[offset:offset + 20000]
            source = "import base64,pathlib; p=pathlib.Path({!r}); data=base64.b64decode({!r}); print(p.open({!r}).write(data))".format(
                target, base64.b64encode(chunk).decode(), "wb" if offset == 0 else "ab")
            assert int(remote(args.api, source).strip()) == len(chunk)
        entry = "/tmp/g1-cosyvoice-install-20261010.sh"
        bootstrap = '''#!/usr/bin/env bash
set -euo pipefail
umask 077
voice_update_dir=$(mktemp -d /home/operator/g1-cosyvoice-update.XXXXXXXX)
docker cp nav_robot:PACKAGE "$voice_update_dir/module.zip"
printf '%s  %s\\n' 'SHA' "$voice_update_dir/module.zip" | sha256sum -c -
python3 -m zipfile -e "$voice_update_dir/module.zip" "$voice_update_dir"
bash "$voice_update_dir/navigation_task/install_cosyvoice.sh"
'''.replace("PACKAGE", target).replace("SHA", sha)
        source = '''import base64,hashlib,json,pathlib,subprocess,zipfile
p=pathlib.Path(TARGET)
assert hashlib.sha256(p.read_bytes()).hexdigest()==SHA
with zipfile.ZipFile(p) as z:
 assert set(z.namelist())==set(FILES)
 for n in ('arrival_voice/web.py','arrival_voice/cosyvoice.py'):
  compile(z.read(n),n,'exec')
 r=subprocess.run(['bash','-n'],input=z.read('navigation_task/install_cosyvoice.sh'),capture_output=True)
 assert r.returncode==0,r.stderr.decode()
entry=pathlib.Path(ENTRY)
entry.write_bytes(base64.b64decode(BOOTSTRAP))
r=subprocess.run(['bash','-n',str(entry)],capture_output=True)
assert r.returncode==0,r.stderr.decode()
print(json.dumps({'package':str(p),'installer':str(entry),'sha256':SHA,'staged':True,'activation_performed':False}))
'''.replace("TARGET", repr(target)).replace("SHA", repr(sha)).replace("FILES", repr(FILES)).replace("ENTRY", repr(entry)).replace("BOOTSTRAP", repr(base64.b64encode(bootstrap.encode()).decode()))
        receipt.update(json.loads(remote(args.api, source)))
    if args.verify:
        validation = io.BytesIO()
        with zipfile.ZipFile(validation, "w", zipfile.ZIP_DEFLATED) as archive:
            for directory in ("arrival_voice", "navigation_task", "tests"):
                for path in sorted((root / directory).rglob("*")):
                    if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                        archive.writestr(path.relative_to(root).as_posix(), path.read_bytes().replace(b"\r\n", b"\n"))
        validation_raw = validation.getvalue()
        validation_sha = hashlib.sha256(validation_raw).hexdigest()
        validation_target = "/tmp/g1-cosyvoice-offline-" + validation_sha[:12] + ".zip"
        for offset in range(0, len(validation_raw), 20000):
            chunk = validation_raw[offset:offset + 20000]
            source = "import base64,pathlib; p=pathlib.Path({!r}); print(p.open({!r}).write(base64.b64decode({!r})))".format(
                validation_target, "wb" if offset == 0 else "ab", base64.b64encode(chunk).decode())
            assert int(remote(args.api, source).strip()) == len(chunk)
        source = '''import hashlib,json,pathlib,subprocess,tempfile,zipfile
p=pathlib.Path(TARGET)
assert hashlib.sha256(p.read_bytes()).hexdigest()==SHA
with tempfile.TemporaryDirectory(prefix='g1-cosyvoice-offline-') as directory:
 with zipfile.ZipFile(p) as z:
  assert all(not pathlib.PurePosixPath(n).is_absolute() and '..' not in pathlib.PurePosixPath(n).parts for n in z.namelist())
  z.extractall(directory)
 r=subprocess.run(['python3','-m','unittest','discover','-s','tests'],cwd=directory,capture_output=True,text=True,timeout=45)
 print(json.dumps({'returncode':r.returncode,'output':(r.stdout+r.stderr)[-5000:],'robot_audio_submitted':False}))
'''.replace("TARGET", repr(validation_target)).replace("SHA", repr(validation_sha))
        validation_result = json.loads(remote(args.api, source))
        receipt["offline_validation"] = validation_result
        if validation_result["returncode"] != 0:
            print(json.dumps(validation_result, ensure_ascii=False))
            raise SystemExit("Container offline validation failed")
    (artifacts / "cosyvoice-stage-20261010.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
