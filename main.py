import os
import asyncio
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message
from aiogram.filters import Command

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

router = Router()

@router.message(Command("start"))
async def start(m: Message):
    await m.answer("✅ Бот живой! /start работает.")

async def handle_root(request):
    return web.Response(text="ok")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_root)
    port = int(os.getenv("PORT", "8080"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info("✅ WEB server started on port %s", port)

async def start_bot():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в Variables.")
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    logging.info("✅ POLLING STARTED")
    await dp.start_polling(bot)

async def main():
    await asyncio.gather(start_web_server(), start_bot())

if __name__ == "__main__":
    asyncio.run(main())