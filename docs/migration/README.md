# Phone-to-PC migration and recovery

Checkpoint 3C preserves one ecosystem: Linux becomes the primary development
host while Android/Termux remains a real Harness runtime/edge node. No cloud
service is required.

## Artifacts

The final builder produces:

- `harness-migration-3c-<sha>/`: verified package directory;
- `harness-migration-3c-<sha>.tar.gz`: portable package archive;
- `.secret-transfer.tar.enc`: separately transferred credentials/keys;
- `.emergency-source-state.tar.enc`: encrypted tracked source, Git bundle,
  persistent state and secrets;
- SHA-256 sidecars for each external artifact;
- an external JSON build attestation that records the final artifact digests and
  clearly labels CI/local results as operator-supplied attestations;
- one mode-0600 encryption key outside the package/artifact directory.

All encrypted archives use AES-256-CTR/PBKDF2 plus encrypt-then-MAC HMAC-SHA256.
The package excludes every value in the classified current secret files. Sealed
Git ancestry is checked for exact matches to those values and strong credential
signatures. This cannot prove the absence of an unknown/rotated credential that
matches neither set; repository review and provider-side secret scanning remain
required before sealing.

## Preferred PC restore

```sh
cd /transfer
sha256sum -c harness-migration-3c-<sha>.tar.gz.sha256
# Compare that digest with an independently transmitted/signed record before extraction.

git clone https://github.com/shunkroy/ultimate-harness.git
cd ultimate-harness
git checkout <sealed-3c-sha>
python3 -m venv .venv
.venv/bin/python -m pip install --no-deps .

tar -xzf /transfer/harness-migration-3c-<sha>.tar.gz -C /transfer
/transfer/harness-migration-3c-<sha>/verification/verify-package.sh \
  /transfer/harness-migration-3c-<sha> /separate/path/migration.key

/transfer/harness-migration-3c-<sha>/verification/restore_state.py \
  --archive /transfer/harness-migration-3c-<sha>/private-state.tar.enc \
  --secrets-archive /separate/path/harness-migration-3c-<sha>.secret-transfer.tar.enc \
  --key-file /separate/path/migration.key \
  --target "$HOME/.local/state/harness2"

HARNESS2_HOME="$HOME/.local/state/harness2" \
  /transfer/harness-migration-3c-<sha>/verification/verify-after-clone.sh \
  /transfer/harness-migration-3c-<sha> "$PWD" /separate/path/migration.key
```

The checksum file transported beside an archive detects accidental corruption;
it is not an independent authenticity anchor. Internal `CHECKSUMS.sha256` proves
package consistency only. The builder does not query GitHub: its CI URL and local
test summary remain explicitly labeled operator attestations.

Re-enter credentials instead of restoring `secrets.dpapi` when moving to a
different Windows account/machine. Never paste secret values into reports or Git.

## Emergency PC restore

1. Verify the emergency archive SHA-256 sidecar.
2. Keep the encryption key separate and mode 0600.
3. Authenticate/decrypt the emergency archive using the tracked restore crypto
   parameters. This full-history path is a documented manual fallback; the
   normal encrypted-state restore is the mechanically tested route.
4. Verify `repository.bundle` with `git bundle verify`.
5. Clone the bundle, checkout the sealed SHA, restore `state/` and `secrets/` to
   a fresh state root, and run the preferred verification commands.

The normal package's `repository.bundle` can also be cloned directly:

```sh
git clone /transfer/harness-migration-3c-<sha>/repository.bundle ultimate-harness
cd ultimate-harness
git checkout <sealed-3c-sha>
```

## Fresh phone reproduction

```sh
pkg install git python openssl curl coreutils
git clone https://github.com/shunkroy/ultimate-harness.git
cd ultimate-harness
git checkout <sealed-3c-sha>
python -m venv "$HOME/.local/share/harness2/venv"
"$HOME/.local/share/harness2/venv/bin/python" -m pip install --no-deps .
mkdir -p "$HOME/.local/bin"
ln -s "$HOME/.local/share/harness2/venv/bin/harness" "$HOME/.local/bin/harness"
```

Restore state into a fresh `~/.harness2` with `restore_state.py`, restore secrets
separately, then run:

```sh
harness integrity verify
harness status --json
harness svc up --interval 30
harness svc status --json
python -m unittest discover -s tests
```

Optional provider CLIs may then be installed/configured. Their absence is a
supported, tested state.

## Rollback to the sealed phone state

1. Stop only the Harness maintenance service: `harness svc down`.
2. Preserve the failed/new state root; do not overwrite it.
3. Verify and decrypt the emergency archive with the separate key.
4. Restore the sealed source from its Git bundle and the sealed state/secrets to
   a fresh directory.
5. Point `HARNESS2_HOME` at the restored directory.
6. Run SQLite integrity, audit, package, and CLI verification.
7. Restart with `harness svc up --interval 30` and confirm a fresh heartbeat.

Prime is independently supervised and must not be killed merely to restore the
Harness state. No history rewrite, force-push or destructive down-migration is
part of rollback.
