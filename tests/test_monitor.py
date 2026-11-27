import asyncio
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import UnixStreamServer
from threading import Thread
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetMe, SendMessage
from aiogram.types import BotFullInfo, Message, Update

from dabro_monitor.bot import (
    TelegramProxySession, TemperatureAlarm, announce_startup, dispatcher_for, format_status,
    notify_group,
)
from dabro_monitor.collector import (
    CpuHistory, cpu_counters, cpu_temperature, cpu_usage, disk_usage, lan_ipv4, memory_usage,
)
from dabro_monitor.config import MonitorConfig, RebootConfig
from dabro_monitor.docker_metrics import docker_ps
from dabro_monitor.reboot import request_reboot
from dabro_monitor.snapshot import Snapshot, read_snapshot, write_snapshot

CONFIG = MonitorConfig(-10012345, frozenset({42}), 15, 50, 47, 0)
TEST_TOKEN = "123456:TEST_TOKEN"


def sample(**changes):
    return Snapshot(**({
        "measured_at": time.monotonic(),
        "temperature_c": 50,
        "cpu_percent": 25,
        "memory_used_kib": 4 * 1024 ** 2,
        "memory_total_kib": 16 * 1024 ** 2,
        "uptime_seconds": 93600,
        "lan_ipv4": "192.168.1.20",
    } | changes))


