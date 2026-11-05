"""The animation plan: what Claude may choose (a CLOSED catalogue of scenes, the React code is in montage/remotion),
how its answer is checked, and the props given to Remotion.

Claude never gives a time: each scene names the INDEX of the word that triggers it, and the time is the real
timestamp of that word (no invented timing). Every text is cleaned (no control character, no em dash, no
instruction aimed at an AI) and shortened; anything outside the catalogue is dropped.
"""

import difflib
import math
import re
from typing import Any

from ultron_hub.esprit import savoir
from ultron_hub.montage.tools import MontageError

FPS = 30
WIDTH, HEIGHT = 1080, 1920
# Keep in sync with montage/remotion/src/scenes.jsx (CATALOGUE) and the task's schema (a test checks both).
SCENE_TYPES = ("titre", "mot_cle", "phrase", "chiffre", "liste", "emotion", "formes", "image", "icone", "photo",
               "schema", "comparaison")
EMOTIONS = ("joie", "surprise", "colere", "peur", "tristesse", "fierte", "reflexion", "idee", "aucune")
STYLES = ("doux", "fort")
# A style is a whole universe (background, palette, font, captions), todo #434. montage/remotion/src/themes.js
THEMES = ("noir", "canevas", "papier")
DEFAULT_THEME = "noir"
LEGACY_THEMES = {"nuit": "noir", "neon": "noir"}   # plans written before #434 still render
# End of the video (todo #438): a launch option, never chosen by Claude. montage/remotion/src/Fin.jsx draws the cards.
FINS = ("abonne", "suite", "rien")
DEFAULT_FIN = "abonne"
LEGACY_FIN = "rien"       # a plan written before #438 has no `fin`: it ends as it always did
FIN_LABELS = {"abonne": "Carte « abonne-toi »", "suite": "Carte « la suite »", "rien": "Fondu sans carte"}
FIN_S = 2.5               # length of the end card, after the voice (the only copy: passed to Remotion in the props)
FADE_S = 0.6              # « rien »: the picture fades to the background of the style for this long
THEME_LABELS = {"noir": "Noir mot à mot", "canevas": "Canevas + pastille", "papier": "Papier + mot surligné"}
TYPE_LABELS = {"titre": "titre qui apparaît", "mot_cle": "mot clé qui claque", "phrase": "phrase mot à mot",
               "chiffre": "chiffre qui défile", "liste": "liste qui s'affiche", "emotion": "émotion animée",
               "formes": "formes qui jaillissent", "image": "photo qui apparaît",
               "icone": "icône qui rebondit", "photo": "photo libre", "schema": "schéma à flèches",
               "comparaison": "comparaison en deux colonnes"}
EMOTION_LABELS = {"joie": "joie", "surprise": "surprise", "colere": "colère", "peur": "peur", "tristesse": "tristesse",
                  "fierte": "fierté", "reflexion": "réflexion", "idee": "idée", "aucune": "aucune"}
