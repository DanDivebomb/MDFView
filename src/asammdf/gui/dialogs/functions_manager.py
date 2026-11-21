from copy import deepcopy
import os

from PySide6 import QtCore, QtWidgets

from ..dialogs.messagebox import MessageBox
from ..mdfview_geometry import remember_geometry
from ..widgets.functions_manager import FunctionsManager


class FunctionsManagerDialog(QtWidgets.QDialog):
    # Definitionen uebernommen - das Fenster bleibt offen
    applied = QtCore.Signal()

    def __init__(
        self,
        definitions,
        channels=None,
        selected_definition="",
        prefix="",
        global_variables="",
        extra_globals="",
        *args,
        **kwargs,
    ):
        definitions = definitions or {}
        definitions = {
            name: {
                "definition": definition,
                "uuid": os.urandom(6).hex(),
            }
            for name, definition in definitions.items()
        }

        self.original_definitions = {}
        self.modified_definitions = {}
        self.global_variables = ""

        for name, info in definitions.items():
            self.original_definitions[info["uuid"]] = {
                "name": name,
                "definition": info["definition"],
            }

        super().__init__(*args, **kwargs)

        self.setObjectName("FunctionsManagerDialog")
        self.resize(404, 294)
        self.setSizeGripEnabled(True)
        self.setWindowFlags(QtCore.Qt.WindowType.Window)
        self.verticalLayout = QtWidgets.QVBoxLayout(self)
        self.widget = FunctionsManager(
            deepcopy(definitions),
            channels,
            selected_definition,
            global_variables,
            extra_globals=extra_globals,
        )

        self.verticalLayout.addWidget(self.widget)

        self.horLayout = QtWidgets.QHBoxLayout()

        spacer = QtWidgets.QSpacerItem(
            40, 20, QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Minimum
        )
        self.apply_btn = QtWidgets.QPushButton("Apply")
        # Kein Abbrechen mehr: das Fenster bleibt offen, es kann also
        # schon uebernommen worden sein - da gibt es nichts
        # zurueckzunehmen.
        self.cancel_btn = QtWidgets.QPushButton("Close")
        self.horLayout.addSpacerItem(spacer)
        self.horLayout.addWidget(self.apply_btn)
        self.horLayout.addWidget(self.cancel_btn)

        self.verticalLayout.addLayout(self.horLayout)

        self.apply_btn.clicked.connect(self.apply)
        self.cancel_btn.clicked.connect(self.cancel)
        self.pressed_button = "cancel"

        if prefix:
            self.setWindowTitle(f"{prefix} - Functions Manager")
        else:
            self.setWindowTitle("Functions Manager")

        # Groesse vom letzten Mal - 404 x 294 ist fuer einen
        # Code-Editor zu klein.
        remember_geometry(self, "functions_manager", (1150, 800))

    def apply(self, *args):
        self.pressed_button = "apply"
        self.modified_definitions = {}

        self.widget.refresh_definitions()

        for name, info in self.widget.definitions.items():
            if info["current_definition"] != info["definition"]:
                result = MessageBox.question(
                    self, "Unsaved function definitions", "Do you want to review the functions before exporting them?"
                )
                if result == MessageBox.StandardButton.No:
                    break
                else:
                    for row in range(self.widget.functions_list.count()):
                        item = self.widget.functions_list.item(row)
                        if item.text() == name:
                            self.widget.functions_list.setCurrentRow(row)
                            self.widget.functions_list.scrollToItem(item)
                            break
                    return

        for name, info in self.widget.definitions.items():
            self.modified_definitions[info["uuid"]] = {
                "name": name,
                "definition": info["current_definition"],
            }

        self.global_variables = "\n".join(
            line for line in self.widget.globals_definition.toPlainText().splitlines() if line
        )

        # Das Fenster bleibt offen - wer zuhoert, uebernimmt jetzt.
        self.applied.emit()

    def mark_applied(self):
        """Nach dem Uebernehmen gilt der jetzige Stand als der alte.

        Sonst vergleicht das naechste Apply wieder gegen den Stand
        beim Oeffnen - und wuerde eine Umbenennung ein zweites Mal
        ausfuehren wollen. Beim Umbenennen loescht update_functions
        den alten Namen, also waere die Funktion danach weg.
        """
        self.original_definitions = deepcopy(self.modified_definitions)

        for info in self.widget.definitions.values():
            info["definition"] = info["current_definition"]

        item = self.widget.functions_list.currentItem()
        self.widget.refresh_functions_list(selected_definition=item.text() if item else "")

    def cancel(self, *args):
        self.pressed_button = "cancel"
        self.close()
