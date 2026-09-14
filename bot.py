import os
import sqlite3
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "weevoo26").lstrip("@")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "Oorderstatebot").lstrip("@")

# Render: use /data when a persistent disk is mounted.
DB = "/data/orders.db" if os.path.isdir("/data") else "orders.db"

app_web = FastAPI()
tg_app = Application.builder().token(TOKEN).build()


STATUSES = {
    "attesa": ("In attesa", "🟡", 0),
    "lavorazione": ("In elaborazione", "🔵", 50),
    "completato": ("Completato", "🟢", 100),
    "annullato": ("Annullato", "🔴", 0),
}


def db():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row

    con.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            code TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'In attesa',
            progress INTEGER NOT NULL DEFAULT 0,
            minutes INTEGER,
            telegram_user_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    con.commit()
    return con


def now():
    return datetime.now(timezone.utc).isoformat()


def is_admin(update: Update):
    user = update.effective_user
    if not user:
        return False

    # Stable ID saved after a successful admin login.
    con = db()
    row = con.execute(
        "SELECT value FROM settings WHERE key='admin_user_id'"
    ).fetchone()
    con.close()

    if row and row["value"] == str(user.id):
        return True

    # First authorization can be done through ADMIN_USERNAME.
    return bool(user.username and user.username.lower() == ADMIN_USERNAME.lower())


def remember_admin(user_id: int):
    con = db()
    con.execute(
        "INSERT INTO settings(key,value) VALUES('admin_user_id',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(user_id),),
    )
    con.commit()
    con.close()


def progress_bar(progress: int):
    progress = max(0, min(100, int(progress)))
    filled = round(progress / 10)
    return "█" * filled + "░" * (10 - filled)


def order_text(row, admin=False):
    status = row["status"]
    icon = next((v[1] for v in STATUSES.values() if v[0] == status), "⚪")
    progress = row["progress"]

    text = (
        f"📦 <b>Ordine #{row['code']}</b>\n\n"
        f"{icon} <b>Stato:</b> {status}\n"
        f"📊 <code>{progress_bar(progress)}</code> {progress}%\n"
    )

    if row["minutes"] is not None:
        text += f"⏱️ <b>Tempo indicativo:</b> {row['minutes']} minuti\n"

    if admin:
        linked = "Sì" if row["telegram_user_id"] else "No"
        text += f"👤 <b>Cliente collegato:</b> {linked}\n"

    return text


def admin_main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Crea ordine", callback_data="admin:create"),
            InlineKeyboardButton("📦 Ordini", callback_data="admin:orders"),
        ],
        [
            InlineKeyboardButton("⚙️ Gestisci ordini", callback_data="admin:manage"),
            InlineKeyboardButton("🗑️ Elimina", callback_data="admin:delete"),
        ],
        [
            InlineKeyboardButton("🔄 Aggiorna", callback_data="admin:home"),
        ],
    ])


def order_keyboard(code):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟡 Attesa", callback_data=f"status:{code}:attesa"),
            InlineKeyboardButton("🔵 Lavorazione", callback_data=f"status:{code}:lavorazione"),
        ],
        [
            InlineKeyboardButton("🟢 Completato", callback_data=f"status:{code}:completato"),
            InlineKeyboardButton("🔴 Annullato", callback_data=f"status:{code}:annullato"),
        ],
        [
            InlineKeyboardButton("⏱️ Imposta minuti", callback_data=f"minutes:{code}"),
        ],
        [
            InlineKeyboardButton("🗑️ Elimina", callback_data=f"delete:{code}"),
            InlineKeyboardButton("⬅️ Menu", callback_data="admin:manage"),
        ],
    ])


def admin_home_text():
    return (
        "👑 <b>PANNELLO ADMIN</b>\n\n"
        "Da qui puoi gestire tutto senza usare menu separati:\n"
        "• creare ordini\n"
        "• vedere gli ordini\n"
        "• cambiare stato\n"
        "• impostare percentuale e minuti\n"
        "• eliminare ordini\n"
        "• aprire il link cliente\n\n"
        "👇 Scegli un'azione:"
    )


