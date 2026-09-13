import os
import sqlite3
from datetime import datetime

from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_USERNAME = "weevoo26"
BOT_USERNAME = "Oorderstatebot"

DB = "orders.db"


# ============================================================
# DATABASE
# ============================================================

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    con.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            code TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'In attesa',
            progress INTEGER NOT NULL DEFAULT 0,
            eta_minutes INTEGER,
            telegram_user_id INTEGER,
            created_at TEXT,
            updated_at TEXT
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_user_id INTEGER,
            username TEXT,
            order_code TEXT,
            action TEXT,
            message TEXT,
            created_at TEXT
        )
    """)

    con.commit()
    return con


def now():
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


# ============================================================
# ADMIN
# ============================================================

def is_admin(update):
    user = update.effective_user

    return bool(
        user
        and user.username
        and user.username.lower() == ADMIN_USERNAME.lower()
    )


def save_admin_id(user_id):
    con = db()

    con.execute("""
        INSERT OR REPLACE INTO settings(key, value)
        VALUES (?, ?)
    """, ("admin_id", str(user_id)))

    con.commit()
    con.close()


def get_admin_id():
    con = db()

    row = con.execute("""
        SELECT value
        FROM settings
        WHERE key = 'admin_id'
    """).fetchone()

    con.close()

    if not row:
        return None

    try:
        return int(row["value"])
    except ValueError:
        return None


def user_name(user):
    if user.username:
        return f"@{user.username}"

    return user.full_name or "Utente"


# ============================================================
# ORDINI
# ============================================================

STATUSES = {
    "0": ("In attesa", 0),
    "25": ("Preso in carico", 25),
    "50": ("In elaborazione", 50),
    "70": ("Quasi completato", 70),
    "100": ("Completato", 100),
}


def get_order(code):
    con = db()

    row = con.execute("""
        SELECT *
        FROM orders
        WHERE code = ?
    """, (code,)).fetchone()

    con.close()

    return row


def get_user_order(user_id):
    con = db()

    row = con.execute("""
        SELECT *
        FROM orders
        WHERE telegram_user_id = ?
        ORDER BY rowid DESC
        LIMIT 1
    """, (user_id,)).fetchone()

    con.close()

    return row


def progress_bar(progress):
    filled = round(progress / 10)

    return (
        "▰" * filled +
        "░" * (10 - filled)
    )


def order_text(row):
    progress = row["progress"]

    if progress == 0:
        emoji = "🟡"
    elif progress < 70:
        emoji = "🔵"
    elif progress < 100:
        emoji = "🟠"
    else:
        emoji = "🟢"

    text = (
        f"{emoji} {row['status']}\n\n"
        f"{progress_bar(progress)} {progress}%"
    )

    if row["eta_minutes"] is not None:
        if row["eta_minutes"] == 0:
            text += "\n⏱️ Tempo stimato: imminente"
        else:
            text += (
                f"\n⏱️ Tempo stimato: "
                f"{row['eta_minutes']} minuti"
            )

    return text


# ============================================================
# LOG
# ============================================================

async def log_activity(update, context, action, message):
    user = update.effective_user

    if not user or is_admin(update):
        return

    con = db()

    row = con.execute("""
        SELECT code
        FROM orders
        WHERE telegram_user_id = ?
        ORDER BY rowid DESC
        LIMIT 1
    """, (user.id,)).fetchone()

    order_code = (
        row["code"]
        if row
        else "Nessun ordine"
    )

    con.execute("""
        INSERT INTO activity_logs(
            telegram_user_id,
            username,
            order_code,
            action,
            message,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        user.id,
        user_name(user),
        order_code,
        action,
        message[:1000],
        now()
    ))

    con.commit()
    con.close()

    admin_id = get_admin_id()

    if not admin_id:
        return

    text = (
        "🔔 NUOVA ATTIVITÀ\n\n"
        f"👤 {user_name(user)}\n"
        f"🆔 ID: {user.id}\n"
        f"📦 Ordine: {order_code}\n"
        f"🕐 {now()}\n\n"
        f"⚡ {action}\n"
        f"💬 {message[:1000]}"
    )

    try:
        await context.bot.send_message(
            chat_id=admin_id,
            text=text
        )
    except Exception as e:
        print("Errore log:", e)


