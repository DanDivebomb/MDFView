"""Diagnose: was ist bei den Spaltenbreiten tatsaechlich gespeichert?

Aufruf im x64-Prompt mit aktivierter Umgebung, aus dem MDFView-Ordner:

    python test\\check_header_sizes.py "C:\\pfad\\zu\\deiner.dspf"

Der Pfad zur Konfiguration ist freiwillig - ohne ihn werden nur die
Qt-Einstellungen gezeigt.
"""

import json
from pathlib import Path
import sys

from PySide6 import QtCore, QtWidgets

app = QtWidgets.QApplication([])
app.setOrganizationName("py-asammdf")
app.setOrganizationDomain("py-asammdf")
app.setApplicationName("py-asammdf")

settings = QtCore.QSettings()

print("=== Qt-Einstellungen ===")
print("Ablage:", settings.fileName())
print()

key = "plot/channel_list_header_sizes"
print(f"{key} vorhanden:", settings.contains(key))
print(f"{key} =", repr(settings.value(key, "", str)))
print()

print("Alle Schluessel unter plot/ (Auszug):")
settings.beginGroup("plot")
keys = sorted(settings.allKeys())
settings.endGroup()
for name in keys[:40]:
    print("   plot/" + name)
print(f"   ... insgesamt {len(keys)} Schluessel")

if len(sys.argv) > 1:
    path = Path(sys.argv[1])
    print()
    print("=== Konfiguration:", path.name, "===")

    info = json.loads(path.read_text(encoding="utf-8"))

    windows = info.get("windows", [])
    if not windows:
        print("keine Fenster in der Konfiguration")

    for i, window in enumerate(windows):
        if window.get("type") != "Plot":
            continue

        cfg = window.get("configuration", {})
        print(f"Fenster {i}: {window.get('title')}")
        print("   channels_header_sizes :", cfg.get("channels_header_sizes"))
        print("   channels_header       :", cfg.get("channels_header"))
        print("   strip_count           :", cfg.get("strip_count"))
        print("   strip_sizes           :", cfg.get("strip_sizes"))
        print("   measurement_file      :", info.get("measurement_file"))
else:
    print()
    print("Kein .dspf angegeben - Pfad als Argument mitgeben, dann sehe ich mehr.")
