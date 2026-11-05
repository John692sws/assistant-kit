# Coût d'une session : `ultron-hub cout session` (todo #227)

Carte : `src/ultron_hub/cout/session.py` (lecture + calcul), `cout/commands.py` (CLI) ; test `tests/test_cout_session.py` ;
spec `~/notes/spec-socle-unique.md` §4.3. Réutilise `cout/usage_jsonl.poids` (table de prix) et `usage_of`.

`ultron-hub cout session <transcript.jsonl>` : script pur, sans IA ni réseau. Lit les lignes `assistant` (`message.usage`) du
transcript et de `<id>/subagents/*.jsonl`. Un message réécrit pendant le streaming (même `message.id`) compte une fois
(dernière ligne, sous-agents compris). Rend : `entree`, `sortie`, `cache_ecriture`, `cache_lecture`, `messages`, `cout_usd`,
`debut`, `fin`, `duree_s` (premier/dernier horodatage), `todos_livrees` (numéros des commits `(todo #N)` de tout le dépôt,
toutes branches, dans la fenêtre) et `cout_par_todo` (null sans todo livrée).

Coût : un transcript n'a pas de `total_cost_usd`. Il vient de `garde.poids` (entrée 1, sortie 5, écriture de cache 1,25, lecture
0,1, fois opus 5 / sonnet 1 / haiku 0,3) multiplié par `USD_PAR_UNITE` (3e-6, prix d'un token d'entrée sonnet) : un ordre de
grandeur, pas une facture.

# Comparer les deux modes : `ultron-hub cout compare [--n 5]` (todo #228)

Code : `cout/compare.py`, test `tests/test_cout_compare.py`. Options de test : `--etat-dir`, `--projets-dir`. À la demande, pas de
timer ; script pur, sans IA ni réseau. Sortie JSON habituelle ; `summary` et `data.text` = tableau lisible sur Telegram.

- Escouade : les N dernières de `~/.local/state/ultron-hub/escouades/*/` (avec `etat.json` et au moins un `session-*.jsonl`).
  Coût = `usage_jsonl.consommation()` (le `total_cost_usd` relevé par Claude, donc sous-agents compris). Points de semaine =
  `points_consommes` de `etat.json`, sinon `soft_semaine - base_semaine` de `garde.json`, sinon `delta_semaine` de `quota.json`.
  Todos livrées = commits `(todo #N)` entre `debut_ts` et la dernière écriture d'un flux de session.
- Interactif : les N transcripts les plus récents de `~/.claude/projects/-home-ultron*/*.jsonl` (hors `subagents/`, hors dossiers
  `-var-missions-`, `-notes-escouade-`, `scratchpad`, hors sessions d'escouade reconnues par leur `session_id`). Mesure =
  `cout session`. Points de semaine : inconnus (`null`, affichés `?`), un transcript ne les contient pas.
- Unité : USD des deux côtés, mais l'interactif est une **estimation** (table de prix du chien de garde) et l'escouade un coût
  relevé : l'écart peut atteindre un facteur 2, le dire en lisant le tableau (rappelé dans `unite` et en tête du texte).
- Par mode : coût total, todos livrées, **coût par todo livrée** (total / total), **points par todo** (sur les sessions aux
  points connus). Des sessions simultanées partagent les mêmes commits : une todo peut compter dans deux sessions.
