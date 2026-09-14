import os
import sqlite3

from fastapi import FastAPI, Request

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)


# ============================================================
# CONFIGURAZIONE
# ============================================================

TOKEN = os.environ["BOT_TOKEN"]

ADMIN_USERNAME = os.environ.get(
    "ADMIN_USERNAME",
    "weevoo26"
).lstrip("@")

BOT_USERNAME = "Oorderstatebot"

# Se /data esiste, usalo per un eventuale disco persistente Render.
# Altrimenti usa orders.db nella cartella del progetto.
if os.path.isdir("/data"):
    DB = "/data/orders.db"
else:
    DB = "orders.db"


app_web = FastAPI()

tg_app = (
    Application
    .builder()
    .token(TOKEN)
    .build()
)


# ============================================================
# DATABASE
# ============================================================

def db():
    con = sqlite3.connect(DB)

    con.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            code TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            progress INTEGER NOT NULL DEFAULT 0,
            telegram_user_id INTEGER
        )
    """)

    # Tabella per ricordare l'ID Telegram dell'admin
    con.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # Compatibilità con il vecchio database
    columns = [
        row[1]
        for row in con.execute(
            "PRAGMA table_info(orders)"
        ).fetchall()
    ]

    if "telegram_user_id" not in columns:
        con.execute(
            "ALTER TABLE orders "
            "ADD COLUMN telegram_user_id INTEGER"
        )

    con.commit()

    return con


# ============================================================
# ADMIN
# ============================================================

def is_admin(update: Update):
    user = update.effective_user

    if not user:
        return False

    if not user.username:
        return False

    return (
        user.username.lower()
        == ADMIN_USERNAME.lower()
    )


def save_admin_id(user_id):
    con = db()

    con.execute(
        """
        INSERT INTO settings(key, value)
        VALUES('admin_user_id', ?)
        ON CONFLICT(key)
        DO UPDATE SET value = excluded.value
        """,
        (str(user_id),)
    )

    con.commit()
    con.close()


def get_admin_id():
    con = db()

    row = con.execute(
        """
        SELECT value
        FROM settings
        WHERE key = 'admin_user_id'
        """
    ).fetchone()

    con.close()

    if not row:
        return None

    try:
        return int(row[0])
    except ValueError:
        return None


# ============================================================
# FORMAT STATO ORDINE
# ============================================================

def text_for(code, status, progress):

    icons = {
        "In attesa": "🟡",
        "In elaborazione": "🔵",
        "Completato": "🟢",
        "Annullato": "🔴",
    }

    filled = max(
        0,
        min(
            10,
            round(progress / 10)
        )
    )

    bar = (
        "█" * filled
        + "░" * (10 - filled)
    )

    return (
        f"📦 *Ordine #{code}*\n\n"
        f"{icons.get(status, '⚪')} "
        f"*Stato:* {status}\n\n"
        f"`{bar}` *{progress}%*"
    )


# ============================================================
# STATI DISPONIBILI
# ============================================================

STATUS_MAPPING = {
    "attesa": ("In attesa", 0),
    "lavorazione": ("In elaborazione", 50),
    "completato": ("Completato", 100),
    "annullato": ("Annullato", 0),
}


# ============================================================
# LOG ATTIVITÀ CLIENTE
# ============================================================

async def notify_admin_activity(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user:
        return

    # Non registrare le attività dell'admin
    if is_admin(update):
        return

    admin_id = get_admin_id()

    if not admin_id:
        return

    username = (
        f"@{user.username}"
        if user.username
        else user.full_name
    )

    con = db()

    row = con.execute(
        """
        SELECT code
        FROM orders
        WHERE telegram_user_id = ?
        ORDER BY rowid DESC
        LIMIT 1
        """,
        (user.id,)
    ).fetchone()

    con.close()

    order_code = (
        row[0]
        if row
        else "Nessun ordine associato"
    )

    # ========================================================
    # Determina cosa ha fatto il cliente
    # ========================================================

    if update.message:

        if update.message.text:

            message_text = update.message.text

        else:

            message_text = "[Messaggio non testuale]"

    elif update.callback_query:

        message_text = (
            f"[Pulsante: "
            f"{update.callback_query.data}]"
        )

    else:

        message_text = "[Interazione]"

    # Limita messaggi enormi
    if len(message_text) > 1500:
        message_text = (
            message_text[:1500]
            + "..."
        )

    # Escape minimo per Markdown
    safe_text = message_text.replace(
        "`",
        "'"
    )

    admin_text = (
        "🔔 *ATTIVITÀ CLIENTE*\n\n"
        f"👤 {username}\n"
        f"🆔 ID: `{user.id}`\n"
        f"📦 Ordine: `{order_code}`\n\n"
        f"💬 Messaggio:\n"
        f"`{safe_text}`"
    )

    try:

        await context.bot.send_message(
            chat_id=admin_id,
            text=admin_text,
            parse_mode="Markdown"
        )

    except Exception:
        pass


# ============================================================
# /START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user:
        return

    # Se sei admin, ricordiamo il tuo Telegram ID
    if is_admin(update):
        save_admin_id(user.id)

    con = db()

    # ========================================================
    # /start CODICE
    # ========================================================

    if context.args:

        code = context.args[0].strip()

        row = con.execute(
            """
            SELECT code, status, progress
            FROM orders
            WHERE code = ?
            """,
            (code,)
        ).fetchone()

        if not row:

            con.close()

            await update.message.reply_text(
                "❌ Ordine non trovato."
            )

            return

        # Associa cliente all'ordine
        con.execute(
            """
            UPDATE orders
            SET telegram_user_id = ?
            WHERE code = ?
            """,
            (
                user.id,
                code
            )
        )

        con.commit()
        con.close()

        order_code, status, progress = row

        await update.message.reply_text(
            text_for(
                order_code,
                status,
                progress
            )
            + "\n\n"
            "✅ Ordine associato al tuo account.\n"
            "La prossima volta ti basterà premere "
            "/start.",
            parse_mode="Markdown"
        )

        return

    # ========================================================
    # /start SENZA CODICE
    # ========================================================

    row = con.execute(
        """
        SELECT code, status, progress
        FROM orders
        WHERE telegram_user_id = ?
        ORDER BY rowid DESC
        LIMIT 1
        """,
        (user.id,)
    ).fetchone()

    con.close()

    if row:

        await update.message.reply_text(
            text_for(*row),
            parse_mode="Markdown"
        )

        return

    # Nessun ordine associato

    await update.message.reply_text(
        "👋 *Benvenuto!*\n\n"
        "📦 Apri il link del tuo ordine e premi "
        "*Start*.\n\n"
        "Dopo la prima apertura, il bot ricorderà "
        "automaticamente il tuo ordine.",
        parse_mode="Markdown"
    )


# ============================================================
# MENU ADMIN
# ============================================================

def admin_keyboard():

    keyboard = [
        [
            InlineKeyboardButton(
                "➕ Crea ordine",
                callback_data="admin_create"
            )
        ],
        [
            InlineKeyboardButton(
                "📦 Visualizza ordini",
                callback_data="admin_orders"
            )
        ],
        [
            InlineKeyboardButton(
                "🗑️ Elimina ordini",
                callback_data="admin_delete"
            )
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


async def admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(update):

        await update.message.reply_text(
            "⛔ Accesso non autorizzato."
        )

        return

    save_admin_id(
        update.effective_user.id
    )

    await update.message.reply_text(
        "👑 *PANNELLO ADMIN*\n\n"
        "Scegli un'operazione:",
        parse_mode="Markdown",
        reply_markup=admin_keyboard()
    )


# ============================================================
# /CREA
# ============================================================

async def crea(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(update):
        return

    save_admin_id(
        update.effective_user.id
    )

    if not context.args:

        await update.message.reply_text(
            "Usa:\n"
            "/crea CODICE"
        )

        return

    code = context.args[0].strip()

    con = db()

    try:

        con.execute(
            """
            INSERT INTO orders(
                code,
                status,
                progress,
                telegram_user_id
            )
            VALUES (?, ?, ?, NULL)
            """,
            (
                code,
                "In attesa",
                0
            )
        )

        con.commit()

    except sqlite3.IntegrityError:

        con.close()

        await update.message.reply_text(
            "⚠️ Questo codice esiste già."
        )

        return

    con.close()

    link = (
        f"https://t.me/"
        f"{BOT_USERNAME}"
        f"?start={code}"
    )

    await update.message.reply_text(
        "✅ *ORDINE CREATO*\n\n"
        f"📦 Codice: `{code}`\n"
        f"🟡 Stato: In attesa\n\n"
        "🔗 *Link cliente:*\n"
        f"{link}",
        parse_mode="Markdown"
    )


# ============================================================
# /STATO
# ============================================================

async def stato(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(update):
        return

    save_admin_id(
        update.effective_user.id
    )

    if len(context.args) < 2:

        await update.message.reply_text(
            "Uso:\n"
            "/stato CODICE attesa\n"
            "/stato CODICE lavorazione\n"
            "/stato CODICE completato\n"
            "/stato CODICE annullato"
        )

        return

    code = context.args[0].strip()
    state = context.args[1].lower().strip()

    if state not in STATUS_MAPPING:

        await update.message.reply_text(
            "❌ Stato non valido."
        )

        return

    status, progress = STATUS_MAPPING[state]

    con = db()

    row = con.execute(
        """
        SELECT
            code,
            telegram_user_id
        FROM orders
        WHERE code = ?
        """,
        (code,)
    ).fetchone()

    if not row:

        con.close()

        await update.message.reply_text(
            "❌ Ordine non trovato."
        )

        return

    telegram_user_id = row[1]

    # AGGIORNA ORDINE
    con.execute(
        """
        UPDATE orders
        SET
            status = ?,
            progress = ?
        WHERE code = ?
        """,
        (
            status,
            progress,
            code
        )
    )

    con.commit()
    con.close()

    # Conferma admin
    await update.message.reply_text(
        "✅ *ORDINE AGGIORNATO*\n\n"
        + text_for(
            code,
            status,
            progress
        ),
        parse_mode="Markdown"
    )

    # ========================================================
    # NOTIFICA CLIENTE
    # ========================================================

    if telegram_user_id:

        try:

            await context.bot.send_message(
                chat_id=telegram_user_id,
                text=(
                    "🔔 *Aggiornamento ordine*\n\n"
                    + text_for(
                        code,
                        status,
                        progress
                    )
                ),
                parse_mode="Markdown"
            )

        except Exception:
            pass


# ============================================================
# VISUALIZZA ORDINI
# ============================================================

async def show_orders(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if not query:
        return

    await query.answer()

    if not is_admin(update):
        return

    con = db()

    rows = con.execute(
        """
        SELECT code, status, progress
        FROM orders
        ORDER BY rowid DESC
        """
    ).fetchall()

    con.close()

    if not rows:

        await query.edit_message_text(
            "📦 *ORDINI*\n\n"
            "Non ci sono ordini.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Indietro",
                        callback_data="admin_back"
                    )
                ]
            ])
        )

        return

    text = "📦 *ORDINI ESISTENTI*\n\n"

    for code, status, progress in rows:

        text += (
            f"• `{code}` — "
            f"{status} — "
            f"{progress}%\n"
        )

    keyboard = [
        [
            InlineKeyboardButton(
                "⬅️ Indietro",
                callback_data="admin_back"
            )
        ]
    ]

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        )
    )


# ============================================================
# MENU ELIMINA ORDINI
# ============================================================

async def delete_orders_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if not query:
        return

    await query.answer()

    if not is_admin(update):
        return

    con = db()

    rows = con.execute(
        """
        SELECT code, status
        FROM orders
        ORDER BY rowid DESC
        """
    ).fetchall()

    con.close()

    if not rows:

        await query.edit_message_text(
            "🗑️ *ELIMINA ORDINI*\n\n"
            "Non ci sono ordini da eliminare.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Indietro",
                        callback_data="admin_back"
                    )
                ]
            ])
        )

        return

    keyboard = []

    for code, status in rows:

        keyboard.append([
            InlineKeyboardButton(
                f"🗑️ {code} — {status}",
                callback_data=f"delete:{code}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "⬅️ Indietro",
            callback_data="admin_back"
        )
    ])

    await query.edit_message_text(
        "🗑️ *ELIMINA ORDINI*\n\n"
        "⚠️ Scegli l'ordine che vuoi eliminare.\n"
        "Gli ordini NON vengono eliminati automaticamente.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        )
    )


# ============================================================
# CONFERMA ELIMINAZIONE
# ============================================================

async def confirm_delete(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if not query:
        return

    await query.answer()

    if not is_admin(update):
        return

    code = query.data.split(
        ":",
        1
    )[1]

    keyboard = [
        [
            InlineKeyboardButton(
                "✅ Sì, elimina",
                callback_data=f"confirm_delete:{code}"
            ),
            InlineKeyboardButton(
                "❌ Annulla",
                callback_data="admin_delete"
            )
        ]
    ]

    await query.edit_message_text(
        "⚠️ *CONFERMA ELIMINAZIONE*\n\n"
        f"Vuoi eliminare definitivamente "
        f"l'ordine `{code}`?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        )
    )


# ============================================================
# ELIMINA ORDINE
# ============================================================

async def delete_order(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if not query:
        return

    await query.answer()

    if not is_admin(update):
        return

    code = query.data.split(
        ":",
        1
    )[1]

    con = db()

    cur = con.execute(
        """
        DELETE FROM orders
        WHERE code = ?
        """,
        (code,)
    )

    con.commit()
    con.close()

    if cur.rowcount == 0:

        await query.edit_message_text(
            "❌ Ordine non trovato.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Indietro",
                        callback_data="admin_delete"
                    )
                ]
            ])
        )

        return

    await query.edit_message_text(
        "🗑️ *ORDINE ELIMINATO*\n\n"
        f"Ordine `{code}` eliminato correttamente.\n\n"
        "Questo è l'unico modo con cui il bot "
        "elimina un ordine.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🗑️ Elimina altri",
                    callback_data="admin_delete"
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Menu",
                    callback_data="admin_back"
                )
            ]
        ])
    )


# ============================================================
# TORNA AL MENU ADMIN
# ============================================================

async def admin_back(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if not query:
        return

    await query.answer()

    if not is_admin(update):
        return

    await query.edit_message_text(
        "👑 *PANNELLO ADMIN*\n\n"
        "Scegli un'operazione:",
        parse_mode="Markdown",
        reply_markup=admin_keyboard()
    )


# ============================================================
# BOTTONI ADMIN
# ============================================================

async def admin_button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if not query:
        return

    if not is_admin(update):
        await query.answer(
            "Non autorizzato.",
            show_alert=True
        )
        return

    data = query.data

    if data == "admin_orders":

        await show_orders(
            update,
            context
        )

    elif data == "admin_delete":

        await delete_orders_menu(
            update,
            context
        )

    elif data == "admin_create":

        await query.answer()

        await query.edit_message_text(
            "➕ *CREA ORDINE*\n\n"
            "Invia ora:\n"
            "`/crea CODICE`\n\n"
            "Esempio:\n"
            "`/crea TEST123`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Indietro",
                        callback_data="admin_back"
                    )
                ]
            ])
        )

    elif data == "admin_back":

        await admin_back(
            update,
            context
        )

    elif data.startswith("delete:"):

        await confirm_delete(
            update,
            context
        )

    elif data.startswith("confirm_delete:"):

        await delete_order(
            update,
            context
        )


# ============================================================
# MESSAGGI CLIENTI
# ============================================================

async def client_message_logger(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    # Registra soltanto messaggi normali dei clienti
    await notify_admin_activity(
        update,
        context
    )


# ============================================================
# HANDLER TELEGRAM
# ============================================================

tg_app.add_handler(
    CommandHandler(
        "start",
        start
    ),
    group=0
)

tg_app.add_handler(
    CommandHandler(
        "admin",
        admin
    ),
    group=0
)

tg_app.add_handler(
    CommandHandler(
        "crea",
        crea
    ),
    group=0
)

tg_app.add_handler(
    CommandHandler(
        "stato",
        stato
    ),
    group=0
)

tg_app.add_handler(
    CallbackQueryHandler(
        admin_button_handler
    ),
    group=0
)

# Messaggi normali dei clienti
tg_app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        client_message_logger
    ),
    group=1
)


# ============================================================
# HOME
# ============================================================

@app_web.get("/")
async def home():

    return {
        "status": "ok",
        "bot": "telegram-order-bot"
    }


# ============================================================
# WEBHOOK TELEGRAM
# ============================================================

@app_web.post("/telegram")
async def telegram_webhook(
    request: Request
):

    data = await request.json()

    update = Update.de_json(
        data,
        tg_app.bot
    )

    await tg_app.process_update(
        update
    )

    return {
        "ok": True
    }


# ============================================================
# STARTUP
# ============================================================

@app_web.on_event("startup")
async def startup():

    db().close()

    await tg_app.initialize()

    await tg_app.start()

    external = os.environ.get(
        "RENDER_EXTERNAL_URL"
    )

    if external:

        await tg_app.bot.set_webhook(
            external.rstrip("/")
            + "/telegram"
        )


# ============================================================
# SHUTDOWN
# ============================================================

@app_web.on_event("shutdown")
async def shutdown():

    await tg_app.stop()

    await tg_app.shutdown()
