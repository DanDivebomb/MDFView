"""Parameter aus DCM- und CDFX-Dateien lesen.

Beides sind die ueblichen Austauschformate fuer Applikationsdaten:
DCM ist das Textformat von ETAS/INCA, CDFX das XML-Format nach ASAM
CDF. Gelesen werden Festwerte, Kennlinien und Kennfelder - also genau
das, was hier als Parameter gespeichert werden kann. Alles andere
(Texte, Funktionsgruppen, Variantenkodierung) wird uebersprungen.

Was herauskommt, hat dieselbe Form wie der Schluessel "parameters" in
der Konfiguration, ergaenzt um "comment" und "source":

    {"IdleSpdTar": {"type": "curve", "unit": "rpm", "x_unit": "degC",
                    "x": [...], "values": [...],
                    "comment": "Solldrehzahl Leerlauf",
                    "source": "DCM"}}

Reihenfolge der Kennfeldwerte: eine Zeile je y-Stuetzstelle, in jeder
Zeile die Werte ueber x - genau so, wie die Tabelle im Parameter
Manager aussieht und wie es die DCM-Datei vorgibt.
"""

from pathlib import Path
import re
from xml.etree import ElementTree

# Bloecke, die hierher passen, mit ihrer Art
DCM_KINDS = {
    "FESTWERT": "scalar",
    "FESTWERTEBLOCK": "block",
    "KENNLINIE": "curve",
    "FESTKENNLINIE": "curve",
    "GRUPPENKENNLINIE": "curve",
    "KENNFELD": "map",
    "FESTKENNFELD": "map",
    "GRUPPENKENNFELD": "map",
}

# Bloecke, die es gibt, hier aber nichts zu suchen haben
DCM_SKIP = ("TEXTSTRING", "TEXT", "FUNKTIONEN", "VARIANTENKODIERUNG", "STUETZSTELLENVERTEILUNG")

ZAHL = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")

# CDFX-Kategorien, die nur eine Achse beschreiben
AXIS_CATEGORIES = ("COM_AXIS", "CURVE_AXIS", "RES_AXIS", "FIX_AXIS", "STD_AXIS")


def _zahlen(text):
    """Alle Zahlen einer Zeile."""
    return [float(treffer) for treffer in ZAHL.findall(text or "")]


def _text_in_anfuehrung(zeile):
    treffer = re.search(r'"(.*)"', zeile)

    return treffer.group(1) if treffer else zeile.split(None, 1)[-1].strip()


def read_dcm(inhalt):
    """Parameter aus dem Text einer DCM-Datei."""
    gefunden = {}

    block = None

    for rohzeile in inhalt.splitlines():
        zeile = rohzeile.strip()

        if zeile.startswith("*") and block is not None:
            # *SSTX und *SSTY nennen die Achsen einer Gruppenkennlinie
            # oder eines Gruppenkennfelds. Sonst ist * ein Kommentar.
            teile = zeile.lstrip("*").split()
            if len(teile) >= 2 and teile[0].upper() in ("SSTX", "SSTY"):
                block["x_name" if teile[0].upper() == "SSTX" else "y_name"] = teile[1].strip('"')
            continue

        if not zeile or zeile.startswith("*") or zeile.startswith("!"):
            continue

        if block is None:
            kopf = zeile.split()
            art = DCM_KINDS.get(kopf[0])

            if art is None:
                continue

            if len(kopf) < 2:
                continue

            block = {
                "art": art,
                "name": kopf[1].strip('"'),
                # Zahlen hinter dem Namen: bei Kennfeldern nx und ny
                "groessen": [int(float(teil)) for teil in kopf[2:] if ZAHL.fullmatch(teil)],
                "unit": "",
                "x_unit": "",
                "y_unit": "",
                "comment": "",
                "x_name": "",
                "y_name": "",
                "x": [],
                "y": [],
                "werte": [],
                "zeilen": [],
            }
            continue

        if zeile == "END":
            fertig = _dcm_fertig(block)
            if fertig is not None:
                gefunden[block["name"]] = fertig

            block = None
            continue

        schluessel = zeile.split(None, 1)[0]

        if schluessel == "LANGNAME":
            block["comment"] = _text_in_anfuehrung(zeile)
        elif schluessel == "EINHEIT_W":
            block["unit"] = _text_in_anfuehrung(zeile)
        elif schluessel == "EINHEIT_X":
            block["x_unit"] = _text_in_anfuehrung(zeile)
        elif schluessel == "EINHEIT_Y":
            block["y_unit"] = _text_in_anfuehrung(zeile)
        elif schluessel in ("ST/X", "ST_X"):
            block["x"].extend(_zahlen(zeile))
        elif schluessel in ("ST/Y", "ST_Y"):
            block["y"].extend(_zahlen(zeile))
            # Ab hier gehoeren die folgenden WERT-Zeilen zu dieser
            # y-Stuetzstelle.
            block["zeilen"].append([])
        elif schluessel == "WERT":
            werte = _zahlen(zeile)
            if block["zeilen"]:
                block["zeilen"][-1].extend(werte)
            else:
                block["werte"].extend(werte)

    return gefunden


