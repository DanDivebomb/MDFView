"""Numerische Grossanzeige - ein Signal je Zeile, nur der Wert am Cursor.

Das Gegenstueck zum numerischen Fenster in vSignalyzer: keine Tabelle
mit Spalten, sondern je Signal eine Zeile mit dem Namen klein daneben
und dem Wert so gross, wie die Zeile es hergibt. Vom anderen Ende des
Pruefstands ablesbar.

Gezeigt wird immer der Wert an der Stelle des Cursors. Gibt es zwei
Cursor, also einen Bereich, zaehlt der erste.
"""

import numpy as np
from pyqtgraph import functions as fn
from PySide6 import QtCore, QtGui, QtWidgets

from ..serde import extract_mime_names
from ..utils import copy_ranges, value_as_str
from .tree import CHANNEL_PRECISION, set_channel_precision

MIME = "application/octet-stream-asammdf"

# Breite des Greifbereichs an der Trennlinie, in Bildpunkten
WIDTH_HANDLE = 4


class ScalarRow(QtWidgets.QFrame):
    """Eine Zeile: Name klein links, Wert gross rechts."""

    def __init__(self, signal, window, parent=None):
        super().__init__(parent)

        self.signal = signal
        self.window_ = window
        self.text = "n.a."

        # Schmalste Zeile: so hoch wie der Name. Erst darunter wird
        # gerollt, vorher schrumpft nur die Wertschrift.
        self.setMinimumHeight(window.minimum_row_height())
        self.setFrameShape(QtWidgets.QFrame.Shape.Box)
        self.setFrameShadow(QtWidgets.QFrame.Shadow.Plain)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )

        # fuer den Doppelpfeil am rechten Rand der Namensspalte
        self.setMouseTracking(True)
        self._width_drag = False
        self._width_at_press = 0

    # ------------------------------------------------------------------
    # Breite der Namensspalte ziehen
    # ------------------------------------------------------------------
    def on_width_handle(self, pos):
        """Liegt die Position auf der Trennlinie zwischen Name und Wert?"""
        border = self.contentsRect().left() + self.window_.name_width

        return abs(pos.x() - border) <= WIDTH_HANDLE

    def mouseMoveEvent(self, event):
        if self._width_drag:
            offset = event.pos().x() - self._press_x
            self.window_.set_name_width(self._width_at_press + offset)
            event.accept()
            return

        if self.on_width_handle(event.pos()):
            self.setCursor(QtCore.Qt.CursorShape.SizeHorCursor)
        else:
            self.unsetCursor()

        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self.on_width_handle(event.pos()):
            # Einmal beim Druecken entscheiden, nicht bei jedem Ereignis -
            # sonst reisst das Ziehen ab, sobald der Rand weggewandert ist.
            self._width_drag = True
            self._press_x = event.pos().x()
            self._width_at_press = self.window_.name_width
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self._width_drag:
            self._width_drag = False
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        self.unsetCursor()
        super().leaveEvent(event)

    def set_text(self, text):
        if text != self.text:
            self.text = text
            self.update()

    def contextMenuEvent(self, event):
        self.window_.row_menu(self, event.globalPos())
        event.accept()

    def paintEvent(self, event):
        super().paintEvent(event)

        paint = QtGui.QPainter(self)
        paint.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing, True)

        rect = self.contentsRect().adjusted(5, 2, -5, -2)
        if rect.width() < 20 or rect.height() < 10:
            return

        # Name: kleine feste Schrift, hoechstens die Haelfte der Breite
        name_font = self.font()
        name_font.setPointSizeF(max(self.window_.name_point_size, 6.0))
        paint.setFont(name_font)

        metrics = QtGui.QFontMetrics(name_font)
        name = self.signal.name

        # Fuer alle Zeilen dieselbe Breite, damit die Werte untereinander
        # stehen. Ziehen am rechten Rand aendert sie.
        name_width = max(min(self.window_.name_width - 10, rect.width() - 30), 10)

        name_rect = QtCore.QRect(rect.left(), rect.top(), name_width, rect.height())
        paint.setPen(self.palette().color(QtGui.QPalette.ColorRole.WindowText))
        paint.drawText(
            name_rect,
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(name, QtCore.Qt.TextElideMode.ElideRight, name_width),
        )

        # Wert: eigene Flaeche mit dem Hintergrund der Zeichenflaeche,
        # damit auf einen Blick klar ist, was Name und was Wert ist
        full = self.contentsRect()
        value_rect = QtCore.QRect(
            name_rect.right() + 6,
            rect.top(),
            max(rect.right() - name_rect.right() - 6, 10),
            rect.height(),
        )

        paint.fillRect(
            QtCore.QRect(value_rect.left() - 3, full.top(), full.right() - value_rect.left() + 4, full.height()),
            QtGui.QColor(self.window_.value_background()),
        )

        text = self.text
        unit = (self.signal.unit or "").strip()
        with_unit = f"{text} {unit}" if unit else text

        # Untergrenze ist die Groesse des Namens - kleiner hilft niemandem,
        # dann wird stattdessen gerollt
        smallest = max(QtGui.QFontMetrics(name_font).height() - 2, 7)

        value_font = self.font()
        size = max(int(value_rect.height() * 0.72), smallest)
        value_font.setPixelSize(size)
        value_font.setBold(True)

        # Passt der Text nicht in die Breite, Schrift entsprechend kleiner
        shown = with_unit
        width = QtGui.QFontMetrics(value_font).horizontalAdvance(shown)
        if width > value_rect.width() and unit:
            # zuerst die Einheit opfern, der Wert ist wichtiger
            shown = text
            width = QtGui.QFontMetrics(value_font).horizontalAdvance(shown)

        if width > value_rect.width():
            size = max(int(size * value_rect.width() / width), 7)
            value_font.setPixelSize(size)

        paint.setFont(value_font)
        paint.setPen(QtGui.QColor(self.window_.color_of(self.signal)))
        paint.drawText(
            value_rect,
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter,
            shown,
        )


