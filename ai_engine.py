"""AI движок: интеграция с OmniRoute, Groq, OpenRouter, OpenAI и DeepSeek.

Все провайдеры используют OpenAI-совместимый API — отличается только base_url.
OmniRoute — агрегатор 50+ бесплатных провайдеров (Claude, GPT, Gemini) с auto-fallback.
Groq и OpenRouter также имеют бесплатные модели.
"""
import asyncio
import json
import logging
import re
import time
import uuid
import httpx
from openai import AsyncOpenAI
from config import (OMNIROUTE_API_KEY, OMNIROUTE_BASE_URL,
                    GROQ_API_KEY, GROQ_API_KEY_2, GROQ_API_KEY_3, OPENROUTER_API_KEY, OPENAI_API_KEY, DEEPSEEK_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, NVIDIA_API_KEY,
                    BYTEPLUS_API_KEY, BYTEPLUS_ENDPOINT_ID,
                    GIGACHAT_API_KEY, GIGACHAT_SCOPE, GIGACHAT_BASE_URL, GIGACHAT_OAUTH_URL,
                    GIGACHAT_FALLBACK_MODELS,
                    AI_HTTP_PROXY,
                    DIALOG_AI_PROVIDER, DIALOG_MODEL,
                    CLASSIFY_AI_PROVIDER, CLASSIFY_MODEL,
                    BROADCAST_AI_PROVIDER, BROADCAST_MODEL,
                    CLASSIFY_SYSTEM_PROMPT, DIALOG_SYSTEM_PROMPT,
                    COLD_CLASSIFY_PROMPT,
                    BROADCAST_PROMPT, NO_RULES_MARKER, DIRECT_PROMPT,
                    BROADCAST_LANGUAGE_INSTRUCTIONS,
                    DEVELOPER_INFO, DEVELOPER_GENDER,
                    FORUM_NAME, FORUM_DATE, FORUM_VENUE,
                    FORUM_TICKET_URL, FORUM_PROMO_CODE, FORUM_DISCOUNT,
                    FORUM_TIME, FORUM_SPEAKERS, FORUM_PROGRAM, FORUM_INSTAGRAM,
                    FORUM_DESCRIPTION, BOT_PERSONA_NAME)
from price_catalog import catalog_for_broadcast_prompt

logger = logging.getLogger(__name__)

_GENDER_RU = {"male": "мужской", "female": "женский"}


async def _get_gender_ru() -> str:
    """Пол разработчика: из настроек БД (меняется через GUI), иначе из .env."""
    try:
        import database as db
        value = await db.get_setting("developer_gender")
    except Exception:
        value = None
    return _GENDER_RU.get(value or DEVELOPER_GENDER, "мужской")

# Base URLs для OpenAI-совместимых провайдеров
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
BYTEPLUS_BASE_URL = "https://ark.ap-southeast.bytepluses.com/api/v3"

# === GigaChat OAuth token cache ===
_gigachat_token: str | None = None
_gigachat_token_expires: float = 0


