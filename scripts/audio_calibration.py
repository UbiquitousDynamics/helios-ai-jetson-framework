"""Select or snapshot a device-local candidate. Never opens audio or enables writes."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recognizer.calibration import CalibrationError, pulse_audio_identity, selected_calibration


def main(argv=None, *, environment=None, probe=pulse_audio_identity):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source")
    parser.add_argument("--model", type=Path)
    parser.add_argument("--rate", type=int, default=16000)
    parser.add_argument("--channel-mode", default="mono")
    parser.add_argument("--candidate-output", type=Path)
    parser.add_argument("--profile-id")
    args = parser.parse_args(argv)
    env = os.environ if environment is None else environment
    selected = bool(env.get("HELIOS_AUDIO_CALIBRATION_CONFIG", "").strip())
    if args.candidate_output and selected:
        parser.error("Separate candidate creation from selected-profile verification")
    if not args.candidate_output and not selected:
        print(json.dumps({"status": "not_selected", "voice_writes_enabled": False}))
        return 0
    if not args.source or args.model is None:
        parser.error("Explicit --source and --model required")
    if args.candidate_output and not (args.profile_id and args.profile_id.strip()):
        parser.error("--profile-id required for a candidate")

    def identity():
        return probe(
            args.source, args.model, sample_rate_hz=args.rate, channel_mode=args.channel_mode
        )

    try:
        if args.candidate_output:
            candidate = {
                "schema_version": 1,
                "profile_id": args.profile_id,
                "status": "candidate",
                "identity": asdict(identity()),
            }
            fd = os.open(args.candidate_output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(candidate, handle, indent=2)
                handle.write("\n")
            status = "candidate_created_not_calibrated"
        else:
            binding = selected_calibration(env, identity)
            assert binding is not None
            status = "candidate_identity_matches_not_calibrated"
        print(json.dumps({"status": status, "voice_writes_enabled": False}))
        return 0
    except (CalibrationError, OSError):
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "profile_or_identity_invalid",
                    "voice_writes_enabled": False,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
