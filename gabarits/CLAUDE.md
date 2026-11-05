# {{NOM_ASSISTANT}} : instructions globales (machine {{MACHINE}})

- Répondre en {{LANGUE}}.
- {{PRENOM}} pilote par le dashboard et des agents Claude Code.
- Charte : `{{HOME}}/.claude/charte.md`. Socle de prompt : `{{HOME}}/.claude/socle.md`.
- Projets : `{{HOME}}/workspace/projects/active/` ; chaque projet a son CLAUDE.md.

## Règles de sécurité
- Ne jamais lire ni afficher : fichiers de mots de passe, `{{HOME}}/.ssh`, fichiers `.env`, `auth.json`, identifiants
  des outils (`{{HOME}}/.claude/.credentials.json`, `{{HOME}}/.config/gh`).
- Jamais `sudo` : proposer la commande à {{PRENOM}} sous la forme `! sudo …`.

## Travail sur le code
- Branche `todo-<id>/…` + worktree, jamais directement sur main.
- Ne jamais clore une todo : la clôture est le bouton de {{PRENOM}}.
