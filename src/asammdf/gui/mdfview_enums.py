"""Wertetabellen von Enum-Kanaelen: Eintraege lesen, Farben einstellen.

Welche Eintraege eine Enum hat, steht in der Messdatei selbst - in
derselben Wertetabelle, die auch die Achsenbeschriftung liefert. Die
Farbe steht dort nicht; sie ergibt sich normalerweise aus der
Reihenfolge der Eintraege.

Hier kann man sie fest vorgeben. Die Vorgabe haengt am *Inhalt* der
Tabelle und nicht am Kanalnamen: alle Signale mit derselben Enum werden
damit gleich dargestellt, auch in anderen Fenstern. Gespeichert wird in
der Anzeigekonfiguration (.dspf) und zusaetzlich in den
Qt-Einstellungen, damit die Farben einen Programmstart ueberleben.
"""

from functools import partial
import hashlib
import json

import numpy as np
import pyqtgraph.functions as fn
from PySide6 import QtCore, QtGui, QtWidgets

from . import serde

SETTINGS_KEY = "plot/enum_colors"

# {Tabellenschluessel: {Wertschluessel: "#rrggbb"}}
ENUM_COLORS = {}

# {id(Umrechnung): {Rohwert: (Text, Farbe)}}
ENUM_ENTRIES_CACHE = {}

_settings_read = False


# ----------------------------------------------------------------------
# Schluessel
# ----------------------------------------------------------------------
def value_key(value):
    """Rohwert als stabiler Textschluessel."""
    try:
        return format(float(value), ".10g")
    except (TypeError, ValueError):
        return str(value)


def table_key(pairs):
    """Schluessel aus dem Inhalt einer Wertetabelle.

    pairs: [(Rohwert, Text), ...]. Gleiche Tabelle - gleicher
    Schluessel, egal an welchem Kanal sie haengt.
    """
    payload = json.dumps(
        sorted((value_key(value), str(text)) for value, text in pairs),
        ensure_ascii=False,
    )

    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------
# Farben
# ----------------------------------------------------------------------
def default_color(index):
    """Farbe nach Position des Eintrags in der Tabelle."""
    return serde.COLORS[index % serde.COLORS_COUNT]


def value_color(value):
    """Farbe fuer einen Wert ohne Wertetabelle - gleicher Wert, gleiche Farbe."""
    try:
        index = abs(int(round(float(value))))
    except (TypeError, ValueError, OverflowError):
        index = 0

    return default_color(index)


def value_label(value):
    """Zahl als Beschriftung eines Balkens."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)

    return str(int(value)) if value.is_integer() else f"{value:g}"


def color_name(color):
    """"#rrggbb" oder None - mkColor wirft bei Unsinn eine Ausnahme."""
    try:
        color = fn.mkColor(color)
    except Exception:
        return None

    return color.name() if color.isValid() else None


def _ensure_settings_read():
    global _settings_read

    if _settings_read:
        return

    _settings_read = True

    raw = QtCore.QSettings().value(SETTINGS_KEY, "", str)
    if raw:
        _merge(raw)


def _merge(data):
    """Farbvorgaben uebernehmen, ohne die vorhandenen zu verlieren."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            return False

    if not isinstance(data, dict):
        return False

    changed = False

    for key, colors in data.items():
        if not isinstance(colors, dict):
            continue

        clean = {}
        for value, color in colors.items():
            name = color_name(color)
            if name:
                clean[str(value)] = name

        if clean and ENUM_COLORS.get(str(key)) != clean:
            ENUM_COLORS[str(key)] = clean
            changed = True

    return changed


def colors_of(key):
    """Eingestellte Farben einer Tabelle."""
    _ensure_settings_read()

    return dict(ENUM_COLORS.get(key) or {})


def color_for(key, value, index):
    """Farbe eines Eintrags: eingestellt oder nach Position."""
    _ensure_settings_read()

    chosen = (ENUM_COLORS.get(key) or {}).get(value_key(value))

    return chosen or default_color(index)


def set_colors(key, colors):
    """Farben einer Tabelle festlegen und alle Fenster neu zeichnen."""
    _ensure_settings_read()

    clean = {str(value): color_name(color) for value, color in (colors or {}).items()}
    clean = {value: color for value, color in clean.items() if color}

    if clean:
        ENUM_COLORS[key] = clean
    else:
        ENUM_COLORS.pop(key, None)

    save_settings()
    repaint_all()


# ----------------------------------------------------------------------
# Ablage
# ----------------------------------------------------------------------
def to_config():
    """Farbvorgaben fuer die Anzeigekonfiguration."""
    _ensure_settings_read()

    return {key: dict(colors) for key, colors in ENUM_COLORS.items() if colors}


def from_config(data):
    """Farbvorgaben aus einer Anzeigekonfiguration uebernehmen."""
    _ensure_settings_read()

    if _merge(data):
        save_settings()
        repaint_all()


def save_settings():
    QtCore.QSettings().setValue(SETTINGS_KEY, json.dumps(to_config()))


def repaint_all():
    """Alle Plots neu zeichnen - die Farben stecken in der Zeichnung."""
    ENUM_ENTRIES_CACHE.clear()

    # erst hier importieren, sonst haengen die Module im Kreis
    try:
        from .widgets.tree import ChannelsTreeWidget
    except ImportError:
        return

    done = set()

    for tree in list(ChannelsTreeWidget.instances):
        try:
            window = tree.plot
        except RuntimeError:
            # Qt-Objekt bereits abgeraeumt
            continue

        if window is None or id(window) in done:
            continue

        done.add(id(window))

        strips = list(getattr(window, "strips", None) or [])
        if not strips:
            strips = [getattr(window, "plot", None)]

        for graphics in strips:
            if graphics is None:
                continue
            try:
                graphics.update()
            except RuntimeError:
                continue


