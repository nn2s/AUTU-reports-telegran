#!/usr/bin/env python3
import asyncio
import random
import sqlite3
import logging
from datetime import datetime
from telethon import TelegramClient, events
from telethon.tl.functions.messages import ReportRequest
from telethon.tl.functions.contacts import BlockRequest
from telethon.errors import FloodWaitError, PeerFloodError
from telethon.network import ConnectionTcpAbridged
import os
import platform
from typing import List, Dict

# Конфигурация логов
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('reporter.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class TurboReporterConfig:
    def __init__(self):
        # Настройки сессий (минимум 3 для ротации)
        self.SESSIONS = [
            {
                'name': 'session1',
                'api_id': 123456,
                'api_hash': 'abcdef123456',
                'phone': '+1234567890',
                'proxy': None  # (host, port, secret) для MTProto
            },
            # Добавьте другие сессии
        ]
        
        # Причины жалоб (Telegram API codes)
        self.REASONS = {
            1: "Спам",
            2: "Насилие",
            3: "Мошенничество",
            4: "Фейк",
            8: "Другое"
        }
        
        # Настройки производительности
        self.MIN_DELAY = 15  # Минимальная задержка
        self.MAX_DELAY = 30  # Максимальная задержка
        self.MAX_RETRIES = 3  # Максимум попыток
        self.CONCURRENT_TASKS = 5  # Параллельных задач

class TurboReporter:
    def __init__(self, config: TurboReporterConfig):
        self.config = config
        self.clients: List[TelegramClient] = []
        self.db = self.init_db()
        self.lock = asyncio.Lock()
        self.semaphore = asyncio.Semaphore(self.config.CONCURRENT_TASKS)

    def init_db(self):
        """Инициализация базы данных SQLite"""
        db = sqlite3.connect('reports.db', check_same_thread=False)
        db.execute('''CREATE TABLE IF NOT EXISTS reports
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      username TEXT UNIQUE,
                      status TEXT,
                      reason TEXT,
                      session TEXT,
                      timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        db.commit()
        return db

    async def init_clients(self):
        """Инициализация клиентов Telegram"""
        for session in self.config.SESSIONS:
            try:
                client = TelegramClient(
                    session['name'],
                    session['api_id'],
                    session['api_hash'],
                    connection=ConnectionTcpAbridged,
                    proxy=session.get('proxy')
                )
                
                await client.start(phone=session['phone'])
                self.clients.append(client)
                logger.info(f"Сессия {session['name']} успешно запущена")
            except Exception as e:
                logger.error(f"Ошибка в сессии {session['name']}: {str(e)}")

    async def safe_report(self, client: TelegramClient, username: str, reason: int) -> bool:
        """Безопасная отправка жалобы с обработкой ошибок"""
        try:
            user = await client.get_entity(username)
            
            await client(ReportRequest(
                peer=user,
                reason=[reason],
                message="Автоматическая жалоба"
            ))
            
            await client(BlockRequest(user))
            return True
            
        except FloodWaitError as e:
            wait = e.seconds + random.randint(5, 15)
            logger.warning(f"Флуд-контроль: ждем {wait} сек.")
            await asyncio.sleep(wait)
            return False
            
        except PeerFloodError:
            logger.error("Лимит жалоб исчерпан для этой сессии")
            return False
            
        except Exception as e:
            logger.error(f"Ошибка для @{username}: {str(e)}")
            return False

    async def process_target(self, username: str):
        """Обработка одного целевого пользователя"""
        async with self.semaphore:
            for attempt in range(self.config.MAX_RETRIES):
                client = random.choice(self.clients)
                reason = random.choice(list(self.config.REASONS.keys()))
                
                success = await self.safe_report(client, username, reason)
                
                if success:
                    async with self.lock:
                        self.db.execute(
                            "INSERT OR IGNORE INTO reports (username, status, reason, session) VALUES (?, ?, ?, ?)",
                            (username, 'reported', self.config.REASONS[reason], client.session.filename)
                        )
                        self.db.commit()
                    logger.info(f"Успех: @{username} | Причина: {self.config.REASONS[reason]}")
                    break
                    
                await asyncio.sleep(random.uniform(self.config.MIN_DELAY, self.config.MAX_DELAY))
            else:
                logger.error(f"Не удалось обработать @{username} после {self.config.MAX_RETRIES} попыток")

    async def run(self, targets: List[str]):
        """Основной цикл обработки"""
        await self.init_clients()
        
        if not self.clients:
            logger.error("Нет активных сессий!")
            return
            
        tasks = [self.process_target(target) for target in targets]
        await asyncio.gather(*tasks)

class TelegramReporterBot:
    """Telegram бот для управления репортером"""
    def __init__(self, reporter: TurboReporter):
        self.reporter = reporter
        self.bot = TelegramClient('reporter_bot', reporter.config.SESSIONS[0]['api_id'], 
                                reporter.config.SESSIONS[0]['api_hash'])
        
    async def start(self):
        await self.bot.start(bot_token='YOUR_BOT_TOKEN')
        
        @self.bot.on(events.NewMessage(pattern='/start'))
        async def start_handler(event):
            await event.respond(
                "🛡️ *Turbo Reporter*\n\n"
                "Отправьте username нарушителя (например @username)\n"
                "Или загрузите файл со списком username'ов",
                parse_mode='md'
            )
        
        @self.bot.on(events.NewMessage)
        async def message_handler(event):
            if event.file:
                # Обработка файла
                pass
            else:
                await self.process_username(event, event.text.strip('@'))
        
        await self.bot.run_until_disconnected()
    
    async def process_username(self, event, username: str):
        """Обработка username от пользователя"""
        try:
            await event.respond(f"⏳ Начинаю обработку @{username}...")
            await self.reporter.process_target(username)
            await event.respond(f"✅ @{username} успешно обработан!")
        except Exception as e:
            await event.respond(f"❌ Ошибка: {str(e)}")

async def main():
    config = TurboReporterConfig()
    reporter = TurboReporter(config)
    
    # Запуск Telegram бота (опционально)
    # bot = TelegramReporterBot(reporter)
    # asyncio.create_task(bot.start())
    
    # Пример обработки целей из файла
    with open('targets.txt') as f:
        targets = [line.strip() for line in f if line.strip()]
    
    await reporter.run(targets)

if __name__ == "__main__":
    # Для максимальной производительности
    if platform.system() == 'Windows':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    asyncio.run(main())
