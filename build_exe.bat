@echo off
REM ============================================================
REM  MDFView als Programmordner bauen (PyInstaller, one directory)
REM  Ergebnis: dist\MDFView\MDFView.exe
REM  Vorher die Umgebung aktivieren, in der MDFView normal laeuft.
REM ============================================================
cd /d "%~dp0"

echo.
echo === MDFView bauen ===
echo.

python -c "import sys; print('Python:   ', sys.executable)" || goto ende
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
