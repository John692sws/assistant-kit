"""The one catalogue of the dashboard's actions (todo #251): every function is ONE entry, never a route written by hand.

An entry declares once: its id, the `ultron-hub` argv (a tuple, never a shell string), its fields (type, `fullmatch`
regex, limits), its kind (`lecture` | `simple` | `signee`), the confirmation sheet (Quoi / Sur quoi / Effet / Risque, the
words of Telegram's `ui.approval_card`) and, for `signee`, the action of `closing.DASHBOARD_ACTIONS` used for the proof.
actions.py validates against it, signs and runs; app.js draws the forms and the sheet from `public()` (no secret, no regex).

`ouvert` (todo #283, the Pilote of a session, decision of the user on 20/10): the action runs without a sheet, through the
same POST as a reading, on an authorized device (whois + signed cookie as always); a `signee` one is still signed by the
server for that one send and every send is journaled (length, never the text). Only low-risk actions may be `ouvert`.

Templates: `{champ}` is a field's value, `{champ_libelle}` the label of a choice, `{empreinte}` = sha256(stdin)[:16],
`{longueur}` = the number of characters of stdin (a trace that must not hold the text itself).
A template part (argv element, piece of Quoi / Sur quoi / journal) naming an EMPTY optional field is dropped whole
(`--notes={notes}` disappears without notes). An argv element `@champ` is replaced by the argv of the chosen option.
`verifier()` runs at import: an ill-formed entry stops the dashboard from starting (and fails the tests).
"""

from __future__ import annotations

import dataclasses
import itertools
import re
import string
from dataclasses import dataclass
from typing import Any, Mapping

from ultron_hub import autorisation, habitudes, notifs, recus, routines
from ultron_hub.chantier.core import DOMAINES
from ultron_hub.compte import connexion as compte_connexion
from ultron_hub.escouade import core as escouade_core
from ultron_hub.escouade import mcp_ultron
from ultron_hub.esprit import focus as esprit_focus
from ultron_hub.esprit.savoir import FINGERPRINT_LEN
from ultron_hub.esprit.store import FACT_MAX
from ultron_hub.health import reflexes
from ultron_hub.montage import plan, tts
from ultron_hub.sessions import envoi as session_envoi
from ultron_hub.sessions import gestion as session_gestion
from ultron_hub.sessions.journal import NAME_RE as SESSION_NAME_RE
from ultron_hub.todo import closing
from ultron_hub.todo.store import CLAIR_MAX

TYPES = ("lecture", "simple", "signee")
RISQUES = {"low": "🟢 faible", "high": "🔴 élevé"}  # icon + label shown on the approval card
CHAMP_TYPES = ("contexte", "texte", "choix")  # contexte: given by the page (id of the row), never typed
NETTOIE = ("bords", "espaces", "aucun")
RUN_TIMEOUT_S = 90  # systemd-run for the session and for the watchdog (session start and watchdog)
MERGE_TIMEOUT_S = 420  # trial merge + test suite (300 s) + git, as the former plugin merge flow
AFTER_TIMEOUT_S = 700  # ✅ Exécuter after a merge: above ultron-user's 660 s
LOT_MAX = 6  # 🔀 Tout fusionner from the dashboard: 6 todos at most (one request, LOT_TIMEOUT_S); ➖ Retirer the others
REFLEX_TIMEOUT_S = 720  # above ultron-user's own limit, as the plugin's health_flow.REFLEX_TIMEOUT_S
SANTE_TIMEOUT_S = 150  # above tasks/sante-serveur timeout_s, as health_flow.RUN_TIMEOUT_S
LOT_TIMEOUT_S = 3600  # LOT_MAX merges (420 s each at worst) and one launched action (960 s), as the plugin computes it
DONNEES_MAX = 200_000  # characters of JSON given back by a reading: above = refused whole, never cut (what is signed is shown)
LONG_TIMEOUT_S = 45 * 60  # the longest command of Telegram's flows (video analyse, video_flow.ANALYSE_TIMEOUT_S)
VIDEO_TOUT_S = 17 * 60 + LONG_TIMEOUT_S  # `video analyse-lien`: collect (17 min) then analyse (45 min), todo #335
ANSWER_MAX = 2000  # maximum length of a free text (the CLI checks again)
TITLE_MAX = 200
NOTES_MAX = 2000
CONFIRMER = "✅ Confirmer"
ID_RE = re.compile(r"[a-z][a-z0-9-]{0,39}")
TODO_ID_RE = re.compile(r"[1-9][0-9]{0,8}")
TODO_IDS_RE = re.compile(r"[0-9]{1,9}(?:,[0-9]{1,9}){0,99}")  # branch_lot.IDS_RE (`--sans`)
LOT_IDS_RE = re.compile(r"[1-9][0-9]{0,8}(?:,[1-9][0-9]{0,8}){0,%d}" % (LOT_MAX - 1))
RANGER_EMPREINTE_RE = re.compile(r"[0-9a-f]{8}")  # service.rattachement_fingerprint
EMPREINTE_RE = re.compile(r"[0-9a-f]{16}")  # branch_lot.EMPREINTE_RE
NOTIF_AT_RE = notifs.AT_RE  # the date of a notification of the fil (todo #301)
N_RE = re.compile(r"[1-9][0-9]{0,2}")
SUIVI_RE = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{8}")  # fond.JOB_RE
FUSION_CIBLE_RE = re.compile(r"[1-9][0-9]{0,8}-[0-9a-f]{12}")  # auto_merge.target: "<todo id>-<sha12>"  # number of an after-merge action (after_merge: 1, 2, …)
CATEGORIE_RE = re.compile(r"[a-z0-9à-ÿ][a-z0-9à-ÿ_-]{0,39}")  # settings todo.categories (the CLI checks the list)
DATE_RE = re.compile(r"[0-9]{1,2}/[0-9]{1,2}(?:/[0-9]{4})?|[0-9]{4}-[0-9]{2}-[0-9]{2}")  # service.parse_due
LIEN_RE = re.compile(r"(?i:https?)://[^\s\x00-\x1f\x7f]+")  # a link from outside = data: http(s), one piece, no control character
LIEN_MAX = 500  # video_flow.MAX_URL_CHARS (the CLI checks the host again: video.links.is_allowed)
LOT_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")  # id of a video, a documents batch, a montage
SORTIES = ("id", "texte", "apres_fusion", "donnees", "duree", "empreinte")
class Refused(Exception):
    """Nothing was run; the message is shown to the user."""


class CatalogueError(ValueError):
    """An entry of the catalogue is ill-formed (checked at import)."""


@dataclass(frozen=True)
class Choix:
    code: str
    libelle: str
    argv: tuple[str, ...] = ()  # spliced where the action's argv says `@champ`


@dataclass(frozen=True)
class Champ:
    nom: str
    type: str  # CHAMP_TYPES
    libelle: str  # placeholder or label in the page; `{champ}` is filled from the row's context
    exemple: str  # a valid value: the tests run every action with it
    refus: str  # message shown when the value is refused
    regex: re.Pattern[str] | None = None  # fullmatch, after cleaning
    min: int = 1  # 0 = optional (an empty value is accepted and drops the template parts naming it)
    max: int = 40
    choix: tuple[Choix, ...] = ()
    liste: str = ""  # choix only: the options come from the page's data (e.g. "lancables"), checked by `regex`
    defaut: str = ""
    nettoie: str = "bords"
    ligne: bool = False  # texte on one line
    fixe_selon: tuple[str, Mapping[str, str]] | None = None  # (other field, its value -> fixed value of this one)
    rapides: tuple[Choix, ...] = ()  # texte only: quick values sent as is (code = text sent, libelle = button label)
    suggestions: tuple[str, ...] = ()  # texte only: completions offered while typing (todo #283: the /commands)

    def valeur(self, raw: Any) -> str:
        if raw is None or raw == "":
            raw = self.defaut
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            raise Refused(self.refus)
        text = str(raw)
        if self.nettoie == "espaces":
            text = " ".join(text.split())
        elif self.nettoie == "bords":
            text = text.strip()
        return text

    def verifie(self, text: str) -> str:
        if not text:
            if self.min:
                raise Refused(self.refus)
            return text
        if len(text) < self.min or len(text) > self.max:
            raise Refused(self.refus)
        if self.choix and text not in {c.code for c in self.choix}:
            raise Refused(self.refus)
        if self.regex is not None and self.regex.fullmatch(text) is None:
            raise Refused(self.refus)
        return text


@dataclass(frozen=True)
class Action:
    id: str
    bouton: str  # the page's button
    type: str  # TYPES
    argv: tuple[str, ...]  # `ultron-hub` arguments
    champs: tuple[Champ, ...]
    quoi: tuple[str, ...]  # sheet: parts joined
    sur: tuple[str, ...]
    effet: str
    risque: str  # RISQUES
    journal: tuple[str, ...]  # « … depuis le dashboard (by=…) » is added
    signe: str | None = None  # signee: the action of closing.DASHBOARD_ACTIONS
    cible: str = ""  # signee: the signed target (template)
    stdin: str = ""  # the field sent on stdin (free text never goes through argv)
    delai: int = RUN_TIMEOUT_S
    sortie: tuple[str, ...] = ()  # SORTIES given back to the page
    confirmer: str = CONFIRMER
    danger: bool = False
    attente: str = ""  # shown while a long command runs
    livre_echec: str = ""  # headline of the message `envoi` delivers when the action FAILS (the page may be closed by then)
    livre: str = ""  # headline of the message `envoi` delivers at the end (CLI data text + MEDIA:<data.path>), as Telegram's flows
    donnees: Mapping[str, Any] | None = None  # sortie "donnees": the whitelist of the CLI's `data` given to the page
    groupe: bool = False  # the page may chain it over several rows picked in a list (🧹 Ménage): one sheet, one proof per row
    fond: bool = False  # run in its own unit (fond.py, todo #267): the answer is a job the page polls with `suivi`
    ouvert_long: bool = False  # todo #379: an `ouvert` entry allowed up to JOINDRE_TIMEOUT_S (see verifier: only `--vocal-seul`)
    ouvert: bool = False  # todo #283 « toujours ouvert »: no sheet (POST /api/action/lire), still signed (signee) and journaled
    detail: str = ""  # todo #417: a reading run again by `prepare`: its `actions[].texte` are listed on the sheet, and its
    #                   `empreinte` must equal the field `empreinte` (what the sheet lists is exactly what gets signed);
    #                   todo #429: the reading may take fields of the action (same names), and give `lignes` (a text, as is)

    def champ(self, nom: str) -> Champ:
        return next(c for c in self.champs if c.nom == nom)


def _ctx(nom: str, exemple: str, regex: re.Pattern[str], refus: str, min_: int = 1) -> Champ:
    return Champ(nom, "contexte", nom, exemple, refus, regex=regex, min=min_, max=96)


_TODO_ID = _ctx("id", "7", TODO_ID_RE, "numéro de todo refusé")
_HABITUDE_DOSSIER = _ctx("dossier", "2026-10-25-ultron-retex", habitudes.DOSSIER_RE, "brouillon d'habitude refusé")
_ROUTINE_NOM = _ctx("nom", "veille-fba", routines.NOM_RE, "nom de routine refusé")


def _champs(noms: str, **sous: Any) -> dict[str, Any]:
    """A whitelist of `data` (sortie "donnees"): each name kept as a plain value, `sous` = a nested whitelist."""
    return {**{n: True for n in noms.split()}, **sous}


