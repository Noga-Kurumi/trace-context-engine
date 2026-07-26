"""Recolector pasivo: portapapeles (AddClipboardFormatListener).

Ventana message-only registrada como listener de WM_CLIPBOARDUPDATE en un
hilo con message loop propio. En cada aviso lee CF_UNICODETEXT (se ignora lo
que no sea texto o esté vacío) e inserta source='clipboard'. Se deduplica el
contenido idéntico consecutivo con un hash blake2b (copiar lo mismo dos
veces seguidas no aporta contexto).
"""

import ctypes
import hashlib
import logging
import threading
from typing import Optional

from modules.timeline_db import TimelineDB

logger = logging.getLogger(__name__)

WM_CLIPBOARDUPDATE = 0x031D
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
CF_UNICODETEXT = 13
_CLASS_NAME = "TimelineClipboardListener"


class ClipboardCollector:
    """Inserta en el timeline cada texto que cae al portapapeles."""

    def __init__(self, db: TimelineDB):
        self.db = db
        self._thread: Optional[threading.Thread] = None
        self._hwnd = None
        self._last_hash: Optional[bytes] = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="collector-clipboard")
        self._thread.start()

    def stop(self) -> None:
        if self._hwnd:
            try:
                import win32gui

                win32gui.PostMessage(self._hwnd, WM_CLOSE, 0, 0)
            except Exception as e:
                logger.warning("⚠️ [CLIPBOARD] Error cerrando la ventana listener: %s", e)
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        self._hwnd = None

    # --------------------------------------------------------- hilo listener

    def _run(self) -> None:
        import win32con
        import win32gui

        user32 = ctypes.windll.user32
        hinst = win32gui.GetModuleHandle(None)

        wc = win32gui.WNDCLASS()
        wc.lpfnWndProc = self._wnd_proc
        wc.lpszClassName = _CLASS_NAME
        wc.hInstance = hinst
        try:
            class_atom = win32gui.RegisterClass(wc)
        except Exception as e:
            logger.error("❌ [CLIPBOARD] No se pudo registrar la clase de ventana: %s", e,
                         exc_info=True)
            return

        self._hwnd = win32gui.CreateWindowEx(
            0, class_atom, _CLASS_NAME, 0, 0, 0, 0, 0,
            win32con.HWND_MESSAGE, 0, hinst, None)
        if not user32.AddClipboardFormatListener(self._hwnd):
            logger.error("❌ [CLIPBOARD] AddClipboardFormatListener falló; recolector inactivo")
            return
        logger.info("✅ [CLIPBOARD] Listener de portapapeles activo")
        try:
            win32gui.PumpMessages()
        except Exception as e:
            logger.error("❌ [CLIPBOARD] Error en el message loop: %s", e, exc_info=True)
        finally:
            logger.info("🛑 [CLIPBOARD] Listener de portapapeles detenido")

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        import win32gui

        if msg == WM_CLIPBOARDUPDATE:
            try:
                self._on_clipboard_update()
            except Exception as e:
                logger.error("❌ [CLIPBOARD] Error leyendo portapapeles: %s", e, exc_info=True)
            return 0
        if msg == WM_CLOSE:
            win32gui.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            ctypes.windll.user32.RemoveClipboardFormatListener(hwnd)
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _on_clipboard_update(self) -> None:
        import win32clipboard

        text = None
        try:
            win32clipboard.OpenClipboard()
            if win32clipboard.IsClipboardFormatAvailable(CF_UNICODETEXT):
                data = win32clipboard.GetClipboardData(CF_UNICODETEXT)
                if isinstance(data, str) and data.strip():
                    text = data.strip()
        finally:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass

        if not text:
            return
        digest = hashlib.blake2b(text.encode("utf-8", "ignore"), digest_size=16).digest()
        if digest == self._last_hash:
            return  # mismo contenido copiado dos veces seguidas
        self._last_hash = digest

        if self.db.insert("clipboard", "", "", text):
            logger.debug("[CLIPBOARD] Copiado: %d chars", len(text))
