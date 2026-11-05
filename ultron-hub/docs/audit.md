# Audit du système (todo #452)

`ultron-hub audit inventaire [--out fichier.json]` : inventaire exact, sans IA, lecture seule (seul `--out` écrit, en 0600).
Code : `src/ultron_hub/audit/` (`inventory.py` collecte, `commands.py` CLI).

Chaque élément : `nom`, `type`, `chemin`, `taille` (octets), `modifie`, `dernier_usage` (ISO ou null), `source_usage`.
Types : `projet`, `notes`/`notes-dossier`, `ultron`, `skill` (+ `appels_30j`), `var`, `commande`, `tache`, `timer`, `service`,
`action-dashboard`. Sortie : `{status, command, summary, data:{elements, totaux, warnings}}`.

- Dossiers : `os.scandir`/`lstat` seulement, liens non suivis.
- Usage des commandes : dernière ligne de `var/logs/ultron-hub.log*` avec `command=<nom>` (ou `"command": "<nom>.`) et mention
  `ultron-hub <nom>` dans le journal commun. Les logs ne notent `command=` que pour les refus : la mesure est donc partielle.
- Tâches : `var/runs/*/request.json` le plus récent. Timers/services : `systemctl --user ... --output=json`.
- Skills : transcripts `~/.claude/projects/**/*.jsonl` (tool_use `Skill`). Actions du dashboard : journal `source=dashboard`
  relié par le gabarit `journal` du catalogue, sinon null.
- Secrets : exclusions de `config/menage.yaml` (`menage/audit.py`) + `~/forum`, `~/.ssh`, `~/.hermes`, `~/.config/gh`,
  `~/.sudo_pass`, `auth.json`, `.env`, `.credentials.json` : jamais ouverts, `stat`és ni listés.
- Une source illisible = une ligne de `warnings`, jamais d'exception.