async def send_admin_home(target, edit=False):
    if edit:
        await target.edit_message_text(
            admin_home_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_main_keyboard(),
        )
    else:
        await target.reply_text(
            admin_home_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_main_keyboard(),
        )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Accesso non autorizzato.")
        return

    remember_admin(update.effective_user.id)
    await send_admin_home(update.message)


async def create_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    remember_admin(update.effective_user.id)

    if not context.args:
        await update.message.reply_text("Uso: /crea CODICE")
        return

    code = context.args[0].strip()
    if not code or len(code) > 80:
        await update.message.reply_text("❌ Codice non valido.")
        return

    con = db()
    try:
        stamp = now()
        con.execute(
            "INSERT INTO orders(code,status,progress,minutes,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (code, "In attesa", 0, None, stamp, stamp),
        )
        con.commit()
    except sqlite3.IntegrityError:
        con.close()
        await update.message.reply_text("⚠️ Questo codice esiste già.")
        return

    con.close()

    link = f"https://t.me/{BOT_USERNAME}?start={code}"
    await update.message.reply_text(
        f"✅ <b>Ordine #{code} creato</b>\n\n"
        f"🔗 <b>Link cliente:</b>\n{link}",
        parse_mode=ParseMode.HTML,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    code = context.args[0].strip() if context.args else None

    if not code:
        # If the client has already been linked to an order, show the latest one.
        con = db()
        row = con.execute(
            "SELECT * FROM orders WHERE telegram_user_id=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (user.id,),
        ).fetchone()
        con.close()

        if row:
            await update.message.reply_text(
                order_text(row), parse_mode=ParseMode.HTML
            )
        else:
            await update.message.reply_text(
                "👋 Benvenuto!\n\nApri il link del tuo ordine per collegarlo e vedere lo stato."
            )
        return

    con = db()
    row = con.execute(
        "SELECT * FROM orders WHERE code=?", (code,)
    ).fetchone()

    if not row:
        con.close()
        await update.message.reply_text("❌ Ordine non trovato.")
        return

    # Link the Telegram account to this order.
    con.execute(
        "UPDATE orders SET telegram_user_id=?, updated_at=? WHERE code=?",
        (user.id, now(), code),
    )
    con.commit()
    row = con.execute(
        "SELECT * FROM orders WHERE code=?", (code,)
    ).fetchone()
    con.close()

    await update.message.reply_text(
        order_text(row), parse_mode=ParseMode.HTML
    )


async def show_orders_message(message, manage=False):
    con = db()
    rows = con.execute(
        "SELECT * FROM orders ORDER BY updated_at DESC"
    ).fetchall()
    con.close()

    if not rows:
        await message.reply_text(
            "📦 <b>Nessun ordine presente.</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Menu", callback_data="admin:home")]
            ]),
        )
        return

    text = "📦 <b>ORDINI</b>\n\n"
    buttons = []

    for row in rows[:50]:
        text += (
            f"• <b>#{row['code']}</b> — {row['status']} — "
            f"{row['progress']}%"
        )
        if row["minutes"] is not None:
            text += f" — ⏱️ {row['minutes']}m"
        text += "\n"

        buttons.append([
            InlineKeyboardButton(
                f"⚙️ #{row['code']}",
                callback_data=f"open:{row['code']}"
            )
        ])

    buttons.append([
        InlineKeyboardButton("⬅️ Menu", callback_data="admin:home")
    ])

    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(update):
        await query.answer("⛔ Non autorizzato.", show_alert=True)
        return

    remember_admin(update.effective_user.id)
    data = query.data or ""

    if data == "admin:home":
        await send_admin_home(query, edit=True)
        return

    if data == "admin:create":
        await query.edit_message_text(
            "➕ <b>CREA ORDINE</b>\n\n"
            "Usa:\n<code>/crea CODICE</code>\n\n"
            "Dopo la creazione, torna qui con /admin.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Menu", callback_data="admin:home")]
            ]),
        )
        return

    if data in ("admin:orders", "admin:manage"):
        con = db()
        rows = con.execute(
            "SELECT * FROM orders ORDER BY updated_at DESC"
        ).fetchall()
        con.close()

        if not rows:
            await query.edit_message_text(
                "📦 <b>Nessun ordine presente.</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Menu", callback_data="admin:home")]
                ]),
            )
            return

        buttons = []
        for row in rows[:50]:
            buttons.append([
                InlineKeyboardButton(
                    f"📦 #{row['code']} · {row['status']} · {row['progress']}%",
                    callback_data=f"open:{row['code']}"
                )
            ])

        buttons.append([
            InlineKeyboardButton("⬅️ Menu", callback_data="admin:home")
        ])

        title = "⚙️ <b>GESTIONE ORDINI</b>" if data == "admin:manage" else "📦 <b>ORDINI</b>"
        await query.edit_message_text(
            title + "\n\nSeleziona un ordine:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if data == "admin:delete":
        con = db()
        rows = con.execute(
            "SELECT code,status FROM orders ORDER BY updated_at DESC"
        ).fetchall()
        con.close()

        if not rows:
            await query.edit_message_text(
                "🗑️ Nessun ordine da eliminare.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Menu", callback_data="admin:home")]
                ]),
            )
            return

        buttons = [
            [InlineKeyboardButton(
                f"🗑️ #{r['code']} — {r['status']}",
                callback_data=f"delete:{r['code']}"
            )]
            for r in rows[:50]
        ]
        buttons.append([
            InlineKeyboardButton("⬅️ Menu", callback_data="admin:home")
        ])

        await query.edit_message_text(
            "🗑️ <b>ELIMINA ORDINE</b>\n\nSeleziona quello da eliminare:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if data.startswith("open:"):
        code = data.split(":", 1)[1]
        con = db()
        row = con.execute(
            "SELECT * FROM orders WHERE code=?", (code,)
        ).fetchone()
        con.close()

        if not row:
            await query.edit_message_text(
                "❌ Ordine non trovato.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Menu", callback_data="admin:manage")]
                ]),
            )
            return

        await query.edit_message_text(
            order_text(row, admin=True),
            parse_mode=ParseMode.HTML,
            reply_markup=order_keyboard(code),
        )
        return

    if data.startswith("status:"):
        _, code, new_status = data.split(":", 2)
        if new_status not in STATUSES:
            return

        status, _, default_progress = STATUSES[new_status]

        con = db()
        old = con.execute(
            "SELECT * FROM orders WHERE code=?", (code,)
        ).fetchone()

        if not old:
            con.close()
            await query.edit_message_text("❌ Ordine non trovato.")
            return

        con.execute(
            "UPDATE orders SET status=?, progress=?, updated_at=? WHERE code=?",
            (status, default_progress, now(), code),
        )
        con.commit()
        row = con.execute(
            "SELECT * FROM orders WHERE code=?", (code,)
        ).fetchone()
        con.close()

        await query.edit_message_text(
            order_text(row, admin=True),
            parse_mode=ParseMode.HTML,
            reply_markup=order_keyboard(code),
        )

        # Notify linked customer automatically.
        if row["telegram_user_id"]:
            try:
                await context.bot.send_message(
                    chat_id=row["telegram_user_id"],
                    text="🔔 <b>Aggiornamento ordine</b>\n\n" + order_text(row),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        return

    if data.startswith("minutes:"):
        code = data.split(":", 1)[1]
        context.user_data["waiting_minutes_for"] = code

        await query.edit_message_text(
            f"⏱️ <b>MINUTI — ORDINE #{code}</b>\n\n"
            "Scrivi un numero, ad esempio:\n"
            "<code>30</code>\n\n"
            "Per rimuovere i minuti scrivi:\n"
            "<code>0</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Annulla", callback_data=f"open:{code}")]
            ]),
        )
        return

    if data.startswith("delete:"):
        code = data.split(":", 1)[1]
        await query.edit_message_text(
            f"⚠️ <b>Confermi l'eliminazione di #{code}?</b>\n\n"
            "Questa operazione elimina definitivamente l'ordine dal database.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Sì, elimina", callback_data=f"confirmdelete:{code}"),
                    InlineKeyboardButton("❌ No", callback_data=f"open:{code}"),
                ]
            ]),
        )
        return

    if data.startswith("confirmdelete:"):
        code = data.split(":", 1)[1]
        con = db()
        cur = con.execute("DELETE FROM orders WHERE code=?", (code,))
        con.commit()
        con.close()

        if cur.rowcount:
            await query.edit_message_text(
                f"✅ Ordine <b>#{code}</b> eliminato.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📦 Gestisci ordini", callback_data="admin:manage")],
                    [InlineKeyboardButton("⬅️ Menu", callback_data="admin:home")],
                ]),
            )
        else:
            await query.edit_message_text("❌ Ordine non trovato.")
        return


