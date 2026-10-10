"""
Единый бот расписания — поддерживает несколько групп.

Каждые CHECK_INTERVAL_SECONDS (10 минут), для КАЖДОЙ настроенной группы:
  1) забирает расписание с raspisanie.rusoil.net через внутренний API
     (обычные HTTP-запросы, без браузера)
  2) сравнивает с предыдущим снимком расписания этой группы (хранится
     в Supabase Storage, свой снимок на каждую группу)
  3) если есть изменения — шлёт push-уведомление в ntfy (на айфон)

Каждые PUBLISH_INTERVAL_SECONDS (1 час) на группу (или сразу при изменении):
  4) собирает .ics и кладёт в публичный бакет Supabase Storage — сразу
     ЧЕТЫРЕ отдельных файла на эту группу: общий + по одному на
     лекции/практики/лабы. Разные файлы = разные подписки в календаре =
     разные цвета на айфоне.

При запуске бот сразу шлёт уведомление "работаю, всё збс" (в NTFY_ADMIN_TOPIC).

--- Группы и ИМЕНА ФАЙЛОВ (важно!) ---
Первая/основная группа задаётся как раньше, через GROUP_NAME/GROUP_ID, и её
файлы называются БЕЗ ПРЕФИКСА — ровно так же, как до появления поддержки
нескольких групп (schedule.ics, schedule_lectures.ics, schedule_practice.ics,
schedule_lab.ics). Те, кто уже подписался на эти ссылки в своём календаре,
ничего менять не должны — ссылки не меняются.

Любые ДОПОЛНИТЕЛЬНЫЕ группы задаются через EXTRA_GROUPS и получают файлы
С ПРЕФИКСОМ (slug_schedule.ics и т.д.) — у каждой группы свой набор из 4
файлов, не пересекающийся с основной группой и с другими дополнительными.

Нужные переменные окружения (задаются на хостинге — bothost/Railway — в
разделе Variables/Переменные окружения; локально можно положить в .env):
    SUPABASE_URL         — обязательно. Адрес проекта, вида https://xxx.supabase.co
    SUPABASE_SERVICE_KEY — обязательно. service_role JWT-ключ (не sb_secret_...,
                           см. Project Settings -> JWT Keys -> Legacy JWT Secret ->
                           "Go to API keys", там service_role в виде eyJ...).
    GROUP_NAME           — обязательно. Основная группа, название как на сайте,
                           например БЦШ02-26-02. Её файлы — без префикса (см. выше).
    GROUP_ID             — обязательно. id основной группы (число из search-параметра сайта).
    EXTRA_GROUPS         — опционально. Дополнительные группы, формат:
                           Название:id:slug,Название:id:slug,...
                           slug — короткий ярлык латиницей/цифрами (a-z, 0-9, _, -),
                           он идёт префиксом в именах файлов этой группы, например:
                           EXTRA_GROUPS=БНИ-26-01:157710:bni2601
                           -> файлы bni2601_schedule.ics, bni2601_schedule_lectures.ics, ...
                           Несколько групп разделяются запятой.
    NTFY_TOPIC           — публичный топик: изменения расписания, на него подписаны все
    NTFY_ADMIN_TOPIC     — приватный топик только для тебя: старт бота, ошибки.
                           Если не задать — эти сообщения просто не отправляются никуда
                           (тихо оседают в логах), в публичный топик они НЕ упадут.
    NTFY_SERVER          — опционально, свой сервер ntfy; по умолчанию https://ntfy.sh
    BUCKET               — опционально, имя бакета в Supabase Storage; по умолчанию calendar
    ANCHOR_WEEK, ANCHOR_MONDAY — опционально, якорь перевода недели в дату (см. ниже);
                           по умолчанию 2 и 2026-09-07. ANCHOR_MONDAY в формате YYYY-MM-DD.
                           Общий для всех групп (все группы одного вуза, один учебный график).

requirements.txt для этого сервиса:
    requests
    icalendar
    python-dotenv
    tzdata

--- Часовой пояс (важно для тех, кто открывает .ics не из Уфы) ---
К каждому времени явно привязан пояс LESSON_TZ (Asia/Yekaterinburg — это и
есть уфимское время, UTC+5; отдельной зоны "Europe/Ufa" в актуальной базе
IANA больше нет, она давно объединена с екатеринбургской). Требует пакет
tzdata в requirements.txt — на Windows без него зоны не резолвятся.

--- Отдельные календари по типу занятия ---
На каждую группу публикуется не один файл, а четыре — общий + только с
лекциями / только с практиками / только с лабами. Смысл: подписной
календарь в приложении "Календарь" на iPhone красится ЦЕЛИКОМ одним цветом
— разным цветом можно покрасить только РАЗНЫЕ подписки. Разбив расписание
на 4 файла и подписавшись на каждый отдельно, можно каждому назначить свой
цвет (например, лекции — синим, практики — зелёным, лабы — оранжевым).

Тип занятия (поле NVIDZANAT с сайта) распознаётся по подстроке в названии
(регистронезависимо): "лекц" -> лекции, "практ" -> практики, "лаб" -> лабы.
Если сайт вдруг начнёт присылать тип, не попадающий ни под одну из этих
подстрок (например "консультация", "экзамен") — такое занятие попадёт
только в общий файл, в тематические не попадёт.
"""
import json
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from hashlib import md5
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from icalendar import Calendar, Event

