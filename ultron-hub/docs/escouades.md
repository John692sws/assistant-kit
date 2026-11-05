# Prompts de chantier et sessions interactives (ex-escouades autonomes)

L'escouade autonome (`escouade run`, moteur, garde, limites, enchaînement, boîte de questions, Telegram) est **retirée** (todo #458,
désactivée depuis le 20/10) ; le détail est dans l'historique git avant ce commit. Restent les prompts et les sessions interactives.

Référence détaillée, sortie de CLAUDE.md le 18/10/2026 (todo #167).

## Prompts de chantier (todo #95)
- `ultron-hub escouade prompt <sujet> --limite "3 h" [--fiche P] [--drapeau P] [--sortie F]` assemble `~/notes/prompt/<sujet>.md` (en-tête, 1, 4) avec le socle `~/notes/prompt/_socle.md` (règles communes : 3, 5) et un mode (`_mode-interactif.md` pour les sessions ; `_mode-escouade.md`, hors dépôt, n'est plus utilisé) ; ordre : 2, 5, 3, en-tête et 1, 4, 6, 7 et remplit `{sujet}`, `{limite}`, `{fiche}`, `{drapeau}`, `{dossier}` (dossier des prompts, absolu ; `escouade/core.py`, `assemble` pure) ; `escouade sujets` liste les sujets. Dossier surchargeable par `--dossier` ou `ULTRON_PROMPTS_DIR`.

- `{jeton}` (todo #107) : 8 caractères aléatoires (`core.new_jeton`, `secrets.token_hex(4)`), tiré à chaque assemblage du prompt. Il marque
  le cadre « [MESSAGE DE L'UTILISATEUR {jeton}] » des messages de l'utilisateur ; `escouade prompt` en tire un nouveau.
- Socle unique (todo #229, `~/notes/spec-socle-unique.md` §3-4) : `_socle.md` ne garde que le commun (sécurité, modèles, sous-agents,
  vérification d'une branche, journal), une raison courte par règle ; `_mode-escouade.md` porte la limite `{limite}`, les outils
  `demander`/`signaler`, le jeton et la passation. Les fichiers commençant par `_` ne sont jamais des sujets. Copies de référence
  versionnées : `deploy/escouade/_socle.md` et `deploy/escouade/_mode-escouade.md` (à poser dans `~/notes/prompt/` à la fusion ;
  test `tests/test_escouade_socle.py` : doublons, marqueurs, équivalence avec `_socle.md.avant-229`).

## Prompt interactif (todo #230)
- `ultron-hub prompt interactif [<sujet>] [--sortie F] [--dossier D]` écrit `var/prompts/interactif.md` (script seul, aucune IA) :
  `_mode-interactif.md` §2 (arrêts : aucun seuil de quota par défaut, jauge de contexte 60 %, consignes en vigueur), `_socle.md` §5 puis §3,
  `_mode-interactif.md` §8 (économie, garde-fous), l'orchestrateur réduit (`demarrage/orchestrateur-ultron.md` : §1 mission, §4 ordre du
  travail), la fiche du sujet si donnée (en-tête, 1, 4), `_mode-interactif.md` §6 (questions en direct, Telegram à chaque nouveauté) et §7
  (bilan « En clair », `ultron relance`), puis **la Reprise en dernier** (`reprise-orchestrateur.md`, jamais remplie de marqueurs).
  `core.assemble_interactif` est pure ; `core.build_interactif` lit le dossier des prompts. Pas de ligne en double entre le socle
  et un mode (test `tests/test_prompt_interactif.py`).
- **Mode noyau (todo #358)** : si `~/notes/prompt/_noyau.md` existe, `build_interactif` n'assemble plus socle + mode + orchestrateur
  entiers mais : le noyau (rempli : `{fiche}`, `{reprise_fichier}`, `{reprise_archiver}`, `{sujet}`), l'en-tête et la section 1
  « Mission » de l'orchestrateur (pour un chantier : `chantier_orchestrateur`), les parties du fichier de sujet, la consigne de
  Reprise du chantier, puis la Reprise **réduite à son en-tête et à son entrée datée la plus récente** (`core.derniere_entree` ;
  un fichier sans entrée datée reste tel quel). Sans `_noyau.md`, l'ancien assemblage est inchangé. Un noyau de plus de
  5000 octets est refusé. Les fiches `~/notes/prompt/fiches/*.md` ne sont jamais injectées (lues à la demande).
- `ultron-hub prompt mesure [<sujet>] [--dossier D]` (lecture seule) : octets de chaque bloc (noyau, mission, reprise, total) et de
  chaque fiche ; `ok: false` + `raison` si le noyau dépasse 5000 octets ou une fiche 4000. Vérifie aussi les renvois du noyau (`renvois`) : chaque `fiches/x.md` cité doit exister dans `<prompts>/fiches/`,
  chaque skill citée (en tête d'élément dans la section « skill ») doit avoir `~/.claude/skills/<nom>/SKILL.md`, sinon `ok: false`.
- `ultron-hub prompt essai <chantier> [--scenario NOM] [--essai-a-blanc]` (todo #232) rejoue les scénarios de `config/prompt-essais.yaml`
  (situation, `attendu` : au moins un mot présent, `interdit` : aucun présent) sur le prompt assemblé du chantier. Chaque scénario
  passe par la tâche `prompt-essai` (sonnet, sans outil) ; comparaison par script, sans IA juge. Sortie : par scénario `ok`,
  `manquants`, `interdits_trouves`, `cout_usd`, et le coût total. Il consomme du quota : `--essai-a-blanc` n'appelle rien et
  rend le nombre de scénarios et la taille du prompt. Tests : `tests/test_prompt_essai.py`.
- `esprit reprise-archiver` lit `~/notes/prompt/reprise-orchestrateur.md` s'il existe, sinon l'ancien orchestrateur (`reprise.current`) ;
  il accepte les titres `## 8. Reprise` et `## Reprise`. `reprise-orchestrateur.md` n'est pas un sujet d'escouade (`core.REPRISE_NAME`).
  Plafonds du ménage (vérif. 19) : `_mode-interactif.md` 8 Ko, `reprise-orchestrateur.md` 12 Ko, avec le geste `reprise-archiver` ;
  un fichier couvert par plusieurs motifs garde le plafond le plus bas et n'est signalé qu'une fois.
- Pose : `deploy/prompt/poser.sh [--essai]` (après la fusion de #229 et #230 ; idempotent, testé sur un HOME temporaire) copie
  `_socle.md`, `_mode-escouade.md`, `_mode-interactif.md` et l'orchestrateur réduit dans `~/notes/prompt/` (sauvegarde `.avant-230`,
  la première seulement), extrait la Reprise (§8) de l'orchestrateur actuel vers `reprise-orchestrateur.md` (sans perte, jamais
  écrasée ensuite) et modifie `~/.local/bin/ultron` : il lance `ultron-hub prompt interactif` puis envoie « Lis
  <projet>/var/prompts/interactif.md et applique-le » (chemin absolu, la session s'ouvre dans `$HOME`) ; si l'assemblage
  échoue, l'ancien message reste le recours.

## Session de chantier : `ultron <chantier>` (todo #284)
- La commande `ultron` est versionnée : `deploy/bin/ultron` (source unique). Pose après 🔀 : `deploy/bin/poser.sh [--essai]`
  (copie dans `~/.local/bin/ultron`, sauvegarde `.avant-284` la première fois, idempotente, finit par un `ultron --essai`).
- `ultron <mot>` avec UN seul mot qui est un chantier connu (`~/notes/prompt/<mot>.md`, mêmes règles que `core.check_name`,
  donc la liste de `chantier tableau`) ouvre la session tmux `ultron-<chantier>-JJ-MM-HHhMM` exactement comme la session
  système (Claude interactif, `--permission-mode auto` depuis la todo #494, Opus, shell après la fin de Claude). Sinon (mot inconnu,
  plusieurs mots) : consigne du jour de la session système, comme avant. `ultron` seul : inchangé.
- Prompt : `ultron-hub prompt interactif <chantier>` → `var/prompts/interactif-<chantier>.md` (un fichier par chantier, deux
  sessions ne s'écrasent pas) = socle + mode interactif + orchestrateur + fiche du chantier + consigne « où écrire la Reprise »
  + **Reprise du chantier** `~/notes/prompt/reprises/<chantier>.md` (créée avec son titre si absente, jamais écrasée ; la Reprise
  de l'orchestrateur n'est ni lue ni exigée). Échec d'assemblage = rien n'est ouvert (pas de recours : ce ne serait plus une
  session de chantier).
- Ciblage (todo #287) : le prompt d'un chantier garde la partie orchestrateur (délégation, branches, vérifs, fusion automatique,
  passation : socle + mode interactif) mais l'en-tête, la §1 (mission = fiche `~/notes/chantier-<chantier>.md`, pas de travail hors
  chantier) et la §4 (ordre du travail = `todo list --chantier <chantier>` seulement) sont ceux de `core.chantier_orchestrateur`,
  pas de `orchestrateur-ultron.md`. La seule Reprise est `reprises/<chantier>.md` ; la « ## REPRISE » de la fiche est gelée (escouades
  en pause). `_mode-interactif.md` §7 utilise `{reprise_fichier}` et `{reprise_archiver}` : `reprise-orchestrateur.md` et
  `reprise-archiver` sans option pour la session système (sortie inchangée), le fichier du chantier et `--chantier` pour un chantier.
  Le fichier du dépôt porte aussi la règle « Fusion automatique » (#254), qui n'existait que dans la copie en service. Pose :
  `deploy/prompt/poser.sh` (copie `_mode-interactif.md` vers `~/notes/prompt/`, sauvegarde `.avant-230` déjà prise).
- Passation : la session met à jour SA Reprise puis `ultron-hub esprit reprise-archiver --chantier <chantier>` (archive
  `~/notes/prompt/archives/reprises-<chantier>-AAAA-MM.md`). Le ménage (#231) archive aussi `~/notes/prompt/reprises/*.md`
  au-delà de 12 Ko (`budgets.archiver_seul` accepte les motifs glob).
- Todo #302 : le lanceur répond lui-même à la question de confiance du dossier (« Yes, I trust this folder » : Bas puis Entrée, une fois,
  seulement si cette ligne est à l'écran ; boucle détachée bornée, jamais d'autre touche) ; `ULTRON_CONFIANCE_ESSAIS`/`_PAUSE` règlent la boucle.
- `ultron relance` depuis une session de chantier rouvre le même chantier (refus s'il n'est plus connu) ; le 🔁 du dashboard
  (`sessions.gestion.relancer`) fait de même et ferme la session ouverte par erreur si le chantier a disparu.
- `ultron liste`, le dashboard (`journal.list_sessions`, champ `chantier`) et l'écriture dans une session acceptent les deux
  formes de nom (`journal.NAME_RE`, `journal.chantier_of`). `ultron --essai …` dit la session et le message prévus sans rien
  ouvrir (le prompt est assemblé). Les escouades `claude -p` ne changent pas.

## Ce qui reste du code `escouade/`
- `core.py` : assemblage des prompts, sujets, noms, Reprise ; `interactif.py` : `prompt interactif` / `prompt essai` ;
  `commands.py` : `escouade prompt` et `escouade sujets` seulement (plus de `run`, `stop`, `passation`, `garde`, `etat`,
  `nouveau`, `questions`, `question-envoyee`, `repondre`, `remarque`).
- `hooks.py` (Stop/PostToolUse/PreToolUse) et `mcp_ultron.py` (outils `demander`/`signaler`) servent encore les sessions
  interactives et la jauge de contexte ; ils lisent un dossier d'état (`etat.json`, drapeaux) quand il existe.
- Coûts d'anciennes escouades : `cout compare` lit encore `~/.local/state/ultron-hub/escouades` (`cout/usage_jsonl.py`).
- Plugin Telegram : les écrans de l'escouade autonome (lancer, limites, arrêt, questions poussées, réponses, remarques) sont retirés ;
  `/escouade` ne propose plus que ➕ Nouveau chantier et 🧩 Mission en équipe, 📥 À toi liste encore les questions sans écran de réponse.

## Chantier de nuit : `ultron nuit` (todo #478)
- `ultron nuit [--detache]` appelle `ultron-hub chantier nuit creer` (nom `nuit-JJ-JJ-MM` : jour, lendemain, mois du 1er jour ; refus
  si le prompt existe) puis ouvre la session comme `ultron <chantier>`. `--detache` n'attache ni ne bascule (lancement depuis un
  agent). `--essai` dit la session prévue sans rien créer.
- `creer` écrit d'un coup le prompt `~/notes/prompt/nuit-….md` (règles de la nuit : sous-agents max 3 dont un Opus, arrêt à 85 % de la
  semaine, un seul `send` = le bilan, passation à 60 % de contexte, jamais clore une todo…), la fiche `~/notes/chantier-nuit-….md`
  (todos ouvertes par chantier ; section « Déjà faites, à écarter » = livrées ou citées dans `git log --oneline -40 main`, log brut en
  preuve) et la Reprise. La fiche est construite après lecture de l'état, jamais d'avance.
- `chantier nuit fin <nom>` : refusé si une session `ultron-<nom>-…` est ouverte ; garde la Reprise dans
  `archives/reprise-<nom>.md` (pour juger la nuit), supprime prompt, fiche, `var/prompts/interactif-<nom>.md`, anciennes archives de Reprise.
- Démarrage bloqué par script : pour tout chantier `nuit-*`, la commande tmux commence par
  `ultron-hub chantier nuit attendre --session <soi>` (boucle tant qu'une autre session ultron travaille, 4 h au plus puis on démarre
  quand même, `temps_depasse`). Cause : panne du 29/10, nuit lancée à 23h23 pendant qu'une session finissait à 23h34.
