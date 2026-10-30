import asyncio
import random
import sqlite3
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from config import BOT_TOKEN

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

ACTIVE_GAMES = {}

START_CHIPS   = 100
BET           = 10
DICE_FACES    = ["", "⚀", "⚁", "⚂", "⚃", "⚄", "⚅"]   # индекс 1–6
DAILY_REWARDS = [50, 75, 100, 125, 150, 175, 250]          # дни 1–7

VARIANT_NAMES = {
    "kessel": "Кессель-Сабакк",
    "spike":  "Корелльский Спайк",
}

# ═══════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ═══════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect("sabacc.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id       INTEGER PRIMARY KEY,
            username      TEXT,
            chips         INTEGER DEFAULT 100,
            last_daily    TEXT,
            daily_streak  INTEGER DEFAULT 0
        )
    """)
    # Миграция: добавить столбцы, если их ещё нет
    for col, ctype in [("last_daily", "TEXT"), ("daily_streak", "INTEGER DEFAULT 0")]:
        try:
            c.execute(f"ALTER TABLE users ADD COLUMN {col} {ctype}")
        except Exception:
            pass
    conn.commit()
    conn.close()

def get_or_create_user(user_id, username="Игрок"):
    conn = sqlite3.connect("sabacc.db")
    c = conn.cursor()
    c.execute("SELECT chips FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if row is None:
        c.execute("INSERT INTO users (user_id, username, chips) VALUES (?, ?, ?)",
                  (user_id, username, START_CHIPS))
        conn.commit()
        chips = START_CHIPS
    else:
        chips = row[0]
        c.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
        conn.commit()
    conn.close()
    return chips

def get_balance(user_id, username="Игрок"):
    return get_or_create_user(user_id, username)

def update_balance(user_id, amount):
    conn = sqlite3.connect("sabacc.db")
    c = conn.cursor()
    c.execute("UPDATE users SET chips = chips + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()

def check_daily(user_id):
    """Возвращает (can_claim, reward, streak, hours_left)."""
    conn = sqlite3.connect("sabacc.db")
    c = conn.cursor()
    c.execute("SELECT last_daily, daily_streak FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()

    if not row or row[0] is None:
        return True, DAILY_REWARDS[0], 1, 0

    last_str, streak = row
    streak = streak or 0
    last_dt = datetime.fromisoformat(last_str)
    now     = datetime.now()
    diff    = now - last_dt

    if diff.total_seconds() < 86400:          # < 24 ч — уже забрал
        hours_left = int((86400 - diff.total_seconds()) / 3600) + 1
        return False, 0, streak, hours_left
    elif diff.total_seconds() < 172800:        # 24–48 ч — серия продолжается
        new_streak = streak + 1
        idx = min(new_streak - 1, len(DAILY_REWARDS) - 1)
        return True, DAILY_REWARDS[idx], new_streak, 0
    else:                                      # > 48 ч — серия сброшена
        return True, DAILY_REWARDS[0], 1, 0

def claim_daily(user_id, username="Игрок"):
    """Забрать бонус. Возвращает (reward, streak) или (0, 0)."""
    can, reward, streak, _ = check_daily(user_id)
    if not can:
        return 0, 0
    get_or_create_user(user_id, username)
    conn = sqlite3.connect("sabacc.db")
    c = conn.cursor()
    c.execute(
        "UPDATE users SET chips = chips + ?, last_daily = ?, daily_streak = ? WHERE user_id = ?",
        (reward, datetime.now().isoformat(), streak, user_id)
    )
    conn.commit()
    conn.close()
    return reward, streak

def transfer_chips(from_id, to_id, amount):
    """Перевод кредитов. Возвращает True при успехе."""
    conn = sqlite3.connect("sabacc.db")
    c = conn.cursor()
    c.execute("SELECT chips FROM users WHERE user_id = ?", (from_id,))
    row = c.fetchone()
    if not row or row[0] < amount:
        conn.close()
        return False
    c.execute("UPDATE users SET chips = chips - ? WHERE user_id = ?", (amount, from_id))
    c.execute("UPDATE users SET chips = chips + ? WHERE user_id = ?", (amount, to_id))
    conn.commit()
    conn.close()
    return True

init_db()

# ═══════════════════════════════════════════════════════════
#  КАРТЫ И ЛОГИКА
# ═══════════════════════════════════════════════════════════

def draw_kessel():
    return 0 if random.random() < 0.08 else random.randint(1, 6)

def draw_spike():
    if random.random() < 0.08:
        return 0
    v = random.randint(1, 6)
    return v if random.random() < 0.5 else -v

def kessel_rank(cards):
    s, b = cards[0], cards[1]
    if s == 0 and b == 0: return (0, 0)
    if s == 0:             return (1, b)
    if b == 0:             return (1, s)
    if s == b:             return (2, s)
    return (3, s + b)

def kessel_str(cards):
    s, b   = cards[0], cards[1]
    ss     = "✨Самозванец" if s == 0 else str(s)
    bs     = "✨Самозванец" if b == 0 else str(b)
    r, v   = kessel_rank(cards)
    if r == 0:   desc = "🌟 ЧИСТЫЙ САБАКК!"
    elif r == 1: desc = f"✨ Сабакк (Самозванец={v})"
    elif r == 2: desc = f"🃏 Пара {v}"
    else:        desc = f"Без пары (сумма {s+b})"
    return f"🟨{ss} 🟥{bs} — {desc}"

def spike_rank(cards):
    return abs(sum(cards))

def spike_str(cards):
    c1 = "✨Сильп" if cards[0] == 0 else f"{cards[0]:+d}"
    c2 = "✨Сильп" if cards[1] == 0 else f"{cards[1]:+d}"
    t  = sum(cards)
    if t == 0:        desc = "🌟 САБАКК! (= 0)"
    elif abs(t) <= 2: desc = f"Почти! (= {t:+d})"
    else:             desc = f"Сумма {t:+d}"
    return f"[{c1}] [{c2}] — {desc}"

# ═══════════════════════════════════════════════════════════
#  🎲 КУБИКИ (без анимации — результат прямо в сообщении)
# ═══════════════════════════════════════════════════════════

def roll_kessel_imposter(cards):
    """Бросаем кубики для Самозванца. Без async — результат inline."""
    if 0 not in cards:
        return cards, ""
    d1, d2 = random.randint(1, 6), random.randint(1, 6)
    best_rank = best_val = best_cards = None
    for die_val in [d1, d2]:
        test = [die_val if c == 0 else c for c in cards]
        r    = kessel_rank(test)
        if best_rank is None or r < best_rank:
            best_rank, best_val, best_cards = r, die_val, test
    text = (
        f"🎲 <b>Самозванец!</b> {DICE_FACES[d1]}{DICE_FACES[d2]}"
        f" → выбрано <b>{best_val}</b>\n"
    )
    return best_cards, text

def roll_shift_dice(game):
    """Бросаем два кубика для Сдвига Сабакка. Возвращает (d1, d2, shifted)."""
    d1, d2 = random.randint(1, 6), random.randint(1, 6)
    if d1 == d2:
        draw = draw_spike
        for p_id in game["players_ids"]:
            game["cards"][p_id] = [draw(), draw()]
        if game.get("is_solo"):
            game["dealer_cards"] = [draw(), draw()]
        return d1, d2, True
    return d1, d2, False

def get_shift_text(d1, d2, shifted):
    if shifted:
        return (
            f"\n🎲 {DICE_FACES[d1]}{DICE_FACES[d2]} — "
            f"<b>ДУБЛЬ! СДВИГ САБАККА!</b> 🌀 Все карты на столе перемешаны!"
        )
    return f"\n🎲 {DICE_FACES[d1]}{DICE_FACES[d2]} — сдвига нет."

# ═══════════════════════════════════════════════════════════
#  🤖 УМНЫЙ ДИЛЕР
# ═══════════════════════════════════════════════════════════

def smart_dealer_action(variant, dealer_cards):
    if variant == "kessel":
        s, b  = dealer_cards[0], dealer_cards[1]
        r, v  = kessel_rank(dealer_cards)
        if r == 0: return "pass"                                   # Чистый сабакк
        if r == 1: return "pass"                                   # Сильп-пара (всегда выигрывает)
        if r == 2:
            if v <= 2:   return "pass"                             # Маленькая пара — держать
            elif v <= 4: return "pass" if random.random() < 0.7 else random.choice(["sand", "blood"])
            else:        return "pass" if random.random() < 0.4 else random.choice(["sand", "blood"])
        # Нет пары — заменяем карту, которая не совпадает
        if s == 0 or b == 0: return "pass"                        # Самозванец = всегда пара
        return "sand" if s > b else "blood"                        # Меняем бо́льшую

    elif variant == "spike":
        total  = sum(dealer_cards)
        abs_t  = abs(total)
        c1, c2 = dealer_cards[0], dealer_cards[1]

        if abs_t == 0: return "pass"                               # Сабакк!
        if abs_t <= 1: return "pass"                               # Практически сабакк
        if abs_t <= 3: return "pass" if random.random() < 0.65 else random.choice(["sand", "blood"])

        # Далеко от 0 — убираем карту, которая сильнее отдаляет
        # Убираем карту, у которой бо́льший вклад в отклонение
        contrib1 = abs(total - c2)   # остаток без c1
        contrib2 = abs(total - c1)   # остаток без c2
        if contrib1 < contrib2:
            return "sand"            # без c1 лучше → убираем c1
        elif contrib2 < contrib1:
            return "blood"
        else:
            return random.choice(["sand", "blood"])

    return "pass"

# ═══════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ═══════════════════════════════════════════════════════════

def kb_choose_variant(prefix):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Кессель-Сабакк",    callback_data=f"choose_{prefix}_kessel")],
        [InlineKeyboardButton(text="🎲 Корелльский Спайк", callback_data=f"choose_{prefix}_spike")],
    ])

def kb_join():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🃏 Сесть за стол", callback_data="table_join"),
        InlineKeyboardButton(text="🚀 Начать!",        callback_data="table_start"),
    ]])

def kb_game(variant):
    if variant == "kessel":
        r1 = InlineKeyboardButton(text="🟨 Заменить Жёлтую",  callback_data="game_action_sand")
        r2 = InlineKeyboardButton(text="🟥 Заменить Красную", callback_data="game_action_blood")
    else:
        r1 = InlineKeyboardButton(text="🎴 Заменить карту 1", callback_data="game_action_sand")
        r2 = InlineKeyboardButton(text="🎴 Заменить карту 2", callback_data="game_action_blood")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👁 Мои карты (тайно)",   callback_data="game_show_cards")],
        [r1, r2],
        [InlineKeyboardButton(text="⏸ Пас",                  callback_data="game_action_pass")],
        [InlineKeyboardButton(text="🏁 Вскрыться!",           callback_data="game_action_finish")],
    ])

def round_bar(game):
    rn  = game["round_number"]
    mr  = game["min_rounds"]
    vn  = VARIANT_NAMES.get(game["variant"], "Сабакк")
    ok  = "✅ Можно вскрываться!" if rn >= mr else f"⏳ Вскрытие с раунда {mr}"
    return f"🃏 {vn} · Раунд {rn} · {ok}"

# ═══════════════════════════════════════════════════════════
#  КОМАНДЫ
# ═══════════════════════════════════════════════════════════

@dp.message(Command("start"))
async def cmd_start(message: Message):
    uid   = message.from_user.id
    chips = get_balance(uid, message.from_user.first_name)
    can_claim, reward, streak, hours_left = check_daily(uid)

    if can_claim:
        daily_hint = f"\n🎁 <b>Доступен ежедневный бонус!</b> → /daily ({reward} кредитов)"
    else:
        daily_hint = f"\n⏰ Следующий бонус через <b>{hours_left} ч.</b>"

    await message.answer(
        "🪐 <b>Кантина Мос-Эйсли приветствует тебя!</b>\n\n"
        f"💰 Твой капитал: <b>{chips} кредитов</b>"
        f"{daily_hint}\n\n"
        "📋 /rules — Правила игры\n"
        "🎮 /sabacc — Играть в ЛС (против Дилера)\n"
        "👥 /sabacc_table — Играть с друзьями (в беседе)\n"
        "🎁 /daily — Ежедневный бонус\n"
        "💸 /send [сумма] — Отправить кредиты (ответом)\n"
        "💵 /balance — Мой баланс",
        parse_mode="HTML"
    )

@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    chips = get_balance(message.from_user.id, message.from_user.first_name)
    await message.answer(
        f"💰 {message.from_user.first_name}: <b>{chips} кредитов</b>",
        parse_mode="HTML"
    )

@dp.message(Command("daily"))
async def cmd_daily(message: Message):
    uid      = message.from_user.id
    username = message.from_user.first_name
    get_or_create_user(uid, username)

    can, reward, streak, hours_left = check_daily(uid)
    if not can:
        await message.answer(
            f"⏰ Ты уже забрал бонус сегодня!\n"
            f"Следующий через <b>{hours_left} ч.</b>\n\n"
            f"🔥 Серия: <b>{streak} дн.</b>",
            parse_mode="HTML"
        )
        return

    actual_reward, actual_streak = claim_daily(uid, username)
    balance = get_balance(uid)

    streak_bar = " ".join("🌟" if i < actual_streak else "⭐" for i in range(min(actual_streak, 7)))
    is_week    = actual_streak >= 7 and actual_streak % 7 == 0

    next_idx   = min(actual_streak, len(DAILY_REWARDS) - 1)
    next_bonus = DAILY_REWARDS[next_idx]

    await message.answer(
        f"{'🎉 СУПЕРСЕРИЯ! ' if is_week else ''}🎁 <b>Ежедневный бонус!</b>\n\n"
        f"+<b>{actual_reward} кредитов</b>\n"
        f"💰 Баланс: <b>{balance} кредитов</b>\n\n"
        f"🔥 Серия: <b>{actual_streak} дн.</b> {streak_bar}\n"
        f"Завтра: <b>+{next_bonus} кредитов</b> — не пропусти!",
        parse_mode="HTML"
    )

@dp.message(Command("send"))
async def cmd_send(message: Message):
    from_id   = message.from_user.id
    from_name = message.from_user.first_name

    if not message.reply_to_message:
        await message.answer(
            "💸 <b>Как перевести кредиты:</b>\n\n"
            "Ответьте на сообщение игрока и напишите:\n"
            "<code>/send 50</code>\n\n"
            "Пример: нажмите «Ответить» на сообщение друга → /send 100",
            parse_mode="HTML"
        )
        return

    to_user = message.reply_to_message.from_user
    if to_user.id == from_id:
        await message.answer("❌ Нельзя отправить кредиты самому себе!")
        return
    if to_user.is_bot:
        await message.answer("❌ Нельзя отправить кредиты боту!")
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("❌ Укажите сумму: <code>/send 50</code>", parse_mode="HTML")
        return

    amount = int(args[1])
    if amount <= 0:
        await message.answer("❌ Сумма должна быть больше 0!")
        return
    if amount > 10000:
        await message.answer("❌ Максимальный перевод: 10 000 кредитов за раз.")
        return

    to_name = to_user.first_name
    get_or_create_user(from_id, from_name)
    get_or_create_user(to_user.id, to_name)

    if transfer_chips(from_id, to_user.id, amount):
        from_balance = get_balance(from_id)
        await message.answer(
            f"💸 <b>{from_name}</b> → <b>{to_name}</b>\n"
            f"+<b>{amount} кредитов</b> переведено!\n\n"
            f"💰 Твой баланс: <b>{from_balance} кредитов</b>",
            parse_mode="HTML"
        )
    else:
        balance = get_balance(from_id)
        await message.answer(
            f"❌ Недостаточно кредитов!\n"
            f"💰 У тебя: <b>{balance} кредитов</b>",
            parse_mode="HTML"
        )

@dp.message(Command("rules"))
async def cmd_rules(message: Message):
    await message.answer(
        "🃏 <b>ПРАВИЛА САБАККА</b>\n\n"

        "━━━━━━━━━━━━━━━━━━━\n"
        "🎯 <b>КЕССЕЛЬ-САБАКК</b>\n"
        "<i>Как в Star Wars Outlaws</i>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "Две карты: 🟨 Жёлтая и 🟥 Красная (значения 1–6).\n"
        "Особая карта — <b>✨ Самозванец</b>: не имеет значения до вскрытия!\n"
        "При вскрытии с Самозванцем — бросаются два кубика, выбирается лучший результат.\n\n"
        "<b>Цель:</b> собрать пару (одинаковые значения).\n"
        "Победа — у кого пара с меньшим числом.\n"
        "Нет пар? Меньшая сумма — победитель.\n\n"
        "🌟 Лучшая рука — <b>Чистый Сабакк</b>: два Самозванца\n"
        "⏱ Минимум <b>2 полных раунда</b> перед вскрытием.\n\n"

        "━━━━━━━━━━━━━━━━━━━\n"
        "🎲 <b>КОРЕЛЛЬСКИЙ СПАЙК</b>\n"
        "<i>Как в фильме «Хан Соло» (на Нумидии)</i>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "Две карты со значениями от <b>-6 до +6</b>.\n"
        "<b>Цель:</b> сумма карт как можно ближе к <b>нулю</b>.\n"
        "<b>Сильп (0)</b> — нейтральная карта.\n\n"
        "🎲 <b>Сдвиг Сабакка — бросок кубиков после каждого раунда!</b>\n"
        "В оригинале используется «электронная колода» — устройство, которое случайно "
        "перемешивает все карты всех игроков. В игре это происходит через кубики:\n"
        "• <b>Дубль</b> (одинаковые числа) → <b>Сдвиг!</b> Все карты заменяются новыми.\n"
        "• Разные числа → сдвига нет.\n"
        "Вероятность дубля ≈ <b>16.7%</b> (6 из 36 комбинаций).\n\n"
        "🌟 Лучшая рука — сумма = 0 (<b>Сабакк</b>)\n"
        "⏱ Минимум <b>3 полных раунда</b> перед вскрытием.\n\n"

        "━━━━━━━━━━━━━━━━━━━\n"
        "🎮 <b>КНОПКИ</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "👁 <b>Мои карты</b> — тайно посмотреть (только ты видишь)\n"
        "🔄 <b>Заменить</b> — взять новую случайную карту\n"
        "⏸ <b>Пас</b> — оставить карты как есть\n"
        "🏁 <b>Вскрыться</b> — завершить и сравнить\n\n"
        "💰 Вход: <b>10 кредитов</b> · Стартовый баланс: <b>100 кредитов</b>",
        parse_mode="HTML"
    )

@dp.message(Command("sabacc"))
async def cmd_sabacc(message: Message):
    if message.chat.type != "private":
        await message.answer("❌ В группах используй <b>/sabacc_table</b>", parse_mode="HTML")
        return
    if message.chat.id in ACTIVE_GAMES:
        await message.answer("🪐 Раунд уже идёт!")
        return
    if get_balance(message.from_user.id, message.from_user.first_name) < BET:
        await message.answer(f"❌ Нужно минимум {BET} кредитов!")
        return
    await message.answer(
        "🎮 <b>Выберите вариант Сабакка:</b>",
        reply_markup=kb_choose_variant("solo"),
        parse_mode="HTML"
    )

@dp.message(Command("sabacc_table"))
async def cmd_sabacc_table(message: Message):
    if message.chat.type == "private":
        await message.answer("❌ Команда только для групп!")
        return
    if message.chat.id in ACTIVE_GAMES:
        await message.answer("🪐 За столом уже идёт игра!")
        return
    if get_balance(message.from_user.id, message.from_user.first_name) < BET:
        await message.answer(f"❌ Нужно минимум {BET} кредитов!")
        return
    await message.answer(
        "👥 <b>Выберите вариант Сабакка для стола:</b>",
        reply_markup=kb_choose_variant("table"),
        parse_mode="HTML"
    )

# ═══════════════════════════════════════════════════════════
#  СОЗДАНИЕ ИГРЫ
# ═══════════════════════════════════════════════════════════

def make_game(chat_id, creator_id, creator_name, variant, is_solo):
    min_rounds = 3 if variant == "kessel" else 4
    ACTIVE_GAMES[chat_id] = {
        "variant":          variant,
        "creator":          creator_id,
        "players_ids":      [creator_id],
        "players_names":    {creator_id: creator_name},
        "cards":            {},
        "dealer_cards":     [],
        "status":           "join",
        "current_turn":     0,
        "round_number":     1,
        "turns_this_round": 0,
        "min_rounds":       min_rounds,
        "bet":              BET,
        "is_solo":          is_solo,
    }

@dp.callback_query(F.data.startswith("choose_solo_"))
async def choose_solo(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    variant = callback.data.split("choose_solo_")[1]

    if chat_id in ACTIVE_GAMES:
        await callback.answer("Раунд уже идёт!", show_alert=True)
        return
    if get_balance(user_id, callback.from_user.first_name) < BET:
        await callback.answer(f"Нужно {BET} кредитов!", show_alert=True)
        return

    update_balance(user_id, -BET)
    make_game(chat_id, user_id, callback.from_user.first_name, variant, True)

    game = ACTIVE_GAMES[chat_id]
    draw = draw_kessel if variant == "kessel" else draw_spike
    game["cards"][user_id] = [draw(), draw()]
    game["dealer_cards"]   = [draw(), draw()]
    game["status"]         = "playing"

    await callback.message.edit_text(
        f"🎲 <b>{VARIANT_NAMES[variant]} — против Дилера!</b>\n\n"
        f"{round_bar(game)}\n\n"
        "🟢 Твой ход! Нажми «👁 Мои карты», чтобы посмотреть расклад.",
        reply_markup=kb_game(variant),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("choose_table_"))
async def choose_table(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    variant = callback.data.split("choose_table_")[1]

    if chat_id in ACTIVE_GAMES:
        await callback.answer("Стол уже открыт!", show_alert=True)
        return
    if get_balance(user_id, callback.from_user.first_name) < BET:
        await callback.answer(f"Нужно {BET} кредитов!", show_alert=True)
        return

    make_game(chat_id, user_id, callback.from_user.first_name, variant, False)

    await callback.message.edit_text(
        f"🌌 <b>Стол {VARIANT_NAMES[variant]} открыт!</b>\n\n"
        f"👥 За столом:\n• {callback.from_user.first_name}\n\n"
        f"🪙 Вход: {BET} кредитов",
        reply_markup=kb_join(),
        parse_mode="HTML"
    )
    await callback.answer()

# ═══════════════════════════════════════════════════════════
#  СТОЛ: ПРИСОЕДИНИТЬСЯ / НАЧАТЬ
# ═══════════════════════════════════════════════════════════

@dp.callback_query(F.data == "table_join")
async def table_join(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    game    = ACTIVE_GAMES.get(chat_id)

    if not game or game["status"] != "join":
        await callback.answer("Сбор уже закрыт.", show_alert=True)
        return
    if user_id in game["players_ids"]:
        await callback.answer("Вы уже за столом!", show_alert=True)
        return
    if len(game["players_ids"]) >= 5:
        await callback.answer("Стол полон (макс. 5)!", show_alert=True)
        return
    if get_balance(user_id, callback.from_user.first_name) < BET:
        await callback.answer(f"Нужно {BET} кредитов!", show_alert=True)
        return

    game["players_ids"].append(user_id)
    game["players_names"][user_id] = callback.from_user.first_name

    players_list = "\n".join(f"• {n}" for n in game["players_names"].values())
    await callback.message.edit_text(
        f"🌌 <b>Стол {VARIANT_NAMES[game['variant']]}</b>\n\n"
        f"👥 За столом ({len(game['players_ids'])}/5):\n{players_list}\n\n"
        f"🪙 Вход: {BET} кредитов",
        reply_markup=kb_join(),
        parse_mode="HTML"
    )
    await callback.answer(f"✅ {callback.from_user.first_name} сел за стол!")

@dp.callback_query(F.data == "table_start")
async def table_start(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    game    = ACTIVE_GAMES.get(chat_id)

    if not game or game["status"] != "join":
        await callback.answer("Игра уже идёт.", show_alert=True)
        return
    if user_id != game["creator"]:
        await callback.answer("Только создатель запускает игру!", show_alert=True)
        return
    if len(game["players_ids"]) < 2:
        await callback.answer("Нужно минимум 2 игрока!", show_alert=True)
        return

    game["status"] = "playing"
    draw = draw_kessel if game["variant"] == "kessel" else draw_spike
    for p_id in game["players_ids"]:
        update_balance(p_id, -BET)
        game["cards"][p_id] = [draw(), draw()]

    first_name   = game["players_names"][game["players_ids"][0]]
    players_list = "\n".join(f"• {n}" for n in game["players_names"].values())

    await callback.message.edit_text(
        f"🚀 <b>{VARIANT_NAMES[game['variant']]} начался!</b>\n\n"
        f"👥 Игроки:\n{players_list}\n\n"
        f"{round_bar(game)}\n\n"
        f"🟢 Ход: <b>{first_name}</b>\n"
        f"ℹ️ {first_name}, нажми «👁 Мои карты» и выбери действие.",
        reply_markup=kb_game(game["variant"]),
        parse_mode="HTML"
    )
    await callback.answer()

# ═══════════════════════════════════════════════════════════
#  ПОКАЗАТЬ КАРТЫ (ТАЙНО)
# ═══════════════════════════════════════════════════════════

@dp.callback_query(F.data == "game_show_cards")
async def game_show_cards(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    game    = ACTIVE_GAMES.get(chat_id)

    if not game or game["status"] not in ("playing",):
        await callback.answer("Игра не найдена.", show_alert=True)
        return
    if user_id not in game["cards"]:
        await callback.answer("Вы не участвуете в этой игре!", show_alert=True)
        return

    cards = game["cards"][user_id]
    hand  = kessel_str(cards) if game["variant"] == "kessel" else spike_str(cards)
    await callback.answer(f"🃏 Твои карты:\n{hand}", show_alert=True)

# ═══════════════════════════════════════════════════════════
#  ПЕРЕХОД ХОДА
# ═══════════════════════════════════════════════════════════

async def next_turn(callback, game, chat_id, action_text):
    game["turns_this_round"] += 1
    game["current_turn"]     += 1

    shift_text  = ""
    round_ended = game["turns_this_round"] >= len(game["players_ids"])

    if round_ended:
        game["turns_this_round"] = 0
        game["round_number"]    += 1
        game["current_turn"]     = 0

        if game["variant"] == "spike":
            d1, d2, shifted = roll_shift_dice(game)
            shift_text = get_shift_text(d1, d2, shifted)

    elif game["current_turn"] >= len(game["players_ids"]):
        game["current_turn"] = 0

    current_id   = game["players_ids"][game["current_turn"]]
    current_name = game["players_names"][current_id]

    await callback.message.edit_text(
        f"{action_text}{shift_text}\n\n"
        f"─────────────────────\n"
        f"{round_bar(game)}\n\n"
        f"🟢 Ход: <b>{current_name}</b>\n"
        f"ℹ️ {current_name}, нажми «👁 Мои карты» и выбери действие.",
        reply_markup=kb_game(game["variant"]),
        parse_mode="HTML"
    )

# ═══════════════════════════════════════════════════════════
#  ЗАВЕРШЕНИЕ ИГРЫ — результат в отдельном сообщении
# ═══════════════════════════════════════════════════════════

async def finish_game(callback, game, chat_id, trigger_text, already_answered=False):
    variant = game["variant"]
    result  = f"{trigger_text}\n\n🛑 <b>ВСКРЫТИЕ!</b>\n\n"

    # ── ОДИНОЧНАЯ ИГРА ──────────────────────────────────────
    if game.get("is_solo"):
        uid     = game["players_ids"][0]
        p_name  = game["players_names"][uid]
        p_cards = game["cards"][uid]
        d_cards = game["dealer_cards"]
        imp_txt = ""

        if variant == "kessel":
            # Разрешаем Самозванцев через кубики (без async — inline результат)
            if 0 in p_cards:
                p_cards, t = roll_kessel_imposter(p_cards)
                game["cards"][uid] = p_cards
                imp_txt += f"👤 {p_name}: {t}"
            if 0 in d_cards:
                d_cards, t = roll_kessel_imposter(d_cards)
                game["dealer_cards"] = d_cards
                imp_txt += f"🤖 Дилер: {t}"
            if imp_txt:
                result += imp_txt + "\n"

            p_rank = kessel_rank(p_cards)
            d_rank = kessel_rank(d_cards)
            result += f"👤 <b>{p_name}:</b> {kessel_str(p_cards)}\n"
            result += f"🤖 <b>Дилер:</b>   {kessel_str(d_cards)}\n\n"
            is_sabacc = (p_rank[0] == 0)
        else:
            p_rank = spike_rank(p_cards)
            d_rank = spike_rank(d_cards)
            result += f"👤 <b>{p_name}:</b> {spike_str(p_cards)}\n"
            result += f"🤖 <b>Дилер:</b>   {spike_str(d_cards)}\n\n"
            is_sabacc = (p_rank == 0)

        tie    = (p_rank == d_rank)
        p_wins = (p_rank < d_rank)

        if tie:
            result += "🤝 Ничья! Ставка возвращена."
            update_balance(uid, BET)
        elif p_wins:
            prize = 35 if is_sabacc else 20
            label = "🌟 САБАКК! " if is_sabacc else ""
            result += f"{label}🏆 Победа! +<b>{prize} кредитов</b>!"
            update_balance(uid, prize)
        else:
            result += "❌ Дилер выиграл. Удачи в следующий раз!"

        result += f"\n\n💰 Баланс: <b>{get_balance(uid)} кредитов</b>"

    # ── МУЛЬТИПЛЕЕР ──────────────────────────────────────────
    else:
        players = game["players_ids"]
        imp_txt = ""

        if variant == "kessel":
            for p in players:
                if 0 in game["cards"][p]:
                    new_cards, t = roll_kessel_imposter(game["cards"][p])
                    game["cards"][p] = new_cards
                    imp_txt += f"👤 {game['players_names'][p]}: {t}"
            if imp_txt:
                result += imp_txt + "\n"

            ranks = {p: kessel_rank(game["cards"][p]) for p in players}
            for p in players:
                result += f"👤 <b>{game['players_names'][p]}:</b> {kessel_str(game['cards'][p])}\n"
        else:
            ranks = {p: spike_rank(game["cards"][p]) for p in players}
            for p in players:
                result += f"👤 <b>{game['players_names'][p]}:</b> {spike_str(game['cards'][p])}\n"

        best    = min(ranks.values())
        winners = [p for p in players if ranks[p] == best]
        pot     = len(players) * BET
        result += "\n"

        if len(winners) == 1:
            wname = game["players_names"][winners[0]]
            result += f"🏆 <b>Победитель: {wname}</b> — банк <b>{pot} кредитов</b>! 🎉"
            update_balance(winners[0], pot)
        else:
            share  = pot // len(winners)
            wnames = ", ".join(game["players_names"][w] for w in winners)
            result += f"🤝 Ничья: {wnames} — по <b>{share} кредитов</b>!"
            for w in winners:
                update_balance(w, share)

        broke = [game["players_names"][p] for p in players if get_balance(p) < BET]
        if broke:
            result += f"\n\n💀 Банкрот(ы): {', '.join(broke)}"

    # Убираем клавиатуру со старого сообщения
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    # Результат — ОТДЕЛЬНОЕ сообщение (видно без прокрутки)
    await bot.send_message(chat_id, result, parse_mode="HTML")

    del ACTIVE_GAMES[chat_id]
    if not already_answered:
        await callback.answer()

# ═══════════════════════════════════════════════════════════
#  ИГРОВЫЕ ДЕЙСТВИЯ
# ═══════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("game_action_"))
async def game_actions(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    game    = ACTIVE_GAMES.get(chat_id)

    if not game or game["status"] != "playing":
        await callback.answer("Игра не найдена или ещё не началась.", show_alert=True)
        return
    if user_id not in game["players_ids"]:
        await callback.answer("Вы не участвуете в этой игре!", show_alert=True)
        return

    action  = callback.data.split("_")[-1]
    variant = game["variant"]
    draw    = draw_kessel if variant == "kessel" else draw_spike

    # ── ВСКРЫТИЕ ─────────────────────────────────────────────
    if action == "finish":
        if game["round_number"] < game["min_rounds"]:
            await callback.answer(
                f"Нельзя вскрываться до раунда {game['min_rounds']}!\n"
                f"Сейчас раунд {game['round_number']}.",
                show_alert=True
            )
            return
        if game.get("is_solo"):
            p_name = game["players_names"][user_id]
            await finish_game(callback, game, chat_id, f"🏁 <b>{p_name}</b> вскрылся!")
        else:
            current_id = game["players_ids"][game["current_turn"]]
            if user_id != current_id:
                cname = game["players_names"][current_id]
                await callback.answer(
                    f"Вскрыться можно только в свой ход!\nСейчас ход {cname}.",
                    show_alert=True
                )
                return
            p_name = game["players_names"][user_id]
            await finish_game(callback, game, chat_id, f"🏁 <b>{p_name}</b> объявил ВСКРЫТИЕ!")
        return

    # ── ПРОВЕРКА ОЧЕРЁДНОСТИ (МУЛЬТИПЛЕЕР) ──────────────────
    if not game.get("is_solo"):
        current_id = game["players_ids"][game["current_turn"]]
        if user_id != current_id:
            cname = game["players_names"][current_id]
            await callback.answer(
                f"Сейчас ход {cname}!\nДождитесь своего хода.",
                show_alert=True
            )
            return

    p_name = game["players_names"][user_id]

    # ── ДЕЙСТВИЕ ИГРОКА ──────────────────────────────────────
    if action == "sand":
        nv   = draw()
        game["cards"][user_id][0] = nv
        nv_s = "✨Самозванец" if nv == 0 else str(nv)
        action_text = (
            f"🟨 <b>{p_name}</b> заменил Жёлтую → <b>{nv_s}</b>"
            if variant == "kessel" else
            f"🎴 <b>{p_name}</b> заменил карту 1 → <b>{nv_s}</b>"
        )
    elif action == "blood":
        nv   = draw()
        game["cards"][user_id][1] = nv
        nv_s = "✨Самозванец" if nv == 0 else str(nv)
        action_text = (
            f"🟥 <b>{p_name}</b> заменил Красную → <b>{nv_s}</b>"
            if variant == "kessel" else
            f"🎴 <b>{p_name}</b> заменил карту 2 → <b>{nv_s}</b>"
        )
    else:  # pass
        action_text = f"⏸ <b>{p_name}</b> пасует."

    # ── ОДИНОЧНАЯ ИГРА: ход Дилера ───────────────────────────
    if game.get("is_solo"):
        await callback.answer()   # сразу, чтобы не было таймаута

        da = smart_dealer_action(variant, game["dealer_cards"])
        if da == "sand":
            game["dealer_cards"][0] = draw()
            dealer_text = "🤖 Дилер заменил карту."
        elif da == "blood":
            game["dealer_cards"][1] = draw()
            dealer_text = "🤖 Дилер заменил карту."
        else:
            dealer_text = "🤖 Дилер пасует."

        game["round_number"] += 1

        # Сдвиг Сабакка (Спайк)
        if variant == "spike":
            d1, d2, shifted = roll_shift_dice(game)
            shift_text = get_shift_text(d1, d2, shifted)
        else:
            shift_text = ""

        # Умный дилер автоматически вскрывается при Сабакке (только после min_rounds)
        if game["round_number"] >= game["min_rounds"]:
            dealer_sabacc = False
            if variant == "kessel":
                d_rank        = kessel_rank(game["dealer_cards"])
                dealer_sabacc = d_rank[0] <= 2   # Чистый / Самозванец-пара / обычная пара
            elif variant == "spike":
                dealer_sabacc = (spike_rank(game["dealer_cards"]) == 0)

            if dealer_sabacc:
                await callback.message.edit_text(
                    f"{action_text}\n{dealer_text}{shift_text}\n\n"
                    f"🤖 <b>Дилер вскрывается — у него Сабакк!</b>",
                    parse_mode="HTML"
                )
                await finish_game(
                    callback, game, chat_id,
                    "🤖 Дилер объявил ВСКРЫТИЕ — у него Сабакк!",
                    already_answered=True
                )
                return

        await callback.message.edit_text(
            f"{action_text}\n{dealer_text}{shift_text}\n\n"
            f"─────────────────────\n"
            f"{round_bar(game)}\n\n"
            "🟢 Твой ход! Нажми «👁 Мои карты» или выбери действие.",
            reply_markup=kb_game(variant),
            parse_mode="HTML"
        )

    # ── МУЛЬТИПЛЕЕР ──────────────────────────────────────────
    else:
        await next_turn(callback, game, chat_id, action_text)
        await callback.answer()

# ═══════════════════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════════════════

async def main():
    print("🚀 Sabacc Bot v6 запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())