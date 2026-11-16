import os
import json
import time
import html
import sqlite3
import random
import threading
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions
import requests

try:
    import speedtest
except ImportError:
    speedtest = None

load_dotenv("ai.env")
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("❌ BOT_TOKEN не найден в ai.env")
bot = telebot.TeleBot(TOKEN)

MY_ID = 7226949215
CO_OWNER_ID = 8215618705
GLOBAL_ADMINS = {MY_ID, CO_OWNER_ID}
ANON_ADMIN_ID = 1087968824  # GroupAnonymousBot (админ, пишущий анонимно)
bot.start_time = time.time()

# =====================================================================
# БАЗА ДАННЫХ SQLITE
# =====================================================================
db_lock = threading.Lock()
db = sqlite3.connect('defender.db', check_same_thread=False)
cursor = db.cursor()

cursor.execute('''CREATE TABLE IF NOT EXISTS warns (chat_id INTEGER, user_id INTEGER, count INTEGER, PRIMARY KEY(chat_id, user_id))''')
cursor.execute('''CREATE TABLE IF NOT EXISTS rep (chat_id INTEGER, user_id INTEGER, rep INTEGER, PRIMARY KEY(chat_id, user_id))''')
cursor.execute('''CREATE TABLE IF NOT EXISTS rep_cooldown (chat_id INTEGER, from_id INTEGER, to_id INTEGER, last_time REAL, PRIMARY KEY(chat_id, from_id, to_id))''')
cursor.execute('''CREATE TABLE IF NOT EXISTS settings (chat_id INTEGER PRIMARY KEY, captcha INTEGER, antiflood INTEGER, antilink INTEGER, antirepeat INTEGER, greetings INTEGER, antichannel INTEGER, nightmode INTEGER, antidelete INTEGER)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS stopwords (chat_id INTEGER, word TEXT, PRIMARY KEY(chat_id, word))''')
cursor.execute('''CREATE TABLE IF NOT EXISTS marriages (chat_id INTEGER, user1_id INTEGER, user2_id INTEGER, PRIMARY KEY(chat_id, user1_id))''')
cursor.execute('''CREATE TABLE IF NOT EXISTS marriages2 (chat_id INTEGER, user1_id INTEGER, user2_id INTEGER, PRIMARY KEY(chat_id, user2_id))''')
cursor.execute('''CREATE TABLE IF NOT EXISTS user_joins (chat_id INTEGER, user_id INTEGER, join_time REAL, PRIMARY KEY(chat_id, user_id))''')
cursor.execute('''CREATE TABLE IF NOT EXISTS custom_admins (chat_id INTEGER, user_id INTEGER, PRIMARY KEY(chat_id, user_id))''')
cursor.execute('''CREATE TABLE IF NOT EXISTS user_cache (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS rules (chat_id INTEGER PRIMARY KEY, text TEXT)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS welcome (chat_id INTEGER PRIMARY KEY, text TEXT)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS night_state (chat_id INTEGER PRIMARY KEY, perms TEXT)''')
db.commit()


def db_execute(query, params=()):
    with db_lock:
        cursor.execute(query, params)
        db.commit()
        return cursor.fetchall()


SETTING_KEYS = ["captcha", "antiflood", "antilink", "antirepeat", "greetings", "antichannel", "nightmode", "antidelete"]
SETTING_NAMES = ["🔒 Капча", "🌊 Антифлуд", "🔗 Анти-ссылки", "🔄 Антиповторы", "👋 Приветствия", "📺 Антиканал", "🌙 Ночной режим", "🧹 Удалять сервисные"]


def get_setting(chat_id, key):
    if key not in SETTING_KEYS:
        return False
    res = db_execute(f"SELECT {key} FROM settings WHERE chat_id=?", (chat_id,))
    return bool(res and res[0][0])


def set_setting(chat_id, key, val):
    if key not in SETTING_KEYS:
        return
    db_execute("INSERT OR IGNORE INTO settings (chat_id) VALUES (?)", (chat_id,))
    db_execute(f"UPDATE settings SET {key}=? WHERE chat_id=?", (1 if val else 0, chat_id))


# =====================================================================
# ХЕЛПЕРЫ
# =====================================================================
def esc(s):
    return html.escape(str(s) if s is not None else "")


def user_link(u):
    return f'<a href="tg://user?id={u.id}">{esc(u.first_name)}</a>'


def is_private(message):
    return message.chat.type == 'private'


_admin_cache = {}  # chat_id -> (время, множество id админов)


def chat_admin_ids(chat_id):
    now = time.time()
    cached = _admin_cache.get(chat_id)
    if cached and now - cached[0] < 60:
        return cached[1]
    ids = {a.user.id for a in bot.get_chat_administrators(chat_id)}
    _admin_cache[chat_id] = (now, ids)
    return ids


def is_admin_id(chat_id, user_id):
    if user_id in GLOBAL_ADMINS:
        return True
    if db_execute("SELECT user_id FROM custom_admins WHERE chat_id=? AND user_id=?", (chat_id, user_id)):
        return True
    try:
        return user_id in chat_admin_ids(chat_id)
    except Exception:
        return False


def is_admin(message):
    if not message.from_user:
        return False
    if message.from_user.id in GLOBAL_ADMINS:
        return True
    if is_private(message):
        return False
    if message.from_user.id == ANON_ADMIN_ID:
        return True
    return is_admin_id(message.chat.id, message.from_user.id)


def is_target_admin(message):
    if not message.reply_to_message or not message.reply_to_message.from_user:
        return False
    return is_admin_id(message.chat.id, message.reply_to_message.from_user.id)


def safe_edit(chat_id, msg_id, text, reply_markup=None, parse_mode=None):
    if not msg_id:
        return
    try:
        bot.edit_message_text(text=text, chat_id=chat_id, message_id=msg_id, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        pass


def safe_delete(chat_id, msg_id):
    try:
        bot.delete_message(chat_id, msg_id)
    except Exception:
        pass


# =====================================================================
# МУТ / РАЗМУТ  (ГЛАВНЫЙ ФИКС)
# В telebot третий позиционный аргумент restrict_chat_member — это until_date,
# а не permissions. Поэтому права ВСЕГДА передаём по ключу permissions=...
# =====================================================================
FULL_PERMS = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True,
    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
    can_send_voice_notes=True, can_send_polls=True,
    can_send_other_messages=True, can_add_web_page_previews=True,
    can_invite_users=True,
)


def mute_user(chat_id, user_id, seconds=None):
    kwargs = {}
    if seconds:
        kwargs["until_date"] = int(time.time()) + int(seconds)
    bot.restrict_chat_member(
        chat_id, user_id,
        permissions=ChatPermissions(can_send_messages=False),
        **kwargs
    )


def try_mute(chat_id, user_id, seconds=None):
    try:
        mute_user(chat_id, user_id, seconds)
        return True
    except Exception as e:
        print(f"❌ Не удалось замутить {user_id}: {e}")
        return False


def default_permissions(chat_id):
    """Права по умолчанию в чате. Если в чате писать нельзя никому — полный набор."""
    try:
        p = bot.get_chat(chat_id).permissions
        if p is not None and p.can_send_messages:
            return p
        if p is not None and night_active(chat_id):
            return p  # ночью права чата закрыты, новичок получает права «как у чата»
    except Exception as e:
        print(f"⚠️ get_chat не вернул права: {e}")
    return FULL_PERMS


def can_write(chat_id, user_id):
    m = bot.get_chat_member(chat_id, user_id)
    if m.status in ("member", "administrator", "creator"):
        return True
    if m.status == "restricted":
        return bool(getattr(m, "can_send_messages", False))
    return False


def unmute_user(chat_id, user_id):
    """Снимает мут и ПРОВЕРЯЕТ через getChatMember, что юзер реально может писать."""
    bot.restrict_chat_member(
        chat_id, user_id,
        permissions=default_permissions(chat_id),
        use_independent_chat_permissions=True,
    )
    if night_active(chat_id):
        return True  # ночью писать нельзя всем, утром права вернутся сами
    ok = can_write(chat_id, user_id)
    if not ok:
        # запасной вариант: явно выдать полный набор прав
        bot.restrict_chat_member(
            chat_id, user_id,
            permissions=FULL_PERMS,
            use_independent_chat_permissions=True,
        )
        ok = can_write(chat_id, user_id)
    return ok


def parse_duration(s):
    s = s.lower().strip()
    try:
        if s.endswith(('м', 'm')):
            return int(s[:-1]) * 60
        if s.endswith(('ч', 'h')):
            return int(s[:-1]) * 3600
        if s.endswith(('д', 'd')):
            return int(s[:-1]) * 86400
        if s.isdigit():
            return int(s) * 60
    except ValueError:
        pass
    return None


# =====================================================================
# ПИНГ
# =====================================================================
@bot.message_handler(commands=['ping'])
def cmd_ping(message):
    start = time.time()
    msg = bot.reply_to(message, "🏓 Пингую...")
    ping = int((time.time() - start) * 1000)
    uptime = int(time.time() - bot.start_time)
    chats_count = len(db_execute("SELECT chat_id FROM settings"))

    try:
        ip_info = requests.get('http://ip-api.com/json/', timeout=5).json()
        server_loc = f"{ip_info.get('country', '?')} ({ip_info.get('city', '?')})"
    except Exception:
        server_loc = "Неизвестно"

    if speedtest is None:
        speed_text = "⚠️ speedtest не установлен"
    else:
        try:
            st = speedtest.Speedtest()
            st.get_best_server()
            down = round(st.download() / 1_000_000, 1)
            up = round(st.upload() / 1_000_000, 1)
            speed_text = f"📥 {down} Мбит/с | 📤 {up} Мбит/с"
        except Exception:
            speed_text = "⚠️ Не удалось замерить"

    text = (f"🛡 <b>DEFENDER ALBERTA v2.1.1</b> 🛡\n🟢 Статус: ONLINE\n📡 Пинг: {ping} ms\n"
            f"⏱ Аптайм: {uptime // 3600}ч {(uptime % 3600) // 60}м {uptime % 60}с\n"
            f"🌍 Сервер: {esc(server_loc)}\n🌐 Сеть: {speed_text}\n"
            f"🏠 Чатов под защитой: {chats_count}")
    safe_edit(msg.chat.id, msg.message_id, text, parse_mode="HTML")


