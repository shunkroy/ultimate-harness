$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $env:PYTHONPATH) { $env:PYTHONPATH = $root }
else { $env:PYTHONPATH = "$root$([IO.Path]::PathSeparator)$env:PYTHONPATH" }
$python = if ($env:HARNESS_PYTHON_BIN) { $env:HARNESS_PYTHON_BIN } else { "python" }
& $python -m harness2 @args
exit $LASTEXITCODE
