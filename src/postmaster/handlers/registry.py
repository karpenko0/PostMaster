"""Регистрация Telegram-обработчиков в боте.

Сервисы передаются в регистрацию явно, поэтому обработчики не создают зависимости сами.
Порядок важен: /start и /my_posts регистрируются раньше обработчиков текста, иначе текст
«/start» попал бы в обработчик шагов сценария.
"""

from telebot.async_telebot import AsyncTeleBot

from postmaster.handlers.cancel import register_cancel_handler
from postmaster.handlers.dialog import register_dialog_handlers
from postmaster.handlers.my_posts import register_my_posts_handler
from postmaster.handlers.start import register_start_handler
from postmaster.services.cancel_service import CancelService
from postmaster.services.dialog_service import DialogService
from postmaster.services.my_posts_service import MyPostsService
from postmaster.services.photo_service import PhotoService
from postmaster.services.user_service import UserService


def register_handlers(
    bot: AsyncTeleBot,
    *,
    user_service: UserService,
    dialog_service: DialogService,
    photo_service: PhotoService,
    my_posts_service: MyPostsService,
    cancel_service: CancelService,
) -> None:
    """Регистрирует все обработчики бота."""
    register_start_handler(bot, user_service, dialog_service)
    register_my_posts_handler(bot, my_posts_service)
    register_cancel_handler(bot, user_service, cancel_service)
    register_dialog_handlers(bot, dialog_service, photo_service)
