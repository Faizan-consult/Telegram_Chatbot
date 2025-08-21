from fastapi import FastAPI, Request
import httpx
import os
from dotenv import load_dotenv
from openai import OpenAI

# -----------------------------
# 1) Environment & clients
# -----------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("Telegram bot token missing. Set BOT_TOKEN or TELEGRAM_BOT_TOKEN in your .env")
if not OPENAI_API_KEY:
    raise RuntimeError("OpenAI API key missing. Set OPENAI_API_KEY in your .env")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = FastAPI(title="Week 2 Bot — Modes + Context")
oai = OpenAI(api_key=OPENAI_API_KEY)

# -----------------------------
# 2) Memory & Modes
# -----------------------------
conversations = {}   # { chat_id: [ {role, content}, ... ] }
user_modes = {}      # { chat_id: "general" }
MAX_TURNS = 10

modes = {
    "general": "You are a helpful, concise assistant for everyday questions.",
    "restaurant": "You are a restaurant assistant. Help users find restaurants, suggest dishes, and answer politely.",
    "fitness": "You are a friendly fitness coach. Give workout routines, diet tips, and motivational advice.",
    "realestate": "You are a professional real estate agent. Provide property suggestions, buying/selling advice, and market insights.",
}

DEFAULT_MODE = "general"


def get_history(chat_id: int):
    return conversations.setdefault(chat_id, [])


def append_and_trim(chat_id: int, role: str, content: str):
    hist = get_history(chat_id)
    hist.append({"role": role, "content": content})
    if len(hist) > MAX_TURNS * 2:
        conversations[chat_id] = hist[-MAX_TURNS * 2:]


def get_mode(chat_id: int) -> str:
    return user_modes.get(chat_id, DEFAULT_MODE)


# -----------------------------
# 3) Helpers
# -----------------------------
# -----------------------------
# 3) Helpers
# -----------------------------
async def answer_callback_query(callback_query_id: str, text: str = None):
    async with httpx.AsyncClient(timeout=10) as http:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        await http.post(f"{TELEGRAM_API}/answerCallbackQuery", json=payload)
        
async def send_typing(chat_id: int):
    async with httpx.AsyncClient(timeout=10) as http:
        await http.post(
            f"{TELEGRAM_API}/sendChatAction",
            params={"chat_id": chat_id, "action": "typing"},
        )

async def send_message(chat_id: int, text: str, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=30) as http:
        await http.post(f"{TELEGRAM_API}/sendMessage", json=payload)


def main_keyboard():
    """Persistent reply keyboard with a Mode button."""
    return {
        "keyboard": [[{"text": "⚙️ Mode"}, {"text": "🔄 Reset"}]],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


# -----------------------------
# 4) Webhook
# -----------------------------
@app.post("/webhook")
async def webhook(req: Request):
    update = await req.json()
    print("📩 Incoming update:", update)

    # Handle callback_query (button press)
    if "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        callback_data = cq["data"]
        cq_id = cq["id"]

        if callback_data.startswith("mode:"):
            mode_choice = callback_data.split(":")[1]
            if mode_choice in modes:
                user_modes[chat_id] = mode_choice
                conversations.pop(chat_id, None)  # clear memory on mode change

                # ✅ Rebuild inline keyboard with updated checkmark
                buttons = [[{
                    "text": f"{'✅ ' if name == mode_choice else ''}{name.title()}",
                    "callback_data": f"mode:{name}"
                }] for name in modes.keys()]
                reply_markup = {"inline_keyboard": buttons}

                await answer_callback_query(cq_id, f"Mode set to {mode_choice}")
                await send_message(chat_id, f"✅ Mode switched to *{mode_choice.title()}*", reply_markup=main_keyboard())
            return {"ok": True}

    # Handle normal messages
    message = update.get("message")
    if not message:
        return {"ok": True}

    chat_id = message.get("chat", {}).get("id")
    text = message.get("text")

    if chat_id is None:
        return {"ok": True}

    # Show typing
    await send_typing(chat_id)

    if not isinstance(text, str):
        await send_message(chat_id, "I can only read text messages for now 😊")
        return {"ok": True}

    # -----------------------------
    # Commands
    # -----------------------------
    if text.strip().lower().startswith("/start"):
        conversations.pop(chat_id, None)
        user_modes[chat_id] = DEFAULT_MODE
        await send_message(
            chat_id,
            "👋 Hi! I’m your AI assistant.\n"
            "• Use /reset to clear memory\n"
            "• Use /mode to change my role (restaurant, fitness, realestate)\n"
            "Default mode: General ✅",
            reply_markup=main_keyboard(),
        )
        return {"ok": True}

    if text.strip().lower().startswith("/reset"):
        conversations.pop(chat_id, None)
        await send_message(chat_id, "Memory cleared. Fresh start! ✨", reply_markup=main_keyboard())
        return {"ok": True}

    if text.strip().lower() in ["/mode", "⚙️ mode"]:
        current = get_mode(chat_id)
        buttons = [[{
            "text": f"{'✅ ' if name == current else ''}{name.title()}",
            "callback_data": f"mode:{name}"
        }] for name in modes.keys()]
        reply_markup = {"inline_keyboard": buttons}
        await send_message(chat_id, "👉 Choose a mode:", reply_markup=reply_markup)
        return {"ok": True}


    # -----------------------------
    # Store + Generate GPT reply
    # -----------------------------
    append_and_trim(chat_id, "user", text)
    mode = get_mode(chat_id)
    system_prompt = modes.get(mode, modes[DEFAULT_MODE])

    messages = [{"role": "system", "content": system_prompt}] + get_history(chat_id)

    try:
        completion = oai.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
        )
        bot_reply = completion.choices[0].message.content or "..."
    except Exception as e:
        print("❌ OpenAI error:", e)
        bot_reply = "Sorry, I ran into an issue."

    append_and_trim(chat_id, "assistant", bot_reply)

    # ✅ Add [Mode] label in responses
    mode_label = mode.title()
    reply_text = f"💬 *[{mode_label} Mode]*\n\n{bot_reply}"
    await send_message(chat_id, reply_text, reply_markup=main_keyboard())

    return {"ok": True}
