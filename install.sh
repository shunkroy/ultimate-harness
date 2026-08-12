#!/bin/sh
set -eu
chmod 0755 /opt/harness2/bin/harness
printf '%s\n' 'Portable install: python -m pip install /opt/harness2' >&2
printf '%s\n' 'Device cutover: preserve the current binary as harness.v1, then copy bin/harness.' >&2
printf '%s\n' 'Harness v1 is never deleted by this installer.' >&2
