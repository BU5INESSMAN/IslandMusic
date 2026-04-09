from aiogram import Router

from handlers.commands import router as commands_router
from handlers.download import router as download_router

main_router = Router()
main_router.include_router(commands_router)
main_router.include_router(download_router)

__all__ = ["main_router"]