load_dotenv()  # локально подхватит .env; на Railway файла нет — просто ничего не делает

# --- конфиг ---

def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Не задана обязательная переменная окружения {name}. "
            f"Укажи её в настройках хостинга (Variables / Переменные окружения)."
        )
    return value


SUPABASE_URL = _require_env("SUPABASE_URL").rstrip("/")
SUPABASE_KEY = _require_env("SUPABASE_SERVICE_KEY")
BUCKET = os.getenv("BUCKET", "calendar")
BASE_URL = "https://raspisanie.rusoil.net"

# Часовой пояс, в котором на самом деле проходят пары (Уфа = Екатеринбургское время, UTC+5).
LESSON_TZ = ZoneInfo("Asia/Yekaterinburg")

# ntfy — сюда шлём push-уведомления на телефон.
# NTFY_TOPIC — публичный топик, на него подписаны все, кто пользуется календарём:
#              сюда идут только уведомления об изменениях в расписании (для всех групп).
# NTFY_ADMIN_TOPIC — отдельный приватный топик только для тебя: сюда идут
#              техническое: старт бота, ошибки. Если не задать —
#              эти уведомления просто не отправляются никуда (тихо оседают в логах),
#              а не падают в публичный топик по ошибке.
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "nikcto")
NTFY_ADMIN_TOPIC = os.getenv("NTFY_ADMIN_TOPIC", "surrad")
NTFY_TIMEOUT_SECONDS = 10
NTFY_MAX_ATTEMPTS = 3
NTFY_RETRY_BACKOFF_SECONDS = 3  # 3с, потом 6с, потом 9с между попытками

# Публичные push-уведомления об изменениях расписания (в NTFY_TOPIC) шлём
# только для ОСНОВНОЙ группы — её подписчики и так ждут эти уведомления.
# Дополнительные группы (EXTRA_GROUPS) по умолчанию НЕ шлют публичных
# уведомлений (чтобы не спамить в чужой топик), но их .ics всё равно
# считаются и публикуются как обычно. Поставь NOTIFY_EXTRA_GROUPS=1 в
# переменных окружения, если хочешь, чтобы и доп. группы тоже слали
# уведомления в тот же NTFY_TOPIC.
NOTIFY_EXTRA_GROUPS = os.getenv("NOTIFY_EXTRA_GROUPS", "0").strip() == "1"

# Якорь для перевода "номер недели + день недели" в календарную дату.
# 09.09.2026 — среда 2-й недели -> понедельник 2-й недели = 07.09.2026.
# Пересчитать вручную (и выставить новыми переменными окружения), если сайт
# сбросит нумерацию недель (новый семестр). Общий для всех групп.
ANCHOR_WEEK = int(os.getenv("ANCHOR_WEEK", "2"))
ANCHOR_MONDAY = date.fromisoformat(os.getenv("ANCHOR_MONDAY", "2026-09-07"))

MAX_WEEK = 30
EMPTY_WEEKS_TO_STOP = 3
CHECK_INTERVAL_SECONDS = 10 * 60  # проверка изменений расписания — каждые 10 минут
PUBLISH_INTERVAL_SECONDS = 60 * 60  # обновление .ics в Supabase — раз в час (на группу)
REQUEST_TIMEOUT_SECONDS = 20  # общий таймаут для запросов к сайту/Supabase

SCHEDULE_LOCK = threading.Lock()  # чтобы публикации разных групп не пересекались в Storage

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Gecko/20100101 Firefox/155.0",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/x-www-form-urlencoded",
}


# --- группы ---

def _is_lecture(t: str) -> bool:
    return "лекц" in t.lower()


def _is_practice(t: str) -> bool:
    return "практ" in t.lower()


def _is_lab(t: str) -> bool:
    return "лаб" in t.lower()