# =====================================================================
# АДМИНЫ
# =====================================================================
@bot.message_handler(commands=['admin'])
def cmd_admin(message):
    if is_private(message):
        return
    if not is_admin(message):
        return bot.reply_to(message, "⛔ Только админы могут назначать админов!")

    target_id, target_name = None, None
    if message.reply_to_message:
        target_id = message.reply_to_message.from_user.id
        target_name = message.reply_to_message.from_user.first_name
    else:
        args = message.text.split()
        if len(args) > 1:
            target = args[1]
            try:
                if target.startswith('@'):
                    target_id = bot.get_chat(target).id
                    target_name = target
                else:
                    target_id = int(target)
                    target_name = f"ID: {target_id}"
            except Exception:
                return bot.reply_to(message, "❌ Не удалось найти юзера.")
        else:
            return bot.reply_to(message, "⚠️ Ответь на сообщение или напиши ID/юзернейм: /admin @user")

    db_execute("INSERT OR IGNORE INTO custom_admins (chat_id, user_id) VALUES (?, ?)", (message.chat.id, target_id))
    bot.reply_to(message, f"✅ {target_name} назначен админом в этом чате!")


# =====================================================================
# БАЗОВЫЕ КОМАНДЫ
# =====================================================================
@bot.message_handler(commands=['info'])
def cmd_info(message):
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    chat_id = message.chat.id
    w_count = db_execute("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (chat_id, target.id))
    w_count = w_count[0][0] if w_count else 0
    r_count = db_execute("SELECT rep FROM rep WHERE chat_id=? AND user_id=?", (chat_id, target.id))
    r_count = r_count[0][0] if r_count else 0

    if target.id in GLOBAL_ADMINS:
        status = "👑 Владелец"
    elif not is_private(message) and is_admin_id(chat_id, target.id):
        status = "⚡ Админ"
    else:
        status = "👤 Участник"

    username = f"@{esc(target.username)}" if target.username else "Нет"
    text = (f"👤 <b>АНКЕТА УЧАСТНИКА</b> 👤\n\n👤 {esc(target.first_name)}\n🆔 ID: <code>{target.id}</code>\n"
            f"🔗 Username: {username}\n"
            f"📌 Статус: {status}\n⚠️ Варны: {w_count}/3\n⭐ Репутация: {r_count}")
    bot.reply_to(message, text, parse_mode="HTML")


@bot.message_handler(commands=['id'])
def cmd_id(message):
    bot.reply_to(message, f"🆔 Твой ID: <code>{message.from_user.id}</code>\n💬 ID чата: <code>{message.chat.id}</code>", parse_mode="HTML")


# =====================================================================
# МОДЕРАЦИЯ
# =====================================================================
# --- Запоминаем юзеров, чтобы находить их по @username ---
def remember_user(u):
    if u:
        db_execute("INSERT OR REPLACE INTO user_cache (user_id, username, first_name) VALUES (?, ?, ?)", (u.id, u.username, u.first_name))


def cache_listener(messages):
    for m in messages:
        try:
            remember_user(m.from_user)
            if m.reply_to_message:
                remember_user(m.reply_to_message.from_user)
            for u in (m.new_chat_members or []):
                remember_user(u)
        except Exception as e:
            print(f"[CACHE] {e}")


bot.set_update_listener(cache_listener)


def display_name(uid, fallback):
    row = db_execute("SELECT username, first_name FROM user_cache WHERE user_id=?", (uid,))
    if row and row[0][0]:
        return f"@{row[0][0]}"
    return (row[0][1] if row and row[0][1] else None) or fallback


def resolve_target(message):
    """Цель команды: ответ на сообщение > упоминание > @username > ID. Возвращает (id, имя, остаток текста)."""
    parts = message.text.split(maxsplit=1)
    tail = parts[1] if len(parts) > 1 else ""
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        return u.id, u.first_name, tail
    toks = tail.split(maxsplit=1)
    if not toks:
        return None, None, ""
    first = toks[0]
    rest = toks[1] if len(toks) > 1 else ""
    for ent in (message.entities or []):
        if ent.type == "text_mention" and getattr(ent, "user", None):
            return ent.user.id, ent.user.first_name, rest
    if first.startswith('@'):
        row = db_execute("SELECT user_id, first_name FROM user_cache WHERE lower(username)=?", (first[1:].lower(),))
        if row:
            return row[0][0], row[0][1] or first, rest
        try:
            ch = bot.get_chat(first)
            if ch.type == 'private':
                return ch.id, ch.first_name or first, rest
        except Exception:
            pass
        return None, None, rest
    if first.isdigit() and len(first) >= 6:
        uid = int(first)
        row = db_execute("SELECT first_name FROM user_cache WHERE user_id=?", (uid,))
        return uid, (row[0][0] if row and row[0][0] else f"ID {uid}"), rest
    return None, None, tail


def get_target(message):
    """Возвращает (id, имя, остаток) или None (и сам отвечает ошибкой)."""
    uid, name, rest = resolve_target(message)
    if uid is None:
        args = message.text.split()
        if len(args) > 1 and args[1].startswith('@') and not message.reply_to_message:
            bot.reply_to(message, "❌ Не нашёл такого юзера. Он должен хоть раз написать в чат при мне, либо укажи его ID.")
        else:
            bot.reply_to(message, "⚠️ Ответь на сообщение или укажи @username / ID.")
        return None
    return uid, name, rest


@bot.message_handler(commands=['ban'])
def cmd_ban(message):
    if is_private(message) or not is_admin(message):
        return
    t = get_target(message)
    if not t:
        return
    uid, name, _ = t
    if is_admin_id(message.chat.id, uid):
        return bot.reply_to(message, "⚠️ Я не могу забанить администратора!")
    try:
        bot.ban_chat_member(message.chat.id, uid)
        bot.reply_to(message, f"🔨 {name} забанен.")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")


@bot.message_handler(commands=['unban'])
def cmd_unban(message):
    if is_private(message) or not is_admin(message):
        return
    t = get_target(message)
    if not t:
        return
    try:
        # only_if_banned: иначе unban выкинет из чата обычного участника
        bot.unban_chat_member(message.chat.id, t[0], only_if_banned=True)
        bot.reply_to(message, "✅ Разбанен.")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")


@bot.message_handler(commands=['kick'])
def cmd_kick(message):
    if is_private(message) or not is_admin(message):
        return
    t = get_target(message)
    if not t:
        return
    uid, name, _ = t
    if is_admin_id(message.chat.id, uid):
        return bot.reply_to(message, "⚠️ Я не могу кикнуть администратора!")
    try:
        bot.ban_chat_member(message.chat.id, uid)
        bot.unban_chat_member(message.chat.id, uid, only_if_banned=True)
        bot.reply_to(message, f"👢 {name} кикнут.")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")


@bot.message_handler(commands=['warn'])
def cmd_warn(message):
    if is_private(message) or not is_admin(message):
        return
    t = get_target(message)
    if not t:
        return
    user_id, name, rest = t
    chat_id = message.chat.id
    if is_admin_id(chat_id, user_id):
        return bot.reply_to(message, "⚠️ Я не могу выдать варн администратору!")
    reason = rest.strip() or "—"

    db_execute("INSERT OR IGNORE INTO warns (chat_id, user_id, count) VALUES (?, ?, 0)", (chat_id, user_id))
    db_execute("UPDATE warns SET count = count + 1 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    count = db_execute("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id))[0][0]

    username = display_name(user_id, name)

    warn_text = (f"⚠️ <b>ПРЕДУПРЕЖДЕНИЕ</b> ⚠️\n👤 {esc(username)}\n🆔 ID: <code>{user_id}</code>\n"
                 f"📋 Причина: {esc(reason)}\n🔢 Предупреждение: {count}/3")
    bot.reply_to(message, warn_text, parse_mode="HTML")

    if count >= 3:
        try:
            bot.ban_chat_member(chat_id, user_id)
            bot.send_message(chat_id, f"🔨 {username} получил 3/3 и был забанен.")
            db_execute("UPDATE warns SET count = 0 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        except Exception as e:
            bot.send_message(chat_id, f"❌ Не могу забанить: {e}")


@bot.message_handler(commands=['mute'])
def cmd_mute(message):
    if is_private(message) or not is_admin(message):
        return
    t = get_target(message)
    if not t:
        return
    uid, name, rest = t
    if is_admin_id(message.chat.id, uid):
        return bot.reply_to(message, "⚠️ Я не могу замутить администратора!")
    if not rest.strip():
        return bot.reply_to(message, "⚠️ Укажи время (/mute 15 или /mute @user 1ч).")

    dur = rest.split()[0]
    seconds = parse_duration(dur)
    if not seconds or seconds <= 0:
        return bot.reply_to(message, "⚠️ Укажи время (например: /mute 15 или /mute @user 1ч).")

    try:
        mute_user(message.chat.id, uid, seconds)
        bot.reply_to(message, f"🔇 Замучен на {dur}.")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")


@bot.message_handler(commands=['unmute'])
def cmd_unmute(message):
    if is_private(message) or not is_admin(message):
        return
    t = get_target(message)
    if not t:
        return
    uid, name, _ = t
    try:
        if unmute_user(message.chat.id, uid):
            bot.reply_to(message, f"🔊 {name} размучен.")
        else:
            bot.reply_to(message, f"⚠️ Запрос прошёл, но {name} всё ещё не может писать. Проверь права бота и разрешения чата.")
    except Exception as e:
        if "can't remove chat owner" in str(e):
            bot.reply_to(message, "⚠️ Это владелец чата. У него и так есть права.")
        else:
            bot.reply_to(message, f"❌ Ошибка: {e}")


# --- Стоп-слова ---
@bot.message_handler(commands=['addword'])
def cmd_addword(message):
    if is_private(message) or not is_admin(message):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return bot.reply_to(message, "⚠️ Использование: /addword слово")
    db_execute("INSERT OR IGNORE INTO stopwords (chat_id, word) VALUES (?, ?)", (message.chat.id, args[1].strip().lower()))
    bot.reply_to(message, "✅ Слово добавлено в стоп-лист.")


@bot.message_handler(commands=['delword'])
def cmd_delword(message):
    if is_private(message) or not is_admin(message):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return bot.reply_to(message, "⚠️ Использование: /delword слово")
    db_execute("DELETE FROM stopwords WHERE chat_id=? AND word=?", (message.chat.id, args[1].strip().lower()))
    bot.reply_to(message, "✅ Слово удалено из стоп-листа.")


@bot.message_handler(commands=['words'])
def cmd_words(message):
    if is_private(message) or not is_admin(message):
        return
    words = db_execute("SELECT word FROM stopwords WHERE chat_id=?", (message.chat.id,))
    bot.reply_to(message, "🤬 Стоп-слова: " + (", ".join(w[0] for w in words) if words else "список пуст"))


# =====================================================================
# ПАНЕЛЬ НАСТРОЕК
# =====================================================================
def settings_markup(chat_id):
    markup = InlineKeyboardMarkup()
    for k, n in zip(SETTING_KEYS, SETTING_NAMES):
        markup.add(InlineKeyboardButton(f"{n}: {'✅' if get_setting(chat_id, k) else '❌'}", callback_data=f"set_{chat_id}_{k}"))
    markup.add(InlineKeyboardButton("❌ Закрыть", callback_data="close_settings"))
    return markup


@bot.message_handler(commands=['settings'])
def cmd_settings(message):
    if message.from_user.id not in GLOBAL_ADMINS:
        return
    chat_id = message.chat.id
    if not db_execute("SELECT chat_id FROM settings WHERE chat_id=?", (chat_id,)):
        return bot.reply_to(message, "⚠️ Сначала активируй защиту: /grant")
    bot.reply_to(message, "⚙️ <b>ПАНЕЛЬ ЗАЩИТЫ</b>\nНажми на модуль для переключения.", parse_mode="HTML", reply_markup=settings_markup(chat_id))


@bot.callback_query_handler(func=lambda call: call.data.startswith('set_') or call.data == 'close_settings')
def cb_settings(call):
    if call.data == 'close_settings':
        safe_delete(call.message.chat.id, call.message.message_id)
        return
    if call.from_user.id not in GLOBAL_ADMINS:
        return bot.answer_callback_query(call.id, "⛔ Только создатель!", show_alert=True)

    parts = call.data.split('_')
    chat_id, key = int(parts[1]), parts[2]
    if key not in SETTING_KEYS:
        return bot.answer_callback_query(call.id)
    set_setting(chat_id, key, not get_setting(chat_id, key))
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=settings_markup(chat_id))
    except Exception:
        pass
    bot.answer_callback_query(call.id, "✅ Настройка сохранена")