def _dcm_fertig(block):
    """Einen gelesenen DCM-Block in einen Parameter umsetzen."""
    art = block["art"]

    if art == "scalar":
        werte = block["werte"] or (block["zeilen"][0] if block["zeilen"] else [])
        if not werte:
            return None

        return {
            "type": "scalar",
            "unit": block["unit"],
            "values": werte[0],
            "comment": block["comment"],
            "source": "DCM",
        }

    if art == "block":
        # Werteblock ohne Achse: als Kennlinie ueber dem Index
        werte = block["werte"] or (block["zeilen"][0] if block["zeilen"] else [])
        if not werte:
            return None

        return {
            "type": "curve",
            "unit": block["unit"],
            "x_unit": block["x_unit"] or "index",
            "x": [float(i) for i in range(len(werte))],
            "values": werte,
            "comment": block["comment"],
            "source": "DCM",
        }

    if art == "curve":
        werte = block["werte"] or (block["zeilen"][0] if block["zeilen"] else [])
        x = block["x"]

        if not werte:
            return None

        if not x:
            x = [float(i) for i in range(len(werte))]

        laenge = min(len(x), len(werte))

        return {
            "type": "curve",
            "unit": block["unit"],
            "x_unit": block["x_unit"],
            "x_name": block["x_name"],
            "x": x[:laenge],
            "values": werte[:laenge],
            "comment": block["comment"],
            "source": "DCM",
        }

    # Kennfeld. Es gibt zwei Schreibweisen:
    #   a) je y-Stuetzstelle ein ST/Y mit den WERT-Zeilen darunter
    #   b) ein ST/Y mit allen Stuetzstellen, danach alle Werte am Stueck
    # Dazu kommen Dateien ganz ohne ST/Y, bei denen nur die Zahlen im
    # Kopf (KENNFELD name nx ny) sagen, wie breit und hoch es ist.
    zeilen = [reihe for reihe in block["zeilen"] if reihe]
    y = list(block["y"])
    x = list(block["x"])
    groessen = block.get("groessen") or []

    alle = list(block["werte"])
    for reihe in zeilen:
        alle.extend(reihe)

    if not alle:
        return None

    breite = len(x) or (groessen[0] if groessen else 0)
    hoehe = len(y) or (len(groessen) > 1 and groessen[1]) or 0

    # Fall a: so viele Zeilen wie y-Stuetzstellen, jede so breit wie x
    sauber = bool(zeilen) and len(zeilen) == max(hoehe, len(zeilen)) and breite and all(
        len(reihe) == breite for reihe in zeilen
    )

    if sauber:
        fertige = zeilen
    else:
        if not breite:
            breite = len(alle) // hoehe if hoehe else len(alle)

        if not breite:
            return None

        fertige = [alle[start : start + breite] for start in range(0, len(alle), breite)]
        fertige = [reihe for reihe in fertige if len(reihe) == breite]

    if not fertige:
        return None

    if not x:
        x = [float(i) for i in range(breite)]

    # y-Stuetzstellen auffuellen oder kuerzen, damit beides zusammenpasst
    if len(y) < len(fertige):
        y = y + [float(i) for i in range(len(y), len(fertige))]
    elif len(y) > len(fertige):
        y = y[: len(fertige)]

    return {
        "type": "map",
        "unit": block["unit"],
        "x_unit": block["x_unit"],
        "y_unit": block["y_unit"],
        "x_name": block["x_name"],
        "y_name": block["y_name"],
        "x": x[:breite],
        "y": y,
        "values": fertige,
        "comment": block["comment"],
        "source": "DCM",
    }


def _ohne_namensraum(tag):
    return tag.split("}")[-1].upper() if "}" in tag else tag.upper()


def _finde(element, name):
    """Erstes Kind mit diesem Namen, egal welcher Namensraum."""
    for kind in element:
        if _ohne_namensraum(kind.tag) == name:
            return kind

    return None


def _alle(element, name):
    return [kind for kind in element if _ohne_namensraum(kind.tag) == name]


def _werte_von(behaelter):
    """Zahlen aus einem SW-VALUES-PHYS-Block."""
    if behaelter is None:
        return []

    werte = []

    for kind in behaelter.iter():
        if _ohne_namensraum(kind.tag) in ("V", "VF"):
            werte.extend(_zahlen(kind.text))

    return werte


