#!/bin/sh
set -eu

REPO="shunkroy/ultimate-harness"
VERSION="2.1.0"
ASSET="harness2-${VERSION}-py3-none-any.whl"
PINNED_SHA256="d7fe4ae5d7eeb0a8b6f0dc13843934420c56f00956b9354fa14a2491eb25fcfe"
BASE_URL="${HARNESS_RELEASE_BASE_URL:-https://github.com/${REPO}/releases/download/v${VERSION}}"
INSTALL_ROOT="${HARNESS_INSTALL_ROOT:-$HOME/.local/share/harness2}"
VENV="$INSTALL_ROOT/venv"
BIN_DIR="${HARNESS_BIN_DIR:-$HOME/.local/bin}"
ORIGINAL_PATH="$PATH"
WORK=""

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

WORK="$(mktemp -d "${TMPDIR:-/tmp}/harness2-install.XXXXXX")"
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

printf 'Downloading Harness v%s from %s...\n' "$VERSION" "$REPO"
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    "$BASE_URL/$ASSET" --output "$WORK/$ASSET"
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    "$BASE_URL/SHA256SUMS" --output "$WORK/SHA256SUMS"

EXPECTED="$(sed -n "s/[[:space:]]\+$ASSET\$//p" "$WORK/SHA256SUMS")"
[ "$EXPECTED" = "$PINNED_SHA256" ] || fail "release checksum does not match the installer pin"
ACTUAL="$(sha256sum "$WORK/$ASSET" | cut -d' ' -f1)"
[ "$ACTUAL" = "$PINNED_SHA256" ] || fail "wheel checksum mismatch"

umask 077
mkdir -p "$INSTALL_ROOT" "$BIN_DIR"
VENV_USABLE=0
if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
    VENV_USABLE=1
else
    if [ -e "$VENV" ] && [ ! -d "$VENV/site-packages" ]; then
        python3 - "$VENV" <<'PY'
import shutil
import sys
shutil.rmtree(sys.argv[1], ignore_errors=True)
PY
    fi
    if [ ! -d "$VENV/site-packages" ]; then
        if python3 -m venv "$VENV" && "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
            VENV_USABLE=1
        else
            python3 - "$VENV" <<'PY'
import shutil
import sys
shutil.rmtree(sys.argv[1], ignore_errors=True)
PY
        fi
    fi
    if [ "$VENV_USABLE" = "0" ]; then
        python3 - "$VENV" <<'PY'
import shutil
import sys
from pathlib import Path
root = Path(sys.argv[1])
if root.exists() and not (root / "site-packages").is_dir():
    shutil.rmtree(root, ignore_errors=True)
PY
        python3 -m pip --disable-pip-version-check --no-input install \
            --root-user-action=ignore --no-deps --target "$VENV/site-packages" "$WORK/$ASSET"
        mkdir -p "$VENV/bin"
        cat >"$VENV/bin/harness" <<EOF
#!/bin/sh
PYTHONPATH='$VENV/site-packages' exec python3 -m harness2.cli "\$@"
EOF
        chmod 0755 "$VENV/bin/harness"
    fi
fi
if [ "$VENV_USABLE" = "1" ]; then
    "$VENV/bin/python" -m pip --disable-pip-version-check install --no-deps --upgrade "$WORK/$ASSET"
else
    python3 -m pip --disable-pip-version-check --no-input install \
        --root-user-action=ignore --no-deps --upgrade --target "$VENV/site-packages" "$WORK/$ASSET"
    mkdir -p "$VENV/bin"
    cat >"$VENV/bin/harness" <<EOF
#!/bin/sh
PYTHONPATH='$VENV/site-packages' exec python3 -m harness2.cli "\$@"
EOF
    chmod 0755 "$VENV/bin/harness"
fi
ln -sfn "$VENV/bin/harness" "$BIN_DIR/harness"
[ "$("$BIN_DIR/harness" version)" = "$VERSION" ] || fail "installed version verification failed"
"$BIN_DIR/harness" integrity pin >/dev/null

if [ "${HARNESS_START_SERVICE:-1}" = "1" ]; then
    "$BIN_DIR/harness" svc restart --interval "${HARNESS_SERVICE_INTERVAL:-30}"
    sleep 2
    "$BIN_DIR/harness" svc status --json || fail "always-active service did not become healthy"
fi

printf '\nHarness v%s installed successfully.\n' "$VERSION"
printf 'Binary: %s\n' "$BIN_DIR/harness"
case ":$ORIGINAL_PATH:" in
    *":$BIN_DIR:"*) ;;
    *) printf 'Add this to your shell profile: export PATH="%s:$PATH"\n' "$BIN_DIR" ;;
esac
printf 'Verify: %s doctor --json\n' "$BIN_DIR/harness"
