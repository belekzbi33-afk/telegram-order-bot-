
"""
Telegram Order Manager — production-oriented single-file bot.

Designed for generic order/status tracking.
Features:
- One unified futuristic admin dashboard
- Reliable admin authorization by Telegram user ID + username fallback
- Automatic DB migration from the old orders table
- Order creation / search / pagination
- Status + progress + minutes management
- Customer deep-link association
- Automatic customer notifications
- Order deletion with confirmation
- Customer activity log
- Admin statistics
- Broadcast to linked customers
- Help / commands
- Render/FastAPI webhook
- SQLite persistence on /data when a Render disk is mounted
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Request
from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "weevoo26").lstrip("@").lower()
BOT_USERNAME_ENV = os.environ.get("BOT_USERNAME", "").lstrip("@")

DB = "/data/orders.db" if os.path.isdir("/data") else "orders.db"

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger("order-bot")

app_web = FastAPI()
tg_app = Application.builder().token(TOKEN).build()

PAGE_SIZE = 8
MAX_BROADCAST = 500

STATUSES = {
    "attesa": ("In attesa", "🟡", 0),
    "lavorazione": ("In elaborazione", "🔵", 50),
    "quasi_fatto": ("Quasi fatto", "🟠", 80),
    "completato": ("Completato", "🟢", 100),
    "annullato": ("Annullato", "🔴", 0),
}

STATUS_BY_NAME = {v[0]: (k, v[1], v[2]) for k, v in STATUSES.items()}


# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=15000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def column_exists(con: sqlite3.Connection, table: str, column: str) -> bool:
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def migrate_db() -> None:
    con = db()

    con.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            code TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'In attesa',
            progress INTEGER NOT NULL DEFAULT 0,
            minutes INTEGER,
            note TEXT,
            telegram_user_id INTEGER,
            customer_username TEXT,
            customer_name TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )
    """)

    # Migrate columns from the user's original DB without deleting data.
    migrations = [
        ("minutes", "INTEGER"),
        ("note", "TEXT"),
        ("telegram_user_id", "INTEGER"),
        ("customer_username", "TEXT"),
        ("customer_name", "TEXT"),
        ("created_at", "TEXT"),
        ("updated_at", "TEXT"),
        ("completed_at", "TEXT"),
    ]

    for name, definition in migrations:
        if not column_exists(con, "orders", name):
            con.execute(f"ALTER TABLE orders ADD COLUMN {name} {definition}")

    stamp = now_iso()
    con.execute(
        "UPDATE orders SET created_at=? WHERE created_at IS NULL OR created_at=''",
        (stamp,),
    )
    con.execute(
        "UPDATE orders SET updated_at=? WHERE updated_at IS NULL OR updated_at=''",
        (stamp,),
    )

    con.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_user_id INTEGER,
            username TEXT,
            display_name TEXT,
            action TEXT NOT NULL,
            details TEXT,
            created_at TEXT NOT NULL
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_user_id INTEGER PRIMARY KEY,
            username TEXT,
            display_name TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        )
    """)

    con.execute("""
        CREATE INDEX IF NOT EXISTS idx_orders_updated
        ON orders(updated_at DESC)
    """)

    con.execute("""
        CREATE INDEX IF NOT EXISTS idx_orders_customer
        ON orders(telegram_user_id)
    """)

    con.execute("""
        CREATE INDEX IF NOT EXISTS idx_activity_user
        ON activity(telegram_user_id, created_at DESC)
    """)

    con.commit()
    con.close()


migrate_db()


# ---------------------------------------------------------------------------
# AUTH / USERS / ACTIVITY
# ---------------------------------------------------------------------------

def remember_user(user) -> None:
    if not user:
        return

    stamp = now_iso()
    username = user.username or ""
    display_name = " ".join(
        x for x in [user.first_name, user.last_name] if x
    ).strip()

    con = db()
    con.execute("""
        INSERT INTO users(
            telegram_user_id, username, display_name, first_seen, last_seen
        )
        VALUES(?,?,?,?,?)
        ON CONFLICT(telegram_user_id) DO UPDATE SET
            username=excluded.username,
            display_name=excluded.display_name,
            last_seen=excluded.last_seen
    """, (
        user.id,
        username,
        display_name,
        stamp,
        stamp,
    ))
    con.commit()
    con.close()


def remember_admin(user_id: int) -> None:
    con = db()
    con.execute("""
        INSERT INTO settings(key,value)
        VALUES('admin_user_id',?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (str(user_id),))
    con.commit()
    con.close()


def stored_admin_id() -> Optional[int]:
    con = db()
    row = con.execute(
        "SELECT value FROM settings WHERE key='admin_user_id'"
    ).fetchone()
    con.close()

    if not row or not row["value"]:
        return None

    try:
        return int(row["value"])
    except ValueError:
        return None


