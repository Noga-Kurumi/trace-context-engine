"""Recolector pasivo: OCR de pantalla a intervalo (Windows.Media.Ocr).

Cada `ocr_interval_seconds` (config, default 12): captura el monitor
principal con mss, hashea la imagen reducida en grises (blake2b — rápido y
barato) y, solo si la pantalla cambió, corre el OCR NATIVO de Windows
(paquete `winsdk`, winsdk.windows.media.ocr) en español si el idioma está
disponible (fallback al perfil del usuario). Inserta source='ocr' con
app_name/window_title de la ventana activa en ese momento.

La API winrt es async: el hilo del recolector tiene su propio event loop
(asyncio.new_event_loop) y cada OCR es un run_until_complete.

Objetivo de recursos: <1% CPU en idle — la captura+hash es barata y el OCR
solo corre cuando la pantalla cambió. winsdk se importa perezoso: si falta el
paquete, el recolector se desactiva con un log claro y el resto sigue.
"""

import asyncio
import hashlib
import io
import logging
import threading
from typing import Optional

from trace_engine.collectors.window_change import get_foreground_window_info
from trace_engine.storage.timeline_db import TimelineDB

logger = logging.getLogger(__name__)

_HASH_WIDTH = 320  # ancho del thumbnail en grises para el hash de cambio

# Motor OCR compartido (lazy; thread-safe para el recolector y api_brain).
_engine_lock = threading.Lock()
_engine_cached = None
_engine_failed = False


def get_ocr_engine():
    """OcrEngine de Windows: español si está disponible, si no el del perfil.

    Devuelve None (con log claro) si falta winsdk o no hay motor usable.
    """
    global _engine_cached, _engine_failed
    if _engine_cached is not None or _engine_failed:
        return _engine_cached
    with _engine_lock:
        if _engine_cached is not None or _engine_failed:
            return _engine_cached
        try:
            from winsdk.windows.globalization import Language
            from winsdk.windows.media.ocr import OcrEngine

            spanish = Language("es")
            if OcrEngine.is_language_supported(spanish):
                _engine_cached = OcrEngine.try_create_from_language(spanish)
                logger.info("✅ [OCR] Motor OCR de Windows en español")
            else:
                logger.warning("⚠️ [OCR] Español no disponible en Windows OCR; "
                               "se usa el idioma del perfil de usuario")
                _engine_cached = OcrEngine.try_create_from_user_profile_languages()
            if _engine_cached is None:
                logger.error("❌ [OCR] Windows no devolvió un motor OCR usable")
                _engine_failed = True
        except ImportError as e:
            logger.error("❌ [OCR] Falta el paquete 'winsdk' (OCR nativo): %s", e)
            _engine_failed = True
        except Exception as e:
            logger.error("❌ [OCR] Error inicializando Windows OCR: %s", e, exc_info=True)
            _engine_failed = True
    return _engine_cached


async def _ocr_png_bytes(engine, png_bytes: bytes) -> str:
    """Corre Windows.Media.Ocr sobre un PNG en memoria."""
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream.get_output_stream_at(0))
    writer.write_bytes(png_bytes)
    await writer.store_async()
    stream.seek(0)

    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bitmap)
    return result.text or ""


def ocr_image(pil_image) -> str:
    """OCR nativo de Windows sobre una PIL.Image. Devuelve "" si falla.

    Optimizado: usa codificación BMP en memoria que decodifica ~3x más rápido
    en Windows.Media.Ocr (WinRT) que PNG.
    """
    engine = get_ocr_engine()
    if engine is None:
        return ""
    try:
        buffer = io.BytesIO()
        pil_image.save(buffer, format="BMP")
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                _ocr_png_bytes(engine, buffer.getvalue())) or ""
        finally:
            loop.close()
    except Exception as e:
        logger.error("❌ [OCR] Error procesando imagen: %s", e, exc_info=True)
        return ""


class ScreenOcrCollector:
    """Inserta en el timeline el texto visible en pantalla cuando cambia."""

    def __init__(self, db: TimelineDB, config=None):
        self.config = config or {}
        self.db = db
        self.interval = float(self.config.get("ocr_interval_seconds", 12))
        self.max_chars = int(self.config.get("ocr_max_chars", 4000))
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_hash: Optional[bytes] = None
        self._last_text = ""
        self.excluded_apps = {str(x).lower() for x in self.config.get("trace_excluded_apps", [])}
        self._sct = None
        self._engine = None       # OcrEngine compartido (get_ocr_engine)

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="collector-screen-ocr")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None

    # ------------------------------------------------------------- hilo OCR

    def _run(self) -> None:
        self._engine = get_ocr_engine()
        if self._engine is None:
            return  # log ya emitido; el recolector queda inactivo
        logger.info("✅ [OCR] Recolector de pantalla activo (cada %.0fs)", self.interval)
        loop = asyncio.new_event_loop()
        import mss
        self._sct = mss.mss()
        try:
            while not self._stop_event.wait(self.interval):
                try:
                    self._tick(loop)
                except Exception as e:
                    logger.error("❌ [OCR] Error en el ciclo OCR: %s", e, exc_info=True)
        finally:
            loop.close()
            if self._sct is not None:
                self._sct.close()
                self._sct = None
            logger.info("🛑 [OCR] Recolector de pantalla detenido")

    # -------------------------------------------------------------- un tick

    def _tick(self, loop) -> None:
        try:
            png_bytes, changed = self._capture_if_changed()
        except Exception as e:
            logger.warning("⚠️ [OCR] Captura fallida, saltando frame: %s", e)
            return
        if not changed:
            return
        text = loop.run_until_complete(_ocr_png_bytes(self._engine, png_bytes))
        text = " ".join((text or "").split())
        if not text:
            return
        if len(text) > self.max_chars:
            text = text[:self.max_chars]
        try:
            app_name, title = get_foreground_window_info()
        except Exception as e:
            logger.warning("⚠️ [OCR] Sin ventana activa para etiquetar: %s", e)
            app_name, title = "", ""
        if app_name.lower() in self.excluded_apps:
            return
        # Cambios visuales menores pueden producir el mismo texto. No lo
        # vuelvas a almacenar si el OCR no aporta contexto nuevo.
        if text == self._last_text:
            return
        self._last_text = text
        if self.db.insert("ocr", app_name, title, text):
            logger.debug("[OCR] Pantalla: %d chars (%s - %s)", len(text), app_name, title[:40])

    def _capture_if_changed(self) -> tuple:
        """(png_bytes, changed): captura + hash del thumbnail en grises.

        El hash corre sobre la imagen reescalada a _HASH_WIDTH px en escala de
        grises: barato y suficiente para detectar cambios de contenido.
        """
        from PIL import Image

        if self._sct is None:
            import mss
            self._sct = mss.mss()
        monitor = self._sct.monitors[1]  # monitor principal
        try:
            shot = self._sct.grab(monitor)
        except Exception as e:
            # BitBlt u otro error GDI: reinicializar contexto mss en el próximo tick.
            logger.warning("⚠️ [OCR] Error en grab (BitBlt?), reiniciando mss: %s", e)
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None
            return b"", False
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

        thumb = img.convert("L")
        ratio = _HASH_WIDTH / float(thumb.width)
        thumb = thumb.resize((_HASH_WIDTH, max(1, int(thumb.height * ratio))),
                             Image.Resampling.BILINEAR)
        digest = hashlib.blake2b(thumb.tobytes(), digest_size=16).digest()
        if digest == self._last_hash:
            return b"", False
        self._last_hash = digest

        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return buffer.getvalue(), True