# ============================================================
# MENU CLIENTE
# ============================================================

def client_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📦 Il mio ordine",
                callback_data="client_order"
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 Aggiorna",
                callback_data="client_order"
            ),
            InlineKeyboardButton(
                "ℹ️ Informazioni",
                callback_data="client_info"
            )
        ]
    ])


# ============================================================
# MENU ADMIN
# ============================================================

def admin_menu():
    return InlineKeyboardMarkup([
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
            )
        ],
        [
            InlineKeyboardButton(
                "📊 Statistiche",
                callback_data="admin_stats"
            ),
            InlineKeyboardButton(
                "👥 Clienti",
                callback_data="admin_clients"
            )
        ],
        [
            InlineKeyboardButton(
                "📜 Log attività",
                callback_data="admin_logs"
            )
        ]
    ])


# ============================================================
# /START
# ============================================================

async def start(update, context):
    user = update.effective_user

    if not user:
        return

    if is_admin(update):
        save_admin_id(user.id)

        await update.message.reply_text(
            "🛠️ PANNELLO ADMIN\n\n"
            "Usa i pulsanti qui sotto.",
            reply_markup=admin_menu()
        )
        return

    start_code = (
        context.args[0].strip()
        if context.args
        else "Nessun codice"
    )

    await log_activity(
        update,
        context,
        "START",
        f"/start {start_code}"
    )

    if context.args:
        code = context.args[0].strip()
        row = get_order(code)

        if not row:
            await update.message.reply_text(
                "❌ Ordine non trovato."
            )
            return

        con = db()

        con.execute("""
            UPDATE orders
            SET telegram_user_id = ?,
                updated_at = ?
            WHERE code = ?
        """, (
            user.id,
            now(),
            code
        ))

        con.commit()
        con.close()

        updated = get_order(code)

        await update.message.reply_text(
            f"📦 Ordine #{code}\n\n"
            + order_text(updated),
            reply_markup=client_menu()
        )

        return

    row = get_user_order(user.id)

    if row:
        await update.message.reply_text(
            f"📦 Ordine #{row['code']}\n\n"
            + order_text(row),
            reply_markup=client_menu()
        )
        return

    await update.message.reply_text(
        "👋 Benvenuto!\n\n"
        "Apri il link del tuo ordine e premi Start "
        "per associarlo automaticamente.",
        reply_markup=client_menu()
    )


# ============================================================
# /ADMIN
# ============================================================

async def admin_command(update, context):
    if not is_admin(update):
        await update.message.reply_text(
            "❌ Non autorizzato."
        )
        return

    save_admin_id(update.effective_user.id)

    await update.message.reply_text(
        "🛠️ PANNELLO ADMIN\n\n"
        "Scegli un'operazione:",
        reply_markup=admin_menu()
    )


# ============================================================
# ADMIN CALLBACK
# ============================================================

