@echo off
setlocal enabledelayedexpansion
title flacveritas - detector de FLAC falsos

rem == carpeta a analizar: primer argumento, o la carpeta actual ==
set "CARPETA=%~1"
if "%CARPETA%"=="" set "CARPETA=%cd%"
if not exist "%CARPETA%" (
    echo la ruta "%CARPETA%" no existe
    pause
    exit /b 2
)

rem == el script debe estar junto a este BAT ==
set "SCRIPT=%~dp0flacveritas.py"
if not exist "%SCRIPT%" (
    echo no encuentro flacveritas.py junto a este BAT: %~dp0
    pause
    exit /b 2
)

rem == comprobar requisitos ==
python --version >nul 2>&1
if errorlevel 1 (
    echo falta Python o no esta en el PATH
    echo instalalo con:  winget install Python.Python.3.12
    pause
    exit /b 2
)

ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo falta ffmpeg o no esta en el PATH
    echo instalalo con:  winget install Gyan.FFmpeg
    pause
    exit /b 2
)

python -c "import numpy, scipy" >nul 2>&1
if errorlevel 1 (
    echo faltan numpy y scipy, los instalo ahora con pip...
    python -m pip install numpy scipy
    if errorlevel 1 (
        echo no se pudieron instalar, ejecuta a mano:  pip install numpy scipy
        pause
        exit /b 2
    )
)

rem == si hay un aucdtect.exe junto al BAT, se usa como segunda opinion ==
set "OPC_AUC="
if exist "%~dp0aucdtect.exe" set OPC_AUC=--aucdtect "%~dp0aucdtect.exe"

rem == CSV con marca de tiempo, guardado junto al BAT ==
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "FECHA=%%i"
set "CSV=%~dp0resultados_%FECHA%.csv"

echo.
echo analizando: %CARPETA%
if defined OPC_AUC echo con segunda opinion de auCDtect
echo.

rem == argumentos extra del BAT se pasan tal cual: --solo-malos, --detalle, --hilos N... ==
python "%SCRIPT%" "%CARPETA%" --csv "%CSV%" %OPC_AUC% %2 %3 %4 %5 %6 %7 %8 %9
set "SALIDA=%errorlevel%"

echo.
if "%SALIDA%"=="0" (
    echo todo limpio: sin lossy ni sospechosos
) else if "%SALIDA%"=="1" (
    echo hay archivos LOSSY o SOSPECHOSOS: revisa el CSV
) else (
    echo el analisis no llego a completarse: revisa el mensaje de arriba
)
if exist "%CSV%" echo csv: %CSV%
echo.
pause
endlocal
