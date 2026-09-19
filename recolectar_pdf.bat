@echo off
REM ===========================================================
REM  KIWI / RECOLECTOR DE PDF — Lanzador por consola
REM  (Para usar la interfaz visual amigable, ejecuta KIWI.bat)
REM ===========================================================

REM --------------------- CONFIGURACION -----------------------
set ORIGEN=C:\ruta\a\revisar
set DESTINO=C:\ruta\de\salida
REM Deja SIMULACRO en 1 para la primera pasada (no copia nada).
REM Cambialo a 0 solo cuando el informe te cuadre.
set SIMULACRO=1
REM -----------------------------------------------------------

setlocal
cd /d "%~dp0"

if "%SIMULACRO%"=="1" (
    set MODO=--simulacro
) else (
    set MODO=
)

python kiwi_app.py "%ORIGEN%" "%DESTINO%" %MODO% --validar-cabecera
if errorlevel 1 (
    echo.
    echo  Ha fallado la ejecucion. Revisa el mensaje de arriba.
)

echo.
pause