# What the page may read of each command's `data` (projeter): plain values, nested lists and objects, nothing else.
TACHE = _champs("id title priority category status due_date nature clair chantier overdue today badges")
BOARD = _champs("filter category open fond overdue today high branches", categories=[True], tasks=[TACHE])
FICHE_TODO = _champs("id title notes priority category status due_date source created_at done_at nature clair chantier",
                     history=[_champs("at action", details=_champs("from to by", **{"*cles": True}))],
                     branch=_champs("branch ahead"))
BRANCHE = _champs("todo_id branch ahead unique", commits=[True], branches=[True])
RANGER = _champs("empreinte chantiers", lines=[_champs("id titre de vers")])
MENAGE = _champs("n empreinte cree_a par bloquee", cancel=[_champs("id titre raison refus")],
                 archiver=[_champs("sujet refus")],
                 corbeille=[_champs("nom raison refus")],
                 dossiers=[_champs("chemin raison refus")])  # todo #362/#474: menage_lot.py
RAPPORTS = _champs("empreinte", actions=[_champs("type texte")],  # todo #417: rapports.py
                   cap=_champs("date resume", corrections=[_champs("titre chantier todo")]),
                   menage=_champs("date resume empreinte n bloquee"),
                   sante=_champs("date resume", alertes=[_champs("cle texte")], pannes=[_champs("nom diagnostic detail cible")]))
HABITUDE = _champs("dossier nom mode resume texte empreinte complet trop_long", lignes=[True])  # todo #429: habitudes.py
HABITUDES = {"habitudes": [HABITUDE]}
BILAN_SOIR = _champs("date total fusions clotures bascules faits gestes_evites",  # todo #530: cap/bilan_soir.py
                     lignes=[_champs("id_evenement heure todo_id titre sorte sha by annulee retour")],
                     remises=[_champs("compte heure texte reel reserve_avant reserve_apres apprentissage")])  # todo #534
ROUTINE = _champs("nom phrase cadence but plafond chantier cree_le statut empreinte prochain active_le",  # todo #503: routines.py
                  critere_fin=[True], lignes=[True])
ROUTINES = {"proposees": [ROUTINE], "actives": [ROUTINE]}
BANC = _champs("texte", passes=[_champs("date reussis sur cout note")])  # todo #513: banc.py
LOT = _champs("empreinte", ids=[True], ecartees=[_champs("todo_id title raison")], lot=[_champs(
    "todo_id title ahead files actions_error", apres=[True], repos=[_champs("repo branch label ahead files")],
    actions=[_champs("n action reason plan error status", args=[True], command=[True])])])
APRES = _champs("todo_id", requests=[_champs("n status line text")])
SUIVI = _champs("suivi action fini depuis_s", etapes=[_champs("nom etat detail")],
                resultat=_champs("ok resume erreur texte apres_fusion"))
FUSION_AUTO = _champs("cible annulee", fusion=_champs("todo_id at by", repos=[_champs("repo branch sha")]))
_BRANCHE_ETAT = _champs("todo_id title", branches=[_champs("repo branch label ahead", commit=_champs("sha date subject"),
                                                           worktree=_champs("path propre"))])
# todo #495: the advance authorisation of automatic merges (autorisation.etat)
AUTORISATION = _champs("valide jusqu_au by perimetre", jours=[True], fins=[_champs("jours jusqu_au")])
ETAT_FUSIONS = _champs("", en_cours=[_BRANCHE_ETAT], auto=[_champs("todo_id title at cible sha annulee a_valider")],
                       attente=[{**_BRANCHE_ETAT, "raisons": [_champs("step reason", files=[True])]}],
                       annulation=_champs("cible annulee"), autorisation=AUTORISATION)
_FENETRE = _champs("percent resets_at")
FAITS = {"tasks": [_champs("id title done_at")]}  # todo #270: « Fait aujourd'hui » of the briefing
COMPTES = _champs("actif comment alerte autre actif_adresse autre_adresse dose_texte", derniere_bascule=_champs("de vers a verifie"),
                  a_reconnecter=[_champs("nom raison adresse")], comptes=[_champs(
    "nom enregistre actif adresse enregistre_a quota_erreur quota_attente dose", quota=_champs("stale", session=_FENETRE, week=_FENETRE))])
_BUDGET_COMPTE = _champs("nom adresse connu veille lu_a pourcent restant remise_a_zero heures_restantes jours_restants "
                         "budget_jour depense_aujourdhui remise_passee dose")
BUDGET = _champs("limite conseil dose_texte", comptes=[_BUDGET_COMPTE], total=_champs("restant budget_jour depense_aujourdhui"))
PLANNING = _champs("jour seuil seuil_source pause pause_a texte", case=_champs("jour reserve_a reserve_b seuil a"),  # todo #534
                   comptes=[_champs("nom dose reserve auto reserve_pct source depense auto_permise auto_epuisee fin_rapide "
                                    "apprentissage jours_complets texte")])
PCT_RE = re.compile(r"[0-9]{1,3}")
FILTRES = {"open": "📋 Ouvertes (en tête)", "urgent": "⚠️ En retard + aujourd'hui",
           "closed": "✅ Terminées / annulées récemment", "fond": "🌱 En fond"}
PRIORITES = {"high": "🔴 Haute", "normal": "🟠 Normale", "low": "🟡 Basse"}
ECHEANCES = {"today": "Aujourd'hui", "tomorrow": "Demain", "week": "Cette semaine", "none": "Aucune"}
_CATEGORIE = Champ("categorie", "choix", "Catégorie", "perso", "catégorie refusée", regex=CATEGORIE_RE, liste="categories")
_N = _ctx("n", "1", N_RE, "numéro d'action refusé")
# Health (todo #247): a diagnosis gives the page its text and its button's label and target, never the ultron-user action.
REFLEXES = _champs("", diagnostics=[_champs("nom diagnostic action detail", bouton=_champs("label cible"))])
GARDE = _champs("ok problem enabled_in_config plugin_file")
BILAN = _champs("level summary recommendations", points=[_champs("level label detail")])


def _lecture(id_: str, bouton: str, argv: tuple[str, ...], champs: tuple[Champ, ...], donnees: Mapping[str, Any]) -> Action:
    """A reading: no sheet (the sheet words are only there for verifier()), never signed, never in the journal."""
    return Action(id_, bouton, "lecture", argv, champs, (bouton,), ("lecture seule",), "rien ne change", "low", (bouton,),
                  sortie=("donnees",), donnees=donnees)


def _edition(id_: str, bouton: str, option: str, champ: Champ | None, quoi: str, sur: str, journal: str) -> Action:
    """✏️ One field of `todo edit` (simple: a sheet, not signed, as Telegram's buttons)."""
    champs = (_TODO_ID,) + ((champ,) if champ else ())
    return Action(id_, bouton, "simple", ("todo", "edit", "{id}", *option.split()), champs, (quoi,), (sur,),
                  "la fiche de la todo change (tracé dans son historique)", "low", (journal,))

# -- 📥 Analyser (todo #248): video, documents, montage; the links and texts are DATA, never instructions ---------------------------
# Long commands (collect, analyse, render) wait for their `delai` like Telegram's flows (server threaded, `attente` shown).
_LIEN = Champ("lien", "texte", "Lien (https://…)", "https://www.youtube.com/watch?v=abcdefghijk",
              f"lien refusé : http(s) seulement, {LIEN_MAX} caractères au plus, sans espace", regex=LIEN_RE, max=LIEN_MAX,
              ligne=True)
_LOT = _ctx("id", "20261019T120000Z-0a1b2c3d", LOT_ID_RE, "identifiant refusé")
_REPONSE = Champ("texte", "texte", "Ta réponse", "garde l'idée du titre", f"réponse vide ou trop longue ({ANSWER_MAX} caractères au plus)",
                 max=ANSWER_MAX)
# 💡 Actions (video, docs): the proposals, so that a fiche can be checked then filed (todo #249); the rest stays on Telegram.
PROPOSITIONS = _champs("id title reponse", actions=[_champs("n type titre resume")])
_STYLE = Champ("style", "choix", "Style", "noir", "style inconnu", max=16, defaut=plan.DEFAULT_THEME,
               choix=tuple(Choix(t, plan.THEME_LABELS[t]) for t in plan.THEMES))
_FIN = Champ("fin", "choix", "Fin de la vidéo", "abonne", "fin inconnue", max=16, defaut=plan.DEFAULT_FIN,
             choix=tuple(Choix(f, plan.FIN_LABELS[f]) for f in plan.FINS))
MONTAGE_TOUT_S = 25 * 60  # voice or Piper + Whisper + plan (Claude) + Remotion render (17 min), as one background unit
JOINT = _champs("fichier genre chemin taille pour_session")  # todo #293: what the page gets of `joindre analyser`
JOINDRE_TIMEOUT_S = 25 * 60  # sound track (ffmpeg) + docs present (6 min) + one transcription (12 min), as Telegram
# todo #440: the optional sentence sent with a video (stdin, 500 characters at most; data for the analysis, never an instruction)
_PHRASE = Champ("phrase", "texte", "Ta phrase (facultatif)", "pour le chantier vidéo", "phrase trop longue (500 caractères au plus)",
                min=0, max=500)
