import datetime
import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.future import select

from app.database import User, async_session_local

from .common import OnboardingStates, get_main_reply_keyboard, reset_menu_button_to_default, safe_float

logger = logging.getLogger(__name__)
onboarding_router = Router()


def _diabetes_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Цукровий діабет 1 типу", callback_data="onboard_type:Type 1")],
        [InlineKeyboardButton(text="Цукровий діабет 2 типу", callback_data="onboard_type:Type 2")],
        [InlineKeyboardButton(text="LADA діабет", callback_data="onboard_type:LADA")],
        [InlineKeyboardButton(text="Інший / Не впевнений", callback_data="onboard_type:Other")]
    ])


async def start_physical_profile_onboarding(message: Message, state: FSMContext, intro: str):
    await state.set_state(OnboardingStates.waiting_for_diabetes_type)
    await message.answer(
        f"{intro}\n\n<b>Який у вас тип діабету?</b>",
        reply_markup=_diabetes_type_keyboard()
    )


async def start_clinical_onboarding(message: Message, state: FSMContext):
    await state.set_state(OnboardingStates.waiting_for_icr)
    await message.answer(
        "🩺 <b>Налаштування клінічних показників</b>\n\n"
        "Без цих значень розрахунок дози інсуліну використовуватиме типові (не ваші) параметри — "
        "тож почнемо з них.\n\n"
        "<b>1. Вуглеводний коефіцієнт (ICR)</b> — скільки грамів вуглеводів компенсує 1 ОД "
        "інсуліну (наприклад, <code>10</code>):"
    )


async def send_feature_walkthrough(message: Message, telegram_id: int):
    text = (
        "📖 <b>Що вміє цей бот:</b>\n\n"
        "🩸 <b>Цукор:</b> напишіть <code>цукор 6.2</code> — запишеться миттєво.\n"
        "💉 <b>Інсулін:</b> напишіть <code>болюс 4</code> або <code>базал 12</code>, або скористайтесь /insulin.\n"
        "🥗 <b>Їжа (AI):</b> надішліть фото страви або опис — ШІ оцінить вуглеводи, білки, жири та порекомендує дозу.\n"
        "💬 <b>Чат-помічник (AI):</b> просто напишіть питання — бот відповість, спираючись на вашу історію.\n"
        "🔍 /status — поточний цукор та активний інсулін.\n"
        "📈 /analyze — аналіз чутливості за 7 днів.\n"
        "⏰ /reminders — нагадування про вимірювання цукру."
    )
    await message.answer(text)


@onboarding_router.message(OnboardingStates.waiting_for_icr)
async def onboard_icr_message(message: Message, state: FSMContext):
    try:
        val = safe_float(message.text)
        if val <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Вкажіть додатне число (наприклад, <code>10</code>):")
        return
    await state.update_data(onboard_icr=val)
    await state.set_state(OnboardingStates.waiting_for_isf)
    await message.answer(
        "<b>2. Фактор чутливості до інсуліну (ISF)</b> — на скільки ммоль/л знижує цукор "
        "1 ОД інсуліну (наприклад, <code>3</code>):"
    )


@onboarding_router.message(OnboardingStates.waiting_for_isf)
async def onboard_isf_message(message: Message, state: FSMContext):
    try:
        val = safe_float(message.text)
        if val <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Вкажіть додатне число (наприклад, <code>3</code>):")
        return
    await state.update_data(onboard_isf=val)
    await state.set_state(OnboardingStates.waiting_for_target_bg)
    await message.answer("<b>3. Цільовий рівень цукру (ммоль/л)</b> (наприклад, <code>5.5</code>):")


@onboarding_router.message(OnboardingStates.waiting_for_target_bg)
async def onboard_target_bg_message(message: Message, state: FSMContext):
    try:
        val = safe_float(message.text)
        if val <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Вкажіть додатне число (наприклад, <code>5.5</code>):")
        return
    await state.update_data(onboard_target_bg=val)
    await state.set_state(OnboardingStates.waiting_for_dia_hours)
    await message.answer("<b>4. Тривалість дії інсуліну, DIA (години)</b> (наприклад, <code>4</code>):")