def is_admin(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False

    admin_id = stored_admin_id()
    if admin_id is not None and user.id == admin_id:
        return True

    return bool(
        user.username and
        user.username.lower() == ADMIN_USERNAME
    )


def log_activity(update: Update, action: str, details: str = "") -> None:
    user = update.effective_user
    if not user:
        return

    username = user.username or ""
    display_name = " ".join(
        x for x in [user.first_name, user.last_name] if x
    ).strip()

    con = db()
    con.execute("""
        INSERT INTO activity(
            telegram_user_id, username, display_name, action, details, created_at
        )
        VALUES(?,?,?,?,?,?)
    """, (
        user.id,
        username,
        display_name,
        action[:100],
        details[:1000],
        now_iso(),
    ))
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

async def bot_username() -> str:
    if BOT_USERNAME_ENV:
        return BOT_USERNAME_ENV
    me = await tg_app.bot.get_me()
    return me.username or "YOUR_BOT_USERNAME"


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def progress_bar(progress: int) -> str:
    progress = max(0, min(100, int(progress)))
    filled = round(progress / 10)
    return "█" * filled + "░" * (10 - filled)


def status_info(status: str):
    return STATUS_BY_NAME.get(status, ("", "⚪", 0))


def order_text(row: sqlite3.Row, admin: bool = False) -> str:
    status = row["status"]
    _, icon, _ = status_info(status)
    progress = int(row["progress"] or 0)

    text = (
        f"📦 <b>ORDINE #{esc(row['code'])}</b>\n\n"
        f"{icon} <b>Stato:</b> {esc(status)}\n"
        f"📊 <code>{progress_bar(progress)}</code> <b>{progress}%</b>\n"
    )

    if row["minutes"] is not None:
        text += f"⏱️ <b>Minuti:</b> {row['minutes']}\n"

    if row["note"]:
        text += f"📝 <b>Nota:</b> {esc(row['note'])}\n"

    if admin:
        linked = "Sì" if row["telegram_user_id"] else "No"
        customer = ""
        if row["customer_username"]:
            customer = f"@{esc(row['customer_username'])}"
        elif row["customer_name"]:
            customer = esc(row["customer_name"])

        text += (
            f"👤 <b>Cliente collegato:</b> {linked}\n"
            f"🧑 <b>Cliente:</b> {customer or '—'}\n"
            f"🕒 <b>Aggiornato:</b> {esc(row['updated_at'])}\n"
        )

    return text


def admin_home_text() -> str:
    return (
        "🚀 <b>CONTROL CENTER</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Gestione completa degli ordini in un unico pannello.\n\n"
        "⚡ Stato • progresso • minuti • clienti\n"
        "🔎 Ricerca • statistiche • attività\n"
        "🗑️ Eliminazione protetta • notifiche\n\n"
        "👇 <b>Scegli un'azione</b>"
    )


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Nuovo ordine", callback_data="home:create"),
            InlineKeyboardButton("📦 Tutti gli ordini", callback_data="home:orders:0"),
        ],
        [
            InlineKeyboardButton("⚙️ Gestisci", callback_data="home:manage:0"),
            InlineKeyboardButton("🔎 Cerca", callback_data="home:search"),
        ],
        [
            InlineKeyboardButton("📊 Statistiche", callback_data="home:stats"),
            InlineKeyboardButton("👥 Clienti", callback_data="home:customers"),
        ],
        [
            InlineKeyboardButton("📝 Attività", callback_data="home:activity:0"),
            InlineKeyboardButton("📣 Notifica clienti", callback_data="home:broadcast"),
        ],
        [
            InlineKeyboardButton("❓ Aiuto", callback_data="home:help"),
            InlineKeyboardButton("🔄 Aggiorna", callback_data="home:refresh"),
        ],
    ])


def back_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Control Center", callback_data="home:refresh")]
    ])


def orders_page_keyboard(rows, page: int, mode: str = "manage"):
    buttons = []

    for row in rows:
        label = f"#{row['code']} · {row['status']} · {row['progress']}%"
        buttons.append([
            InlineKeyboardButton(
                label[:60],
                callback_data=f"open:{row['code']}"
            )
        ])

    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton("◀️", callback_data=f"home:{mode}:{page-1}")
        )
    if len(rows) == PAGE_SIZE:
        nav.append(
            InlineKeyboardButton("▶️", callback_data=f"home:{mode}:{page+1}")
        )
    if nav:
        buttons.append(nav)

    buttons.append([
        InlineKeyboardButton("⬅️ Menu", callback_data="home:refresh")
    ])
    return InlineKeyboardMarkup(buttons)


def order_keyboard(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟡 Attesa", callback_data=f"status:{code}:attesa"),
            InlineKeyboardButton("🔵 Lavorazione", callback_data=f"status:{code}:lavorazione"),
        ],
        [
            InlineKeyboardButton("🟠 Quasi fatto", callback_data=f"status:{code}:quasi_fatto"),
            InlineKeyboardButton("🟢 Completato", callback_data=f"status:{code}:completato"),
        ],
        [
            InlineKeyboardButton("🔴 Annullato", callback_data=f"status:{code}:annullato"),
        ],
        [
            InlineKeyboardButton("📊 Progresso", callback_data=f"progress:{code}"),
            InlineKeyboardButton("⏱️ Minuti", callback_data=f"minutes:{code}"),
        ],
        [
            InlineKeyboardButton("📝 Nota / messaggio", callback_data=f"note:{code}"),
        ],
        [
            InlineKeyboardButton("🔗 Link cliente", callback_data=f"link:{code}"),
            InlineKeyboardButton("👤 Cliente", callback_data=f"customer:{code}"),
        ],
        [
            InlineKeyboardButton("🗑️ Elimina", callback_data=f"delete:{code}"),
            InlineKeyboardButton("⬅️ Indietro", callback_data="home:manage:0"),
        ],
    ])


async def edit_or_send(update: Update, text: str, markup=None):
    q = update.callback_query
    if q and q.message:
        try:
            await q.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
            return
        except TelegramError:
            pass

    if update.effective_message:
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )


