from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎵 Поиск трека"), KeyboardButton(text="💿 Скачать альбом")],
        [KeyboardButton(text="📁 Список из .txt"), KeyboardButton(text="ℹ️ О IslandMusic")],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
)
