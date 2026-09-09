"""
Бот «Подслушано» — анонимные истории с модерацией.

Запуск:
1) заполни файл .env (см. .env.example)
2) pip install -r requirements.txt
3) python bot.py

Команды пользователя (в личке):
/start          — приветствие и инструкция
/help           — список команд и как отправить историю
/story          — как отправить историю (напоминание)
/id             — узнать ID чата и свой ID (работает где угодно)

Команды админов (в группе админов):
/help           — справка для модераторов
/queue          — сколько историй в очереди
/pending        — список историй на модерации
/stats          — статистика бота
/banned         — список забаненных
/unban <id>     — разблокировать пользователя
/check          — диагностика доступов
/cancel         — отменить режим редактирования истории
/when           — расписание очереди: когда выйдет каждая история
/at <id> <время> — назначить точное время публикации
"""

import asyncio
import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
DATA_FILE = BASE_DIR / "data.json"

log = logging.getLogger("podslushano")


# =====================================================================
#  КОНФИГ: читаем из .env
# =====================================================================
def read_env_file(path: Path) -> Dict[str, str]:
    """Простой парсер .env без внешних зависимостей."""
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


class ConfigError(Exception):
    pass


class Config:
    def __init__(self, raw: Dict[str, str]) -> None:
        import os

        def get(name: str) -> str:
            # приоритет: переменная окружения -> .env
            return (os.getenv(name) or raw.get(name) or "").strip()

        self.bot_token = get("BOT_TOKEN")
        self.admin_chat_id_raw = get("ADMIN_CHAT_ID")
        self.channel_id_raw = get("CHANNEL_ID")
        self.tz_name = get("TZ_NAME") or "Europe/Moscow"
        self.cooldown = int(get("COOLDOWN_SECONDS") or 10)
        times = get("PUBLISH_TIMES") or "10:00,15:00,20:00"
        self.publish_times = [t.strip() for t in times.split(",") if t.strip()]

        self.admin_chat_id: Optional[int] = None
        self.channel_id: Any = None

    def validate(self) -> None:
        problems = []

        # --- токен ---
        if not self.bot_token:
            problems.append(
                "BOT_TOKEN пустой. Открой файл .env и вставь токен от @BotFather."
            )
        elif not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{30,}", self.bot_token):
            problems.append(
                "BOT_TOKEN выглядит неправильно.\n"
                "   Правильный формат:  123456789:AAHVBHw3uCUfDOr8kKT9FQPBB5ldrOJXl1o\n"
                "   Обрати внимание на ДВОЕТОЧИЕ после цифр — оно часто теряется при копировании.\n"
                f"   Сейчас в .env: {self.bot_token[:14]}..."
            )

        # --- id группы админов ---
        if not self.admin_chat_id_raw:
            problems.append(
                "ADMIN_CHAT_ID пустой.\n"
                "   Как узнать: добавь бота в свою группу админов, напиши там /id — "
                "бот пришлёт ID этой группы."
            )
        else:
            try:
                self.admin_chat_id = int(self.admin_chat_id_raw)
            except ValueError:
                problems.append(
                    f"ADMIN_CHAT_ID должен быть числом, а сейчас: {self.admin_chat_id_raw!r}\n"
                    "   Пример: -1002537199333"
                )
            else:
                if self.admin_chat_id > 0:
                    problems.append(
                        f"ADMIN_CHAT_ID = {self.admin_chat_id} похож на ID пользователя, "
                        "а не группы.\n"
                        "   ID групп всегда отрицательный, у супергрупп начинается с -100.\n"
                        "   Напиши /id в самой группе, чтобы получить верный ID."
                    )

        # --- канал ---
        if not self.channel_id_raw:
            problems.append(
                "CHANNEL_ID пустой.\n"
                "   Укажи @username канала (например @Storozh_Schol) "
                "или числовой ID вида -100..."
            )
        elif self.channel_id_raw.lstrip("-").isdigit():
            self.channel_id = int(self.channel_id_raw)
        elif self.channel_id_raw.startswith("@"):
            self.channel_id = self.channel_id_raw
        else:
            # пользователь забыл собаку — поправим сами
            self.channel_id = "@" + self.channel_id_raw
            log.warning(
                "CHANNEL_ID был без @ — использую %s. Лучше поправь .env.",
                self.channel_id,
            )

        # --- расписание ---
        for t in self.publish_times:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
                problems.append(
                    f"PUBLISH_TIMES содержит некорректное время {t!r}. Формат ЧЧ:ММ, например 10:00"
                )

        try:
            ZoneInfo(self.tz_name)
        except Exception:
            problems.append(f"TZ_NAME {self.tz_name!r} — неизвестный часовой пояс.")

        if problems:
            raise ConfigError(
                "\n\n❌ Бот не запущен — проблемы с настройками:\n\n"
                + "\n\n".join(f" • {p}" for p in problems)
                + f"\n\nФайл настроек: {ENV_FILE}\n"
                "Если файла нет — скопируй .env.example в .env и заполни.\n"
            )


# =====================================================================
#  ХРАНИЛИЩЕ
# =====================================================================
DEFAULT_DATA: Dict[str, Any] = {
    "counter": 0,
    "pending": {},
    "queue": [],
    "banned": [],
    "last_slot": "",
    "stats": {"received": 0, "published": 0, "rejected": 0, "edited": 0},
}


