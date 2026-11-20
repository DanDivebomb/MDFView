import bisect
from collections import ChainMap
from datetime import timedelta
from functools import lru_cache, partial, reduce
from math import ceil
import os
from pathlib import Path
from tempfile import gettempdir
from threading import Lock
from time import perf_counter
from traceback import format_exc
from zipfile import ZIP_DEFLATED, ZipFile

import dateutil.tz
import numpy as np
import pyqtgraph as pg
from pyqtgraph import Qt
import pyqtgraph.functions as fn
from PySide6 import QtCore, QtGui, QtWidgets

from ... import tool as Tool
from ...blocks.conversion_utils import from_dict, to_dict
from ...blocks.cutils import get_idx_with_edges, positions
from ...blocks.utils import target_byte_order
from ..dialogs.messagebox import MessageBox
from ..function_library import FunctionLibrary
from ..utils import FONT_SIZE, value_as_str
from .viewbox import ViewBoxWithCursor

LOCAL_TIMEZONE = dateutil.tz.tzlocal()


@lru_cache(maxsize=1024)
def polygon_and_ndarray(size):
    polygon = QtGui.QPolygonF()
    polygon.resize(size)

    nbytes = 2 * len(polygon) * 8
    ptr = polygon.data()
    if ptr is None:
        ptr = 0
    buffer = Qt.shiboken.VoidPtr(ptr, nbytes, True)
    ndarray = np.frombuffer(buffer, np.double).reshape((-1, 2))

    return polygon, ndarray


def monkey_patch_pyqtgraph():
    def _keys(self, styles):
        def getId(obj):
            try:
                return obj._id
            except AttributeError:
                obj._id = next(pg.graphicsItems.ScatterPlotItem.SymbolAtlas._idGenerator)
                return obj._id

        res = [
            (
                symbol if isinstance(symbol, (str, int)) else getId(symbol),
                size,
                getId(pen),
                getId(brush),
            )
            for symbol, size, pen, brush in styles[:1]
        ]

        return res

    mkColor_factory = fn.mkColor
    mkBrush_factory = fn.mkBrush
    mkPen_factory = fn.mkPen

    def mkColor(*args):
        if isinstance(args[0], QtGui.QColor):
            return QtGui.QColor(args[0])
        elif isinstance(args[0], QtGui.QBrush):
            return QtGui.QColor(args[0].color())
        else:
            try:
                return cached_mkColor_factory(*args)
            except:
                return mkColor_factory(*args)

    @lru_cache(maxsize=2048)
    def cached_mkColor_factory(*args):
        return mkColor_factory(*args)

    def mkBrush(*args, **kwargs):
        if len(args) == 1 and isinstance(args[0], QtGui.QBrush):
            return args[0]
        try:
            return cached_mkBrush_factory(*args, **kwargs)
        except:
            return mkBrush_factory(*args, **kwargs)

    @lru_cache(maxsize=2048)
    def cached_mkBrush_factory(*args, **kargs):
        return mkBrush_factory(*args, **kargs)

    def mkPen(*args, **kwargs):
        try:
            return cached_mkPen_factory(*args, **kwargs)
        except:
            return mkPen_factory(*args, **kwargs)

    @lru_cache(maxsize=2048)
    def cached_mkPen_factory(*args, **kargs):
        return mkPen_factory(*args, **kargs)

    # speed-up monkey patches
    pg.graphicsItems.ScatterPlotItem.SymbolAtlas._keys = _keys
    pg.graphicsItems.ScatterPlotItem._USE_QRECT = False

    fn.mkBrush = mkBrush
    fn.mkColor = mkColor
    fn.mkPen = mkPen


import asammdf.mdf as mdf_module

from ...signal import Signal
from .. import serde
from ..dialogs.define_channel import DefineChannel
from ..mdfview_enums import conversion_of, enum_entries, value_color, value_label
from ..serde import extract_mime_names
from ..utils import copy_ranges
from .channel_stats import ChannelStats
from .cursor import Bookmark, Cursor, Region
from .dict_to_tree import ComputedChannelInfoWindow
from .formated_axis import FormatedAxis
from .tree import (
    CHANNEL_PRECISION,
    ChannelsTreeItem,
    ChannelsTreeWidget,
    moving_source,
    signal_count,
    validate_drag_items,
)

bin_ = bin

HERE = Path(__file__).resolve().parent

NOT_FOUND = 0xFFFFFFFF

# Liniendicke und -art je Signal. 0 heisst: globale Einstellung benutzen,
# "" heisst durchgezogen - so bleibt alles wie bisher, solange niemand
# etwas anderes waehlt.
LINE_WIDTHS = {
    "thin": 0,
    "thick": 3,
    "very thick": 5,
}

LINE_STYLES = {
    "": QtCore.Qt.PenStyle.SolidLine,
    "solid": QtCore.Qt.PenStyle.SolidLine,
    "dashed": QtCore.Qt.PenStyle.DashLine,
    "dotted": QtCore.Qt.PenStyle.DotLine,
    "dash-dot": QtCore.Qt.PenStyle.DashDotLine,
}

# Hoehe der Knopfreihe eines Strips. Der Spaltenkopf der Kanalliste
# bekommt dieselbe, damit beide Spalten gleich hoch anfangen.
STRIP_BAR_HEIGHT = 26

# Ablaufverfolgung der Strip-Geometrie, einschalten mit
# MDFVIEW_DEBUG_STRIPS=1
STRIP_DEBUG = bool(os.environ.get("MDFVIEW_DEBUG_STRIPS"))


def second_cursor_color(color):
    """Farbe des zweiten Bereichscursors, deutlich vom ersten getrennt."""
    base = fn.mkColor(color)
    hue, saturation, value, _alpha = base.getHsv()

    if hue < 0:
        # grau oder weiss: fester Gegenwert
        return "#00b0ff"

    return QtGui.QColor.fromHsv((hue + 150) % 360, max(saturation, 180), max(value, 200)).name()
HONEYWELL_SECONDS_PER_CM = 0.1

float64 = np.float64


def simple_min(a, b):
    if b != b:  # noqa: PLR0124
        # b is NaN
        return a
    if a <= b:
        return a
    return b


def simple_max(a, b):
    if b != b:  # noqa: PLR0124
        # b is NaN
        return a
    if a <= b:
        return b
    return a


def get_descriptions_by_uuid(mime):
    descriptions = {}
    if mime:
        for item in mime:
            descriptions[item["uuid"]] = item
            if item.get("type", "channel") == "group":
                descriptions.update(get_descriptions_by_uuid(item["channels"]))

    return descriptions