MIN_SCENES = 3
MAX_SCENES = 14
MIN_GAP_S = 1.0         # two scenes never start closer than this
LEAD_S = 0.1            # a scene appears just before its word
TEXT_MAX = 48
POINT_MAX = 32
MAX_POINTS = 4          # a list or a schema shows 4 points at most (todo #436)
TITLE_MAX = 60
VISUEL_MAX = 40
CAPTION_MAX = 80        # a photo's caption given by the user (todo #76)
MAX_PHOTOS = 5
SCENE_KEYS = ("mot", "type", "text", "points", "emotion", "style", "visuel", "src", "credit", "mots")
STOCK_QUERY_RE = re.compile(r"[a-z]+(?: [a-z]+){0,4}")   # todo #78: a few English words, nothing else
# Closed list of pictograms (todo #77): French name chosen by Claude -> Lucide icon (lucide-react, ISC licence,
# pinned in montage/remotion/package.json). A test checks every name exists in the installed package.
ICONS = {
    # argent, commerce
    "argent": "Banknote", "euro": "Euro", "pieces": "Coins", "portefeuille": "Wallet", "carte_bancaire": "CreditCard",
    "tirelire": "PiggyBank", "prix": "Tag", "promo": "Percent", "panier": "ShoppingCart", "sac_achat": "ShoppingBag",
    "boutique": "Store", "facture": "Receipt", "calculatrice": "Calculator", "hausse": "TrendingUp",
    "baisse": "TrendingDown", "graphique": "ChartColumn", "camembert": "ChartPie", "cadeau": "Gift",
    # colis, transport
    "colis": "Package", "colis_ouvert": "PackageOpen", "camion": "Truck", "entrepot": "Warehouse", "avion": "Plane",
    "bateau": "Ship", "voiture": "Car", "train": "TrainFront", "metro": "TramFront", "bus": "Bus", "velo": "Bike",
    "carburant": "Fuel", "carte": "Map", "lieu": "MapPin", "itineraire": "Route", "globe": "Globe",
    "boussole": "Compass", "fusee": "Rocket",
    # lavage, maison, outils
    "goutte": "Droplet", "gouttes": "Droplets", "eau": "WavesHorizontal", "brillance": "Sparkles", "savon": "SprayCan",
    "aspirateur": "Wind", "maison": "House", "cle": "Key", "outil": "Wrench", "marteau": "Hammer",
    "reglages": "Settings", "usine": "Factory", "chantier": "Construction", "ampoule": "Lightbulb",
    "batterie": "BatteryFull", "eclair": "Zap", "prise": "Plug",
    # temps
    "horloge": "Clock", "chrono": "Timer", "sablier": "Hourglass", "calendrier": "Calendar", "alarme": "AlarmClock",
    "soleil": "Sun", "lune": "Moon", "nuage": "Cloud", "pluie": "CloudRain", "neige": "Snowflake",
    # personnes, échanges
    "personne": "User", "groupe": "Users", "client": "UserCheck", "poignee_de_main": "Handshake", "message": "MessageCircle",
    "telephone": "Phone", "mail": "Mail", "envoyer": "Send", "notification": "Bell", "pouce": "ThumbsUp",
    "pouce_bas": "ThumbsDown", "coeur": "Heart", "etoile": "Star", "sourire": "FaceGrinning", "ecouter": "Headphones",
    "micro": "Mic", "camera": "Camera", "video": "Video", "musique": "Music",
    # travail, savoir
    "ordinateur": "Laptop", "ecran": "Monitor", "smartphone": "Smartphone", "code": "Code", "terminal": "Terminal",
    "serveur": "Server", "base_de_donnees": "Database", "nuage_info": "CloudUpload", "robot": "Bot",
    "cerveau": "Brain", "puce": "Cpu", "wifi": "Wifi", "lien": "Link", "recherche": "Search", "dossier": "Folder",
    "document": "FileText", "livre": "BookOpen", "diplome": "GraduationCap", "crayon": "Pencil",
    "presse_papiers": "ClipboardList", "liste": "ListChecks", "valide": "CircleCheck", "refuse": "CircleX",
    "attention": "TriangleAlert", "question": "CircleQuestionMark", "info": "Info", "idee": "Lightbulb",
    "cible": "Target", "drapeau": "Flag", "trophee": "Trophy", "medaille": "Medal", "couronne": "Crown",
    "diamant": "Gem", "cadenas": "Lock", "bouclier": "Shield", "oeil": "Eye", "loupe": "ZoomIn",
    # corps, sport, vie
    "muscle": "Dumbbell", "course": "Footprints", "sante": "HeartPulse", "pomme": "Apple", "cafe": "Coffee",
    "repas": "Utensils", "lit": "BedDouble", "feu": "Flame", "plante": "Sprout", "arbre": "TreePine",
    "montagne": "Mountain", "valise": "Luggage", "langue": "Languages", "priere": "HandHeart", "mosquee": "MoonStar",
    # direction, mouvement
    "fleche_haut": "ArrowUp", "fleche_bas": "ArrowDown", "fleche_droite": "ArrowRight", "recommencer": "RefreshCw",
    "plus": "Plus", "moins": "Minus", "lecture": "Play", "pause": "Pause", "stop": "Square",
}
POINT_TYPES = ("liste", "schema", "comparaison")   # the scenes that show their `points` one by one
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_DIGIT_RE = re.compile(r"\d")


