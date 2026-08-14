# Harness v2 — portable unified agent harness

> Development branch: `3.0.0.dev1`. The latest stable public installer remains
> v2.1.1 while the v3 task/event/skill foundations accumulate CI evidence.

Harness Core is the provider-independent control plane. OpenCode, Prime Agent,
OpenCode Zen, Hermes, and local runtimes are replaceable capability providers.
The local llama.cpp adapter is installed but disabled by default.

The deterministic Python substrate owns policy, credential boundaries, truthful
health, circuit breakers and a hash-chained metadata-only audit ledger.

## One-command installation

Termux or Linux:

```sh
installer="$(mktemp "${TMPDIR:-/tmp}/install-harness2.XXXXXX")" && \
trap 'rm -f "$installer"' EXIT HUP INT TERM && \
curl --proto '=https' --tlsv1.2 -fsSLo "$installer" \
  https://raw.githubusercontent.com/shunkroy/ultimate-harness/v2.1.1/install.sh && \
sh "$installer"
```

The version-pinned installer downloads the GitHub Release wheel and
`SHA256SUMS`, verifies both against its embedded SHA-256 pin before execution,
installs it in an isolated venv under
`~/.local/share/harness2`, pins installed-source integrity, and starts the
bounded always-active runtime. Native Termux prerequisites:
`pkg install python openssl curl coreutils`. Debian/Ubuntu prerequisites:
`sudo apt install python3 python3-pip openssl curl coreutils`; `python3-venv` is
recommended, with a private target-directory fallback when unavailable.

Supported terminals/platforms:

- Windows 10/11: PowerShell, Windows Terminal, CMD
- macOS: Terminal, iTerm2, any POSIX shell
- Linux: bash/zsh/fish through the installed console script
- Android: native Termux and Ubuntu/Debian PRoot

## Portable installation

Python 3.11+ is required. Install from a checkout:

```sh
python -m pip install .
# or isolated:
pipx install .
```

Install a release wheel into an isolated environment:

```sh
python3 -m venv ~/.local/share/harness2/venv
~/.local/share/harness2/venv/bin/python -m pip install /path/to/harness2-2.1.1-py3-none-any.whl
mkdir -p ~/.local/bin
ln -sf ~/.local/share/harness2/venv/bin/harness ~/.local/bin/harness
```

On Termux use `pkg install python openssl git` first. On Debian/Ubuntu use
`sudo apt install python3 python3-venv openssl git`. Ensure `~/.local/bin` is on
`PATH`, then run `harness integrity pin`, `harness svc up`, and
`harness doctor`. Provider CLIs such as OpenCode and Prime are optional; the
Harness Core and executable-context runtime install without them.

This creates the shell-neutral `harness` console command on Windows, Linux and
macOS. Without installation, use:

```sh
python -m harness2 status
```

Windows checkout launchers are also provided:

```powershell
.\bin\harness.ps1 status
```

```cmd
bin\harness.cmd status
```

POSIX checkout launcher:

```sh
./bin/harness status
```

## Engine discovery

Harness discovers binaries from `PATH` and platform-native locations. Override
when needed:

`HARNESS_OPENCODE_BIN`, `HARNESS_PRIME_BIN`, `HARNESS_PRIME_REPO`,
`HARNESS_HERMES_BIN`, `HARNESS_NODE_BIN`, `HARNESS_PYTHON_BIN`,
`HARNESS_OPENSSL_BIN`, `HARNESS2_HOME`, `HARNESS_DEFAULT_MODEL`,
`HARNESS_DEFAULT_AGENT`. Optional protected roots use the path-separated
`HARNESS_GUARDED_ROOTS`; external guardian diagnostics require both
`HARNESS_GUARDIAN_STATE` and `HARNESS_GUARDIAN_PROCESS`. Extra paths for
`doctor --fix-modes` can be supplied through path-separated
`HARNESS_HARDEN_PATHS`.

State paths follow OS conventions: `%LOCALAPPDATA%\Harness2` on Windows,
`~/Library/Application Support/Harness2` on macOS, XDG state on Linux, and
`~/.harness2` on Termux/PRoot or where an existing installation is detected.

Run `harness status`, `harness doctor`, `harness policy "task"`, or
`harness run "task"` after installation.

Always-active mode is desired by default. It becomes **observed active** only
when a real detached service process has a fresh private heartbeat:

```sh
harness svc up
harness svc status
```

The bounded service supervises configured durable infrastructure and processes
encrypted run jobs plus explicit context-compilation jobs even when no chat
message is present. Set `HARNESS_ALWAYS_ACTIVE=false` to disable the desired
default; configuration alone is never reported as executed background work.

## Executable context v1

The first Context-as-Program slice compiles structured UTF-8 text into a
content-addressed package with ContextIR, source provenance, operation
contracts, validation, and deterministic execution:

```sh
harness context compile source.txt --name Example
harness context inspect ~/.harness2/contexts/<context-id>
harness context execute ~/.harness2/contexts/<context-id> query \
  --inputs '{"topic":"example"}'
```

Initial operations are `query`, allowlisted `transform`, and an optional
source-supported `generate` evidence brief. No source text can grant itself
shell, network, credential, or filesystem-write authority.

Pin the installed Harness module and launcher after an intentional installation
or upgrade. Prime wrapper/bundle pins are included only when Prime source is
installed:

```sh
harness integrity pin
harness integrity verify
```

`harness doctor` fails closed when this private integrity manifest is missing,
incomplete, or disagrees with any pinned artifact.

On Windows, foreground commands and all engine adapters are portable. The
long-running service should be launched with Task Scheduler/NSSM/WinSW using
`harness supervise`; `harness svc` remains the hardened POSIX/Termux helper.
