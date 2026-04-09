import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from handlers.keyboards import main_menu
from handlers.texts import ABOUT_TEXT, ALBUM_PROMPT, SEARCH_PROMPT, START_TEXT, TXT_PROMPT

logger = logging.getLogger(__name__)
router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    logger.info("User %d started the bot", message.from_user.id)
    await message.answer(START_TEXT, parse_mode="HTML", reply_markup=main_menu)


@router.message(F.text == "🎵 Поиск трека")
async def btn_search(message: Message) -> None:
    await message.answer(SEARCH_PROMPT, parse_mode="HTML")


@router.message(F.text == "💿 Скачать альбом")
async def btn_album(message: Message) -> None:
    await message.answer(ALBUM_PROMPT, parse_mode="HTML")


@router.message(F.text == "📁 Список из .txt")
async def btn_txt(message: Message) -> None:
    await message.answer(TXT_PROMPT, parse_mode="HTML")


@router.message(F.text == "ℹ️ О IslandMusic")
async def btn_about(message: Message) -> None:
    await message.answer(ABOUT_TEXT, parse_mode="HTML")
