"""Parameter bearbeiten: Skalare, Kennlinien, Kennfelder.

Links die Liste der Parameter, rechts ihre Werte als Tabelle - beim
Kennfeld mit der x-Achse quer und der y-Achse senkrecht, so wie in
INCA oder MDA. Was hier herauskommt, ist der Inhalt des Schluessels
"parameters" in der Konfiguration; den Code fuer die Berechnung macht
mdfview_parameters daraus.

Das Fenster blockiert nichts: es bleibt offen, kann auf einen zweiten
Bildschirm wandern, und "Apply" uebernimmt die Werte und rechnet die
virtuellen Signale neu, die daran haengen. Mit dem Haken "apply
automatically" passiert das von selbst, kurz nachdem man aufhoert zu
tippen.
"""

from copy import deepcopy
from pathlib import Path
import re

from PySide6 import QtCore, QtGui, QtWidgets

from ..mdfview_geometry import remember_geometry
from traceback import format_exc

from ..mdfview_dcm import read_parameter_file
from ..mdfview_geometry import remember_geometry as _merke_groesse
from ..mdfview_parameters import is_valid_name, new_parameter, safe_name
from ..mdfview_paths import remember_folder, start_folder
from ..utils import PARAMETER_MIME, wildcard_pattern
from .messagebox import MessageBox

TYPE_NAMES = {"scalar": "scalar", "curve": "curve", "map": "map"}


def _zahl(text, ersatz=0.0):
    try:
        return float(str(text).replace(",", "."))
    except (TypeError, ValueError):
        return ersatz


def _normalisieren(info):
    """Alle Zahlen eines Parameters als float.

    Aus der Konfiguration kommen teils ganze Zahlen (10), aus der
    Tabelle immer Kommazahlen (10.0). Ohne diesen Durchgang gilt der
    Parameter schon beim Oeffnen als geaendert.
    """
    info = deepcopy(info)
    kind = info.get("type", "scalar")

    # Felder anlegen, die die Tabelle ohnehin schreibt - sonst
    # unterscheiden sich die beiden Staende allein durch leere Einheiten.
    info.setdefault("unit", "")
    if kind in ("curve", "map"):
        info.setdefault("x_unit", "")
        info.setdefault("x_name", "")
    if kind == "map":
        info.setdefault("y_unit", "")
        info.setdefault("y_name", "")

    if kind == "scalar":
        info["values"] = _zahl(info.get("values", 0.0))
        return info

    info["x"] = [_zahl(wert) for wert in info.get("x", [])]

    if kind == "curve":
        info["values"] = [_zahl(wert) for wert in info.get("values", [])]
        return info

    info["y"] = [_zahl(wert) for wert in info.get("y", [])]
    info["values"] = [[_zahl(wert) for wert in reihe] for reihe in info.get("values", [])]

    return info