async def _get_gigachat_token() -> str:
    """Получает OAuth-токен GigaChat (с кэшированием и авто-обновлением).
    GigaChat использует OAuth2 client_credentials flow.
    """
    global _gigachat_token, _gigachat_token_expires
    # Возвращаем кэшированный токен если он ещё валиден (с запасом 60 сек)
    if _gigachat_token and time.time() < _gigachat_token_expires - 60:
        return _gigachat_token
    if not GIGACHAT_API_KEY:
        raise ValueError("GIGACHAT_API_KEY не настроен. Получите на developers.sber.ru")
    headers = {
        "Authorization": f"Basic {GIGACHAT_API_KEY}",
        "Content-Type": "application/x-www-form-urlencoded",
        "RqUID": str(uuid.uuid4()),
    }
    data = f"scope={GIGACHAT_SCOPE}"
    # GigaChat OAuth использует самоподписанный сертификат — отключаем проверку
    async with httpx.AsyncClient(verify=False, trust_env=False) as client:
        resp = await client.post(GIGACHAT_OAUTH_URL, headers=headers, content=data, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        _gigachat_token = result["access_token"]
        _gigachat_token_expires = float(result['expires_at']) / 1000 if result.get('expires_at') else time.time() + 1800
        logger.info("GigaChat OAuth токен получен, истекает через %d сек",
                    int(_gigachat_token_expires - time.time()))
        return _gigachat_token


def _proxied_http_client_url() -> str | None:
    """Возвращает URL прокси для httpx-клиента (GigaChat OAuth)."""
    if AI_HTTP_PROXY:
        return AI_HTTP_PROXY
    return None

# Модели, которые поддерживают response_format json_object
_JSON_MODELS = {
    "gpt-4o", "gpt-4o-mini", "gpt-4-turbo",
    "deepseek-chat", "deepseek-reasoner",
}

_clients: dict[str, AsyncOpenAI] = {}

# Все ключи Groq для ротации при 429
_GROQ_KEYS = [k for k in (GROQ_API_KEY, GROQ_API_KEY_2, GROQ_API_KEY_3) if k]
_groq_key_idx = 0


def _get_groq_client() -> AsyncOpenAI:
    """Создаёт Groq-клиент с текущим ключом (ротация при 429)."""
    global _groq_key_idx
    key = _GROQ_KEYS[_groq_key_idx % len(_GROQ_KEYS)]
    return AsyncOpenAI(
        api_key=key,
        base_url=GROQ_BASE_URL,
        http_client=_proxied_http_client(),
        max_retries=0
    )


def _rotate_groq_key():
    """Переключается на следующий Groq ключ."""
    global _groq_key_idx
    _groq_key_idx += 1
    if len(_GROQ_KEYS) > 1:
        logger.warning(f"Ротация Groq ключа: теперь ключ #{_groq_key_idx % len(_GROQ_KEYS) + 1}")
    # Сбрасываем кэш клиента чтобы пересоздался с новым ключом
    _clients.pop("groq", None)


async def _try_groq_all_keys(model: str, messages: list, kwargs: dict) -> str | None:
    """Пробует указанную модель на всех доступных ключах Groq по очереди
    (начиная с текущего). Ротирует ключ при неудаче."""
    for _ in range(len(_GROQ_KEYS)):
        try:
            client = _get_client("groq")
            response = await client.chat.completions.create(
                model=model, messages=messages, **kwargs
            )
            if response.choices and response.choices[0].message.content:
                return response.choices[0].message.content
        except Exception as e:
            logger.warning(f"Groq ключ #{_groq_key_idx % len(_GROQ_KEYS) + 1}, модель {model}: {e}")
        _rotate_groq_key()
    return None


def _extract_json(text: str | None) -> dict:
    """Извлекает JSON из текста ответа (для моделей без response_format).

    Устойчив к: None, reasoning-блокам, обрезанному JSON (max_tokens),
    JSON внутри code-блоков и лишнему тексту вокруг.
    """
    _default = {"category": "NOT_LEAD", "task": "", "budget": "", "deadline": ""}

    if not text:
        logger.warning("Пустой ответ AI (content=None)")
        return dict(_default)

    # Убираем reasoning-блоки <think>...</think>
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    # Если текст уже валидный JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Ищем JSON в блоке кода ```json ... ```
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    # Ищем от первой { до последней } (JSON с вложенностью)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    # Ищем первый плоский { ... } блок
    match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    # Последний рубеж: JSON обрезан лимитом токенов — вытаскиваем поля регэкспами,
    # чтобы не потерять лид из-за оборванного ответа
    field_re = re.compile(r'"(category|task|budget|deadline|business_type|pain|hook'
                          r'|skip|reason|variant_a|variant_b|variant_c|selected'
                          r'|chat_niche|entry_point|message)"'
                          r'\s*:\s*"?([^",}]*)', re.DOTALL)
    fields = {m.group(1): m.group(2).strip() for m in field_re.finditer(text)}
    if fields.get("category") or fields.get("skip") or fields.get("variant_a"):
        logger.warning(f"JSON обрезан, восстановил поля регэкспом: {list(fields.keys())}")
        result = {}
        result.update(fields)
        # Преобразуем skip из строки в bool если нужно
        if "skip" in result:
            result["skip"] = result["skip"].lower() in ("true", "1", "yes")
        return result

    logger.warning(f"Не удалось извлечь JSON из ответа: {text[:200]}")
    return dict(_default)


def _proxied_http_client() -> httpx.AsyncClient | None:
    """HTTP-клиент с прокси для внешних провайдеров (Groq блокирует РФ-IP 403 Forbidden)."""
    if AI_HTTP_PROXY:
        return httpx.AsyncClient(proxy=AI_HTTP_PROXY)
    return None


def _get_client(provider: str) -> AsyncOpenAI:
    """Возвращает клиент для указанного провайдера."""
    if provider not in _clients:
        if provider == "omniroute":
            _clients[provider] = AsyncOpenAI(
                api_key=OMNIROUTE_API_KEY,
                base_url=OMNIROUTE_BASE_URL,
                max_retries=0
            )
        elif provider == "groq":
            if not _GROQ_KEYS:
                raise ValueError("GROQ_API_KEY не настроен. Получите бесплатно на console.groq.com")
            _clients[provider] = _get_groq_client()
        elif provider == "openrouter":
            if not OPENROUTER_API_KEY:
                raise ValueError("OPENROUTER_API_KEY не настроен. Получите на openrouter.ai")
            _clients[provider] = AsyncOpenAI(
                api_key=OPENROUTER_API_KEY,
                base_url=OPENROUTER_BASE_URL,
                http_client=_proxied_http_client(),
                max_retries=0
            )
        elif provider == "openai":
            if not OPENAI_API_KEY:
                raise ValueError("OPENAI_API_KEY не настроен")
            _clients[provider] = AsyncOpenAI(api_key=OPENAI_API_KEY, http_client=_proxied_http_client())
        elif provider == "deepseek":
            if not DEEPSEEK_API_KEY:
                raise ValueError("DEEPSEEK_API_KEY не настроен")
            _clients[provider] = AsyncOpenAI(
                api_key=DEEPSEEK_API_KEY,
                base_url=DEEPSEEK_BASE_URL,
                http_client=_proxied_http_client()
            )
        elif provider == "anthropic":
            if not ANTHROPIC_API_KEY:
                raise ValueError("ANTHROPIC_API_KEY не настроен. Получите на console.anthropic.com")
            _clients[provider] = AsyncOpenAI(
                api_key=ANTHROPIC_API_KEY,
                base_url=ANTHROPIC_BASE_URL,
                http_client=_proxied_http_client()
            )
        elif provider == "gemini":
            if not GEMINI_API_KEY:
                raise ValueError("GEMINI_API_KEY не настроен. Получите бесплатно на ai.google.dev")
            _clients[provider] = AsyncOpenAI(
                api_key=GEMINI_API_KEY,
                base_url=GEMINI_BASE_URL,
            )
        elif provider == "nvidia":
            if not NVIDIA_API_KEY:
                raise ValueError("NVIDIA_API_KEY не настроен. Получите бесплатно на build.nvidia.com")
            _clients[provider] = AsyncOpenAI(
                api_key=NVIDIA_API_KEY,
                base_url=NVIDIA_BASE_URL,
                max_retries=0
            )
        elif provider == "byteplus":
            if not BYTEPLUS_API_KEY:
                raise ValueError("BYTEPLUS_API_KEY не настроен. Регистрация: byteplus.com -> ModelArk")
            _clients[provider] = AsyncOpenAI(
                api_key=BYTEPLUS_API_KEY,
                base_url=BYTEPLUS_BASE_URL,
                max_retries=0
            )
        elif provider == "gigachat":
            # GigaChat обрабатывается отдельно через _gigachat_chat() — не кэшируем клиент
            raise ValueError("GigaChat использует собственный OAuth, вызывается через _gigachat_chat()")
        else:
            raise ValueError(f"Неизвестный AI провайдер: {provider}")
    return _clients[provider]


_RETRYABLE_CODES = ["429", "502", "503", "rate limit", "too many requests",
                     "all_accounts_inactive", "service temporarily unavailable"]

# response_format json_object не поддерживается Groq для некоторых моделей —
# убираем его при кросс-провайдерном fallback, чтобы не сломать запрос.
_GROQ_FALLBACK_MODEL = "qwen/qwen3.8-27b"


# GigaChat не принимает параллельные запросы (HTTP 429) — сериализуем все вызовы
_gigachat_request_lock = asyncio.Lock()


async def _gigachat_chat(model: str, messages: list[dict], **kwargs) -> str | None:
    async with _gigachat_request_lock:
        for attempt in range(3):
            try:
                return await _gigachat_chat_once(model, messages, **kwargs)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 429 or attempt == 2:
                    raise
                await asyncio.sleep(2 ** (attempt + 1))


async def _gigachat_chat_once(model: str, messages: list[dict], **kwargs) -> str | None:
    """Прямой вызов GigaChat API через httpx (OAuth + самоподписанный сертификат).
    GigaChat API совместим с OpenAI /chat/completions.
    """
    token = await _get_gigachat_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    # GigaChat не поддерживает response_format — убираем
    body = {
        "model": model,
        "messages": messages,
        "temperature": kwargs.get("temperature", 0.7),
        "max_tokens": kwargs.get("max_tokens", 500),
    }
    # GigaChat поддерживает stream=false только
    body["stream"] = False
    # profanity_check — уникальная фича GigaChat, отключаем (может блокировать нормальные тексты)
    body["profanity_check"] = False

    url = f"{GIGACHAT_BASE_URL}/chat/completions"

    async with httpx.AsyncClient(verify=False, trust_env=False) as client:
        resp = await client.post(url, headers=headers, json=body, timeout=60)
        if resp.status_code == 401:
            # Токен истёк — сбрасываем и пробуем ещё раз
            global _gigachat_token, _gigachat_token_expires
            _gigachat_token = None
            _gigachat_token_expires = 0
            token = await _get_gigachat_token()
            headers["Authorization"] = f"Bearer {token}"
            resp = await client.post(url, headers=headers, json=body, timeout=60)
        resp.raise_for_status()
        result = resp.json()
        if result.get("choices"):
            return result["choices"][0]["message"]["content"]
        logger.warning(f"GigaChat вернул пустой choices: {result}")
        return None


async def _chat_with_fallback(
    provider: str,
    model: str,
    messages: list[dict],
    **kwargs
) -> str | None:
    """Отправляет запрос к AI с fallback на доступный резервный провайдер."""
    # GigaChat — отдельный путь (OAuth + самоподписанный сертификат)
    # При 429/503 ретраим с экспоненциальной задержкой, без OmniRoute
    if provider == "gigachat":
        chain = [model] + [m for m in GIGACHAT_FALLBACK_MODELS if m != model]
        _gigachat_delays = [3, 8, 15]  # секунды между ретраями
        last_err = None
        for mi, m in enumerate(chain):
            if mi:
                logger.warning(
                    f"GigaChat: {chain[mi - 1]} недоступна/исчерпана — "
                    f"переключаюсь на {m}")
            for attempt, delay in enumerate([0] + _gigachat_delays):
                if delay:
                    logger.warning(f"GigaChat ретрай #{attempt} через {delay}с (ошибка: {last_err})")
                    await asyncio.sleep(delay)
                try:
                    return await _gigachat_chat(m, messages, **kwargs)
                except Exception as e:
                    last_err = e
                    error_text = str(e).lower()
                    if any(code in error_text for code in _RETRYABLE_CODES):
                        continue  # retryable (429/503) — пробуем ещё раз
                    if "404" in error_text or "no such model" in error_text:
                        break  # модели не существует — сразу следующая в цепочке
                    raise  # не retryable — сразу выбрасываем
        logger.error(f"GigaChat: все модели из цепочки исчерпаны ({last_err})")
        raise last_err

    client = _get_client(provider)
    try:
        response = await client.chat.completions.create(
            model=model, messages=messages, **kwargs
        )
        if not response.choices:
            logger.warning(f"AI вернул пустой choices для модели {model}")
            return None
        return response.choices[0].message.content
    except Exception as e:
        error_text = str(e).lower()
        if not any(code in error_text for code in _RETRYABLE_CODES):
            raise
        logger.warning(f"Модель {model} недоступна ({e}), пробуем fallback")
        if provider == "groq":
            # Пробуем ту же модель на всех оставшихся ключах Groq
            result = await _try_groq_all_keys(model, messages, kwargs)
            if result:
                return result
            # Пробуем резервную модель на всех ключах Groq
            result = await _try_groq_all_keys(_GROQ_FALLBACK_MODEL, messages, kwargs)
            if result:
                return result
            logger.error("Все ключи Groq и резервная модель не сработали")
            # Groq исчерпан — пробуем OpenRouter как бесплатный fallback
            if OPENROUTER_API_KEY:
                logger.warning("Пробуем fallback на OpenRouter (бесплатная модель)")
                try:
                    or_client = _get_client("openrouter")
                    or_kwargs = {k: v for k, v in kwargs.items() if k != "response_format"}
                    response = await or_client.chat.completions.create(
                        model="nvidia/nemotron-3-super-120b-a12b:free", messages=messages, **or_kwargs
                    )
                    if response.choices and response.choices[0].message.content:
                        return response.choices[0].message.content
                    else:
                        logger.warning(f"OpenRouter вернул пустой ответ: {response}")
                except Exception as e_or:
                    logger.error(f"Fallback на OpenRouter тоже не сработал: {e_or}")
            # OpenRouter тоже недоступен — пробуем NVIDIA NIM
            if NVIDIA_API_KEY:
                logger.warning("Пробуем fallback на NVIDIA NIM")
                try:
                    nv_client = _get_client("nvidia")
                    nv_kwargs = {k: v for k, v in kwargs.items() if k != "response_format"}
                    response = await nv_client.chat.completions.create(
                        model="nvidia/nemotron-3-super-120b-a12b", messages=messages, **nv_kwargs
                    )
                    if response.choices and response.choices[0].message.content:
                        return response.choices[0].message.content
                    else:
                        logger.warning(f"NVIDIA NIM вернул пустой ответ: {response}")
                except Exception as e_nv:
                    logger.error(f"Fallback на NVIDIA NIM тоже не сработал: {e_nv}")
            # NVIDIA тоже недоступна — пробуем BytePlus ModelArk
            if BYTEPLUS_API_KEY and BYTEPLUS_ENDPOINT_ID:
                logger.warning("Пробуем fallback на BytePlus ModelArk")
                try:
                    bp_client = _get_client("byteplus")
                    bp_kwargs = {k: v for k, v in kwargs.items() if k != "response_format"}
                    response = await bp_client.chat.completions.create(
                        model=BYTEPLUS_ENDPOINT_ID, messages=messages, **bp_kwargs
                    )
                    if response.choices and response.choices[0].message.content:
                        return response.choices[0].message.content
                    else:
                        logger.warning(f"BytePlus вернул пустой ответ: {response}")
                except Exception as e_bp:
                    logger.error(f"Fallback на BytePlus тоже не сработал: {e_bp}")
            raise
        try:
            response = await client.chat.completions.create(
                model="auto", messages=messages, **kwargs
            )
            if not response.choices:
                logger.warning(f"AI вернул пустой choices для модели auto")
                return None
            return response.choices[0].message.content
        except Exception as e2:
            logger.error(f"Fallback auto тоже не сработал: {e2}")
            # Пробуем OpenRouter (бесплатные модели) как fallback
            if provider != "openrouter" and OPENROUTER_API_KEY:
                logger.warning("Пробуем fallback на OpenRouter (бесплатная модель)")
                try:
                    or_client = _get_client("openrouter")
                    or_kwargs = {k: v for k, v in kwargs.items() if k != "response_format"}
                    response = await or_client.chat.completions.create(
                        model="nvidia/nemotron-3-super-120b-a12b:free", messages=messages, **or_kwargs
                    )
                    if response.choices:
                        return response.choices[0].message.content
                except Exception as e_or:
                    logger.error(f"Fallback на OpenRouter тоже не сработал: {e_or}")
            # Последний рубеж: Groq
            if provider != "groq" and _GROQ_KEYS:
                logger.warning("Пробуем кросс-провайдерный fallback на Groq")
                try:
                    groq_client = _get_client("groq")
                    groq_kwargs = {k: v for k, v in kwargs.items() if k != "response_format"}
                    response = await groq_client.chat.completions.create(
                        model=_GROQ_FALLBACK_MODEL, messages=messages, **groq_kwargs
                    )
                    if response.choices:
                        return response.choices[0].message.content
                except Exception as e3:
                    logger.error(f"Fallback на Groq тоже не сработал: {e3}")
            raise


async def classify_message(message_text: str) -> dict | None:
    """Классификация сообщения: лид/не лид. Возвращает dict с category, task, budget, deadline."""
    try:
        kwargs = {
            "temperature": 0.1,
            "max_tokens": 300,
        }
        # response_format json_object поддерживают не все модели
        if CLASSIFY_MODEL in _JSON_MODELS or CLASSIFY_AI_PROVIDER == "openai":
            kwargs["response_format"] = {"type": "json_object"}
        content = await _chat_with_fallback(
            CLASSIFY_AI_PROVIDER, CLASSIFY_MODEL,
            [
                {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
                {"role": "user", "content": message_text},
            ],
            **kwargs
        )
        # Если модель не поддерживает json_object, пытаемся извлечь JSON из текста
        result = _extract_json(content)
        logger.info(f"Классификация: {result.get('category')} | task={result.get('task', '')[:60]}")
        return result
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка парсинга JSON от AI: {e}\nСырой ответ: {content}")
        return None
    except Exception as e:
        logger.error(f"Ошибка классификации: {e}")
        return None


async def generate_dialogue_response(
    messages_history: list[dict],
    stage: str,
    price: str = None,
) -> tuple[str, str] | None:
    """Генерация ответа для диалога с клиентом.
    
    Возвращает (текст_ответа, новая_стадия) или None при ошибке.
    """
    try:
        gender_ru = await _get_gender_ru()
        system_content = f"{DIALOG_SYSTEM_PROMPT.format(gender=gender_ru)}\n\n{DEVELOPER_INFO}\n\n"
        system_content += f"Текущая стадия диалога: {stage}\n"
        if price:
            system_content += f"\nМенеджер установил цену: {price}. "
            system_content += "Презентуй эту цену клиенту через ценность, не как сухую цифру. "
            system_content += "Обоснуй почему эта цена оправдана для его задачи."

        messages = [{"role": "system", "content": system_content}]
        messages.extend(messages_history)

        content = await _chat_with_fallback(
            DIALOG_AI_PROVIDER, DIALOG_MODEL, messages, temperature=0.7, max_tokens=500
        )
        if content:
            content = content.strip()
        else:
            return None

        # Определение новой стадии по содержанию ответа
        new_stage = stage
        if stage == "INITIATING":
            new_stage = "QUALIFYING"
        elif stage == "QUALIFYING":
            # Если AI написал про уточнение у команды — переходим к ожиданию задачи
            if any(phrase in content.lower() for phrase in [
                "у команды", "уточню", "вернусь к вам", "пара минут",
                "паре минут", "через пару", "свяжусь с командой"
            ]):
                new_stage = "TASK_RECEIVED"
        elif stage == "NEGOTIATING":
            # Если диалог идёт к финалу
            if any(phrase in content.lower() for phrase in [
                "договор", "предоплата", "начать работу", "следующий шаг",
                "контакт", "связаться", "телеграм", "почта", "обсудить детали"
            ]):
                new_stage = "CLOSING"

        logger.info(f"AI ответ (стадия {stage} -> {new_stage}): {content[:80]}...")
        return content, new_stage

    except Exception as e:
        logger.error(f"Ошибка генерации ответа диалога: {e}")
        return None


# === A/B Testing: генерация вариантов первого сообщения ===

_AB_VARIANT_STYLES = [
    "Прямой подход: коротко назовите конкретную проблему и задайте один вопрос о ней.",
    "Дружелюбный подход: по-человечески отреагируйте на боль и задайте один простой вопрос.",
    "Вопросный подход: сразу задайте точный вопрос по ситуации без продажи своих услуг.",
]

_FIRST_MESSAGE_META_RE = re.compile(
    r"(?i)(we need|let(?:'|’)s|нужно написать|надо написать|вариант\s*[а-вa-c]|"
    r"ответ\s*:|сообщение\s*:|system prompt|assistant|user prompt)"
)


def _validate_first_message(text: str) -> tuple[bool, str]:
    value = (text or "").strip().strip('"“”')
    if len(value) < 35:
        return False, "слишком короткое или оборванное сообщение"
    if len(value) > 350:
        return False, "сообщение длиннее 350 символов"
    if _FIRST_MESSAGE_META_RE.search(value):
        return False, "модель вернула рассуждения или служебный текст"
    if value.count("(") != value.count(")"):
        return False, "незакрытые скобки"
    if not value.endswith("?"):
        return False, "первое сообщение должно завершаться вопросом"
    if len(re.findall(r"[.!?](?:\s|$)", value)) > 3:
        return False, "больше трёх предложений"
    return True, ""


async def generate_first_message_variants(
    context: str,
    anti_repeat_note: str = "",
    num_variants: int = 3,
) -> list[str]:
    """Генерирует num_variants вариантов первого сообщения разными подходами.

    Возвращает список текстов (может быть меньше num_variants при ошибках).
    """
    gender_ru = await _get_gender_ru()
    system_content = f"{DIALOG_SYSTEM_PROMPT.format(gender=gender_ru)}\n\n{DEVELOPER_INFO}\n\n"
    system_content += (
        "Текущая стадия диалога: INITIATING. Верни только готовый текст сообщения без "
        "пояснений и вариантов. 1-3 коротких предложения, до 350 символов. "
        "Заверши одним естественным вопросом. Не начинай с продажи и не заявляй об опыте, "
        "которого нет в контексте.\n"
    )

    variants = []
    for i in range(num_variants):
        style = _AB_VARIANT_STYLES[i % len(_AB_VARIANT_STYLES)]
        user_msg = f"{context}\n\nСтиль сообщения: {style}\n{anti_repeat_note}"
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_msg},
        ]
        for attempt in range(2):
            try:
                content = await _chat_with_fallback(
                    DIALOG_AI_PROVIDER, DIALOG_MODEL, messages,
                    temperature=0.7 + i * 0.1, max_tokens=220,
                )
                value = (content or "").strip().strip('"“”')
                valid, reason = _validate_first_message(value)
                if valid:
                    variants.append(value)
                    break
                logger.warning(f"Вариант #{i} отклонён: {reason}: {value[:120]}")
                messages.extend([
                    {"role": "assistant", "content": value},
                    {"role": "user", "content": (
                        f"Текст отклонён: {reason}. Перепиши полностью. Верни только законченное "
                        "сообщение до 350 символов, которое заканчивается одним вопросом."
                    )},
                ])
            except Exception as e:
                logger.error(f"Ошибка генерации варианта #{i}, попытка #{attempt + 1}: {e}")

    if not variants:
        logger.error("Не удалось сгенерировать ни одного варианта первого сообщения")
    return variants


