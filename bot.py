"""
Бот «Подслушано» — анонимные истории с модерацией.

Запуск:
    1) заполни файл .env (см. .env.example)
    2) pip install -r requirements.txt
    3) python bot.py

Команды:
    /start          — приветствие (в личке)
    /id             — узнать ID чата и свой ID (работает где угодно)
    /queue          — сколько историй в очереди (в группе админов)
    /unban <id>     — разблокировать пользователя (в группе админов)
"""

import asyncio
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
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
#  КОНФИГ: читаем из .env (никаких set BOT_TOKEN=... в cmd не нужно)
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
}


def load_data() -> Dict[str, Any]:
    if DATA_FILE.exists():
        try:
            loaded = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("data.json повреждён — начинаю с чистого хранилища")
        else:
            merged = dict(DEFAULT_DATA)
            merged.update(loaded)
            return merged
    return json.loads(json.dumps(DEFAULT_DATA))  # глубокая копия


data: Dict[str, Any] = {}


def save_data() -> None:
    tmp = DATA_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_FILE)  # атомарная запись: файл не побьётся при сбое


_last_message_at: Dict[int, float] = {}


# =====================================================================
#  КЛАВИАТУРЫ
# =====================================================================
def moderation_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Опубликовать сейчас", callback_data=f"pub:{key}")],
            [InlineKeyboardButton(text="🕐 В очередь (по расписанию)", callback_data=f"que:{key}")],
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
            [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rej:{key}")],
        ]
    )


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
            lines += [
                "",
                f"👉 Впиши в .env:  ADMIN_CHAT_ID={chat.id}",
            ]
        elif chat.type == "channel":
            lines += [
                "",
                f"👉 Впиши в .env:  CHANNEL_ID={chat.id}",
            ]
        await message.answer("\n".join(lines))

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
    async def publish(bot: Bot, key: str) -> bool:
        item = data["pending"].get(key)
        if not item:
            return False
        await bot.copy_message(
            chat_id=cfg.channel_id,
            from_chat_id=cfg.admin_chat_id,
            message_id=item["content_message_id"],
        )
        try:
            await bot.send_message(item["user_id"], "Твоя история опубликована! 🎉")
        except Exception:
            pass  # пользователь мог заблокировать бота
        if key in data["queue"]:
            data["queue"].remove(key)
        data["pending"].pop(key, None)
        save_data()
        return True

    # ---------- личка: /start ----------
    @dp.message(CommandStart(), F.chat.type == "private")
    async def cmd_start(message: Message) -> None:
        await message.answer(
            "Привет! 👋\n\n"
            "Это бот «Подслушано». Пришли свою историю — текст, фото или видео — "
            "и после проверки админами она появится в канале анонимно."
        )

    # ---------- личка: история ----------
    @dp.message(F.chat.type == "private", F.content_type.in_({"text", "photo", "video"}))
    async def on_story(message: Message) -> None:
        user = message.from_user
        if user is None:
            return
        if user.id in data["banned"]:
            return  # молча игнорируем заблокированных

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
        }
        save_data()

        await message.answer(
            "Спасибо! История отправлена на модерацию. "
            "Если её одобрят — она появится в канале анонимно. ✌️"
        )

    # ---------- личка: остальное ----------
    @dp.message(F.chat.type == "private")
    async def on_other_content(message: Message) -> None:
        await message.answer("Я принимаю текст, фото и видео 🙈")

    # ---------- кнопки модерации ----------
    @dp.callback_query(F.data.startswith(("pub:", "que:", "rej:", "ban:")))
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
        control_text = call.message.text or f"История #{key}"

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
            if key in data["queue"]:
                await call.answer("Уже в очереди", show_alert=True)
                return
            data["queue"].append(key)
            save_data()
            await call.message.edit_text(
                f"{control_text}\n\n🕐 В очереди ({moderator}), место {len(data['queue'])}",
                reply_markup=queued_keyboard(key),
            )
            await call.answer("Добавлено в очередь 🕐")

        elif action == "rej":
            if key in data["queue"]:
                data["queue"].remove(key)
            data["pending"].pop(key, None)
            save_data()
            await call.message.edit_text(f"{control_text}\n\n❌ Отклонено ({moderator})")
            await call.answer("Отклонено")

        elif action == "ban":
            if item["user_id"] not in data["banned"]:
                data["banned"].append(item["user_id"])
            if key in data["queue"]:
                data["queue"].remove(key)
            data["pending"].pop(key, None)
            save_data()
            await call.message.edit_text(
                f"{control_text}\n\n🚫 Пользователь заблокирован ({moderator})\n"
                f"Разбан: /unban {item['user_id']}"
            )
            await call.answer("Заблокирован 🚫")

    # ---------- команды в группе админов ----------
    @dp.message(Command("queue"), F.chat.id == cfg.admin_chat_id)
    async def cmd_queue(message: Message) -> None:
        await message.answer(
            f"В очереди: {len(data['queue'])} шт.\n"
            f"На модерации всего: {len(data['pending'])} шт.\n"
            f"Расписание: {', '.join(cfg.publish_times)} ({cfg.tz_name})."
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
            slot = now.strftime("%Y-%m-%d %H:%M")
            if now.strftime("%H:%M") in cfg.publish_times and slot != data["last_slot"]:
                data["last_slot"] = slot
                save_data()
                if data["queue"]:
                    key = data["queue"][0]
                    item = data["pending"].get(key)
                    if item:
                        control_id = item["control_message_id"]
                        try:
                            await publish(bot, key)
                            await bot.send_message(
                                cfg.admin_chat_id,
                                "✅ История из очереди опубликована по расписанию.",
                                reply_to_message_id=control_id,
                            )
                        except Exception:
                            log.exception("Не удалось опубликовать историю %s из очереди", key)
                    else:
                        data["queue"].pop(0)
                        save_data()
        except Exception:
            log.exception("Ошибка в планировщике")
        await asyncio.sleep(20)


# =====================================================================
#  ТОЧКА ВХОДА
# =====================================================================
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