async def notify_customer(
    context: ContextTypes.DEFAULT_TYPE,
    row: sqlite3.Row,
    title: str = "🔔 Aggiornamento ordine",
):
    user_id = row["telegram_user_id"]
    if not user_id:
        return False

    try:
        await context.bot.send_message(
            chat_id=int(user_id),
            text=(
                f"{title}\n\n"
                + order_text(row, admin=False)
            ),
            parse_mode=ParseMode.HTML,
        )
        return True
    except (Forbidden, TelegramError):
        return False


# ---------------------------------------------------------------------------
# /START / /HELP / ADMIN
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update.effective_user)

    # Admin gets the dashboard immediately from /start.
    if is_admin(update) and not context.args:
        remember_admin(update.effective_user.id)
        log_activity(update, "/start", "admin dashboard")
        await update.message.reply_text(
            admin_home_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=home_keyboard(),
        )
        return

    code = context.args[0].strip() if context.args else None

    if not code:
        con = db()
        row = con.execute(
            "SELECT * FROM orders "
            "WHERE telegram_user_id=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (update.effective_user.id,),
        ).fetchone()
        con.close()

        if row:
            await update.message.reply_text(
                order_text(row),
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(
                "👋 <b>Benvenuto!</b>\n\n"
                "Apri il link del tuo ordine per collegarlo e visualizzarne lo stato.",
                parse_mode=ParseMode.HTML,
            )
        log_activity(update, "/start", "no code")
        return

    con = db()
    row = con.execute(
        "SELECT * FROM orders WHERE code=?",
        (code,),
    ).fetchone()

    if not row:
        con.close()
        await update.message.reply_text("❌ Ordine non trovato.")
        log_activity(update, "/start", f"invalid order {code}")
        return

    user = update.effective_user
    username = user.username or ""
    customer_name = " ".join(
        x for x in [user.first_name, user.last_name] if x
    ).strip()

    con.execute(
        "UPDATE orders SET telegram_user_id=?, customer_username=?, "
        "customer_name=?, updated_at=? WHERE code=?",
        (user.id, username, customer_name, now_iso(), code),
    )
    con.commit()

    row = con.execute(
        "SELECT * FROM orders WHERE code=?",
        (code,),
    ).fetchone()
    con.close()

    log_activity(update, "/start", f"linked order {code}")

    await update.message.reply_text(
        order_text(row),
        parse_mode=ParseMode.HTML,
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update.effective_user)

    if not is_admin(update):
        await update.message.reply_text("⛔ Accesso non autorizzato.")
        return

    remember_admin(update.effective_user.id)
    log_activity(update, "/admin", "opened dashboard")

    await update.message.reply_text(
        admin_home_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=home_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update):
        remember_admin(update.effective_user.id)

    text = (
        "❓ <b>GUIDA</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "👤 <b>Cliente</b>\n"
        "• /start — visualizza l'ordine collegato\n"
        "• link /start=CODICE — collega un ordine\n\n"
        "👑 <b>Admin</b>\n"
        "• /admin — apre il Control Center\n"
        "• /crea CODICE — crea rapidamente un ordine\n"
        "• /stato CODICE STATO — cambia rapidamente lo stato\n"
        "• /ordini — lista ordini\n"
        "• /stats — statistiche\n"
        "• /cerca TESTO — ricerca\n\n"
        "Gli strumenti completi sono disponibili nei pulsanti del pannello."
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=back_home_keyboard() if is_admin(update) else None,
    )


# ---------------------------------------------------------------------------
# QUICK COMMANDS
# ---------------------------------------------------------------------------

async def create_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    remember_admin(update.effective_user.id)

    if not context.args:
        await update.message.reply_text("Uso: /crea CODICE")
        return

    code = context.args[0].strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", code):
        await update.message.reply_text(
            "❌ Codice non valido. Usa lettere, numeri, . _ -"
        )
        return

    stamp = now_iso()
    con = db()

    try:
        con.execute("""
            INSERT INTO orders(
                code,status,progress,minutes,
                telegram_user_id,customer_username,customer_name,
                created_at,updated_at,completed_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?)
        """, (
            code,
            "In attesa",
            0,
            None,
            None,
            None,
            None,
            stamp,
            stamp,
            None,
        ))
        con.commit()
    except sqlite3.IntegrityError:
        con.close()
        await update.message.reply_text("⚠️ Questo codice esiste già.")
        return

    row = con.execute(
        "SELECT * FROM orders WHERE code=?",
        (code,),
    ).fetchone()
    con.close()

    username = await bot_username()
    link = f"https://t.me/{username}?start={code}"

    log_activity(update, "/crea", f"created {code}")

    await update.message.reply_text(
        "✅ <b>ORDINE CREATO</b>\n\n"
        + order_text(row, admin=True)
        + f"\n🔗 <b>Link cliente:</b> <a href=\"{esc(link)}\">Apri il link</a>\n\n<code>{esc(link)}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            order_keyboard(code).inline_keyboard
            + [[
                InlineKeyboardButton("🔗 Apri link", url=link),
                InlineKeyboardButton("📋 Copia link", copy_text=CopyTextButton(link)),
            ]]
        ),
    )


async def stato_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Uso: /stato CODICE attesa|lavorazione|quasi_fatto|completato|annullato"
        )
        return

    code = context.args[0].strip()
    key = context.args[1].lower()

    if key not in STATUSES:
        await update.message.reply_text("❌ Stato non valido.")
        return

    status, _, progress = STATUSES[key]

    con = db()
    row = con.execute(
        "SELECT * FROM orders WHERE code=?",
        (code,),
    ).fetchone()

    if not row:
        con.close()
        await update.message.reply_text("❌ Ordine non trovato.")
        return

    completed_at = now_iso() if status == "Completato" else None

    con.execute(
        "UPDATE orders SET status=?, progress=?, updated_at=?, "
        "completed_at=? WHERE code=?",
        (status, progress, now_iso(), completed_at, code),
    )
    con.commit()

    row = con.execute(
        "SELECT * FROM orders WHERE code=?",
        (code,),
    ).fetchone()
    con.close()

    log_activity(update, "/stato", f"{code} -> {status}")

    await update.message.reply_text(
        order_text(row, admin=True),
        parse_mode=ParseMode.HTML,
        reply_markup=order_keyboard(code),
    )
    await notify_customer(context, row)