@bot.message_handler(commands=['grant'])
def cmd_grant(message):
    if message.from_user.id not in GLOBAL_ADMINS or is_private(message):
        return
    db_execute("INSERT OR IGNORE INTO settings (chat_id) VALUES (?)", (message.chat.id,))
    bot.reply_to(message, "👑 Защита Alberta активирована в этом чате!")


# =====================================================================
# КАПЧА
# =====================================================================
CAPTCHA_TIMEOUT = 120
pending_captcha = {}  # (chat_id, user_id) -> message_id капчи
captcha_state = {}  # (chat_id, user_id) -> состояние текущего задания
CAPTCHA_ATTEMPTS = 3


def captcha_timeout(chat_id, user_id, msg_id, name):
    time.sleep(CAPTCHA_TIMEOUT)
    if pending_captcha.get((chat_id, user_id)) != msg_id:
        return  # уже прошёл капчу или перезашёл
    pending_captcha.pop((chat_id, user_id), None)
    captcha_state.pop((chat_id, user_id), None)
    safe_delete(chat_id, msg_id)
    try:
        bot.ban_chat_member(chat_id, user_id)
        bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        bot.send_message(chat_id, f"⌛ {name} не прошёл проверку за {CAPTCHA_TIMEOUT // 60} мин. и был кикнут.")
    except Exception as e:
        print(f"❌ Не удалось кикнуть {user_id} после таймаута капчи: {e}")


# ---------- КАПЧА 2.0: 5 типов заданий, 3 попытки, новое задание после ошибки ----------
FRUITS = ["🍎", "🍌", "🍇", "🍒", "🍓", "🍋", "🍉", "🥝"]
ANIMALS = {"🐱": "кота", "🐶": "собаку", "🐼": "панду", "🦊": "лису", "🐸": "лягушку",
           "🐵": "обезьяну", "🐧": "пингвина", "🦁": "льва", "🐰": "кролика"}


def gen_math():
    kind = random.choice(["+", "-", "*"])
    if kind == "+":
        a, b = random.randint(2, 15), random.randint(2, 15)
        ans, q = a + b, f"{a} + {b}"
    elif kind == "-":
        a = random.randint(8, 25)
        b = random.randint(1, a - 1)
        ans, q = a - b, f"{a} − {b}"
    else:
        a, b = random.randint(2, 9), random.randint(2, 6)
        ans, q = a * b, f"{a} × {b}"
    wrongs = set()
    while len(wrongs) < 3:
        w = ans + random.choice([-4, -3, -2, -1, 1, 2, 3, 4, 10, -10])
        if w != ans and w >= 0:
            wrongs.add(w)
    opts = list(wrongs) + [ans]
    random.shuffle(opts)
    return {"type": "math", "q": f"🧮 Сколько будет <b>{q}</b>?",
            "buttons": [(str(o), "ok" if o == ans else "fail") for o in opts], "width": 2}


def gen_animal():
    picks = random.sample(list(ANIMALS), 6)
    target = random.choice(picks)
    return {"type": "animal", "q": f"🐾 Нажми на <b>{ANIMALS[target]}</b>",
            "buttons": [(e, "ok" if e == target else "fail") for e in picks], "width": 3}


def gen_count():
    target = random.choice(FRUITS)
    others = [f for f in FRUITS if f != target]
    n = random.randint(2, 5)
    line = [target] * n + [random.choice(others) for _ in range(random.randint(3, 5))]
    random.shuffle(line)
    opts = {n}
    while len(opts) < 4:
        opts.add(random.randint(1, 9))
    opts = list(opts)
    random.shuffle(opts)
    return {"type": "count", "q": f"🔢 Сколько {target} в строке?\n\n{' '.join(line)}",
            "buttons": [(str(o), "ok" if o == n else "fail") for o in opts], "width": 4}


def gen_odd():
    a, b = random.sample(FRUITS, 2)
    items = [a] * 5 + [b]
    random.shuffle(items)
    return {"type": "odd", "q": "👀 Найди <b>лишний</b> смайлик",
            "buttons": [(e, "ok" if e == b else "fail") for e in items], "width": 3}


def gen_seq():
    digits = random.sample(range(1, 10), 6)
    seq = random.sample(digits, 3)
    return {"type": "seq", "q": f"👆 Нажми цифры по порядку: <b>{' → '.join(map(str, seq))}</b>",
            "buttons": [(str(d), f"seq{d}") for d in digits], "width": 3, "seq": seq}


CAPTCHA_TYPES = [gen_math, gen_animal, gen_count, gen_odd, gen_seq]


def build_captcha(chat_id, user_id, name, attempts_left, note=""):
    c = random.choice(CAPTCHA_TYPES)()
    markup = InlineKeyboardMarkup(row_width=c["width"])
    markup.add(*[InlineKeyboardButton(label, callback_data=f"cap_{key}_{chat_id}_{user_id}") for label, key in c["buttons"]])
    mention = f'<a href="tg://user?id={user_id}">{esc(name)}</a>'
    text = (f"🔒 <b>ПРОВЕРКА НА ЧЕЛОВЕКА</b>\n\n👤 {mention}, докажи, что ты не бот!\n\n{note}{c['q']}\n\n"
            f"❤️ Попыток: {attempts_left}  •  ⏳ {CAPTCHA_TIMEOUT // 60} мин.")
    state = {"type": c["type"], "seq": c.get("seq", []), "step": 0, "attempts_left": attempts_left}
    return text, markup, state


def send_captcha(chat_id, user_id, name):
    """Мутит юзера и выдаёт ему капчу. Возвращает True, если капча отправлена."""
    if not try_mute(chat_id, user_id):
        print("⚠️ Капча пропущена: бот не смог выдать мут (проверь права бота)")
        return False

    text, markup, state = build_captcha(chat_id, user_id, name, CAPTCHA_ATTEMPTS)
    try:
        sent = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        print(f"❌ Не удалось отправить капчу: {e}")
        return False
    pending_captcha[(chat_id, user_id)] = sent.message_id
    captcha_state[(chat_id, user_id)] = state
    threading.Thread(target=captcha_timeout, args=(chat_id, user_id, sent.message_id, esc(name)), daemon=True).start()
    return True


