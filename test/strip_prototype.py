"""
Prototyp: mehrere "Strips" (Subplots) in EINEM Fenster.

Ziel dieses Wegwerf-Prototyps:
  - mehrere PlotGraphics-Flaechen untereinander in einem Splitter
  - gemeinsame Zeitachse (x-Achse verknuepft)
  - Zeitachsen-Beschriftung nur beim untersten Strip
  - jeder Strip hat seine eigene y-Achse

Aufruf:
    python strip_prototype.py                      -> Demo-Daten
    python strip_prototype.py messung.mf4          -> erste 6 Kanaele, 3 Strips
    python strip_prototype.py messung.dat EngN AccelPedalPos1 ...
"""

import os
import sys
from datetime import datetime, timezone
from functools import partial

os.environ["QT_API"] = "pyside6"
os.environ["PYQTGRAPH_QT_LIB"] = "PySide6"

import numpy as np
import pyqtgraph
from PySide6 import QtCore, QtWidgets

from asammdf import MDF, Signal
from asammdf.gui.widgets.plot import PlotGraphics, monkey_patch_pyqtgraph


class ParentStub:
    """Minimalersatz fuer die Container-Klasse Plot.

    PlotGraphics fragt beim Zeichnen plot_parent.item_by_uuid(uuid) ab, um
    die Farbbereiche eines Signals zu holen. Die kommen normalerweise aus
    der Haekchenliste links, die es hier nicht gibt. Die Zeichenroutine
    vertraegt ein None - direkt danach steht "if not item: continue".
    """

    channel_selection = None

    def item_by_uuid(self, uuid):
        return None

    def selected_items(self):
        return []


class StripWindow(QtWidgets.QWidget):
    """Ein Fenster, das mehrere PlotGraphics-Flaechen uebereinander haelt."""

    def __init__(self, strips, mdf=None, parent=None):
        """strips: Liste von Listen -> pro Strip die Signale, die hineingehoeren."""
        super().__init__(parent)

        self.setWindowTitle("Strip-Prototyp")
        self.plots = []
        self._syncing_cursor = False

        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.splitter.setOpaqueResize(False)
        self.splitter.setHandleWidth(4)

        # PlotGraphics braucht einen Zeitnullpunkt, sonst kracht die Achse
        if mdf is not None:
            origin = mdf.start_time
        else:
            origin = datetime.now(timezone.utc)

        self._parent_stub = ParentStub()

        for signals in strips:
            plot = PlotGraphics(
                mdf=mdf,
                origin=origin,
                x_axis="time",
                allow_cursor=True,
                plot_parent=self._parent_stub,
            )
            self._add_signals(plot, signals)
            self.plots.append(plot)
            self.splitter.addWidget(plot)

        self._link_x_axes()
        self._link_cursors()
        self._only_bottom_axis()

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.splitter)

        self.resize(1100, 800)

    # ------------------------------------------------------------------
    @staticmethod
    def _add_signals(plot, signals):
        """Signale in eine Zeichenflaeche haengen."""
        channels = {}
        for sig in signals:
            uuid = getattr(sig, "uuid", None) or os.urandom(6).hex()
            sig.uuid = uuid
            channels[uuid] = sig

        plot.add_new_channels(channels)

    # ------------------------------------------------------------------
    def activate(self):
        """Signale einschalten und das Zeichnen freigeben.

        Zwei Dinge, die sonst die Container-Klasse Plot erledigt:

        1. add_new_channels setzt intern _can_paint = False und setzt es
           NICHT zurueck. Plot.add_new_channels macht das am Ende.
           Ohne das bleibt das Fenster weiss.
        2. set_signal_enable schaltet ein Signal sichtbar, beschneidet es
           auf den Sichtbereich und traegt es in die Zeitbasis ein.
           In der echten GUI loest das die Haekchenliste links aus.

        Erst nach show() aufrufen: set_signal_enable braucht die
        tatsaechliche Breite des Widgets.
        """
        for plot in self.plots:
            for sig in plot.signals:
                plot.set_signal_enable(sig.uuid, True)
            plot._can_paint = True
            plot.update()

    # ------------------------------------------------------------------
    def _link_x_axes(self):
        """Alle Strips an den ersten koppeln.

        pyqtgraph erledigt Weitergabe und Rueckkopplungsschutz selbst.
        Weil asammdf sein xrange_changed an viewbox.sigXRangeChanged haengt,
        zeichnet sich jeder Strip dabei korrekt neu.
        """
        if len(self.plots) < 2:
            return
        first = self.plots[0].plotItem.vb
        for plot in self.plots[1:]:
            plot.plotItem.vb.setXLink(first)

    def _link_cursors(self):
        """Cursor ueber alle Strips koppeln.

        Anders als bei der x-Achse gibt es dafuer in pyqtgraph nichts
        Fertiges - der Cursor ist ein eigenes Objekt je Zeichenflaeche.
        Muster uebernommen aus mdi_area.set_cursor, nur ohne Fenster.
        """
        for plot in self.plots:
            if plot.cursor1 is not None:
                plot.cursor_moved.connect(partial(self._cursor_moved, plot))

    def _cursor_moved(self, source, cursor):
        """Cursorposition auf die anderen Strips uebertragen."""
        if self._syncing_cursor:
            return

        self._syncing_cursor = True
        try:
            pos = cursor.value()
            for plot in self.plots:
                if plot is source or plot.cursor1 is None:
                    continue
                plot.cursor1.setPos(pos)
                plot.cursor_move_finished.emit(plot.cursor1)
        finally:
            self._syncing_cursor = False

    def _only_bottom_axis(self):
        """Zeitachse nur ganz unten anzeigen."""
        for plot in self.plots[:-1]:
            plot.plotItem.hideAxis("bottom")
        self.plots[-1].plotItem.showAxis("bottom")