async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await render_orders(update, page=0, mode="orders")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await render_stats(update)


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    if not context.args:
        await update.message.reply_text("Uso: /cerca CODICE o @username")
        return

    term = " ".join(context.args).strip()
    await render_search(update, term)


# ---------------------------------------------------------------------------
# RENDER PAGES
# ---------------------------------------------------------------------------

async def render_orders(update: Update, page: int = 0, mode: str = "orders"):
    con = db()
    offset = page * PAGE_SIZE

    rows = con.execute(
        "SELECT * FROM orders ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        (PAGE_SIZE, offset),
    ).fetchall()

    total = con.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"]
    con.close()

    title = "📦 <b>TUTTI GLI ORDINI</b>" if mode == "orders" else "⚙️ <b>GESTIONE ORDINI</b>"
    text = (
        f"{title}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Pagina <b>{page + 1}</b> · Totale <b>{total}</b>\n\n"
    )

    if not rows:
        text += "Nessun ordine in questa pagina."
    else:
        for r in rows:
            _, icon, _ = status_info(r["status"])
            text += (
                f"{icon} <b>#{esc(r['code'])}</b> — "
                f"{esc(r['status'])} — {r['progress']}%"
            )
            if r["minutes"] is not None:
                text += f" — ⏱️ {r['minutes']}m"
            text += "\n"

    await edit_or_send(
        update,
        text,
        orders_page_keyboard(rows, page, mode),
    )


async def render_stats(update: Update):
    con = db()

    total = con.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]
    linked = con.execute(
        "SELECT COUNT(*) n FROM orders WHERE telegram_user_id IS NOT NULL"
    ).fetchone()["n"]
    pending = con.execute(
        "SELECT COUNT(*) n FROM orders WHERE status='In attesa'"
    ).fetchone()["n"]
    working = con.execute(
        "SELECT COUNT(*) n FROM orders WHERE status='In elaborazione'"
    ).fetchone()["n"]
    completed = con.execute(
        "SELECT COUNT(*) n FROM orders WHERE status='Completato'"
    ).fetchone()["n"]
    cancelled = con.execute(
        "SELECT COUNT(*) n FROM orders WHERE status='Annullato'"
    ).fetchone()["n"]
    users = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    activities = con.execute("SELECT COUNT(*) n FROM activity").fetchone()["n"]

    con.close()

    text = (
        "📊 <b>STATISTICHE</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 Ordini totali: <b>{total}</b>\n"
        f"🟡 In attesa: <b>{pending}</b>\n"
        f"🔵 In elaborazione: <b>{working}</b>\n"
        f"🟢 Completati: <b>{completed}</b>\n"
        f"🔴 Annullati: <b>{cancelled}</b>\n\n"
        f"👤 Ordini collegati: <b>{linked}</b>\n"
        f"👥 Utenti visti: <b>{users}</b>\n"
        f"📝 Attività registrate: <b>{activities}</b>\n"
    )

    await edit_or_send(update, text, back_home_keyboard())


async def render_customers(update: Update):
    con = db()
    rows = con.execute("""
        SELECT
            u.telegram_user_id,
            u.username,
            u.display_name,
            u.last_seen,
            COUNT(o.code) AS orders_count
        FROM users u
        LEFT JOIN orders o
            ON o.telegram_user_id = u.telegram_user_id
        GROUP BY u.telegram_user_id
        ORDER BY u.last_seen DESC
        LIMIT 30
    """).fetchall()
    con.close()

    text = "👥 <b>CLIENTI RECENTI</b>\n━━━━━━━━━━━━━━━━━━\n\n"

    if not rows:
        text += "Nessun utente registrato."
    else:
        for r in rows:
            name = r["display_name"] or "Senza nome"
            username = f"@{r['username']}" if r["username"] else ""
            text += (
                f"👤 <b>{esc(name)}</b> {esc(username)}\n"
                f"   Ordini: {r['orders_count']} · "
                f"ID: <code>{r['telegram_user_id']}</code>\n"
            )

    await edit_or_send(update, text, back_home_keyboard())


async def render_activity(update: Update, page: int = 0):
    con = db()
    offset = page * PAGE_SIZE
    rows = con.execute("""
        SELECT * FROM activity
        ORDER BY id DESC
        LIMIT ? OFFSET ?
    """, (PAGE_SIZE, offset)).fetchall()
    con.close()

    text = (
        "📝 <b>ATTIVITÀ RECENTE</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
    )

    if not rows:
        text += "Nessuna attività."
    else:
        for r in rows:
            name = f"@{r['username']}" if r["username"] else str(r["telegram_user_id"])
            text += (
                f"• <b>{esc(name)}</b> — {esc(r['action'])}\n"
                f"  {esc(r['details'] or '')}\n"
                f"  <i>{esc(r['created_at'])}</i>\n\n"
            )

    buttons = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "◀️",
            callback_data=f"home:activity:{page-1}"
        ))
    if len(rows) == PAGE_SIZE:
        nav.append(InlineKeyboardButton(
            "▶️",
            callback_data=f"home:activity:{page+1}"
        ))
    if nav:
        buttons.append(nav)
    buttons.append([
        InlineKeyboardButton("⬅️ Menu", callback_data="home:refresh")
    ])

    await edit_or_send(update, text, InlineKeyboardMarkup(buttons))


