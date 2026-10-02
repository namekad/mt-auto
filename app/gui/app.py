from __future__ import annotations

import os
import queue
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QFont, QFontInfo, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.config import PROJECT_ROOT, load_settings
from app.database.repository import Repository
from app.database.session import create_db_engine, create_session_factory, init_db
from app.env_store import ENV_GROUPS, apply_to_process, read_env, write_env
from app.gui.channels_store import (
    ChannelRow,
    authorized_csv,
    load_catalog,
    merge_discovered,
    parse_channel_ids,
    save_catalog,
)
from app.gui.feed import (
    FeedName,
    count_feeds,
    detail_line,
    feed_for,
    format_legs,
    format_price,
    format_when,
    format_zone,
    ignored_line,
    is_ignored_event,
    side_text,
    status_text,
)
from app.gui.prefs import load_trades_tab, save_trades_tab
from app.gui.theme import AMBER, APP_STYLESHEET, GREEN, MUTED, RED, TEXT
from app.paths import ensure_runtime_files, is_frozen
from app.runtime_host import RuntimeHost
from app.trading.models import Setup, SetupState
from app.updater import UpdateWatcher, relaunch, repo_root
from app.utils.health import RuntimeState
from app.utils.logging import setup_logging

LEVEL_COLORS = {
    "DEBUG": MUTED,
    "INFO": TEXT,
    "WARNING": AMBER,
    "ERROR": RED,
    "CRITICAL": RED,
}

NAV_PAGES = ("trades", "channels", "setup", "log")
NAV_LABELS = {"trades": "Trades", "channels": "Channels", "setup": "Setup", "log": "Log"}
TRADE_COLUMNS = ("Time", "Symbol", "Side", "Zone", "SL", "TP1", "TP2", "State", "Legs")
TAB_LABELS = {"live": "Live", "cancelled": "Cancelled", "closed": "Closed"}


def _field_map() -> dict[str, dict[str, object]]:
    mapping: dict[str, dict[str, object]] = {}
    for _group, fields in ENV_GROUPS:
        for field in fields:
            mapping[str(field["key"])] = field
    return mapping


def _open_reader() -> Repository:
    settings = load_settings()
    engine = create_db_engine(settings)
    init_db(engine)
    return Repository(create_session_factory(engine))


def _mono_font() -> QFont:
    font = QFont("Cascadia Mono", 10)
    if not QFontInfo(font).family().lower().startswith("cascadia"):
        font = QFont("Consolas", 10)
    return font


class _PromptBox:
    def __init__(self) -> None:
        self.value = ""
        self.done = threading.Event()


class PromptDialog(QDialog):
    def __init__(self, parent: QWidget, title: str, message: str, secret: bool) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.result_text = ""
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setObjectName("section")
        layout.addWidget(heading)
        body = QLabel(message)
        body.setObjectName("hint")
        body.setWordWrap(True)
        layout.addWidget(body)
        self._entry = QLineEdit()
        if secret:
            self._entry.setEchoMode(QLineEdit.EchoMode.Password)
        self._entry.returnPressed.connect(self._accept)
        layout.addWidget(self._entry)
        if secret:
            show = QCheckBox("Show password")
            show.toggled.connect(self._toggle_secret)
            layout.addWidget(show)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._entry.setFocus()

    def _toggle_secret(self, visible: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        self._entry.setEchoMode(mode)

    def _accept(self) -> None:
        self.result_text = self._entry.text().strip()
        self.accept()


class AddChannelDialog(QDialog):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add channel")
        self.setModal(True)
        self.channel_id = 0
        self.channel_name = ""
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Channel ID"))
        self._id = QLineEdit()
        self._id.setPlaceholderText("-1004410224742")
        layout.addWidget(self._id)
        hint = QLabel("Numeric id from Telegram. You can authorize it after it is added.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addWidget(QLabel("Name (optional)"))
        self._name = QLineEdit()
        layout.addWidget(self._name)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self) -> None:
        try:
            self.channel_id = int(self._id.text().strip())
        except ValueError:
            QMessageBox.warning(self, "Channel", "Enter the numeric channel ID.")
            return
        self.channel_name = self._name.text().strip()
        self.accept()


class _SetupLoader(QThread):
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, reader: Repository) -> None:
        super().__init__()
        self._reader = reader

    def run(self) -> None:
        try:
            rows = self._reader.list_recent_setups()
        except Exception as error:
            self.failed.emit(str(error))
            return
        self.ready.emit(rows)


class _ChannelLoader(QThread):
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, runtime: RuntimeHost) -> None:
        super().__init__()
        self._runtime = runtime

    def run(self) -> None:
        try:
            rows = self._runtime.list_joined_channels()
        except Exception as error:
            self.failed.emit(str(error))
            return
        self.loaded.emit(rows)