class CollectorTest(unittest.TestCase):
    def test_disk_used_and_total(self):
        with tempfile.TemporaryDirectory() as directory:
            stats = os.statvfs(directory)
            used, total = disk_usage(Path(directory))
            self.assertEqual(total, stats.f_frsize * stats.f_blocks)
            self.assertGreaterEqual(used, 0)
            self.assertLessEqual(used, total)
            self.assertEqual(disk_usage(Path(directory) / "missing"), (None, None))

    def test_cpu_history_uses_counter_deltas_over_the_window(self):
        history = CpuHistory(300)
        self.assertEqual(history.sample(0, (100, 0)), (None, None, None))
        self.assertEqual(history.sample(100, (200, 100)), (0, 0, 100))
        self.assertEqual(history.sample(280, (400, 100)), (100, 200 / 3, 280))
        current, average, seconds = history.sample(400, (500, 200))
        self.assertEqual(current, 0)
        self.assertAlmostEqual(average, 200 / 3)
        self.assertEqual(seconds, 300)
        self.assertEqual(history.sample(410, None), (None, None, None))
        self.assertEqual(history.sample(420, (510, 210)), (None, None, None))

    def test_docker_api_counts_all_rows_and_limits_the_summary(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                body = json.dumps([
                    {"Id": "abcdef", "Names": ["/bot"], "Image": "bot:latest", "Status": "Up 1 minute"},
                    {"Id": "123456", "Names": ["/db"], "Image": "postgres", "Status": "Up 2 minutes"},
                ]).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api.sock"
            with UnixStreamServer(str(path), Handler) as server:
                thread = Thread(target=server.handle_request)
                thread.start()
                result = docker_ps(path, 1)
                thread.join(timeout=4)
            self.assertEqual(result, (2, [
                {"name": "bot", "image": "bot:latest", "status": "Up 1 minute"},
            ]))
            self.assertEqual(requests, ["/containers/json?all=0"])
        self.assertEqual(docker_ps(path, 1), (None, None))

    def test_uses_host_cpu_counters_and_available_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            proc.joinpath("stat").write_text("cpu  100 0 0 200 20 0 0 0 90 0\n")
            previous = cpu_counters(proc)
            proc.joinpath("stat").write_text("cpu  125 0 0 275 20 0 0 0 100 0\n")
            self.assertEqual(cpu_usage(previous, cpu_counters(proc)), 25)
            proc.joinpath("meminfo").write_text(
                "MemTotal: 4000 kB\nMemFree: 100 kB\nMemAvailable: 3000 kB\n",
            )
            self.assertEqual(memory_usage(proc), (1000, 4000))

    def test_coretemp_package_and_per_socket_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            hwmon = Path(directory)
            for index, name in enumerate(("coretemp", "coretemp", "nvme")):
                device = hwmon / f"hwmon{index}"
                device.mkdir()
                device.joinpath("name").write_text(name)
                device.joinpath("temp1_label").write_text(f"Package id {index}")
                device.joinpath("temp1_input").write_text(("50000", "invalid", "90000")[index])
                device.joinpath("temp2_label").write_text("Core 2")
                device.joinpath("temp2_input").write_text(("70000", "51000", "90000")[index])
            self.assertEqual(cpu_temperature(hwmon), 51)
            hwmon.joinpath("hwmon0/temp1_input").unlink()
            self.assertEqual(cpu_temperature(hwmon), 70)

    def test_lan_interface_selection(self):
        routes = [
            {"dev": "wg0", "metric": 200},
            {"dev": "enp1s0", "metric": 100, "prefsrc": "192.168.1.20"},
        ]
        addresses = {
            "enp1s0": ["192.168.1.10", "192.168.1.20"],
            "wg0": ["10.55.1.4"],
        }
        with patch("dabro_monitor.collector.ip_query", return_value=routes), patch(
            "dabro_monitor.collector.iface_addresses", side_effect=addresses.get,
        ):
            self.assertEqual(lan_ipv4(""), "192.168.1.20")
            self.assertEqual(lan_ipv4("wg0"), "10.55.1.4")

    def test_snapshot_staleness_and_absent_sensor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            current = sample(temperature_c=None)
            write_snapshot(current, path)
            self.assertEqual(read_snapshot(45, path), current)
            self.assertIn("Температура CPU: нет данных", format_status(current))
            self.assertIn("RAM: 4.0 / 16.0 ГиБ (25%)", format_status(current))
            write_snapshot(sample(measured_at=time.monotonic() - 60), path)
            self.assertIsNone(read_snapshot(45, path))
            write_snapshot(sample(measured_at=time.monotonic() + 60), path)
            self.assertIsNone(read_snapshot(45, path))


class AlarmTest(unittest.TestCase):
    def test_threshold_hysteresis_and_sensor_loss(self):
        alarm = TemperatureAlarm(50, 47)
        self.assertEqual(
            [alarm.update(temp) for temp in (49, 50, 51, None, 50, 47.1, 50, 47, 50)],
            [False, True, False, False, False, False, False, False, True],
        )
        self.assertTrue(TemperatureAlarm(50, 47).update(52))


class RecordingSession(AiohttpSession):
    def __init__(self):
        super().__init__()
        self.messages = []
        self.requests = []

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, GetMe):
            return BotFullInfo.model_construct(
                id=bot.id, is_bot=True, first_name="Dabro", username="DabroTestBot",
            )
        if isinstance(method, SendMessage):
            self.messages.append((method.chat_id, method.text))
            return Message.model_validate({
                "message_id": len(self.messages),
                "date": 0,
                "chat": {
                    "id": method.chat_id, "type": "private" if method.chat_id > 0 else "supergroup",
                },
            }, context={"bot": bot})
        if isinstance(method, (AnswerCallbackQuery, EditMessageText)):
            return True
        raise AssertionError(type(method).__name__)