async def render_search(update: Update, term: str):
    like = f"%{term}%"
    con = db()
    rows = con.execute("""
        SELECT * FROM orders
        WHERE code LIKE ?
           OR status LIKE ?
           OR customer_username LIKE ?
           OR customer_name LIKE ?
        ORDER BY updated_at DESC
        LIMIT 30
    """, (like, like, like, like)).fetchall()
    con.close()

    text = (
        f"🔎 <b>RISULTATI</b>\n"
        f"Ricerca: <code>{esc(term)}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
    )

    buttons = []

    if not rows:
        text += "Nessun risultato."
    else:
        for r in rows:
            _, icon, _ = status_info(r["status"])
            text += (
                f"{icon} <b>#{esc(r['code'])}</b> — "
                f"{esc(r['status'])} — {r['progress']}%\n"
            )
            buttons.append([
                InlineKeyboardButton(
                    f"⚙️ #{r['code']}",
                    callback_data=f"open:{r['code']}"
                )
            ])

    buttons.append([
        InlineKeyboardButton("⬅️ Menu", callback_data="home:refresh")
    ])

    await edit_or_send(update, text, InlineKeyboardMarkup(buttons))


# ---------------------------------------------------------------------------
# CALLBACK HANDLER
# ---------------------------------------------------------------------------

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not is_admin(update):
        await query.answer("⛔ Accesso non autorizzato.", show_alert=True)
        return

    await query.answer()
    remember_admin(update.effective_user.id)
    data = query.data or ""

    # Main dashboard
    if data in ("home:refresh", "home:home"):
        context.user_data.pop("awaiting", None)
        await edit_or_send(update, admin_home_text(), home_keyboard())
        return

    if data == "home:create":
        context.user_data["awaiting"] = "create"
        await edit_or_send(
            update,
            "➕ <b>NUOVO ORDINE</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Scrivi ora il <b>codice</b> del nuovo ordine.\n\n"
            "Esempio: <code>ORD-1042</code>\n\n"
            "❌ /annulla per uscire.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Annulla", callback_data="home:refresh")]
            ]),
        )
        return

    if data.startswith("home:orders:"):
        page = int(data.rsplit(":", 1)[1])
        await render_orders(update, page, "orders")
        return

    if data.startswith("home:manage:"):
        page = int(data.rsplit(":", 1)[1])
        await render_orders(update, page, "manage")
        return

    if data == "home:stats":
        await render_stats(update)
        return

    if data == "home:customers":
        await render_customers(update)
        return

    if data.startswith("home:activity:"):
        page = int(data.rsplit(":", 1)[1])
        await render_activity(update, page)
        return

    if data == "home:search":
        context.user_data["awaiting"] = "search"
        await edit_or_send(
            update,
            "🔎 <b>CERCA ORDINE</b>\n\n"
            "Scrivi codice, username o nome cliente.\n\n"
            "❌ /annulla per uscire.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Annulla", callback_data="home:refresh")]
            ]),
        )
        return

    if data == "home:broadcast":
        context.user_data["awaiting"] = "broadcast"
        await edit_or_send(
            update,
            "📣 <b>NOTIFICA CLIENTI</b>\n\n"
            "Scrivi il messaggio da inviare agli utenti collegati.\n\n"
            "Il sistema mostrerà prima una conferma.\n\n"
            "❌ /annulla per uscire.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Annulla", callback_data="home:refresh")]
            ]),
        )
        return

    if data == "home:help":
        await edit_or_send(
            update,
            "❓ <b>CONTROL CENTER — GUIDA</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "📦 <b>Ordini</b> — lista completa con paginazione.\n"
            "⚙️ <b>Gestisci</b> — modifica ogni ordine.\n"
            "🔎 <b>Cerca</b> — ricerca per codice/cliente.\n"
            "📊 <b>Statistiche</b> — riepilogo del database.\n"
            "👥 <b>Clienti</b> — utenti collegati.\n"
            "📝 <b>Attività</b> — storico delle interazioni.\n"
            "📣 <b>Notifica</b> — messaggi agli utenti collegati.\n\n"
            "Dentro ogni ordine puoi cambiare stato, "
            "progresso, minuti, vedere il cliente, copiare il link "
            "o eliminare l'ordine.",
            back_home_keyboard(),
        )
        return

    # Open a specific order
    if data.startswith("open:"):
        code = data.split(":", 1)[1]
        con = db()
        row = con.execute(
            "SELECT * FROM orders WHERE code=?",
            (code,),
        ).fetchone()
        con.close()

        if not row:
            await edit_or_send(update, "❌ Ordine non trovato.", back_home_keyboard())
            return

        await edit_or_send(
            update,
            order_text(row, admin=True),
            order_keyboard(code),
        )
        return

    # Status
    if data.startswith("status:"):
        _, code, key = data.split(":", 2)

        if key not in STATUSES:
            return

        status, _, default_progress = STATUSES[key]

        con = db()
        row = con.execute(
            "SELECT * FROM orders WHERE code=?",
            (code,),
        ).fetchone()

        if not row:
            con.close()
            await edit_or_send(update, "❌ Ordine non trovato.", back_home_keyboard())
            return

        completed_at = now_iso() if status == "Completato" else None

        con.execute("""
            UPDATE orders
            SET status=?, progress=?, updated_at=?, completed_at=?
            WHERE code=?
        """, (
            status,
            default_progress,
            now_iso(),
            completed_at,
            code,
        ))
        con.commit()

        row = con.execute(
            "SELECT * FROM orders WHERE code=?",
            (code,),
        ).fetchone()
        con.close()

        log_activity(update, "status", f"{code} -> {status}")
        await notify_customer(context, row)

        await edit_or_send(
            update,
            "✅ <b>Stato aggiornato</b>\n\n" + order_text(row, admin=True),
            order_keyboard(code),
        )
        return

    # Progress
    if data.startswith("progress:"):
        code = data.split(":", 1)[1]
        context.user_data["awaiting"] = f"progress:{code}"

        await edit_or_send(
            update,
            f"📊 <b>PROGRESSO — #{esc(code)}</b>\n\n"
            "Scrivi una percentuale da <b>0 a 100</b>.\n"
            "Esempio: <code>75</code>",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Annulla", callback_data=f"open:{code}")]
            ]),
        )
        return

    # Minutes
    if data.startswith("minutes:"):
        code = data.split(":", 1)[1]
        context.user_data["awaiting"] = f"minutes:{code}"

        await edit_or_send(
            update,
            f"⏱️ <b>MINUTI — #{esc(code)}</b>\n\n"
            "Scrivi il numero di minuti.\n"
            "Esempio: <code>30</code>\n\n"
            "Scrivi <code>0</code> per rimuoverli.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Annulla", callback_data=f"open:{code}")]
            ]),
        )
        return

    # Note / customer-facing message
    if data.startswith("note:"):
        code = data.split(":", 1)[1]
        context.user_data["awaiting"] = f"note:{code}"

        con = db()
        row = con.execute("SELECT note FROM orders WHERE code=?", (code,)).fetchone()
        con.close()
        current = row["note"] if row else None

        await edit_or_send(
            update,
            f"📝 <b>NOTA ORDINE — #{esc(code)}</b>\n\n"
            "Scrivi il messaggio che vuoi mostrare al cliente insieme allo stato.\n"
            "È <b>opzionale</b>: puoi anche lasciare la nota vuota/rimuoverla.\n\n"
            + (f"Nota attuale: <blockquote>{esc(current)}</blockquote>\n\n" if current else "Nessuna nota impostata.\n\n")
            + "Esempio: <i>L'ordine non è andato a buon fine per un problema tecnico.</i>",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑️ Rimuovi nota", callback_data=f"note_clear:{code}")],
                [InlineKeyboardButton("⬅️ Ordine", callback_data=f"open:{code}")],
            ]),
        )
        return

    if data.startswith("note_clear:"):
        code = data.split(":", 1)[1]
        con = db()
        con.execute("UPDATE orders SET note=NULL, updated_at=? WHERE code=?", (now_iso(), code))
        con.commit()
        row = con.execute("SELECT * FROM orders WHERE code=?", (code,)).fetchone()
        con.close()
        if row:
            log_activity(update, "note", f"{code} -> cleared")
            await notify_customer(context, row, "📝 Nota ordine aggiornata")
            await edit_or_send(update, "✅ <b>Nota rimossa</b>\n\n" + order_text(row, admin=True), order_keyboard(code))
        else:
            await edit_or_send(update, "❌ Ordine non trovato.", back_home_keyboard())
        return

    # Customer info
    if data.startswith("customer:"):
        code = data.split(":", 1)[1]
        con = db()
        row = con.execute(
            "SELECT * FROM orders WHERE code=?",
            (code,),
        ).fetchone()
        con.close()

        if not row:
            await edit_or_send(update, "❌ Ordine non trovato.", back_home_keyboard())
            return

        if not row["telegram_user_id"]:
            text = f"👤 <b>CLIENTE — #{esc(code)}</b>\n\nNessun cliente collegato."
        else:
            text = (
                f"👤 <b>CLIENTE — #{esc(code)}</b>\n\n"
                f"Telegram ID: <code>{row['telegram_user_id']}</code>\n"
                f"Username: @{esc(row['customer_username'] or '—')}\n"
                f"Nome: {esc(row['customer_name'] or '—')}"
            )

        await edit_or_send(
            update,
            text,
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Ordine", callback_data=f"open:{code}")]
            ]),
        )
        return

    # Link
    if data.startswith("link:"):
        code = data.split(":", 1)[1]
        username = await bot_username()
        link = f"https://t.me/{username}?start={code}"

        await edit_or_send(
            update,
            f"🔗 <b>LINK CLIENTE</b>\n\n"
            f"Ordine: <b>#{esc(code)}</b>\n\n"
            f"👉 <a href=\"{esc(link)}\">Apri il link del cliente</a>\n\n"
            f"<code>{esc(link)}</code>\n\n"
            "💡 Puoi anche copiare l'URL qui sopra e inviarlo direttamente al cliente.",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔗 Apri link cliente", url=link),
                    InlineKeyboardButton("📋 Copia link", copy_text=CopyTextButton(link)),
                ],
                [InlineKeyboardButton("⬅️ Ordine", callback_data=f"open:{code}")]
            ]),
        )
        return

    # Delete
    if data.startswith("delete:"):
        code = data.split(":", 1)[1]

        await edit_or_send(
            update,
            f"⚠️ <b>ELIMINAZIONE</b>\n\n"
            f"Vuoi eliminare definitivamente <b>#{esc(code)}</b>?\n\n"
            "Questa azione non può essere annullata.",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ Sì, elimina",
                        callback_data=f"confirmdelete:{code}"
                    ),
                    InlineKeyboardButton(
                        "❌ No",
                        callback_data=f"open:{code}"
                    ),
                ]
            ]),
        )
        return

    if data.startswith("confirmdelete:"):
        code = data.split(":", 1)[1]

        con = db()
        cur = con.execute(
            "DELETE FROM orders WHERE code=?",
            (code,),
        )
        con.commit()
        con.close()

        if cur.rowcount:
            log_activity(update, "delete", f"deleted order {code}")
            await edit_or_send(
                update,
                f"🗑️ <b>Ordine #{esc(code)} eliminato.</b>",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚙️ Gestisci", callback_data="home:manage:0")],
                    [InlineKeyboardButton("⬅️ Menu", callback_data="home:refresh")],
                ]),
            )
        else:
            await edit_or_send(update, "❌ Ordine non trovato.", back_home_keyboard())
        return


