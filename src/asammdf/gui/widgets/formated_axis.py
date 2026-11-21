from datetime import datetime, timedelta, timezone
from math import ceil
import os
from traceback import format_exc

import numpy as np
import pandas as pd
import pyqtgraph as pg
import pyqtgraph.functions as fn
from pyqtgraph.graphicsItems.ButtonItem import ButtonItem
from pyqtgraph.Point import Point
from PySide6 import QtCore, QtGui, QtWidgets

LOCAL_TIMEZONE = datetime.now(timezone.utc).astimezone().tzinfo

# Ablaufverfolgung fuer die Achsenbeschriftung. Einschalten mit der
# Umgebungsvariablen MDFVIEW_DEBUG_AXIS=1, dann schreibt jede Achse in
# die Konsole, was sie tut.
AXIS_DEBUG = bool(os.environ.get("MDFVIEW_DEBUG_AXIS"))

# Abstand, den die Beschriftung an den Enden der Achse frei laesst.
# Zum Ausprobieren ueber MDFVIEW_AXIS_MARGIN verstellbar.
AXIS_LABEL_MARGIN = int(os.environ.get("MDFVIEW_AXIS_MARGIN", "8"))


def axis_log(*parts):
    if AXIS_DEBUG:
        print("[achse]", *parts, flush=True)


if AXIS_DEBUG:
    print("[achse] Modul geladen aus", __file__, flush=True)

# Breite des Greifbereichs am inneren Rand der Skala
WIDTH_HANDLE = 6

BUTTON_SIZE = 16


