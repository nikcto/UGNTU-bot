import logging
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from schedule_data import SCHEDULE, DAY_NAMES, DAY_NAMES_SHORT

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = "YOUR_BOT_TOKEN_HERE"


def parse_date(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%d.%m.%Y")


def get_schedule_for_date(date: datetime) -> list[dict]:
    date_str = date.strftime("%d.%m.%Y")
    return sorted(
        [e for e in SCHEDULE if e["date"] == date_str],
        key=lambda x: x["time"],
    )


def format_lesson(lesson: dict) -> str:
    room = f"  {lesson['room']}" if lesson.get("room") else ""
    teacher = f"\n  {lesson['teacher']}" if lesson.get("teacher") else ""
    return f"⏰ {lesson['time']}\n📚 {lesson['subject']} ({lesson['type']}){teacher}{room}"


def format_day(date: datetime) -> str:
    day_name = DAY_NAMES[date.weekday()]
    date_str = date.strftime("%d.%m.%Y")
    lessons = get_schedule_for_date(date)

    header = f"📅 *{day_name}*  {date_str}\n"
    if not lessons:
        return header + "\n_Выходной_"

    lessons_text = "\n\n".join(format_lesson(l) for l in lessons)
    return header + "\n" + lessons_text


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [
            InlineKeyboardButton("Сегодня", callback_data="today"),
            InlineKeyboardButton("Завтра", callback_data="tomorrow"),
        ],
        [
            InlineKeyboardButton("Эта неделя", callback_data="week"),
            InlineKeyboardButton("Следующая неделя", callback_data="next_week"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "🎓 *Бот расписания БНИ-26-01*\n\n"
        "Команды:\n"
        "/today — расписание на сегодня\n"
        "/tomorrow — расписание на завтра\n"
        "/week — расписание на эту неделю\n"
        "/next_week — расписание на следующую неделю\n"
        "/day пн/вт/ср/чт/пт/сб — расписание на день недели\n"
        "/date ДД.ММ.ГГГГ — расписание на конкретную дату\n\n"
        "Или нажми на кнопку:"
    )
    await update.message.reply_text(
        text, reply_markup=reply_markup, parse_mode="Markdown"
    )


async def send_schedule(update: Update, date: datetime) -> None:
    text = format_day(date)
    keyboard = [
        [
            InlineKeyboardButton("◀️ Пред. день", callback_data=f"prev:{date.strftime('%d.%m.%Y')}"),
            InlineKeyboardButton("След. день ▶️", callback_data=f"next:{date.strftime('%d.%m.%Y')}"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        text, reply_markup=reply_markup, parse_mode="Markdown"
    )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_schedule(update, datetime.now())


async def cmd_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_schedule(update, datetime.now() + timedelta(days=1))


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    today = datetime.now()
    monday = today - timedelta(days=today.weekday())
    text = "📅 *Расписание на неделю*\n"
    for i in range(6):
        day = monday + timedelta(days=i)
        day_text = format_day(day)
        text += "\n\n" + day_text
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_next_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    today = datetime.now()
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=1)
    text = "📅 *Расписание на следующую неделю*\n"
    for i in range(6):
        day = monday + timedelta(days=i)
        day_text = format_day(day)
        text += "\n\n" + day_text
    await update.message.reply_text(text, parse_mode="Markdown")


DAY_MAP = {
    "пн": 0, "понедельник": 0,
    "вт": 1, "вторник": 1,
    "ср": 2, "среда": 2,
    "чт": 3, "четверг": 3,
    "пт": 4, "пятница": 4,
    "сб": 5, "суббота": 5,
    "вс": 6, "воскресенье": 6,
}


async def cmd_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Укажи день недели: /day пн, /day среда и т.д."
        )
        return

    day_input = context.args[0].lower()
    target_weekday = DAY_MAP.get(day_input)
    if target_weekday is None:
        await update.message.reply_text(
            "Неизвестный день. Используй: пн, вт, ср, чт, пт, сб"
        )
        return

    today = datetime.now()
    days_ahead = target_weekday - today.weekday()
    if days_ahead < 0:
        days_ahead += 7
    target_date = today + timedelta(days=days_ahead)
    await send_schedule(update, target_date)


async def cmd_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Укажи дату: /date 15.09.2026")
        return

    try:
        target_date = parse_date(context.args[0])
    except ValueError:
        await update.message.reply_text("Неверный формат. Используй: ДД.ММ.ГГГГ")
        return

    await send_schedule(update, target_date)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "today":
        date = datetime.now()
    elif data == "tomorrow":
        date = datetime.now() + timedelta(days=1)
    elif data == "week":
        today = datetime.now()
        monday = today - timedelta(days=today.weekday())
        text = "📅 *Расписание на неделю*\n"
        for i in range(6):
            day = monday + timedelta(days=i)
            text += "\n\n" + format_day(day)
        await query.edit_message_text(text, parse_mode="Markdown")
        return
    elif data == "next_week":
        today = datetime.now()
        monday = today - timedelta(days=today.weekday()) + timedelta(weeks=1)
        text = "📅 *Расписание на следующую неделю*\n"
        for i in range(6):
            day = monday + timedelta(days=i)
            text += "\n\n" + format_day(day)
        await query.edit_message_text(text, parse_mode="Markdown")
        return
    elif data.startswith("prev:"):
        date_str = data.split(":")[1]
        date = parse_date(date_str) - timedelta(days=1)
    elif data.startswith("next:"):
        date_str = data.split(":")[1]
        date = parse_date(date_str) + timedelta(days=1)
    else:
        return

    text = format_day(date)
    keyboard = [
        [
            InlineKeyboardButton(
                "◀️ Пред. день", callback_data=f"prev:{date.strftime('%d.%m.%Y')}"
            ),
            InlineKeyboardButton(
                "След. день ▶️", callback_data=f"next:{date.strftime('%d.%m.%Y')}"
            ),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        text, reply_markup=reply_markup, parse_mode="Markdown"
    )


def main() -> None:
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("next_week", cmd_next_week))
    app.add_handler(CommandHandler("day", cmd_day))
    app.add_handler(CommandHandler("date", cmd_date))
    app.add_handler(CallbackQueryHandler(callback_handler))

    print("Бот запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()