def load_data() -> Dict[str, Any]:
    if DATA_FILE.exists():
        try:
            loaded = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("data.json повреждён — начинаю с чистого хранилища")
        else:
            merged = json.loads(json.dumps(DEFAULT_DATA))
            merged.update(loaded)
            # достраиваем недостающие ключи статистики после обновления бота
            stats = dict(DEFAULT_DATA["stats"])
            stats.update(merged.get("stats") or {})
            merged["stats"] = stats
            return merged
    return json.loads(json.dumps(DEFAULT_DATA))  # глубокая копия


data: Dict[str, Any] = {}


def save_data() -> None:
    tmp = DATA_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_FILE)  # атомарная запись: файл не побьётся при сбое


def bump(stat: str, delta: int = 1) -> None:
    stats = data.setdefault("stats", {})
    stats[stat] = int(stats.get(stat, 0)) + delta


_last_message_at: Dict[int, float] = {}
# модератор -> ключ истории, которую он сейчас редактирует
_editing: Dict[int, str] = {}
# пользователь -> момент, когда он нажал /story (ждём от него историю)
_awaiting_story: Dict[int, float] = {}
# сколько секунд действует режим отправки после /story
STORY_WAIT_TTL = 15 * 60


def is_awaiting_story(user_id: int) -> bool:
    """True, если пользователь недавно нажал /story и ещё не прислал историю."""
    started = _awaiting_story.get(user_id)
    if started is None:
        return False
    if time.monotonic() - started > STORY_WAIT_TTL:
        _awaiting_story.pop(user_id, None)
        return False
    return True


# =====================================================================
#  ТЕКСТЫ
# =====================================================================
WELCOME_TEXT = (
    "Привет! 👋\n\n"
    "Это бот «Подслушано» — здесь твоя история попадёт в канал <b>анонимно</b>.\n\n"
    "<b>Как отправить историю:</b>\n"
    "1. Отправь команду /story\n"
    "2. Следующим сообщением пришли текст, фото или видео\n\n"
    "Без команды /story сообщения не принимаются — так мы боремся со спамом.\n\n"
    "<b>Команды:</b>\n"
    "/help — справка и список команд\n"
    "/story — как отправить историю\n"
    "/rules — правила публикации\n"
    "/id — узнать свой ID и ID чата\n\n"
    "После проверки модераторами история появится в канале. Автор не указывается. ✌️"
)

STORY_TEXT = (
    "✍️ <b>Жду твою историю</b>\n\n"
    "Пришли <b>следующим сообщением</b> текст, фото или видео (подпись тоже отправится).\n"
    "Одно сообщение — одна история, она сразу уйдёт модераторам.\n"
    "Когда её опубликуют, я пришлю уведомление.\n\n"
    f"Режим отправки активен {STORY_WAIT_TTL // 60} минут. Передумал — /cancel\n\n"
    "Отправляй анонимно — твоё имя и username в канал не попадают."
)

NEED_STORY_COMMAND_TEXT = (
    "Чтобы отправить историю, сначала отправь команду /story, "
    "а потом уже сам текст, фото или видео 🙌"
)

RULES_TEXT = (
    "📜 <b>Правила</b>\n\n"
    "• Без оскорблений, травли и угроз\n"
    "• Без личных данных других людей\n"
    "• Без рекламы и спама\n"
    "• Одна история — одно сообщение\n\n"
    "Модераторы могут отклонить историю или слегка отредактировать текст "
    "перед публикацией (например, убрать имена)."
)

HELP_USER_TEXT = (
    "ℹ️ <b>Справка</b>\n\n"
    "Чтобы отправить историю — отправь /story, а следующим сообщением "
    "пришли текст, фото или видео.\n\n"
    "<b>Команды:</b>\n"
    "/start — приветствие\n"
    "/help — эта справка\n"
    "/story — отправить историю\n"
    "/cancel — отменить отправку истории\n"
    "/rules — правила публикации\n"
    "/id — узнать свой ID и ID чата"
)

HELP_ADMIN_TEXT = (
    "🛠 <b>Справка для модераторов</b>\n\n"
    "<b>Команды:</b>\n"
    "/queue — сколько историй в очереди\n"
    "/pending — список историй на модерации\n"
    "/stats — статистика бота\n"
    "/banned — список забаненных\n"
    "/unban &lt;id&gt; — разблокировать пользователя\n"
    "/when — когда выйдет каждая история из очереди\n"
    "/at &lt;id&gt; &lt;время&gt; — назначить точное время (например /at 7 21:30)\n"
    "/check — диагностика доступов\n"
    "/cancel — выйти из режима редактирования\n"
    "/id — ID этого чата\n\n"
    "<b>Кнопки под каждой историей:</b>\n"
    "🚀 Опубликовать сейчас — сразу в канал\n"
    "🕐 В очередь — публикация по расписанию (бот сразу пишет, когда выйдет)\n"
    "🗓 Выбрать время публикации — выбрать точное время кнопками\n"
    "✏️ Изменить текст — переписать историю перед публикацией\n"
    "👁 Предпросмотр — показать, как выйдет в канале\n"
    "❌ Отклонить · 🚫 Заблокировать автора"
)


# =====================================================================
#  ВРЕМЯ ПУБЛИКАЦИИ
# =====================================================================
TIME_FMT = "%Y-%m-%d %H:%M"


def zone() -> ZoneInfo:
    return ZoneInfo(cfg.tz_name)


def now_local() -> datetime:
    return datetime.now(zone())


def parse_local(value: Optional[str]) -> Optional[datetime]:
    """Строка 'YYYY-MM-DD HH:MM' -> datetime с часовым поясом бота."""
    if not value:
        return None
    try:
        return datetime.strptime(value, TIME_FMT).replace(tzinfo=zone())
    except (ValueError, TypeError):
        return None


