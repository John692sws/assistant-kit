"""`ultron-hub dashboard apercu` (todo #241): the dashboard of THIS worktree on 127.0.0.1 with FAKE data.

Only for an agent that wants to look at the page it changed. Nothing real is read or run:
- the blocks of `views` are replaced by fixtures (no health, usage, escouade or chantier command is called);
- the device check is replaced by "127.0.0.1 only" (a Tailscale address or any other caller is refused);
- readings of the catalogue (todo #242) answer LECTURES, cut by the same whitelist as the real CLI's data;
- actions: the first step (the confirmation sheet) works, the second is refused, and there is no closing key
  (`Actions.key` raises, the CLI is never called);
- a file sent with 📎 joindre lands in a throwaway folder of the system's temp dir, never in var/recus;
- the real dashboard (`dashboard serve`) is untouched: it still refuses the server's own node.
"""

import contextlib
import os
import struct
import tempfile
import threading
import time
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ultron_hub import notifs
from ultron_hub.dashboard import actions, catalogue, server, views

LOOPBACK = "127.0.0.1"
_MAINTENANT = int(time.time())
NO_KEY = "aperçu : aucune clé de clôture"

FIXTURES: dict[str, dict[str, Any]] = {
    "quota": {"session": {"percent": 42, "remise_a_zero": "2026-01-01T15:00:00+00:00"},
              "semaine": {"percent": 77, "remise_a_zero": "2026-01-05T08:00:00+00:00"}, "cache": False},
    "a_toi": {"items": [
        {"icone": "🔀", "kind": "fusionner", "chantier": "exemple-un", "ref": "#901", "texte": "Branche d'exemple prête à fusionner",
         "bloquante": False, "todo": 901, "question": None, **catalogue.decision("fusionner")},
        {"icone": "✅", "kind": "terminer", "chantier": "exemple-deux", "ref": "#902", "texte": "Todo d'exemple livrée",
         "bloquante": False, "todo": 902, "question": None, **catalogue.decision("terminer")},
        {"icone": "❓", "kind": "question", "chantier": "exemple-un", "ref": "Q-901", "texte": "Quelle couleur pour le bouton ? (fausse question)",
         "bloquante": True, "todo": None, "question": "Q-901", **catalogue.decision("question")}], "total": 3,
        "phrase": "Il me faut ton feu vert sur 1 branche, ta réponse à 1 question et ton avis sur 1 autre point."},
    "sante": {"disque_pct": 38, "ram_pct": 55, "charge": 0.8, "cpus": 8, "hermes_gateway": "active",
              "unites_en_echec": [], "sauvegarde_age_h": 6},
    "chantiers": {"chantiers": [  # todo #281: x/y todos, waiting items; exemple-un has a session (see LECTURES "sessions")
        {"sujet": "exemple-un", "titre": "Chantier d'exemple un", "groupe": "📥", "hors": False, "ouvertes": 4, "faites": 9,
         "a_toi": 2},
        {"sujet": "exemple-deux", "titre": "Chantier d'exemple deux", "groupe": "📥", "hors": False, "ouvertes": 10, "faites": 3,
         "a_toi": 1},
        {"sujet": "exemple-trois", "titre": "Chantier d'exemple trois", "groupe": "💤", "hors": False, "ouvertes": 1, "faites": 6,
         "a_toi": 0}]},
}
FICHE = {"sujet": "exemple-un", "titre": "Chantier d'exemple un", "groupe": "🧪",
         "ou_en_est": "Fausse fiche pour l'aperçu : rien de réel ici.", "reprise": ["Première étape (exemple)", "Deuxième étape (exemple)"],
         "todos": [{"id": 901, "titre": "Todo d'exemple"}], "questions": [{"id": "Q-901", "texte": "Question d'exemple ?"}]}


_TACHES = [
    {"id": 901, "title": "Branche d'exemple prête à fusionner", "priority": "high", "category": "serveur", "status": "todo",
     "due_date": "2026-01-01", "nature": "chantier", "clair": "montrer une todo en retard (fausse donnée)",
     "chantier": "exemple-un", "overdue": True, "today": False, "badges": "🌿"},
    {"id": 903, "title": "Relancer le fournisseur (exemple)", "priority": "normal", "category": "travail", "status": "todo",
     "due_date": "2026-01-02", "nature": "chantier", "clair": "", "chantier": None, "overdue": False, "today": True,
     "badges": ""},
    *({"id": 910 + i, "title": f"Todo d'exemple {i}", "priority": "low", "category": "perso", "status": "todo", "due_date": None,
       "nature": "chantier", "clair": "", "chantier": "exemple-deux", "overdue": False, "today": False, "badges": ""}
      for i in range(1, 11)),
]
_TEXTE_HABITUDE = ("---\nname: relire-avant-fusion\ndescription: \"Avant une fusion, relire le diff et les tests (exemple).\"\n---\n\n"
                   "# relire-avant-fusion\n\n## Règles tirées des corrections\n- Relire le diff complet avant 🔀 (exemple).\n\n"
                   "## Étapes\n1. `git diff main...` puis les tests.\n2. Seulement ensuite, proposer la fusion.\n")
