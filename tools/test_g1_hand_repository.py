"""Run one explicitly authorized repository hand command, without changing it.

Defaults to an offline plan. The original source and all write values stay intact.
Each execution is one HTTP request, without retries; telemetry gates the next step.
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "_repo_inspection/g1-robot-dd6ea93-hand/tools/inspire_hand_ctl.py"
SOURCE_URL = "https://source.example.invalid/robot/reference"
API_URL = "http://192.0.2.5:9000/api/system/execute_cmd"

CHILD = r'''
import base64, json, sys
from datetime import datetime, timezone
ns = {"__name__": "repository_hand_test"}
exec(compile(base64.b64decode("SOURCE_BASE64"), "repository/inspire_hand_ctl.py", "exec"), ns)
side, command = "TEST_SIDE", "TEST_COMMAND"
index, angle = FINGER_INDEX, FINGER_ANGLE
positions = POSITION_VALUES
ip = ns["HANDS"][side]
before = ns["read_regs"](ip, ns["ANGLE_ACT"], 6)
if len(before) != 6 or any(type(v) is not int or not 0 <= v <= 1000 for v in before):
    raise RuntimeError("Invalid preflight angle telemetry; no motion sent")
if command == "finger":
    target = list(before)
    target[index] = angle
elif command == "pos":
    target = positions
else:
    target = ns["pct_to_angles"](0.1 if command == "open" else 1.0)
print("REPOSITORY_COMMAND_START " + json.dumps({"side": side, "command": command,
      "target": target, "speed": 300, "before": before}), flush=True)
sys.argv = ["inspire_hand_ctl.py", command, side]
if command == "finger":
    sys.argv.extend([str(index), str(angle)])
elif command == "pos":
    sys.argv.extend([str(value) for value in positions])
exit_code = ns["main"]()
after = ns["read_regs"](ip, ns["ANGLE_ACT"], 6)
force = ns["read_regs"](ip, ns["FORCE_ACT"], 6)
reached = len(after) == 6 and (abs(after[index]-angle) < 8 if command == "finger"
          else max(abs(a-b) for a,b in zip(after,target)) < 8)
result = {"side": side, "command": command, "target": target, "speed": 300,
          "before": before, "actual": after, "force_raw": force,
          "repository_exit_code": exit_code, "reached": reached,
          "observed_at_utc": datetime.now(timezone.utc).isoformat()}
if command == "finger":
    result.update(controlled_channel=index, controlled_target=angle,
                  reached_scope="controlled channel only",
                  other_targets="Original finger command reads all actual angles again before writing")
print("REPOSITORY_COMMAND_RESULT " + json.dumps(result), flush=True)
sys.exit(0 if exit_code == 0 and reached else 2)
'''

PARENT = r'''
import base64, json, subprocess
result = {"marker": "G1_REPOSITORY_HAND_TEST", "automatic_retries": 0}
try:
    p = subprocess.run(["python3", "-u", "-c", base64.b64decode("CHILD_BASE64").decode()],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=22)
    result.update(exit_code=p.returncode, stdout=p.stdout, stderr=p.stderr, timed_out=False)
except subprocess.TimeoutExpired as exc:
    def text(v):
        return v.decode(errors="replace") if isinstance(v, bytes) else (v or "")
    result.update(exit_code=None, stdout=text(exc.stdout), stderr=text(exc.stderr),
                  timed_out=True, outcome="Motion may still be executing; do not retry")
print(json.dumps(result))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("open", "close", "finger", "pos"))
    parser.add_argument("side", choices=("L", "R"))
    parser.add_argument("values", nargs="*", type=int)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    args.index, args.angle = None, None
    if args.command == "finger":
        if len(args.values) != 2 or not 0 <= args.values[0] <= 5 or not 0 <= args.values[1] <= 1000:
            parser.error("finger requires index 0..5 and angle 0..1000")
        args.index, args.angle = args.values
    elif args.command == "pos":
        if len(args.values) != 6 or any(not 0 <= value <= 1000 for value in args.values):
            parser.error("pos requires six values in 0..1000")
    elif args.values:
        parser.error("Only finger and pos accept position values")
    reference = REFERENCE.read_text(encoding="utf-8").replace("\r\n", "\n")
    target = [100 if args.command == "open" else 1000] * 6
    if args.command == "finger":
        target = ["Original finger command uses actual angle"] * 6
        target[args.index] = args.angle
    elif args.command == "pos":
        target = args.values
    plan = {"mode": "LIVE_REPOSITORY_COMMAND" if args.execute else "OFFLINE_PLAN",
            "side": args.side, "command": args.command,
            "target": target,
            "speed": 300, "source_url": SOURCE_URL, "repository_source_modified": False}
    print(json.dumps(plan, ensure_ascii=False), flush=True)
    if not args.execute:
        return 0
    with urllib.request.urlopen(SOURCE_URL, timeout=15) as response:
        source = response.read().decode("utf-8").replace("\r\n", "\n")
    if source != reference:
        raise RuntimeError("Repository source changed; no motion sent")
    child = CHILD.replace("SOURCE_BASE64", base64.b64encode(source.encode()).decode())
    child = child.replace("TEST_SIDE", args.side).replace("TEST_COMMAND", args.command)
    child = child.replace("FINGER_INDEX", str(args.index if args.index is not None else 0))
    child = child.replace("FINGER_ANGLE", str(args.angle if args.angle is not None else 0))
    child = child.replace("POSITION_VALUES", json.dumps(args.values if args.command == "pos" else []))
    parent = PARENT.replace("CHILD_BASE64", base64.b64encode(child.encode()).decode())
    request = urllib.request.Request(API_URL,
        data=json.dumps({"command": "python3 -c " + shlex.quote(parent)}).encode(),
        headers={"Content-Type": "application/json"})
    record = {"plan": plan, "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
              "requested_at_utc": datetime.now(timezone.utc).isoformat()}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = ROOT / "artifacts" / ("g1-hand-repository-{}-{}-{}.json".format(args.side,args.command,stamp))
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            envelope = json.load(response)
        record["management_response"] = envelope
        output = envelope.get("output", "")
        result = json.loads(output)
        record["remote_result"] = result
        if result.get("marker") != "G1_REPOSITORY_HAND_TEST":
            raise RuntimeError("Unexpected response; execution outcome uncertain; do not retry")
        print(result.get("stdout", ""), end="", flush=True)
        print(result.get("stderr", ""), file=sys.stderr, end="", flush=True)
        lines = result.get("stdout", "").splitlines()
        summaries = [json.loads(line.split(" ",1)[1]) for line in lines
                     if line.startswith("REPOSITORY_COMMAND_RESULT ")]
        record["telemetry"] = summaries[-1] if summaries else None
        passed = (result.get("exit_code") == 0 and not result.get("timed_out")
                  and bool(summaries) and summaries[-1].get("reached") is True)
        record["phase_passed"] = passed
        return 0 if passed else 2
    except Exception as exc:
        record["error"] = str(exc)
        record["phase_passed"] = False
        print("Execution not confirmed; do not retry: " + str(exc), file=sys.stderr)
        return 2
    finally:
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Record: " + str(path), flush=True)


if __name__ == "__main__":
    sys.exit(main())
