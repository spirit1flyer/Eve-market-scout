"""Keep worker print lines together in both the terminal and persistent log."""

import threading

from core.esi_diagnostics import observe_line


_output_lock = threading.RLock()


class TeeStream:
    """Buffer print fragments per thread; emit and flush complete lines."""

    def __init__(self, original, log_func):
        self.original = original
        self.log_func = log_func
        self.encoding = getattr(original, "encoding", None) or "utf-8"
        self._local = threading.local()

    def write(self, text):
        combined = getattr(self._local, "pending", "") + text
        lines = combined.split("\n")
        self._local.pending = lines.pop()
        for line in lines:
            self._emit(line, newline=True)
        return len(text)

    def _emit(self, line, newline):
        with _output_lock:
            if line.strip():
                self.log_func(line.rstrip())
                observe_line(line)
            if self.original:
                try:
                    self.original.write(line + ("\n" if newline else ""))
                    self.original.flush()
                except Exception:
                    pass  # Frozen builds may have no console.

    def flush(self):
        pending = getattr(self._local, "pending", "")
        if pending:
            self._local.pending = ""
            self._emit(pending, newline=False)
        if self.original:
            with _output_lock:
                try:
                    self.original.flush()
                except Exception:
                    pass