# ----------------------------------------------------------------------
# Wertetabelle lesen
# ----------------------------------------------------------------------
def enum_entries(conversion):
    """{Rohwert: (Text, Farbe)} aus der Wertetabelle einer Umrechnung."""
    if conversion is None:
        return {}

    key = id(conversion)
    cached = ENUM_ENTRIES_CACHE.get(key)
    if cached is not None:
        return cached

    values = []
    count = int(getattr(conversion, "val_param_nr", 0) or 0)
    blocks = getattr(conversion, "referenced_blocks", None) or {}
    texts = sum(1 for name in blocks if name.startswith("text_"))

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

    pairs = []
    for value in values:
        try:
            raw = conversion.convert(np.array([value]), as_bytes=True)[0]
        except Exception:
            raw = b""

        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace")
        else:
            text = str(raw)

        pairs.append((value, text))

    table = table_key(pairs)

    entries = {}
    for index, (value, text) in enumerate(pairs):
        entries[value] = (text, fn.mkColor(color_for(table, value, index)))

    if len(ENUM_ENTRIES_CACHE) > 256:
        ENUM_ENTRIES_CACHE.clear()

    ENUM_ENTRIES_CACHE[key] = entries

    return entries


def enum_table(conversion):
    """(Tabellenschluessel, [(Rohwert, Text, Farbe), ...]) einer Umrechnung."""
    entries = enum_entries(conversion)

    if not entries:
        return "", []

    rows = sorted(entries.items())
    key = table_key([(value, text) for value, (text, _color) in rows])

    return key, [(value, text, color) for value, (text, color) in rows]


def conversion_of(signal):
    """Die Umrechnung, aus der die Enum-Texte kommen."""
    if signal is None:
        return None

    return getattr(signal, "text_conversion", None) or getattr(signal, "conversion", None)


# ----------------------------------------------------------------------
# Dialog
# ----------------------------------------------------------------------
class EnumColorsDialog(QtWidgets.QDialog):
    """Farbe je Eintrag einer Wertetabelle waehlen."""

    def __init__(self, channel_name, key, rows, parent=None):
        super().__init__(parent)

        self.setWindowTitle(f"Enum colors - {channel_name}")

        self.key = key
        self.rows = list(rows)
        self.chosen = colors_of(key)
        self.buttons = []

        layout = QtWidgets.QVBoxLayout(self)

        hint = QtWidgets.QLabel(
            "The colors belong to the value table, not to a single channel: "
            "every channel using this enumeration is drawn the same way. "
            "They are stored in the display configuration."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QtWidgets.QTableWidget(len(self.rows), 3, self)
        self.table.setHorizontalHeaderLabels(["Value", "Text", "Color"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)

        for row, (value, text, color) in enumerate(self.rows):
            number = QtWidgets.QTableWidgetItem(value_key(value))
            number.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, 0, number)
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(text))

            button = QtWidgets.QPushButton(self.table)
            button.setFlat(True)
            button.clicked.connect(partial(self.pick_color, row))
            self.table.setCellWidget(row, 2, button)
            self.buttons.append(button)

            self.show_color(row, color)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)

        layout.addWidget(self.table)

        box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
            | QtWidgets.QDialogButtonBox.StandardButton.Reset
        )
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)

        reset = box.button(QtWidgets.QDialogButtonBox.StandardButton.Reset)
        reset.setToolTip("Back to the automatic colors")
        reset.clicked.connect(self.reset_colors)

        layout.addWidget(box)

        self.resize(500, min(700, 200 + 30 * len(self.rows)))

    def show_color(self, row, color):
        """Farbe auf dem Knopf der Zeile zeigen."""
        color = fn.mkColor(color)
        button = self.buttons[row]
        button.setText(color.name())

        # Schrift hell oder dunkel, je nachdem was auf der Farbe lesbar ist
        text = "#000000" if QtGui.QColor(color).lightness() > 127 else "#ffffff"

        button.setStyleSheet(
            f"QPushButton {{ background-color: {color.name()}; color: {text};"
            " border: 1px solid #808080; padding: 3px; }"
        )

    def pick_color(self, row):
        value = self.rows[row][0]
        current = fn.mkColor(self.chosen.get(value_key(value)) or self.rows[row][2])

        color = QtWidgets.QColorDialog.getColor(current, self, f"Color for {self.rows[row][1]}")

        if color.isValid():
            self.chosen[value_key(value)] = color.name()
            self.show_color(row, color)

    def reset_colors(self):
        """Wieder die Farben nach Position der Eintraege."""
        self.chosen = {}

        for row in range(len(self.rows)):
            self.show_color(row, default_color(row))

    def chosen_colors(self):
        """Nur die abweichenden Farben - der Rest bleibt automatisch."""
        result = {}

        for row, (value, _text, _color) in enumerate(self.rows):
            key = value_key(value)
            color = self.chosen.get(key)

            if color and fn.mkColor(color).name() != fn.mkColor(default_color(row)).name():
                result[key] = fn.mkColor(color).name()

        return result
