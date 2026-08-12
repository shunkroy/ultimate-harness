@echo off
setlocal
set "HARNESS_ROOT=%~dp0.."
if defined PYTHONPATH (
  set "PYTHONPATH=%HARNESS_ROOT%;%PYTHONPATH%"
) else (
  set "PYTHONPATH=%HARNESS_ROOT%"
)
if defined HARNESS_PYTHON_BIN (
  "%HARNESS_PYTHON_BIN%" -m harness2 %*
) else (
  python -m harness2 %*
)
exit /b %ERRORLEVEL%