def _einheit_von(element):
    if element is None:
        return ""

    for kind in element.iter():
        if _ohne_namensraum(kind.tag) in ("UNIT-DISPLAY-NAME", "SW-UNIT-REF", "UNIT-REF"):
            if kind.text and kind.text.strip():
                return kind.text.strip()

    return ""


def _kategorie(element):
    kind = _finde(element, "CATEGORY")

    return (kind.text or "").strip().upper() if kind is not None and kind.text else ""


def _werte_mit_zeilen(behaelter):
    """Werte eines SW-VALUES-PHYS, nach Moeglichkeit zeilenweise.

    Kennfelder stehen dort oft in <VG>-Gruppen - eine Gruppe je Zeile.
    Dann ist die Aufteilung schon vorgegeben und muss nicht geraten
    werden.
    """
    if behaelter is None:
        return [], []

    gruppen = [kind for kind in behaelter if _ohne_namensraum(kind.tag) == "VG"]

    if gruppen:
        zeilen = [_werte_von(gruppe) for gruppe in gruppen]
        zeilen = [reihe for reihe in zeilen if reihe]

        return [wert for reihe in zeilen for wert in reihe], zeilen

    return _werte_von(behaelter), []


def _verweis(element):
    """Auf welchen anderen Eintrag zeigt dieser Achsenbehaelter?"""
    for kind in element.iter():
        name = _ohne_namensraum(kind.tag)

        if name in ("SW-INSTANCE-REF", "SW-AXIS-INSTANCE-REF", "INSTANCE-REF"):
            if kind.text and kind.text.strip():
                return kind.text.strip()

            for wert in kind.attrib.values():
                if wert and wert.strip():
                    return wert.strip()

            for enkel in kind.iter():
                if enkel is kind:
                    continue
                if enkel.text and enkel.text.strip():
                    return enkel.text.strip()

        if name in ("SHORT-LABEL", "SW-INSTANCE-NAME") and kind.text and kind.text.strip():
            return kind.text.strip()

    return ""


def _achs_index(element):
    kind = _finde(element, "SW-AXIS-INDEX")

    try:
        return int((kind.text or "").strip())
    except (AttributeError, TypeError, ValueError):
        return 0


def read_cdfx(inhalt):
    """Parameter aus dem Text einer CDFX-Datei (ASAM CDF, XML).

    Achsen koennen auf zwei Arten dastehen: mit ihren Werten direkt im
    Kennfeld, oder als eigener Eintrag, auf den das Kennfeld nur
    verweist (COM_AXIS, gemeinsame Achse). Darum wird erst alles
    eingelesen und danach zusammengesetzt.
    """
    wurzel = ElementTree.fromstring(inhalt)

    rohdaten = {}
    reihenfolge = []

    for element in wurzel.iter():
        if _ohne_namensraum(element.tag) != "SW-INSTANCE":
            continue

        name_element = _finde(element, "SHORT-NAME")
        if name_element is None or not (name_element.text or "").strip():
            continue

        name = name_element.text.strip()

        behaelter = _finde(element, "SW-VALUE-CONT")
        if behaelter is None:
            continue

        werte, zeilen = _werte_mit_zeilen(_finde(behaelter, "SW-VALUES-PHYS"))
        if not werte:
            continue

        kommentar = ""
        langname = _finde(element, "LONG-NAME")
        if langname is not None:
            for kind in langname.iter():
                if kind.text and kind.text.strip():
                    kommentar = kind.text.strip()
                    break

        achsen = []
        achsbehaelter = _finde(element, "SW-AXIS-CONTS")
        if achsbehaelter is not None:
            for achse in _alle(achsbehaelter, "SW-AXIS-CONT"):
                achswerte, _ = _werte_mit_zeilen(_finde(achse, "SW-VALUES-PHYS"))

                achsen.append(
                    {
                        "index": _achs_index(achse) or len(achsen) + 1,
                        "werte": achswerte,
                        "unit": _einheit_von(achse),
                        "verweis": "" if achswerte else _verweis(achse),
                    }
                )

        achsen.sort(key=lambda achse: achse["index"])

        rohdaten[name] = {
            "kategorie": _kategorie(element),
            "werte": werte,
            "zeilen": zeilen,
            "groessen": [int(zahl) for zahl in _werte_von(_finde(behaelter, "SW-ARRAYSIZE"))],
            "unit": _einheit_von(behaelter),
            "comment": kommentar,
            "achsen": achsen,
        }
        reihenfolge.append(name)

    gefunden = {}

    for name in reihenfolge:
        roh = rohdaten[name]

        # Achsen stehen in CDFX-Dateien als eigene Eintraege, auf die
        # Kennlinien und Kennfelder verweisen. Als Parameter taugen sie
        # nicht - sie haetten nur eine Zaehlachse. Zum Aufloesen der
        # Verweise sind sie oben trotzdem eingelesen.
        if roh["kategorie"] in AXIS_CATEGORIES:
            continue

        fertig = _cdfx_fertig(name, roh, rohdaten)

        if fertig is not None:
            gefunden[name] = fertig

    return gefunden


