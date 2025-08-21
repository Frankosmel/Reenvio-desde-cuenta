import asyncio
import os
import re
from datetime import datetime
from typing import List, Optional, Tuple

import aiosqlite
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from telethon.tl.types import Message

from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils import executor

# ========= Carga de entorno =========
load_dotenv()
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
TZ = os.getenv("TZ", "America/Havana")

assert API_ID and API_HASH and BOT_TOKEN and OWNER_ID, "Faltan variables en .env"

DB_PATH = "control.db"
SESSION_NAME = "user_session"

# ========= Clientes =========
user_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)
scheduler = AsyncIOScheduler(timezone=TZ)

# ========= Teclados =========
KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("💫 Enviar ahora"), KeyboardButton("🗂️ Listas destinos")],
        [KeyboardButton("📡 Origen"), KeyboardButton("⏱️ Programar")],
        [KeyboardButton("🧪 Prueba envío"), KeyboardButton("⚙️ Ayuda")]
    ],
    resize_keyboard=True
)

LISTS_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("➕ Crear lista"), KeyboardButton("➖ Eliminar lista")],
        [KeyboardButton("📥 Agregar destinos"), KeyboardButton("📤 Ver listas")],
        [KeyboardButton("⬅️ Volver")]
    ],
    resize_keyboard=True
)

ORIGIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("🎯 Definir origen"), KeyboardButton("🔍 Ver origen")],
        [KeyboardButton("⬅️ Volver")]
    ],
    resize_keyboard=True
)

PROGRAM_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("🆕 Nueva tarea"), KeyboardButton("📃 Ver tareas")],
        [KeyboardButton("🗑️ Eliminar tarea"), KeyboardButton("⬅️ Volver")]
    ],
    resize_keyboard=True
)

# ========= DB =========
INIT_SQL = """
CREATE TABLE IF NOT EXISTS lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS list_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id INTEGER NOT NULL,
    peer TEXT NOT NULL,
    FOREIGN KEY(list_id) REFERENCES lists(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name TEXT UNIQUE NOT NULL,
    list_name TEXT NOT NULL,
    seconds INTEGER NOT NULL
);
"""

