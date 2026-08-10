@echo off
REM Stop hook: refresh the cost meter after an assistant turn.
REM
REM The Windows twin of hooks/tally.sh. The hook is registered by absolute path
REM and runs with an arbitrary working directory, so `pixi run` cannot be the
REM registered command -- it has to be told where the manifest is. That is all
REM this wrapper does.
REM
REM stdin is deliberately not redirected: tally.py reads the hook payload from
REM it to learn its own session_id, which is what keeps the `last turn` row
REM correct when several Claude Code sessions run at once.
REM
REM It exits 0 unconditionally. This sits on the user's critical path, so a
REM missing environment, an unsolved lock, or a crash inside tally.py must cost
REM a number on screen and never the ability to work. When that happens the
REM panel says so itself: every row greys out and the warning row shows
REM `! stale <age>`.

cd /d "%~dp0.." || exit /b 0

REM Same PATH repair as launch_widget.cmd, for the same reason: a Claude Code
REM session started from a terminal that predates the pixi install inherits a
REM PATH without it, and this hook would then quietly do nothing after every
REM turn. A no-op when pixi is already found.
where pixi >nul 2>&1 || set "PATH=%USERPROFILE%\.pixi\bin;%PATH%"

REM --frozen: use the environment exactly as pixi.lock describes it. No solving,
REM no network, no surprise install latency on a hook that runs after every turn.
pixi run --frozen tally >nul 2>&1

exit /b 0