ANALYSER: tuple[Action, ...] = (
    Action(  # todo #293 « 📎 joindre »: the file was stored by POST /api/fichier (nothing run); this analyses it
        "joindre", "📎 Analyser le fichier…", "simple", ("joindre", "analyser", "{fichier}"),
        (_ctx("fichier", "20261020T101500-a1b2c3-capture.png", recus.RECU_RE, "fichier reçu refusé"),
         _ctx("session", "ultron-19-10-15h38", SESSION_NAME_RE, "nom de session refusé", min_=0)),
        ("📎 Analyser le fichier joint",), ("{fichier}", " · puis écrit dans la session {session}"),
        "photo : gardée sur le serveur et montrée dans le Fil ; PDF, texte, audio, vidéo : lus ou transcrits puis résumés "
        "par Claude (Sonnet, sans outil), résultat dans le Fil", "low",
        ("📎 fichier joint analysé : {fichier}",), delai=JOINDRE_TIMEOUT_S, sortie=("id", "texte", "donnees"), donnees=JOINT,
        attente="⏳ Je lis ton fichier (une photo : tout de suite ; un audio ou une vidéo : quelques minutes)…"),
    Action(  # todo #379: the 🎙 recorder, 2 taps: same command, no sheet; the CLI refuses anything not marked vocal (--vocal-seul)
        "joindre-vocal", "🎙 Analyser le vocal", "simple", ("joindre", "analyser", "{fichier}", "--vocal-seul"),
        (_ctx("fichier", "20261020T101500-a1b2c3-vocal.m4a", recus.RECU_RE, "fichier reçu refusé"),
         _ctx("session", "ultron-19-10-15h38", SESSION_NAME_RE, "nom de session refusé", min_=0)),
        ("🎙 Analyser le vocal enregistré",), ("{fichier}", " · puis écrit dans la session {session}"),
        "seule ta parole enregistrée (marquée vocale par le serveur) est transcrite puis écrite dans la session ; tout autre fichier est refusé",
        "low", ("🎙 vocal analysé : {fichier}",), delai=JOINDRE_TIMEOUT_S, sortie=("id", "texte", "donnees"), donnees=JOINT,
        ouvert=True, ouvert_long=True),
    Action(  # A25: the duration before any download; a reading, no sheet
        "video-duree", "⏱ Durée de la vidéo", "lecture", ("video", "probe", "{lien}"), (_LIEN,),
        ("⏱ Lire la durée de la vidéo",), ("{lien}",), "rien n'est téléchargé : seule la durée est lue", "low",
        ("⏱ durée lue : {lien}",), sortie=("duree",)),
    Action(  # todo #335: one button for a link; fond=True: its own unit, it goes on if the page is closed or the service restarts
        "video-tout", "🔬 Analyser une vidéo…", "simple",
        ("video", "analyse-lien", "{lien}", "--session={session}", "--phrase-stdin"), (_LIEN, _ctx("session", "ultron-19-10-15h38", SESSION_NAME_RE, "nom de session refusé", min_=0), _PHRASE),
        ("🔬 Télécharger puis analyser la vidéo (Opus), d'un coup",), ("{lien}", " · depuis la session {session}"),
        "la vidéo est téléchargée, transcrite puis lue en entier par Opus ; tu es prévenu à la fin (ou en cas d'échec), "
        "même si tu fermes la page", "low",
        ("🔬 vidéo analysée d'un coup : {lien}",), delai=VIDEO_TOUT_S, sortie=("texte", "donnees"), donnees=PROPOSITIONS,
        fond=True, stdin="phrase",
        livre="🔬 Analyse de la vidéo prête", livre_echec="❌ L'analyse de la vidéo a échoué",
        attente="⏳ Téléchargement puis lecture complète (10 à 30 minutes) ; tu seras prévenu à la fin…"),
    Action(
        "video-collecte", "📥 Collecter la vidéo…", "simple",
        ("video", "collect", "{lien}", "--session={session}", "--phrase-stdin"), (_LIEN, _ctx("session", "ultron-19-10-15h38", SESSION_NAME_RE, "nom de session refusé", min_=0), _PHRASE),
        ("📥 Télécharger et préparer la vidéo",), ("{lien}", " · depuis la session {session}"),
        "la vidéo est téléchargée, transcrite, ses images et son son extraits ; puis 🔬 Analyser", "low",
        ("📥 vidéo collectée : {lien}",), delai=17 * 60, sortie=("id",), stdin="phrase",
        attente="⏳ Téléchargement, transcription, images et son (quelques minutes)…"),
    Action(
        "video-analyse", "🔬 Analyser la vidéo…", "simple", ("video", "analyse", "{id}"), (_LOT,),
        ("🔬 Analyse complète (Opus), selon ce que tu cherches",), ("vidéo {id}",),
        "Opus lit toute la vidéo, devine ce que tu cherches et propose quoi ranger (aucune action n'est faite) ; le "
        "compte rendu est écrit dans le dossier de la vidéo", "low",
        ("🔬 vidéo {id} analysée",), delai=LONG_TIMEOUT_S, sortie=("texte", "donnees"), donnees=PROPOSITIONS,
        livre="🔬 Analyse de la vidéo prête",
        attente="⏳ Lecture complète par Opus (quelques minutes, parfois plus)…"),
    Action(  # todo #437 (A27): one gesture from a script or a received audio/video to the delivered MP4, in its own unit
        "montage-script", "🎬 Monter un script…", "simple", ("montage", "make", "--script", "--style", "{style}", "--fin", "{fin}"),
        (dataclasses.replace(_REPONSE, libelle="Le script à dire", exemple="Salut, voici mon premier montage.",
                             refus=f"script vide ou trop long ({tts.MAX_CHARS} caractères au plus)", max=tts.MAX_CHARS),
         _STYLE, _FIN),
        ("🎬 Faire dire ce script par la voix du serveur, puis monter la vidéo",), ("script de {longueur} caractères · style {style_libelle} · fin {fin_libelle}",),
        "la voix locale lit le script, le plan est fait puis la vidéo est rendue (quelques minutes à un quart d'heure) ; "
        "tu es prévenu à la fin (ou en cas d'échec), même si tu fermes la page", "low",
        ("🎬 montage lancé depuis un script ({longueur} caractères)",), stdin="texte", delai=MONTAGE_TOUT_S, fond=True,
        livre="🎬 Montage rendu", livre_echec="❌ Le montage a échoué",
        attente="⏳ Voix, plan puis rendu (jusqu'à 25 minutes) ; tu seras prévenu à la fin…"),
    Action(  # todo #437: the audio or video comes from 📎 joindre (name checked by RECU_RE, resolved by the CLI, never a path)
        "montage-audio", "🎬 Monter ce fichier audio…", "simple",
        ("montage", "make", "--recu={fichier}", "--style", "{style}", "--fin", "{fin}"),
        (_ctx("fichier", "20261020T101500-a1b2c3-note.m4a", recus.RECU_RE, "fichier reçu refusé"), _STYLE, _FIN),
        ("🎬 Monter la vidéo sur ce fichier audio ou vidéo joint",), ("{fichier} · style {style_libelle} · fin {fin_libelle}",),
        "ta voix est nettoyée et transcrite, le plan est fait puis la vidéo est rendue (jusqu'à 25 min) ; tu es prévenu à la fin "
        "(ou en cas d'échec), même si tu fermes la page", "low",
        ("🎬 montage lancé depuis le fichier {fichier}",), delai=MONTAGE_TOUT_S, fond=True,
        livre="🎬 Montage rendu", livre_echec="❌ Le montage a échoué",
        attente="⏳ Nettoyage, plan puis rendu (jusqu'à 25 minutes) ; tu seras prévenu à la fin…"),
)


# -- 👤 Profil et savoir (todo #249): profile and knowledge commands. ✅ profil is signed on the proposal's number, 📚 Ranger on the draft's fingerprint (the CLI
# reads the draft again: a draft changed since 🔎 Vérifier = refused). A draft is named by (source, id, n), never by a path typed.
VIDEO_LOT = _champs("id empreinte", actions=[_champs("n type titre texte")], faites=[_champs("n type titre")],
                    ignorees=[_champs("n type titre raison")])  # todo #442: video/lot.py
PROFIL = _champs("profil", pending=[_champs("id date source texte statut invisible_retire")])
FOCUS = _champs("line", focus=_champs("depuis par", domaines=[True]), domaines=[_champs("cle icone titre")])
CAP = _champs("exists date text", corrections=[_champs("titre chantier notes preuve")])
SOUVENIRS = _champs("refreshed_in_s", results=[_champs("session outil date extrait")])
SOUVENIRS_MAX = 200  # souvenirs_flow.QUERY_MAX
FICHE_RE = re.compile(r"[0-9a-f]{%d}" % FINGERPRINT_LEN)  # savoir.fingerprint
# 1 or 2 domains (esprit.focus.MAX), in DOMAINES order, as Telegram's ticks: "systeme", "systeme+business"…
FOCUS_CHOIX = tuple(Choix("+".join(k for k, _, _ in combo), " + ".join(f"{i} {t}" for _, i, t in combo),
                          ("--set", *(k for k, _, _ in combo)))
                    for n in range(1, esprit_focus.MAX + 1) for combo in itertools.combinations(DOMAINES, n))
_PROPOSITION = _ctx("id", "3", TODO_ID_RE, "numéro de proposition refusé")
_SOURCE = _ctx("source", "video", re.compile(r"video|docs"), "origine refusée")  # var/video/<id> or var/docs/<id>
_BROUILLON = "var/{source}/{id}/actions/{n}.md"  # proposals.ACTIONS_DIR, relative to the project root (the CLI's cwd)
PROFIL_SAVOIR: tuple[Action, ...] = (
    _lecture("profil", "👤 Profil", ("profil", "show"), (), PROFIL),
    _lecture("focus", "🧭 Focus de la semaine", ("esprit", "focus"), (), FOCUS),
    _lecture("cap", "🔭 Revue de cap", ("cap", "last"), (), CAP),
    _lecture("souvenirs", "🔎 Chercher", ("souvenirs", "chercher", "--texte={mots}"),
             (Champ("mots", "texte", "Des mots des anciennes conversations", "facture fournisseur",
                    f"écris au moins un mot ({SOUVENIRS_MAX} caractères au plus)", max=SOUVENIRS_MAX, nettoie="espaces",
                    ligne=True),), SOUVENIRS),
    Action(
        "focus-choisir", "💾 Enregistrer le focus…", "simple", ("esprit", "focus", "@domaines", "--by", "dashboard"),
        (Champ("domaines", "choix", "Domaines du focus", "systeme+business", "domaines refusés (1 ou 2)", max=40,
               defaut=FOCUS_CHOIX[0].code, choix=FOCUS_CHOIX),),
        ("🧭 Choisir le focus de la semaine",), ("{domaines_libelle}",),
        "un chantier est dans le focus si son domaine l'est (accueil, À toi, escouades)", "low",
        ("🧭 focus de la semaine : {domaines}",)),
    Action(  # ✏️ of /profil: the corrected text goes with the same signature (on the number), as on Telegram
        "profil-accepter", "✅ Ajouter au profil…", "signee", ("profil", "accept", "{id}", "--text={texte}"),
        (_PROPOSITION, Champ("texte", "texte", "Texte corrigé (facultatif)", "Préfère le thé à la menthe",
                             f"texte trop long ({FACT_MAX} caractères au plus)", min=0, max=FACT_MAX, nettoie="espaces")),
        ("✅ Ajouter un fait au profil (signé)",), ("proposition #{id}", " : « {texte} »"),
        "le fait est lu par Hermes et Claude Code dans toutes les conversations", "low",
        ("✅ proposition #{id} ajoutée au profil",), signe="profil", cible="{id}"),
    Action(
        "profil-rejeter", "✖ Refuser…", "simple", ("profil", "reject", "{id}"), (_PROPOSITION,),
        ("✖ Refuser un fait proposé",), ("proposition #{id}",), "le fait n'entre pas dans le profil", "low",
        ("✖ proposition #{id} refusée",), danger=True),
    Action(  # writes nothing (the card of Telegram): shows the fiche and its fingerprint, which 📚 Ranger signs
        "savoir-verifier", "🔎 Vérifier la fiche…", "simple", ("savoir", "verifier", _BROUILLON),
        (_SOURCE, _LOT, _N), ("🔎 Vérifier une fiche proposée",), ("fiche {n} · {source} {id}",),
        "la fiche est relue et affichée ; rien n'est rangé avant 📚 Ranger", "low",
        ("🔎 fiche {n} de {source} {id} vérifiée",), sortie=("texte", "empreinte")),
    Action(
        "savoir-ranger", "📚 Ranger la fiche…", "signee", ("savoir", "ranger", _BROUILLON),
        (_SOURCE, _LOT, _N, _ctx("empreinte", "0123456789abcdef01234567", FICHE_RE, "empreinte de fiche refusée")),
        ("📚 Ranger la fiche dans le carnet de savoir (signé)",), ("fiche {n} · {source} {id} · empreinte {empreinte}",),
        "la fiche est relue plus tard par Hermes et Claude Code ; elle est sauvegardée chaque nuit", "low",
        ("📚 fiche {n} de {source} {id} rangée",), signe="savoir", cible="{empreinte}"),
    # todo #442: every pending proposal of a video (fiches, todos, recherches, fait) filed on ONE signature of the lot
    _lecture("video-lot-voir", "📥 Propositions à ranger", ("video", "lot", "{id}"), (_LOT,), VIDEO_LOT),
    Action(  # the CLI recomputes the lot: a proposal filed meanwhile or a draft changed = refused, nothing filed
        "video-lot", "✅ Tout ranger…", "signee", ("video", "ranger-lot", "{id}", "--empreinte={empreinte}"),
        (_LOT, _ctx("empreinte", "0123456789abcdef", EMPREINTE_RE, "empreinte des propositions refusée")),
        ("📥 Ranger toutes les propositions de la vidéo (signé)",), ("vidéo {id} · lot {empreinte} : les propositions listées",),
        "chaque fiche listée est rangée dans le carnet de savoir, chaque todo ou recherche est créée (source video:<id>), "
        "le fait proposé attend ta validation dans Profil ; chaque proposition est tentée, le bilan dit ce qui a échoué",
        "low", ("📥 propositions de la vidéo {id} rangées ({empreinte})",), signe="video-lot", cible="{id}:{empreinte}",
        delai=5 * 60, sortie=("texte",), confirmer="✅ Tout ranger", detail="video-lot-voir"),
)


