"""
Единый бот расписания.
Раз в N часов:
  1) забирает расписание с raspisanie.rusoil.net через внутренний API
     (обычные HTTP-запросы, без браузера)
  2) собирает из него .ics
  3) кладёт .ics в публичный бакет Supabase Storage

requirements.txt для этого сервиса:
    requests
    icalendar
    supabase
"""
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from hashlib import md5

import requests
from icalendar import Calendar, Event
from supabase import create_client

# --- конфиг ---

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]  # service_role, не anon
BUCKET = "calendar"
FILENAME = "schedule.ics"

BASE_URL = "https://raspisanie.rusoil.net"
GROUP_NAME = "БНИ-26-01"
GROUP_ID = 157710

# Якорь для перевода "номер недели + день недели" в календарную дату.
# 09.09.2026 — среда 2-й недели -> понедельник 2-й недели = 07.09.2026.
# Пересчитать вручную, если сайт сбросит нумерацию недель (новый семестр).
ANCHOR_WEEK = 2
ANCHOR_MONDAY = date(2026, 9, 7)

MAX_WEEK = 30
EMPTY_WEEKS_TO_STOP = 3
SYNC_INTERVAL_SECONDS = 6 * 60 * 60  # каждые 6 часов

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Gecko/20100101 Firefox/155.0",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/x-www-form-urlencoded",
}

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# --- шаг 1: скрейпинг ---

def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    search_param = json.dumps(
        {
            "value": GROUP_NAME,
            "id": GROUP_ID,
            "FILIAL": 1,
            "GRUPPA": GROUP_NAME,
            "BELLFAK": 1,
            "FOB": 1,
        },
        ensure_ascii=False,
    )
    resp = session.get(f"{BASE_URL}/", params={"page": "schedule", "search": search_param})
    resp.raise_for_status()
    return session


def fetch_week_raw(session: requests.Session, week: int) -> list[dict]:
    payload = json.dumps({"gruppa": GROUP_NAME, "beginweek": week, "endweek": week}, ensure_ascii=False)
    resp = session.post(
        f"{BASE_URL}/origins/get_rasp_student",
        data=payload.encode("utf-8"),
        headers={"Referer": f"{BASE_URL}/?page=schedule", "Origin": BASE_URL},
    )
    resp.raise_for_status()
    return resp.json()


def week_day_to_date(week: int, dayweek: int) -> date:
    monday = ANCHOR_MONDAY + timedelta(weeks=week - ANCHOR_WEEK)
    return monday + timedelta(days=dayweek - 1)


def make_uid(lesson: dict) -> str:
    key = f"{lesson['date']}_{lesson['time']}_{lesson['subject']}"
    return md5(key.encode("utf-8")).hexdigest()


def transform(raw_lessons: list[dict], week: int) -> list[dict]:
    out = []
    for item in raw_lessons:
        lesson_date = week_day_to_date(week, item["DAYWEEK"])
        start = item["START_TIME"][:5]
        end = item["END_TIME"][:5]
        lesson = {
            "date": lesson_date.strftime("%d.%m.%Y"),
            "time": f"{start}-{end}",
            "subject": item["NDISC"],
            "type": item["NVIDZANAT"],
            "teacher": item["TEACHER_NAME"],
            "room": item["AUD"],
        }
        lesson["uid"] = make_uid(lesson)
        out.append(lesson)
    return out


def fetch_all_lessons() -> list[dict]:
    session = make_session()
    all_lessons: list[dict] = []
    empty_streak = 0

    for week in range(1, MAX_WEEK + 1):
        raw = fetch_week_raw(session, week)
        if not raw:
            empty_streak += 1
            if empty_streak >= EMPTY_WEEKS_TO_STOP:
                break
            continue
        empty_streak = 0
        all_lessons.extend(transform(raw, week))

    return all_lessons


# --- шаг 2: сборка .ics ---

def parse_dt(date_str: str, time_str: str) -> datetime:
    return datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")


def build_ics(lessons: list[dict]) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//BNI-26-01 Schedule//rusoil.net parser//RU")
    cal.add("version", "2.0")
    cal.add("X-WR-CALNAME", "Расписание БНИ-26-01")
    cal.add("X-PUBLISHED-TTL", "PT6H")

    for lesson in lessons:
        start_str, end_str = lesson["time"].split("-")
        start = parse_dt(lesson["date"], start_str)
        end = parse_dt(lesson["date"], end_str)

        event = Event()
        event.add("uid", f"{lesson['uid']}@rusoil-schedule")
        event.add("summary", f"{lesson['subject']} ({lesson['type']})")
        event.add("dtstart", start)
        event.add("dtend", end)
        event.add("dtstamp", datetime.now(timezone.utc))
        if lesson.get("teacher"):
            event.add("description", lesson["teacher"])
        if lesson.get("room"):
            event.add("location", lesson["room"])

        cal.add_component(event)

    return cal.to_ical()


# --- шаг 3: публикация в Supabase Storage ---

def publish(ics_bytes: bytes) -> str:
    supabase.storage.from_(BUCKET).upload(
        FILENAME,
        ics_bytes,
        {"content-type": "text/calendar; charset=utf-8", "upsert": "true"},
    )
    return supabase.storage.from_(BUCKET).get_public_url(FILENAME)


# --- цикл ---

def run_once() -> None:
    lessons = fetch_all_lessons()
    ics_bytes = build_ics(lessons)
    url = publish(ics_bytes)
    print(f"Опубликовано {len(lessons)} занятий -> {url}")


def main() -> None:
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"Ошибка: {e}")
        time.sleep(SYNC_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
