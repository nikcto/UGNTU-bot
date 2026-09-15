"""
Единый бот расписания.

Каждые CHECK_INTERVAL_SECONDS (10 минут):
  1) забирает расписание с raspisanie.rusoil.net через внутренний API
     (обычные HTTP-запросы, без браузера)
  2) сравнивает с предыдущим снимком расписания (хранится в Supabase Storage)
  3) если есть изменения — шлёт уведомление в Telegram

Каждые PUBLISH_INTERVAL_SECONDS (1 час):
  4) собирает .ics и кладёт его в публичный бакет Supabase Storage

Нужные переменные окружения (.env локально / переменные окружения на Railway):
    TELEGRAM_BOT_TOKEN — токен бота, выданный BotFather
    TELEGRAM_CHAT_ID   — id чата/пользователя, куда слать уведомления

requirements.txt для этого сервиса:
    requests
    icalendar
    supabase
    python-dotenv
"""
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from hashlib import md5

import requests
from dotenv import load_dotenv
from icalendar import Calendar, Event

load_dotenv()  # локально подхватит .env; на Railway файла нет — просто ничего не делает

# --- конфиг ---

SUPABASE_URL = "https://pobepdbenznpdpgobwli.supabase.co".rstrip("/")
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBvYmVwZGJlbnpucGRwZ29id2xpIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4ODg4ODk3MywiZXhwIjoyMTA0NDY0OTczfQ.pFrVaNnhvN0F7mcfrJ1iQEVwVkoizDmwwhBVt0t-5gg"
BUCKET = "calendar"
FILENAME = "schedule.ics"
STATE_FILENAME = "schedule_state.json"  # снимок расписания для отслеживания изменений
BASE_URL = "https://raspisanie.rusoil.net"
GROUP_NAME = "БЦШ02-26-02"
GROUP_ID = 163685

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Якорь для перевода "номер недели + день недели" в календарную дату.
# 09.09.2026 — среда 2-й недели -> понедельник 2-й недели = 07.09.2026.
# Пересчитать вручную, если сайт сбросит нумерацию недель (новый семестр).
ANCHOR_WEEK = 2
ANCHOR_MONDAY = date(2026, 9, 7)

MAX_WEEK = 30
EMPTY_WEEKS_TO_STOP = 3
CHECK_INTERVAL_SECONDS = 10 * 60  # проверка изменений расписания — каждые 10 минут
PUBLISH_INTERVAL_SECONDS = 60 * 60  # обновление .ics в Supabase — раз в час

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Gecko/20100101 Firefox/155.0",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/x-www-form-urlencoded",
}

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


# --- шаг 3: публикация в Supabase Storage (напрямую через REST, без клиента) ---

def publish(ics_bytes: bytes) -> str:
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{FILENAME}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "text/calendar; charset=utf-8",
        "x-upsert": "true",  # перезаписать, если файл уже существует
    }
    resp = requests.post(upload_url, headers=headers, data=ics_bytes)
    if not resp.ok:
        print(f"Supabase Storage ответил {resp.status_code}: {resp.text}")
    resp.raise_for_status()
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{FILENAME}"


# --- шаг 4: снимок расписания в Supabase Storage (для отслеживания изменений) ---

def fetch_state() -> dict[str, dict]:
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{STATE_FILENAME}"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    resp = requests.get(url, headers=headers)
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()
    lessons = resp.json()
    return {lesson["uid"]: lesson for lesson in lessons}


def save_state(lessons: list[dict]) -> None:
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{STATE_FILENAME}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "x-upsert": "true",
    }
    resp = requests.post(upload_url, headers=headers, data=json.dumps(lessons, ensure_ascii=False).encode("utf-8"))
    if not resp.ok:
        print(f"Supabase Storage (state) ответил {resp.status_code}: {resp.text}")
    resp.raise_for_status()


# --- шаг 5: сравнение расписаний и уведомления в Telegram ---

def format_lesson(lesson: dict) -> str:
    parts = [lesson["date"], lesson["time"], f"{lesson['subject']} ({lesson['type']})"]
    if lesson.get("teacher"):
        parts.append(lesson["teacher"])
    if lesson.get("room"):
        parts.append(f"ауд. {lesson['room']}")
    return " | ".join(parts)


def compute_diff(prev: dict[str, dict], curr: dict[str, dict]) -> list[str]:
    lines: list[str] = []

    added = sorted(curr.keys() - prev.keys(), key=lambda u: (curr[u]["date"], curr[u]["time"]))
    for uid in added:
        lines.append(f"➕ Добавлено: {format_lesson(curr[uid])}")

    removed = sorted(prev.keys() - curr.keys(), key=lambda u: (prev[u]["date"], prev[u]["time"]))
    for uid in removed:
        lines.append(f"➖ Отменено: {format_lesson(prev[uid])}")

    changed = sorted(
        (uid for uid in curr.keys() & prev.keys() if curr[uid] != prev[uid]),
        key=lambda u: (curr[u]["date"], curr[u]["time"]),
    )
    for uid in changed:
        lines.append(f"✏️ Изменено:\n   было: {format_lesson(prev[uid])}\n   стало: {format_lesson(curr[uid])}")

    return lines


def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Пропускаю отправку в Telegram: не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    max_len = 3500  # запас от лимита Telegram в 4096 символов
    chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)] or [text]
    for chunk in chunks:
        resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": chunk})
        if not resp.ok:
            print(f"Telegram ответил {resp.status_code}: {resp.text}")


# --- цикл ---

def check_for_changes() -> list[dict]:
    """Забирает текущее расписание, сравнивает с сохранённым снимком и уведомляет об изменениях."""
    lessons = fetch_all_lessons()
    curr_state = {lesson["uid"]: lesson for lesson in lessons}
    prev_state = fetch_state()

    if prev_state:  # не спамим уведомлением при первом запуске / пустом снимке
        diff_lines = compute_diff(prev_state, curr_state)
        if diff_lines:
            send_telegram_message("📅 Изменения в расписании:\n\n" + "\n".join(diff_lines))
            print(f"Найдено изменений: {len(diff_lines)}")

    if curr_state != prev_state:
        save_state(lessons)

    return lessons


def main() -> None:
    last_publish = 0.0
    while True:
        try:
            lessons = check_for_changes()

            now = time.monotonic()
            if now - last_publish >= PUBLISH_INTERVAL_SECONDS:
                ics_bytes = build_ics(lessons)
                url = publish(ics_bytes)
                print(f"Опубликовано {len(lessons)} занятий -> {url}")
                last_publish = now
        except Exception as e:
            print(f"Ошибка: {e}")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