def clean(value: Any, limit: int) -> str:
    """Text written by Claude from an untrusted voice: one line, no em dash, no instruction aimed at an AI."""
    text = " ".join(_CTRL_RE.sub(" ", str(value or "")).replace("—", ",").replace("–", ",").split())
    kept, _ = savoir.filter_instructions(text.replace(" ,", ","))
    text = " ".join(kept.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip(" ,;:") + "…"


def stamp(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


def words_text(words: list[dict[str, Any]]) -> str:
    """The cleaned transcript, as the user reads it."""
    return " ".join(w["w"] for w in words)


def icon_names() -> str:
    """The closed list given to Claude (names only, comma separated)."""
    return ", ".join(ICONS)


def metrics(words: list[dict[str, Any]], duration: float, photos: list[dict[str, Any]] | None = None,
            stock: bool = False) -> dict:
    """What the task montage-plan receives in {metrics}: numbered words with their time (untrusted data), the
    user's photos (id and caption, also untrusted) when he sent some, and whether free photos can be searched."""
    return {"duree_s": round(duration, 1),
            "mots": "\n".join(f"{i} [{w['s']:.2f}] {w['w']}" for i, w in enumerate(words)),
            "photos": photo_list(photos), "photos_libres": stock}


def photo_list(photos: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    return [{"id": p["id"], "legende": p.get("legende") or "sans légende"} for p in photos or []]


def photo_sources(photos: list[dict[str, Any]] | None) -> dict[str, str]:
    """id -> file under the montage folder (« images/p1.jpg »), for the photos of images.json."""
    return {p["id"]: p["file"] for p in photos or [] if isinstance(p, dict) and "id" in p and "file" in p}


def bare(scene: dict[str, Any]) -> dict[str, Any]:
    """The choices of a scene without its timing (plans written before todo #76 have no visuel nor src)."""
    defaults = {"points": [], "mots": [], "emotion": "aucune", "style": "doux", "visuel": "", "src": "", "text": "",
                "credit": ""}
    return {k: scene.get(k, defaults.get(k)) for k in SCENE_KEYS}


def mots_list(value: Any) -> list[int]:
    """The word indexes Claude gave for the points of a scene (checked later against the scene's window)."""
    if not isinstance(value, list):
        return []
    return [i for i in value[:MAX_POINTS] if isinstance(i, int) and not isinstance(i, bool) and i >= 0]


def build_scene(raw: Any, words: list[dict[str, Any]], sources: dict[str, str] | None = None) -> dict | None:
    """One scene from Claude's answer, checked against the catalogue; None when unusable. `sources` = the photos
    this montage has (id -> file): an `image` scene must name one of them."""
    if not isinstance(raw, dict) or raw.get("type") not in SCENE_TYPES:
        return None
    index = raw.get("mot")
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(words):
        return None
    kind = raw["type"]
    text = clean(raw.get("texte"), TEXT_MAX)
    points = [p for p in (clean(x, POINT_MAX) for x in (raw.get("points") or [])[:MAX_POINTS]) if p] if isinstance(
        raw.get("points"), list) else []
    emotion = raw.get("emotion") if raw.get("emotion") in EMOTIONS else "aucune"
    if kind == "chiffre" and not _DIGIT_RE.search(text):
        kind = "mot_cle"
    if kind == "liste" and not points:
        kind = "phrase"
    if kind == "schema" and len(points) < 2:
        kind = "phrase"
    if kind == "comparaison" and len(points) != 2:
        kind = "phrase"
    if kind == "emotion" and emotion == "aucune":
        kind = "mot_cle"
    visuel = raw.get("visuel") if isinstance(raw.get("visuel"), str) else ""
    if kind == "image" and visuel not in (sources or {}):
        kind = "mot_cle"
    if kind == "icone" and visuel not in ICONS:
        kind = "mot_cle"
    visuel = visuel.strip().lower() if kind == "photo" else visuel
    if kind == "photo" and (len(visuel) > VISUEL_MAX or not STOCK_QUERY_RE.fullmatch(visuel)):
        kind = "mot_cle"
    if not text and kind not in ("emotion", "image", "icone", "photo"):
        return None
    shown = kind in POINT_TYPES
    return {"mot": index, "type": kind, "text": text, "points": points if shown else [],
            "mots": mots_list(raw.get("mots")) if shown else [],
            "emotion": emotion if kind == "emotion" else "aucune",
            "style": raw.get("style") if raw.get("style") in STYLES else "doux",
            "visuel": visuel if kind in ("image", "icone", "photo") else "",
            "src": (sources or {}).get(visuel, "") if kind == "image" else "", "credit": ""}


def time_scenes(scenes: list[dict[str, Any]], words: list[dict[str, Any]], duration: float) -> list[dict[str, Any]]:
    """Sorts the scenes by their word, drops those too close to the previous one, gives each its start (the word's
    timestamp) and its end (the start of the next scene, the end of the voice for the last: nothing stays empty).
    `mots` keeps only the words said during the scene, increasing, one per point (todo #436)."""
    timed: list[dict[str, Any]] = []
    for scene in sorted(scenes, key=lambda s: s["mot"]):
        start = max(0.0, words[scene["mot"]]["s"] - LEAD_S)
        if timed and start - timed[-1]["from"] < MIN_GAP_S:
            continue
        if duration - start < 0.5:
            continue
        timed.append({**scene, "from": round(start, 2), "word": words[scene["mot"]]["w"]})
        if len(timed) >= MAX_SCENES:
            break
    for n, scene in enumerate(timed):
        scene["to"] = round(min(timed[n + 1]["from"] if n + 1 < len(timed) else duration, duration), 2)
        high = timed[n + 1]["mot"] if n + 1 < len(timed) else len(words)
        mots = scene.get("mots") or []
        ok = (len(mots) == len(scene["points"]) and all(scene["mot"] <= i < high for i in mots)
              and all(a < b for a, b in zip(mots, mots[1:])))
        scene["mots"] = mots if ok else []
    return timed


def place_photos(timed: list[dict[str, Any]], sources: dict[str, str]) -> list[dict[str, Any]]:
    """Every photo of the user appears: a photo used twice gives its second place to a forgotten one, then each
    photo still missing takes the place of the longest scene that is neither the first one nor a photo (same moment,
    its text kept as caption). Nothing moves in time."""
    used = [s["visuel"] for s in timed if s["type"] == "image"]
    missing = [p for p in sources if p not in used]
    seen: set[str] = set()
    for scene in timed:
        if scene["type"] != "image":
            continue
        if scene["visuel"] in seen and missing:
            scene.update(visuel=missing[0], src=sources[missing.pop(0)])
        seen.add(scene["visuel"])
    for photo_id in missing:
        free = [s for s in timed[1:] if s["type"] not in ("image", "photo")]
        if not free:
            break
        target = max(free, key=lambda s: s["to"] - s["from"])
        target.update(type="image", visuel=photo_id, src=sources[photo_id], points=[], mots=[], emotion="aucune",
                      text=target["text"] if target["type"] in ("mot_cle", "phrase", "titre") else "")
    return timed


def normalized(plan_data: dict[str, Any]) -> dict[str, Any]:
    """The plan read from disk, with an old theme name replaced by its current style."""
    out = dict(plan_data)
    if out.get("theme") not in THEMES:
        out["theme"] = LEGACY_THEMES.get(out.get("theme"), DEFAULT_THEME)
    if out.get("fin") not in FINS:
        out["fin"] = LEGACY_FIN
    return out


def fin_seconds(fin: str) -> float:
    """What the end of the video adds after the voice."""
    return FIN_S if fin in ("abonne", "suite") else FADE_S


def total_duration(plan_data: dict[str, Any]) -> float:
    """Length of the rendered video: the voice plus its end (card or fade)."""
    return round(float(plan_data["duration"]) + fin_seconds(plan_data["fin"]), 2)


def build_plan(answer: Any, words: list[dict[str, Any]], duration: float, theme: str = DEFAULT_THEME,
               photos: list[dict[str, Any]] | None = None, fin: str = DEFAULT_FIN) -> dict:
    """Claude's answer -> the checked plan, or MontageError (fewer than MIN_SCENES usable scenes)."""
    if not isinstance(answer, dict):
        raise MontageError("Le plan d'animation est illisible.")
    sources = photo_sources(photos)
    scenes = [s for s in (build_scene(r, words, sources) for r in (answer.get("scenes") or [])
                          if isinstance(answer.get("scenes"), list)) if s]
    timed = time_scenes(scenes, words, duration)
    if len(timed) < MIN_SCENES:
        raise MontageError("Le plan d'animation proposé est inutilisable (moins de 3 scènes valables). Réessaie.")
    timed = place_photos(timed, sources)
    title = clean(answer.get("titre"), TITLE_MAX) or clean(words_text(words), TITLE_MAX)
    return {"title": title, "theme": theme if theme in THEMES else DEFAULT_THEME,
            "fin": fin if fin in FINS else DEFAULT_FIN, "duration": round(duration, 2),
            "scenes": timed}


def scene_line(scene: dict[str, Any]) -> str:
    """« 0:03 « Ultron » → titre qui apparaît : Ultron » (the preview shown before ✅)."""
    what = TYPE_LABELS.get(scene["type"], scene["type"])
    detail = scene["text"]
    if scene["type"] in POINT_TYPES and scene["points"]:
        detail = f"{scene['text']} ({' / '.join(scene['points'])})" if scene["text"] else " / ".join(scene["points"])
    if scene["type"] == "emotion":
        label = EMOTION_LABELS.get(scene["emotion"], scene["emotion"])
        detail = f"{label}{', ' + scene['text'] if scene['text'] else ''}"
    if scene["type"] == "image":
        number = scene.get("visuel", "").lstrip("p")
        detail = f"ta photo {number}{', ' + scene['text'] if scene['text'] else ''}"
    if scene["type"] == "photo":
        credit = f" (Pexels, {scene['credit']})" if scene.get("credit") else ""
        detail = f"« {scene.get('visuel', '')} »{credit}{', ' + scene['text'] if scene['text'] else ''}"
    if scene["type"] == "icone":
        detail = f"{scene.get('visuel', '').replace('_', ' ')}{', ' + scene['text'] if scene['text'] else ''}"
    return f"{stamp(scene['from'])} « {scene['word']} » → {what} : {detail}"


def frames(duration: float) -> int:
    return max(FPS, math.ceil(duration * FPS))


def beats(words: list[dict[str, Any]], scene: dict[str, Any]) -> list[int]:
    """Frames, relative to the start of the scene, of the words said during it (each one may move the scene)."""
    start = round(scene["from"] * FPS)
    return [max(0, round(w["s"] * FPS) - start) for w in words if scene["from"] <= w["s"] < scene["to"]]


def reveals(words: list[dict[str, Any]], scene: dict[str, Any]) -> list[int]:
    """Frames, relative to the start of the scene, when each point appears: on its word when Claude gave one that
    holds, else evenly spread over the scene."""
    count = len(scene["points"])
    start = round(scene["from"] * FPS)
    if scene.get("mots") and len(scene["mots"]) == count:
        return [max(0, round(words[i]["s"] * FPS) - start) for i in scene["mots"]]
    length = max(1, round(scene["to"] * FPS) - start)
    first, last = min(8, length // 4), round(length * 0.75)
    return [first + round((last - first) * k / max(1, count - 1)) for k in range(count)]


def props(plan: dict[str, Any], words: list[dict[str, Any]], audio: str = "voice.wav") -> dict[str, Any]:
    """Input props of the Remotion composition « Montage » (montage/remotion/src/Root.jsx)."""
    return {"durationInFrames": frames(total_duration(plan)), "audio": audio, "theme": plan["theme"],
            "fin": plan["fin"], "voiceS": plan["duration"], "finS": fin_seconds(plan["fin"]),
            "title": plan["title"], "words": [{"w": w["w"], "s": w["s"], "e": w["e"]} for w in words],
            "scenes": [{"type": s["type"], "text": s["text"], "points": s["points"], "emotion": s["emotion"],
                        "style": s["style"], "src": s.get("src", ""),
                        "icon": ICONS.get(s.get("visuel", ""), "") if s["type"] == "icone" else "",
                        "from": s["from"], "to": s["to"], "beats": beats(words, s), "reveals": reveals(words, s)}
                       for s in plan["scenes"]]}


# --- ✏️ Corriger le texte (todo #74) -------------------------------------------------------------

FIX_MAX_CHARS = 2000
FIX_MIN_RATIO = 0.3     # below this the text is not the same speech: refused
_NORM_RE = re.compile(r"[\W_]+")


def _norm(word: str) -> str:
    return _NORM_RE.sub("", word.casefold())


def _spread(tokens: list[str], start: float, end: float) -> list[dict[str, Any]]:
    """Tokens sharing [start, end] evenly (a replaced or inserted passage keeps its place in the voice)."""
    step = max(0.0, end - start) / len(tokens)
    return [{"w": t, "s": round(start + i * step, 2), "e": round(start + (i + 1) * step, 2)}
            for i, t in enumerate(tokens)]


def realign(words: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """The user's corrected text on the timestamps of the voice: unchanged words keep their time, changed or added
    ones share the time of the passage they replace. MontageError when the text is not the same speech."""
    clean_text = " ".join(_CTRL_RE.sub(" ", text or "").split())
    if not clean_text:
        raise MontageError("Le texte corrigé est vide.")
    if len(clean_text) > FIX_MAX_CHARS:
        raise MontageError(f"Texte trop long ({len(clean_text)} caractères) : {FIX_MAX_CHARS} au maximum.")
    tokens = [t[:40] for t in clean_text.split()]
    matcher = difflib.SequenceMatcher(a=[_norm(w["w"]) for w in words], b=[_norm(t) for t in tokens],
                                      autojunk=False)
    if matcher.ratio() < FIX_MIN_RATIO:
        raise MontageError("Le texte corrigé est trop différent de la voix : corrige seulement les mots mal compris.")
    out: list[dict[str, Any]] = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            out += [{**words[i], "w": tokens[j]} for i, j in zip(range(i1, i2), range(j1, j2))]
        elif op == "replace":
            out += _spread(tokens[j1:j2], words[i1]["s"], words[i2 - 1]["e"])
        elif op == "insert":
            start = words[i1 - 1]["e"] if i1 > 0 else 0.0
            end = words[i1]["s"] if i1 < len(words) else start + 0.3 * (j2 - j1)
            out += _spread(tokens[j1:j2], start, max(start, end))
    return out
