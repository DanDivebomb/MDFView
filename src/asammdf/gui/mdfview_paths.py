"""Zuletzt benutzte Ordner, getrennt nach Art des Dateidialogs.

asammdf merkt sich den Ordner nur beim Oeffnen einer Messdatei
("last_opened_path"). Alle anderen Dialoge bekommen einen leeren
Startordner, und Qt nimmt dann den zuletzt besuchten - also meist den
Messungsordner.

Hier bekommt jede Art von Dialog ihren eigenen Schluessel:
    "measurement"  Messdateien
    "config"       Anzeigekonfigurationen (.dspf, .cfg, .lab, .txt)
    "filter"       Filterlisten
    "export"       Exporte

Ablage in den Qt-Einstellungen unter paths/last/<key>, also neben den
vorhandenen Eintraegen und ohne diese anzufassen.
"""

from pathlib import Path

from PySide6 import QtCore


def start_folder(key, fallback=""):
    """Startordner fuer einen Dateidialog dieser Art."""
    folder = QtCore.QSettings().value(f"paths/last/{key}", "", str)

    # gemerkter Ordner kann geloescht oder ein Netzlaufwerk ohne
    # Verbindung sein - dann lieber den Rueckfallwert nehmen
    if folder and Path(folder).is_dir():
        return folder

    return fallback or ""


def remember_folder(key, file_name):
    """Ordner der gewaehlten Datei fuer das naechste Mal merken."""
    if not file_name:
        return

    path = Path(file_name)
    folder = path if path.is_dir() else path.parent

    QtCore.QSettings().setValue(f"paths/last/{key}", str(folder))