# Диапазоны Unicode для "чужих" алфавитов, которые не должны появляться в русском тексте
# (CJK, хирагана/катакана, хангыль, арабский, деванагари и т.п.)
_FOREIGN_SCRIPT_RE = re.compile(
    r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\u0600-\u06ff\u0900-\u097f'
    r'\u0e00-\u0e7f\u1100-\u11ff\uff00-\uffef]'
)


def _has_foreign_script(text: str) -> bool:
    """Проверяет, есть ли в тексте символы чужих алфавитов (баг слабых моделей)."""
    return bool(_FOREIGN_SCRIPT_RE.search(text or ""))


_BROADCAST_PROHIBITED_RE = re.compile(
    r"(?i)(реклам\w*\s+(?:запрещ|нельзя)|запрещ\w*\s+(?:реклам|самореклам|услуг)|"
    r"самореклам\w*\s+(?:запрещ|нельзя)|без\s+реклам|коммерческ\w*\s+(?:пост|сообщ).*запрещ)"
)


def _broadcast_similarity(text: str, recent_messages: list[str]) -> float:
    words = set(re.findall(r"[а-яёa-z0-9]+", (text or "").casefold()))
    if not words:
        return 1.0
    maximum = 0.0
    for previous in recent_messages:
        previous_words = set(re.findall(r"[а-яёa-z0-9]+", (previous or "").casefold()))
        if not previous_words:
            continue
        union = words | previous_words
        maximum = max(maximum, len(words & previous_words) / len(union))
    return maximum


