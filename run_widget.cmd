@echo off
REM Start the panel from outside the pixi environment.
REM
REM The Windows twin of run_widget.sh. This is the entry point for anything not
REM already inside pixi: the SessionStart hook by way of cost_meter/launch.py,
REM a Startup-folder entry, or a shell. It adds nothing but the environment.
REM
REM --frozen installs nothing and never touches the network: the environment is
REM used exactly as pixi.lock describes it, or the run fails. Re-run
REM `pixi install` after editing pixi.toml.

cd /d "%~dp0" || exit /b 1

pixi run --frozen widget %*
