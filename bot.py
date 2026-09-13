import os
import sqlite3
import asyncio
from datetime import datetime, timedelta

from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

TOKEN = os.environ["BOT_TOKEN"]

ADMIN_USERNAME = "weevoo26"
BOT_USERNAME = "Oorderstatebot"

DB = "orders.db"

app_web = FastAPI()

telegram_app = Application.builder().token(TOKEN).build()


# =========================================================
# DATABASE
# =========================================================

def db_connect():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            code TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'In attesa',
            progress INTEGER NOT NULL DEFAULT 0,
            eta_minutes INTEGER,
            telegram_user_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_user_id INTEGER,
            username TEXT,
            order_code TEXT,
            action TEXT,
            message TEXT,
            created_at TEXT NOT NULL
        )
    """)

    # Migrazione per database vecchi che non hanno completed_at
    columns = [
        row["name"]
        for row in cur.execute("PRAGMA table_info(orders)").fetchall()
    ]

    if "completed_at" not in columns:
        cur.execute(
            "ALTER TABLE orders ADD COLUMN completed_at TEXT"
        )

    conn.commit()
    conn.close()


init_db()


# =========================================================
# UTILS
# =========================================================

def now():
    return datetime.now()


def now_str():
    return now().isoformat()


def get_admin_id():
    conn = db_connect()
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'admin_id'"
    ).fetchone()
    conn.close()

    if row:
        try:
            return int(row["value"])
        except:
            return None

    return None


def save_admin_id(user_id):
    conn = db_connect()
    conn.execute("""
        INSERT INTO settings(key, value)
        VALUES('admin_id', ?)
        ON CONFLICT(key)
        DO UPDATE SET value = excluded.value
    """, (str(user_id),))
    conn.commit()
    conn.close()


def is_admin(update: Update):
    user = update.effective_user

    if not user:
        return False

    username = user.username or ""

    return username.lower() == ADMIN_USERNAME.lower()


def log_activity(
    user_id=None,
    username=None,
    order_code=None,
    action="",
    message=""
):
    conn = db_connect()

    conn.execute("""
        INSERT INTO activity_logs
        (
            telegram_user_id,
            username,
            order_code,
            action,
            message,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        username,
        order_code,
        action,
        message,
        now_str()
    ))

    conn.commit()
    conn.close()


# =========================================================
# AUTO DELETE ORDERS
# =========================================================

def delete_expired_orders():
    """
    Cancella esclusivamente gli ordini completati da almeno 30 minuti.
    Gli ordini non completati non vengono mai cancellati.
    """

    limit = now() - timedelta(minutes=30)

    conn = db_connect()

    rows = conn.execute("""
        SELECT code
        FROM orders
        WHERE progress = 100
        AND completed_at IS NOT NULL
        AND completed_at <= ?
    """, (limit.isoformat(),)).fetchall()

    for row in rows:
        code = row["code"]

        conn.execute(
            "DELETE FROM orders WHERE code = ?",
            (code,)
        )

        log_activity(
            action="auto_delete",
            order_code=code,
            message="Ordine cancellato automaticamente dopo 30 minuti dal completamento."
        )

    conn.commit()
    conn.close()


async def cleanup_loop():
    while True:
        try:
            delete_expired_orders()
        except Exception as e:
            print("Errore cleanup:", e)

        # Controlla ogni minuto
        await asyncio.sleep(60)


# =========================================================
# ORDER FUNCTIONS
# =========================================================

def get_order(code):
    conn = db_connect()

    row = conn.execute("""
        SELECT *
        FROM orders
        WHERE code = ?
    """, (code.upper(),)).fetchone()

    conn.close()

    return row


def progress_bar(progress):
    total = 10
    filled = round(progress / 10)

    return "█" * filled + "░" * (total - filled)


def order_text(row):

    status = row["status"]
    progress = row["progress"]
    eta = row["eta_minutes"]

    text = (
        f"📦 <b>Ordine {row['code']}</b>\n\n"
        f"📍 Stato: <b>{status}</b>\n"
        f"📊 Avanzamento: <b>{progress}%</b>\n\n"
        f"{progress_bar(progress)}"
    )

    if eta is not None:
        text += f"\n\n⏱️ Tempo stimato: <b>{eta} minuti</b>"

    return text


