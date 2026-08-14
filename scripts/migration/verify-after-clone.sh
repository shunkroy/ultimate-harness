#!/bin/sh
set -eu

PACKAGE=${1:?usage: verify-after-clone.sh PACKAGE REPO KEY_FILE}
REPO=${2:-.}
KEY_FILE=${3:?usage: verify-after-clone.sh PACKAGE REPO KEY_FILE}

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
"$SCRIPT_DIR/verify-package.sh" "$PACKAGE" "$KEY_FILE"

python3 - "$PACKAGE" "$REPO" <<'PY'
import pathlib
import subprocess
import sys

package = pathlib.Path(sys.argv[1]).resolve()
repo = pathlib.Path(sys.argv[2]).resolve()
source = {}
for line in (package / "SOURCE.txt").read_text(encoding="utf-8").splitlines():
    key, separator, value = line.partition("=")
    if separator:
        source[key] = value
expected = source.get("commit", "")
head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True).stdout.strip()
dirty = subprocess.run(
    ["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo,
    check=True, text=True, capture_output=True,
).stdout
if head != expected:
    raise SystemExit("cloned HEAD does not match sealed commit")
if dirty.strip():
    raise SystemExit("cloned repository is dirty")
print(f"clone_ok={head}")
PY

(
    cd "$REPO"
    python3 -m compileall -q harness2 tests
    python3 -m unittest discover -s tests
    python3 - <<'PY'
import pathlib
import subprocess
import sys
import tempfile
import tomllib

repo = pathlib.Path.cwd()
expected = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
with tempfile.TemporaryDirectory(prefix="harness-migration-smoke-") as value:
    root = pathlib.Path(value)
    wheel_dir = root / "dist"
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation", "--wheel-dir", str(wheel_dir)],
        check=True,
    )
    subprocess.run([sys.executable, "-m", "venv", str(root / "venv")], check=True)
    python = root / "venv" / "bin" / "python"
    harness = root / "venv" / "bin" / "harness"
    wheel = next(wheel_dir.glob("*.whl"))
    subprocess.run([str(python), "-m", "pip", "install", "--no-deps", str(wheel)], check=True)
    observed = subprocess.run([str(harness), "version"], check=True, text=True, capture_output=True).stdout.strip()
    if observed != expected:
        raise SystemExit(f"wheel version mismatch: expected {expected}, got {observed}")
    print(f"clean_install_ok={observed}")
PY
)