def _broadcast_message_from_result(result: dict) -> str:
    selected = str(result.get("selected", "a")).lower()
    message = str(result.get(f"variant_{selected}", "") or "").strip()
    if message:
        return message
    for key in ("variant_a", "variant_b", "variant_c"):
        message = str(result.get(key, "") or "").strip()
        if message:
            return message
    return ""


_WTS_WTB_RE = re.compile(r"(?i)\b(wts|wtb)\b")
_HAS_PRICE_RE = re.compile(r"(?i)(\$|price|цена|руб|₽|eur|usd)")
_HAS_HASHTAG_RE = re.compile(r"#\w+")


def _enforce_chat_rules_format(message: str, chat_rules: str, chat_niche: str = "") -> str:
    """Программно приводит текст в соответствие с форматом правил чата.
    GigaChat часто игнорирует требования формата — эта функция исправляет это.
    """
    rules = (chat_rules or "").strip()
    if not rules or rules == NO_RULES_MARKER:
        return message

    rules_lower = rules.lower()
    msg = message.strip()

    # 1. WTB/WTS формат
    if _WTS_WTB_RE.search(rules_lower):
        if not re.match(r"(?i)^(wts|wtb)\b", msg):
            msg = f"WTS: {msg}"
            logger.info("Broadcast: добавлен префикс WTS по правилам чата")

    # 2. Требуется цена
    if re.search(r"(?i)(without.*price.*delet|ads?.*price|цена.*обязат|price.*required)", rules_lower):
        if not _HAS_PRICE_RE.search(msg):
            msg += " Price: $500."
            logger.info("Broadcast: добавлена цена по правилам чата")

    # 3. Требуются хэштеги
    if re.search(r"(?i)(hashtag|#tiker|хэштег|теги.*обязат)", rules_lower):
        if not _HAS_HASHTAG_RE.search(msg):
            # Подбираем хэштеги по нише
            niche_lower = (chat_niche or "").lower()
            tags = []
            if any(w in niche_lower for w in ("крипт", "crypto", "p2p", "трейд", "trade")):
                tags = ["#crypto", "#bot", "#automation"]
            elif any(w in niche_lower for w in ("ресторан", "кафе", "food", "restaurant")):
                tags = ["#food", "#bot", "#automation"]
            elif any(w in niche_lower for w in ("недвиж", "realty", "property")):
                tags = ["#realty", "#bot", "#automation"]
            else:
                tags = ["#automation", "#bot", "#development"]
            msg += f" {' '.join(tags)}"
            logger.info(f"Broadcast: добавлены хэштеги по правилам чата: {tags}")

    # 4. Без эмодзи
    if re.search(r"(?i)(without emoji|без эмодзи|no emoji)", rules_lower):
        msg = re.sub(r"[\U0001F000-\U0001FFFF\u2600-\u27BF\u2700-\u27BF]", "", msg)

    return msg