def upcoming_slots(count: int = 4) -> List[datetime]:
    """Ближайшие слоты из PUBLISH_TIMES (с учётом перехода на следующие дни)."""
    now = now_local().replace(second=0, microsecond=0)
    slots: List[datetime] = []
    day = 0
    while len(slots) < count and day < 30:
        base = now + timedelta(days=day)
        for raw in sorted(cfg.publish_times):
            hh, _, mm = raw.partition(":")
            try:
                moment = base.replace(hour=int(hh), minute=int(mm))
            except ValueError:
                continue
            if moment > now and moment not in slots:
                slots.append(moment)
        day += 1
    slots.sort()
    return slots[:count]


def auto_queue_keys() -> List[str]:
    """Ключи в очереди без точного врем      ни — они идут по расписанию."""
    return [
        k
        for k in data["queue"]
        if not (data["pending"].get(k) or {}).get("publish_at")
    ]


def eta_for_key(key: str) -> Tuple[Optional[datetime], str]:
    """(время публикации, режим) для истории в очереди."""
    item = data["pending"].get(key)
    if not item or key not in data["queue"]:
        return None, "none"
    exact = parse_local(item.get("publish_at"))
    if exact:
        return exact, "exact"
    keys = auto_queue_keys()
    if key not in keys:
        return None, "auto"
    index = keys.index(key)
    slots = upcoming_slots(index + 1)
    if len(slots) > index:
        return slots[index], "auto"
    return None, "auto"


def human_when(moment: Optional[datetime]) -> str:
    """'сегодня в 20:00 (через 2 ч 15 мин)'"""
    if moment is None:
        return "время пока неизвестно"
    now = now_local()
    if moment.date() == now.date():
        day = "сегодня"
    elif moment.date() == (now + timedelta(days=1)).date():
        day = "завтра"
    else:
        day = moment.strftime("%d.%m")
    minutes = int((moment - now).total_seconds() // 60)
    if minutes <= 0:
        left = "вот-вот"
    elif minutes < 60:
        left = f"через {minutes} мин"
    else:
        left = f"через {minutes // 60} ч {minutes % 60:02d} мин"
    return f"{day} в {moment.strftime('%H:%M')} ({left})"


def schedule_line(key: str) -> str:
    """Строка статуса для сообщения модерации."""
    moment, mode = eta_for_key(key)
    if mode == "none":
        return "Не в очереди"
    place = data["queue"].index(key) + 1
    if mode == "exact":
        return f"🕐 Запланировано на {human_when(moment)} — точное время"
    return f"🕐 В очереди, место {place} — выйдет {human_when(moment)} (по расписанию)"


def base_control_text(text: str) -> str:
    """Убирает ранее добавленные строки статуса, чтобы они не накапливались."""
    return text.split("\n\n")[0]


# =====================================================================
#  КЛАВИАТУРЫ
# =====================================================================
def moderation_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Опубликовать сейчас", callback_data=f"pub:{key}")],
            [InlineKeyboardButton(text="🕐 В очередь (по расписанию)", callback_data=f"que:{key}")],
            [InlineKeyboardButton(text="🗓 Выбрать время публикации", callback_data=f"tim:{key}")],
            [
                InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"edt:{key}"),
                InlineKeyboardButton(text="👁 Предпросмотр", callback_data=f"prv:{key}"),
            ],
            [
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rej:{key}"),
                InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"ban:{key}"),
            ],
        ]
    )


def queued_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Опубликовать сейчас", callback_data=f"pub:{key}")],
            [InlineKeyboardButton(text="🗓 Изменить время публикации", callback_data=f"tim:{key}")],
            [
                InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"edt:{key}"),
                InlineKeyboardButton(text="👁 Предпросмотр", callback_data=f"prv:{key}"),
            ],
            [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rej:{key}")],
        ]
    )


