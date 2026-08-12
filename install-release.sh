#!/bin/sh
set -eu

REPO="${HARNESS_GITHUB_REPO:-shunkroy/ultimate-harness}"
VERSION="${HARNESS_VERSION:-2.1.0}"
ASSET="harness2-${VERSION}-py3-none-any.whl"
BASE_URL="https://github.com/${REPO}/releases/download/v${VERSION}"
INSTALL_ROOT="${HARNESS_INSTALL_ROOT:-$HOME/.local/share/harness2}"
VENV="$INSTALL_ROOT/venv"
BIN_DIR="${HARNESS_BIN_DIR:-$HOME/.local/bin}"
TMP_ROOT="${TMPDIR:-/tmp}"
WORK="$(mktemp -d "$TMP_ROOT/harness2-install.XXXXXX")"

cleanup() {
    if [ -n "${WORK:-}" ] && [ -d "$WORK" ]; then
        python3 - "$WORK" <<'PY'
import shutil
import sys
shutil.rmtree(sys.argv[1], ignore_errors=True)
PY
    fi
}
trap cleanup EXIT HUP INT TERM

fail() {
    printf 'harness2 installer: %s\n' "$*" >&2
    exit 1
}

command -v python3 >/dev/null 2>&1 || fail "python3 >= 3.11 is required"
command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is required"

python3 - <<'PY' || fail "python3 >= 3.11 is required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY

printf 'Downloading Harness v%s from %s...\n' "$VERSION" "$REPO"
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    "$BASE_URL/$ASSET" --output "$WORK/$ASSET"
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    "$BASE_URL/SHA256SUMS" --output "$WORK/SHA256SUMS"

EXPECTED="$(sed -n "s/[[:space:]]\+$ASSET\$//p" "$WORK/SHA256SUMS")"
[ -n "$EXPECTED" ] || fail "release checksum entry is missing"
ACTUAL="$(sha256sum "$WORK/$ASSET" | cut -d' ' -f1)"
[ "$ACTUAL" = "$EXPECTED" ] || fail "wheel checksum mismatch"

mkdir -p "$INSTALL_ROOT" "$BIN_DIR"
if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv "$VENV" || fail "venv creation failed; install python3-venv on Debian/Ubuntu"
fi
"$VENV/bin/python" -m pip install --no-deps --upgrade "$WORK/$ASSET"
ln -sf "$VENV/bin/harness" "$BIN_DIR/harness"

PATH="$BIN_DIR:$PATH"
export PATH
"$BIN_DIR/harness" integrity pin >/dev/null

if [ "${HARNESS_START_SERVICE:-1}" = "1" ]; then
    "$BIN_DIR/harness" svc restart --interval "${HARNESS_SERVICE_INTERVAL:-30}"
    sleep 2
    "$BIN_DIR/harness" svc status --json || fail "always-active service did not become healthy"
fi

printf '\nHarness v%s installed successfully.\n' "$VERSION"
printf 'Binary: %s\n' "$BIN_DIR/harness"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) printf 'Add this to your shell profile: export PATH="%s:$PATH"\n' "$BIN_DIR" ;;
esac
printf 'Verify: harness doctor --json\n'