async def generate_broadcast(chat_rules: str, recent_messages: list[str],
                             now_str: str, is_direct_promo: bool = False,
                             chat_name: str = "",
                             recent_chat_messages: list[str] | None = None,
                             chat_niche: str = "",
                             language: str = "ru") -> dict | None:
    """Генерация рекламной рассылки с учётом правил чата.

    Если is_direct_promo=True — используется прямой промпт (для чатов без правил).
    chat_niche — ручная тематика чата (приоритет). Если пусто — AI использует
    chat_name и recent_chat_messages для определения ниши.
    language — язык рассылки: ru, en, kz, uz, az.
    Возвращает {"skip": bool, "reason": str, "message": str} или None при ошибке.
    """
    try:
        recent_text = "\n---\n".join(recent_messages) if recent_messages else "(ещё не было отправок)"
        rules_value = (chat_rules or "").strip()
        if is_direct_promo and rules_value and rules_value != NO_RULES_MARKER:
            is_direct_promo = False

        if not is_direct_promo and rules_value != NO_RULES_MARKER and _BROADCAST_PROHIBITED_RE.search(rules_value):
            return {
                "skip": True,
                "reason": "В правилах найден запрет рекламы или саморекламы",
                "message": "",
                "chat_niche": "",
                "entry_point": "",
            }

        # Контекст чата: ручная тематика (приоритет) + название + сообщения
        chat_context = ""
        niche = (chat_niche or "").strip()
        if niche:
            chat_context += f"Тематика чата (указана владельцем): «{niche}».\n"
        if chat_name:
            chat_context += f"Название чата: «{chat_name}».\n"
        if recent_chat_messages:
            sample = recent_chat_messages[:15]
            chat_context += "Последние сообщения участников чата (для понимания ниши):\n"
            chat_context += "\n---\n".join(sample)
            chat_context += "\n"
        if not chat_context:
            chat_context = "(название и сообщения чата недоступны — пиши для общей бизнес-аудитории)"

        # GigaChat всегда пишет на русском — переводчик переведёт на нужный язык
        # Это гарантирует что "оригинал" в уведомлении всегда на русском
        lang_instr = BROADCAST_LANGUAGE_INSTRUCTIONS["ru"]

        price_catalog_text = catalog_for_broadcast_prompt()

        if is_direct_promo:
            prompt = DIRECT_PROMPT.format(
                gender=await _get_gender_ru(),
                developer_info=DEVELOPER_INFO,
                price_catalog=price_catalog_text,
                recent_messages=recent_text,
                current_datetime=now_str,
                chat_context=chat_context,
                language_instruction=lang_instr,
            )
        else:
            rules_text = chat_rules.strip()
            if rules_text == NO_RULES_MARKER:
                rules_text = "Правил в чате нет (владелец подтвердил). Пиши в нейтральном деловом тоне."

            prompt = BROADCAST_PROMPT.format(
                gender=await _get_gender_ru(),
                developer_info=DEVELOPER_INFO,
                price_catalog=price_catalog_text,
                chat_rules=rules_text,
                recent_messages=recent_text,
                current_datetime=now_str,
                chat_context=chat_context,
                language_instruction=lang_instr,
            )
            prompt += (
                "\n\nСТРОГИЙ ПРИОРИТЕТ БЕЗОПАСНОСТИ: правила чата — приоритет №1."
                "\n1. ФОРМАТ: если правила требуют определённый формат (WTB/WTS, хэштеги, "
                "подпись, цена, структура) — текст ОБЯЗАТЕЛЬНО должен ему соответствовать. "
                "Нельзя писать свободный текст в чате с жёстким форматом. "
                "Пример: правила требуют WTS + цену + хэштеги → "
                "'WTS: Custom Telegram bot for P2P crypto automation. Price: $500. #crypto #bot'"
                "\n2. РЕКЛАМА: если реклама запрещена — не делай рекламный пост. Но если "
                "формат позволяет (например, WTS-объявление) — можно подать услугу в этом формате."
                "\n3. ЯЗЫК: если правила указывают язык (Only English) — пиши ТОЛЬКО на нём."
                "\n4. skip=true только если: нет НИ ОДНОЙ легитимной точки входа с соблюдением "
                "ВСЕХ правил формата. Если можно написать текст в требуемом формате — делай skip=false."
            )
            prompt += (
                "\n\nВАЖНО: НЕ пропускай (skip=false) только из-за того, что не удалось точно "
                "определить нишу. Используй название чата и последние сообщения для вывода. "
                "Если ниша неясна — пиши для общей бизнес-аудитории (предприниматели, малый бизнес)."
            )
            prompt += (
                "\n\nКРИПТО/ФИНАНС/ТРЕЙДИНГ чаты — это НЕ причина для skip. В таких чатах "
                "актуальны: боты для P2P-автоматизации, парсеры курсов, платёжные шлюзы, "
                "приложения с виртуальными картами, биржевые дашборды, арбитраж-сканеры, "
                "Telegram-боты для приёма криптооплаты. Найди подходящую точку входа "
                "и ОБЯЗАТЕЛЬНО соблюдай формат правил чата."
            )
            prompt += (
                "\n\nОБЯЗАТЕЛЬНО упоминай НЕ МЕНЕЕ ДВУХ услуг из спектра: сайты, "
                "веб-приложения, мобильные приложения, Telegram-боты, автоматизация, "
                "интеграции. Не пиши только про ботов — ты делаешь сайты и приложения тоже. "
                "Перечисли 2-3 услуги в тексте. Пример: 'website, mobile app, or Telegram bot'."
            )

        messages = [{"role": "user", "content": prompt}]

        logger.info(f"generate_broadcast: is_direct_promo={is_direct_promo}, prompt_len={len(prompt)}")
        content = await _chat_with_fallback(
            BROADCAST_AI_PROVIDER, BROADCAST_MODEL,
            messages,
            temperature=0.9, max_tokens=1200,
        )
        logger.info(f"generate_broadcast: AI ответил, content_len={len(content) if content else 0}")

        # Retry с напоминанием о языке, если первая попытка вернула посторонние символы
        if content and _has_foreign_script(content):
            logger.warning("generate_broadcast: первая попытка содержала посторонние символы, retry")
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": "В твоём ответе обнаружены иероглифы или символы чужих алфавитов. Перепиши ТОЛЬКО на русском кириллицей. Убери все нерусские символы. Верни только JSON."})
            content = await _chat_with_fallback(
                BROADCAST_AI_PROVIDER, BROADCAST_MODEL,
                messages,
                temperature=0.7, max_tokens=1200,
            )
        if not content:
            logger.error("generate_broadcast: content пустой после AI")
            return None

        logger.info(f"generate_broadcast: raw content[:300]={content[:300]}")
        result = _extract_json(content)
        logger.info(f"generate_broadcast: extracted keys={list(result.keys())}")
        skip = result.get("skip")
        if not isinstance(skip, bool):
            logger.error(f"Неверный формат ответа рассылки (skip не bool): skip={skip!r}, content[:200]={content[:200]}")
            return None
        
        selected = str(result.get("selected", "a")).lower()
        message = _broadcast_message_from_result(result)

        if not skip and not message:
            logger.error(f"Рассылка без текста при skip=false: selected={selected!r}, result_keys={list(result.keys())}, content[:300]={content[:300]}")
            return None

        if not skip and recent_messages and _broadcast_similarity(message, recent_messages) >= 0.52:
            logger.warning("Рассылка слишком похожа на предыдущие, повторная генерация")
            retry_messages = messages + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": (
                    "Выбранный текст программно признан слишком похожим на прошлые публикации. "
                    "Выбери другую тему, другой заход и другую структуру. Не переставляй слова в "
                    "старом тексте. Верни новый JSON в исходном формате либо skip=true."
                )},
            ]
            retry_content = await _chat_with_fallback(
                BROADCAST_AI_PROVIDER, BROADCAST_MODEL,
                retry_messages, temperature=0.95, max_tokens=1200,
            )
            retry_result = _extract_json(retry_content)
            retry_skip = retry_result.get("skip")
            if not isinstance(retry_skip, bool):
                return None
            result = retry_result
            skip = retry_skip
            selected = str(result.get("selected", "a")).lower()
            message = _broadcast_message_from_result(result)
            if not skip and (not message or _broadcast_similarity(message, recent_messages) >= 0.52):
                return {
                    "skip": True,
                    "reason": "Не удалось создать достаточно непохожую публикацию",
                    "message": "",
                    "chat_niche": result.get("chat_niche", ""),
                    "entry_point": result.get("entry_point", ""),
                }

        if not skip and _has_foreign_script(message):
            logger.error(f"Рассылка отклонена: обнаружены посторонние символы в тексте: {message[:200]}")
            return None

        # Программная проверка формата по правилам чата
        if not skip and message:
            message = _enforce_chat_rules_format(message, chat_rules, chat_niche)

        result["reason"] = result.get("reason") or ""
        result["message"] = message
        niche = result.get("chat_niche", "")
        logger.info(f"Рассылка сгенерирована: skip={skip}, selected={selected}, niche={niche}, "
                    f"{result['reason'][:60] if skip else result['message'][:60]}")
        return result
    except Exception as e:
        logger.error(f"Ошибка генерации рассылки: {e}")
        return None


