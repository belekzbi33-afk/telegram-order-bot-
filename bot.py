import os
import sqlite3

from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# =========================
# CONFIG
# =========================

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "weevoo26").lstrip("@")

BOT_USERNAME = "Oorderstatebot"
DB = "orders.db"

# =========================
# DATABASE
# =========================

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

    # Migrazione per database già esistenti
    columns = [
        row[1]
        for row in con.execute("PRAGMA table_info(orders)").fetchall()
    ]

    if "telegram_user_id" not in columns:
        con.execute(
            "ALTER TABLE orders ADD COLUMN telegram_user_id INTEGER"
        )

    con.commit()
    return con


# =========================
# STATI
# =========================

def text_for(status, progress):
    status = status.lower()

    if status == "in attesa":
        return f"🟡 *In attesa*\n\nProgresso: {progress}%"

    if status == "in elaborazione":
        return f"🔵 *In elaborazione*\n\nProgresso: {progress}%"

    if status == "completato":
        return f"🟢 *Completato*\n\nProgresso: {progress}%"

    if status == "annullato":
        return f"🔴 *Annullato*\n\nProgresso: {progress}%"

    return f"📦 *{status}*\n\nProgresso: {progress}%"


def status_from_command(status):
    status = status.lower()

    mapping = {
        "attesa": ("In attesa", 0),
        "lavorazione": ("In elaborazione", 50),
        "completato": ("Completato", 100),
        "annullato": ("Annullato", 0),
    }

    return mapping.get(status)


# =========================
# ADMIN
# =========================

def is_admin(update: Update):
    user = update.effective_user

    if not user:
        return False

    username = user.username

    if not username:
        return False

    return username.lower() == ADMIN_USERNAME.lower()


# =========================
# /START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not user:
        return

    con = db()

    # ---------------------------------
    # /start CODICE
    # ---------------------------------

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
                "❌ Ordine non trovato.\n\n"
                "Controlla il codice e riprova."
            )

            return

        # Associa l'utente Telegram all'ordine
        con.execute(
            """
            UPDATE orders
            SET telegram_user_id = ?
            WHERE code = ?
            """,
            (user.id, code)
        )

        con.commit()
        con.close()

        order_code, status, progress = row

        await update.message.reply_text(
            f"📦 *Ordine #{order_code}*\n\n"
            f"{text_for(status, progress)}\n\n"
            "✅ Questo ordine è stato associato al tuo account Telegram.\n"
            "La prossima volta ti basterà premere /start.",
            parse_mode="Markdown"
        )

        return

    # ---------------------------------
    # /start SENZA CODICE
    # ---------------------------------

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

        code, status, progress = row

        await update.message.reply_text(
            f"📦 *Ordine #{code}*\n\n"
            f"{text_for(status, progress)}",
            parse_mode="Markdown"
        )

        return

    # Nessun ordine associato

    await update.message.reply_text(
        "👋 Ciao! Benvenuto.\n\n"
        "📦 Per controllare lo stato del tuo ordine, "
        "apri il link che ti è stato inviato e premi *Start*.\n\n"
        "Dopo la prima apertura, il bot ricorderà automaticamente "
        "il tuo ordine.",
        parse_mode="Markdown"
    )


# =========================
# /ADMIN
# =========================

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await update.message.reply_text("❌ Non autorizzato.")
        return

    await update.message.reply_text(
        "🛠 *Pannello Admin*\n\n"
        "Crea ordine:\n"
        "`/crea CODICE`\n\n"
        "Aggiorna stato:\n"
        "`/stato CODICE attesa`\n"
        "`/stato CODICE lavorazione`\n"
        "`/stato CODICE completato`\n"
        "`/stato CODICE annullato`",
        parse_mode="Markdown"
    )


# =========================
# /CREA
# =========================

async def crea(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await update.message.reply_text("❌ Non autorizzato.")
        return

    if not context.args:
        await update.message.reply_text(
            "Uso:\n/crea CODICE"
        )
        return

    code = context.args[0].strip()

    con = db()

    try:
        con.execute(
            """
            INSERT INTO orders (
                code,
                status,
                progress,
                telegram_user_id
            )
            VALUES (?, ?, ?, NULL)
            """,
            (code, "In attesa", 0)
        )

        con.commit()

    except sqlite3.IntegrityError:
        con.close()

        await update.message.reply_text(
            "❌ Questo codice esiste già."
        )

        return

    con.close()

    link = f"https://t.me/{BOT_USERNAME}?start={code}"

    await update.message.reply_text(
        f"✅ Ordine creato!\n\n"
        f"📦 Codice: `{code}`\n"
        f"🔗 Link cliente:\n{link}",
        parse_mode="Markdown"
    )


# =========================
# /STATO
# =========================

async def stato(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update):
        await update.message.reply_text("❌ Non autorizzato.")
        return

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
    new_status_key = context.args[1].lower().strip()

    result = status_from_command(new_status_key)

    if not result:
        await update.message.reply_text(
            "❌ Stato non valido.\n\n"
            "Usa:\n"
            "attesa\n"
            "lavorazione\n"
            "completato\n"
            "annullato"
        )
        return

    new_status, progress = result

    con = db()

    row = con.execute(
        """
        SELECT telegram_user_id
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

    telegram_user_id = row[0]

    # Aggiorna database
    con.execute(
        """
        UPDATE orders
        SET status = ?, progress = ?
        WHERE code = ?
        """,
        (new_status, progress, code)
    )

    con.commit()
    con.close()

    # Conferma all'admin
    await update.message.reply_text(
        f"✅ Ordine `{code}` aggiornato.\n\n"
        f"{text_for(new_status, progress)}",
        parse_mode="Markdown"
    )

    # ---------------------------------
    # NOTIFICA AUTOMATICA AL CLIENTE
    # ---------------------------------

    if telegram_user_id:

        try:

            await context.bot.send_message(
                chat_id=telegram_user_id,
                text=(
                    f"🔔 *Aggiornamento ordine #{code}*\n\n"
                    f"{text_for(new_status, progress)}"
                ),
                parse_mode="Markdown"
            )

        except Exception:
            # Il cliente potrebbe aver bloccato il bot,
            # cancellato la chat, ecc.
            pass


# =========================
# TELEGRAM APPLICATION
# =========================

application = (
    Application.builder()
    .token(TOKEN)
    .build()
)

application.add_handler(
    CommandHandler("start", start)
)

application.add_handler(
    CommandHandler("admin", admin)
)

application.add_handler(
    CommandHandler("crea", crea)
)

application.add_handler(
    CommandHandler("stato", stato)
)


# =========================
# FASTAPI / WEBHOOK
# =========================

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


# =========================
# STARTUP
# =========================

@app_web.on_event("startup")
async def startup():

    db().close()

    await application.initialize()
    await application.start()

    external_url = os.environ.get("RENDER_EXTERNAL_URL")

    if external_url:

        webhook_url = (
            external_url.rstrip("/")
            + "/telegram"
        )

        await application.bot.set_webhook(
            webhook_url
        )


# =========================
# SHUTDOWN
# =========================

@app_web.on_event("shutdown")
async def shutdown():

    await application.stop()
    await application.shutdown()