@bot.message_handler(commands=['proverka', 'проверка'])
def cmd_proverka(message):
    if is_private(message) or not is_admin(message):
        return
    t = get_target(message)
    if not t:
        return
    uid, name, _ = t
    chat_id = message.chat.id
    if is_admin_id(chat_id, uid):
        return bot.reply_to(message, "⚠️ Админа проверять не нужно!")
    if not send_captcha(chat_id, uid, name):
        bot.reply_to(message, "❌ Не удалось выдать капчу. Проверь, что бот админ с правом ограничивать участников и что юзер в чате.")


@bot.message_handler(content_types=['new_chat_members'])
def welcome_and_captcha(message):
    chat_id = message.chat.id
    for user in message.new_chat_members:
        db_execute("INSERT OR REPLACE INTO user_joins (chat_id, user_id, join_time) VALUES (?, ?, ?)", (chat_id, user.id, time.time()))
        if user.is_bot:
            continue
        if not get_setting(chat_id, "captcha"):
            continue

        send_captcha(chat_id, user.id, user.first_name)

    if get_setting(chat_id, "antidelete"):
        safe_delete(chat_id, message.message_id)


@bot.message_handler(content_types=['left_chat_member'])
def on_left_member(message):
    if get_setting(message.chat.id, "antidelete"):
        safe_delete(message.chat.id, message.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('cap_'))
def check_captcha(call):
    parts = call.data.split('_')
    action, chat_id, user_id = parts[1], int(parts[2]), int(parts[3])
    key = (chat_id, user_id)

    if call.from_user.id != user_id:
        return bot.answer_callback_query(call.id, "⛔ Это капча для другого пользователя!", show_alert=True)

    state = captcha_state.get(key)
    name = call.from_user.first_name

    # задание «нажми цифры по порядку»
    if action.startswith("seq"):
        if not state or state.get("type") != "seq":
            action = "fail"  # капча устарела (бот перезапускался) — выдадим новую
        else:
            digit = int(action[3:])
            if digit == state["seq"][state["step"]]:
                state["step"] += 1
                if state["step"] < len(state["seq"]):
                    return bot.answer_callback_query(call.id, f"✅ Верно! Дальше: {state['seq'][state['step']]}")
                action = "ok"
            else:
                action = "fail"

    # неверный ответ: минус попытка и новое задание
    if action != "ok":
        attempts_left = (state["attempts_left"] if state else CAPTCHA_ATTEMPTS) - 1
        if attempts_left <= 0:
            pending_captcha.pop(key, None)
            captcha_state.pop(key, None)
            safe_delete(chat_id, call.message.message_id)
            bot.answer_callback_query(call.id, "❌ Попытки закончились.", show_alert=True)
            try:
                bot.ban_chat_member(chat_id, user_id)
                bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
                bot.send_message(chat_id, f"🚫 {esc(name)} не прошёл проверку (исчерпал попытки) и был кикнут.", parse_mode="HTML")
            except Exception as e:
                print(f"❌ Не удалось кикнуть {user_id} после провала капчи: {e}")
            return
        if key not in pending_captcha:  # после перезапуска бота вернуть таймер
            pending_captcha[key] = call.message.message_id
            threading.Thread(target=captcha_timeout, args=(chat_id, user_id, call.message.message_id, esc(name)), daemon=True).start()
        text, markup, new_state = build_captcha(chat_id, user_id, name, attempts_left, note="❌ <b>Неверно!</b> Новое задание:\n\n")
        captcha_state[key] = new_state
        safe_edit(chat_id, call.message.message_id, text, reply_markup=markup, parse_mode="HTML")
        return bot.answer_callback_query(call.id, f"❌ Неправильно! Осталось попыток: {attempts_left}", show_alert=True)

    # правильный ответ
    try:
        ok = unmute_user(chat_id, user_id)
    except Exception as e:
        print(f"❌ ОШИБКА РАЗМУТА: {e}")
        ok = False

    if not ok:
        print(f"❌ {user_id} всё ещё в муте после капчи")
        return bot.answer_callback_query(call.id, "⚠️ Не удалось снять ограничения. Сообщи админу.", show_alert=True)

    print(f"✅ Реально размучен {user_id}")
    pending_captcha.pop(key, None)
    captcha_state.pop(key, None)
    bot.answer_callback_query(call.id, "✅ Проверка пройдена!")
    safe_delete(chat_id, call.message.message_id)

    safe_name = esc(name)
    try:
        custom = db_execute("SELECT text FROM welcome WHERE chat_id=?", (chat_id,)) if get_setting(chat_id, "greetings") else None
        if custom:
            bot.send_message(chat_id, esc(custom[0][0]).replace("{name}", safe_name), parse_mode="HTML")
        elif get_setting(chat_id, "greetings"):
            bot.send_message(chat_id, f"👋 <b>ДОБРО ПОЖАЛОВАТЬ</b>\n• {safe_name} присоединился к чату!\nЧитай правила и уважай участников 🤝", parse_mode="HTML")
        else:
            bot.send_message(chat_id, f"✅ <b>ПРОВЕРКА ПРОЙДЕНА</b>\n• {safe_name} — человек! Добро пожаловать 🎉", parse_mode="HTML")
    except Exception:
        pass


# =====================================================================
# ИГРЫ И КНБ
# =====================================================================
games_db = {}
GAME_IDLE = 60


def game_watchdog(chat_id, game):
    """Закрывает игру при бездействии. Не трогает чужие/новые игры."""
    while True:
        time.sleep(5)
        if games_db.get(chat_id) is not game:
            return
        if time.time() - game["ts"] > GAME_IDLE:
            games_db.pop(chat_id, None)
            if game["step"] == "waiting":
                safe_edit(chat_id, game["msg_id"], "⏳ Время вышло! Противник не ответил. Игра отменена.")
            else:
                safe_edit(chat_id, game["msg_id"], "⏳ Игра окончена из-за бездействия.")
            return


def prepare_game(message):
    if is_private(message) or not message.reply_to_message:
        bot.reply_to(message, "⚠️ Ответь на сообщение соперника!")
        return None
    chat_id, p1, p2 = message.chat.id, message.from_user, message.reply_to_message.from_user
    if p2.is_bot or p1.id == p2.id:
        bot.reply_to(message, "⚠️ Выбирай реального соперника.")
        return None
    if chat_id in games_db:
        bot.reply_to(message, "⏳ Игра уже идет.")
        return None
    return chat_id, p1, p2


def launch_game(message, chat_id, game, text, markup, parse_mode=None):
    game["ts"] = time.time()
    game["msg_id"] = None
    games_db[chat_id] = game
    threading.Thread(target=game_watchdog, args=(chat_id, game), daemon=True).start()
    try:
        msg = bot.reply_to(message, text, reply_markup=markup, parse_mode=parse_mode)
        game["msg_id"] = msg.message_id
    except Exception as e:
        print(f"❌ Не удалось начать игру: {e}")
        games_db.pop(chat_id, None)


@bot.message_handler(commands=['кубики', 'dice'])
def cmd_dice(message):
    prep = prepare_game(message)
    if not prep:
        return
    chat_id, p1, p2 = prep
    game = {"type": "dice", "p1": p1.id, "p2": p2.id, "p1_name": p1.first_name, "p2_name": p2.first_name, "step": "waiting"}
    markup = InlineKeyboardMarkup().row(InlineKeyboardButton("✅ Да, играю", callback_data="game_accept"), InlineKeyboardButton("❌ Нет, боюсь", callback_data="game_decline"))
    launch_game(message, chat_id, game, f"🎲 {p1.first_name} вызывает {p2.first_name}!\nПроигравший получает мут на 5 минут.\n\n{p2.first_name}, принимаешь вызов?", markup)


@bot.message_handler(commands=['рулетка', 'roulette'])
def cmd_roulette(message):
    prep = prepare_game(message)
    if not prep:
        return
    chat_id, p1, p2 = prep
    game = {"type": "roulette", "p1": p1.id, "p2": p2.id, "p1_name": p1.first_name, "p2_name": p2.first_name, "turn": p1.id, "step": "waiting"}
    markup = InlineKeyboardMarkup().row(InlineKeyboardButton("✅ Да, я в деле", callback_data="game_accept"), InlineKeyboardButton("❌ Нет, страшно", callback_data="game_decline"))
    launch_game(message, chat_id, game, f"🔫 {p1.first_name} вызывает {p2.first_name} на Русскую рулетку!\n1 патрон на 6 гнезд.\n\n{p2.first_name}, принимаешь вызов?", markup)


@bot.message_handler(commands=['дуэль', 'duel'])
def cmd_duel(message):
    prep = prepare_game(message)
    if not prep:
        return
    chat_id, p1, p2 = prep
    game = {"type": "duel", "p1": p1.id, "p2": p2.id, "p1_name": p1.first_name, "p2_name": p2.first_name, "hp1": 100, "hp2": 100, "step": "waiting"}
    markup = InlineKeyboardMarkup().row(InlineKeyboardButton("✅ Да, принимаю", callback_data="game_accept"), InlineKeyboardButton("❌ Нет, струсил", callback_data="game_decline"))
    launch_game(message, chat_id, game, f"⚔️ {p1.first_name} вызывает {p2.first_name} на дуэль!\nПроигравший — мут на 15 минут.\n\n{p2.first_name}, принимаешь вызов?", markup)


RPS_NAMES = {"rock": "✊ Камень", "scissors": "✌️ Ножницы", "paper": "✋ Бумага"}
RPS_EMOJI = {"rock": "✊", "scissors": "✌️", "paper": "✋"}


def rps_markup(num):
    return InlineKeyboardMarkup().row(
        InlineKeyboardButton("✊ Камень", callback_data=f"rps_rock_{num}"),
        InlineKeyboardButton("✌️ Ножницы", callback_data=f"rps_scissors_{num}"),
        InlineKeyboardButton("✋ Бумага", callback_data=f"rps_paper_{num}")
    )


