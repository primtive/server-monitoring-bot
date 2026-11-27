import math
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Укажите {name} в конфигурации Compose.")
    return value


def number_env(name: str) -> float:
    try:
        value = float(required_env(name))
    except ValueError:
        raise ValueError(f"Укажите числовое значение {name} в Compose.") from None
    if not math.isfinite(value):
        raise ValueError(f"Значение {name} должно быть конечным числом.")
    return value


def positive_env(name: str) -> float:
    value = number_env(name)
    if value <= 0:
        raise ValueError(f"Значение {name} должно быть положительным.")
    return value


def user_ids_env(name: str) -> frozenset[int]:
    try:
        user_ids = frozenset(
            int(value) for value in os.environ.get(name, "").split(",") if value.strip()
        )
    except ValueError:
        raise ValueError(f"В {name} укажите числовые ID через запятую.") from None
    if any(user_id <= 0 for user_id in user_ids):
        raise ValueError(f"{name} принимает положительные ID пользователей.")
    return user_ids


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    proxy_url: str

    @classmethod
    def from_env(cls) -> "TelegramConfig":
        from aiogram.utils.token import TokenValidationError, validate_token

        token = required_env("BOT_TOKEN")
        try:
            validate_token(token)
        except TokenValidationError:
            raise ValueError("Проверьте BOT_TOKEN в конфигурации Compose.") from None

        proxy_url = required_env("TELEGRAM_PROXY_URL")
        try:
            proxy = urlsplit(proxy_url)
            if proxy.scheme != "http" or not proxy.hostname or not proxy.port:
                raise ValueError
        except ValueError:
            raise ValueError("TELEGRAM_PROXY_URL должен быть HTTP URL с портом.") from None
        return cls(token, proxy_url)


@dataclass(frozen=True)
class RebootConfig:
    user_ids: frozenset[int] = frozenset()
    socket_path: Path | None = None
    confirmation_seconds: float = 60

    @classmethod
    def from_env(cls, allowed_user_ids: frozenset[int]) -> "RebootConfig":
        reboot_user_ids = user_ids_env("REBOOT_USER_IDS")
        socket_path = os.environ.get("REBOOT_SOCKET", "").strip()
        if not reboot_user_ids <= allowed_user_ids:
            raise ValueError("REBOOT_USER_IDS должны входить в ALLOWED_USER_IDS.")
        if bool(reboot_user_ids) != bool(socket_path):
            raise ValueError("Для /reboot задайте REBOOT_USER_IDS и REBOOT_SOCKET вместе.")
        if not socket_path:
            return cls()
        path = Path(socket_path)
        if not path.is_absolute():
            raise ValueError("REBOOT_SOCKET должен быть абсолютным путём.")
        return cls(reboot_user_ids, path, positive_env("REBOOT_CONFIRM_SECONDS"))


@dataclass(frozen=True)
class MonitorConfig:
    group_chat_id: int
    allowed_user_ids: frozenset[int]
    interval: float
    alert_c: float
    rearm_c: float
    startup_wait: float
    reboot: RebootConfig = RebootConfig()

    @classmethod
    def from_env(cls) -> "MonitorConfig":
        try:
            group_chat_id = int(required_env("GROUP_CHAT_ID"))
        except ValueError:
            raise ValueError("Укажите отрицательный GROUP_CHAT_ID в Compose.") from None
        if group_chat_id >= 0:
            raise ValueError("GROUP_CHAT_ID должен быть отрицательным.")

        allowed_user_ids = user_ids_env("ALLOWED_USER_IDS")
        alert_c = number_env("TEMPERATURE_ALERT_C")
        rearm_c = number_env("TEMPERATURE_REARM_C")
        if rearm_c >= alert_c:
            raise ValueError("TEMPERATURE_REARM_C должен быть ниже TEMPERATURE_ALERT_C.")

        return cls(
            group_chat_id, allowed_user_ids, positive_env("SAMPLE_INTERVAL_SECONDS"),
            alert_c, rearm_c, positive_env("STARTUP_IP_WAIT_SECONDS"),
            RebootConfig.from_env(allowed_user_ids),
        )