async def admin_callback(update, context):
    query = update.callback_query
    await query.answer()

    if not is_admin(update):
        await query.edit_message_text(
            "❌ Non autorizzato."
        )
        return

    save_admin_id(update.effective_user.id)

    action = query.data

    if action == "admin_create":
        context.user_data["action"] = "create"

        await query.edit_message_text(
            "➕ CREA ORDINE\n\n"
            "Scrivi il codice dell'ordine."
        )
        return

    if action == "admin_list":
        con = db()

        rows = con.execute("""
            SELECT code, status, progress, eta_minutes
            FROM orders
            ORDER BY rowid DESC
            LIMIT 30
        """).fetchall()

        con.close()

        if not rows:
            text = "📋 Nessun ordine presente."
        else:
            text = "📋 ORDINI\n\n"

            for row in rows:
                eta = (
                    f"{row['eta_minutes']} min"
                    if row["eta_minutes"] is not None
                    else "-"
                )

                text += (
                    f"📦 {row['code']}\n"
                    f"   {row['status']} — "
                    f"{row['progress']}%\n"
                    f"   ⏱️ {eta}\n\n"
                )

        await query.edit_message_text(
            text,
            reply_markup=admin_menu()
        )
        return

    if action == "admin_search":
        context.user_data["action"] = "search"

        await query.edit_message_text(
            "🔎 CERCA ORDINE\n\n"
            "Scrivi il codice."
        )
        return

    if action == "admin_status":
        context.user_data["action"] = "status_code"

        await query.edit_message_text(
            "🔄 CAMBIA STATO\n\n"
            "Scrivi il codice dell'ordine."
        )
        return

    if action == "admin_eta":
        context.user_data["action"] = "eta"

        await query.edit_message_text(
            "⏱️ IMPOSTA MINUTI\n\n"
            "Scrivi:\n"
            "CODICE MINUTI\n\n"
            "Esempio:\n"
            "ABC123 25"
        )
        return

    if action == "admin_stats":
        con = db()

        total = con.execute(
            "SELECT COUNT(*) FROM orders"
        ).fetchone()[0]

        completed = con.execute("""
            SELECT COUNT(*)
            FROM orders
            WHERE progress = 100
        """).fetchone()[0]

        active = con.execute("""
            SELECT COUNT(*)
            FROM orders
            WHERE progress < 100
        """).fetchone()[0]

        clients = con.execute("""
            SELECT COUNT(DISTINCT telegram_user_id)
            FROM orders
            WHERE telegram_user_id IS NOT NULL
        """).fetchone()[0]

        con.close()

        await query.edit_message_text(
            "📊 STATISTICHE\n\n"
            f"📦 Ordini totali: {total}\n"
            f"🔄 Attivi: {active}\n"
            f"🟢 Completati: {completed}\n"
            f"👥 Clienti: {clients}",
            reply_markup=admin_menu()
        )
        return

    if action == "admin_clients":
        con = db()

        rows = con.execute("""
            SELECT DISTINCT telegram_user_id
            FROM orders
            WHERE telegram_user_id IS NOT NULL
            ORDER BY telegram_user_id
        """).fetchall()

        con.close()

        if not rows:
            text = "👥 Nessun cliente associato."
        else:
            text = "👥 CLIENTI\n\n"

            for row in rows:
                text += f"🆔 {row['telegram_user_id']}\n"

        await query.edit_message_text(
            text[:4000],
            reply_markup=admin_menu()
        )
        return

    if action == "admin_logs":
        con = db()

        rows = con.execute("""
            SELECT username,
                   order_code,
                   action,
                   message,
                   created_at
            FROM activity_logs
            ORDER BY id DESC
            LIMIT 20
        """).fetchall()

        con.close()

        if not rows:
            text = "📜 Nessun log."
        else:
            text = "📜 ULTIMI LOG\n\n"

            for row in rows:
                text += (
                    f"🕐 {row['created_at']}\n"
                    f"👤 {row['username']}\n"
                    f"📦 {row['order_code']}\n"
                    f"⚡ {row['action']}\n"
                    f"💬 {row['message']}\n\n"
                )

        await query.edit_message_text(
            text[:4000],
            reply_markup=admin_menu()
        )


# ============================================================
# CAMBIO STATO
# ============================================================

async def status_code_received(update, context):
    code = update.message.text.strip()

    row = get_order(code)

    if not row:
        await update.message.reply_text(
            "❌ Ordine non trovato.",
            reply_markup=admin_menu()
        )
        context.user_data.clear()
        return

    context.user_data["order_code"] = code
    context.user_data["action"] = "status_value"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🟡 0%",
                callback_data="status_0"
            ),
            InlineKeyboardButton(
                "🔵 25%",
                callback_data="status_25"
            )
        ],
        [
            InlineKeyboardButton(
                "🔵 50%",
                callback_data="status_50"
            ),
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
    ])

    await update.message.reply_text(
        f"📦 Ordine #{code}\n\n"
        "Scegli il nuovo stato:",
        reply_markup=keyboard
    )


