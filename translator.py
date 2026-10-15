"""Бесплатный переводчик текстов (MyMemory API + GigaChat fallback).

MyMemory — бесплатный REST API без ключа, лимит 5000 слов/день.
GigaChat — fallback для языков, которые MyMemory не поддерживает,
или при превышении лимита.

Использование:
    from translator import translate, detect_language

    # Перевод
    ru_text = await translate("Olá mundo", "pt", "ru")
    pt_text = await translate("Привет мир", "ru", "pt")

    # Определение языка (по словарю ключевых слов)
    lang = detect_language("Olá, preciso de um site")  # -> "pt"
"""
import json
import logging
import re
import urllib.request
import urllib.parse
import urllib.error

logger = logging.getLogger(__name__)

# MyMemory API: https://api.mymemory.translated.net/get?q=Hello&langpair=en|pt
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
MYMEMORY_EMAIL = os.getenv("MYMEMORY_EMAIL", "") if False else ""  # можно указать email для увеличения лимита

# Маппинг кодов языков для MyMemory (ISO 639-1)
LANG_CODES = {
    "ru": "ru",
    "en": "en",
    "ar": "ar",
    "pt": "pt",
    "id": "id",
    "kz": "kk",
    "uz": "uz",
    "az": "az",
    "es": "es",
    "fr": "fr",
    "de": "de",
    "tr": "tr",
    "zh": "zh",
    "ja": "ja",
    "ko": "ko",
    "hi": "hi",
}

# Названия языков для подписи в уведомлениях
LANG_NAMES = {
    "ru": "🇷🇺 Русский",
    "en": "🇬🇧 English",
    "ar": "🇸🇦 العربية",
    "pt": "🇧🇷 Português",
    "id": "🇮🇩 Indonesia",
    "kz": "🇰🇿 Қазақша",
    "uz": "🇺🇿 O'zbekcha",
    "az": "🇦🇿 Azərbaycan",
    "es": "🇪🇸 Español",
    "fr": "🇫🇷 Français",
    "de": "🇩🇪 Deutsch",
    "tr": "🇹🇷 Türkçe",
    "zh": "🇨🇳 中文",
    "ja": "🇯🇵 日本語",
    "ko": "🇰🇷 한국어",
    "hi": "🇮🇳 हिन्दी",
}

# Флаги стран для подписи
LANG_FLAGS = {
    "ru": "🇷🇺",
    "en": "🇬🇧",
    "ar": "🇸🇦",
    "pt": "🇧🇷",
    "id": "🇮🇩",
    "kz": "🇰🇿",
    "uz": "🇺🇿",
    "az": "🇦🇿",
    "es": "🇪🇸",
    "fr": "🇫🇷",
    "de": "🇩🇪",
    "tr": "🇹🇷",
    "zh": "🇨🇳",
    "ja": "🇯🇵",
    "ko": "🇰🇷",
    "hi": "🇮🇳",
}

# Ключевые слова для определения языка (быстрая эвристика)
LANG_DETECT_KEYWORDS = {
    "pt": ["preciso", "quero", "site", "aplicativo", "orçamento", "preço",
           "desenvolvimento", "sistema", "bot", "olá", "obrigado", "python",
           "react", "node", "aplicação", "plataforma", "comprar", "vender"],
    "id": ["saya", "butuh", "aplikasi", "website", "sistem", "bot",
           "ingin", "membuat", "harga", "biaya", "terima", "halo",
           "jasa", "pengembangan", "mobile", "web"],
    "ar": ["أحتاج", "موقع", "تطبيق", "نظام", "بوت", "أريد", "ميزانية",
           "سعر", "تطوير", "برمجة", "مرحبا", "شكرا", "موقع ويب",
           "هاتف", "متجر", "إلكتروني"],
    "es": ["necesito", "quiero", "sitio", "aplicación", "sistema", "bot",
           "presupuesto", "precio", "desarrollo", "hola", "gracias",
           "programación", "plataforma", "comprar", "vender"],
    "tr": ["ihtiyac", "istiyorum", "site", "uygulama", "sistem", "bot",
           "fiyat", "bütçe", "geliştirme", "merhaba", "teşekkür",
           "yazılım", "platform", "almak", "satmak"],
    "kz": ["қажет", "сайт", "қосымша", "жүйе", "бот", "баға", "жасау",
           "сәлем", "рахмет", "дамыту", "платформа"],
    "uz": ["kerak", "sayt", "ilova", "tizim", "bot", "narxi", "yaratish",
           "salom", "rahmat", "ishlab"],
    "az": ["lazım", "sayt", "tətbiq", "sistem", "bot", "qiymət", "yaratmaq",
           "salam", "təşəkkür", "inkişaf"],
}


# Специфические буквы казахского и украинского/белорусского —
# их наличие надёжно отличает эти языки от русского без ложных срабатываний
_KAZAKH_LETTERS = set("әғқңөұүһі")
_UK_BY_LETTERS = set("іїєґў")

