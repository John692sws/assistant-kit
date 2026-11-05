"""CLAUDE.md keeps only rules (todo #167): bounded size, and every doc of its map exists."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 30_000


def test_claude_md_stays_small():
    size = len((ROOT / "CLAUDE.md").read_bytes())
    assert size <= MAX_BYTES, (f"CLAUDE.md fait {size} octets (max {MAX_BYTES}) : range l'historique dans docs/<module>.md "
                               "et le message de commit, garde ici seulement des règles")


def test_every_doc_of_the_map_exists_and_every_doc_is_on_the_map():
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    cited = set(re.findall(r"`docs/([a-z0-9-]+\.md)`", text))
    present = {p.name for p in (ROOT / "docs").glob("*.md")}
    assert cited and cited <= present, cited - present
    assert present <= cited, f"docs absents de la carte de CLAUDE.md : {present - cited}"
