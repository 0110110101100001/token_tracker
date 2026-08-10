@echo off
REM Register this tool's hooks in ~/.claude/settings.json.
REM
REM   install.cmd                 register the Stop and SessionStart hooks
REM   install.cmd --autostart     also bring the panel up on login
REM   install.cmd --uninstall     remove both hooks and the autostart entry
REM   install.cmd --help          every flag
REM
REM The Windows twin of install.sh. The work is in cost_meter/install.py, which
REM reuses this project's own atomic JSON write rather than reimplementing it.
REM Unlike the hooks it installs, this script is run by hand and is allowed to
REM fail loudly.

cd /d "%~dp0" || exit /b 1

pixi run --frozen python -m cost_meter.install %*
