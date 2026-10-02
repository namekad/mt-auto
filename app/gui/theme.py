from __future__ import annotations

BG = "#0B1020"
SURFACE = "#12182A"
CARD = "#1B2436"
BORDER = "#31415C"
TEXT = "#F7F9FC"
MUTED = "#A8B3C7"
BLUE = "#5B9DFF"
BLUE_HOVER = "#3D84F0"
GREEN = "#3DDC97"
AMBER = "#FFC857"
RED = "#FF5D73"
YELLOW_TEXT = "#1A1408"
INK = "#06281C"

APP_STYLESHEET = f"""
QWidget {{
    background: {BG};
    color: {TEXT};
    font-family: "Segoe UI";
    font-size: 13px;
}}
QMainWindow, QScrollArea, QAbstractScrollArea {{
    background: {BG};
    border: none;
}}
QFrame#sidebar {{
    background: {SURFACE};
    border-right: 1px solid {BORDER};
}}
QFrame#topbar {{
    background: {SURFACE};
    border-bottom: 1px solid {BORDER};
}}
QFrame#card {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 12px;
}}
QLabel#title {{
    font-size: 22px;
    font-weight: 700;
    background: transparent;
}}
QLabel#muted, QLabel#hint {{
    color: {MUTED};
    background: transparent;
}}
QLabel#section {{
    font-size: 15px;
    font-weight: 700;
    background: transparent;
}}
QLabel#pillOff, QLabel#pillOn, QLabel#pillWait, QLabel#chipOff, QLabel#chipOn, QLabel#chipWait, QLabel#chipBad {{
    border-radius: 10px;
    padding: 4px 10px;
    font-weight: 700;
}}
QLabel#pillOff, QLabel#chipOff {{
    color: {MUTED};
    background: {BG};
    border: 1px solid {BORDER};
}}
QLabel#pillOn, QLabel#chipOn {{
    color: {GREEN};
    background: #12352A;
    border: 1px solid #1F6B4A;
}}
QLabel#pillWait, QLabel#chipWait {{
    color: {AMBER};
    background: #3A2E12;
    border: 1px solid #6B5420;
}}
QLabel#chipBad {{
    color: {RED};
    background: #3A1820;
    border: 1px solid #7A3040;
}}
QLabel#alert {{
    color: {AMBER};
    background: transparent;
}}
QLabel#banner {{
    background: transparent;
}}
QPushButton {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 8px 14px;
}}
QPushButton:hover {{
    background: #243044;
}}
QPushButton:disabled {{
    color: {MUTED};
    background: {BG};
}}
QPushButton#pause {{
    background: #3A2E12;
    color: {AMBER};
    border: 1px solid {AMBER};
    font-weight: 700;
}}
QPushButton#pause:hover {{
    background: #4A3B16;
}}
QPushButton#resume {{
    background: {GREEN};
    color: {INK};
    border: none;
    font-weight: 700;
}}
QPushButton#resume:hover {{
    background: #2EC888;
}}
QPushButton#pause:disabled, QPushButton#resume:disabled {{
    background: {BG};
    color: {MUTED};
    border: 1px solid {BORDER};
}}
QPushButton#start {{
    background: #7C3AED;
    color: white;
    border: none;
    font-weight: 700;
}}
QPushButton#start:hover {{
    background: #6D28D9;
}}
QPushButton#start:disabled {{
    background: {BORDER};
    color: {MUTED};
}}
QPushButton#primary {{
    background: {BLUE};
    color: white;
    border: none;
    font-weight: 700;
}}
QPushButton#primary:hover {{
    background: {BLUE_HOVER};
}}
QPushButton#primary:disabled {{
    background: {BORDER};
    color: {MUTED};
}}
QPushButton#danger {{
    background: {RED};
    color: white;
    border: none;
    font-weight: 700;
}}
QPushButton#danger:hover {{
    background: #E04060;
}}
QPushButton#danger:disabled {{
    background: {BG};
    color: {MUTED};
    border: 1px solid {BORDER};
}}
QPushButton#nav {{
    text-align: left;
    background: transparent;
    border: none;
    border-radius: 8px;
    padding: 10px 14px;
    font-weight: 600;
}}
QPushButton#nav:hover {{
    background: #243044;
}}
QPushButton#nav:checked {{
    background: #1A3358;
    color: {BLUE};
}}
QPushButton#tab:checked {{
    background: #1A3358;
    color: {TEXT};
    border: 1px solid {BLUE};
}}
QLineEdit, QPlainTextEdit, QTextEdit {{
    background: {BG};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px 8px;
    selection-background-color: #1A3358;
}}
QTableWidget {{
    background: {BG};
    alternate-background-color: #121A2C;
    color: {TEXT};
    gridline-color: {BORDER};
    border: 1px solid {BORDER};
    border-radius: 10px;
    selection-background-color: #1A3358;
    selection-color: {TEXT};
}}
QHeaderView::section {{
    background: {SURFACE};
    color: {MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 8px;
    font-weight: 600;
}}
QCheckBox {{
    background: transparent;
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {BORDER};
    border-radius: 4px;
    background: {BG};
}}
QCheckBox::indicator:checked {{
    background: {BLUE};
    border-color: {BLUE};
}}
QScrollBar:vertical {{
    background: {BG};
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER};
    border-radius: 5px;
    min-height: 24px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QProgressBar {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    height: 10px;
    text-align: center;
    color: {TEXT};
    font-size: 11px;
}}
QProgressBar::chunk {{
    background: {BLUE};
    border-radius: 5px;
}}
"""
