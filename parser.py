"""
Парсер расписания с https://raspisanie.rusoil.net

Сайт — React-приложение: расписание подгружается в браузере через JS
уже ПОСЛЕ загрузки страницы. Поэтому обычный requests.get() вернёт
пустой HTML. Используем Playwright (headless Chromium), чтобы
дождаться отрисовки, а затем парсим готовый DOM через BeautifulSoup.
"""

import asyncio
import re
from dataclasses import dataclass, field
from typing import Optional

from bs4 import BeautifulSoup
from bs4.element import Tag
from playwright.async_api import async_playwright

# URL для группы БНИ-26-01 (id и параметры group взяты из ссылки сайта).
# Если понадобится другая группа — достаточно поменять "value"/"GRUPPA"
# на нужное имя, id менять не обязательно если сайт сам его переопределит,
# но надёжнее подставить свой id (смотри /?page=schedule&search=... в браузере).
SCHEDULE_URL = (
    "https://raspisanie.rusoil.net/?page=schedule&search="
    "%7B%22value%22%3A%22%D0%91%D0%9D%D0%98-26-01%22%2C%22id%22%3A157710%2C"
    "%22FILIAL%22%3A1%2C%22GRUPPA%22%3A%22%D0%91%D0%9D%D0%98-26-01%22%2C"
    "%22BELLFAK%22%3A1%2C%22FOB%22%3A1%7D"
)

TIME_RE = re.compile(r"\d+\s+\S+\s*‧\s*\d{2}:\d{2}\s*[–-]\s*\d{2}:\d{2}")
SUBGROUP_RE = re.compile(r"^\d+\s+подгруппа$")


@dataclass
class Lesson:
    title: str
    type_time: Optional[str] = None
    subgroup: Optional[str] = None
    location_teacher: Optional[str] = None
    changed: bool = False
    no_class: bool = False


@dataclass
class Day:
    name: str
    lessons: list = field(default_factory=list)


def _has_classes(tag: Tag, *classes: str) -> bool:
    tag_classes = tag.get("class") or []
    return all(c in tag_classes for c in classes)


def parse_schedule_html(html: str) -> list:
    """Парсит уже отрендеренный HTML страницы расписания в список Day."""
    soup = BeautifulSoup(html, "html.parser")
    days: list = []

    # Каждый день — это <div class="flex flex-col"> с вложенным
    # <div class="flex flex-col">, где первый ребёнок — "шапка" дня
    # (класс содержит "sticky" и "top-[10px]"), а остальные дети —
    # карточки пар.
    for outer in soup.find_all("div", recursive=True):
        if not _has_classes(outer, "flex", "flex-col"):
            continue
        inner = outer.find("div", recursive=False)
        if inner is None or not _has_classes(inner, "flex", "flex-col"):
            continue

        children = inner.find_all("div", recursive=False)
        if not children:
            continue

        header = children[0]
        header_classes = header.get("class") or []
        if not any("sticky" in c for c in header_classes):
            continue

        day_name = header.get_text(strip=True)
        day = Day(name=day_name)

        for card in children[1:]:
            blocks = [d.get_text(" ", strip=True) for d in card.find_all("div", recursive=False)]
            blocks = [b for b in blocks if b]
            if not blocks:
                continue

            title = blocks[0]

            if title == "Нет занятий":
                day.lessons.append(Lesson(title=title, no_class=True))
                continue

            lesson = Lesson(title=title)
            for block in blocks[1:]:
                if block == "изменено":
                    lesson.changed = True
                elif SUBGROUP_RE.match(block):
                    lesson.subgroup = block
                elif TIME_RE.search(block):
                    lesson.type_time = block
                elif lesson.location_teacher is None:
                    lesson.location_teacher = block

            day.lessons.append(lesson)

        # Пропускаем случайные пустые "flex flex-col" совпадения
        if day.lessons:
            days.append(day)

    return days


async def fetch_schedule(url: str = SCHEDULE_URL, timeout_ms: int = 20000) -> str:
    """Открывает страницу в headless-браузере и возвращает готовый HTML
    после того, как JS отрисовал расписание."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        # даём React немного времени на дорисовку карточек
        try:
            await page.wait_for_selector('div:has-text("подгруппа"), div.title', timeout=5000)
        except Exception:
            pass
        html = await page.content()
        await browser.close()
        return html


async def get_schedule(url: str = SCHEDULE_URL) -> list:
    html = await fetch_schedule(url)
    return parse_schedule_html(html)


def format_day(day: Day) -> str:
    lines = [f"📅 <b>{day.name}</b>"]
    for lesson in day.lessons:
        if lesson.no_class:
            lines.append("  Нет занятий")
            continue
        line = f"  • <b>{lesson.title}</b>"
        if lesson.type_time:
            line += f"\n     {lesson.type_time}"
        if lesson.subgroup:
            line += f" ({lesson.subgroup})"
        if lesson.location_teacher:
            line += f"\n     {lesson.location_teacher}"
        if lesson.changed:
            line += "\n     ⚠️ изменено"
        lines.append(line)
    return "\n".join(lines)


def format_schedule(days: list) -> list:
    """Возвращает список текстовых сообщений (по одному на день),
    чтобы не упереться в лимит длины сообщения Telegram."""
    return [format_day(d) for d in days]


if __name__ == "__main__":
    async def _main():
        days = await get_schedule()
        for d in days:
            print(format_day(d))
            print()

    asyncio.run(_main())
