"""Regression: an expired UI lease during preparation must send no P frames."""
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest


class WorkerLeaseTests(unittest.TestCase):
    def test_preparation_does_not_refresh_an_expired_client_heartbeat(self):
        directory = str(Path(__file__).resolve().parents[1])
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / 'runtime.json'
            runtime.write_text(json.dumps(dict(log_root=temporary)))
            source = f'''
import sys,time
sys.path.insert(0,{directory!r})
import action_flow,console_worker
action_flow.LEASE_SECONDS=.05
original=console_worker.resources
def resources(runtime,mock):
    defaults,policy,factory=original(runtime,True)
    class Delayed(console_worker.MockBridge):
        def wait(self):
            time.sleep(.2)
            return super().wait()
    return defaults,policy,lambda execute,directory:Delayed(defaults)
console_worker.resources=resources
sys.argv=['console_worker','--mock','--runtime',{str(runtime)!r}]
console_worker.main()
'''
            process = subprocess.Popen([sys.executable, '-u', '-c', source], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            events = queue.Queue()
            def read():
                for line in process.stdout:
                    events.put(json.loads(line))
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            try:
                process.stdin.write(json.dumps(dict(action='connect')) + '\n')
                process.stdin.write(json.dumps(dict(action='start', target_xyz_m=[.5,-.23,.31], stop_at='released')) + '\n')
                process.stdin.flush()
                for unused in range(30):
                    event = events.get(timeout=3)
                    if event.get('error') == 'page_heartbeat_timeout':
                        self.assertEqual(event['native_finished']['sent'], 0)
                        self.assertEqual(event['native_finished']['final_weight'], 0)
                        self.assertFalse(event['active'])
                        break
                else:
                    self.fail('expected pre-motion lease timeout')
            finally:
                process.stdin.write(json.dumps(dict(action='quit')) + '\n')
                process.stdin.flush()
                process.stdin.close()
                process.wait(timeout=3)
                reader.join(timeout=1)
                process.stdout.close()
                process.stderr.close()


if __name__ == '__main__':
    unittest.main()