_TYPE_FILTERS = {
    "lectures": ("Лекции", _is_lecture),
    "practice": ("Практики", _is_practice),
    "lab": ("Лабораторные", _is_lab),
}


def build_calendars(name: str, filename_prefix: str) -> dict:
    """filename_prefix='' -> старые имена файлов без изменений (основная группа).
    filename_prefix='slug_' -> имена файлов с префиксом (дополнительные группы)."""
    calendars = {
        "all": {
            "filename": f"{filename_prefix}schedule.ics",
            "calname": f"Расписание {name}",
            "filter": None,
        }
    }
    for key, (label, filter_fn) in _TYPE_FILTERS.items():
        calendars[key] = {
            "filename": f"{filename_prefix}schedule_{key}.ics",
            "calname": f"Расписание {name} — {label}",
            "filter": filter_fn,
        }
    return calendars


_SLUG_RE_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789_-")


def _validate_slug(slug: str, raw_entry: str) -> None:
    if not slug or any(ch.lower() not in _SLUG_RE_ALLOWED for ch in slug):
        raise RuntimeError(
            f"EXTRA_GROUPS: некорректный slug {slug!r} в записи {raw_entry!r}. "
            f"slug может содержать только латинские буквы, цифры, '_' и '-'."
        )


def parse_groups() -> list[dict]:
    """Основная группа (GROUP_NAME/GROUP_ID) всегда идёт первой, с префиксом ''
    (старые имена файлов, ничего не меняется для текущих подписчиков).
    Дополнительные группы из EXTRA_GROUPS добавляются следом, с префиксом
    '<slug>_' в именах файлов и отдельным снимком состояния."""
    primary_name = _require_env("GROUP_NAME")
    primary_id = int(_require_env("GROUP_ID"))

    groups = [
        {
            "name": primary_name,
            "id": primary_id,
            "state_filename": "schedule_state.json",
            "calendars": build_calendars(primary_name, ""),
            "label": primary_name,  # для логов/уведомлений
            "notify": True,  # публичные push-уведомления об изменениях шлём только для основной группы
        }
    ]

    extra_raw = os.environ.get("EXTRA_GROUPS", "").strip()
    if extra_raw:
        seen_slugs = set()
        for entry in extra_raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(":")
            if len(parts) != 3:
                raise RuntimeError(
                    f"EXTRA_GROUPS: неверный формат записи {entry!r}, "
                    f"ожидается Название:id:slug"
                )
            name, gid_str, slug = (p.strip() for p in parts)
            _validate_slug(slug, entry)
            if slug in seen_slugs:
                raise RuntimeError(f"EXTRA_GROUPS: повторяющийся slug {slug!r}")
            seen_slugs.add(slug)
            try:
                gid = int(gid_str)
            except ValueError:
                raise RuntimeError(f"EXTRA_GROUPS: id должен быть числом в записи {entry!r}")

            groups.append(
                {
                    "name": name,
                    "id": gid,
                    "state_filename": f"{slug}_schedule_state.json",
                    "calendars": build_calendars(name, f"{slug}_"),
                    "label": name,
                    # дополнительные группы НЕ шлют публичные push-уведомления в NTFY_TOPIC
                    # (на него подписана аудитория основной группы) — только считают диффы
                    # и публикуют .ics; при желании включить уведомления и для них, см.
                    # комментарий у NOTIFY_EXTRA_GROUPS ниже.
                    "notify": NOTIFY_EXTRA_GROUPS,
                }
            )

    return groups


# --- шаг 1: скрейпинг ---