class ParameterManagerDialog(QtWidgets.QDialog):
    # Werte uebernommen; dabei die Namen, die sich geaendert haben
    applied = QtCore.Signal(object)

    def __init__(self, parameters=None, file_widget=None, parent=None):
        super().__init__(parent)

        self.file_widget = file_widget

        if parameters is None and file_widget is not None:
            parameters = getattr(file_widget, "parameters", None)

        self.parameters = {name: _normalisieren(info) for name, info in (parameters or {}).items()}
        self.changed_names = set()
        self.pressed_button = "cancel"
        self._original = deepcopy(self.parameters)
        self._current = None
        self._offen = False

        self.setSizeGripEnabled(True)
        self.setWindowFlags(QtCore.Qt.WindowType.Window)

        # Spaeter Aenderungen sammeln und erst kurz nach dem letzten
        # Tastendruck rechnen - sonst rechnet das Programm bei jeder
        # Ziffer alles neu.
        self._auto_timer = QtCore.QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.setInterval(400)
        self._auto_timer.timeout.connect(self.apply)

        # Mit der Messdatei geht auch das Fenster zu: danach zeigen die
        # Parameter auf etwas, das es nicht mehr gibt.
        if file_widget is not None:
            file_widget.destroyed.connect(self.close)

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

        # Parameter lassen sich in einen Strip ziehen - dort entsteht
        # daraus ein virtuelles Signal, ohne dass man eine Funktion
        # schreiben muss.
        self._drag_start = None
        self._drag_item = None
        self.liste.viewport().installEventFilter(self)

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

        knopf = QtWidgets.QPushButton("Import DCM / CDFX...")
        knopf.setToolTip("Festwerte, Kennlinien und Kennfelder aus einer DCM- oder CDFX-Datei uebernehmen")
        knopf.clicked.connect(self.importieren)
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

        # Namen der Achsen: stehen in DCM- und CDFX-Dateien mit drin und
        # landen spaeter als Argumentnamen im Dialog fuer das virtuelle
        # Signal - dort sieht man dann gleich, welche Groesse wohin kommt.
        self.x_name = QtWidgets.QLineEdit()
        self.y_name = QtWidgets.QLineEdit()
        self.x_name.setPlaceholderText("x")
        self.y_name.setPlaceholderText("y")
        kopf.addRow("Name of x axis", self.x_name)
        kopf.addRow("Name of y axis", self.y_name)
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
        self.auto_box = QtWidgets.QCheckBox("apply automatically")
        self.auto_box.setToolTip("Werte kurz nach dem Tippen von selbst uebernehmen und neu rechnen")
        unten.addWidget(self.auto_box)
        unten.addStretch(1)
        self.apply_btn = QtWidgets.QPushButton("Apply")
        self.recompute_btn = QtWidgets.QPushButton("Recompute")
        self.recompute_btn.setToolTip("Alle virtuellen Signale neu rechnen, die Parameter benutzen")
        self.close_btn = QtWidgets.QPushButton("Close")
        self.apply_btn.clicked.connect(self.apply)
        self.recompute_btn.clicked.connect(self.recompute)
        self.close_btn.clicked.connect(self.close)
        unten.addWidget(self.apply_btn)
        unten.addWidget(self.recompute_btn)
        unten.addWidget(self.close_btn)
        haupt.addLayout(unten)

        self.liste_fuellen()
        if not self.liste.count():
            self.tabelle.setEnabled(False)

        self.tabelle.itemChanged.connect(self.wert_getippt)
        self._offen = True
        self.titel_setzen()

        remember_geometry(self, "parameter_manager", (900, 650))

    def liste_fuellen(self, auswahl=""):
        """Parameterliste neu aufbauen.

        Der Mauszeiger-Text sagt, was dahinter steckt - damit man nicht
        erst klicken muss, um Kennlinie von Kennfeld zu unterscheiden.
        """
        self.liste.clear()

        for name in sorted(self.parameters):
            info = self.parameters[name]
            art = info.get("type", "scalar")

            if art == "map":
                groesse = f"{len(info.get('x', []))} x {len(info.get('y', []))}"
            elif art == "curve":
                groesse = f"{len(info.get('x', []))} Punkte"
            else:
                groesse = str(info.get("values", ""))

            item = QtWidgets.QListWidgetItem(name)
            einheit = info.get("unit", "")
            item.setToolTip(f"{art}, {groesse}" + (f", {einheit}" if einheit else ""))
            self.liste.addItem(item)

        if auswahl:
            treffer = self.liste.findItems(auswahl, QtCore.Qt.MatchFlag.MatchExactly)
            if treffer:
                self.liste.setCurrentItem(treffer[0])
                return

        if self.liste.count():
            self.liste.setCurrentRow(0)

    # ------------------------------------------------------------ Import

    def importieren(self, *args):
        """Parameter aus einer DCM- oder CDFX-Datei uebernehmen."""
        pfad, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Import parameters",
            start_folder("parameter"),
            "Parameter files (*.dcm *.cdfx *.cdf *.xml);;DCM (*.dcm);;CDFX (*.cdfx *.cdf *.xml);;All files (*)",
        )

        if not pfad:
            return

        remember_folder("parameter", pfad)

        try:
            gefunden = read_parameter_file(pfad)
        except Exception:
            MessageBox.warning(self, "Import failed", f"The file could not be read.\n\n{format_exc()}")
            return

        if not gefunden:
            MessageBox.information(
                self,
                "Nothing to import",
                "No scalars, curves or maps were found in this file.",
            )
            return

        self.werte_lesen()

        dlg = ParameterImportDialog(gefunden, set(self.parameters), Path(pfad).name, parent=self)
        dlg.setModal(True)
        dlg.exec()

        if dlg.pressed_button != "apply" or not dlg.auswahl:
            return

        umbenannt = []

        for name in dlg.auswahl:
            info = dict(gefunden[name])
            ziel = name if is_valid_name(name) else safe_name(name)

            if ziel != name:
                umbenannt.append(f"{name} -> {ziel}")

            self.parameters[ziel] = _normalisieren(info)

        self.liste_fuellen()
        if self.liste.count():
            self.tabelle.setEnabled(True)

        # Gleich uebernehmen: sonst kennt das Programm die neuen
        # Parameter noch nicht, und sie liessen sich weder in einen
        # Strip ziehen noch in einer Funktion benutzen - ohne dass man
        # sieht, warum.
        self.apply()

        self.titel_setzen()

        if umbenannt:
            MessageBox.information(
                self,
                "Names adjusted",
                "These names are not valid Python names and were changed:\n\n" + "\n".join(umbenannt[:20]),
            )

    # ------------------------------------------------------------ Ziehen

    def eventFilter(self, obj, event):
        """Ziehen aus der Parameterliste mitlesen."""
        if obj is self.liste.viewport():
            art = event.type()

            if art == QtCore.QEvent.Type.MouseButtonPress:
                self._drag_start = event.position().toPoint()
                self._drag_item = self.liste.itemAt(self._drag_start)

            elif art == QtCore.QEvent.Type.MouseMove and self._drag_start is not None:
                weg = (event.position().toPoint() - self._drag_start).manhattanLength()
                if weg >= QtWidgets.QApplication.startDragDistance():
                    item, self._drag_item = self._drag_item, None
                    self._drag_start = None
                    self.start_parameter_drag(item)

            elif art == QtCore.QEvent.Type.MouseButtonRelease:
                self._drag_start = None
                self._drag_item = None

        return super().eventFilter(obj, event)

    def start_parameter_drag(self, item):
        """Einen Parameter zum Ziehen aufnehmen."""
        if item is None:
            return

        name = item.text()

        daten = QtCore.QMimeData()
        daten.setData(PARAMETER_MIME, name.encode("utf-8"))
        daten.setText(name)

        zug = QtGui.QDrag(self.liste)
        zug.setMimeData(daten)
        zug.exec(QtCore.Qt.DropAction.CopyAction)

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

        self.x_name.setText(info.get("x_name", ""))
        self.y_name.setText(info.get("y_name", ""))

        self.x_einheit.setEnabled(kind in ("curve", "map"))
        self.y_einheit.setEnabled(kind == "map")
        self.x_name.setEnabled(kind in ("curve", "map"))
        self.y_name.setEnabled(kind == "map")
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
            # Stuetzstellen stehen quer, die Werte darunter - so wie in
            # INCA und MDA, und so wie beim Kennfeld daneben. "Column +"
            # legt dann auch sichtbar eine Spalte dazu.
            x = info.get("x", [])
            werte = info.get("values", [])
            tabelle.setRowCount(2)
            tabelle.setColumnCount(len(x))
            tabelle.setVerticalHeaderLabels(["x", "Value"])
            tabelle.setHorizontalHeaderLabels([f"x{i + 1}" for i in range(len(x))])
            for spalte, stelle in enumerate(x):
                tabelle.setItem(0, spalte, QtWidgets.QTableWidgetItem(str(stelle)))
                wert = werte[spalte] if spalte < len(werte) else 0.0
                tabelle.setItem(1, spalte, QtWidgets.QTableWidgetItem(str(wert)))

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
        info["x_name"] = self.x_name.text().strip()

        if kind == "curve":
            paare = []
            for spalte in range(tabelle.columnCount()):
                x_item = tabelle.item(0, spalte)
                w_item = tabelle.item(1, spalte)
                paare.append((_zahl(x_item.text() if x_item else 0.0), _zahl(w_item.text() if w_item else 0.0)))

            # Die Interpolation braucht eine steigende Achse.
            paare.sort(key=lambda paar: paar[0])
            info["x"] = [paar[0] for paar in paare]
            info["values"] = [paar[1] for paar in paare]
            return

        info["y_unit"] = self.y_einheit.text().strip()
        info["y_name"] = self.y_name.text().strip()

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

    def titel_setzen(self):
        """Fenstertitel: Messdatei und ob etwas noch nicht uebernommen ist."""
        titel = "Parameter Manager"

        datei = getattr(self.file_widget, "file_name", None)
        if datei:
            titel = f"{titel} - {Path(str(datei)).name}"

        if self.offene_aenderungen():
            titel = f"{titel} *"

        self.setWindowTitle(titel)

    def offene_aenderungen(self):
        """Steht in der Tabelle etwas, das noch nicht uebernommen wurde?"""
        if not self._offen:
            return False

        self.werte_lesen()

        return self.parameters != self._original

    def wert_getippt(self, *args):
        """Eine Zelle wurde geaendert."""
        if not self._offen:
            return

        self.titel_setzen()

        if self.auto_box.isChecked():
            self._auto_timer.start()

    def apply(self, *args):
        """Werte uebernehmen - das Fenster bleibt offen."""
        self.werte_lesen()

        self.changed_names = {
            name
            for name in set(self.parameters) | set(self._original)
            if self.parameters.get(name) != self._original.get(name)
        }

        self.pressed_button = "apply"

        if self.file_widget is not None:
            self.file_widget.parameters = deepcopy(self.parameters)

        self._original = deepcopy(self.parameters)
        self.titel_setzen()

        self.applied.emit(set(self.changed_names))

        if self.changed_names and self.file_widget is not None:
            self.file_widget.recompute_parameter_channels(self.changed_names)

    def recompute(self, *args):
        """Alles neu rechnen, was Parameter benutzt - ohne Rueckfrage."""
        self.apply()

        if self.file_widget is not None:
            self.file_widget.recompute_parameter_channels(set(self.parameters))

    def cancel(self, *args):
        """Nur noch fuer alte Aufrufer: schliesst das Fenster."""
        self.close()


