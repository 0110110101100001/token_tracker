@echo off
REM SessionStart hook: bring the cost meter up if it is not already running.
REM
REM The Windows twin of launch_widget.sh. Both are deliberately this thin: the
REM decisions -- is a panel already up, is there a display, how to detach the
REM one we start -- live in cost_meter/launch.py, which the two share.
REM
REM It exits 0 unconditionally, like the module it calls. This runs on the
REM critical path of starting a Claude Code session, so a missing environment,
REM a broken widget, or no desktop at all must cost the user a panel, never a
REM session. Nothing is written to stdout or stderr.
REM
REM --frozen: use the environment exactly as pixi.lock describes it. No solving,
REM no network, no surprise install latency at session start.

cd /d "%~dp0" || exit /b 0

REM The installer puts pixi on the *user* PATH, which a process only picks up if
REM it was started after the install. Claude Code is long-lived and inherits the
REM environment of whatever terminal launched it, so a session started from an
REM older shell sees no pixi and this hook would exit 0 having done nothing --
REM silently, since it must never break a session. Prepending the default
REM install directory covers that case; it is a no-op when pixi is already found.
REM PATH, not an absolute call: cost_meter/launch.py spawns `pixi run widget`
REM again for the panel itself and resolves it the same way, off PATH.
where pixi >nul 2>&1 || set "PATH=%USERPROFILE%\.pixi\bin;%PATH%"

pixi run --frozen launch >nul 2>&1

exit /b 0