# =========================================================
# ADMIN MENU
# =========================================================

def admin_menu():

    keyboard = [
        [
            InlineKeyboardButton(
                "➕ Crea ordine",
                callback_data="admin_create"
            ),
            InlineKeyboardButton(
                "📋 Lista ordini",
                callback_data="admin_list"
            )
        ],
        [
            InlineKeyboardButton(
                "🔎 Cerca ordine",
                callback_data="admin_search"
            ),
            InlineKeyboardButton(
                "🔄 Cambia stato",
                callback_data="admin_status"
            )
        ],
        [
            InlineKeyboardButton(
                "⏱️ Imposta minuti",
                callback_data="admin_eta"
            ),
            InlineKeyboardButton(
                "📊 Statistiche",
                callback_data="admin_stats"
            )
        ],
        [
            InlineKeyboardButton(
                "👥 Clienti",
                callback_data="admin_clients"
            ),
            InlineKeyboardButton(
                "📜 Log attività",
                callback_data="admin_logs"
            )
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# CLIENT MENU
# =========================================================

def client_menu():

    keyboard = [
        [
            InlineKeyboardButton(
                "📦 Il mio ordine",
                callback_data="client_order"
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 Aggiorna",
                callback_data="client_refresh"
            ),
            InlineKeyboardButton(
                "ℹ️ Informazioni",
                callback_data="client_info"
            )
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# /START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not user:
        return

    username = user.username or ""

    # Salva automaticamente l'ID dell'admin
    if username.lower() == ADMIN_USERNAME.lower():
        save_admin_id(user.id)

        log_activity(
            user_id=user.id,
            username=username,
            action="admin_start",
            message="/start admin"
        )

        await update.message.reply_text(
            "👑 <b>Pannello amministratore</b>\n\n"
            "Usa i pulsanti qui sotto.",
            parse_mode="HTML",
            reply_markup=admin_menu()
        )

        return

    args = context.args

    # /start CODICE
    if args:

        code = args[0].upper()

        order = get_order(code)

        if not order:

            log_activity(
                user_id=user.id,
                username=username,
                order_code=code,
                action="start_invalid_order",
                message=f"/start {code}"
            )

            await update.message.reply_text(
                "❌ Ordine non trovato."
            )

            return

        # Associa l'utente all'ordine
        conn = db_connect()

        conn.execute("""
            UPDATE orders
            SET telegram_user_id = ?,
                updated_at = ?
            WHERE code = ?
        """, (
            user.id,
            now_str(),
            code
        ))

        conn.commit()
        conn.close()

        log_activity(
            user_id=user.id,
            username=username,
            order_code=code,
            action="associate_order",
            message=f"/start {code}"
        )

        await update.message.reply_text(
            "✅ Ordine associato!\n\n" +
            order_text(get_order(code)),
            parse_mode="HTML",
            reply_markup=client_menu()
        )

        return

    # /start senza codice
    conn = db_connect()

    order = conn.execute("""
        SELECT *
        FROM orders
        WHERE telegram_user_id = ?
        ORDER BY updated_at DESC
        LIMIT 1
    """, (user.id,)).fetchone()

    conn.close()

    log_activity(
        user_id=user.id,
        username=username,
        order_code=order["code"] if order else None,
        action="client_start",
        message="/start"
    )

    if order:

        await update.message.reply_text(
            order_text(order),
            parse_mode="HTML",
            reply_markup=client_menu()
        )

    else:

        await update.message.reply_text(
            "👋 Benvenuto!\n\n"
            "Non hai ancora un ordine associato."
        )


# =========================================================
# /ADMIN
# =========================================================

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):

        await update.message.reply_text(
            "❌ Non autorizzato."
        )

        return

    save_admin_id(update.effective_user.id)

    log_activity(
        user_id=update.effective_user.id,
        username=update.effective_user.username,
        action="admin_panel",
        message="/admin"
    )

    await update.message.reply_text(
        "👑 <b>Pannello amministratore</b>",
        parse_mode="HTML",
        reply_markup=admin_menu()
    )


# =========================================================
# ADMIN CALLBACK
# =========================================================

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    if not is_admin(update):
        await query.edit_message_text("❌ Non autorizzato.")
        return

    data = query.data

    # -------------------------
    # CREA
    # -------------------------

    if data == "admin_create":

        context.user_data["action"] = "create"

        await query.edit_message_text(
            "➕ <b>Crea ordine</b>\n\n"
            "Invia il codice dell'ordine.\n\n"
            "Esempio:\n"
            "<code>ABC123</code>",
            parse_mode="HTML"
        )

        return

    # -------------------------
    # LISTA
    # -------------------------

    if data == "admin_list":

        delete_expired_orders()

        conn = db_connect()

        rows = conn.execute("""
            SELECT *
            FROM orders
            ORDER BY created_at DESC
        """).fetchall()

        conn.close()

        if not rows:

            await query.edit_message_text(
                "📋 Nessun ordine presente.",
                reply_markup=admin_menu()
            )

            return

        text = "📋 <b>Ordini presenti</b>\n\n"

        for row in rows:

            text += (
                f"📦 <b>{row['code']}</b>\n"
                f"   {row['status']} — {row['progress']}%\n"
            )

            if row["eta_minutes"] is not None:
                text += f"   ⏱️ {row['eta_minutes']} min\n"

            text += "\n"

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=admin_menu()
        )

        return

    # -------------------------
    # CERCA
    # -------------------------

    if data == "admin_search":

        context.user_data["action"] = "search"

        await query.edit_message_text(
            "🔎 <b>Cerca ordine</b>\n\n"
            "Invia il codice.",
            parse_mode="HTML"
        )

        return

    # -------------------------
    # CAMBIA STATO
    # -------------------------

    if data == "admin_status":

        context.user_data["action"] = "status_code"

        await query.edit_message_text(
            "🔄 <b>Cambia stato</b>\n\n"
            "Invia il codice dell'ordine.",
            parse_mode="HTML"
        )

        return

    # -------------------------
    # ETA
    # -------------------------

    if data == "admin_eta":

        context.user_data["action"] = "eta"

        await query.edit_message_text(
            "⏱️ <b>Imposta minuti</b>\n\n"
            "Formato:\n"
            "<code>CODICE MINUTI</code>\n\n"
            "Esempio:\n"
            "<code>ABC123 25</code>\n\n"
            "Per rimuovere il tempo stimato:\n"
            "<code>ABC123 0</code>",
            parse_mode="HTML"
        )

        return

    # -------------------------
    # STATISTICHE
    # -------------------------

    if data == "admin_stats":

        delete_expired_orders()

        conn = db_connect()

        total = conn.execute(
            "SELECT COUNT(*) FROM orders"
        ).fetchone()[0]

        completed = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE progress = 100"
        ).fetchone()[0]

        active = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE progress < 100"
        ).fetchone()[0]

        customers = conn.execute("""
            SELECT COUNT(DISTINCT telegram_user_id)
            FROM orders
            WHERE telegram_user_id IS NOT NULL
        """).fetchone()[0]

        conn.close()

        await query.edit_message_text(
            "📊 <b>Statistiche</b>\n\n"
            f"📦 Ordini presenti: <b>{total}</b>\n"
            f"🟢 Completati: <b>{completed}</b>\n"
            f"🟠 Attivi: <b>{active}</b>\n"
            f"👥 Clienti associati: <b>{customers}</b>",
            parse_mode="HTML",
            reply_markup=admin_menu()
        )

        return

    # -------------------------
    # CLIENTI
    # -------------------------

    if data == "admin_clients":

        conn = db_connect()

        rows = conn.execute("""
            SELECT
                telegram_user_id,
                COUNT(*) AS orders_count,
                MAX(updated_at) AS last_activity
            FROM orders
            WHERE telegram_user_id IS NOT NULL
            GROUP BY telegram_user_id
            ORDER BY last_activity DESC
        """).fetchall()

        conn.close()

        if not rows:

            await query.edit_message_text(
                "👥 Nessun cliente associato.",
                reply_markup=admin_menu()
            )

            return

        text = "👥 <b>Clienti</b>\n\n"

        for row in rows:

            text += (
                f"👤 ID: <code>{row['telegram_user_id']}</code>\n"
                f"📦 Ordini: {row['orders_count']}\n"
                f"🕐 Ultima attività: {row['last_activity']}\n\n"
            )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=admin_menu()
        )

        return

    # -------------------------
    # LOG
    # -------------------------

    if data == "admin_logs":

        conn = db_connect()

        rows = conn.execute("""
            SELECT *
            FROM activity_logs
            ORDER BY id DESC
            LIMIT 30
        """).fetchall()

        conn.close()

        if not rows:

            await query.edit_message_text(
                "📜 Nessun log.",
                reply_markup=admin_menu()
            )

            return

        text = "📜 <b>Ultime attività</b>\n\n"

        for row in rows:

            username = (
                f"@{row['username']}"
                if row["username"]
                else "senza username"
            )

            text += (
                f"👤 {username}\n"
                f"🆔 {row['telegram_user_id']}\n"
                f"⚙️ {row['action']}\n"
            )

            if row["order_code"]:
                text += f"📦 {row['order_code']}\n"

            if row["message"]:
                text += f"💬 {row['message']}\n"

            text += f"🕐 {row['created_at']}\n\n"

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=admin_menu()
        )

        return