@onboarding_router.message(OnboardingStates.waiting_for_dia_hours)
async def onboard_dia_hours_message(message: Message, state: FSMContext):
    try:
        val = safe_float(message.text)
        if val <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Вкажіть додатне число (наприклад, <code>4</code>):")
        return

    data = await state.get_data()
    telegram_id = message.from_user.id
    profile_incomplete = True

    async with async_session_local() as session:
        async with session.begin():
            result = await session.execute(select(User).filter(User.telegram_id == telegram_id))
            user = result.scalars().first()
            if user:
                user.icr = data.get("onboard_icr")
                user.isf = data.get("onboard_isf")
                user.target_bg = data.get("onboard_target_bg")
                user.dia_hours = val
                user.clinical_setup_complete = True
                session.add(user)
                profile_incomplete = (
                    user.weight is None or
                    user.diabetes_type is None or
                    user.insulin_type is None or
                    user.activity_level is None or
                    user.gender is None or
                    user.birth_year is None
                )

    await message.answer(
        f"✅ Клінічні показники збережено: ICR={data.get('onboard_icr')}, "
        f"ISF={data.get('onboard_isf')}, Ціль={data.get('onboard_target_bg')} ммоль/л, DIA={val} год."
    )

    if profile_incomplete:
        await start_physical_profile_onboarding(
            message, state, "Тепер коротко про вас — це допоможе давати кращі поради."
        )
    else:
        await state.clear()
        await send_feature_walkthrough(message, telegram_id)


@onboarding_router.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    """Handles /start command, registers user in database if missing, checks onboarding status, and shows main menu."""
    telegram_id = message.from_user.id
    username = message.from_user.username or "користувачу"

    # Register user in DB if they don't exist yet
    async with async_session_local() as session:
        async with session.begin():
            result = await session.execute(
                select(User).filter(User.telegram_id == telegram_id)
            )
            user = result.scalars().first()
            if not user:
                user = User(
                    telegram_id=telegram_id,
                    icr=10.0,
                    isf=3.0,
                    target_bg=5.5,
                    dia_hours=4.0
                )
                session.add(user)
                await session.flush()
                logger.info(f"Registered new user: {telegram_id}")

            clinical_done = user.clinical_setup_complete
            user_icr, user_isf, user_target_bg, user_dia_hours = user.icr, user.isf, user.target_bg, user.dia_hours

            # Check if physical profile onboarding is complete
            profile_incomplete = (
                user.weight is None or
                user.diabetes_type is None or
                user.insulin_type is None or
                user.activity_level is None or
                user.gender is None or
                user.birth_year is None
            )

    if not clinical_done:
        await start_clinical_onboarding(message, state)
        return

    if profile_incomplete:
        await start_physical_profile_onboarding(
            message, state,
            f"👋 Вітаємо, {username}!\n\n"
            "Для точного розрахунку доз інсуліну та корисних порад ШІ, будь ласка, пройдіть "
            "коротке налаштування профілю."
        )
        return

    welcome_text = (
        f"👋 Привіт, {username}!\n\n"
        f"Ласкаво просимо до <b>Бот-помічника контролю діабету</b>.\n\n"
        f"Надішліть фото або опис страви для ШІ-аналізу, або надішліть значення цукру (наприклад, <code>цукор 6.2</code>).\n\n"
        f"<b>Ваші клінічні показники:</b>\n"
        f"• Вуглеводний коефіцієнт (ICR): 1 ОД на {user_icr}г вуглеводів\n"
        f"• Фактор чутливості (ISF): 1 ОД на {user_isf} ммоль/л\n"
        f"• Цільовий рівень цукру: {user_target_bg} ммоль/л\n"
        f"• DIA: {user_dia_hours} години\n\n"
        f"Використовуйте кнопки або меню нижче для швидкого перегляду статусу організму."
    )
    # Send welcome text with start reply keyboard (which stays at the bottom) and inline start keyboard
    await message.answer(welcome_text, reply_markup=get_main_reply_keyboard(telegram_id))
    await reset_menu_button_to_default(telegram_id)


@onboarding_router.callback_query(OnboardingStates.waiting_for_diabetes_type, F.data.startswith("onboard_type:"))
async def onboard_type_callback(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.answer()
    _, dt_type = callback_query.data.split(":")
    await state.update_data(onboard_diabetes_type=dt_type)

    await state.set_state(OnboardingStates.waiting_for_insulin_type)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Ультракороткий", callback_data="onboard_insulin:ultra_rapid")],
        [InlineKeyboardButton(text="Швидкий", callback_data="onboard_insulin:rapid")],
        [InlineKeyboardButton(text="Короткий", callback_data="onboard_insulin:regular")]
    ])
    await callback_query.message.edit_text(
        "<b>2. Який тип інсуліну ви використовуєте для їжі (болюсний)?</b>\n"
        "Це допомагає точніше розрахувати, за скільки хвилин до їжі варто вводити інсулін.",
        reply_markup=keyboard
    )