@bot.message_handler(commands=['кнб', 'rps'])
def cmd_rps(message):
    prep = prepare_game(message)
    if not prep:
        return
    chat_id, p1, p2 = prep
    game = {"type": "rps", "p1": p1.id, "p2": p2.id, "p1_name": p1.first_name, "p2_name": p2.first_name,
            "p1_choice": None, "p2_choice": None, "step": "p1_choose"}
    text = (f"✊✌️✋ <b>КАМЕНЬ, НОЖНИЦЫ, БУМАГА!</b>\n\n{esc(p1.first_name)} вызывает {esc(p2.first_name)}!\n"
            f"Проигравший получит мут на 5 минут.\n\n👉 {esc(p1.first_name)}, делай свой выбор (его никто не увидит):")
    launch_game(message, chat_id, game, text, rps_markup(1), parse_mode="HTML")


def handle_rps(call, game):
    chat_id = call.message.chat.id
    if game["type"] != "rps":
        return bot.answer_callback_query(call.id, "Игра окончена или отменена.", show_alert=True)

    _, choice, num = call.data.split('_')
    num = int(num)
    if choice not in RPS_NAMES:
        return bot.answer_callback_query(call.id)

    if num == 1:
        if call.from_user.id != game["p1"]:
            return bot.answer_callback_query(call.id, "⛔ Это выбор первого игрока!", show_alert=True)
        if game["step"] != "p1_choose":
            return bot.answer_callback_query(call.id, "Ты уже сделал выбор.", show_alert=True)
        game["p1_choice"] = choice
        game["step"] = "p2_choose"
        bot.answer_callback_query(call.id, f"✅ Ты выбрал: {RPS_NAMES[choice]}. Передаем ход!", show_alert=True)
        safe_edit(chat_id, call.message.message_id,
                  f"✊✌️✋ <b>КАМЕНЬ, НОЖНИЦЫ, БУМАГА!</b>\n\n👉 {esc(game['p2_name'])}, теперь твой выбор! (Его никто не увидит):",
                  reply_markup=rps_markup(2), parse_mode="HTML")
        return

    # num == 2
    if call.from_user.id != game["p2"]:
        return bot.answer_callback_query(call.id, "⛔ Это выбор второго игрока!", show_alert=True)
    if game["step"] != "p2_choose":
        return bot.answer_callback_query(call.id, "Сейчас не твой ход.", show_alert=True)
    game["p2_choice"] = choice
    game["step"] = "done"
    bot.answer_callback_query(call.id, f"✅ Ты выбрал: {RPS_NAMES[choice]}. Считаем результат!", show_alert=True)

    c1, c2 = game["p1_choice"], game["p2_choice"]
    safe_edit(chat_id, call.message.message_id, "🤔 Считаем результат...")
    time.sleep(1)

    res_text = (f"✊✌️✋ <b>РЕЗУЛЬТАТ</b>\n\n{esc(game['p1_name'])}: {RPS_EMOJI[c1]}\n"
                f"{esc(game['p2_name'])}: {RPS_EMOJI[c2]}\n\n")

    if c1 == c2:
        res_text += "🤝 <b>НИЧЬЯ!</b> Никто не получает мут."
    else:
        p1_wins = (c1, c2) in (("rock", "scissors"), ("scissors", "paper"), ("paper", "rock"))
        if p1_wins:
            winner_name, loser_name, loser_id = game["p1_name"], game["p2_name"], game["p2"]
        else:
            winner_name, loser_name, loser_id = game["p2_name"], game["p1_name"], game["p1"]
        res_text += f"🏆 Победил <b>{esc(winner_name)}</b>!\n"
        if try_mute(chat_id, loser_id, 300):
            res_text += f"🔇 {esc(loser_name)} замучен на 5 минут."
        else:
            res_text += f"⚠️ Не удалось замутить {esc(loser_name)}."

    safe_edit(chat_id, call.message.message_id, res_text, parse_mode="HTML")
    games_db.pop(chat_id, None)


def handle_duel_hit(call, game):
    chat_id = call.message.chat.id
    if game["type"] != "duel" or game["step"] != "playing":
        return bot.answer_callback_query(call.id, "Игра окончена.", show_alert=True)
    who = int(call.data.split('_')[2])
    if who == 1 and call.from_user.id != game["p1"]:
        return bot.answer_callback_query(call.id, "⛔ Это кнопка первого!", show_alert=True)
    if who == 2 and call.from_user.id != game["p2"]:
        return bot.answer_callback_query(call.id, "⛔ Это кнопка второго!", show_alert=True)

    damage = random.randint(10, 25)
    if who == 1:
        game["hp2"] -= damage
    else:
        game["hp1"] -= damage
    bot.answer_callback_query(call.id, f"Урон: {damage}")

    if game["hp1"] <= 0 or game["hp2"] <= 0:
        game["step"] = "done"
        games_db.pop(chat_id, None)
        if game["hp1"] <= 0 and game["hp2"] <= 0:
            text = "🤝 Оба упали замертво! Ничья."
        else:
            loser_id = game["p1"] if game["hp1"] <= 0 else game["p2"]
            winner = game["p2_name"] if game["hp1"] <= 0 else game["p1_name"]
            text = f"☠️ Победил {winner}!"
            if try_mute(chat_id, loser_id, 900):
                text += "\n🤐 Проигравший замучен на 15 минут."
            else:
                text += "\n(Не смог замутить)"
        safe_edit(chat_id, call.message.message_id, text)
    else:
        text = f"⚔️ Удар на {damage}!\n\n❤️ {game['p1_name']}: {game['hp1']} HP\n❤️ {game['p2_name']}: {game['hp2']} HP"
        markup = InlineKeyboardMarkup().row(
            InlineKeyboardButton(f"⚔️ Атаковать ({game['p1_name']})", callback_data="duel_hit_1"),
            InlineKeyboardButton(f"⚔️ Атаковать ({game['p2_name']})", callback_data="duel_hit_2"))
        safe_edit(chat_id, call.message.message_id, text, reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith(('game_', 'duel_hit', 'rps_')))
def handle_game_callbacks(call):
    chat_id = call.message.chat.id
    game = games_db.get(chat_id)
    if not game:
        return bot.answer_callback_query(call.id, "Игра окончена или отменена.", show_alert=True)
    game["ts"] = time.time()

    if call.data.startswith('rps_'):
        return handle_rps(call, game)
    if call.data.startswith('duel_hit'):
        return handle_duel_hit(call, game)

    action = call.data.split('_', 1)[1]
    if action in ('accept', 'decline'):
        if call.from_user.id != game["p2"]:
            return bot.answer_callback_query(call.id, "⛔ Эти кнопки не для тебя! Мама не разрешала их трогать.", show_alert=True)
        if game["step"] != "waiting":
            return bot.answer_callback_query(call.id, "Игра уже началась.", show_alert=True)
        if action == 'decline':
            safe_edit(chat_id, call.message.message_id, f"❌ {game['p2_name']} струсил.")
            games_db.pop(chat_id, None)
            return
        game["step"] = "playing"
        bot.answer_callback_query(call.id)
        if game["type"] == "dice":
            start_dice_duel(chat_id, call.message.message_id, game)
        elif game["type"] == "roulette":
            start_roulette(chat_id, call.message.message_id, game)
        elif game["type"] == "duel":
            start_duel(chat_id, call.message.message_id, game)
    elif action == 'spin':
        if game["type"] != "roulette" or game["step"] != "playing":
            return bot.answer_callback_query(call.id, "Игра окончена.", show_alert=True)
        if call.from_user.id != game["turn"]:
            return bot.answer_callback_query(call.id, "⛔ Сейчас не твой ход!", show_alert=True)
        bot.answer_callback_query(call.id)
        spin_roulette(chat_id, call.message.message_id, game)


def start_dice_duel(chat_id, msg_id, game):
    try:
        safe_edit(chat_id, msg_id, "🎲 Дуэль началась! Бросаем кубики...")
        time.sleep(1)
        bot.send_message(chat_id, f"🎲 Бросает {game['p1_name']}...")
        msg1 = bot.send_dice(chat_id, emoji="🎲")
        time.sleep(4)
        bot.send_message(chat_id, f"🎲 Бросает {game['p2_name']}...")
        msg2 = bot.send_dice(chat_id, emoji="🎲")
        time.sleep(4)
        v1, v2 = msg1.dice.value, msg2.dice.value
        if v1 == v2:
            bot.send_message(chat_id, f"🤝 Ничья! {v1} : {v2}.")
            return
        loser_id = game["p2"] if v1 > v2 else game["p1"]
        winner, loser = (game["p1_name"], game["p2_name"]) if v1 > v2 else (game["p2_name"], game["p1_name"])
        mut_text = " (замучен на 5 минут)" if try_mute(chat_id, loser_id, 300) else " (не смог замутить)"
        bot.send_message(chat_id, f"🏆 Победил {winner} ({v1} против {v2})!\n💀 {loser} проиграл{mut_text}.")
    except Exception as e:
        print(f"❌ Ошибка в игре кубики: {e}")
    finally:
        games_db.pop(chat_id, None)


def start_roulette(chat_id, msg_id, game):
    markup = InlineKeyboardMarkup().row(InlineKeyboardButton("🔫 Крутить барабан", callback_data="game_spin"))
    try:
        sent = bot.send_message(chat_id, f"🔫 Рулетка началась!\nПервый крутит: {game['p1_name']}\n\nПуля — мут на 10 минут.", reply_markup=markup)
        game["msg_id"] = sent.message_id
    except Exception as e:
        print(f"❌ Ошибка старта рулетки: {e}")
        games_db.pop(chat_id, None)
        return
    safe_delete(chat_id, msg_id)