class ParameterImportDialog(QtWidgets.QDialog):
    """Auswahl, was aus einer DCM- oder CDFX-Datei uebernommen wird.

    Links die Haken, rechts was dahinter steckt - Art, Groesse,
    Einheit und ob der Name schon vergeben ist. Die Suche oben versteht
    * und ? als Platzhalter, wie im Explorer.
    """

    SPALTEN = ("Name", "Type", "Size", "Unit", "Comment", "Status")

    def __init__(self, gefunden, vorhandene=None, quelle="", parent=None):
        super().__init__(parent)

        self.gefunden = gefunden or {}
        self.vorhandene = set(vorhandene or ())
        self.auswahl = []
        self.pressed_button = "cancel"

        self.setWindowTitle(f"Import parameters - {quelle}" if quelle else "Import parameters")
        self.setSizeGripEnabled(True)
        self.setWindowFlags(QtCore.Qt.WindowType.Window)

        haupt = QtWidgets.QVBoxLayout(self)

        oben = QtWidgets.QHBoxLayout()
        oben.addWidget(QtWidgets.QLabel("Search"))
        self.suche = QtWidgets.QLineEdit()
        self.suche.setPlaceholderText("z. B. Idle*  oder  *Tar")
        self.suche.textChanged.connect(self.filtern)
        oben.addWidget(self.suche, 1)

        for text, art in (("Scalars", "scalar"), ("Curves", "curve"), ("Maps", "map")):
            box = QtWidgets.QCheckBox(text)
            box.setChecked(True)
            box.stateChanged.connect(self.filtern)
            setattr(self, f"box_{art}", box)
            oben.addWidget(box)

        haupt.addLayout(oben)

        self.tabelle = QtWidgets.QTableWidget()
        self.tabelle.setColumnCount(len(self.SPALTEN))
        self.tabelle.setHorizontalHeaderLabels(self.SPALTEN)
        self.tabelle.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.tabelle.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tabelle.verticalHeader().setVisible(False)
        haupt.addWidget(self.tabelle, 1)

        unten = QtWidgets.QHBoxLayout()

        for text, zustand, nur_sichtbar in (
            ("Check shown", True, True),
            ("Uncheck shown", False, True),
            ("Uncheck all", False, False),
        ):
            knopf = QtWidgets.QPushButton(text)
            knopf.setToolTip(
                "Gilt fuer die gerade gezeigten Zeilen" if nur_sichtbar else "Gilt fuer alle Zeilen"
            )
            knopf.clicked.connect(
                lambda _=False, wert=zustand, sichtbar=nur_sichtbar: self.alle_haken(wert, sichtbar)
            )
            unten.addWidget(knopf)

        self.anzahl_label = QtWidgets.QLabel("")
        unten.addWidget(self.anzahl_label)
        unten.addStretch(1)

        self.apply_btn = QtWidgets.QPushButton("Import")
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.apply_btn.clicked.connect(self.apply)
        self.cancel_btn.clicked.connect(self.cancel)
        unten.addWidget(self.apply_btn)
        unten.addWidget(self.cancel_btn)

        haupt.addLayout(unten)

        self.aufbauen()
        _merke_groesse(self, "parameter_import", (900, 600))

    # ------------------------------------------------------------ Tabelle

    def aufbauen(self):
        """Alle gefundenen Parameter in die Tabelle."""
        namen = sorted(self.gefunden)

        self.tabelle.setRowCount(len(namen))

        for zeile, name in enumerate(namen):
            info = self.gefunden[name]
            art = info.get("type", "scalar")

            if art == "map":
                groesse = f"{len(info.get('x', []))} x {len(info.get('y', []))}"
            elif art == "curve":
                groesse = str(len(info.get("x", [])))
            else:
                groesse = "1"

            ziel = name if is_valid_name(name) else safe_name(name)
            zustand = []
            if ziel != name:
                zustand.append(f"name -> {ziel}")
            if ziel in self.vorhandene:
                zustand.append("overwrites")

            erste = QtWidgets.QTableWidgetItem(name)
            erste.setFlags(erste.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            # Nichts vorausgewaehlt: man sucht, hakt an, sucht weiter.
            # Alles auf einmal gibt es ueber "Check shown" ohne Suche.
            erste.setCheckState(QtCore.Qt.CheckState.Unchecked)
            erste.setData(QtCore.Qt.ItemDataRole.UserRole, name)
            self.tabelle.setItem(zeile, 0, erste)

            for spalte, wert in enumerate(
                (art, groesse, info.get("unit", ""), info.get("comment", ""), ", ".join(zustand)), start=1
            ):
                self.tabelle.setItem(zeile, spalte, QtWidgets.QTableWidgetItem(str(wert)))

        self.tabelle.resizeColumnsToContents()
        self.tabelle.itemChanged.connect(self.zaehlen)
        self.filtern()

    def filtern(self, *args):
        """Zeilen zeigen, die zur Suche und zu den Arten passen."""
        text = self.suche.text().strip()
        muster = None

        if text:
            try:
                muster = re.compile(".*" + wildcard_pattern(text) + ".*", re.IGNORECASE)
            except re.error:
                muster = None

        arten = {
            art
            for art in ("scalar", "curve", "map")
            if getattr(self, f"box_{art}").isChecked()
        }

        sichtbar = 0

        for zeile in range(self.tabelle.rowCount()):
            name = self.tabelle.item(zeile, 0).text()
            art = self.tabelle.item(zeile, 1).text()

            passt = art in arten and (muster is None or muster.fullmatch(name) is not None)

            self.tabelle.setRowHidden(zeile, not passt)
            sichtbar += int(passt)

        self._sichtbar = sichtbar
        self.zaehlen()

    def zaehlen(self, *args):
        """Anzeigen, wie viele angehakt und wie viele gezeigt sind."""
        angehakt = sum(
            1
            for zeile in range(self.tabelle.rowCount())
            if self.tabelle.item(zeile, 0) is not None
            and self.tabelle.item(zeile, 0).checkState() == QtCore.Qt.CheckState.Checked
        )

        sichtbar = getattr(self, "_sichtbar", self.tabelle.rowCount())

        self.anzahl_label.setText(
            f"{angehakt} angehakt  |  {sichtbar} von {self.tabelle.rowCount()} gezeigt"
        )

    def alle_haken(self, zustand, nur_sichtbar=True):
        """Haken setzen oder loeschen - fuer die gezeigten oder fuer alle."""
        wert = QtCore.Qt.CheckState.Checked if zustand else QtCore.Qt.CheckState.Unchecked

        for zeile in range(self.tabelle.rowCount()):
            if nur_sichtbar and self.tabelle.isRowHidden(zeile):
                continue

            self.tabelle.item(zeile, 0).setCheckState(wert)

    # ------------------------------------------------------------ Schluss

    def apply(self, *args):
        # Alles, was angehakt ist - auch wenn es die Suche gerade nicht
        # zeigt. Sonst waere die Arbeit von vorher weg: suchen, anhaken,
        # anders suchen, anhaken, importieren ist der normale Weg.
        self.auswahl = [
            self.tabelle.item(zeile, 0).data(QtCore.Qt.ItemDataRole.UserRole)
            for zeile in range(self.tabelle.rowCount())
            if self.tabelle.item(zeile, 0).checkState() == QtCore.Qt.CheckState.Checked
        ]

        self.pressed_button = "apply"
        self.close()

    def cancel(self, *args):
        self.auswahl = []
        self.pressed_button = "cancel"
        self.close()
