"""Fenstergroessen von Dialogen behalten.

Mehrere Dialoge von asammdf gehen mit showMaximized() auf, also immer
bildschirmfuellend - unabhaengig davon, wie gross man sie zuletzt
gezogen hat. Hier merkt sich jeder Dialog seine eigene Groesse und
Lage.

Ablage in den Qt-Einstellungen unter dialogs/<key>/geometry, neben den
vorhandenen Eintraegen und ohne diese anzufassen.

Benutzung am Ende von __init__ eines Dialogs:

    remember_geometry(self, "define_channel", (1200, 800))

Der zweite Wert ist die Groesse beim allerersten Oeffnen.

Gemerkt wird beim Schliessen ueber das Signal finished - QDialog
schickt es bei OK, bei Abbrechen und beim Kreuz. Ein Ereignisfilter
stand hier vorher und war ein Fehler: ein eigener QObject bekommt von
PySide seine Python-Seite neu gebaut, wenn nur noch C++ ihn haelt -
ohne __init__, also ohne die gemerkten Felder. Der Filter lief dann in
einen AttributeError, der Fehlerdialog loeste das naechste Ereignis
aus, und das Programm war in einer Schleife aus Fehlerfenstern
gefangen.
"""

from PySide6 import QtCore, QtWidgets


def store_geometry(dialog, key):
    """Groesse und Lage eines Dialogs merken."""
    try:
        if dialog.isMinimized():
            return

        settings = QtCore.QSettings()
        settings.setValue(f"dialogs/{key}/geometry", dialog.saveGeometry())
        settings.setValue(f"dialogs/{key}/maximized", dialog.isMaximized())
    except RuntimeError:
        # Dialog ist schon abgeraeumt - dann gibt es nichts zu merken.
        pass


def remember_geometry(dialog, key, default_size=None):
    """Gemerkte Groesse setzen und beim Schliessen die neue merken.

    Beim ersten Mal gilt default_size, aber hoechstens so gross, dass
    der Dialog auf den Bildschirm passt.
    """
    settings = QtCore.QSettings()
    gespeichert = settings.value(f"dialogs/{key}/geometry")

    wiederhergestellt = False
    if isinstance(gespeichert, QtCore.QByteArray) and not gespeichert.isEmpty():
        wiederhergestellt = dialog.restoreGeometry(gespeichert)

    if not wiederhergestellt and default_size:
        breite, hoehe = default_size

        screen = dialog.screen() or QtWidgets.QApplication.primaryScreen()
        if screen is not None:
            frei = screen.availableGeometry()
            breite = min(breite, int(frei.width() * 0.9))
            hoehe = min(hoehe, int(frei.height() * 0.9))

        dialog.resize(breite, hoehe)

    if settings.value(f"dialogs/{key}/maximized", False, type=bool):
        dialog.showMaximized()

    # finished kommt von QDialog. Alles andere (ein Fenster, das kein
    # Dialog ist) behaelt seine Groesse eben nicht - besser als ein
    # Filter, der im Fehlerfall das Programm lahmlegt.
    signal = getattr(dialog, "finished", None)
    if signal is not None:
        signal.connect(lambda _ergebnis=0: store_geometry(dialog, key))