class StripDropHint(QtWidgets.QWidget):
    """Auswahlfeld, das waehrend des Ziehens neben dem Mauszeiger steht.

    Ein echtes Menue geht waehrend eines Ziehvorgangs nicht auf - Windows
    haelt die Maus gefangen und das Menue bekaeme keine Klicks. Dieses
    Feld braucht keine: es wird nur gezeichnet, die Auswahl folgt der
    Mausposition, und beim Loslassen gilt, was hervorgehoben ist.
    """

    ROW_HEIGHT = 26
    WIDTH = 180

    def __init__(self, parent=None):
        super().__init__(parent, QtCore.Qt.WindowType.ToolTip | QtCore.Qt.WindowType.FramelessWindowHint)

        # Das Feld darf dem Ziehvorgang nicht in die Quere kommen.
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self.rows = ("New strip above", "Add to this strip", "New strip below")
        self.choice = 1

        self.resize(self.WIDTH, self.ROW_HEIGHT * 3)

    def show_for(self, global_position, middle):
        self.rows = ("New strip above", middle, "New strip below")
        self.choice = 1

        # seitlich versetzt, damit der Zeiger nie darauf steht
        self.move(global_position.x() + 24, global_position.y() - self.height() // 2)

        self.show()
        self.raise_()
        self.update()

    def update_choice(self, global_position):
        row = int((global_position.y() - self.y()) // self.ROW_HEIGHT)
        row = max(0, min(2, row))

        if row != self.choice:
            self.choice = row
            self.update()

        return self.choice

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)

        palette = self.palette()
        # Window statt ToolTip: sonst gelb, egal ob helles oder dunkles Thema
        background = palette.color(QtGui.QPalette.ColorRole.Window)
        foreground = palette.color(QtGui.QPalette.ColorRole.WindowText)
        highlight = palette.color(QtGui.QPalette.ColorRole.Highlight)
        highlight_text = palette.color(QtGui.QPalette.ColorRole.HighlightedText)

        painter.fillRect(self.rect(), background)
        painter.setPen(foreground)
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

        for index, text in enumerate(self.rows):
            rect = QtCore.QRect(1, index * self.ROW_HEIGHT, self.width() - 2, self.ROW_HEIGHT)

            if index == self.choice:
                painter.fillRect(rect, highlight)
                painter.setPen(highlight_text)
            else:
                painter.setPen(foreground)

            painter.drawText(
                rect.adjusted(8, 0, -8, 0),
                QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignLeft,
                text,
            )

        painter.end()


class TimevaseOverview(QtWidgets.QWidget):
    # Neuer sichtbarer Zeitbereich, wenn der Benutzer die Leiste schiebt
    range_moved = QtCore.Signal(float, float)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Etwas hoeher als noetig: direkt darueber liegt die Zeitachse,
        # und wer die Leiste knapp verfehlt, zieht dort an der Achse.
        self.setFixedHeight(20)
        self.setMouseTracking(False)
        self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        self.setToolTip("Ausschnitt verschieben: ziehen oder klicken")

        self._x_range = (0, 1)
        self._timebase = None
        self._drag_offset = None
        self._drag_span = None
        self._hover = False

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    @property
    def x_range(self):
        return self._x_range

    @x_range.setter
    def x_range(self, value):
        self._x_range = sorted(value)
        self.update()

    @property
    def timebase(self):
        return self._timebase

    @timebase.setter
    def timebase(self, value):
        self._timebase = sorted(value)
        self.update()

    def span(self):
        """Gesamter dargestellter Zeitbereich und die nutzbare Breite.

        Die Leiste zeigt immer alles, was es gibt: die Messdauer und,
        falls man darueber hinaus gescrollt hat, auch den sichtbaren
        Bereich. Gibt (Anfang, Ende, Breite) zurueck oder None.
        """
        # Waehrend des Ziehens bleibt der Massstab stehen. Sonst
        # aendert sich mit dem Ausschnitt auch der dargestellte
        # Gesamtbereich, und die Leiste rutscht unter der Maus weg.
        if self._drag_span is not None:
            return self._drag_span

        if self.timebase is None:
            return None

        width = self.geometry().width() - 2
        if width < 3:
            return None

        alles = sorted([*self.x_range, *self.timebase])
        anfang, ende = alles[0], alles[-1]

        if ende <= anfang:
            return None

        return anfang, ende, width

    def time_at(self, x):
        """Zeitpunkt unter dem Mauszeiger."""
        bereich = self.span()
        if bereich is None:
            return None

        anfang, ende, width = bereich

        return anfang + max(min((x - 1) / width, 1.0), 0.0) * (ende - anfang)

    def move_to(self, mitte):
        """Den sichtbaren Ausschnitt um diesen Zeitpunkt legen."""
        bereich = self.span()
        if bereich is None:
            return

        anfang, ende, _width = bereich
        breite = self.x_range[1] - self.x_range[0]

        if breite <= 0:
            return

        # Der Ausschnitt darf nicht ganz aus der Messung herauswandern.
        links = max(min(mitte - breite / 2, ende - breite), anfang)

        if abs(links - self.x_range[0]) < breite * 1e-4:
            return

        self.range_moved.emit(float(links), float(links + breite))

    def mousePressEvent(self, event):
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        bereich = self.span()
        zeit = self.time_at(event.position().x())
        if zeit is None or bereich is None:
            return

        mitte = (self.x_range[0] + self.x_range[1]) / 2

        # Innerhalb des blauen Felds mitnehmen, sonst dorthin springen.
        if self.x_range[0] <= zeit <= self.x_range[1]:
            self._drag_offset = zeit - mitte
        else:
            self._drag_offset = 0.0
            self.move_to(zeit)

        self._drag_span = bereich
        self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._drag_offset is None:
            super().mouseMoveEvent(event)
            return

        zeit = self.time_at(event.position().x())
        if zeit is not None:
            self.move_to(zeit - self._drag_offset)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        self._drag_span = None
        self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        rect = self.geometry()
        width = rect.width()
        height = rect.height()

        if width < 3 or self.timebase is None:
            super().paintEvent(event)
        else:
            width -= 2

            painter = QtGui.QPainter(self)

            complete_range = sorted([*self.x_range, *self.timebase])
            start = complete_range[0]
            end = complete_range[-1]
            complete_range = end - start

            x_range = self.x_range

            x_start = (x_range[0] - start) / complete_range * width + 1
            x_end = (x_range[1] - start) / complete_range * width + 1

            timebase = self.timebase

            t_start = (timebase[0] - start) / complete_range * width + 1
            t_end = (timebase[1] - start) / complete_range * width + 1

            pen = fn.mkPen("#00ff00")
            pen.setWidth(3)
            painter.setPen(pen)
            painter.drawLine(
                QtCore.QPointF(t_start, height / 2),
                QtCore.QPointF(t_end, height / 2),
            )

            farbe = "#4b4bffbf" if self._hover else "#0000ff7f"
            pen = fn.mkPen(farbe)
            brush = fn.mkBrush(farbe)
            painter.setPen(pen)
            painter.setBrush(brush)
            painter.drawRect(
                QtCore.QRectF(x_start, 0, x_end - x_start, height),
            )

            painter.end()


class PlotSignal(Signal):
    def __init__(self, signal, index=0, trim_info=None, duplication=1, allow_trim=True, allow_nans=False):
        super().__init__(
            signal.samples,
            signal.timestamps,
            signal.unit,
            signal.name,
            signal.conversion,
            signal.comment,
            signal.master_metadata,
            signal.display_names,
            signal.attachment,
            signal.source,
            signal.bit_count,
            invalidation_bits=signal.invalidation_bits,
            encoding=signal.encoding,
            flags=signal.flags,
        )

        self._pos = self._plot_samples = self._plot_timestamps = None
        self._enable = False

        self.path = None

        self._dtype = "i1"
        self._buffer_size = 1

        self.duplication = duplication
        self.uuid = getattr(signal, "uuid", os.urandom(6).hex())
        self.origin_uuid = getattr(signal, "origin_uuid", os.urandom(6).hex())
        self.origin_mdf = getattr(signal, "origin_mdf", "")

        self.group_index = getattr(signal, "group_index", NOT_FOUND)
        self.channel_index = getattr(signal, "channel_index", NOT_FOUND)
        self.precision = getattr(signal, "precision", 3)

        self._mode = "phys"
        self.enable = getattr(signal, "enable", False)

        self.format = getattr(signal, "format", "phys")

        self.individual_axis = False
        self.computation = signal.computation
        self.original_name = getattr(signal, "original_name", None)
        if hasattr(signal, "tooltip"):
            self.tooltip = signal.tooltip

        self.y_link = False

        self._y_range = None
        # Gesperrte Signale behalten ihren Y-Bereich, egal was Fit, Home,
        # Zoom oder die gemeinsame Achse damit vorhaben.
        self._y_locked = False

        # Eigene Liniendicke und -art. 0 bzw. "" bedeutet: so zeichnen
        # wie bisher, also nach der globalen Einstellung.
        self.line_width = 0
        self.line_style = ""

        self.y_range = (0, -1)
        self.home = (0, -1)

        self.trim_info = None

        # take out NaN values
        samples = self.samples
        if samples.dtype.kind not in "SUV":
            if not allow_nans:
                nans = np.isnan(samples)
                if np.any(nans):
                    self.samples = self.samples[~nans]
                    self.timestamps = self.timestamps[~nans]

        if self.samples.dtype.byteorder not in target_byte_order:
            self.samples = self.samples.byteswap().view(self.samples.dtype.newbyteorder())

        if self.timestamps.dtype.byteorder not in target_byte_order:
            self.timestamps = self.timestamps.byteswap().view(self.timestamps.dtype.newbyteorder())

        if self.timestamps.dtype != float64:
            self.timestamps = self.timestamps.astype(float64)

        self.text_conversion = None

        if self.conversion:
            samples = self.conversion.convert(self.samples, as_bytes=True)
            if samples.dtype.kind not in "SUV":
                if not allow_nans:
                    nans = np.isnan(samples)
                    if np.any(nans):
                        self.raw_samples = self.samples[~nans]
                        self.phys_samples = samples[~nans]
                        self.timestamps = self.timestamps[~nans]
                        self.samples = self.samples[~nans]
                    else:
                        self.raw_samples = self.samples
                        self.phys_samples = samples
                else:
                    self.raw_samples = self.samples
                    self.phys_samples = samples
            else:
                self.text_conversion = self.conversion
                self.phys_samples = self.raw_samples = self.samples

        else:
            self.phys_samples = self.raw_samples = self.samples

        self.plot_samples = self.phys_samples
        self.plot_timestamps = self.timestamps

        self._stats = {
            "range": (0, -1),
            "range_stats": {},
            "visible": (0, -1),
            "visible_stats": {},
            "fmt": "",
        }

        if getattr(signal, "color", None):
            color = signal.color or serde.COLORS[index % serde.COLORS_COUNT]
        else:
            color = serde.COLORS[index % serde.COLORS_COUNT]
        self.color = fn.mkColor(color)
        self.color_name = self.color.name()
        self.pen = fn.mkPen(color=color, style=QtCore.Qt.PenStyle.SolidLine)

        self._min = None
        self._max = None
        self._rms = None
        self._avg = None
        self._std = None
        self._min_raw = None
        self._max_raw = None
        self._avg_raw = None
        self._rms_raw = None
        self._std_raw = None

        self._stats_available = False
        self._compute_basic_stats()

        self.mode = getattr(signal, "mode", "phys")
        if allow_trim:
            self.trim(*(trim_info or (None, None, 1900)))

    @property
    def avg(self):
        if not self._stats_available:
            self._compute_stats()
        return self._avg if self._mode == "phys" else self._avg_raw

    @avg.setter
    def avg(self, avg):
        self._avg = avg

    def _compute_basic_stats(self):
        self._stats_available = False

        if len(self.phys_samples):
            if self.raw_samples.dtype.kind in "SUV":
                self._min_raw = ""
                self._max_raw = ""
                self._avg_raw = ""
                self._rms_raw = ""
                self._std_raw = ""
            else:
                samples = self.raw_samples[np.isfinite(self.raw_samples)]
                if len(samples):
                    self._min_raw = np.nanmin(samples)
                    self._max_raw = np.nanmax(samples)
                else:
                    self._min_raw = "n.a."
                    self._max_raw = "n.a."

            if self.phys_samples is self.raw_samples:
                if self.phys_samples.dtype.kind in "SUV":
                    self.is_string = True
                else:
                    self.is_string = False

                self._min = self._min_raw
                self._max = self._max_raw

            else:
                if self.phys_samples.dtype.kind in "SUV":
                    self.is_string = True
                    self._min = ""
                    self._max = ""
                    self._avg = ""
                    self._rms = ""
                    self._std = ""
                else:
                    self.is_string = False
                    samples = self.phys_samples[np.isfinite(self.phys_samples)]
                    if len(samples):
                        self._min = np.nanmin(samples)
                        self._max = np.nanmax(samples)
                    else:
                        self._min = "n.a."
                        self._max = "n.a."
                        self._avg = "n.a."
                        self._rms = "n.a."
                        self._std = "n.a."

            self.empty = False

        else:
            self.empty = True
            if self.phys_samples.dtype.kind in "SUV":
                self.is_string = True
                self._min = ""
                self._max = ""
                self._rms = ""
                self._avg = ""
                self._std = ""
                self._min_raw = ""
                self._max_raw = ""
                self._avg_raw = ""
                self._rms_raw = ""
                self._std_raw = ""
            else:
                self.is_string = False
                self._min = "n.a."
                self._max = "n.a."
                self._rms = "n.a."
                self._avg = "n.a."
                self._std = "n.a."
                self._min_raw = "n.a."
                self._max_raw = "n.a."
                self._avg_raw = "n.a."
                self._rms_raw = "n.a."
                self._std_raw = "n.a."

    def _compute_stats(self):
        if len(self.phys_samples):
            if self.raw_samples.dtype.kind in "SUV":
                self._min_raw = ""
                self._max_raw = ""
                self._avg_raw = ""
                self._rms_raw = ""
                self._std_raw = ""
            else:
                samples = self.raw_samples[np.isfinite(self.raw_samples)]
                if len(samples):
                    _float_samples = samples.astype(np.float64)
                    self._avg_raw = np.mean(_float_samples)
                    self._rms_raw = np.sqrt(np.mean(np.square(_float_samples)))
                    self._std_raw = np.std(_float_samples)
                else:
                    self._min_raw = "n.a."
                    self._max_raw = "n.a."
                    self._avg_raw = "n.a."
                    self._rms_raw = "n.a."
                    self._std_raw = "n.a."

            if self.phys_samples is self.raw_samples:
                if self.phys_samples.dtype.kind in "SUV":
                    self.is_string = True
                else:
                    self.is_string = False

                self._min = self._min_raw
                self._max = self._max_raw
                self._avg = self._avg_raw
                self._rms = self._rms_raw
                self._std = self._std_raw

            else:
                if self.phys_samples.dtype.kind in "SUV":
                    self.is_string = True
                    self._min = ""
                    self._max = ""
                    self._avg = ""
                    self._rms = ""
                    self._std = ""
                else:
                    self.is_string = False
                    samples = self.phys_samples[np.isfinite(self.phys_samples)]
                    if len(samples):
                        _float_samples = samples.astype(np.float64)
                        self._avg = np.mean(_float_samples)
                        self._rms = np.sqrt(np.mean(np.square(_float_samples)))
                        self._std = np.std(_float_samples)
                    else:
                        self._min = "n.a."
                        self._max = "n.a."
                        self._avg = "n.a."
                        self._rms = "n.a."
                        self._std = "n.a."

            self.empty = False

        else:
            self.empty = True
            if self.phys_samples.dtype.kind in "SUV":
                self.is_string = True
                self._min = ""
                self._max = ""
                self._rms = ""
                self._avg = ""
                self._std = ""
                self._min_raw = ""
                self._max_raw = ""
                self._avg_raw = ""
                self._rms_raw = ""
                self._std_raw = ""
            else:
                self.is_string = False
                self._min = "n.a."
                self._max = "n.a."
                self._rms = "n.a."
                self._avg = "n.a."
                self._std = "n.a."
                self._min_raw = "n.a."
                self._max_raw = "n.a."
                self._avg_raw = "n.a."
                self._rms_raw = "n.a."
                self._std_raw = "n.a."
        self._stats_available = True

    def cut(self, start=None, stop=None, include_ends=True, interpolation_mode=0):
        cut_sig = super().cut(start, stop, include_ends, interpolation_mode)

        cut_sig.group_index = self.group_index
        cut_sig.channel_index = self.channel_index
        cut_sig.color = self.color
        cut_sig.computation = self.computation
        cut_sig.precision = self.precision
        cut_sig.mdf_uuid = self.origin_uuid

        return PlotSignal(cut_sig, duplication=self.duplication)

    @property
    def enable(self):
        return self._enable

    @enable.setter
    def enable(self, enable_state):
        if self._enable != enable_state:
            self._enable = enable_state
            if len(self.samples):
                if enable_state:
                    self._pos = np.empty(2 * self._buffer_size, dtype="i4")
                    self._plot_samples = np.empty(2 * self._buffer_size, dtype=self._dtype)
                    self._plot_timestamps = np.empty(2 * self._buffer_size, dtype="f8")

                else:
                    self._pos = self._plot_samples = self._plot_timestamps = None

    def get_stats(self, cursor=None, region=None, view_region=None, precision=6):
        stats = {}
        sig = self
        x = sig.timestamps
        size = len(x)

        if precision == -1:
            precision = 16

        format = sig.format

        if size:
            if sig.is_string:
                stats["overall_min"] = ""
                stats["overall_max"] = ""
                stats["overall_average"] = ""
                stats["overall_rms"] = ""
                stats["overall_std"] = ""
                stats["overall_start"] = x[0]
                stats["overall_stop"] = x[-1]
                stats["overall_gradient"] = ""
                stats["overall_integral"] = ""
                stats["overall_delta"] = ""
                stats["overall_start"] = x[0]
                stats["overall_stop"] = x[-1]
                stats["overall_delta_t"] = x[-1] - x[0]
                stats["unit"] = ""
                stats["color"] = sig.color
                stats["name"] = sig.name

                if cursor is not None:
                    position = cursor
                    stats["cursor_t"] = position

                    raw_value, raw_kind, value, kind, _ = self.value_at_timestamp(position)

                    stats["cursor_value"] = value_as_str(value, format, self.plot_samples.dtype, precision)

                else:
                    stats["cursor_t"] = ""
                    stats["cursor_value"] = ""

                if region:
                    start, stop = region
                    stats["selected_start"] = value_as_str(start, format, np.dtype("f8"), precision)
                    stats["selected_stop"] = value_as_str(stop, format, np.dtype("f8"), precision)
                    stats["selected_delta_t"] = value_as_str(stop - start, format, np.dtype("f8"), precision)

                    rraw_value, raw_kind, value, kind, _ = self.value_at_timestamp(start)

                    stats["selected_left"] = value_as_str(value, format, self.plot_samples.dtype, precision)

                    raw_value, raw_kind, value, kind, _ = self.value_at_timestamp(stop)

                    stats["selected_right"] = value_as_str(value, format, self.plot_samples.dtype, precision)

                else:
                    stats["selected_start"] = ""
                    stats["selected_stop"] = ""
                    stats["selected_delta_t"] = ""
                    stats["selected_left"] = ""
                    stats["selected_right"] = ""

                stats["selected_min"] = ""
                stats["selected_max"] = ""
                stats["selected_average"] = ""
                stats["selected_rms"] = ""
                stats["selected_std"] = ""
                stats["selected_delta"] = ""
                stats["selected_gradient"] = ""
                stats["selected_integral"] = ""
                stats["visible_min"] = ""
                stats["visible_max"] = ""
                stats["visible_average"] = ""
                stats["visible_rms"] = ""
                stats["visible_delta"] = ""
                stats["visible_std"] = ""
                stats["visible_gradient"] = ""
                stats["visible_integral"] = ""
            else:
                if size == 1:
                    stats["overall_gradient"] = 0
                    stats["overall_integral"] = 0
                else:
                    stats["overall_gradient"] = value_as_str(
                        (float(sig.samples[-1]) - float(sig.samples[0])) / (sig.timestamps[-1] - sig.timestamps[0]),
                        format,
                        None,
                        precision,
                    )
                    stats["overall_integral"] = value_as_str(
                        np.trapezoid(sig.samples, sig.timestamps), format, None, precision
                    )

                stats["overall_min"] = value_as_str(self.min, format, self.plot_samples.dtype, precision)
                stats["overall_max"] = value_as_str(self.max, format, self.plot_samples.dtype, precision)
                stats["overall_average"] = value_as_str(sig.avg, format, None, precision)
                stats["overall_rms"] = value_as_str(sig.rms, format, None, precision)
                stats["overall_std"] = value_as_str(sig.std, format, None, precision)
                stats["overall_start"] = value_as_str(sig.timestamps[0], format, np.dtype("f8"), precision)
                stats["overall_stop"] = value_as_str(sig.timestamps[-1], format, np.dtype("f8"), precision)
                if self.plot_samples.dtype.kind in "ui":
                    stats["overall_delta"] = value_as_str(
                        int(sig.samples[-1]) - int(sig.samples[0]),
                        format,
                        self.plot_samples.dtype,
                        precision,
                    )
                else:
                    stats["overall_delta"] = value_as_str(
                        sig.samples[-1] - sig.samples[0],
                        format,
                        self.plot_samples.dtype,
                        precision,
                    )
                stats["overall_delta_t"] = value_as_str(x[-1] - x[0], format, np.dtype("f8"), precision)
                stats["unit"] = sig.unit
                stats["color"] = sig.color
                stats["name"] = sig.name

                if cursor is not None:
                    position = cursor
                    stats["cursor_t"] = value_as_str(position, format, np.dtype("f8"), precision)

                    raw_value, raw_kind, value, kind, _ = self.value_at_timestamp(position)

                    stats["cursor_value"] = value_as_str(value, format, self.plot_samples.dtype, precision)

                else:
                    stats["cursor_t"] = ""
                    stats["cursor_value"] = ""

                if region:
                    start, stop = region

                    new_stats = {}
                    new_stats["selected_start"] = value_as_str(start, format, np.dtype("f8"), precision)
                    new_stats["selected_stop"] = value_as_str(stop, format, np.dtype("f8"), precision)
                    new_stats["selected_delta_t"] = value_as_str(stop - start, format, np.dtype("f8"), precision)

                    cut = sig.cut(start, stop)

                    if self.mode == "raw":
                        samples = cut.raw_samples
                    else:
                        samples = cut.phys_samples

                    idx = np.isfinite(samples).ravel()
                    samples = samples[idx]
                    timestamps = cut.timestamps[idx]

                    size = len(samples)

                    if size:
                        new_stats["selected_left"] = value_as_str(
                            samples[0], format, self.plot_samples.dtype, precision
                        )

                        new_stats["selected_right"] = value_as_str(
                            samples[-1], format, self.plot_samples.dtype, precision
                        )

                        _float_samples = samples.astype(np.float64)
                        new_stats["selected_min"] = value_as_str(np.nanmin(samples), format, samples.dtype, precision)
                        new_stats["selected_max"] = value_as_str(np.nanmax(samples), format, samples.dtype, precision)
                        new_stats["selected_average"] = value_as_str(np.mean(_float_samples), format, None, precision)
                        new_stats["selected_std"] = value_as_str(np.std(_float_samples), format, None, precision)
                        new_stats["selected_rms"] = value_as_str(
                            np.sqrt(np.mean(np.square(_float_samples))),
                            format,
                            None,
                            precision,
                        )
                        if samples.dtype.kind in "ui":
                            new_stats["selected_delta"] = value_as_str(
                                int(samples[-1]) - int(samples[0]),
                                format,
                                samples.dtype,
                                precision,
                            )
                        else:
                            new_stats["selected_delta"] = value_as_str(
                                samples[-1] - samples[0],
                                format,
                                samples.dtype,
                                precision,
                            )

                        if size == 1:
                            new_stats["selected_gradient"] = 0
                            new_stats["selected_integral"] = 0
                        else:
                            new_stats["selected_gradient"] = value_as_str(
                                (float(samples[-1]) - float(samples[0])) / (timestamps[-1] - timestamps[0]),
                                format,
                                None,
                                precision,
                            )
                            new_stats["selected_integral"] = value_as_str(
                                np.trapezoid(samples, timestamps), format, None, precision
                            )

                    else:
                        new_stats["selected_min"] = "n.a."
                        new_stats["selected_max"] = "n.a."
                        new_stats["selected_average"] = "n.a."
                        new_stats["selected_left"] = "n.a."
                        new_stats["selected_right"] = "n.a."
                        new_stats["selected_rms"] = "n.a."
                        new_stats["selected_std"] = "n.a."
                        new_stats["selected_gradient"] = "n.a."
                        new_stats["selected_integral"] = "n.a."
                        new_stats["selected_delta"] = "n.a."

                    sig._stats["range"] = (start, stop)
                    sig._stats["range_stats"] = new_stats

                    stats.update(sig._stats["range_stats"])

                else:
                    stats["selected_start"] = ""
                    stats["selected_stop"] = ""
                    stats["selected_delta_t"] = ""
                    stats["selected_left"] = ""
                    stats["selected_right"] = ""
                    stats["selected_min"] = ""
                    stats["selected_max"] = ""
                    stats["selected_average"] = ""
                    stats["selected_rms"] = ""
                    stats["selected_std"] = ""
                    stats["selected_delta"] = ""
                    stats["selected_gradient"] = ""
                    stats["selected_integral"] = ""

                start, stop = view_region

                new_stats = {}
                new_stats["visible_start"] = value_as_str(start, format, np.dtype("f8"), precision)
                new_stats["visible_stop"] = value_as_str(stop, format, np.dtype("f8"), precision)
                new_stats["visible_delta_t"] = value_as_str(stop - start, format, np.dtype("f8"), precision)

                cut = sig.cut(start, stop)

                if self.mode == "raw":
                    samples = cut.raw_samples
                else:
                    samples = cut.phys_samples

                idx = np.isfinite(samples).ravel()
                samples = samples[idx]
                timestamps = cut.timestamps[idx]

                size = len(samples)

                if size:
                    kind = samples.dtype.kind
                    _float_samples = samples.astype(np.float64)
                    new_stats["visible_min"] = value_as_str(np.nanmin(samples), format, samples.dtype, precision)
                    new_stats["visible_max"] = value_as_str(np.nanmax(samples), format, samples.dtype, precision)
                    new_stats["visible_average"] = value_as_str(np.mean(_float_samples), format, None, precision)
                    new_stats["visible_std"] = value_as_str(np.std(_float_samples), format, None, precision)
                    new_stats["visible_rms"] = value_as_str(
                        np.sqrt(np.mean(np.square(_float_samples))), format, None, precision
                    )
                    if kind in "ui":
                        new_stats["visible_delta"] = value_as_str(
                            int(cut.samples[-1]) - int(cut.samples[0]),
                            format,
                            samples.dtype,
                            precision,
                        )
                    else:
                        new_stats["visible_delta"] = value_as_str(
                            _float_samples[-1] - _float_samples[0],
                            format,
                            samples.dtype,
                            precision,
                        )

                    if size == 1:
                        new_stats["visible_gradient"] = 0
                        new_stats["visible_integral"] = 0
                    else:
                        new_stats["visible_gradient"] = value_as_str(
                            (float(samples[-1]) - float(samples[0])) / (timestamps[-1] - timestamps[0]),
                            format,
                            None,
                            precision,
                        )
                        new_stats["visible_integral"] = value_as_str(
                            np.trapezoid(samples, timestamps), format, None, precision
                        )

                else:
                    new_stats["visible_min"] = "n.a."
                    new_stats["visible_max"] = "n.a."
                    new_stats["visible_average"] = "n.a."
                    new_stats["visible_rms"] = "n.a."
                    new_stats["visible_std"] = "n.a."
                    new_stats["visible_delta"] = "n.a."
                    new_stats["visible_gradient"] = "n.a."
                    new_stats["visible_integral"] = "n.a."

                sig._stats["visible"] = (start, stop)
                sig._stats["visible_stats"] = new_stats

                stats.update(sig._stats["visible_stats"])

        else:
            stats["overall_min"] = "n.a."
            stats["overall_max"] = "n.a."
            stats["overall_average"] = "n.a."
            stats["overall_rms"] = "n.a."
            stats["overall_std"] = "n.a."
            stats["overall_start"] = "n.a."
            stats["overall_stop"] = "n.a."
            stats["overall_gradient"] = "n.a."
            stats["overall_integral"] = "n.a."
            stats["overall_delta"] = "n.a."
            stats["overall_delta_t"] = "n.a."
            stats["unit"] = sig.unit
            stats["color"] = sig.color
            stats["name"] = sig.name

            if cursor is not None:
                position = cursor
                stats["cursor_t"] = value_as_str(position, format, np.dtype("f8"), precision)

                stats["cursor_value"] = "n.a."

            else:
                stats["cursor_t"] = ""
                stats["cursor_value"] = ""

            if region is not None:
                start, stop = region

                stats["selected_start"] = value_as_str(start, format, np.dtype("f8"), precision)
                stats["selected_stop"] = value_as_str(stop, format, np.dtype("f8"), precision)
                stats["selected_delta_t"] = value_as_str(stop - start, format, np.dtype("f8"), precision)

                stats["selected_min"] = "n.a."
                stats["selected_max"] = "n.a."
                stats["selected_left"] = "n.a."
                stats["selected_right"] = "n.a."
                stats["selected_average"] = "n.a."
                stats["selected_rms"] = "n.a."
                stats["selected_std"] = "n.a."
                stats["selected_delta"] = "n.a."
                stats["selected_gradient"] = "n.a."
                stats["selected_integral"] = "n.a."

            else:
                stats["selected_start"] = ""
                stats["selected_stop"] = ""
                stats["selected_delta_t"] = ""
                stats["selected_min"] = ""
                stats["selected_max"] = ""
                stats["selected_left"] = ""
                stats["selected_right"] = ""
                stats["selected_average"] = "n.a."
                stats["selected_rms"] = "n.a."
                stats["selected_std"] = "n.a."
                stats["selected_delta"] = ""
                stats["selected_gradient"] = ""
                stats["selected_integral"] = ""

            start, stop = view_region

            stats["visible_start"] = value_as_str(start, format, np.dtype("f8"), precision)
            stats["visible_stop"] = value_as_str(stop, format, np.dtype("f8"), precision)
            stats["visible_delta_t"] = value_as_str(stop - start, format, np.dtype("f8"), precision)

            stats["visible_min"] = "n.a."
            stats["visible_max"] = "n.a."
            stats["visible_average"] = "n.a."
            stats["visible_rms"] = "n.a."
            stats["visible_std"] = "n.a."
            stats["visible_delta"] = "n.a."
            stats["visible_gradient"] = "n.a."
            stats["visible_integral"] = "n.a."

        stats["region"] = region is not None
        stats["color"] = self.color_name

        return stats

    @property
    def max(self):
        if self._mode == "phys":
            _max = self._max
            samples = self.phys_samples
        else:
            _max = self._max_raw
            samples = self.raw_samples

        if _max is not None:
            return _max
        else:
            if samples.dtype.kind in "SUV":
                return ""
            else:
                if len(samples):
                    return np.nanmax(samples)
                else:
                    return "n.a."

    @property
    def min(self):
        if self._mode == "phys":
            _min = self._min
            samples = self.phys_samples
        else:
            _min = self._min_raw
            samples = self.raw_samples

        if _min is not None:
            return _min
        else:
            if samples.dtype.kind in "SUV":
                return ""
            else:
                if len(samples):
                    return np.nanmin(samples)
                else:
                    return "n.a."

    @property
    def mode(self):
        return self._mode

    @mode.setter
    def mode(self, mode):
        if mode != self._mode:
            self._mode = mode
            if mode == "raw":
                self.plot_samples = self.raw_samples
                self.plot_timestamps = self.timestamps
                style = QtCore.Qt.PenStyle.DashLine
            else:
                self.plot_samples = self.phys_samples
                self.plot_timestamps = self.timestamps
                style = QtCore.Qt.PenStyle.SolidLine

            self.pen = fn.mkPen(color=self.color, style=style)

            if self.plot_samples.dtype.kind in "SUV":
                self.is_string = True
            else:
                self.is_string = False

    @property
    def rms(self):
        if not self._stats_available:
            self._compute_stats()
        return self._rms if self.mode == "phys" else self._rms_raw

    @rms.setter
    def rms(self, rms):
        self._rms = rms

    def set_color(self, color):
        self.color = color
        if self.mode == "raw":
            style = QtCore.Qt.PenStyle.DashLine
        else:
            style = QtCore.Qt.PenStyle.SolidLine

        self.pen = fn.mkPen(color=color, style=style)

    def set_home(self, y_range=None):
        self.home = y_range or self.y_range

    @property
    def std(self):
        if not self._stats_available:
            self._compute_stats()
        return self._std if self.mode == "phys" else self._std_raw

    def trim_c(self, start=None, stop=None, width=1, force=False):
        trim_info = (start, stop, width)
        if not force and self.trim_info == trim_info:
            return None

        self.trim_info = trim_info
        sig_timestamps = self.timestamps
        dim = sig_timestamps.size

        if dim:
            if start is None:
                start = sig_timestamps[0]
            if stop is None:
                stop = sig_timestamps[-1]

            if start > stop:
                start, stop = stop, start

            if self._mode == "raw":
                signal_samples = self.raw_samples
            else:
                signal_samples = self.phys_samples

            start_t_sig, stop_t_sig = (
                sig_timestamps[0],
                sig_timestamps[-1],
            )
            if start > stop_t_sig or stop < start_t_sig:
                self.plot_samples = signal_samples[:0]
                self.plot_timestamps = sig_timestamps[:0]
                pos = []
            else:
                start_t = simple_max(start, start_t_sig)
                stop_t = simple_min(stop, stop_t_sig)

                if start_t == start_t_sig:
                    start_ = 0
                else:
                    start_ = np.searchsorted(sig_timestamps, start_t, side="right")

                if stop_t == stop_t_sig:
                    stop_ = dim
                else:
                    stop_ = np.searchsorted(sig_timestamps, stop_t, side="right")

                if stop_ == start_:
                    visible_duplication = 0
                else:
                    visible = ceil((stop_t - start_t) / (stop - start) * width)

                    if visible:
                        visible_duplication = (stop_ - start_) // abs(visible)
                    else:
                        visible_duplication = 0

                if visible_duplication > self.duplication:
                    samples = signal_samples[start_:stop_]
                    timestamps = sig_timestamps[start_:stop_]
                    count, rest = divmod(samples.size, visible_duplication)
                    if rest:
                        count += 1
                    else:
                        rest = visible_duplication
                    steps = visible_duplication

                    if count > self._buffer_size:
                        self._buffer_size = count
                        self._pos = np.empty(2 * self._buffer_size, dtype="i4")
                        self._plot_samples = np.empty(2 * self._buffer_size, dtype=self._dtype)
                        self._plot_timestamps = np.empty(2 * self._buffer_size, dtype="f8")

                    if samples.dtype.kind == "f" and samples.itemsize == 2:
                        samples = samples.astype("f8")
                        self._dtype = samples.dtype

                    if samples.dtype != self._plot_samples.dtype:
                        self._plot_samples = np.empty(2 * self._buffer_size, dtype=samples.dtype)

                        self._dtype = samples.dtype

                    if samples.flags.c_contiguous and timestamps.flags.c_contiguous:
                        positions(
                            samples,
                            timestamps,
                            self._plot_samples,
                            self._plot_timestamps,
                            self._pos,
                            steps,
                            count,
                            rest,
                            samples.dtype.kind,
                            samples.itemsize,
                        )
                    else:
                        positions(
                            samples.copy(),
                            timestamps,
                            self._plot_samples,
                            self._plot_timestamps,
                            self._pos,
                            steps,
                            count,
                            rest,
                            samples.dtype.kind,
                            samples.itemsize,
                        )

                    size = 2 * count
                    pos = self._pos[:size]
                    self.plot_samples = self._plot_samples[:size]
                    self.plot_timestamps = self._plot_timestamps[:size]

                else:
                    start_ = simple_min(simple_max(0, start_ - 2), dim - 1)
                    stop_ = simple_min(stop_ + 2, dim)

                    if start_ == 0 and stop_ == dim:
                        self.plot_samples = signal_samples
                        self.plot_timestamps = sig_timestamps

                        pos = None
                    else:
                        self.plot_samples = signal_samples[start_:stop_]
                        self.plot_timestamps = sig_timestamps[start_:stop_]

                        pos = np.arange(start_, stop_)

        else:
            pos = None

        return pos

    def trim_python(self, start=None, stop=None, width=1900, force=False):
        trim_info = (start, stop, width)
        if not force and self.trim_info == trim_info:
            return None

        self.trim_info = trim_info
        sig = self
        dim = len(sig.timestamps)

        if dim:
            if start is None:
                start = sig.timestamps[0]
            if stop is None:
                stop = sig.timestamps[-1]

            if self.mode == "raw":
                signal_samples = self.raw_samples
            else:
                signal_samples = self.phys_samples

            start_t_sig, stop_t_sig = (
                sig.timestamps[0],
                sig.timestamps[-1],
            )
            if start > stop_t_sig or stop < start_t_sig:
                sig.plot_samples = signal_samples[:0]
                sig.plot_timestamps = sig.timestamps[:0]
                pos = []
            else:
                start_t = max(start, start_t_sig)
                stop_t = min(stop, stop_t_sig)

                if start_t == start_t_sig:
                    start_ = 0
                else:
                    start_ = np.searchsorted(sig.timestamps, start_t, side="right")
                if stop_t == stop_t_sig:
                    stop_ = dim
                else:
                    stop_ = np.searchsorted(sig.timestamps, stop_t, side="right")

                try:
                    visible = abs(int((stop_t - start_t) / (stop - start) * width))

                    if visible:
                        visible_duplication = abs(stop_ - start_) // visible
                    else:
                        visible_duplication = 0
                except:
                    visible_duplication = 0

                while visible_duplication > self.duplication:
                    rows = (stop_ - start_) // visible_duplication
                    stop_2 = start_ + rows * visible_duplication

                    samples = signal_samples[start_:stop_2].reshape(rows, visible_duplication)

                    try:
                        pos_max = samples.argmax(axis=1)
                        pos_min = samples.argmin(axis=1)
                        break
                    except:
                        try:
                            pos_max = np.nanargmax(samples, axis=1)
                            pos_min = np.nanargmin(samples, axis=1)
                            break
                        except ValueError:
                            visible_duplication -= 1

                if visible_duplication > self.duplication:
                    pos = np.dstack([pos_min, pos_max])[0]
                    pos.sort()
                    # pos = np.sort(pos)

                    offsets = np.arange(rows) * visible_duplication

                    pos = (pos.T + offsets).T.ravel()

                    samples = signal_samples[start_:stop_2][pos]

                    timestamps = sig.timestamps[start_:stop_2][pos]

                    if stop_2 != stop_:
                        samples_ = signal_samples[stop_2:stop_]

                        try:
                            pos_max = samples_.argmax()
                            pos_min = samples_.argmin()
                        except:
                            pos_max = np.nanargmax(samples_)
                            pos_min = np.nanargmin(samples_)

                        pos2 = [pos_min, pos_max] if pos_min < pos_max else [pos_max, pos_min]

                        _size = len(pos)

                        samples_ = signal_samples[stop_2:stop_][pos2]
                        timestamps_ = sig.timestamps[stop_2:stop_][pos2]

                        samples = np.concatenate((samples, samples_))
                        timestamps = np.concatenate((timestamps, timestamps_))

                        pos2 = p1, p2 = [min(e + stop_2, dim - 1) for e in pos2]

                        # pos = np.concatenate([pos, pos2])

                        new_pos = np.empty(_size + 2, dtype=pos.dtype)
                        new_pos[:_size] = pos
                        new_pos[_size] = p1
                        new_pos[_size + 1] = p2
                        pos = new_pos

                    sig.plot_samples = samples
                    sig.plot_timestamps = timestamps

                else:
                    start_ = min(max(0, start_ - 2), dim - 1)
                    stop_ = min(stop_ + 2, dim)

                    if start_ == 0 and stop_ == dim:
                        sig.plot_samples = signal_samples
                        sig.plot_timestamps = sig.timestamps

                        pos = None
                    else:
                        sig.plot_samples = signal_samples[start_:stop_]
                        sig.plot_timestamps = sig.timestamps[start_:stop_]

                        pos = np.arange(start_, stop_)

        else:
            pos = None

        return pos

    def trim(self, start=None, stop=None, width=1900, force=False):
        if self._enable:
            if width > self._buffer_size and len(self.samples):
                width = ceil(width)
                self._buffer_size = width
                self._pos = np.empty(2 * self._buffer_size, dtype="i4")
                self._plot_samples = np.empty(2 * self._buffer_size, dtype=self._dtype)
                self._plot_timestamps = np.empty(2 * self._buffer_size, dtype="f8")

            self.path = None
            try:
                return self.trim_c(start, stop, width, force)
            except:
                print(format_exc())
                print(self)
                return self.trim_python(start, stop, width, force)

    def value_at_index(self, index):
        if self.mode == "raw":
            kind = self.raw_samples.dtype.kind
        else:
            kind = self.phys_samples.dtype.kind

        raw_kind = self.raw_samples.dtype.kind

        size = len(self)

        if index is None or size == 0:
            value = raw_value = "n.a."
        else:
            if index >= size:
                index = size - 1

            if self.mode == "raw":
                value = self.raw_samples[index]
            else:
                value = self.phys_samples[index]
            raw_value = self.raw_samples[index]

            match kind:
                case "S":
                    try:
                        value = value.decode("utf-8").strip(" \r\n\t\v\0")
                    except:
                        value = value.decode("latin-1").strip(" \r\n\t\v\0")

                    value = value or "<empty string>"
                case "f":
                    value = value
                case _:
                    value = int(value)

            match raw_kind:
                case "S":
                    try:
                        raw_value = raw_value.decode("utf-8").strip(" \r\n\t\v\0")
                    except:
                        raw_value = raw_value.decode("latin-1").strip(" \r\n\t\v\0")

                    raw_value = raw_value or "<empty string>"
                case "f":
                    raw_value = raw_value
                case _:
                    raw_value = int(raw_value)

        return raw_value, raw_kind, value, kind, self.format

    def value_at_timestamp(self, timestamp, numeric=False, strict_timebase=True):
        if self.mode == "raw":
            kind = self.raw_samples.dtype.kind
            samples = self.raw_samples
        else:
            kind = self.phys_samples.dtype.kind
            samples = self.phys_samples

        raw_kind = self.raw_samples.dtype.kind

        if numeric and kind not in "uif":
            samples = self.raw_samples
            kind = self.raw_samples.dtype.kind

        if self.samples.size == 0 or (strict_timebase and not (self.timestamps[0] <= timestamp <= self.timestamps[-1])):
            value = "n.a."
            raw_value = "n.a."
        else:
            if timestamp > self.timestamps[-1]:
                index = -1
            elif timestamp < self.timestamps[0]:
                index = 0
            else:
                index = np.searchsorted(self.timestamps, timestamp, side="left")

            value = samples[index]
            raw_value = self.raw_samples[index]

            match kind:
                case "S":
                    try:
                        value = value.decode("utf-8", errors="replace").strip(" \r\n\t\v\0")
                    except:
                        value = value.decode("latin-1", errors="replace").strip(" \r\n\t\v\0")

                    value = value or "<empty string>"
                case "f":
                    value = value
                case _:
                    value = int(value)

            match raw_kind:
                case "S":
                    try:
                        raw_value = raw_value.decode("utf-8", errors="replace").strip(" \r\n\t\v\0")
                    except:
                        raw_value = raw_value.decode("latin-1", errors="replace").strip(" \r\n\t\v\0")

                    raw_value = raw_value or "<empty string>"
                case "f":
                    raw_value = raw_value
                case _:
                    raw_value = int(raw_value)

        return raw_value, raw_kind, value, kind, self.format

    def timestamp_of_next_different_value(self, timestamp, mode="higher", previous=False):
        if self.mode == "raw":
            kind = self.raw_samples.dtype.kind
            samples = self.raw_samples
        else:
            kind = self.phys_samples.dtype.kind
            samples = self.phys_samples

        if kind not in "uif":
            samples = self.raw_samples
            kind = self.raw_samples.dtype.kind

        if kind == "S" or self.samples.size == 0 or timestamp < self.timestamps[0] or timestamp > self.timestamps[-1]:
            new_timestamp = timestamp
        else:
            index = np.searchsorted(self.timestamps, timestamp, side="left")

            value = samples[index]

            match mode:
                case "higher":
                    idx = np.argwhere(samples > value).ravel()
                case "lower":
                    idx = np.argwhere(samples < value).ravel()
                case _:
                    idx = np.argwhere(samples != value).ravel()

            if previous:
                idx = idx[idx < index]
            else:
                idx = idx[idx > index]

            if len(idx) == 0:
                new_timestamp = timestamp
            else:
                if previous:
                    new_timestamp = self.timestamps[idx[-1]]
                else:
                    new_timestamp = self.timestamps[idx[0]]

        return new_timestamp

    @property
    def y_range(self):
        return self._y_range

    @y_range.setter
    def y_range(self, value):
        if self._y_locked:
            return
        self.path = None
        self._y_range = value

    @property
    def y_locked(self):
        return self._y_locked

    @y_locked.setter
    def y_locked(self, value):
        self._y_locked = bool(value)


from .signal_scale import ScaleDialog


class Plot(QtWidgets.QWidget):
    add_channels_request = QtCore.Signal(list)
    close_request = QtCore.Signal()
    clicked = QtCore.Signal()
    cursor_moved_signal = QtCore.Signal(object, float)
    cursor_removed_signal = QtCore.Signal(object)
    edit_channel_request = QtCore.Signal(object, object)
    region_moved_signal = QtCore.Signal(object, list)
    region_removed_signal = QtCore.Signal(object)
    show_overlapping_alias = QtCore.Signal(object)
    show_properties = QtCore.Signal(object)
    splitter_moved = QtCore.Signal(object, int)
    pattern_group_added = QtCore.Signal(object, object)
    verify_bookmarks = QtCore.Signal(object)
    x_range_changed_signal = QtCore.Signal(object, object)

    bookmarks_changed = QtCore.Signal(list, object)

    item_double_click_handling = "enable/disable"
    dynamic_columns_width = True
    mouse_mode = "pan"

    def __init__(
        self,
        signals,
        with_dots=False,
        origin=None,
        mdf=None,
        line_interconnect="line",
        hide_missing_channels=False,
        hide_disabled_channels=False,
        x_axis="time",
        allow_cursor=True,
        show_cursor_circle=True,
        show_cursor_horizontal_line=True,
        cursor_line_width=1,
        cursor_color="#ffffff",
        region_values_display_mode="delta",
        owner=None,
        enable_zoom_history=True,
        *args,
        **kwargs,
    ):
        self.owner = owner
        self.enable_zoom_history = enable_zoom_history

        bookmarks = kwargs.pop("events", ())
        if not bookmarks:
            bookmarks = kwargs.pop("bookmarks", ())

        timebase_overview = kwargs.pop("timebase_overview", True)

        super().__init__(*args, **kwargs)
        self.closed = False
        self.line_interconnect = line_interconnect
        self.setContentsMargins(0, 0, 0, 0)
        self.pattern = {}
        self.mdf = mdf

        self.timebase_overview = None

        self._settings = QtCore.QSettings()

        self.x_name = "t" if x_axis == "time" else "f"
        self.x_unit = "s" if x_axis == "time" else "Hz"

        self.info_uuid = None

        self._can_switch_mode = True
        self._inhibit_x_range_changed_signal = False
        self._inhibit_timestamp_signals = False
        self._inhibit_timestamp_signals_timer = QtCore.QTimer()
        self._inhibit_timestamp_signals_timer.setSingleShot(True)
        self._inhibit_timestamp_signals_timer.timeout.connect(self._inhibit_timestamp_handler)
        self.can_edit_ranges = True

        self.region_values_display_mode = region_values_display_mode

        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.setSpacing(1)
        main_layout.setContentsMargins(1, 1, 1, 1)

        vbox = QtWidgets.QVBoxLayout()
        vbox.setSpacing(1)
        vbox.setContentsMargins(1, 1, 1, 1)
        widget = QtWidgets.QWidget()
        self._channel_list_options = {
            "hide_missing_channels": hide_missing_channels,
            "hide_disabled_channels": hide_disabled_channels,
        }
        self.channel_selection = ChannelsTreeWidget(
            parent=self,
            plot=self,
            **self._channel_list_options,
        )

        widget.setLayout(vbox)

        self.focused_mode = False
        self.show_bookmarks = True

        self.splitter = QtWidgets.QSplitter()
        self.splitter.addWidget(widget)
        self.splitter.setOpaqueResize(False)

        self.plot = PlotGraphics(
            with_dots=with_dots,
            line_interconnect=self.line_interconnect,
            parent=self,
            origin=origin,
            mdf=self.mdf,
            x_axis=x_axis,
            allow_cursor=allow_cursor,
            plot_parent=self,
        )
        self.plot.zoom_changed.connect(self.zoom_changed)
        self.plot.bookmarks_changed.connect(self.bookmarks_changed)

        if self.plot.cursor1 is not None:
            self.plot.cursor1.show_circle = show_cursor_circle
            self.plot.cursor1.show_horizontal_line = show_cursor_horizontal_line
            self.plot.cursor1.line_width = cursor_line_width
            self.plot.cursor1.color = cursor_color

            self.lock = self.plot.lock

        self.cursor_info = CursorInfo(
            precision=QtCore.QSettings().value("plot/cursor/display_precision", 6),
            unit=self.x_unit,
            name=self.x_name,
            plot=self.plot,
        )

        hbox = QtWidgets.QHBoxLayout()
        hbox.setSpacing(1)
        hbox.setContentsMargins(1, 1, 1, 1)

        vbox.addLayout(hbox)

        # Es gibt nur noch EINE Knopfreihe, und die steht ueber der
        # Kanalliste jedes Strips (siehe _build_list_panel). Die Knoepfe
        # hier bleiben als Traeger ihres Zustands bestehen - mehrere
        # Methoden lesen ihr isFlat() -, sind aber unsichtbar. Alle
        # Befehle dazu stehen im Cmd-Menue des jeweiligen Strips.
        self._hidden_buttons = QtWidgets.QWidget()
        hidden_box = QtWidgets.QHBoxLayout(self._hidden_buttons)
        hidden_box.setContentsMargins(0, 0, 0, 0)
        vbox.addWidget(self._hidden_buttons)
        self._hidden_buttons.hide()

        if self.enable_zoom_history:
            self.undo_btn = btn = QtWidgets.QPushButton("")
            btn.clicked.connect(self.undo_zoom)
            icon = QtGui.QIcon()
            icon.addPixmap(QtGui.QPixmap(":/undo.png"), QtGui.QIcon.Mode.Normal, QtGui.QIcon.State.Off)
            btn.setIcon(icon)
            btn.setToolTip("Undo zoom")
            hidden_box.addWidget(btn)
            btn.setEnabled(False)

            self.redo_btn = btn = QtWidgets.QPushButton("")
            btn.clicked.connect(self.redo_zoom)
            icon = QtGui.QIcon()
            icon.addPixmap(QtGui.QPixmap(":/redo.png"), QtGui.QIcon.Mode.Normal, QtGui.QIcon.State.Off)
            btn.setIcon(icon)
            btn.setToolTip("Redo zoom")
            hidden_box.addWidget(btn)
            btn.setEnabled(False)

        self.lock_btn = btn = QtWidgets.QPushButton("")
        btn.setObjectName("lock_btn")
        btn.clicked.connect(self.set_locked)
        icon = QtGui.QIcon()
        icon.addPixmap(QtGui.QPixmap(":/unlocked.png"), QtGui.QIcon.Mode.Normal, QtGui.QIcon.State.Off)
        btn.setIcon(icon)
        btn.setToolTip("The Y axis is unlocked. Press to lock")
        hidden_box.addWidget(btn)

        self.locked = False

        self.hide_axes_btn = btn = QtWidgets.QPushButton("")
        btn.setObjectName("hide_axes_btn")
        self.hide_axes_btn.clicked.connect(self.hide_axes)
        icon = QtGui.QIcon()
        icon.addPixmap(QtGui.QPixmap(":/axis_on.png"), QtGui.QIcon.Mode.Normal, QtGui.QIcon.State.Off)
        btn.setIcon(icon)
        btn.setToolTip("Hide axis")
        hidden_box.addWidget(self.hide_axes_btn)

        self.focused_mode_btn = btn = QtWidgets.QPushButton("")
        btn.setObjectName("focused_mode_btn")
        self.focused_mode_btn.clicked.connect(self.toggle_focused_mode)
        icon = QtGui.QIcon()
        icon.addPixmap(QtGui.QPixmap(":/focus_on.png"), QtGui.QIcon.Mode.Normal, QtGui.QIcon.State.Off)
        btn.setIcon(icon)
        btn.setToolTip("Toggle focused mode")
        hidden_box.addWidget(self.focused_mode_btn)

        self.delta_btn = btn = QtWidgets.QPushButton("")
        btn.setObjectName("delta_btn")
        self.delta_btn.clicked.connect(self.toggle_region_values_display_mode)
        icon = QtGui.QIcon()
        icon.addPixmap(QtGui.QPixmap(":/delta_on.png"), QtGui.QIcon.Mode.Normal, QtGui.QIcon.State.Off)
        btn.setIcon(icon)
        btn.setToolTip("Toggle region values display mode")
        hidden_box.addWidget(self.delta_btn)

        self.bookmark_btn = btn = QtWidgets.QPushButton("")
        btn.setObjectName("bookmark_btn")
        btn.clicked.connect(self.toggle_bookmarks)
        icon = QtGui.QIcon()
        icon.addPixmap(QtGui.QPixmap(":/bookmark_on.png"), QtGui.QIcon.Mode.Normal, QtGui.QIcon.State.Off)
        btn.setIcon(icon)
        btn.setToolTip("Toggle bookmarks")
        hidden_box.addWidget(btn)

        hbox.addStretch()

        # Die Listen stehen untereinander in der linken Spalte, je eine
        # pro Strip. Die Hoehen werden mit dem Strip-Splitter rechts
        # gekoppelt, damit jede Liste auf Hoehe ihres Strips sitzt.
        self.list_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.list_splitter.setOpaqueResize(False)
        self.list_splitter.setHandleWidth(4)
        self.list_splitter.setChildrenCollapsible(False)
        # Die Kanalliste wandert nicht direkt in den Splitter, sondern in
        # ein Panel mit eigener Knopfreihe - siehe _register_strip.
        vbox.addWidget(self.list_splitter)

        # Die Zeitanzeige steht unter der Kanalliste und damit genau
        # links neben der Zeitachse, die nur noch der unterste Strip
        # zeigt. align_columns gibt ihr die Hoehe dieser Achse, damit
        # Text und Achse auf einer Zeile liegen.
        vbox.addWidget(self.cursor_info)

        if timebase_overview:
            self.timebase_overview = TimevaseOverview()
            self.timebase_overview.range_moved.connect(self.move_x_range)

        self.range_proxy = pg.SignalProxy(self.plot.range_modified, rateLimit=16, slot=self.range_modified)
        # self.plot.range_modified.connect(self.range_modified)
        self.plot.range_removed.connect(self.range_removed)
        self.plot.range_modified_finished.connect(self.range_modified_finished)
        self.plot.cursor_removed.connect(self.cursor_removed)
        self.plot.current_uuid_changed.connect(self.current_uuid_changed)

        self.cursor_proxy = pg.SignalProxy(self.plot.cursor_moved, rateLimit=16, slot=self.cursor_moved)
        # self.plot.cursor_moved.connect(self.cursor_moved)
        self.plot.cursor_move_finished.connect(self.cursor_move_finished)
        self.plot.xrange_changed.connect(self.xrange_changed)
        self.plot.computation_channel_inserted.connect(self.computation_channel_inserted)
        self.plot.curve_clicked.connect(self.curve_clicked)
        self._visible_entries = set()
        self.visible_entries_modified = False
        self.lock = Lock()
        self._visible_items = {}
        self._item_cache = {}

        self._prev_region = None

        # --- Strips -------------------------------------------------------
        # Die Zeichenflaeche haengt nicht mehr direkt im Splitter, sondern in
        # einem senkrechten Splitter, der spaeter mehrere Strips halten kann.
        # self.plot zeigt immer auf den AKTIVEN Strip - dadurch bleiben die
        # rund 180 bestehenden self.plot-Zugriffe unveraendert gueltig.
        self.strips = [self.plot]
        self.plot.viewport().installEventFilter(self)
        self._syncing_strip_cursor = False
        self._syncing_strip_heights = False
        self._syncing_strip_region = False
        self._drop_choice_strip = None
        self._drop_choice_before = set()
        self._drop_choice_offset = 0
        self._drag_dwell_strip = None
        self._drag_dwell_pos = None
        self._drag_dwell_start = 0.0
        self._strip_hint = None
        self._hint_timer = None
        if self.plot.cursor1 is not None:
            self.plot.cursor_moved.connect(self._strip_cursor_moved)
        self._strip_cursor_style = {
            "show_circle": show_cursor_circle,
            "show_horizontal_line": show_cursor_horizontal_line,
            "line_width": cursor_line_width,
            "color": cursor_color,
        }
        self._strip_kwargs = {
            "with_dots": with_dots,
            "line_interconnect": self.line_interconnect,
            "origin": origin,
            "mdf": self.mdf,
            "x_axis": x_axis,
            "allow_cursor": allow_cursor,
        }
        self.strip_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.strip_splitter.setOpaqueResize(False)
        self.strip_splitter.setHandleWidth(4)
        self.strip_splitter.setChildrenCollapsible(False)
        self._register_strip(self.plot, self.channel_selection)
        self._wire_strip_region(self.plot)
        self.strip_splitter.addWidget(self.plot.strip_panel)
        self.list_splitter.addWidget(self.plot.list_panel)

        # Die rechte Spalte bekommt oben und unten genau den Rand, den
        # die linke durch ihre uebrigen Felder hat. Nur wenn beide
        # Splitter gleich hoch sind UND an derselben Stelle beginnen,
        # liegen die Zeilen auf einer Linie: gleiche Groessen allein
        # genuegen nicht, weil ein Splitter uebrige Hoehe immer auf
        # seine Felder verteilt - daher lief der Versatz nach unten hin
        # auf, gemessen 3, 5, 12, 18, 26 Punkte.
        self._left_pane = widget
        self._strip_top_spacer = QtWidgets.QWidget()
        self._strip_top_spacer.setFixedHeight(0)
        self._strip_bottom_spacer = QtWidgets.QWidget()
        self._strip_bottom_spacer.setFixedHeight(0)

        right = QtWidgets.QWidget()
        rbox = QtWidgets.QVBoxLayout(right)
        rbox.setContentsMargins(0, 0, 0, 0)
        rbox.setSpacing(0)
        rbox.addWidget(self._strip_top_spacer)
        rbox.addWidget(self.strip_splitter)
        rbox.addWidget(self._strip_bottom_spacer)

        self.splitter.addWidget(right)

        self.strip_splitter.splitterMoved.connect(lambda *_: self._sync_strip_heights(self.strip_splitter))
        self.list_splitter.splitterMoved.connect(lambda *_: self._sync_strip_heights(self.list_splitter))

        self.info = ChannelStats(
            self.x_unit,
            precision=self._settings.value("stats_float_precision", 6, type=int),
        )
        self.info.hide()
        self.info.precision_modified.connect(self.info_precision_modified)
        self.splitter.addWidget(self.info)

        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)

        self.plot.add_channels_request.connect(self.add_channels_request)
        self.plot.edit_channel_request.connect(self.edit_channel_request)
        self.setAcceptDrops(True)

        main_layout.addWidget(self.splitter)
        if self.timebase_overview is not None:
            main_layout.addWidget(self.timebase_overview)

        self.show()
        size = sum(self.splitter.sizes())
        self.splitter.setSizes([600, max(size - 600, 1)])

        nameColumnWidth = 5 * self.font().pointSize()
        if signals:
            nameColumnWidth = max([len(signal.name) + 10 for signal in signals])

        unitColumnWidth = 3 * self.font().pointSize()
        if signals:
            unitColumnWidth = max([len(signal.unit) + 10 for signal in signals])

        # Nur setzen, wenn keine gemerkten Breiten vorliegen - sonst
        # ueberschreibt dieser Vorgabewert jedes Mal die eingestellten.
        if not self.channel_selection.user_header_sizes:
            self.channel_selection.set_header_sizes(
                {
                    "NameColumn": nameColumnWidth,
                    "ValueColumn": 83,
                    "UnitColumn": unitColumnWidth,
                }
            )

        self.hide()

        if signals:
            self.add_new_channels(signals)

        self._wire_channel_list(self.channel_selection, self.plot)

        self.disabled_keyboard_events = set()

        # Wurde die Breite der linken Spalte von Hand oder aus einer
        # Konfiguration gesetzt? Dann nicht mehr automatisch anpassen.
        self._user_splitter = False
        self.splitter.splitterMoved.connect(self.set_splitter)

        self.toggle_focused_mode(focused=self._settings.value("plot/focused_mode", False, type=bool))
        self.toggle_region_values_display_mode(mode=self._settings.value("plot/region_values_display_mode", "value"))

        self.bookmarks = bookmarks  # bookmarks need to be set, before `toggle_bookmarks` is called

        self.toggle_bookmarks(hide=not self._settings.value("plot/bookmarks", False, type=bool))
        self.hide_axes(hide=self._settings.value("plot/hide_axes", False, type=bool))
        self.set_locked(locked=self._settings.value("plot/locked", False, type=bool))

        self.zoom_history = []
        self.zoom_history_index = -1
        self.update_zoom = False

        self.show()

    def add_new_channels(self, channels, mime_data=None, destination=None, update=True):
        initial = self.channel_selection.topLevelItemCount() == 0

        def add_new_items(tree, root, items, items_pool):
            children = []
            groups = []

            for info in items:
                pattern = info.get("pattern", None)
                uuid = info["uuid"]
                name = info["name"]
                origin_uuid = info.get("origin_uuid", "000000000000")

                ranges = copy_ranges(info["ranges"])

                if info.get("type", "channel") == "group":
                    item = ChannelsTreeItem(
                        ChannelsTreeItem.Group,
                        name=name,
                        pattern=pattern,
                        uuid=uuid,
                        origin_uuid=origin_uuid,
                    )
                    children.append(item)
                    item.set_ranges(ranges)

                    groups.extend(add_new_items(tree, item, info["channels"], items_pool))
                    groups.append((item, info))

                else:
                    if uuid in items_pool:
                        item = items_pool[uuid]
                        children.append(item)

                        del items_pool[uuid]

                if root and root.type() == ChannelsTreeItem.Group and root.pattern:
                    y_range = root.pattern.get("y_range", (0, 100))

                    item.y_range = y_range
                    if item.uuid == self.plot.current_uuid:
                        self.plot.viewbox.setYRange(*y_range, padding=0)

            if root is None:
                root = self.channel_selection.invisibleRootItem()
                root.addChildren(children)
            else:
                if root.type() == ChannelsTreeItem.Group:
                    root.addChildren(children)
                else:
                    parent = root.parent() or self.channel_selection.invisibleRootItem()
                    index = parent.indexOfChild(root)
                    parent.insertChildren(index, children)

            return groups

        self.plot._can_paint = False

        descriptions = get_descriptions_by_uuid(mime_data)

        invalid = []

        can_trim = True
        for channel in channels.values():
            diff = np.diff(channel.timestamps)
            invalid_indexes = np.argwhere(diff <= 0).ravel()
            if len(invalid_indexes):
                invalid_indexes = invalid_indexes[:10] + 1
                idx = invalid_indexes[0]
                ts = channel.timestamps[idx - 1 : idx + 2]
                invalid.append(f"{channel.name} @ index {invalid_indexes[:10] - 1} with first time stamp error: {ts}")
                if len(np.argwhere(diff < 0).ravel()):
                    can_trim = False

        if invalid:
            errors = "\n".join(invalid)
            try:
                mdi_title = self.parent().windowTitle()
                title = f"plot <{mdi_title}>"
            except:
                title = "plot window"

            MessageBox.warning(
                self,
                f"Channels with corrupted time stamps added to {title}",
                f"The following channels do not have monotonous increasing time stamps:\n{errors}",
            )
            self.plot._can_trim = can_trim or True  # allow it for now

        valid = {}
        invalid = []
        for uuid, channel in channels.items():
            if len(channel):
                samples = channel.samples
                if samples.dtype.kind not in "SUV" and np.all(np.isnan(samples)):
                    invalid.append(channel.name)
                elif channel.conversion:
                    samples = channel.physical(copy=False).samples
                    if samples.dtype.kind not in "SUV" and np.all(np.isnan(samples)):
                        invalid.append(channel.name)
                    else:
                        valid[uuid] = channel
                else:
                    valid[uuid] = channel
            else:
                valid[uuid] = channel

        if invalid:
            MessageBox.warning(
                self,
                "All NaN channels will not be plotted:",
                f"The following channels have all NaN samples and will not be plotted:\n{', '.join(invalid)}",
            )

        channels = valid

        channels = self.plot.add_new_channels(channels, descriptions=descriptions)

        enforce_y_axis = False
        iterator = QtWidgets.QTreeWidgetItemIterator(self.channel_selection)
        while item := iterator.value():
            if item.type() == item.Channel:
                if item.checkState(item.CommonAxisColumn) == QtCore.Qt.CheckState.Unchecked:
                    enforce_y_axis = False
                    break
                else:
                    enforce_y_axis = True

            iterator += 1

        children = []

        if self._settings.value("interface/current_theme") == "Dark":
            background_color = QtGui.QColor(0, 0, 0)
        else:
            background_color = QtGui.QColor(255, 255, 255)

        new_items = {}

        items_map = {}
        for sig_uuid, sig in channels.items():
            description = descriptions.get(sig_uuid, {})

            if description:
                sig.format = description.get("format", "phys")
            sig.mode = description.get("mode", "phys")

            if "comment" in description:
                sig.comment = description["comment"] or ""
                sig.flags |= Signal.Flags.user_defined_comment

            item = ChannelsTreeItem(
                ChannelsTreeItem.Channel,
                signal=sig,
                check=QtCore.Qt.CheckState.Checked if sig.enable else QtCore.Qt.CheckState.Unchecked,
            )

            if len(sig):
                raw_value, raw_kind, value, kind, fmt = sig.value_at_timestamp(sig.timestamps[0])
                item.kind = kind
                item._value = "n.a."
                item.set_value(raw_value, value, force=True, update=True)

            if mime_data is None:
                children.append(item)
            else:
                new_items[sig_uuid] = item
            items_map[sig_uuid] = item

            ranges = description.get("ranges", [])
            if not ranges and self.pattern:
                ranges = self.pattern["ranges"]
            item.set_ranges(copy_ranges(ranges))

            self.info_uuid = sig_uuid

        if mime_data:
            destination = destination or self.channel_selection.drop_target

            groups = add_new_items(
                self.channel_selection,
                destination,
                mime_data,
                new_items,
            )

            if groups:
                self.channel_selection.blockSignals(True)

                for item, info in groups:
                    item.setExpanded(info.get("expanded", False))
                    if item.pattern:
                        item.setCheckState(
                            item.NameColumn,
                            QtCore.Qt.CheckState.Checked if info["enabled"] else QtCore.Qt.CheckState.Unchecked,
                        )
                    else:
                        if not item.childCount():
                            item.setCheckState(
                                item.NameColumn,
                                QtCore.Qt.CheckState.Checked if info["enabled"] else QtCore.Qt.CheckState.Unchecked,
                            )
                    if info.get("disabled", False):
                        item.set_disabled(info["disabled"])

                self.channel_selection.blockSignals(False)
                self.channel_selection.refresh()

            # still have simple signals to add
            if new_items:
                self.channel_selection.addTopLevelItems(list(new_items.values()))

        elif children:
            destination = destination or self.channel_selection.drop_target

            if destination is None:
                self.channel_selection.addTopLevelItems(children)
            else:
                if destination.type() == ChannelsTreeItem.Group:
                    destination.addChildren(children)
                    if destination.pattern:
                        y_range = destination.pattern.get("y_range", (0, 100))
                        for child in children:
                            self._strip_of(child.uuid).set_y_range(child.uuid, y_range, emit=False)

                        if child.uuid == self.plot.current_uuid:
                            self.plot.viewbox.setYRange(*y_range, padding=0)
                else:
                    parent = destination.parent() or self.channel_selection.invisibleRootItem()
                    index = parent.indexOfChild(destination)
                    parent.insertChildren(index, children)

        for sig_uuid, sig in channels.items():
            description = descriptions.get(sig_uuid, {})
            item = items_map[sig_uuid]
            if description:
                individual_axis = description.get("individual_axis", False)
                if individual_axis:
                    item.setCheckState(item.IndividualAxisColumn, QtCore.Qt.CheckState.Checked)

                    strip, _sig, idx = self.strip_signal_by_uuid(sig_uuid)
                    axis = strip.get_axis(idx)
                    if isinstance(axis, FormatedAxis):
                        axis.setWidth(description["individual_axis_width"])

                if description.get("common_axis", False):
                    item.setCheckState(item.CommonAxisColumn, QtCore.Qt.CheckState.Checked)

                if description.get("y_locked", False):
                    item.setCheckState(item.LockColumn, QtCore.Qt.CheckState.Checked)

                breite = description.get("line_width", 0)
                if breite:
                    item.signal.line_width = int(breite)

                stil = description.get("line_style", "")
                if stil in LINE_STYLES:
                    item.signal.line_style = stil

                item.precision = description.get("precision", 3)

                if description.get("conversion", None):
                    conversion = from_dict(description["conversion"])
                    item.signal.flags |= Signal.Flags.user_defined_conversion
                    item.set_conversion(conversion)

                if description.get("user_defined_name", None):
                    item.name = description["user_defined_name"]

            # Hat der Benutzer fuer diesen Kanalnamen Nachkommastellen
            # eingestellt, gelten sie auch fuer neue Vorkommen.
            precision = CHANNEL_PRECISION.get(sig.name, None)
            if precision is not None and item.precision != precision:
                item.precision = precision

            if enforce_y_axis:
                item.setCheckState(item.CommonAxisColumn, QtCore.Qt.CheckState.Checked)

        if update:
            self.channel_selection.update_channel_groups_count()
            self.channel_selection.refresh()

        self.update_timebase_overview()

        # Neue Kanaele in einem XY-Strip: Achse und Markierung nachziehen.
        for strip in self.strips:
            if getattr(strip, "xy_mode", False):
                strip._xy_cache = {}
                self.apply_xy_range(strip)
                self.update_strip_marks(strip)

        self.adjust_splitter(initial=initial)
        self.current_uuid_changed(self.plot.current_uuid)
        self.plot._can_paint = True
        self.plot.update()

        # Kommt die Liste aus einer Konfiguration, haengt an jedem Kanal
        # seine Stripnummer - dann wird jetzt wieder aufgeteilt.
        pending = getattr(self, "_pending_strip_count", 0)
        self._pending_strip_count = 0
        self.apply_strip_assignment(descriptions, pending)

        for strip in self.strips:
            self.layout_digital_strip(strip)

    # ------------------------------------------------------------------
    # Strips (Subplots innerhalb dieses Fensters)
    # ------------------------------------------------------------------
    def add_strip(self, index=None):
        """Einen weiteren Strip anlegen und seine Zeichenflaeche zurueckgeben.

        Ein Strip besteht aus einer Zeichenflaeche und einer eigenen
        Kanalliste daneben.
        """
        plot = PlotGraphics(parent=self, plot_parent=self, **self._strip_kwargs)
        plot.zoom_changed.connect(self.zoom_changed)
        plot.bookmarks_changed.connect(self.bookmarks_changed)
        plot.add_channels_request.connect(self.add_channels_request)
        plot.edit_channel_request.connect(self.edit_channel_request)

        if index is None:
            index = len(self.strips)

        self._wire_strip_region(plot)

        if plot.cursor1 is not None:
            # Aussehen des Cursors wie im ersten Strip
            for name, value in getattr(self, "_strip_cursor_style", {}).items():
                setattr(plot.cursor1, name, value)

            plot.cursor_moved.connect(self._strip_cursor_moved)

        plot.viewport().installEventFilter(self)

        tree = ChannelsTreeWidget(parent=self, plot=self, **self._channel_list_options)
        self._wire_channel_list(tree, plot)
        self._register_strip(plot, tree)

        self.strips.insert(index, plot)
        self.strip_splitter.insertWidget(index, plot.strip_panel)
        self.list_splitter.insertWidget(index, plot.list_panel)

        # Lesezeichen des Fensters auch in den neuen Strip
        source = self.plot
        if source is not None and source is not plot and source.bookmarks:
            plot.bookmarks = [bookmark.copy() for bookmark in source.bookmarks]

            for bookmark in plot.bookmarks:
                bookmark.visible = self.show_bookmarks

        self._relink_strips()
        self._equalize_strips()
        plot._can_paint = True
        return plot

    def remove_strip(self, strip):
        """Einen Strip entfernen. Der letzte bleibt immer stehen."""
        if len(self.strips) <= 1 or strip not in self.strips:
            return False

        self.strips.remove(strip)

        tree = getattr(strip, "channel_list", None)
        if tree is not None:
            tree.blockSignals(True)

        for name in ("list_panel", "strip_panel"):
            panel = getattr(strip, name, None)
            if panel is not None:
                panel.setParent(None)
                panel.deleteLater()

        strip.setParent(None)
        strip.close()

        if self.plot is strip:
            self.set_active_strip(self.strips[0])

        self._relink_strips()
        self._equalize_strips()
        return True

    def move_strip(self, strip, offset):
        """Strip nach oben (-1) oder unten (+1) schieben."""
        if strip not in self.strips:
            return False
        old = self.strips.index(strip)
        new = old + offset
        if not 0 <= new < len(self.strips):
            return False

        self.strips.insert(new, self.strips.pop(old))
        self.strip_splitter.insertWidget(new, strip.strip_panel)
        self.list_splitter.insertWidget(new, strip.list_panel)
        self._relink_strips()
        return True

    def set_active_strip(self, strip):
        """Legt fest, welcher Strip ab jetzt self.plot ist.

        Neu hinzugefuegte Kanaele landen immer im aktiven Strip, weil
        add_new_channels auf self.plot arbeitet.
        """
        if strip in self.strips:
            self.plot = strip
            self.channel_selection = strip.channel_list
            self._mark_active_strip()

    def _mark_active_strip(self):
        """Den aktiven Strip mit einem duennen Rahmen kennzeichnen.

        Nur bei mehr als einem Strip - sonst waere der Rahmen nur Unruhe.
        """
        # Der aktive Strip wird nicht mehr hervorgehoben: Kanaele landen
        # dort, wo man sie fallen laesst, und die Knoepfe gehoeren ohnehin
        # sichtbar zu ihrem Strip. Die Markierung war nur Unruhe.
        for strip in self.strips:
            strip.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

            tree = getattr(strip, "channel_list", None)
            if tree is not None:
                tree.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

                # Die Listen der anderen Strips zeigen ihre Markierung
                # gedaempft, damit immer zu sehen ist, wo man zuletzt
                # gearbeitet hat.
                aktiv = strip is self.plot
                if getattr(tree, "strip_active", None) != aktiv:
                    tree.strip_active = aktiv
                    tree.viewport().update()

    def move_x_range(self, start, stop):
        """Alle Strips auf diesen Zeitbereich schieben.

        Kommt von der Zeitleiste unten. Der erste Strip genuegt, die
        anderen haengen ueber den x-Verbund daran.
        """
        if not getattr(self, "strips", None):
            return

        self.strips[0].viewbox.setXRange(start, stop, padding=0)

    def update_timebase_overview(self):
        """Die Zeitleiste unten auf die Messdauer ALLER Strips stellen.

        Vorher zaehlte nur der gerade aktive Strip. War der beim Laden
        noch leer - etwa weil die Kanaele erst danach auf die anderen
        Strips verteilt wurden -, bekam die Leiste nie eine Zeitbasis
        und blieb leer. Die gemeinsame Zeitbasis eines Strips wird
        ausserdem nicht neu gerechnet, wenn ein Signal nachtraeglich in
        ihn hineinwandert - deshalb zaehlen notfalls die Zeitstempel
        der Signale selbst.
        """
        leiste = getattr(self, "timebase_overview", None)
        if leiste is None:
            return

        anfang = ende = None

        def merken(von, bis):
            nonlocal anfang, ende
            anfang = von if anfang is None else min(anfang, von)
            ende = bis if ende is None else max(ende, bis)

        for strip in getattr(self, "strips", None) or ():
            timebase = getattr(strip, "all_timebase", None)

            if timebase is not None and len(timebase):
                merken(timebase[0], timebase[-1])
                continue

            for signal in getattr(strip, "signals", None) or ():
                stempel = getattr(signal, "timestamps", None)
                if stempel is not None and len(stempel):
                    merken(stempel[0], stempel[-1])

        if anfang is not None and ende > anfang:
            leiste.timebase = anfang, ende

    def _update_strip_axes(self):
        """Die Zeitachse nur unter dem untersten Strip zeigen.

        So macht es vSignalyzer: eine einzige Zeitskala in einem eigenen
        Streifen ganz unten. Die Strips darueber gewinnen dadurch jeder
        die Hoehe einer Achse an Zeichenflaeche. Versteckt man eine
        Achse, setzt pyqtgraph ihre Hoehe selbst auf null - der Platz
        verfaellt also nicht.
        """
        btn = getattr(self, "hide_axes_btn", None)
        if not getattr(self, "strips", None) or btn is None:
            return

        versteckt = btn.isFlat()

        # Die eine Zeitachse gehoert unter den untersten Strip, der
        # ueberhaupt die Zeit zeigt. Ist der unterste ein XY-Strip,
        # bekommt sie der letzte Zeitstrip darueber - sonst haette das
        # Fenster gar keine Zeitachse mehr.
        zeitstrips = [strip for strip in self.strips if not getattr(strip, "xy_mode", False)]
        letzter = zeitstrips[-1] if zeitstrips else None

        for strip in self.strips:
            # Ein XY-Strip zeigt auf der x-Achse einen Signalwert - der
            # passt nicht zur gemeinsamen Zeitachse, also bekommt er
            # immer seine eigene.
            if versteckt:
                strip.plotItem.hideAxis("bottom")
            elif getattr(strip, "xy_mode", False) or strip is letzter:
                strip.plotItem.showAxis("bottom")
            else:
                strip.plotItem.hideAxis("bottom")

        # Jede Aktion bekommt ihr eigenes Nachmess-Budget.
        self._column_passes = 0
        self.align_columns()
        self._schedule_column_sync()

    def bottom_axis_height(self):
        """Hoehe der einen Zeitachse unten - null, wenn keine da ist."""
        if not getattr(self, "strips", None):
            return 0

        # Fuer die Zeitanzeige links unten zaehlt nur eine Achse am
        # UNTERSTEN Strip: die eines XY-Strips weiter oben sitzt mitten
        # im Fenster und laesst unten keinen Platz frei.
        axis = getattr(self.strips[-1], "x_axis", None)
        if axis is None or not axis.isVisible():
            return 0

        return int(axis.height())

    def align_columns(self):
        """Beide Spalten auf denselben senkrechten Ausschnitt bringen.

        Links stehen ueber und unter den Listen noch andere Felder, die
        rechte Spalte hat sie nicht. Dadurch sind die beiden Splitter
        verschieden hoch, und dieselbe Groessenvorgabe ergibt
        verschiedene Zeilenhoehen. Die Rahmen hier gleichen das aus.

        Unten ist eine Besonderheit: dort sitzt rechts die Zeitachse des
        untersten Strips, links die Zeitanzeige. Beide bekommen dieselbe
        Hoehe und liegen damit auf einer Zeile - der Text steht links
        neben der Achse, und keine Spalte laesst Platz frei, den die
        andere gar nicht braucht.
        """
        pane = getattr(self, "_left_pane", None)
        oben_spacer = getattr(self, "_strip_top_spacer", None)
        unten_spacer = getattr(self, "_strip_bottom_spacer", None)

        if pane is None or oben_spacer is None or unten_spacer is None:
            return

        if not self.list_splitter.isVisible() or pane.height() < 10:
            return

        geaendert = False

        achse = self.bottom_axis_height()
        info = getattr(self, "cursor_info", None)
        if info is not None:
            hoehe = max(achse, info.sizeHint().height())
            if info.height() != hoehe:
                info.setFixedHeight(hoehe)
                geaendert = True

        oben = self.list_splitter.mapTo(pane, QtCore.QPoint(0, 0)).y()
        unten = pane.height() - oben - self.list_splitter.height()

        # Die Achse rechts deckt die Zeitanzeige links ab; nur was
        # darueber hinausgeht, muss die rechte Spalte frei lassen.
        unten = max(unten - achse, 0)

        if oben >= 0 and oben_spacer.height() != oben:
            oben_spacer.setFixedHeight(oben)
            geaendert = True

        if unten >= 0 and unten_spacer.height() != unten:
            unten_spacer.setFixedHeight(unten)
            geaendert = True

        if geaendert:
            self._schedule_column_sync()

    def _schedule_column_sync(self):
        """Nach einer Randaenderung die Strip-Hoehen neu angleichen.

        Ein geaenderter Rand aendert die Hoehe des Splitters, und der
        verteilt seine neue Hoehe von selbst auf alle Felder. Erst wenn
        das durch ist, kann man die beiden Spalten wieder aufeinander
        legen - messen wir vorher, bekommen wir die alten Werte und der
        Versatz bleibt stehen. Darum verzoegert.
        """
        timer = getattr(self, "_column_timer", None)

        if timer is None:
            timer = self._column_timer = QtCore.QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._column_sync_tick)

        timer.start(0)

    def _column_sync_tick(self):
        """Raender nachmessen, dann die Hoehen angleichen.

        Eine frisch eingeblendete Zeitachse ist beim ersten Messen noch
        null hoch - erst die Anordnung gibt ihr ihre Hoehe. Deshalb wird
        hier noch einmal gemessen; aendert sich dabei etwas, plant
        align_columns von selbst den naechsten Durchgang. Die Schranke
        verhindert, dass sich zwei Werte gegenseitig aufschaukeln.
        """
        durchgang = getattr(self, "_column_passes", 0)

        if durchgang < 6:
            self._column_passes = durchgang + 1
            self.align_columns()

        self._sync_strip_heights(self.strip_splitter, align=False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._column_passes = 0
        self.align_columns()
        # Beide Splitter verteilen eine neue Fensterhoehe jeder fuer
        # sich. Weil sie um die Zeitachse verschieden hoch sind, fallen
        # die Abschnitte dabei verschieden aus - danach also angleichen.
        self._schedule_column_sync()

    def log_strip_geometry(self, reason=""):
        """Oberkanten beider Spalten ausmessen und ausgeben.

        Links die Kanalliste mit ihrem Spaltenkopf, rechts die
        Knopfreihe mit der Zeichenflaeche darunter. Alles in
        Bildschirmkoordinaten, damit die Werte direkt vergleichbar sind.
        """
        if not STRIP_DEBUG:
            return

        def oben(widget):
            return widget.mapToGlobal(QtCore.QPoint(0, 0)).y() if widget is not None else -1

        print(f"[strip] ===== {reason} =====", flush=True)
        print(
            f"[strip] fenster={oben(self)} "
            f"listen_splitter={oben(self.list_splitter)} strip_splitter={oben(self.strip_splitter)}",
            flush=True,
        )

        for index, strip in enumerate(self.strips):
            tree = getattr(strip, "channel_list", None)
            header = tree.header() if tree is not None else None
            panel = getattr(strip, "list_panel", None)
            frame = getattr(strip, "strip_panel", None)

            bar = None
            if frame is not None and frame.layout() is not None and frame.layout().count():
                bar = frame.layout().itemAt(0).widget()

            print(
                f"[strip] {index}: "
                f"listenrahmen={oben(panel)} baum={oben(tree)} "
                f"kopf={oben(header)}+{header.height() if header else 0} "
                f"rahmen={tree.frameWidth() if tree is not None else 0} "
                f"| plotrahmen={oben(frame)} "
                f"knopfreihe={oben(bar)}+{bar.height() if bar else 0} "
                f"plot={oben(strip)} "
                f"| versatz={oben(bar) - oben(header) if (bar is not None and header is not None) else 0}",
                flush=True,
            )

    def _equalize_strips(self):
        """Allen Strips die gleiche Hoehe geben.

        Ohne das bekommt ein frisch eingefuegter Strip vom Splitter unter
        Umstaenden die Hoehe null und ist unsichtbar - es sieht dann aus,
        als waere nichts passiert.
        """
        count = len(self.strips)
        if count < 2:
            return

        total = sum(self.strip_splitter.sizes()) or self.strip_splitter.height()
        if total > 0:
            self.strip_splitter.setSizes([max(total // count, 1)] * count)

        # Die Liste folgt den tatsaechlichen Hoehen der Zeichenflaechen,
        # statt sich dieselbe Rechnung noch einmal selbst zu machen.
        self._sync_strip_heights(self.strip_splitter)

    def _strip_for_object(self, obj):
        """Zu welchem Strip gehoert dieses Qt-Objekt?"""
        for strip in self.strips:
            tree = getattr(strip, "channel_list", None)
            panels = (getattr(strip, "list_panel", None), getattr(strip, "strip_panel", None))

            if (
                obj is strip.viewport()
                or (tree is not None and obj is tree.viewport())
                or any(panel is not None and obj is panel for panel in panels)
            ):
                return strip

        return None

    def _remember_selection(self, tree):
        """Markierte Kanaele einer Liste festhalten.

        Eine leere Auswahl loescht die Erinnerung nicht - genau dann
        soll sie ja wieder gelten.
        """
        uuids = [item.uuid for item in tree.selectedItems() if item.type() == ChannelsTreeItem.Channel]

        if uuids:
            tree.last_selected_uuids = uuids

    def ensure_strip_selection(self, strip):
        """Dem Strip eine Markierung geben, falls keine da ist.

        Farbe, Y-Bereich, Loeschen und viele andere Befehle arbeiten auf
        den markierten Zeilen der Kanalliste. Klickt man nur in die
        Zeichenflaeche, ist dort nichts markiert und der Befehl tut
        scheinbar nichts. Dann gelten die zuletzt markierten Zeilen,
        und solange es keine gab, die erste der Liste.
        """
        tree = getattr(strip, "channel_list", None)
        if tree is None or tree.selectedItems():
            return

        merke = list(getattr(tree, "last_selected_uuids", None) or [])
        treffer = []
        erste = None

        iterator = QtWidgets.QTreeWidgetItemIterator(tree)
        while item := iterator.value():
            iterator += 1

            if item.isHidden() or item.type() != ChannelsTreeItem.Channel:
                continue

            if erste is None:
                erste = item

            if getattr(item, "uuid", None) in merke:
                treffer.append(item)

        if not treffer:
            if erste is None:
                return
            treffer = [erste]

        tree.setCurrentItem(treffer[0])
        for item in treffer:
            item.setSelected(True)

    def eventFilter(self, obj, event):
        """Klicks und Ziehvorgaenge der Strips mitlesen.

        Die Ereignisse gehen an die Anzeigeflaeche der Strips. Hier
        liegen sie vor deren eigenen Handlern, deshalb laeuft die
        Auswahl des Zielstrips zuverlaessig an dieser Stelle.
        """
        kind = event.type()

        if kind in (
            QtCore.QEvent.Type.MouseButtonPress,
            QtCore.QEvent.Type.DragEnter,
            QtCore.QEvent.Type.Drop,
        ):
            strip = self._strip_for_object(obj)
            if strip is not None:
                if strip is not self.plot:
                    self.set_active_strip(strip)

                # In der Kanalliste entscheidet der Klick selbst, was
                # markiert ist. In der Zeichenflaeche markiert Qt nichts
                # - dort gilt die letzte Markierung des Strips.
                tree = getattr(strip, "channel_list", None)
                if (
                    kind == QtCore.QEvent.Type.MouseButtonPress
                    and tree is not None
                    and obj is not tree.viewport()
                    and obj is not getattr(strip, "list_panel", None)
                ):
                    self.ensure_strip_selection(strip)

        return super().eventFilter(obj, event)

    def _strip_uuids(self, strip):
        """uuids aller Kanaele eines Strips."""
        uuids = set()
        tree = getattr(strip, "channel_list", None)

        if tree is None:
            return uuids

        iterator = QtWidgets.QTreeWidgetItemIterator(tree)
        while item := iterator.value():
            iterator += 1
            if item.type() == ChannelsTreeItem.Channel:
                uuids.add(item.uuid)

        return uuids

    def schedule_strip_choice(self, strip, offset):
        """Die gerade abgelegten Kanaele in einen neuen Strip umhaengen.

        Erst nach dem Abwurf, weil die Kanaele vorher noch nicht da sind.
        """
        if strip is None or strip not in self.strips or not offset:
            return

        self._drop_choice_strip = strip
        self._drop_choice_offset = offset
        self._drop_choice_before = self._strip_uuids(strip)
        QtCore.QTimer.singleShot(250, self._move_dropped_to_new_strip)

    def _move_dropped_to_new_strip(self):
        """Die neu hinzugekommenen Kanaele in einen neuen Strip legen."""
        strip = getattr(self, "_drop_choice_strip", None)
        before = getattr(self, "_drop_choice_before", set())
        offset = getattr(self, "_drop_choice_offset", 0)

        self._drop_choice_strip = None
        self._drop_choice_before = set()
        self._drop_choice_offset = 0

        if strip is None or strip not in self.strips or not offset:
            return

        tree = getattr(strip, "channel_list", None)
        if tree is None:
            return

        items = []
        iterator = QtWidgets.QTreeWidgetItemIterator(tree)
        while item := iterator.value():
            iterator += 1
            if item.type() == ChannelsTreeItem.Channel and item.uuid not in before:
                items.append(item)

        if not items:
            return

        index = self.strips.index(strip) + (0 if offset < 0 else 1)
        target = self.add_strip(index)

        if not self.move_items_to_strip(tree, items, target):
            self.remove_strip(target)
        else:
            self.set_active_strip(target)

    def add_strip_action(self):
        """Neuen Strip unter dem aktiven anlegen und dorthin wechseln."""
        index = self.strips.index(self.plot) + 1 if self.plot in self.strips else None
        self.set_active_strip(self.add_strip(index))

    def move_strip_action(self, offset):
        self.move_strip(self.plot, offset)

    def delete_strip_action(self):
        self.remove_strip(self.plot)

    def move_signals_between_strips(self, source_tree, target_strip, destination=None):
        """Markierte Eintraege aus einem Strip in einen anderen verschieben.

        Ohne das legt der Drag&Drop im Zielstrip eine zweite Kopie an und
        das Signal steht danach in beiden Strips.
        """
        items = validate_drag_items(source_tree.invisibleRootItem(), source_tree.selectedItems(), [])

        return self.move_items_to_strip(source_tree, items, target_strip, destination=destination)

    def move_items_to_strip(self, source_tree, items, target_strip, destination=None):
        """Bestimmte Eintraege in einen anderen Strip umhaengen."""
        source_strip = getattr(source_tree, "owner_plot", None)
        target_tree = getattr(target_strip, "channel_list", None)

        if source_strip is None or target_tree is None or source_strip is target_strip:
            return False

        if not items:
            return False

        # Gruppen mitnehmen, aber die Signale darin einzeln umhaengen.
        channel_items = []
        stack = list(items)
        while stack:
            item = stack.pop()
            if item.type() == ChannelsTreeItem.Channel:
                channel_items.append(item)
            elif item.type() == ChannelsTreeItem.Group:
                stack.extend(item.child(i) for i in range(item.childCount()))

        signals = {}
        for item in channel_items:
            pair = source_strip._uuid_map.get(item.uuid)
            if pair is not None:
                signals[item.uuid] = pair[0]

        if not signals:
            return False

        individual = [item for item in channel_items if item.signal.individual_axis]
        locked = [item for item in channel_items if item.signal.y_locked]

        source_tree.setUpdatesEnabled(False)
        target_tree.setUpdatesEnabled(False)
        source_tree.blockSignals(True)
        target_tree.blockSignals(True)
        try:
            root = source_tree.invisibleRootItem()
            for item in items:
                (item.parent() or root).removeChild(item)

            source_strip.delete_channels(list(signals))

            # Darstellung mitnehmen - sonst bekommt das Signal im neuen
            # Strip wieder den vollen Bereich und das Rohformat.
            descriptions = {
                uuid: {
                    "enabled": bool(sig.enable),
                    "format": sig.format,
                    "y_range": list(sig.y_range),
                    "y_locked": bool(sig.y_locked),
                }
                for uuid, sig in signals.items()
            }

            target_strip._can_paint = False
            new_signals = target_strip.add_new_channels(signals, descriptions=descriptions)

            for item in channel_items:
                signal = new_signals.get(item.uuid)
                if signal is not None:
                    item.signal = signal

            if destination is not None and destination.type() == ChannelsTreeItem.Group:
                destination.addChildren(items)
            else:
                target_tree.addTopLevelItems(items)
        finally:
            source_tree.blockSignals(False)
            target_tree.blockSignals(False)
            source_tree.setUpdatesEnabled(True)
            target_tree.setUpdatesEnabled(True)
            target_strip._can_paint = True

        # Eine eigene Achse ueberlebt den Umzug nicht von selbst, weil
        # PlotSignal.individual_axis beim Neuanlegen auf False steht.
        for item in individual:
            target_strip.set_individual_axis(item.uuid, True)

        for item in locked:
            target_strip.set_y_locked(item.uuid, True)

        for tree in (source_tree, target_tree):
            tree.update_channel_groups_count()
            tree.refresh()

        self.set_active_strip(target_strip)
        self._update_visibile_entries()
        source_strip.update()
        target_strip.update()

        return True

    def apply_strip_assignment(self, descriptions, strip_count=0):
        """Nach dem Laden einer Konfiguration die Strips wiederherstellen.

        Beim Speichern haengt an jedem Kanal seine Stripnummer. Geladen
        wird alles zuerst in den ersten Strip - hier wird es wieder
        auseinandersortiert.
        """
        descriptions = descriptions or {}

        tree = self.strips[0].channel_list
        root = tree.invisibleRootItem()

        pairs = []
        for i in range(root.childCount()):
            item = root.child(i)
            index = self._strip_index_of_item(item, descriptions)
            if index:
                pairs.append((item, index))

        wanted = max([index for _item, index in pairs], default=0) + 1
        wanted = max(wanted, int(strip_count or 0))

        if wanted <= 1 and not pairs:
            return

        while len(self.strips) < wanted:
            self.add_strip()

        for item, index in pairs:
            if index < len(self.strips):
                self.move_items_to_strip(tree, [item], self.strips[index])

        for strip in self.strips:
            self._restore_strip_state(strip, descriptions)

        modes = getattr(self, "_pending_strip_modes", None) or []
        digital = getattr(self, "_pending_strip_digital", None) or []
        self._pending_strip_modes = []
        self._pending_strip_digital = []

        if not modes:
            # aeltere Konfigurationen kennen nur digital ja/nein
            modes = ["digital" if state else "analog" for state in digital]

        x_channels = getattr(self, "_pending_strip_x_channels", None) or []
        self._pending_strip_x_channels = []

        for index, mode in enumerate(modes):
            if index >= len(self.strips):
                break

            strip = self.strips[index]

            # Erst das X-Signal setzen, dann umschalten: sonst nimmt der
            # Strip beim Umschalten das erste Signal der Liste.
            if mode == "xy" and index < len(x_channels):
                name = x_channels[index]
                for signal in strip.signals:
                    if signal.name == name:
                        strip.x_uuid = signal.uuid
                        break

            if mode != "analog":
                self.set_window_mode(strip, mode)

        self.set_active_strip(self.strips[0])
        self._equalize_strips()

    def _restore_strip_state(self, strip, descriptions):
        """Y-Bereiche und gemeinsame Achse aus der Konfiguration setzen.

        Beim Laden landen zuerst alle Kanaele in Strip 1 und haengen dort
        an einer einzigen gemeinsamen Achse. Deren Bereich passt nach dem
        Aufteilen nicht mehr - ein Signal mit Werten um 1 waere in einem
        Bereich von 2200 bis 2700 schlicht unsichtbar.
        """
        tree = getattr(strip, "channel_list", None)
        if tree is None:
            return

        members = []

        # Mitgliedschaft neu aufbauen, sonst zeigt der Haken in der Liste
        # etwas anderes an als der Strip tatsaechlich fuehrt.
        strip.common_axis_items.clear()

        iterator = QtWidgets.QTreeWidgetItemIterator(tree)
        while item := iterator.value():
            iterator += 1

            if item.type() != ChannelsTreeItem.Channel:
                continue

            description = descriptions.get(item.uuid, None)
            if not description:
                continue

            try:
                signal, _index = strip.signal_by_uuid(item.uuid)
            except KeyError:
                continue

            y_range = description.get("y_range", None)
            if y_range and len(y_range) == 2:
                was_locked = signal.y_locked
                signal.y_locked = False
                signal.y_range = tuple(sorted(float(e) for e in y_range))
                signal.y_locked = was_locked

            if description.get("common_axis", False):
                members.append(item.uuid)

        if members:
            # Der erste Eintrag gibt den Bereich der gemeinsamen Achse vor.
            for uuid in members:
                strip.set_common_axis(uuid, True)
        else:
            strip.common_axis_label = ""
            if strip.current_uuid is not None:
                strip.set_current_uuid(strip.current_uuid, True)

        strip.update()

    def _strip_index_of_item(self, item, descriptions):
        """Stripnummer eines Eintrags - bei Gruppen die des ersten Kanals."""
        stack = [item]
        while stack:
            current = stack.pop(0)

            description = descriptions.get(current.uuid, None)
            if description is not None and "strip" in description:
                return int(description["strip"] or 0)

            if current.type() == ChannelsTreeItem.Group:
                stack.extend(current.child(i) for i in range(current.childCount()))

        return 0

    def set_strip_sizes(self, sizes):
        """Hoehen der Strips aus der Konfiguration uebernehmen."""
        sizes = [int(e) for e in sizes or []]

        if len(sizes) != self.strip_splitter.count() or not all(sizes):
            return

        self.strip_splitter.setSizes(sizes)
        self._sync_strip_heights(self.strip_splitter)

    def _strip_set_color(self, uuid, color):
        """Farbe am besitzenden Strip setzen, nicht am aktiven."""
        strip, _sig, _idx = self.strip_signal_by_uuid(uuid)
        strip.set_color(uuid, color)

    def _strip_set_unit(self, uuid, unit):
        strip, _sig, _idx = self.strip_signal_by_uuid(uuid)
        strip.set_unit(uuid, unit)

    def _strip_set_name(self, uuid, name):
        strip, _sig, _idx = self.strip_signal_by_uuid(uuid)
        strip.set_name(uuid, name)

    def _relink_strips(self):
        """x-Achsen koppeln, Cursor koppeln, Zeitachse nur unten zeigen.

        XY-Strips bleiben aussen vor: ihre x-Achse zeigt einen Signalwert
        und darf nicht am gemeinsamen Zeitbereich haengen.
        """
        zeitstrips = [plot for plot in self.strips if not getattr(plot, "xy_mode", False)]
        master = zeitstrips[0].plotItem.vb if zeitstrips else None

        for plot in self.strips:
            vb = plot.plotItem.vb
            vb.setXLink(None)

            if master is None or vb is master or getattr(plot, "xy_mode", False):
                continue

            vb.setXRange(*master.viewRange()[0], padding=0)
            vb.setXLink(master)

        self._update_strip_axes()
        self.update_timebase_overview()
        self._number_strips()
        self._mark_active_strip()

    def _register_strip(self, plot, tree):
        """Zeichenflaeche und zugehoerige Kanalliste aneinander binden."""
        plot.channel_list = tree
        tree.owner_plot = plot
        tree.viewport().installEventFilter(self)
        plot.list_panel = self._build_list_panel(plot, tree)
        plot.strip_panel = self._build_strip_panel(plot)

        # Spaltenkopf so hoch wie die Knopfreihe nebenan: nur dann
        # beginnen Liste und Zeichenflaeche auf derselben Hoehe, und die
        # Zeilen im Digital- und Enumfenster stehen neben ihrem Namen.
        tree.header().setFixedHeight(STRIP_BAR_HEIGHT)

        # Die neue Liste bekommt gleich die Spaltenbreiten des Fensters,
        # damit nicht ein Strip aus der Reihe faellt.
        for vorhandene in self.channel_lists():
            if vorhandene is not tree and getattr(vorhandene, "user_header_sizes", False):
                tree.set_header_sizes(vorhandene.header_sizes())
                tree.user_header_sizes = True
                break

        # Zieht man die Breite der y-Skala, gilt sie im ganzen Fenster.
        achse = getattr(plot, "y_axis", None)
        if achse is not None:
            achse.width_changed.connect(self.spread_axis_width)

            for vorhandener in self.strips:
                if vorhandener is plot:
                    continue

                vorbild = getattr(vorhandener, "y_axis", None)
                if vorbild is not None:
                    achse.setWidth(vorbild.tick_width())
                    break

        # Hoehe der Zeichenflaeche und Rollstand der Liste bestimmen, wo
        # die Signalzeilen liegen - beides kann sich jederzeit aendern.
        plot.viewbox.sigResized.connect(partial(self.schedule_strip_layout, plot))
        tree.verticalScrollBar().valueChanged.connect(partial(self.schedule_strip_layout, plot))

    def _build_list_panel(self, plot, tree):
        """Kanalliste eines Strips, ohne alles weitere.

        Die Knoepfe sitzen ueber der Zeichenflaeche (siehe
        _build_strip_panel), nicht hier - so beginnen Liste und Zeilen
        auf derselben Hoehe und der Platz ueber dem Plot wird genutzt.
        """
        panel = QtWidgets.QWidget()
        panel.installEventFilter(self)
        vbox = QtWidgets.QVBoxLayout(panel)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(1)
        vbox.addWidget(tree)

        tree.setMinimumHeight(20)

        # Bewusst niedriger als die Zeichenflaeche nebenan: die unterste
        # Liste muss um die Hoehe der Zeitachse kuerzer sein koennen als
        # ihr Strip. Darf sie das nicht, holt sich der Splitter die
        # fehlenden Punkte bei den anderen Listen - und dann laufen die
        # Zeilen beider Spalten wieder auseinander.
        panel.setMinimumHeight(20)

        return panel

    def _build_strip_panel(self, plot):
        """Zeichenflaeche eines Strips mit der Knopfreihe darueber.

        Die Knoepfe wirken immer auf GENAU diesen Strip - damit ist
        eindeutig, welchen Strip "nach oben" verschiebt.
        """
        panel = QtWidgets.QWidget()
        panel.installEventFilter(self)
        vbox = QtWidgets.QVBoxLayout(panel)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(1)

        bar = QtWidgets.QWidget()
        bar.setFixedHeight(STRIP_BAR_HEIGHT)

        row = QtWidgets.QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(1)

        cmd = QtWidgets.QPushButton("Cmd")
        cmd.setMenu(self._build_strip_menu(plot))
        cmd.setToolTip("Commands for this strip")
        row.addWidget(cmd)

        buttons = (
            ("▲", "Move this strip up", lambda: self.move_strip(plot, -1), ""),
            ("▼", "Move this strip down", lambda: self.move_strip(plot, 1), ""),
            ("×", "Delete this strip", lambda: self.remove_strip(plot), "color: #d04030; font-weight: bold;"),
        )

        for text, tip, slot, style in buttons:
            btn = QtWidgets.QPushButton(text)
            btn.setToolTip(tip)
            btn.setFixedWidth(24)
            if style:
                btn.setStyleSheet(style)
            btn.clicked.connect(slot)
            row.addWidget(btn)

        # Die vier Umschalter, die als Knopf sichtbar bleiben sollen.
        # Ihren Zustand (Symbol, flach/erhaben) halten weiterhin die
        # unsichtbaren Knoepfe des Fensters - _sync_strip_buttons kopiert
        # ihn auf alle Strips.
        plot.strip_buttons = {}
        for name, master in (
            ("focused", self.focused_mode_btn),
            ("delta", self.delta_btn),
            ("bookmarks", self.bookmark_btn),
        ):
            btn = QtWidgets.QPushButton("")
            btn.setFixedWidth(24)
            btn.setIcon(master.icon())
            btn.setFlat(master.isFlat())
            btn.setToolTip(master.toolTip())
            btn.clicked.connect(partial(self._strip_button_clicked, name, plot))
            row.addWidget(btn)
            plot.strip_buttons[name] = btn

        label = QtWidgets.QLabel("")
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        plot.strip_label = label
        row.addStretch()
        row.addWidget(label)

        vbox.addWidget(bar)
        vbox.addWidget(plot)

        # Beide Spalten muessen sich gleich weit zusammendruecken lassen,
        # sonst setzt ein Splitter dieselbe Vorgabe anders um als der
        # andere und die Zeilen laufen nach unten hin auseinander.
        plot.setMinimumHeight(20)
        panel.setMinimumHeight(STRIP_BAR_HEIGHT + 20)

        return panel

    def _build_strip_menu(self, plot):
        """Cmd-Menue eines Strips - die einzige Knopfebene im Fenster.

        Alles, was frueher in der Werkzeugleiste oben stand, liegt hier.
        Die Umschalter arbeiten auf self.plot; damit das immer dieser
        Strip ist, macht aboutToShow ihn vorher zum aktiven Strip.
        """

        def key(code):
            return lambda: plot.keyPressEvent(
                QtGui.QKeyEvent(
                    QtCore.QEvent.Type.KeyPress,
                    code,
                    QtCore.Qt.KeyboardModifier.NoModifier,
                )
            )

        menu = QtWidgets.QMenu()

        def entry(png, text, slot):
            icon = QtGui.QIcon()
            icon.addPixmap(QtGui.QPixmap(png), QtGui.QIcon.Mode.Normal, QtGui.QIcon.State.Off)
            return menu.addAction(icon, text, slot)

        entry(":/home.png", "Home", key(QtCore.Qt.Key.Key_W))
        entry(":/fit.png", "Fit", key(QtCore.Qt.Key.Key_F))
        entry(":/axis.png", "Honeywell", key(QtCore.Qt.Key.Key_H))
        entry(":/stack.png", "Stack", key(QtCore.Qt.Key.Key_S))

        menu.addSeparator()
        entry(":/zoom-in.png", "Zoom in", key(QtCore.Qt.Key.Key_I))
        entry(":/zoom-out.png", "Zoom out", key(QtCore.Qt.Key.Key_O))
        undo = entry(":/undo.png", "Undo zoom", self.undo_zoom)
        redo = entry(":/redo.png", "Redo zoom", self.redo_zoom)
        menu._strip_zoom_actions = (undo, redo)

        menu.addSeparator()
        entry(":/increase-font.png", "Increase font", self.increase_font)
        entry(":/decrease-font.png", "Decrease font", self.decrease_font)

        menu.addSeparator()
        # Nur die Umschalter, fuer die es keinen eigenen Knopf gibt.
        toggles = {}
        for name, text in (
            ("hide_axes", "Hide axes"),
            ("locked", "Lock all Y axes of this strip"),
        ):
            action = menu.addAction(text)
            action.setCheckable(True)
            action.toggled.connect(partial(self._strip_toggle, name, plot))
            toggles[name] = action
        menu._strip_toggles = toggles

        menu.addSeparator()
        window_type = menu.addMenu("Window type")
        menu._strip_mode_actions = {}
        for mode, text in (("analog", "Analog"), ("digital", "Digital"), ("enum", "Enum"), ("xy", "XY")):
            action = window_type.addAction(text, partial(self.set_window_mode, plot, mode))
            action.setCheckable(True)
            menu._strip_mode_actions[mode] = action

        # Die Strip-Befehle stehen im Rechtsklickmenue der Zeichenflaeche.

        menu.aboutToShow.connect(partial(self._strip_menu_about_to_show, plot, menu))

        return menu

    def _strip_menu_about_to_show(self, plot, menu):
        """Haken im Cmd-Menue an den tatsaechlichen Zustand anpassen."""
        self.set_active_strip(plot)

        toggles = getattr(menu, "_strip_toggles", {})
        state = {
            "locked": bool(self.locked),
            "hide_axes": self.hide_axes_btn.isFlat(),
            "focused": bool(self.focused_mode),
            "delta": self.region_values_display_mode == "delta",
            "bookmarks": bool(self.show_bookmarks),
        }

        for name, action in toggles.items():
            action.blockSignals(True)
            action.setChecked(state[name])
            action.blockSignals(False)

        current = self.window_mode(plot)
        for mode, action in getattr(menu, "_strip_mode_actions", {}).items():
            action.blockSignals(True)
            action.setChecked(mode == current)
            action.blockSignals(False)

        undo, redo = getattr(menu, "_strip_zoom_actions", (None, None))
        has_history = self.enable_zoom_history and hasattr(self, "undo_btn")
        if undo is not None:
            undo.setEnabled(has_history and self.undo_btn.isEnabled())
        if redo is not None:
            redo.setEnabled(has_history and self.redo_btn.isEnabled())

    def drop_from_own_list(self, source, strip, offset):
        """Abwurf eines Zugs aus einer eigenen Kanalliste.

        Liefert True, wenn der Umzug erledigt ist. Mit Richtung entsteht
        vorher der neue Strip - auch dann, wenn das Signal aus genau dem
        Strip kommt, ueber dem die Maus steht.
        """
        if strip is None or strip not in self.strips or source not in self.channel_lists():
            return False

        target = strip

        if offset:
            index = self.strips.index(strip) + (0 if offset < 0 else 1)
            target = self.add_strip(index)

        items = validate_drag_items(source.invisibleRootItem(), source.selectedItems(), [])

        if self.move_items_to_strip(source, items, target):
            self.set_active_strip(target)
            return True

        if offset:
            self.remove_strip(target)

        return False

    def move_selection_to_new_strip(self, tree, offset):
        """Markierte Kanaele in einen neuen Strip darueber oder darunter."""
        source = getattr(tree, "owner_plot", None)

        if source is None or source not in self.strips:
            return False

        items = validate_drag_items(tree.invisibleRootItem(), tree.selectedItems(), [])

        if not items:
            return False

        index = self.strips.index(source) + (1 if offset > 0 else 0)
        target = self.add_strip(index)

        if not self.move_items_to_strip(tree, items, target):
            self.remove_strip(target)
            return False

        return True

    def _x_channel_name(self, plot):
        """Name des X-Signals eines Strips - leer, wenn keins gesetzt ist."""
        signal = plot.xy_x_signal() if hasattr(plot, "xy_x_signal") else None

        return signal.name if signal is not None else ""

    def window_mode(self, plot):
        """Fenstertyp eines Strips: analog, digital, enum oder xy."""
        if getattr(plot, "xy_mode", False):
            return "xy"

        if not getattr(plot, "digital_mode", False):
            return "analog"

        return "enum" if getattr(plot, "enum_mode", False) else "digital"

    def set_window_mode(self, plot, mode):
        """Zwischen analoger, digitaler, Enum- und XY-Darstellung wechseln."""
        if mode not in ("analog", "digital", "enum", "xy"):
            return

        if mode == "xy":
            # Gestapelte Baender und XY vertragen sich nicht: dort steht
            # die Zeit auf der x-Achse, hier ein Signalwert.
            if getattr(plot, "digital_mode", False):
                self.set_digital_mode(plot, False)

            plot.enum_mode = False
            self.set_xy_mode(plot, True)
            return

        if getattr(plot, "xy_mode", False):
            self.set_xy_mode(plot, False)

        plot.enum_mode = mode == "enum"

        stacked = mode in ("digital", "enum")

        if getattr(plot, "digital_mode", False) != stacked:
            self.set_digital_mode(plot, stacked)
        else:
            # gleiche Anordnung, nur andere Zeichenart
            self.layout_digital_strip(plot)
            self._number_strips()
            plot.update()

    def set_xy_mode(self, plot, state):
        """Strip auf XY umschalten.

        Auf der x-Achse steht dann der Wert eines Signals aus diesem
        Strip statt der Zeit. Der Strip haengt dafuer nicht mehr am
        gemeinsamen Zeitbereich der anderen Strips.
        """
        state = bool(state)

        if state == bool(getattr(plot, "xy_mode", False)):
            if state:
                self.apply_xy_range(plot)
            return

        plot.xy_mode = state
        plot._xy_cache = {}

        if state:
            # Zeitzustand merken, um ihn beim Zurueckschalten wieder
            # herzustellen.
            plot._xy_time_range = tuple(plot.viewbox.viewRange()[0])
            plot._xy_old_format = plot.x_axis.format
            plot._xy_old_label = plot.x_axis.labelText

            if plot.x_uuid is None or not any(sig.uuid == plot.x_uuid for sig in plot.signals):
                # Ohne Angabe nimmt der Strip das erste Signal der Liste;
                # ueber das Rechtsklickmenue ist es schnell geaendert.
                plot.x_uuid = plot.signals[0].uuid if plot.signals else None

            # ERST abkoppeln, dann den Wertebereich setzen. Andersherum
            # zieht der Verbund alle anderen Strips auf diesen Bereich
            # mit - aus 0..30 s wuerden 440..3560 1/min.
            self._relink_strips()
            self.apply_xy_range(plot)
            self._leave_x_signal(plot)
        else:
            plot.x_axis.format = getattr(plot, "_xy_old_format", "time")
            plot.x_axis.setLabel(getattr(plot, "_xy_old_label", "") or "")

            bereich = getattr(plot, "_xy_time_range", None)
            if bereich:
                plot.viewbox.setXRange(*bereich, padding=0)

            # Erst jetzt wieder an den gemeinsamen Zeitbereich haengen.
            self._relink_strips()
            plot.trim(force=True)

        self._update_strip_axes()
        self.update_strip_marks(plot)
        plot.update()

    def apply_xy_range(self, plot):
        """x-Achse eines XY-Strips auf das X-Signal einstellen."""
        if not getattr(plot, "xy_mode", False):
            return

        x_sig = plot.xy_x_signal()

        if x_sig is not None:
            plot.x_axis.format = x_sig.format
            plot.x_axis.text_conversion = x_sig.conversion if hasattr(x_sig.conversion, "text_0") else None
            plot.x_axis.setLabel(plot.axis_label(x_sig))
        else:
            plot.x_axis.format = "phys"
            plot.x_axis.text_conversion = None
            plot.x_axis.setLabel("")

        bereich = plot.xy_value_range()
        if bereich is not None:
            plot.viewbox.blockSignals(True)
            try:
                plot.viewbox.setXRange(*bereich, padding=0)
            finally:
                plot.viewbox.blockSignals(False)
            plot.x_range = bereich

    def set_x_signal(self, plot, uuid):
        """Dieses Signal stellt ab jetzt die x-Achse des Strips.

        Schaltet den Strip gleich mit in den XY-Modus - wer ein Signal
        zur x-Achse macht, will genau das.
        """
        plot.x_uuid = uuid
        plot._xy_cache = {}

        if not getattr(plot, "xy_mode", False):
            self.set_window_mode(plot, "xy")
        else:
            self.apply_xy_range(plot)
            self._leave_x_signal(plot)
            self.update_strip_marks(plot)
            plot.update()

    def _leave_x_signal(self, plot):
        """Die linke Achse soll ein Signal zeigen, das auch gezeichnet wird.

        Bleibt das X-Signal das aktuelle, gehoert die Werteachse einer
        Kurve, die es gar nicht gibt - Zoomen und Schieben wirken dann
        ins Leere.
        """
        if not getattr(plot, "xy_mode", False) or plot.current_uuid != plot.x_uuid:
            return

        for signal in plot.signals:
            if signal.uuid != plot.x_uuid and signal.enable:
                plot.set_current_uuid(signal.uuid, True)
                return

    def update_strip_marks(self, plot):
        """Das rote X in der Kanalliste nachziehen."""
        tree = getattr(plot, "channel_list", None)
        if tree is None:
            return

        x_uuid = plot.x_uuid if getattr(plot, "xy_mode", False) else None

        iterator = QtWidgets.QTreeWidgetItemIterator(tree)
        while item := iterator.value():
            iterator += 1
            if item.type() == ChannelsTreeItem.Channel:
                item.set_x_axis_mark(item.uuid == x_uuid)

    def toggle_digital_mode(self, plot):
        self.set_digital_mode(plot, not getattr(plot, "digital_mode", False))

    def set_digital_mode(self, plot, state):
        """Strip zwischen analoger und digitaler Darstellung umschalten.

        Digital heisst: jedes Signal bekommt sein eigenes waagrechtes
        Band, die Kurven laufen als Treppe und die gemeinsame Y-Skala
        entfaellt - so wie das Digitalfenster in MDA.
        """
        state = bool(state)
        plot.digital_mode = state

        if state:
            # Eine gemeinsame Achse vertraegt sich nicht mit eigenen
            # Baendern: jede Bereichsaenderung wuerde auf alle Signale
            # der Achse durchschlagen und sie wieder uebereinanderlegen.
            plot._analog_common_axis = self._common_axis_uuids(plot) | set(plot.common_axis_items)
            self._set_common_axis_checks(plot, plot._analog_common_axis, False)

            # Der Haken allein genuegt nicht: bei gesperrtem Strip laeuft
            # der Handler gar nicht, und dann bleibt die Mitgliedschaft
            # bestehen und zieht beim naechsten Bereichswechsel wieder
            # alle Signale zusammen.
            plot.common_axis_items.clear()
            plot.common_axis_label = ""

            plot._analog_line_interconnect = plot.line_interconnect
            plot.set_line_interconnect("right")
            plot.y_axis.hide()
            plot.viewbox.setMouseEnabled(y=False)
            self.layout_digital_strip(plot)
        else:
            self._reset_list_rows(plot)
            plot.set_line_interconnect(getattr(plot, "_analog_line_interconnect", ""))

            if not self.hide_axes_btn.isFlat():
                plot.y_axis.show()

            plot._update_y_mouse()

            # zurueck auf den eigenen Wertebereich jedes Signals
            for signal in plot.signals:
                if signal.empty or signal.min == "n.a.":
                    continue

                was_locked = signal.y_locked
                signal.y_locked = False
                signal.y_range = float(signal.min), float(signal.max)
                signal.y_locked = was_locked

            if plot.current_uuid is not None:
                plot.set_current_uuid(plot.current_uuid, True)

            restore = getattr(plot, "_analog_common_axis", set())
            self._set_common_axis_checks(plot, restore, True)

            for uuid in restore:
                try:
                    plot.signal_by_uuid(uuid)
                except KeyError:
                    continue
                plot.common_axis_items.add(uuid)

            if plot.common_axis_items:
                plot.common_axis_label = ", ".join(
                    plot.signal_by_uuid(uuid)[0].name for uuid in plot.common_axis_items
                )

            plot._analog_common_axis = set()

        self._number_strips()
        plot.update()

    def _common_axis_uuids(self, plot):
        """uuids der Kanaele, die in diesem Strip auf der gemeinsamen Achse liegen."""
        uuids = set()
        tree = getattr(plot, "channel_list", None)
        if tree is None:
            return uuids

        iterator = QtWidgets.QTreeWidgetItemIterator(tree)
        while item := iterator.value():
            iterator += 1
            if item.type() == ChannelsTreeItem.Channel:
                if item.checkState(item.CommonAxisColumn) == QtCore.Qt.CheckState.Checked:
                    uuids.add(item.uuid)

        return uuids

    def _set_common_axis_checks(self, plot, uuids, state):
        """Haken der gemeinsamen Achse setzen; den Rest macht der Handler."""
        if not uuids:
            return

        tree = getattr(plot, "channel_list", None)
        if tree is None:
            return

        check = QtCore.Qt.CheckState.Checked if state else QtCore.Qt.CheckState.Unchecked

        iterator = QtWidgets.QTreeWidgetItemIterator(tree)
        while item := iterator.value():
            iterator += 1
            if item.type() != ChannelsTreeItem.Channel or item.uuid not in uuids:
                continue
            if item.checkState(item.CommonAxisColumn) != check:
                item.setCheckState(item.CommonAxisColumn, check)

    def layout_digital_strip(self, plot):
        """Jedem Signal eines Digitalstrips sein eigenes Band geben.

        Umgesetzt ueber den Wertebereich: jedes Signal bekommt einen
        Bereich, der das Vielfache seiner eigenen Spanne umfasst und um
        sein Band verschoben ist. Dadurch zeichnet asammdf die Kurven
        uebereinander statt ineinander.
        """
        if not getattr(plot, "digital_mode", False):
            return

        tree = getattr(plot, "channel_list", None)
        if tree is None:
            return

        rows = []
        iterator = QtWidgets.QTreeWidgetItemIterator(tree)
        while item := iterator.value():
            iterator += 1
            if item.type() == ChannelsTreeItem.Channel and item.signal.enable:
                rows.append((item.uuid, item))

        count = len(rows)
        if not count:
            return

        # Anteil der Bandhoehe, den die Kurve ausfuellt: 1 / 1.25 = 80 %
        spread = 1.25

        # Der erste Eintrag der Liste gehoert nach oben, also von unten
        # her durchzaehlen.
        bands = {}
        for position, (uuid, _item) in enumerate(reversed(rows)):
            try:
                signal, _index = plot.signal_by_uuid(uuid)
            except KeyError:
                continue

            if signal.empty or signal.min == "n.a." or signal.max == "n.a.":
                continue

            minimum, maximum = float(signal.min), float(signal.max)

            if minimum == maximum:
                # Ein Signal, das die ganze Messung ueber auf einem Wert
                # steht, soll aussehen wie ein Signal, das dauerhaft auf
                # diesem Pegel liegt: Linie oben an der Zeile, Flaeche
                # darunter ganz gefuellt. Frueher lag der Wert in der
                # Mitte der Zeile, dadurch war die Flaeche nur halb so
                # hoch und sass sichtbar tiefer als ihr Listeneintrag.
                # Dauerhaft null bleibt eine leere Zeile.
                wert = minimum
                if wert:
                    minimum, maximum = wert - abs(wert), wert
                else:
                    minimum, maximum = 0.0, 1.0

            span = maximum - minimum
            dim = span * spread
            bottom = minimum - (dim - span) / 2
            top = bottom + dim * count

            # Neben dem Wertebereich auch die Zeile selbst merken. Die
            # Enum-Balken fuellen sie ganz aus, auch wenn das Signal nur
            # einen einzigen Wert hat.
            bands[uuid] = ((bottom - dim * position, top - dim * position), (minimum, maximum))

        if not bands:
            return

        # Wenn moeglich, jedes Signal auf die Hoehe seines Eintrags in der
        # Kanalliste legen statt die Hoehe gleichmaessig aufzuteilen.
        self._align_bands_to_list(plot, rows, bands)

        def apply_bands():
            for uuid, (y_range, lane) in bands.items():
                try:
                    signal, _index = plot.signal_by_uuid(uuid)
                except KeyError:
                    continue

                was_locked = signal.y_locked
                signal.y_locked = False
                signal.y_range = y_range
                signal.y_locked = was_locked
                signal.enum_band = lane

        apply_bands()

        if plot.current_uuid in bands:
            # Achse UND Zeichenflaeche stumm schalten: beide schreiben den
            # neuen Bereich sonst auf die Signale zurueck.
            plot.y_axis.blockSignals(True)
            plot.viewbox.blockSignals(True)
            try:
                plot.viewbox.setYRange(*bands[plot.current_uuid][0], padding=0)
            finally:
                plot.viewbox.blockSignals(False)
                plot.y_axis.blockSignals(False)

        # noch einmal setzen, falls doch etwas dazwischengefunkt hat
        apply_bands()

        plot.update()

    def _align_bands_to_list(self, plot, rows, bands):
        """Die ganze Hoehe gleichmaessig aufteilen - Zeichenflaeche und Liste.

        Bei Enums sagt die Farbe nichts ueber den Kanal, sie gehoert zum
        Wert. Zuordnen laesst sich eine Zeile also nur ueber ihre Hoehe.
        Darum bekommen beide Seiten dieselbe Einteilung: die
        Zeichenflaeche wird in gleich hohe Zeilen geteilt, und die
        Eintraege der Kanalliste werden auf genau diese Hoehen gebracht.

        Geht das nicht - zu niedriges Fenster, eingeklappte Liste -,
        bleibt es bei der gleichmaessigen Aufteilung allein im Plot.
        """
        tree = getattr(plot, "channel_list", None)
        if tree is None or not rows:
            return

        area = plot.viewbox.sceneBoundingRect()
        height = area.height()

        if height < 20:
            return

        frame = tree.frameWidth()
        header = 0 if tree.isHeaderHidden() else tree.header().height()

        # Oberkante der ersten Zeile und Platz fuer alle Zeilen, beides
        # ohne Umweg ueber das Rollfeld - das waere von der eigenen
        # vorigen Einstellung abhaengig.
        top_of_list = tree.mapToGlobal(QtCore.QPoint(0, frame + header)).y()
        available = tree.height() - 2 * frame - header

        scrollbar = tree.horizontalScrollBar()
        if scrollbar is not None and scrollbar.isVisible():
            available -= scrollbar.height()

        top_of_plot = plot.mapToGlobal(QtCore.QPoint(0, int(area.top()))).y()

        # Faengt die Liste tiefer an als die Zeichenflaeche, bleibt oben
        # im Plot entsprechend Luft - dann stehen beide auf einer Hoehe.
        start = max(top_of_list - top_of_plot, 0)

        count = len(rows)
        # zwei Bildpunkte Reserve, sonst kippt die Liste wegen Rundung in
        # einen Rollbalken und verschiebt sich dadurch wieder
        lane = min(height - start, available - 2) / count

        if lane < 6:
            return

        for index, (uuid, item) in enumerate(rows):
            first = start + index * lane
            last = first + lane

            # Ganze Bildpunkte, aber ohne sich aufzuaddieren
            wanted = int(round(last)) - int(round(first))
            if item.sizeHint(0).height() != wanted:
                item.setSizeHint(0, QtCore.QSize(0, wanted))

            if uuid not in bands:
                continue

            _y_range, (minimum, maximum) = bands[uuid]

            # Wertebereich so legen, dass min..max genau auf dieser Zeile
            # liegt: oben am Rand der Zeichenflaeche steht y_high. Ein
            # Bildpunkt Luft, damit sich benachbarte Zeilen nicht beruehren.
            scale = (maximum - minimum) / (last - first - 2)
            y_high = maximum + (first + 1) * scale

            bands[uuid] = ((y_high - height * scale, y_high), (minimum, maximum))

        if tree.uniformRowHeights():
            tree.setUniformRowHeights(False)

    def _reset_list_rows(self, plot):
        """Zeilenhoehen der Kanalliste wieder dem Stil ueberlassen."""
        tree = getattr(plot, "channel_list", None)
        if tree is None:
            return

        iterator = QtWidgets.QTreeWidgetItemIterator(tree)
        while item := iterator.value():
            iterator += 1
            if item.sizeHint(0).height() > 0:
                item.setSizeHint(0, QtCore.QSize())

        tree.setUniformRowHeights(True)

    def schedule_strip_layout(self, plot, *args):
        """Zeilen neu ausrichten, wenn Groesse oder Liste sich geaendert haben."""
        if not getattr(plot, "digital_mode", False):
            return

        timer = getattr(plot, "_layout_timer", None)

        if timer is None:
            timer = QtCore.QTimer(plot)
            timer.setSingleShot(True)
            timer.timeout.connect(partial(self.layout_digital_strip, plot))
            plot._layout_timer = timer

        # beim Ziehen am Rand feuert das Signal laufend
        timer.start(60)

    def _strip_button_clicked(self, name, plot):
        """Einer der vier Umschaltknoepfe in der Reihe eines Strips."""
        self.set_active_strip(plot)

        if name == "hide_axes":
            self.hide_axes()
        elif name == "focused":
            self.toggle_focused_mode()
        elif name == "delta":
            self.toggle_region_values_display_mode()
        elif name == "bookmarks":
            self.toggle_bookmarks()

    def _sync_strip_buttons(self):
        """Symbole der Strip-Knoepfe an den Fensterzustand angleichen."""
        masters = {
            "hide_axes": getattr(self, "hide_axes_btn", None),
            "focused": getattr(self, "focused_mode_btn", None),
            "delta": getattr(self, "delta_btn", None),
            "bookmarks": getattr(self, "bookmark_btn", None),
        }

        for strip in getattr(self, "strips", []):
            for name, btn in getattr(strip, "strip_buttons", {}).items():
                master = masters.get(name)
                if master is None:
                    continue
                btn.setIcon(master.icon())
                btn.setFlat(master.isFlat())
                btn.setToolTip(master.toolTip())

    def _strip_toggle(self, name, plot, checked):
        """Umschalter aus dem Cmd-Menue eines Strips."""
        self.set_active_strip(plot)

        if name == "locked":
            self.set_locked(locked=checked)
            self._settings.setValue("plot/locked", checked)
        elif name == "hide_axes":
            self.hide_axes(hide=checked)
            self._settings.setValue("plot/hide_axes", checked)
        elif name == "focused":
            self.toggle_focused_mode(focused=checked)
            self._settings.setValue("plot/focused_mode", checked)
        elif name == "delta":
            mode = "delta" if checked else "value"
            self.toggle_region_values_display_mode(mode=mode)
            self._settings.setValue("plot/region_values_display_mode", mode)
        elif name == "bookmarks":
            self.toggle_bookmarks(hide=not checked)
            self._settings.setValue("plot/bookmarks", checked)

    def _add_strip_above(self, plot):
        index = self.strips.index(plot) if plot in self.strips else None
        self.set_active_strip(self.add_strip(index))

    def _add_strip_below(self, plot):
        index = self.strips.index(plot) + 1 if plot in self.strips else None
        self.set_active_strip(self.add_strip(index))

    def _number_strips(self):
        """Beschriftung "Strip n" aktuell halten."""
        many = len(self.strips) > 1
        for i, plot in enumerate(self.strips, 1):
            label = getattr(plot, "strip_label", None)
            if label is None:
                continue

            mode = self.window_mode(plot)

            if mode == "analog":
                label.setText(f"Strip {i}" if many else "")
            else:
                label.setText(f"Strip {i} ({mode})" if many else mode)

    def _wire_channel_list(self, tree, plot):
        """Alle Verbindungen einer Kanalliste herstellen.

        Frueher stand das als ein Block in __init__ - jetzt braucht es
        jeder Strip, also ist es eine eigene Methode.
        """
        tree.color_changed.connect(self._strip_set_color)
        tree.unit_changed.connect(self._strip_set_unit)
        tree.name_changed.connect(self._strip_set_name)
        tree.conversion_changed.connect(self.set_conversion)

        tree.itemsDeleted.connect(self.channel_selection_reduced)
        tree.group_activation_changed.connect(plot.update)
        tree.group_activation_changed.connect(self.cursor_moved)
        tree.currentItemChanged.connect(self.channel_selection_row_changed)
        tree.itemSelectionChanged.connect(self.channel_selection_changed)
        tree.itemSelectionChanged.connect(partial(self._remember_selection, tree))
        tree.add_channels_request.connect(self.add_channels_request)
        tree.set_time_offset.connect(plot.set_time_offset)
        tree.show_overlapping_alias.connect(self._show_overlapping_alias)
        tree.show_properties.connect(self._show_properties)
        tree.insert_computation.connect(plot.insert_computation)
        tree.edit_computation.connect(plot.edit_computation)
        tree.itemClicked.connect(self.flash_curve)

        tree.model().dataChanged.connect(self.channel_selection_item_changed)

        tree.visible_items_changed.connect(self._update_visibile_entries)

        tree.pattern_group_added.connect(self.pattern_group_added_req)
        tree.double_click.connect(self.channel_selection_item_double_clicked)

        tree.compute_fft_request.connect(self.compute_fft)
        tree.itemExpanded.connect(self.update_current_values)
        tree.verticalScrollBar().valueChanged.connect(self.update_current_values)

    def _sync_strip_heights(self, source, align=True):
        """Hoehen der beiden Spalten gleich halten.

        Links stehen die Kanallisten, rechts die Zeichenflaechen. Zieht
        man eine Trennlinie, muss die andere Spalte folgen, sonst sitzt
        eine Liste nicht mehr auf Hoehe ihres Strips.

        align=False kommt aus _schedule_column_sync: die Raender stehen
        dort schon, ein weiterer Durchgang wuerde sich nur selbst wieder
        aufrufen.
        """
        if self._syncing_strip_heights:
            return

        if align:
            self.align_columns()

        target = self.strip_splitter if source is self.list_splitter else self.list_splitter

        # Die tatsaechlichen Hoehen uebernehmen, nicht die angeforderten:
        # ein Splitter kann eine Vorgabe wegen Mindesthoehen abweichend
        # umsetzen, und dann laufen die beiden Spalten auseinander.
        sizes = [source.widget(i).height() for i in range(source.count())]

        if len(sizes) != target.count() or not all(sizes):
            return

        # Die rechte Spalte ist um die Zeitachse unten hoeher als die
        # linke. Der Unterschied gehoert in den letzten Abschnitt: dann
        # endet jede Liste genau dort, wo die Zeichenflaeche ihres
        # Strips endet - beim untersten also oberhalb der Zeitachse.
        delta = target.height() - source.height()
        if delta:
            sizes[-1] = max(sizes[-1] + delta, 1)

        self._syncing_strip_heights = True
        try:
            target.setSizes(sizes)
        finally:
            self._syncing_strip_heights = False

        self.log_strip_geometry("nach dem Angleichen der Hoehen")

    def channel_lists(self):
        """Alle Kanallisten des Fensters, in Strip-Reihenfolge."""
        return [plot.channel_list for plot in self.strips if getattr(plot, "channel_list", None) is not None]

    def _strip_of(self, uuid):
        """Den Strip liefern, der dieses Signal haelt.

        Faellt auf den aktiven Strip zurueck, wenn die uuid nirgends
        bekannt ist - damit verhaelt sich alles wie vorher, falls ein
        Aufrufer mit einer veralteten uuid kommt.
        """
        try:
            strip, _sig, _idx = self.strip_signal_by_uuid(uuid)
        except KeyError:
            return self.plot

        return strip

    def strip_signal_by_uuid(self, uuid):
        """(strip, signal, index) - sucht ueber ALLE Strips.
        Die Kanalliste gehoert dem Fenster, die Signale liegen aber in den
        einzelnen Strips. Ein Nachschlagen nur in self.plot findet Signale
        aus anderen Strips nicht. Der Index gilt jeweils innerhalb des
        zurueckgegebenen Strips - wer damit weiterarbeitet, muss diesen
        Strip benutzen, nicht self.plot.
        """
        for strip in self.strips:
            pair = strip._uuid_map.get(uuid)
            if pair is not None:
                return (strip, *pair)
        raise KeyError(uuid)

    def _wire_strip_region(self, plot):
        """Bereichsmeldungen eines Strips mitlesen, um sie zu koppeln."""
        plot.range_modified.connect(self._strip_region_modified)
        plot.range_removed.connect(self._strip_region_removed)

    def _toggle_strip_region(self, strip):
        """Bereich eines Strips oeffnen oder schliessen - wie Taste R."""
        strip.keyPressEvent(
            QtGui.QKeyEvent(
                QtCore.QEvent.Type.KeyPress,
                QtCore.Qt.Key.Key_R,
                QtCore.Qt.KeyboardModifier.NoModifier,
            )
        )

    def _strip_region_modified(self, region):
        """Bereich in allen Strips anlegen und gleich halten."""
        if self._syncing_strip_region or region is None:
            return

        source = next((strip for strip in self.strips if strip.region is region), None)
        if source is None:
            return

        bounds = region.getRegion()

        self._syncing_strip_region = True
        try:
            for strip in self.strips:
                if strip is source:
                    continue

                if strip.region is None:
                    self._toggle_strip_region(strip)

                if strip.region is not None and tuple(strip.region.getRegion()) != tuple(bounds):
                    strip.region.setRegion(bounds)
        finally:
            self._syncing_strip_region = False

    def _strip_region_removed(self):
        """Bereich ueberall schliessen, wenn er in einem Strip verschwindet."""
        if self._syncing_strip_region:
            return

        self._syncing_strip_region = True
        try:
            for strip in self.strips:
                if strip.region is not None:
                    self._toggle_strip_region(strip)
        finally:
            self._syncing_strip_region = False

    def _strip_cursor_moved(self, cursor):
        """Cursorposition auf alle anderen Strips uebertragen."""
        if self._syncing_strip_cursor:
            return

        self._syncing_strip_cursor = True
        try:
            pos = cursor.value()
            for plot in self.strips:
                if plot.cursor1 is None or plot.cursor1 is cursor:
                    continue
                plot.cursor1.setPos(pos)
                plot.cursor_move_finished.emit(plot.cursor1)
        finally:
            self._syncing_strip_cursor = False
    def spread_header_sizes(self, quelle):
        """Spaltenbreiten einer Kanalliste auf alle Strips uebertragen.

        Die Listen stehen untereinander - unterschiedlich breite Spalten
        saehen dort aus wie ein Fehler. Darum gilt eine Einstellung fuer
        das ganze Fenster, und gespeichert werden muss auch nur eine.
        """
        sizes = quelle.header_sizes()

        for tree in self.channel_lists():
            if tree is quelle:
                continue

            tree.set_header_sizes(sizes, clamp=False)
            tree.user_header_sizes = True

    def spread_axis_width(self, width):
        """Breite der y-Skala auf alle Strips uebertragen.

        Die Strips liegen untereinander. Unterschiedlich breite Skalen
        versetzen ihre Zeichenflaechen gegeneinander - also gilt eine
        Breite fuer das ganze Fenster, genau wie bei den Spalten der
        Kanallisten.
        """
        try:
            width = float(width)
        except (TypeError, ValueError):
            return

        if width < 10:
            return

        for strip in self.strips:
            achse = getattr(strip, "y_axis", None)
            if achse is None or achse.tick_width() == width:
                continue

            achse.setWidth(width)
            achse.picture = None
            achse.update()

    def apply_axis_width(self, width):
        """Breite der y-Skala aus einer Konfiguration uebernehmen.

        Gespeichert war bisher die ganze sichtbare Breite, also Skala
        plus Beschriftung. Beim Laden kam die Beschriftung noch einmal
        dazu: in alten Konfigurationen stehen deshalb 300, 400, 500
        Punkte. So breit stellt die Skala niemand ein - bei solchen
        Werten entscheidet wieder der Inhalt.
        """
        try:
            width = float(width)
        except (TypeError, ValueError):
            return

        if width < 10 or width > 200:
            return

        self.spread_axis_width(width)

    def set_list_width(self, width):
        """Breite der Kanalspalte setzen - und als gewuenscht merken.

        Kommt aus der Konfiguration. Ohne die Markierung wuerde der
        naechste Kanal die Breite wieder ueberschreiben.
        """
        try:
            width = int(width)
        except (TypeError, ValueError):
            return

        # In dieser Breite ist keine Spalte lesbar. Alte Konfigurationen
        # tragen hier teils 70 Punkte, weil die Breite damals von der
        # einen Liste des Fensters kam. Dann zaehlen die gespeicherten
        # Spaltenbreiten: genau so breit, dass sie hineinpassen.
        if width < 100:
            width = max(self.header_width(), 150)

        gesamt = sum(self.splitter.sizes())
        self.splitter.setSizes([width, max(gesamt - width, 50), 0])
        self._user_splitter = True

    def header_width(self):
        """Breite, die die sichtbaren Spalten einer Kanalliste brauchen."""
        tree = self.channel_selection
        if tree is None:
            return 0

        return sum(
            tree.columnWidth(col) for col in range(tree.columnCount()) if not tree.isColumnHidden(col)
        )

    def apply_header_sizes(self, sizes):
        """Spaltenbreiten aus einer Konfiguration auf alle Listen legen."""
        if not sizes:
            return

        for tree in self.channel_lists():
            tree.set_header_sizes(sizes)
            tree.user_header_sizes = True

    def adjust_splitter(self, initial=False):
        """Breite der Kanallisten an ihren Inhalt anpassen.

        Frueher bemass das die linke Spalte. Seit jeder Strip seine eigene
        Liste neben der Zeichenflaeche hat, gilt es fuer diese Listen.
        """
        size = sum(self.splitter.sizes())
        tree = self.channel_selection
        if tree is None:
            return

        # Hat der Benutzer die Breiten selbst eingestellt, bleiben sie
        # stehen - sonst wuerde jeder neue Kanal sie wieder verwerfen.
        if (Plot.dynamic_columns_width or initial) and not getattr(tree, "user_header_sizes", False):
            tree.resize_columns_to_contents(tree.NameColumn, tree.UnitColumn)

        # Hat der Benutzer die Trennlinie selbst gezogen oder kommt die
        # Breite aus einer Konfiguration, bleibt sie stehen. Vorher hat
        # jeder neue Kanal sie wieder auf die Summe der Spaltenbreiten
        # gesetzt - deshalb liess sich die Breite nicht merken.
        if getattr(self, "_user_splitter", False):
            return

        # Nur die sichtbaren Spalten: Qt merkt sich die Breite einer
        # ausgeblendeten Spalte weiter, und die zaehlte hier mit - die
        # Kanalspalte wurde dadurch deutlich breiter als ihr Inhalt.
        width = sum(
            tree.columnWidth(col) for col in range(tree.columnCount()) if not tree.isColumnHidden(col)
        )

        if width > self.splitter.sizes()[0]:
            if size - width >= 300:
                self.splitter.setSizes([width, size - width, 0])
            else:
                if size >= 350:
                    self.splitter.setSizes([size - 300, 300, 0])
                elif size >= 100:
                    self.splitter.setSizes([50, size - 50, 0])
        else:
            self.splitter.setSizes([width, size - width, 0])

    @property
    def bookmarks(self):
        return self.plot.bookmarks

    @bookmarks.setter
    def bookmarks(self, bkms):
        bkms = bkms or []

        # Lesezeichen gehoeren zur Messdatei, also in jeden Strip. Jeder
        # braucht aber eigene Zeichenobjekte - dasselbe Objekt kann nicht
        # in zwei Zeichenflaechen haengen.
        for strip in getattr(self, "strips", None) or [self.plot]:
            strip.bookmarks = [
                bookmark.copy() if isinstance(bookmark, Bookmark) else bookmark for bookmark in bkms
            ]

        self.bookmarks_changed.emit([bookmark.copy() for bookmark in self.plot.bookmarks], self)

    def channel_group_item_to_config(self, item):
        widget = item
        pattern = widget.pattern

        channel_group = {
            "type": "group",
            "name": widget.name,
            "enabled": item.checkState(item.NameColumn) == QtCore.Qt.CheckState.Checked,
            "pattern": widget.pattern,
            "ranges": widget.ranges,
            "origin_uuid": item.origin_uuid,
            "expanded": item.isExpanded(),
            "disabled": item.isDisabled(),
        }

        return channel_group

    def channel_item_to_config(self, item):
        widget = item

        channel = {"type": "channel"}

        strip, sig, idx = self.strip_signal_by_uuid(widget.uuid)

        if sig.flags & Signal.Flags.user_defined_name:
            channel["user_defined_name"] = sig.name
            channel["name"] = sig.original_name
        else:
            channel["name"] = sig.name

        channel["unit"] = sig.unit
        channel["flags"] = int(sig.flags)
        channel["enabled"] = item.checkState(item.NameColumn) == QtCore.Qt.CheckState.Checked

        if item.checkState(item.IndividualAxisColumn) == QtCore.Qt.CheckState.Checked:
            channel["individual_axis"] = True
            channel["individual_axis_width"] = strip.get_axis(idx).stored_width()
        else:
            channel["individual_axis"] = False

        channel["common_axis"] = item.checkState(item.CommonAxisColumn) == QtCore.Qt.CheckState.Checked
        channel["color"] = sig.color
        channel["computed"] = bool(sig.flags & Signal.Flags.computed)
        channel["ranges"] = widget.ranges
        channel["precision"] = widget.precision
        channel["fmt"] = widget.fmt
        channel["format"] = widget.format
        channel["mode"] = widget.mode
        if sig.flags & Signal.Flags.computed:
            channel["computation"] = sig.computation

        if sig.flags & Signal.Flags.user_defined_comment:
            channel["comment"] = sig.comment

        if sig.flags & Signal.Flags.user_defined_unit:
            channel["unit"] = sig.unit

        channel["y_range"] = [float(e) for e in sig.y_range]
        channel["y_locked"] = bool(sig.y_locked)

        # Nur eintragen, wenn vom Standard abgewichen wird - alte
        # Konfigurationen bleiben dadurch unveraendert lesbar.
        if getattr(sig, "line_width", 0):
            channel["line_width"] = int(sig.line_width)

        if getattr(sig, "line_style", ""):
            channel["line_style"] = str(sig.line_style)
        channel["origin_uuid"] = str(sig.origin_uuid)

        if sig.flags & Signal.Flags.user_defined_conversion:
            channel["conversion"] = to_dict(sig.conversion)

        return channel

    def channel_selection_changed(self, update=False):
        def set_focused(item):
            if item.type() == item.Channel:
                item.signal.path = None
                item.signal.enable = True

            elif item.type() == item.Group:
                for i in range(item.childCount()):
                    set_focused(item.child(i))

        if self.focused_mode:
            for signal in self.plot.signals:
                signal.enable = False

            for item in self.channel_selection.selectedItems():
                set_focused(item)
            self.plot.trim()
            self.plot.update()
        else:
            if update:
                for signal in self.plot.signals:
                    signal.path = None
                    signal.enable = False

                iterator = QtWidgets.QTreeWidgetItemIterator(self.channel_selection)
                while item := iterator.value():
                    if (
                        item.type() == item.Channel
                        and item.checkState(item.NameColumn) == QtCore.Qt.CheckState.Checked
                        and not item.isDisabled()
                    ):
                        item.signal.enable = True

                    iterator += 1

                self.plot.trim()
                self.plot.update()

    def channel_selection_item_changed(self, top_left, bottom_right, roles):
        # Der Index gilt fuer das Modell der Liste, die das Signal geschickt
        # hat - nicht zwangslaeufig fuer die aktive Liste.
        model = self.sender()
        tree = self.channel_selection
        for candidate in self.channel_lists():
            if candidate.model() is model:
                tree = candidate
                break

        item = tree.itemFromIndex(top_left)
        if item is None:
            return

        if QtCore.Qt.ItemDataRole.CheckStateRole not in roles:
            return

        if item.type() != item.Channel or item.isDisabled():
            return

        column = top_left.column()

        if column == item.NameColumn:
            enabled = item.checkState(column) == QtCore.Qt.CheckState.Checked
            if enabled != item.signal.enable:
                item.signal.enable = enabled
                try:
                    strip, _sig, _idx = self.strip_signal_by_uuid(item.uuid)
                except KeyError:
                    return
                strip.set_signal_enable(item.uuid, item.checkState(column))
                self.layout_digital_strip(strip)
            if not enabled and tree.hide_disabled_channels:
                item.setHidden(True)

        elif column == item.CommonAxisColumn:
            if not self.locked:
                enabled = item.checkState(column) == QtCore.Qt.CheckState.Checked
                if enabled != item.signal.y_link:
                    item.signal.y_link = enabled
                    self._strip_of(item.uuid).set_common_axis(item.uuid, enabled)

        elif column == item.IndividualAxisColumn:
            enabled = item.checkState(column) == QtCore.Qt.CheckState.Checked
            if enabled != item.signal.individual_axis:
                self._strip_of(item.uuid).set_individual_axis(item.uuid, enabled)

        elif column == item.LockColumn:
            enabled = item.checkState(column) == QtCore.Qt.CheckState.Checked
            if enabled != item.signal.y_locked:
                uuids = self._strip_of(item.uuid).set_y_locked(item.uuid, enabled)

                # Auf der gemeinsamen Achse haengen mehrere Signale an
                # derselben Sperre - deren Haken muessen mitziehen.
                state = QtCore.Qt.CheckState.Checked if enabled else QtCore.Qt.CheckState.Unchecked
                for other in self.iter_channel_items():
                    if other is not item and other.uuid in uuids:
                        if other.checkState(other.LockColumn) != state:
                            other.setCheckState(other.LockColumn, state)

    def channel_selection_item_double_clicked(self, item, button):
        if item is None:
            return

        elif item.type() != item.Info:
            if item.type() == item.Channel:
                # Virtuelles Signal: der Doppelklick oeffnet seine
                # Einstellungen. Der Menueeintrag dafuer liegt tief in
                # einem Untermenue und ist kaum zu finden. Ein- und
                # ausschalten geht weiter ueber das Kaestchen.
                if (
                    button == QtCore.Qt.MouseButton.LeftButton
                    and item.signal.flags & item.signal.Flags.computed
                ):
                    strip = self._strip_of(item.uuid)
                    if strip is not None:
                        strip.edit_computation(item)
                    return

                if not item.isDisabled():
                    if item.checkState(item.NameColumn) == QtCore.Qt.CheckState.Checked:
                        item.setCheckState(item.NameColumn, QtCore.Qt.CheckState.Unchecked)
                    else:
                        item.setCheckState(item.NameColumn, QtCore.Qt.CheckState.Checked)
            elif item.type() == item.Group:
                if (
                    Plot.item_double_click_handling == "enable/disable" and button == QtCore.Qt.MouseButton.LeftButton
                ) or (
                    Plot.item_double_click_handling == "expand/collapse" and button == QtCore.Qt.MouseButton.RightButton
                ):
                    if self.channel_selection.expandsOnDoubleClick():
                        self.channel_selection.setExpandsOnDoubleClick(False)
                    if item.isDisabled():
                        item.set_disabled(False)
                    else:
                        item.set_disabled(True)
                    self.plot.update()
                else:
                    item.setExpanded(not item.isExpanded())

    def channel_selection_reduced(self, deleted):
        # Jeder Strip bekommt nur die uuids, die ihm gehoeren. Sonst wuerde
        # ein Loeschen in Strip 2 die Kurven aus Strip 1 entfernen.
        by_strip = {}
        for uuid in deleted:
            strip = self._strip_of(uuid)
            by_strip.setdefault(id(strip), (strip, []))[1].append(uuid)

        for strip, uuids in by_strip.values():
            strip.delete_channels(uuids)
            self.layout_digital_strip(strip)

        if self.info_uuid in deleted:
            self.info_uuid = None
            self.info.hide()

        # Das Fenster schliesst erst, wenn ALLE Strips leer sind.
        count = 0
        for tree in self.channel_lists():
            iterator = QtWidgets.QTreeWidgetItemIterator(tree)
            while iterator.value():
                count += 1
                iterator += 1

        if not count:
            self.info_uuid = None
            self.info.set_stats(None)
            self.close_request.emit()

    def channel_selection_row_changed(self, current, previous):
        if not self.closed:
            if current and current.type() == ChannelsTreeItem.Channel:
                item = current
                uuid = item.uuid
                self.info_uuid = uuid

                self._strip_of(self.info_uuid).set_current_uuid(self.info_uuid)

                if self.info.isVisible():
                    stats = self._strip_of(self.info_uuid).get_stats(self.info_uuid)
                    self.info.set_stats(stats)

                if len(self.channel_selection.selectedItems()) == 1:
                    self.flash_curve(current, 0)

    def clear(self):
        event = QtGui.QKeyEvent(
            QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_A, QtCore.Qt.KeyboardModifier.ControlModifier
        )
        self.channel_selection.keyPressEvent(event)
        event = QtGui.QKeyEvent(
            QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_Delete, QtCore.Qt.KeyboardModifier.NoModifier
        )
        self.channel_selection.keyPressEvent(event)

    def close(self):
        if self.closed:
            return

        self.closed = True

        self.channel_selection.blockSignals(True)
        self.channel_selection.model().blockSignals(True)
        self.channel_selection.verticalScrollBar().blockSignals(True)
        self.splitter.blockSignals(True)
        self.plot.blockSignals(True)
        self.plot._can_paint_global = False
        self.owner = None
        self._inhibit_timestamp_signals_timer.blockSignals(True)
        self._inhibit_timestamp_signals_timer.stop()
        self.info.blockSignals(True)

        tree = self.channel_selection
        tree.plot = None
        iterator = QtWidgets.QTreeWidgetItemIterator(tree)
        while item := iterator.value():
            item.signal = None
            iterator += 1

        tree.clear()
        tree.close()
        self._visible_items.clear()

        for sig in self.plot.signals:
            sig.enable = False
            del sig.plot_samples
            del sig.timestamps
            del sig.plot_timestamps
            del sig.samples
            del sig.phys_samples
            del sig.raw_samples
            sig._raw_samples = None
            sig._phys_samples = None
            sig._timestamps = None
        self.plot.signals.clear()
        self.plot._uuid_map.clear()
        self.plot._timebase_db.clear()
        for axis in self.plot.axes:
            if isinstance(axis, FormatedAxis):
                axis.blockSignals(True)
        self.plot.axes = None
        self.plot.plot_parent = None
        self.plot.close()

        self.verify_bookmarks.emit(self)

        self.mdf = None

        super().close()

    def color_same_origin_signals(self, origin_uuid="", color=""):
        iterator = QtWidgets.QTreeWidgetItemIterator(self.channel_selection)
        while item := iterator.value():
            if item.type() == item.Channel and item.signal.origin_uuid == origin_uuid:
                item.color = color

            iterator += 1

    def computation_channel_inserted(self, sig):
        sig.enable = True

        if self.channel_selection.selectedItems():
            item = self.channel_selection.selectedItems()[0]
            item_below = self.channel_selection.itemBelow(item)
            if item_below is None or item_below.parent() != item.parent():
                destination = item.parent()
            else:
                destination = item_below
        else:
            destination = None

        self.add_new_channels({sig.name: sig}, destination=destination)

        self.info_uuid = sig.uuid

        self._strip_of(self.info_uuid).set_current_uuid(self.info_uuid, True)

    def compute_fft(self, uuid):
        _strip, signal, _index = self.strip_signal_by_uuid(uuid)
        try:
            window = FFTWindow(PlotSignal(signal), parent=self)
            window.show()
        except:
            pass

    def current_uuid_changed(self, uuid):
        self.info_uuid = uuid

    def cursor_moved(self, cursor=None):
        if self.plot.cursor1 is None:
            return

        position = self.plot.cursor1.value()

        if not self.plot.region:
            self.cursor_info.update_value()

            for item in self._visible_items.values():
                if item.type() == item.Channel:
                    try:
                        strip, signal, _idx = self.strip_signal_by_uuid(item.uuid)
                    except KeyError:
                        continue
                    index = strip.get_timestamp_index(position, signal.timestamps)

                    raw_value, raw_kind, value, kind, fmt = signal.value_at_index(index)

                    item.set_prefix()
                    item.kind = kind
                    item.set_fmt(fmt)

                    item.set_value(raw_value, value, update=True)

        if self.info.isVisible():
            stats = self._strip_of(self.info_uuid).get_stats(self.info_uuid)
            self.info.set_stats(stats)

        if not self._inhibit_timestamp_signals:
            self.cursor_moved_signal.emit(self, position)

    def cursor_move_finished(self, cursor=None):
        x = self.plot.get_current_timebase()
        if x.size:
            dim = len(x)
            position = self.plot.cursor1.value()

            right = np.searchsorted(x, position, side="right")
            if right == 0:
                next_pos = x[0]
            elif right == dim:
                next_pos = x[-1]
            else:
                if position - x[right - 1] < x[right] - position:
                    next_pos = x[right - 1]
                else:
                    next_pos = x[right]
            self.plot.cursor1.setPos(next_pos)

    def cursor_removed(self):
        iterator = QtWidgets.QTreeWidgetItemIterator(self.channel_selection)
        while item := iterator.value():
            if item.type() == item.Channel and not self.plot.region:
                self.cursor_info.update_value()
                item.set_prefix()
                item.set_value("", "")

            iterator += 1

        if self.info.isVisible():
            stats = self._strip_of(self.info_uuid).get_stats(self.info_uuid)
            self.info.set_stats(stats)

        self.cursor_removed_signal.emit(self)

    def curve_clicked(self, uuid):
        iterator = QtWidgets.QTreeWidgetItemIterator(self.channel_selection)
        while item := iterator.value():
            if item.type() == item.Channel and item.uuid == uuid:
                self.channel_selection.clearSelection()
                self.channel_selection.setCurrentItem(item)
                break

            iterator += 1

    def decrease_font(self):
        font = self.font()
        size = font.pointSize()
        pos = bisect.bisect_left(FONT_SIZE, size) - 1
        pos = max(pos, 0)
        new_size = FONT_SIZE[pos]

        self.set_font_size(new_size)

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat("application/octet-stream-asammdf"):
            e.accept()
        super().dragEnterEvent(e)

    # Haltezeit in Millisekunden, bis das Auswahlfeld erscheint
    STRIP_DROP_DWELL = 350

    def strip_at(self, global_position):
        """Welcher Strip liegt unter dieser Bildschirmposition?"""
        for strip in self.strips:
            if strip.rect().contains(strip.mapFromGlobal(global_position)):
                return strip

            tree = getattr(strip, "channel_list", None)
            if tree is not None and tree.rect().contains(tree.mapFromGlobal(global_position)):
                return strip

        return None

    def strip_area_at(self, global_position):
        """Welche Zeichenflaeche liegt unter dieser Bildschirmposition?

        Anders als strip_at zaehlt die Kanalliste daneben nicht mit: das
        Auswahlfeld fuer einen neuen Strip gehoert an die Zeichenflaeche.
        In der Liste zieht man Signale in eine andere Reihenfolge, da
        waere es nur im Weg.
        """
        for strip in self.strips:
            if strip.rect().contains(strip.mapFromGlobal(global_position)):
                return strip

        return None

    def strip_hint(self):
        hint = getattr(self, "_strip_hint", None)

        if hint is None:
            hint = self._strip_hint = StripDropHint(self)

        return hint

    def _strip_hint_timer(self):
        """Zeitgeber, damit das Feld auch bei voellig ruhiger Maus kommt.

        Ohne ihn muesste man wackeln: Qt schickt waehrend des Ziehens
        nur dann Ereignisse, wenn sich die Maus bewegt.
        """
        timer = getattr(self, "_hint_timer", None)

        if timer is None:
            timer = self._hint_timer = QtCore.QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self.show_strip_hint)

        return timer

    def show_strip_hint(self):
        strip = self._drag_dwell_strip

        if strip is None or strip not in self.strips:
            return

        hint = self.strip_hint()
        if hint.isVisible():
            return

        if len(self.strips) > 1:
            middle = f"Add to strip {self.strips.index(strip) + 1}"
        else:
            middle = "Add to this strip"

        hint.show_for(QtGui.QCursor.pos(), middle)

    def strip_hint_update(self, strip, position):
        """Verweildauer verfolgen und das Auswahlfeld fuehren."""
        hint = self.strip_hint()

        if strip is None:
            self.strip_hint_hide()
            return

        if hint.isVisible():
            hint.update_choice(QtGui.QCursor.pos())
            return

        moved = self._drag_dwell_pos is None or (position - self._drag_dwell_pos).manhattanLength() > 6

        if strip is not self._drag_dwell_strip or moved:
            self._drag_dwell_strip = strip
            self._drag_dwell_pos = position
            self._drag_dwell_start = perf_counter()
            self._strip_hint_timer().start(self.STRIP_DROP_DWELL)

        elif perf_counter() - self._drag_dwell_start >= self.STRIP_DROP_DWELL / 1000:
            # Rueckfall, falls der Zeitgeber waehrend des Ziehens nicht
            # zum Zug kommt
            self.show_strip_hint()

    def strip_hint_hide(self):
        timer = getattr(self, "_hint_timer", None)
        if timer is not None:
            timer.stop()

        hint = getattr(self, "_strip_hint", None)
        if hint is not None:
            hint.hide()

        self._drag_dwell_strip = None
        self._drag_dwell_pos = None
        self._drag_dwell_start = 0.0

    def strip_hint_take(self):
        """Gewaehlte Richtung abholen: -1 darueber, 0 hier, +1 darunter."""
        hint = getattr(self, "_strip_hint", None)
        choice = 1

        if hint is not None and hint.isVisible():
            choice = hint.update_choice(QtGui.QCursor.pos())

        self.strip_hint_hide()

        return choice - 1

    def dragMoveEvent(self, e):
        self.strip_hint_update(self.strip_area_at(QtGui.QCursor.pos()), e.position().toPoint())
        e.accept()

    def dragLeaveEvent(self, e):
        self.strip_hint_hide()
        super().dragLeaveEvent(e)

    def dropEvent(self, e):
        strip = self.strip_at(QtGui.QCursor.pos())
        offset = self.strip_hint_take()

        source = e.source()

        # Zug aus einer eigenen Kanalliste auf eine Zeichenflaeche: der
        # Abwurf landet hier, nicht bei der Zeichenflaeche. Ohne diesen
        # Zweig wuerde er wirkungslos verpuffen.
        if source in self.channel_lists():
            if self.drop_from_own_list(source, strip, offset):
                e.accept()
            else:
                super().dropEvent(e)
            return

        if strip is not None:
            self.set_active_strip(strip)

            if offset:
                self.schedule_strip_choice(strip, offset)

        # Zug aus der Kanalliste eines ANDEREN Plotfensters: das soll
        # verschieben, nicht kopieren. Hier entstehen die Kanaele neu,
        # drueben muessen sie also weg - aber erst, wenn es geklappt hat.
        moving = moving_source(e, self)

        data = e.mimeData()
        if data.hasFormat("application/octet-stream-asammdf"):
            names = extract_mime_names(data)

            before = signal_count(self)
            self.add_channels_request.emit(names)

            if moving is not None and signal_count(self) > before:
                tree, items = moving
                tree.delete_items(items)
        else:
            e.ignore()

    def flash_curve(self, item, column):
        if self.plot.cursor1:
            self.plot.flash_current_signal = 6
            self.plot.update()

    def hide_axes(self, event=None, hide=None):
        if hide is None:
            hide = not self.hide_axes_btn.isFlat()
            self._settings.setValue("plot/hide_axes", hide)

        # Der Knopf gilt fuer das ganze Fenster, nicht nur fuer den
        # aktiven Strip - sonst behalten die uebrigen ihre Achsen.
        for strip in self.strips:
            if hide:
                strip.y_axis.hide()
            elif not getattr(strip, "digital_mode", False):
                # Im Digitalmodus gehoert die Werteachse ohnehin weg.
                strip.y_axis.show()

        if hide:
            self.hide_axes_btn.setFlat(True)
            self.hide_axes_btn.setToolTip("Show axes")
        else:
            self.hide_axes_btn.setFlat(False)
            self.hide_axes_btn.setToolTip("Hide axes")

        # Die Zeitachse haengt am untersten Strip, also hier entlang.
        self._update_strip_axes()

        if hide:
            png = ":/axis.png"
        else:
            png = ":/axis_on.png"
        icon = QtGui.QIcon()
        icon.addPixmap(QtGui.QPixmap(png), QtGui.QIcon.Mode.Normal, QtGui.QIcon.State.Off)
        self.hide_axes_btn.setIcon(icon)
        self._sync_strip_buttons()

    def increase_font(self):
        font = self.font()
        size = font.pointSize()
        pos = bisect.bisect_right(FONT_SIZE, size)
        if pos == len(FONT_SIZE):
            pos -= 1
        new_size = FONT_SIZE[pos]

        self.set_font_size(new_size)

    def info_precision_modified(self):
        if not self.closed:
            if self.info_uuid is not None:
                stats = self._strip_of(self.info_uuid).get_stats(self.info_uuid)
                self.info.set_stats(stats)

    def _inhibit_timestamp_handler(self):
        self._inhibit_timestamp_signals = False

    def item_by_uuid(self, uuid):
        return self._item_cache.get(uuid, None)

    def keyPressEvent(self, event):
        key = event.key()
        modifiers = event.modifiers()

        if event.keyCombination().toCombined() in self.disabled_keyboard_events:
            event.ignore()
            return

        if key == QtCore.Qt.Key.Key_M and modifiers == QtCore.Qt.KeyboardModifier.NoModifier:
            event.accept()
            ch_size, plt_size, info_size = self.splitter.sizes()

            if self.info.isVisible():
                self.info.hide()
                self.splitter.setSizes((ch_size, plt_size + info_size, 0))

            else:
                self.info.show()
                self.splitter.setSizes(
                    (
                        ch_size,
                        int(0.8 * (plt_size + info_size)),
                        int(0.2 * (plt_size + info_size)),
                    )
                )

                if self.info_uuid:
                    stats = self._strip_of(self.info_uuid).get_stats(self.info_uuid)
                    self.info.set_stats(stats)

        elif key == QtCore.Qt.Key.Key_2 and modifiers == QtCore.Qt.KeyboardModifier.NoModifier:
            self.focused_mode = not self.focused_mode
            if self.focused_mode:
                self.focused_mode_btn.setFlat(False)
            else:
                self.focused_mode_btn.setFlat(True)
            self.channel_selection_changed(update=True)

            event.accept()

        elif (
            key in (QtCore.Qt.Key.Key_B, QtCore.Qt.Key.Key_H, QtCore.Qt.Key.Key_P, QtCore.Qt.Key.Key_T)
            and modifiers == QtCore.Qt.KeyboardModifier.ControlModifier
        ):
            selected_items = self.channel_selection.selectedItems() or [
                self.channel_selection.topLevelItem(i) for i in range(self.channel_selection.topLevelItemCount())
            ]

            if key == QtCore.Qt.Key.Key_B:
                fmt = "bin"
            elif key == QtCore.Qt.Key.Key_H:
                fmt = "hex"
            elif key == QtCore.Qt.Key.Key_P:
                fmt = "phys"
            else:
                fmt = "ascii"

            for item in selected_items:
                item_type = item.type()
                if item_type == item.Info:
                    continue

                elif item_type == item.Channel:
                    strip, signal, idx = self.strip_signal_by_uuid(item.uuid)

                    if signal.plot_samples.dtype.kind in "uif":
                        signal.format = fmt

                        raw_value, raw_kind, value, kind, fmt = signal.value_at_timestamp(0)

                        widget = self.item_by_uuid(signal.uuid)
                        widget.kind = kind
                        widget.set_fmt(fmt)
                        widget.set_value(update=True)

                        if strip.current_uuid == signal.uuid:
                            strip.y_axis.format = fmt
                            strip.y_axis.picture = None
                            strip.y_axis.update()

                        axis = strip.get_axis(idx)
                        if isinstance(axis, FormatedAxis):
                            axis.format = fmt
                            axis.picture = None
                            axis.update()

                elif item_type == item.Group:
                    for i in range(item.childCount()):
                        selected_items.append(item.child(i))

                    if item.pattern:
                        item.pattern["integer_format"] = fmt

            if self.info.isVisible():
                stats = self._strip_of(self.info_uuid).get_stats(self.info_uuid)
                self.info.set_stats(stats)

            self.current_uuid_changed(self.plot.current_uuid)
            self.plot.update()

            event.accept()

        elif (
            key in (QtCore.Qt.Key.Key_R, QtCore.Qt.Key.Key_S)
            and modifiers == QtCore.Qt.KeyboardModifier.AltModifier
            and self._can_switch_mode
        ):
            selected_items = self.channel_selection.selectedItems()
            if not selected_items:
                signals = [(strip, sig, i) for strip in self.strips for i, sig in enumerate(strip.signals)]
                uuids = [sig.uuid for strip in self.strips for sig in strip.signals]

            else:
                uuids = [item.uuid for item in selected_items if item.type() == ChannelsTreeItem.Channel]

                signals = [self.strip_signal_by_uuid(uuid) for uuid in uuids]

            if signals:
                if key == QtCore.Qt.Key.Key_R:
                    mode = "raw"
                    style = QtCore.Qt.PenStyle.DashLine

                else:
                    mode = "phys"
                    style = QtCore.Qt.PenStyle.SolidLine

                for strip, signal, idx in signals:
                    if signal.mode != mode:
                        signal.pen = fn.mkPen(color=signal.color, style=style)

                        buttom, top = signal.y_range

                        try:
                            min_, max_ = float(signal.min), float(signal.max)
                        except:
                            min_, max_ = 0, 1

                        if max_ != min_ and top != buttom:
                            factor = (top - buttom) / (max_ - min_)
                            offset = (buttom - min_) / (top - buttom)
                        else:
                            factor = 1
                            offset = 0

                        signal.mode = mode

                        try:
                            min_, max_ = float(signal.min), float(signal.max)
                        except:
                            min_, max_ = 0, 1

                        if max_ != min_:
                            delta = (max_ - min_) * factor
                            buttom = min_ + offset * delta
                            top = buttom + delta
                        else:
                            buttom, top = max_ - 1, max_ + 1

                        signal.y_range = buttom, top
                        item = self.item_by_uuid(signal.uuid)
                        item._value = None

                        if strip.current_uuid == signal.uuid:
                            strip.y_axis.mode = mode
                            strip.y_axis.picture = None
                            strip.y_axis.update()
                            strip.viewbox.setYRange(buttom, top, padding=0, update=True)

                        strip.get_axis(idx).mode = mode
                        strip.get_axis(idx).picture = None
                        strip.get_axis(idx).update()

            for uuid in uuids:
                item = self.item_by_uuid(uuid)
                item.setText(item.UnitColumn, item.unit)

            self.plot.update()

            if self.plot.cursor1:
                self.plot.cursor_moved.emit(self.plot.cursor1)

            event.accept()

        elif key == QtCore.Qt.Key.Key_I and modifiers == QtCore.Qt.KeyboardModifier.ControlModifier:
            if self.plot.cursor1:
                position = self.plot.cursor1.value()
                comment, submit = QtWidgets.QInputDialog.getMultiLineText(
                    self,
                    "Insert comments",
                    f"Enter the comments for cursor position {position:.9f}s:",
                    "",
                )
                if submit:
                    visible = True
                    for bookmark in self.bookmarks:
                        visible = bookmark.visible
                        break

                    bookmark = Bookmark(
                        pos=position,
                        message=comment,
                        color="#FF0000",
                        tool=Tool.__tool__,
                    )
                    bookmark.visible = visible
                    bookmark.edited = True

                    bookmarks = [*self.bookmarks, bookmark]

                    self.bookmarks = bookmarks

                    if not visible:
                        self.toggle_bookmarks()

            event.accept()

        elif key == QtCore.Qt.Key.Key_I and modifiers == QtCore.Qt.KeyboardModifier.AltModifier:
            self.toggle_bookmarks(not self.show_bookmarks)
            event.accept()

        elif key == QtCore.Qt.Key.Key_G and modifiers == QtCore.Qt.KeyboardModifier.ControlModifier:
            selected_items = [
                item for item in self.channel_selection.selectedItems() if item.type() != ChannelsTreeItem.Info
            ]

            channel_items = [item for item in selected_items if item.type() == ChannelsTreeItem.Channel]

            if not channel_items:
                channel_items = [item.first_signal() for item in selected_items]
                channel_items = [item for item in channel_items if item is not None]

            if selected_items:
                uuids = [item.uuid for item in channel_items]

                signals = {}
                for i, uuid in enumerate(uuids):
                    _strip, sig, _idx = self.strip_signal_by_uuid(uuid)
                    if i == 0:
                        y_range = sig.y_range
                    signals[sig.name] = sig

                if not signals:
                    signals["Demo signal"] = Signal(
                        name="Demo signal",
                        samples=[],
                        timestamps=[],
                    )
                    y_range = (0, 100)

                diag = ScaleDialog(signals, y_range, parent=self)

                if diag.exec():
                    offset = diag.offset.value()
                    scale = diag.scaling.value()

                    y_bottom = -offset * scale / 100
                    y_top = y_bottom + scale

                    y_range = y_bottom, y_top

                    for item in selected_items:
                        item.y_range = y_range

                    self.zoom_changed()

                    self.plot.update()

            event.accept()

        elif key == QtCore.Qt.Key.Key_R and modifiers == QtCore.Qt.KeyboardModifier.NoModifier:
            iterator = QtWidgets.QTreeWidgetItemIterator(self.channel_selection)
            while item := iterator.value():
                if item.type() == item.Channel:
                    item.set_prefix()
                    item.set_value("", "")

                iterator += 1

            self.plot.keyPressEvent(event)

        elif key == QtCore.Qt.Key.Key_C and modifiers in (
            QtCore.Qt.KeyboardModifier.NoModifier,
            QtCore.Qt.KeyboardModifier.ControlModifier,
            QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.ShiftModifier,
        ):
            self.channel_selection.keyPressEvent(event)

        elif (
            key == QtCore.Qt.Key.Key_R
            and modifiers == QtCore.Qt.KeyboardModifier.ControlModifier
            and self.can_edit_ranges
        ):
            self.channel_selection.keyPressEvent(event)

        elif key == QtCore.Qt.Key.Key_V and modifiers in (
            QtCore.Qt.KeyboardModifier.ControlModifier,
            QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.ShiftModifier,
        ):
            self.channel_selection.keyPressEvent(event)

        elif key == QtCore.Qt.Key.Key_BracketLeft and modifiers == QtCore.Qt.KeyboardModifier.ControlModifier:
            self.decrease_font()

        elif key == QtCore.Qt.Key.Key_BracketRight and modifiers == QtCore.Qt.KeyboardModifier.ControlModifier:
            self.increase_font()

        elif key == QtCore.Qt.Key.Key_Backspace:
            if modifiers == QtCore.Qt.KeyboardModifier.ShiftModifier:
                self.redo_zoom()
            else:
                self.undo_zoom()

            event.accept()

        elif key == QtCore.Qt.Key.Key_W and modifiers == QtCore.Qt.KeyboardModifier.ShiftModifier:
            if self.enable_zoom_history and self.zoom_history:
                self.zoom_history_index = 0

                snapshot = self.zoom_history[self.zoom_history_index]

                self.plot.block_zoom_signal = True

                for sig in self.plot.signals:
                    y_range = snapshot["y"].get(sig.uuid, None)

                    if y_range is None:
                        continue

                    self._strip_of(sig.uuid).set_y_range(sig.uuid, y_range, emit=False)

                self.plot.viewbox.setXRange(*snapshot["x"], padding=0)

                self.undo_btn.setEnabled(False)
                if len(self.zoom_history) > 1:
                    self.redo_btn.setEnabled(True)

                self.plot.block_zoom_signal = False

            event.accept()

        elif key == QtCore.Qt.Key.Key_W and modifiers == QtCore.Qt.KeyboardModifier.ControlModifier:
            if self.enable_zoom_history:
                self.set_initial_zoom()

            event.accept()

        else:
            try:
                self.plot.keyPressEvent(event)
            except:
                print(format_exc())

    def line_style_same_origin_signals(self, origin_uuid="", style=QtCore.Qt.PenStyle.SolidLine):
        for signal in self.plot.signals:
            if signal.origin_uuid == origin_uuid:
                signal.pen = fn.mkPen(color=signal.color, style=style)

        self.plot.update()

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)

    def pattern_group_added_req(self, group):
        self.pattern_group_added.emit(self, group)

    def range_modified(self, region=None):
        if self.plot.region is None:
            return

        start, stop = sorted(self.plot.region.getRegion())

        self.cursor_info.update_value()

        for item in self._visible_items.values():
            if item.type() == item.Channel:
                try:
                    strip, signal, _i = self.strip_signal_by_uuid(item.uuid)
                except KeyError:
                    continue

                index = strip.get_timestamp_index(start, signal.timestamps)
                start_raw_v, raw_kind, start_v, kind, fmt = signal.value_at_index(index)

                index = strip.get_timestamp_index(stop, signal.timestamps)
                stop_raw_v, raw_kind, stop_v, kind, fmt = signal.value_at_index(index)

                if self.region_values_display_mode == "delta":
                    item.set_prefix()
                    item.set_fmt(signal.format)

                    if "n.a." not in (start_v, stop_v):
                        if kind in "ui":
                            delta = np.int64(stop_v) - np.int64(start_v)
                            delta_raw = np.int64(stop_raw_v) - np.int64(start_raw_v)
                            item.kind = kind
                            item.set_value(delta_raw, delta)
                            item.set_fmt(fmt)
                        elif kind == "f":
                            delta = stop_v - start_v
                            delta_raw = stop_raw_v - start_raw_v
                            item.kind = kind
                            item.set_value(delta_raw, delta)
                            item.set_fmt(fmt)
                        else:
                            item.set_value("n.a.", "n.a.")
                    else:
                        item.set_value("n.a.", "n.a.")

                else:
                    if self.plot.region_lock is not None:
                        if start == self.plot.region_lock:
                            value = stop_v
                            raw_value = stop_raw_v
                        else:
                            value = start_v
                            raw_value = start_raw_v

                    else:
                        if self._prev_region is None:
                            value = start_v
                            raw_value = start_raw_v
                        else:
                            if stop == self._prev_region[1]:
                                value = start_v
                                raw_value = start_raw_v
                            else:
                                value = stop_v
                                raw_value = stop_raw_v

                    item.set_prefix()
                    item.set_fmt(signal.format)

                    if value != "n.a.":
                        if kind in "uif":
                            item.kind = kind
                            item.set_value(raw_value, value)
                            item.set_fmt(fmt)
                        else:
                            item.set_value("n.a.", "n.a.")
                    else:
                        item.set_value("n.a.", "n.a.")

        if self.info.isVisible():
            stats = self._strip_of(self.info_uuid).get_stats(self.info_uuid)
            self.info.set_stats(stats)

        self._prev_region = (start, stop)

        self.region_moved_signal.emit(self, [start, stop])

    def range_modified_finished(self):
        if not self.plot.region:
            return
        start, stop = self.plot.region.getRegion()

        timebase = self.plot.get_current_timebase()

        if timebase.size:
            dim = len(timebase)

            if self.plot.region_lock is None:
                right = np.searchsorted(timebase, start, side="right")
                if right == 0:
                    next_pos = timebase[0]
                elif right == dim:
                    next_pos = timebase[-1]
                else:
                    if start - timebase[right - 1] < timebase[right] - start:
                        next_pos = timebase[right - 1]
                    else:
                        next_pos = timebase[right]
                start = next_pos

                right = np.searchsorted(timebase, stop, side="right")
                if right == 0:
                    next_pos = timebase[0]
                elif right == dim:
                    next_pos = timebase[-1]
                else:
                    if stop - timebase[right - 1] < timebase[right] - stop:
                        next_pos = timebase[right - 1]
                    else:
                        next_pos = timebase[right]
                stop = next_pos

                self.plot.region.setRegion((start, stop))

            else:
                if start == self.plot.region_lock:
                    pos = stop
                else:
                    pos = start

                right = np.searchsorted(timebase, pos, side="right")
                if right == 0:
                    next_pos = timebase[0]
                elif right == dim:
                    next_pos = timebase[-1]
                else:
                    if pos - timebase[right - 1] < timebase[right] - pos:
                        next_pos = timebase[right - 1]
                    else:
                        next_pos = timebase[right]
                pos = next_pos

                self.plot.region.setRegion((self.plot.region_lock, pos))

    def range_removed(self):
        self._prev_region = None
        iterator = QtWidgets.QTreeWidgetItemIterator(self.channel_selection)
        while item := iterator.value():
            if item.type() == item.Channel:
                item.set_prefix()
                item.set_value("", "")

            iterator += 1

        self.cursor_info.update_value()

        if self.plot.cursor1:
            self.plot.cursor_moved.emit(self.plot.cursor1)
        if self.info.isVisible():
            stats = self._strip_of(self.info_uuid).get_stats(self.info_uuid)
            self.info.set_stats(stats)

        self.region_removed_signal.emit(self)

    def redo_zoom(self):
        if self.enable_zoom_history and self.zoom_history:
            self.zoom_history_index = min(self.zoom_history_index + 1, len(self.zoom_history) - 1)

            snapshot = self.zoom_history[self.zoom_history_index]

            self.plot.block_zoom_signal = True

            for sig in self.plot.signals:
                y_range = snapshot["y"].get(sig.uuid, None)

                if y_range is None:
                    continue

                self._strip_of(sig.uuid).set_y_range(sig.uuid, y_range, emit=False)

            self.plot.viewbox.setXRange(*snapshot["x"], padding=0)

            if self.zoom_history_index == len(self.zoom_history) - 1:
                self.redo_btn.setEnabled(False)
            if len(self.zoom_history) > 1:
                self.undo_btn.setEnabled(True)

            self.plot.block_zoom_signal = False

    def selected_items(self):
        uuids = [
            item.uuid for item in self.channel_selection.selectedItems() if item.type() == ChannelsTreeItem.Channel
        ]
        return set(uuids)

    def set_conversion(self, uuid, conversion):
        self._strip_of(uuid).set_conversion(uuid, conversion)
        self.cursor_moved()

    def set_font_size(self, size):
        font = self.font()
        font.setPointSize(size)
        self.setFont(font)
        self.channel_selection.set_font_size(size)
        self.plot.y_axis.set_font_size(size)
        self.plot.x_axis.set_font_size(size)

    def set_initial_zoom(self):
        self.zoom_history.clear()
        self.zoom_history_index = -1
        self.zoom_changed(inplace=True)

    def set_locked(self, event=None, locked=None):
        if locked is None:
            locked = not self.locked
            self._settings.setValue("plot/locked", locked)

        if locked:
            tooltip = "The Y axis is locked. Press to unlock"
            png = ":/locked.png"
            self.lock_btn.setFlat(True)
        else:
            tooltip = "The Y axis is unlocked. Press to lock"
            png = ":/unlocked.png"
            self.lock_btn.setFlat(False)
        icon = QtGui.QIcon()
        icon.addPixmap(QtGui.QPixmap(png), QtGui.QIcon.Mode.Normal, QtGui.QIcon.State.Off)
        self.lock_btn.setToolTip(tooltip)
        self.lock_btn.setIcon(icon)
        self._sync_strip_buttons()

        for tree in self.channel_lists():
            tree.setColumnHidden(tree.CommonAxisColumn, locked)

        self.locked = locked
        self.plot.set_locked(locked)

    def set_splitter(self, pos, index):
        # Ab jetzt gilt die Breite als gewuenscht und wird nicht mehr
        # automatisch nachgerechnet.
        self._user_splitter = True
        self.splitter_moved.emit(self, pos)

    def set_timestamp(self, stamp):
        if self.plot.cursor1 is None:
            event = QtGui.QKeyEvent(
                QtCore.QEvent.Type.KeyPress,
                QtCore.Qt.Key.Key_C,
                QtCore.Qt.KeyboardModifier.NoModifier,
            )
            self.plot.keyPressEvent(event)

        self._inhibit_timestamp_signals = True
        self._inhibit_timestamp_signals_timer.start(50)
        self.plot.cursor1.setPos(stamp)
        self.cursor_move_finished()

    def shift_same_origin_signals(self, origin_uuid="", delta=0.0, absolute=False):
        uuids = []
        iterator = QtWidgets.QTreeWidgetItemIterator(self.channel_selection)
        while item := iterator.value():
            if item.type() == item.Channel and item.signal.origin_uuid == origin_uuid:
                uuids.append(item.signal.uuid)

            iterator += 1

        if not uuids:
            return

        self.plot.set_time_offset([absolute, delta, *uuids])

    def update_missing_signals(self, uuids=()):
        model = self.channel_selection.selectionModel()
        model.clearSelection()

        iterator = QtWidgets.QTreeWidgetItemIterator(self.channel_selection)
        while item := iterator.value():
            if item.type() == item.Channel and item.signal.origin_uuid not in uuids:
                item.setSelected(True)

            iterator += 1

        self.channel_selection.keyPressEvent(
            QtGui.QKeyEvent(
                QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_Delete, QtCore.Qt.KeyboardModifier.NoModifier
            )
        )

        self.plot.update()

    def _show_overlapping_alias(self, origin_uuid, uuid):
        for sig in self.plot.signals:
            if sig.uuid == uuid:
                self.show_overlapping_alias.emit(sig)

    def _show_properties(self, origin_uuid, uuid):
        for sig in self.plot.signals:
            if sig.uuid == uuid:
                if sig.flags & Signal.Flags.computed:
                    try:
                        view = ComputedChannelInfoWindow(sig, self)
                        view.show()
                    except:
                        print(format_exc())
                        raise

                else:
                    self.show_properties.emit(sig)

    def to_config(self):
        if self.closed:
            return

        def item_to_config(tree, root):
            channels = []

            for i in range(root.childCount()):
                item = root.child(i)
                if item.type() == item.Channel:
                    channel = self.channel_item_to_config(item)

                elif item.type() == item.Group:
                    channel = self.channel_group_item_to_config(item)
                    channel["channels"] = item_to_config(tree, item) if not item.pattern else []

                channels.append(channel)

            return channels

        def all_strips_to_config():
            """Kanaele aller Strips, jeder Eintrag mit seiner Stripnummer.

            Alles landet weiterhin in einer einzigen Liste, damit eine
            Konfiguration auch im unveraenderten asammdf noch laedt -
            dort erscheinen dann eben alle Kanaele in einem Plot.
            """
            def tag(entries, index):
                for entry in entries:
                    entry["strip"] = index
                    if entry.get("type") == "group":
                        tag(entry.get("channels", []), index)

            channels = []

            for index, strip in enumerate(self.strips):
                tree = getattr(strip, "channel_list", None)
                if tree is None:
                    continue

                entries = item_to_config(tree, tree.invisibleRootItem())
                tag(entries, index)
                channels.extend(entries)

            return channels

        config = {
            "channels": all_strips_to_config() if not self.pattern else [],
            "strip_count": len(self.strips),
            "strip_sizes": [int(e) for e in self.strip_splitter.sizes()],
            "strip_digital": [bool(getattr(strip, "digital_mode", False)) for strip in self.strips],
            "strip_modes": [self.window_mode(strip) for strip in self.strips],
            "strip_x_channels": [self._x_channel_name(strip) for strip in self.strips],
            "pattern": self.pattern,
            "splitter": [int(e) for e in self.splitter.sizes()[:2]]
            + [
                0,
            ],
            "x_range": self.plot.viewbox.viewRange()[0],
            "y_axis_width": self.plot.y_axis.stored_width(),
            "grid": [
                self.plot.plotItem.ctrl.xGridCheck.isChecked(),
                self.plot.plotItem.ctrl.yGridCheck.isChecked(),
            ],
            "cursor_precision": self.cursor_info.precision,
            "font_size": self.font().pointSize(),
            "locked": self.locked,
            "common_axis_y_range": [float(e) for e in self.plot.common_axis_y_range],
            "channels_header": [
                self.splitter.sizes()[0],
                [self.channel_selection.columnWidth(i) for i in range(self.channel_selection.columnCount())],
            ],
            # Eine Spaltenaufteilung fuer das ganze Fenster: alle Listen
            # eines Plots halten dieselben Breiten.
            "channels_header_sizes": [self.splitter.sizes()[0], self.channel_selection.header_sizes()],
            "channels_header_columns_visible": [
                not self.channel_selection.isColumnHidden(i)
                for i in range(self.channel_selection.columnCount())
                if i != self.channel_selection.RawColumn
            ],
            "channels_header_columns_visiblity": self.channel_selection.header_columns_visibility(),
            "hide_axes": self.hide_axes_btn.isFlat(),
            "focused_mode": not self.focused_mode_btn.isFlat(),
            "delta_mode": "value" if self.delta_btn.isFlat() else "delta",
            "hide_bookmarks": self.bookmark_btn.isFlat(),
            "hide_missing_channels": self.channel_selection.hide_missing_channels,
            "hide_disabled_channels": self.channel_selection.hide_disabled_channels,
        }

        return config

    def toggle_bookmarks(self, *args, hide=None):
        if hide is not None:
            self.show_bookmarks = hide

        self.show_bookmarks = not self.show_bookmarks

        if self.show_bookmarks:
            self.bookmark_btn.setFlat(False)
        else:
            self.bookmark_btn.setFlat(True)

        # ueber alle Strips: der Knopf sitzt zwar in einer Strip-Reihe,
        # gemeint ist aber das ganze Fenster
        for strip in getattr(self, "strips", None) or [self.plot]:
            for bookmark in strip.bookmarks:
                bookmark.visible = self.show_bookmarks

            strip.update()

        if not self.show_bookmarks:
            png = ":/bookmark.png"
        else:
            png = ":/bookmark_on.png"
        icon = QtGui.QIcon()
        icon.addPixmap(QtGui.QPixmap(png), QtGui.QIcon.Mode.Normal, QtGui.QIcon.State.Off)
        self.bookmark_btn.setIcon(icon)
        self._sync_strip_buttons()

        if hide is None:
            self._settings.setValue("plot/bookmarks", not self.bookmark_btn.isFlat())

    def toggle_focused_mode(self, event=None, focused=None):
        if focused is not None:
            # invert so that the key press event will set the desider focused mode
            self.focused_mode = not focused

        key_event = QtGui.QKeyEvent(
            QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_2, QtCore.Qt.KeyboardModifier.NoModifier
        )
        self.keyPressEvent(key_event)

        if focused is None:
            self._settings.setValue("plot/focused_mode", self.focused_mode)

        if not self.focused_mode:
            self.focused_mode_btn.setFlat(True)
            self.focused_mode_btn.setToolTip("Switch on focused mode")
        else:
            self.focused_mode_btn.setFlat(False)
            self.focused_mode_btn.setToolTip("Switch off focused mode")

        if not self.focused_mode:
            png = ":/focus.png"
        else:
            png = ":/focus_on.png"
        icon = QtGui.QIcon()
        icon.addPixmap(QtGui.QPixmap(png), QtGui.QIcon.Mode.Normal, QtGui.QIcon.State.Off)
        self.focused_mode_btn.setIcon(icon)
        self._sync_strip_buttons()

    def toggle_region_values_display_mode(self, event=None, mode=None):
        if mode is None:
            self.region_values_display_mode = "delta" if self.region_values_display_mode == "value" else "value"
        else:
            self.region_values_display_mode = mode

        if self.region_values_display_mode == "value":
            self.delta_btn.setFlat(True)
            self.delta_btn.setToolTip("Switch to region cursors delta display mode")
        else:
            self.delta_btn.setFlat(False)
            self.delta_btn.setToolTip("Switch to active region cursor value display mode")

        if self.delta_btn.isFlat():
            png = ":/delta.png"
        else:
            png = ":/delta_on.png"
        icon = QtGui.QIcon()
        icon.addPixmap(QtGui.QPixmap(png), QtGui.QIcon.Mode.Normal, QtGui.QIcon.State.Off)
        self.delta_btn.setIcon(icon)
        self._sync_strip_buttons()

        if mode is None:
            self._settings.setValue("plot/region_values_display_mode", self.region_values_display_mode)

        # Statt jeden Wert mit "Δ = " zu verlaengern, wandert das
        # Zeichen in die Spaltenueberschrift - das spart Platz.
        heading = "Δ" if self.region_values_display_mode == "delta" else "Value"
        for tree in self.channel_lists():
            tree.headerItem().setText(tree.ValueColumn, heading)

        self.range_modified()

        for item in self.iter_channel_items():
            item.set_value(update=True, force=True)

    def iter_channel_items(self):
        """Alle Kanaleintraege aller Strips durchlaufen."""
        for tree in self.channel_lists():
            iterator = QtWidgets.QTreeWidgetItemIterator(tree)
            while item := iterator.value():
                iterator += 1
                if item.type() == ChannelsTreeItem.Channel:
                    yield item

    def toggle_same_origin_signals(self, origin_uuid="", visible=True):
        state = QtCore.Qt.CheckState.Checked if visible else QtCore.Qt.CheckState.Unchecked

        for item in self.iter_channel_items():
            if item.signal.origin_uuid == origin_uuid:
                item.setCheckState(item.NameColumn, state)

    def undo_zoom(self):
        if self.enable_zoom_history and self.zoom_history:
            self.zoom_history_index = max(self.zoom_history_index - 1, 0)

            snapshot = self.zoom_history[self.zoom_history_index]

            self.plot.block_zoom_signal = True

            for sig in self.plot.signals:
                y_range = snapshot["y"].get(sig.uuid, None)

                if y_range is None:
                    continue

                self._strip_of(sig.uuid).set_y_range(sig.uuid, y_range, emit=False)

            self.plot.viewbox.setXRange(*snapshot["x"], padding=0)

            if self.zoom_history_index == 0:
                self.undo_btn.setEnabled(False)
            if len(self.zoom_history) > 1:
                self.redo_btn.setEnabled(True)

            self.plot.block_zoom_signal = False

    def update_current_values(self, *args):
        if self.plot.region:
            self.range_modified(None)
        else:
            self.cursor_moved()

    def _update_visibile_entries(self):
        with self.lock:
            _item_cache = self._item_cache = {}
            _visible_entries = self._visible_entries = set()
            _visible_items = self._visible_items = {}

            # Ueber ALLE Kanallisten laufen - sonst werden die Werte in den
            # anderen Strips beim Cursorbewegen nicht mehr aktualisiert.
            for tree in self.channel_lists():
                iterator = QtWidgets.QTreeWidgetItemIterator(tree)

                while item := iterator.value():
                    iterator += 1
                    if item.type() == ChannelsTreeItem.Channel:
                        _item_cache[item.uuid] = item

                        if item.uuid == self.info_uuid or (
                            item.exists
                            and (item.checkState(item.NameColumn) == QtCore.Qt.CheckState.Checked or item._is_visible)
                        ):
                            entry = (item.origin_uuid, item.signal.name, item.uuid)
                            _visible_entries.add(entry)
                            _visible_items[entry] = item

            self.visible_entries_modified = True

        if self.plot.cursor1 is not None:
            self.cursor_moved()

    def visible_entries(self):
        return self._visible_entries

    def visible_items(self):
        return self._visible_items

    def xrange_changed(self, *args):
        vb, x_range = args

        if self.info.isVisible():
            stats = self._strip_of(self.info_uuid).get_stats(self.info_uuid)
            self.info.set_stats(stats)

        if self.timebase_overview:
            self.timebase_overview.x_range = x_range

        if not self._inhibit_x_range_changed_signal:
            self.x_range_changed_signal.emit(self, x_range)

    def zoom_changed(self, inplace=False):
        if self.enable_zoom_history and self.plot.signals and not self.plot.block_zoom_signal:
            snapshot = {
                "x": self.plot.viewbox.viewRange()[0],
                "y": {sig.uuid: sig.y_range for sig in self.plot.signals},
            }

            if inplace:
                if not self.zoom_history:
                    self.zoom_history.append(snapshot)
                    self.zoom_history_index = 0
                else:
                    self.zoom_history[self.zoom_history_index] = snapshot
            else:
                if not self.zoom_history or self.zoom_history[self.zoom_history_index] != snapshot:
                    self.zoom_history = self.zoom_history[: self.zoom_history_index + 1]

                    self.zoom_history.append(snapshot)
                    self.zoom_history_index = len(self.zoom_history) - 1

                self.redo_btn.setEnabled(False)
                if len(self.zoom_history) > 1:
                    self.undo_btn.setEnabled(True)


class PlotGraphics(pg.PlotWidget):
    cursor_moved = QtCore.Signal(object)
    cursor_removed = QtCore.Signal()
    range_removed = QtCore.Signal()
    range_modified = QtCore.Signal(object)
    range_modified_finished = QtCore.Signal(object)
    cursor_move_finished = QtCore.Signal(object)
    xrange_changed = QtCore.Signal(object, object)
    computation_channel_inserted = QtCore.Signal(object)
    curve_clicked = QtCore.Signal(str)
    signals_enable_changed = QtCore.Signal()
    current_uuid_changed = QtCore.Signal(str)
    edit_channel_request = QtCore.Signal(object, object)
    bookmarks_changed = QtCore.Signal(list, object)

    add_channels_request = QtCore.Signal(list)
    zoom_changed = QtCore.Signal(bool)

    def __init__(
        self,
        signals=None,
        with_dots=False,
        origin=None,
        mdf=None,
        line_interconnect="line",
        x_axis="time",
        plot_parent=None,
        allow_cursor=True,
        *args,
        **kwargs,
    ):
        viewBox = ViewBoxWithCursor(plot=self)
        self.initial_x_range = "adjust"
        super().__init__(viewBox=viewBox)

        # Die Klassenvorgabe hat die gemerkte Einstellung ueberschrieben.
        self.plotItem.vb.setLeftButtonAction(
            QtCore.QSettings().value("plot/mouse_mode", Plot.mouse_mode, str)
        )

        self.lock = Lock()

        self.plot_parent = plot_parent

        self.setViewportUpdateMode(QtWidgets.QGraphicsView.ViewportUpdateMode.FullViewportUpdate)

        self.autoFillBackground()

        self._pixmap = None
        self._prev_pixmap = None
        self._grid_pixmap = None

        self.locked = False

        self.cursor_unit = "s" if x_axis == "time" else "Hz"

        self.line_interconnect = line_interconnect if line_interconnect != "line" else ""

        self._can_trim = True
        self._can_paint = True
        self._can_compute_all_timebase = True
        self._can_paint_global = True
        self.mdf = mdf

        self.setAcceptDrops(True)

        self._last_size = self.geometry()
        self._settings = QtCore.QSettings()

        self.setContentsMargins(1, 1, 1, 1)
        self.xrange_changed.connect(self.xrange_changed_handle)
        self.with_dots = with_dots

        self.current_uuid = None
        self.digital_mode = False
        self.enum_mode = False

        # XY-Modus: auf der x-Achse steht nicht die Zeit, sondern der
        # Wert eines Signals aus diesem Strip.
        self.xy_mode = False
        self.x_uuid = None
        self._xy_cache = {}

        self.standalone = kwargs.get("standalone", False)

        self.region = None
        self.region_lock = None

        self.cursor1 = None
        self.cursor2 = None
        self.signals = []

        self.axes = []
        self._axes_layout_pos = 2

        self._timebase_db = {}
        self._timestamps_indexes = {}
        self.all_timebase = self.timebase = np.array([])
        for sig in self.signals:
            uuids = self._timebase_db.setdefault(id(sig.timestamps), set())
            uuids.add(sig.uuid)

        #        self._compute_all_timebase()

        self.showGrid(x=True, y=True)

        self.plot_item = self.plotItem
        self.plot_item.layout.removeItem(self.plot_item.titleLabel)
        self.plot_item.layout.removeItem(self.plot_item.axes["top"]["item"])
        self.plot_item.layout.removeItem(self.plot_item.axes["right"]["item"])

        self.plot_item.layout.setContentsMargins(0, 0, 0, 0)
        self.plot_item.hideButtons()
        self.plotItem.showGrid(x=False, y=False)
        self.layout = self.plot_item.layout
        self.scene_ = self.plot_item.scene()
        self.scene_.sigMouseClicked.connect(self._clicked)

        self.viewbox = self.plot_item.vb
        self.viewbox.border = None
        self.viewbox.disableAutoRange()

        self.viewbox.sigCursorMoved.connect(self._cursor_moved)
        self.viewbox.sigZoomFinished.connect(self._cursor_zoom_finished)
        self.viewbox.sigZoomChanged.connect(self._cursor_zoom)

        self.x_range = self.y_range = (0, 1)
        self._curve = pg.PlotCurveItem(
            np.array([]),
            np.array([]),
            stepMode=self.line_interconnect,
            skipFiniteCheck=False,
            connect="finite",
        )

        self.scene_.contextMenu = []
        self.plot_item.setMenuEnabled(False, None)

        self.common_axis_items = set()
        self.common_axis_label = ""
        self.common_axis_y_range = (0, 1)

        if allow_cursor:
            start, stop = self.viewbox.viewRange()[0]
            pos = QtCore.QPointF((start + stop) / 2, 0)

            if pg.getConfigOption("background") == "k":
                color = "white"
            else:
                color = "black"

            self.cursor1 = Cursor(pos=pos, angle=90, movable=True, pen=color, hoverPen=color)

            self.viewbox.cursor = self.cursor1
            self.viewbox.addItem(self.cursor1, ignoreBounds=True)

            self.cursor1.sigPositionChanged.connect(self.cursor_moved.emit)
            self.cursor1.sigPositionChangeFinished.connect(self.cursor_move_finished.emit)
            self.cursor_move_finished.emit(self.cursor1)
            self.cursor1.show()
        else:
            self.cursor1 = None

        self.viewbox.sigYRangeChanged.connect(self.y_changed)
        self.viewbox.sigRangeChangedManually.connect(self.y_changed)

        self.x_axis = FormatedAxis(
            "bottom", maxTickLength=5, background=self.backgroundBrush().color(), linked_signal=(self, None)
        )

        if x_axis == "time":
            fmt = self._settings.value("plot/xaxis_units")
            if fmt == "seconds" or not fmt:
                fmt = "phys"
        else:
            fmt = "phys"
        self.x_axis.format = fmt
        self.x_axis.origin = origin.astimezone(LOCAL_TIMEZONE)

        self.y_axis = FormatedAxis(
            "left", maxTickLength=-5, background=self.backgroundBrush().color(), linked_signal=(self, None)
        )
        self.y_axis.setWidth(48)

        self.y_axis.scale_editor_requested.connect(self.open_scale_editor)
        self.y_axis.rangeChanged.connect(self.set_y_range)

        self.plot_item.setAxisItems({"left": self.y_axis, "bottom": self.x_axis})

        def plot_item_wheel_event(event):
            if event is not None:
                pos = event.pos()

                if pos.x() <= self.y_axis.width():
                    self.y_axis.wheelEvent(event)
                else:
                    for axis in self.axes:
                        if isinstance(axis, FormatedAxis) and axis.isVisible():
                            rect = axis.sceneBoundingRect()
                            if rect.contains(pos):
                                axis.wheelEvent(event)
                                break

        def plot_item_mousePressEvent(event):
            if event is not None:
                pos = event.pos()

                if pos.x() <= self.y_axis.width():
                    if not self.locked:
                        self.y_axis.mousePressEvent(event)
                else:
                    for axis in self.axes:
                        if isinstance(axis, FormatedAxis) and axis.isVisible():
                            rect = axis.sceneBoundingRect()
                            if rect.contains(pos):
                                if not self.locked:
                                    axis.mousePressEvent(event)
                                break
                    else:
                        self.plot_item._mousePressEvent(event)

        def plot_item_mouseMoveEvent(event):
            if event is not None:
                pos = event.pos()

                if pos.x() <= self.y_axis.width():
                    self.y_axis.mouseMoveEvent(event)
                else:
                    for axis in self.axes:
                        if isinstance(axis, FormatedAxis) and axis.isVisible():
                            rect = axis.sceneBoundingRect()
                            if rect.contains(pos):
                                if not self.locked:
                                    axis.mouseMoveEvent(event)
                                break
                    else:
                        self.plot_item._mouseMoveEvent(event)

        def plot_item_mouseReleaseEvent(event):
            if event is not None:
                pos = event.pos()

                if pos.x() <= self.y_axis.width():
                    self.y_axis.mouseReleaseEvent(event)
                else:
                    for axis in self.axes:
                        if isinstance(axis, FormatedAxis) and axis.isVisible():
                            rect = axis.sceneBoundingRect()
                            if rect.contains(pos):
                                if not self.locked:
                                    axis.mouseReleaseEvent(event)
                                break
                    else:
                        self.plot_item._mouseReleaseEvent(event)

        self.plot_item.wheelEvent = plot_item_wheel_event
        self.plot_item._mousePressEvent = self.plot_item.mousePressEvent
        self.plot_item._mouseMoveEvent = self.plot_item.mouseMoveEvent
        self.plot_item._mouseReleaseEvent = self.plot_item.mouseReleaseEvent
        self.plot_item.mousePressEvent = plot_item_mousePressEvent
        self.plot_item.mouseMoveEvent = plot_item_mouseMoveEvent
        self.plot_item.mouseReleaseEvent = plot_item_mouseReleaseEvent

        self.viewbox_geometry = self.viewbox.sceneBoundingRect().toRect()

        self.viewbox.sigResized.connect(partial(self.xrange_changed_handle, force=True))

        self._uuid_map = {}

        self._enabled_changed_signals = []
        self._enable_timer = QtCore.QTimer()
        self._enable_timer.setSingleShot(True)
        self._enable_timer.timeout.connect(self._signals_enabled_changed_handler)

        self._update_timer = QtCore.QTimer()
        self._update_timer.setSingleShot(True)
        self._update_timer.timeout.connect(self.update)
        self._update_timer.setInterval(16)

        self._inhibit = False

        self.viewbox.setXRange(0, 10, update=False)

        if signals:
            self.add_new_channels(signals)

        self.viewbox.sigXRangeChanged.connect(self.xrange_changed.emit)

        self._bookmarks = []
        bookmarks = kwargs.pop("events", ())
        if not bookmarks:
            bookmarks = kwargs.pop("bookmarks", ())
        if bookmarks:
            self.bookmarks = bookmarks

        self.disabled_keyboard_events = set()

        self.viewbox.sigResized.connect(self.update_views)
        if signals:
            self.update_views()

        self.zoom = None

        self.px = 1
        self.py = 1

        self.last_click = perf_counter()
        self.flash_current_signal = 0
        self.flash_curve_timer = QtCore.QTimer()
        self.flash_curve_timer.setSingleShot(True)
        self.flash_curve_timer.timeout.connect(self.update)

        self.block_zoom_signal = False

    def add_new_channels(self, channels, descriptions=None):
        descriptions = descriptions or {}

        initial_index = len(self.signals)
        self._can_paint = False

        for sig in channels.values():
            if not sig.flags & Signal.Flags.computed:
                sig.computation = {}

        if initial_index == 0:
            start_t, stop_t = np.inf, -np.inf
            for sig in channels.values():
                if len(sig):
                    start_t = min(start_t, sig.timestamps[0])
                    stop_t = max(stop_t, sig.timestamps[-1])

            if (start_t, stop_t) != (np.inf, -np.inf):
                if self.initial_x_range == "adjust":
                    self.viewbox.setXRange(start_t, stop_t, update=False)
                else:
                    delta = self.viewbox.viewRange()[0][1] - self.viewbox.viewRange()[0][0]
                    stop_t = start_t + delta
                    self.viewbox.setXRange(start_t, stop_t, padding=0, update=False)

        (start, stop), _ = self.viewbox.viewRange()

        width = self.viewbox.sceneBoundingRect().toRect().width()
        trim_info = start, stop, width

        channels = [
            PlotSignal(sig, i, trim_info=trim_info) for i, sig in enumerate(channels.values(), len(self.signals))
        ]

        for sig in self.signals:
            sig.path = None

        self.signals.extend(channels)
        for sig in channels:
            uuids = self._timebase_db.setdefault(id(sig.timestamps), set())
            uuids.add(sig.uuid)
        self._compute_all_timebase()

        self._uuid_map = {sig.uuid: (sig, i) for i, sig in enumerate(self.signals)}

        axis_uuid = None

        for index, sig in enumerate(channels, initial_index):
            description = descriptions.get(sig.uuid, {})
            if description:
                sig.enable = description.get("enabled", True)
                sig.format = description.get("format", "phys")

            y_range = description.get("y_range", None)
            if y_range:
                mn, mx = y_range
                if mn > mx:
                    y_range = mx, mn
                else:
                    y_range = tuple(y_range)

                sig.y_range = y_range

            elif not sig.empty:
                sig.y_range = sig.min, sig.max

            # erst ganz zum Schluss sperren, sonst greift der Bereich oben
            # nicht mehr
            sig.y_locked = description.get("y_locked", False)

            self.axes.append(self._axes_layout_pos)
            self._axes_layout_pos += 1

            if initial_index == 0 and index == 0:
                axis_uuid = sig.uuid

        if axis_uuid is not None:
            self.set_current_uuid(sig.uuid)
            if len(self.all_timebase) and self.cursor1 is not None:
                self.cursor1.set_value(self.all_timebase[0])

        self.viewbox._matrixNeedsUpdate = True
        self.viewbox.updateMatrix()

        self.zoom_changed.emit(True)

        return {sig.uuid: sig for sig in channels}

    def auto_clip_rect(self, painter, viewbox_rect):
        rect = QtCore.QRectF(viewbox_rect)
        rect.setX(rect.x() + 5)
        rect.setWidth(rect.width() - 5)
        rect.setHeight(rect.height() - 1)
        painter.setClipRect(rect)
        painter.setClipping(True)

        return rect

    @property
    def bookmarks(self):
        return self._bookmarks

    @bookmarks.setter
    def bookmarks(self, bkms):
        for bookmark in self._bookmarks:
            self.viewbox.removeItem(bookmark)

        bkms = bkms or []
        self._bookmarks.clear()

        for i, bookmark in enumerate(bkms):
            if isinstance(bookmark, Bookmark):
                self._bookmarks.append(bookmark)
                self.viewbox.addItem(bookmark)
            else:
                color = serde.COLORS[serde.COLORS_COUNT - (i % serde.COLORS_COUNT) - 1]
                if isinstance(bookmark, (list, tuple)):
                    to_display = bookmark
                    labels = [" - Start", " - End"]
                else:
                    to_display = [bookmark]
                    labels = [""]
                for event, label in zip(to_display, labels, strict=False):
                    bookmark = Bookmark(
                        pos=event["value"],
                        message=event["description"],
                        title=f"{event['type']}{label}",
                        color=color,
                        tool=event.get("tool", ""),
                    )
                    self._bookmarks.append(bookmark)
                    self.viewbox.addItem(bookmark)

        self.update()

    def _clicked(self, event):
        modifiers = QtWidgets.QApplication.keyboardModifiers()

        scene_pos = event.scenePos()
        pos = self.plot_item.vb.mapSceneToView(scene_pos)

        start, stop = self.viewbox.viewRange()[0]
        if not start <= pos.x() <= stop:
            return

        x = pos.x()
        y = scene_pos.y()

        for bookmark in self._bookmarks:
            if not bookmark.visible:
                continue

            rect = bookmark.label.textItem.sceneBoundingRect()
            if rect.contains(scene_pos):
                if bookmark.editable:
                    edit_rect = QtCore.QRectF(
                        rect.x() + rect.width() - 35,
                        rect.y() + 1,
                        18,
                        18,
                    )

                    if edit_rect.contains(scene_pos):
                        comment, submit = QtWidgets.QInputDialog.getMultiLineText(
                            self,
                            "Edit bookmark comments",
                            f"Enter new comments for cursor position {bookmark.value():.9f}s:",
                            bookmark.message,
                        )
                        if submit:
                            bookmark.message = comment
                            bookmark.edited = True

                            self.update()

                            self.bookmarks_changed.emit(
                                [bookmark.copy() for bookmark in self._bookmarks], self.plot_parent
                            )

                    delete_rect = QtCore.QRectF(
                        rect.x() + rect.width() - 18,
                        rect.y() + 1,
                        18,
                        18,
                    )
                    if delete_rect.contains(scene_pos):
                        bookmark.deleted = True
                        self.viewbox.removeItem(bookmark)
                        self.update()

                        self.bookmarks_changed.emit([bookmark.copy() for bookmark in self._bookmarks], self.plot_parent)

                break
        else:
            if (
                QtCore.QKeyCombination(QtCore.Qt.Key.Key_C, QtCore.Qt.KeyboardModifier.ControlModifier).toCombined()
                not in self.disabled_keyboard_events
            ):
                if self.region is not None:
                    start, stop = self.region.getRegion()

                    if self.region_lock is not None:
                        self.region.moving_cursor.setPos(pos)
                    else:
                        if modifiers == QtCore.Qt.KeyboardModifier.ControlModifier:
                            self.region.setRegion((start, pos.x()))
                        else:
                            self.region.setRegion((pos.x(), stop))

                else:
                    if self.cursor1 is not None:
                        self.cursor1.setPos(self.cursor_position(pos))
                        self.cursor1.sigPositionChangeFinished.emit(self.cursor1)

            now = perf_counter()
            if modifiers == QtCore.Qt.KeyboardModifier.ControlModifier:
                self.select_curve(x, y)
            elif now - self.last_click < 0.3:
                self.select_curve(x, y)

        self.last_click = perf_counter()

    def close(self):
        self.viewbox.blockSignals(True)
        self.scene_.blockSignals(True)
        if self.cursor1 is not None:
            self.cursor1.blockSignals(True)
        if self.region is not None:
            self.region.blockSignals(True)
        self._enable_timer.stop()
        self._enable_timer.blockSignals(True)
        self.flash_curve_timer.stop()
        self.flash_curve_timer.blockSignals(True)
        self.y_axis.blockSignals(True)

        if self.plotItem is not None:
            self._can_trim = False
            self._can_paint = False
            self._can_compute_all_timebase = False
            self._can_paint_global = False
            self.plot_parent = None
            self.mdf = None
            super().close()

    def _compute_all_timebase(self):
        if self._can_compute_all_timebase:
            if self._timebase_db:
                stamps = {id(sig.timestamps): sig.timestamps for sig in self.signals}

                timebases = [timestamps for id_, timestamps in stamps.items() if id_ in self._timebase_db]

                count = len(timebases)

                if count == 0:
                    new_timebase = np.array([])
                elif count == 1:
                    new_timebase = timebases[0]
                else:
                    try:
                        new_timebase = np.unique(np.concatenate(timebases))
                    except MemoryError:
                        new_timebase = reduce(np.union1d, timebases)

                self.all_timebase = self.timebase = new_timebase
            else:
                self.all_timebase = self.timebase = np.array([])

    def _cursor_moved(self, event):
        pos = self.plot_item.vb.mapSceneToView(event.scenePos()).x()
        start, stop = self.viewbox.viewRange()[0]
        if not start <= pos <= stop:
            return

        scene_pos = event.scenePos()
        pos = self.plot_item.vb.mapSceneToView(scene_pos)
        x = pos.x()
        y = event.scenePos().y()

        if self.region and self.region.moving_cursor is not None:
            self.region.moving_cursor.setPos(pos)

        elif self.cursor1 is not None:
            self.cursor1.setPos(self.cursor_position(pos))
            self.cursor1.sigPositionChangeFinished.emit(self.cursor1)

    def cursor_position(self, pos):
        """Wohin der Cursor beim Klicken gehoert.

        Im Zeitbetrieb genau dorthin, wo geklickt wurde. Im XY-Strip ist
        die Stelle ein Wertepaar und keine Zeit - dort wird der
        naechstgelegene Punkt der gezeichneten Kurven genommen, so wie es
        das XY-Fenster auch macht.
        """
        if not self.xy_mode:
            return pos

        stempel = self.xy_timestamp_at(pos)

        return pos if stempel is None else stempel

    def xy_timestamp_at(self, pos):
        """Zeitpunkt des Kurvenpunktes, der dieser Stelle am naechsten liegt.

        Verglichen wird im Bild, nicht in den Werten: beide Achsen werden
        auf 0..1 gebracht, sonst gewaenne immer die Achse mit den
        groesseren Zahlen.
        """
        x0, x1 = self.x_range
        y0, y1 = self.viewbox.viewRange()[1]

        if x1 <= x0 or y1 <= y0:
            return None

        ziel_x = (pos.x() - x0) / (x1 - x0)
        ziel_y = (pos.y() - y0) / (y1 - y0)

        bester = None
        kleinster = None

        for sig in self.signals:
            if not sig.enable:
                continue

            paar = self.xy_pairs(sig)
            if paar is None:
                continue

            x_werte, y_werte = paar
            if not len(x_werte):
                continue

            try:
                sig_low, sig_high = (float(e) for e in sig.y_range)
            except (TypeError, ValueError):
                continue

            if sig_high <= sig_low:
                continue

            dx = (x_werte - x0) / (x1 - x0) - ziel_x
            dy = (y_werte - sig_low) / (sig_high - sig_low) - ziel_y

            abstand = dx * dx + dy * dy
            index = int(np.nanargmin(abstand))

            if kleinster is None or abstand[index] < kleinster:
                kleinster = abstand[index]
                bester = float(sig.timestamps[index])

        return bester

    def _cursor_zoom(self, zoom):
        self.zoom = zoom
        if zoom is not None:
            self.update()

    def _cursor_zoom_finished(self, zoom=None):
        p1, p2, zoom_mode = zoom

        self.block_zoom_signal = True

        if zoom_mode in (self.viewbox.Y_zoom, *self.viewbox.XY_zoom) and not self.locked:
            y1, y2 = sorted([p1.y(), p2.y()])
            y_bottom, y_top = self.viewbox.viewRange()[1]
            r_top = (y2 - y_bottom) / (y_top - y_bottom)
            r_bottom = (y1 - y_bottom) / (y_top - y_bottom)

            for sig in self.signals:
                sig_y_bottom, sig_y_top = sig.y_range
                delta = sig_y_top - sig_y_bottom
                sig_y_top = sig_y_bottom + r_top * delta
                sig_y_bottom = sig_y_bottom + r_bottom * delta

                sig, idx = self.signal_by_uuid(sig.uuid)

                axis = self.axes[idx]
                if isinstance(axis, FormatedAxis):
                    axis.setRange(sig_y_bottom, sig_y_top)
                else:
                    self.set_y_range(sig.uuid, (sig_y_bottom, sig_y_top))

        if zoom_mode in (self.viewbox.X_zoom, *self.viewbox.XY_zoom):
            x1, x2 = sorted([p1.x(), p2.x()])
            self.viewbox.setXRange(x1, x2, padding=0)

        self.block_zoom_signal = False
        self.zoom_changed.emit(False)

    def curve_clicked_handle(self, curve, ev, uuid):
        self.curve_clicked.emit(uuid)

    def delete_channels(self, deleted):
        self._can_paint = False

        needs_timebase_compute = False

        uuid_map = self._uuid_map

        indexes = sorted(
            [(uuid_map[uuid][1], uuid) for uuid in deleted if uuid in uuid_map],
            reverse=True,
        )

        for i, uuid in indexes:
            item = self.axes.pop(i)
            if isinstance(item, FormatedAxis):
                self.layout.removeItem(item)
                item.scene().removeItem(item)
                item.unlinkFromView()

            sig = self.signals.pop(i)

            if uuid in self.common_axis_items:
                self.common_axis_items.remove(uuid)

            if sig.enable:
                try:
                    self._timebase_db[id(sig.timestamps)].remove(sig.uuid)

                    if len(self._timebase_db[id(sig.timestamps)]) == 0:
                        del self._timebase_db[id(sig.timestamps)]
                        needs_timebase_compute = True
                except KeyError:
                    pass

        uuids = [sig.uuid for sig in self.signals]

        self._uuid_map = {sig.uuid: (sig, i) for i, sig in enumerate(self.signals)}

        if uuids:
            if self.current_uuid in uuids:
                self.set_current_uuid(self.current_uuid, True)
            else:
                self.set_current_uuid(uuids[0], True)
        else:
            self.current_uuid = None

            # Leerer Strip: sonst bleibt die Achse mit Beschriftung und
            # Farbe des zuletzt gezeigten Signals stehen.
            match self._settings.value("plot/background"):
                case "Black":
                    foreground = "#FFFFFF"
                case "White":
                    foreground = "#000000"
                case _:
                    foreground = self._settings.value("plot/foreground", "#ffffff")

            self.y_axis.setLabel("")
            self.y_axis.text_conversion = None
            self.y_axis.set_pen(fn.mkPen(foreground))
            self.y_axis.setTextPen(foreground)
            self.y_axis.picture = None
            self.y_axis.update()

        if needs_timebase_compute:
            self._compute_all_timebase()

        self._can_paint = True
        self.update()

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat("application/octet-stream-asammdf"):
            e.accept()

        super().dragEnterEvent(e)

    def grid_time_strip(self):
        """Welcher Strip liefert die Teilstriche fuer das Zeitgitter?

        Die Zeitachse zeigt nur noch der unterste Strip, und eine
        versteckte Achse rechnet keine Teilstriche aus. Also nimmt jeder
        Strip die Striche der einen sichtbaren Zeitachse - alle Strips
        zeigen ohnehin denselben Zeitbereich. Sind alle Achsen aus,
        kommt None zurueck und es gibt kein Zeitgitter.
        """
        if self.x_axis.isVisible():
            return self

        parent = getattr(self, "plot_parent", None)

        for strip in reversed(getattr(parent, "strips", None) or ()):
            axis = getattr(strip, "x_axis", None)
            if axis is not None and axis.isVisible() and strip.px:
                return strip

        return None

    def draw_grids(self, paint, viewbox_rect, ratio):
        if self._grid_pixmap is None:
            _pixmap = QtGui.QPixmap(ceil(ceil(viewbox_rect.width()) * ratio), ceil(ceil(viewbox_rect.height()) * ratio))
            _pixmap.fill(QtCore.Qt.transparent)

            paint = QtGui.QPainter()
            if _pixmap.isNull():
                    return
            paint.begin(_pixmap)
            paint.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceOver)
            paint.setRenderHints(paint.RenderHint.Antialiasing, False)

            if self.y_axis.grid or self.x_axis.grid:

                if self.y_axis.grid and self.y_axis.isVisible():
                    for pen, p1, p2 in self.y_axis.tickSpecs:
                        pen2 = fn.mkPen(pen)
                        pen2.setStyle(QtCore.Qt.PenStyle.DashLine)
                        y_pos = p1.y()
                        paint.setPen(pen2)
                        paint.drawLine(
                            QtCore.QPointF(0, y_pos),
                            QtCore.QPointF((viewbox_rect.x() + viewbox_rect.width()) * ratio, y_pos),
                        )

                quelle = self.grid_time_strip() if self.x_axis.grid else None

                if quelle is not None:
                    # Die Teilstriche kommen von der einen sichtbaren
                    # Zeitachse ganz unten. Breit sind die Strips
                    # verschieden (die Werteachsen links brauchen
                    # unterschiedlich viel Platz), darum wird die Lage
                    # jedes Strichs ueber seinen Zeitwert umgerechnet
                    # und nicht einfach uebernommen.
                    faktor = quelle.px / self.px if quelle is not self and self.px else 1.0
                    versatz = (quelle.x_range[0] - self.x_range[0]) / self.px if self.px else 0.0

                    for pen, p1, p2 in quelle.x_axis.tickSpecs:
                        pen2 = fn.mkPen(pen)
                        pen2.setStyle(QtCore.Qt.PenStyle.DashLine)
                        x_pos = p1.x() * faktor + versatz
                        paint.setPen(pen2)
                        paint.drawLine(
                            QtCore.QPointF(x_pos, 0),
                            QtCore.QPointF(x_pos, (viewbox_rect.y() + viewbox_rect.height()) * ratio),
                        )

                paint.end()
                _pixmap.setDevicePixelRatio(self.devicePixelRatio())

                self._grid_pixmap = _pixmap
            else:
                paint.end()

        return self._grid_pixmap

    def dropEvent(self, e):
        parent = self.plot_parent

        target = self

        # Der Strip, auf den fallen gelassen wird, wird der aktive -
        # sonst landen die Kanaele in irgendeinem anderen.
        if parent is not None and hasattr(parent, "set_active_strip"):
            parent.set_active_strip(target)

        own_list = getattr(self, "channel_list", None)
        if own_list is not None and e.source() is own_list and target is self:
            super().dropEvent(e)
        elif self._moved_from_other_strip(e, target):
            e.accept()
        else:
            data = e.mimeData()
            if data.hasFormat("application/octet-stream-asammdf"):
                names = extract_mime_names(data)
                self.add_channels_request.emit(names)
            else:
                e.ignore()

    def _moved_from_other_strip(self, e, target=None):
        """Zug aus einem anderen Strip desselben Fensters: verschieben.

        Liefert True, wenn der Umzug erledigt ist.
        """
        parent = self.plot_parent
        if parent is None or not hasattr(parent, "move_signals_between_strips"):
            return False

        source = e.source()
        if source not in parent.channel_lists():
            return False

        return parent.move_signals_between_strips(source, target or self)

    def edit_computation(self, item):
        signal = item.signal
        functions = self.plot_parent.owner.functions

        mdf = self.mdf or self.plot_parent.owner.generate_mdf()
        dlg = DefineChannel(
            mdf=mdf,
            computation=signal.computation,
            functions=functions,
            global_variables=self.plot_parent.owner.effective_global_variables(),
            parent=self,
        )
        dlg.setModal(True)
        dlg.exec()
        computed_channel = dlg.payload

        if self.mdf is None:
            mdf.close()

        if computed_channel is not None:
            self.edit_channel_request.emit(computed_channel, item)

    def generatePath(self, x, y, sig=None):
        if sig is None or sig.path is None:
            if x is None or len(x) == 0 or y is None or len(y) == 0:
                path = QtGui.QPainterPath()
            else:
                path = self._curve.generatePath(x, y)
            if sig is not None:
                sig.path = path
        else:
            path = sig.path

        return path

    def get_axis(self, index):
        axis = self.axes[index]
        if isinstance(axis, int):
            sig = self.signals[index]
            position = axis

            axis = FormatedAxis(
                "left",
                pen=sig.pen,
                textPen=sig.pen,
                text=sig.name if len(sig.name) <= 32 else "{sig.name[:29]}...",
                units=sig.unit,
                uuid=sig.uuid,
                locked=self.locked,
                maxTickLength=5,
                background=self.backgroundBrush().color(),
                linked_signal=(self, sig.uuid),
            )
            axis.scale_editor_requested.connect(self.open_scale_editor)
            if sig.conversion and hasattr(sig.conversion, "text_0"):
                axis.text_conversion = sig.conversion

            axis.setRange(*sig.y_range)

            axis.rangeChanged.connect(self.set_y_range)
            axis.hide()
            self.layout.addItem(axis, 2, position)

            self.axes[index] = axis

        return axis

    def get_current_timebase(self):
        if self.current_uuid:
            sig, _ = self._uuid_map[self.current_uuid]
            t = sig.timestamps
            if t.size:
                return t
            else:
                return self.all_timebase
        else:
            return self.all_timebase

    def get_stats(self, uuid):
        try:
            sig, index = self.signal_by_uuid(uuid)
        except KeyError:
            return {}
        else:
            return sig.get_stats(
                cursor=self.cursor1.value() if self.cursor1 else None,
                region=self.region.getRegion() if self.region else None,
                view_region=self.viewbox.viewRange()[0],
                precision=self._settings.value("stats_float_precision", sig.precision, type=int),
            )

    def get_timestamp_index(self, timestamp, timestamps):
        key = id(timestamps), timestamp
        if key in self._timestamps_indexes:
            return self._timestamps_indexes[key]
        else:
            if timestamps.size:
                if timestamp >= timestamps[-1]:
                    index = -1
                elif timestamp <= timestamps[0]:
                    index = 0
                else:
                    index = np.searchsorted(timestamps, timestamp, side="right") - 1
            else:
                index = None

            if len(self._timestamps_indexes) > 100000:
                self._timestamps_indexes.clear()
            self._timestamps_indexes[key] = index
            return index

    def insert_computation(self, name=""):
        functions = self.plot_parent.owner.functions
        if not ChainMap(functions, FunctionLibrary):
            MessageBox.warning(
                self,
                "Cannot add computed channel",
                "There is no user defined function. Create new function using the Functions Manger (F6)",
            )
            return

        mdf = self.mdf or self.plot_parent.owner.generate_mdf()
        dlg = DefineChannel(
            mdf=mdf,
            computation=None,
            functions=functions,
            global_variables=self.plot_parent.owner.effective_global_variables(),
            parent=self,
        )
        dlg.setModal(True)
        dlg.exec()
        computed_channel = dlg.payload

        if self.mdf is None:
            mdf.close()

        if computed_channel is not None:
            self.add_channels_request.emit([computed_channel])

    def keyPressEvent(self, event):
        if event.keyCombination().toCombined() in self.disabled_keyboard_events:
            event.ignore()
            return

        key = event.key()
        modifier = event.modifiers()

        handled = True
        if key in (QtCore.Qt.Key.Key_X, QtCore.Qt.Key.Key_Y) and modifier == QtCore.Qt.KeyboardModifier.NoModifier:
            # Zoomachse umschalten, wie in vSignalyzer. Gilt sofort fuer
            # alle Strips und alle Fenster, nicht nur fuer dieses hier.
            ViewBoxWithCursor.set_zoom_axis("x" if key == QtCore.Qt.Key.Key_X else "y")

        elif key == QtCore.Qt.Key.Key_Y and modifier == QtCore.Qt.KeyboardModifier.ControlModifier:
            if self.region is None:
                pos = self.cursor1.value()
                event_ = QtGui.QKeyEvent(
                    QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_R, QtCore.Qt.KeyboardModifier.NoModifier
                )
                self.keyPressEvent(event_)

                self.region.setRegion([pos, pos])

            if self.region_lock is not None:
                self.region_lock = None
                for i in range(2):
                    self.region.lines[i].setMovable(True)
                    self.region.lines[i].locked = False
                self.region.movable = True
                self.region.moving_cursor = self.region.lines[0]
            else:
                self.region_lock = self.region.getRegion()[0]
                self.region.lines[0].setMovable(False)
                self.region.lines[0].locked = True
                self.region.lines[1].setMovable(True)
                self.region.lines[1].locked = False
                self.region.moving_cursor = self.region.lines[1]
                self.region.movable = False

            self.update()

        elif key == QtCore.Qt.Key.Key_Y and modifier == QtCore.Qt.KeyboardModifier.ShiftModifier:
            if self.region is None:
                event_ = QtGui.QKeyEvent(
                    QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_R, QtCore.Qt.KeyboardModifier.NoModifier
                )
                self.keyPressEvent(event_)

                self.region.lines[0].setMovable(False)
                self.region.lines[0].locked = True
                self.region.lines[1].setMovable(True)
                self.region.lines[1].locked = False
                self.region.movable = False
                self.region.moving_cursor = self.region.lines[1]
                self.region_lock = self.region.lines[0].value()

            else:
                if self.region.lines[0].isMovable():
                    self.region_lock = self.region.lines[0].value()
                    self.region.lines[0].setMovable(False)
                    self.region.lines[0].locked = True
                    self.region.lines[1].setMovable(True)
                    self.region.lines[1].locked = False
                    self.region.moving_cursor = self.region.lines[1]
                else:
                    self.region_lock = self.region.lines[1].value()
                    self.region.lines[0].setMovable(True)
                    self.region.lines[0].locked = False
                    self.region.lines[1].setMovable(False)
                    self.region.lines[1].locked = True
                    self.region.moving_cursor = self.region.lines[0]

            self.update()

        elif key == QtCore.Qt.Key.Key_X and modifier == QtCore.Qt.KeyboardModifier.ControlModifier:
            # frueher auf X allein - X schaltet jetzt die Zoomachse um
            if self.region is not None:
                self.viewbox.setXRange(*self.region.getRegion(), padding=0)
                event_ = QtGui.QKeyEvent(
                    QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_R, QtCore.Qt.KeyboardModifier.NoModifier
                )
                self.keyPressEvent(event_)

        elif key == QtCore.Qt.Key.Key_F and modifier == QtCore.Qt.KeyboardModifier.NoModifier and not self.locked:
            self.block_zoom_signal = True
            if self.common_axis_items:
                if any(
                    not self.signal_by_uuid(uuid)[0].empty
                    for uuid in self.common_axis_items
                    if self.signal_by_uuid(uuid)[0].enable
                ):
                    common_min = np.nanmin(
                        [
                            self.signal_by_uuid(uuid)[0].min
                            for uuid in self.common_axis_items
                            if not self.signal_by_uuid(uuid)[0].empty
                        ]
                    )
                    common_max = np.nanmax(
                        [
                            self.signal_by_uuid(uuid)[0].max
                            for uuid in self.common_axis_items
                            if not self.signal_by_uuid(uuid)[0].empty
                        ]
                    )
                else:
                    common_min, common_max = 0, 1

            for i, signal in enumerate(self.signals):
                if not signal.empty:
                    if signal.uuid in self.common_axis_items:
                        min_ = common_min
                        max_ = common_max
                    else:
                        min_, max_ = signal.min, signal.max

                    if min_ != min_:  # noqa: PLR0124
                        # min_ is NaN
                        min_ = 0
                    if max_ != max_:  # noqa: PLR0124
                        # max_ is NaN
                        max_ = 1

                    delta = 0.01 * (max_ - min_)
                    min_, max_ = min_ - delta, max_ + delta

                    signal.y_range = min_, max_

                    if signal.uuid == self.current_uuid:
                        self.viewbox.setYRange(float(min_), float(max_), padding=0)

            self.block_zoom_signal = False
            self.zoom_changed.emit(False)
            self.update()

        elif key == QtCore.Qt.Key.Key_F and modifier == QtCore.Qt.KeyboardModifier.ShiftModifier and not self.locked:
            self.block_zoom_signal = True
            parent = self.plot_parent
            uuids = [
                item.uuid
                for item in parent.channel_selection.selectedItems()
                if item.type() == ChannelsTreeItem.Channel
            ]
            uuids = set(uuids)

            if not uuids:
                return

            for i, signal in enumerate(self.signals):
                if signal.uuid not in uuids:
                    continue

                if len(signal.plot_samples):
                    samples = signal.plot_samples
                    if len(samples):
                        min_, max_ = signal.min, signal.max
                    else:
                        min_, max_ = 0, 1

                    if min_ != min_:  # noqa: PLR0124
                        # min_ is NaN
                        min_ = 0
                    if max_ != max_:  # noqa: PLR0124
                        # max is NaN
                        max_ = 1

                    delta = 0.01 * (max_ - min_)
                    min_, max_ = min_ - delta, max_ + delta

                    signal.y_range = min_, max_
                    if signal.uuid == self.current_uuid:
                        self.viewbox.setYRange(min_, max_, padding=0)

            self.block_zoom_signal = False
            self.zoom_changed.emit(False)
            self.update()

        elif key == QtCore.Qt.Key.Key_G:
            if modifier == QtCore.Qt.KeyboardModifier.NoModifier:
                next_grid = {
                    (False, False): (True, False),
                    (True, False): (True, True),
                    (True, True): (False, True),
                    (False, True): (False, False),
                }

                self.x_axis.grid, self.y_axis.grid = next_grid[(self.x_axis.grid, self.y_axis.grid)]

                self.x_axis.picture = None
                self.y_axis.picture = None
                self._grid_pixmap = None

                self.update()

            elif modifier == QtCore.Qt.KeyboardModifier.ShiftModifier:
                if self.cursor1 is not None:
                    value, ok = QtWidgets.QInputDialog.getDouble(
                        self,
                        "Go to time stamp",
                        "Time stamp",
                        value=self.cursor1.value(),
                        decimals=9,
                    )

                    if ok:
                        self.cursor1.setPos(value)
                        self.cursor_move_finished.emit(self.cursor1)

        elif key in (QtCore.Qt.Key.Key_I, QtCore.Qt.Key.Key_O) and modifier == QtCore.Qt.KeyboardModifier.NoModifier:
            x_range, _ = self.viewbox.viewRange()
            delta = x_range[1] - x_range[0]
            if key == QtCore.Qt.Key.Key_I:
                step = -delta * 0.25
            else:
                step = delta * 0.5
            if (
                self.cursor1
                and self.cursor1.isVisible()
                and self._settings.value("plot/zoom/x_center_on_cursor", True, type=bool)
            ):
                pos = self.cursor1.value()
                x_range = pos - delta / 2, pos + delta / 2

            self.viewbox.setXRange(x_range[0] - step, x_range[1] + step, padding=0)

        elif (
            key in (QtCore.Qt.Key.Key_I, QtCore.Qt.Key.Key_O)
            and modifier == QtCore.Qt.KeyboardModifier.ShiftModifier
            and not self.locked
        ):
            self.block_zoom_signal = True

            self.viewbox.vertical_zoom(zoom_in=key == QtCore.Qt.Key.Key_I)

            self.block_zoom_signal = False
            self.zoom_changed.emit(False)

        elif key == QtCore.Qt.Key.Key_R and modifier == QtCore.Qt.KeyboardModifier.NoModifier:
            if self.region is None:
                color = self.cursor1.pen.color().name()
                self.cursor1.hide()
                self.cursor1.store_position()

                self.region = Region(
                    (0, 0),
                    pen=color,
                    hoverPen=color,
                    show_circle=self.cursor1.show_circle,
                    show_horizontal_line=self.cursor1.show_horizontal_line,
                    line_width=self.cursor1.line_width,
                )
                # Der zweite Cursor bekommt eine eigene Farbe, sonst sind
                # Anfang und Ende des Bereichs nicht zu unterscheiden.
                self.region.lines[1].color = second_cursor_color(color)

                self.region.setZValue(-10)
                self.viewbox.addItem(self.region)
                self.region.sigRegionChanged.connect(self.range_modified.emit)
                self.region.sigRegionChanged.connect(self.range_modified_handler)
                self.region.sigRegionChangeFinished.connect(self.range_modified_finished_handler)
                start, stop = self.viewbox.viewRange()[0]

                if start <= self.cursor1.value() <= stop:
                    mid = (stop + start) / 2
                    start, stop = sorted([mid, self.cursor1.value()])

                else:
                    start, stop = (
                        start + 0.1 * (stop - start),
                        stop - 0.1 * (stop - start),
                    )

                self.region.setRegion((start, stop))

            else:
                self.region_lock = None
                self.region.setParent(None)
                self.region.hide()
                self.region.deleteLater()
                self.region = None
                self.range_removed.emit()

                if self.cursor1 is not None:
                    self.cursor1.restore_position()
                    self.cursor1.show()

            self.update()

        elif key == QtCore.Qt.Key.Key_S and modifier == QtCore.Qt.KeyboardModifier.ControlModifier:
            file_name, _ = QtWidgets.QFileDialog.getSaveFileName(
                self,
                "Save as measurement file",
                "",
                "MDF version 4 files (*.mf4 *.mf4z)",
            )

            if file_name:
                signals = [signal for signal in self.signals if signal.enable]
                if signals:
                    with mdf_module.MDF() as mdf:
                        groups = {}
                        for sig in signals:
                            id_ = id(sig.timestamps)
                            group_ = groups.setdefault(id_, [])
                            group_.append(sig)

                        for signals in groups.values():
                            sigs = []
                            for signal in signals:
                                if ":" in signal.name:
                                    sig = signal.copy()
                                    sig.name = sig.name.split(":")[-1].strip()
                                    sigs.append(sig)
                                else:
                                    sigs.append(signal)
                            mdf.append(sigs, common_timebase=True)

                        file_name = Path(file_name)

                        if file_name.suffix.lower() in (".zip", ".mf4z"):
                            tmpf = Path(gettempdir()) / f"{perf_counter()}.mf4"
                            mdf.save(tmpf, overwrite=True, compression=2)

                            zipped_mf4 = ZipFile(file_name, "w", compression=ZIP_DEFLATED)
                            zipped_mf4.write(
                                str(tmpf),
                                file_name.with_suffix(".mf4").name,
                                compresslevel=1,
                            )

                            tmpf.unlink()

                        else:
                            mdf.save(file_name, overwrite=True, compression=2)

        elif key == QtCore.Qt.Key.Key_S and modifier == QtCore.Qt.KeyboardModifier.NoModifier and not self.locked:
            self.block_zoom_signal = True
            parent = self.plot_parent
            uuids = []

            iterator = QtWidgets.QTreeWidgetItemIterator(parent.channel_selection)
            while item := iterator.value():
                if item.type() == ChannelsTreeItem.Channel and item.signal.enable:
                    uuids.append(item.uuid)

                iterator += 1

            uuids = reversed(uuids)

            count = sum(
                1 for sig in self.signals if sig.min != "n.a." and sig.enable and sig.uuid not in self.common_axis_items
            )

            common_axis_signals = [
                sig for sig in self.signals if sig.min != "n.a." and sig.enable and sig.uuid in self.common_axis_items
            ]

            if common_axis_signals:
                count += 1

                common_min_ = np.nanmin([sig.min for sig in common_axis_signals])
                common_max_ = np.nanmax([sig.max for sig in common_axis_signals])
            if count:
                position = 0
                common_axis_handled = False
                for uuid in uuids:
                    signal, index = self.signal_by_uuid(uuid)

                    if not signal.empty and signal.enable:
                        if signal.uuid in self.common_axis_items:
                            if common_axis_handled:
                                continue

                            min_ = common_min_
                            max_ = common_max_

                        else:
                            min_ = signal.min
                            max_ = signal.max

                        if min_ == -float("inf") and max_ == float("inf"):
                            min_ = 0
                            max_ = 1
                        elif min_ == -float("inf"):
                            min_ = max_ - 1
                        elif max_ == float("inf"):
                            max_ = min_ + 1

                        min_, max_ = float(min_), float(max_)

                        if min_ == max_:
                            min_, max_ = min_ - 1, max_ + 1

                        dim = (float(max_) - min_) * 1.1

                        max_ = min_ + dim * count - 0.05 * dim
                        min_ = min_ - 0.05 * dim

                        min_, max_ = (
                            min_ - dim * position,
                            max_ - dim * position,
                        )

                        if signal.uuid in self.common_axis_items:
                            y_range = min_, max_
                            self.common_axis_y_range = y_range
                            for cuuid in self.common_axis_items:
                                sig, _ = self.signal_by_uuid(cuuid)
                                sig.y_range = y_range

                            common_axis_handled = True

                        else:
                            signal.y_range = min_, max_

                        if signal.uuid == self.current_uuid:
                            self.viewbox.setYRange(min_, max_, padding=0)

                        position += 1

            else:
                xrange, _ = self.viewbox.viewRange()
                self.viewbox.autoRange(padding=0)
                self.viewbox.setXRange(*xrange, padding=0)
                self.viewbox.disableAutoRange()

            self.block_zoom_signal = False
            self.zoom_changed.emit(False)

            self.update()

        elif key == QtCore.Qt.Key.Key_S and modifier == QtCore.Qt.KeyboardModifier.ShiftModifier and not self.locked:
            self.block_zoom_signal = True
            parent = self.plot_parent
            uuids = [
                item.uuid
                for item in parent.channel_selection.selectedItems()
                if item.type() == ChannelsTreeItem.Channel
            ]
            uuids = list(reversed(uuids))
            uuids_set = set(uuids)

            if not uuids:
                return

            count = sum(
                1 for i, sig in enumerate(self.signals) if sig.uuid in uuids_set and sig.min != "n.a." and sig.enable
            )

            if count:

                common_axis_handled = False
                position = 0
                for uuid in uuids:
                    signal, index = self.signal_by_uuid(uuid)

                    if not signal.empty and signal.enable:
                        if uuid in self.common_axis_items:
                            if common_axis_handled:
                                continue

                            common_axis_signals = [
                                sig
                                for sig in self.signals
                                if sig.uuid in uuids_set
                                and sig.min != "n.a."
                                and sig.enable
                                and sig.uuid in self.common_axis_items
                            ]

                            min_ = np.nanmin([sig.min for sig in common_axis_signals])
                            max_ = np.nanmax([sig.max for sig in common_axis_signals])

                        else:
                            min_ = signal.min
                            max_ = signal.max

                        if min_ == -float("inf") and max_ == float("inf"):
                            min_ = 0
                            max_ = 1
                        elif min_ == -float("inf"):
                            min_ = max_ - 1
                        elif max_ == float("inf"):
                            max_ = min_ + 1

                        min_, max_ = float(min_), float(max_)

                        if min_ == max_:
                            min_, max_ = min_ - 1, max_ + 1

                        dim = (float(max_) - min_) * 1.1

                        max_ = min_ + dim * count - 0.05 * dim
                        min_ = min_ - 0.05 * dim

                        min_, max_ = (
                            min_ - dim * position,
                            max_ - dim * position,
                        )

                        if signal.uuid in self.common_axis_items:
                            y_range = min_, max_
                            self.common_axis_y_range = y_range
                            for cuuid in self.common_axis_items:
                                sig, _ = self.signal_by_uuid(cuuid)
                                sig.y_range = y_range

                            common_axis_handled = True

                        else:
                            signal.y_range = min_, max_

                        if signal.uuid == self.current_uuid:
                            self.viewbox.setYRange(min_, max_, padding=0)

                        position += 1

            else:
                xrange, _ = self.viewbox.viewRange()
                self.viewbox.autoRange(padding=0)
                self.viewbox.setXRange(*xrange, padding=0)
                self.viewbox.disableAutoRange()

            self.block_zoom_signal = False
            self.zoom_changed.emit(False)

            self.update()

        elif (
            key in (QtCore.Qt.Key.Key_Left, QtCore.Qt.Key.Key_Right)
            and modifier == QtCore.Qt.KeyboardModifier.AltModifier
        ):
            if self.region is None:
                pos = self.cursor1.value()
                sig, idx = self.signal_by_uuid(self.current_uuid)

                timestamp = sig.timestamp_of_next_different_value(
                    pos, mode="different", previous=key == QtCore.Qt.Key.Key_Left
                )

                self.cursor1.set_value(timestamp)

                view_range = self.viewbox.viewRange()[0]
                x_size = view_range[1] - view_range[0]
                self.viewbox.setXRange(timestamp - x_size / 2, timestamp + x_size / 2, padding=0)

            else:
                pos = self.region.moving_cursor.value()
                sig, idx = self.signal_by_uuid(self.current_uuid)

                timestamp = sig.timestamp_of_next_different_value(
                    pos, mode="different", previous=key == QtCore.Qt.Key.Key_Left
                )

                self.region.moving_cursor.set_value(timestamp)

                view_range = sorted([line.value() for line in self.region.lines])
                x_size = view_range[1] - view_range[0]
                self.viewbox.setXRange(view_range[0] - x_size * 0.05, view_range[1] + x_size * 0.05, padding=0)

        elif key in (QtCore.Qt.Key.Key_PageUp, QtCore.Qt.Key.Key_PageDown) and modifier in (
            QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.ShiftModifier,
            QtCore.Qt.KeyboardModifier.ControlModifier,
        ):
            if self.region is None:
                pos = self.cursor1.value()
                sig, idx = self.signal_by_uuid(self.current_uuid)

                timestamp = sig.timestamp_of_next_different_value(
                    pos,
                    mode="higher" if key == QtCore.Qt.Key.Key_PageUp else "lower",
                    previous=modifier
                    == QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.ShiftModifier,
                )

                self.cursor1.set_value(timestamp)

                view_range = self.viewbox.viewRange()
                x_size = view_range[1] - view_range[0]
                self.viewbox.setXRange(timestamp - x_size / 2, timestamp + x_size / 2, padding=0)

            else:
                pos = self.region.moving_cursor.value()
                sig, idx = self.signal_by_uuid(self.current_uuid)

                timestamp = sig.timestamp_of_next_different_value(
                    pos,
                    mode="higher" if key == QtCore.Qt.Key.Key_PageUp else "lower",
                    previous=modifier
                    == QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.ShiftModifier,
                )

                self.region.moving_cursor.set_value(timestamp)

                view_range = sorted([line.value() for line in self.region.lines])
                x_size = view_range[1] - view_range[0]
                self.viewbox.setXRange(view_range[0] - x_size * 0.05, view_range[1] + x_size * 0.05, padding=0)

        elif key in (QtCore.Qt.Key.Key_Left, QtCore.Qt.Key.Key_Right) and modifier in (
            QtCore.Qt.KeyboardModifier.NoModifier,
            QtCore.Qt.KeyboardModifier.ControlModifier,
        ):
            if self.region is None:
                if modifier == QtCore.Qt.KeyboardModifier.ControlModifier:
                    increment = 20
                else:
                    increment = 1

                prev_pos = pos = self.cursor1.value()
                x = self.get_current_timebase()
                dim = x.size
                if dim:
                    pos = np.searchsorted(x, pos)
                    if key == QtCore.Qt.Key.Key_Right:
                        pos += increment
                    else:
                        pos -= increment
                    pos = np.clip(pos, 0, dim - increment)
                    pos = x[pos]
                else:
                    if key == QtCore.Qt.Key.Key_Right:
                        pos += increment
                    else:
                        pos -= increment

                (left_side, right_side), _ = self.viewbox.viewRange()

                if pos >= right_side:
                    delta = abs(pos - prev_pos)
                    self.viewbox.setXRange(left_side + delta, right_side + delta, padding=0)
                elif pos <= left_side:
                    delta = abs(pos - prev_pos)
                    self.viewbox.setXRange(left_side - delta, right_side - delta, padding=0)

                self.cursor1.set_value(pos)

            else:
                increment = 1
                start, stop = self.region.getRegion()

                if self.region_lock is None:
                    if modifier == QtCore.Qt.KeyboardModifier.ControlModifier:
                        pos = stop
                        second_pos = start
                    else:
                        pos = start
                        second_pos = stop
                else:
                    if start != stop:
                        pos = start if stop == self.region_lock else stop
                    else:
                        pos = self.region_lock

                x = self.get_current_timebase()
                dim = x.size
                if dim:
                    pos = np.searchsorted(x, pos)
                    if key == QtCore.Qt.Key.Key_Right:
                        pos += increment
                    else:
                        pos -= increment
                    pos = np.clip(pos, 0, dim - increment)
                    pos = x[pos]
                else:
                    if key == QtCore.Qt.Key.Key_Right:
                        pos += increment
                    else:
                        pos -= increment

                (left_side, right_side), _ = self.viewbox.viewRange()

                if pos >= right_side:
                    self.viewbox.setXRange(left_side, pos, padding=0)
                elif pos <= left_side:
                    self.viewbox.setXRange(pos, right_side, padding=0)

                if self.region_lock is not None:
                    self.region.setRegion((self.region_lock, pos))
                else:
                    self.region.setRegion(tuple(sorted((second_pos, pos))))

        elif (
            key in (QtCore.Qt.Key.Key_Left, QtCore.Qt.Key.Key_Right)
            and modifier == QtCore.Qt.KeyboardModifier.ShiftModifier
        ):
            parent = self.plot_parent
            uuids = list(
                {
                    item.uuid
                    for item in parent.channel_selection.selectedItems()
                    if item.type() == ChannelsTreeItem.Channel
                }
            )

            if not uuids:
                return

            start, stop = self.viewbox.viewRange()[0]

            offset = (stop - start) / 100

            if key == QtCore.Qt.Key.Key_Left:
                offset = -offset

            self.set_time_offset([False, offset, *uuids])

        elif (
            key
            in (
                QtCore.Qt.Key.Key_Up,
                QtCore.Qt.Key.Key_Down,
                QtCore.Qt.Key.Key_PageUp,
                QtCore.Qt.Key.Key_PageDown,
            )
            and modifier == QtCore.Qt.KeyboardModifier.ShiftModifier
        ):
            parent = self.plot_parent
            uuids = list(
                {
                    item.uuid
                    for item in parent.channel_selection.selectedItems()
                    if item.type() == ChannelsTreeItem.Channel
                }
            )

            if not uuids:
                return

            factor = 10 if key in (QtCore.Qt.Key.Key_PageUp, QtCore.Qt.Key.Key_PageDown) else 100

            for uuid in uuids:
                signal, index = self.signal_by_uuid(uuid)

                bottom, top = signal.y_range
                step = (top - bottom) / factor

                if key in (QtCore.Qt.Key.Key_Up, QtCore.Qt.Key.Key_PageUp):
                    step = -step

                signal.y_range = bottom + step, top + step

            self.zoom_changed.emit(False)

            self.update()

        elif key == QtCore.Qt.Key.Key_H and modifier == QtCore.Qt.KeyboardModifier.NoModifier:
            start_ts, stop_ts = self.viewbox.viewRange()[0]

            if len(self.all_timebase):
                min_start_ts = np.amin(self.all_timebase)
                max_stop_ts = np.amax(self.all_timebase)
            else:
                min_start_ts = start_ts
                max_stop_ts = stop_ts

            rect = self.viewbox.sceneBoundingRect()
            width = rect.width() - 5

            dpi = QtWidgets.QApplication.primaryScreen().physicalDotsPerInchX()
            dpc = dpi / 2.54

            physical_viewbox_width = width / dpc  # cm
            time_width = physical_viewbox_width * HONEYWELL_SECONDS_PER_CM

            if self.cursor1.isVisible():
                mid = self.cursor1.value()
            else:
                mid = self.region.getRegion()[0]

            if mid - time_width / 2 < min_start_ts:
                start_ts = min_start_ts
                stop_ts = min_start_ts + time_width
            elif mid + time_width / 2 > max_stop_ts:
                start_ts = max_stop_ts - time_width
                stop_ts = max_stop_ts
            else:
                start_ts = mid - time_width / 2
                stop_ts = mid + time_width / 2

            self.viewbox.setXRange(start_ts, stop_ts, padding=0)

            if self.cursor1:
                self.cursor_moved.emit(self.cursor1)

        elif key == QtCore.Qt.Key.Key_W and modifier == QtCore.Qt.KeyboardModifier.NoModifier:
            ts_vals = []
            for sig in self.signals:
                if not sig.enable or not len(sig.timestamps):
                    continue

                ts_vals.append(sig.timestamps[0])
                ts_vals.append(sig.timestamps[-1])

            if ts_vals:
                ts_vals.sort()
                start_ts, stop_ts = ts_vals[0], ts_vals[-1]

                self.viewbox.setXRange(start_ts, stop_ts)

                if self.cursor1:
                    self.cursor_moved.emit(self.cursor1)

        elif key == QtCore.Qt.Key.Key_W and modifier == QtCore.Qt.KeyboardModifier.AltModifier:
            if len(self.all_timebase):
                start_ts = np.amin(self.all_timebase)
                stop_ts = np.amax(self.all_timebase)

                self.viewbox.setXRange(start_ts, stop_ts)

                if self.cursor1:
                    self.cursor_moved.emit(self.cursor1)

        elif key == QtCore.Qt.Key.Key_Insert and modifier == QtCore.Qt.KeyboardModifier.NoModifier:
            self.insert_computation()

        else:
            handled = False

        if not handled:
            event.ignore()
            self.parent().keyPressEvent(event)
        else:
            event.accept()

    def open_scale_editor(self, uuid):
        uuid = uuid or self.current_uuid

        if uuid is None:
            return

        signal, idx = self.signal_by_uuid(uuid)
        signals = {signal.name: signal}

        diag = ScaleDialog(signals, signal.y_range, parent=self)

        if diag.exec():
            offset = diag.offset.value()
            scale = diag.scaling.value()

            y_bottom = -offset * scale / 100
            y_top = y_bottom + scale

            signal.y_range = y_bottom, y_top

            self.zoom_changed.emit(False)
            self.update()

    def paintEvent(self, ev):
        if not self._can_paint or not self._can_paint_global:
            return

        viewbox_rect = self.viewbox.sceneBoundingRect()
        vb_x_range, vb_y_range = self.viewbox.viewRange()

        super().paintEvent(ev)

        ratio = self.devicePixelRatio()

        if self._pixmap is None:
            self._grid_pixmap = None

            _pixmap = QtGui.QPixmap(ceil(ceil(viewbox_rect.width()) * ratio), ceil(ceil(viewbox_rect.height()) * ratio))
            _pixmap.fill(QtCore.Qt.transparent)

            paint = QtGui.QPainter()
            if _pixmap.isNull():
                return
            paint.begin(_pixmap)
            paint.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceOver)
            paint.setRenderHints(paint.RenderHint.Antialiasing, False)

            self.x_range, self.y_range = vb_x_range, vb_y_range

            rect = QtCore.QRectF(viewbox_rect)
            rect.setSize(QtCore.QSize(ceil(ceil(rect.width()) * ratio), ceil(ceil(rect.height()) * ratio)))
            rect.moveTo(rect.topLeft() * ratio)

            self.px = (vb_x_range[1] - vb_x_range[0]) / rect.width()
            self.py = rect.height()

            with_dots = self.with_dots

            x_start = vb_x_range[0]

            no_brush = QtGui.QBrush()
            pen_width = self._settings.value("plot/curve/line_width", 1, type=int)
            dots_with = self._settings.value("plot/curve/dots_width", 4, type=int)

            paint.resetTransform()
            paint.translate(0, 0)
            paint.setBrush(no_brush)

            flash_current_signal = self.flash_current_signal

            if self._settings.value("plot/curve/dots_cap_style", "square") == "square":
                cap_style = QtCore.Qt.PenCapStyle.SquareCap
            else:
                cap_style = QtCore.Qt.PenCapStyle.RoundCap

            curve = self._curve
            step_mode = self.line_interconnect
            default_connect = curve.opts["connect"]

            for i, sig in enumerate(self.signals):
                if not sig.enable or (
                    flash_current_signal > 0 and flash_current_signal % 2 == 0 and sig.uuid == self.current_uuid
                ):
                    continue

                if self.xy_mode:
                    # Ueber der Zeit wird hier nichts gezeichnet: x kommt
                    # vom X-Signal. Gezeichnet wird die ganze Messung,
                    # nicht der sichtbare Zeitausschnitt.
                    paar = self.xy_pairs(sig)
                    if paar is None:
                        continue
                    x, y = paar
                else:
                    y = sig.plot_samples
                    x = sig.plot_timestamps

                if len(x):
                    if self.enum_mode and self.draw_enum_bands(paint, sig, x_start):
                        continue

                    x, y = self.scale_curve_to_pixmap(x, y, y_range=sig.y_range, x_start=x_start)

                    # Eigene Dicke und Linienart des Signals gehen vor.
                    # Gezeichnet wird mit einer Kopie des Stifts, damit
                    # die Achsen ihren eigenen behalten - sie benutzen
                    # dasselbe Objekt.
                    stift = QtGui.QPen(sig.pen)
                    stift.setWidth(getattr(sig, "line_width", 0) or pen_width)
                    stift.setStyle(LINE_STYLES.get(getattr(sig, "line_style", ""), QtCore.Qt.PenStyle.SolidLine))

                    paint.setPen(stift)

                    pth = self.generatePath(x, y, sig)

                    if self.digital_mode:
                        fill = self.digital_fill_path(pth, sig, x)
                        if fill is not None:
                            color = QtGui.QColor(sig.color)
                            color.setAlpha(110)
                            paint.fillPath(fill, color)

                    paint.drawPath(pth)

                    if with_dots:
                        pos = np.isfinite(y)
                        y = y[pos]
                        x = x[pos]

                        _pen = fn.mkPen(sig.color.name())
                        _pen.setWidth(dots_with)
                        _pen.setCapStyle(cap_style)
                        paint.setPen(_pen)

                        poly, arr = polygon_and_ndarray(x.size)

                        arr[:, 0] = x
                        arr[:, 1] = y
                        paint.drawPoints(poly)

                item = self.plot_parent.item_by_uuid(sig.uuid)
                if not item:
                    continue

                ranges = item.get_ranges()

                if ranges:
                    for range_info in ranges:
                        val = range_info["value1"]
                        if val is not None and isinstance(val, float):
                            op = range_info["op1"]
                            if op == ">":
                                idx1 = sig.plot_samples < val
                            elif op == ">=":
                                idx1 = sig.plot_samples <= val
                            elif op == "<":
                                idx1 = sig.plot_samples > val
                            elif op == "<=":
                                idx1 = sig.plot_samples >= val
                            elif op == "==":
                                idx1 = sig.plot_samples == val
                            elif op == "!=":
                                idx1 = sig.plot_samples != val
                        else:
                            idx1 = None

                        val = range_info["value2"]
                        if val is not None and isinstance(val, float):
                            op = range_info["op2"]
                            if op == ">":
                                idx2 = sig.plot_samples > val
                            elif op == ">=":
                                idx2 = sig.plot_samples >= val
                            elif op == "<":
                                idx2 = sig.plot_samples < val
                            elif op == "<=":
                                idx2 = sig.plot_samples <= val
                            elif op == "==":
                                idx2 = sig.plot_samples == val
                            elif op == "!=":
                                idx2 = sig.plot_samples != val
                        else:
                            idx2 = None

                        if idx1 is not None or idx2 is not None:
                            if idx1 is None:
                                idx = idx2
                            elif idx2 is None:
                                idx = idx1
                            else:
                                idx = idx1 & idx2

                            if not np.any(idx):
                                continue

                            if step_mode == "right":
                                idx_with_edges = get_idx_with_edges(idx)
                                _connect = "finite"
                                y = sig.plot_samples.astype("f8")
                                y[~idx_with_edges] = np.inf
                                x = sig.plot_timestamps

                            elif step_mode == "":
                                last = idx[-1]
                                idx_with_edges = np.roll(idx, -1)
                                idx_with_edges[-1] = last
                                _connect = idx_with_edges.view("u1")
                                y = sig.plot_samples.astype("f8")
                                x = sig.plot_timestamps

                            elif step_mode == "left":
                                idx_with_edges = np.repeat(get_idx_with_edges(idx), 2)
                                idx_with_edges = np.insert(idx_with_edges, 0, idx_with_edges[0])
                                _connect = idx_with_edges[:-1].view("u1")
                                y = sig.plot_samples.astype("f8")
                                x = sig.plot_timestamps

                            curve.opts["connect"] = _connect

                            x, y = self.scale_curve_to_pixmap(x, y, y_range=sig.y_range, x_start=x_start)

                            color = range_info["font_color"]
                            pen = fn.mkPen(color.name())
                            pen.setWidth(pen_width)

                            paint.setPen(pen)
                            paint.drawPath(self.generatePath(x, y))

                            curve.opts["connect"] = default_connect

                            if with_dots:
                                y = sig.plot_samples.astype("f8")
                                y[~idx] = np.inf

                                x = sig.plot_timestamps

                                x, y = self.scale_curve_to_pixmap(x, y, y_range=sig.y_range, x_start=x_start)

                                pen.setWidth(dots_with)
                                pen.setCapStyle(cap_style)
                                paint.setPen(pen)

                                pos = np.isfinite(y)
                                y = y[pos]
                                x = x[pos]

                                poly, arr = polygon_and_ndarray(x.size)

                                arr[:, 0] = x
                                arr[:, 1] = y
                                paint.drawPoints(poly)

            paint.end()

            _pixmap.setDevicePixelRatio(self.devicePixelRatio())
        else:
            _pixmap = self._pixmap

        paint = QtGui.QPainter()
        vp = self.viewport()
        paint.begin(vp)

        paint.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceOver)
        paint.setRenderHint(paint.RenderHint.Antialiasing, False)

        # Versteckte Achsen haben keine Flaeche mehr. Sie trotzdem zu
        # zeichnen kostet nur Zeit und wirft Warnungen, weil das Bild
        # dann leer ist - die Zeitachse zeigt ohnehin nur der unterste
        # Strip.
        for axis in (self.y_axis, self.x_axis):
            if not axis.isVisible():
                continue

            if axis.picture is None:
                axis.paint(paint, None, None)

            if axis.picture is None or axis.picture.isNull():
                continue

            paint.drawPixmap(
                axis.sceneBoundingRect(),
                axis.picture,
                axis.picture.rect(),
            )

        for ax in self.axes:
            if isinstance(ax, FormatedAxis) and ax.isVisible():
                if ax.picture is None:
                    ax.paint(paint, None, None)

                paint.drawPixmap(
                    ax.sceneBoundingRect(),
                    ax.picture,
                    ax.picture.rect(),
                )

        grid_pixmap = self.draw_grids(paint, viewbox_rect, ratio)

        self.auto_clip_rect(paint, viewbox_rect)

        if grid_pixmap:
            paint.drawPixmap(viewbox_rect, grid_pixmap, grid_pixmap.rect())
        paint.drawPixmap(viewbox_rect, _pixmap, _pixmap.rect())

        if self.zoom is None:

            if self.xy_mode:
                # Eine senkrechte Linie waere hier sinnlos - auf der
                # x-Achse steht ein Signalwert, keine Zeit.
                self.draw_xy_cursor(paint)
            elif self.region is not None:
                self.region.paint(paint, plot=self, uuid=self.current_uuid)
            elif self.cursor1 is not None and self.cursor1.isVisible():
                self.cursor1.paint(paint, plot=self, uuid=self.current_uuid)

            for bookmark in self._bookmarks:
                if bookmark.visible:
                    bookmark.paint(paint, plot=self, uuid=self.current_uuid)

            if pix := self._prev_pixmap:
                position = self.cursor1.value()

                px, py = self.px, self.py

                self.px = (vb_x_range[1] - vb_x_range[0]) / viewbox_rect.width()
                self.py = viewbox_rect.height()

                x, y = self.scale_curve_to_pixmap(
                    position,
                    0,
                    y_range=vb_y_range,
                    x_start=vb_x_range[0],
                )
                x = int(x) + 1 + viewbox_rect.x()

                clip_rect = QtCore.QRectF(paint.clipBoundingRect())
                clip_rect.setX(x)

                paint.setClipRect(clip_rect)

                self.px, self.py = px, py

                paint.drawPixmap(viewbox_rect, pix, pix.rect())

        else:
            p1, p2, zoom_mode = self.zoom

            old_px, old_py = self.px, self.py

            self.px = (vb_x_range[1] - vb_x_range[0]) / viewbox_rect.width()
            self.py = viewbox_rect.height()

            height = viewbox_rect.height()
            width = viewbox_rect.width()

            delta = viewbox_rect.x()

            x1, y1 = self.scale_curve_to_pixmap(
                p1.x(),
                p1.y(),
                y_range=vb_y_range,
                x_start=vb_x_range[0],
            )
            x2, y2 = self.scale_curve_to_pixmap(
                p2.x(),
                p2.y(),
                y_range=vb_y_range,
                x_start=vb_x_range[0],
            )

            rect = None

            if zoom_mode == self.viewbox.X_zoom or (zoom_mode in self.viewbox.XY_zoom and self.locked):
                x1, x2 = sorted([x1, x2])
                rect = QtCore.QRectF(
                    x1 + delta,
                    0,
                    x2 - x1,
                    height,
                )

            elif zoom_mode == self.viewbox.Y_zoom and not self.locked:
                y1, y2 = sorted([y1, y2])
                rect = QtCore.QRectF(
                    delta,
                    y1,
                    width + delta,
                    y2 - y1,
                )
            elif zoom_mode in self.viewbox.XY_zoom:
                x1, x2 = sorted([x1, x2])
                y1, y2 = sorted([y1, y2])

                rect = QtCore.QRectF(
                    x1 + delta,
                    y1,
                    x2 - x1,
                    y2 - y1,
                )

            if rect is not None:
                color = fn.mkColor(0x62, 0xB2, 0xE2)
                paint.setPen(fn.mkPen(color))
                color = fn.mkColor(0x62, 0xB2, 0xE2, 50)
                paint.setBrush(fn.mkBrush(color))

                paint.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceAtop)
                paint.drawRect(rect)

            self.px, self.py = old_px, old_py

        paint.end()

        if self._pixmap is None:
            self._pixmap = _pixmap

        if self.cursor1 and self.flash_current_signal > 0:
            self.flash_current_signal -= 1
            self.flash_curve_timer.start(50)

    def range_modified_finished_handler(self, region):
        if self.region_lock is not None:
            for i in range(2):
                if self.region.lines[i].value() == self.region_lock:
                    self.region.lines[i].pen.setStyle(QtCore.Qt.PenStyle.DashDotDotLine)
                else:
                    self.region.lines[i].pen.setStyle(QtCore.Qt.PenStyle.SolidLine)
        self.range_modified_finished.emit(region)
        if self.region.moving_cursor is not None:
            self.cursor1.setPos(self.region.moving_cursor.pos())

    def range_modified_handler(self, region):
        if self.region_lock is not None:
            for i in range(2):
                if self.region.lines[i].value() == self.region_lock:
                    self.region.lines[i].pen.setStyle(QtCore.Qt.PenStyle.DashDotDotLine)
                else:
                    self.region.lines[i].pen.setStyle(QtCore.Qt.PenStyle.SolidLine)
        if self.region.moving_cursor is not None:
            self.cursor1.setPos(self.region.moving_cursor.pos())

    def draw_enum_bands(self, paint, sig, x_start):
        """Ein Signal als farbige Abschnitte mit Text zeichnen.

        Jeder Wert bekommt seine eigene Farbe; wo der Wert konstant
        bleibt, entsteht ein Balken mit der Beschriftung darin. Hat der
        Kanal eine Wertetabelle, steht deren Text im Balken, sonst die
        Zahl selbst. Der Balken fuellt die ganze Zeile des Signals, so
        dass mehrere Signale als gleich hohe Zeilen untereinander
        stehen - wie im Digitalfenster.
        """
        entries = enum_entries(conversion_of(sig))

        if not self.py or sig.y_range is None:
            return False

        timestamps = sig.plot_timestamps
        samples = sig.plot_samples

        if not len(timestamps) or len(samples) != len(timestamps):
            return False

        # Ohne Wertetabelle nur dann Balken, wenn das Signal wirklich
        # stufig ist - sonst wuerde jede Stuetzstelle ein Balken.
        if not entries and len(samples) > 1:
            if 1 + int(np.count_nonzero(samples[1:] != samples[:-1])) > 64:
                return False

        try:
            y_low, y_high = (float(e) for e in sig.y_range)
        except (TypeError, ValueError):
            return False

        # Die Zeile, die der Digitalmodus dem Signal zugeteilt hat. Ohne
        # sie bliebe von einem Signal mit nur einem Wert ein Haarstrich.
        band = getattr(sig, "enum_band", None)

        try:
            if band:
                minimum, maximum = (float(e) for e in band)
            else:
                minimum, maximum = float(sig.min), float(sig.max)
        except (TypeError, ValueError):
            return False

        y_scale = (y_high - y_low) / float(self.py)
        if not y_scale:
            return False

        top = (y_high + y_scale - maximum) / y_scale
        bottom = (y_high + y_scale - minimum) / y_scale

        if bottom < top:
            top, bottom = bottom, top

        if bottom - top < 6:
            middle = (top + bottom) / 2
            top, bottom = middle - 3, middle + 3

        height = bottom - top
        px = self.px
        if not px:
            return False

        metrics = QtGui.QFontMetrics(paint.font())
        right_edge = self.viewbox.sceneBoundingRect().width() / px + timestamps[0]

        start = 0
        count = len(samples)

        for index in range(1, count + 1):
            if index < count and samples[index] == samples[start]:
                continue

            value = float(samples[start])
            stop_time = timestamps[index] if index < count else max(timestamps[-1], right_edge)

            x0 = (timestamps[start] - x_start) / px
            x1 = (stop_time - x_start) / px

            # Wert nicht in der Tabelle (oder gar keine Tabelle): die Zahl
            # selbst zeigen, Farbe fest am Wert
            text, color = entries.get(value) or (value_label(value), fn.mkColor(value_color(value)))

            rect = QtCore.QRectF(x0, top, max(x1 - x0, 1.0), height)
            paint.fillRect(rect, color)

            # ascent statt height: eine Zeile ist nur so hoch wie der
            # Eintrag in der Kanalliste, der Text passt trotzdem hinein
            if text and rect.width() > metrics.horizontalAdvance(text) + 8 and height >= metrics.ascent():
                # dunkle oder helle Schrift, je nach Helligkeit des Balkens
                paint.setPen(QtCore.Qt.GlobalColor.black if color.lightness() > 127 else QtCore.Qt.GlobalColor.white)
                paint.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter, text)

            start = index

        return True

    def xy_x_signal(self):
        """Das Signal, das im XY-Modus die x-Achse stellt.

        Ist es geloescht worden, rueckt das erste Signal der Liste nach -
        sonst bliebe der Strip ohne jeden Hinweis leer.
        """
        if not self.xy_mode:
            return None

        if self.x_uuid is not None:
            try:
                signal, _index = self.signal_by_uuid(self.x_uuid)
            except KeyError:
                signal = None

            if signal is not None:
                return signal

        self.x_uuid = self.signals[0].uuid if self.signals else None
        self._xy_cache = {}

        return self.signals[0] if self.signals else None

    def xy_pairs(self, sig):
        """Wertepaare (x, y) eines Signals im XY-Modus.

        Auf der x-Achse stehen die Werte des X-Signals. Beide Signale
        koennen verschiedene Zeitstempel haben, deshalb wird das
        X-Signal auf die Zeitstempel des gezeichneten Signals
        interpoliert. Das Ergebnis wird gemerkt, solange sich an den
        Laengen nichts aendert.
        """
        x_sig = self.xy_x_signal()
        if x_sig is None or sig.uuid == x_sig.uuid:
            return None

        zeit = sig.timestamps
        if zeit is None or not len(zeit) or not len(x_sig.timestamps):
            return None

        schluessel = (x_sig.uuid, len(x_sig.timestamps), len(zeit))
        gemerkt = self._xy_cache.get(sig.uuid)
        if gemerkt is not None and gemerkt[0] == schluessel:
            return gemerkt[1]

        try:
            x_werte = np.interp(zeit, x_sig.timestamps, x_sig.samples.astype("f8"))
            y_werte = sig.samples.astype("f8")
        except (TypeError, ValueError):
            return None

        paar = (x_werte, y_werte)
        self._xy_cache[sig.uuid] = (schluessel, paar)

        return paar

    def xy_value_range(self):
        """Wertebereich des X-Signals - das ist der Bereich der x-Achse."""
        x_sig = self.xy_x_signal()
        if x_sig is None:
            return None

        werte = getattr(x_sig, "samples", None)
        if werte is None or not len(werte):
            return None

        try:
            werte = werte.astype("f8")
            unten, oben = float(np.nanmin(werte)), float(np.nanmax(werte))
        except (TypeError, ValueError):
            return None

        if not (np.isfinite(unten) and np.isfinite(oben)):
            return None

        if unten == oben:
            unten, oben = unten - 0.5, oben + 0.5

        rand = (oben - unten) * 0.02

        return unten - rand, oben + rand

    def draw_xy_cursor(self, paint):
        """Punkt je Kurve an der Stelle des Zeitcursors.

        Der Cursor bleibt auch im XY-Modus eine Zeitangabe - er wandert
        nur nicht mehr als Linie durch das Bild, sondern sitzt als
        Markierung auf jeder Kurve.
        """
        cursor = self.cursor1
        if cursor is None or not cursor.isVisible():
            return

        rect = self.viewbox.sceneBoundingRect()
        breite, hoehe = rect.width(), rect.height()
        x0, x1 = self.x_range

        if breite <= 0 or hoehe <= 0 or x1 <= x0:
            return

        stempel = cursor.value()

        paint.setRenderHints(paint.RenderHint.Antialiasing, True)

        for sig in self.signals:
            if not sig.enable:
                continue

            paar = self.xy_pairs(sig)
            if paar is None:
                continue

            x_werte, y_werte = paar
            if not len(x_werte):
                continue

            index = int(np.searchsorted(sig.timestamps, stempel, side="right")) - 1
            index = min(max(index, 0), len(x_werte) - 1)

            try:
                y_low, y_high = (float(e) for e in sig.y_range)
            except (TypeError, ValueError):
                continue

            if y_high <= y_low:
                continue

            px = rect.x() + (float(x_werte[index]) - x0) / (x1 - x0) * breite
            py = rect.y() + (y_high - float(y_werte[index])) / (y_high - y_low) * hoehe

            if not (np.isfinite(px) and np.isfinite(py)):
                continue

            stift = fn.mkPen(sig.color.name())
            stift.setWidth(2)
            paint.setPen(stift)
            paint.setBrush(QtGui.QBrush())
            paint.drawEllipse(QtCore.QPointF(px, py), 5, 5)

        paint.setRenderHints(paint.RenderHint.Antialiasing, False)

    def digital_fill_path(self, path, sig, x):
        """Treppenkurve bis zur Grundlinie schliessen, damit sie fuellbar wird.

        Im Digitalfenster zaehlt die Flaeche, nicht die Linie - ein
        schmaler Puls ist als Strich kaum zu sehen.
        """
        if not self.py or sig.y_range is None or len(x) < 2:
            return None

        # Gefuellt wird bis zur Unterkante der eigenen Zeile, nicht bis
        # zum kleinsten Wert des Signals. Ein Signal, das die ganze
        # Messung ueber auf 1 steht, haette sonst Linie und Grundlinie
        # an derselben Stelle - und damit gar keine Flaeche.
        band = getattr(sig, "enum_band", None)

        try:
            y_low, y_high = (float(e) for e in sig.y_range)
            minimum = float(band[0]) if band else float(sig.min)
        except (TypeError, ValueError, IndexError):
            return None

        y_scale = (y_high - y_low) / float(self.py)
        if not y_scale:
            return None

        baseline = (y_high + y_scale - minimum) / y_scale

        fill = QtGui.QPainterPath(path)
        fill.lineTo(float(x[-1]), baseline)
        fill.lineTo(float(x[0]), baseline)
        fill.closeSubpath()

        return fill

    def scale_curve_to_pixmap(self, x, y, y_range, x_start):
        if self.py:
            y_low, y_high = y_range

            y_scale = (np.float64(y_high) - np.float64(y_low)) / np.float64(self.py)
            x_scale = self.px

            if y_scale * x_scale:
                all_bad = False
            else:
                all_bad = True
        else:
            all_bad = True

        if all_bad:
            try:
                y = np.full(len(y), np.inf)
            except:
                y = np.inf
        else:
            # xs = x_start
            # ys = y_high
            # x = (x - xs) / x_scale + delta
            # y = (ys - y) / y_scale + 1
            # is rewriten as

            ys = y_high + y_scale

            x = (x - x_start) / x_scale
            y = (ys - y) / y_scale

        return x, y

    def select_curve(self, x, y):
        ratio = self.devicePixelRatio()
        rect = self.viewbox.sceneBoundingRect()
        rect.setSize(rect.size() * ratio)
        rect.moveTo(rect.topLeft() * ratio)

        delta = rect.x()
        x_start = self.x_range[0]

        y = y * ratio

        candidates = []
        for sig in self.signals:
            if not sig.enable:
                continue

            raw_value, raw_kind, val, _1, _2 = sig.value_at_timestamp(x, numeric=True)

            if val == "n.a.":
                continue

            x_val, y_val = self.scale_curve_to_pixmap(x, val, y_range=sig.y_range, x_start=x_start)

            candidates.append((abs(y_val - y), sig.uuid))

        if candidates:
            candidates.sort()
            self.curve_clicked.emit(candidates[0][1])

    def selected_items(self):
        uuids = self.plot_parent.selected_items()
        if self.current_uuid:
            uuids.add(self.current_uuid)

        return uuids, self.current_uuid

    def set_color(self, uuid, color):
        sig, index = self.signal_by_uuid(uuid)

        if sig.mode == "raw":
            style = QtCore.Qt.PenStyle.DashLine
        else:
            style = QtCore.Qt.PenStyle.SolidLine

        sig.pen = fn.mkPen(color=color, style=style)

        if sig.individual_axis:
            self.get_axis(index).set_pen(sig.pen)
            self.get_axis(index).setTextPen(sig.pen)

        if uuid == self.current_uuid:
            self.y_axis.set_pen(sig.pen)
            self.y_axis.setTextPen(sig.pen)

        if not self._update_timer.isActive():
            self._update_timer.start()

    def set_common_axis(self, uuid, state):
        signal, idx = self.signal_by_uuid(uuid)

        if state in (QtCore.Qt.CheckState.Checked, True, 1):
            if not self.common_axis_items:
                self.common_axis_y_range = signal.y_range
            else:
                signal.y_range = self.common_axis_y_range
            self.common_axis_items.add(uuid)
        else:
            self.common_axis_items.remove(uuid)

        self.common_axis_label = ", ".join(self.signal_by_uuid(uuid)[0].name for uuid in self.common_axis_items)

        self.set_current_uuid(self.current_uuid, True)

        if not self._update_timer.isActive():
            self._update_timer.start()

    def set_conversion(self, uuid, conversion):
        sig, index = self.signal_by_uuid(uuid)

        axis = self.axes[index]
        if isinstance(axis, FormatedAxis):
            if sig.conversion and hasattr(sig.conversion, "text_0"):
                axis.text_conversion = sig.conversion
            else:
                axis.text_conversion = None

            axis.picture = None

        if uuid == self.current_uuid:
            axis = self.y_axis
            if sig.conversion and hasattr(sig.conversion, "text_0"):
                axis.text_conversion = sig.conversion
            else:
                axis.text_conversion = None

            axis.picture = None

        if sig.trim_info is not None:
            sig.trim(*sig.trim_info, force=True)

        self.update()

    def axis_label(self, sig, axis=None):
        """Beschriftung der Y-Achse: Name und Einheit, ungekuerzt.

        Das Kuerzen auf die Laenge der Achse macht die Achse selbst -
        nur sie kennt ihre jeweils aktuelle Hoehe.
        """
        return f"{sig.name} [{sig.unit}]" if sig.unit else sig.name

    def set_current_uuid(self, uuid, force=False):
        axis = self.y_axis
        viewbox = self.viewbox

        sig, index = self.signal_by_uuid(uuid)

        if sig.conversion and hasattr(sig.conversion, "text_0"):
            axis.text_conversion = sig.conversion
        else:
            axis.text_conversion = None
        axis.format = sig.format

        if uuid in self.common_axis_items:
            if self.current_uuid not in self.common_axis_items or force:
                match self._settings.value("plot/background"):
                    case "Black":
                        axis.set_pen(fn.mkPen("#FFFFFF"))
                        axis.setTextPen("#FFFFFF")
                    case "White":
                        axis.set_pen(fn.mkPen("#000000"))
                        axis.setTextPen("#000000")
                    case _:
                        plot_foreground = self._settings.value("plot/foreground", "#ffffff")
                        axis.set_pen(fn.mkPen(plot_foreground))
                        axis.setTextPen(plot_foreground)

            # Nur das aktive Signal beschriften. Alle Namen der Achse
            # aneinandergereiht sind ab dem dritten nicht mehr lesbar,
            # und welches gerade gemeint ist, sieht man dann erst recht
            # nicht. Dass die Achse geteilt ist, zeigt ihre helle Farbe.
            axis.setLabel(self.axis_label(sig, axis))

        else:
            axis.setLabel(self.axis_label(sig, axis))

            axis.set_pen(sig.pen)
            axis.setTextPen(sig.pen)
            axis.update()

        self.current_uuid = uuid

        viewbox.setYRange(*[float(y) for y in sig.y_range], padding=0)

        self._update_y_mouse()

        self.current_uuid_changed.emit(uuid)
        self.update()

    def set_dots(self, with_dots):
        self.with_dots = with_dots
        self.update()

    def set_y_locked(self, uuid, state):
        """Y-Bereich eines Signals festhalten.

        Liegt das Signal auf der gemeinsamen Achse, gilt die Sperre fuer
        alle Signale dieser Achse - sonst wuerden sie auseinanderlaufen.
        Liefert die uuids, die tatsaechlich umgeschaltet wurden.
        """
        state = bool(state)

        if uuid in self.common_axis_items:
            uuids = list(self.common_axis_items)
        else:
            uuids = [uuid]

        for item_uuid in uuids:
            try:
                sig, index = self.signal_by_uuid(item_uuid)
            except KeyError:
                continue

            sig.y_locked = state

            axis = self.get_axis(index)
            if isinstance(axis, FormatedAxis):
                axis.locked = state or self.locked

        self._update_y_mouse()

        return uuids

    def _update_y_mouse(self):
        """Y mit der Maus nur zulassen, wenn das aktuelle Signal frei ist."""
        locked = self.locked

        if not locked and self.current_uuid is not None:
            try:
                sig, _index = self.signal_by_uuid(self.current_uuid)
            except KeyError:
                sig = None
            if sig is not None:
                locked = sig.y_locked

        self.y_axis.locked = locked
        self.viewbox.setMouseEnabled(y=not locked)

    def set_individual_axis(self, uuid, state):
        sig, index = self.signal_by_uuid(uuid)

        if state in (QtCore.Qt.CheckState.Checked, True, 1):
            if sig.enable:
                self.get_axis(index).show()
            sig.individual_axis = True
        else:
            self.get_axis(index).hide()
            sig.individual_axis = False

        if not self._update_timer.isActive():
            self._update_timer.start()

    def set_line_interconnect(self, line_interconnect):
        self.line_interconnect = line_interconnect

        self._curve.setData(stepMode=line_interconnect)
        for sig in self.signals:
            sig.path = None
        self.update()

    def set_locked(self, locked):
        self.locked = locked
        for index, axis in enumerate(self.axes):
            if isinstance(axis, FormatedAxis):
                # Eine Einzelsperre bleibt bestehen, auch wenn die
                # Gesamtsperre aufgehoben wird.
                own = index < len(self.signals) and self.signals[index].y_locked
                axis.locked = locked or own

        self._update_y_mouse()

    def set_name(self, uuid, name):
        sig, index = self.signal_by_uuid(uuid)
        sig.name = name or ""

        sig_axis = [self.get_axis(index)]

        if uuid == self.current_uuid:
            sig_axis.append(self.y_axis)

        for axis in sig_axis:
            axis.setLabel(self.axis_label(sig, axis))
            axis.update()
        self.update()

    def set_signal_enable(self, uuid, state):
        signal, index = self.signal_by_uuid(uuid)

        if state in (QtCore.Qt.CheckState.Checked, True, 1):
            (start, stop), _ = self.viewbox.viewRange()
            width = self.width() - self.y_axis.width()

            signal.enable = True

            signal.trim(start, stop, width)
            if signal.individual_axis:
                self.get_axis(index).show()

            uuids = self._timebase_db.setdefault(id(signal.timestamps), set())
            uuids.add(signal.uuid)

        else:
            signal.enable = False
            if signal.individual_axis:
                self.get_axis(index).hide()

            try:
                self._timebase_db[id(signal.timestamps)].remove(uuid)

                if len(self._timebase_db[id(signal.timestamps)]) == 0:
                    del self._timebase_db[id(signal.timestamps)]
            except:
                pass

        for sig in self.signals:
            sig.path = None

        self._enable_timer.start(50)

    def set_time_offset(self, info):
        absolute, offset, *uuids = info

        signals = [sig for sig in self.signals if sig.uuid in uuids]

        if absolute:
            for sig in signals:
                if not len(sig.timestamps):
                    continue
                id_ = id(sig.timestamps)
                delta = sig.timestamps[0] - offset
                sig.timestamps = sig.timestamps - delta

                uuids = self._timebase_db.setdefault(id(sig.timestamps), set())
                uuids.add(sig.uuid)

                self._timebase_db[id_].remove(sig.uuid)
                if len(self._timebase_db[id_]) == 0:
                    del self._timebase_db[id_]

                if sig.trim_info is not None:
                    sig.trim(*sig.trim_info, force=True)
        else:
            for sig in signals:
                if not len(sig.timestamps):
                    continue
                id_ = id(sig.timestamps)

                sig.timestamps = sig.timestamps + offset

                uuids = self._timebase_db.setdefault(id(sig.timestamps), set())
                uuids.add(sig.uuid)

                self._timebase_db[id_].remove(sig.uuid)
                if len(self._timebase_db[id_]) == 0:
                    del self._timebase_db[id_]

                if sig.trim_info is not None:
                    sig.trim(*sig.trim_info, force=True)

        self._compute_all_timebase()

        self.update()

    def set_unit(self, uuid, unit):
        sig, index = self.signal_by_uuid(uuid)
        sig.unit = unit or ""

        sig_axis = [self.get_axis(index)]

        if uuid == self.current_uuid:
            sig_axis.append(self.y_axis)

        for axis in sig_axis:
            axis.setLabel(self.axis_label(sig, axis))
            axis.update()

        self.update()

    def set_y_range(self, uuid, y_range, emit=True):
        # Das X-Signal eines XY-Strips wird nicht als Kurve gezeichnet -
        # sein Wertebereich gehoert der x-Achse. Ihn ueber die Werteachse
        # zu verschieben wuerde nur die Beschriftung verstellen.
        ziel = self.current_uuid if uuid is None else uuid
        if self.xy_mode and ziel is not None and ziel == self.x_uuid:
            return

        update = False

        if uuid is None:
            # y axis was changed
            if self.current_uuid is None:
                return
            uuid = self.current_uuid
            sig, idx = self.signal_by_uuid(uuid)
            if sig.y_range != y_range:
                sig.y_range = y_range
                axis = self.axes[idx]
                if isinstance(axis, FormatedAxis):
                    axis.setRange(*y_range)
                update = True
        else:
            sig, idx = self.signal_by_uuid(uuid)
            if sig.y_range != y_range:
                sig.y_range = y_range
                if uuid == self.current_uuid:
                    self.y_axis.setRange(*y_range)
                update = True

        if uuid in self.common_axis_items:
            for uuid in self.common_axis_items:
                sig, idx = self.signal_by_uuid(uuid)
                if sig.y_range != y_range:
                    sig.y_range = y_range
                    axis = self.axes[idx]
                    if isinstance(axis, FormatedAxis):
                        axis.setRange(*y_range)
                    update = True
        if update:
            self._sync_viewbox_to_current()

        if emit:
            self.zoom_changed.emit(False)
        if update:
            if not self._update_timer.isActive():
                self._update_timer.start()

    def _sync_viewbox_to_current(self):
        """Zeichenflaeche auf den Bereich des aktiven Signals nachziehen.

        Das Scrollrad ueber der Y-Achse aendert nur den Bereich des
        Signals - gezeichnet wird ja ohnehin je Signal. Die
        Zeichenflaeche selbst bleibt dabei auf ihrem alten Bereich
        stehen, und das naechste Verschieben mit der Maus rechnet von
        dort weiter. Dadurch war der Zoom beim ersten Schieben wieder
        verloren.
        """
        if self.current_uuid is None:
            return

        try:
            signal, _index = self.signal_by_uuid(self.current_uuid)
        except KeyError:
            return

        if signal.y_range is None:
            return

        try:
            wanted = tuple(float(value) for value in signal.y_range)
        except (TypeError, ValueError):
            return

        if tuple(self.viewbox.viewRange()[1]) == wanted:
            return

        # stumm schalten, sonst schreibt der Bereichswechsel sofort
        # wieder auf die Signale zurueck
        self.viewbox.blockSignals(True)
        try:
            self.viewbox.setYRange(*wanted, padding=0)
        finally:
            self.viewbox.blockSignals(False)

    def signal_by_name(self, name):
        for i, sig in enumerate(self.signals):
            if sig.name == name:
                return sig, i

        raise Exception(f"Signal not found: {name} {[sig.name for sig in self.signals]}")

    def signal_by_uuid(self, uuid):
        return self._uuid_map[uuid]

    def _signals_enabled_changed_handler(self):
        self._compute_all_timebase()
        if self.cursor1:
            self.cursor_move_finished.emit(self.cursor1)
        self.signals_enable_changed.emit()
        self.update()

    def trim(self, signals=None, force=False, view_range=None):
        signals = signals or self.signals
        if not self._can_trim:
            return

        # Im XY-Modus ist der sichtbare Bereich ein Wertebereich und
        # keine Zeitspanne - danach zu beschneiden wuerde die Kurve
        # zerstoeren. Dort wird ohnehin die ganze Messung gezeichnet.
        #
        # Die gemerkten Kurvenpfade muessen aber weg: generatePath gibt
        # sonst beim naechsten Zeichnen den alten Pfad zurueck, und die
        # Kurve bleibt beim Schieben und Zoomen stehen, waehrend sich
        # Achse und Cursorpunkte bewegen.
        if self.xy_mode:
            for sig in signals:
                sig.path = None
            return

        if view_range is None:
            (start, stop), _ = self.viewbox.viewRange()
        else:
            start, stop = view_range

        width = self.viewbox.sceneBoundingRect().toRect().width()

        for sig in signals:
            if sig.enable:
                sig.trim(start, stop, width, force)

    def update(self, *args, pixmap=None, **kwargs):
        self._pixmap = pixmap

        for idx, sig in enumerate(self.signals):
            if sig.individual_axis:
                axis = self.get_axis(idx)
                if tuple(axis.range) != tuple(sig.y_range):
                    axis.setRange(*sig.y_range)

            if sig.uuid == self.current_uuid:
                if tuple(self.y_axis.range) != tuple(sig.y_range):
                    self.y_axis.setRange(*sig.y_range)

        if self.viewbox and self._can_paint_global:
            self.viewbox.update()

    def update_views(self):
        geometry = self.viewbox.sceneBoundingRect().toRect()
        if geometry != self.viewbox_geometry:
            self.viewbox_geometry = geometry

    def value_at_cursor(self, uuid=None):
        uuid = uuid or self.current_uuid

        if not uuid:
            y, sig_y_bottom, sig_y_top = "n.a.", 0, 1
        elif self.cursor1:
            if not self.cursor1.isVisible():
                cursor = self.region.lines[0]
            else:
                cursor = self.cursor1

            timestamp = cursor.value()
            sig, idx = self.signal_by_uuid(uuid)
            sig_y_bottom, sig_y_top = sig.y_range
            raw_value, raw_kind, y, *_ = sig.value_at_timestamp(timestamp, numeric=True, strict_timebase=False)

        else:
            sig, idx = self.signal_by_uuid(uuid)
            sig_y_bottom, sig_y_top = sig.y_range

            if not len(sig):
                y = "n.a."
            else:
                y = sig.phys_samples[-1]

                if y.dtype.kind not in "uif":
                    y = sig.raw_samples[-1]

        return y, sig_y_bottom, sig_y_top

    def xrange_changed_handle(self, *args, force=False):
        if self._can_paint:
            self.trim(force=force)
            self.update()

        self.zoom_changed.emit(False)

    def y_changed(self, *args):
        if len(args) == 1:
            # range manually changed by the user with the wheel or drag
            mask = args[0]
            if mask[1]:
                y_range = self.viewbox.viewRange()[1]
            else:
                return
        else:
            # range changed by the linked axis
            y_range = args[1]

        update = False

        # Das X-Signal hat im XY-Strip keine Kurve - sein Wertebereich
        # zu verschieben bewegt nichts und verstellt nur die Achse.
        if self.xy_mode and self.current_uuid and self.current_uuid == self.x_uuid:
            return

        if self.current_uuid in self.common_axis_items:
            for uuid in self.common_axis_items:
                sig, idx = self.signal_by_uuid(uuid)
                if sig.y_range != y_range:
                    update = True
                    if sig.individual_axis:
                        axis = self.get_axis(idx)
                        axis.setRange(*y_range)

                sig.y_range = y_range

        elif self.current_uuid:
            sig, idx = self.signal_by_uuid(self.current_uuid)
            if sig.y_range != y_range:
                update = True
                if sig.individual_axis:
                    axis = self.get_axis(idx)
                    axis.setRange(*y_range)

            sig.y_range = y_range

        if update:
            self.update()