# ---------------------------------------------------------------------------
# TEXT INPUT FLOWS
# ---------------------------------------------------------------------------

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    remember_admin(update.effective_user.id)

    state = context.user_data.get("awaiting")
    text = (update.message.text or "").strip()

    if not state:
        # Don't treat arbitrary admin chat as a command.
        return

    if state == "create":
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", text):
            await update.message.reply_text(
                "❌ Codice non valido. Usa lettere, numeri, . _ -"
            )
            return

        stamp = now_iso()
        con = db()
        try:
            con.execute("""
                INSERT INTO orders(
                    code,status,progress,minutes,
                    telegram_user_id,customer_username,customer_name,
                    created_at,updated_at,completed_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?)
            """, (
                text, "In attesa", 0, None, None, None, None,
                stamp, stamp, None,
            ))
            con.commit()
        except sqlite3.IntegrityError:
            con.close()
            await update.message.reply_text("⚠️ Questo codice esiste già.")
            return

        row = con.execute(
            "SELECT * FROM orders WHERE code=?",
            (text,),
        ).fetchone()
        con.close()

        context.user_data.pop("awaiting", None)
        username = await bot_username()
        link = f"https://t.me/{username}?start={text}"

        log_activity(update, "create", f"created {text}")

        await update.message.reply_text(
            "✅ <b>ORDINE CREATO</b>\n\n"
            + order_text(row, admin=True)
            + f"\n🔗 <b>Link:</b> <a href=\"{esc(link)}\">Apri il link</a>\n\n<code>{esc(link)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
            order_keyboard(text).inline_keyboard
            + [[
                InlineKeyboardButton("🔗 Apri link", url=link),
                InlineKeyboardButton("📋 Copia link", copy_text=CopyTextButton(link)),
            ]]
        ),
        )
        return

    if state == "search":
        context.user_data.pop("awaiting", None)
        await render_search(update, text)
        return

    if state == "broadcast":
        if not text:
            await update.message.reply_text("❌ Messaggio vuoto.")
            return

        context.user_data["pending_broadcast"] = text

        await update.message.reply_text(
            "📣 <b>CONFERMA INVIO</b>\n\n"
            f"Messaggio:\n<blockquote>{esc(text)}</blockquote>\n\n"
            "Verrà inviato agli utenti collegati agli ordini.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ Invia",
                        callback_data="broadcast:confirm"
                    ),
                    InlineKeyboardButton(
                        "❌ Annulla",
                        callback_data="home:refresh"
                    ),
                ]
            ]),
        )
        return

    if state.startswith("note:"):
        code = state.split(":", 1)[1]
        context.user_data.pop("awaiting", None)

        con = db()
        row = con.execute("SELECT * FROM orders WHERE code=?", (code,)).fetchone()
        if not row:
            con.close()
            await update.message.reply_text("❌ Ordine non trovato.")
            return

        note = text.strip() or None
        con.execute("UPDATE orders SET note=?, updated_at=? WHERE code=?", (note, now_iso(), code))
        con.commit()
        row = con.execute("SELECT * FROM orders WHERE code=?", (code,)).fetchone()
        con.close()

        log_activity(update, "note", f"{code} -> {'set' if note else 'cleared'}")
        await notify_customer(context, row, "📝 Nota ordine aggiornata")
        await update.message.reply_text(
            "✅ <b>Nota salvata</b>\n\n" + order_text(row, admin=True),
            parse_mode=ParseMode.HTML,
            reply_markup=order_keyboard(code),
        )
        return

    if state.startswith("progress:"):
        code = state.split(":", 1)[1]

        try:
            progress = int(text)
        except ValueError:
            await update.message.reply_text("❌ Inserisci un numero da 0 a 100.")
            return

        if not 0 <= progress <= 100:
            await update.message.reply_text("❌ Il progresso deve essere tra 0 e 100.")
            return

        context.user_data.pop("awaiting", None)

        con = db()
        row = con.execute(
            "SELECT * FROM orders WHERE code=?",
            (code,),
        ).fetchone()

        if not row:
            con.close()
            await update.message.reply_text("❌ Ordine non trovato.")
            return

        con.execute(
            "UPDATE orders SET progress=?, updated_at=? WHERE code=?",
            (progress, now_iso(), code),
        )
        con.commit()

        row = con.execute(
            "SELECT * FROM orders WHERE code=?",
            (code,),
        ).fetchone()
        con.close()

        log_activity(update, "progress", f"{code} -> {progress}%")
        await notify_customer(context, row)

        await update.message.reply_text(
            "✅ <b>Progresso aggiornato</b>\n\n"
            + order_text(row, admin=True),
            parse_mode=ParseMode.HTML,
            reply_markup=order_keyboard(code),
        )
        return

    if state.startswith("minutes:"):
        code = state.split(":", 1)[1]

        try:
            minutes = int(text)
        except ValueError:
            await update.message.reply_text("❌ Inserisci un numero.")
            return

        if minutes < 0 or minutes > 100000:
            await update.message.reply_text("❌ Numero di minuti non valido.")
            return

        context.user_data.pop("awaiting", None)
        minutes_value = None if minutes == 0 else minutes

        con = db()
        row = con.execute(
            "SELECT * FROM orders WHERE code=?",
            (code,),
        ).fetchone()

        if not row:
            con.close()
            await update.message.reply_text("❌ Ordine non trovato.")
            return

        con.execute(
            "UPDATE orders SET minutes=?, updated_at=? WHERE code=?",
            (minutes_value, now_iso(), code),
        )
        con.commit()

        row = con.execute(
            "SELECT * FROM orders WHERE code=?",
            (code,),
        ).fetchone()
        con.close()

        log_activity(update, "minutes", f"{code} -> {minutes}")
        await notify_customer(context, row)

        await update.message.reply_text(
            "✅ <b>Minuti aggiornati</b>\n\n"
            + order_text(row, admin=True),
            parse_mode=ParseMode.HTML,
            reply_markup=order_keyboard(code),
        )
        return


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update):
        context.user_data.pop("awaiting", None)
        context.user_data.pop("pending_broadcast", None)
        await update.message.reply_text(
            "↩️ Operazione annullata.",
            reply_markup=home_keyboard(),
        )


