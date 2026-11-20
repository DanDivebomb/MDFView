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
"""

from PySide6 import QtCore, QtWidgets


class _GeometryKeeper(QtCore.QObject):
    """Schreibt die Groesse weg, sobald der Dialog zugeht.

    Als Ereignisfilter, damit die Dialoge selbst nichts ueberschreiben
    muessen - closeEvent und reject gehen beide durch Hide.
    """

    def __init__(self, dialog, key):
        super().__init__(dialog)
        self._dialog = dialog
        self._key = key

    def eventFilter(self, obj, event):
        if obj is self._dialog and event.type() in (
            QtCore.QEvent.Type.Hide,
            QtCore.QEvent.Type.Close,
        ):
            store_geometry(self._dialog, self._key)

        return False


def store_geometry(dialog, key):
    """Groesse und Lage eines Dialogs merken."""
    if dialog.isMinimized():
        return

    QtCore.QSettings().setValue(f"dialogs/{key}/geometry", dialog.saveGeometry())
    QtCore.QSettings().setValue(f"dialogs/{key}/maximized", dialog.isMaximized())


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

    # Der Filter haengt am Dialog und stirbt mit ihm.
    dialog.installEventFilter(_GeometryKeeper(dialog, key))