# -- todo #264: an interactive ultron session seen read only (bubbles + Avancement panel); writing is #265, managing #266 (new, relaunch) ----
# todo #283 (the Pilote): the raw tmux screen (`session-ecran`), writing and keys « toujours ouvert » (no sheet, still signed).
_SESSION = _ctx("nom", "ultron-19-10-15h38", SESSION_NAME_RE, "nom de session refusé")
_SESSION_N = _ctx("n", "60", re.compile(r"[1-9][0-9]{0,2}"), "nombre d'entrées refusé", min_=0)
SESSIONS = {"sessions": [_champs("nom cree ouverte chantier sous_agents attend travaille contexte derniere_phrase",  # todo #281: home screen
                                 agents=[_champs("nom modele depuis")],
                                 declare=_champs("maintenant ensuite attend bloque at"))],  # todo #431: state the session declares  # todo #406: detail of each session
            "chantiers": [True]}  # todo #297: the choice list of « Nouvelle session »
FERMETURE = _champs("session sous_agents", travail_non_commite=[True])  # todo #297: what the closing sheet warns about
_MENU = [_champs("touche libelle")]  # todo #283: options of the numbered menu read on the screen (sessions/ecran.py)
SESSION_LUE = _champs("session total tronque maj sous_agents_finis cree chantier contexte attend travaille",
                      entrees=[_champs("k ts texte outil modele etat")], sous_agents=[_champs("description model depuis")],
                      menu=_MENU)
SESSION_ECRAN = _champs("session largeur hauteur attend", lignes=[True], menu=_MENU)
CONSEIL = _champs("chantier raison regle")  # todo #364: the advice card of the Travail tab
USAGE = _champs("stale", session=_champs("percent resets_at"), week=_champs("percent resets_at"))
NOTIFS = _champs("non_lues", notifs=[_champs("at titre corps complet onglet lue genre genre_fil origine todo", details=[True], pj=[_champs("nom orig genre url")],
                                        cible=_champs("genre ref libelle"))])  # #276, #289; genre: Questions filter (#466)
# Claude Code's built-in commands offered while typing « / » in the Pilote (no custom command in ~/.claude/commands).
COMMANDES = ("/clear", "/compact", "/context", "/cost", "/usage", "/status", "/model", "/agents", "/memory", "/resume",
             "/rewind", "/todos", "/export", "/help")
TOUCHES = {"haut": "↑", "bas": "↓", "gauche": "←", "droite": "→", "entree": "↵ Entrée", "echap": "Échap", "tab": "Tab",
           "ctrl-c": "Ctrl-C",
           "ctrl-b": "Arrière-plan (Ctrl-B)", "ctrl-x-s": "Envoyer maintenant (Ctrl-X Ctrl-S)"}
SESSION_CATALOGUE: tuple[Action, ...] = (
    _lecture("notifs", "📜 Fil", ("notifs", "liste"), (), NOTIFS),
    Action(
        "notifs-vu", "✔ Tout marquer lu…", "simple", ("notifs", "vu"), (),
        ("✔ Marquer toutes les notifications comme lues",), ("toutes les notifications",),
        "elles ne sont plus en gras ; rien n'est supprimé", "low", ("📜 fil marqué lu",)),
    # todo #301: opening an unread entry of the fil marks it read, no sheet (`ouvert`, low risk, nothing is deleted).
    Action(
        "notifs-lire", "Marquer lue", "simple", ("notifs", "lire", "{at}"),
        (Champ("at", "contexte", "date", "2026-10-20T13:32:05+00:00", "date de notification refusée", regex=NOTIF_AT_RE, max=25),),
        ("📜 Marquer une notification comme lue",), ("{at}",), "elle n'est plus en gras", "low", ("📜 notification lue",),
        ouvert=True),
    # todo #439: 🔊 Lire with the montage's Piper voice (montage/lecture.py): the text is on stdin, the answer is the name and url of a
    # 0600 wav of var/notifs/pj/ (GET /api/pj/, removed after an hour). No sheet (`ouvert`): it reads a text aloud, nothing changes.
    Action(
        "voix-lire", "🔊 Lire à voix haute", "simple", ("montage", "lire"),
        (Champ("texte", "texte", "Le texte à lire", "Bonjour, voici la réponse.",
               f"texte vide ou trop long ({tts.LIRE_MAX_CHARS} caractères au plus)", max=tts.LIRE_MAX_CHARS),),
        ("🔊 Lire un texte à voix haute",), ("{longueur} caractères",), "un fichier audio temporaire est créé sur le serveur", "low",
        ("🔊 lecture à voix haute ({longueur} caractères)",), stdin="texte", sortie=("donnees",), donnees=_champs("nom url"),
        ouvert=True),
    _lecture("sessions", "💬 Sessions ultron", ("session", "liste"), (), SESSIONS),
    _lecture("session", "💬 Session", ("session", "lire", "{nom}", "--n={n}"), (_SESSION, _SESSION_N), SESSION_LUE),
    _lecture("session-ecran", "▤ Terminal", ("session", "ecran", "{nom}"), (_SESSION,), SESSION_ECRAN),
    _lecture("conseil", "🧭 Session conseillée", ("chantier", "conseil"), (), CONSEIL),  # todo #364
    _lecture("usage", "📊 Quota", ("usage",), (), USAGE),
    # -- todo #265: write into the session (signed `session`, like ❓ Répondre) and keys; todo #283: « toujours ouvert ».
    # The journal keeps the session and the length, never the text.
    Action(
        "session-ecrire", "➤ Envoyer", "signee", ("session", "ecrire", "{nom}", "--texte-stdin"),
        (_SESSION, Champ("texte", "texte", "Écrire à la session", "Regarde l'état des branches.",
                         f"texte vide ou trop long ({session_envoi.TEXT_MAX} caractères au plus)",
                         max=session_envoi.TEXT_MAX, nettoie="espaces", rapides=(
                             Choix("Oui.", "Oui"), Choix("Non.", "Non"),
                             Choix("Vas-y, suis tes recommandations.", "Suis tes recos")), suggestions=COMMANDES)),
        ("✍️ Écrire dans la session (signé)",), ("{nom} · {longueur} caractères",),
        "le texte est tapé dans la session puis Entrée : l'agent le lit comme un message de toi", "low",
        ("✍️ {longueur} caractères envoyés à la session {nom}",), signe="session", cible="{nom}:{empreinte}",
        stdin="texte", ouvert=True),
    Action(
        "session-choisir", "Touche", "signee", ("session", "choisir", "{nom}", "@touche"),
        (_SESSION, Champ("touche", "choix", "Touche", "1", "touche refusée", max=8, defaut="1", choix=tuple(
            Choix(k, TOUCHES.get(k, k), (k,)) for k in session_envoi.KEYS))),
        ("⌨️ Appuyer sur une touche dans la session (signé)",), ("{nom} · touche {touche_libelle}",),
        "la touche est envoyée au terminal de la session (choix, Entrée, Échap, Ctrl-C…)", "low",
        ("⌨️ touche {touche} envoyée à la session {nom}",), signe="session", cible="{nom}:{touche}", ouvert=True),
    # -- todo #266: open a new session (signed `session`, target `nouvelle:<fingerprint of the instruction>`; an empty
    # instruction is allowed) and relaunch one (simple: the CLI refuses while a worktree holds uncommitted work).
    # todo #297: the chantier (a sheet of ~/notes/prompt, « Main » = none) and the instruction both go to the script; the signed
    # target covers both (`nouvelle:<chantier>:<fingerprint of the instruction>`). Closing is signed too, after a reading that
    # warns (sub-agents, uncommitted todo worktrees) because a sheet's words are fixed.
    Action(
        "session-nouvelle", "➕ Nouvelle session…", "signee", ("session", "nouvelle", "--chantier={chantier}", "--consigne-stdin"),
        (Champ("chantier", "choix", "Chantier", "agent-ultron", "chantier refusé", regex=escouade_core.NAME_RE, min=0,
               liste="chantiers"),
         Champ("consigne", "texte", "Consigne du jour (facultative)", "Regarde l'état des branches.",
               f"consigne trop longue ({session_gestion.CONSIGNE_MAX} caractères au plus)",
               min=0, max=session_gestion.CONSIGNE_MAX, nettoie="espaces")),
        ("➕ Ouvrir une nouvelle session ultron (signé)",), ("chantier {chantier} · ", "consigne de {longueur} caractères"),
        "un nouvel agent Opus avec tous les droits démarre sur le serveur, lit la passation (celle du chantier choisi, sinon "
        "celle de Main) et la consigne puis agit", "high",
        ("➕ nouvelle session ultron ouverte", " (chantier {chantier})", " ({longueur} caractères de consigne)"), signe="session",
        cible="nouvelle:{chantier}:{empreinte}", stdin="consigne", confirmer="➕ Confirmer l'ouverture", danger=True,
        sortie=("id",)),  # the name of the new session: the page then offers to close the old one
    _lecture("session-fermeture", "Fermeture", ("session", "fermeture", "{nom}"), (_SESSION,), FERMETURE),
    Action(
        "session-fermer", "✖ Fermer…", "signee", ("session", "fermer", "{nom}"), (_SESSION,),
        ("✖ Fermer la session ultron (signé)",), ("{nom}",),
        "la session tmux est fermée (jamais forcée) et Claude s'y arrête ; refusé si des sous-agents y travaillent ; "
        "la fermeture est vérifiée ; les worktrees todo-* ne sont pas touchés", "high",
        ("✖ session {nom} fermée",), signe="session", cible="fermer:{nom}", confirmer="✖ Confirmer la fermeture", danger=True),
    Action(
        "session-relancer", "🔁 Relancer…", "simple", ("session", "relancer", "{nom}"), (_SESSION,),
        ("🔁 Relancer la session (/clear puis relecture du prompt, même session)",), ("{nom}",),
        "/clear puis relecture du prompt dans la même session ; les sous-agents continuent ; "
        "refusé si un travail n'est pas commité", "high",
        ("🔁 session {nom} relancée",), confirmer="🔁 Confirmer la relance", danger=True),
)