def spin_roulette(chat_id, msg_id, game):
    if random.randint(1, 6) == 1:
        loser_id = game["turn"]
        current = game["p1_name"] if game["turn"] == game["p1"] else game["p2_name"]
        game["step"] = "done"
        games_db.pop(chat_id, None)
        mut_text = "и получает мут на 10 минут." if try_mute(chat_id, loser_id, 600) else "(бот не смог замутить)."
        safe_edit(chat_id, msg_id, f"💥 БАМ! {current} поймал пулю. {mut_text}")
    else:
        game["turn"] = game["p2"] if game["turn"] == game["p1"] else game["p1"]
        next_p = game["p1_name"] if game["turn"] == game["p1"] else game["p2_name"]
        markup = InlineKeyboardMarkup().row(InlineKeyboardButton("🔫 Крутить барабан", callback_data="game_spin"))
        safe_edit(chat_id, msg_id, f"💨 Щелчок! Выжил.\nТеперь крутит: {next_p}", reply_markup=markup)


def start_duel(chat_id, msg_id, game):
    markup = InlineKeyboardMarkup().row(
        InlineKeyboardButton(f"⚔️ Атаковать ({game['p1_name']})", callback_data="duel_hit_1"),
        InlineKeyboardButton(f"⚔️ Атаковать ({game['p2_name']})", callback_data="duel_hit_2"))
    text = f"⚔️ ДУЭЛЬ!\n❤️ {game['p1_name']}: {game['hp1']} HP\n❤️ {game['p2_name']}: {game['hp2']} HP\n\nЖмите на свою кнопку!"
    safe_edit(chat_id, msg_id, text, reply_markup=markup)


# =====================================================================
# РП КОМАНДЫ
# =====================================================================
def is_married(chat_id, user_id):
    return bool(db_execute("SELECT 1 FROM marriages WHERE chat_id=? AND (user1_id=? OR user2_id=?)", (chat_id, user_id, user_id)))


@bot.message_handler(commands=['брак', 'marry'])
def cmd_marry(message):
    if is_private(message) or not message.reply_to_message:
        return bot.reply_to(message, "⚠️ Ответь на сообщение того, с кем хочешь вступить в брак!")
    chat_id, p1, p2 = message.chat.id, message.from_user, message.reply_to_message.from_user
    if p2.is_bot or p1.id == p2.id:
        return bot.reply_to(message, "🤦‍♂️ С ботом или самим собой в брак нельзя.")
    if is_married(chat_id, p1.id):
        return bot.reply_to(message, f"💔 {p1.first_name}, ты уже в браке! Сначала /развод")
    if is_married(chat_id, p2.id):
        return bot.reply_to(message, f"💔 {p2.first_name} уже занят(а)!")

    markup = InlineKeyboardMarkup().row(
        InlineKeyboardButton("💍 Да, согласен(на)", callback_data=f"marry_yes_{p1.id}_{p2.id}"),
        InlineKeyboardButton("💔 Нет, отказ", callback_data=f"marry_no_{p1.id}_{p2.id}")
    )
    bot.reply_to(message, f"💍 {p1.first_name} делает предложение руки и сердца {p2.first_name}!\n\n{p2.first_name}, ты согласен(на)?", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith('marry_'))
def handle_marry_callbacks(call):
    chat_id = call.message.chat.id
    parts = call.data.split('_')
    action, p1_id, p2_id = parts[1], int(parts[2]), int(parts[3])
    if call.from_user.id != p2_id:
        return bot.answer_callback_query(call.id, "⛔ Эй, это предложение не тебе! Мама не разрешала.", show_alert=True)

    if action == 'no':
        safe_edit(chat_id, call.message.message_id, f"💔 {call.from_user.first_name} отклонил(а) предложение. Жестоко...")
        return bot.answer_callback_query(call.id)

    if is_married(chat_id, p1_id) or is_married(chat_id, p2_id):
        safe_edit(chat_id, call.message.message_id, "💔 Кто-то из вас уже в браке. Свадьба отменена.")
        return bot.answer_callback_query(call.id)

    db_execute("INSERT OR IGNORE INTO marriages (chat_id, user1_id, user2_id) VALUES (?, ?, ?)", (chat_id, p1_id, p2_id))
    db_execute("INSERT OR IGNORE INTO marriages2 (chat_id, user1_id, user2_id) VALUES (?, ?, ?)", (chat_id, p1_id, p2_id))

    orig = call.message.reply_to_message
    p1_name = orig.from_user.first_name if orig and orig.from_user else "Партнёр"
    safe_edit(chat_id, call.message.message_id, f"💍 Свадьба! {p1_name} и {call.from_user.first_name} теперь в браке! 🎉\nСовет да любовь.")
    bot.answer_callback_query(call.id)


@bot.message_handler(commands=['развод', 'divorce'])
def cmd_divorce(message):
    if is_private(message):
        return
    chat_id, user_id = message.chat.id, message.from_user.id
    db_execute("DELETE FROM marriages WHERE chat_id=? AND (user1_id=? OR user2_id=?)", (chat_id, user_id, user_id))
    db_execute("DELETE FROM marriages2 WHERE chat_id=? AND (user1_id=? OR user2_id=?)", (chat_id, user_id, user_id))
    bot.reply_to(message, f"💔 {message.from_user.first_name} расторг(ла) брак. Теперь снова свободен(на).")


@bot.message_handler(commands=['поцеловать', 'kiss'])
def cmd_kiss(message):
    if is_private(message) or not message.reply_to_message:
        return bot.reply_to(message, "⚠️ Ответь на сообщение того, кого хочешь поцеловать!")
    p1, p2 = message.from_user, message.reply_to_message.from_user
    if p2.is_bot or p1.id == p2.id:
        return bot.reply_to(message, "🤦‍♂️ Себя или бота целовать не надо.")
    bot.reply_to(message, f"😘 {p1.first_name} нежно целует {p2.first_name} в щечку! 💋\nТак мило.")


def plural(n, f1, f2, f5):
    n = abs(n) % 100
    n1 = n % 10
    if 10 < n < 20:
        return f5
    if 1 < n1 < 5:
        return f2
    if n1 == 1:
        return f1
    return f5