def make_session(group_name: str, group_id: int) -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    search_param = json.dumps(
        {
            "value": group_name,
            "id": group_id,
            "FILIAL": 1,
            "GRUPPA": group_name,
            "BELLFAK": 1,
            "FOB": 1,
        },
        ensure_ascii=False,
    )
    resp = session.get(
        f"{BASE_URL}/",
        params={"page": "schedule", "search": search_param},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return session


def fetch_week_raw(session: requests.Session, group_name: str, week: int) -> list[dict]:
    payload = json.dumps({"gruppa": group_name, "beginweek": week, "endweek": week}, ensure_ascii=False)
    resp = session.post(
        f"{BASE_URL}/origins/get_rasp_student",
        data=payload.encode("utf-8"),
        headers={"Referer": f"{BASE_URL}/?page=schedule", "Origin": BASE_URL},
        timeout=REQUEST_TIMEOUT_SECONDS,
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


def fetch_all_lessons(group_name: str, group_id: int) -> list[dict]:
    session = make_session(group_name, group_id)
    all_lessons: list[dict] = []
    empty_streak = 0

    for week in range(1, MAX_WEEK + 1):
        raw = fetch_week_raw(session, group_name, week)
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
    """Возвращает datetime с явно проставленным часовым поясом Уфы (UTC+5).
    "Голый" datetime без tzinfo каждый календарь трактует как своё локальное
    время — у людей в другом поясе пары показывались бы не в то время."""
    naive = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
    return naive.replace(tzinfo=LESSON_TZ)


def build_ics(lessons: list[dict], calname: str) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//Rusoil Schedule Bot//rusoil.net parser//RU")
    cal.add("version", "2.0")
    cal.add("X-WR-CALNAME", calname)
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

def publish_file(filename: str, data: bytes, content_type: str) -> str:
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{filename}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": content_type,
        "x-upsert": "true",  # перезаписать, если файл уже существует
    }
    resp = requests.post(upload_url, headers=headers, data=data, timeout=REQUEST_TIMEOUT_SECONDS)
    if not resp.ok:
        print(f"Supabase Storage ответил {resp.status_code} для {filename}: {resp.text}")
    resp.raise_for_status()
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{filename}"


def publish_all_calendars(group: dict, lessons: list[dict]) -> dict[str, str]:
    """Собирает и публикует все файлы группы (общий + по типам занятий).
    Возвращает {ключ_календаря: публичная_ссылка}."""
    urls: dict[str, str] = {}
    for key, cfg in group["calendars"].items():
        filter_fn = cfg["filter"]
        subset = lessons if filter_fn is None else [l for l in lessons if filter_fn(l["type"])]
        ics_bytes = build_ics(subset, cfg["calname"])
        urls[key] = publish_file(cfg["filename"], ics_bytes, "text/calendar; charset=utf-8")
    return urls


# --- шаг 4: снимок расписания в Supabase Storage (для отслеживания изменений) ---

def fetch_state(group: dict) -> dict[str, dict]:
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{group['state_filename']}"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)

    # Supabase Storage отдаёт 400 ИЛИ 404 на несуществующий объект (зависит от версии) —
    # при первом запуске файла-снимка ещё нет, это ожидаемо, не ошибка.
    if resp.status_code in (400, 404):
        return {}
    resp.raise_for_status()

    lessons = resp.json()
    return {lesson["uid"]: lesson for lesson in lessons}


def save_state(group: dict, lessons: list[dict]) -> None:
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{group['state_filename']}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "x-upsert": "true",
    }
    resp = requests.post(
        upload_url,
        headers=headers,
        data=json.dumps(lessons, ensure_ascii=False).encode("utf-8"),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if not resp.ok:
        print(f"Supabase Storage (state) ответил {resp.status_code}: {resp.text}")
    resp.raise_for_status()


# --- шаг 5: сравнение расписаний и push-уведомления в ntfy ---

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


def send_ntfy(text: str, title: str | None = None, priority: int = 3, tags: list[str] | None = None, topic: str | None = None) -> bool:
    """Шлёт push-уведомление в ntfy (на телефон). Использует JSON-публикацию,
    чтобы кириллица в заголовке/тексте не ломалась (обычные HTTP-заголовки
    ntfy требуют латиницы).

    topic — в какой топик слать; по умолчанию публичный NTFY_TOPIC (расписание,
    его видят все). Для служебных/админских сообщений передавайте
    topic=NTFY_ADMIN_TOPIC явно — здесь нет автоматического фолбэка на
    публичный топик, чтобы техническое случайно не улетело всем подписчикам.

    Публичный ntfy.sh время от времени рвёт соединение (SSLEOFError,
    RemoteDisconnected и т.п.) — это ожидаемо для бесплатного общего сервера.
    Поэтому здесь есть retry с задержкой, таймаут на каждый запрос, и —
    самое важное — функция НИКОГДА не бросает исключение наружу. В худшем
    случае она просто напечатает ошибку в лог и вернёт False, чтобы не
    уронить вызывающий код (включая обработку других ошибок).

    Возвращает True, если все части сообщения отправлены успешно.
    """
    target_topic = topic if topic is not None else NTFY_TOPIC
    if not target_topic:
        print(f"Пропускаю отправку в ntfy (топик не задан): {title or ''} {text}".strip())
        return False

    max_len = 3800  # запас от лимита сообщения ntfy (~4096 байт)
    chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)] or [text]

    all_ok = True
    for i, chunk in enumerate(chunks):
        payload = {
            "topic": target_topic,
            "message": chunk,
            "priority": priority,
        }
        if title:
            payload["title"] = title if len(chunks) == 1 else f"{title} ({i + 1}/{len(chunks)})"
        if tags:
            payload["tags"] = tags

        chunk_ok = False
        for attempt in range(1, NTFY_MAX_ATTEMPTS + 1):
            try:
                resp = requests.post(NTFY_SERVER, json=payload, timeout=NTFY_TIMEOUT_SECONDS)
                if resp.ok:
                    chunk_ok = True
                    break
                print(f"ntfy ответил {resp.status_code}: {resp.text} (попытка {attempt}/{NTFY_MAX_ATTEMPTS})")
            except requests.exceptions.RequestException as e:
                # Сюда попадают в том числе SSLEOFError и RemoteDisconnected —
                # это транспортные ошибки на стороне ntfy.sh, не баг в коде.
                print(f"Не удалось достучаться до ntfy: {e} (попытка {attempt}/{NTFY_MAX_ATTEMPTS})")

            if attempt < NTFY_MAX_ATTEMPTS:
                time.sleep(NTFY_RETRY_BACKOFF_SECONDS * attempt)

        if not chunk_ok:
            print(f"Не удалось отправить уведомление в ntfy после {NTFY_MAX_ATTEMPTS} попыток, пропускаю.")
            all_ok = False

    return all_ok


