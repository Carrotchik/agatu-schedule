"""Расписание АГАТУ (ИСиП-9-25) -> schedule.ics для календаря.

Запуск:  python agatu_schedule.py [--subgroup 1|2] [--pdf файл.pdf] [--alarm 15] [--out schedule.ics]
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

import pdfplumber

PAGE_URL = "https://agatu.ru/obuchauschimsya/raspisanie/"
LINK_RE = re.compile(r'href="([^"]*informaczionnye-sistemy-i-programmirovanie[^"]*\.pdf)"')
TIME_RE = re.compile(r"(\d{1,2})[.:](\d{2})(?:\s*-\s*(\d{1,2})[.:](\d{2}))?")
DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}")
GROUP_MARKER = "9-25-1"  # по нему ищется страница PDF с нужной группой
TZ = "Asia/Yakutsk"  # часовой пояс вуза (UTC+9, без перехода на летнее время)
LESSON_MINUTES = 95  # длительность, если в расписании указано только время начала
BREAK_AFTER, BREAK_LEN = 40, 5  # перемена внутри пары: через 40 минут после начала, 5 минут
CATEGORIES = {"theory": "Теория", "practice": "Практика", "dfk": "ДФК и сводка"}  # файл <ключ>.ics = отдельный календарь; сводка дня лежит в dfk.ics


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


def clean(s):
    return re.sub(r"\s+", " ", s).strip()


def parse(pdf_bytes, subgroup):
    pdf = pdfplumber.open(io.BytesIO(pdf_bytes))
    page = next((p for p in pdf.pages if GROUP_MARKER in (p.extract_text() or "")), None)
    if page is None:
        sys.exit(f"В PDF нет страницы с группой {GROUP_MARKER}")
    table = page.find_tables()[0]
    words = page.extract_words(x_tolerance=1)

    def cell_text(bbox):
        if not bbox:
            return ""
        x0, top, x1, bottom = bbox
        inside = [w for w in words
                  if x0 <= (w["x0"] + w["x1"]) / 2 <= x1 and top <= (w["top"] + w["bottom"]) / 2 <= bottom]
        inside.sort(key=lambda w: (round(w["top"]), w["x0"]))
        return clean(" ".join(w["text"] for w in inside))

    lessons, cur_date = [], None
    for row in table.rows:
        c = row.cells  # 6 колонок: день | время | гр1 | ауд1 | гр2 | ауд2; объединённые ячейки = None
        m = DATE_RE.search(cell_text(c[0]))
        if m:
            cur_date = dt.datetime.strptime(m.group(), "%d.%m.%Y").date()
        tm = TIME_RE.fullmatch(cell_text(c[1]))
        if not tm or cur_date is None:
            continue
        h1, m1, h2, m2 = tm.groups()
        start = dt.datetime.combine(cur_date, dt.time(int(h1), int(m1)))
        end = (dt.datetime.combine(cur_date, dt.time(int(h2), int(m2))) if h2
               else start + dt.timedelta(minutes=LESSON_MINUTES))
        if c[3] is None:  # общая пара для обеих подгрупп: одна ячейка на гр1+гр2, аудитория справа
            title, room = cell_text(c[2]), cell_text(c[5])
        elif subgroup == 1:
            title, room = cell_text(c[2]), cell_text(c[3])
        else:
            title, room = cell_text(c[4]), cell_text(c[5])
        if not title or title.lower().startswith("кураторск"):
            continue
        lessons.append((start, end, title, room))
    return lessons


def esc(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build_event(start, end, title, room, alarm_min):
    # название без скобок: "Предмет (практ.зан) (Фамилия И.О.)" -> "Предмет"
    name = re.sub(r"\s*\([^)]*\)", "", title).strip()
    uid = hashlib.md5(f"{start}{title}".encode()).hexdigest()
    desc = title
    if end - start >= dt.timedelta(minutes=90):
        b0 = start + dt.timedelta(minutes=BREAK_AFTER)
        desc += f"\nПеремена {b0:%H:%M}-{b0 + dt.timedelta(minutes=BREAK_LEN):%H:%M}"
    alarm = "Следующая пара - " + name + (f", ауд. {room}" if room else "")
    fmt = "%Y%m%dT%H%M%S"
    ev = ["BEGIN:VEVENT", f"UID:{uid}@agatu-schedule", "DTSTAMP:20260101T000000Z",  # DTSTAMP фиксирован, чтобы файл не менялся зря
          f"DTSTART;TZID={TZ}:{start.strftime(fmt)}", f"DTEND;TZID={TZ}:{end.strftime(fmt)}",
          f"SUMMARY:{esc(name)}"]
    teacher = re.findall(r"\(([^()]*\w\.\w\.)\)", title)  # "(Фамилия И.О.)"
    place = " · ".join(filter(None, [f"ауд. {room}" if room else "", teacher[-1] if teacher else ""]))
    if place:
        ev.append(f"LOCATION:{esc(place)}")
    ev += [f"DESCRIPTION:{esc(desc)}",
           "BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{esc(alarm)}", f"TRIGGER:-PT{alarm_min}M", "END:VALARM",
           "END:VEVENT"]
    return ev


def category(title):
    t = title.lower()
    if re.search(r"дфк|диф\w*\.?\s*зач", t):
        return "dfk"
    if "теория" in t:
        return "theory"
    return "practice"  # (практ.зан) и занятия без пометки (например, физкультура)


def build_digest(lessons):
    """По событию на весь день с перечнем пар: в Google на него ставится одно утреннее уведомление."""
    days = {}
    for lesson in sorted(lessons):
        days.setdefault(lesson[0].date(), []).append(lesson)
    out = []
    for day, items in days.items():
        short = [f"{s:%H:%M} {re.sub(r'\s*\([^)]*\)', '', t).strip()}" + (f" ({r})" if r else "")
                 for s, e, t, r in items]
        full = [f"{s:%H:%M}-{e:%H:%M} {t}" + (f", ауд. {r}" if r else "") for s, e, t, r in items]
        out += ["BEGIN:VEVENT", f"UID:digest-{day:%Y%m%d}@agatu-schedule", "DTSTAMP:20260101T000000Z",
                f"DTSTART;VALUE=DATE:{day:%Y%m%d}", f"DTEND;VALUE=DATE:{day + dt.timedelta(days=1):%Y%m%d}",
                f"SUMMARY:{esc(' · '.join(short))}", f"DESCRIPTION:{esc(chr(10).join(full))}",
                "TRANSP:TRANSPARENT", "END:VEVENT"]
    return out


def build_ics(lessons, alarm_min, cal_name, extra=()):
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//agatu-schedule//RU", "CALSCALE:GREGORIAN",
           f"X-WR-CALNAME:{cal_name}", f"X-WR-TIMEZONE:{TZ}",
           "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H",
           "BEGIN:VTIMEZONE", f"TZID:{TZ}", "BEGIN:STANDARD", "DTSTART:19700101T000000",
           "TZOFFSETFROM:+0900", "TZOFFSETTO:+0900", "TZNAME:YAKT", "END:STANDARD", "END:VTIMEZONE"]
    for lesson in lessons:
        out += build_event(*lesson, alarm_min)
    out += extra
    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subgroup", type=int, default=1)
    ap.add_argument("--pdf")
    ap.add_argument("--alarm", type=int, default=15)
    ap.add_argument("--out-dir", default=".")
    a = ap.parse_args()
    data = open(a.pdf, "rb").read() if a.pdf else fetch(find_pdf_url())
    lessons = parse(data, a.subgroup)
    for start, end, title, room in lessons:
        print(f"{start:%Y-%m-%d %H:%M}-{end:%H:%M} | {category(title):8} | {title} | {room}")
    for key, label in CATEGORIES.items():
        part = [x for x in lessons if category(x[2]) == key]
        path = os.path.join(a.out_dir, f"{key}.ics")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(build_ics(part, a.alarm, f"АГАТУ: {label}", build_digest(lessons) if key == "dfk" else ()))
        print(f"{path}: {len(part)}")


if __name__ == "__main__":
    main()
