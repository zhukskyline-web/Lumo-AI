"""Lumo AI — Telegram-бот + Mini App с подписками за Telegram Stars."""
import asyncio, hashlib, hmac, json, logging, os, sqlite3, time
from pathlib import Path
from urllib.parse import parse_qsl

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
                           MenuButtonWebApp, Message, PreCheckoutQuery, WebAppInfo, CallbackQuery)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)
BOT_TOKEN = os.environ["BOT_TOKEN"]
AI_KEY = os.environ["AI_API_KEY"]
AI_URL = os.getenv("AI_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "llama-3.3-70b-versatile")
BASE_URL = (os.getenv("BASE_URL") or os.getenv("RENDER_EXTERNAL_URL", "")).rstrip("/")
SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
ROOT = Path(__file__).parent

FREE_DAILY = 10_000  # бесплатных токенов в сутки
PLANS = {  # цены в Telegram Stars (XTR), срок 30 дней
    "plus": {"name": "Plus", "stars": 150, "tokens": 300_000, "perks": ["300 тыс. токенов", "Приоритетные ответы"]},
    "pro":  {"name": "Pro",  "stars": 400, "tokens": 1_000_000, "perks": ["1 млн токенов", "Приоритетные ответы", "Длинные ответы"]},
    "max":  {"name": "Max",  "stars": 900, "tokens": 3_000_000, "perks": ["3 млн токенов", "Приоритетные ответы", "Длинные ответы"]},
}
SYSTEM = ("Ты Lumo — дружелюбный и умный ИИ-ассистент в Telegram. Отвечай на языке пользователя, "
          "по делу и без воды. Код оформляй простым текстом с отступами.")

conn = sqlite3.connect(os.getenv("DB_PATH", "lumo.db"), check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, plan TEXT DEFAULT 'free',
  plan_until INTEGER DEFAULT 0, tokens INTEGER DEFAULT 0, free_tokens INTEGER, free_day INTEGER);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, role TEXT, content TEXT);