# ---------------------------------------------------------------------------
# BROADCAST CALLBACK
# ---------------------------------------------------------------------------

async def broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not is_admin(update):
        await query.answer("⛔ Non autorizzato.", show_alert=True)
        return

    await query.answer()

    if query.data != "broadcast:confirm":
        return

    message = context.user_data.pop("pending_broadcast", None)
    context.user_data.pop("awaiting", None)

    if not message:
        await edit_or_send(update, "❌ Nessun messaggio da inviare.", back_home_keyboard())
        return

    con = db()
    rows = con.execute("""
        SELECT DISTINCT telegram_user_id
        FROM orders
        WHERE telegram_user_id IS NOT NULL
        LIMIT ?
    """, (MAX_BROADCAST,)).fetchall()
    con.close()

    sent = 0
    failed = 0

    for row in rows:
        try:
            await context.bot.send_message(
                chat_id=int(row["telegram_user_id"]),
                text=message,
            )
            sent += 1
        except TelegramError:
            failed += 1

        await asyncio.sleep(0.05)

    log_activity(update, "broadcast", f"sent={sent}, failed={failed}")

    await edit_or_send(
        update,
        "📣 <b>INVIO COMPLETATO</b>\n\n"
        f"✅ Inviati: <b>{sent}</b>\n"
        f"⚠️ Falliti: <b>{failed}</b>",
        back_home_keyboard(),
    )


