"""Собирает tracker.html из content.py. PDF печатается через render.js (Playwright)."""

from html import escape
from pathlib import Path

import content as c

HERE = Path(__file__).resolve().parent

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,500;0,600;1,400;1,500&display=swap');
@page { size: A4; margin: 0; }
:root {
  --paper: #F7F2EC; --ink: #2E2A27; --muted: #7D716A; --accent: #9A5B48;
  --line: #D8CCC2; --soft: #EFE6DD;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { background: var(--paper); color: var(--ink); }
body { font-family: 'Inter', 'DejaVu Sans', sans-serif; font-size: 10.5pt; line-height: 1.55;
  -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.page { width: 210mm; height: 297mm; padding: 20mm 20mm 18mm; position: relative;
  page-break-after: always; overflow: hidden; display: flex; flex-direction: column; }
.page:last-child { page-break-after: auto; }
.serif { font-family: 'Cormorant Garamond', 'DejaVu Serif', serif; }
h1, h2, h3 { font-family: 'Cormorant Garamond', 'DejaVu Serif', serif; font-weight: 500; }
h2 { font-size: 30pt; line-height: 1.1; margin-bottom: 8mm; }
h3 { font-size: 15pt; margin: 0 0 2mm; }
.kicker { font-size: 8pt; letter-spacing: .28em; text-transform: uppercase; color: var(--accent); margin-bottom: 3mm; }
.folio { position: absolute; bottom: 9mm; left: 20mm; right: 20mm; display: flex; justify-content: space-between;
  font-size: 7.5pt; letter-spacing: .2em; text-transform: uppercase; color: var(--muted); }
p + p { margin-top: 3.5mm; }
.lines { flex: 1; min-height: 0; overflow: hidden; display: flex; flex-direction: column; }
.lines i { flex: 0 0 8.5mm; border-bottom: .6px solid var(--line); }
.box { border: .6px solid var(--line); border-radius: 3mm; padding: 4mm 5mm; background: rgba(255,255,255,.35); }
.prompt { font-family: 'Cormorant Garamond', serif; font-size: 13.5pt; font-style: italic; margin-bottom: 1mm; }

/* Обложка */
.cover { justify-content: space-between; padding: 26mm 22mm; }
.cover .series { font-size: 9pt; letter-spacing: .5em; text-transform: uppercase; color: var(--accent); }
.cover .circle { width: 88mm; height: 88mm; border-radius: 50%; border: .8px solid var(--accent);
  margin: 0 auto; position: relative; }
.cover .circle::after { content: ""; position: absolute; inset: 9mm; border-radius: 50%;
  background: radial-gradient(circle at 50% 45%, #E9D7C9 0%, #F1E5DA 55%, transparent 72%); }
.cover h1 { font-size: 40pt; line-height: 1.05; }
.cover .sub { margin-top: 5mm; font-size: 12pt; color: var(--muted); }
.cover .days { font-family: 'Cormorant Garamond', serif; font-size: 64pt; color: var(--accent); line-height: 1; }

/* Шкалы */
.scales { display: grid; grid-template-columns: 1fr 1fr; gap: 3mm 10mm; }
.scale { display: flex; align-items: center; justify-content: space-between; }
.scale span { font-size: 9.5pt; }
.dots { display: flex; gap: 2.2mm; }
.dots i { width: 6mm; height: 6mm; border-radius: 50%; border: .7px solid var(--muted); font-style: normal;
  font-size: 6.5pt; color: var(--muted); display: flex; align-items: center; justify-content: center; }

/* Таблицы-трекеры */
table.grid { width: 100%; border-collapse: collapse; table-layout: fixed; flex: 1; }
table.grid th { font-weight: 500; font-size: 7.5pt; color: var(--muted); padding: 1mm .5mm 2mm; vertical-align: bottom;
  line-height: 1.2; }
table.grid td { border: .5px solid var(--line); height: 6.85mm; text-align: center; font-size: 7.5pt; color: var(--muted); }
table.grid td.n { border: none; text-align: right; padding-right: 2mm; width: 9mm; }
table.grid tr:nth-child(even) td:not(.n) { background: rgba(239,230,221,.55); }
.legend { font-size: 8pt; color: var(--muted); margin-bottom: 3mm; }

/* Страница дня */
.dayhead { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 5mm;
  border-bottom: .6px solid var(--line); padding-bottom: 3mm; }
.dayhead .num { font-family: 'Cormorant Garamond', serif; font-size: 34pt; line-height: .9; }
.dayhead .num small { font-size: 11pt; letter-spacing: .25em; text-transform: uppercase; color: var(--accent); display: block; margin-bottom: 1.5mm; font-family: 'Inter', sans-serif; }
.date { font-size: 9pt; color: var(--muted); }
.date b { display: inline-block; width: 38mm; border-bottom: .6px solid var(--muted); }
.section-label { font-size: 7.5pt; letter-spacing: .2em; text-transform: uppercase; color: var(--muted); margin: 4.5mm 0 1.5mm; }
.check { display: flex; align-items: center; gap: 3mm; font-size: 9.5pt; }
.check i { width: 4.5mm; height: 4.5mm; border: .7px solid var(--muted); border-radius: 1mm; display: inline-block; }
.affirm { margin-top: 5mm; text-align: center; font-family: 'Cormorant Garamond', serif; font-style: italic;
  font-size: 15pt; color: var(--accent); padding-top: 4mm; border-top: .6px solid var(--line); }

/* Аффирмации */
.aff-list { columns: 2; column-gap: 10mm; }
.aff-list li { list-style: none; break-inside: avoid; display: flex; gap: 3mm; margin-bottom: 3.3mm;
  font-family: 'Cormorant Garamond', serif; font-size: 12.5pt; line-height: 1.25; }
.aff-list li b { font-family: 'Inter', sans-serif; font-weight: 500; font-size: 7.5pt; color: var(--accent); min-width: 5mm; padding-top: 1mm; }

.howto { display: grid; gap: 4.5mm; }
.howto div b { display: block; font-weight: 600; margin-bottom: .8mm; }
.note { font-size: 8.5pt; color: var(--muted); border-left: 2px solid var(--accent); padding: 1mm 0 1mm 4mm; }
.center { text-align: center; }
"""


def e(s: str) -> str:
    return escape(s)


def folio(left: str, right: str = "") -> str:
    return f'<div class="folio"><span>{e(left)}</span><span>{e(right)}</span></div>'


def lines(style: str = "") -> str:
    """Линии для письма: столько, сколько поместится в блок (лишние обрезаются)."""
    return f'<div class="lines" style="{style}">' + "<i></i>" * 30 + "</div>"


def dots() -> str:
    return '<div class="dots">' + "".join(f"<i>{i}</i>" for i in range(1, 6)) + "</div>"


def page(body: str, cls: str = "") -> str:
    return f'<section class="page {cls}">{body}</section>'


def cover() -> str:
    return page(f"""
      <div class="series">{e(c.SERIES)}</div>
      <div class="circle"></div>
      <div>
        <div class="days">30</div>
        <h1>{e(c.TITLE)}</h1>
        <div class="sub">{e(c.SUBTITLE)}</div>
      </div>""", "cover")


def intro() -> str:
    paras = "".join(f"<p>{e(p)}</p>" for p in c.INTRO)
    return page(f"""
      <div class="kicker">Введение</div>
      <h2>Прежде чем начать</h2>
      <div class="serif" style="font-size:14pt;line-height:1.5">{paras}</div>
      <div style="flex:1"></div>
      <div class="note">{e(c.DISCLAIMER)}</div>
      {folio(c.SERIES, "Введение")}""")


def how_to() -> str:
    items = "".join(f"<div><b>{e(t)}</b>{e(d)}</div>" for t, d in c.HOW_TO)
    return page(f"""
      <div class="kicker">Инструкция</div>
      <h2>Как пользоваться</h2>
      <div class="howto">{items}</div>
      <div class="section-label" style="margin-top:9mm">Шкалы на каждой странице</div>
      <p>1 — совсем плохо или очень мало, 3 — средне, 5 — хорошо или много. Здесь нет правильных ответов — только честные.</p>
      <div class="box" style="margin-top:5mm">
        <div class="scales">{"".join(f'<div class="scale"><span>{e(s)}</span>{dots()}</div>' for s in c.SCALES)}</div>
      </div>
      {folio(c.SERIES, "Инструкция")}""")


def start_page() -> str:
    blocks = "".join(
        f'<div class="prompt">{e(p)}</div>{lines("flex:1")}' for p in c.START_PROMPTS
    )
    return page(f"""
      <div class="kicker">День 0</div>
      <h2>Точка старта</h2>
      <div class="date" style="margin-bottom:5mm">Дата: <b></b></div>
      {blocks}
      {folio(c.SERIES, "Точка старта")}""")


def affirmations() -> str:
    items = "".join(f"<li><b>{i:02d}</b><span>{e(a)}</span></li>" for i, a in enumerate(c.AFFIRMATIONS, 1))
    return page(f"""
      <div class="kicker">30 аффирмаций</div>
      <h2>Слова на каждый день</h2>
      <p style="margin-bottom:7mm;color:var(--muted)">Каждая аффирмация есть и на странице своего дня. Здесь — все вместе, чтобы вернуться к тем, что откликаются.</p>
      <ul class="aff-list">{items}</ul>
      {folio(c.SERIES, "Аффирмации")}""")


def grid_page(kicker: str, title: str, legend: str, columns: list[str]) -> str:
    head = '<th class="n"></th>' + "".join(f"<th>{e(col)}</th>" for col in columns)
    rows = "".join(
        f'<tr><td class="n">{d}</td>' + "<td></td>" * len(columns) + "</tr>" for d in range(1, 31)
    )
    return page(f"""
      <div class="kicker">{e(kicker)}</div>
      <h2 style="margin-bottom:3mm">{e(title)}</h2>
      <div class="legend">{e(legend)}</div>
      <table class="grid"><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>
      {folio(c.SERIES, kicker)}""")


def day_page(n: int) -> str:
    scales = "".join(f'<div class="scale"><span>{e(s)}</span>{dots()}</div>' for s in c.SCALES)
    return page(f"""
      <div class="dayhead">
        <div class="num"><small>День</small>{n:02d}</div>
        <div class="date">Дата: <b></b></div>
      </div>
      <div class="box"><div class="scales">{scales}</div></div>
      <div class="section-label">Что я замечаю в теле</div>
      {lines("flex:0 0 17mm")}
      <div class="section-label">Вопрос дня</div>
      <div class="prompt">{e(c.QUESTIONS[n - 1])}</div>
      {lines("flex:1")}
      <div class="section-label">Что мне сегодня нужно</div>
      {lines("flex:0 0 17mm")}
      <div class="check" style="margin-top:3mm"><i></i>Сегодня я сделала одну вещь для себя: <span style="flex:1;border-bottom:.6px solid var(--line);height:4mm"></span></div>
      <div class="affirm">{e(c.AFFIRMATIONS[n - 1])}</div>
      {folio(c.SERIES, f"День {n}")}""")


def final_pages() -> str:
    blocks = "".join(
        f'<div class="prompt">{e(p)}</div>{lines("flex:1")}' for p in c.FINAL_PROMPTS
    )
    reflection = page(f"""
      <div class="kicker">Итог</div>
      <h2>Тридцать дней</h2>
      {blocks}
      {folio(c.SERIES, "Итог")}""")
    closing_text = "".join(f"<p>{e(p)}</p>" for p in c.CLOSING)
    closing = page(f"""
      <div style="flex:1"></div>
      <div class="center">
        <div class="kicker">{e(c.SERIES)}</div>
        <h2 style="font-size:34pt">Ты прошла этот путь</h2>
        <div class="serif" style="font-size:14pt;line-height:1.55;max-width:140mm;margin:0 auto">{closing_text}</div>
      </div>
      <div style="flex:1"></div>
      <div class="affirm" style="border:none">{e(c.AFFIRMATIONS[-1])}</div>
      <div style="flex:.4"></div>""")
    return reflection + closing


def build() -> Path:
    pages = [
        cover(), intro(), how_to(), start_page(), affirmations(),
        grid_page("Трекер состояния", "Как я день за днём",
                  "Ставь оценку от 1 до 5. «Движение» и «Время для себя» — отметь ✓, если было.",
                  c.STATE_COLUMNS),
        grid_page("Сигналы тела", "Что говорит тело",
                  "Отмечай ✓ то, что заметила за день. Если сигналы сильные или держатся долго — покажись врачу.",
                  c.BODY_SIGNALS),
        *[day_page(n) for n in range(1, 31)],
        final_pages(),
    ]
    html = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>{e(c.TITLE)}</title><style>{CSS}</style></head><body>{''.join(pages)}</body></html>"""
    out = HERE / "tracker.html"
    out.write_text(html, encoding="utf-8")
    return out


if __name__ == "__main__":
    print(build())