class BotTest(unittest.IsolatedAsyncioTestCase):
    async def feed_message(self, dispatcher, bot, chat_id, chat_type, user_id, text):
        update = Update.model_validate({
            "update_id": 1,
            "message": {
                "message_id": 100,
                "date": 1,
                "chat": {"id": chat_id, "type": chat_type},
                "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
                "text": text,
            },
        }, context={"bot": bot})
        await dispatcher.feed_update(bot, update)

    async def feed_callback(
        self, dispatcher, bot, data, message_id, user_id=42, chat_id=42, chat_type="private",
    ):
        update = Update.model_validate({
            "update_id": 2,
            "callback_query": {
                "id": "1",
                "chat_instance": "test",
                "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
                "message": {
                    "message_id": message_id, "date": 1, "chat": {"id": chat_id, "type": chat_type},
                },
                "data": data,
            },
        }, context={"bot": bot})
        await dispatcher.feed_update(bot, update)

    async def test_each_metric_and_status_without_docker(self):
        dispatcher = dispatcher_for(CONFIG)
        session = RecordingSession()
        snapshot = sample(
            cpu_average_percent=17, cpu_average_seconds=300,
            disk_used_bytes=8 * 1024 ** 3, disk_total_bytes=16 * 1024 ** 3,
            docker_running=2, docker_containers=[
                {"name": "bot", "image": "bot:latest", "status": "Up 1 minute"},
            ],
        )
        commands = {
            "/status": "Диск: 8.0 / 16.0 ГиБ (50%)",
            "/cpu": "Средняя за 5 мин: 17%",
            "/temp": "50 °C",
            "/mem": "4.0 / 16.0 ГиБ",
            "/disk": "8.0 / 16.0 ГиБ",
            "/uptime": "1 д 2 ч",
            "/docker": "Контейнеров запущено: 2",
            "/ps": "bot · bot:latest · Up 1 minute\n… и ещё 1",
        }
        async with session:
            bot = Bot(TEST_TOKEN, session=session)
            with patch("dabro_monitor.bot.read_snapshot", return_value=snapshot):
                for command, expected in commands.items():
                    await self.feed_message(dispatcher, bot, 42, "private", 42, command)
                    self.assertIn(expected, session.messages[-1][1])
        self.assertNotIn("Контейнеров", session.messages[0][1])
        self.assertNotIn("bot:latest", session.messages[0][1])

    async def test_reboot_requires_operator_private_chat_and_single_use_confirmation(self):
        config = replace(CONFIG, reboot=RebootConfig(frozenset({42}), Path("/test"), 60))
        dispatcher = dispatcher_for(config)
        session = RecordingSession()
        async with session:
            bot = Bot(TEST_TOKEN, session=session)
            with patch("dabro_monitor.reboot.Path.is_socket", return_value=True), patch(
                "dabro_monitor.reboot.request_reboot", new_callable=AsyncMock, return_value=True,
            ) as reboot:
                await self.feed_message(dispatcher, bot, CONFIG.group_chat_id, "group", 42, "/reboot")
                await self.feed_message(dispatcher, bot, 99, "private", 99, "/reboot")
                self.assertEqual(session.messages, [])
                await self.feed_message(dispatcher, bot, 42, "private", 42, "/reboot")
                keyboard = session.requests[-1].reply_markup.inline_keyboard[0]
                confirm, cancel = [button.callback_data for button in keyboard]
                reboot.assert_not_awaited()
                await self.feed_callback(dispatcher, bot, confirm, 1, user_id=99)
                await self.feed_callback(dispatcher, bot, confirm, 2)
                await self.feed_callback(dispatcher, bot, cancel, 1)
                await self.feed_callback(dispatcher, bot, confirm, 1)
                reboot.assert_not_awaited()
                await self.feed_message(dispatcher, bot, 42, "private", 42, "/reboot")
                confirm = session.requests[-1].reply_markup.inline_keyboard[0][0].callback_data
                await self.feed_callback(dispatcher, bot, confirm, 2)
                await self.feed_callback(dispatcher, bot, confirm, 2)
                reboot.assert_awaited_once_with(Path("/test"))

    async def test_reboot_rejects_expired_confirmation(self):
        config = replace(CONFIG, reboot=RebootConfig(frozenset({42}), Path("/test"), 60))
        dispatcher = dispatcher_for(config)
        session = RecordingSession()
        async with session:
            bot = Bot(TEST_TOKEN, session=session)
            with patch("dabro_monitor.reboot.Path.is_socket", return_value=True), patch(
                "dabro_monitor.reboot.request_reboot", new_callable=AsyncMock,
            ) as reboot, patch("dabro_monitor.reboot.time", wraps=time) as clock:
                clock.monotonic.return_value = 100
                await self.feed_message(dispatcher, bot, 42, "private", 42, "/reboot")
                confirm = session.requests[-1].reply_markup.inline_keyboard[0][0].callback_data
                clock.monotonic.return_value = 160
                await self.feed_callback(dispatcher, bot, confirm, 1)
                reboot.assert_not_awaited()

    async def test_reboot_socket_requires_an_ack(self):
        requests = []
        responses = iter([b"OK\n", b""])

        async def mock_listener(reader, writer):
            requests.append(await reader.readline())
            writer.write(next(responses))
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.sock"
            server = await asyncio.start_unix_server(mock_listener, path)
            async with server:
                self.assertTrue(await request_reboot(path))
                self.assertFalse(await request_reboot(path))
        self.assertEqual(requests, [b"REBOOT\n", b"REBOOT\n"])

    async def test_commands_in_authorized_chats(self):
        dispatcher = dispatcher_for(CONFIG)
        session = RecordingSession()
        async with session:
            bot = Bot(TEST_TOKEN, session=session)
            messages = [
                (CONFIG.group_chat_id, "supergroup", 99, "/status@DabroTestBot"),
                (CONFIG.group_chat_id, "supergroup", 99, "/status@OtherBot"),
                (42, "private", 42, "/ip"),
                (99, "private", 99, "/status"),
                (-123, "group", 42, "/status"),
                (42, "private", 42, "/help"),
            ]
            with patch("dabro_monitor.bot.read_snapshot", return_value=sample()):
                for index, (chat_id, chat_type, user_id, command) in enumerate(messages, 1):
                    update = Update.model_validate({
                        "update_id": index,
                        "message": {
                            "message_id": index,
                            "date": 0,
                            "chat": {"id": chat_id, "type": chat_type},
                            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
                            "text": command,
                        },
                    }, context={"bot": bot})
                    await dispatcher.feed_update(bot, update)
        self.assertEqual(
            [chat_id for chat_id, _ in session.messages], [CONFIG.group_chat_id, 42, 42],
        )
        self.assertIn("Температура CPU: 50 °C", session.messages[0][1])
        self.assertIn("192.168.1.20", session.messages[1][1])
        self.assertIn("/status", session.messages[2][1])

    async def test_startup_sends_address_to_group_and_handles_unavailable_ip(self):
        session = RecordingSession()
        async with session:
            bot = Bot(TEST_TOKEN, session=session)
            for snapshot in (sample(), None):
                with patch("dabro_monitor.bot.read_snapshot", return_value=snapshot):
                    await announce_startup(bot, CONFIG)
        self.assertEqual(session.messages, [
            (CONFIG.group_chat_id, "Dabro · Бот запущен\n\nLAN IPv4: 192.168.1.20"),
            (CONFIG.group_chat_id, "Dabro · Бот запущен\n\nLAN IPv4: нет данных"),
        ])

    async def test_group_notification_retries_a_temporary_network_error(self):
        send_message = AsyncMock(side_effect=[
            TelegramNetworkError(SendMessage(chat_id=CONFIG.group_chat_id, text="alert"), "offline"),
            True,
        ])
        bot = AsyncMock(send_message=send_message)
        with patch("dabro_monitor.bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertLogs("dabro_monitor.bot", level="WARNING"):
                await notify_group(bot, CONFIG.group_chat_id, "alert")
        sleep.assert_awaited_once_with(1)
        self.assertEqual(send_message.await_count, 2)

    async def test_telegram_uses_http_connect(self):
        connect_headers = []

        async def reject_tunnel(reader, writer):
            try:
                connect_headers.append(await reader.readuntil(b"\r\n\r\n"))
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(reject_tunnel, "127.0.0.1", 0)
        async with server:
            port = server.sockets[0].getsockname()[1]
            async with TelegramProxySession(proxy=f"http://127.0.0.1:{port}", timeout=2) as session:
                bot = Bot(TEST_TOKEN, session=session)
                with self.assertRaises(TelegramNetworkError):
                    await bot.get_me()
        self.assertTrue(connect_headers[0].startswith(b"CONNECT api.telegram.org:443 "))
        self.assertNotIn(TEST_TOKEN.encode(), connect_headers[0])


if __name__ == "__main__":
    unittest.main()
