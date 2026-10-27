"""Расписание АГАТУ (ИСиП-9-25) -> три календаря .ics: theory, practice, dfk.

Запуск:  python agatu_schedule.py [--subgroup 1|2] [--pdf файл.pdf] [--alarm 15] [--out-dir .]
Нужно:   pip install pdfplumber
"""
import argparse
import datetime as dt
import hashlib
import io
import os
import re
import sys
import urllib.request
from typing import NamedTuple

import pdfplumber

PAGE_URL = "https://agatu.ru/obuchauschimsya/raspisanie/"
LINK_RE = re.compile(r'href="([^"]*informaczionnye-sistemy-i-programmirovanie[^"]*\.pdf)"')
TIME_RE = re.compile(r"(\d{1,2})[.:](\d{2})(?:\s*-\s*(\d{1,2})[.:](\d{2}))?")
DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}")
DFK_RE = re.compile(r"дфк|диф\w*\.?\s*зач")
TEACHER_RE = re.compile(r"\(([^()]*\w\.\w\.)\)")  # "(Фамилия И.О.)"
PARENS_RE = re.compile(r"\s*\([^)]*\)")

GROUP_MARKER = "9-25-1"  # по нему ищется страница PDF с нужной группой
TZ = "Asia/Yakutsk"  # часовой пояс вуза (UTC+9, без перехода на летнее время)
LESSON_MINUTES = 95  # длительность, если в расписании указано только время начала
BREAK_AFTER, BREAK_LEN = 40, 5  # перемена внутри пары: через 40 минут после начала, 5 минут
CALENDARS = {"theory": "Теория", "practice": "Практика", "dfk": "ДФК"}  # <ключ>.ics = отдельный календарь


class Lesson(NamedTuple):
    start: dt.datetime
    end: dt.datetime
    title: str  # как в расписании: "Предмет (практ.зан) (Фамилия И.О.)"
    room: str

    @property
    def name(self):
        return PARENS_RE.sub("", self.title).strip()

    @property
    def category(self):
        t = self.title.lower()
        if DFK_RE.search(t):
            return "dfk"
        return "theory" if "теория" in t else "practice"  # без пометки (физкультура) = практика


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=30).read()


def find_pdf_url():
    html = fetch(PAGE_URL).decode("utf-8", "replace")
    # ссылка в блоке "Расписание занятий" идёт раньше экзаменационных
    m = LINK_RE.search(html, max(html.find("Расписание занятий"), 0))
    if not m:
        sys.exit("Не нашёл ссылку на PDF на странице расписания")
    return m.group(1)


def parse(pdf_bytes, subgroup):
    pdf = pdfplumber.open(io.BytesIO(pdf_bytes))
    page = next((p for p in pdf.pages if GROUP_MARKER in (p.extract_text() or "")), None)
    if page is None:
        sys.exit(f"В PDF нет страницы с группой {GROUP_MARKER}")
    words = page.extract_words(x_tolerance=1)

    def cell_text(bbox):
        if not bbox:
            return ""
        x0, top, x1, bottom = bbox
        inside = [w for w in words
                  if x0 <= (w["x0"] + w["x1"]) / 2 <= x1 and top <= (w["top"] + w["bottom"]) / 2 <= bottom]
        inside.sort(key=lambda w: (round(w["top"]), w["x0"]))
        return re.sub(r"\s+", " ", " ".join(w["text"] for w in inside)).strip()

    lessons, day = [], None
    for row in page.find_tables()[0].rows:
        c = row.cells  # 6 колонок: день | время | гр1 | ауд1 | гр2 | ауд2; объединённые ячейки = None
        if m := DATE_RE.search(cell_text(c[0])):
            day = dt.datetime.strptime(m.group(), "%d.%m.%Y").date()
        t = TIME_RE.fullmatch(cell_text(c[1]))
        if not t or day is None:
            continue
        h1, m1, h2, m2 = t.groups()
        start = dt.datetime.combine(day, dt.time(int(h1), int(m1)))
        end = (dt.datetime.combine(day, dt.time(int(h2), int(m2))) if h2
               else start + dt.timedelta(minutes=LESSON_MINUTES))
        if c[3] is None:  # общая пара для обеих подгрупп: одна ячейка на гр1+гр2, аудитория справа
            title, room = cell_text(c[2]), cell_text(c[5])
        elif subgroup == 1:
            title, room = cell_text(c[2]), cell_text(c[3])
        else:
            title, room = cell_text(c[4]), cell_text(c[5])
        if title and not title.lower().startswith("кураторск"):
            lessons.append(Lesson(start, end, title, room))
    return lessons


def esc(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build_event(lesson, alarm_min):
    start, end, title, room = lesson
    uid = hashlib.md5(f"{start}{title}".encode()).hexdigest()
    teacher = TEACHER_RE.findall(title)
    place = " · ".join(filter(None, [f"ауд. {room}" if room else "", teacher[-1] if teacher else ""]))
    desc = title
    if end - start >= dt.timedelta(minutes=90):
        b0 = start + dt.timedelta(minutes=BREAK_AFTER)
        desc += f"\nПеремена {b0:%H:%M}-{b0 + dt.timedelta(minutes=BREAK_LEN):%H:%M}"
    alarm = f"Следующая пара - {lesson.name}" + (f", ауд. {room}" if room else "")
    fmt = "%Y%m%dT%H%M%S"
    return [
        "BEGIN:VEVENT", f"UID:{uid}@agatu-schedule",
        "DTSTAMP:20260101T000000Z",  # фиксированный, чтобы файл не менялся без изменений в расписании
        f"DTSTART;TZID={TZ}:{start:{fmt}}", f"DTEND;TZID={TZ}:{end:{fmt}}",
        f"SUMMARY:{esc(lesson.name)}",
        *([f"LOCATION:{esc(place)}"] if place else []),
        f"DESCRIPTION:{esc(desc)}",
        "BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{esc(alarm)}", f"TRIGGER:-PT{alarm_min}M", "END:VALARM",
        "END:VEVENT",
    ]


def build_ics(lessons, alarm_min, cal_name):
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//agatu-schedule//RU", "CALSCALE:GREGORIAN",
           f"X-WR-CALNAME:{cal_name}", f"X-WR-TIMEZONE:{TZ}",
           "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H",
           "BEGIN:VTIMEZONE", f"TZID:{TZ}", "BEGIN:STANDARD", "DTSTART:19700101T000000",
           "TZOFFSETFROM:+0900", "TZOFFSETTO:+0900", "TZNAME:YAKT", "END:STANDARD", "END:VTIMEZONE"]
    for lesson in lessons:
        out += build_event(lesson, alarm_min)
    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subgroup", type=int, default=1, choices=(1, 2))
    ap.add_argument("--pdf", help="локальный PDF вместо загрузки с сайта")
    ap.add_argument("--alarm", type=int, default=15, help="за сколько минут напоминать")
    ap.add_argument("--out-dir", default=".")
    a = ap.parse_args()

    lessons = parse(open(a.pdf, "rb").read() if a.pdf else fetch(find_pdf_url()), a.subgroup)
    for les in lessons:
        print(f"{les.start:%Y-%m-%d %H:%M}-{les.end:%H:%M} | {les.category:8} | {les.title} | {les.room}")
    for key, label in CALENDARS.items():
        part = [les for les in lessons if les.category == key]
        path = os.path.join(a.out_dir, f"{key}.ics")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(build_ics(part, a.alarm, f"АГАТУ: {label}"))
        print(f"{path}: {len(part)}")


if __name__ == "__main__":
    main()