async def generate_message_variants(base_text: str, count: int = 3) -> list[str]:
    """Генерация вариантов сообщения для рассылки (анти-бан рандомизация)."""
    try:
        prompt = (
            f"Создай {count} варианта следующего сообщения для Telegram-чата. "
            f"Смысл одинаковый, но формулировки разные. "
            f"Не используй эмодзи. Пиши коротко и естественно.\n\n"
            f"Оригинал: {base_text}\n\n"
            f"Ответай в JSON: {{\"variants\": [\"вариант1\", \"вариант2\", ...]}}"
        )
        kwargs = {
            "temperature": 0.8,
            "max_tokens": 500,
        }
        if CLASSIFY_MODEL in _JSON_MODELS or CLASSIFY_AI_PROVIDER == "openai":
            kwargs["response_format"] = {"type": "json_object"}
        content = await _chat_with_fallback(
            CLASSIFY_AI_PROVIDER, CLASSIFY_MODEL,
            [{"role": "user", "content": prompt}],
            **kwargs
        )
        result = _extract_json(content)
        variants = result.get("variants", [base_text])
        logger.info(f"Сгенерировано {len(variants)} вариантов сообщения")
        return variants
    except Exception as e:
        logger.error(f"Ошибка генерации вариантов: {e}")
        return [base_text]


# === Forum outreach ===


def _forum_facts() -> str:
    return (
        f"Мероприятие: {FORUM_NAME}\n"
        f"Позиционирование: {FORUM_DESCRIPTION}\n"
        f"Дата и время: {FORUM_DATE}, {FORUM_TIME}\n"
        f"Место: {FORUM_VENUE}\n"
        f"Спикеры: {FORUM_SPEAKERS}\n"
        f"Программа: {FORUM_PROGRAM}\n"
        f"Ссылка на билеты: {FORUM_TICKET_URL}\n"
        f"Промокод на скидку {FORUM_DISCOUNT}: {FORUM_PROMO_CODE}\n"
        f"Атмосфера прошлых мероприятий: {FORUM_INSTAGRAM}"
    )