# ---------------------------------------------------------------------------
# ERROR HANDLER
# ---------------------------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception(
        "Unhandled exception while processing update",
        exc_info=context.error,
    )


# ---------------------------------------------------------------------------
# HANDLER REGISTRATION
# ---------------------------------------------------------------------------

tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(CommandHandler("admin", admin_command))
tg_app.add_handler(CommandHandler("help", help_command))
tg_app.add_handler(CommandHandler("crea", create_command))
tg_app.add_handler(CommandHandler("stato", stato_command))
tg_app.add_handler(CommandHandler("ordini", orders_command))
tg_app.add_handler(CommandHandler("stats", stats_command))
tg_app.add_handler(CommandHandler("cerca", search_command))
tg_app.add_handler(CommandHandler("annulla", cancel_command))

# Broadcast callback is registered before the generic callback handler.
tg_app.add_handler(
    CallbackQueryHandler(broadcast_callback, pattern=r"^broadcast:")
)
tg_app.add_handler(CallbackQueryHandler(callback))

tg_app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, text_router)
)

tg_app.add_error_handler(error_handler)


# ---------------------------------------------------------------------------
# FASTAPI / RENDER WEBHOOK
# ---------------------------------------------------------------------------

@app_web.get("/")
async def home():
    return {
        "status": "ok",
        "bot": "telegram-order-bot",
        "database": DB,
    }


@app_web.post("/telegram")
async def telegram_webhook(request: Request):
    data = await request.json()
    await tg_app.process_update(
        Update.de_json(data, tg_app.bot)
    )
    return {"ok": True}


@app_web.on_event("startup")
async def startup():
    migrate_db()

    await tg_app.initialize()
    await tg_app.start()

    # Discover the actual Telegram username so links don't depend on a
    # hard-coded placeholder.
    try:
        me = await tg_app.bot.get_me()
        logger.info("Bot started as @%s", me.username)
    except TelegramError:
        logger.exception("Could not read bot profile")

    external = os.environ.get("RENDER_EXTERNAL_URL")
    if external:
        webhook_url = external.rstrip("/") + "/telegram"
        await tg_app.bot.set_webhook(webhook_url)
        logger.info("Webhook configured: %s", webhook_url)
    else:
        logger.warning("RENDER_EXTERNAL_URL is not set; webhook not configured.")


@app_web.on_event("shutdown")
async def shutdown():
    await tg_app.stop()
    await tg_app.shutdown()


if __name__ == "__main__":
    # Useful for local testing only. Render uses uvicorn bot:app_web.
    import uvicorn
    uvicorn.run(app_web, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