async def status_callback(update, context):
    query = update.callback_query
    await query.answer()

    if not is_admin(update):
        return

    code = context.user_data.get("order_code")

    if not code:
        await query.edit_message_text(
            "❌ Sessione scaduta.",
            reply_markup=admin_menu()
        )
        return

    value = query.data.replace("status_", "")

    status, progress = STATUSES[value]

    row = get_order(code)

    if not row:
        await query.edit_message_text(
            "❌ Ordine non trovato.",
            reply_markup=admin_menu()
        )
        context.user_data.clear()
        return

    con = db()

    con.execute("""
        UPDATE orders
        SET status = ?,
            progress = ?,
            updated_at = ?
        WHERE code = ?
    """, (
        status,
        progress,
        now(),
        code
    ))

    con.commit()
    con.close()

    updated = get_order(code)

    if row["telegram_user_id"]:
        try:
            await context.bot.send_message(
                chat_id=row["telegram_user_id"],
                text=(
                    f"🔔 Aggiornamento ordine #{code}\n\n"
                    + order_text(updated)
                )
            )
        except Exception as e:
            print("Errore notifica:", e)

    await query.edit_message_text(
        f"✅ ORDINE AGGIORNATO\n\n"
        f"📦 #{code}\n\n"
        + order_text(updated),
        reply_markup=admin_menu()
    )

    context.user_data.clear()


# ============================================================
# ADMIN TEXT
# ============================================================