def _cdfx_fertig(name, roh, rohdaten):
    """Einen gelesenen CDFX-Eintrag in einen Parameter umsetzen."""
    werte = roh["werte"]
    groessen = [zahl for zahl in roh["groessen"] if zahl > 0]
    kategorie = roh["kategorie"]

    gemeinsam = {"unit": roh["unit"], "comment": roh["comment"], "source": "CDFX"}

    # Achsen aufloesen: eigene Werte oder die des Eintrags, auf den
    # verwiesen wird.
    achsen = []
    for achse in roh["achsen"]:
        achswerte = achse["werte"]
        einheit = achse["unit"]

        if not achswerte and achse["verweis"]:
            quelle = rohdaten.get(achse["verweis"])
            if quelle is not None:
                achswerte = quelle["werte"]
                einheit = einheit or quelle["unit"]

        if achswerte:
            achsen.append((achswerte, einheit, achse["verweis"]))

    # Welche Art? Die Kategorie weiss es am besten, sonst die Form.
    if kategorie in ("MAP", "CUBOID") or len(groessen) >= 2 or len(achsen) >= 2:
        art = "map"
    elif kategorie in ("CURVE",) or len(achsen) == 1:
        art = "curve"
    elif kategorie in ("VALUE", "BOOLEAN", "DEPENDENT_VALUE") or len(werte) == 1:
        art = "scalar"
    else:
        art = "curve" if len(werte) > 1 else "scalar"

    if art == "scalar":
        return {"type": "scalar", "values": werte[0], **gemeinsam}

    if art == "curve":
        if achsen:
            x, x_einheit, x_name = achsen[0]
        else:
            x, x_einheit, x_name = [float(i) for i in range(len(werte))], "index", ""

        laenge = min(len(x), len(werte))
        if laenge < 2:
            return {"type": "scalar", "values": werte[0], **gemeinsam}

        return {
            "type": "curve",
            "x_unit": x_einheit,
            "x_name": x_name,
            "x": x[:laenge],
            "values": werte[:laenge],
            **gemeinsam,
        }

    # Kennfeld
    if len(achsen) >= 2:
        x, x_einheit, x_name = achsen[0]
        y, y_einheit, y_name = achsen[1]
    elif len(achsen) == 1 and len(groessen) >= 2:
        x, x_einheit, x_name = achsen[0]
        y, y_einheit, y_name = [float(i) for i in range(groessen[1])], "index", ""
    elif len(groessen) >= 2:
        x, x_einheit, x_name = [float(i) for i in range(groessen[0])], "index", ""
        y, y_einheit, y_name = [float(i) for i in range(groessen[1])], "index", ""
    else:
        return None

    breite = len(x)
    if not breite:
        return None

    zeilen = roh["zeilen"]

    if not zeilen or any(len(reihe) != breite for reihe in zeilen):
        zeilen = [werte[start : start + breite] for start in range(0, len(werte), breite)]

    zeilen = [reihe for reihe in zeilen if len(reihe) == breite]

    if not zeilen:
        return None

    if len(y) < len(zeilen):
        y = list(y) + [float(i) for i in range(len(y), len(zeilen))]
    elif len(y) > len(zeilen):
        y = list(y)[: len(zeilen)]

    return {
        "type": "map",
        "x_unit": x_einheit,
        "y_unit": y_einheit,
        "x_name": x_name,
        "y_name": y_name,
        "x": list(x),
        "y": list(y),
        "values": zeilen,
        **gemeinsam,
    }


def read_parameter_file(pfad):
    """Parameter aus einer DCM- oder CDFX-Datei lesen.

    Welches Format es ist, entscheidet die Endung; sagt die nichts,
    entscheidet der Anfang der Datei.
    """
    pfad = Path(pfad)
    rohdaten = pfad.read_bytes()

    for kodierung in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            inhalt = rohdaten.decode(kodierung)
            break
        except UnicodeDecodeError:
            continue
    else:
        inhalt = rohdaten.decode("latin-1", errors="replace")

    endung = pfad.suffix.lower()

    if endung == ".dcm":
        return read_dcm(inhalt)

    if endung in (".cdfx", ".cdf", ".xml"):
        return read_cdfx(inhalt)

    if inhalt.lstrip().startswith("<"):
        return read_cdfx(inhalt)

    return read_dcm(inhalt)
