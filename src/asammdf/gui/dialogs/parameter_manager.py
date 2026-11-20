"""Parameter bearbeiten: Skalare, Kennlinien, Kennfelder.

Links die Liste der Parameter, rechts ihre Werte als Tabelle - beim
Kennfeld mit der x-Achse quer und der y-Achse senkrecht, so wie in
INCA oder MDA. Was hier herauskommt, ist der Inhalt des Schluessels
"parameters" in der Konfiguration; den Code fuer die Berechnung macht
mdfview_parameters daraus.
"""

from copy import deepcopy

from PySide6 import QtCore, QtWidgets

from ..mdfview_geometry import remember_geometry
from ..mdfview_parameters import is_valid_name, new_parameter
from .messagebox import MessageBox

TYPE_NAMES = {"scalar": "scalar", "curve": "curve", "map": "map"}


def _zahl(text, ersatz=0.0):
    try:
        return float(str(text).replace(",", "."))
    except (TypeError, ValueError):
        return ersatz


class ParameterManagerDialog(QtWidgets.QDialog):
    def __init__(self, parameters=None, parent=None):
        super().__init__(parent)

        self.parameters = deepcopy(parameters or {})
        self.changed_names = set()
        self.pressed_button = "cancel"
        self._original = deepcopy(self.parameters)
        self._current = None

        self.setWindowTitle("Parameter Manager")
        self.setSizeGripEnabled(True)
        self.setWindowFlags(QtCore.Qt.WindowType.Window)

        haupt = QtWidgets.QVBoxLayout(self)
        oben = QtWidgets.QHBoxLayout()
        haupt.addLayout(oben, 1)

        # ---------------------------------------------------------- links
        links = QtWidgets.QVBoxLayout()
        oben.addLayout(links)

        self.liste = QtWidgets.QListWidget()
        self.liste.setMinimumWidth(200)
        self.liste.currentTextChanged.connect(self.auswahl_gewechselt)
        links.addWidget(self.liste, 1)

        for text, kind in (("New scalar", "scalar"), ("New curve", "curve"), ("New map", "map")):
            knopf = QtWidgets.QPushButton(text)
            knopf.clicked.connect(lambda _=False, k=kind: self.neu(k))
            links.addWidget(knopf)

        knopf = QtWidgets.QPushButton("Rename")
        knopf.clicked.connect(self.umbenennen)
        links.addWidget(knopf)

        knopf = QtWidgets.QPushButton("Delete")
        knopf.clicked.connect(self.loeschen)
        links.addWidget(knopf)

        # ---------------------------------------------------------- rechts
        rechts = QtWidgets.QVBoxLayout()
        oben.addLayout(rechts, 1)

        kopf = QtWidgets.QFormLayout()
        self.art = QtWidgets.QLabel("-")
        self.einheit = QtWidgets.QLineEdit()
        self.x_einheit = QtWidgets.QLineEdit()
        self.y_einheit = QtWidgets.QLineEdit()
        kopf.addRow("Type", self.art)
        kopf.addRow("Unit", self.einheit)
        kopf.addRow("Unit of x axis", self.x_einheit)
        kopf.addRow("Unit of y axis", self.y_einheit)
        rechts.addLayout(kopf)

        self.tabelle = QtWidgets.QTableWidget()
        self.tabelle.horizontalHeader().setStretchLastSection(False)
        rechts.addWidget(self.tabelle, 1)

        knoepfe = QtWidgets.QHBoxLayout()
        self.spalte_mehr = QtWidgets.QPushButton("Column +")
        self.spalte_weniger = QtWidgets.QPushButton("Column -")
        self.zeile_mehr = QtWidgets.QPushButton("Row +")
        self.zeile_weniger = QtWidgets.QPushButton("Row -")
        self.spalte_mehr.clicked.connect(lambda: self.achse_aendern("x", 1))
        self.spalte_weniger.clicked.connect(lambda: self.achse_aendern("x", -1))
        self.zeile_mehr.clicked.connect(lambda: self.achse_aendern("y", 1))
        self.zeile_weniger.clicked.connect(lambda: self.achse_aendern("y", -1))
        for knopf in (self.spalte_mehr, self.spalte_weniger, self.zeile_mehr, self.zeile_weniger):
            knoepfe.addWidget(knopf)
        knoepfe.addStretch(1)
        rechts.addLayout(knoepfe)

        # ---------------------------------------------------------- unten
        unten = QtWidgets.QHBoxLayout()
        unten.addStretch(1)
        self.apply_btn = QtWidgets.QPushButton("Apply")
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.apply_btn.clicked.connect(self.apply)
        self.cancel_btn.clicked.connect(self.cancel)
        unten.addWidget(self.apply_btn)
        unten.addWidget(self.cancel_btn)
        haupt.addLayout(unten)

        self.liste.addItems(sorted(self.parameters))
        if self.liste.count():
            self.liste.setCurrentRow(0)
        else:
            self.tabelle.setEnabled(False)

        remember_geometry(self, "parameter_manager", (900, 650))

    # ------------------------------------------------------------ Liste

    def neu(self, kind):
        name, ok = QtWidgets.QInputDialog.getText(self, "New parameter", "Name:")
        if not ok:
            return

        name = name.strip()

        if not is_valid_name(name):
            MessageBox.warning(self, "Invalid name", "The name must be a valid Python name, e.g. eta_kf.")
            return

        if name in self.parameters:
            MessageBox.warning(self, "Name already used", f"There is already a parameter called {name}.")
            return

        self.werte_lesen()
        self.parameters[name] = new_parameter(kind)
        self.liste.addItem(name)
        self.liste.setCurrentRow(self.liste.count() - 1)

    def umbenennen(self):
        alt = self.liste.currentItem().text() if self.liste.currentItem() else ""
        if not alt:
            return

        name, ok = QtWidgets.QInputDialog.getText(self, "Rename parameter", "Name:", text=alt)
        if not ok:
            return

        name = name.strip()

        if not is_valid_name(name) or (name in self.parameters and name != alt):
            MessageBox.warning(self, "Invalid name", "The name must be a valid, unused Python name.")
            return

        self.werte_lesen()
        self.parameters[name] = self.parameters.pop(alt)
        self._current = name
        self.liste.currentItem().setText(name)

    def loeschen(self):
        item = self.liste.currentItem()
        if item is None:
            return

        name = item.text()
        self.parameters.pop(name, None)
        self._current = None
        self.liste.takeItem(self.liste.row(item))

        if not self.liste.count():
            self.tabelle.setRowCount(0)
            self.tabelle.setColumnCount(0)
            self.tabelle.setEnabled(False)
            self.art.setText("-")

    def auswahl_gewechselt(self, name):
        # Werte des vorherigen Parameters sichern, bevor die Tabelle neu
        # aufgebaut wird.
        self.werte_lesen()

        self._current = name or None

        if not self._current:
            return

        self.tabelle.setEnabled(True)
        self.tabelle_aufbauen()

    # ------------------------------------------------------------ Tabelle

    def tabelle_aufbauen(self):
        info = self.parameters.get(self._current)
        if info is None:
            return

        kind = info.get("type", "scalar")
        self.art.setText(TYPE_NAMES.get(kind, kind))
        self.einheit.setText(info.get("unit", ""))
        self.x_einheit.setText(info.get("x_unit", ""))
        self.y_einheit.setText(info.get("y_unit", ""))

        self.x_einheit.setEnabled(kind in ("curve", "map"))
        self.y_einheit.setEnabled(kind == "map")
        self.spalte_mehr.setEnabled(kind in ("curve", "map"))
        self.spalte_weniger.setEnabled(kind in ("curve", "map"))
        self.zeile_mehr.setEnabled(kind == "map")
        self.zeile_weniger.setEnabled(kind == "map")

        tabelle = self.tabelle
        tabelle.blockSignals(True)
        tabelle.clear()

        if kind == "scalar":
            tabelle.setRowCount(1)
            tabelle.setColumnCount(1)
            tabelle.setHorizontalHeaderLabels(["Value"])
            tabelle.setVerticalHeaderLabels([""])
            tabelle.setItem(0, 0, QtWidgets.QTableWidgetItem(str(info.get("values", 0.0))))

        elif kind == "curve":
            x = info.get("x", [])
            werte = info.get("values", [])
            tabelle.setRowCount(len(x))
            tabelle.setColumnCount(2)
            tabelle.setHorizontalHeaderLabels(["x", "Value"])
            tabelle.setVerticalHeaderLabels([str(i + 1) for i in range(len(x))])
            for zeile, stelle in enumerate(x):
                tabelle.setItem(zeile, 0, QtWidgets.QTableWidgetItem(str(stelle)))
                wert = werte[zeile] if zeile < len(werte) else 0.0
                tabelle.setItem(zeile, 1, QtWidgets.QTableWidgetItem(str(wert)))

        else:
            x = info.get("x", [])
            y = info.get("y", [])
            werte = info.get("values", [])

            tabelle.setRowCount(len(y) + 1)
            tabelle.setColumnCount(len(x) + 1)
            tabelle.setHorizontalHeaderLabels(["y \\ x"] + [f"x{i + 1}" for i in range(len(x))])
            tabelle.setVerticalHeaderLabels([""] + [f"y{j + 1}" for j in range(len(y))])

            # Kopfzeile: die x-Achse
            for spalte, stelle in enumerate(x, start=1):
                tabelle.setItem(0, spalte, QtWidgets.QTableWidgetItem(str(stelle)))

            sperre = QtWidgets.QTableWidgetItem("")
            sperre.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            tabelle.setItem(0, 0, sperre)

            for zeile, stelle in enumerate(y, start=1):
                tabelle.setItem(zeile, 0, QtWidgets.QTableWidgetItem(str(stelle)))

                for spalte in range(1, len(x) + 1):
                    try:
                        wert = werte[zeile - 1][spalte - 1]
                    except (IndexError, TypeError):
                        wert = 0.0
                    tabelle.setItem(zeile, spalte, QtWidgets.QTableWidgetItem(str(wert)))

        tabelle.blockSignals(False)
        tabelle.resizeColumnsToContents()

    def werte_lesen(self):
        """Tabelle in den Parameter zurueckschreiben."""
        if not self._current or self._current not in self.parameters:
            return

        info = self.parameters[self._current]
        kind = info.get("type", "scalar")
        tabelle = self.tabelle

        info["unit"] = self.einheit.text().strip()

        if kind == "scalar":
            item = tabelle.item(0, 0)
            info["values"] = _zahl(item.text() if item else 0.0)
            return

        info["x_unit"] = self.x_einheit.text().strip()

        if kind == "curve":
            paare = []
            for zeile in range(tabelle.rowCount()):
                x_item = tabelle.item(zeile, 0)
                w_item = tabelle.item(zeile, 1)
                paare.append((_zahl(x_item.text() if x_item else 0.0), _zahl(w_item.text() if w_item else 0.0)))

            # Die Interpolation braucht eine steigende Achse.
            paare.sort(key=lambda paar: paar[0])
            info["x"] = [paar[0] for paar in paare]
            info["values"] = [paar[1] for paar in paare]
            return

        info["y_unit"] = self.y_einheit.text().strip()

        spalten = tabelle.columnCount() - 1
        zeilen = tabelle.rowCount() - 1

        x = []
        for spalte in range(1, spalten + 1):
            item = tabelle.item(0, spalte)
            x.append(_zahl(item.text() if item else 0.0))

        y = []
        werte = []
        for zeile in range(1, zeilen + 1):
            item = tabelle.item(zeile, 0)
            y.append(_zahl(item.text() if item else 0.0))

            reihe = []
            for spalte in range(1, spalten + 1):
                feld = tabelle.item(zeile, spalte)
                reihe.append(_zahl(feld.text() if feld else 0.0))
            werte.append(reihe)

        # beide Achsen steigend sortieren und die Werte mitnehmen
        x_reihenfolge = sorted(range(len(x)), key=lambda i: x[i])
        y_reihenfolge = sorted(range(len(y)), key=lambda j: y[j])

        info["x"] = [x[i] for i in x_reihenfolge]
        info["y"] = [y[j] for j in y_reihenfolge]
        info["values"] = [[werte[j][i] for i in x_reihenfolge] for j in y_reihenfolge]

    def achse_aendern(self, achse, schritt):
        """Eine Stuetzstelle anhaengen oder die letzte wegnehmen."""
        self.werte_lesen()

        info = self.parameters.get(self._current)
        if info is None:
            return

        kind = info.get("type")

        if achse == "x":
            stellen = list(info.get("x", []))
        else:
            stellen = list(info.get("y", []))

        if schritt > 0:
            letzte = stellen[-1] if stellen else 0.0
            vorletzte = stellen[-2] if len(stellen) > 1 else letzte - 1.0
            stellen.append(letzte + max(letzte - vorletzte, 1.0))
        else:
            if len(stellen) <= 2:
                return
            stellen.pop()

        if kind == "curve":
            werte = list(info.get("values", []))
            if schritt > 0:
                werte.append(werte[-1] if werte else 0.0)
            else:
                werte.pop()
            info["x"] = stellen
            info["values"] = werte

        else:
            werte = [list(reihe) for reihe in info.get("values", [])]

            if achse == "x":
                for reihe in werte:
                    if schritt > 0:
                        reihe.append(reihe[-1] if reihe else 0.0)
                    else:
                        reihe.pop()
                info["x"] = stellen
            else:
                if schritt > 0:
                    werte.append(list(werte[-1]) if werte else [0.0] * len(info.get("x", [])))
                else:
                    werte.pop()
                info["y"] = stellen

            info["values"] = werte

        self.tabelle_aufbauen()

    # ------------------------------------------------------------ Schluss

    def apply(self, *args):
        self.werte_lesen()

        self.changed_names = {
            name
            for name in set(self.parameters) | set(self._original)
            if self.parameters.get(name) != self._original.get(name)
        }

        self.pressed_button = "apply"
        self.close()

    def cancel(self, *args):
        self.pressed_button = "cancel"
        self.parameters = deepcopy(self._original)
        self.changed_names = set()
        self.close()
