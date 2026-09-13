import os, sqlite3
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "weevoo26").lstrip("@")
DB = "orders.db"

app_web = FastAPI()
tg_app = Application.builder().token(TOKEN).build()

def db():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS orders(
        code TEXT PRIMARY KEY, status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0
    )""")
    con.commit()
    return con

def is_admin(update):
    u = update.effective_user
    return u and u.username and u.username.lower() == ADMIN_USERNAME.lower()

def text_for(code, status, progress):
    icons = {"In attesa":"🟡", "In elaborazione":"🔵", "Completato":"🟢", "Annullato":"🔴"}
    filled = max(0, min(10, round(progress/10)))
    bar = "█"*filled + "░"*(10-filled)
    return f"📦 *Ordine #{code}*\n\n{icons.get(status,'⚪')} *Stato:* {status}\n\n`{bar}` *{progress}%*"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = context.args[0] if context.args else None
    if not code:
        await update.message.reply_text("👋 Benvenuto! Apri il link del tuo ordine per vedere lo stato.")
        return
    con=db()
    row=con.execute("SELECT code,status,progress FROM orders WHERE code=?", (code,)).fetchone()
    con.close()
    if not row:
        await update.message.reply_text("❌ Ordine non trovato.")
        return
    await update.message.reply_text(text_for(*row), parse_mode="Markdown")

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Accesso non autorizzato.")
        return
    await update.message.reply_text(
        "👑 *Pannello admin*\n\n"
        "Crea ordine:\n`/crea CODICE`\n\n"
        "Cambia stato:\n`/stato CODICE attesa`\n"
        "`/stato CODICE lavorazione`\n"
        "`/stato CODICE completato`\n"
        "`/stato CODICE annullato`\n\n"
        "Il link cliente sarà:\n"
        "`https://t.me/USERNAME_DEL_BOT?start=CODICE`",
        parse_mode="Markdown"
    )

async def crea(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    if not context.args:
        await update.message.reply_text("Usa: /crea CODICE")
        return
    code=context.args[0]
    con=db()
    try:
        con.execute("INSERT INTO orders(code,status,progress) VALUES(?,?,?)",(code,"In attesa",0))
        con.commit()
        await update.message.reply_text(f"✅ Ordine #{code} creato.\n\nLink cliente:\nhttps://t.me/USERNAME_DEL_BOT?start={code}")
    except sqlite3.IntegrityError:
        await update.message.reply_text("⚠️ Questo codice esiste già.")
    finally:
        con.close()

async def stato(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    if len(context.args) < 2:
        await update.message.reply_text("Usa: /stato CODICE attesa|lavorazione|completato|annullato")
        return
    code, s = context.args[0], context.args[1].lower()
    mapping={"attesa":("In attesa",0),"lavorazione":("In elaborazione",50),"completato":("Completato",100),"annullato":("Annullato",0)}
    if s not in mapping:
        await update.message.reply_text("Stato non valido.")
        return
    status,progress=mapping[s]
    con=db()
    cur=con.execute("UPDATE orders SET status=?,progress=? WHERE code=?",(status,progress,code))
    con.commit()
    row=con.execute("SELECT code,status,progress FROM orders WHERE code=?",(code,)).fetchone()
    con.close()
    if cur.rowcount == 0:
        await update.message.reply_text("❌ Ordine non trovato.")
    else:
        await update.message.reply_text(text_for(*row),parse_mode="Markdown")

tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(CommandHandler("admin", admin))
tg_app.add_handler(CommandHandler("crea", crea))
tg_app.add_handler(CommandHandler("stato", stato))

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
    db().close()
    await tg_app.initialize()
    await tg_app.start()
    external = os.environ.get("RENDER_EXTERNAL_URL")
    if external:
        await tg_app.bot.set_webhook(external.rstrip("/") + "/telegram")

@app_web.on_event("shutdown")
async def shutdown():
    await tg_app.stop()
    await tg_app.shutdown()
