"""Десктоп-интерфейс управления чатами (тонкий клиент).

Вся работа (Telethon, бот уведомлений, планировщик, AI) идёт на СЕРВЕРЕ.
Эта программа только управляет настройками через HTTP API сервера,
поэтому все изменения сохраняются на сервере и не слетают при перезапуске.

Настройка: в .env рядом с exe укажите
    SERVER_URL=http://82.202.170.14:8080
    API_TOKEN=<токен с сервера>

Запуск: python main_gui.py  или  LeadHunter.exe
"""
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.request
import urllib.error
import tkinter as tk
from tkinter import ttk, messagebox

# .env и gui.log лежат рядом с exe (или со скриптом)
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(BASE_DIR, "gui.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger("main_gui")

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE_DIR, ".env"))

SERVER_URL = os.getenv("SERVER_URL", "").rstrip("/")
API_TOKEN = os.getenv("API_TOKEN", "")
NO_RULES_MARKER = "NO_RULES"

BROADCAST_LANGUAGES = {
    "ru": "Русский",
    "en": "English",
    "ar": "العربية",
    "pt": "Português",
    "id": "Indonesia",
    "kz": "Қазақша",
    "uz": "O'zbekcha",
    "az": "Azərbaycan",
}

TIMES_RE = re.compile(r"^\s*([01]?\d|2[0-3]):[0-5]\d(\s*,\s*([01]?\d|2[0-3]):[0-5]\d)*\s*$")

# ═══════════════════════════════════════════════════════════════
# БРУТАЛИСТСКАЯ ЦВЕТОВАЯ СИСТЕМА
# ═══════════════════════════════════════════════════════════════

C_BG = "#FFFFFF"          # основной фон
C_BLACK = "#0A0A0A"       # чёрный (текст, шапка, кнопки)
C_ACCENT = "#FF3B00"      # оранжевый акцент
C_ACCENT_DIM = "#CC2F00"  # тёмный оранжевый (hover)
C_GRAY = "#E8E8E8"        # светло-серый (поля ввода)
C_GRAY_DARK = "#999999"   # тёмно-серый (вторичный текст)
C_GRAY_BORDER = "#D0D0D0" # границы секций
C_GREEN = "#008C2E"       # зелёный (статус OK)
C_RED = "#CC0000"         # красный (ошибка)
C_YELLOW = "#FFD600"      # жёлтый (предупреждение)
C_PANEL = "#F5F5F5"       # фон панелей
C_DISABLED = "#F0F0F0"    # отключенные поля

# Шрифты
F_TITLE = ("Consolas", 16, "bold")
F_HEADER = ("Consolas", 11, "bold")
F_BODY = ("Segoe UI", 10)
F_BODY_BOLD = ("Segoe UI", 10, "bold")
F_SMALL = ("Segoe UI", 9)
F_SMALL_BOLD = ("Segoe UI", 9, "bold")
F_MONO = ("Consolas", 10)
F_BTN = ("Segoe UI", 10, "bold")
F_STATUS = ("Consolas", 9, "bold")

BORDER_W = 2  # толщина границ


# ═══════════════════════════════════════════════════════════════
# БРУТАЛИСТСКИЕ ВИДЖЕТЫ
# ═══════════════════════════════════════════════════════════════

class BFrame(tk.Frame):
    """Фрейм с чёрной границей."""
    def __init__(self, parent, bg=C_BG, border=True, **kw):
        kw.setdefault("bg", bg)
        if border:
            kw.setdefault("highlightbackground", C_BLACK)
            kw.setdefault("highlightthickness", BORDER_W)
        super().__init__(parent, **kw)


class BButton(tk.Button):
    """Бруталистская кнопка: чёрный фон, белый текст, плоская."""
    def __init__(self, parent, text, command=None, style="primary", **kw):
        if style == "primary":
            bg, fg, abg = C_BLACK, "#FFFFFF", C_ACCENT
        elif style == "accent":
            bg, fg, abg = C_ACCENT, "#FFFFFF", C_ACCENT_DIM
        elif style == "danger":
            bg, fg, abg = C_RED, "#FFFFFF", "#A00000"
        elif style == "ghost":
            bg, fg, abg = C_BG, C_BLACK, C_GRAY
        else:
            bg, fg, abg = C_BLACK, "#FFFFFF", C_ACCENT
        kw.setdefault("text", text)
        kw.setdefault("command", command)
        kw.setdefault("bg", bg)
        kw.setdefault("fg", fg)
        kw.setdefault("activebackground", abg)
        kw.setdefault("activeforeground", "#FFFFFF")
        kw.setdefault("font", F_BTN)
        kw.setdefault("relief", tk.FLAT)
        kw.setdefault("cursor", "hand2")
        kw.setdefault("borderwidth", 0)
        kw.setdefault("padx", 16)
        kw.setdefault("pady", 8)
        super().__init__(parent, **kw)


class BLabel(tk.Label):
    """Бруталистская метка."""
    def __init__(self, parent, text="", font=F_BODY, fg=C_BLACK, bg=C_BG, **kw):
        kw.setdefault("text", text)
        kw.setdefault("font", font)
        kw.setdefault("fg", fg)
        kw.setdefault("bg", bg)
        kw.setdefault("anchor", tk.W)
        super().__init__(parent, **kw)


class BEntry(tk.Entry):
    """Поле ввода с чёрной границей."""
    def __init__(self, parent, **kw):
        kw.setdefault("font", F_BODY)
        kw.setdefault("bg", C_BG)
        kw.setdefault("fg", C_BLACK)
        kw.setdefault("insertbackground", C_BLACK)
        kw.setdefault("relief", tk.FLAT)
        kw.setdefault("highlightbackground", C_BLACK)
        kw.setdefault("highlightthickness", 1)
        kw.setdefault("highlightcolor", C_ACCENT)
        super().__init__(parent, **kw)