@onboarding_router.callback_query(OnboardingStates.waiting_for_insulin_type, F.data.startswith("onboard_insulin:"))
async def onboard_insulin_type_callback(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.answer()
    _, insulin_type = callback_query.data.split(":")
    await state.update_data(onboard_insulin_type=insulin_type)

    await state.set_state(OnboardingStates.waiting_for_weight)
    await callback_query.message.edit_text(
        "<b>3. Вкажіть вашу вагу (кг)</b> (наприклад, <code>72.5</code>):"
    )


@onboarding_router.message(OnboardingStates.waiting_for_weight)
async def onboard_weight_message(message: Message, state: FSMContext):
    text = message.text.strip()
    try:
        val = safe_float(text)
        if not (20 <= val <= 250):
            raise ValueError
    except ValueError:
        await message.answer("❌ Некоректний формат ваги. Вкажіть число від 20 до 250:")
        return

    await state.update_data(onboard_weight=val)

    await state.set_state(OnboardingStates.waiting_for_activity_level)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сидячий (офіс, мало руху)", callback_data="onboard_activity:sedentary")],
        [InlineKeyboardButton(text="Помірний (прогулянки, легкий спорт)", callback_data="onboard_activity:moderate")],
        [InlineKeyboardButton(text="Активний (важка праця, регулярний спорт)", callback_data="onboard_activity:active")]
    ])
    await message.answer(
        "<b>4. Який у вас рівень фізичної активності?</b>",
        reply_markup=keyboard
    )


@onboarding_router.callback_query(OnboardingStates.waiting_for_activity_level, F.data.startswith("onboard_activity:"))
async def onboard_activity_callback(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.answer()
    _, act_lvl = callback_query.data.split(":")
    await state.update_data(onboard_activity_level=act_lvl)

    await state.set_state(OnboardingStates.waiting_for_gender)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Чоловіча", callback_data="onboard_gender:male")],
        [InlineKeyboardButton(text="Жіноча", callback_data="onboard_gender:female")]
    ])
    await callback_query.message.edit_text(
        "<b>5. Ваша стать:</b>",
        reply_markup=keyboard
    )


@onboarding_router.callback_query(OnboardingStates.waiting_for_gender, F.data.startswith("onboard_gender:"))
async def onboard_gender_callback(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.answer()
    _, gndr = callback_query.data.split(":")
    await state.update_data(onboard_gender=gndr)

    await state.set_state(OnboardingStates.waiting_for_birth_year)
    await callback_query.message.edit_text(
        "<b>6. Вкажіть рік вашого народження</b> (наприклад, <code>1990</code>):"
    )


@onboarding_router.message(OnboardingStates.waiting_for_birth_year)
async def onboard_birth_year_message(message: Message, state: FSMContext):
    text = message.text.strip()
    try:
        val = int(text)
        current_year = datetime.datetime.now().year
        if not (current_year - 110 <= val <= current_year - 2):
            raise ValueError
    except ValueError:
        await message.answer("❌ Некоректний рік народження. Вкажіть 4-значний рік народження:")
        return

    # Get all accumulated onboarding data
    data = await state.get_data()
    diabetes_type = data.get("onboard_diabetes_type")
    insulin_type = data.get("onboard_insulin_type")
    weight = data.get("onboard_weight")
    activity_level = data.get("onboard_activity_level")
    gender = data.get("onboard_gender")
    birth_year = val

    telegram_id = message.from_user.id

    # Save parameters to DB
    async with async_session_local() as session:
        async with session.begin():
            result = await session.execute(
                select(User).filter(User.telegram_id == telegram_id)
            )
            user = result.scalars().first()
            if user:
                user.diabetes_type = diabetes_type
                user.insulin_type = insulin_type
                user.weight = weight
                user.activity_level = activity_level
                user.gender = gender
                user.birth_year = birth_year
                session.add(user)

    await state.clear()

    act_name = {
        "sedentary": "Сидячий",
        "moderate": "Помірний",
        "active": "Активний"
    }.get(activity_level, activity_level)

    insulin_name = {
        "ultra_rapid": "Ультракороткий",
        "rapid": "Швидкий",
        "regular": "Короткий"
    }.get(insulin_type, insulin_type)

    gender_name = "Чоловіча" if gender == "male" else "Жіноча"

    success_text = (
        "🎉 <b>Профіль успішно налаштовано!</b>\n\n"
        "<b>Ваші дані:</b>\n"
        f"•  Тип діабету: {diabetes_type}\n"
        f"•  Тип інсуліну: {insulin_name}\n"
        f"•  Вага: {weight} кг\n"
        f"•  Активність: {act_name}\n"
        f"•  Стать: {gender_name}\n"
        f"•  Рік народження: {birth_year}\n\n"
        ""
        "Тепер ви можете використовувати повний функціонал боту!"
    )
    await message.answer(success_text, reply_markup=get_main_reply_keyboard(telegram_id))
    await reset_menu_button_to_default(telegram_id)
    await send_feature_walkthrough(message, telegram_id)
