"""
Запусти это ОДИН РАЗ ЛОКАЛЬНО (не на Railway!), чтобы залогиниться
в Telegram-аккаунт менеджера и получить SESSION_STRING.
Введёшь номер телефона и код из Telegram (и пароль 2FA, если включён).

После получения строки — вставь её в Railway Variables как SESSION_STRING
и больше этот скрипт запускать не нужно.
"""
import os
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID = int(input("API_ID (с my.telegram.org): "))
API_HASH = input("API_HASH: ")

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    print("\nТвой SESSION_STRING (сохрани в Railway Variables как SESSION_STRING):\n")
    print(client.session.save())
