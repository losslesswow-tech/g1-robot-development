"""Local JSONL replay first. Live event adapter is intentionally separate."""
import argparse
import json
import sys
from pathlib import Path

from .audio import DryRunPlayer, UnitreePlayer
from .config import load_config
from .core import ArrivalGate, Event


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--database", required=True, help="persistent SQLite file, distinct for simulation/live")
    parser.add_argument("--events", help="JSONL file; default stdin")
    parser.add_argument("--backend", choices=("dry-run", "unitree"), default="dry-run")
    parser.add_argument("--interface", help="DDS NIC; required for unitree")
    parser.add_argument("--allow-robot-audio", action="store_true", help="explicitly permit audible robot output")
    args = parser.parse_args()
    if args.backend == "unitree" and not (args.interface and args.allow_robot_audio):
        parser.error("unitree requires --interface and --allow-robot-audio")
    messages, duration = load_config(args.config)
    Path(args.database).resolve().parent.mkdir(parents=True, exist_ok=True)
    player = UnitreePlayer(args.interface) if args.backend == "unitree" else DryRunPlayer()
    if args.backend == "unitree":
        player.probe()
    gate = ArrivalGate(args.database, messages, duration)
    stream = open(args.events, encoding="utf-8") if args.events else sys.stdin
    failed = False
    try:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                event = Event(**json.loads(line))
                result = gate.handle(event, player)
                print(json.dumps({"line": line_number, "task_id": event.task_id, "result": result}, ensure_ascii=False))
                failed |= result == "audio_failed_or_unknown"
            except (ValueError, TypeError) as exc:
                print(json.dumps({"line": line_number, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
                failed = True
    finally:
        gate.close()
        if args.events:
            stream.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