async def db_exec(query: str, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(query, params)
        await db.commit()

async def db_query(query: str, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(query, params)
        rows = await cur.fetchall()
        return rows

async def set_config(key: str, value: str):
    await db_exec(
        "INSERT INTO config(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value)
    )

async def get_config(key: str) -> Optional[str]:
    rows = await db_query("SELECT value FROM config WHERE key=?", (key,))
    return rows[0][0] if rows else None

# ========= Utilidades =========
def owner_only(func):
    async def wrapper(message: types.Message, *args, **kwargs):
        if message.from_user.id != OWNER_ID:
            return
        return await func(message, *args, **kwargs)
    return wrapper

async def resolve_peer(identifier: str):
    try:
        return await user_client.get_entity(identifier)
    except Exception:
        # Intento con int
        if re.fullmatch(r"-?\d+", identifier):
            try:
                return await user_client.get_entity(int(identifier))
            except Exception:
                return None
        return None

async def get_list_targets(list_name: str) -> List[str]:
    row = await db_query("SELECT id FROM lists WHERE name=?", (list_name,))
    if not row:
        return []
    list_id = row[0][0]
    rows = await db_query("SELECT peer FROM list_targets WHERE list_id=?", (list_id,))
    return [r[0] for r in rows]

async def copy_message_to_targets(msg: Message, targets: List[str]) -> Tuple[int, int]:
    ok, fail = 0, 0
    for t in targets:
        try:
            entity = await resolve_peer(t)
            if not entity:
                fail += 1
                continue

            if msg.grouped_id:
                # ÁLBUM: recolectar y enviar todos los medios del mismo grouped_id
                media_msgs = []
                async for m in user_client.iter_messages(msg.peer_id, min_id=msg.id-20, max_id=msg.id+20):
                    if m.grouped_id == msg.grouped_id:
                        media_msgs.append(m)
                media_msgs.sort(key=lambda x: x.id)
                files = []
                caption = None
                entities = None
                for m in media_msgs:
                    if m.media:
                        path = await user_client.download_media(m, file=f"temp_media_{m.id}")
                        files.append(path)
                    if (m.message or "") and caption is None:
                        caption = m.message
                        entities = m.entities
                if files:
                    await user_client.send_file(entity, files, caption=caption or "", formatting_entities=entities)
                for f in files:
                    try: os.remove(f)
                    except Exception: pass

            elif msg.media:
                path = await user_client.download_media(msg, file="temp_media")
                caption = msg.message or ""
                entities = msg.entities or []
                await user_client.send_file(entity, path, caption=caption, formatting_entities=entities)
                try: os.remove(path)
                except Exception: pass
            else:
                await user_client.send_message(entity, msg.message or "", formatting_entities=msg.entities or [])

            ok += 1
            await asyncio.sleep(0.8)  # antiflood
        except FloodWaitError as fw:
            # Respetar FloodWait de Telegram
            await asyncio.sleep(fw.seconds + 1)
        except Exception:
            fail += 1
            await asyncio.sleep(1.2)
    return ok, fail

# ========= Origen =========
async def get_origin() -> Optional[str]:
    return await get_config("origin")

async def set_origin(identifier: str):
    ent = await resolve_peer(identifier)
    if not ent:
        return False
    await set_config("origin", str(identifier))
    return True

async def fetch_last_message_from_origin() -> Optional[Message]:
    origin = await get_origin()
    if not origin:
        return None
    try:
        ent = await user_client.get_entity(origin)
        async for m in user_client.iter_messages(ent, limit=1):
            return m
    except Exception:
        return None
    return None

# ========= Scheduler =========
async def run_scheduled_job(list_name: str):
    msg = await fetch_last_message_from_origin()
    if not msg:
        return
    targets = await get_list_targets(list_name)
    await copy_message_to_targets(msg, targets)

def add_interval_job(job_name: str, seconds: int, list_name: str):
    scheduler.add_job(
        run_scheduled_job,
        trigger=IntervalTrigger(seconds=seconds),
        id=job_name,
        args=[list_name],
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30
    )

# ========= Handlers =========
@dp.message_handler(commands=["start"])
@owner_only
async def start_cmd(m: types.Message):
    await m.answer(
        "👋 Bienvenido al panel de publicaciones.\n"
        "Use los botones de abajo. Si es su primera vez, ejecute /login.",
        reply_markup=KB
    )

@dp.message_handler(commands=["login"])
@owner_only
async def login_cmd(m: types.Message):
    await m.answer("📱 Envíe su número en formato internacional, ej: <code>+5355555555</code>")

    @dp.message_handler(regexp=r"^\+\d{5,15}$")
    @owner_only
    async def on_phone(mm: types.Message):
        phone = mm.text.strip()
        try:
            await user_client.connect()
            if await user_client.is_user_authorized():
                await mm.answer("✅ Ya existe una sesión activa.", reply_markup=KB)
                return
            await user_client.send_code_request(phone)
            await mm.answer("🔐 Le envié un código por Telegram. Escríbalo aquí (solo números).")
        except Exception as e:
            await mm.answer(f"❌ Error solicitando código: <code>{e}</code>")
            return

        @dp.message_handler(regexp=r"^\d{3,6}$")
        @owner_only
        async def on_code(mc: types.Message):
            code = mc.text.strip()
            try:
                await user_client.sign_in(phone=phone, code=code)
                await mc.answer("✅ Sesión iniciada correctamente.", reply_markup=KB)
            except SessionPasswordNeededError:
                await mc.answer("🔑 Su cuenta tiene 2FA. Envíe ahora su contraseña (solo texto).")
                @dp.message_handler()
                @owner_only
                async def on_2fa(mp: types.Message):
                    try:
                        await user_client.sign_in(password=mp.text)
                        await mp.answer("✅ Sesión iniciada con 2FA.", reply_markup=KB)
                    except Exception as e2:
                        await mp.answer(f"❌ Error 2FA: <code>{e2}</code>")
            except Exception as e:
                await mc.answer(f"❌ Error iniciando sesión: <code>{e}</code>")

@dp.message_handler(lambda m: m.text == "⚙️ Ayuda")
@owner_only
async def help_menu(m: types.Message):
    await m.answer(
        "📚 <b>Guía rápida</b>\n"
        "• /login → iniciar sesión con su cuenta personal.\n"
        "• 📡 Origen → defina canal/grupo/usuario de donde se copiará el último mensaje.\n"
        "• 🗂️ Listas → cree listas y agregue destinos (@, id, enlace t.me).\n"
        "• 💫 Enviar ahora → copia el último mensaje del origen a la lista elegida.\n"
        "• ⏱️ Programar → tareas periódicas cada X segundos.\n",
        reply_markup=KB
    )

# ---- ORIGEN ----
@dp.message_handler(lambda m: m.text == "📡 Origen")
@owner_only
async def origin_menu(m: types.Message):
    await m.answer("Opciones de Origen:", reply_markup=ORIGIN_KB)

@dp.message_handler(lambda m: m.text == "🔍 Ver origen")
@owner_only
async def view_origin(m: types.Message):
    o = await get_origin()
    await m.answer(f"🎯 Origen actual: <code>{o or 'No definido'}</code>", reply_markup=ORIGIN_KB)

@dp.message_handler(lambda m: m.text == "🎯 Definir origen")
@owner_only
async def set_origin_prompt(m: types.Message):
    await m.answer("Envíe ahora @usuario, enlace t.me o ID del origen.", reply_markup=ORIGIN_KB)

    @dp.message_handler()
    @owner_only
    async def capture_origin(mx: types.Message):
        ident = (mx.text or "").strip()
        ok = await set_origin(ident)
        if ok:
            await mx.answer(f"✅ Origen establecido: <code>{ident}</code>", reply_markup=ORIGIN_KB)
        else:
            await mx.answer("❌ No pude resolver ese origen. Revise y reintente.", reply_markup=ORIGIN_KB)

# ---- LISTAS ----
@dp.message_handler(lambda m: m.text == "🗂️ Listas destinos")
@owner_only
async def lists_menu(m: types.Message):
    await m.answer("Gestione sus listas:", reply_markup=LISTS_KB)

@dp.message_handler(lambda m: m.text == "📤 Ver listas")
@owner_only
async def view_lists(m: types.Message):
    rows = await db_query("SELECT name FROM lists ORDER BY name")
    if not rows:
        await m.answer("No hay listas aún.", reply_markup=LISTS_KB)
        return
    lines = ["<b>Listas:</b>"]
    for (name,) in rows:
        cnt = await db_query("""
            SELECT COUNT(*) FROM list_targets lt
            JOIN lists l ON l.id=lt.list_id WHERE l.name=?
        """, (name,))
        lines.append(f"• <code>{name}</code> — {cnt[0][0]} destinos")
    await m.answer("\n".join(lines), reply_markup=LISTS_KB)

@dp.message_handler(lambda m: m.text == "➕ Crear lista")
@owner_only
async def create_list(m: types.Message):
    await m.answer("Escriba el <b>nombre</b> de la nueva lista.", reply_markup=LISTS_KB)

    @dp.message_handler()
    @owner_only
    async def capture_name(mx: types.Message):
        name = (mx.text or "").strip()
        try:
            await db_exec("INSERT INTO lists(name) VALUES(?)", (name,))
            await mx.answer(f"✅ Lista <code>{name}</code> creada.", reply_markup=LISTS_KB)
        except Exception as e:
            await mx.answer(f"❌ No se pudo crear: <code>{e}</code>", reply_markup=LISTS_KB)

@dp.message_handler(lambda m: m.text == "➖ Eliminar lista")
@owner_only
async def delete_list(m: types.Message):
    await m.answer("Indique el nombre de la lista a eliminar.", reply_markup=LISTS_KB)

    @dp.message_handler()
    @owner_only
    async def capture_del(mx: types.Message):
        name = (mx.text or "").strip()
        await db_exec("DELETE FROM lists WHERE name=?", (name,))
        await mx.answer(f"🗑️ Lista <code>{name}</code> eliminada (si existía).", reply_markup=LISTS_KB)

@dp.message_handler(lambda m: m.text == "📥 Agregar destinos")
@owner_only
async def add_targets(m: types.Message):
    await m.answer(
        "Formato:\n<code>NOMBRE_LISTA\n@canal_1\n123456789\nhttps://t.me/grupo</code>\n"
        "Una entrada por línea.",
        reply_markup=LISTS_KB
    )

    @dp.message_handler()
    @owner_only
    async def capture_targets(mx: types.Message):
        lines = [l.strip() for l in (mx.text or "").splitlines() if l.strip()]
        if not lines:
            await mx.answer("Entrada vacía.", reply_markup=LISTS_KB)
            return
        list_name = lines[0]
        row = await db_query("SELECT id FROM lists WHERE name=?", (list_name,))
        if not row:
            await mx.answer("Esa lista no existe. Créela primero.", reply_markup=LISTS_KB)
            return
        list_id = row[0][0]
        added, skipped = 0, 0
        for peer in lines[1:]:
            try:
                await db_exec("INSERT INTO list_targets(list_id,peer) VALUES(?,?)", (list_id, peer))
                added += 1
            except Exception:
                skipped += 1
        await mx.answer(f"✅ Agregados: {added} • Omitidos: {skipped}", reply_markup=LISTS_KB)

# ---- ENVIAR AHORA ----
@dp.message_handler(lambda m: m.text == "💫 Enviar ahora")
@owner_only
async def send_now_menu(m: types.Message):
    rows = await db_query("SELECT name FROM lists ORDER BY name")
    if not rows:
        await m.answer("No hay listas. Cree una en 🗂️ Listas destinos.", reply_markup=KB)
        return
    names = [r[0] for r in rows]
    await m.answer("Escriba el nombre de la lista destino o <code>ALL</code>.", reply_markup=KB)

    @dp.message_handler()
    @owner_only
    async def capture_list_send(mx: types.Message):
        sel = (mx.text or "").strip()
        msg = await fetch_last_message_from_origin()
        if not msg:
            await mx.answer("❌ No hay mensaje de origen (defina 📡 Origen).", reply_markup=KB)
            return

        if sel.upper() == "ALL":
            targets = []
            for n in names:
                targets += await get_list_targets(n)
        else:
            targets = await get_list_targets(sel)

        if not targets:
            await mx.answer("Esa lista no tiene destinos.", reply_markup=KB)
            return

        ok, fail = await copy_message_to_targets(msg, targets)
        await mx.answer(f"📤 Envío completado → OK: {ok} • Fallos: {fail}", reply_markup=KB)

# ---- PROGRAMAR ----
@dp.message_handler(lambda m: m.text == "⏱️ Programar")
@owner_only
async def program_menu(m: types.Message):
    await m.answer("Gestione tareas:", reply_markup=PROGRAM_KB)

@dp.message_handler(lambda m: m.text == "📃 Ver tareas")
@owner_only
async def view_jobs(m: types.Message):
    rows = await db_query("SELECT job_name, list_name, seconds FROM jobs ORDER BY job_name")
    if not rows:
        await m.answer("No hay tareas.", reply_markup=PROGRAM_KB)
        return
    lines = ["<b>Tareas:</b>"]
    for jn, ln, s in rows:
        lines.append(f"• <code>{jn}</code> → lista <code>{ln}</code> cada {s}s")
    await m.answer("\n".join(lines), reply_markup=PROGRAM_KB)

@dp.message_handler(lambda m: m.text == "🗑️ Eliminar tarea")
@owner_only
async def del_job_prompt(m: types.Message):
    await m.answer("Escriba el <code>job_name</code> a eliminar.", reply_markup=PROGRAM_KB)

    @dp.message_handler()
    @owner_only
    async def del_job(mc: types.Message):
        job_name = (mc.text or "").strip()
        try:
            scheduler.remove_job(job_name)
        except Exception:
            pass
        await db_exec("DELETE FROM jobs WHERE job_name=?", (job_name,))
        await mc.answer("🗑️ Tarea eliminada (si existía).", reply_markup=PROGRAM_KB)

@dp.message_handler(lambda m: m.text == "🆕 Nueva tarea")
@owner_only
async def new_job_prompt(m: types.Message):
    await m.answer("Formato:\n<code>job_name|NOMBRE_LISTA|segundos</code>", reply_markup=PROGRAM_KB)

    @dp.message_handler(regexp=r"^[\w\-]{3,}\|.+\|\d+$")
    @owner_only
    async def capture_job(mx: types.Message):
        try:
            job_name, list_name, secs = (mx.text or "").strip().split("|")
            secs = int(secs)
            if not await get_list_targets(list_name):
                await mx.answer("❌ La lista no existe o no tiene destinos.", reply_markup=PROGRAM_KB)
                return
            await db_exec(
                "INSERT INTO jobs(job_name, list_name, seconds) VALUES(?,?,?)",
                (job_name, list_name, secs)
            )
            add_interval_job(job_name, secs, list_name)
            await mx.answer(f"✅ Tarea {job_name} creada: cada {secs}s → <code>{list_name}</code>.", reply_markup=PROGRAM_KB)
        except Exception as e:
            await mx.answer(f"❌ Error: <code>{e}</code>", reply_markup=PROGRAM_KB)

# ---- PRUEBA ----
@dp.message_handler(lambda m: m.text == "🧪 Prueba envío")
@owner_only
async def test_send(m: types.Message):
    msg = await fetch_last_message_from_origin()
    if not msg:
        await m.answer("❌ No hay origen definido.", reply_markup=KB)
        return
    await m.answer("✅ Puedo leer el último mensaje del origen. Use «💫 Enviar ahora» o «⏱️ Programar».", reply_markup=KB)

@dp.message_handler(lambda m: m.text == "⬅️ Volver")
@owner_only
async def back_main(m: types.Message):
    await m.answer("Menú principal:", reply_markup=KB)

# ========= Boot =========
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(INIT_SQL)
        await db.commit()

async def restore_jobs():
    rows = await db_query("SELECT job_name, list_name, seconds FROM jobs")
    for jn, ln, s in rows:
        try:
            add_interval_job(jn, s, ln)
        except Exception:
            pass

async def start_all():
    await init_db()
    await user_client.connect()
    scheduler.start()
    await restore_jobs()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(start_all())
    executor.start_polling(dp, skip_updates=True)
