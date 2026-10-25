# tools/capture_probe.py
# Computer audio probe (#108): checks that Windows loopback capture opens on
# this computer, for each source the back end can record, and prints numbers only.
#
#   python tools/capture_probe.py            # list outputs, then probe every source
#   python tools/capture_probe.py --list     # only count the output devices
#
# What it does, and what it never does:
#   - Lists the active output devices (a count, and how many are the default;
#     device names are not printed).
#   - Opens loopback on every active output, mixed (the source the app
#     records), then on the default output, for at most one second each,
#     then process loopback of this probe's own process (which plays nothing)
#     for at most one second, and prints how many frames arrived, how many were
#     not silent, and the device events seen.
#   - It never plays a sound, never opens the microphone and saves no audio.
#     Run it with nothing private playing: loopback hears what the computer
#     plays, even though only counts are printed.
# Real-call, echo, drift and headset checks are manual sittings (#189).

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Computer audio probe (#108): numbers only.")
    parser.add_argument("--list", action="store_true", help="only count the output devices")
    parser.add_argument("--seconds", type=float, default=1.0,
                        help="how long to open each stream (at most 1)")
    args = parser.parse_args(argv)
    from workspace import loopback

    report = {}
    try:
        outputs = loopback.list_outputs()
        report["outputs"] = {"count": len(outputs),
                             "default": sum(1 for o in outputs if o["default"])}
    except loopback.LoopbackError as exc:
        report["outputs"] = {"error": str(exc)}
    if not args.list:
        for name, source in (("all", {"kind": "all"}), ("endpoint", {"kind": "endpoint"}),
                             ("process_self", {"kind": "process", "pid": os.getpid()})):
            try:
                report[name] = loopback.probe(source, args.seconds)
            except loopback.LoopbackError as exc:
                report[name] = {"error": str(exc),
                                "hresult": None if exc.hresult is None
                                else f"0x{exc.hresult:08X}"}
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