async def admin_text(update, context):
    if not is_admin(update):
        return

    action = context.user_data.get("action")

    if not action:
        return

    text = update.message.text.strip()

    # --------------------------------------------------------
    # CREA
    # --------------------------------------------------------

    if action == "create":
        code = text

        if get_order(code):
            await update.message.reply_text(
                "❌ Questo codice esiste già.",
                reply_markup=admin_menu()
            )
            context.user_data.clear()
            return

        con = db()

        con.execute("""
            INSERT INTO orders(
                code,
                status,
                progress,
                eta_minutes,
                telegram_user_id,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            code,
            "In attesa",
            0,
            None,
            None,
            now(),
            now()
        ))

        con.commit()
        con.close()

        link = (
            f"https://t.me/"
            f"{BOT_USERNAME}"
            f"?start={code}"
        )

        await update.message.reply_text(
            "✅ ORDINE CREATO\n\n"
            f"📦 Codice: {code}\n"
            f"🔗 Link:\n{link}",
            reply_markup=admin_menu()
        )

        context.user_data.clear()
        return

    # --------------------------------------------------------
    # CERCA
    # --------------------------------------------------------

    if action == "search":
        row = get_order(text)

        if not row:
            await update.message.reply_text(
                "❌ Ordine non trovato.",
                reply_markup=admin_menu()
            )
        else:
            eta = (
                f"{row['eta_minutes']} minuti"
                if row["eta_minutes"] is not None
                else "Non impostato"
            )

            await update.message.reply_text(
                f"📦 ORDINE #{row['code']}\n\n"
                f"📌 Stato: {row['status']}\n"
                f"📊 Progresso: {row['progress']}%\n"
                f"⏱️ Tempo: {eta}\n"
                f"👤 Telegram ID: "
                f"{row['telegram_user_id'] or 'Non associato'}",
                reply_markup=admin_menu()
            )

        context.user_data.clear()
        return

    # --------------------------------------------------------
    # STATO
    # --------------------------------------------------------

    if action == "status_code":
        await status_code_received(update, context)
        return

    # --------------------------------------------------------
    # MINUTI
    # --------------------------------------------------------

    if action == "eta":
        parts = text.split()

        if len(parts) != 2:
            await update.message.reply_text(
                "❌ Formato:\nCODICE MINUTI\n\n"
                "Esempio:\nABC123 25",
                reply_markup=admin_menu()
            )
            return

        code = parts[0]

        try:
            minutes = int(parts[1])
        except ValueError:
            await update.message.reply_text(
                "❌ I minuti devono essere un numero.",
                reply_markup=admin_menu()
            )
            return

        if minutes < 0:
            await update.message.reply_text(
                "❌ Il valore non può essere negativo.",
                reply_markup=admin_menu()
            )
            return

        row = get_order(code)

        if not row:
            await update.message.reply_text(
                "❌ Ordine non trovato.",
                reply_markup=admin_menu()
            )
            context.user_data.clear()
            return

        eta = None if minutes == 0 else minutes

        con = db()

        con.execute("""
            UPDATE orders
            SET eta_minutes = ?,
                updated_at = ?
            WHERE code = ?
        """, (
            eta,
            now(),
            code
        ))

        con.commit()
        con.close()

        updated = get_order(code)

        if row["telegram_user_id"]:
            try:
                await context.bot.send_message(
                    chat_id=row["telegram_user_id"],
                    text=(
                        f"🔔 Aggiornamento ordine #{code}\n\n"
                        + order_text(updated)
                    )
                )
            except Exception as e:
                print("Errore notifica:", e)

        await update.message.reply_text(
            f"✅ Tempo aggiornato.\n\n"
            f"📦 #{code}\n"
            f"⏱️ {minutes} minuti",
            reply_markup=admin_menu()
        )

        context.user_data.clear()


# ============================================================
# CLIENT CALLBACK
# ============================================================

async def client_callback(update, context):
    query = update.callback_query
    await query.answer()

    user = update.effective_user

    if query.data == "client_info":
        await query.edit_message_text(
            "ℹ️ INFORMAZIONI\n\n"
            "Qui puoi controllare lo stato "
            "del tuo ordine associato.",
            reply_markup=client_menu()
        )
        return

    if query.data == "client_order":
        row = get_user_order(user.id)

        if not row:
            await query.edit_message_text(
                "📦 Nessun ordine associato.",
                reply_markup=client_menu()
            )
            return

        await query.edit_message_text(
            f"📦 Ordine #{row['code']}\n\n"
            + order_text(row),
            reply_markup=client_menu()
        )


# ============================================================
# MESSAGGI CLIENTI
# ============================================================

async def client_message(update, context):
    if is_admin(update):
        return

    if not update.message:
        return

    text = update.message.text or "[Messaggio non testuale]"

    await log_activity(
        update,
        context,
        "MESSAGGIO",
        text
    )

    await update.message.reply_text(
        "📩 Messaggio ricevuto.",
        reply_markup=client_menu()
    )


# ============================================================
# TELEGRAM
# ============================================================

application = (
    Application.builder()
    .token(TOKEN)
    .build()
)

application.add_handler(
    CommandHandler("start", start)
)

application.add_handler(
    CommandHandler("admin", admin_command)
)

application.add_handler(
    CallbackQueryHandler(
        admin_callback,
        pattern=r"^admin_"
    )
)

application.add_handler(
    CallbackQueryHandler(
        status_callback,
        pattern=r"^status_"
    )
)

application.add_handler(
    CallbackQueryHandler(
        client_callback,
        pattern=r"^client_"
    )
)

# IMPORTANTE:
# un unico handler per i messaggi di testo.
application.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        lambda update, context: (
            admin_text(update, context)
            if is_admin(update)
            else client_message(update, context)
        )
    )
)


# ============================================================
# FASTAPI / WEBHOOK
# ============================================================

app_web = FastAPI()


@app_web.post("/telegram")
async def telegram_webhook(request: Request):
    data = await request.json()

    update = Update.de_json(
        data,
        application.bot
    )

    await application.process_update(update)

    return {"ok": True}


# ============================================================
# STARTUP
# ============================================================

@app_web.on_event("startup")
async def startup():
    db().close()

    await application.initialize()
    await application.start()

    external_url = os.environ.get(
        "RENDER_EXTERNAL_URL"
    )

    if external_url:
        webhook_url = (
            external_url.rstrip("/")
            + "/telegram"
        )

        await application.bot.set_webhook(
            webhook_url
        )

        print(
            "Webhook impostato:",
            webhook_url
        )


# ============================================================
# SHUTDOWN
# ============================================================

@app_web.on_event("shutdown")
async def shutdown():
    await application.stop()
    await application.shutdown()
