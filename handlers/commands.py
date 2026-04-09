import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from database.repository import get_user_download_count, get_user_history
from handlers.keyboards import main_menu
from handlers.texts import ABOUT_TEXT, ALBUM_PROMPT, SEARCH_PROMPT, START_TEXT, TXT_PROMPT

logger = logging.getLogger(__name__)
router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    logger.info("User %d started the bot", message.from_user.id)
    await message.answer(START_TEXT, parse_mode="HTML", reply_markup=main_menu)


@router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    user_id = message.from_user.id
    total = await get_user_download_count(user_id)
    records = await get_user_history(user_id, limit=15)

    if not records:
        await message.answer(
            "📭 У тебя пока нет загрузок.", parse_mode="HTML",
        )
        return

    lines = [f"📊 <b>Всего загрузок:</b> {total}\n"]
    for r in records:
        status_icon = "✅" if r.status == "success" else "❌"
        title = r.title or r.query
        artist = r.artist or ""
        entry = f"{status_icon} {title}"
        if artist:
            entry += f" — {artist}"
        lines.append(entry)

    await message.answer("\n".join(lines), parse_mode="HTML")


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