_HABITUDE = {"dossier": "2026-01-04-relire-avant-fusion", "nom": "relire-avant-fusion", "mode": "nouvelle",
             "resume": "Nouvelle skill « relire-avant-fusion », proposée le 2026-01-04. Tirée de 2 correction(s) et 1 piège(s) ; "
                       "gestes répétés : fusion ×5, diff ×4, tests ×3.",
             "texte": _TEXTE_HABITUDE, "lignes": _TEXTE_HABITUDE.splitlines(), "empreinte": "abcdef0123456789",
             "complet": True, "trop_long": False}
_HABITUDE_A_COMPLETER = {**_HABITUDE, "dossier": "2026-01-03-notes-retex", "nom": "notes-retex", "empreinte": "9876543210abcdef",
                         "resume": "Nouvelle skill « notes-retex », proposée le 2026-01-03. Tirée de 1 correction(s) et 0 piège(s).",
                         "texte": "# notes-retex\n\n## Étapes\nA COMPLÉTER\n", "lignes": ["# notes-retex", "", "## Étapes", "A COMPLÉTER"],
                         "complet": False}
# The readings of the catalogue (todo #242), by action id: what the fake CLI answers (then whitelisted like the real one).
_ECRAN = [  # todo #283: a fake Claude Code screen (exemple)
    "❯ Regarde l'état des branches (exemple).",
    "",
    "● Bash(git branch --list 'todo-*')",
    "  └ todo-901/exemple",
    "    todo-904/suite",
    "",
    "● Deux branches sont prêtes à fusionner (fausse donnée). La première a 2 commits, la seconde 1 commit empilé sur la première.",
    "",
    "● Agent(Agent-901 exemple (Sonnet))",
    "  └ Backgrounded agent (↓ to manage · ctrl+o to expand)",
    "",
    "● Bash(git merge --no-ff todo-901/exemple)",
    "",
    "──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────",
    " Bash command",
    "",
    "   git merge --no-ff todo-901/exemple",
    "   Fusionne la branche d'exemple",
    "",
    " Do you want to proceed?",
    " ❯ 1. Yes",
    "   2. Yes, and don't ask again for git commands in /exemple/ultron-hub",
    "   3. No, and tell Claude what to do differently (esc)",
    "",
    " Esc to cancel · Tab to amend",
]
_ROUTINE = {"nom": "veille-fba", "phrase": "Chaque lundi matin, regarde les nouveaux produits FBA et fais-moi un point (exemple).",
            "cadence": "chaque lundi 08:00", "but": "Un point hebdomadaire des produits FBA à étudier (exemple).",
            "critere_fin": ["test -s var/fba/point.md"], "plafond": 5, "chantier": "fba",
            "cree_le": "2026-01-04T09:00:00+01:00", "statut": "proposee", "empreinte": "0f1e2d3c4b5a6978",
            "prochain": "2026-01-05T08:00+01:00"}
_ROUTINE["lignes"] = ["Routine « veille-fba »", "Phrase : " + _ROUTINE["phrase"], "Cadence : chaque lundi 08:00",
                      "But : " + _ROUTINE["but"], "Fin quand : test -s var/fba/point.md",
                      "Plafond : 5 point(s) de quota par lancement", "Chantier : fba", "Proposée le : 2026-01-04T09:00:00+01:00"]