# ----------------------------------------------------------------------
def demo_signals():
    """Drei Strips mit erfundenen Daten, falls keine Messdatei angegeben ist."""
    t = np.linspace(0, 20, 20_000)

    drehzahl = Signal(
        samples=(2000 + 1500 * np.sin(0.5 * t) + 50 * np.random.randn(t.size)),
        timestamps=t,
        name="EngN",
        unit="rpm",
    )
    druck = Signal(
        samples=(8 + 3 * np.sin(1.3 * t + 0.7)),
        timestamps=t,
        name="RailPressure",
        unit="bar",
    )
    pedal = Signal(
        samples=np.clip(50 + 50 * np.sin(0.22 * t), 0, 100),
        timestamps=t,
        name="AccelPedalPos1",
        unit="%",
    )
    lambda_ = Signal(
        samples=(1.0 + 0.08 * np.sin(2.1 * t)),
        timestamps=t,
        name="Lambda",
        unit="-",
    )

    return [[drehzahl], [druck, lambda_], [pedal]]


def signals_from_file(path, names):
    mdf = MDF(path)

    max_strips = 6

    if not names:
        # Ohne Angabe Kandidaten sammeln: nur aus Gruppen, die ueberhaupt
        # Zyklen haben, und den Zeit-Kanal der Gruppe auslassen.
        masters = getattr(mdf, "masters_db", {}) or {}
        names = []
        for i, group in enumerate(mdf.groups):
            if getattr(group.channel_group, "cycles_nr", 0) == 0:
                continue
            master_index = masters.get(i)
            for j, channel in enumerate(group.channels):
                if j == master_index or channel.name in ("time", "t"):
                    continue
                if channel.name in names:
                    continue
                names.append(channel.name)
            if len(names) >= 200:
                break

        if not names:
            raise SystemExit(
                "Keine Gruppe mit Messwerten gefunden. "
                "Gib die Kanalnamen direkt an: python strip_prototype.py <datei> Kanal1 Kanal2"
            )

    signals = []
    skipped = 0
    for name in names:
        if len(signals) >= max_strips:
            break

        try:
            sig = mdf.get(name)
        except Exception:
            # Name kommt in mehreren Gruppen vor -> erstes Vorkommen nehmen
            try:
                group, index = mdf.channels_db[name][0]
                sig = mdf.get(group=group, index=index)
            except Exception:
                skipped += 1
                continue

        if not len(sig) or sig.samples.dtype.kind in "SUV":
            skipped += 1
            continue

        signals.append(sig)

    if skipped:
        print(f"  {skipped} Kanaele uebersprungen (leer, Text oder nicht lesbar)")

    if not signals:
        raise SystemExit(
            "Keine darstellbaren Kanaele gefunden. "
            "Gib die Kanalnamen direkt an: python strip_prototype.py <datei> Kanal1 Kanal2"
        )

    # ein Signal pro Strip - wie in MDA.
    # Mehrere Signale in einem Strip teilen sich eine y-Achse, dann sieht man
    # nur eines davon vernuenftig. Genau die Frage, die das MDA-Pop-up stellt.
    strips = [[sig] for sig in signals]
    return strips, mdf


def main():
    args = sys.argv[1:]

    app = pyqtgraph.mkQApp()
    app.setStyle(QtWidgets.QStyleFactory.create("Fusion"))
    monkey_patch_pyqtgraph()

    mdf = None
    if args:
        strips, mdf = signals_from_file(args[0], args[1:])
    else:
        strips = demo_signals()

    print(f"{len(strips)} Strips:")
    for i, group in enumerate(strips, 1):
        print(f"  Strip {i}: {', '.join(sig.name for sig in group)}")

    window = StripWindow(strips, mdf=mdf)
    window.show()

    # erst wenn das Fenster steht und eine echte Breite hat
    QtCore.QTimer.singleShot(0, window.activate)

    app.exec()


if __name__ == "__main__":
    main()
