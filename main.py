import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ----------------- НАСТРОЙКИ -----------------
load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)

DB_PATH = "shop.db"

RESERVE_MINUTES = 60
EXTEND_MINUTES = 30
MAX_EXTENDS = 1

SUPPORT_USERNAME = "@your_support"  # <-- поменяй
PAYMENT_CARD_TEXT = (
    "💳 Оплата картой\n"
    "1) Переведите сумму на карту: XXXX XXXX XXXX XXXX\n"
    "2) В комментарии ничего не пишите\n"
    "3) Нажмите «✅ Я оплатил(а)»"
)
PAYMENT_OTHER_TEXT = (
    "💰 Другая оплата\n"
    f"Напишите в поддержку: {SUPPORT_USERNAME}"
)

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

# ----------------- БАЗА ДАННЫХ -----------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            tg_user_id INTEGER PRIMARY KEY,
            city TEXT,
            banned INTEGER DEFAULT 0
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city TEXT NOT NULL,
            name TEXT NOT NULL,
            variant TEXT NOT NULL,
            price INTEGER NOT NULL,
            description TEXT DEFAULT ''
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_user_id INTEGER NOT NULL,
            city TEXT NOT NULL,
            product_id INTEGER NOT NULL,
            total_price INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            reserved_until TEXT NOT NULL,
            extends_count INTEGER DEFAULT 0
        )
        """)
        await db.commit()

async def seed_demo_products():
    # Поменяй на свои ЛЕГАЛЬНЫЕ товары (или добавляй через /addproduct)
    demo = [
        ("КРИВОЙ РОГ", "Кофе в зернах", "250 г", 280, "Свежая обжарка"),
        ("КРИВОЙ РОГ", "Кофе в зернах", "500 г", 560, "Свежая обжарка"),
        ("КРИВОЙ РОГ", "Чай листовой", "100 г", 220, "Насыщенный вкус"),
    ]
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM products")
        (cnt,) = await cur.fetchone()
        if cnt == 0:
            await db.executemany(
                "INSERT INTO products(city,name,variant,price,description) VALUES(?,?,?,?,?)",
                demo
            )
            await db.commit()

async def ensure_user(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM users WHERE tg_user_id=?", (uid,))
        if await cur.fetchone() is None:
            await db.execute("INSERT INTO users(tg_user_id, city, banned) VALUES(?,?,0)", (uid, None))
            await db.commit()

async def get_user(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT city, banned FROM users WHERE tg_user_id=?", (uid,))
        row = await cur.fetchone()
        return row if row else (None, 0)

async def set_city(uid: int, city: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET city=? WHERE tg_user_id=?", (city, uid))
        await db.commit()

async def get_cities():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT DISTINCT city FROM products ORDER BY city")
        rows = await cur.fetchall()
        return [r[0] for r in rows]

async def get_products_by_city(city: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, name, variant, price
            FROM products
            WHERE city=?
            ORDER BY name, price
        """, (city,))
        return await cur.fetchall()

async def get_product(pid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, city, name, variant, price, description
            FROM products
            WHERE id=?
        """, (pid,))
        return await cur.fetchone()

async def create_order(uid: int, city: str, product_id: int, total: int) -> int:
    created = now_utc()
    reserved_until = created + timedelta(minutes=RESERVE_MINUTES)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO orders(tg_user_id, city, product_id, total_price, status, created_at, reserved_until, extends_count)
            VALUES(?,?,?,?,?,?,?,0)
        """, (uid, city, product_id, total, "AWAITING_PAYMENT", created.isoformat(), reserved_until.isoformat()))
        await db.commit()
        return int(cur.lastrowid)

async def get_order(order_id: int, uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, city, product_id, total_price, status, created_at, reserved_until, extends_count
            FROM orders
            WHERE id=? AND tg_user_id=?
        """, (order_id, uid))
        return await cur.fetchone()

async def get_last_order_id(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id FROM orders
            WHERE tg_user_id=?
            ORDER BY id DESC
            LIMIT 1
        """, (uid,))
        row = await cur.fetchone()
        return row[0] if row else None

