#!/bin/sh
set -eu
umask 077

PACKAGE=${1:-.}
KEY_FILE=${2:?usage: verify-package.sh PACKAGE KEY_FILE}

python3 - "$PACKAGE" <<'PY'
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import tempfile

raw_root = pathlib.Path(sys.argv[1])
if raw_root.is_symlink():
    raise SystemExit("package root must not be a symlink")
root = raw_root.resolve()
manifest = root / "MANIFEST.json"
checksums = root / "CHECKSUMS.sha256"
source_file = root / "SOURCE.txt"
bundle = root / "repository.bundle"
if (
    not root.is_dir() or not manifest.is_file() or manifest.is_symlink()
    or not checksums.is_file() or checksums.is_symlink()
    or not source_file.is_file() or source_file.is_symlink()
    or not bundle.is_file() or bundle.is_symlink()
):
    raise SystemExit("invalid migration package")
data = json.loads(manifest.read_text(encoding="utf-8"))
if data.get("schema") != "harness2.migration-package/v1":
    raise SystemExit("unsupported migration manifest")
source = {}
for line in source_file.read_text(encoding="utf-8").splitlines():
    key, separator, value = line.partition("=")
    if not separator or not key or key in source:
        raise SystemExit("malformed SOURCE.txt")
    source[key] = value
manifest_source = data.get("source")
if not isinstance(manifest_source, dict):
    raise SystemExit("manifest source metadata is invalid")
sealed = manifest_source.get("sealed_commit_sha")
baseline = manifest_source.get("baseline_commit_sha")
if not isinstance(sealed, str) or not re.fullmatch(r"[0-9a-f]{40}", sealed):
    raise SystemExit("sealed commit is invalid")
if source.get("commit") != sealed or source.get("repository") != manifest_source.get("repository"):
    raise SystemExit("SOURCE.txt does not match the manifest")
if baseline is not None and (not isinstance(baseline, str) or not re.fullmatch(r"[0-9a-f]{40}", baseline)):
    raise SystemExit("baseline commit is invalid")
def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()
seen = set()
for line in checksums.read_text(encoding="utf-8").splitlines():
    expected, separator, relative = line.partition("  ")
    pure = pathlib.PurePosixPath(relative)
    if (
        not separator or not re.fullmatch(r"[0-9a-f]{64}", expected)
        or not relative or "\\" in relative or not pure.parts or pure.is_absolute()
        or ".." in pure.parts or relative in seen
    ):
        raise SystemExit("malformed checksum record")
    path = root.joinpath(*pure.parts)
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"missing or unsafe package file: {relative}")
    if digest(path) != expected:
        raise SystemExit(f"checksum mismatch: {relative}")
    seen.add(relative)
actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and p.name != "CHECKSUMS.sha256"}
if seen != actual:
    raise SystemExit("checksum inventory does not exactly match package files")
with tempfile.TemporaryDirectory(prefix="harness-bundle-verify-") as temp_name:
    bare = pathlib.Path(temp_name) / "repository.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "bundle", "verify", str(bundle)], cwd=bare, check=True, stdout=subprocess.DEVNULL)
    heads = subprocess.run(
        ["git", "bundle", "list-heads", str(bundle)], cwd=bare,
        check=True, text=True, capture_output=True,
    ).stdout.splitlines()
    if heads != [f"{sealed} HEAD"]:
        raise SystemExit("bundle heads do not match sealed manifest commit")
    subprocess.run(
        ["git", "fetch", str(bundle), "HEAD:refs/heads/sealed"], cwd=bare,
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    subprocess.run(["git", "cat-file", "-e", f"{sealed}^{{commit}}"], cwd=bare, check=True)
    if baseline is not None:
        subprocess.run(["git", "cat-file", "-e", f"{baseline}^{{commit}}"], cwd=bare, check=True)
        subprocess.run(["git", "merge-base", "--is-ancestor", baseline, sealed], cwd=bare, check=True)
print(f"checksums_ok={len(seen)}")
print(f"bundle_commit_ok={sealed}")
PY

python3 "$PACKAGE/verification/restore_state.py" \
    --archive "$PACKAGE/private-state.tar.enc" --key-file "$KEY_FILE" --verify-only

echo "package_ok=1"
