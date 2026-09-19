@echo off
REM ===========================================================
REM  KIWI — Recolector Inteligente de PDFs
REM  Lanzador con interfaz visual amigable
REM ===========================================================

setlocal
cd /d "%~dp0"

REM 1. Intenta con pythonw (para abrir directamente la ventana limpia sin consola)
where pythonw >nul 2>nul
if %errorlevel% equ 0 (
    start "" pythonw kiwi_app.py
    exit /b 0
)

REM 2. Intenta con python
where python >nul 2>nul
if %errorlevel% equ 0 (
    start "" python kiwi_app.py
    exit /b 0
)

REM 3. Intenta con el lanzador estándar 'py'
where py >nul 2>nul
if %errorlevel% equ 0 (
    start "" py -3 kiwi_app.py
    exit /b 0
)

echo.
echo ===========================================================
echo  ERROR: No se encontro Python en este equipo.
echo  Por favor instala Python o asegurate de marcar
echo  "Add Python to PATH" durante la instalacion.
echo ===========================================================
echo.
pause
