"""Регистрация Telegram-обработчиков в боте.

Сервисы передаются в регистрацию явно, поэтому обработчики не создают зависимости сами.
Порядок важен: /start регистрируется раньше обработчиков текста, иначе текст «/start»
попал бы в обработчик шагов сценария.
"""

from telebot.async_telebot import AsyncTeleBot

from postmaster.handlers.dialog import register_dialog_handlers
from postmaster.handlers.start import register_start_handler
from postmaster.services.dialog_service import DialogService
from postmaster.services.photo_service import PhotoService
from postmaster.services.user_service import UserService


def register_handlers(
    bot: AsyncTeleBot,
    *,
    user_service: UserService,
    dialog_service: DialogService,
    photo_service: PhotoService,
) -> None:
    """Регистрирует все обработчики бота."""
    register_start_handler(bot, user_service, dialog_service)
    register_dialog_handlers(bot, dialog_service, photo_service)