CATALOGUE: tuple[Action, ...] = (
    Action(
        "done", "✅ Terminer…", "signee", ("todo", "done", "{id}"), (_TODO_ID,),
        ("✅ Terminer la todo (signé)",), ("#{id}",), "la tâche passe en terminée", "low",
        ("✅ todo #{id} terminée",), signe="done", cible="{id}"),
    Action(
        "merge", "🔀 Fusionner…", "signee", ("branch", "merge", "{id}"), (_TODO_ID,),
        ("🔀 Fusionner la branche puis terminer (signé)",), ("#{id}",),
        "tests, fusion dans main, tâche terminée, redémarrage de la passerelle", "high",
        ("🔀 fusion de la todo #{id}",), signe="merge", cible="{id}", delai=MERGE_TIMEOUT_S,
        sortie=("texte", "apres_fusion"), attente="⏳ Fusion d'essai et tests en cours (jusqu'à quelques minutes)…",
        fond=True),
    Action(
        "todo", "➕ Todo…", "simple",
        ("todo", "add", "--title={titre}", "--truncate", "--source", "dashboard", "--notes={notes}", "--chantier={chantier}",
         "--priority={priorite}", "--due={echeance}", "--category={categorie}"),
        (_ctx("chantier", "fba", escouade_core.NAME_RE, "nom de chantier refusé", min_=0),
         Champ("titre", "texte", "Titre de la todo (40 caractères conseillés)", "Relancer le fournisseur",
               f"titre vide ou trop long ({TITLE_MAX} caractères au plus), notes {NOTES_MAX} au plus", max=TITLE_MAX,
               nettoie="espaces", ligne=True),
         Champ("notes", "texte", "Notes (facultatif)", "voir mail",
               f"titre vide ou trop long ({TITLE_MAX} caractères au plus), notes {NOTES_MAX} au plus", min=0,
               max=NOTES_MAX),
         Champ("priorite", "choix", "Priorité", "high", "priorité inconnue", max=8, defaut="normal",
               choix=tuple(Choix(k, v) for k, v in PRIORITES.items())),
         Champ("echeance", "choix", "Échéance", "tomorrow", "échéance inconnue", max=8, defaut="none",
               choix=tuple(Choix(k, v) for k, v in ECHEANCES.items())),
         dataclasses.replace(_CATEGORIE, min=0)),  # empty = the CLI's default category (the first of todo.categories)
        ("➕ Ajouter une todo", " au chantier {chantier}"),
        ("{titre}", " ({notes})", " · {priorite_libelle}", " · échéance : {echeance_libelle}", " · catégorie {categorie}"),
        "une todo est ajoutée à ta liste, avec ces notes, cette priorité, cette échéance et cette catégorie", "low",
        ("➕ todo ajoutée : {titre}",), sortie=("id",)),
    # -- todo #242: the todos and their merges, the same commands as Telegram's /todo, 🔀 Tout fusionner and 🧰 --------
    _lecture("todos", "🎯 Toutes les todos", ("todo", "board", "--filter", "{filtre}", "--category={categorie}"),
             (Champ("filtre", "choix", "Filtre", "urgent", "filtre inconnu", max=8, defaut="open",
                    choix=tuple(Choix(k, v) for k, v in FILTRES.items())), dataclasses.replace(_CATEGORIE, min=0)), BOARD),
    _lecture("ranger-proposition", "🗂 Ranger les todos", ("todo", "rattacher", "--proposer"), (), RANGER),
    Action(  # applies the proposal read above, by its fingerprint (the CLI recomputes it: a moved todo = refused, nothing changed)
        "ranger-appliquer", "🗂 Ranger…", "simple", ("todo", "rattacher", "--appliquer={empreinte}", "--by", "dashboard"),
        (_ctx("empreinte", "0123abcd", RANGER_EMPREINTE_RE, "empreinte de rangement refusée"),),
        ("🗂 Ranger les todos dans leurs chantiers",), ("proposition {empreinte}",),
        "chaque todo de la proposition affichée change de chantier (tracé dans son historique)", "low",
        ("🗂 todos rangées dans leurs chantiers ({empreinte})",)),
    # todo #362: the tidy-up an agent prepared (`menage proposer`), applied whole on ONE signature of its fingerprint
    _lecture("menage-proposition", "🧹 Ménage proposé", ("menage", "proposition"), (), MENAGE),
    Action(  # the CLI recomputes the fingerprint: proposal changed, todo closed, chantier gone = refused, nothing applied
        "menage-lot", "✅ Tout appliquer…", "signee", ("menage", "appliquer", "--empreinte={empreinte}"),
        (_ctx("empreinte", "0123456789abcdef", EMPREINTE_RE, "empreinte de la proposition refusée"),),
        ("🧹 Appliquer le ménage proposé (signé)",), ("proposition {empreinte}",),
        "les todos listées passent en annulées (notes gardées, ↩️ Rouvrir possible), puis les chantiers listés sont "
        "archivés, les skills et dossiers listés vont à la corbeille (.trash) ; arrêt au premier refus", "low",
        ("🧹 ménage proposé appliqué ({empreinte})",), signe="menage-lot", cible="{empreinte}", delai=5 * 60,
        sortie=("texte",), confirmer="✅ Tout appliquer"),
    # todo #417: 📋 Rapports du dimanche (Moi): cap review, proposed tidy-up and health, the advised actions on ONE signature
    _lecture("rapports", "📋 Rapports du dimanche", ("rapports", "dimanche"), (), RAPPORTS),
    Action(  # the CLI recomputes the lot (todo made meanwhile, proposal changed = refused, nothing done); reflexes never in it
        "rapports-lot", "✅ Appliquer les actions conseillées…", "signee", ("rapports", "appliquer", "--empreinte={empreinte}"),
        (_ctx("empreinte", "0123456789abcdef", EMPREINTE_RE, "empreinte des rapports refusée"),),
        ("📋 Appliquer les actions conseillées des rapports du dimanche (signé)",), ("lot {empreinte} : les actions listées",),
        "chaque correction de cap listée devient une todo de son chantier (source cap:<date>), puis le ménage proposé est "
        "appliqué (todos annulées, notes gardées, ↩️ Rouvrir possible ; chantiers archivés) ; les pannes de santé ne sont "
        "jamais traitées ici", "low",
        ("📋 actions conseillées des rapports appliquées ({empreinte})",), signe="rapports-lot", cible="{empreinte}",
        delai=5 * 60, sortie=("texte",), confirmer="✅ Appliquer", detail="rapports"),
    # todo #429: 💡 Nouvelle habitude proposée (Moi): a skill draft of fin-de-chantier, its FULL text signed by its fingerprint
    _lecture("bilan-soir", "🌙 Bilan du soir", ("bilan", "soir"), (), BILAN_SOIR),
    _lecture("habitudes", "💡 Habitudes proposées", ("habitudes", "liste"), (), HABITUDES),
    _lecture("habitude-voir", "💡 Habitude proposée", ("habitudes", "voir", "--dossier={dossier}"), (_HABITUDE_DOSSIER,), HABITUDE),
    Action(  # the CLI reads the draft again: text changed, incomplete, bad name, link, synced, existing skill = refused
        "habitude", "✅ Installer…", "signee", ("habitudes", "installer", "--dossier={dossier}", "--empreinte={empreinte}"),
        (_HABITUDE_DOSSIER, _ctx("empreinte", "0123456789abcdef", EMPREINTE_RE, "empreinte de l'habitude refusée")),
        ("💡 Installer l'habitude proposée (signé)",), ("brouillon {dossier} : le texte ci-dessous, tel quel",),
        "nouvelle : ~/.claude/skills/<nom>/SKILL.md est créé avec ce texte ; patch : ce texte est ajouté à la fin du "
        "SKILL.md de la skill maison visée, sous un titre daté ; remplace : le SKILL.md entier est remplacé (l'ancien gardé en SKILL.md.bak-<dossier>) ; le brouillon passe dans appliquees/", "low",
        ("💡 habitude installée ({dossier})",), signe="habitude", cible="{dossier}:{empreinte}",
        sortie=("texte",), confirmer="✅ Installer", detail="habitude-voir"),
    Action(
        "habitude-jeter", "❌ Jeter…", "simple", ("habitudes", "jeter", "--dossier={dossier}"), (_HABITUDE_DOSSIER,),
        ("❌ Jeter l'habitude proposée",), ("brouillon {dossier}",),
        "le brouillon passe dans ~/ultron/propositions-skills/jetees/ : rien n'est installé ni supprimé", "low",
        ("❌ habitude jetée ({dossier})",), sortie=("texte",), confirmer="❌ Jeter"),
    # todo #503: 🔁 Routine proposée (À toi): a routine in one sentence, activated by a ✅ signed on its exact content (nothing runs)
    _lecture("routines", "🔁 Routines", ("routine", "liste"), (), ROUTINES),
    _lecture("routine-voir", "🔁 Routine proposée", ("routine", "voir", "--nom={nom}"), (_ROUTINE_NOM,), ROUTINE),
    Action(  # the CLI reads the routine again: content changed, bad cadence, 50 active already = refused
        "routine", "✅ Activer…", "signee", ("routine", "activer", "--nom={nom}", "--empreinte={empreinte}"),
        (_ROUTINE_NOM, _ctx("empreinte", "0123456789abcdef", EMPREINTE_RE, "empreinte de la routine refusée")),
        ("🔁 Activer la routine proposée (signé)",), ("routine {nom} : ce qui est listé ci-dessous, tel quel",),
        "la routine passe dans ~/ultron/routines/actives/ ; rien n'est lancé ici (le lanceur à l'heure est une autre todo)",
        "low", ("🔁 routine activée ({nom})",), signe="routine", cible="{nom}:{empreinte}",
        sortie=("texte",), confirmer="✅ Activer", detail="routine-voir"),
    Action(
        "routine-refuser", "❌ Refuser…", "simple", ("routine", "refuser", "--nom={nom}"), (_ROUTINE_NOM,),
        ("❌ Refuser la routine proposée",), ("routine {nom}",),
        "la routine passe dans ~/ultron/routines/refusees/ : rien n'est activé ni supprimé", "low",
        ("❌ routine refusée ({nom})",), sortie=("texte",), confirmer="❌ Refuser"),
    Action(  # todo #539: stops an ACTIVE routine; unsigned, it only removes work
        "routine-retirer", "🛑 Retirer…", "simple", ("routine", "retirer", "--nom={nom}"), (_ROUTINE_NOM,),
        ("🛑 Retirer la routine active",), ("routine {nom}",),
        "la routine passe dans ~/ultron/routines/retirees/ : le lanceur ne la lance plus, rien n'est supprimé", "low",
        ("🛑 routine retirée ({nom})",), sortie=("texte",), confirmer="🛑 Retirer"),
    _lecture("banc-historique", "📈 Historique du banc", ("banc", "historique"), (), BANC),  # todo #513
    _lecture("todo-fiche", "📋 Fiche", ("todo", "show", "{id}"), (_TODO_ID,), FICHE_TODO),
    _lecture("commits", "🌿 Commits", ("branch", "info", "{id}"), (_TODO_ID,), BRANCHE),
    _lecture("lot", "🔀 Tout fusionner", ("branch", "lot", "--sans={sans}"),
             (_ctx("sans", "134,135", TODO_IDS_RE, "liste de todos refusée", min_=0),), LOT),
    _lecture("apres", "🧰 Actions après fusion", ("branch", "after", "{id}"), (_TODO_ID,), APRES),
    _edition("titre", "📝 Titre…", "--title={titre}",
             Champ("titre", "texte", "Nouveau titre (40 caractères conseillés)", "Relancer le fournisseur",
                   f"titre vide ou trop long ({TITLE_MAX} caractères au plus)", max=TITLE_MAX, nettoie="espaces", ligne=True),
             "📝 Modifier le titre", "#{id} : {titre}", "📝 titre de la todo #{id} modifié"),
    _edition("notes", "🗒 Notes…", "--notes={notes}",
             Champ("notes", "texte", "Nouvelles notes (remplacent les anciennes)", "voir mail",
                   f"notes vides ou trop longues ({NOTES_MAX} caractères au plus)", max=NOTES_MAX),
             "🗒 Remplacer les notes", "#{id} : {notes}", "🗒 notes de la todo #{id} modifiées"),
    _edition("clair", "💡 En clair…", "--clair={clair}",
             Champ("clair", "texte", "En clair : à quoi sert la tâche (une phrase)", "garder le client",
                   f"phrase vide ou trop longue ({CLAIR_MAX} caractères au plus)", max=CLAIR_MAX, nettoie="espaces",
                   ligne=True),
             "💡 Modifier « En clair »", "#{id} : {clair}", "💡 « En clair » de la todo #{id} modifié"),
    _edition("categorie", "🏷 Catégorie…", "--category={categorie}", _CATEGORIE,
             "🏷 Changer la catégorie", "#{id} → {categorie}", "🏷 catégorie de la todo #{id} : {categorie}"),
    _edition("priorite", "Priorité…", "--priority={priorite}",
             Champ("priorite", "choix", "Priorité", "high", "priorité inconnue", max=8, defaut="normal",
                   choix=tuple(Choix(k, v) for k, v in PRIORITES.items())),
             "Changer la priorité", "#{id} → {priorite_libelle}", "priorité de la todo #{id} : {priorite}"),
    _edition("echeance", "📅 Échéance…", "--due={echeance}",
             Champ("echeance", "choix", "Échéance", "tomorrow", "échéance inconnue", max=8, defaut="today",
                   choix=tuple(Choix(k, v) for k, v in ECHEANCES.items())),
             "📅 Changer l'échéance", "#{id} → {echeance_libelle}", "📅 échéance de la todo #{id} : {echeance}"),
    _edition("echeance-date", "📅 Autre date…", "--due={date}",
             Champ("date", "texte", "Date : 20/10 ou 20/10/2027", "20/10", "date refusée (20/10 ou 20/10/2027)",
                   regex=DATE_RE, max=10, ligne=True),
             "📅 Changer l'échéance", "#{id} → {date}", "📅 échéance de la todo #{id} : {date}"),
    _edition("tete", "🔥 Passer en tête…", "--nature chantier --by dashboard", None,
             "🔥 Passer la todo en tête", "#{id}", "🔥 todo #{id} passée en tête"),
    _edition("fond", "🌱 Mettre en fond…", "--nature fond --by dashboard", None,
             "🌱 Mettre la todo en fond", "#{id}", "🌱 todo #{id} mise en fond"),
    Action(
        "abandon", "✖ Abandonner…", "signee", ("todo", "cancel", "{id}"), (_TODO_ID,),
        ("✖ Abandonner la todo (signé)",), ("#{id}",), "la tâche passe en annulée ; ↩️ Rouvrir reste possible", "low",
        ("✖ todo #{id} abandonnée",), signe="cancel", cible="{id}", confirmer="✖ Confirmer l'abandon", danger=True, groupe=True),
    Action(  # `reopen` needs no proof (CLAUDE.md « Clôture signée »): simple, like the CLI
        "rouvrir", "↩️ Rouvrir…", "simple", ("todo", "reopen", "{id}"), (_TODO_ID,),
        ("↩️ Rouvrir la todo",), ("#{id}",), "la tâche repasse à faire", "low", ("↩️ todo #{id} rouverte",)),
    Action(  # signed on the fingerprint shown (`branch lot`): the CLI recomputes it, a moved branch = refused
        "fusion-lot", "✅ Tout fusionner…", "signee", ("branch", "merge-lot", "--ids={ids}", "--empreinte={empreinte}"),
        (_ctx("ids", "81,82", LOT_IDS_RE, f"lot refusé : {LOT_MAX} todos au plus depuis le dashboard (➖ Retirer les autres)"),
         _ctx("empreinte", "0123456789abcdef", EMPREINTE_RE, "empreinte du lot refusée")),
        ("🔀 Tout fusionner (signé)",), ("todos {ids} · lot {empreinte}",),
        "tests puis fusion dans main une par une, todos terminées, actions après fusion affichées lancées, "
        "un seul redémarrage du bot ; tests rouges = todo laissée ouverte et retentée, autre refus = arrêt", "high",
        ("🔀 tout fusionner : {ids}",), signe="fusion-lot", cible="{empreinte}", delai=LOT_TIMEOUT_S, sortie=("texte",),
        attente="⏳ Fusion des todos une par une, tests compris (plusieurs minutes)…", fond=True),
    Action(  # target = after_merge.target: "todo-<id>:<n>", the commands shown above the button
        "apres-run", "✅ Exécuter…", "signee", ("branch", "after-run", "{id}", "{n}"), (_TODO_ID, _N),
        ("🧰 Exécuter une action après fusion (signé)",), ("action {n} de #{id}, commandes affichées",),
        "les commandes affichées tournent une fois (utilisateur ultron, sans sudo)", "low",
        ("🧰 action {n} après la fusion de #{id}",), signe="user", cible="todo-{id}:{n}", delai=AFTER_TIMEOUT_S,
        sortie=("texte",), attente="⚙️ Action en cours…", fond=True),
    # todo #267: the state of a background action (merge, lot, after-merge action), polled by the page every 2 s
    _lecture("suivi", "⏳ Suivi", ("dashboard", "suivi", "{suivi}"),
             (_ctx("suivi", "20261019-120000-0a1b2c3d", SUIVI_RE, "suivi refusé"),), SUIVI),
    # -- todo #254: the orchestrator merges green branches alone; ↩️ undoes the last one (signed, like ✅ Terminer) ----------
    _lecture("etat-fusions", "🌿 État des fusions", ("branch", "etat"), (), ETAT_FUSIONS),
    _lecture("fusion-auto", "🤖 Dernière fusion automatique", ("branch", "auto-last"), (), FUSION_AUTO),
    Action(  # target = auto_merge.target, shown by the reading above: the CLI refuses if the last merge is another one
        "annuler-fusion", "↩️ Annuler la dernière fusion…", "signee", ("branch", "revert-last", "--cible={fusion}"),
        (_ctx("fusion", "254-0123456789ab", FUSION_CIBLE_RE, "fusion à annuler refusée"),),
        ("↩️ Annuler la dernière fusion automatique (signé)",), ("fusion {fusion}",),
        "git revert de cette fusion dans main (refusé s'il y a conflit), la todo reste ouverte, redémarrage du bot", "high",
        ("↩️ fusion automatique {fusion} annulée",), signe="revert-fusion", cible="{fusion}", delai=MERGE_TIMEOUT_S,
        sortie=("texte",), confirmer="↩️ Confirmer l'annulation", danger=True),
    # -- todo #495: the user's signed advance authorisation: the orchestrator merges alone all but the security list ---------
    _lecture("autorisation-etat", "🤝 Autorisation d'avance", ("branch", "autorisation", "etat"), (), AUTORISATION),
    Action(  # target = "<perimeter shown>:<days>": the CLI refuses a perimeter other than the current SECURITY list
        "autorisation", "🤝 Autoriser d'avance…", "signee",
        ("branch", "autorisation", "donner", "--jours={jours}", "--perimetre={perimetre}"),
        (Champ("jours", "choix", "Durée", "7", "durée refusée", max=2, defaut="7",
               choix=tuple(Choix(str(j), f"{j} jour" + ("s" if j > 1 else "") + (" (aujourd'hui seulement)" if j == 1 else
                                                                                   " (aujourd'hui compris)"))
                           for j in autorisation.JOURS)),
         _ctx("perimetre", "0123456789abcdef", EMPREINTE_RE, "périmètre refusé")),
        ("🤝 Autoriser d'avance les fusions automatiques (signé)",), ("{jours_libelle}, liste de sécurité {perimetre}",),
        "jusqu'au soir du dernier jour (heure de Paris), l'orchestrateur fusionne seul tout sauf la liste de sécurité "
        "(root, secrets, envoi, suppression, signature, auth du dashboard), autres dépôts testés compris ; "
        "la todo reste ouverte pour ton ✅, ↩️ annule une fusion, 🚫 Retirer arrête tout", "high",
        ("🤝 fusions automatiques autorisées ({jours_libelle})",), signe="autorisation", cible="{perimetre}:{jours}",
        confirmer="🤝 Confirmer l'autorisation", danger=True),
    Action(
        "autorisation-retirer", "🚫 Retirer l'autorisation…", "simple", ("branch", "autorisation", "retirer"), (),
        ("🚫 Retirer l'autorisation d'avance",), ("les fusions automatiques",),
        "retour aux règles habituelles : l'orchestrateur ne fusionne plus seul les fichiers sensibles", "low",
        ("🚫 autorisation d'avance retirée",)),
    *ANALYSER,
    # -- escouades et chantiers (todo #246) : mêmes commandes que le plugin Telegram, rien d'autre ici ------------------
    Action(
        "chantier-archiver", "🗄 Archiver…", "simple", ("chantier", "archiver", "{sujet}"),
        (_ctx("sujet", "fba", escouade_core.NAME_RE, "nom de chantier refusé"),),
        ("🗄 Archiver le chantier",), ("{sujet}",),
        "le chantier passe dans termines/ (refusé tant qu'une escouade tourne ou qu'une todo est ouverte) ; ↩️ Rouvrir le ramène",
        "low", ("🗄 chantier {sujet} archivé",), confirmer="🗄 Confirmer l'archivage"),
    Action(
        "chantier-rouvrir", "↩️ Rouvrir…", "simple", ("chantier", "rouvrir", "{sujet}"),
        (Champ("sujet", "texte", "Nom du chantier archivé à rouvrir", "fba", "nom de chantier refusé",
               regex=escouade_core.NAME_RE, max=40, ligne=True),),
        ("↩️ Rouvrir le chantier",), ("{sujet}",), "le chantier revient dans la liste des chantiers ouverts", "low",
        ("↩️ chantier {sujet} rouvert",)),
    *SESSION_CATALOGUE,
    *PROFIL_SAVOIR,
    # -- todo #247: the detailed health, the same commands as Telegram's /sante (hermes_plugin/health_flow.py) -----------
    _lecture("sante-reflexes", "🩺 Pannes connues", ("health", "reflexes"), (), REFLEXES),
    _lecture("sante-garde", "🛡 ultron-guard", ("health", "guard"), (), GARDE),
    _lecture("sante-batterie", "🔋 Batterie", ("health", "battery"), (), _champs("text")),
    Action(  # signed on the "<reflex>:<fingerprint>" shown; ultron-user checks every condition again (reflexes.agir)
        "reflexe", "🩺 Agir sur la panne…", "signee", ("health", "reflexe-agir", "{panne}"),
        (_ctx("panne", "verrou-restic:0123456789ab", reflexes.CIBLE_RE, "diagnostic refusé"),),
        ("🩺 Agir sur une panne connue (signé)",), ("diagnostic {panne}",),
        "ultron-user revérifie toutes les conditions juste avant d'agir, sinon rien n'est fait ; verrou de sauvegarde : "
        "retire les verrous périmés (« restic unlock », jamais --remove-all), la sauvegarde suivante repasse", "low",
        ("🩺 réflexe {panne}",), signe="reflexe", cible="{panne}", delai=REFLEX_TIMEOUT_S,
        attente="⏳ Vérification des conditions puis action…"),
    Action(  # `claude -p` (haiku): simple as on Telegram, one CLI call; the job's own timeout answers first
        "sante-analyse", "🩺 Analyse complète…", "simple", ("run", "sante-serveur", "--requested-by", "dashboard"), (),
        ("🩺 Analyse complète du serveur",), ("métriques du serveur, lues par Claude (haiku)",),
        "un bilan est écrit (collecte des métriques puis Claude, ~15 s) ; rien ne change sur le serveur", "low",
        ("🩺 analyse complète du serveur",), delai=SANTE_TIMEOUT_S, sortie=("donnees",), donnees=BILAN,
        attente="⏳ Analyse en cours… (collecte des métriques puis Claude, ~15 s)"),
    # -- Telegram en secours (todo #290): `telegram on|off`, off by default; alerts of failure always go through --------------
    *(Action(
        f"telegram-{nom}", bouton, "simple", ("telegram", etat), (), (quoi,), ("les notifications Telegram",), effet, "low",
        (journal,), sortie=("texte",))
      for nom, etat, bouton, quoi, effet, journal in (
          ("activer", "on", "📨 Réactiver Telegram…", "📨 Réactiver les notifications Telegram",
           "les notifications repartent sur Telegram", "📨 Telegram réactivé"),
          ("couper", "off", "🔕 Couper Telegram…", "🔕 Couper les notifications Telegram",
           "plus de notification sur Telegram, sauf les alertes de panne ; le dashboard reçoit tout", "🔕 Telegram coupé"))),
    # -- comptes Claude (todo #263): `compte etat|enregistrer|basculer`, the switch signed on the destination -----------------
    _lecture("comptes", "👥 Comptes", ("compte", "etat"), (), COMPTES),
    _lecture("budget", "💰 Budget du jour", ("compte", "budget"), (), BUDGET),  # todo #328: history only, no network
    *(Action(
        f"compte-enregistrer-{s}", f"💾 Mémoriser comme {s.upper()}…", "simple", ("compte", "enregistrer", s), (),
        (f"💾 Mémoriser la connexion Claude en cours comme {s.upper()}",), (f"copie privée {s.upper()} (fichier 0600)",),
        f"la connexion actuelle de Claude Code est copiée dans {s.upper()} ; refusé si c'est déjà l'autre compte", "low",
        (f"💾 compte Claude enregistré comme {s.upper()}",)) for s in ("a", "b")),
    Action(
        "compte-basculer", "🔁 Basculer…", "signee", ("compte", "basculer", "--vers={vers}"),
        (_ctx("vers", "b", re.compile(r"[ab]"), "compte refusé : a ou b"),),
        ("🔁 Basculer de compte Claude (signé)",), ("vers le compte {vers}",),
        "les nouvelles sessions Claude utilisent l'autre compte ; les sessions ouvertes restent sur l'ancien (➕ Nouvelle session)",
        "high", ("🔁 bascule vers le compte Claude {vers}",), signe="compte", cible="{vers}", sortie=("texte",)),
    # -- todo #534: « Planning du jour » (À toi): today's box until midnight (Paris), and the pause of the automatic switch
    _lecture("planning", "📅 Planning du jour", ("compte", "planning", "voir"), (), PLANNING),
    Action(
        "planning-poser", "📅 Poser le planning…", "simple",
        ("compte", "planning", "poser", "--reserve-a={ra}", "--reserve-b={rb}", "--seuil={seuil}"),
        tuple(Champ(nom, "texte", libelle, exemple, refus, regex=PCT_RE, min=0, max=3, ligne=True) for nom, libelle, exemple, refus in (
            ("ra", "Réserve A (% de la dose, vide = 25)", "25", "réserve A refusée : un nombre de 0 à 100"),
            ("rb", "Réserve B (% de la dose, vide = 25)", "30", "réserve B refusée : un nombre de 0 à 100"),
            ("seuil", "Seuil (% session 5 h, vide = 90)", "90", "seuil refusé : un nombre de 1 à 100"))),
        ("📅 Poser le planning du jour",), ("jusqu'à minuit", " · réserve A {ra} %", " · réserve B {rb} %",
                                           " · seuil {seuil} %"),
        "la bascule auto suit ces valeurs jusqu'à minuit (heure de Paris) ; un champ vide = sa valeur par défaut, tout vide = "
        "retour aux défauts", "low", ("📅 planning du jour posé",), sortie=("texte",)),
    *(Action(
        f"planning-{nom}", bouton, "simple", ("compte", "planning", nom), (), (quoi,), ("bascule automatique de compte",),
        effet, "low", (journal,), sortie=("texte",)) for nom, bouton, quoi, effet, journal in (
          ("pause", "⏸ Mettre l'auto en pause…", "⏸ Mettre en pause la bascule auto",
           "plus aucune bascule automatique de compte jusqu'à ▶️ Reprendre ; le bouton 🔁 reste", "⏸ bascule auto en pause"),
          ("reprendre", "▶️ Reprendre l'auto…", "▶️ Reprendre la bascule auto",
           "la bascule automatique de compte reprend selon le planning du jour", "▶️ bascule auto reprise"))),
    # -- todo #336: log into an account again (🔑), signed `compte` on "login:<slot>" then on "code:<fingerprint of the code>".
    # The link goes back to the page only (sortie donnees), the code only on stdin: neither is journaled.
    *(Action(
        f"compte-connexion-{s}", f"🔑 Se reconnecter à {s.upper()}…", "signee",
        ("compte", "connexion", "demarrer", f"--compte={s}"), (),
        (f"🔑 Se reconnecter au compte Claude {s.upper()} (signé)",), (f"compte {s.upper()}",),
        "le serveur lance la connexion de Claude Code et te donne son lien ; rien ne change avant que tu colles le code", "low",
        (f"🔑 connexion au compte Claude {s.upper()} lancée",), signe="compte", cible=f"login:{s}", sortie=("donnees",),
        donnees=_champs("compte lien adresse"), attente="⏳ Lancement de la connexion (20 s au plus)…") for s in ("a", "b")),
    Action(
        "compte-connexion-code", "✅ Valider le code", "signee",
        ("compte", "connexion", "code", "--compte={compte}", "--code-stdin"),
        (_ctx("compte", "a", re.compile(r"[ab]"), "compte refusé : a ou b"),
         Champ("code", "texte", "Code affiché par claude.ai", "abcDEF123_xyz#state-42",
               "code refusé : colle le code exact de la page, sans espace", regex=compte_connexion.CODE_RE,
               max=compte_connexion.CODE_MAX, ligne=True)),
        ("🔑 Finir la connexion avec le code (signé)",), ("compte {compte} · code de {longueur} caractères",),
        "Claude Code est connecté avec ce code, puis la connexion est enregistrée dans ce compte si c'est bien lui "
        "(refusé si c'est l'autre) ; les sessions ouvertes gardent l'ancienne", "high",
        ("🔑 compte Claude {compte} reconnecté",), signe="compte", cible="code:{empreinte}", stdin="code", sortie=("texte",),
        delai=180, attente="⏳ Connexion en cours (jusqu’à 3 minutes)…"),
    Action(
        "compte-connexion-abandonner", "✖ Abandonner la connexion…", "simple", ("compte", "connexion", "abandonner"), (),
        ("✖ Abandonner la connexion Claude en cours",), ("la session ultron-connexion",),
        "la connexion en cours est fermée ; rien n'est enregistré, le compte actif ne change pas", "low",
        ("✖ connexion Claude abandonnée",)),
    # -- briefing (todo #270): the todos finished today, for « Fait aujourd'hui » ------------------------------------------
    _lecture("faits-du-jour", "Fait aujourd'hui", ("todo", "board", "--filter", "done_today"), (), FAITS),
)
PAR_ID: dict[str, Action] = {a.id: a for a in CATALOGUE}

