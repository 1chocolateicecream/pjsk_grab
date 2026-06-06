@echo off
chcp 65001 >nul
rem двойной клик по этому файлу — и качается самый свежий ивент. всё.
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py pjsk_grab.py
    goto end
)
where python >nul 2>nul
if %errorlevel%==0 (
    python pjsk_grab.py
    goto end
)

echo питончика нет :(
echo поставь его с https://www.python.org/downloads/
echo ВАЖНО!! при установке поставь галочку "Add Python to PATH", а то не заведётся

:end
echo.
pause