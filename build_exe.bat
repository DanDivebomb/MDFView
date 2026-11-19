@echo off
REM ============================================================
REM  MDFView als Programmordner bauen (PyInstaller, one directory)
REM  Ergebnis: dist\MDFView\MDFView.exe
REM
REM  Per Doppelklick startet ein frisches cmd ohne aktivierte
REM  Conda-Umgebung - darum wird sie hier selbst aktiviert.
REM  Andere Umgebung? Die beiden Zeilen unten anpassen.
REM ============================================================
cd /d "%~dp0"

set "CONDA_BAT=C:\workspace\Tools\Dan_tooldev\miniconda3\condabin\conda.bat"
set "CONDA_ENV=MDFViewer"

echo.
echo === MDFView bauen ===
echo.

where python >nul 2>&1 && goto gefunden

echo Kein Python im Pfad - aktiviere %CONDA_ENV% ...
if not exist "%CONDA_BAT%" goto keinconda
call "%CONDA_BAT%" activate %CONDA_ENV%
where python >nul 2>&1 && goto gefunden
echo Umgebung %CONDA_ENV% liess sich nicht aktivieren.
goto ende

:keinconda
echo Conda nicht gefunden unter:
echo   %CONDA_BAT%
echo Entweder den Pfad oben im Skript anpassen oder das Skript aus der
echo Anaconda-Eingabeaufforderung mit aktiver Umgebung starten.
goto ende

:gefunden
python -c "import sys; print('Python:   ', sys.executable)"
REM Welches asammdf wird eingepackt? Muss das src-Verzeichnis hier sein,
REM sonst landet eine alte Installation in der .exe.
python -c "import asammdf; print('asammdf:  ', asammdf.__file__)"
python -c "import PyInstaller" 2>nul || python -m pip install pyinstaller || goto ende

echo.
echo Das dauert ein paar Minuten...
echo.

python -m PyInstaller --noconfirm --clean MDFView.spec || goto ende

echo.
echo Fertig: "%cd%\dist\MDFView\MDFView.exe"
echo Der Ordner dist\MDFView gehoert als Ganzes zusammen - zum Weitergeben
echo den kompletten Ordner kopieren, nicht nur die .exe.

:ende
echo.
pause
