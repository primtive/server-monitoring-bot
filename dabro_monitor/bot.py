import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatType
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.filters import Command, CommandObject
from aiogram.methods import TelegramMethod
from aiogram.types import BotCommand, Message
from aiohttp_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError

from .config import MonitorConfig, TelegramConfig
from .reboot import can_reboot, reboot_router
from .render import (
    format_cpu,
    format_disk,
    format_docker,
    format_docker_ps,
    format_ip,
    format_memory,
    format_status,
    format_temperature,
    format_uptime,
)
from .snapshot import read_snapshot

LOGGER = logging.getLogger(__name__)
GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
METRICS = {
    "status": ("Состояние сервера", format_status),
    "cpu": ("Текущая и средняя загрузка CPU, температура", format_cpu),
    "temp": ("Температура CPU", format_temperature),
    "mem": ("Занятая и общая RAM", format_memory),
    "disk": ("Занятое и общее место на диске", format_disk),
    "uptime": ("Аптайм сервера", format_uptime),
    "ip": ("LAN-адрес сервера", format_ip),
    "docker": ("Число запущенных контейнеров", format_docker),
    "ps": ("Краткий список запущенных контейнеров", format_docker_ps),
}
COMMANDS = [
    *(BotCommand(command=name, description=description) for name, (description, _) in METRICS.items()),
    BotCommand(command="help", description="Список команд"),
    BotCommand(command="start", description="Открыть мониторинг"),
]
HELP_TEXT = "Dabro · Мониторинг\n\n" + "\n".join(
    f"/{name} — {description}" for name, (description, _) in METRICS.items()
)


class TokenRedactingFormatter(logging.Formatter):
    def __init__(self, token: str) -> None:
        super().__init__("%(levelname)s %(name)s %(message)s")
        self.token = token

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record).replace(self.token, "[BOT_TOKEN]")


class TelegramProxySession(AiohttpSession):
    async def make_request[T](
        self, bot: Bot, method: TelegramMethod[T], timeout: int | None = None,
    ) -> T:
        try:
            return await super().make_request(bot, method, timeout)
        except (ProxyConnectionError, ProxyError, ProxyTimeoutError):
            raise TelegramNetworkError(method=method, message="HTTP proxy unavailable") from None


def is_allowed(message: Message, config: MonitorConfig) -> bool:
    if message.chat.type in GROUP_TYPES:
        return message.chat.id == config.group_chat_id
    if message.chat.type == ChatType.PRIVATE and message.from_user:
        return message.from_user.id in config.allowed_user_ids
    return False


def dispatcher_for(config: MonitorConfig) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.message.filter(lambda message: is_allowed(message, config))

    @dispatcher.message(Command("start", "help"))
    async def help_command(message: Message) -> None:
        help_text = HELP_TEXT
        if message.from_user and can_reboot(config.reboot, message.chat, message.from_user.id):
            help_text += "\n/reboot — Перезагрузка сервера с подтверждением"
        await message.answer(help_text)

    @dispatcher.message(Command(*METRICS, ignore_case=True))
    async def metric_command(message: Message, command: CommandObject) -> None:
        _, format_metric = METRICS[command.command.lower()]
        await message.answer(format_metric(read_snapshot(config.interval * 3)))

    dispatcher.include_router(reboot_router(config.reboot))
    return dispatcher


async def retry_telegram[T](request: Callable[[], Awaitable[T]]) -> T:
    delay = 1
    while True:
        try:
            return await request()
        except TelegramRetryAfter as error:
            wait = max(1, error.retry_after)
        except (TelegramNetworkError, TelegramServerError) as error:
            LOGGER.warning("Запрос Telegram повторится (%s).", type(error).__name__)
            wait = delay
            delay = min(delay * 2, 30)
        await asyncio.sleep(wait)


async def notify_group(bot: Bot, group_chat_id: int, text: str) -> None:
    try:
        await retry_telegram(lambda: bot.send_message(group_chat_id, text))
    except TelegramMigrateToChat as error:
        LOGGER.error("Группа стала супергруппой. Обновите GROUP_CHAT_ID на %s.", error.migrate_to_chat_id)
    except (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound) as error:
        LOGGER.error("Сообщение в группу не доставлено (%s). Проверьте ID и права бота.",
                     type(error).__name__)