class BCheckbutton(tk.Checkbutton):
    """Чекбокс: чёрный акцент."""
    def __init__(self, parent, text="", **kw):
        kw.setdefault("text", text)
        kw.setdefault("font", F_BODY)
        kw.setdefault("bg", C_BG)
        kw.setdefault("fg", C_BLACK)
        kw.setdefault("activebackground", C_BG)
        kw.setdefault("activeforeground", C_BLACK)
        kw.setdefault("selectcolor", C_BG)
        kw.setdefault("relief", tk.FLAT)
        super().__init__(parent, **kw)


class BRadiobutton(tk.Radiobutton):
    """Радиокнопка."""
    def __init__(self, parent, text="", **kw):
        kw.setdefault("text", text)
        kw.setdefault("font", F_BODY)
        kw.setdefault("bg", C_BG)
        kw.setdefault("fg", C_BLACK)
        kw.setdefault("activebackground", C_BG)
        kw.setdefault("activeforeground", C_BLACK)
        kw.setdefault("selectcolor", C_BG)
        super().__init__(parent, **kw)


class BCombobox(ttk.Combobox):
    """Combobox с бруталистской темой."""
    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)


class BText(tk.Text):
    """Многострочное поле с чёрной границей."""
    def __init__(self, parent, **kw):
        kw.setdefault("font", F_MONO)
        kw.setdefault("bg", C_BG)
        kw.setdefault("fg", C_BLACK)
        kw.setdefault("insertbackground", C_BLACK)
        kw.setdefault("relief", tk.FLAT)
        kw.setdefault("highlightbackground", C_BLACK)
        kw.setdefault("highlightthickness", 1)
        kw.setdefault("highlightcolor", C_ACCENT)
        kw.setdefault("padx", 6)
        kw.setdefault("pady", 4)
        super().__init__(parent, **kw)


class ScrollableFrame(tk.Frame):
    """Фрейм со скроллингом — для настроек, когда окно маленькое."""
    def __init__(self, parent, bg=C_BG, **kw):
        kw.setdefault("bg", bg)
        super().__init__(parent, **kw)

        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0,
                                borderwidth=0)
        self.scrollbar = tk.Scrollbar(self, orient=tk.VERTICAL,
                                      command=self.canvas.yview,
                                      bg=C_BLACK, troughcolor=C_GRAY)
        self.scrollable = tk.Frame(self.canvas, bg=bg)

        self.scrollable.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self._inner_id = self.canvas.create_window((0, 0), window=self.scrollable,
                                                    anchor=tk.NW)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.bind("<Configure>", self._on_canvas_configure)

        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Скролл колесом мыши
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind_all("<Button-4>", self._on_mousewheel)
        self.canvas.bind_all("<Button-5>", self._on_mousewheel)

    def _on_canvas_configure(self, event):
        # Растягиваем внутренний фрейм по ширине canvas
        self.canvas.itemconfig(self._inner_id, width=event.width)

    def _on_mousewheel(self, event):
        if sys.platform == "win32":
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        else:
            if event.num == 4:
                self.canvas.yview_scroll(-1, "units")
            elif event.num == 5:
                self.canvas.yview_scroll(1, "units")


class MessageDialog(tk.Toplevel):
    """Диалог с копируемым текстом — для ошибок и тестовых сообщений."""
    def __init__(self, parent, title, message, style="info"):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=C_BG)
        self.transient(parent)
        self.grab_set()

        # Размер по длине текста
        lines = message.split("\n")
        max_len = max(len(l) for l in lines) if lines else 40
        width = min(max(max_len + 4, 50), 90)
        height = min(max(len(lines) + 6, 6), 30)
        self.geometry(f"{width * 9}x{height * 22}")

        # Шапка
        if style == "error":
            header_bg, header_fg, header_text = C_RED, "#FFFFFF", "ОШИБКА"
        elif style == "warn":
            header_bg, header_fg, header_text = C_YELLOW, C_BLACK, "ПРЕДУПРЕЖДЕНИЕ"
        elif style == "success":
            header_bg, header_fg, header_text = C_GREEN, "#FFFFFF", "РЕЗУЛЬТАТ"
        else:
            header_bg, header_fg, header_text = C_BLACK, "#FFFFFF", "ИНФО"

        header = tk.Frame(self, bg=header_bg, height=36)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text=f"  {header_text}", font=F_HEADER,
                 bg=header_bg, fg=header_fg, anchor=tk.W).pack(
            side=tk.LEFT, fill=tk.X, expand=True, pady=8)

        # Текстовое поле — копируемое
        text_frame = tk.Frame(self, bg=C_BG, highlightbackground=C_BLACK,
                              highlightthickness=BORDER_W)
        text_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        text = BText(text_frame, wrap=tk.WORD, height=height - 6)
        text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        text.insert("1.0", message)
        text.configure(state=tk.NORMAL)  # оставляем selectable

        # Кнопки
        btn_frame = tk.Frame(self, bg=C_BG)
        btn_frame.pack(fill=tk.X, padx=8, pady=(0, 8))

        BButton(btn_frame, "КОПИРОВАТЬ", command=lambda: self._copy(text),
                style="accent").pack(side=tk.LEFT)
        BButton(btn_frame, "ЗАКРЫТЬ", command=self.destroy,
                style="primary").pack(side=tk.RIGHT)

        # Enter / Esc закрывают
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Return>", lambda e: self.destroy())

        self.focus_set()
        text.focus_set()

    def _copy(self, text_widget):
        content = text_widget.get("1.0", tk.END).strip()
        self.clipboard_clear()
        self.clipboard_append(content)
        self.update()


# ═══════════════════════════════════════════════════════════════
# HTTP КЛИЕНТ
# ═══════════════════════════════════════════════════════════════

