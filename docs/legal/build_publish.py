"""Готовит версии документов для публикации (telegra.ph и т. п.).

- убирает служебные пометки LEGAL_REVIEW_REQUIRED и пояснение о них;
- превращает таблицы в списки (telegra.ph не показывает таблицы);
- снимает markdown-разметку (#, **, `).

Запуск: python3 docs/legal/build_publish.py  →  docs/legal/publish/*.txt
Перед публикацией пункты LEGAL_REVIEW_REQUIRED должен подтвердить юрист.
"""

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARK = "`LEGAL_REVIEW_REQUIRED`"
DOCS = {"offer.md": "offer.txt", "privacy.md": "privacy.txt", "consent.md": "consent.txt"}


def _strip_md(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = text.replace("`", "")
    return text


def _table_to_list(rows: list[str]) -> list[str]:
    cells = [[_strip_md(c.replace(MARK, "")).strip() for c in r.strip().strip("|").split("|")] for r in rows]
    header, body = cells[0], [c for c in cells[2:]]
    out = []
    for c in body:
        first, rest = c[0], [f"{h.lower()}: {v}" for h, v in zip(header[1:], c[1:]) if v]
        out.append(f"- {first}" + (f" ({'; '.join(rest)})" if rest else ""))
    return out


def convert(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(">") and "LEGAL_REVIEW_REQUIRED" in line:
            i += 1
            continue
        if line.strip() == "---":
            i += 1
            continue
        if line.startswith("|"):
            table = []
            while i < len(lines) and lines[i].startswith("|"):
                table.append(lines[i])
                i += 1
            out.extend(_table_to_list(table))
            continue
        stripped = re.sub(r"^\d+(\.\d+)*\.\s*", "", line.strip())
        if stripped.startswith(MARK):  # целый пункт - служебный
            i += 1
            continue
        if MARK in line:  # пометка в конце строки
            line = line.split(MARK)[0].rstrip(" -:;")
        line = re.sub(r"^#{1,6}\s*", "", line)
        out.append(_strip_md(line))
        i += 1
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    return text


def main() -> None:
    target = HERE / "publish"
    target.mkdir(exist_ok=True)
    for src, dst in DOCS.items():
        (target / dst).write_text(convert((HERE / src).read_text(encoding="utf-8")), encoding="utf-8")
        print("→", target / dst)


if __name__ == "__main__":
    main()