# The briefing's decision cards (todo #270): for each kind of À toi item, the catalogue action of « Approuver » (None = no
# one-tap approval: the card offers « Voir » or « Répondre ») and the first-person line that says why it waits.
DECISIONS: dict[str, tuple[str | None, str]] = {
    "fusionner": ("merge", "C'est prêt et testé. Il me faut ton feu vert pour l'intégrer."),
    "livree": ("done", "Je l'ai livrée. Si c'est bon pour toi, je la range."),
    "apres_fusion": (None, "C'est intégré ; il reste une action à lancer après."),
    "question": (None, "J'ai besoin de ta réponse pour continuer."),
}


def decision(kind: str | None) -> dict[str, Any]:
    """What the briefing shows for an À toi item: the approving action (a catalogue id or None) and why it waits."""
    action, pourquoi = DECISIONS.get(kind or "", (None, "Ça attend ton avis."))
    return {"approuver": action, "pourquoi": pourquoi}


# -- templates ------------------------------------------------------------------------------------------------------

def noms(template: str) -> list[str]:
    return [name for _, name, _, _ in string.Formatter().parse(template) if name is not None]


def _derives(action: Action) -> set[str]:
    out = {c.nom for c in action.champs} | {f"{c.nom}_libelle" for c in action.champs if c.choix}
    return out | ({"empreinte", "longueur"} if action.stdin else set())