LECTURES: dict[str, dict[str, Any]] = {
    "todos": {"filter": "open", "category": None, "open": 14, "fond": 2, "overdue": 1, "today": 1, "high": 1,
              "branches": 1, "categories": ["perso", "travail", "serveur", "apprentissage", "admin"],
              "tasks": _TACHES},
    "ranger-proposition": {"lines": [  # todo #243: the proposal of `todo rattacher --proposer` (fake)
        {"id": 903, "titre": "Relancer le fournisseur (exemple)", "de": None, "vers": "fba"},
        {"id": 910, "titre": "Todo d'exemple 0", "de": "fba", "vers": None}], "empreinte": "0123abcd", "chantiers": 1},
    "menage-proposition": {  # todo #362: the proposal of `menage proposer` (fake)
        "n": 3, "empreinte": "0123456789abcdef", "cree_a": "2026-01-01T09:00:00+00:00", "par": "orchestrateur", "bloquee": None,
        "cancel": [{"id": 905, "titre": "Ancienne idée (exemple)", "raison": "remplacée par #903", "refus": None},
                   {"id": 906, "titre": "Essai abandonné (exemple)", "raison": "plus utile depuis la refonte", "refus": None}],
        "archiver": [{"sujet": "ancien-chantier", "refus": None}],
        "corbeille": [{"nom": "vieille-skill", "raison": "aucun usage depuis 30 j", "refus": None}],
        "dossiers": [{"chemin": "/exemple/var/missions/m-ancienne/work", "raison": "work/ d'une vieille mission",
                      "refus": None}]},
    "rapports": {  # todo #417: `rapports dimanche` (fake): two cap corrections to create and the tidy-up above
        "empreinte": "fedcba9876543210",
        "cap": {"date": "2026-01-04", "resume": "2 correction(s) · 3🟢 1🟠", "corrections": [
            {"titre": "Relancer le chantier exemple", "chantier": "exemple-un", "todo": None},
            {"titre": "Classer les todos d'exemple", "chantier": "exemple-un", "todo": None}]},
        "menage": {"date": "2026-01-01T09:00:00+00:00", "resume": "2 todo(s), 1 chantier(s)",
                   "empreinte": "0123456789abcdef", "n": 3, "bloquee": None},
        "sante": {"date": "2026-01-04T08:00:00+00:00", "resume": "1 panne(s) connue(s)", "alertes": [],
                  "pannes": [{"nom": "verrou-restic", "diagnostic": "Verrou de sauvegarde périmé (exemple)", "detail": "",
                              "cible": "verrou-restic:0123456789ab"}]},
        "actions": [{"type": "todo", "texte": "➕ Todo « Relancer le chantier exemple » (chantier exemple-un)"},
                    {"type": "todo", "texte": "➕ Todo « Classer les todos d'exemple » (chantier exemple-un)"},
                    {"type": "menage", "texte": "🧹 Appliquer le ménage proposé : abandonner/archiver #905, #906, chantier ancien-chantier"}]},
    "bilan-soir": {"date": "2026-01-04", "total": 3, "fusions": 1, "clotures": 1, "bascules": 1, "faits": 3, "gestes_evites": 3, "lignes": [  # todo #530: `bilan soir` (fake)
        {"id_evenement": 9001, "heure": "10:15", "todo_id": 901, "titre": "Todo d'exemple fusionnée seule", "sorte": "fusion",
         "sha": "0123456789ab", "by": "auto:orchestrateur", "annulee": False},
        {"id_evenement": "compte-2026-01-04T14:05:00+00:00", "heure": "15:05", "todo_id": None, "sorte": "bascule",  # todo #534
         "titre": "Bascule de compte A → B (session 5 h de A à 92 % (seuil 90 %), B à 10 %)", "sha": None, "by": "auto:quota",
         "annulee": False, "retour": "a"},
        {"id_evenement": 9002, "heure": "18:40", "todo_id": 902, "titre": "Todo d'exemple close seule", "sorte": "clôture",
         "sha": None, "by": "auto:verifie", "annulee": False}],
        "remises": [{"compte": "a", "heure": "09:00", "reel": 93.0, "reserve_avant": 25, "reserve_apres": 20, "apprentissage": True,  # todo #534
                     "texte": "Remise à zéro de A : prévu 100 %, réel 93 %, réserve 25 % → 20 %"}]},
    "habitudes": {"habitudes": [_HABITUDE, _HABITUDE_A_COMPLETER]},  # todo #429: `habitudes liste` (fake)
    "habitude-voir": _HABITUDE,
    "routines": {"proposees": [_ROUTINE], "actives": []},  # todo #503: `routine liste` (fake)
    "routine-voir": _ROUTINE,
    "todo-fiche": {**_TACHES[0], "notes": "Fausses notes pour l'aperçu.", "source": "dashboard",
                   "created_at": "2026-01-01T09:00:00+00:00", "done_at": None, "secret": "jamais montré",
                   "history": [{"at": "2026-01-01T09:00:00+00:00", "action": "created", "details": {}},
                               {"at": "2026-01-01T10:00:00+00:00", "action": "edit", "details": {"title": {}}}],
                   "branch": {"branch": "todo-901/exemple", "ahead": 2}},
    "commits": {"todo_id": 901, "branch": "todo-901/exemple", "ahead": 2, "unique": True,
                "commits": ["Ajoute l'exemple (todo #901)", "Corrige l'exemple (todo #901)"], "branches": ["todo-901/exemple"]},
    "video-lot-voir": {"id": "20261019T120000Z-0a1b2c3d", "empreinte": "0123456789abcdef", "faites": [], "actions": [  # todo #477
        {"n": 1, "type": "fiche", "titre": "Sourcing en déstockage (exemple)", "texte": "📚 fiche : Sourcing en déstockage (exemple)"},
        {"n": 2, "type": "todo", "titre": "Lister 5 grossistes (exemple)", "texte": "➕ todo : Lister 5 grossistes (exemple)"}]},
    "lot": {"empreinte": "0123456789abcdef", "ids": [901, 904], "lot": [
        {"todo_id": 901, "title": "Branche d'exemple prête à fusionner", "ahead": 2, "files": 3, "apres": [], "actions_error": None,
         "repos": [{"repo": "ultron-hub", "branch": "todo-901/exemple", "label": "todo-901/exemple", "ahead": 2, "files": 3}],
         "actions": [{"n": 1, "action": "user_unit_restart", "args": ["ultron-hub-dashboard.service"], "plan": "lancée",
                      "reason": "recharger le dashboard (exemple)", "error": None, "status": "pending",
                      "command": ["systemctl --user restart ultron-hub-dashboard.service"]}]},
        {"todo_id": 904, "title": "Deuxième branche d'exemple", "ahead": 3, "files": 4, "apres": [901], "actions_error": None,
         "repos": [{"repo": "ultron-hub", "branch": "todo-904/suite", "label": "todo-904/suite", "ahead": 3, "files": 4}], "actions": []}],
        "ecartees": [{"todo_id": 905, "title": "Branche pas prête", "raison": "worktree pas propre (exemple)"}]},
    "faits-du-jour": {"tasks": [  # todo #270: « Fait aujourd'hui » of the briefing (fake)
        {"id": 905, "title": "Sauvegarde de la nuit vérifiée (exemple)", "done_at": "2026-01-01T14:12:00+00:00"},
        {"id": 906, "title": "Résumé de la vidéo sur les agents (exemple)", "done_at": "2026-01-01T11:40:00+00:00"}]},
    "comptes": {"actif": "a", "actif_adresse": "alice.pro@exemple.fr", "autre_adresse": "alice.perso@exemple.fr", "comment": "identique à la copie", "alerte": None, "autre": "b", "derniere_bascule": None,
                "comptes": [
                    {"nom": "a", "enregistre": True, "actif": True, "adresse": "alice.pro@exemple.fr", "enregistre_a": "2026-01-01T09:00:00+00:00", "quota_erreur": None, "quota_attente": False,
                     "quota": {"stale": False, "session": {"percent": 42.0, "resets_at": "2026-01-01T15:00:00+00:00"},
                               "week": {"percent": 77.0, "resets_at": "2026-01-05T08:00:00+00:00"}}},
                    {"nom": "b", "enregistre": True, "actif": False, "adresse": "alice.perso@exemple.fr", "enregistre_a": "2026-01-01T09:05:00+00:00",
                     "quota_erreur": "profil : erreur HTTP 401 (exemple)", "quota_attente": False, "quota": None}],
                "a_reconnecter": [{"nom": "b", "adresse": "alice.perso@exemple.fr", "raison": "quota illisible (profil : erreur HTTP 401 (exemple))"}]},  # todo #336
    "budget": {"limite": 100.0, "dose_texte": "Aujourd'hui : A 7 points, B 11 points", "at": "2026-01-01T12:00:00+00:00", "conseil": "Épuise d'abord alice.pro@exemple.fr : sa semaine se remet à zéro dans 3 j.",
               "total": {"restant": 55.0, "budget_jour": 14.5, "depense_aujourdhui": 6.0},
               "comptes": [
                   {"nom": "a", "adresse": "alice.pro@exemple.fr", "connu": True, "veille": False, "lu_a": "2026-01-01T11:55:00+00:00", "pourcent": 77.0,
                    "restant": 11.0, "remise_a_zero": "2026-01-05T08:00:00+00:00", "heures_restantes": 68.0, "jours_restants": 3, "budget_jour": 3.7, "dose": 7,
                    "depense_aujourdhui": 6.0, "remise_passee": False},
                   {"nom": "b", "adresse": "alice.perso@exemple.fr", "connu": True, "veille": True, "lu_a": "2026-01-01T07:10:00+00:00", "pourcent": 44.0,
                    "restant": 44.0, "remise_a_zero": "2026-01-07T08:00:00+00:00", "heures_restantes": 116.0, "jours_restants": 5, "budget_jour": 8.8, "dose": 11,
                    "depense_aujourdhui": 0.0, "remise_passee": False}]},
    "planning": {"jour": "2026-01-01", "seuil": 90, "seuil_source": "défaut", "pause": False, "pause_a": None,  # todo #534 (fake)
                 "case": {"jour": "2026-01-01", "reserve_a": None, "reserve_b": 40, "seuil": None, "a": "2026-01-01T08:00:00+00:00"},
                 "texte": "A : 21 points = auto 16, à toi 5 · B : 12 points = auto 7, à toi 5",
                 "comptes": [
                     {"nom": "a", "dose": 21, "reserve": 5, "auto": 16, "reserve_pct": 25, "source": "défaut", "depense": 6.0,
                      "auto_permise": 16.0, "auto_epuisee": False, "fin_rapide": False, "apprentissage": False, "jours_complets": 9,
                      "texte": "A : 21 points = auto 16, à toi 5"},
                     {"nom": "b", "dose": 12, "reserve": 5, "auto": 7, "reserve_pct": 40, "source": "case", "depense": 0.0,
                      "auto_permise": 7.0, "auto_epuisee": False, "fin_rapide": False, "apprentissage": False, "jours_complets": 4,
                      "texte": "B : 12 points = auto 7, à toi 5"}]},
    "apres": {"todo_id": 901, "requests": [{"n": 1, "status": "pending", "line": "redémarrer ultron-hub-dashboard.service",
                                            "text": "Commandes exactes (exemple) :\nsystemctl --user restart ultron-hub-dashboard.service"}]},
    "notifs": {"non_lues": 1, "notifs": [  # todo #276, #289 (the fil, fake): an image, a file, a long text, an old row without attachments
        {"at": "2026-01-01T10:05:00+00:00", "titre": "Capture de l'écran Fil (exemple)", "corps": "Voici la capture demandée.",
         "complet": "Voici la capture demandée.\nElle montre l'écran Fil avec une pièce jointe image.\nPage à ouvrir : https://claude.ai/artifact/exemple-123.\nAncien lien http://exemple.fr/reste-du-texte\nCommit 3f9a1c2 sur todo-901/exemple\n3306 passed, rc=0\nRapport : {{HOME}}/workspace/exemple/rapport.md", "onglet": "briefing", "lue": False,
         "pj": [{"nom": "20260101T100500-abc123-capture.png", "orig": "capture.png", "genre": "image",
                 "url": "/api/pj/20260101T100500-abc123-capture.png"}]},
        {"at": "2026-01-01T09:55:00+00:00", "titre": "📎 Photo reçue : 20260101T095500-abc123-capture.png", "corps": "Gardée sur le serveur.",
         "complet": "Gardée sur le serveur.", "onglet": "briefing", "lue": True, "origine": "toi",  # todo #353: sent by the user
         "pj": [{"nom": "20260101T100500-abc123-capture.png", "orig": "capture.png", "genre": "image",
                 "url": "/api/pj/20260101T100500-abc123-capture.png"}]},
        {"at": "2026-01-01T09:40:00+00:00", "titre": "Bilan de la journée (exemple)", "corps": "3 todos livrées, 1 question.",
         "complet": "3 todos livrées, 1 question.\nLa todo #901 attend ton accord.\nLe rapport complet est en pièce jointe.",
         "onglet": "travail", "lue": True, "cible": {"genre": "todo", "ref": "901", "libelle": "Voir la todo #901"}, "todo": "901",
         "pj": [{"nom": "20260101T094000-def456-bilan.pdf", "orig": "bilan.pdf", "genre": "fichier",
                 "url": "/api/pj/20260101T094000-def456-bilan.pdf"},
                {"nom": "20260101T094001-aaa111-notes.md", "orig": "notes.md", "genre": "texte", "url": "/api/pj/20260101T094001-aaa111-notes.md"},
                {"nom": "20260101T094002-bbb222-verset-2-255.html", "orig": "verset-2-255.html", "genre": "html",
                 "url": "/api/pj/20260101T094002-bbb222-verset-2-255.html"},
                {"nom": "20260101T094003-ccc333-memo.mp3", "orig": "memo.mp3", "genre": "audio", "url": "/api/pj/20260101T094003-ccc333-memo.mp3"}]},
        # todo #347: two steps of the same todo on the same day = ONE line « · 2 étapes » (key `todo` served by the server)
        *({"at": f"2026-01-01T09:{m}:00+00:00", "titre": t, "corps": c, "complet": c, "pj": [], "onglet": "travail", "lue": True,
           "cible": {"genre": "todo", "ref": "902", "libelle": "Voir la todo #902"}, "todo": "902"}
          for m, t, c in (("20", "Todo #902 : tests verts (exemple)", "Les tests passent."), ("10", "Todo #902 : branche créée (exemple)", "Branche prête."))),
        # todo #466: three repeated rows in a row (folded into one « 3× » line of the Fil)
        *({"at": f"2026-01-01T0{h}:15:00+00:00", "titre": f"main a avancé (todo #90{h}, exemple)", "corps": "Fusion automatique.",
           "complet": "Fusion automatique.", "pj": [], "onglet": "briefing", "lue": True} for h in (8, 7, 6)),
        {"at": "2025-12-31T18:40:00+00:00", "titre": "Question de l'agent (exemple)", "corps": "Quelle branche garder ?",
         "complet": "Quelle branche garder ?", "pj": [], "onglet": "briefing", "lue": True, "genre": "question"}]},
    # -- todo #264: a fake ultron session (bubbles, tool lines, a running sub-agent) and the quota --
    "sessions": {"sessions": [  # todo #281: chantier, sub-agents, working, waiting (the ages follow the clock)
        {"nom": "ultron-19-10-09h12", "cree": _MAINTENANT - 26 * 3600, "ouverte": False, "chantier": None, "sous_agents": 0,
         "attend": False, "travaille": False},
        {"nom": "ultron-19-10-15h38", "cree": _MAINTENANT - 7800, "ouverte": True, "chantier": None, "sous_agents": 1,
         "attend": False, "travaille": True, "contexte": 62,
         "agents": [{"nom": "Agent-901 exemple", "modele": "sonnet",
                    "depuis": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(_MAINTENANT - 540))}],
         "derniere_phrase": "Je lance un second agent pour la suite (fausse donnée).",
         "declare": {"maintenant": "Écrit les tests (fausse donnée)", "ensuite": "Fusionner la branche", "attend": None,
                     "bloque": None, "at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(_MAINTENANT - 300))}},
        {"nom": "ultron-exemple-un-19-10-16h02", "cree": _MAINTENANT - 2100, "ouverte": False, "chantier": "exemple-un",
         "sous_agents": 0, "attend": True, "travaille": False, "contexte": 31, "agents": [],
         "derniere_phrase": "On fusionne la première branche maintenant ?", "declare": None}],
        "chantiers": ["exemple-un", "exemple-deux", "exemple-trois"]},  # todo #297: choice list of « Nouvelle session »
    "session-fermeture": {"session": "ultron-19-10-09h12", "sous_agents": 0, "travail_non_commite": ["todo-901-exemple"]},
    "session": {"session": "ultron-19-10-15h38", "total": 7, "tronque": False, "maj": "2026-01-01T10:05:00+00:00",
                "sous_agents_finis": 1, "chemin": "/jamais/montré",
                # todo #283 (Pilote): the bar (age, context) and a question in the journal -> quick replies Oui / Non / recos
                "cree": _MAINTENANT - 7800, "chantier": None, "contexte": 62, "attend": True, "travaille": False, "menu": [],
                "sous_agents": [{"description": "Agent-901 exemple (Sonnet)", "model": "sonnet", "depuis": "2026-01-01T10:02:00+00:00"}],
                "entrees": [
                    {"k": "utilisateur", "ts": "2026-01-01T10:00:00+00:00", "texte": "Regarde l'état des branches (exemple)."},
                    {"k": "outil", "ts": "2026-01-01T10:00:05+00:00", "outil": "Bash", "texte": "Liste les branches todo-* du dépôt"},
                    {"k": "reponse", "ts": "2026-01-01T10:00:20+00:00", "texte": "Deux branches sont prêtes à fusionner (fausse donnée)."},
                    {"k": "agent_lance", "ts": "2026-01-01T10:01:00+00:00", "texte": "Agent-900 exemple (Haiku)", "modele": "haiku"},
                    {"k": "agent_fini", "ts": "2026-01-01T10:01:30+00:00", "texte": "Agent-900 exemple (Haiku)", "modele": "haiku"},
                    {"k": "agent_lance", "ts": "2026-01-01T10:02:00+00:00", "texte": "Agent-901 exemple (Sonnet)", "modele": "sonnet"},
                    {"k": "reponse", "ts": "2026-01-01T10:05:00+00:00",
                     "texte": "Je lance un second agent pour la suite.\n\nOn fusionne la première branche maintenant ?"}]},
    # todo #283: the raw tmux screen (wider than the phone: horizontal scroll) with a permission menu at the bottom
    "session-ecran": {"session": "ultron-19-10-15h38", "largeur": 120, "hauteur": 30, "attend": False, "pid": "jamais montré",
                      "lignes": _ECRAN, "menu": [{"touche": "1", "libelle": "Yes"},
                                                  {"touche": "2", "libelle": "Yes, and don't ask again for git commands"},
                                                  {"touche": "3", "libelle": "No, and tell Claude what to do differently (esc)"}]},
    "conseil": {"chantier": "exemple-un", "raison": "3 todos prêtes, c'est de l'usage réel", "regle": "b-usage"},  # todo #364
    "usage": {"session": {"percent": 42, "resets_at": "2026-01-01T15:00:00+00:00"},
              "week": {"percent": 77, "resets_at": "2026-01-05T08:00:00+00:00"}, "stale": False, "text": "jamais montré"},
    # ⋯ Plus (todo #249)
    "profil": {"profil": "# Profil (exemple)\n- Parle français.\n- Préfère les réponses courtes.\n", "pending": [
        {"id": 3, "date": "2026-01-01T09:00:00+01:00", "source": "agent:hermes", "texte": "Préfère le thé à la menthe (exemple)",
         "statut": "en_attente"},
        {"id": 4, "date": "2026-01-02T18:30:00+01:00", "source": "video:exemple", "texte": "Travaille le soir (exemple)",
         "statut": "en_attente", "invisible_retire": 2}]},
    "focus": {"focus": {"domaines": ["systeme", "business"], "depuis": "2026-01-01T09:00:00+01:00", "par": "telegram-button"},
              "line": "🧭 Focus de la semaine : 🛠 Système + 💼 Business (depuis le 01/01, exemple)",
              "domaines": [{"cle": "systeme", "icone": "🛠", "titre": "Système"}, {"cle": "business", "icone": "💼", "titre": "Business"},
                           {"cle": "perso", "icone": "🕌", "titre": "Perso"}]},
    "cap": {"exists": True, "date": "2026-01-04", "text": "🔭 Revue de cap (exemple)\nLa semaine a surtout servi le système.",
            "corrections": [{"titre": "Relancer le chantier exemple", "chantier": "exemple-un", "notes": "Fausse note.",
                             "preuve": "aucune todo fermée depuis 7 jours (exemple)"}]},
    "souvenirs": {"refreshed_in_s": None, "results": [
        {"session": "s-exemple", "outil": "claude-code", "date": "2026-01-03", "extrait": "… la facture du fournisseur (exemple) …"}]},
    # -- todo #247: the detailed health (fake diagnosis, guard and battery) --
    "sante-reflexes": {"diagnostics": [
        {"nom": "verrou-restic", "diagnostic": "Verrou de sauvegarde périmé (exemple)", "detail": "verrou abcd de 9.0 h (exemple)",
         "action": "🔓 Retirer le verrou (bouton dans /sante)",
         "bouton": {"label": "🔓 Retirer le verrou", "cible": "verrou-restic:0123456789ab", "action": "jamais montré",
                    "args": ["6", "0123456789ab"]}}]},
    "sante-garde": {"ok": True, "problem": None, "enabled_in_config": True, "plugin_file": True},
    "sante-batterie": {"text": "🔌 Secteur : branché\n🔋 Batterie : 87 % (en charge, exemple)\n❤️ Santé : 91 % (exemple)", "percent": 87},
    "etat-fusions": {
        "en_cours": [{"todo_id": 901, "title": "Branche d'exemple prête à fusionner", "branches": [
            {"repo": "ultron-hub", "branch": "todo-901/exemple", "label": "todo-901/exemple", "ahead": 2,
             "commit": {"sha": "0123456789ab", "date": "2026-01-01T09:30:00+01:00", "subject": "Ajoute l'exemple (todo #901)"},
             "worktree": {"path": "/exemple/ultron-hub.worktrees/todo-901", "propre": True}}]}],
        "attente": [{"todo_id": 904, "title": "Branche d'exemple sensible", "branches": [
            {"repo": "ultron-hub", "branch": "todo-904/suite", "label": "todo-904/suite", "ahead": 1,
             "commit": {"sha": "89abcdef0123", "date": "2026-01-01T08:00:00+01:00", "subject": "Touche au déploiement (exemple)"},
             "worktree": {"path": "/exemple/ultron-hub.worktrees/todo-904", "propre": False}}],
            "raisons": [{"step": "auto-sensitive", "reason": "fusion automatique refusée : la branche touche un fichier sensible",
                         "files": ["deploy/user/apres-fusion/todo-904.json"]}]}],
        "auto": [{"todo_id": 902, "title": "Fusion automatique d'exemple", "at": 1767258000.0, "cible": "902-0123456789ab",
                  "sha": "0123456789ab", "annulee": False, "a_valider": True},
                 {"todo_id": 903, "title": "Ancienne fusion annulée (exemple)", "at": 1767171600.0, "cible": "903-ba9876543210",
                  "sha": "ba9876543210", "annulee": True, "a_valider": False}],
        "annulation": {"cible": "902-0123456789ab", "annulee": False},
        # todo #495: no advance authorisation yet, so the card shows the durations and the sign button
        "autorisation": {"valide": False, "jusqu_au": None, "by": None, "perimetre": "0123456789abcdef", "jours": [1, 3, 7, 14],
                         "fins": [{"jours": 1, "jusqu_au": "2026-01-01"}, {"jours": 3, "jusqu_au": "2026-01-03"},
                                  {"jours": 7, "jusqu_au": "2026-01-07"}, {"jours": 14, "jusqu_au": "2026-01-14"}]}},
    # -- todo #267: a lot merging in the background, second todo under test --
    "suivi": {"suivi": "20261019-120000-0a1b2c3d", "action": "fusion-lot", "fini": False, "depuis_s": 74, "resultat": None,
              "etapes": [{"nom": "Vérification", "etat": "fait", "detail": ""},
                         {"nom": "Tests", "etat": "en_cours", "detail": "#904 (2/2)"},
                         {"nom": "Fusion", "etat": "a_venir", "detail": ""},
                         {"nom": "Actions après fusion / redémarrage", "etat": "a_venir", "detail": ""},
                         {"nom": "Terminé", "etat": "a_venir", "detail": ""}], "job_interne": "jamais montré"},
    "fusion-auto": {"cible": "901-0123456789ab", "annulee": False,
                    "fusion": {"todo_id": 901, "at": 1767258000.0, "by": "auto:orchestrateur",
                               "repos": [{"repo": "ultron-hub", "branch": "todo-901/exemple", "sha": "0123456789ab" + "0" * 28}]}},
}

for _n in LECTURES["notifs"]["notifs"]:  # todo #351: the fake rows get the same split as `notifs.liste` (readable text + technical drawer)
    _n["complet"], _n["details"] = notifs.separer_technique(_n["complet"])


class _Caller:
    """Replaces the Tailscale gate: the loopback address is the only caller."""

    def caller(self, ip: str) -> str | None:
        return "aperçu" if ip == LOOPBACK else None


class PreviewActions(actions.Actions):
    """The confirmation sheet opens (step 1 runs); confirming is refused, nothing is signed or launched."""

    def _execute(self, p: actions.Pending) -> dict[str, Any]:
        action = catalogue.PAR_ID[p.action]
        if action.type == "lecture" and p.action in LECTURES:  # fake data, through the same whitelist as the real one
            return {"ok": True, "resume": "aperçu", "erreur": None,
                    "donnees": catalogue.projeter(action.donnees, LECTURES[p.action])}
        raise actions.Refused("aperçu : actions désactivées, rien n'a été lancé")

    def _lire_detail(self, reading: catalogue.Action, args: dict[str, str]) -> Any:
        return LECTURES.get(reading.id)


def _no_key() -> bytes:
    raise actions.Refused(NO_KEY)


def _fiche(sujet: str) -> dict[str, Any]:
    return {**FICHE, "sujet": sujet if sujet != "-" else FICHE["sujet"]}


def _png() -> bytes:
    """A small valid PNG (a green gradient 240x120) for the fil's image preview."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\x00" + b"".join(bytes((20, 60 + x * 120 // 240 + y // 4, 80)) for x in range(240)) for y in range(120))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 240, 120, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


@contextlib.contextmanager
def fake_data() -> Iterator[None]:
    """Swap the blocks and the sheet for the fixtures, then put the real ones back (tests, in-process captures)."""
    saved_blocks, saved_fiche = dict(views.BLOCKS), views.fiche
    views.clear_cache()
    views.BLOCKS.update({name: (lambda data=data: data) for name, data in FIXTURES.items()})
    views.fiche = _fiche  # type: ignore[assignment]
    saved_dossier = notifs.dossier
    with tempfile.TemporaryDirectory() as tmp:  # the fil's attachments are served from a fake var/notifs/pj/
        fake = Path(tmp) / "notifs"
        (fake / notifs.PJ_DIR).mkdir(parents=True)
        (fake / notifs.PJ_DIR / "20260101T100500-abc123-capture.png").write_bytes(_png())
        (fake / notifs.PJ_DIR / "20260101T094000-def456-bilan.pdf").write_bytes(b"%PDF-1.4 (exemple)")
        (fake / notifs.PJ_DIR / "20260101T094001-aaa111-notes.md").write_text(
            "# Notes (exemple)\n\nUn **point** important et `du code`.\n\n- premier\n- deuxième <b>pas du html</b>\n\n```\nbloc de code\n```\n", encoding="utf-8")
        (fake / notifs.PJ_DIR / "20260101T094002-bbb222-verset-2-255.html").write_text(
            "<!doctype html><meta charset=utf-8><body style='background:#111;color:#eee'><h1>Verset 2:255</h1><script>document.title='ok'</script>", encoding="utf-8")
        (fake / notifs.PJ_DIR / "20260101T094003-ccc333-memo.mp3").write_bytes(b"ID3" + bytes(range(256)))
        notifs.dossier = lambda settings=None: fake  # type: ignore[assignment]
        try:
            yield
        finally:
            notifs.dossier = saved_dossier  # type: ignore[assignment]
            views.BLOCKS.clear()
            views.BLOCKS.update(saved_blocks)
            views.fiche = saved_fiche  # type: ignore[assignment]
            views.clear_cache()


def make_server(port: int = 0) -> server.Server:
    """Bound to 127.0.0.1 only (port 0 = a free one). Call inside `fake_data()`."""
    acts = PreviewActions(cli=["false"], key=_no_key)
    srv = server.Server((LOOPBACK, port), _Caller(), (), acts)  # type: ignore[arg-type]
    fake = Path(tempfile.gettempdir()) / f"ultron-apercu-recus-{os.getuid()}"
    srv.recus_dir = lambda: fake  # 📎 joindre (todo #293): never the real var/recus from a preview
    return srv


@contextlib.contextmanager
def running(port: int = 0) -> Iterator[str]:
    """Serve in a thread and yield the base URL (the captures use this)."""
    with fake_data():
        srv = make_server(port)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://{LOOPBACK}:{srv.server_address[1]}"
        finally:
            srv.shutdown()
            srv.server_close()


def serve(port: int) -> None:
    with fake_data():
        srv = make_server(port)
        try:
            srv.serve_forever()
        finally:
            srv.server_close()