class FormatedAxis(pg.AxisItem):
    rangeChanged = QtCore.Signal(object, object)
    scale_editor_requested = QtCore.Signal(object)
    # Breite der Skala wurde mit der Maus gezogen
    width_changed = QtCore.Signal(object)

    def __init__(self, *args, **kwargs):
        self.plus = self.minus = None
        self.uuid = kwargs.pop("uuid", None)
        self.background = kwargs.pop("background", fn.mkColor("#000000"))
        self.linked_signal = kwargs.pop("linked_signal", None)

        super().__init__(*args, **kwargs)

        self.setAcceptHoverEvents(True)

        self._settings = QtCore.QSettings()

        self.format = "phys"
        self.mode = "phys"
        self.text_conversion = None
        self._enumeration_cache = (None, [])
        self._on_width_handle = False
        self._width_at_press = None
        self._width_drag = False
        self.origin = None

        self.hovering = False

        self._label_with_unit = ""

        self.locked = kwargs.pop("locked", False)

        # Ungekuerzte Beschriftung. pyqtgraph ruft setLabel bereits im
        # Konstruktor auf, wenn text= uebergeben wurde - das darf hier
        # nicht wieder weggeworfen werden.
        self.full_label = getattr(self, "full_label", "") or self.labelText or ""
        self._fitting = False

        # Beim Groessenereignis ist die Zeichenflaeche noch nicht neu
        # vermessen - erst einen Durchlauf spaeter stimmen ihre Werte.
        self._fit_timer = QtCore.QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self.refit_label)

        if self.orientation in ("left", "right"):
            # Die Knoepfe + und - entfallen: die Breite zieht man am
            # inneren Rand der Skala. Die Felder bleiben auf None, damit
            # der restliche Code sie weiter abfragen kann.
            self.setWidth(48)

        # pyqtgraph haengt der Beschriftung sonst einen Hinweis auf den
        # SI-Vorsatz an, etwa "(x1000)". Die Achse zeigt hier die
        # Rohwerte, der Zusatz waere also schlicht falsch - und er
        # entsteht erst beim Zeichnen, laeuft damit ueber die vorher
        # berechnete Laenge hinaus und wird abgeschnitten.
        self.enableAutoSIPrefix(False)

        self.set_pen(self._pen)

    def increase_width(self):
        self.setWidth(self.tick_width() + 10)

    def decrease_width(self):
        self.setWidth(self.tick_width() - 10)

    def setWidth(self, w=None):
        if w is not None and self.orientation in ("left", "right"):
            # Untergrenze gilt nur noch fuer die Zahlen. Frueher standen
            # hier 48 Punkte, weil die Beschriftung in dieser Breite mit
            # drin lag - sie bekommt jetzt ihren eigenen Platz daneben.
            super().setWidth(max(w, 10))
        else:
            super().setWidth(w)

    def tick_width(self):
        """Eingestellte Breite der Skala, ohne die Achsenbeschriftung."""
        return self.fixedWidth if self.fixedWidth is not None else self.width()

    def stored_width(self):
        """Breite fuer die Konfiguration - ohne die Achsenbeschriftung.

        width() liefert die ganze sichtbare Breite, also Skala plus
        Beschriftung. Wird dieser Wert gespeichert und beim naechsten
        Laden wieder als Skalenbreite gesetzt, kommt die Beschriftung
        erneut dazu: die Achse wuchs mit jedem Laden um ihre Dicke.
        """
        if self.fixedWidth is not None:
            return float(self.fixedWidth)

        breite = float(self.width())
        if self.label is not None and self.label.isVisible():
            breite -= self.label.boundingRect().height() * 0.8

        return max(breite, 10.0)

    def _updateWidth(self):
        """Auch bei fester Breite Platz fuer die Achsenbeschriftung lassen.

        pyqtgraph rechnet die Dicke der Beschriftung nur dann dazu, wenn
        es die Breite selbst bestimmt. Sobald sie festliegt - also sobald
        man sie einmal mit der Maus gezogen hat - stand die Beschriftung
        sonst mitten in den Zahlen.
        """
        super()._updateWidth()

        if self.fixedWidth is None or not self.isVisible() or not self.label.isVisible():
            return

        # Volle Hoehe statt pyqtgraphs 0.8: der Faktor ist dort als
        # Ueberschlag gedacht und schneidet hohe Zeichen wie [ und ] an.
        width = self.fixedWidth + int(self.label.boundingRect().height()) + 2

        self.setMaximumWidth(width)
        self.setMinimumWidth(width)
        self.picture = None

    def enumeration_values(self):
        """Die in der Umrechnungstabelle hinterlegten Rohwerte.

        Bei einer Wertetabelle mit Texten soll die Achse nur diese Werte
        beschriften - die Zahlen dazwischen bedeuten nichts.
        """
        conversion = self.text_conversion

        cached_for, cached = self._enumeration_cache
        if cached_for is conversion:
            return cached

        values = []

        if conversion is not None:
            count = int(getattr(conversion, "val_param_nr", 0) or 0)
            blocks = getattr(conversion, "referenced_blocks", None) or {}
            texts = sum(1 for key in blocks if key.startswith("text_"))

            # Bereichstabelle: zwei Grenzen je Text, davon die untere
            step = 2 if texts and count == 2 * texts else 1

            for index in range(0, count, step):
                value = getattr(conversion, f"val_{index}", None)
                if value is None:
                    continue
                try:
                    values.append(float(value))
                except (TypeError, ValueError):
                    continue

            values.sort()

        self._enumeration_cache = (conversion, values)

        return values

    def enumeration_tick_values(self, minVal, maxVal):
        """Sichtbare Tabellenwerte, oder None fuer die normale Einteilung."""
        low, high = (minVal, maxVal) if minVal <= maxVal else (maxVal, minVal)

        values = [value for value in self.enumeration_values() if low <= value <= high]

        # Nichts im Bild oder zu viele Eintraege: lieber die uebliche
        # Einteilung, sonst klebt die Beschriftung uebereinander.
        if not values or len(values) > 40:
            return None

        return values

    def tickValues(self, minVal, maxVal, size):
        if self.text_conversion is not None and self.mode == "phys":
            values = self.enumeration_tick_values(minVal, maxVal)
            if values is not None:
                return [(None, values)]

        return super().tickValues(minVal, maxVal, size)

    def tickStrings(self, values, scale, spacing):
        strns = []
        self.tick_positions = values

        if self.text_conversion and self.mode == "phys":
            strns = []
            for val in values:
                nv = self.text_conversion.convert(np.array([val]), as_bytes=True)[0]

                val = float(val)

                if val.is_integer():
                    val = int(val)

                    if self.format == "hex":
                        val = hex(int(val))
                    elif self.format == "bin":
                        val = bin(int(val))
                    else:
                        val = str(val)
                else:
                    val = f"{val:.6f}"

                if isinstance(nv, bytes):
                    try:
                        text = nv.decode("utf-8", errors="replace")
                    except:
                        text = nv.decode("latin-1", errors="replace")

                    # nur der Text - die Rohzahl davor stiftet mehr
                    # Verwirrung als Nutzen
                    strns.append(text or val)
                else:
                    strns.append(val)
        else:
            if self.format == "phys":
                strns = super().tickStrings(values, scale, spacing)

            elif self.format == "hex":
                for val in values:
                    val = float(val)
                    if val.is_integer():
                        val = hex(int(val))
                    else:
                        val = ""
                    strns.append(val)

            elif self.format == "bin":
                for val in values:
                    val = float(val)
                    if val.is_integer():
                        val = bin(int(val))
                    else:
                        val = ""
                    strns.append(val)
            elif self.format == "ascii":
                for val in values:
                    val = float(val)
                    if val.is_integer():
                        val = int(val)
                        if 0 < val < 0x110000:
                            val = chr(val)
                        else:
                            val = str(val)
                    else:
                        val = ""
                    strns.append(val)
            elif self.format == "time":
                strns = [str(timedelta(seconds=val)) for val in values]
            elif self.format == "date":
                strns = (
                    (
                        pd.to_timedelta(
                            np.array(values),
                            unit="s",
                            errors="coerce",
                        )
                        + self.origin
                    )
                    .astype(str)
                    .to_list()
                )

        return [val[:80] for val in strns]

    def label_width(self, text):
        """Laenge der Beschriftung in Bildpunkten, so wie sie gezeichnet wird.

        Nicht die reine Schriftbreite - gezeichnet wird als HTML, und das
        kostet Rand.
        """
        style = ";".join(f"{key}: {value}" for key, value in (self.labelStyle or {}).items())

        document = QtGui.QTextDocument()
        document.setDefaultFont(self.label.font())
        document.setHtml(f"<span style='{style}'>{text}</span>")

        return document.idealWidth()

    def label_candidates(self, text, room):
        """Fassungen der Beschriftung, von lang nach kurz."""
        yield text

        base = text

        # Einheit in eckigen Klammern am Ende opfern, der Name zaehlt mehr
        if text.endswith("]") and " [" in text:
            base = text[: text.rindex(" [")]
            yield base

        metrics = QtGui.QFontMetrics(self.label.font())
        width = max(int(room - self.label_width("")), 24)

        while width >= 24:
            yield metrics.elidedText(base, QtCore.Qt.TextElideMode.ElideRight, width)
            width -= 8

    def label_span(self):
        """Der Bereich laengs der Achse, in dem die Beschriftung stehen darf.

        Das ist die Zeichenflaeche, nicht die Achse: die Achse reicht
        darueber hinaus, unten bis in die Zeitskala. Mittig zur Achse
        heisst deshalb zu tief, und oben bleibt Platz liegen.
        """
        # Waehrend pyqtgraph die Achse noch baut, gibt es _linkedView
        # noch nicht - linkedView() liefe in einen AttributeError.
        view = self.linkedView() if hasattr(self, "_linkedView") else None

        if view is not None:
            try:
                rect = self.mapRectFromItem(view, view.boundingRect())
            except Exception:
                rect = None

            if rect is not None and rect.height() > 20:
                return float(rect.top()), float(rect.bottom())

        return 0.0, float(self.height())

    def place_label(self, oben, unten):
        """Beschriftung mittig in den nutzbaren Bereich setzen."""
        if self.label is None:
            return

        breite = self.label.boundingRect().width()

        pos = self.label.pos()
        pos.setY((oben + unten) / 2 + breite / 2)
        self.label.setPos(pos)

    def refit_label(self):
        """Beschriftung kuerzen, bis sie wirklich auf die Achse passt.

        Die Beschriftung laeuft laengs der Achse. Was nicht hineinpasst,
        schneidet Qt an BEIDEN Enden ab, weil der Text mittig sitzt -
        vom Namen fehlt dann vorne wie hinten etwas.

        Gemessen wird am gesetzten Text, nicht an einer Vorhersage:
        gezeichnet wird HTML, und was pyqtgraph dabei noch anhaengt,
        laesst sich von aussen nicht zuverlaessig abschaetzen.
        """
        # Beim Schliessen des Fensters ist die Beschriftung schon weg,
        # die Zeitgeber laufen aber noch einmal ab.
        if self.label is None:
            return

        # pyqtgraph ruft setLabel schon in seinem Konstruktor - also
        # bevor _linkedView ueberhaupt angelegt ist. Dann gibt es
        # noch keine Zeichenflaeche, an der sich die Beschriftung
        # ausrichten koennte; das passiert beim ersten
        # Groessenereignis.
        if not hasattr(self, "_linkedView"):
            return

        marke = f"refit    id={id(self) % 10000} lage={self.orientation}"

        if getattr(self, "_fitting", False):
            axis_log(marke, "-> laeuft schon, uebersprungen")
            return

        # Falls die Beschriftung an setLabel vorbei gesetzt wurde, gilt
        # der erste gesehene Text als der ungekuerzte.
        if not getattr(self, "full_label", ""):
            self.full_label = self.labelText or ""

        text = self.full_label

        if not text or self.orientation not in ("left", "right"):
            axis_log(marke, f"-> nichts zu tun (text={text!r})")
            return

        # Nutzbar ist die Zeichenflaeche, nicht die Achse selbst - die
        # reicht unten bis in die Zeitskala hinein.
        oben, unten = self.label_span()
        room = int(unten - oben) - AXIS_LABEL_MARGIN

        # noch ohne brauchbare Groesse: der naechste Groessenwechsel kuerzt
        if room < 30:
            axis_log(marke, f"-> zu klein, hoehe={self.height():.0f} platz={room}")
            return

        self._fitting = True
        try:
            for candidate in self.label_candidates(text, room):
                if candidate != self.labelText:
                    self.labelText = candidate
                    self.label.setHtml(self.labelString())
                    self._adjustSize()

                breite = self.label.boundingRect().width()
                axis_log(marke, f"platz={room} probiert={candidate!r} breite={breite:.0f}")

                if breite <= room:
                    break

            # pyqtgraph setzt die Beschriftung nur beim Aendern der
            # Achsengroesse wieder mittig, nicht beim Aendern des Textes
            super().resizeEvent(None)
            self.place_label(oben, unten)

            self.picture = None
            self.update()

            if AXIS_DEBUG:
                breite = self.label.boundingRect().width()
                fuss = self.label.pos().y()
                axis_log(
                    marke,
                    f"fertig text={self.labelText!r} achse={self.height():.0f}",
                    f"flaeche={oben:.0f}..{unten:.0f} text={fuss - breite:.0f}..{fuss:.0f}",
                )
        finally:
            self._fitting = False

    def setLabel(self, text=None, units=None, unitPrefix=None, **args):
        """Override pyqtgraph.AxisItem.setLabel."""
        show_label = False
        if text is not None:
            # ungekuerzt merken - gezeigt wird die passende Fassung
            self.full_label = text
            self.labelText = text
            show_label = True
        if units is not None:
            self.labelUnits = units
            show_label = True
        if show_label:
            self.showLabel()
        if unitPrefix is not None:
            self.labelUnitPrefix = unitPrefix
        if len(args) > 0:
            self.labelStyle = args

        axis_log(f"setLabel id={id(self) % 10000} lage={self.orientation}", repr(text), f"hoehe={self.height():.0f}")

        self.label.setHtml(self.labelString())
        self._adjustSize()
        super().resizeEvent(None)

        self.picture = None
        self.update()

        self.refit_label()

        timer = getattr(self, "_fit_timer", None)
        if timer is not None:
            timer.start(0)

    def labelString(self):
        if self.labelUnits == "":
            if not self.autoSIPrefix or self.autoSIPrefixScale == 1.0:
                units = ""
            else:
                units = "(x%g)" % (1.0 / self.autoSIPrefixScale)
        else:
            units = f"({self.labelUnitPrefix}{self.labelUnits})"

        # ohne Einheit kein angehaengtes Leerzeichen - sonst sitzt die
        # mittig ausgerichtete Beschriftung sichtbar daneben
        s = f"{self.labelText} {units}" if units else self.labelText
        self._label_with_unit = s

        style = ";".join([f"{k}: {self.labelStyle[k]}" for k in self.labelStyle])

        lsbl = f"<span style='{style}'>{s}</span>"
        return lsbl

    def on_width_handle(self, pos):
        """Liegt die Position am inneren Rand der Skala?"""
        if self.orientation == "left":
            return pos.x() >= self.width() - WIDTH_HANDLE
        elif self.orientation == "right":
            return pos.x() <= WIDTH_HANDLE

        return False

    def hoverMoveEvent(self, event):
        if self.orientation in ("left", "right"):
            on_handle = self.on_width_handle(event.pos())
            if on_handle != self._on_width_handle:
                self._on_width_handle = on_handle
                if on_handle:
                    self.setCursor(QtCore.Qt.CursorShape.SizeHorCursor)
                else:
                    self.unsetCursor()

        super().hoverMoveEvent(event)

    def mouseDragEvent(self, event):
        # Am inneren Rand zieht die Maus die Breite der Skala - auch bei
        # gesperrter Achse, denn das ist reine Darstellung.
        # Ob am Rand gezogen wird, entscheidet sich EINMAL beim Druecken.
        # Frueher wurde das bei jedem Ereignis neu geprueft - sobald die
        # Skala breiter war als der Druckpunkt, galt sie nicht mehr als
        # getroffen und das Ziehen endete. Genau daher die Grenze.
        if event.isStart():
            self._width_drag = (
                self.orientation in ("left", "right")
                and event.button() == QtCore.Qt.MouseButton.LeftButton
                and self.on_width_handle(event.buttonDownPos())
            )
            # Die Breite der Skala, nicht die sichtbare Gesamtbreite:
            # sonst kaeme bei jedem Ziehen die Beschriftung noch dazu.
            self._width_at_press = self.tick_width()

        if self._width_drag:
            event.accept()

            offset = event.pos().x() - event.buttonDownPos().x()
            if self.orientation == "right":
                offset = -offset

            self.setWidth(int(self._width_at_press + offset))
            self.picture = None
            self.update()

            if event.isFinish():
                self._width_drag = False
                self._width_at_press = None
                # Alle Strips eines Fensters halten dieselbe
                # Skalenbreite - sonst beginnen die
                # Zeichenflaechen untereinander an verschiedenen
                # Stellen.
                self.width_changed.emit(self.tick_width())

            return

        if self.locked:
            return

        if self.orientation in ("left", "right"):
            ev = event
            ev.accept()

            pos = ev.pos()
            lastPos = ev.lastPos()
            dif = pos - lastPos
            dif = dif * -1

            if ev.button() in [
                QtCore.Qt.MouseButton.LeftButton,
                QtCore.Qt.MouseButton.MiddleButton,
            ]:
                if self.orientation in ("left", "right"):
                    scale = (self.range[1] - self.range[0]) / self.sceneBoundingRect().height()
                    delta = scale * dif.y()
                else:
                    scale = (self.range[1] - self.range[0]) / self.sceneBoundingRect().width()
                    delta = scale * dif.x()

                self.setRange(self.range[0] - delta, self.range[1] - delta)

            elif ev.button() & QtCore.Qt.MouseButton.RightButton:
                mid = sum(self.range) / 2
                delta = self.range[-1] - self.range[0]

                if self.orientation in ("left", "right"):
                    if dif.y() > 0:
                        delta = 0.94 * delta
                    else:
                        delta = 1.06 * delta

                else:
                    if dif.x() > 0:
                        delta = 0.94 * delta
                    else:
                        delta = 1.06 * delta

                self.setRange(mid - delta / 2, mid + delta / 2)
        else:
            # Ziehen auf der Zeitachse verschiebt die Zeit (linke Taste)
            # oder dehnt sie (rechte Taste) - genau wie bei den
            # Werteachsen daneben. Vorher ging das Ereignis an die
            # Zeichenflaeche, die daraus einen Zoomrahmen gemacht hat:
            # wer die Zeitleiste knapp verfehlt hat, bekam einen Zoom
            # statt einer Verschiebung.
            view = self.linkedView()
            if view is None:
                return

            event.accept()

            breite = self.sceneBoundingRect().width() or 1
            x0, x1 = view.viewRange()[0]
            spanne = x1 - x0
            weg = event.pos().x() - event.lastPos().x()

            if event.button() in (
                QtCore.Qt.MouseButton.LeftButton,
                QtCore.Qt.MouseButton.MiddleButton,
            ):
                delta = spanne / breite * weg
                view.setXRange(x0 - delta, x1 - delta, padding=0)

            elif event.button() & QtCore.Qt.MouseButton.RightButton:
                mitte = (x0 + x1) / 2
                spanne = spanne * (0.94 if weg > 0 else 1.06)
                view.setXRange(mitte - spanne / 2, mitte + spanne / 2, padding=0)

            return

    def mouseClickEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.RightButton:
            if self.sceneBoundingRect().contains(event.scenePos()):
                event.accept()
                self.raiseContextMenu(event)
        else:
            lv = self.linkedView()
            if lv is None:
                return
            return lv.mouseClickEvent(event)

    def hoverEnterEvent(self, event):
        self.hovering = True
        self.picture = None
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.hovering = False
        self.picture = None
        self.update()
        super().hoverLeaveEvent(event)

    def resizeEvent(self, ev=None):
        if self.orientation in ("left", "right"):
            nudge = 5

            if self.minus is not None:
                br = self.minus.boundingRect()
                p = QtCore.QPointF(0, 0)
                if self.orientation == "left":
                    p.setY(5)
                    p.setX(nudge)
                elif self.orientation == "right":
                    p.setY(5)
                    p.setX(int(self.size().width() - br.height() + nudge))

                self.minus.setPos(p)

            if self.plus is not None:
                br = self.plus.boundingRect()
                p = QtCore.QPointF(0, 0)
                if self.orientation == "left":
                    p.setY(26)
                    p.setX(nudge)
                elif self.orientation == "right":
                    p.setY(26)
                    p.setX(int(self.size().width() - br.height() + nudge))

                self.plus.setPos(p)

        result = super().resizeEvent(ev)

        axis_log(f"resize   id={id(self) % 10000} lage={self.orientation} hoehe={self.height():.0f}")

        # Die Beschriftung passt nur zu einer bestimmten Achsenhoehe -
        # bei jeder Groessenaenderung neu kuerzen und mittig setzen.
        # Aber erst, wenn auch die Zeichenflaeche ihre neue Groesse hat:
        # an dieser Stelle liefert sie noch die alte, und daran gemessen
        # wird der Text mal zu frueh, mal zu spaet gekuerzt.
        timer = getattr(self, "_fit_timer", None)
        if timer is not None:
            timer.start(0)
        else:
            self.refit_label()

        return result

    def close(self):
        self.linked_signal = None
        if self.plus is not None:
            self.scene().removeItem(self.plus)
        if self.minus is not None:
            self.scene().removeItem(self.minus)
        self.plus = None
        self.minus = None

        super().close()

    def set_pen(self, pen=None):
        if pen is None:
            pen = fn.mkPen(pen)

        color = pen.color()

        if self.minus is not None:
            p = QtGui.QPainter(self.minus.pixmap)
            self.minus.pixmap.fill(color)
            p.setBrush(self.background)
            p.drawRect(QtCore.QRect(4, 24, 56, 15))
            p.end()

        if self.plus is not None:
            p = QtGui.QPainter(self.plus.pixmap)
            self.plus.pixmap.fill(color)
            p.setBrush(self.background)
            p.drawRect(QtCore.QRect(4, 24, 56, 15))
            p.drawRect(QtCore.QRect(24, 4, 15, 56))
            p.end()

        if pen is not self._pen:
            self.setPen(pen)

    def raiseContextMenu(self, ev):
        low, high = self.range

        if self.orientation in ("left", "right"):
            if self.linked_signal is None:
                axis = f"{self.linked_signal.name} Y"
            else:
                axis = "Y"
        else:
            axis = "X"

        menu = QtWidgets.QMenu()
        menu.addAction(f"Edit {axis} axis scaling")
        menu.addSeparator()
        menu.addAction("Apply new axis limits")
        menu.addSeparator()

        widget = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout()
        widget.setLayout(layout)
        layout.addWidget(QtWidgets.QLabel("max:"))

        upper = QtWidgets.QDoubleSpinBox()
        upper.setDecimals(9)
        upper.setMinimum(-1e64)
        upper.setMaximum(1e35)
        upper.setValue(high)
        layout.addWidget(upper)

        a = QtWidgets.QWidgetAction(self)
        a.setDefaultWidget(widget)
        menu.addAction(a)

        widget = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout()
        widget.setLayout(layout)
        layout.addWidget(QtWidgets.QLabel("min:"))

        lower = QtWidgets.QDoubleSpinBox()
        lower.setDecimals(9)
        lower.setMinimum(-1e64)
        lower.setMaximum(1e35)
        lower.setValue(low)
        layout.addWidget(lower)

        a = QtWidgets.QWidgetAction(self)
        a.setDefaultWidget(widget)
        menu.addAction(a)

        action = menu.exec_(ev.screenPos().toPoint())

        if action is None:
            return

        elif action.text() == "Apply new axis limits":
            if self.orientation in ("left", "right"):
                self.setRange(lower.value(), upper.value())
            else:
                self.setRange(lower.value(), upper.value())

        elif action.text() == f"Edit {axis} axis scaling":
            self.scale_editor_requested.emit(self.uuid)

    def set_font_size(self, size):
        font = self.font()
        font.setPointSize(size)

        if self.orientation in ("top", "bottom"):
            metric = QtGui.QFontMetrics(font)
            height = max(metric.height() + 2, 18)
            self.setStyle(tickFont=font, tickTextHeight=height)
        else:
            self.setStyle(tickFont=font)
            self.setFont(font)

        self.label.setFont(font)

    def setRange(self, mn, mx):
        if mn > mx:
            mn, mx = mx, mn
        super().setRange(mn, mx)
        self.rangeChanged.emit(self.uuid, (mn, mx))

    def wheelEvent(self, event):
        if self.locked:
            return

        lv = self.linkedView()

        factor = self._settings.value("plot/zoom/wheel_factor", 0.165, type=float)

        if lv is None:
            # this is one of the individual axis

            pos = event.pos()
            rect = self.boundingRect()

            zoom_y_mode = self._settings.value("plot/zoom/y_mode", "center_on_mouse")

            if zoom_y_mode == "pin_zero_level":
                plot, uuid = self.linked_signal

                y_pos_val, sig_y_bottom, sig_y_top = plot.value_at_cursor(uuid)
                delta_proc = sig_y_top / (sig_y_top - sig_y_bottom)

                delta = sig_y_top - sig_y_bottom

                if event.delta() > 0:
                    delta *= 1 / (1 + factor)
                else:
                    delta *= 1 + factor

                end = delta_proc * delta
                start = end - delta

                self.setRange(start, end)

            elif zoom_y_mode == "center_on_cursor":
                plot, uuid = self.linked_signal

                y_pos_val, sig_y_bottom, sig_y_top = plot.value_at_cursor(uuid)

                delta_proc = (y_pos_val - (sig_y_top + sig_y_bottom) / 2) / (sig_y_top - sig_y_bottom)
                shift = delta_proc * (sig_y_top - sig_y_bottom)
                sig_y_top, sig_y_bottom = sig_y_top + shift, sig_y_bottom + shift

                delta = sig_y_top - sig_y_bottom

                if event.delta() > 0:
                    end = sig_y_top - factor * delta
                    start = sig_y_bottom + factor * delta
                else:
                    end = sig_y_top + factor * delta
                    start = sig_y_bottom - factor * delta

                self.setRange(start, end)

            elif zoom_y_mode == "center_on_mouse":
                y_pos_val = ((rect.height() + rect.y()) - pos.y()) / rect.height() * (
                    self.range[-1] - self.range[0]
                ) + self.range[0]

                ratio = abs((pos.y() - (rect.height() + rect.y())) / rect.height())

                delta = self.range[-1] - self.range[0]

                if event.delta() > 0:
                    delta = (1 - factor) * delta
                else:
                    delta = (1 + factor) * delta

                start = y_pos_val - ratio * delta
                end = y_pos_val + (1 - ratio) * delta

                self.setRange(start, end)

            event.accept()
        else:
            # this is the main Y axis or the X axis
            if self.orientation in ("top", "bottom"):
                super().wheelEvent(event)
            else:
                # main Y axis

                if not lv.state["mouseEnabled"][1]:
                    event.ignore()
                else:
                    # the plot is not Y locked
                    pos = event.pos()
                    rect = self.boundingRect()

                    zoom_y_mode = self._settings.value("plot/zoom/y_mode", "center_on_mouse")

                    if zoom_y_mode == "pin_zero_level":
                        plot, uuid = self.linked_signal

                        y_pos_val, sig_y_bottom, sig_y_top = plot.value_at_cursor()
                        delta_proc = sig_y_top / (sig_y_top - sig_y_bottom)

                        delta = sig_y_top - sig_y_bottom

                        if event.delta() > 0:
                            delta *= 1 / (1 + factor)
                        else:
                            delta *= 1 + factor

                        end = delta_proc * delta
                        start = end - delta

                        self.setRange(start, end)

                    elif zoom_y_mode == "center_on_cursor":
                        plot, uuid = self.linked_signal

                        y_pos_val, sig_y_bottom, sig_y_top = plot.value_at_cursor()

                        if isinstance(y_pos_val, (int, float)):
                            delta_proc = (y_pos_val - (sig_y_top + sig_y_bottom) / 2) / (sig_y_top - sig_y_bottom)
                            shift = delta_proc * (sig_y_top - sig_y_bottom)
                            sig_y_top, sig_y_bottom = sig_y_top + shift, sig_y_bottom + shift

                        delta = sig_y_top - sig_y_bottom

                        if event.delta() > 0:
                            end = sig_y_top - factor * delta
                            start = sig_y_bottom + factor * delta
                        else:
                            end = sig_y_top + factor * delta
                            start = sig_y_bottom - factor * delta

                        self.setRange(start, end)

                    else:
                        y_pos_val = ((rect.height() + rect.y()) - pos.y()) / rect.height() * (
                            self.range[-1] - self.range[0]
                        ) + self.range[0]

                        ratio = abs((pos.y() - (rect.height() + rect.y())) / rect.height())

                        delta = self.range[-1] - self.range[0]

                        if event.delta() > 0:
                            delta = (1 - factor) * delta
                        else:
                            delta = (1 + factor) * delta

                        start = y_pos_val - ratio * delta
                        end = y_pos_val + (1 - ratio) * delta

                        self.setRange(start, end)

                    event.accept()

    def drawPicture(self, p, axisSpec, tickSpecs, textSpecs, ratio=1.0):
        p.setRenderHint(p.RenderHint.Antialiasing, False)
        p.setRenderHint(p.RenderHint.TextAntialiasing, True)

        bounding = self.boundingRect().toAlignedRect()
        bounding.setSize(bounding.size() * ratio)
        bounding.moveTo(bounding.topLeft() * ratio)

        ## draw long line along axis
        pen, p1, p2 = axisSpec
        p.setPen(pen)
        p.drawLine(p1, p2)

        ## draw ticks
        for pen, p1, p2 in tickSpecs:
            p.setPen(pen)
            p.drawLine(p1, p2)

        # Draw all text
        if self.style["tickFont"] is not None:
            p.setFont(self.style["tickFont"])
        p.setPen(self.textPen())

        p.setClipRect(bounding)
        for rect, flags, text in textSpecs:
            p.drawText(rect, int(flags), text)

    def paint(self, p, opt, widget):
        rect = self.boundingRect()

        width = ceil(rect.width())
        height = ceil(rect.height())
        ratio = widget.devicePixelRatio() if widget else 1.0

        if self.picture is None:
            try:
                picture = QtGui.QPixmap(ceil(width * ratio), ceil(height * ratio))

                if self.hovering:
                    if self.background == fn.mkColor("#000000"):
                        bk = fn.mkColor("#323232")
                    else:
                        bk = fn.mkColor("#CDCDCD")

                else:
                    bk = self.background

                picture.fill(bk)

                painter = QtGui.QPainter()
                painter.begin(picture)

                if self.isVisible():
                    painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceOver)
                    if self.style["tickFont"]:
                        painter.setFont(self.style["tickFont"])
                    specs = self.generateDrawSpecs(painter, ratio)

                    if specs is not None:
                        self.drawPicture(painter, *specs, ratio)

                    bounding = self.boundingRect().toAlignedRect()
                    bounding.setSize(bounding.size() * ratio)
                    bounding.moveTo(bounding.topLeft() * ratio)

                    if self.minus is not None:
                        painter.drawPixmap(
                            QtCore.QPoint(int(rect.x() * ratio) + 5 * ratio, 6 * ratio),
                            self.minus.pixmap.scaled(BUTTON_SIZE * ratio, BUTTON_SIZE * ratio),
                        )
                        painter.drawPixmap(
                            QtCore.QPoint((int(rect.x()) + 5) * ratio, 27 * ratio),
                            self.plus.pixmap.scaled(BUTTON_SIZE * ratio, BUTTON_SIZE * ratio),
                        )

                    if self.orientation in ("left", "right"):
                        painter.setPen(self._pen)

                        label_rect = QtCore.QRectF(
                            1 * ratio,
                            1 * ratio,
                            rect.height() * ratio - (28 + BUTTON_SIZE) * ratio,
                            rect.width() * ratio,
                        )
                        painter.translate(rect.bottomLeft() * ratio)
                        painter.rotate(-90)

                        painter.setRenderHint(painter.RenderHint.TextAntialiasing, True)
                        painter.drawText(
                            label_rect,
                            QtCore.Qt.AlignmentFlag.AlignHCenter | QtCore.Qt.AlignmentFlag.AlignTop,
                            self._label_with_unit,
                        )
                        painter.rotate(90)
                        painter.resetTransform()

            except:
                print(format_exc())

            finally:
                painter.end()

            picture.setDevicePixelRatio(widget.devicePixelRatio() if widget else 1.0)

            self.picture = picture

    def generateDrawSpecs(self, p, ratio):
        """Call tickValues() and tickStrings() to determine where and how ticks
        should be drawn, then generates from this a set of drawing commands to
        be interpreted by drawPicture().
        """
        bounds = self.mapRectFromParent(self.geometry())

        tickBounds = bounds

        if self.orientation == "left":
            span = (bounds.topRight(), bounds.bottomRight())
            tickStart = tickBounds.right()
            tickStop = bounds.right()
            tickDir = -1
            axis = 0
        elif self.orientation == "right":
            span = (bounds.topLeft(), bounds.bottomLeft())
            tickStart = tickBounds.left()
            tickStop = bounds.left()
            tickDir = 1
            axis = 0
        elif self.orientation == "top":
            span = (bounds.bottomLeft(), bounds.bottomRight())
            tickStart = tickBounds.bottom()
            tickStop = bounds.bottom()
            tickDir = -1
            axis = 1
        elif self.orientation == "bottom":
            span = (bounds.topLeft(), bounds.topRight())
            tickStart = tickBounds.top()
            tickStop = bounds.top()
            tickDir = 1
            axis = 1
        else:
            raise ValueError("self.orientation must be in ('left', 'right', 'top', 'bottom')")

        ## determine size of this item in pixels
        points = list(map(self.mapToDevice, span))
        if None in points:
            return
        lengthInPixels = Point(points[1] - points[0]).length()
        if lengthInPixels == 0:
            return

        # Determine major / minor / subminor axis ticks
        if self._tickLevels is None:
            tickLevels = self.tickValues(self.range[0], self.range[1], lengthInPixels)
            tickStrings = None
        else:
            ## parse self.tickLevels into the formats returned by tickLevels() and tickStrings()
            tickLevels = []
            tickStrings = []
            for level in self._tickLevels:
                values = []
                strings = []
                tickLevels.append((None, values))
                tickStrings.append(strings)
                for val, strn in level:
                    values.append(val)
                    strings.append(strn)

        if self._settings.value("grid_major_ticks_only", False, type=bool):
            tickLevels = tickLevels[:1]
            if tickStrings is not None:
                tickStrings = tickStrings[:1]

        ## determine mapping between tick values and local coordinates
        dif = self.range[1] - self.range[0]
        if dif == 0:
            xScale = 1
            offset = 0
        else:
            if axis == 0:
                xScale = -bounds.height() / dif
                offset = self.range[0] * xScale - bounds.height()
            else:
                xScale = bounds.width() / dif
                offset = self.range[0] * xScale

        xRange = [x * xScale - offset for x in self.range]
        xMin = min(xRange)
        xMax = max(xRange)

        tickPositions = []  # remembers positions of previously drawn ticks

        ## compute coordinates to draw ticks
        ## draw three different intervals, long ticks first
        tickSpecs = []
        for i in range(len(tickLevels)):
            tickPositions.append([])
            ticks = tickLevels[i][1]

            ## length of tick
            tickLength = self.style["tickLength"] / ((i * 0.5) + 1.0)

            lineAlpha = self.style["tickAlpha"]
            if lineAlpha is None:
                lineAlpha = (0.6**i) * 255

            elif isinstance(lineAlpha, float):
                lineAlpha *= 255
                lineAlpha = max(0, round(lineAlpha))
                lineAlpha = min(255, round(lineAlpha))
            elif isinstance(lineAlpha, int):
                if (lineAlpha > 255) or (lineAlpha < 0):
                    raise ValueError("lineAlpha should be [0..255]")
            else:
                raise TypeError("Line Alpha should be of type None, float or int")

            for v in ticks:
                ## determine actual position to draw this tick
                x = (v * xScale) - offset
                if x < xMin or x > xMax:  ## last check to make sure no out-of-bounds ticks are drawn
                    tickPositions[i].append(None)
                    continue
                tickPositions[i].append(x)

                p1 = [x, x]
                p2 = [x, x]
                p1[axis] = tickStart
                p2[axis] = tickStop
                p2[axis] += tickLength * tickDir
                tickPen = self.pen()
                color = tickPen.color()
                color.setAlpha(int(lineAlpha))
                tickPen.setColor(color)
                tickSpecs.append((tickPen, Point(p1), Point(p2)))

        if self.style["stopAxisAtTick"][0] is True:
            minTickPosition = min(map(min, tickPositions))
            if axis == 0:
                stop = max(span[0].y(), minTickPosition)
                span[0].setY(stop)
            else:
                stop = max(span[0].x(), minTickPosition)
                span[0].setX(stop)
        if self.style["stopAxisAtTick"][1] is True:
            maxTickPosition = max(map(max, tickPositions))
            if axis == 0:
                stop = min(span[1].y(), maxTickPosition)
                span[1].setY(stop)
            else:
                stop = min(span[1].x(), maxTickPosition)
                span[1].setX(stop)
        axisSpec = (self.pen(), span[0], span[1])

        textOffset = self.style["tickTextOffset"][axis]  ## spacing between axis and text
        # if self.style['autoExpandTextSpace'] is True:
        # textWidth = self.textWidth
        # textHeight = self.textHeight
        # else:
        # textWidth = self.style['tickTextWidth'] ## space allocated for horizontal text
        # textHeight = self.style['tickTextHeight'] ## space allocated for horizontal text

        textSize2 = 0
        lastTextSize2 = 0
        textRects = []
        textSpecs = []  ## list of draw

        # If values are hidden, return early
        if not self.style["showValues"]:
            return (axisSpec, tickSpecs, textSpecs)

        for i in range(min(len(tickLevels), self.style["maxTextLevel"] + 1)):
            ## Get the list of strings to display for this level
            if tickStrings is None:
                spacing, values = tickLevels[i]
                strings = self.tickStrings(values, self.autoSIPrefixScale * self.scale, spacing)
            else:
                strings = tickStrings[i]

            if len(strings) == 0:
                continue

            ## ignore strings belonging to ticks that were previously ignored
            for j in range(len(strings)):
                if tickPositions[i][j] is None:
                    strings[j] = None

            ## Measure density of text; decide whether to draw this level
            rects = []
            for s in strings:
                if s is None:
                    rects.append(None)
                else:
                    br = p.boundingRect(
                        QtCore.QRectF(0, 0, 100, 100),
                        QtCore.Qt.AlignmentFlag.AlignCenter,
                        s,
                    )
                    ## boundingRect is usually just a bit too large
                    ## (but this probably depends on per-font metrics?)
                    br.setHeight(br.height() * 0.8)

                    rects.append(br)
                    textRects.append(rects[-1])

            if len(textRects) > 0:
                ## measure all text, make sure there's enough room
                if axis == 0:
                    textSize = np.sum([r.height() for r in textRects])
                    textSize2 = np.max([r.width() for r in textRects])
                else:
                    textSize = np.sum([r.width() for r in textRects])
                    textSize2 = np.max([r.height() for r in textRects])
            else:
                textSize = 0
                textSize2 = 0

            if i > 0:  ## always draw top level
                ## If the strings are too crowded, stop drawing text now.
                ## We use three different crowding limits based on the number
                ## of texts drawn so far.
                textFillRatio = float(textSize) / lengthInPixels
                finished = False
                for nTexts, limit in self.style["textFillLimits"]:
                    if len(textSpecs) >= nTexts and textFillRatio >= limit:
                        finished = True
                        break
                if finished:
                    break

            lastTextSize2 = textSize2

            # spacing, values = tickLevels[best]
            # strings = self.tickStrings(values, self.scale, spacing)
            # Determine exactly where tick text should be drawn
            for j in range(len(strings)):
                vstr = strings[j]
                if vstr is None:  ## this tick was ignored because it is out of bounds
                    continue
                x = tickPositions[i][j]
                # textRect = p.boundingRect(QtCore.QRectF(0, 0, 100, 100), QtCore.Qt.AlignmentFlag.AlignCenter, vstr)
                textRect = rects[j]
                height = textRect.height()
                width = textRect.width()
                # self.textHeight = height
                offset = max(0, self.style["tickLength"]) + textOffset

                if self.orientation == "left":
                    alignFlags = QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
                    rect = QtCore.QRectF(tickStop - offset - width, x - (height / 2), width, height)
                elif self.orientation == "right":
                    alignFlags = QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
                    rect = QtCore.QRectF(tickStop + offset, x - (height / 2), width, height)
                elif self.orientation == "top":
                    alignFlags = QtCore.Qt.AlignmentFlag.AlignHCenter | QtCore.Qt.AlignmentFlag.AlignBottom
                    rect = QtCore.QRectF(x - width / 2.0, tickStop - offset - height, width, height)
                elif self.orientation == "bottom":
                    alignFlags = QtCore.Qt.AlignmentFlag.AlignHCenter | QtCore.Qt.AlignmentFlag.AlignTop
                    rect = QtCore.QRectF(x - width / 2.0, tickStop + offset, width, height)

                textFlags = alignFlags | QtCore.Qt.TextFlag.TextDontClip
                # p.setPen(self.pen())
                # p.drawText(rect, textFlags, vstr)
                textSpecs.append((rect, textFlags, vstr))

        ## update max text size if needed.
        self._updateMaxTextSize(lastTextSize2)

        self.tickSpecs = tickSpecs

        axisSpec = (
            axisSpec[0],
            axisSpec[1] * ratio,
            axisSpec[2] * ratio,
        )

        bounds.setSize(bounds.size() * ratio)

        for spec in textSpecs:
            spec[0].setSize(spec[0].size() * ratio)
            spec[0].moveTo(spec[0].topLeft() * ratio)

        for i, spec in enumerate(tickSpecs):
            tickSpecs[i] = (
                spec[0],
                spec[1] * ratio,
                spec[2] * ratio,
            )

        return (axisSpec, tickSpecs, textSpecs)

    def boundingRect(self):
        m = 0
        hide_overlapping_labels = self.style["hideOverlappingLabels"]
        if hide_overlapping_labels is True:
            pass  # skip further checks
        elif hide_overlapping_labels is False:
            m = 15
        else:
            try:
                m = int(self.style["hideOverlappingLabels"])
            except ValueError:
                pass  # ignore any non-numeric value

        rect = self.mapRectFromParent(self.geometry())
        ## extend rect if ticks go in negative direction
        ## also extend to account for text that flows past the edges
        tl = self.style["tickLength"]
        if self.orientation == "left":
            rect = rect.adjusted(0, 0, -min(0, tl), 0)
        elif self.orientation == "right":
            rect = rect.adjusted(min(0, tl), 0, 0, 0)
        elif self.orientation == "top":
            rect = rect.adjusted(0, 0, 0, -min(0, tl))
        elif self.orientation == "bottom":
            rect = rect.adjusted(0, min(0, tl), 0, 0)
        return rect