class AppWindow(QMainWindow):
    prompt_requested = Signal(str, str, bool, object)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Telegram → MT5")
        self.resize(1180, 760)
        self.setMinimumSize(980, 640)
        ensure_runtime_files()
        self.log_queue: queue.Queue[dict[str, object]] = queue.Queue()
        setup_logging(self.log_queue)
        self.runtime = RuntimeHost()
        self._fields: dict[str, QWidget] = {}
        self._field_rows: dict[str, QWidget] = {}
        self._field_defs = _field_map()
        self._mono = _mono_font()
        self._dirty = False
        self._loading = False
        self._closing = False
        self._page = "trades"
        self._tab: FeedName = "live"
        saved_tab = load_trades_tab()
        if saved_tab == "cancelled":
            self._tab = "cancelled"
        elif saved_tab == "closed":
            self._tab = "closed"
        elif saved_tab == "live":
            self._tab = "live"
        else:
            raise ValueError(f"Unhandled trades tab: {saved_tab}")
        self._trade_token = ""
        self._setups: list[Setup] = []
        self._selected_setup_id = ""
        self._reader = _open_reader()
        self._setup_loader: _SetupLoader | None = None
        self._setup_clock = 0
        self._log_lines: list[tuple[str, str]] = []
        self._log_dirty = False
        self._banner_key = ""
        env = read_env()
        try:
            env_ids = parse_channel_ids(env.get("ALLOWED_CHANNEL_IDS", ""))
        except ValueError:
            env_ids = []
        self._channels = load_catalog(env_ids)
        self._loader: _ChannelLoader | None = None
        self._nav_buttons: dict[str, QPushButton] = {}
        self._tab_buttons: dict[str, QPushButton] = {}
        self.prompt_requested.connect(self._show_prompt)
        self._build()
        self._load_settings_into_form()
        self._show_page("trades")
        self._updater = UpdateWatcher(on_restart=self._schedule_update_restart)
        self._updater.start()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._pump)
        self._timer.start(250)
        self._refresh_status()
        self._request_setups()

    def _build(self) -> None:
        shell = QWidget()
        row = QHBoxLayout(shell)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self._build_sidebar())
        main = QWidget()
        column = QVBoxLayout(main)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(self._build_topbar())
        self._alert = QLabel("")
        self._alert.setObjectName("alert")
        self._alert.setWordWrap(True)
        self._alert.setContentsMargins(20, 8, 20, 0)
        self._alert.setVisible(False)
        column.addWidget(self._alert)
        self._stack = QStackedWidget()
        column.addWidget(self._stack, 1)
        self._pages: dict[str, QWidget] = {}
        for name in NAV_PAGES:
            page = QWidget()
            self._pages[name] = page
            self._stack.addWidget(page)
        self._build_trades(self._pages["trades"])
        self._build_channels(self._pages["channels"])
        self._build_setup(self._pages["setup"])
        self._build_log(self._pages["log"])
        row.addWidget(main, 1)
        self.setCentralWidget(shell)

    def _build_sidebar(self) -> QFrame:
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(220)
        layout = QVBoxLayout(side)
        layout.setContentsMargins(16, 20, 16, 16)
        brand = QLabel("Telegram MT5")
        brand.setObjectName("section")
        layout.addWidget(brand)
        note = QLabel("Demo setups")
        note.setObjectName("muted")
        layout.addWidget(note)
        layout.addSpacing(18)
        for name in NAV_PAGES:
            button = QPushButton(NAV_LABELS[name])
            button.setObjectName("nav")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, page=name: self._show_page(page))
            self._nav_buttons[name] = button
            layout.addWidget(button)
        layout.addStretch(1)
        return side

    def _build_topbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("topbar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(20, 12, 20, 12)
        layout.setSpacing(12)
        self._status_pill = QLabel("Stopped")
        self._status_pill.setObjectName("pillOff")
        layout.addWidget(self._status_pill)
        self._telegram_chip = QLabel("Telegram off")
        self._telegram_chip.setObjectName("chipOff")
        layout.addWidget(self._telegram_chip)
        self._mt5_chip = QLabel("MetaTrader off")
        self._mt5_chip.setObjectName("chipOff")
        layout.addWidget(self._mt5_chip)
        self._status_copy = QLabel("Open Setup if this is the first time, then press Start.")
        self._status_copy.setObjectName("muted")
        self._status_copy.setWordWrap(True)
        layout.addWidget(self._status_copy, 1)
        self._start_btn = QPushButton("Start")
        self._start_btn.setObjectName("start")
        self._start_btn.clicked.connect(self._start)
        self._pause_btn = QPushButton("Pause")
        self._pause_btn.setObjectName("pause")
        self._pause_btn.clicked.connect(self._toggle_pause)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setObjectName("danger")
        self._stop_btn.clicked.connect(self._stop)
        for button in (self._start_btn, self._pause_btn, self._stop_btn):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            layout.addWidget(button)
        return bar

    def _build_trades(self, parent: QWidget) -> None:
        layout = QVBoxLayout(parent)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        self._banner = QLabel("")
        self._banner.setObjectName("banner")
        self._banner.setWordWrap(True)
        layout.addWidget(self._banner)
        tabs = QHBoxLayout()
        for name in ("live", "cancelled", "closed"):
            button = QPushButton(TAB_LABELS[name])
            button.setObjectName("tab")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, tab=name: self._set_tab(tab))
            self._tab_buttons[name] = button
            tabs.addWidget(button)
        self._ignored_button = QPushButton("0 ignored")
        self._ignored_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ignored_button.clicked.connect(self._toggle_ignored)
        tabs.addStretch(1)
        tabs.addWidget(self._ignored_button)
        layout.addLayout(tabs)
        self._ignored_list = QTextEdit()
        self._ignored_list.setReadOnly(True)
        self._ignored_list.setFixedHeight(120)
        self._ignored_list.setVisible(False)
        layout.addWidget(self._ignored_list)
        self._empty = QLabel("No setups in this list.")
        self._empty.setObjectName("muted")
        layout.addWidget(self._empty)
        self._table = QTableWidget(0, len(TRADE_COLUMNS))
        self._table.setHorizontalHeaderLabels(list(TRADE_COLUMNS))
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setMinimumSectionSize(72)
        self._table.setShowGrid(False)
        self._table.itemSelectionChanged.connect(self._on_setup_selected)
        layout.addWidget(self._table, 1)
        self._detail = QLabel("Select a setup.")
        self._detail.setObjectName("muted")
        self._detail.setWordWrap(True)
        layout.addWidget(self._detail)
        self._mark_tab(self._tab)

    def _build_channels(self, parent: QWidget) -> None:
        layout = QVBoxLayout(parent)
        layout.setContentsMargins(20, 16, 20, 16)
        title = QLabel("Channels")
        title.setObjectName("title")
        layout.addWidget(title)
        copy = QLabel("Add every channel you care about, then authorize the ones this app may read.")
        copy.setObjectName("hint")
        copy.setWordWrap(True)
        layout.addWidget(copy)
        actions = QHBoxLayout()
        add = QPushButton("Add channel")
        add.setObjectName("primary")
        add.clicked.connect(self._add_channel)
        load = QPushButton("Load from Telegram")
        load.clicked.connect(self._load_telegram_channels)
        self._load_channels_btn = load
        save = QPushButton("Save")
        save.clicked.connect(self._save_settings)
        for button in (add, load, save):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self._channel_note = QLabel("Authorized channels are saved into the allowed list.")
        self._channel_note.setObjectName("hint")
        self._channel_note.setWordWrap(True)
        layout.addWidget(self._channel_note)
        self._channel_table = QTableWidget(0, 4)
        self._channel_table.setHorizontalHeaderLabels(["Name", "ID", "Authorized", ""])
        self._channel_table.verticalHeader().setVisible(False)
        self._channel_table.verticalHeader().setDefaultSectionSize(42)
        self._channel_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        header = self._channel_table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self._channel_table.setColumnWidth(2, 120)
        self._channel_table.setColumnWidth(3, 108)
        self._channel_table.setShowGrid(False)
        self._channel_table.itemChanged.connect(self._on_channel_name)
        layout.addWidget(self._channel_table, 1)
        self._paint_channels()

    def _build_setup(self, parent: QWidget) -> None:
        outer = QVBoxLayout(parent)
        outer.setContentsMargins(20, 16, 20, 12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        form = QVBoxLayout(inner)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(12)

        telegram, telegram_layout = self._card(
            "Telegram",
            "Sign in once. The login stays saved on this computer.",
        )
        self._add_listener_mode(telegram_layout)
        for key in (
            "TELEGRAM_API_ID",
            "TELEGRAM_API_HASH",
            "TELEGRAM_PHONE",
            "TELEGRAM_BOT_TOKEN",
        ):
            self._add_field(telegram_layout, key)
        forget = QPushButton("Forget Telegram login")
        forget.clicked.connect(self._clear_session)
        telegram_layout.addWidget(forget)
        form.addWidget(telegram)

        mt5, mt5_layout = self._card(
            "MetaTrader 5",
            "Use a demo account. Open MetaTrader on this PC first.",
        )
        for key in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH"):
            self._add_field(mt5_layout, key)
        form.addWidget(mt5)

        signals, signals_layout = self._card(
            "Signals",
            "Auto on demo waits for the zone, then opens two 0.01 positions (TP1 and TP2) and moves the stop after a 4.0 favorable move. A close setup reply closes only that setup.",
        )
        self._add_execution_mode(signals_layout)
        self._add_safe_mode(signals_layout)
        form.addWidget(signals)

        self._advanced_btn = QPushButton("More options")
        self._advanced_btn.clicked.connect(self._toggle_advanced)
        form.addWidget(self._advanced_btn, 0, Qt.AlignmentFlag.AlignLeft)
        self._advanced = QFrame()
        self._advanced.setObjectName("card")
        advanced_layout = QVBoxLayout(self._advanced)
        advanced_layout.setContentsMargins(16, 14, 16, 14)
        for key in (
            "CONTROL_BOT_TOKEN",
            "CONTROL_CHAT_ID",
            "AUTHORIZED_CONTROL_USER_IDS",
            "LOCAL_TIMEZONE",
            "DATABASE_URL",
            "TELEGRAM_SESSION_PATH",
        ):
            self._add_field(advanced_layout, key)
        self._advanced.setVisible(False)
        form.addWidget(self._advanced)
        form.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        footer = QHBoxLayout()
        self._save_note = QLabel("Changes are kept after you save.")
        self._save_note.setObjectName("hint")
        footer.addWidget(self._save_note, 1)
        save = QPushButton("Save")
        save.clicked.connect(self._save_settings)
        start = QPushButton("Save and start")
        start.setObjectName("primary")
        start.clicked.connect(self._save_and_restart)
        for button in (save, start):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            footer.addWidget(button)
        outer.addLayout(footer)

    def _build_log(self, parent: QWidget) -> None:
        layout = QVBoxLayout(parent)
        layout.setContentsMargins(20, 16, 20, 16)
        header = QHBoxLayout()
        title = QLabel("Log")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch(1)
        self._log_problems = QPushButton("Problems")
        self._log_problems.setCheckable(True)
        self._log_problems.clicked.connect(self._show_log_filter)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear_logs)
        header.addWidget(self._log_problems)
        header.addWidget(clear)
        layout.addLayout(header)
        self._log_box = QTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setFont(_mono_font())
        layout.addWidget(self._log_box, 1)

    def _card(self, title: str, subtitle: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        heading = QLabel(title)
        heading.setObjectName("section")
        layout.addWidget(heading)
        note = QLabel(subtitle)
        note.setObjectName("hint")
        note.setWordWrap(True)
        layout.addWidget(note)
        return frame, layout

    def _add_listener_mode(self, layout: QVBoxLayout) -> None:
        label = QLabel("How do you read the channel?")
        label.setObjectName("section")
        layout.addWidget(label)
        hint = QLabel("Choose your account if you cannot add a bot as admin.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        row = QHBoxLayout()
        user = QPushButton("My Telegram account")
        bot = QPushButton("A channel bot")
        user.clicked.connect(lambda: self._set_listener_mode("USER"))
        bot.clicked.connect(lambda: self._set_listener_mode("BOT"))
        self._listener_buttons = {"USER": user, "BOT": bot}
        for button in (user, bot):
            button.setObjectName("tab")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)

    def _add_execution_mode(self, layout: QVBoxLayout) -> None:
        label = QLabel("When a signal arrives")
        label.setObjectName("section")
        layout.addWidget(label)
        row = QHBoxLayout()
        options = (
            ("OBSERVE", "Watch only"),
            ("APPROVAL", "Wait for approval"),
            ("AUTO_DEMO", "Auto on demo"),
        )
        self._execution_buttons: dict[str, QPushButton] = {}
        for value, text in options:
            button = QPushButton(text)
            button.setObjectName("tab")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, mode=value: self._set_execution_mode(mode))
            self._execution_buttons[value] = button
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        self._execution_hint = QLabel("")
        self._execution_hint.setObjectName("hint")
        self._execution_hint.setWordWrap(True)
        layout.addWidget(self._execution_hint)

    def _add_safe_mode(self, layout: QVBoxLayout) -> None:
        widget = QCheckBox("Keep orders off")
        widget.toggled.connect(self._on_safe_mode)
        layout.addWidget(widget)
        self._dry_hint = QLabel("")
        self._dry_hint.setObjectName("hint")
        self._dry_hint.setWordWrap(True)
        layout.addWidget(self._dry_hint)
        self._fields["DRY_RUN"] = widget

    def _add_field(self, layout: QVBoxLayout, key: str) -> None:
        field = self._field_defs[key]
        wrap = QWidget()
        column = QVBoxLayout(wrap)
        column.setContentsMargins(0, 6, 0, 0)
        label = QLabel(str(field["label"]))
        column.addWidget(label)
        kind = str(field.get("kind", "text"))
        controls = QHBoxLayout()
        widget = QLineEdit()
        widget.setPlaceholderText(str(field.get("placeholder") or ""))
        if kind == "secret":
            widget.setEchoMode(QLineEdit.EchoMode.Password)
        widget.textChanged.connect(lambda _text: self._mark_dirty())
        controls.addWidget(widget, 1)
        if kind == "secret":
            reveal = QPushButton("Show")
            reveal.clicked.connect(lambda _checked=False, target=widget, button=reveal: self._toggle_secret(target, button))
            controls.addWidget(reveal)
        if kind == "path":
            browse = QPushButton("Browse")
            browse.clicked.connect(lambda _checked=False, target=widget: self._browse(target))
            controls.addWidget(browse)
        column.addLayout(controls)
        hint = str(field.get("hint") or field.get("help") or "")
        if hint:
            note = QLabel(hint)
            note.setObjectName("hint")
            note.setWordWrap(True)
            column.addWidget(note)
        layout.addWidget(wrap)
        self._fields[key] = widget
        self._field_rows[key] = wrap

    def _toggle_secret(self, widget: QLineEdit, button: QPushButton) -> None:
        hidden = widget.echoMode() == QLineEdit.EchoMode.Password
        widget.setEchoMode(QLineEdit.EchoMode.Normal if hidden else QLineEdit.EchoMode.Password)
        button.setText("Hide" if hidden else "Show")

    def _toggle_advanced(self) -> None:
        open_now = not self._advanced.isVisible()
        self._advanced.setVisible(open_now)
        self._advanced_btn.setText("Hide extra options" if open_now else "More options")

    def _set_listener_mode(self, mode: str) -> None:
        if mode not in {"USER", "BOT"}:
            raise ValueError(f"Unhandled listener mode: {mode}")
        for key, button in self._listener_buttons.items():
            self._polish_active(button, key == mode)
        self._apply_listener_visibility()
        self._mark_dirty()

    def _listener_mode(self) -> str:
        for key, button in self._listener_buttons.items():
            if button.property("active") is True:
                return key
        return "USER"

    def _set_execution_mode(self, mode: str) -> None:
        if mode not in self._execution_buttons:
            raise ValueError(f"Unhandled execution mode: {mode}")
        for key, button in self._execution_buttons.items():
            self._polish_active(button, key == mode)
        self._refresh_execution_hint()
        self._mark_dirty()

    def _execution_mode(self) -> str:
        for key, button in self._execution_buttons.items():
            if button.property("active") is True:
                return key
        return "OBSERVE"

    def _on_safe_mode(self) -> None:
        self._refresh_execution_hint()
        self._mark_dirty()

    def _dry_run(self) -> bool:
        widget = self._fields["DRY_RUN"]
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        return True

    def _refresh_execution_hint(self) -> None:
        mode = self._execution_mode()
        dry = self._dry_run()
        if mode == "OBSERVE":
            text = "Watch only stores each setup and never sends an order."
        elif mode == "APPROVAL":
            text = "Wait for approval holds a setup. This screen does not send after approval yet."
        elif mode == "AUTO_DEMO" and dry:
            text = "Auto on demo will open two 0.01 positions after you turn Keep orders off, then Save and Start."
        elif mode == "AUTO_DEMO":
            text = (
                "Auto on demo waits for the zone, then opens TP1 and TP2. "
                "A close setup reply closes only that setup."
            )
        else:
            raise ValueError(f"Unhandled execution mode: {mode}")
        self._execution_hint.setText(text)
        if dry:
            self._dry_hint.setText("On = no order is sent, even if Auto on demo is selected.")
        else:
            self._dry_hint.setText(
                "Off = Auto on demo may send two demo positions. Watch only and Wait for approval still do not send."
            )

    def _apply_listener_visibility(self) -> None:
        mode = self._listener_mode()
        for key, row in self._field_rows.items():
            modes = self._field_defs[key].get("modes")
            allowed = {str(item) for item in modes} if isinstance(modes, list) else set()
            row.setVisible(not allowed or mode in allowed)

    def _polish_active(self, button: QPushButton, active: bool) -> None:
        button.setChecked(active)

    def _show_page(self, name: str) -> None:
        if name not in self._pages:
            raise ValueError(f"Unhandled page: {name}")
        self._page = name
        self._stack.setCurrentWidget(self._pages[name])
        for key, button in self._nav_buttons.items():
            self._polish_active(button, key == name)
        if name == "log":
            self._flush_log()

    def _set_tab(self, tab: str) -> None:
        if tab == "live":
            self._tab = "live"
        elif tab == "cancelled":
            self._tab = "cancelled"
        elif tab == "closed":
            self._tab = "closed"
        else:
            raise ValueError(f"Unhandled trades tab: {tab}")
        self._trade_token = ""
        self._mark_tab(self._tab)
        save_trades_tab(self._tab)
        self._refresh_trades(force=True)

    def _mark_tab(self, tab: FeedName) -> None:
        for key, button in self._tab_buttons.items():
            self._polish_active(button, key == tab)

    def _toggle_ignored(self) -> None:
        self._ignored_list.setVisible(not self._ignored_list.isVisible())
        self._refresh_trades(force=True)

    def _paint_channels(self) -> None:
        table = self._channel_table
        table.blockSignals(True)
        table.setRowCount(0)
        for index, row in enumerate(self._channels):
            table.insertRow(index)
            name = QTableWidgetItem(row.name)
            name.setFlags(name.flags() | Qt.ItemFlag.ItemIsEditable)
            ident = QTableWidgetItem(str(row.channel_id))
            ident.setFlags(Qt.ItemFlag.ItemIsEnabled)
            table.setItem(index, 0, name)
            table.setItem(index, 1, ident)
            box = QCheckBox()
            box.setChecked(row.authorized)
            box.toggled.connect(lambda checked, cid=row.channel_id: self._set_authorized(cid, checked))
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(8, 0, 0, 0)
            holder_layout.addWidget(box)
            holder_layout.addStretch(1)
            holder.setStyleSheet("background: transparent;")
            table.setCellWidget(index, 2, holder)
            remove = QPushButton("Remove")
            remove.setFixedWidth(88)
            remove.setCursor(Qt.CursorShape.PointingHandCursor)
            remove.clicked.connect(lambda _checked=False, cid=row.channel_id: self._remove_channel(cid))
            table.setCellWidget(index, 3, remove)
        table.blockSignals(False)

    def _on_channel_name(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        ident = self._channel_table.item(item.row(), 1)
        if ident is None:
            return
        channel_id = int(ident.text())
        for row in self._channels:
            if row.channel_id == channel_id:
                row.name = item.text().strip()
                self._mark_dirty()
                return

    def _set_authorized(self, channel_id: int, authorized: bool) -> None:
        for row in self._channels:
            if row.channel_id == channel_id:
                row.authorized = authorized
                self._mark_dirty()
                return

    def _remove_channel(self, channel_id: int) -> None:
        self._channels = [row for row in self._channels if row.channel_id != channel_id]
        self._paint_channels()
        self._mark_dirty()

    def _add_channel(self) -> None:
        dialog = AddChannelDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        for row in self._channels:
            if row.channel_id == dialog.channel_id:
                if dialog.channel_name:
                    row.name = dialog.channel_name
                self._paint_channels()
                self._mark_dirty()
                return
        self._channels.append(
            ChannelRow(channel_id=dialog.channel_id, name=dialog.channel_name, authorized=False)
        )
        self._channels = merge_discovered(self._channels, [])
        self._paint_channels()
        self._mark_dirty()

    def _load_telegram_channels(self) -> None:
        self._apply_form()
        self._load_channels_btn.setEnabled(False)
        self._load_channels_btn.setText("Loading…")
        self._channel_note.setText("Reading channels from your Telegram account.")
        loader = _ChannelLoader(self.runtime)
        loader.loaded.connect(self._channels_loaded)
        loader.failed.connect(self._channels_failed)
        loader.finished.connect(loader.deleteLater)
        self._loader = loader
        loader.start()

    def _channels_loaded(self, rows: object) -> None:
        self._load_channels_btn.setEnabled(True)
        self._load_channels_btn.setText("Load from Telegram")
        if not isinstance(rows, list):
            self._channel_note.setText("Could not read the channel list.")
            return
        found: list[tuple[int, str]] = []
        for item in rows:
            if isinstance(item, tuple) and len(item) == 2:
                found.append((int(item[0]), str(item[1])))
        self._channels = merge_discovered(self._channels, found)
        self._paint_channels()
        self._mark_dirty()
        self._channel_note.setText(
            f"Loaded {len(found)} channels. Turn on Authorized for the ones to read, then Save."
        )

    def _channels_failed(self, message: str) -> None:
        self._load_channels_btn.setEnabled(True)
        self._load_channels_btn.setText("Load from Telegram")
        self._channel_note.setText(message)
        QMessageBox.warning(self, "Channels", message)

    def _browse(self, widget: QLineEdit) -> None:
        path, _selected = QFileDialog.getOpenFileName(
            self,
            "Select MetaTrader",
            "",
            "MetaTrader (terminal64.exe);;Programs (*.exe);;All files (*.*)",
        )
        if path:
            widget.setText(path.replace("\\", "/"))
            self._mark_dirty()

    def _mark_dirty(self) -> None:
        if self._loading:
            return
        self._dirty = True
        self._save_note.setText("Unsaved changes")
        self._save_note.setStyleSheet(f"color: {AMBER};")

    def _form_values(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for key, widget in self._fields.items():
            if isinstance(widget, QLineEdit):
                values[key] = widget.text().strip()
        values["TELEGRAM_LISTENER_MODE"] = self._listener_mode()
        values["EXECUTION_MODE"] = self._execution_mode()
        values["DRY_RUN"] = "true" if self._dry_run() else "false"
        values["ALLOWED_CHANNEL_IDS"] = authorized_csv(self._channels)
        values["APP_MODE"] = "DEMO"
        return values

    def _load_settings_into_form(self) -> None:
        self._loading = True
        values = read_env()
        listener = values.get("TELEGRAM_LISTENER_MODE") or "USER"
        if listener not in self._listener_buttons:
            listener = "USER"
        self._set_listener_mode(listener)
        execution = values.get("EXECUTION_MODE") or "OBSERVE"
        if execution not in self._execution_buttons:
            execution = "OBSERVE"
        self._set_execution_mode(execution)
        dry = self._fields["DRY_RUN"]
        if isinstance(dry, QCheckBox):
            dry.setChecked(values.get("DRY_RUN", "true").lower() in {"", "true", "1", "yes"})
        for key, widget in self._fields.items():
            if isinstance(widget, QLineEdit):
                widget.setText(values.get(key, ""))
        self._apply_listener_visibility()
        self._refresh_execution_hint()
        self._loading = False
        self._dirty = False
        self._save_note.setText("Changes are kept after you save.")
        self._save_note.setStyleSheet(f"color: {MUTED};")

    def _apply_form(self) -> None:
        values = self._form_values()
        apply_to_process(values)
        os.environ["ALLOWED_CHANNEL_IDS"] = values.get("ALLOWED_CHANNEL_IDS", "")
        os.environ["APP_MODE"] = "DEMO"
        os.environ["DRY_RUN"] = values.get("DRY_RUN", "true")
        os.environ["EXECUTION_MODE"] = values.get("EXECUTION_MODE", "OBSERVE")
        os.environ["TELEGRAM_LISTENER_MODE"] = values.get("TELEGRAM_LISTENER_MODE", "USER")

    def _save_settings(self) -> None:
        values = self._form_values()
        write_env(values)
        save_catalog(self._channels)
        self._apply_form()
        self._reader = _open_reader()
        self._trade_token = ""
        self._dirty = False
        self._save_note.setText("Saved on this computer.")
        self._save_note.setStyleSheet(f"color: {GREEN};")
        self._channel_note.setText("Saved. Authorized channels are the ones this app reads.")

    def _save_and_restart(self) -> None:
        self._save_settings()
        if self.runtime.running:
            self.runtime.stop()
        self.runtime.start()
        self._append_log("INFO", "application", "Saved settings and started")
        self._show_page("trades")

    def _clear_session(self) -> None:
        answer = QMessageBox.question(
            self,
            "Telegram",
            "Forget the saved Telegram login on this computer?\nThe next Start will ask for a code.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        relative = self._form_values().get("TELEGRAM_SESSION_PATH") or "data/telegram.session"
        session = (PROJECT_ROOT / relative).resolve()
        for extra in (session, Path(str(session) + "-journal")):
            if extra.exists():
                extra.unlink()
        self._save_note.setText("Telegram login forgotten.")
        self._save_note.setStyleSheet(f"color: {AMBER};")

    def _start(self) -> None:
        self._apply_form()
        self.runtime.start()
        self._show_page("trades")

    def _stop(self) -> None:
        self.runtime.stop()

    def _toggle_pause(self) -> None:
        if self.runtime.state.paused:
            self.runtime.resume()
            return
        self.runtime.pause()

    def _ask_code(self) -> str:
        return self._ask("Telegram code", "Enter the code Telegram just sent you.", False)

    def _ask_password(self) -> str:
        return self._ask("Telegram password", "Enter your Telegram two-step password.", True)

    def _ask(self, title: str, message: str, secret: bool) -> str:
        if threading.current_thread() is threading.main_thread():
            return self._prompt(title, message, secret)
        holder = _PromptBox()
        self.prompt_requested.emit(title, message, secret, holder)
        if not holder.done.wait(timeout=300):
            return ""
        return holder.value

    def _show_prompt(self, title: str, message: str, secret: bool, holder: object) -> None:
        if not isinstance(holder, _PromptBox):
            return
        holder.value = self._prompt(title, message, secret)
        holder.done.set()

    def _prompt(self, title: str, message: str, secret: bool) -> str:
        dialog = PromptDialog(self, title, message, secret)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return ""
        return dialog.result_text

    def _clear_logs(self) -> None:
        self._log_lines.clear()
        self._log_box.clear()
        self._log_dirty = False

    def _show_log_filter(self) -> None:
        self._log_dirty = True
        self._flush_log()

    def _append_log(self, level: str, name: str, message: str) -> None:
        del name
        display = message
        if "  " in message:
            parts = message.split("  ", 3)
            if len(parts) == 4:
                display = f"{parts[1]}  {parts[3]}"
        self._log_lines.append((level, display))
        if len(self._log_lines) > 400:
            self._log_lines = self._log_lines[-400:]
        self._log_dirty = True

    def _flush_log(self) -> None:
        if not self._log_dirty or self._page != "log":
            return
        self._log_dirty = False
        problems = self._log_problems.isChecked()
        lines = [
            text
            for level, text in self._log_lines
            if not problems or level in {"WARNING", "ERROR", "CRITICAL"}
        ]
        self._log_box.setPlainText("\n".join(lines))
        self._log_box.moveCursor(QTextCursor.MoveOperation.End)

    def _request_setups(self) -> None:
        if self._setup_loader is not None and self._setup_loader.isRunning():
            return
        loader = _SetupLoader(self._reader)
        loader.ready.connect(self._apply_setups)
        loader.failed.connect(self._setups_failed)
        loader.finished.connect(loader.deleteLater)
        self._setup_loader = loader
        loader.start()

    def _setups_failed(self, message: str) -> None:
        self._detail.setText(f"Could not read setups: {message}")

    def _apply_setups(self, rows: object) -> None:
        if not isinstance(rows, list):
            return
        setups = [item for item in rows if isinstance(item, Setup)]
        self._setups = setups
        counts = count_feeds(setups)
        for name, button in self._tab_buttons.items():
            label = TAB_LABELS[name]
            button.setText(f"{label}  {counts[name]}")
        quiet = [event for event in self.runtime.state.loop_events if is_ignored_event(event)]
        self._ignored_button.setText(f"{len(quiet)} ignored")
        if self._ignored_list.isVisible():
            text = (
                "\n".join(ignored_line(event) for event in quiet)
                if quiet
                else "Nothing ignored in this session."
            )
            if self._ignored_list.toPlainText() != text:
                self._ignored_list.setPlainText(text)
        visible = [setup for setup in setups if feed_for(setup.state) == self._tab]
        token = self._tab + "|" + "|".join(
            f"{item.setup_id}:{item.state.value}:{item.trade_1_ticket}:{item.trade_2_ticket}:{int(item.break_even_applied)}:{item.closed_at}"
            for item in visible
        )
        if token == self._trade_token:
            return
        self._trade_token = token
        self._paint_setups(visible)

    def _refresh_trades(self, force: bool = False) -> None:
        if force:
            self._trade_token = ""
        self._request_setups()

    def _paint_setups(self, setups: list[Setup]) -> None:
        self._empty.setVisible(not setups)
        self._table.setVisible(True)
        selected = self._selected_setup_id
        self._table.blockSignals(True)
        self._table.setRowCount(0)
        for setup in setups:
            row = self._table.rowCount()
            self._table.insertRow(row)
            values = (
                format_when(setup.created_at),
                setup.symbol,
                side_text(setup.direction),
                format_zone(setup),
                format_price(setup.stop_loss),
                format_price(setup.tp1),
                format_price(setup.tp2),
                status_text(setup),
                format_legs(setup),
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                if column in {3, 4, 5, 6, 8}:
                    item.setFont(self._mono)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, setup.setup_id)
                if column == 2:
                    item.setForeground(QColor(GREEN if setup.direction.value == "BUY" else RED))
                if column == 7:
                    item.setForeground(QColor(self._state_color(setup.state)))
                self._table.setItem(row, column, item)
        self._table.blockSignals(False)
        if selected:
            self._select_setup(selected)
        elif setups:
            self._detail.setText("Select a setup.")
        else:
            self._detail.setText("")

    def _select_setup(self, setup_id: str) -> None:
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == setup_id:
                self._table.selectRow(row)
                return
        self._selected_setup_id = ""
        self._detail.setText("Select a setup.")

    def _on_setup_selected(self) -> None:
        row = self._table.currentRow()
        item = self._table.item(row, 0) if row >= 0 else None
        setup_id = str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else ""
        self._selected_setup_id = setup_id
        for setup in self._setups:
            if setup.setup_id == setup_id:
                self._detail.setText(detail_line(setup))
                return
        self._detail.setText("Select a setup.")

    def _state_color(self, state: SetupState) -> str:
        if state is SetupState.RECEIVED or state is SetupState.WAITING_ENTRY or state is SetupState.CLOSED:
            return MUTED
        if state is SetupState.ACTIVE or state is SetupState.BREAK_EVEN:
            return GREEN
        if state is SetupState.PARTIALLY_CLOSED or state is SetupState.ERROR:
            return AMBER
        if state is SetupState.CANCELLED or state is SetupState.CLOSED_BY_SIGNAL:
            return RED
        never: SetupState = state
        raise ValueError(f"Unhandled setup state: {never}")

    def _banner_state(self) -> tuple[str, str]:
        mode = self._execution_mode()
        dry = self._dry_run()
        if mode == "AUTO_DEMO" and not dry:
            return (
                "Auto on demo waits for the zone, then opens two 0.01 positions (TP1 and TP2) and moves the stop after a 4.0 favorable move. A close setup reply closes only that setup.",
                GREEN,
            )
        if mode == "AUTO_DEMO" and dry:
            return ("Auto on demo is selected. Turn off Keep orders off to open two 0.01 positions.", AMBER)
        if mode == "APPROVAL":
            return ("Wait for approval holds a setup. This screen does not send after approval yet.", AMBER)
        if mode == "OBSERVE":
            return ("Watch only stores each setup and never sends an order.", AMBER)
        raise ValueError(f"Unhandled execution mode: {mode}")

    def _paint(self, widget: QWidget, name: str, text: str | None) -> None:
        if text is not None and widget.text() != text:
            if isinstance(widget, QLabel):
                widget.setText(text)
            elif isinstance(widget, QPushButton):
                widget.setText(text)
        if widget.objectName() == name:
            return
        widget.setObjectName(name)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _link_chips(self, running: bool, state: RuntimeState) -> tuple[str, str, str, str]:
        if not running:
            return "chipOff", "Telegram off", "chipOff", "MetaTrader off"
        if state.telegram_connected:
            channels = ", ".join(state.listening_channels) or "no channels yet"
            if len(channels) > 42:
                channels = channels[:41] + "…"
            telegram_name, telegram_text = "chipOn", f"Telegram · {channels}"
        else:
            telegram_name, telegram_text = "chipWait", "Telegram connecting"
        if state.mt5_account_verified:
            account = f"{state.mt5_login or ''} · {state.mt5_server or 'Demo'}".strip(" ·")
            extra = ""
            if state.mt5_balance is not None:
                extra = f" · {state.mt5_balance:.2f} · {state.open_positions} open"
            return telegram_name, telegram_text, "chipOn", f"MetaTrader · {account}{extra}"
        if state.mt5_installation == "NOT FOUND":
            return telegram_name, telegram_text, "chipBad", "MetaTrader not found"
        return telegram_name, telegram_text, "chipWait", "MetaTrader connecting"

    def _refresh_status(self) -> None:
        state: RuntimeState = self.runtime.state
        running = self.runtime.running
        paused = running and state.paused
        if paused:
            pill = "Paused"
            copy = "Signals are on hold. Press Resume to continue."
        elif running:
            pill = "Running"
            copy = "Watching your authorized channels."
        else:
            pill = "Stopped"
            copy = "Open Setup if this is the first time, then press Start."
        values = self._form_values()
        if not running and not (values.get("TELEGRAM_API_ID") or values.get("TELEGRAM_BOT_TOKEN")):
            copy = "Add Telegram details in Setup, then press Start."
        pill_name = "pillWait" if paused else "pillOn" if running else "pillOff"
        self._paint(self._status_pill, pill_name, pill)
        if self._status_copy.text() != copy:
            self._status_copy.setText(copy)
        self._start_btn.setEnabled(not running)
        self._stop_btn.setEnabled(running)
        self._pause_btn.setText("Resume" if paused else "Pause")
        self._pause_btn.setEnabled(running)
        self._paint(self._pause_btn, "resume" if paused else "pause", None)
        telegram_name, telegram_text, mt5_name, mt5_text = self._link_chips(running, state)
        self._paint(self._telegram_chip, telegram_name, telegram_text)
        self._paint(self._mt5_chip, mt5_name, mt5_text)
        banner, banner_color = self._banner_state()
        banner_key = f"{banner}:{banner_color}"
        if banner_key != self._banner_key:
            self._banner_key = banner_key
            self._banner.setText(banner)
            self._banner.setStyleSheet(f"color: {banner_color}; background: transparent;")
        if state.last_exception:
            self._alert.setText(f"Something needs attention: {state.last_exception}")
            self._alert.setVisible(True)
        else:
            self._alert.clear()
            self._alert.setVisible(False)

    def _pump(self) -> None:
        self.runtime.poll(self.log_queue, self._ask_code, self._ask_password)
        drained = 0
        while drained < 40:
            try:
                item = self.log_queue.get_nowait()
            except queue.Empty:
                break
            drained += 1
            level = str(item.get("level", "INFO"))
            name = str(item.get("name", ""))
            message = str(item.get("message", ""))
            self._append_log(level, name, message)
        while self.log_queue.qsize() > 200:
            try:
                self.log_queue.get_nowait()
            except queue.Empty:
                break
        self._flush_log()
        self._refresh_status()
        self._setup_clock += 1
        if self._setup_clock >= 4:
            self._setup_clock = 0
            self._request_setups()

    def _schedule_update_restart(self) -> None:
        QTimer.singleShot(0, self._restart_for_update)

    def _restart_for_update(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._updater.stop()
        self._paint(self._status_pill, "pillWait", "Updating")
        if self.runtime.running:
            self.runtime.stop()
        if not self._updater.launch_pending_apply():
            root = repo_root()
            if root is not None and not is_frozen():
                relaunch("app.gui", root)
        self.close()
        os._exit(0)

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._closing:
            self._closing = True
            self._updater.stop()
            if self.runtime.running:
                self.runtime.stop()
        event.accept()


def run_gui() -> None:
    ensure_runtime_files()
    app = QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLESHEET)
    window = AppWindow()
    window.show()
    raise SystemExit(app.exec())
