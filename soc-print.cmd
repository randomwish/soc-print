@echo off
rem Start SoC Print and open it in the browser. Extra arguments go to `python -m socprint`
rem (try --help). uv keeps the dependencies in .\.venv; without uv, an installed Python is used.
setlocal
set "HERE=%~dp0"
where uv >nul 2>nul
if not errorlevel 1 (
  uv run --quiet --no-dev --directory "%HERE%." python -m socprint %*
  exit /b
)
cd /d "%HERE%"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% -c "import keyring, pikepdf, PIL, paramiko" >nul 2>nul
if errorlevel 1 (
  echo soc-print needs uv ^(https://docs.astral.sh/uv/^), or: %PY% -m pip install keyring pikepdf pillow paramiko 1>&2
  exit /b 1
)
%PY% -m socprint %*