class ServerAPI:
    """HTTP-клиент к API сервера (urllib, без внешних зависимостей)."""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url
        self.token = token

    def _request(self, method: str, path: str, payload: dict | None = None,
                 timeout: int = 90, retries: int = 2) -> dict:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-API-Token"] = self.token
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_err = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError:
                raise  # 401 и прочие ответы сервера не ретраим
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last_err = e
                logger.warning(f"{method} {path}: попытка {attempt + 1} не удалась: {e}")
                if attempt < retries:
                    time.sleep(2)
        raise last_err

    def get_chats(self) -> list[dict]:
        return self._request("GET", "/api/chats")["chats"]

    def update_chat(self, chat_data: dict) -> bool:
        return self._request("POST", "/api/chats", chat_data).get("success", False)

    def sync_dialogs(self) -> int:
        return self._request("POST", "/api/sync_dialogs", {},
                             timeout=300, retries=0).get("count", 0)

    def preview_broadcast(self, chat_id: int) -> dict | None:
        return self._request("POST", "/api/preview_broadcast",
                             {"chat_id": chat_id}, timeout=300, retries=0).get("result")

    def get_settings(self) -> dict:
        return self._request("GET", "/api/settings")

    def set_settings(self, settings: dict) -> bool:
        return self._request("POST", "/api/settings", settings).get("success", False)

    def get_status(self) -> dict:
        return self._request("GET", "/api/status", timeout=30)

    def pause_parsing(self) -> dict:
        return self._request("POST", "/api/pause", {})

    def resume_parsing(self) -> dict:
        return self._request("POST", "/api/resume", {})

    def restart_server(self) -> dict:
        """Перезапустить сервис на сервере (ручной рестарт при отвале Telethon)."""
        return self._request("POST", "/api/restart", {}, timeout=15, retries=0)


# ═══════════════════════════════════════════════════════════════
# ГЛАВНОЕ ПРИЛОЖЕНИЕ
# ═══════════════════════════════════════════════════════════════

