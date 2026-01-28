@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  where py >nul 2>nul
  if %errorlevel%==0 (
    py -3 -m venv .venv
  ) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
      python -m venv .venv
    ) else (
      echo Python not found. Please install Python 3 and add it to PATH.
      exit /b 1
    )
  )
)

call ".venv\Scripts\activate.bat"

python -m pip install -r requirements.txt
python -m playwright install

python watch.py %*
