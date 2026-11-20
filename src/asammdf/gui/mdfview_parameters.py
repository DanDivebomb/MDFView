"""Parameter: Skalare, Kennlinien und Kennfelder der Konfiguration.

Virtuelle Signale rechnen in asammdf mit dem Block "global variables":
Python-Code, der vor jeder Berechnung ausgefuehrt wird. Ein Kennfeld
liesse sich dort auch von Hand hinschreiben - nur ist Code zum
Bearbeiten von Zahlen unbequem, und eine Oberflaeche kann damit nicht
umgehen.

Darum liegen Parameter hier als Daten in der Konfiguration, und dieses
Modul macht daraus den Code, den die Berechnung braucht. Aus

    {"eta": {"type": "map", "x": [...], "y": [...], "values": [[...]]}}

entsteht ein Block, der PARAMETER anlegt und die drei Zugriffe P, KL
und KF bereitstellt. In einer Funktion schreibt man dann:

    def Eta(n=0, last=0, t=0):
        return KF("eta", n, last)

Form der Werte:
    Skalar     "values" ist eine Zahl
    Kennlinie  "x" sind die Stuetzstellen, "values" die Werte dazu
    Kennfeld   "x" steht quer (Spalten), "y" senkrecht (Zeilen),
               "values" ist also eine Liste von Zeilen: values[y][x].
               Genau so, wie die Tabelle in der Oberflaeche aussieht.

Zwischen den Stuetzstellen wird linear interpoliert, aussen wird der
Randwert gehalten (kein Extrapolieren) - wie in INCA und MDA.
"""

from keyword import iskeyword

TYPES = ("scalar", "curve", "map")

# Vorspann des erzeugten Blocks: die drei Zugriffsfunktionen. Sie
# stehen als Text hier, weil sie im Namensraum der Berechnung leben
# muessen und nicht in diesem Modul.
HELPERS = '''
def _hole(name):
    """Parameter heraussuchen - mit einer Meldung, die weiterhilft."""
    try:
        return PARAMETER[name]
    except KeyError:
        bekannt = ", ".join(sorted(PARAMETER)) or "keine"
        raise KeyError(
            f"Parameter '{name}' gibt es nicht. Im Parameter Manager (F8) anlegen. Vorhanden: {bekannt}"
        ) from None


def P(name):
    """Wert eines skalaren Parameters."""
    return _hole(name)["values"]


def _stelle(achse, wert):
    """Index links der Stelle und Anteil bis zum naechsten Punkt."""
    wert = np.clip(np.asarray(wert, dtype=float), achse[0], achse[-1])

    if len(achse) < 2:
        return np.zeros_like(wert, dtype=int), np.zeros_like(wert), 0

    index = np.clip(np.searchsorted(achse, wert) - 1, 0, len(achse) - 2)
    breite = achse[index + 1] - achse[index]
    breite = np.where(breite == 0, 1.0, breite)

    return index, (wert - achse[index]) / breite, 1


def KL(name, x):
    """Kennlinie an der Stelle x."""
    p = _hole(name)
    achse, werte = p["x"], p["values"]

    if len(achse) < 2:
        return np.zeros_like(np.asarray(x, dtype=float)) + werte[0]

    return np.interp(np.asarray(x, dtype=float), achse, werte)


def KF(name, x, y):
    """Kennfeld an der Stelle (x, y) - bilinear."""
    p = _hole(name)
    xa, ya, werte = p["x"], p["y"], p["values"]

    i, tx, nx = _stelle(xa, x)
    j, ty, ny = _stelle(ya, y)

    w00 = werte[j, i]
    w10 = werte[j, i + nx]
    w01 = werte[j + ny, i]
    w11 = werte[j + ny, i + nx]

    return (1 - tx) * (1 - ty) * w00 + tx * (1 - ty) * w10 + (1 - tx) * ty * w01 + tx * ty * w11
'''


def is_valid_name(name):
    """Taugt der Name als Python-Name und ist er nicht schon belegt?"""
    name = (name or "").strip()

    return bool(name) and name.isidentifier() and not iskeyword(name) and name not in ("P", "KL", "KF", "PARAMETER")


def new_parameter(kind):
    """Frischer Parameter mit brauchbaren Startwerten."""
    if kind == "scalar":
        return {"type": "scalar", "unit": "", "values": 0.0}

    if kind == "curve":
        return {"type": "curve", "unit": "", "x_unit": "", "x": [0.0, 1.0], "values": [0.0, 0.0]}

    return {
        "type": "map",
        "unit": "",
        "x_unit": "",
        "y_unit": "",
        "x": [0.0, 1.0],
        "y": [0.0, 1.0],
        "values": [[0.0, 0.0], [0.0, 0.0]],
    }


def _zahl(wert):
    try:
        return repr(float(wert))
    except (TypeError, ValueError):
        return "0.0"


def _liste(werte):
    return "[" + ", ".join(_zahl(wert) for wert in werte) + "]"


def parameters_to_code(parameters):
    """Aus den Parametern den Codeblock fuer die Berechnung machen.

    Der Block kommt auch ohne Parameter: P, KL und KF sind dann
    trotzdem da und sagen, welcher Parameter fehlt. Sonst scheitert
    eine Funktion mit KF an "name 'KF' is not defined" und man sucht
    an der falschen Stelle.
    """
    parameters = parameters or {}

    zeilen = [
        "# Parameter aus der Konfiguration - erzeugt, Aenderungen hier",
        "# werden beim naechsten Mal ueberschrieben.",
        "PARAMETER = {",
    ]

    skalare = []

    for name, info in parameters.items():
        if not is_valid_name(name):
            continue

        kind = info.get("type", "scalar")

        if kind == "scalar":
            wert = _zahl(info.get("values", 0.0))
            zeilen.append(f'    "{name}": {{"type": "scalar", "values": {wert}}},')
            skalare.append((name, wert))

        elif kind == "curve":
            zeilen.append(f'    "{name}": {{')
            zeilen.append('        "type": "curve",')
            zeilen.append(f'        "x": np.array({_liste(info.get("x", []))}),')
            zeilen.append(f'        "values": np.array({_liste(info.get("values", []))}),')
            zeilen.append("    },")

        else:
            reihen = ", ".join(_liste(reihe) for reihe in info.get("values", []))
            zeilen.append(f'    "{name}": {{')
            zeilen.append('        "type": "map",')
            zeilen.append(f'        "x": np.array({_liste(info.get("x", []))}),')
            zeilen.append(f'        "y": np.array({_liste(info.get("y", []))}),')
            zeilen.append(f"        \"values\": np.array([{reihen}]),")
            zeilen.append("    },")

    zeilen.append("}")

    if skalare:
        zeilen.append("")
        zeilen.append("# Skalare zusaetzlich als einfache Variable - damit sie auch")
        zeilen.append("# ohne P() und in der Zeitverschiebung zu haben sind.")
        zeilen.extend(f"{name} = {wert}" for name, wert in skalare)

    return "\n".join(zeilen) + "\n" + HELPERS


def used_in(definition, names):
    """Kommt einer dieser Parameternamen im Funktionstext vor?

    Grob, aber billig: es geht nur darum, beim Neurechnen die Signale
    zu ueberspringen, die den Parameter sicher nicht benutzen.
    """
    if not definition:
        return False

    return any(name and name in definition for name in names)
