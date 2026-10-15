"""Детектор лидов: двухуровневая фильтрация сообщений из чатов.

AI-классификация убрана для скорости — L0+L1 работают мгновенно (regex).
AI остаётся только для диалогов с клиентами (sales_engine.py).
"""
import re
import logging
from datetime import datetime, timedelta
from config import (LEAD_EXCLUDE_KEYWORDS, MAX_MESSAGE_AGE_DAYS,
                    COLD_LEAD_KEYWORDS, COLD_LEAD_EXCLUDE_KEYWORDS,
                    LEAD_BUYER_INTENT, LEAD_SERVICE_SCOPES, LEAD_HARD_EXCLUDES)

logger = logging.getLogger(__name__)

# Компилируем паттерны один раз
_exclude_patterns = [re.compile(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", re.IGNORECASE)
                     for kw in LEAD_EXCLUDE_KEYWORDS]
_cold_keyword_patterns = [re.compile(kw, re.IGNORECASE) for kw in COLD_LEAD_KEYWORDS]
_cold_exclude_patterns = [re.compile(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", re.IGNORECASE)
                          for kw in COLD_LEAD_EXCLUDE_KEYWORDS]
_hard_exclude_patterns = [re.compile(kw, re.IGNORECASE) for kw in LEAD_HARD_EXCLUDES]
_vacancy_pattern = re.compile(r"\bваканси\w*\b", re.IGNORECASE)
_project_hire_pattern = re.compile(
    r"\bищу\s+(?:разработчика|программиста)\b.{0,50}\bпод\s+задач\w*\b", re.IGNORECASE)
_cold_frustration_pattern = re.compile(
    r"(?i)(устал|надоел|достал|замуч|возиться|руки\s+опускаются|нет\s+сил|сколько\s+можно)"
)
_cold_process_pattern = re.compile(
    r"(?i)(документ|бумаг|первичк|накладн|договор|акт\b|сч[её]т|отч[её]т|"
    r"вручн|заполня|проверя|перепроверя|вбива|перенос|таблиц|excel|эксел|"
    r"заявк|заказ|уч[её]т|рутин|каждый\s+день\s+одно\s+и\s+то\s+же|постоянно)"
)

# Минимальная длина сообщения для анализа
MIN_MESSAGE_LENGTH = 8

# Паттерны для отсева ботов/рекламы/не-IT задач
_bot_patterns = re.compile(
    r"(?i)(#реклама|#ad|промокод|купить со скидкой|переходи по ссылке|"
    r"\bказино\b|\bставки\b|\bбукмекер\w*|криптобиржа|заработок|инвестиции|"
    r"пирамида|mlm|сетевой|"
    r"otc|воркер|дам ворк|ищу людей на|ищу людей для|нужны воркеры|"
    r"сетка проектов|пассивный доход|накрутка|подписчик|"
    r"аккаунты? (steam|discord)|продам аккаунт|куплю аккаунт|"
    r"фрифаер|free fire|верификаци.{0,3}аккаунт|"
    # Не-IT задачи, которые ложно матчатся по IT-словам
    r"отзыв\s+за|оставить\s+отзыв|написать\s+отзыв|накрутка\s+отзыв|"
    r"отзыв\s+на\s+сайте|отзывы\s+на\s+сайт|"
    r"\bопрос\b|анкетирование|фокус-группа|респондент|"
    r"тестировщик|протестировать|возьму\s+на\s+тест|проверить\s+приложение|"
    r"собрать\s+пакеты|упаковщик|курьер\s+на|грузчик|"
    r"разгрузить|погрузить|поклейк|поклеить|клининг|"
    r"кулинари|кондитер|торт\s+на\s+заказ|"
    r"дизайн\s+логотипа|логотип\s+за|копирайтинг|рерайт|"
    r"монтаж\s+видео|"
    # Самопиар чужих продуктов
    r"создали\s+бот|мы\s+создали|запустили\s+сервис|"
    r"дам\s+ссылку\s+за|за\s+честную\s+обратную\s+связь)"
)

# Паттерны признаков реального запроса
_request_patterns = re.compile(
    r"(?i)(нужн|ищу|требуется|заказать|сделать|разработать|"
    r"кто\s+может|кто\s+сделает|помогите|оценить|сколько\s+стоит|"
    r"бюджет|сроки|тз|техническое\s+задание|под\s+ключ|"
    # Проектные работы
    r"нужна\s+платформа|нужен\s+сервис|нужна\s+система|нужно\s+решение|"
    r"запустить\s+онлайн|создать\s+маркетплейс|автоматизировать|"
    r"нужна\s+crm|нужен\s+лендинг|нужен\s+портал|нужен\s+дашборд|"
    r"интегрировать|интеграция\s+с|подключить\s+api|"
    r"редизайн|обновить\s+сайт|переделать\s+сайт|ускорить\s+сайт|"
    r"перенести\s+на|миграция\s+на)"
)

# Паттерны ответов на рассылку — заинтересованные реакции
_broadcast_response_patterns = re.compile(
    r"(?i)(интересно|давайте\s+обсудим|давайте\s+поговорим|"
    r"расскажите\s+подробнее|расскажите\s+больше|что\s+предлагаете|"
    r"какие\s+услуги|что\s+вы\s+делаете|чем\s+занимаетесь|"
    r"сколько\s+стоит|какая\s+цена|какой\s+прайс|"
    r"можно\s+примеры|покажите\s+работы|портфолио|"
    r"как\s+с\s+вами\s+связаться|как\s+связаться|"
    r"this\s+is\s+interesting|interesting|tell\s+me\s+more|"
    r"let'?s\s+discuss|let'?s\s+talk|how\s+much|"
    r"what\s+do\s+you\s+offer|what\s+services|how\s+to\s+contact)"
)

_homoglyphs = str.maketrans({
    "a": "а", "e": "е", "o": "о", "p": "р", "c": "с",
    "x": "х", "y": "у", "k": "к", "m": "м", "t": "т",
    "h": "н", "b": "в",
})


def _normalize_filter_text(text: str) -> str:
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text).lower()
    def normalize_word(match):
        word = match.group(0)
        if re.search(r"[а-яё]", word) and re.search(r"[a-z]", word):
            word = word.translate(_homoglyphs)
        return word
    # Не транслитерируем linux в linux-сервер и API в API-интеграция.
    return re.sub(r"[a-zа-яё]+", normalize_word, text)


_developer_offer_patterns = re.compile(
    r"(?i)(\bавтоматизирую\b|\bразработаю\b|\bсоздам\b|\bнастрою\b|"
    r"\bнапишу\s+(вам\s+)?бот|\bинтегрирую\b|"
    r"\bпомогу\s+с\s+(разработ|создан|настройк|интеграц|автоматизац)|"
    r"\bсделаю\s+(вам|под\s+ключ|для\s+вас)|"
    r"я\s+технический\s+специалист|я\s+техспец|я\s+it[-\s]специалист|"
    r"я\s+технический\s+партн|я\s+fullstack|я\s+фулстек|"
    r"я\s+веб-разработчик|я\s+web\s+разработчик|"
    r"не\s+собираю\s+(очередного|шаблон|по\s+шаблон)|"
    r"разрабатываю\s+на\s+заказ|делаю\s+под\s+ключ|"
    r"пополняю\s+(?:сейчас\s+)?портфолио|создаю\s+само[йм]ей?\s+себе|"
    r"обращайтесь\s+в\s+лс|жду\s+в\s+лс|"
    r"\bвозьму\s+(проект|заказ|работу)\b|\bищу\s+(задачи|проекты|заказы)\b|"
    r"\bi\s+(develop|build|create|automate|set\s+up|integrate|write)\b|"
    r"\bi\s+can\s+(develop|build|create|automate|set\s+up|integrate|write)|"
    r"\bi\s+will\s+(develop|build|create|automate|set\s+up|integrate|write)|"
    r"\bi'?m\s+a\s+(developer|programmer|engineer|specialist|tech|freelance))"
)
_service_offer_patterns = re.compile(
    r"(?i)(\bделаю\b|оказываю|предлагаю|настраиваю|подключаю|выполняю|"
    r"готова?\s+(взять|настроить|подключить|сделать)|могу\s+(настроить|подключить|сделать))"
)
_promotion_evidence_patterns = re.compile(
    r"(?i)(портфолио|отзывы|\d+\s*\+?\s*заказов|заказов\s+уже\s+выполн|"
    r"пишите\s+(мне\s+)?в\s+лс|пишите\s+в\s+личные|канал\s+в\s+шапке|"
    r"бесплатно\s+(узна|оцен|провер)|стек[:\s])"
)
_spam_scam_patterns = re.compile(
    r"(?i)(крипто|майнинг|хак\w*|высокоперспективн\w*\s+проект|"
    r"сформировать\s+свою\s+команду|цель\s*[-—:]?\s*сформировать\s+команду|"
    r"заработ\w*.{0,30}(?:в\s+сутки|в\s+день|в\s+час)|"
    r"\d[\d\s.,]*\s*(?:тыс\.?|к)?\s*(?:в\s+сутки|в\s+день|в\s+час)|"
    r"рабочий\s+вариант\s+с\s+нормальн\w*\s+оплат|"
    r"несложн\w*\s+дело.{0,30}плач\w*\s+\d|"
    r"свободное\s+время.{0,80}(?:заработ|доход|сутки|день)|"
    r"наркот\w*|заклад\w*|кладмен\w*|мефедрон\w*|кокаин\w*|"
    r"марихуан\w*|каннабис\w*|гашиш\w*|спайс\w*|мдма|экстази|"
    r"drug\s*deal|sell\s+drugs|cocaine|marijuana|methamphetamine|"
    # Крипто-обмен и прочий серый спам
    r"\busdt\b|gift[\s-]*card|e-gift|обмен\w*\s+(?:usdt|крипто|валют)|"
    r"покупа\w*\s+usdt|прода\w*\s+usdt|\bbuy\s+usdt\b|\bsell\s+usdt\b|"
    r"\binr\b.{0,25}\busdt\b|\busdt\b.{0,25}\binr\b)"
)
# Стена хэштегов — почти всегда рекламная карточка, а не запрос
_hashtag_wall_pattern = re.compile(r"(?:#\w+[\s,]*){5,}")
# Самопиар продавца услуг («если вам нужен сайт — помогу», «I provide..., DM me»)
_seller_ad_patterns = re.compile(
    r"(?i)(\bпомогу\b|\bпоможем\b|\bпомогаю\b|"
    r"если\s+(?:тебе|вам|у\s+вас)\s+(?:есть\s+)?нужн|"
    r"кому\s+нужн\w*\s+(?:сайт|лендинг|бот|приложени|дизайн|разработк|продвижени)|"
    r"представляю\s+(?:digital|агентств|студи|компан|команды?)|"
    r"возьму\s+на\s+себя|беру\s+на\s+себя|"
    r"услуги\s+(?:по\s+)?разработк|сделаю\s+недорого|недорого\s+сделаю|"
    r"\bi\s+(?:provide|offer|deliver|specialize\s+in)\b|"
    r"\bwe\s+(?:provide|offer|develop|build|specialize\s+in)\b|"
    r"\b(?:my|our)\s+services\b|"
    r"\bdm\s+(?:me|to\s+discuss|for)\b|feel\s+free\s+to\s+(?:dm|message|contact)\b)"
)
_buyer_intent_patterns = re.compile(LEAD_BUYER_INTENT, re.IGNORECASE)
_service_patterns = {name: re.compile(pattern, re.IGNORECASE)
                     for name, pattern in LEAD_SERVICE_SCOPES.items()}
_software_scope_patterns = re.compile("|".join(LEAD_SERVICE_SCOPES.values()), re.IGNORECASE)
_cold_business_context_patterns = re.compile(
    r"(?i)\b(?:у\s+нас|у\s+меня|мой\s+бизнес|наш\s+бизнес|наша\s+компания|"
    r"в\s+наш(?:ем|ей)\s+(?:магазине|кафе|салоне|бизнесе|компании)|our\s+(?:company|business)|we)\b"
)
_cold_business_process = re.compile(
    r"(?i)\b(?:заказ\w*|заявк\w*|клиент\w*|уч[её]т\w*|продаж\w*|документ\w*|"
    r"накладн\w*|отч[её]т\w*|запис\w*|сайт\w*|онлайн\w*|crm|"
    r"customers?|orders?|website|booking|invoices?)\b"
)
_cold_pain_pattern = re.compile(
    r"(?i)\b(?:нет\s+сайт\w*|теряем\s+(?:клиент\w*|заказ\w*)|"
    r"вручн\w*|ручн\w*\s+(?:работа|труд|уч[её]т|ввод|режим)|руками|"
    r"на\s+бумаге|бумажн\w*|в\s+экселе|excel\b|в\s+заметках|в\s+тетрад\w*|"
    r"рутин\w*|не\s+справля\w*|не\s+успева\w*|"
    r"пропуска\w*\s+(?:заявк\w*|заказ\w*)|не\s+могут\s+нас\s+найти|"
    r"много\s+(?:клиент\w*|заказ\w*)|очеред\w*|manual|wasting\s+time|"
    r"on\s+paper|pen\s+and\s+paper)\b"
)
_cold_news_pattern = re.compile(
    r"(?i)\b(?:ввп|росстат|инфляц\w*|ключевая\s+ставка|центробанк|"
    r"рост\s+экономик\w*|динамика\s+ввп|курс\s+(?:доллара|евро|рубля)|"
    r"gdp|inflation|central\s+bank)\b"
)


class LeadResult:
    """Результат анализа сообщения."""
    def __init__(self, is_lead: bool, category: str = "NOT_LEAD",
                 task: str = None, budget: str = None, deadline: str = None,
                 market_price: str = None, market_deadline: str = None,
                 business_type: str = None, pain: str = None, hook: str = None,
                 lead_score: int = 0, reason: str = "", service: str = None,
                 evidence: str = None):
        self.is_lead = is_lead
        self.category = category
        self.task = task
        self.budget = budget
        self.deadline = deadline
        self.market_price = market_price
        self.market_deadline = market_deadline
        self.business_type = business_type
        self.pain = pain
        self.hook = hook
        self.lead_score = lead_score
        self.reason = reason
        self.service = service
        self.evidence = evidence


def _matches_cold_keywords(text: str) -> bool:
    text = _normalize_filter_text(text)
    if _spam_scam_patterns.search(text) or _seller_ad_patterns.search(text):
        return False
    if _hashtag_wall_pattern.search(text):
        return False
    if _developer_offer_patterns.search(text):
        return False
    if (_service_offer_patterns.search(text) and
            _promotion_evidence_patterns.search(text)):
        return False
    for pattern in _cold_exclude_patterns:
        if pattern.search(text):
            return False
    if _cold_news_pattern.search(text):
        return False
    has_signal = any(pattern.search(text) for pattern in _cold_keyword_patterns)
    has_pain = bool(_cold_frustration_pattern.search(text) and
                    _cold_process_pattern.search(text))
    return bool((has_signal or has_pain) and _cold_business_context_patterns.search(text)
                and _cold_business_process.search(text) and _cold_pain_pattern.search(text))


def _level0_filter(text: str, message_date: datetime, sender_is_bot: bool) -> bool:
    """Уровень 0: быстрый отсев мусора. True = пропустить сообщение."""
    # Возраст сообщения
    age = datetime.now(tz=message_date.tzinfo) - message_date
    if age > timedelta(days=MAX_MESSAGE_AGE_DAYS):
        return True

    # Слишком короткое
    if len(text) < MIN_MESSAGE_LENGTH:
        return True

    # От бота
    if sender_is_bot:
        return True

    text = _normalize_filter_text(text)
    if (_bot_patterns.search(text) or _spam_scam_patterns.search(text)
            or _seller_ad_patterns.search(text) or _hashtag_wall_pattern.search(text)
            or any(pattern.search(text) for pattern in _hard_exclude_patterns)):
        return True
    if _vacancy_pattern.search(text) and not _project_hire_pattern.search(text):
        return True
    return False


def _level1_filter(text: str) -> bool:
    text = _normalize_filter_text(text)
    if any(pattern.search(text) for pattern in _exclude_patterns):
        return False
    if _spam_scam_patterns.search(text) or _developer_offer_patterns.search(text):
        return False
    if (_service_offer_patterns.search(text) and
            _promotion_evidence_patterns.search(text)):
        return False
    return bool(_buyer_intent_patterns.search(text) and
                _software_scope_patterns.search(text))


# === Regex-извлечение данных (вместо AI) ===

_number = r"(?:\d{1,3}(?:[ \u00a0.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d+)?)"
_currency = r"(?:[₽$€]|\b(?:руб(?:лей|ля|ль)?\.?|usd|eur|rub)\b)"
_unit = r"(?:млн\b\.?|тыс\b\.?|[кk]\b)"
_amount = rf"(?:{_currency}[ \t]*)?{_number}(?:[ \t]*{_unit})?(?:[ \t]*{_currency})?"
_budget_pattern = re.compile(
    rf"(?<![\w.,])(?:от[ \t]+)?{_amount}(?:[ \t]*(?:[-–—]|до\b|to\b)[ \t]*{_amount})?",
    re.IGNORECASE,
)
_budget_label = re.compile(r"\b(?:бюджет|цена|budget|price)\s*[:=—-]?\s*(?:до\s+)?$", re.IGNORECASE)
_duration = re.compile(r"\s*(?:дн\w*|день|недел\w*|месяц\w*|час\w*|days?|weeks?|months?)\b", re.IGNORECASE)

_deadline_patterns = [
    re.compile(r"(?i)срок[:\s]+([^\n.]{3,60})"),
    re.compile(r"(?i)сроки[:\s]+([^\n.]{3,60})"),
    re.compile(r"(?i)(нужно\s+до\s+[^\n.]{3,40})"),
    re.compile(r"(?i)(за\s+\d+\s+(?:дн|недел|месяц|час)[^\n.]{0,20})"),
]


def _extract_budget(text: str) -> str | None:
    """Извлекает бюджет из текста регулярками (вместо AI)."""
    for match in _budget_pattern.finditer(text):
        raw = match.group(0).strip()
        if _duration.match(text[match.end():]):
            continue
        if not (re.search(_currency, raw, re.IGNORECASE) or
                _budget_label.search(text[max(0, match.start() - 40):match.start()])):
            continue
        raw = re.sub(r"(?<=\d)[ \u00a0](?=\d{3}(?:\D|$))", "", raw)
        return raw
    negotiable = re.search(r"\b(?:бюджет|цена)\s*[:—-]?\s*(договорн\w*)", text, re.IGNORECASE)
    if negotiable:
        return negotiable.group(1)
    return None


def _extract_deadline(text: str) -> str | None:
    """Извлекает срок из текста регулярками."""
    for pattern in _deadline_patterns:
        m = pattern.search(text)
        if m:
            raw = m.group(1).strip() if m.lastindex else m.group(0).strip()
            if raw and len(raw) < 60:
                return raw
    return None


def _extract_task(text: str) -> str | None:
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text)
    for line in text.splitlines():
        line = re.sub(r"[*_`]+", "", line).strip()
        if len(line) < MIN_MESSAGE_LENGTH or line.startswith(("#", "http")):
            continue
        normalized = _normalize_filter_text(line)
        if (_buyer_intent_patterns.search(normalized) and
                _software_scope_patterns.search(normalized) and
                not _developer_offer_patterns.search(normalized)):
            return line[:120]
    return None