class CursorInfo(QtWidgets.QLabel):
    def __init__(self, precision, name="t", unit="s", plot=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.precision = precision
        self.name = name
        self.unit = unit
        self.plot = plot

        self.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight
            | QtCore.Qt.AlignmentFlag.AlignTrailing
            | QtCore.Qt.AlignmentFlag.AlignVCenter
        )

        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.open_menu)

        if precision == -1:
            self.setToolTip("Cursor information uses maximum precision")
        else:
            self.setToolTip(f"Cursor information precision is set to {self.precision} decimals")

    def open_menu(self, point):
        menu = QtWidgets.QMenu(self)
        menu.addAction("Set precision")
        action = menu.exec_(self.mapToGlobal(point))

        if action is None:
            return

        if action.text() == "Set precision":
            precision, ok = QtWidgets.QInputDialog.getInt(
                self, "Set new precision (float decimals)", "Precision:", self.precision, -1, 15, 1
            )

            if ok:
                self.set_precision(precision)
                QtCore.QSettings().setValue("plot/cursor/display_precision", precision)

    def update_value(self):
        cursor_info_text = ""
        if not self.plot.region:
            if self.plot.cursor1 is not None:
                position = self.plot.cursor1.value()

                fmt = self.plot.x_axis.format
                if fmt == "phys" or not fmt:
                    if self.precision == -1:
                        cursor_info_text = f"{self.name} = {position}{self.unit}"
                    else:
                        template = f"{self.name} = {{:.{self.precision}f}}{self.unit}"
                        cursor_info_text = template.format(position)
                elif fmt == "time":
                    cursor_info_text = f"{self.name} = {timedelta(seconds=position)}"
                elif fmt == "date":
                    position_date = self.plot.x_axis.origin + timedelta(seconds=position)
                    cursor_info_text = f"{self.name} = {position_date}"

                if cursor_info_text:
                    self.setText(cursor_info_text)
            else:
                self.setText("")

        else:
            start, stop = self.plot.region.getRegion()

            fmt = self.plot.x_axis.format
            if fmt == "phys":
                if self.precision == -1:
                    start_info = f"{start}{self.unit}"
                    stop_info = f"{stop}{self.unit}"
                    delta_info = f"{stop - start}{self.unit}"
                else:
                    template = f"{{:.{self.precision}f}}{self.unit}"
                    start_info = template.format(start)
                    stop_info = template.format(stop)
                    delta_info = template.format(stop - start)

            elif fmt == "time":
                start_info = f"{timedelta(seconds=start)}"
                stop_info = f"{timedelta(seconds=stop)}"
                delta_info = f"{timedelta(seconds=(stop - start))}"
            elif fmt == "date":
                start_info = self.plot.x_axis.origin + timedelta(seconds=start)
                stop_info = self.plot.x_axis.origin + timedelta(seconds=stop)

                delta_info = f"{timedelta(seconds=(stop - start))}"

            self.setText(
                "<html><head/><body>"
                f"<p>{self.name}1 = {start_info}, {self.name}2 = {stop_info}</p>"
                f"<p>Δ{self.name} = {delta_info}</p> "
                "</body></html>"
            )

    def set_precision(self, precision):
        self.precision = precision
        if precision == -1:
            self.setToolTip("Cursor information uses maximum precision")
        else:
            self.setToolTip(f"Cursor information precision is set to {precision} decimals")
        self.update_value()


try:
    import scipy  # noqa: F401

    from .fft_window import FFTWindow
except ImportError:
    pass