# Ключевые слова для латинских языков — матчим целыми словами, не подстроками,
# иначе английский текст со словами «site»/«bot» ошибочно уходит в pt/id.
_LANG_WORD_PATTERNS = {
    "en": re.compile(r"(?i)\b(the|need|want|looking|for|with|and|you|your|"
                     r"please|contact|services?|development|website|app|"
                     r"price|budget|hire|my|we|our|is|are|to|dm|me)\b"),
    "pt": re.compile(r"(?i)\b(preciso|quero|aplicativo|orçamento|preço|"
                     r"desenvolvimento|olá|obrigado|aplicação|plataforma|"
                     r"comprar|vender|você|não)\b"),
    "id": re.compile(r"(?i)\b(saya|butuh|aplikasi|ingin|membuat|harga|"
                     r"biaya|terima|halo|jasa|pengembangan|yang)\b"),
    "es": re.compile(r"(?i)\b(necesito|quiero|sitio|aplicación|presupuesto|"
                     r"precio|desarrollo|hola|gracias|programación|"
                     r"comprar|vender|una)\b"),
    "tr": re.compile(r"(?i)\b(ihtiyac|istiyorum|uygulama|fiyat|bütçe|"
                     r"geliştirme|merhaba|teşekkür|yazılım|almak|satmak)\b"),
    "uz": re.compile(r"(?i)\b(kerak|sayt|ilova|tizim|narxi|yaratish|"
                     r"salom|rahmat|ishlab)\b"),
    "az": re.compile(r"(?i)\b(lazım|tətbiq|qiymət|yaratmaq|salam|"
                     r"təşəkkür|inkişaf)\b"),
}


def detect_language(text: str) -> str:
    """Определяет язык текста. Возвращает код языка ('ru', 'en', ...)."""
    if not text:
        return "en"

    # Арабский алфавит
    if any("\u0600" <= c <= "\u06FF" for c in text):
        return "ar"
    # Китайский / японский / корейский
    if any("\u4E00" <= c <= "\u9FFF" for c in text):
        return "zh"
    if any("\u3040" <= c <= "\u30FF" for c in text):
        return "ja"
    if any("\uAC00" <= c <= "\uD7A3" for c in text):
        return "ko"

    # Кириллица: специфические буквы казахского/украинского — иначе русский
    if any("\u0400" <= c <= "\u04FF" for c in text):
        text_lower = text.lower()
        if any(c in _KAZAKH_LETTERS for c in text_lower):
            return "kz"
        if any(c in _UK_BY_LETTERS for c in text_lower):
            return "uk"
        return "ru"

    # Латиница: подсчёт слов-маркеров по словарям (word-boundary)
    scores = {}
    for lang, pattern in _LANG_WORD_PATTERNS.items():
        count = len(pattern.findall(text))
        if count:
            scores[lang] = count
    if scores:
        best, best_score = max(scores.items(), key=lambda kv: kv[1])
        # Неанглийскому языку нужно минимум 2 слова-маркера,
        # иначе короткие английские тексты ловят ложный язык
        if best == "en" or best_score >= 2:
            return best
        return "en"
    return "en"


async def _mymemory_translate(text: str, from_lang: str, to_lang: str) -> str | None:
    """Перевод через MyMemory API (бесплатно, без ключа)."""
    src = LANG_CODES.get(from_lang, from_lang)
    dst = LANG_CODES.get(to_lang, to_lang)

    if src == dst:
        return text

    params = {
        "q": text[:500],  # MyMemory лимит ~500 символов за запрос
        "langpair": f"{src}|{dst}",
    }
    if MYMEMORY_EMAIL:
        params["de"] = MYMEMORY_EMAIL

    url = MYMEMORY_URL + "?" + urllib.parse.urlencode(params)

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "LeadHunter/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        # Проверяем статус ответа
        status = data.get("responseStatus")
        translated = data.get("responseData", {}).get("translatedText", "")

        # MyMemory возвращает текст ошибки как "перевод" — фильтруем
        error_markers = (
            "INVALID SOURCE LANGUAGE",
            "INVALID TARGET LANGUAGE",
            "IS AN INVALID",
            "PLEASE SELECT TWO DISTINCT",
            "COULD NOT TRANSLATE",
        )
        if any(marker in (translated or "").upper() for marker in error_markers):
            logger.warning(f"MyMemory: ошибка языка {src}->{dst}: {translated[:100]}")
            return None

        if status == 200 and translated and translated.upper() != text.upper():
            return translated

        # Иногда MyMemory возвращает перевод в matches
        matches = data.get("matches", [])
        if matches:
            best = matches[0].get("translation")
            if best and best.upper() != text.upper():
                # Проверяем что это не сообщение об ошибке
                if not any(marker in best.upper() for marker in error_markers):
                    return best

        logger.warning(f"MyMemory: нет перевода для {src}->{dst}")
        return None

    except Exception as e:
        logger.warning(f"MyMemory API ошибка: {e}")
        return None


