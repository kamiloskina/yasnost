"""Требование владельца: никаких длинных и средних тире в текстах, которые видят люди."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = ("—", "–")  # — и –


def _files():
    yield ROOT / "bot" / "texts.py"
    yield ROOT / "products" / "tracker" / "content.py"
    yield ROOT / "CLAUDE.md"
    for folder in ("content", "docs"):
        for p in (ROOT / folder).rglob("*"):
            if p.suffix in (".md", ".txt"):
                yield p


def test_no_long_dashes():
    bad = [str(p.relative_to(ROOT)) for p in _files() if any(ch in p.read_text(encoding="utf-8") for ch in FORBIDDEN)]
    assert bad == [], f"Длинное или среднее тире найдено в: {bad}"