async def admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    code = context.user_data.get("waiting_minutes_for")
    if not code:
        return

    raw = (update.message.text or "").strip()
    if not raw.isdigit():
        await update.message.reply_text("❌ Inserisci solo un numero di minuti.")
        return

    minutes = int(raw)
    if minutes > 100000:
        await update.message.reply_text("❌ Numero di minuti troppo alto.")
        return

    context.user_data.pop("waiting_minutes_for", None)

    con = db()
    row = con.execute(
        "SELECT * FROM orders WHERE code=?", (code,)
    ).fetchone()

    if not row:
        con.close()
        await update.message.reply_text("❌ Ordine non trovato.")
        return

    new_minutes = None if minutes == 0 else minutes
    con.execute(
        "UPDATE orders SET minutes=?, updated_at=? WHERE code=?",
        (new_minutes, now(), code),
    )
    con.commit()
    row = con.execute(
        "SELECT * FROM orders WHERE code=?", (code,)
    ).fetchone()
    con.close()

    await update.message.reply_text(
        "✅ Minuti aggiornati.\n\n" + order_text(row, admin=True),
        parse_mode=ParseMode.HTML,
        reply_markup=order_keyboard(code),
    )

    if row["telegram_user_id"]:
        try:
            await context.bot.send_message(
                chat_id=row["telegram_user_id"],
                text="🔔 <b>Aggiornamento ordine</b>\n\n" + order_text(row),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


async def stato_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Compatibility command: /stato CODICE attesa|lavorazione|completato|annullato"""
    if not is_admin(update):
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Uso: /stato CODICE attesa|lavorazione|completato|annullato"
        )
        return

    code = context.args[0].strip()
    key = context.args[1].lower()

    if key not in STATUSES:
        await update.message.reply_text("❌ Stato non valido.")
        return

    status, _, progress = STATUSES[key]

    con = db()
    old = con.execute(
        "SELECT * FROM orders WHERE code=?", (code,)
    ).fetchone()

    if not old:
        con.close()
        await update.message.reply_text("❌ Ordine non trovato.")
        return

    con.execute(
        "UPDATE orders SET status=?, progress=?, updated_at=? WHERE code=?",
        (status, progress, now(), code),
    )
    con.commit()
    row = con.execute(
        "SELECT * FROM orders WHERE code=?", (code,)
    ).fetchone()
    con.close()

    await update.message.reply_text(
        order_text(row, admin=True),
        parse_mode=ParseMode.HTML,
        reply_markup=order_keyboard(code),
    )

    if row["telegram_user_id"]:
        try:
            await context.bot.send_message(
                chat_id=row["telegram_user_id"],
                text="🔔 <b>Aggiornamento ordine</b>\n\n" + order_text(row),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


# Commands
tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(CommandHandler("admin", admin_command))
tg_app.add_handler(CommandHandler("crea", create_command))
tg_app.add_handler(CommandHandler("stato", stato_command))

# Buttons
tg_app.add_handler(CallbackQueryHandler(callback))

# Admin numeric input for the minutes flow.
tg_app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text_input)
)


@app_web.get("/")
async def home():
    return {"status": "ok", "bot": "telegram-order-bot"}


@app_web.post("/telegram")
async def telegram_webhook(request: Request):
    data = await request.json()
    await tg_app.process_update(Update.de_json(data, tg_app.bot))
    return {"ok": True}


@app_web.on_event("startup")
async def startup():
    con = db()
    con.close()

    await tg_app.initialize()
    await tg_app.start()

    external = os.environ.get("RENDER_EXTERNAL_URL")
    if external:
        await tg_app.bot.set_webhook(external.rstrip("/") + "/telegram")


@app_web.on_event("shutdown")
async def shutdown():
    await tg_app.stop()
    await tg_app.shutdown()