class App:
    def __init__(self, root: tk.Tk, api: ServerAPI):
        self.root = root
        self.api = api
        self.chats: dict[int, dict] = {}
        self.current_chat_id: int | None = None
        self._all_chats: list[dict] = []

        root.title("LEAD HUNTER")
        root.configure(bg=C_BG)
        root.geometry("1200x750")
        root.minsize(900, 500)

        self._setup_theme()
        self._build_ui()
        self._connect_to_server()

    def _setup_theme(self):
        """Настройка ttk стиля под брутализм."""
        style = ttk.Style()
        style.theme_use("clam")

        # Treeview
        style.configure("Treeview",
                        background=C_BG,
                        foreground=C_BLACK,
                        fieldbackground=C_BG,
                        bordercolor=C_BLACK,
                        borderwidth=2,
                        font=F_BODY,
                        rowheight=28,
                        selectbackground=C_BLACK,
                        selectforeground=C_ACCENT)
        style.configure("Treeview.Heading",
                        background=C_BLACK,
                        foreground="#FFFFFF",
                        font=F_SMALL_BOLD,
                        borderwidth=1,
                        relief=tk.FLAT)
        style.map("Treeview.Heading",
                  background=[("active", C_ACCENT)])

        # Scrollbar
        style.configure("Vertical.TScrollbar",
                        background=C_BLACK,
                        troughcolor=C_GRAY,
                        bordercolor=C_BLACK,
                        arrowcolor="#FFFFFF")

        # Combobox
        style.configure("TCombobox",
                        fieldbackground=C_BG,
                        background=C_BLACK,
                        foreground=C_BLACK,
                        bordercolor=C_BLACK,
                        borderwidth=2,
                        arrowcolor=C_BLACK,
                        padding=4)
        style.map("TCombobox",
                  fieldbackground=[("readonly", C_BG)],
                  selectbackground=[("readonly", C_BLACK)],
                  selectforeground=[("readonly", "#FFFFFF")])
        style.configure("TCombobox.Field",
                        fieldbackground=C_BG,
                        background=C_BG,
                        bordercolor=C_BLACK,
                        borderwidth=1)
        root = self.root
        root.option_add("*TCombobox*Listbox.background", C_BG)
        root.option_add("*TCombobox*Listbox.foreground", C_BLACK)
        root.option_add("*TCombobox*Listbox.selectBackground", C_BLACK)
        root.option_add("*TCombobox*Listbox.selectForeground", C_ACCENT)
        root.option_add("*TCombobox*Listbox.font", F_BODY)

    # === UI ===

    def _build_ui(self):
        # ── ШАПКА ──
        header = tk.Frame(self.root, bg=C_BLACK, height=52)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        tk.Label(header, text="LEAD HUNTER",
                 font=F_TITLE, bg=C_BLACK, fg=C_ACCENT,
                 anchor=tk.W).pack(side=tk.LEFT, padx=16, pady=10)

        tk.Label(header, text="панель управления",
                 font=F_SMALL, bg=C_BLACK, fg="#888888",
                 anchor=tk.W).pack(side=tk.LEFT, pady=10)

        # ── ОСНОВНАЯ ОБЛАСТЬ ──
        body = tk.Frame(self.root, bg=C_BG)
        body.pack(fill=tk.BOTH, expand=True)

        # Левая колонка — список чатов
        left = BFrame(body, bg=C_BG, border=False)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 0))

        # Заголовок левой панели
        left_header = tk.Frame(left, bg=C_BLACK, height=32)
        left_header.pack(fill=tk.X)
        left_header.pack_propagate(False)
        tk.Label(left_header, text="  ЧАТЫ",
                 font=F_HEADER, bg=C_BLACK, fg="#FFFFFF",
                 anchor=tk.W).pack(side=tk.LEFT, pady=6)

        # Поиск
        search_frame = tk.Frame(left, bg=C_BG)
        search_frame.pack(fill=tk.X, padx=8, pady=8)
        BLabel(search_frame, text="ПОИСК", font=F_SMALL_BOLD, fg=C_GRAY_DARK,
               bg=C_BG).pack(anchor=tk.W)
        self.search_var = tk.StringVar()
        self.search_entry = BEntry(search_frame, textvariable=self.search_var)
        self.search_entry.pack(fill=tk.X, ipady=4)
        self.search_entry.bind("<KeyRelease>", self._filter_chats)
        # Контекстное меню для поиска (копировать/вставить/вырезать)
        self.search_entry.bind("<Button-3>", self._show_search_context_menu)
        self.search_entry.bind("<Button-2>", self._show_search_context_menu)

        # Список чатов
        tree_frame = tk.Frame(left, bg=C_BG)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        columns = ("read", "broadcast", "times")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="tree headings",
                                 selectmode="browse")
        self.tree.heading("#0", text="ЧАТ")
        self.tree.heading("read", text="ЧИТ.")
        self.tree.heading("broadcast", text="РАСС.")
        self.tree.heading("times", text="ВРЕМЯ")
        self.tree.column("#0", width=320, anchor=tk.W)
        self.tree.column("read", width=60, anchor=tk.CENTER)
        self.tree.column("broadcast", width=60, anchor=tk.CENTER)
        self.tree.column("times", width=100, anchor=tk.CENTER)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        tree_scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL,
                                    command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # ── ПРАВАЯ КОЛОНКА — НАСТРОЙКИ (СКРОЛЛИТСЯ) ──
        right_outer = tk.Frame(body, bg=C_BG, highlightbackground=C_BLACK,
                               highlightthickness=BORDER_W)
        right_outer.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        # Заголовок правой панели
        right_header = tk.Frame(right_outer, bg=C_BLACK, height=32)
        right_header.pack(fill=tk.X)
        right_header.pack_propagate(False)
        tk.Label(right_header, text="  НАСТРОЙКИ",
                 font=F_HEADER, bg=C_BLACK, fg="#FFFFFF",
                 anchor=tk.W).pack(side=tk.LEFT, pady=6)
        # Кнопки СИНХРОН / ОБНОВИТЬ — в заголовке, всегда видны
        self.sync_btn = BButton(right_header, "СИНХРОН", command=self.sync_dialogs, style="ghost")
        self.sync_btn.pack(side=tk.RIGHT, padx=(0, 4), pady=4)
        self.reload_btn = BButton(right_header, "ОБНОВИТЬ", command=self.reload_chat_list, style="ghost")
        self.reload_btn.pack(side=tk.RIGHT, padx=(0, 4), pady=4)

        # Скроллируемый контейнер
        scroll_area = ScrollableFrame(right_outer, bg=C_BG)
        scroll_area.pack(fill=tk.BOTH, expand=True)
        right = scroll_area.scrollable

        # Название чата
        self.chat_title_var = tk.StringVar(value="Выберите чат →")
        tk.Label(right, textvariable=self.chat_title_var,
                 font=F_HEADER, bg=C_BG, fg=C_BLACK,
                 anchor=tk.W, wraplength=380).pack(fill=tk.X, padx=16, pady=(12, 4))

        # Разделитель
        tk.Frame(right, bg=C_BLACK, height=2).pack(fill=tk.X, padx=16, pady=(0, 12))

        # Флажки
        self.read_var = tk.BooleanVar()
        self.bcast_var = tk.BooleanVar()
        self.direct_var = tk.BooleanVar()
        BCheckbutton(right, text="ЧИТАТЬ — искать лидов в этом чате",
                     variable=self.read_var).pack(anchor=tk.W, padx=16, pady=2)
        BCheckbutton(right, text="РАССЫЛКА — AI пишет рекламу в этот чат",
                     variable=self.bcast_var).pack(anchor=tk.W, padx=16, pady=2)
        BCheckbutton(right, text="ПРЯМАЯ РЕКЛАМА — открыто предлагаю услуги (без правил)",
                     variable=self.direct_var).pack(anchor=tk.W, padx=16, pady=2)

        # Пол разработчика
        tk.Frame(right, bg=C_BLACK, height=1).pack(fill=tk.X, padx=16, pady=12)
        BLabel(right, text="ПОЛ РАЗРАБОТЧИКА", font=F_SMALL_BOLD, fg=C_GRAY_DARK,
               bg=C_BG).pack(anchor=tk.W, padx=16, pady=(0, 4))
        gender_frame = tk.Frame(right, bg=C_BG)
        gender_frame.pack(anchor=tk.W, padx=16)
        self.gender_var = tk.StringVar(value="male")
        BRadiobutton(gender_frame, text="Мужской", variable=self.gender_var,
                     value="male", command=self.save_gender).pack(side=tk.LEFT, padx=(0, 16))
        BRadiobutton(gender_frame, text="Женский", variable=self.gender_var,
                     value="female", command=self.save_gender).pack(side=tk.LEFT)

        # Тематика
        tk.Frame(right, bg=C_BLACK, height=1).pack(fill=tk.X, padx=16, pady=12)
        BLabel(right, text="ТЕМАТИКА ЧАТА", font=F_SMALL_BOLD, fg=C_GRAY_DARK,
               bg=C_BG).pack(anchor=tk.W, padx=16, pady=(0, 4))
        self.niche_var = tk.StringVar()
        BEntry(right, textvariable=self.niche_var).pack(fill=tk.X, padx=16, ipady=4)
        BLabel(right, text="Например: «рестораны», «недвижимость», «крипта P2P». "
                           "Пусто — AI определит по названию и сообщениям чата.",
               font=F_SMALL, fg=C_GRAY_DARK, bg=C_BG,
               wraplength=360).pack(anchor=tk.W, padx=16, pady=(4, 0))

        # Язык
        BLabel(right, text="ЯЗЫК РАССЫЛКИ", font=F_SMALL_BOLD, fg=C_GRAY_DARK,
               bg=C_BG).pack(anchor=tk.W, padx=16, pady=(12, 4))
        lang_frame = tk.Frame(right, bg=C_BG)
        lang_frame.pack(fill=tk.X, padx=16)
        self.lang_var = tk.StringVar(value="ru")
        lang_codes = list(BROADCAST_LANGUAGES.keys())
        lang_values = [f"{code} — {BROADCAST_LANGUAGES[code]}" for code in lang_codes]
        self.lang_combo = BCombobox(lang_frame, textvariable=self.lang_var,
                                    values=lang_values, state="readonly")
        self.lang_combo.pack(fill=tk.X, ipady=2)
        self.lang_combo.bind("<<ComboboxSelected>>", self._on_lang_select)

        # Правила
        tk.Frame(right, bg=C_BLACK, height=1).pack(fill=tk.X, padx=16, pady=12)
        BLabel(right, text="ПРАВИЛА ЧАТА", font=F_SMALL_BOLD, fg=C_GRAY_DARK,
               bg=C_BG).pack(anchor=tk.W, padx=16, pady=(0, 4))
        self.rules_text = BText(right, height=8, width=50, wrap=tk.WORD)
        self.rules_text.pack(fill=tk.X, padx=16, pady=(0, 4))

        # Контекстное меню для копирования/вставки
        self.context_menu = tk.Menu(self.root, tearoff=0, bg=C_BG, fg=C_BLACK,
                                    activebackground=C_BLACK,
                                    activeforeground=C_ACCENT,
                                    borderwidth=2)
        self.context_menu.add_command(label="Копировать", command=self._copy_text)
        self.context_menu.add_command(label="Вставить", command=self._paste_text)
        self.context_menu.add_command(label="Вырезать", command=self._cut_text)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Очистить", command=self._clear_text)

        self.rules_text.bind("<Button-3>", self._show_context_menu)
        self.rules_text.bind("<Button-2>", self._show_context_menu)

        self.no_rules_var = tk.BooleanVar()
        BCheckbutton(right, text="ПРАВИЛ НЕТ — я проверил, в чате нет правил",
                     variable=self.no_rules_var,
                     command=self._toggle_no_rules).pack(anchor=tk.W, padx=16, pady=4)

        # Время рассылки
        tk.Frame(right, bg=C_BLACK, height=1).pack(fill=tk.X, padx=16, pady=12)
        BLabel(right, text="ВРЕМЯ РАССЫЛКИ", font=F_SMALL_BOLD, fg=C_GRAY_DARK,
               bg=C_BG).pack(anchor=tk.W, padx=16, pady=(0, 4))
        BLabel(right, text="Формат: 10:00, 19:30", font=F_SMALL, fg=C_GRAY_DARK,
               bg=C_BG).pack(anchor=tk.W, padx=16, pady=(0, 2))
        self.times_var = tk.StringVar()
        BEntry(right, textvariable=self.times_var).pack(fill=tk.X, padx=16, ipady=4)

        # Кнопки SAVE / TEST
        tk.Frame(right, bg=C_BLACK, height=1).pack(fill=tk.X, padx=16, pady=12)
        btns1 = tk.Frame(right, bg=C_BG)
        btns1.pack(fill=tk.X, padx=16, pady=(8, 4))
        self.save_btn = BButton(btns1, "СОХРАНИТЬ", command=self.save_chat, style="primary")
        self.save_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.test_btn = BButton(btns1, "ТЕСТ", command=self.test_broadcast, style="accent")
        self.test_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))

        # Кнопки СИНХРОН/ОБНОВИТЬ перенесены в заголовок панели (всегда видны)

        # Парсинг
        tk.Frame(right, bg=C_BLACK, height=1).pack(fill=tk.X, padx=16, pady=12)
        BLabel(right, text="УПРАВЛЕНИЕ ПАРСИНГОМ", font=F_SMALL_BOLD, fg=C_GRAY_DARK,
               bg=C_BG).pack(anchor=tk.W, padx=16, pady=(0, 4))

        self.parse_state_var = tk.StringVar(value="... проверка статуса")
        tk.Label(right, textvariable=self.parse_state_var,
                 font=F_STATUS, bg=C_BG, fg=C_BLACK,
                 anchor=tk.W).pack(anchor=tk.W, padx=16, pady=(0, 6))

        parse_btns = tk.Frame(right, bg=C_BG)
        parse_btns.pack(fill=tk.X, padx=16, pady=(0, 16))
        self.pause_btn = BButton(parse_btns, "СТОП", command=self.pause_parsing,
                                 style="danger")
        self.pause_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.resume_btn = BButton(parse_btns, "ЗАПУСК", command=self.resume_parsing,
                                  style="primary")
        self.resume_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))
        self.restart_btn = BButton(parse_btns, "ПЕРЕЗАПУСК", command=self.restart_server,
                                   style="ghost")
        self.restart_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))

        # ── СТАТУС-БАР ──
        self.status_var = tk.StringVar(value=f"Подключение к {SERVER_URL}...")
        status_bar = tk.Frame(self.root, bg=C_BLACK, height=28)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        status_bar.pack_propagate(False)
        tk.Label(status_bar, textvariable=self.status_var,
                 font=F_STATUS, bg=C_BLACK, fg="#FFFFFF",
                 anchor=tk.W).pack(side=tk.LEFT, padx=12, pady=5)

        self._set_controls_enabled(False)

    def _set_controls_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for btn in (self.save_btn, self.test_btn, self.sync_btn, self.reload_btn,
                    self.pause_btn, self.resume_btn, self.restart_btn):
            btn.configure(state=state)

    def _toggle_no_rules(self):
        if self.no_rules_var.get():
            self.rules_text.configure(state=tk.DISABLED, bg=C_DISABLED)
        else:
            self.rules_text.configure(state=tk.NORMAL, bg=C_BG)

    def _show_context_menu(self, event):
        self.context_menu.post(event.x_root, event.y_root)

    def _show_search_context_menu(self, event):
        """Контекстное меню для поля поиска."""
        menu = tk.Menu(self.root, tearoff=0, bg=C_BG, fg=C_BLACK,
                       activebackground=C_BLACK, activeforeground=C_ACCENT,
                       borderwidth=2)
        menu.add_command(label="Копировать", command=self._copy_search)
        menu.add_command(label="Вставить", command=self._paste_search)
        menu.add_command(label="Вырезать", command=self._cut_search)
        menu.add_separator()
        menu.add_command(label="Очистить", command=self._clear_search)
        menu.post(event.x_root, event.y_root)

    def _copy_search(self):
        try:
            selected = self.search_entry.get()
            if self.search_entry.selection_present():
                selected = self.search_entry.selection_get()
            self.root.clipboard_clear()
            self.root.clipboard_append(selected)
        except tk.TclError:
            pass

    def _paste_search(self):
        try:
            text = self.root.clipboard_get()
            self.search_entry.insert(tk.INSERT, text)
            self._filter_chats()
        except tk.TclError:
            pass

    def _cut_search(self):
        try:
            selected = self.search_entry.selection_get()
            self.root.clipboard_clear()
            self.root.clipboard_append(selected)
            self.search_entry.delete("sel.first", "sel.last")
            self._filter_chats()
        except tk.TclError:
            pass

    def _clear_search(self):
        self.search_var.set("")
        self._filter_chats()

    def _copy_text(self):
        try:
            selected = self.rules_text.get("sel.first", "sel.last")
            self.root.clipboard_clear()
            self.root.clipboard_append(selected)
        except tk.TclError:
            pass

    def _paste_text(self):
        try:
            text = self.root.clipboard_get()
            self.rules_text.insert(tk.INSERT, text)
        except tk.TclError:
            pass

    def _cut_text(self):
        try:
            selected = self.rules_text.get("sel.first", "sel.last")
            self.root.clipboard_clear()
            self.root.clipboard_append(selected)
            self.rules_text.delete("sel.first", "sel.last")
        except tk.TclError:
            pass

    def _clear_text(self):
        self.rules_text.delete("1.0", tk.END)

    def _filter_chats(self, _event=None):
        search_text = self.search_var.get().lower()
        filtered = self._all_chats
        if search_text:
            filtered = [c for c in self._all_chats
                        if search_text in (c.get("chat_name") or "").lower()]
        self._fill_tree(filtered)
        # Возвращаем фокус в поле поиска, чтобы Ctrl+V работал
        self.search_entry.focus_set()
        # Ставим курсор в конец текста
        self.search_entry.icursor(tk.END)

    def _fill_tree(self, chats: list[dict]):
        selected = self.current_chat_id
        self.tree.delete(*self.tree.get_children())
        for c in chats:
            read_mark = "+" if c.get("is_monitored") else "-"
            bcast_mark = "+" if c.get("is_broadcast") else "-"
            times = c.get("broadcast_times") or ""
            self.tree.insert("", tk.END, iid=str(c["chat_id"]),
                             text=c.get("chat_name") or str(c["chat_id"]),
                             values=(read_mark, bcast_mark, times))
        if selected and str(selected) in self.tree.get_children():
            self.tree.selection_set(str(selected))

    # === Работа с сервером (фоновые потоки, GUI не блокируется) ===

    def run_bg(self, func, on_done=None, on_error=None):
        """Выполняет func() в фоне, результат возвращает в GUI-поток."""
        def worker():
            try:
                result = func()
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    err = Exception("Сервер отклонил доступ (401): проверьте API_TOKEN в .env")
                else:
                    err = Exception(f"Ошибка сервера: HTTP {e.code}")
                logger.error(f"HTTP ошибка: {e}")
                self.root.after(0, lambda: (on_error or self._default_error)(err))
                return
            except urllib.error.URLError as e:
                err = Exception(f"Нет связи с сервером {SERVER_URL}:\n{e.reason}")
                logger.error(f"Сервер недоступен: {e}")
                self.root.after(0, lambda: (on_error or self._default_error)(err))
                return
            except (TimeoutError, ConnectionError, OSError) as e:
                err = Exception(
                    f"Сервер {SERVER_URL} не ответил вовремя.\n"
                    f"Попробуйте ещё раз через несколько секунд\n"
                    f"(кнопка RELOAD).\n\nДетали: {e}"
                )
                logger.error(f"Таймаут/обрыв связи: {e}")
                self.root.after(0, lambda: (on_error or self._default_error)(err))
                return
            except Exception as e:
                logger.error(f"Ошибка фоновой операции: {e}", exc_info=True)
                self.root.after(0, lambda: (on_error or self._default_error)(e))
                return
            if on_done:
                self.root.after(0, lambda: on_done(result))

        threading.Thread(target=worker, daemon=True).start()

    def _default_error(self, e: Exception):
        self.status_var.set("ОШИБКА — нет связи с сервером")
        MessageDialog(self.root, "Ошибка", str(e), style="error")

    def _connect_to_server(self):
        """Первое подключение: статус + настройки + список чатов."""
        def load():
            status = self.api.get_status()
            settings = self.api.get_settings()
            chats = self.api.get_chats()
            return status, settings, chats

        def done(result):
            status, settings, chats = result
            self.gender_var.set(settings.get("developer_gender", "male"))
            self._apply_chats(chats)
            self._set_controls_enabled(True)
            self._apply_parse_state(status)
            running = "РАБОТАЕТ" if status.get("running") else "ОСТАНОВЛЕН"
            self.status_var.set(
                f"{SERVER_URL} | {running} | чатов: "
                f"{status.get('monitored_chats', 0)} | действий сегодня: "
                f"{status.get('daily_actions', 0)}"
            )

        def err(e):
            self.status_var.set(f"ОШИБКА — {SERVER_URL} недоступен. Нажмите ОБНОВИТЬ.")
            MessageDialog(self.root, "Ошибка соединения",
                f"{e}\n\nПроверьте:\n"
                f"  - SERVER_URL и API_TOKEN в файле .env\n"
                f"  - сервис lead-hunter запущен на сервере\n"
                f"  - интернет-соединение\n\n"
                f"Затем нажмите ОБНОВИТЬ.",
                style="error")
            self._set_controls_enabled(True)

        self.run_bg(load, on_done=done, on_error=err)

    def _apply_chats(self, chats: list[dict]):
        self.chats = {c["chat_id"]: c for c in chats}
        self._all_chats = list(chats)
        if self.search_var.get():
            self._filter_chats()
        else:
            self._fill_tree(chats)

    def reload_chat_list(self):
        self.status_var.set("ОБНОВЛЕНИЕ — загрузка данных с сервера...")
        def load():
            status = self.api.get_status()
            chats = self.api.get_chats()
            return status, chats
        def done(result):
            status, chats = result
            self._apply_chats(chats)
            self._set_controls_enabled(True)
            self._apply_parse_state(status)
            self.status_var.set(f"OK — загружено чатов: {len(chats)}")
        self.run_bg(load, on_done=done)

    def _on_select(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        chat_id = int(sel[0])
        chat = self.chats.get(chat_id)
        if not chat:
            return
        self.current_chat_id = chat_id
        self.chat_title_var.set(chat.get("chat_name") or str(chat_id))
        self.read_var.set(bool(chat.get("is_monitored")))
        self.bcast_var.set(bool(chat.get("is_broadcast")))
        self.direct_var.set(bool(chat.get("is_direct_promo")))

        rules = chat.get("chat_rules") or ""
        self.no_rules_var.set(rules == NO_RULES_MARKER)
        self.rules_text.configure(state=tk.NORMAL, bg=C_BG)
        self.rules_text.delete("1.0", tk.END)
        if rules and rules != NO_RULES_MARKER:
            self.rules_text.insert("1.0", rules)
        self._toggle_no_rules()

        self.times_var.set(chat.get("broadcast_times") or "")
        self.niche_var.set(chat.get("chat_niche") or "")
        lang = chat.get("broadcast_language") or "ru"
        if lang not in BROADCAST_LANGUAGES:
            lang = "ru"
        self.lang_var.set(lang)
        self.lang_combo.set(f"{lang} — {BROADCAST_LANGUAGES[lang]}")

    def _on_lang_select(self, _event):
        """Обработка выбора языка рассылки из выпадающего списка."""
        raw = self.lang_combo.get()
        if " — " in raw:
            code = raw.split(" — ")[0].strip()
        else:
            code = raw.strip()
        if code in BROADCAST_LANGUAGES:
            self.lang_var.set(code)

    # === Кнопки ===

    def _apply_parse_state(self, status: dict):
        """Обновляет индикатор состояния парсинга по ответу /api/status."""
        if not status.get("running"):
            self.parse_state_var.set("Telegram-клиент ОСТАНОВЛЕН")
            self.pause_btn.configure(state="disabled")
            self.resume_btn.configure(state="disabled")
            return
        if status.get("paused"):
            self.parse_state_var.set("ПАУЗУА")
        else:
            self.parse_state_var.set("РАБОТАЕТ")
        self.pause_btn.configure(state="normal")
        self.resume_btn.configure(state="normal")

    def pause_parsing(self):
        self.status_var.set("ОСТАНОВКА — останавливаю парсинг на сервере...")
        self.pause_btn.configure(state="disabled")

        def done(result):
            self._apply_parse_state(result)
            self.status_var.set("ПАУЗА — парсинг и рассылки остановлены")

        def err(e):
            self.pause_btn.configure(state="normal")
            self.status_var.set("ОШИБКА — не удалось остановить")
            MessageDialog(self.root, "Ошибка", str(e), style="error")

        self.run_bg(self.api.pause_parsing, on_done=done, on_error=err)

    def resume_parsing(self):
        self.status_var.set("ЗАПУСК — запускаю парсинг на сервере...")
        self.resume_btn.configure(state="disabled")

        def done(result):
            self._apply_parse_state(result)
            self.status_var.set("РАБОТАЕТ — парсинг и рассылки активны")

        def err(e):
            self.resume_btn.configure(state="normal")
            self.status_var.set("ОШИБКА — не удалось запустить")
            MessageDialog(self.root, "Ошибка", str(e), style="error")

        self.run_bg(self.api.resume_parsing, on_done=done, on_error=err)

    def restart_server(self):
        """Ручной рестарт сервиса на сервере. Актуально когда Telethon отвалился
        (прокси/сеть) и не переподключился — 500 на синхронизации/отправке.
        Сервис перезапускается через systemd, Telethon поднимается заново."""
        if not messagebox.askyesno("Перезапуск сервера",
                "Перезапустить сервис lead-hunter на сервере?\n\n"
                "Это переподключит рабочий аккаунт к Telegram.\n"
                "GUI потеряет связь на ~15 секунд, затем переподключится автоматически."):
            return
        self.status_var.set("ПЕРЕЗАПУСК — сервер перезапускается...")
        self.restart_btn.configure(state="disabled")
        self._set_controls_enabled(False)

        def done(result):
            self.status_var.set("OK — сервер перезапускается. Переподключение через 15с...")
            # Сервис убивает себя, поэтому переподключаемся с задержкой
            self.root.after(15000, self._connect_to_server)

        def err(e):
            self.restart_btn.configure(state="normal")
            self._set_controls_enabled(True)
            self.status_var.set("ОШИБКА — не удалось перезапустить")
            MessageDialog(self.root, "Ошибка", str(e), style="error")

        self.run_bg(self.api.restart_server, on_done=done, on_error=err)

    def save_gender(self):
        gender = self.gender_var.get()
        def done(_):
            label = "мужской" if gender == "male" else "женский"
            self.status_var.set(f"OK — пол разработчика сохранён: {label}")
        self.run_bg(lambda: self.api.set_settings({"developer_gender": gender}),
                    on_done=done)

    def save_chat(self):
        if not self.current_chat_id:
            MessageDialog(self.root, "Предупреждение",
                          "Сначала выберите чат из списка слева.", style="warn")
            return
        chat_id = self.current_chat_id

        if self.no_rules_var.get():
            rules = NO_RULES_MARKER
        else:
            rules = self.rules_text.get("1.0", tk.END).strip()

        times = self.times_var.get().strip()
        is_direct = self.direct_var.get()
        if self.bcast_var.get():
            if not times:
                MessageDialog(self.root, "Предупреждение",
                    "Рассылка включена, но не указано время.\n"
                    "Введите время в формате: 10:00, 19:30",
                    style="warn")
                return
            if not TIMES_RE.match(times):
                MessageDialog(self.root, "Предупреждение",
                    "Неверный формат времени.\n"
                    "Используйте ЧЧ:ММ через запятую.\n"
                    "Пример: 10:00, 19:30",
                    style="warn")
                return
            if not is_direct and not rules:
                MessageDialog(self.root, "Предупреждение",
                    "Рассылка включена, но правила чата не заполнены.\n\n"
                    "Варианты:\n"
                    "  1. Вставьте правила чата в поле правил\n"
                    "  2. Отметьте «ПРАВИЛ НЕТ» — если в чате нет правил\n"
                    "  3. Включите «ПРЯМАЯ РЕКЛАМА» — правила не нужны\n\n"
                    "Без одного из этих рассылка НЕ будет отправляться (защита от бана).",
                    style="warn")
                return

        chat_data = {
            "chat_id": chat_id,
            "chat_name": self.chats[chat_id].get("chat_name"),
            "is_monitored": self.read_var.get(),
            "is_broadcast": self.bcast_var.get(),
            "chat_rules": rules,
            "broadcast_times": times,
            "is_direct_promo": is_direct,
            "chat_niche": self.niche_var.get().strip(),
            "broadcast_language": self.lang_var.get().strip() or "ru",
        }

        self.status_var.set("СОХРАНЕНИЕ — отправка на сервер...")
        def done(success):
            if success:
                self.status_var.set(f"OK — сохранено: {self.chats[chat_id]['chat_name']}")
                self.reload_chat_list()
            else:
                MessageDialog(self.root, "Ошибка",
                              "Сервер не подтвердил сохранение.", style="error")

        self.run_bg(lambda: self.api.update_chat(chat_data), on_done=done)

    def test_broadcast(self):
        if not self.current_chat_id:
            MessageDialog(self.root, "Предупреждение",
                          "Сначала выберите чат из списка слева.", style="warn")
            return
        chat_id = self.current_chat_id
        self.status_var.set("ТЕСТ — генерация сообщения на сервере (AI)...")
        self.test_btn.configure(state="disabled")

        def done(result):
            self.test_btn.configure(state="normal")
            self.status_var.set("ГОТОВО")
            if result is None:
                MessageDialog(self.root, "Ошибка",
                    "AI не смог сгенерировать сообщение.\n"
                    "Проверьте, что AI-провайдер доступен на сервере.",
                    style="error")
                return
            if result.get("skip"):
                MessageDialog(self.root, "AI пропустил бы отправку",
                    f"Сообщение НЕ было бы отправлено.\n\n"
                    f"Причина:\n{result.get('reason', '')}\n\n"
                    f"Ниша: {result.get('chat_niche', '')}\n"
                    f"Точка входа: {result.get('entry_point', '')}",
                    style="warn")
            else:
                msg = result.get("message", "")
                niche = result.get("chat_niche", "")
                entry = result.get("entry_point", "")
                MessageDialog(self.root, "Тестовое сообщение (НЕ отправлено)",
                    f"AI сгенерировал такой текст:\n\n"
                    f"{msg}\n\n"
                    f"──────────────────────\n"
                    f"Ниша: {niche}\n"
                    f"Точка входа: {entry}",
                    style="success")

        def err(e):
            self.test_btn.configure(state="normal")
            self.status_var.set("ОШИБКА — генерация не удалась")
            MessageDialog(self.root, "Ошибка", str(e), style="error")

        self.run_bg(lambda: self.api.preview_broadcast(chat_id), on_done=done, on_error=err)

    def sync_dialogs(self):
        self.status_var.set("СИНХРОНИЗАЦИЯ — сервер загружает чаты из Telegram...")
        self.sync_btn.configure(state="disabled")

        def done(count):
            self.sync_btn.configure(state="normal")
            self.status_var.set(f"OK — загружено чатов: {count}")
            self.reload_chat_list()

        def err(e):
            self.sync_btn.configure(state="normal")
            self.status_var.set("ОШИБКА — синхронизация не удалась")
            MessageDialog(self.root, "Ошибка", str(e), style="error")

        self.run_bg(self.api.sync_dialogs, on_done=done, on_error=err)


def main():
    logger.info("Запуск GUI (тонкий клиент)")
    logger.info(f"Сервер: {SERVER_URL or 'НЕ НАСТРОЕН'}")

    root = tk.Tk()

    if not SERVER_URL:
        root.withdraw()
        MessageDialog(root, "Ошибка конфигурации",
            "Не настроен адрес сервера.\n\n"
            "Создайте файл .env рядом с программой и укажите:\n\n"
            "SERVER_URL=http://82.202.170.14:8080\n"
            "API_TOKEN=<токен с сервера>",
            style="error")
        return

    App(root, ServerAPI(SERVER_URL, API_TOKEN))
    root.mainloop()
    logger.info("GUI закрыт")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"Фатальная ошибка GUI: {e}", exc_info=True)
        try:
            root = tk.Tk()
            root.withdraw()
            MessageDialog(root, "Фатальная ошибка", str(e), style="error")
        except Exception:
            pass
        input("Нажмите Enter для выхода...")