async def _gigachat_translate(text: str, from_lang: str, to_lang: str) -> str | None:
    """Перевод через GigaChat (fallback)."""
    try:
        from ai_engine import _chat_with_fallback, DIALOG_AI_PROVIDER, DIALOG_MODEL
    except ImportError:
        logger.error("Не удалось импортировать ai_engine для GigaChat перевода")
        return None

    lang_name_from = LANG_NAMES.get(from_lang, from_lang)
    lang_name_to = LANG_NAMES.get(to_lang, to_lang)

    prompt = (
        f"Переведи следующий текст с {lang_name_from} на {lang_name_to}. "
        f"Сохраняй смысл, тон и форматирование. "
        f"Если текст уже на нужном языке — верни как есть. "
        f"Верни ТОЛЬКО перевод, без пояснений.\n\n"
        f"Текст:\n{text[:2000]}"
    )

    try:
        messages = [{"role": "user", "content": prompt}]
        content = await _chat_with_fallback(
            DIALOG_AI_PROVIDER, DIALOG_MODEL,
            messages, temperature=0.3, max_tokens=2000,
        )
        if content and content.strip():
            # Убираем возможные кавычки вокруг перевода
            result = content.strip().strip('"').strip("'").strip("`")
            return result
    except Exception as e:
        logger.error(f"GigaChat перевод ошибка: {e}")

    return None


async def translate(text: str, from_lang: str, to_lang: str,
                    use_fallback: bool = True) -> str:
    """Перевод текста с одного языка на другой.

    Сначала пробует MyMemory (бесплатно, без ключа).
    При неудаче — GigaChat как fallback.
    Если оба не сработали — возвращает оригинальный текст.

    Args:
        text: текст для перевода
        from_lang: исходный язык (код: ru, en, ar, pt, id, ...)
        to_lang: целевой язык
        use_fallback: использовать GigaChat при неудаче MyMemory

    Returns:
        Переведённый текст или оригинал при неудаче
    """
    if not text or not text.strip():
        return text

    if from_lang == to_lang:
        return text

    # Делим длинный текст на части (MyMemory лимит ~500 символов)
    if len(text) <= 500:
        result = await _mymemory_translate(text, from_lang, to_lang)
        if result and _is_valid_translation(result, text):
            return result
        if use_fallback:
            result = await _gigachat_translate(text, from_lang, to_lang)
            if result and _is_valid_translation(result, text):
                return result
        # Не смогли перевести — возвращаем оригинал
        logger.warning(f"Перевод не удался {from_lang}->{to_lang}, возврат оригинала")
        return text

    # Длинный текст — переводим по частям
    chunks = []
    words = text.split(" ")
    current_chunk = ""
    for word in words:
        if len(current_chunk) + len(word) + 1 > 450:
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = word
        else:
            current_chunk = current_chunk + " " + word if current_chunk else word
    if current_chunk:
        chunks.append(current_chunk)

    translated_chunks = []
    for chunk in chunks:
        result = await _mymemory_translate(chunk, from_lang, to_lang)
        if (not result or not _is_valid_translation(result, chunk)) and use_fallback:
            result = await _gigachat_translate(chunk, from_lang, to_lang)
        if result and _is_valid_translation(result, chunk):
            translated_chunks.append(result)
        else:
            translated_chunks.append(chunk)  # fallback на оригинал

    return " ".join(translated_chunks)


# Маркеры ошибок переводчика
_TRANSLATION_ERROR_MARKERS = (
    "INVALID SOURCE LANGUAGE",
    "INVALID TARGET LANGUAGE",
    "IS AN INVALID",
    "PLEASE SELECT TWO DISTINCT",
    "COULD NOT TRANSLATE",
    "TRANSLATION SERVICE ERROR",
)


def _is_valid_translation(translated: str, original: str) -> bool:
    """Проверяет, что перевод валиден — не сообщение об ошибке."""
    if not translated or not translated.strip():
        return False
    upper = translated.upper()
    if any(marker in upper for marker in _TRANSLATION_ERROR_MARKERS):
        return False
    # Если перевод идентичен оригиналу — скорее всего перевод не сработал
    # (но для коротких слов это может быть нормально, поэтому не блокируем)
    return True


def get_language_label(lang_code: str) -> str:
    """Возвращает подпись языка с флагом для уведомлений.
    Пример: 'pt' -> '🇧🇷 Português'
    """
    return LANG_NAMES.get(lang_code, lang_code)


def get_language_flag(lang_code: str) -> str:
    """Возвращает только флаг языка."""
    return LANG_FLAGS.get(lang_code, "🌐")


# Импорт os для MYMEMORY_EMAIL
import os
