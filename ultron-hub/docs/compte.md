# Deux comptes Claude en quinconce (todo #263)

**Carte** : `src/ultron_hub/compte/store.py` (fichiers, identification, bascule), `compte/commands.py` (CLI). Tests :
`tests/test_compte.py` (chemins jetables de la fixture `comptes_paths` de `tests/conftest.py`, réseau remplacé).
Décision du 19/10 (✅ sécurité de l'utilisateur) : **une seule config Claude Code, échange du fichier** `~/.claude/.credentials.json`
(pas de `CLAUDE_CONFIG_DIR`, pas de double config).

## Commandes
- `ultron-hub compte enregistrer <a|b> [--remplacer]` (simple, non signée) : copie le fichier actif dans
  `~/.config/ultron-hub/comptes/<a|b>.json` (dossier 0700, remis à 0700 s'il est plus ouvert ; fichier 0600 ; écriture atomique :
  fichier temporaire 0600, fsync, `os.replace`). Refus : pas de fichier actif, pas de jeton OAuth, compte déjà enregistré sous
  l'autre lettre, emplacement déjà pris alors que le fichier actif n'est reconnu comme aucun compte : autre compte connu, OU
  compte non vérifiable (profil injoignable, jeton expiré, aucun identifiant noté pour l'emplacement) → refus, `--remplacer`
  obligatoire (revue Opus : jamais d'écrasement silencieux de la seule copie d'un compte). Un emplacement libre n'en demande pas.
  La copie est relue après écriture : SHA-256 différent des octets lus = refus « mal écrite » (comptes.json non modifié).
- `ultron-hub compte etat` (lecture) : compte actif et comment il est reconnu, quota de chaque compte enregistré (code de
  `usage/collect.py`, cache de 60 s par compte `var/cache/usage-compte-<a|b>.json`), date d'enregistrement, `autre` (la
  destination d'une bascule), `alerte`. Un jeton expiré n'est jamais renouvelé : `quota_attente` vrai et « quota lisible après
  la prochaine session Claude sur ce compte » (todo #330 : attendu, pas une panne, pas de 🔑).
- `ultron-hub compte basculer [--vers a|b] [--i-am-human]` (signée, action `compte` de `closing.ACTIONS`, cible = lettre de
  destination, acteur `dashboard:<appareil>` ou `cli-human`) : reconnaît le fichier actif (sinon refus), resauve ce fichier dans
  son emplacement s'il a changé (jetons renouvelés), revérifie que le fichier actif n'a pas bougé entre-temps (sinon refus
  « vient de changer »), met la copie de l'autre à sa place (atomique, 0600), **relit le fichier actif et compare son SHA-256 à
  celui de la copie** (différent = l'ancien fichier est remis et revérifié de même, refus « mal écrit », pas de
  `derniere_bascule` ; jeton déjà dépensé), note `derniere_bascule`, supprime les caches
  d'usage (`/usage` ne montre plus l'ancien compte), puis lit l'usage avec le nouveau jeton (`verification`, `verifie` noté dans
  `derniere_bascule`) : True lu, False échec (401, réseau, fichier illisible), None « à confirmer » (jeton d'accès de la copie
  expiré, le cas courant : `collect.TokenExpired`). `etat` passe None à True dès que le compte actif est la destination et que
  son quota se lit (todo #330). Refus aussi :
  autre compte pas enregistré, `--vers` qui n'est pas l'autre (carte périmée), A et B = même compte. Jeton déjà dépensé =
  refus (`preuves` de `comptes.json`, 200 dernières). Verrou `flock` sur `.verrou` : deux appuis ne basculent pas deux fois.

## Reconnaître le compte actif
Dans l'ordre, sans jamais afficher une valeur : 1) mêmes octets qu'une copie ; 2) même `refreshToken` (jeton d'accès renouvelé) ;
3) même compte derrière le jeton : `GET /api/oauth/profile` (non documenté, l'appel que fait Claude Code lui-même), dont on ne
garde que `sha256("ultron-hub-compte:" + account.uuid)[:16]` (jamais le jeton), noté à l'enregistrement et retenu par empreinte
du fichier (`identites`, 20 au plus : un appel réseau par nouveau fichier, pas par affichage). **Les échecs ne sont pas mis en
cache** : tant qu'un fichier reste non reconnu (profil injoignable, erreur HTTP, jeton expiré), chaque `compte etat` refait un
appel au profil (au plus un par minute depuis la carte du dashboard, qui ne relit qu'une fois par minute) ; un jeton expiré
n'en fait aucun (jamais renouvelé ici). Un échec passager se corrige donc seul au prochain affichage.
**Adresse (todo #339)** : l'adresse e-mail du profil est gardée dans `comptes.json` (0600, `comptes.<a|b>.adresse`) pour l'affichage (« compte A/B » devient l'adresse, repli sur la lettre si inconnue) ; jamais dans le journal commun ni les logs ; `etat` la remplit (un appel de profil par emplacement, jeton non expiré, sans jamais bloquer). D'après son format connu (non relu
ici : `claudeAiOauth` = jetons, échéance, portées, abonnement), `.credentials.json` ne porte pas d'identité stable, d'où le profil.

## Sessions déjà ouvertes (limite assumée)
- Une session Claude ouverte avant la bascule garde l'ancien jeton en mémoire : elle continue sur l'ancien compte jusqu'à sa
  relance (`ultron relance`, proposé par le message de sortie).
- Risque : en renouvelant son jeton, elle peut réécrire l'ANCIEN compte dans le fichier. Détection : `compte etat` dit
  « revenu à l'ancien compte X après la bascule vers Y » (reconnu par jeton ou profil) ou « fichier actif inconnu… changé depuis
  la bascule vers Y » (profil injoignable) ; la carte du dashboard l'affiche en rouge. Rien ne l'empêche, sauf relancer les sessions.
- Lu dans le binaire 2.1.303 (non vérifié en réel) : avant de renouveler, Claude Code relit `.credentials.json` si son mtime a
  changé ; une session ouverte devrait donc plutôt adopter le nouveau compte à son prochain renouvellement. La détection reste.
- Fenêtre de course : Claude Code écrit `.credentials.json` sans le verrou d'ultron-hub. Entre la revérification du fichier actif
  et `os.replace`, un renouvellement d'une session serait écrasé (quelques microsecondes) ; la copie resauvée juste avant garde
  alors un jeton peut-être révoqué → `/login` puis `compte enregistrer <x> --remplacer` le répare. Après la bascule, une session
  ouverte qui renouvelle son jeton RÉÉCRIT le fichier avec l'ancien compte (voir plus haut) : la vérification d'empreinte ne
  porte que sur l'instant de l'écriture, pas sur la suite ; seul `compte etat` le voit ensuite.
- `~/.claude.json` (`oauthAccount` : e-mail et organisation affichés par `/status`) n'est ni lu ni modifié : après une bascule
  il garde l'e-mail de l'ANCIEN compte jusqu'à ce que Claude Code le remette à jour lui-même. Ne pas s'y fier pour savoir quel
  compte est actif : la référence est `compte etat` (copie, jeton ou profil).

## Sécurité
- Seul ce module (et `usage/collect.py`) lit le fichier de connexion : Python d'ultron-hub, jamais une session Claude ni un
  collecteur de tâche. Ni jeton ni contenu dans stdout, les logs (`compte basculer de=a vers=b by=… verifie=…`) ou `comptes.json`
  (dates, empreintes, preuves). Chemins dérivés de `pwd` (pas de `$HOME`), lecture `O_NOFOLLOW`, fichier à nous seulement.
- Les copies `a.json`/`b.json` sont des secrets au même titre que l'original (0600). Le dossier est déjà couvert par le `denyRead`
  des escouades (`.config/ultron-hub/**`, `escouade/moteur.SECRET_PATHS`) et des missions (`~/`) ; un agent hors sandbox sous le
  même uid peut les lire comme il lirait l'original (même limite que `close.key`).

## Dashboard
Carte « Comptes » de l'Accueil (`static/app.js`, `loadComptes`, au plus une lecture par minute) : lecture `comptes`, boutons
`compte-basculer` (signé, risque élevé, vers `autre`) et `compte-enregistrer-a|b` (simples), tous dans `dashboard/catalogue.py`.
Captures `comptes-*.png` et `feuille-compte-*.png`. Après 🔀 : `deploy/user/apres-fusion/todo-263.json` redémarre le dashboard.

## Se reconnecter (todo #336)
Après une bascule, Claude Code peut redemander `/login` (copie dont le jeton de renouvellement est déjà invalidé) : on se
reconnecte depuis le téléphone, sans ssh (`compte/connexion.py`, tests `tests/test_compte_connexion.py`).
- `compte connexion demarrer --compte a|b` (signé `compte`, cible `login:<a|b>`) : recopie d'abord le fichier actif dans sa
  copie s'il a été renouvelé (`store.sauver_actif`), ouvre la tmux dédiée `ultron-connexion` (une seule ; un nouvel appui ferme
  l'ancienne ; verrou `var/compte/.connexion.lock`) qui lance `env -i HOME PATH LANG TERM claude auth login --claudeai`
  (argv en liste, jamais de shell), attend le lien 20 s au plus, le recolle s'il est coupé sur plusieurs lignes et le refuse
  s'il ne commence pas par `https://claude.ai/`, `https://claude.com/` (lien `claude.com/cai/` depuis le 22/10/2026, todo #338) ou `https://console.anthropic.com/`. Rend `{compte, lien}`.
- `compte connexion code --code-stdin [--compte a|b]` (signé, cible `code:<sha16 du code>`) : colle le code + Entrée, attend la
  fin (60 s), exige code de sortie 0, `claude auth status --json` → `loggedIn` et un fichier actif changé, puis
  `store.enregistrer(<x>, remplacer=True)` ; refusé avant toute écriture si le compte derrière le jeton est l'autre
  (« tu t'es connecté avec l'autre compte ») ou un troisième. `verifie` = compte reconnu par le profil. Un seul code par lien ;
  la tmux est toujours fermée.
- Filets (revue) : un fichier actif ni A ni B est d'abord copié en `inconnu-<horodatage UTC>.json` (0600, 3 gardés) ; une
  copie écrasée sans preuve que c'est le même compte (profil injoignable ou identifiant absent) est gardée en
  `<x>.avant-connexion.json` (0600, nommée dans le message). Le code passe par `tmux load-buffer` (stdin) puis
  `paste-buffer -d`, jamais dans un argv ; l'identité trouvée par `adopter` sert à `enregistrer` (un seul appel au profil) ;
  le dashboard attend 180 s.
- `compte connexion abandonner` (simple) : ferme la tmux. Jamais `claude auth logout` (il révoque le jeton chez Anthropic).
- Le lien et le code ne vont jamais dans le log, le journal ni sur disque (`var/compte/connexion.json` = compte, heure,
  empreinte du fichier avant, preuves dépensées) ; le lien ne va qu'à la page (sortie `donnees` du catalogue).
- Réflexe : `compte etat` rend `a_reconnecter` (compte enregistré au quota illisible, ou destination d'une bascule
  `verifie=False`, ou restée à confirmer plus de 12 h « bascule jamais confirmée », pas réenregistrée depuis ; une bascule
  sans clé `verifie`, d'avant #336, n'est jamais signalée). La carte Comptes montre alors
  « 🔑 Compte X à reconnecter » avec son bouton ; les boutons A/B et ✖ Abandonner sont aussi dans le pli « 🔑 Se reconnecter ».
  Un jeton d'accès seulement expiré (normal après quelques heures) n'est pas « à reconnecter » (todo #330, ⏳ sur la carte).
- Dashboard : `compte-connexion-a|b` (feuille, puis lien « 🌐 Ouvrir la page de connexion » en `_blank` + rappel du bon compte
  claude.ai + champ Code), `compte-connexion-code` (risque élevé), `compte-connexion-abandonner`. Après 🔀 :
  `deploy/user/apres-fusion/todo-336.json` redémarre le dashboard.

## Budget du jour (todo #328)
Script pur, aucune IA. Code : `compte/budget.py`, tests `tests/test_compte_budget.py`.
- `compte releve` (réseau, code existant de `store.etat`) ajoute à `var/compte/historique.jsonl` (pourcentages et dates, jamais de
  jeton ; 35 jours gardés) la semaine et la session de chaque compte dont le quota se lit (cache périmé ou jeton expiré = ignoré).
  Timer utilisateur `deploy/systemd/ultron-hub-compte-releve.timer` (toutes les 15 min) ; mise en service par
  `deploy/user/apres-fusion/todo-328.json` (activer le timer, redémarrer le dashboard), rien n'est activé à la main.
- `compte budget` (lecture seule, historique seul, sans réseau) rend par compte : points restants jusqu'à 100 % de la semaine (seule cible : 100 % de chaque compte dépensés à SA remise à zéro, aucun seuil par défaut, todo #424), temps
  avant la remise à zéro, **budget du jour = restant / jours restants** (arrondi au jour supérieur, au moins 1), dépensé depuis 00h
  locale (somme des hausses du pourcentage ; une baisse = remise à zéro, comptée depuis 0). Total = somme des comptes.
- **Dose du jour** (todo #424, `budget.dose_du_jour`, fonction pure) = (100 − % semaine) / jours restants avant la remise à zéro de CE compte (jours décimaux, plancher 1 jour), arrondie à l'entier ; champ `dose` par compte et `dose_texte` (« Aujourd'hui : A x points, B y points ») dans `compte etat` et `compte budget`. Dépassement la veille = la dose baisse.
- Conseil : épuiser d'abord le compte dont la semaine se remet à zéro le plus tôt (ce qui reste y serait perdu).
- Compte en veille (dernière lecture de plus de 45 min, jeton expiré jamais renouvelé ici) : dernière valeur connue et son heure ;
  si sa remise à zéro est passée pendant la veille, il repart de 0 %.
- Dashboard : lecture `budget` du catalogue ; la carte « Quota et comptes » n'en montre que la dose du jour par compte et la phrase de conseil (`app.js`, `conseilNodes`, #411, #412).

## Alertes de changement de compte (todo #329)
Code : `compte/alertes.py`, tests `tests/test_compte_alertes.py`. Vérifiées à la fin de chaque `compte releve` (même timer,
aucun réseau de plus, aucune action signée) et envoyées par `envoi.envoyer(sujet="alertes")` : budget du jour dépassé
(dépensé depuis 00h > budget du début de journée) ; session 5 h du compte actif au-dessus d'une limite posée explicitement (`alertes.SESSION_LIMIT`, absente par défaut = pas d'alerte) alors que l'autre a de la marge
(« Bascule vers B maintenant, parce que… », le bouton 🔁 existant fait la bascule) ; semaine du compte actif au-dessus de `alertes.WEEK_LIMIT` (absente par défaut = pas d'alerte).
Au plus une alerte par type et par demi-journée (AM/PM locale), état dans `var/compte/alertes.json` (écriture atomique) ;
un envoi en échec est retenté au relevé suivant. Le service du relevé étant un oneshot, le nouveau code est pris sans redémarrage.

## Bascule automatique sur quota (todo #534)
Code : `compte/auto.py`, tests `tests/test_compte_auto.py`. Appelée à chaque passage du heartbeat (`backup/commands.handle_heartbeat`,
jamais bloquante), sans jeton : c'est la règle écrite qui autorise, celle de l'alerte « Bascule » (`alertes.switch_rule`,
partagée) : session 5 h du compte actif ≥ `alertes.SESSION_LIMIT` et l'autre a de la marge. Sans limite posée (défaut, todo #424),
rien n'est lu ni basculé. Refus (rien ne change, raison rendue et journalisée en WARNING) : `ULTRON_HUB_JOB` ou
`ULTRON_ESCOUADE_DIR` posé ; destination dans `a_reconnecter` (la reconnexion OAuth reste au ✅) ; dernière bascule (manuelle ou
auto) de moins de 6 h (revérifié sous le verrou de la bascule) ; destination elle-même au-dessus du seuil ou au quota illisible.
La bascule passe par `store.basculer` (mêmes contrôles et écritures que la bascule signée) avec `by = auto:quota` ; preuve dans
`comptes.json` (`derniere_bascule.by`, liste `bascules_auto`, 50 gardées, lettres et pourcentages seulement) et dans le journal
commun (« Bascule automatique de compte A → B (auto:quota) : … »). Le bilan du soir (`cap/bilan_soir.py`) la liste (sorte
`bascule`, `retour`) avec un ↩️ = l'action signée existante `compte-basculer` vers `retour` ; la ligne passe « annulée » dès
qu'une bascule plus récente revient au compte d'origine.

## Planning des points (todo #534, option 1 du 05/11)
Code : `compte/planning.py` (fonctions pures + `var/compte/planning.json`), tests `tests/test_compte_planning.py`. Aucune IA, aucun réseau.
- **Parts** par compte et par jour : dose du jour prise à minuit (`budget.dose_du_jour` du % de minuit, elle ne fond pas en dépensant) ;
  réserve (à toi) = 25 % de la dose, au moins 3 points (jamais plus que la dose) ; part auto = dose − réserve. **Fin rapide** : dans les
  12 h avant la remise à zéro du compte, réserve 0 (la réserve inutilisée passe à l'auto).
- **Bascule auto** (`auto.decide`) : seuil 90 % de la session 5 h par défaut (le planning le fournit, `alertes.SESSION_LIMIT` ne sert
  plus qu'à l'alerte). Refus en plus : auto en pause ; compte actif qui a dépensé sa part auto (« l'auto s'arrête… le reste est ta
  réserve », rien basculé) ; destination qui a dépensé la sienne ; part inconnue (pas de relevé). Toujours ≤ 1 bascule / 6 h ; la
  destination est le compte qui se remet à zéro le plus tôt (`planning.destinations`, avec deux comptes : l'autre). La pause est revérifiée sous le verrou.
  Limite : le compteur ne sait pas qui dépense ; ta réserve = ce qui reste quand la part auto est atteinte. Le heartbeat lit le quota
  (`store.etat`, cache 60 s) à chaque passage sauf en pause.
- **Case « Planning du jour »** (📥 À toi, `cartePlanning`) : `compte planning poser [--reserve-a N] [--reserve-b N] [--seuil N]` (simple,
  catalogue `planning-poser`) ; champ vide = défaut, tout vide = case effacée ; valable jusqu'à minuit (Paris), prime sur l'appris. La carte
  affiche « A : 21 points = auto 16, à toi 5 » (texte du serveur, `compte planning voir`, lecture `planning`).
- **Pause** : `compte planning pause|reprendre` (catalogue `planning-pause` / `planning-reprendre`, simples) ; état sur la carte, gardé jusqu'à reprise.
- **Bilan hebdo** : au bilan du soir (le plus simple : recalculé de l'historique, rien à mémoriser), champ `remises` = une ligne par
  remise à zéro du jour : prévu 100 %, réel (% final), réserve avant → après. Hors `lignes` (ce n'est pas un geste évité).
- **Apprentissage** par compte, allumé seul après 21 jours complets d'historique (00 h–24 h sans trou > 45 min ; un compte en veille
  en perd) : profil heure × jour de semaine (lissage exponentiel, 0,3 pour la semaine neuve) qui étale la part auto dans la journée
  (`allowed_so_far`) ; à chaque remise à zéro postérieure à 21 jours complets, % final à plus de 5 points de 100 = réserve ± 5 % (0 à 50 ;
  en pratique seule la baisse arrive, le % s'arrête à 100). Tout est recalculé de l'historique gardé (35 jours).
- **Laissé de côté** : la « part de l’utilisateur mesurée » (90e centile hors travail auto) : aucun signal fiable ne dit qui a dépensé.
