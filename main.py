import os
import asyncio
from datetime import datetime

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

import aiosqlite
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or "0")

DB_PATH = "shop.db"

router = Router()

# ---------- DB ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            tg_user_id INTEGER PRIMARY KEY,
            city TEXT,
            last_order_id INTEGER,
            cancel_count INTEGER DEFAULT 0
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
            created_at TEXT NOT NULL
        )
        """)
        await db.commit()

async def seed_demo_products():
    # ВАЖНО: поменяй на свои ЛЕГАЛЬНЫЕ товары
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
                "INSERT INTO products(city, name, variant, price, description) VALUES(?,?,?,?,?)",
                demo
            )
            await db.commit()

async def ensure_user(tg_user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT tg_user_id FROM users WHERE tg_user_id=?", (tg_user_id,))
        row = await cur.fetchone()
        if not row:
            await db.execute(
                "INSERT INTO users(tg_user_id, city, last_order_id, cancel_count) VALUES(?,?,?,?)",
                (tg_user_id, None, None, 0)
            )
            await db.commit()

async def set_city(tg_user_id: int, city: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET city=? WHERE tg_user_id=?", (city, tg_user_id))
        await db.commit()

async def get_city(tg_user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT city FROM users WHERE tg_user_id=?", (tg_user_id,))
        row = await cur.fetchone()
        return row[0] if row else None

async def set_last_order(tg_user_id: int, order_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_order_id=? WHERE tg_user_id=?", (order_id, tg_user_id))
        await db.commit()

async def get_last_order_id(tg_user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT last_order_id FROM users WHERE tg_user_id=?", (tg_user_id,))
        row = await cur.fetchone()
        return row[0] if row else None

async def inc_cancel_count(tg_user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET cancel_count = cancel_count + 1 WHERE tg_user_id=?", (tg_user_id,))
        await db.commit()
        cur = await db.execute("SELECT cancel_count FROM users WHERE tg_user_id=?", (tg_user_id,))
        row = await cur.fetchone()
        return row[0] if row else 0

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

async def get_product(product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, city, name, variant, price, description
            FROM products WHERE id=?
        """, (product_id,))
        return await cur.fetchone()

async def create_order(tg_user_id: int, city: str, product_id: int, total_price: int):
    created_at = datetime.now().isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO orders(tg_user_id, city, product_id, total_price, status, created_at)
            VALUES(?,?,?,?,?,?)
        """, (tg_user_id, city, product_id, total_price, "AWAITING_PAYMENT", created_at))
        await db.commit()
        return cur.lastrowid

async def get_order(order_id: int, tg_user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, city, product_id, total_price, status, created_at
            FROM orders WHERE id=? AND tg_user_id=?
        """, (order_id, tg_user_id))
        return await cur.fetchone()

async def set_order_status(order_id: int, tg_user_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE orders SET status=? WHERE id=? AND tg_user_id=?
        """, (status, order_id, tg_user_id))
        await db.commit()

# ---------- Keyboards ----------
def kb_main():
    kb = InlineKeyboardBuilder()
    kb.button(text="🏙 Выбрать город", callback_data="choose_city")
    kb.button(text="🛍 Каталог", callback_data="catalog")
    kb.button(text="📦 Статус заказа", callback_data="status")
    kb.adjust(1, 2)
    return kb.as_markup()

def kb_payment(order_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Оплата картой (инструкция)", callback_data=f"pay_card:{order_id}")
    kb.button(text="💵 Оплата наличными/при получении", callback_data=f"pay_cash:{order_id}")
    kb.button(text="✅ Я оплатил(а)", callback_data=f"paid:{order_id}")
    kb.button(text="❌ Отменить заказ", callback_data=f"cancel:{order_id}")
    kb.adjust(1, 1, 1, 1)
    return kb.as_markup()

def kb_back_to_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В меню", callback_data="menu")
    return kb.as_markup()

# ---------- Handlers ----------
@router.message(Command("start"))
async def cmd_start(message: Message):
    await ensure_user(message.from_user.id)
    text = (
        "Привет! Я магазин-бот.\n\n"
        "1) Выбери город\n"
        "2) Открой каталог\n"
        "3) Оформи заказ и смотри статус\n"
    )
    await message.answer(text, reply_markup=kb_main())

@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery):
    await call.message.edit_text("Главное меню:", reply_markup=kb_main())
    await call.answer()

@router.callback_query(F.data == "choose_city")
async def cb_choose_city(call: CallbackQuery):
    cities = await get_cities()
    if not cities:
        await call.message.edit_text("Пока нет городов/товаров в базе.", reply_markup=kb_back_to_menu())
        await call.answer()
        return

    kb = InlineKeyboardBuilder()
    for c in cities:
        kb.button(text=c, callback_data=f"set_city:{c}")
    kb.button(text="⬅️ В меню", callback_data="menu")
    kb.adjust(1)
    await call.message.edit_text("Выберите город:", reply_markup=kb.as_markup())
    await call.answer()

@router.callback_query(F.data.startswith("set_city:"))
async def cb_set_city(call: CallbackQuery):
    city = call.data.split("set_city:", 1)[1]
    await ensure_user(call.from_user.id)
    await set_city(call.from_user.id, city)
    await call.message.edit_text(f"✅ Город выбран: {city}\n\nТеперь открой каталог.", reply_markup=kb_main())
    await call.answer()

@router.callback_query(F.data == "catalog")
async def cb_catalog(call: CallbackQuery):
    await ensure_user(call.from_user.id)
    city = await get_city(call.from_user.id)
    if not city:
        await call.message.edit_text("Сначала выбери город 👇", reply_markup=kb_main())
        await call.answer()
        return

    products = await get_products_by_city(city)
    if not products:
        await call.message.edit_text(f"В городе {city} пока нет товаров.", reply_markup=kb_main())
        await call.answer()
        return

    kb = InlineKeyboardBuilder()
    for pid, name, variant, price in products:
        kb.button(text=f"{name} • {variant} — {price} грн", callback_data=f"product:{pid}")
    kb.button(text="⬅️ В меню", callback_data="menu")
    kb.adjust(1)
    await call.message.edit_text(f"Каталог ({city}):", reply_markup=kb.as_markup())
    await call.answer()

@router.callback_query(F.data.startswith("product:"))
async def cb_product(call: CallbackQuery):
    pid = int(call.data.split("product:", 1)[1])
    p = await get_product(pid)
    if not p:
        await call.message.edit_text("Товар не найден.", reply_markup=kb_back_to_menu())
        await call.answer()
        return

    _id, city, name, variant, price, desc = p
    text = (
        f"📦 {name