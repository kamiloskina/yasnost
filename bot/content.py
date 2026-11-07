"""Загрузка уроков из content/dayN.md.

Формат файла: части урока разделены строкой `---`. Каждая часть отправляется отдельным
сообщением (лимит Telegram — 4096 символов). Разметка: Telegram HTML (<b>, <i>).

Часть, первая строка которой `@audio`, — это аудиопрактика. В ней хранится только название
(«Аудиопрактика «…»»): текст практики пользователям не отправляется. Сама запись загружается
через бота (/audio) или лежит в content/audio/dayN.mp3; пока её нет, бот присылает название
и короткое сообщение «скоро появится». Сценарии для записи — docs/audio-scripts.md.
"""

from dataclasses import dataclass
from pathlib import Path

CONTENT_DIR = Path(__file__).resolve().parent.parent / "content"
AUDIO_EXTS = (".mp3", ".ogg", ".m4a")
TG_LIMIT = 4096
CAPTION_LIMIT = 1024


@dataclass(frozen=True)
class Part:
    text: str
    is_audio: bool = False


@dataclass(frozen=True)
class Lesson:
    day: int
    title: str
    parts: list[Part]


def audio_path(day: int, content_dir: Path = CONTENT_DIR) -> Path | None:
    for ext in AUDIO_EXTS:
        p = content_dir / "audio" / f"day{day}{ext}"
        if p.exists():
            return p
    return None


def load_lesson(day: int, content_dir: Path = CONTENT_DIR) -> Lesson:
    raw = (content_dir / f"day{day}.md").read_text(encoding="utf-8")
    lines = raw.strip().splitlines()
    # Первая строка файла: «# Заголовок дня» (для меню «Мои дни»)
    title = lines[0].lstrip("#").strip() if lines and lines[0].startswith("#") else f"День {day}"
    body = "\n".join(lines[1:] if lines and lines[0].startswith("#") else lines)
    parts: list[Part] = []
    for chunk in body.split("\n---\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk.startswith("@audio"):
            parts.append(Part(text=chunk[len("@audio"):].strip(), is_audio=True))
        else:
            parts.append(Part(text=chunk))
    for p in parts:
        if len(p.text) > TG_LIMIT:
            raise ValueError(f"day{day}.md: часть длиннее {TG_LIMIT} символов, раздели её строкой ---")
    return Lesson(day=day, title=title, parts=parts)


def all_titles(total: int = 7, content_dir: Path = CONTENT_DIR) -> dict[int, str]:
    return {d: load_lesson(d, content_dir).title for d in range(1, total + 1)}