async def detect_cold_lead(text: str, message_date: datetime,
                           sender_is_bot: bool = False) -> LeadResult:
    """Детектор холодных лидов: ищет латентный спрос в сообщениях бизнес-чатов.

    L0 (отсев мусора) → L1 (холодные ключевые слова) → сразу HOT_COLD.
    AI-классификация убрана для скорости — работает мгновенно.
    """
    # Уровень 0
    if _level0_filter(text, message_date, sender_is_bot):
        return LeadResult(False, reason="level0_filtered")

    # Отсев разработчиков-конкурентов
    if _developer_offer_patterns.search(text):
        return LeadResult(False, reason="developer_offer")

    # Уровень 1: холодные ключевые слова
    if not _matches_cold_keywords(text):
        return LeadResult(False, reason="no_cold_signals")

    # Прошло L0+L1 — холодный лид (без AI-классификации)
    logger.info(f"Холодный лид (L0+L1, без AI): {text[:80]}...")
    return LeadResult(
        is_lead=True,
        category="HOT_COLD",
        lead_score=50,
        reason="cold_keyword_matched",
        pain=text[:250],
        evidence=text[:500],
    )


async def detect_lead(text: str, message_date: datetime,
                      sender_is_bot: bool = False) -> LeadResult:
    """Пайплайн фильтрации сообщения (без AI — мгновенно).

    Уровень 0: быстрый отсев (возраст, длина, боты, спам)
    Уровень 1: ключевые слова, паттерны запроса, отсев разработчиков
    → Сразу HOT лид с regex-извлечением бюджета/задачи.
    """
    # Уровень 0
    if _level0_filter(text, message_date, sender_is_bot):
        return LeadResult(False, reason="level0_filtered")

    # Уровень 1
    if not _level1_filter(text):
        return LeadResult(False, reason="level1_no_keywords")

    # Прошло L0+L1 — лид найден (без AI-классификации)
    budget = _extract_budget(text)
    task = _extract_task(text)
    deadline = _extract_deadline(text)
    logger.info(f"ЛИД (L0+L1, без AI): budget={budget}, task={task[:40] if task else 'None'}...")
    return LeadResult(
        is_lead=True,
        category="HOT",
        task=task,
        budget=budget,
        deadline=deadline,
        lead_score=70,
        reason="keyword_matched",
        service=next((name for name, pattern in _service_patterns.items()
                      if pattern.search(_normalize_filter_text(task or text))), None),
        evidence=task or text[:500],
    )
