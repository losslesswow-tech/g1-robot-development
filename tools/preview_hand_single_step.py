"""Local UI test server with a FAKE driver; no robot connections or writes."""
from pathlib import Path
import sys

root=Path(__file__).resolve().parents[1]/'hand-teleop'
sys.path.insert(0,str(root/'tests'))
from test_single_step import FakeDriver, step

bench=step.StepWorkbench(step.load_mapping(root/'calibration.example.json'),
                         FakeDriver(),step.load_plan(root/'single-step-left-index.json'))
http=step.ThreadingHTTPServer(('127.0.0.1',8798),step.make_handler(bench,'offline-test'))
print('FAKE ONLY http://127.0.0.1:8798 token=offline-test',flush=True)
try:http.serve_forever()
finally:http.server_close()
