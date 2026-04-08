import json
from pathlib import Path
from typing import Dict, Any, List, Optional
import threading
import time

import tkinter as tk
from tkinter import ttk, messagebox

from datetime import datetime

from instagrapi import Client

CONFIG_PATH = Path("config_operator.json")


class SimpleClient:
    """
    Минимальная обёртка над instagrapi.Client для GUI:
    - логин только по готовому session.json (без пароля, без sessionid)
    - загрузка диалогов
    - чтение сообщений в треде
    - отправка ответа
    """

    def __init__(self, session_path: str, proxy: str | None = None):
        self.session_path = Path(session_path)
        self.cl = Client()
        if proxy:
            self.cl.set_proxy(proxy)

    def login_from_session(self):
        if not self.session_path.exists():
            raise FileNotFoundError(f"Файл сессии не найден: {self.session_path}")

        # загружаем готовые настройки (сессию), без логина/пароля
        self.cl.load_settings(self.session_path)
        # проверяем, что сессия живая
        self.cl.get_timeline_feed()

    @property
    def user_id(self) -> int:
        return self.cl.user_id

    def get_threads(self, amount: int = 20):
        return self.cl.direct_threads(amount=amount)

    def get_messages(self, thread_id: str, amount: int = 30):
        return self.cl.direct_messages(thread_id, amount=amount)

    def send_to_thread(self, thread_id: str, text: str):
        return self.cl.direct_send(text=text, thread_ids=[thread_id])


class InstaReplyGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Instagram Ответчик")
        self.geometry("900x600")

        # состояние
        self.config_data: Dict[str, Any] = {}
        self.accounts: List[Dict[str, Any]] = []
        self.current_client: Optional[SimpleClient] = None
        self.current_account_username: Optional[str] = None

        self.threads = []  # список DirectThread
        self.thread_by_index: Dict[int, Any] = {}

        # listener
        self.listener_thread: Optional[threading.Thread] = None
        self.listener_stop_flag = threading.Event()
        self.refresh_interval_sec = 8  # как часто опрашивать директ

        self._build_ui()
        self._load_config()

        # корректно остановим listener при закрытии окна
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ========================
    # UI
    # ========================
    def _build_ui(self):
        # Верхняя панель: выбор аккаунта + кнопка "Подключиться" + "Обновить диалоги"
        top_frame = ttk.Frame(self)
        top_frame.pack(side=tk.TOP, fill=tk.X, padx=10, pady=10)

        ttk.Label(top_frame, text="Аккаунт:").pack(side=tk.LEFT)

        self.account_var = tk.StringVar()
        self.account_combo = ttk.Combobox(
            top_frame,
            textvariable=self.account_var,
            state="readonly",
            width=30,
        )
        self.account_combo.pack(side=tk.LEFT, padx=5)

        self.connect_btn = ttk.Button(
            top_frame, text="Подключиться", command=self.on_connect
        )
        self.connect_btn.pack(side=tk.LEFT, padx=5)

        self.refresh_btn = ttk.Button(
            top_frame, text="Обновить диалоги", command=self.manual_refresh_threads, state=tk.DISABLED
        )
        self.refresh_btn.pack(side=tk.LEFT, padx=5)

        # Основная область: слева список диалогов, справа чат
        main_frame = ttk.Frame(self)
        main_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Левая колонка — диалоги
        left_frame = ttk.Frame(main_frame)
        left_frame.pack(side=tk.LEFT, fill=tk.Y)

        ttk.Label(left_frame, text="Диалоги").pack(anchor="w")

        self.thread_listbox = tk.Listbox(left_frame, width=40)
        self.thread_listbox.pack(side=tk.LEFT, fill=tk.Y, expand=False)
        self.thread_listbox.bind("<<ListboxSelect>>", self.on_thread_select)

        thread_scroll = ttk.Scrollbar(left_frame, orient=tk.VERTICAL, command=self.thread_listbox.yview)
        thread_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.thread_listbox.config(yscrollcommand=thread_scroll.set)

        # Правая колонка — чат
        right_frame = ttk.Frame(main_frame)
        right_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0))

        ttk.Label(right_frame, text="Сообщения").pack(anchor="w")

        self.chat_text = tk.Text(right_frame, wrap=tk.WORD, state=tk.DISABLED)
        self.chat_text.pack(fill=tk.BOTH, expand=True)

        # Нижняя панель — поле ввода и кнопка "Отправить"
        bottom_frame = ttk.Frame(self)
        bottom_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 10))

        ttk.Label(bottom_frame, text="Ответ:").pack(anchor="w")

        self.reply_entry = tk.Text(bottom_frame, height=3, wrap=tk.WORD)
        self.reply_entry.pack(fill=tk.X, expand=True)

        send_btn = ttk.Button(bottom_frame, text="Отправить", command=self.send_reply)
        send_btn.pack(anchor="e", pady=(5, 0))

    # ========================
    # Загрузка конфига
    # ========================
    def _load_config(self):
        try:
            with CONFIG_PATH.open(encoding="utf-8") as f:
                self.config_data = json.load(f)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось прочитать config_operator.json: {e}")
            self.config_data = {}
            return

        self.accounts = self.config_data.get("accounts", [])
        if not self.accounts:
            messagebox.showwarning("Внимание", "В config_operator.json не найдено ни одного аккаунта.")
            return

        # наполняем комбобокс
        usernames = [acc["username"] for acc in self.accounts]
        self.account_combo["values"] = usernames
        if usernames:
            self.account_combo.current(0)

    # ========================
    # Логика подключения
    # ========================
    def on_connect(self):
        if not self.accounts:
            return

        selected_username = self.account_var.get()
        if not selected_username:
            messagebox.showwarning("Внимание", "Выберите аккаунт.")
            return

        acc = next((a for a in self.accounts if a["username"] == selected_username), None)
        if not acc:
            messagebox.showerror("Ошибка", f"Аккаунт {selected_username} не найден в config_operator.json")
            return

        session_file = acc.get("session_file") or f"sessions/account{acc['number']}.json"
        proxy = acc.get("proxy")

        self.current_client = SimpleClient(session_path=session_file, proxy=proxy)

        def do_login():
            try:
                self.current_client.login_from_session()
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Ошибка логина", f"Не удалось залогиниться по сессии:\n{e}"))
                self.current_client = None
                return

            self.current_account_username = selected_username
            # включаем кнопку "Обновить диалоги"
            self.after(0, lambda: self.refresh_btn.config(state=tk.NORMAL))
            # первый refresh диалогов
            self._background_refresh_threads()
            # запуск фонового листенера
            self.start_listener()

        threading.Thread(target=do_login, daemon=True).start()

    # ========================
    # Фоновый listener
    # ========================
    def start_listener(self):
        if self.listener_thread and self.listener_thread.is_alive():
            return

        self.listener_stop_flag.clear()

        def loop():
            while not self.listener_stop_flag.is_set():
                self._background_refresh_threads()
                time.sleep(self.refresh_interval_sec)

        self.listener_thread = threading.Thread(target=loop, daemon=True)
        self.listener_thread.start()

    def stop_listener(self):
        self.listener_stop_flag.set()

    def on_close(self):
        self.stop_listener()
        self.destroy()

    # ========================
    # Обновление диалогов
    # ========================
    def manual_refresh_threads(self):
        """Ручная кнопка 'Обновить диалоги' — тоже в фоне, чтобы не лагало."""
        self._background_refresh_threads()

    def _background_refresh_threads(self):
        """Запрашиваем список диалогов в фоне и применяем к UI через after()."""

        if not self.current_client:
            return

        def worker():
            try:
                threads = self.current_client.get_threads(amount=20)
            except Exception as e:
                # показывать ошибку при каждом автопуле не будем, только в лог/консоль
                print(f"Ошибка загрузки диалогов: {e}")
                return

            self.after(0, lambda t=threads: self._apply_threads(t))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_threads(self, threads):
        """Обновляем список диалогов в UI, не блокируя интерфейс."""

        self.threads = threads
        self.thread_by_index.clear()

        # запомним текущий выбранный индекс
        sel = self.thread_listbox.curselection()
        prev_index = sel[0] if sel else None

        self.thread_listbox.delete(0, tk.END)

        if not self.current_client:
            return

        own_id = self.current_client.user_id

        for idx, thread in enumerate(threads):
            # ищем "оппонента" — первого пользователя, отличного от нас
            other_username = "unknown"
            other_full_name = ""
            try:
                for u in thread.users:
                    if u.pk != own_id:
                        other_username = u.username
                        other_full_name = getattr(u, "full_name", "") or ""
                        break
            except Exception:
                pass

            display_name = other_full_name if other_full_name else other_username

            # последний месседж (обычно [0] — самый новый)
            last_text = ""
            if thread.messages:
                m = thread.messages[0]
                last_text = getattr(m, "text", "") or ""
                if len(last_text) > 40:
                    last_text = last_text[:40] + "..."

            # Имя + @юзернейм
            display = f"{display_name} (@{other_username}): {last_text}"
            self.thread_listbox.insert(tk.END, display)
            self.thread_by_index[idx] = thread

        # восстановим выбор, если он был
        if prev_index is not None and prev_index < len(self.thread_by_index):
            self.thread_listbox.selection_clear(0, tk.END)
            self.thread_listbox.selection_set(prev_index)
            self.thread_listbox.activate(prev_index)
        elif self.thread_by_index:
            # если до этого ничего не было выбрано — выберем первый диалог
            self.thread_listbox.selection_clear(0, tk.END)
            self.thread_listbox.selection_set(0)
            self.thread_listbox.activate(0)

        # если что-то выбрано — обновляем сообщения
        if self.thread_listbox.curselection():
            self._background_load_thread_messages()


    # ========================
    # Сообщения в выбранном диалоге
    # ========================
    def on_thread_select(self, event):
        self._background_load_thread_messages()

    def _background_load_thread_messages(self):
        if not self.current_client:
            return
        if not self.thread_listbox.curselection():
            return

        idx = self.thread_listbox.curselection()[0]
        thread = self.thread_by_index.get(idx)
        if not thread:
            return

        thread_id = thread.id

        def worker():
            try:
                messages = self.current_client.get_messages(thread_id, amount=30)
            except Exception as e:
                print(f"Ошибка загрузки сообщений: {e}")
                return

            self.after(0, lambda msgs=messages, t=thread: self._apply_messages(t, msgs))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_messages(self, thread, messages):
        if not self.current_client:
            return

        own_id = self.current_client.user_id

        # маппинг user_id -> username
        id_to_username: Dict[int, str] = {}
        try:
            for u in thread.users:
                id_to_username[u.pk] = u.username
        except Exception:
            pass

        # на всякий случай добавим свой id
        if own_id not in id_to_username:
            if self.current_account_username:
                id_to_username[own_id] = self.current_account_username

        self.chat_text.config(state=tk.NORMAL)
        self.chat_text.delete("1.0", tk.END)

        # сообщения приходят от нового к старому, развернём
        for msg in reversed(messages):
            ts = getattr(msg, "timestamp", None)
            if ts:
                ts_str = ts.strftime("%Y-%m-%d %H:%M")
            else:
                ts_str = "????-??-?? ??:??"

            # если юзер неизвестен — один раз дергаем user_info и кэшируем
            if msg.user_id not in id_to_username:
                try:
                    ui = self.current_client.cl.user_info(msg.user_id)
                    id_to_username[msg.user_id] = ui.username
                except Exception:
                    id_to_username[msg.user_id] = f"id_{msg.user_id}"

            sender_username = id_to_username.get(msg.user_id, f"id_{msg.user_id}")

            if msg.user_id == own_id:
                sender_label = f"@{self.current_account_username or sender_username}"
            else:
                sender_label = f"@{sender_username}"

            text = getattr(msg, "text", "") or ""
            line = f"[{ts_str}] {sender_label}: {text}\n"
            self.chat_text.insert(tk.END, line)

        self.chat_text.config(state=tk.DISABLED)

    # ========================
    # Ответ
    # ========================
    def send_reply(self):
        if not self.current_client:
            messagebox.showwarning("Внимание", "Сначала подключитесь к аккаунту.")
            return
        if not self.thread_listbox.curselection():
            messagebox.showwarning("Внимание", "Выберите диалог.")
            return

        text = self.reply_entry.get("1.0", tk.END).strip()
        if not text:
            messagebox.showwarning("Внимание", "Нельзя отправить пустое сообщение.")
            return

        idx = self.thread_listbox.curselection()[0]
        thread = self.thread_by_index.get(idx)
        if not thread:
            messagebox.showerror("Ошибка", "Не удалось найти выбранный диалог.")
            return

        thread_id = thread.id

        def worker():
            try:
                self.current_client.send_to_thread(thread_id, text)
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Ошибка", f"Не удалось отправить сообщение:\n{e}"))
                return

            # после успешной отправки обновим чат
            self.after(0, self._background_load_thread_messages)
            self.after(0, lambda: self.reply_entry.delete("1.0", tk.END))

        threading.Thread(target=worker, daemon=True).start()


def main():
    app = InstaReplyGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
