"""Shared window-sizing helper for dialogs.

Background: dialogs in this app historically hardcoded ``win.geometry("WxH")``,
with W/H eyeballed against the developer's Windows (Segoe UI) font metrics. On
Linux the same widgets render a few pixels taller per row (different default
fonts), so the content overflows the fixed height and the bottom widgets -- the
Save/Cancel buttons -- get clipped off-screen. Reported by an alpha tester on
CachyOS @ 1200x800 and reproduced on Linux Mint.

``fit_window()`` replaces the hardcoded geometry: it measures what the laid-out
content actually requests on the current machine, clamps that to the visible
screen, centers it over the parent, and makes the window resizable (plus a
minsize floor) so anything clamped is still reachable by dragging. Call it
AFTER the dialog's widgets have been created.
"""

import tkinter as tk
from tkinter import ttk


def make_scrollable(win):
    """Wrap *win*'s content area in a Canvas+Scrollbar and return the inner frame.

    Call this BEFORE creating any child widgets (and after the Toplevel is set up
    with title/transient/grab_set).  All content widgets should be packed into the
    returned ``inner`` frame instead of ``win`` directly.

    Sticky Save/Cancel buttons belong outside the scroll area — pack them into
    ``win`` AFTER calling make_scrollable (they land below the canvas).

    Mousewheel scrolling works anywhere inside the dialog, not just over the
    scrollbar.  Every descendant widget is bound recursively (after_idle so it
    runs once _create_widgets has finished), and re-bound on layout changes.
    Widgets with their own scroll (Listbox, Text, Canvas) use a focus-aware
    handler: scroll the dialog while unfocused, scroll themselves once clicked
    into.  Linux uses Button-4/5; Windows/macOS use the <MouseWheel> event.

    Example usage::

        inner = make_scrollable(self)
        ttk.Label(inner, text="Some content").pack(...)
        # ... more widgets into inner ...

        # buttons stay outside the scroll so they're always visible
        btn = ttk.Frame(self)
        btn.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=8)
        ttk.Button(btn, text="OK", command=self._on_ok).pack(side=tk.RIGHT)

        fit_window(self, min_width=500)
    """
    canvas = tk.Canvas(win, borderwidth=0, highlightthickness=0)
    vsb = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)

    vsb.pack(side=tk.RIGHT, fill=tk.Y)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    inner = ttk.Frame(canvas)
    cw_id = canvas.create_window((0, 0), window=inner, anchor="nw")

    def _on_canvas_configure(event):
        canvas.itemconfigure(cw_id, width=event.width)

    canvas.bind("<Configure>", _on_canvas_configure)

    # Scroll handlers.
    def _wheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _wheel_linux(event):
        canvas.yview_scroll(-1 if event.num == 4 else 1, "units")

    # Recursively bind mousewheel to every descendant of inner so scrolling
    # works wherever the cursor is, not just over the narrow scrollbar.
    #
    # Widgets with their own scroll (Listbox, Text, inner Canvas) get a "smart"
    # handler: if the widget has keyboard focus (user clicked into it) the event
    # passes through so the widget scrolls itself; otherwise it is forwarded to
    # the canvas so the dialog scrolls.  This matches normal app behaviour —
    # you have to click into a listbox before its own scroll activates.
    _SCROLL_OWN = (tk.Listbox, tk.Text, tk.Canvas)

    def _bind_tree(widget):
        if isinstance(widget, _SCROLL_OWN):
            # Each scroll-own widget gets its own activation latch.
            # Click inside  → latch on  → widget scrolls itself.
            # Cursor leaves → latch off → dialog scrolls again.
            # This means passing the cursor over a previously-clicked widget
            # during a scroll gesture does NOT re-lock the widget, because the
            # latch was cleared the moment the cursor exited.
            active = [False]

            def _on_click(e, a=active):   a[0] = True
            def _on_leave(e, a=active):   a[0] = False

            def _smart_wheel(e, a=active):
                if a[0]:
                    return      # latched → let class binding scroll the widget
                canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
                return "break"

            def _smart_wheel_linux(e, a=active):
                if a[0]:
                    return
                canvas.yview_scroll(-1 if e.num == 4 else 1, "units")
                return "break"

            widget.bind("<Button-1>",   _on_click,           add="+")
            widget.bind("<Leave>",      _on_leave)
            widget.bind("<MouseWheel>", _smart_wheel)
            widget.bind("<Button-4>",   _smart_wheel_linux)
            widget.bind("<Button-5>",   _smart_wheel_linux)
        else:
            widget.bind("<MouseWheel>", _wheel)
            widget.bind("<Button-4>",   _wheel_linux)
            widget.bind("<Button-5>",   _wheel_linux)
        for child in widget.winfo_children():
            _bind_tree(child)

    def _on_inner_configure(event):
        canvas.configure(scrollregion=canvas.bbox("all"))
        # Re-bind after layout changes so newly added widgets are covered.
        win.after_idle(_bind_tree, inner)

    inner.bind("<Configure>", _on_inner_configure)

    # Bind once after the caller finishes creating all child widgets.
    win.after_idle(_bind_tree, inner)
    canvas.bind("<MouseWheel>", _wheel)
    canvas.bind("<Button-4>", _wheel_linux)
    canvas.bind("<Button-5>", _wheel_linux)

    return inner


def fit_window(win, min_width: int = 0, min_height: int = 0,
               max_screen_frac: float = 0.9):
    """Size *win* to its content, clamped to the screen, centered, resizable.

    Drop-in replacement for a hardcoded ``win.geometry("WxH")`` call -- call it
    after all child widgets exist so the requested size is final.

    min_width / min_height: optional floors, for when the natural content is
        smaller than you want the dialog to open (e.g. to keep a familiar
        width). Height is best left to auto-fit -- that's the bug we're fixing.
    max_screen_frac: never open larger than this fraction of the screen.
    """
    win.update_idletasks()

    # Size the content actually wants on THIS machine (honouring any floor).
    width = max(win.winfo_reqwidth(), min_width)
    height = max(win.winfo_reqheight(), min_height)

    # Never open larger than the visible screen.
    screen_w = win.winfo_screenwidth()
    screen_h = win.winfo_screenheight()
    width = min(width, int(screen_w * max_screen_frac))
    height = min(height, int(screen_h * max_screen_frac))

    # Center over the parent window when it's on-screen, else center on screen.
    x = y = None
    try:
        parent = win.master.winfo_toplevel()
        if parent.winfo_ismapped() and parent.winfo_width() > 1:
            x = parent.winfo_x() + (parent.winfo_width() - width) // 2
            y = parent.winfo_y() + (parent.winfo_height() - height) // 2
    except (AttributeError, tk.TclError):
        x = y = None
    if x is None:
        x = (screen_w - width) // 2
        y = (screen_h - height) // 3

    # Keep the whole window on-screen.
    x = max(0, min(x, screen_w - width))
    y = max(0, min(y, screen_h - height))

    win.geometry(f"{width}x{height}+{x}+{y}")

    # Backstop: let the user drag to reveal anything that got clamped, but don't
    # let them shrink back below where the content fits.
    win.resizable(True, True)
    win.minsize(width, height)
