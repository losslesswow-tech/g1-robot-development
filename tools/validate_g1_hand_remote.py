"""Run offline tests in memory, then the production CLI's read-only status.

No program is installed remotely. Tests use temporary profiles and fake sockets;
the final status phase sends only the four known FC03 telemetry requests.
"""
import json
from pathlib import Path
import shlex
import urllib.request


def main():
    root = Path(__file__).resolve().parents[1]
    source_root = root / "_repo_inspection" / "g1-robot"
    driver = (source_root / "tools" / "inspire_hand_ctl.py").read_text(encoding="utf-8")
    tests = (source_root / "tests" / "test_inspire_hand_ctl.py").read_text(encoding="utf-8")
    source = "DRIVER_SOURCE = " + repr(driver) + "\nTEST_SOURCE = " + repr(tests) + r'''
import contextlib,datetime,hashlib,io,json,pathlib,socket,sys,types,unittest
from unittest.mock import patch
module=types.ModuleType('inspire_hand_ctl')
module.__file__='/tmp/g1-hand-inmemory/tools/inspire_hand_ctl.py'
sys.modules[module.__name__]=module
exec(compile(DRIVER_SOURCE,module.__file__,'exec'),module.__dict__)
test_module=types.ModuleType('test_inspire_hand_ctl')
test_module.__file__='/tmp/g1-hand-inmemory/tests/test_inspire_hand_ctl.py'
exec(compile(TEST_SOURCE,test_module.__file__,'exec'),test_module.__dict__)
log=io.StringIO()
suite=unittest.defaultTestLoader.loadTestsFromModule(test_module)
result=unittest.TextTestRunner(stream=log,verbosity=1).run(suite)
out={'observed_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
     'python':sys.version,'execution_scope':'navigation container, not host Python',
     'source_sha256':hashlib.sha256(DRIVER_SOURCE.encode()).hexdigest(),
     'tests_run':result.testsRun,'tests_passed':result.wasSuccessful(),'test_log':log.getvalue(),
     'files_installed':False,'motion_commands_sent':0,'register_write_commands_sent':0}
if result.wasSuccessful():
 buffer=io.StringIO()
 with patch.object(module.socket,'create_connection',side_effect=AssertionError('preview attempted network')),contextlib.redirect_stdout(buffer):
  out['preview_exit_code']=module.main(['finger','R','0','800'])
 out['preview_output']=buffer.getvalue()
 real_connect=socket.create_connection
 connections=[]
 def traced_connect(address,**kwargs):
  if address not in [('192.0.2.3',6000),('192.0.2.4',6000)]:
   raise ValueError('unexpected telemetry destination')
  sock=real_connect(address,**kwargs)
  connections.append({'peer':address,'local':sock.getsockname()})
  return sock
 buffer,errors=io.StringIO(),io.StringIO()
 with patch.object(module.socket,'create_connection',side_effect=traced_connect),contextlib.redirect_stdout(buffer),contextlib.redirect_stderr(errors):
  out['status_exit_code']=module.main(['status','B'])
 out['status_stdout']=buffer.getvalue();out['status_stderr']=errors.getvalue()
 out['connections']=connections
out['routes_from_shared_network_namespace']=pathlib.Path('/proc/net/route').read_text()
out['marker']='G1_HAND_REMOTE_VALIDATION_COMPLETE'
print(json.dumps(out))
'''
    request = urllib.request.Request(
        "http://192.0.2.5:9000/api/system/execute_cmd",
        data=json.dumps({"command": "python3 -c " + shlex.quote(source)}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=25) as response:
        envelope = json.load(response)
    result = json.loads(envelope["output"])
    if result.get("marker") != "G1_HAND_REMOTE_VALIDATION_COMPLETE":
        raise RuntimeError("remote validation did not complete")
    target = root / "artifacts" / "g1-hand-remote-validation-20261012.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("tests_passed") or result.get("status_exit_code") != 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