CREATE TABLE IF NOT EXISTS payments(charge_id TEXT PRIMARY KEY, uid INTEGER, plan TEXT, stars INTEGER, ts INTEGER);
""")

# ---------- пользователи и токены ----------
def get_user(uid, name=""):
    now = int(time.time()); day = now // 86400
    conn.execute("INSERT OR IGNORE INTO users(id,name,free_tokens,free_day) VALUES(?,?,?,?)", (uid, name, FREE_DAILY, day))
    u = dict(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
    if u["free_day"] != day: u["free_tokens"], u["free_day"] = FREE_DAILY, day
    if u["plan"] != "free" and u["plan_until"] < now: u["plan"], u["tokens"] = "free", 0
    conn.execute("UPDATE users SET plan=?,tokens=?,free_tokens=?,free_day=? WHERE id=?",
                 (u["plan"], u["tokens"], u["free_tokens"], u["free_day"], uid))
    conn.commit()
    return u

def spend(u, n):
    f = min(u["free_tokens"], n); p = min(u["tokens"], n - f)
    conn.execute("UPDATE users SET free_tokens=free_tokens-?, tokens=tokens-? WHERE id=?", (f, p, u["id"]))
    conn.commit()

def activate(uid, plan_id, charge_id="", stars=0):
    u = get_user(uid); now = int(time.time()); p = PLANS[plan_id]
    until = (max(u["plan_until"], now) if u["plan"] == plan_id else now) + 30 * 86400
    conn.execute("UPDATE users SET plan=?, plan_until=?, tokens=tokens+? WHERE id=?", (plan_id, until, p["tokens"], uid))
    conn.execute("INSERT OR IGNORE INTO payments VALUES(?,?,?,?,?)", (charge_id or f"m{now}{uid}", uid, plan_id, stars, now))
    conn.commit()

def public(u):
    plan = PLANS.get(u["plan"], {"name": "Free"})
    return {"plan": u["plan"], "plan_name": plan["name"], "until": u["plan_until"],
            "balance": u["free_tokens"] + u["tokens"], "free": u["free_tokens"], "paid": u["tokens"],
            "limit": FREE_DAILY if u["plan"] == "free" else PLANS[u["plan"]]["tokens"]}

class NoTokens(Exception): ...

async def ask_ai(uid, name, text):
    u = get_user(uid, name)
    if u["free_tokens"] + u["tokens"] <= 0: raise NoTokens
    hist = conn.execute("SELECT role,content FROM messages WHERE uid=? ORDER BY id DESC LIMIT 12", (uid,)).fetchall()[::-1]
    msgs = [{"role": "system", "content": SYSTEM}] + [dict(r) for r in hist] + [{"role": "user", "content": text[:4000]}]
    max_t = 2048 if u["plan"] in ("pro", "max") else 1024
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{AI_URL}/chat/completions", headers={"Authorization": f"Bearer {AI_KEY}"},
                          json={"model": AI_MODEL, "messages": msgs, "max_tokens": max_t},
                          timeout=aiohttp.ClientTimeout(total=90)) as r:
            data = await r.json()
            if r.status != 200: raise RuntimeError(str(data)[:300])
    reply = data["choices"][0]["message"]["content"].strip()
    used = (data.get("usage") or {}).get("total_tokens") or max(1, (len(text) + len(reply)) // 3)
    spend(u, used)
    conn.executemany("INSERT INTO messages(uid,role,content) VALUES(?,?,?)", [(uid, "user", text), (uid, "assistant", reply)])
    conn.commit()
    return reply, used, public(get_user(uid))

def fmt(n): return f"{n:,}".replace(",", " ")

# ---------- бот ----------
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

def app_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✨ Открыть Lumo", web_app=WebAppInfo(url=BASE_URL))]])

@dp.message(CommandStart())
async def start(m: Message):
    u = public(get_user(m.from_user.id, m.from_user.full_name))
    await m.answer(f"Привет, {m.from_user.first_name}! Я <b>Lumo</b> — ИИ, который освещает любой вопрос.\n\n"
                   f"Пиши прямо сюда или открой приложение: там чат, баланс и тарифы.\n"
                   f"Сегодня доступно: <b>{fmt(u['balance'])}</b> токенов.", parse_mode="HTML", reply_markup=app_kb())

@dp.message(Command("balance"))
async def balance(m: Message):
    u = public(get_user(m.from_user.id, m.from_user.full_name))
    until = time.strftime("%d.%m.%Y", time.localtime(u["until"])) if u["plan"] != "free" else "—"
    await m.answer(f"Тариф: <b>{u['plan_name']}</b> (до {until})\nТокенов: <b>{fmt(u['balance'])}</b>\n"
                   f"Бесплатные обновляются каждые сутки.", parse_mode="HTML")

@dp.message(Command("new"))
async def new(m: Message):
    conn.execute("DELETE FROM messages WHERE uid=?", (m.from_user.id,)); conn.commit()
    await m.answer("Начали с чистого листа.")

@dp.message(Command("buy"))
async def buy(m: Message):
    kb = [[InlineKeyboardButton(text=f"{p['name']} · {p['stars']} ⭐ · {fmt(p['tokens'])} ток.", callback_data=f"buy:{k}")]
          for k, p in PLANS.items()]
    await m.answer("Выбери тариф на 30 дней:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("buy:"))
async def buy_cb(c: CallbackQuery):
    k = c.data.split(":")[1]; p = PLANS[k]
    await bot.send_invoice(c.from_user.id, title=f"Lumo {p['name']}", description=f"{fmt(p['tokens'])} токенов на 30 дней",
                           payload=f"plan:{k}", currency="XTR", provider_token="", prices=[LabeledPrice(label=p["name"], amount=p["stars"])])
    await c.answer()

@dp.pre_checkout_query()
async def pre(q: PreCheckoutQuery): await q.answer(ok=True)

@dp.message(F.successful_payment)
async def paid(m: Message):
    sp = m.successful_payment; k = sp.invoice_payload.split(":")[1]
    activate(m.from_user.id, k, sp.telegram_payment_charge_id, sp.total_amount)
    await m.answer(f"🎉 Тариф <b>{PLANS[k]['name']}</b> активен на 30 дней. Начислено {fmt(PLANS[k]['tokens'])} токенов.", parse_mode="HTML")

@dp.message(F.text & ~F.text.startswith("/"))
async def chat(m: Message):
    await bot.send_chat_action(m.chat.id, "typing")
    try:
        reply, used, u = await ask_ai(m.from_user.id, m.from_user.full_name, m.text)
    except NoTokens:
        return await m.answer("Токены на сегодня закончились. Они обновятся завтра, или возьми тариф: /buy")
    except Exception as e:
        logging.exception(e); return await m.answer("Не получилось получить ответ. Попробуй ещё раз через минуту.")
    for i in range(0, len(reply), 3900):
        await m.answer(reply[i:i + 3900])
    await m.answer(f"⚡ −{fmt(used)} · осталось {fmt(u['balance'])}")

# ---------- API для Mini App ----------
def check_init(raw):
    try:
        d = dict(parse_qsl(raw, strict_parsing=True)); h = d.pop("hash")
        s = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(key, s.encode(), hashlib.sha256).hexdigest(), h): return None
        if time.time() - int(d["auth_date"]) > 86400: return None
        return json.loads(d["user"])
    except Exception:
        return None

def me(req):
    tg = check_init(req.headers.get("X-Init-Data", ""))
    if not tg: raise web.HTTPUnauthorized(text=json.dumps({"error": "auth"}), content_type="application/json")
    return tg["id"], tg.get("first_name", "")

async def api_me(req):
    uid, name = me(req); u = get_user(uid, name)
    h = conn.execute("SELECT role,content FROM messages WHERE uid=? ORDER BY id DESC LIMIT 30", (uid,)).fetchall()[::-1]
    plans = [{"id": "free", "name": "Free", "stars": 0, "perks": [f"{fmt(FREE_DAILY)} токенов в сутки", "Без оплаты"]}]
    plans += [{"id": k, "name": p["name"], "stars": p["stars"], "perks": p["perks"]} for k, p in PLANS.items()]
    return web.json_response({"name": name, "me": public(u), "plans": plans, "history": [dict(r) for r in h]})

async def api_chat(req):
    uid, name = me(req); text = ((await req.json()).get("text") or "").strip()
    if not text: raise web.HTTPBadRequest()
    try:
        reply, used, u = await ask_ai(uid, name, text)
    except NoTokens:
        return web.json_response({"error": "no_tokens"}, status=402)
    except Exception as e:
        logging.exception(e); return web.json_response({"error": "ai"}, status=502)
    return web.json_response({"reply": reply, "used": used, "me": u})

async def api_invoice(req):
    uid, _ = me(req); k = (await req.json()).get("plan")
    if k not in PLANS: raise web.HTTPBadRequest()
    p = PLANS[k]
    link = await bot.create_invoice_link(title=f"Lumo {p['name']}", description=f"{fmt(p['tokens'])} токенов на 30 дней",
                                         payload=f"plan:{k}", provider_token="", currency="XTR",
                                         prices=[LabeledPrice(label=p["name"], amount=p["stars"])])
    return web.json_response({"link": link})

async def api_reset(req):
    uid, _ = me(req); conn.execute("DELETE FROM messages WHERE uid=?", (uid,)); conn.commit()
    return web.json_response({"ok": True})

# ---------- запуск ----------
async def keepalive():  # Render Free засыпает через 15 мин без трафика
    while True:
        await asyncio.sleep(600)
        try:
            async with aiohttp.ClientSession() as s: await s.get(f"{BASE_URL}/health")
        except Exception: pass

async def on_start(app):
    await bot.set_webhook(f"{BASE_URL}/webhook", secret_token=SECRET, allowed_updates=dp.resolve_used_update_types())
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Lumo", web_app=WebAppInfo(url=BASE_URL)))
    app["ka"] = asyncio.create_task(keepalive())

def main():
    app = web.Application()
    app.router.add_get("/", lambda r: web.FileResponse(ROOT / "static" / "index.html"))
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    app.router.add_get("/api/me", api_me); app.router.add_post("/api/chat", api_chat)
    app.router.add_post("/api/invoice", api_invoice); app.router.add_post("/api/reset", api_reset)
    SimpleRequestHandler(dp, bot, secret_token=SECRET).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_start)
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

if __name__ == "__main__":
    main()
