"""
Telegram-бот: присылает расписание группы БНИ-26-01 с
https://raspisanie.rusoil.net

Команды:
  /start     — приветствие
  /schedule  — прислать расписание целиком (по дням)
  /today     — прислать расписание только на сегодня

Запуск:
  1) pip install -r requirements.txt
  2) playwright install chromium
  3) задать переменную окружения BOT_TOKEN (токен от @BotFather)
  4) python bot.py
"""

import asyncio
import logging
import os
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

from parser import get_schedule, format_schedule, format_day

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8660024020:AAFijdCAcBUKkMKGebmAhYeMtkHsTKJJTuA")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Русские названия дней недели, чтобы можно было сматчить "сегодня"
WEEKDAY_NAMES = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
]


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я присылаю расписание группы БНИ-26-01.\n\n"
        "/schedule — расписание на все дни\n"
        "/today — расписание на сегодня"
    )


@dp.message(Command("schedule"))
async def cmd_schedule(message: Message):
    status = await message.answer("Гружу расписание, секунду…")
    try:
        days = await get_schedule()
    except Exception as e:
        logging.exception("Ошибка при получении расписания")
        await status.edit_text(f"Не получилось получить расписание: {e}")
        return

    if not days:
        await status.edit_text("Расписание не найдено (сайт мог изменить формат страницы).")
        return

    await status.delete()
    for chunk in format_schedule(days):
        await message.answer(chunk, parse_mode="HTML")


@dp.message(Command("today"))
async def cmd_today(message: Message):
    status = await message.answer("Гружу расписание, секунду…")
    try:
        days = await get_schedule()
    except Exception as e:
        logging.exception("Ошибка при получении расписания")
        await status.edit_text(f"Не получилось получить расписание: {e}")
        return

    today_name = WEEKDAY_NAMES[datetime.now().weekday()]
    today_day = next((d for d in days if d.name.lower().startswith(today_name)), None)

    await status.delete()
    if today_day is None:
        await message.answer("На сегодня пар не нашёл (или сегодня выходной вне расписания).")
        return

    await message.answer(format_day(today_day), parse_mode="HTML")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
