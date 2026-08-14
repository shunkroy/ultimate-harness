"""Stable-process-title parent for the Prime daemon.

Prime overwrites its own argv to ``prime-agent``. The harness supervisor needs
an immutable identity for safe PID verification, so it supervises this parent;
the parent launches Prime as its child in the same process group and forwards
termination signals.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon-socket", required=True)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--cwd", required=True)
    args = parser.parse_args(argv)
    child = subprocess.Popen(
        [args.node, args.bundle, "--daemon-socket", args.daemon_socket, "--mode", "daemon"],
        stdin=subprocess.DEVNULL,
        stdout=None,
        stderr=None,
        cwd=args.cwd,
        env=os.environ.copy(),
    )

    def stop(signum, _frame):
        if child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