def rendre(parts: tuple[str, ...], valeurs: Mapping[str, str], vides: set[str]) -> list[str]:
    """Each part filled with the values; a part naming an empty optional field is dropped."""
    return [p.format_map(valeurs) for p in parts if not set(noms(p)) & vides]


def valider(action_id: Any, body: Mapping[str, Any]) -> tuple[Action, dict[str, str], str | None]:
    """The action and its checked values (args, stdin), or Refused. Nothing else than the catalogue decides."""
    action = PAR_ID.get(action_id) if isinstance(action_id, str) else None
    if action is None:
        raise Refused("action inconnue")
    return valider_dans(action, body)


def valider_dans(action: Action, body: Mapping[str, Any]) -> tuple[Action, dict[str, str], str | None]:
    """The checked values of one entry (also used on an entry outside CATALOGUE, by verifier())."""
    args = {c.nom: c.valeur(body.get(c.nom)) for c in action.champs}
    for c in action.champs:
        if c.fixe_selon and args[c.fixe_selon[0]] in c.fixe_selon[1]:
            args[c.nom] = c.fixe_selon[1][args[c.fixe_selon[0]]]
    for c in action.champs:  # in the declared order: the first refused field gives the message
        if not c.fixe_selon:
            c.verifie(args[c.nom])
    for c in action.champs:
        if c.fixe_selon:
            c.verifie(args[c.nom])
    return action, args, (args[action.stdin] if action.stdin else None)


def valeurs(action: Action, args: Mapping[str, str], stdin: str | None) -> dict[str, str]:
    out = dict(args)
    for c in action.champs:
        if c.choix:
            out[f"{c.nom}_libelle"] = next((x.libelle for x in c.choix if x.code == args.get(c.nom)), "")
    if action.stdin:
        out["empreinte"] = mcp_ultron.value_hash(stdin or "")
        out["longueur"] = str(len(stdin or ""))
    return out


def vides(action: Action, args: Mapping[str, str]) -> set[str]:
    return {c.nom for c in action.champs if not args.get(c.nom)}


