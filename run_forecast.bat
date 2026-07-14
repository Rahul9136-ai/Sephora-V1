@echo off
REM ============================================================
REM  Sephora contact-volume forecaster - auto-retrain runner
REM  Re-ingests Ss.xlsx, re-normalizes outliers, retrains the
REM  best model, and regenerates forecasts in .\outputs.
REM  Schedule this .bat (Task Scheduler) to retrain automatically.
REM ============================================================
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

REM Optional: pass a different export path or horizon, e.g.
REM   run_forecast.bat "D:\path\Ss.xlsx" 45
set XLSX=%~1
if "%XLSX%"=="" set XLSX=C:\Users\lenovo\Desktop\Ss.xlsx
set HORIZON=%~2
if "%HORIZON%"=="" set HORIZON=30

echo [%date% %time%] retraining on "%XLSX%" (horizon %HORIZON%) >> outputs\run.log
python forecast_pipeline.py --xlsx "%XLSX%" --horizon %HORIZON% >> outputs\run.log 2>&1
echo [%date% %time%] done (exit %errorlevel%) >> outputs\run.log
endlocal