class NumericScalar(QtWidgets.QWidget):
    """Fenster mit den aktuellen Werten einiger Signale, gross."""

    add_channels_request = QtCore.Signal(list)
    timestamp_changed_signal = QtCore.Signal(object, float)

    def __init__(
        self,
        channels=None,
        format=None,
        float_precision=None,
        name_width=None,
        owner=None,
        parent=None,
        *args,
        **kwargs,
    ):
        super().__init__(parent)

        self.owner = owner
        self.mode = "offline"
        self.pattern = {}

        self.signals = []
        self.rows = []

        self._timestamp = None
        self._region = False
        self.format = format or "Physical"
        self.float_precision = 3 if float_precision is None else int(float_precision)
        self.name_point_size = 8.0

        # Breite der Namensspalte, fuer alle Zeilen dieses Fensters gleich
        self.name_width = max(int(name_width or 0), 40) if name_width else 140

        self.setAcceptDrops(True)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)

        self.container = QtWidgets.QWidget()
        self.rows_layout = QtWidgets.QVBoxLayout(self.container)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(1)

        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.container)

        layout.addWidget(self.scroll)

        self.hint = QtWidgets.QLabel("Drag channels here")
        self.hint.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.rows_layout.addWidget(self.hint)

        if channels:
            self.add_new_channels(channels)

    # ------------------------------------------------------------------
    # Kanaele
    # ------------------------------------------------------------------
    def entry_of(self, signal):
        """Kennung, an der ein Signal wiedererkannt wird."""
        return (
            signal.name,
            getattr(signal, "origin_uuid", ""),
            getattr(signal, "group_index", -1),
            getattr(signal, "channel_index", -1),
        )

    def add_new_channels(self, channels, mime_data=None):
        known = {self.entry_of(signal) for signal in self.signals}

        for signal in channels or []:
            if signal is None:
                continue

            samples = getattr(signal, "samples", None)
            if samples is None or samples.dtype.names or len(samples.shape) > 1:
                continue

            entry = self.entry_of(signal)
            if entry in known:
                continue

            known.add(entry)
            self.signals.append(signal)

            row = ScalarRow(signal, self, self.container)
            self.rows.append(row)
            self.rows_layout.addWidget(row)

        self.hint.setVisible(not self.signals)
        self.update_values()

    def remove_row(self, row):
        if row not in self.rows:
            return

        self.rows.remove(row)
        self.signals.remove(row.signal)
        self.rows_layout.removeWidget(row)
        row.setParent(None)
        row.deleteLater()

        self.hint.setVisible(not self.signals)

    def set_name_width(self, width):
        """Breite der Namensspalte setzen - gilt fuer alle Zeilen."""
        width = max(int(width), 40)

        if self.rows:
            # mindestens ein Stueck fuer den Wert stehen lassen
            width = min(width, max(self.rows[0].width() - 40, 40))

        if width == self.name_width:
            return

        self.name_width = width

        for row in self.rows:
            row.update()

    def minimum_row_height(self):
        """Schmalste Zeile: gerade so hoch wie der Kanalname."""
        font = self.font()
        font.setPointSizeF(max(self.name_point_size, 6.0))

        return QtGui.QFontMetrics(font).height() + 4

    def value_background(self):
        """Hintergrund der Wertflaeche - derselbe wie in der Zeichenflaeche."""
        if QtCore.QSettings().value("plot/background", "Black") == "White":
            return "#ffffff"

        return "#000000"

    def color_of(self, signal):
        try:
            return fn.mkColor(signal.color).name()
        except Exception:
            return "#e0e0e0"

    def precision_of(self, signal):
        precision = CHANNEL_PRECISION.get(signal.name)
        if precision is None:
            precision = getattr(signal, "precision", None)
        if precision is None:
            precision = self.float_precision

        return int(precision)

    # ------------------------------------------------------------------
    # Werte
    # ------------------------------------------------------------------
    def value_text(self, signal):
        """Der Wert dieses Signals an der Stelle des Cursors."""
        timestamps = getattr(signal, "timestamps", None)
        samples = getattr(signal, "samples", None)

        if timestamps is None or samples is None or not len(timestamps) or not len(samples):
            return "n.a."

        stamp = self._timestamp

        if stamp is None:
            index = len(timestamps) - 1
        else:
            index = int(np.searchsorted(timestamps, stamp, side="right")) - 1
            if index < 0:
                # Cursor liegt vor dem ersten Abtastwert
                return "n.a."

        index = min(index, len(samples) - 1)
        value = samples[index]

        # Die Signale kommen roh herein, die Umrechnung macht erst den
        # physikalischen Wert - und bei einer Wertetabelle den Text.
        conversion = getattr(signal, "conversion", None)
        if conversion is not None:
            try:
                converted = conversion.convert(np.array([value]), as_bytes=True)
            except TypeError:
                try:
                    converted = conversion.convert(np.array([value]))
                except Exception:
                    converted = None
            except Exception:
                converted = None

            if converted is not None and len(converted):
                value = converted[0]

        if isinstance(value, (bytes, np.bytes_)):
            return bytes(value).decode("utf-8", errors="replace").strip("\0")

        try:
            return value_as_str(value, "phys", precision=self.precision_of(signal))
        except Exception:
            return str(value)

    def update_values(self):
        for row in self.rows:
            row.set_text(self.value_text(row.signal))

    def _apply_timestamp(self, stamp):
        try:
            stamp = float(stamp)
        except (TypeError, ValueError):
            return

        if stamp == self._timestamp:
            return

        self._timestamp = stamp
        self.update_values()

    def set_timestamp(self, stamp=None, emit=True, spinbox=False):
        """Von der Fensterkopplung gerufen, wenn der Cursor sich bewegt."""
        if stamp is None:
            return

        if self._region:
            # Bereich offen: es fuehrt der Hauptcursor, nicht der
            # bewegte. Der Plot schickt hier den bewegten her.
            return

        self._apply_timestamp(stamp)

    def set_region(self, region):
        """Zwei Cursor: angezeigt wird der erste, der Hauptcursor."""
        if not region:
            self.clear_region()
            return

        self._region = True
        self._apply_timestamp(min(region))

    def clear_region(self):
        """Bereich aufgeloest - wieder dem Cursor folgen."""
        self._region = False

    # ------------------------------------------------------------------
    # Bedienung
    # ------------------------------------------------------------------
    def row_menu(self, row, position):
        menu = QtWidgets.QMenu(self)
        menu.addAction("Set color")
        menu.addAction("Set precision")
        menu.addSeparator()
        menu.addAction("Remove this channel")

        action = menu.exec(position)
        if action is None:
            return

        text = action.text()

        if text == "Set color":
            color = QtWidgets.QColorDialog.getColor(
                QtGui.QColor(self.color_of(row.signal)), self, f"Color for {row.signal.name}"
            )
            if color.isValid():
                row.signal.color = color.name()
                row.update()

        elif text == "Set precision":
            precision, ok = QtWidgets.QInputDialog.getInt(
                self,
                "Set new precision (float decimals)",
                "Precision:",
                self.precision_of(row.signal),
                -1,
                15,
                1,
            )
            if ok:
                # gilt fuer alle Fenster, wie in der Kanalliste
                set_channel_precision(row.signal.name, precision)
                self.update_values()

        elif text == "Remove this channel":
            self.remove_row(row)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(MIME):
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(MIME):
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        data = event.mimeData()

        if data.hasFormat(MIME):
            names = extract_mime_names(data)
            if names:
                self.add_channels_request.emit(names)
            event.accept()
        else:
            event.ignore()

    # ------------------------------------------------------------------
    # Konfiguration
    # ------------------------------------------------------------------
    def to_config(self):
        channels = []

        if not self.pattern:
            for signal in self.signals:
                channels.append(
                    {
                        "origin_uuid": str(getattr(signal, "origin_uuid", "")),
                        "name": signal.name,
                        "ranges": copy_ranges(getattr(signal, "ranges", None) or []),
                        "format": getattr(signal, "format", "phys"),
                        "color": self.color_of(signal),
                    }
                )

        return {
            "format": self.format,
            "mode": self.mode,
            "channels": channels,
            "pattern": self.pattern,
            "float_precision": self.float_precision,
            "name_width": int(self.name_width),
        }
