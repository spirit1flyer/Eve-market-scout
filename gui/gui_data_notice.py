"""Accessible source/binary data-use disclosure and Fenris Creations notice."""

from pathlib import Path
import sys
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from gui.gui_window_utils import fit_window


def notice_path():
    base = Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent
    return base / 'ESI_DATA_USAGE.md'


def show_data_notice(parent):
    window = tk.Toplevel(parent)
    window.title('Data use and Fenris Creations notice')
    window.transient(parent)
    frame = ttk.Frame(window, padding=12)
    frame.pack(fill=tk.BOTH, expand=True)
    text = ScrolledText(frame, wrap=tk.WORD, width=88, height=30, font=('Segoe UI', 10))
    text.pack(fill=tk.BOTH, expand=True)
    try:
        content = notice_path().read_text(encoding='utf-8')
    except OSError as exc:
        content = 'The bundled data-use notice could not be read. Please reinstall the application.'
        print(f'[DataNotice] decision=READ_FAILED error_type={type(exc).__name__}')
    text.insert('1.0', content)
    text.configure(state=tk.DISABLED)
    ttk.Button(frame, text='Close', command=window.destroy).pack(pady=(10, 0))
    fit_window(window, min_width=640, min_height=420)