def commande(action: Action, args: Mapping[str, str], stdin: str | None) -> list[str]:
    """The argv: literals, `{champ}` filled, `@champ` replaced by the chosen option's argv; never a shell."""
    vals, empty, argv = valeurs(action, args, stdin), vides(action, args), []
    for part in action.argv:
        if part.startswith("@"):
            c = action.champ(part[1:])
            argv.extend(next(x.argv for x in c.choix if x.code == args[c.nom]))
        else:
            argv.extend(rendre((part,), vals, empty))
    return argv


def feuille(action: Action, args: Mapping[str, str], stdin: str | None) -> dict[str, Any]:
    """The confirmation sheet: Quoi / Sur quoi / Effet / Risque, then the confirm button."""
    vals, empty = valeurs(action, args, stdin), vides(action, args)
    return {"quoi": "".join(rendre(action.quoi, vals, empty)), "sur_quoi": "".join(rendre(action.sur, vals, empty)),
            "effet": action.effet, "risque": RISQUES[action.risque], "confirmer": action.confirmer,
            "danger": action.danger, "attente": action.attente or None}


def cible(action: Action, args: Mapping[str, str], stdin: str | None) -> str:
    return action.cible.format_map(valeurs(action, args, stdin))


def ligne_journal(action: Action, args: Mapping[str, str], stdin: str | None) -> str:
    return "".join(rendre(action.journal, valeurs(action, args, stdin), vides(action, args)))


_CLE_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")


def projeter(spec: Any, value: Any) -> Any:
    """`value` cut down to the whitelist `spec`: True = a plain value (str, number, bool, None), {name: spec} = an object
    with only those names (`"*cles": True` adds `cles`, the sorted names of the source object), [spec] = a list.
    Nothing is shortened: a reading is shown whole or refused (actions.py, DONNEES_MAX)."""
    if spec is True:
        return value if value is None or isinstance(value, (str, int, float, bool)) else None
    if isinstance(spec, list):
        return [projeter(spec[0], v) for v in value] if isinstance(value, list) else []
    if not isinstance(value, dict):
        return None
    out = {k: projeter(sub, value.get(k)) for k, sub in spec.items() if k != "*cles" and k in value}
    if spec.get("*cles"):
        out["cles"] = sorted(k for k in value if isinstance(k, str) and _CLE_RE.fullmatch(k))
    return out


def _verifier_spec(spec: Any, where: str) -> None:
    if spec is True:
        return
    if isinstance(spec, list) and len(spec) == 1:
        return _verifier_spec(spec[0], where)
    if not isinstance(spec, Mapping) or not spec:
        raise CatalogueError(f"{where} : données (liste blanche) mal formées")
    for k, sub in spec.items():
        if k != "*cles" and not _CLE_RE.fullmatch(k):
            raise CatalogueError(f"{where} : nom de donnée invalide {k!r}")
        _verifier_spec(sub, where)


# -- what the page receives ------------------------------------------------------------------------------------------

def public() -> list[dict[str, Any]]:
    """The page's view of the catalogue: ids, buttons, fields to draw. No argv, no regex, no signed action, no secret."""
    return [{"id": a.id, "bouton": a.bouton, "type": a.type, "danger": a.danger, "groupe": a.groupe, "ouvert": a.ouvert,
             "champs": [{"nom": c.nom, "type": c.type, "libelle": c.libelle, "max": c.max, "obligatoire": bool(c.min),
                         "ligne": c.ligne, "defaut": c.defaut, "liste": c.liste or None,
                         "choix": [[x.code, x.libelle] for x in c.choix],
                         "rapides": [[x.code, x.libelle] for x in c.rapides],
                         "suggestions": list(c.suggestions)} for c in a.champs]} for a in CATALOGUE]


# -- the catalogue checks itself ---------------------------------------------------------------------------------------

def verifier(catalogue: tuple[Action, ...] = CATALOGUE) -> None:
    """Every entry well formed, or CatalogueError (at import, so a bad entry never serves)."""
    seen: set[str] = set()
    for a in catalogue:
        where = f"action {a.id!r}"
        if not ID_RE.fullmatch(a.id) or a.id in seen:
            raise CatalogueError(f"{where} : identifiant invalide ou en double")
        seen.add(a.id)
        if a.type not in TYPES:
            raise CatalogueError(f"{where} : type inconnu {a.type!r}")
        if a.type == "signee" and (a.signe not in closing.DASHBOARD_ACTIONS or not a.cible):
            raise CatalogueError(f"{where} : action signée sans action de closing.DASHBOARD_ACTIONS ni cible")
        if a.signe == "user" and not a.cible.startswith("todo-"):  # `user` here: only an after-merge action
            raise CatalogueError(f"{where} : `user` ne signe qu'une action après fusion (todo-<id>:<n>)")
        if a.type != "signee" and (a.signe or a.cible):
            raise CatalogueError(f"{where} : seule une action `signee` signe")
        if a.risque not in RISQUES or not a.effet or not "".join(a.quoi) or not a.sur or not a.journal or not a.bouton:
            raise CatalogueError(f"{where} : feuille incomplète (quoi, sur quoi, effet, risque, journal, bouton)")
        if not isinstance(a.argv, tuple) or not a.argv or not all(isinstance(p, str) and p for p in a.argv) \
                or noms(a.argv[0]) or a.argv[0].startswith(("@", "-")):
            raise CatalogueError(f"{where} : argv doit être un tuple de chaînes commençant par une commande")
        if not set(a.sortie) <= set(SORTIES):
            raise CatalogueError(f"{where} : sortie inconnue")
        if ("donnees" in a.sortie) != (a.donnees is not None):
            raise CatalogueError(f"{where} : sortie « donnees » sans liste blanche (ou l'inverse)")
        if a.donnees is not None:
            _verifier_spec(a.donnees, where)
        if not 0 < a.delai <= max(LOT_TIMEOUT_S, VIDEO_TOUT_S):
            raise CatalogueError(f"{where} : délai hors limites")
        names = [c.nom for c in a.champs]
        if len(set(names)) != len(names):
            raise CatalogueError(f"{where} : champ en double")
        for c in a.champs:
            _verifier_champ(a, c, names)
        if a.ouvert_long and not (a.ouvert and a.risque == "low" and not a.stdin and not a.fond and not a.danger
                                  and "--vocal-seul" in a.argv and a.delai <= JOINDRE_TIMEOUT_S):
            # todo #379: the user's recorded voice (marked vocal by the server, #365) is analysed without a sheet, in up to 25 min
            raise CatalogueError(f"{where} : ouvert_long exige ouvert, risque faible, sans stdin, ni fond, ni danger, avec --vocal-seul")
        if a.ouvert and (a.type == "lecture" or a.fond or a.danger or a.risque != "low"
                         or a.delai > (JOINDRE_TIMEOUT_S if a.ouvert_long else RUN_TIMEOUT_S)):
            raise CatalogueError(f"{where} : seule une action courte à risque faible, à confirmer d'habitude, peut être ouverte")
        if a.detail and (a.type != "signee" or "empreinte" not in names or a.detail not in {
                x.id for x in catalogue if x.type == "lecture" and {c.nom for c in x.champs} <= set(names) - {"empreinte"}
                and x.donnees and "empreinte" in x.donnees}):
            raise CatalogueError(f"{where} : detail = une lecture (champs de l'action seulement) qui rend une empreinte, "
                                 "pour une action signée sur la sienne")
        if a.fond and (a.type == "lecture" or (a.stdin and a.stdin not in names)):
            # todo #440: the job file (0600) carries the stdin; todo #437: a required one too (a montage script)
            raise CatalogueError(f"{where} : seule une action à confirmer, à stdin connu, part en fond")
        if a.stdin and (a.stdin not in names or a.champ(a.stdin).type != "texte"):
            raise CatalogueError(f"{where} : stdin doit être un champ texte")
        known = _derives(a)
        for part in (*a.quoi, *a.sur, *a.journal, a.cible, *[p for p in a.argv if not p.startswith("@")]):
            if not set(noms(part)) <= known:
                raise CatalogueError(f"{where} : modèle {part!r} nomme un champ inconnu")
        for part in a.argv:
            if part.startswith("@"):
                c = next((c for c in a.champs if c.nom == part[1:]), None)
                if c is None or not c.choix or not all(x.argv for x in c.choix):
                    raise CatalogueError(f"{where} : {part} doit nommer un choix dont chaque option a son argv")
            for name in noms(part):
                c = a.champ(name) if name in names else None
                if c is None:
                    raise CatalogueError(f"{where} : argv {part!r} nomme une valeur dérivée")
                if c.type == "texte" and c.regex is None and not part.startswith("--"):  # free text: `--option=valeur` or stdin
                    raise CatalogueError(f"{where} : le texte libre {name!r} doit passer en --option={{{name}}}")
                if part == f"{{{name}}}" and c.regex is None and not c.choix:
                    raise CatalogueError(f"{where} : valeur positionnelle {name!r} sans regex")
        try:  # the example of every field passes, and gives a sheet and an argv
            _, args, stdin = valider_dans(a, {c.nom: c.exemple for c in a.champs})
            commande(a, args, stdin), feuille(a, args, stdin), ligne_journal(a, args, stdin)
            if a.type == "signee":
                closing.message(a.signe or "", cible(a, args, stdin), 0)
        except (Refused, closing.CloseRefused, KeyError, StopIteration) as exc:
            raise CatalogueError(f"{where} : l'exemple ne passe pas ({exc})") from None


def _verifier_champ(a: Action, c: Champ, names: list[str]) -> None:
    where = f"action {a.id!r}, champ {c.nom!r}"
    if not ID_RE.fullmatch(c.nom) or c.type not in CHAMP_TYPES or c.nettoie not in NETTOIE or not c.refus:
        raise CatalogueError(f"{where} : nom, type, nettoyage ou message de refus invalide")
    if not 0 <= c.min <= c.max <= 4000:
        raise CatalogueError(f"{where} : limites invalides")
    if c.type == "choix" and bool(c.choix) == bool(c.liste):
        raise CatalogueError(f"{where} : un choix a des options OU une liste de la page")
    if c.type != "choix" and (c.choix or c.liste):
        raise CatalogueError(f"{where} : options sur un champ qui n'est pas un choix")
    if (c.liste or c.type == "contexte") and c.regex is None:
        raise CatalogueError(f"{where} : valeur venue de la page sans regex")
    if c.regex is not None and (c.regex.fullmatch("-" + c.exemple) or c.regex.fullmatch("--help")):
        raise CatalogueError(f"{where} : la regex accepte une valeur qui commence par « - »")
    if (c.rapides or c.suggestions) and c.type != "texte":
        raise CatalogueError(f"{where} : réponses rapides sur un champ qui n'est pas un texte")
    for x in (*c.rapides, *(Choix(v, v) for v in c.suggestions)):  # a quick value goes through the field's own checks, unchanged
        try:
            if not x.libelle or c.verifie(c.valeur(x.code)) != x.code:
                raise Refused(c.refus)
        except Refused:
            raise CatalogueError(f"{where} : réponse rapide {x.code!r} refusée par le champ") from None
    if c.defaut and c.choix and c.defaut not in {x.code for x in c.choix}:
        raise CatalogueError(f"{where} : défaut hors des options")
    if c.fixe_selon and (c.fixe_selon[0] not in names or c.fixe_selon[0] == c.nom):
        raise CatalogueError(f"{where} : fixe_selon nomme un champ inconnu")


verifier()