def time_keyboard(key: str) -> InlineKeyboardMarkup:
    """Выбор времени публикации прямо в чате."""
    rows: List[List[InlineKeyboardButton]] = []
    for moment in upcoming_slots(4):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📅 {human_when(moment)}",
                    callback_data=f"set:{key}:{moment.strftime('%Y-%m-%dT%H:%M')}",
                )
            ]
        )
    now = now_local().replace(second=0, microsecond=0)
    quick = [("+15 мин", 15), ("+1 час", 60), ("+3 часа", 180), ("+6 часов", 360)]
    row: List[InlineKeyboardButton] = []
    for label, minutes in quick:
        moment = now + timedelta(minutes=minutes)
        row.append(
            InlineKeyboardButton(
                text=f"⏱ {label} ({moment.strftime('%H:%M')})",
                callback_data=f"set:{key}:{moment.strftime('%Y-%m-%dT%H:%M')}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton(text="🔁 По расписанию (авто)", callback_data=f"aut:{key}")]
    )
    rows.append(
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"bck:{key}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# =====================================================================
#  СОЗДАЁМ БОТА
# =====================================================================
cfg = Config(read_env_file(ENV_FILE))


def build_dispatcher(cfg: Config) -> Dispatcher:
    dp = Dispatcher()

    # ---------- /id : помощник для получения ID ----------
    @dp.message(Command("id"))
    async def cmd_id(message: Message) -> None:
        chat = message.chat
        lines = [
            "🆔 Информация о чате",
            "",
            f"chat.id: {chat.id}",
            f"тип чата: {chat.type}",
        ]
        if chat.title:
            lines.append(f"название: {chat.title}")
        if chat.username:
            lines.append(f"username: @{chat.username}")
        if message.from_user:
            lines.append("")
            lines.append(f"твой user.id: {message.from_user.id}")

        if chat.type in ("group", "supergroup"):
            lines += ["", f"👉 Впиши в .env:  ADMIN_CHAT_ID={chat.id}"]
        elif chat.type == "channel":
            lines += ["", f"👉 Впиши в .env:  CHANNEL_ID={chat.id}"]
        await message.answer("\n".join(lines))

    # ---------- /help : работает везде ----------
    @dp.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        if message.chat.id == cfg.admin_chat_id:
            await message.answer(HELP_ADMIN_TEXT, parse_mode="HTML")
        else:
            await message.answer(HELP_USER_TEXT, parse_mode="HTML")

    # ---------- диагностика доступов ----------
    @dp.message(Command("check"), F.chat.id == cfg.admin_chat_id)
    async def cmd_check(message: Message) -> None:
        report = ["🔍 Проверка настроек", ""]
        report.append(f"ADMIN_CHAT_ID: {cfg.admin_chat_id} — ✅ совпал с этим чатом")
        try:
            chat = await message.bot.get_chat(cfg.channel_id)
            member = await message.bot.get_chat_member(chat.id, (await message.bot.me()).id)
            report.append(f"CHANNEL_ID: {cfg.channel_id} → «{chat.title}» ✅")
            report.append(f"права бота в канале: {member.status}")
            if member.status not in ("administrator", "creator"):
                report.append("⚠️ Бот должен быть АДМИНОМ канала с правом публикации.")
        except Exception as exc:
            report.append(f"CHANNEL_ID: {cfg.channel_id} — ❌ ошибка: {exc}")
            report.append("Добавь бота администратором канала и проверь CHANNEL_ID.")
        report.append("")
        report.append(f"Расписание: {', '.join(cfg.publish_times)} ({cfg.tz_name})")
        report.append(f"В очереди: {len(data['queue'])}")
        await message.answer("\n".join(report))

    # ---------- публикация ----------
    async def send_story(bot: Bot, chat_id: Any, item: Dict[str, Any]) -> None:
        """Отправляет историю (оригинальную или отредактированную) в чат."""
        text = (item.get("edited_text") or "").strip()
        content_type = item.get("content_type", "text")
        if text and content_type == "text":
            # текстовую историю после правки отправляем как новый текст
            await bot.send_message(chat_id, text)
        elif text:
            # у фото/видео правим подпись
            await bot.copy_message(
                chat_id=chat_id,
                from_chat_id=cfg.admin_chat_id,
                message_id=item["content_message_id"],
                caption=text,
            )
        else:
            await bot.copy_message(
                chat_id=chat_id,
                from_chat_id=cfg.admin_chat_id,
                message_id=item["content_message_id"],
            )

    async def publish(bot: Bot, key: str) -> bool:
        item = data["pending"].get(key)
        if not item:
            return False
        await send_story(bot, cfg.channel_id, item)
        try:
            await bot.send_message(item["user_id"], "Твоя история опубликована! 🎉")
        except Exception:
            pass  # пользователь мог заблокировать бота
        if key in data["queue"]:
            data["queue"].remove(key)
        data["pending"].pop(key, None)
        bump("published")
        save_data()
        return True

    # ---------- личка: /start ----------
    @dp.message(CommandStart(), F.chat.type == "private")
    async def cmd_start(message: Message) -> None:
        await message.answer(WELCOME_TEXT, parse_mode="HTML")

    # ---------- личка: /story и /rules ----------
    @dp.message(Command("story"), F.chat.type == "private")
    async def cmd_story(message: Message) -> None:
        user = message.from_user
        if user is None:
            return
        if user.id in data["banned"]:
            return  # молча игнорируем заблокированных
        # открываем окно приёма истории для этого пользователя
        _awaiting_story[user.id] = time.monotonic()
        await message.answer(STORY_TEXT, parse_mode="HTML")

    @dp.message(Command("cancel"), F.chat.type == "private")
    async def cmd_cancel_private(message: Message) -> None:
        user = message.from_user
        if user is None:
            return
        was_editing = _editing.pop(user.id, None)
        was_awaiting = _awaiting_story.pop(user.id, None)
        if was_editing:
            await message.answer("Режим редактирования отменён.")
        elif was_awaiting:
            await message.answer("Ок, отменил — историю не жду. Набери /story, когда будешь готов.")
        else:
            await message.answer("Отменять нечего 🙂 Чтобы отправить историю — /story")

    @dp.message(Command("rules"), F.chat.type == "private")
    async def cmd_rules(message: Message) -> None:
        await message.answer(RULES_TEXT, parse_mode="HTML")

    async def save_moderator_edit(message: Message, key: str, user) -> None:
        """Сохраняет новый текст истории, который прислал модератор."""
        new_text = (message.text or "").strip()
        if new_text.startswith("/"):
            return

        item = data["pending"].get(key)
        if not item:
            _editing.pop(user.id, None)
            await message.reply("История уже обработана — правка не сохранена.")
            return

        if item.get("content_type", "text") != "text" and len(new_text) > 1024:
            await message.reply(
                "Слишком длинная подпись для фото/видео (максимум 1024 символа). "
                "Сократи текст и пришли снова."
            )
            return

        item["edited_text"] = new_text
        item["edited_by"] = user.full_name
        bump("edited")
        _editing.pop(user.id, None)
        save_data()

        in_queue = key in data["queue"]
        preview = new_text if len(new_text) <= 500 else new_text[:500] + "…"
        await message.reply(
            f"✅ Текст истории #{key} обновлён ({user.full_name}).\n\n"
            f"Так она выйдет в канал:\n{preview}"
        )
        try:
            await message.bot.edit_message_reply_markup(
                chat_id=cfg.admin_chat_id,
                message_id=item["control_message_id"],
                reply_markup=queued_keyboard(key) if in_queue else moderation_keyboard(key),
            )
        except Exception:
            pass

    # ---------- личка: история ----------
    @dp.message(F.chat.type == "private", F.content_type.in_({"text", "photo", "video"}))
    async def on_story(message: Message) -> None:
        user = message.from_user
        if user is None:
            return

        # Если модератор нажал «Изменить текст», разрешаем прислать новый текст
        # не только в группе админов, но и в личку боту. Иначе такой текст
        # воспринимался как новая история/игнорировался и правка не сохранялась.
        edit_key = _editing.get(user.id)
        if edit_key and message.content_type == "text":
            await save_moderator_edit(message, edit_key, user)
            return

        if user.id in data["banned"]:
            return  # молча игнорируем заблокированных

        # ГЛАВНОЕ: историю принимаем только после команды /story — защита от спама
        if not is_awaiting_story(user.id):
            await message.answer(NEED_STORY_COMMAND_TEXT)
            return

        now = time.monotonic()
        if now - _last_message_at.get(user.id, 0.0) < cfg.cooldown:
            await message.answer("Не так быстро 🙂 Подожди немного перед следующей историей.")
            return
        _last_message_at[user.id] = now

        try:
            copied = await message.bot.copy_message(
                chat_id=cfg.admin_chat_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
        except Exception:
            log.exception("Не удалось скопировать историю в группу админов")
            await message.answer(
                "Не получилось отправить историю на модерацию 😔 Попробуй позже."
            )
            return

        # окно   ри  м   закрываем: следующая история — только после нового /story
        _awaiting_story.pop(user.id, None)

        data["counter"] += 1
        key = str(data["counter"])
        username = f"@{user.username}" if user.username else "без username"
        control = await message.bot.send_message(
            cfg.admin_chat_id,
            f"📝 Новая история #{key}\nОт: {username} (id: {user.id})",
            reply_markup=moderation_keyboard(key),
            reply_to_message_id=copied.message_id,
        )
        data["pending"][key] = {
            "user_id": user.id,
            "content_message_id": copied.message_id,
            "control_message_id": control.message_id,
            "content_type": message.content_type,
            "created_at": datetime.now(ZoneInfo(cfg.tz_name)).strftime("%Y-%m-%d %H:%M"),
        }
        bump("received")
        save_data()

        await message.answer(
            "Спасибо! История отправлена на модерацию. "
            "Если её одобрят — она появится в канале анонимно. ✌️\n"
            "Хочешь отправить ещё одну — снова набери /story"
        )

    # ---------- личка: остальное ----------
    @dp.message(F.chat.type == "private")
    async def on_other_content(message: Message) -> None:
        user = message.from_user
        if user is not None and is_awaiting_story(user.id):
            await message.answer("Я принимаю только текст, фото и видео 🙈 Пришли историю так.")
            return
        await message.answer(NEED_STORY_COMMAND_TEXT)

    # ---------- кнопки модерации ----------
    @dp.callback_query(F.data.startswith(("pub:", "que:", "rej:", "ban:", "edt:", "prv:")))
    async def on_moderation(call: CallbackQuery) -> None:
        if call.message is None or call.message.chat.id != cfg.admin_chat_id:
            await call.answer("Недоступно", show_alert=True)
            return

        action, key = (call.data or "").split(":", 1)
        item = data["pending"].get(key)
        if not item:
            await call.answer("История уже обработана", show_alert=True)
            return

        moderator = call.from_user.full_name
        control_text = base_control_text(call.message.text or f"История #{key}")

        if action == "pub":
            try:
                await publish(call.message.bot, key)
            except Exception as exc:
                log.exception("Ошибка публикации %s", key)
                await call.answer(f"Ошибка публикации: {exc}", show_alert=True)
                return
            await call.message.edit_text(f"{control_text}\n\n✅ Опубликовано ({moderator})")
            await call.answer("Опубликовано ✅")

        elif action == "que":
            if key in data["queue"] and not item.get("publish_at"):
                await call.answer("Уже в очереди", show_alert=True)
                return
            item.pop("publish_at", None)  # вернули в авторежим
            if key not in data["queue"]:
                data["queue"].append(key)
            save_data()
            await call.message.edit_text(
                f"{control_text}\n\n{schedule_line(key)}\nПоставил: {moderator}",
                reply_markup=queued_keyboard(key),
            )
            moment, _ = eta_for_key(key)
            await call.answer(f"В очереди 🕐 Выйдет {human_when(moment)}", show_alert=True)

        elif action == "edt":
            # включаем режим редактирования для этого модератора
            _editing[call.from_user.id] = key
            hint = (
                f"✏️ {moderator}, пришли новый текст истории #{key} одним сообщением.\n"
                "Можно ответить на это сообщение в группе админов или отправить текст в личку боту.\n"
                "Если бот не видит обычные сообщения в группе — отключи Privacy Mode у бота в @BotFather.\n"
                "Для фото/видео текст станет подписью.\n"
                "Отмена — /cancel"
            )
            if item.get("edited_text"):
                hint += f"\n\nТекущая версия:\n{item['edited_text']}"
            await call.message.reply(hint)
            await call.answer("Жду новый текст ✏️")

        elif action == "prv":
            try:
                await send_story(call.message.bot, cfg.admin_chat_id, item)
            except Exception as exc:
                await call.answer(f"Не удалось показать: {exc}", show_alert=True)
                return
            await call.answer("Предпросмотр отправлен 👁")

        elif action == "rej":
            if key in data["queue"]:
                data["queue"].remove(key)
            data["pending"].pop(key, None)
            bump("rejected")
            save_data()
            await call.message.edit_text(f"{control_text}\n\n❌ Отклонено ({moderator})")
            await call.answer("Отклонено")

        elif action == "ban":
            if item["user_id"] not in data["banned"]:
                data["banned"].append(item["user_id"])
            if key in data["queue"]:
                data["queue"].remove(key)
            data["pending"].pop(key, None)
            bump("rejected")
            save_data()
            await call.message.edit_text(
                f"{control_text}\n\n🚫 Пользователь заблокирован ({moderator})\n"
                f"Разбан: /unban {item['user_id']}"
            )
            await call.answer("Заблокирован 🚫")

    # ---------- выбор времени публикации ----------
    @dp.callback_query(F.data.startswith(("tim:", "set:", "aut:", "bck:")))
    async def on_time_pick(call: CallbackQuery) -> None:
        if call.message is None or call.message.chat.id != cfg.admin_chat_id:
            await call.answer("Недоступно", show_alert=True)
            return

        action, _, rest = (call.data or "").partition(":")
        key, _, raw_time = rest.partition(":")
        item = data["pending"].get(key)
        if not item:
            await call.answer("История уже обработана", show_alert=True)
            return

        moderator = call.from_user.full_name
        control_text = base_control_text(call.message.text or f"История #{key}")

        if action == "tim":
            # показываем пикер времени
            hint = (
                f"{control_text}\n\n🗓 Выбери время публикации для #{key}.\n"
                f"Сейчас: {now_local().strftime('%d.%m %H:%M')} ({cfg.tz_name})\n"
                f"Любое своё время: /at {key} 21:30 или /at {key} 12.09 21:30"
            )
            await call.message.edit_text(hint, reply_markup=time_keyboard(key))
            await call.answer("Выбери время 🗓")
            return

        if action == "bck":
            in_queue = key in data["queue"]
            status = f"\n\n{schedule_line(key)}" if in_queue else ""
            await call.message.edit_text(
                f"{control_text}{status}",
                reply_markup=queued_keyboard(key) if in_queue else moderation_keyboard(key),
            )
            await call.answer()
            return

        if action == "aut":
            item.pop("publish_at", None)
            if key not in data["queue"]:
                data["queue"].append(key)
            save_data()
            await call.message.edit_text(
                f"{control_text}\n\n{schedule_line(key)}\nПоставил: {moderator}",
                reply_markup=queued_keyboard(key),
            )
            moment, _mode = eta_for_key(key)
            await call.answer(f"По расписанию: {human_when(moment)}", show_alert=True)
            return

        # action == "set": точное время из кнопки
        try:
            moment = datetime.strptime(raw_time, "%Y-%m-%dT%H:%M").replace(tzinfo=zone())
        except ValueError:
            await call.answer("Не понял время", show_alert=True)
            return
        if moment <= now_local():
            moment = now_local().replace(second=0, microsecond=0) + timedelta(minutes=1)

        item["publish_at"] = moment.strftime(TIME_FMT)
        item["scheduled_by"] = moderator
        if key not in data["queue"]:
            data["queue"].append(key)
        save_data()
        await call.message.edit_text(
            f"{control_text}\n\n{schedule_line(key)}\nНазначил: {moderator}",
            reply_markup=queued_keyboard(key),
        )
        await call.answer(f"Ок! Выйдет {human_when(moment)}", show_alert=True)

    # ---------- команды в группе админов ----------
    @dp.message(Command("cancel"), F.chat.id == cfg.admin_chat_id)
    async def cmd_cancel(message: Message) -> None:
        user = message.from_user
        if user and _editing.pop(user.id, None):
            await message.answer("Редактирование отменено.")
        else:
            await message.answer("Ты сейчас ничего не редактируешь.")

    @dp.message(Command("queue"), F.chat.id == cfg.admin_chat_id)
    async def cmd_queue(message: Message) -> None:
        await message.answer(
            f"В очереди: {len(data['queue'])} шт.\n"
            f"На модерации всего: {len(data['pending'])} шт.\n"
            f"Расписание: {', '.join(cfg.publish_times)} ({cfg.tz_name}).\n"
            "Когда выйдет каждая история — /when"
        )

    # ---------- расписание очереди ----------
    @dp.message(Command("when"), F.chat.id == cfg.admin_chat_id)
    async def cmd_when(message: Message) -> None:
        if not data["queue"]:
            slots = upcoming_slots(3)
            hint = ", ".join(m.strftime("%d.%m %H:%M") for m in slots)
            await message.answer(
                "Очередь пуста ✨\n"
                f"Ближайшие слоты расписания: {hint}"
            )
            return

        lines = [
            "🗓 Когда выйдут истории",
            f"Сейчас: {now_local().strftime('%d.%m %H:%M')} ({cfg.tz_name})",
            "",
        ]
        rows = []
        for key in data["queue"]:
            moment, mode = eta_for_key(key)
            rows.append((moment, key, mode))
        rows.sort(key=lambda r: (r[0] is None, r[0]))
        for moment, key, mode in rows:
            item = data["pending"].get(key) or {}
            mark = "точное время" if mode == "exact" else "по расписанию"
            extra = " · отредактирована" if item.get("edited_text") else ""
            lines.append(
                f"#{key} · {item.get('content_type', 'text')} — {human_when(moment)} "
                f"[{mark}]{extra}"
            )
        lines += [
            "",
            "Изменить время: кнопка «🗓 Изменить время публикации» под историей",
            "или команда: /at <id> 21:30 · /at <id> 12.09 21:30 · /at <id> auto",
        ]
        await message.answer("\n".join(lines))

    # ---------- точное время командой ----------
    @dp.message(Command("at"), F.chat.id == cfg.admin_chat_id)
    async def cmd_at(message: Message) -> None:
        usage = (
            "Использование:\n"
            "/at <id истории> 21:30 — сегодня/завтра в 21:30\n"
            "/at <id> 12.09 21:30 — конкретная дата\n"
            "/at <id> 2026-09-12 21:30 — полный формат\n"
            "/at <id> auto — вернуть по расписанию\n\n"
            "Список id — /pending или /when"
        )
        parts = (message.text or "").split()
        if len(parts) < 3:
            await message.answer(usage)
            return

        key = parts[1].lstrip("#")
        item = data["pending"].get(key)
        if not item:
            await message.answer(f"Истории #{key} нет на модерации. Посмотри /pending")
            return

        arg = " ".join(parts[2:]).strip()
        if arg.lower() in ("auto", "авто", "-"):
            item.pop("publish_at", None)
            if key not in data["queue"]:
                data["queue"].append(key)
            save_data()
            await message.answer(f"История #{key}: {schedule_line(key)}")
            return

        now = now_local().replace(second=0, microsecond=0)
        moment: Optional[datetime] = None
        if re.fullmatch(r"\d{1,2}:\d{2}", arg):
            hh, mm = arg.split(":")
            try:
                moment = now.replace(hour=int(hh), minute=int(mm))
            except ValueError:
                moment = None
            if moment and moment <= now:
                moment += timedelta(days=1)  # время уже прошло — значит завтра
        else:
            for fmt, with_year in (
                ("%Y-%m-%d %H:%M", True),
                ("%d.%m.%Y %H:%M", True),
                ("%d.%m %H:%M", False),
            ):
                try:
                    parsed = datetime.strptime(arg, fmt)
                except ValueError:
                    continue
                if not with_year:
                    parsed = parsed.replace(year=now.year)
                moment = parsed.replace(tzinfo=zone())
                if not with_year and moment <= now:
                    moment = moment.replace(year=now.year + 1)
                break

        if moment is None:
            await message.answer("Не понял время 😕\n\n" + usage)
            return
        if moment <= now:
            await message.answer("Это время уже прошло. Укажи будущее время.")
            return

        item["publish_at"] = moment.strftime(TIME_FMT)
        item["scheduled_by"] = message.from_user.full_name if message.from_user else ""
        if key not in data["queue"]:
            data["queue"].append(key)
        save_data()
        await message.answer(f"✅ История #{key}: {schedule_line(key)}")
        try:
            await message.bot.edit_message_reply_markup(
                chat_id=cfg.admin_chat_id,
                message_id=item["control_message_id"],
                reply_markup=queued_keyboard(key),
            )
        except Exception:
            pass

    @dp.message(Command("pending"), F.chat.id == cfg.admin_chat_id)
    async def cmd_pending(message: Message) -> None:
        if not data["pending"]:
            await message.answer("На модерации ничего нет ✨")
            return
        lines = ["📋 На модерации:", ""]
        for key, item in list(data["pending"].items())[:30]:
            marks = []
            if key in data["queue"]:
                moment, mode = eta_for_key(key)
                mark = "точное время" if mode == "exact" else "по расписанию"
                marks.append(
                    f"в очереди #{data['queue'].index(key) + 1}, "
                    f"выйдет {human_when(moment)} [{mark}]"
                )
            if item.get("edited_text"):
                marks.append("отредактирована")
            suffix = f" — {', '.join(marks)}" if marks else ""
            lines.append(
                f"#{key} · {item.get('content_type', 'text')} · "
                f"{item.get('created_at', 'без даты')}{suffix}"
            )
        if len(data["pending"]) > 30:
            lines.append(f"...и ещё {len(data['pending']) - 30}")
        await message.answer("\n".join(lines))

    @dp.message(Command("stats"), F.chat.id == cfg.admin_chat_id)
    async def cmd_stats(message: Message) -> None:
        stats = data.get("stats", {})
        await message.answer(
            "📊 Статистика\n\n"
            f"Получено историй: {stats.get('received', 0)}\n"
            f"Опубликовано: {stats.get('published', 0)}\n"
            f"Отклонено/забанено: {stats.get('rejected', 0)}\n"
            f"Отредактировано: {stats.get('edited', 0)}\n\n"
            f"Сейчас на модерации: {len(data['pending'])}\n"
            f"В очер  ди: {len(data['queue'])}\n"
            f"В бане: {len(data['banned'])}"
        )

    @dp.message(Command("unban"), F.chat.id == cfg.admin_chat_id)
    async def cmd_unban(message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.answer("Использование: /unban <id пользователя>")
            return
        try:
            uid = int(parts[1])
        except ValueError:
            await message.answer("ID должен быть числом. Пример: /unban 123456789")
            return
        if uid in data["banned"]:
            data["banned"].remove(uid)
            save_data()
            await message.answer(f"Пользователь {uid} разблокирован.")
        else:
            await message.answer("Этот пользователь не в бане.")

    @dp.message(Command("banned"), F.chat.id == cfg.admin_chat_id)
    async def cmd_banned(message: Message) -> None:
        if not data["banned"]:
            await message.answer("Забаненных нет.")
            return
        await message.answer(
            "Забаненные:\n" + "\n".join(str(u) for u in data["banned"])
        )

    # ---------- приём нового текста истории от модератора ----------
    @dp.message(F.chat.id == cfg.admin_chat_id, F.text)
    async def on_admin_text(message: Message) -> None:
        user = message.from_user
        if user is None:
            return
        key = _editing.get(user.id)
        if not key:
            return  # обычная переписка в группе админов — игнорируем

        await save_moderator_edit(message, key, user)

    dp["publish"] = publish
    return dp


# =====================================================================
#  ПЛАНИРОВЩИК
# =====================================================================
async def scheduler(bot: Bot, cfg: Config, publish) -> None:
    tz = ZoneInfo(cfg.tz_name)
    while True:
        try:
            now = datetime.now(tz)

            # --- 1. истории с точным временем, которое уже наступило ---
            for key in list(data["queue"]):
                item = data["pending"].get(key)
                if not item:
                    data["queue"].remove(key)
                    save_data()
                    continue
                moment = parse_local(item.get("publish_at"))
                if moment is None or moment > now:
                    continue
                control_id = item["control_message_id"]
                try:
                    await publish(bot, key)
                    await bot.send_message(
                        cfg.admin_chat_id,
                        f"✅ История #{key} опубликована в назначенное время "
                        f"({moment.strftime('%d.%m %H:%M')}).",
                        reply_to_message_id=control_id,
                    )
                except Exception:
                    log.exception("Не удалось опубликовать историю %s по точному времени", key)

            # --- 2. обычные слоты расписания ---
            slot = now.strftime("%Y-%m-%d %H:%M")
            if now.strftime("%H:%M") in cfg.publish_times and slot != data["last_slot"]:
                data["last_slot"] = slot
                save_data()
                auto_keys = auto_queue_keys()
                if auto_keys:
                    key = auto_keys[0]
                    item = data["pending"].get(key)
                    if item:
                        control_id = item["control_message_id"]
                        try:
                            await publish(bot, key)
                            await bot.send_message(
                                cfg.admin_chat_id,
                                f"✅ История #{key} из очереди опубликована по расписанию.",
                                reply_to_message_id=control_id,
                            )
                        except Exception:
                            log.exception("Не удалось опубликовать историю %s из очереди", key)
                    else:
                        if key in data["queue"]:
                            data["queue"].remove(key)
                        save_data()
        except Exception:
            log.exception("Ошибка в планировщике")
        await asyncio.sleep(20)


# =====================================================================
#  ТОЧКА ВХОДА
# =====================================================================
async def set_bot_commands(bot: Bot, cfg: Config) -> None:
    """Меню команд в интерфейсе Telegram (кнопка «Меню» рядом с полем ввода)."""
    from aiogram.types import (
        BotCommand,
        BotCommandScopeAllPrivateChats,
        BotCommandScopeChat,
    )

    private_commands = [
        BotCommand(command="start", description="Приветствие"),
        BotCommand(command="help", description="Справка и команды"),
        BotCommand(command="story", description="Отправить историю"),
        BotCommand(command="cancel", description="Отменить отправку истории"),
        BotCommand(command="rules", description="Правила публикации"),
        BotCommand(command="id", description="Мой ID"),
    ]
    admin_commands = [
        BotCommand(command="help", description="Справка для модераторов"),
        BotCommand(command="queue", description="Очередь публикаций"),
        BotCommand(command="when", description="Когда выйдут истории"),
        BotCommand(command="at", description="Время публикации: /at id 21:30"),
        BotCommand(command="pending", description="Истории на модерации"),
        BotCommand(command="stats", description="Статистика"),
        BotCommand(command="banned", description="Забаненные"),
        BotCommand(command="unban", description="Разбанить: /unban id"),
        BotCommand(command="check", description="Диагностика доступов"),
        BotCommand(command="cancel", description="Отменить редактирование"),
    ]
    try:
        await bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats())
        if cfg.admin_chat_id:
            await bot.set_my_commands(
                admin_commands, scope=BotCommandScopeChat(chat_id=cfg.admin_chat_id)
            )
    except Exception:
        log.warning("Не удалось установить меню команд", exc_info=True)


async def main() -> None:
    global data

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    try:
        cfg.validate()
    except ConfigError as exc:
        print(exc)
        sys.exit(1)

    data = load_data()

    bot = Bot(token=cfg.bot_token)

    try:
        me = await bot.me()
    except Exception as exc:
        print(
            "\n❌ Telegram отклонил токен.\n"
            f"   Ошибка: {exc}\n"
            "   Проверь BOT_TOKEN в .env (и что токен не отозван в @BotFather).\n"
        )
        await bot.session.close()
        sys.exit(1)

    log.info("Бот запущен: @%s (id %s)", me.username, me.id)
    log.info("Группа админов: %s | Канал: %s", cfg.admin_chat_id, cfg.channel_id)
    log.info("Расписание: %s (%s)", ", ".join(cfg.publish_times), cfg.tz_name)

    await set_bot_commands(bot, cfg)

    dp = build_dispatcher(cfg)
    asyncio.create_task(scheduler(bot, cfg, dp["publish"]))

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлено.")