async def announce_startup(bot: Bot, config: MonitorConfig) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + config.startup_wait
    while True:
        snapshot = read_snapshot(config.interval * 3)
        time_left = deadline - loop.time()
        if (snapshot and snapshot.lan_ipv4) or time_left <= 0:
            break
        await asyncio.sleep(min(1, time_left))
    await notify_group(bot, config.group_chat_id, f"Dabro · Бот запущен\n\n{format_ip(snapshot)}")


@dataclass
class TemperatureAlarm:
    alert_c: float
    rearm_c: float
    armed: bool = True

    def update(self, temperature: float | None) -> bool:
        if temperature is None:
            return False
        if temperature <= self.rearm_c:
            self.armed = True
        if self.armed and temperature >= self.alert_c:
            self.armed = False
            return True
        return False


async def watch_temperature(config: MonitorConfig, alerts: asyncio.Queue[float]) -> None:
    alarm = TemperatureAlarm(config.alert_c, config.rearm_c)
    while True:
        snapshot = read_snapshot(config.interval * 3)
        temperature = snapshot.temperature_c if snapshot else None
        if alarm.update(temperature) and temperature is not None:
            if alerts.full():
                alerts.get_nowait()
                alerts.task_done()
            alerts.put_nowait(temperature)
        await asyncio.sleep(min(1, config.interval))


async def deliver_alerts(bot: Bot, config: MonitorConfig, alerts: asyncio.Queue[float]) -> None:
    while True:
        temperature = await alerts.get()
        try:
            await notify_group(
                bot,
                config.group_chat_id,
                f"🌡 Dabro · Высокая температура CPU\n\n"
                f"Температура: {temperature:g} °C\nПорог: {config.alert_c:g} °C",
            )
        finally:
            alerts.task_done()


async def run_bot(bot: Bot, config: MonitorConfig) -> None:
    await retry_telegram(bot.me)
    await retry_telegram(bot.delete_webhook)
    await retry_telegram(lambda: bot.set_my_commands(COMMANDS))
    dispatcher = dispatcher_for(config)
    alerts: asyncio.Queue[float] = asyncio.Queue(maxsize=1)
    async with asyncio.TaskGroup() as tasks:
        background = (
            tasks.create_task(announce_startup(bot, config)),
            tasks.create_task(watch_temperature(config, alerts)),
            tasks.create_task(deliver_alerts(bot, config, alerts)),
        )
        try:
            await dispatcher.start_polling(bot, close_bot_session=False)
        finally:
            for task in background:
                task.cancel()


async def discover_ids(bot: Bot) -> None:
    identity = await retry_telegram(bot.me)
    username = identity.username or ""
    await retry_telegram(bot.delete_webhook)
    print(f"Отправьте /start в личке и /start@{username} в группе. Для выхода Ctrl+C.")
    start_commands = {"/start", f"/start@{username.lower()}"}
    offset = None
    while True:
        updates = await retry_telegram(
            lambda: bot.get_updates(offset=offset, timeout=10, allowed_updates=["message"]),
        )
        for update in updates:
            offset = update.update_id + 1
            message = update.message
            if not message or not message.text or not message.text.strip():
                continue
            if message.text.split()[0].lower() not in start_commands:
                continue
            if message.chat.type in GROUP_TYPES:
                print(f"GROUP_CHAT_ID: {message.chat.id}")
            elif message.chat.type == ChatType.PRIVATE and message.from_user:
                print(f"ALLOWED_USER_IDS: {message.from_user.id}")


async def main(discover: bool) -> None:
    telegram = TelegramConfig.from_env()
    config = None if discover else MonitorConfig.from_env()
    handler = logging.StreamHandler()
    handler.setFormatter(TokenRedactingFormatter(telegram.token))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    async with TelegramProxySession(proxy=telegram.proxy_url, timeout=15) as session:
        bot = Bot(token=telegram.token, session=session)
        try:
            if config is None:
                await discover_ids(bot)
            else:
                await run_bot(bot, config)
        except TelegramAPIError as error:
            raise SystemExit(f"Запуск Telegram остановлен ({type(error).__name__}).") from None


if __name__ == "__main__":
    if sys.argv[1:] not in ([], ["discover-ids"]):
        raise SystemExit("Использование: python -m dabro_monitor.bot [discover-ids]")
    try:
        asyncio.run(main(sys.argv[1:] == ["discover-ids"]))
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except KeyboardInterrupt:
        pass
