import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import RebootConfig

LOGGER = logging.getLogger(__name__)


class RebootChoice(CallbackData, prefix="reboot"):
    action: Literal["confirm", "cancel"]
    nonce: str


@dataclass(frozen=True)
class Confirmation:
    nonce: str
    message_id: int
    deadline: float


def can_reboot(config: RebootConfig, chat: Chat, user_id: int) -> bool:
    return (
        config.socket_path is not None
        and chat.type == ChatType.PRIVATE
        and chat.id == user_id
        and user_id in config.user_ids
    )


async def request_reboot(path: Path) -> bool:
    try:
        async with asyncio.timeout(4):
            reader, writer = await asyncio.open_unix_connection(path, limit=64)
            try:
                writer.write(b"REBOOT\n")
                await writer.drain()
                return await reader.readline() == b"OK\n"
            finally:
                writer.close()
                await writer.wait_closed()
    except (OSError, ValueError):
        LOGGER.warning("Не получено подтверждение перезагрузки от systemd.")
        return False


def reboot_router(config: RebootConfig) -> Router:
    router = Router()
    pending: dict[int, Confirmation] = {}

    @router.message(Command("reboot"))
    async def prompt_reboot(message: Message) -> None:
        if not message.from_user or not can_reboot(config, message.chat, message.from_user.id):
            return
        path = config.socket_path
        if path is None or not path.is_socket():
            await message.answer("Перезагрузка недоступна. Проверьте службу на сервере.")
            return

        user_id = message.from_user.id
        nonce = secrets.token_urlsafe(16)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Перезагрузить", callback_data=RebootChoice(action="confirm", nonce=nonce).pack(),
            ),
            InlineKeyboardButton(
                text="Отмена", callback_data=RebootChoice(action="cancel", nonce=nonce).pack(),
            ),
        ]])
        prompt = await message.answer(
            f"⚠️ Перезагрузить dabro? Подтверждение действует {config.confirmation_seconds:g} с.",
            reply_markup=keyboard,
        )
        pending[user_id] = Confirmation(
            nonce, prompt.message_id, time.monotonic() + config.confirmation_seconds,
        )

    @router.callback_query(RebootChoice.filter())
    async def confirm_reboot(query: CallbackQuery, callback_data: RebootChoice) -> None:
        message = query.message
        user_id = query.from_user.id
        if not isinstance(message, Message) or not can_reboot(config, message.chat, user_id):
            await query.answer("Перезагрузка недоступна.")
            return

        confirmation = pending.get(user_id)
        if (
            confirmation is None
            or confirmation.message_id != message.message_id
            or confirmation.nonce != callback_data.nonce
            or confirmation.deadline <= time.monotonic()
        ):
            await query.answer("Подтверждение недействительно. Отправьте /reboot ещё раз.")
            return

        del pending[user_id]
        if callback_data.action == "cancel":
            result = "Перезагрузка отменена."
        elif config.socket_path and await request_reboot(config.socket_path):
            LOGGER.info("Перезагрузку подтвердил Telegram-пользователь %s.", user_id)
            result = "Сервер начнёт перезагрузку через несколько секунд."
        else:
            result = "Не удалось подтвердить запуск перезагрузки. Проверьте состояние сервера."
        await query.answer()
        await message.edit_text(result)

    return router