async def set_order_status(order_id: int, uid: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE orders SET status=? WHERE id=? AND tg_user_id=?", (status, order_id, uid))
        await db.commit()

async def maybe_expire(order_id: int, uid: int):
    order = await get_order(order_id, uid)
    if not order:
        return
    _, _, _, _, status, _, reserved_until, _ = order
    if status != "AWAITING_PAYMENT":
        return
    ru = datetime.fromisoformat(reserved_until)
    if now_utc() > ru:
        await set_order_status(order_id, uid, "EXPIRED")

async def extend_reserve(order_id: int, uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT status, reserved_until, extends_count
            FROM orders WHERE id=? AND tg_user_id=?
        """, (order_id, uid))
        row = await cur.fetchone()
        if not row:
            return False, "Заказ не найден."
        status, reserved_until, extends_count = row
        if status != "AWAITING_PAYMENT":
            return False, "Продлить можно только когда ожидается оплата."
        if extends_count >= MAX_EXTENDS:
            return False, "Лимит продления исчерпан."
        ru = datetime.fromisoformat(reserved_until)
        if now_utc() > ru:
            return False, "Бронь уже истекла."
        new_ru = ru + timedelta(minutes=EXTEND_MINUTES)
        await db.execute("""
            UPDATE orders SET reserved_until=?, extends_count=extends_count+1
            WHERE id=? AND tg_user_id=?
        """, (new_ru.isoformat(), order_id, uid))
        await db.commit()
        return True, f"Бронь продлена на {EXTEND_MINUTES} мин."

# ----------------- КНОПКИ -----------------
def kb_main():
    b = InlineKeyboardBuilder()
    b.button(text="🏙 Выбрать город", callback_data="pick_city")
    b.button(text="🛒 Каталог", callback_data="catalog")
    b.button(text="📦 Статус последнего заказа", callback_data="last_status")
    b.button(text="🆘 Поддержка", callback_data="support")
    b.adjust(1)
    return b.as_markup()

def kb_cities(cities: list[str]):
    b = InlineKeyboardBuilder()
    for c in cities:
        b.button(text=c, callback_data=f"city:{c}")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    return b.as_markup()

def kb_catalog(items):
    b = InlineKeyboardBuilder()
    for pid, name, variant, price in items:
        b.button(text=f"{name} • {variant} — {price} грн", callback_data=f"prod:{pid}")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    return b.as_markup()

def kb_product(pid: int):
    b = InlineKeyboardBuilder()
    b.button(text="✅ Заказать", callback_data=f"order:{pid}")
    b.button(text="⬅️ Каталог", callback_data="catalog")
    b.adjust(1)
    return b.as_markup()

def kb_order(order_id: int):
    b = InlineKeyboardBuilder()
    b.button(text="💳 Оплата картой", callback_data=f"pay:card:{order_id}")
    b.button(text="💰 Другая оплата", callback_data=f"pay:other:{order_id}")
    b.button(text="✅ Я оплатил(а)", callback_data=f"paid:{order_id}")
    b.button(text="📌 Статус", callback_data=f"status:{order_id}")
    b.button(text="⏳ Продлить бронь", callback_data=f"extend:{order_id}")
    b.button(text="❌ Отменить заказ", callback_data=f"cancel:{order_id}")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    return b.as_markup()

# ----------------- BOT -----------------
router = Router()

@router.message(Command("start"))
async def start(m: Message):
    await ensure_user(m.from_user.id)
    await m.answer("👋 Привет! Выберите действие:", reply_markup=kb_main())

@router.callback_query(F.data == "menu")
async def menu(c: CallbackQuery):
    await c.message.edit_text("Главное меню:", reply_markup=kb_main())
    await c.answer()

@router.callback_query(F.data == "support")
async def support(c: CallbackQuery):
    await c.message.edit_text(f"🆘 Поддержка: {SUPPORT_USERNAME}", reply_markup=kb_main())
    await c.answer()

@router.callback_query(F.data == "pick_city")
async def pick_city(c: CallbackQuery):
    cities = await get_cities()
    await c.message.edit_text("Выберите город:", reply_markup=kb_cities(cities))
    await c.answer()

@router.callback_query(F.data.startswith("city:"))
async def set_city_cb(c: CallbackQuery):
    city = c.data.split(":", 1)[1]
    await ensure_user(c.from_user.id)
    await set_city(c.from_user.id, city)
    await c.message.edit_text(f"✅ Город выбран: <b>{city}</b>\nОткройте каталог.", reply_markup=kb_main())
    await c.answer()

@router.callback_query(F.data == "catalog")
async def catalog(c: CallbackQuery):
    await ensure_user(c.from_user.id)
    city, banned = await get_user(c.from_user.id)
    if banned:
        await c.message.edit_text("⛔️ У вас бан. Напишите в поддержку.", reply_markup=kb_main())
        await c.answer()
        return
    if not city:
        await c.message.edit_text("Сначала выберите город:", reply_markup=kb_cities(await get_cities()))
        await c.answer()
        return
    items = await get_products_by_city(city)
    await c.message.edit_text(f"🛒 Каталог • <b>{city}</b>:", reply_markup=kb_catalog(items))
    await c.answer()

@router.callback_query(F.data.startswith("prod:"))
async def prod(c: CallbackQuery):
    pid = int(c.data.split(":", 1)[1])
    p = await get_product(pid)
    if not p:
        await c.answer("Товар не найден", show_alert=True)
        return
    _id, city, name, variant, price, desc = p
    text = (
        f"📦 <b>{name}</b>\n"
        f"🏙 Город: <b>{city}</b>\n"
        f"🔹 Вариант: <b>{variant}</b>\n"
        f"💵 Цена: <b>{price} грн</b>\n\n"
        f"{desc or ''}"
    )
    await c.message.edit_text(text, reply_markup=kb_product(pid))
    await c.answer()

@router.callback_query(F.data.startswith("order:"))
async def order(c: CallbackQuery, bot: Bot):
    await ensure_user(c.from_user.id)
    city, banned = await get_user(c.from_user.id)
    if banned:
        await c.answer("Вы заблокированы.", show_alert=True)
        return
    if not city:
        await c.answer("Сначала выберите город.", show_alert=True)
        return

    pid = int(c.data.split(":", 1)[1])
    p = await get_product(pid)
    if not p:
        await c.answer("Товар не найден.", show_alert=True)
        return

    _id, p_city, name, variant, price, _ = p
    if p_city != city:
        await c.answer("Товар из другого города.", show_alert=True)
        return

    order_id = await create_order(c.from_user.id, city, pid, price)

    text = (
        "✅ Заказ создан!\n\n"
        f"🧾 Заказ № <b>{order_id}</b>\n"
        f"🏙 Город: <b>{city}</b>\n"
        f"📦 Товар: <b>{name}</b> — {variant}\n"
        f"💵 Сумма: <b>{price} грн</b>\n"
        f"⏳ Бронь: <b>{RESERVE_MINUTES} мин</b>\n\n"
        "Выберите действие:"
    )
    await c.message.edit_text(text, reply_markup=kb_order(order_id))
    await c.answer()

    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                (
                    "🆕 Новый заказ\n"
                    f"Заказ № {order_id}\n"
                    f"User id: {c.from_user.id}\n"
                    f"Город: {city}\n"
                    f"Товар: {name} ({variant})\n"
                    f"Сумма: {price} грн\n"
                    "Статус: AWAITING_PAYMENT"
                )
            )
        except Exception:
            pass

@router.callback_query(F.data.startswith("pay:"))
async def pay(c: CallbackQuery):
    _, method, order_id_s = c.data.split(":")
    order_id = int(order_id_s)

    await maybe_expire(order_id, c.from_user.id)
    order = await get_order(order_id, c.from_user.id)
    if not order:
        await c.answer("Заказ не найден", show_alert=True)
        return

    oid, _city, _pid, total, status, _created, reserved_until, extends = order
    if status == "EXPIRED":
        await c.message.edit_text("⏰ Бронь истекла. Создайте новый заказ.", reply_markup=kb_main())
        await c.answer()
        return

    ru = datetime.fromisoformat(reserved_until)
    mins_left = max(0, int((ru - now_utc()).total_seconds() // 60))
    pay_text = PAYMENT_CARD_TEXT if method == "card" else PAYMENT_OTHER_TEXT

    text = (
        f"🧾 Заказ № <b>{oid}</b>\n"
        f"💵 К оплате: <b>{total} грн</b>\n"
        f"⏳ Осталось по брони: <b>{mins_left} мин</b>\n"
        f"🔁 Продлений: <b>{extends}/{MAX_EXTENDS}</b>\n\n"
        f"{pay_text}"
    )
    await c.message.edit_text(text, reply_markup=kb_order(order_id))
    await c.answer()

@router.callback_query(F.data.startswith("paid:"))
async def paid(c: CallbackQuery, bot: Bot):
    order_id = int(c.data.split(":", 1)[1])
    await set_order_status(order_id, c.from_user.id, "PAID_REPORTED")
    await c.message.edit_text("✅ Отметка об оплате получена. Ожидайте подтверждения.", reply_markup=kb_order(order_id))
    await c.answer()

    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, f"✅ Клиент отметил оплату. Заказ № {order_id}. User {c.from_user.id}")
        except Exception:
            pass

@router.callback_query(F.data.startswith("status:"))
async def status(c: CallbackQuery):
    order_id = int(c.data.split(":", 1)[1])
    await maybe_expire(order_id, c.from_user.id)
    order = await get_order(order_id, c.from_user.id)
    if not order:
        await c.answer("Заказ не найден", show_alert=True)
        return

    oid, city, pid, total, status_val, created_at, reserved_until, extends = order
    p = await get_product(pid)
    name = p[2] if p else "Товар"
    variant = p[3] if p else ""

    status_map = {
        "AWAITING_PAYMENT": "Ожидается оплата",
        "PAID_REPORTED": "Оплата заявлена",
        "EXPIRED": "Бронь истекла",
        "CANCELLED": "Отменён",
        "COMPLETED": "Завершён",
    }

    ru = datetime.fromisoformat(reserved_until)
    mins_left = int((ru - now_utc()).total_seconds() // 60)

    text = (
        f"📌 Статус заказа № <b>{oid}</b>\n\n"
        f"🏙 Город: <b>{city}</b>\n"
        f"📦 Товар: <b>{name}</b> — {variant}\n"
        f"💵 Сумма: <b>{total} грн</b>\n"
        f"🕒 Создан: {created_at}\n"
        f"📍 Статус: <b>{status_map.get(status_val, status_val)}</b>\n"
    )
    if status_val == "AWAITING_PAYMENT":
        text += f"⏳ Осталось по брони: <b>{max(0, mins_left)} мин</b>\n"
        text += f"🔁 Продлений: <b>{extends}/{MAX_EXTENDS}</b>\n"

    await c.message.edit_text(text, reply_markup=kb_order(order_id))
    await c.answer()

@router.callback_query(F.data == "last_status")
async def last_status(c: CallbackQuery):
    last_id = await get_last_order_id(c.from_user.id)
    if not last_id:
        await c.message.edit_text("У вас ещё нет заказов.", reply_markup=kb_main())
        await c.answer()
        return
    c.data = f"status:{last_id}"
    await status(c)

@router.callback_query(F.data.startswith("extend:"))
async def extend(c: CallbackQuery):
    order_id = int(c.data.split(":", 1)[1])
    ok, msg = await extend_reserve(order_id, c.from_user.id)
    await c.answer(msg, show_alert=True)
    c.data = f"status:{order_id}"
    await status(c)

@router.callback_query(F.data.startswith("cancel:"))
async def cancel(c: CallbackQuery):
    order_id = int(c.data.split(":", 1)[1])
    await set_order_status(order_id, c.from_user.id, "CANCELLED")
    await c.message.edit_text(f"❌ Заказ № <b>{order_id}</b> отменён.", reply_markup=kb_main())
    await c.answer()

# ----------------- АДМИН ДОБАВИТЬ ТОВАР -----------------
@router.message(Command("addproduct"))
async def addproduct(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    raw = m.text.replace("/addproduct", "", 1).strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 4:
        await m.answer(
            "Формат:\n"
            "/addproduct ГОРОД | НАЗВАНИЕ | ВАРИАНТ | ЦЕНА | ОПИСАНИЕ(необязательно)\n"
            "Пример:\n"
            "/addproduct КРИВОЙ РОГ | Кофе | 250 г | 280 | Свежая обжарка"
        )
        return
    city, name, variant, price_s = parts[:4]
    desc = parts[4] if len(parts) >= 5 else ""
    try:
        price = int(price_s)
    except:
        await m.answer("Цена должна быть числом.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO products(city,name,variant,price,description) VALUES(?,?,?,?,?)",
            (city, name, variant, price, desc)
        )
        await db.commit()
    await m.answer(f"✅ Добавлено: {city} / {name} / {variant} / {price} грн")

# ----------------- WEB для Railway -----------------
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

    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()
    dp.include_router(router)

    logging.info("✅ POLLING STARTED")
    await dp.start_polling(bot)

async def main():
    await init_db()
    await seed_demo_products()
    await asyncio.gather(start_web_server(), start_bot())

if __name__ == "__main__":
    asyncio.run(main())