async def classify_forum_candidate(sample_texts: str) -> str:
    """Квалификация активного участника чата по его сообщениям.
    Возвращает DIRECT | PAIN | WARM_COLD | REJECT.
    """
    prompt = f"""Ты — аналитик аудитории женского делового форума в Краснодаре.
Тебе даны последние сообщения человека из Telegram-чата. Определи, подходит ли
он под целевую аудиторию форума ({FORUM_NAME}: карьера, личный бренд, баланс, нетворкинг).

Категории (ответ строго одним словом):
DIRECT — человек прямо ищет то, что даёт форум: женский форум, нетворкинг,
  мероприятия по саморазвитию, «куда сходить», знакомства с предпринимателями.
PAIN — есть боль, которую форум закрывает: выгорание, «не знаю куда двигаться»,
  рутина, «хочу больше денег», «потеряла себя в декрете», нет окружения.
WARM_COLD — в жизни событие, форум уместен: открыла салон/бизнес, запускает
  магазин, повышение, вышла из декрета, хочет свой бизнес, ищет смену деятельности.
REJECT — не ЦА: мужские темы, не Краснодар, спам/реклама/бот, болтовня без
  отношения к развитию и бизнесу.

Сообщения человека:
{sample_texts[:1500]}

Ответь строго одним словом: DIRECT, PAIN, WARM_COLD или REJECT."""
    try:
        content = await _chat_with_fallback(
            CLASSIFY_AI_PROVIDER, CLASSIFY_MODEL,
            [{"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=10,
        )
        verdict = (content or "").strip().upper()
        return verdict if verdict in {"DIRECT", "PAIN", "WARM_COLD"} else "REJECT"
    except Exception as e:
        logger.error(f"Ошибка квалификации кандидата на приглашение: {e}")
        return "REJECT"


async def extract_forum_topic(message_text: str) -> str | None:
    """Короткая тема сообщения для персонализации приглашения (вариант B)."""
    try:
        content = await _chat_with_fallback(
            CLASSIFY_AI_PROVIDER, CLASSIFY_MODEL,
            [{"role": "user", "content":
              "В 2-4 словах назови тему этого сообщения по-русски "
              f"(например: «открытии салона», «выгорании на работе», «поиске идей»).\n"
              f"Сообщение: «{message_text[:300]}»\nТолько тема, без пояснений."}],
            temperature=0.2, max_tokens=20,
        )
        topic = (content or "").strip().strip("«»\"'")
        return topic if 3 <= len(topic) <= 50 else None
    except Exception as e:
        logger.error(f"Ошибка извлечения темы: {e}")
        return None


async def extract_person_name(sender_name: str, sample_texts: str = "") -> str | None:
    """Извлекает настоящее имя из ника Telegram и сообщений.
    «Мария 💅 маникюр» → Мария; «beauty_master» → None; «я Лена» в тексте → Лена.
    Русские имена отдаём кириллицей (Anna → Анна)."""
    prompt = f"""Найди настоящее женское или мужское имя человека.
Источники: ник в Telegram и его сообщения (часто представляются: «я Лена», «Оксана, мастер бровей»).
Русские имена верни кириллицей с большой буквы (Anna → Анна, Masha → Маша).
Если настоящего имени нет — ответь ровно «NONE».

Ник: «{sender_name or ''}»
Сообщения: «{sample_texts[:400]}»

Ответь только именем или NONE."""
    try:
        content = await _chat_with_fallback(
            CLASSIFY_AI_PROVIDER, CLASSIFY_MODEL,
            [{"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=12,
        )
        name = (content or "").strip().strip("«»\"'.").title()
        if name.upper() == "NONE" or not (2 <= len(name) <= 20):
            return None
        return name
    except Exception as e:
        logger.error(f"Ошибка извлечения имени: {e}")
        return None


async def detect_person_gender(name: str, sample_texts: str) -> str:
    """Определяет пол автора: female | male | unknown.
    Признаки: имя, женские окончания глаголов прошедшего времени в русском
    (устала, открыла, вышла), самоназвания (мама, предпринимательница).
    При ошибке — unknown (не отсекаем)."""
    prompt = f"""Определи пол автора по имени и тексту его сообщений (русский язык).
Сильные признаки: женские окончания глаголов прошедшего времени
(устала, открыла, вышла, работала, хотела), самоназвания
(мама, предпринимательница, хозяйка, я девушка/женщина).
Мужские: устал, открыл, вышел, предприниматель, мужские имена.
Неоднозначные имена (Саша, Женя, Валя) без грамматики пола — unknown.

Имя: {name or 'неизвестно'}
Сообщения:
{sample_texts[:1200]}

Ответь строго одним словом: female, male или unknown."""
    try:
        content = await _chat_with_fallback(
            CLASSIFY_AI_PROVIDER, CLASSIFY_MODEL,
            [{"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=8,
        )
        v = (content or "").strip().lower()
        return v if v in {"female", "male"} else "unknown"
    except Exception as e:
        logger.error(f"Ошибка определения пола: {e}")
        return "unknown"


async def detect_person_city(sample_texts: str, city: str = "Краснодар") -> str:
    """Определяет город автора по его сообщениям: local | other | unknown.
    Признаки local: упоминания города, Кубани, районов Краснодара
    (ЦМР, КМР, ФМР, Западный обход, Энка, Гидрострой, Прикубанский округ).
    other: явный другой город. При ошибке — unknown (не отсекаем)."""
    prompt = f"""Определи, живёт ли автор сообщений в городе {city}.
Признаки «local»: упоминает {city}, Кубань, районы/места {city}а
(ЦМР, КМР, ФМР, Западный обход, Энка, Гидрострой, Прикубанский округ,
Галерея, Красная улица).
Признаки «other»: явно живёт в другом городе (упоминает Москву, Ростов,
Сочи и т.п. как свой город или место работы).
Если город не понятен — unknown.

Сообщения:
{sample_texts[:1200]}

Ответь строго одним словом: local, other или unknown."""
    try:
        content = await _chat_with_fallback(
            CLASSIFY_AI_PROVIDER, CLASSIFY_MODEL,
            [{"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=8,
        )
        v = (content or "").strip().lower()
        return v if v in {"local", "other"} else "unknown"
    except Exception as e:
        logger.error(f"Ошибка определения города: {e}")
        return "unknown"


async def analyze_opener_reply(reply_text: str) -> dict:
    """Классификация ответа на вопрос «чем занимаетесь?».
    Возвращает {"gender": FEMALE|MALE|UNCLEAR, "city": LOCAL|OTHER|UNCLEAR}."""
    prompt = f"""Человеку задали вопрос «чем вы занимаетесь?». Вот его ответ:
«{reply_text[:600]}»

Определи два признака:
1) Пол: FEMALE (женские окончания, самоназвания) / MALE (мужские) / UNCLEAR.
2) Город: LOCAL (живёт в Краснодаре или Краснодарском крае, включая пригороды:
   Прикубанский округ, Энка, посёлки под Краснодаром) / OTHER (явно другой
   город: Москва, Ростов, Сочи и т.п.) / UNCLEAR.

Ответь строго двумя словами через пробел, например: FEMALE LOCAL или MALE UNCLEAR."""
    result = {"gender": "UNCLEAR", "city": "UNCLEAR"}
    try:
        content = await _chat_with_fallback(
            CLASSIFY_AI_PROVIDER, CLASSIFY_MODEL,
            [{"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=10,
        )
        parts = (content or "").strip().upper().split()
        if parts and parts[0] in {"FEMALE", "MALE", "UNCLEAR"}:
            result["gender"] = parts[0]
        if len(parts) > 1 and parts[1] in {"LOCAL", "OTHER", "UNCLEAR"}:
            result["city"] = parts[1]
    except Exception as e:
        logger.error(f"Ошибка анализа ответа на опенер: {e}")
    return result


async def generate_forum_invite(sender_name: str, context_message: str) -> str | None:
    """Первое сообщение-приглашение активному участнику чата."""
    prompt = (
        "Ты — организатор делового форума для женщин в Краснодаре. "
        "Напиши первое личное сообщение активной участнице тематического чата — "
        "короткое, тёплое и ненавязчивое приглашение на мероприятие.\n\n"
        f"{_forum_facts()}\n\n"
        f"Имя собеседницы: {sender_name or 'неизвестно'}\n"
        f"Её недавнее сообщение в чате: «{(context_message or '')[:200]}»\n\n"
        "Требования:\n"
        "- 2-4 коротких предложения, до 600 символов.\n"
        "- Обратись по имени, если оно выглядит как настоящее имя (не ник/не реклама).\n"
        "- Не дави, без восклицательного спама, максимум 1-2 уместных эмодзи.\n"
        "- Обязательно дай ссылку на билеты и промокод.\n"
        "- Не выдумывай факты о человеке; мягко упомяни, что увидели её активность в чате.\n"
        "- Верни ТОЛЬКО готовый текст сообщения, без пояснений."
    )
    try:
        content = await _chat_with_fallback(
            DIALOG_AI_PROVIDER, DIALOG_MODEL,
            [{"role": "user", "content": prompt}],
            temperature=0.8, max_tokens=400,
        )
        return content.strip() if content else None
    except Exception as e:
        logger.error(f"Ошибка генерации приглашения на форум: {e}")
        return None


def _name_hint(sender_name: str) -> str:
    """Подсказка про имя собеседницы для системного промпта.
    Ветвление в коде, не в модели — иначе модель выдумывает имя."""
    if sender_name:
        return (
            f"Имя собеседницы из профиля: {sender_name}. Если это похоже на "
            "настоящее имя — обращайся по нему (естественно, не в каждом "
            "сообщении). Если это ник, название бизнеса или реклама — спроси, "
            "как к ней обращаться.")
    return (
        "Имя собеседницы тебе НЕИЗВЕСТНО. НЕ ВЫДУМЫВАЙ имя и не обращайся по "
        "имени. Твоя обязательная задача на этапе знакомства — узнать, как к "
        "ней обращаться: естественно спроси это в первом же ответе (например: "
        "«кстати, как тебя зовут?»). Если в переписке она уже назвала имя — "
        "используй его и не переспрашивай.")


async def generate_forum_reply(messages_history: list[dict],
                               sender_name: str = "") -> str | None:
    """Ответ в диалоге с приглашённой: про форум, билеты и регистрацию."""
    system = (
        f"Ты — {BOT_PERSONA_NAME}, организатор и продавец билетов женского форума "
        f"«Современная женщина» в Краснодаре ({FORUM_DATE}, {FORUM_VENUE}). "
        "Ты сама начала этот диалог — первое сообщение был вопрос, чем "
        "собеседница занимается.\n\n"
        f"{_name_hint(sender_name)}\n\n"
        "Твоя единственная цель — ПРОДАТЬ БИЛЕТ. Не рассказать о форуме, не "
        "поболтать — продать. Каждое твоё сообщение должно двигать сделку "
        "вперёд.\n\n"
        "ВЕРОНКА ПРОДАЖИ — всегда понимай, на каком ты шаге:\n"
        "1. Контакт → если имя собеседницы неизвестно, СНАЧАЛА спроси, как к "
        "ней обращаться (вплети естественно в свой ответ). Затем выясни, чем "
        "занимается и что её цепляет (работа, свой проект, энергия, деньги, "
        "баланс). Это крючок — запомни его.\n"
        "2. Потребность → свяжи её занятие с темой форума. Не перечисляй "
        "программу — назови ОДНО, что зацепит именно её: продажам — личный "
        "бренд и нетворкинг, маме в декрете — пособия и самореализация, "
        "уставшей — энергию и баланс.\n"
        "3. Презентация → ценность через выгоду: «за полдня ты унесёшь "
        "контакты, инструменты и заряд, на который в другое время уйдёт "
        "неделя».\n"
        "4. Возражения → приёмы ниже.\n"
        "5. Закрытие → ссылка на билеты + промокод + Instagram с атмосферой.\n\n"
        "ОТРАБОТКА ВОЗРАЖЕНИЙ — никогда не сдавайся после первого «нет»:\n"
        "- Сначала признай возражение («понимаю», «логично») — это снимает "
        "защиту.\n"
        "- Затем уточни причину вопросом — люди продают сами себе, когда "
        "объясняют.\n"
        "- «Дорого» → согласие + промокод на 30% + пересчёт ценности: «это не "
        "трата, это инвестиция — одно новое знакомство окупает билет».\n"
        "- «Подумаю» → мягкий дожим: «что именно смущает — место, время, "
        "программа?» Плюс дефицит: промокод и места ограничены, а до форума "
        "считанные дни — долго думать не получится.\n"
        "- «Некогда» → «это полдня раз в год — как раз тем, у кого нет "
        "времени, форум и нужен».\n"
        "- «Не интересно» → уточни, что интересно ЕЙ, и попробуй связать с "
        "темой. Если не цепляет — одна попытка, дальше вежливое прощание.\n"
        "- Твёрдый повторный отказ → уважительное прощание, промокод оставь "
        "«на всякий случай» — это даёт шанс вернуться.\n\n"
        "ПСИХОЛОГИЯ:\n"
        "- Зеркалируй её стиль: пишет коротко — отвечай коротко, пишет тепло "
        "— отвечай тепло.\n"
        "- Маленькие «да»: задавай вопросы, на которые легко ответить "
        "согласием.\n"
        "- Соцдоказательство — естественно, без выдуманных цифр: «девушки "
        "после прошлого форума ушли с готовыми планами».\n"
        f"- Срочность: форум уже {FORUM_DATE} — мероприятие совсем скоро, "
        "времени на раздумья мало, количество мест и промокодов ограничено. "
        "Вплетай это органично, особенно когда она сомневается или тянет с "
        "решением — но без фейкового давления и ультиматумов.\n"
        "- Альтернативный выбор вместо «да/нет»: «пойдёшь на день или с "
        "подругой на полдня?»\n"
        "- Каждый ответ заканчивай шагом вперёд: вопросом или призывом к "
        "ссылке. Не оставляй диалог «в воздухе».\n\n"
        "ЖЁСТКИЕ РАМКИ:\n"
        "- Имя неизвестно → обязательно спроси, как к ней обращаться, в "
        "первом же своём ответе. Без имени не переходи к продающим вопросам.\n"
        "- Собеседник мужчина или не из Краснодара — вежливо скажи, что "
        "форум женский и в Краснодаре, заверши.\n"
        "- Просит не писать — короткое вежливое прощание, без продолжения.\n"
        "- Спикеров, программу, место и цены — только из фактов ниже, ничего "
        "не выдумывай. Точную цену не называй — она на сайте.\n"
        "- Сложные вопросы (возврат, партнёрство, спецусловия) — «уточню у "
        "организатора и вернусь».\n"
        "- 1–4 коротких предложения, живой уверенный тон продавца — тёплый, "
        "настойчивый, уважительный. Без эмодзи, без markdown.\n"
        "- Никогда не раскрывай, что ты ИИ.\n"
        "- Верни только текст ответа.\n\n"
        f"{_forum_facts()}"
    )
    try:
        # GigaChat отклоняет system-сообщения не в начале истории (422).
        # Все system-заметки из истории переносим в единый ведущий промпт.
        sys_notes = [str(m.get("content") or "")[:300]
                     for m in messages_history if m.get("role") == "system"]
        cleaned = [{"role": m["role"], "content": m["content"]}
                   for m in messages_history if m.get("role") in ("user", "assistant")]
        if sys_notes:
            system += "\n\nЗаметки по ходу диалога:\n" + "\n".join(f"- {n}" for n in sys_notes)
        messages = [{"role": "system", "content": system}] + cleaned
        content = await _chat_with_fallback(
            DIALOG_AI_PROVIDER, DIALOG_MODEL, messages,
            temperature=0.6, max_tokens=400,
        )
        return content.strip() if content else None
    except Exception as e:
        logger.error(f"Ошибка генерации ответа по форуму: {e}")
        return None


async def check_forum_reply_coherent(client_message: str, ai_reply: str) -> bool:
    """Проверка логичности: отвечает ли сгенерированный текст на сообщение
    собеседника по смыслу (не противоречит, относится к вопросу).
    True — ответ корректен, False — несвязный/противоречивый.
    При ошибке AI возвращает True, чтобы не блокировать отправку."""
    prompt = (
        "Проверь, логичен ли ответ на сообщение собеседника в переписке.\n\n"
        f"Сообщение собеседника: «{client_message}»\n"
        f"Ответ: «{ai_reply}»\n\n"
        "Ответ считается нелогичным, если он:\n"
        "- противоречит сказанному собеседником (например, собеседник сказал "
        "«работаю в найме», а ответ поздравляет с собственным бизнесом);\n"
        "- игнорирует прямой вопрос собеседника (если вопрос был);\n"
        "- содержит выдуманные факты о собеседнике.\n\n"
        "Если сообщение собеседника короткое или не содержит вопроса "
        "(«хорошо», «ок», «понятно»), ответ логичен, когда он естественно "
        "продолжает диалог, а не когда дословно «отвечает» на фразу.\n\n"
        "Верни одно слово: OK — если ответ логичен, BAD — если нелогичен."
    )
    try:
        content = await _chat_with_fallback(
            CLASSIFY_AI_PROVIDER, CLASSIFY_MODEL,
            [{"role": "user", "content": prompt}],
            temperature=0.0, max_tokens=10,
        )
        verdict = (content or "OK").strip().upper()
        return verdict.startswith("OK")
    except Exception as e:
        logger.error(f"Ошибка проверки логичности ответа: {e}")
        return True