@bot.message_handler(commands=['стаж', 'age'])
def cmd_age(message):
    if is_private(message):
        return
    chat_id = message.chat.id
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    res = db_execute("SELECT join_time FROM user_joins WHERE chat_id=? AND user_id=?", (chat_id, target.id))
    if not res:
        return bot.reply_to(message, f"🤷‍♂️ {target.first_name} еще не внесен в базу стажа.")

    delta = time.time() - res[0][0]
    days, hours, minutes = int(delta // 86400), int((delta % 86400) // 3600), int((delta % 3600) // 60)
    t_str = ""
    if days > 0:
        t_str += f"{days} {plural(days, 'день', 'дня', 'дней')} "
    if hours > 0 or days > 0:
        t_str += f"{hours} {plural(hours, 'час', 'часа', 'часов')} "
    if minutes > 0 or (days == 0 and hours == 0):
        t_str += f"{minutes} {plural(minutes, 'минута', 'минуты', 'минут')}"
    rank = "👴 Старик" if days >= 150 else "🔥 Старожил" if days >= 30 else "👤 Постоянный" if days >= 7 else "🐣 Новичок"
    bot.reply_to(message, f"📅 <b>СТАЖ В ЧАТЕ</b>\n\n👤 {esc(target.first_name)}\n🏅 Ранг: {rank}\n⏳ В чате уже: {t_str.strip()}", parse_mode="HTML")


# =====================================================================
# АВТО-ЗАЩИТА
# =====================================================================
user_msg_count = {}
last_texts = {}  # (chat_id, user_id) -> (текст, сколько раз подряд)
PROTECT_TYPES = ['text', 'photo', 'video', 'document', 'sticker', 'animation', 'voice', 'video_note', 'audio']


@bot.message_handler(func=lambda message: not is_private(message) and not is_admin(message), content_types=PROTECT_TYPES)
def handle_protection(message):
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0
    name = message.from_user.first_name if message.from_user else "Участник"
    text = message.text or message.caption or ""

    # Антиканал: сообщения от имени каналов
    sender_chat = getattr(message, "sender_chat", None)
    if (get_setting(chat_id, "antichannel") and sender_chat and sender_chat.id != chat_id
            and not getattr(message, "is_automatic_forward", False)):
        safe_delete(chat_id, message.message_id)
        return

    # Стоп-слова
    if text:
        words = db_execute("SELECT word FROM stopwords WHERE chat_id=?", (chat_id,))
        text_lower = text.lower()
        for w in words:
            if w[0] in text_lower:
                safe_delete(chat_id, message.message_id)
                try:
                    bot.send_message(chat_id, f"🤬 {name}, мат запрещен!")
                except Exception:
                    pass
                return

    # Антифлуд
    if get_setting(chat_id, "antiflood"):
        now = time.time()
        stamps = user_msg_count.setdefault(chat_id, {}).setdefault(user_id, [])
        stamps.append(now)
        stamps[:] = [t for t in stamps if now - t < 3]
        if len(stamps) > 5:
            safe_delete(chat_id, message.message_id)
            if try_mute(chat_id, user_id, 900):
                try:
                    bot.send_message(chat_id, f"🌊 {name} замучен за флуд на 15 минут.")
                except Exception:
                    pass
            stamps.clear()
            return

    # Анти-ссылки
    if get_setting(chat_id, "antilink") and text:
        low = text.lower()
        if "http" in low or "t.me/" in low or "www." in low:
            safe_delete(chat_id, message.message_id)
            try:
                bot.send_message(chat_id, f"🔗 {name}, ссылки запрещены!")
            except Exception:
                pass
            return

    # Антиповторы: 3 одинаковых сообщения подряд
    if get_setting(chat_id, "antirepeat") and text:
        key = (chat_id, user_id)
        prev = last_texts.get(key)
        count = prev[1] + 1 if prev and prev[0] == text else 1
        last_texts[key] = (text, count)
        if count >= 3:
            safe_delete(chat_id, message.message_id)
            return


# =====================================================================
# РЕПУТАЦИЯ
# =====================================================================
REP_COOLDOWN = 3600


def change_rep(message, delta):
    if is_private(message) or not message.reply_to_message:
        return bot.reply_to(message, "⚠️ Ответь на сообщение того, кому хочешь изменить репутацию!")
    chat_id = message.chat.id
    giver, target = message.from_user, message.reply_to_message.from_user
    if target.is_bot or giver.id == target.id:
        return bot.reply_to(message, "🤦‍♂️ Себе и ботам репутацию менять нельзя.")

    now = time.time()
    row = db_execute("SELECT last_time FROM rep_cooldown WHERE chat_id=? AND from_id=? AND to_id=?", (chat_id, giver.id, target.id))
    if row and now - row[0][0] < REP_COOLDOWN:
        left = int((REP_COOLDOWN - (now - row[0][0])) // 60) + 1
        return bot.reply_to(message, f"⏳ Ты уже менял репутацию этому участнику. Попробуй через {left} мин.")

    db_execute("INSERT OR REPLACE INTO rep_cooldown (chat_id, from_id, to_id, last_time) VALUES (?, ?, ?, ?)", (chat_id, giver.id, target.id, now))
    db_execute("INSERT OR IGNORE INTO rep (chat_id, user_id, rep) VALUES (?, ?, 0)", (chat_id, target.id))
    db_execute("UPDATE rep SET rep = rep + ? WHERE chat_id=? AND user_id=?", (delta, chat_id, target.id))
    total = db_execute("SELECT rep FROM rep WHERE chat_id=? AND user_id=?", (chat_id, target.id))[0][0]

    if delta > 0:
        bot.reply_to(message, f"⭐ {giver.first_name} повысил репутацию {target.first_name}!\n📈 Теперь: {total}")
    else:
        bot.reply_to(message, f"👎 {giver.first_name} понизил репутацию {target.first_name}.\n📉 Теперь: {total}")


@bot.message_handler(commands=['rep', 'реп'])
def cmd_rep(message):
    change_rep(message, 1)


@bot.message_handler(commands=['unrep', 'минусреп'])
def cmd_unrep(message):
    change_rep(message, -1)


@bot.message_handler(commands=['top', 'топ'])
def cmd_top(message):
    if is_private(message):
        return
    chat_id = message.chat.id
    rows = db_execute("SELECT user_id, rep FROM rep WHERE chat_id=? AND rep != 0 ORDER BY rep DESC LIMIT 10", (chat_id,))
    if not rows:
        return bot.reply_to(message, "🤷‍♂️ Пока ни у кого нет репутации. Используй /rep ответом на сообщение.")
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid, r) in enumerate(rows):
        try:
            name = bot.get_chat_member(chat_id, uid).user.first_name
        except Exception:
            name = f"ID {uid}"
        place = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{place} {esc(name)} — {r}")
    bot.reply_to(message, "🏆 <b>ТОП РЕПУТАЦИИ</b>\n\n" + "\n".join(lines), parse_mode="HTML")


# =====================================================================
# НОЧНОЙ РЕЖИМ (00:00-07:00 по Москве)
# =====================================================================
MSK = timezone(timedelta(hours=3))
NIGHT_START, NIGHT_END = 0, 7
_night_retry = {}


def is_night_now():
    return NIGHT_START <= datetime.now(MSK).hour < NIGHT_END


def night_active(chat_id):
    return bool(db_execute("SELECT 1 FROM night_state WHERE chat_id=?", (chat_id,)))


def night_on(chat_id):
    if time.time() - _night_retry.get(chat_id, 0) < 600:
        return
    try:
        p = bot.get_chat(chat_id).permissions
        saved = json.dumps(p.to_dict()) if p else "{}"
        bot.set_chat_permissions(chat_id, ChatPermissions(can_send_messages=False))
        db_execute("INSERT OR REPLACE INTO night_state (chat_id, perms) VALUES (?, ?)", (chat_id, saved))
        bot.send_message(chat_id, f"🌙 Ночной режим включён. Чат закрыт до {NIGHT_END:02d}:00 (МСК). Всем спокойной ночи! 😴")
    except Exception as e:
        _night_retry[chat_id] = time.time()
        print(f"❌ Ночной режим: не удалось закрыть чат {chat_id}: {e}")


def night_off(chat_id):
    if time.time() - _night_retry.get(chat_id, 0) < 600:
        return
    try:
        row = db_execute("SELECT perms FROM night_state WHERE chat_id=?", (chat_id,))
        saved = json.loads(row[0][0]) if row and row[0][0] else {}
        perms = ChatPermissions(**saved) if saved else FULL_PERMS
        bot.set_chat_permissions(chat_id, perms, use_independent_chat_permissions=True)
        db_execute("DELETE FROM night_state WHERE chat_id=?", (chat_id,))
        bot.send_message(chat_id, "☀️ Доброе утро! Ночной режим выключен, чат снова открыт.")
    except Exception as e:
        _night_retry[chat_id] = time.time()
        print(f"❌ Ночной режим: не удалось открыть чат {chat_id}: {e}")


def night_scheduler():
    while True:
        try:
            night = is_night_now()
            chats = {r[0] for r in db_execute("SELECT chat_id FROM settings WHERE nightmode=1")}
            chats |= {r[0] for r in db_execute("SELECT chat_id FROM night_state")}
            for chat_id in chats:
                want = night and get_setting(chat_id, "nightmode")
                active = night_active(chat_id)
                if want and not active:
                    night_on(chat_id)
                elif not want and active:
                    night_off(chat_id)
        except Exception as e:
            print(f"[NIGHT SCHEDULER] {e}")
        time.sleep(30)


@bot.message_handler(commands=['night', 'ночь'])
def cmd_night(message):
    if is_private(message):
        return
    chat_id = message.chat.id
    now = datetime.now(MSK).strftime("%H:%M")
    enabled = "✅ включён" if get_setting(chat_id, "nightmode") else "❌ выключен"
    state = "🌙 сейчас чат закрыт" if night_active(chat_id) else "☀️ сейчас чат открыт"
    bot.reply_to(message, f"🌙 Ночной режим: {enabled}\n🕒 Время (МСК): {now}\n🔒 Закрывается с {NIGHT_START:02d}:00 до {NIGHT_END:02d}:00\n{state}")


# =====================================================================
# ДОПОЛНИТЕЛЬНО: /unwarn и /help
# =====================================================================
@bot.message_handler(commands=['unwarn'])
def cmd_unwarn(message):
    if is_private(message) or not is_admin(message):
        return
    t = get_target(message)
    if not t:
        return
    uid, name, _ = t
    chat_id = message.chat.id
    row = db_execute("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (chat_id, uid))
    if not row or row[0][0] <= 0:
        return bot.reply_to(message, f"🤷‍♂️ У {name} нет предупреждений.")
    db_execute("UPDATE warns SET count = count - 1 WHERE chat_id=? AND user_id=?", (chat_id, uid))
    bot.reply_to(message, f"✅ Варн снят. У {name} теперь {row[0][0] - 1}/3.")


@bot.message_handler(commands=['help', 'start', 'помощь'])
def cmd_help(message):
    text = ("🛡 <b>DEFENDER ALBERTA — команды</b>\n\n"
            "<b>Для всех</b>\n"
            "/info, /id, /ping, /top, /night, /rules, /report, /admins, /stats\n"
            "/coin, /roll, /шар — развлечения\n"
            "/rep и /unrep — репутация (ответом на сообщение, раз в час на человека)\n"
            "/стаж, /брак, /развод, /поцеловать\n"
            "/кубики, /рулетка, /дуэль, /кнб — игры на мут\n\n"
            "<b>Для админов</b>\n"
            "/ban, /unban, /kick, /warn, /unwarn, /mute, /unmute, /proverka\n"
            "/addword, /delword, /words — стоп-слова\n"
            "/admin, /unadmin — назначить / снять админа бота\n"
            "/pin, /unpin, /del, /purge, /setrules, /setwelcome\n\n"
            "<b>Для создателя</b>\n"
            "/grant — включить защиту, /settings — панель")
    bot.reply_to(message, text, parse_mode="HTML")


# =====================================================================
# v2.1: /unadmin, /admins, /pin, /unpin, /del, /rules, /setrules, /report
# =====================================================================
@bot.message_handler(commands=['unadmin'])
def cmd_unadmin(message):
    if is_private(message) or not is_admin(message):
        return
    t = get_target(message)
    if not t:
        return
    uid, name, _ = t
    chat_id = message.chat.id
    if uid in GLOBAL_ADMINS:
        return bot.reply_to(message, "⛔ Создателя бота снять нельзя!")
    if db_execute("SELECT 1 FROM custom_admins WHERE chat_id=? AND user_id=?", (chat_id, uid)):
        db_execute("DELETE FROM custom_admins WHERE chat_id=? AND user_id=?", (chat_id, uid))
        return bot.reply_to(message, f"✅ {name} больше не админ бота в этом чате.")
    try:
        is_real = uid in chat_admin_ids(chat_id)
    except Exception:
        is_real = False
    if is_real:
        bot.reply_to(message, f"⚠️ {name} — настоящий админ чата. Я снимаю только тех, кого назначили через /admin. Убрать его можно в настройках группы.")
    else:
        bot.reply_to(message, f"🤷‍♂️ {name} и так не админ бота.")


@bot.message_handler(commands=['admins', 'админы'])
def cmd_admins(message):
    if is_private(message):
        return
    chat_id = message.chat.id
    lines, seen = [], set()
    try:
        for a in bot.get_chat_administrators(chat_id):
            if a.user.is_bot:
                continue
            seen.add(a.user.id)
            icon = "👑" if a.status == "creator" else "⚡"
            lines.append(f"{icon} {esc(a.user.first_name)}")
    except Exception as e:
        print(f"❌ /admins: {e}")
    for (uid,) in db_execute("SELECT user_id FROM custom_admins WHERE chat_id=?", (chat_id,)):
        if uid not in seen:
            lines.append(f"🔧 {esc(display_name(uid, f'ID {uid}'))} (админ бота)")
    if not lines:
        return bot.reply_to(message, "🤷‍♂️ Не удалось получить список админов.")
    bot.reply_to(message, "👮 <b>АДМИНИСТРАЦИЯ ЧАТА</b>\n\n" + "\n".join(lines), parse_mode="HTML")


@bot.message_handler(commands=['pin', 'закреп'])
def cmd_pin(message):
    if is_private(message) or not is_admin(message):
        return
    if not message.reply_to_message:
        return bot.reply_to(message, "⚠️ Ответь на сообщение, которое нужно закрепить.")
    try:
        bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id, disable_notification=True)
        bot.reply_to(message, "📌 Закреплено.")
    except Exception as e:
        err = str(e)
        if "service messages" in err:
            bot.reply_to(message, "⚠️ Это служебное сообщение (вход в чат, закрепление и т.п.), его закрепить нельзя. Ответь на обычное сообщение.")
        elif "not enough rights" in err or "CHAT_ADMIN_REQUIRED" in err:
            bot.reply_to(message, "⚠️ У бота нет права «Закреплять сообщения». Включи его в правах админа.")
        else:
            bot.reply_to(message, f"❌ Ошибка: {e}")


@bot.message_handler(commands=['unpin', 'открепить'])
def cmd_unpin(message):
    if is_private(message) or not is_admin(message):
        return
    try:
        if message.reply_to_message:
            bot.unpin_chat_message(message.chat.id, message_id=message.reply_to_message.message_id)
        else:
            bot.unpin_chat_message(message.chat.id)
        bot.reply_to(message, "📍 Откреплено.")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")


@bot.message_handler(commands=['del', 'удалить'])
def cmd_del(message):
    if is_private(message) or not is_admin(message):
        return
    if not message.reply_to_message:
        return bot.reply_to(message, "⚠️ Ответь на сообщение, которое нужно удалить.")
    safe_delete(message.chat.id, message.reply_to_message.message_id)
    safe_delete(message.chat.id, message.message_id)


@bot.message_handler(commands=['setrules', 'правила_задать'])
def cmd_setrules(message):
    if is_private(message) or not is_admin(message):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return bot.reply_to(message, "⚠️ Использование: /setrules текст правил")
    text = parts[1].strip()[:3000]
    db_execute("INSERT OR REPLACE INTO rules (chat_id, text) VALUES (?, ?)", (message.chat.id, text))
    bot.reply_to(message, "✅ Правила чата сохранены. Посмотреть: /rules")


@bot.message_handler(commands=['rules', 'правила'])
def cmd_rules(message):
    if is_private(message):
        return
    row = db_execute("SELECT text FROM rules WHERE chat_id=?", (message.chat.id,))
    if not row:
        return bot.reply_to(message, "📜 Правила ещё не заданы. Админ может задать: /setrules текст")
    bot.reply_to(message, f"📜 <b>ПРАВИЛА ЧАТА</b>\n\n{esc(row[0][0])}", parse_mode="HTML")


_last_report = {}


@bot.message_handler(commands=['report', 'репорт'])
def cmd_report(message):
    if is_private(message):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        return bot.reply_to(message, "⚠️ Ответь на сообщение нарушителя!")
    chat_id = message.chat.id
    giver, target = message.from_user, message.reply_to_message.from_user
    if giver.id == target.id:
        return bot.reply_to(message, "🤦‍♂️ На себя жаловаться нельзя.")
    now = time.time()
    if now - _last_report.get(giver.id, 0) < 60:
        return bot.reply_to(message, "⏳ Не так быстро. Жалобу можно отправлять раз в минуту.")
    _last_report[giver.id] = now

    parts = message.text.split(maxsplit=1)
    reason = parts[1].strip() if len(parts) > 1 else "—"
    mentions = ""
    try:
        for a in bot.get_chat_administrators(chat_id):
            if not a.user.is_bot:
                mentions += f'<a href="tg://user?id={a.user.id}">&#8203;</a>'
    except Exception:
        pass
    link = ""
    if str(chat_id).startswith("-100"):
        link = f"\n🔗 https://t.me/c/{str(chat_id)[4:]}/{message.reply_to_message.message_id}"
    bot.send_message(
        chat_id,
        f"🚨 <b>ЖАЛОБА</b>\n👤 На: {user_link(target)}\n✍️ От: {user_link(giver)}\n📋 Причина: {esc(reason)}{link}{mentions}",
        parse_mode="HTML")
    safe_delete(chat_id, message.message_id)


# =====================================================================
# v2.2: /stats, /setwelcome, /purge, /coin, /roll, /8ball
# =====================================================================
@bot.message_handler(commands=['stats', 'стат'])
def cmd_stats(message):
    if is_private(message):
        return
    chat_id = message.chat.id
    try:
        members = bot.get_chat_member_count(chat_id)
    except Exception:
        members = "?"
    try:
        admins = len([a for a in bot.get_chat_administrators(chat_id) if not a.user.is_bot])
    except Exception:
        admins = "?"
    warned = db_execute("SELECT COUNT(*) FROM warns WHERE chat_id=? AND count>0", (chat_id,))[0][0]
    pairs = db_execute("SELECT COUNT(*) FROM marriages WHERE chat_id=?", (chat_id,))[0][0]
    words = db_execute("SELECT COUNT(*) FROM stopwords WHERE chat_id=?", (chat_id,))[0][0]
    reps = db_execute("SELECT COUNT(*) FROM rep WHERE chat_id=? AND rep != 0", (chat_id,))[0][0]
    on = [n for k, n in zip(SETTING_KEYS, SETTING_NAMES) if get_setting(chat_id, k)]
    modules = ", ".join(on) if on else "ничего не включено"
    bot.reply_to(message,
                 f"📊 СТАТИСТИКА ЧАТА\n\n👥 Участников: {members}\n⚡ Админов: {admins}\n"
                 f"⚠️ С предупреждениями: {warned}\n⭐ С репутацией: {reps}\n💍 Пар: {pairs}\n"
                 f"🤬 Стоп-слов: {words}\n\n🛡 Включено: {modules}")


@bot.message_handler(commands=['setwelcome'])
def cmd_setwelcome(message):
    if is_private(message) or not is_admin(message):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return bot.reply_to(message, "⚠️ Использование: /setwelcome текст ({name} — имя новичка)\nСбросить: /setwelcome сброс")
    text = parts[1].strip()
    if text.lower() in ("сброс", "reset", "off"):
        db_execute("DELETE FROM welcome WHERE chat_id=?", (message.chat.id,))
        return bot.reply_to(message, "✅ Приветствие сброшено на стандартное.")
    db_execute("INSERT OR REPLACE INTO welcome (chat_id, text) VALUES (?, ?)", (message.chat.id, text[:1500]))
    bot.reply_to(message, "✅ Приветствие сохранено. Оно показывается после капчи, если включены «Приветствия» в /settings.")


@bot.message_handler(commands=['purge', 'чистка'])
def cmd_purge(message):
    if is_private(message) or not is_admin(message):
        return
    if not message.reply_to_message:
        return bot.reply_to(message, "⚠️ Ответь на сообщение, с которого нужно начать чистку.")
    chat_id = message.chat.id
    ids = list(range(message.reply_to_message.message_id, message.message_id + 1))
    if len(ids) > 100:
        return bot.reply_to(message, "⚠️ За раз можно удалить максимум 100 сообщений.")
    try:
        bot.delete_messages(chat_id, ids)
    except Exception:
        for i in ids:
            safe_delete(chat_id, i)
    try:
        sent = bot.send_message(chat_id, f"🧹 Чистка выполнена (до {len(ids)} сообщений).")
        threading.Timer(5, safe_delete, args=(chat_id, sent.message_id)).start()
    except Exception:
        pass


@bot.message_handler(commands=['coin', 'монетка'])
def cmd_coin(message):
    bot.reply_to(message, "🪙 " + random.choice(["Орёл!", "Решка!"]))


@bot.message_handler(commands=['roll', 'рандом'])
def cmd_roll(message):
    args = message.text.split()
    hi = 100
    if len(args) > 1 and args[1].isdigit() and int(args[1]) >= 2:
        hi = min(int(args[1]), 1_000_000)
    bot.reply_to(message, f"🎲 Выпало: {random.randint(1, hi)} (из {hi})")


EIGHT_BALL = ["✅ Да, определённо", "✅ Без сомнений", "✅ Скорее всего да", "🤔 Пока неясно, спроси позже",
              "🤔 Лучше не рассказывать", "❌ Даже не думай", "❌ Мой ответ — нет", "❌ Очень сомнительно"]


@bot.message_handler(commands=['8ball', 'шар'])
def cmd_8ball(message):
    if len(message.text.split()) < 2:
        return bot.reply_to(message, "🔮 Задай вопрос: /шар вопрос")
    bot.reply_to(message, "🔮 " + random.choice(EIGHT_BALL))


# =====================================================================
# ЗАПУСК
# =====================================================================
print("🛡 DEFENDER ALBERTA v2.1.1 запущен!")


def safe_polling():
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5, skip_pending=True)
        except Exception as e:
            print(f"[POLLING CRASH] {e}")
            time.sleep(5)


if __name__ == '__main__':
    threading.Thread(target=night_scheduler, daemon=True).start()
    safe_polling()
