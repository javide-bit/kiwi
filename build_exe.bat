@echo off
REM ===========================================================
REM  KIWI - Compilacion de los ejecutables con PyInstaller
REM    dist\KIWI.exe      -> interfaz grafica (sin consola)
REM    dist\KIWI-cli.exe  -> linea de comandos (con consola)
REM ===========================================================

setlocal
cd /d "%~dp0"

python -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo Instalando PyInstaller...
    python -m pip install pyinstaller || goto :error
)

echo.
echo [1/2] Compilando la interfaz grafica KIWI.exe ...
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name KIWI ^
    --icon "%~dp0assets\kiwi_logo.ico" ^
    --add-data "%~dp0assets;assets" ^
    --distpath dist --workpath build --specpath build ^
    kiwi_app.py || goto :error

echo.
echo [2/2] Compilando la version de consola KIWI-cli.exe ...
python -m PyInstaller --noconfirm --onefile --console ^
    --name KIWI-cli ^
    --icon "%~dp0assets\kiwi_logo.ico" ^
    --add-data "%~dp0assets;assets" ^
    --distpath dist --workpath build --specpath build ^
    kiwi_app.py || goto :error

echo.
echo ===========================================================
echo  LISTO. Ejecutables generados en la carpeta dist\
echo ===========================================================
dir /b dist
pause
exit /b 0

:error
echo.
echo ERROR: la compilacion ha fallado. Revisa los mensajes anteriores.
pause
exit /b 1