# --- шаг 6: обработка одной группы за один цикл ---

def check_group_for_changes(group: dict) -> tuple[list[dict], bool]:
    """Забирает текущее расписание ОДНОЙ группы, сравнивает с её снимком,
    уведомляет об изменениях и, если расписание реально изменилось, сразу же
    публикует все .ics этой группы (не дожидаясь часового цикла публикации)."""
    lessons = fetch_all_lessons(group["name"], group["id"])
    curr_state = {lesson["uid"]: lesson for lesson in lessons}
    prev_state = fetch_state(group)

    if prev_state:  # не спамим уведомлением при первом запуске / пустом снимке
        diff_lines = compute_diff(prev_state, curr_state)
        if diff_lines:
            if group.get("notify", True):
                title = f"Изменения в расписании ({group['label']})"
                send_ntfy("\n".join(diff_lines), title=title, priority=4, tags=["calendar"])
            print(f"[{group['label']}] Найдено изменений: {len(diff_lines)}")

    changed = curr_state != prev_state
    if changed:
        save_state(group, lessons)
        urls = publish_all_calendars(group, lessons)
        print(f"[{group['label']}] Расписание изменилось, .ics опубликованы немедленно:")
        for key, url in urls.items():
            print(f"  {key}: {url}")

    return lessons, changed


# --- цикл ---

def main() -> None:
    groups = parse_groups()
    group_names = ", ".join(g["label"] for g in groups)
    print(f"Бот запущен. Группы: {group_names}")

    send_ntfy(
        f"Бот расписания запущен и работает — всё збс. Группы: {group_names}",
        title="Бот в строю ✅",
        tags=["rocket"],
        topic=NTFY_ADMIN_TOPIC,
    )

    last_publish: dict[str, float] = {}

    while True:
        for group in groups:
            try:
                with SCHEDULE_LOCK:
                    lessons, changed = check_group_for_changes(group)
                now = time.monotonic()

                if changed:
                    # Уже опубликовано немедленно внутри check_group_for_changes —
                    # просто сбрасываем часовой таймер этой группы.
                    last_publish[group["label"]] = now
                elif now - last_publish.get(group["label"], 0.0) >= PUBLISH_INTERVAL_SECONDS:
                    # Изменений не было, но раз в час всё равно republish-имся на всякий
                    # случай (защита от рассинхрона, если что-то пошло не так раньше).
                    with SCHEDULE_LOCK:
                        urls = publish_all_calendars(group, lessons)
                    print(f"[{group['label']}] Опубликовано {len(lessons)} занятий:")
                    for key, url in urls.items():
                        print(f"  {key}: {url}")
                    last_publish[group["label"]] = now
            except Exception as e:
                # Ошибки по одной группе идут только тебе в админский топик, а не в
                # публичный — остальные подписчики никогда их не увидят. Если
                # NTFY_ADMIN_TOPIC не задан, send_ntfy сама тихо пропустит отправку
                # и просто напечатает сообщение в лог. Ошибка по одной группе не
                # прерывает обработку остальных групп в этом же цикле.
                print(f"[{group['label']}] Ошибка: {e}")
                try:
                    send_ntfy(
                        f"⚠️ [{group['label']}] {e}",
                        title="Ошибка бота",
                        priority=4,
                        tags=["warning"],
                        topic=NTFY_ADMIN_TOPIC,
                    )
                except Exception as notify_error:
                    print(f"Не удалось даже уведомить об ошибке: {notify_error}")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
