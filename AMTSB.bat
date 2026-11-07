@echo off
setlocal
rem AMTSB - Automatic Movie Translator SuBtitles
rem Pierwsze uruchomienie instaluje biblioteki do %USERPROFILE%\.amt (wspolne z AMT).
set "AMT_DIR=%AMT_HOME%"
if "%AMT_DIR%"=="" set "AMT_DIR=%USERPROFILE%\.amt"
set "PY=%AMT_DIR%\venv\Scripts\python.exe"
set "PYW=%AMT_DIR%\venv\Scripts\pythonw.exe"
set "MODS=['faster_whisper','sentencepiece','pyaudiowpatch','mss','customtkinter','PIL','winrt.windows.media.ocr','winrt.windows.graphics.imaging','winrt.windows.storage.streams','winrt.windows.globalization']"

if exist "%PY%" goto check
echo Pierwsze uruchomienie - przygotowuje srodowisko w %AMT_DIR% ...
if not exist "%AMT_DIR%" mkdir "%AMT_DIR%"
py -3.11 -m venv "%AMT_DIR%\venv" 2>nul || py -3.12 -m venv "%AMT_DIR%\venv" 2>nul || python -m venv "%AMT_DIR%\venv"
if not exist "%PY%" (
  echo.
  echo Nie znaleziono Pythona. Zainstaluj Python 3.11 lub 3.12 ze strony https://www.python.org
  echo ^(zaznacz "Add python.exe to PATH"^) i uruchom ten plik ponownie.
  pause
  exit /b 1
)

:check
"%PY%" -c "import importlib.util as u,sys; sys.exit(any(u.find_spec(m) is None for m in %MODS%))" 2>nul
if not errorlevel 1 goto run
echo Instaluje biblioteki (kilka minut, tylko raz)...
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
  echo.
  echo Instalacja nie powiodla sie - sprawdz polaczenie z internetem i sprobuj ponownie.
  pause
  exit /b 1
)

:run
start "" "%PYW%" "%~dp0amtsb.py" %*