# =========================================================
# STATUS BUTTONS
# =========================================================

async def status_code_received(update: Update, context: ContextTypes.DEFAULT_TYPE):

    code = update.message.text.strip().upper()

    order = get_order(code)

    if not order:

        await update.message.reply_text(
            "❌ Ordine non trovato."
        )

        return

    context.user_data["status_code"] = code

    keyboard = [
        [
            InlineKeyboardButton(
                "🟡 0% In attesa",
                callback_data="status_0"
            )
        ],
        [
            InlineKeyboardButton(
                "🟠 25% Preso in carico",
                callback_data="status_25"
            )
        ],
        [
            InlineKeyboardButton(
                "🟠 50% In elaborazione",
                callback_data="status_50"
            )
        ],
        [
            InlineKeyboardButton(
                "🟠 70% Quasi completato",
                callback_data="status_70"
            )
        ],
        [
            InlineKeyboardButton(
                "🟢 100% Completato",
                callback_data="status_100"
            )
        ]
    ]

    await update.message.reply_text(
        f"🔄 <b>Ordine {code}</b>\n\n"
        "Scegli il nuovo stato:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================================================
# STATUS CALLBACK
# =========================================================

async def status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    if not is_admin(update):
        await query.edit_message_text("❌ Non autorizzato.")
        return

    code = context.user_data.get("status_code")

    if not code:
        await query.edit_message_text(
            "❌ Codice ordine non trovato."
        )
        return

    progress = int(query.data.replace("status_", ""))

    status_names = {
        0: "In attesa",
        25: "Preso in carico",
        50: "In elaborazione",
        70: "Quasi completato",
        100: "Completato"
    }

    status = status_names[progress]

    conn = db_connect()

    # =====================================================
    # COMPLETATO
    # =====================================================

    if progress == 100:

        completed_time = now_str()

        conn.execute("""
            UPDATE orders
            SET status = ?,
                progress = ?,
                completed_at = ?,
                updated_at = ?
            WHERE code = ?
        """, (
            status,
            progress,
            completed_time,
            completed_time,
            code
        ))

    # =====================================================
    # QUALSIASI STATO PRIMA DEL COMPLETATO
    # =====================================================

    else:

        conn.execute("""
            UPDATE orders
            SET status = ?,
                progress = ?,
                completed_at = NULL,
                updated_at = ?
            WHERE code = ?
        """, (
            status,
            progress,
            now_str(),
            code
        ))

    conn.commit()

    order = conn.execute("""
        SELECT *
        FROM orders
        WHERE code = ?
    """, (code,)).fetchone()

    conn.close()

    log_activity(
        user_id=update.effective_user.id,
        username=update.effective_user.username,
        order_code=code,
        action="change_status",
        message=f"{status} ({progress}%)"
    )

    # =====================================================
    # NOTIFICA CLIENTE
    # =====================================================

    if order and order["telegram_user_id"]:

        try:

            notification = (
                "🔄 <b>Aggiornamento ordine</b>\n\n"
                + order_text(order)
            )

            if progress == 100:

                notification += (
                    "\n\n🕐 L'ordine rimarrà disponibile "
                    "per 30 minuti, dopodiché verrà rimosso."
                )

            await context.bot.send_message(
                chat_id=order["telegram_user_id"],
                text=notification,
                parse_mode="HTML"
            )

        except Exception as e:

            print(
                "Errore invio notifica cliente:",
                e
            )

    await query.edit_message_text(
        f"✅ <b>Stato aggiornato</b>\n\n"
        f"📦 Ordine: <b>{code}</b>\n"
        f"📍 Stato: <b>{status}</b>\n"
        f"📊 Avanzamento: <b>{progress}%</b>",
        parse_mode="HTML",
        reply_markup=admin_menu()
    )

    context.user_data.pop("status_code", None)


# =========================================================
# ADMIN TEXT
# =========================================================

async def admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):

    action = context.user_data.get("action")

    if not action:
        return

    text = update.message.text.strip()

    # =====================================================
    # CREA ORDINE
    # =====================================================

    if action == "create":

        code = text.upper()

        if get_order(code):

            await update.message.reply_text(
                "❌ Esiste già un ordine con questo codice."
            )

            return

        conn = db_connect()

        conn.execute("""
            INSERT INTO orders
            (
                code,
                status,
                progress,
                eta_minutes,
                telegram_user_id,
                created_at,
                updated_at,
                completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            code,
            "In attesa",
            0,
            None,
            None,
            now_str(),
            now_str(),
            None
        ))

        conn.commit()
        conn.close()

        log_activity(
            user_id=update.effective_user.id,
            username=update.effective_user.username,
            order_code=code,
            action="create_order",
            message=f"Creato ordine {code}"
        )

        link = f"https://t.me/{BOT_USERNAME}?start={code}"

        await update.message.reply_text(
            "✅ <b>Ordine creato!</b>\n\n"
            f"📦 Codice: <code>{code}</code>\n\n"
            f"🔗 Link cliente:\n{link}",
            parse_mode="HTML",
            reply_markup=admin_menu()
        )

        context.user_data.pop("action", None)

        return

    # =====================================================
    # CERCA
    # =====================================================

    if action == "search":

        code = text.upper()

        order = get_order(code)

        if not order:

            await update.message.reply_text(
                "❌ Ordine non trovato.",
                reply_markup=admin_menu()
            )

            context.user_data.pop("action", None)

            return

        await update.message.reply_text(
            order_text(order),
            parse_mode="HTML",
            reply_markup=admin_menu()
        )

        context.user_data.pop("action", None)

        return

    # =====================================================
    # CAMBIO STATO
    # =====================================================

    if action == "status_code":

        await status_code_received(
            update,
            context
        )

        context.user_data.pop("action", None)

        return

    # =====================================================
    # ETA
    # =====================================================

    if action == "eta":

        parts = text.split()

        if len(parts) != 2:

            await update.message.reply_text(
                "❌ Formato:\n\n"
                "<code>CODICE MINUTI</code>\n\n"
                "Esempio:\n"
                "<code>ABC123 25</code>",
                parse_mode="HTML"
            )

            return

        code = parts[0].upper()

        try:
            minutes = int(parts[1])
        except:

            await update.message.reply_text(
                "❌ I minuti devono essere un numero."
            )

            return

        if minutes < 0:

            await update.message.reply_text(
                "❌ I minuti non possono essere negativi."
            )

            return

        order = get_order(code)

        if not order:

            await update.message.reply_text(
                "❌ Ordine non trovato."
            )

            return

        eta = None if minutes == 0 else minutes

        conn = db_connect()

        conn.execute("""
            UPDATE orders
            SET eta_minutes = ?,
                updated_at = ?
            WHERE code = ?
        """, (
            eta,
            now_str(),
            code
        ))

        conn.commit()

        order = conn.execute("""
            SELECT *
            FROM orders
            WHERE code = ?
        """, (code,)).fetchone()

        conn.close()

        log_activity(
            user_id=update.effective_user.id,
            username=update.effective_user.username,
            order_code=code,
            action="change_eta",
            message=f"ETA: {minutes} minuti"
        )

        # Notifica cliente
        if order["telegram_user_id"]:

            try:

                await context.bot.send_message(
                    chat_id=order["telegram_user_id"],
                    text=(
                        "⏱️ <b>Aggiornamento tempo stimato</b>\n\n"
                        + order_text(order)
                    ),
                    parse_mode="HTML"
                )

            except Exception as e:

                print(
                    "Errore notifica ETA:",
                    e
                )

        await update.message.reply_text(
            "✅ <b>Tempo aggiornato!</b>\n\n"
            f"📦 Ordine: <b>{code}</b>\n"
            f"⏱️ Minuti: <b>{minutes}</b>",
            parse_mode="HTML",
            reply_markup=admin_menu()
        )

        context.user_data.pop("action", None)

        return


# =========================================================
# CLIENT CALLBACK
# =========================================================

async def client_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    user_id = update.effective_user.id
    username = update.effective_user.username or ""

    delete_expired_orders()

    conn = db_connect()

    order = conn.execute("""
        SELECT *
        FROM orders
        WHERE telegram_user_id = ?
        ORDER BY updated_at DESC
        LIMIT 1
    """, (user_id,)).fetchone()

    conn.close()

    if query.data in (
        "client_order",
        "client_refresh"
    ):

        log_activity(
            user_id=user_id,
            username=username,
            order_code=order["code"] if order else None,
            action=query.data,
            message="Cliente ha richiesto ordine"
        )

        if not order:

            await query.edit_message_text(
                "❌ Non hai nessun ordine associato."
            )

            return

        await query.edit_message_text(
            order_text(order),
            parse_mode="HTML",
            reply_markup=client_menu()
        )

        return

    if query.data == "client_info":

        log_activity(
            user_id=user_id,
            username=username,
            order_code=order["code"] if order else None,
            action="client_info",
            message="Cliente ha aperto informazioni"
        )

        await query.edit_message_text(
            "ℹ️ <b>Informazioni</b>\n\n"
            "Qui puoi controllare lo stato del tuo ordine "
            "e il tempo stimato quando disponibile.",
            parse_mode="HTML",
            reply_markup=client_menu()
        )


# =========================================================
# CLIENT MESSAGES / LOG
# =========================================================

async def client_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not user:
        return

    username = user.username or ""
    message = update.message.text or ""

    conn = db_connect()

    order = conn.execute("""
        SELECT code
        FROM orders
        WHERE telegram_user_id = ?
        ORDER BY updated_at DESC
        LIMIT 1
    """, (user.id,)).fetchone()

    conn.close()

    log_activity(
        user_id=user.id,
        username=username,
        order_code=order["code"] if order else None,
        action="client_message",
        message=message
    )

    await update.message.reply_text(
        "📦 Usa i pulsanti qui sotto per controllare il tuo ordine.",
        reply_markup=client_menu()
    )


# =========================================================
# TEXT ROUTER
# =========================================================

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if is_admin(update):

        await admin_text(
            update,
            context
        )

    else:

        await client_message(
            update,
            context
        )


# =========================================================
# WEBHOOK
# =========================================================

@app_web.post("/telegram")
async def telegram_webhook(request: Request):

    data = await request.json()

    update = Update.de_json(
        data,
        telegram_app.bot
    )

    await telegram_app.process_update(update)

    return {"ok": True}


@app_web.get("/")
async def home():

    return {
        "status": "online",
        "bot": BOT_USERNAME
    }


# =========================================================
# STARTUP
# =========================================================

async def startup():

    await telegram_app.initialize()
    await telegram_app.start()

    render_url = os.environ.get("RENDER_EXTERNAL_URL")

    if render_url:

        webhook_url = f"{render_url}/telegram"

        await telegram_app.bot.set_webhook(
            webhook_url
        )

        print(
            "Webhook impostato:",
            webhook_url
        )

    asyncio.create_task(
        cleanup_loop()
    )

    print("Bot avviato.")


asyncio.get_event_loop().run_until_complete(
    startup()
)


# =========================================================
# HANDLERS
# =========================================================

telegram_app.add_handler(
    CommandHandler(
        "start",
        start
    )
)

telegram_app.add_handler(
    CommandHandler(
        "admin",
        admin_command
    )
)

telegram_app.add_handler(
    CallbackQueryHandler(
        admin_callback,
        pattern=r"^admin_"
    )
)

telegram_app.add_handler(
    CallbackQueryHandler(
        status_callback,
        pattern=r"^status_"
    )
)

telegram_app.add_handler(
    CallbackQueryHandler(
        client_callback,
        pattern=r"^client_"
    )
)

telegram_app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        text_router
    )
)
