"""Isolated G1 audio probe/test. Default is read-only GetVolume."""
import argparse
import json
import uuid

from .audio import UnitreePlayer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--text", help="optional audible TTS test")
    parser.add_argument("--speaker-id", choices=(0, 1), type=int, default=0)
    parser.add_argument("--allow-robot-audio", action="store_true")
    args = parser.parse_args()
    if args.text is not None and not args.allow_robot_audio:
        parser.error("--text requires --allow-robot-audio (robot will speak)")
    if args.text is not None and (not args.text.strip() or len(args.text) > 1000):
        parser.error("--text must contain 1..1000 characters")
    player = UnitreePlayer(args.interface)
    print(json.dumps({"GetVolume": player.probe()}, ensure_ascii=False))
    if args.text is not None:
        player.play({"mode": "tts", "text": args.text, "speaker_id": args.speaker_id}, uuid.uuid4().hex)
        print(json.dumps({"tts_submitted": True, "audible_completion_verified": False}))


if __name__ == "__main__":
    main()
