"""Servicio Whisper compartido con cola de prioridades.

TRACE es dueño del modelo y de su ciclo de vida. Los consumidores solo
encolan audio; nunca crean instancias Whisper por su cuenta.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class TranscriptionPriority(IntEnum):
    """Prioridades disponibles; menor valor significa mayor prioridad."""

    INTERACTIVE = 0
    MEETING = 10


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    source: str
    duration_seconds: float = 0.0
    error: Optional[str] = None


@dataclass(order=True)
class _Request:
    priority: int
    sequence: int
    audio: Any = field(compare=False)
    source: str = field(compare=False)
    future: "queue.Queue[TranscriptionResult]" = field(compare=False)


class TranscriptionService:
    """Una única instancia Whisper atendida por un worker priorizado."""

    def __init__(self, model_path: str, *, n_threads: int = 4,
                 language: str = "es", model: Any = None):
        self.model_path = model_path
        self.n_threads = int(n_threads or 4)
        self.language = language
        self._model = model
        self._model_lock = threading.Lock()
        self._model_load_failed = False
        self._queue: "queue.PriorityQueue[_Request]" = queue.PriorityQueue()
        self._sequence = 0
        self._sequence_lock = threading.Lock()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, daemon=True,
                                        name="trace-transcription")
        self._worker.start()

    def _get_model(self):
        if self._model is not None or self._model_load_failed:
            return self._model
        with self._model_lock:
            if self._model is not None or self._model_load_failed:
                return self._model
            try:
                if not os.path.exists(self.model_path):
                    raise FileNotFoundError(self.model_path)
                from pywhispercpp.model import Model

                self._model = Model(
                    self.model_path,
                    n_threads=self.n_threads,
                    language=self.language,
                    print_realtime=False,
                    print_progress=False,
                    print_timestamps=False,
                )
                logger.info("[TRACE-ASR] Modelo Whisper cargado: %s",
                            os.path.basename(self.model_path))
            except Exception as exc:
                self._model_load_failed = True
                logger.error("[TRACE-ASR] No se pudo cargar Whisper: %s", exc,
                             exc_info=True)
        return self._model

    def submit(self, audio: Any, *, source: str = "meeting",
               priority: TranscriptionPriority = TranscriptionPriority.MEETING
               ) -> "queue.Queue[TranscriptionResult]":
        """Encola audio y devuelve una cola con exactamente un resultado."""
        result_queue: "queue.Queue[TranscriptionResult]" = queue.Queue(maxsize=1)
        with self._sequence_lock:
            sequence = self._sequence
            self._sequence += 1
        self._queue.put(_Request(int(priority), sequence, audio, source,
                                 result_queue))
        return result_queue

    def transcribe(self, audio: Any, *, source: str = "interactive",
                   priority: TranscriptionPriority = TranscriptionPriority.INTERACTIVE
                   ) -> TranscriptionResult:
        """API bloqueante para consumidores que necesitan el texto."""
        result_queue = self.submit(audio, source=source, priority=priority)
        return result_queue.get()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                request = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                started = time.perf_counter()
                model = self._get_model()
                if model is None:
                    result = TranscriptionResult(
                        "", request.source, error="whisper_unavailable")
                else:
                    try:
                        import numpy as np
                        audio = np.ascontiguousarray(request.audio, dtype=np.float32)
                    except ImportError:
                        # Permite probar/integrar backends que ya normalizan
                        # el audio sin obligar al núcleo base a instalar numpy.
                        audio = request.audio
                    text = " ".join(
                        segment.text for segment in model.transcribe(audio)
                        if getattr(segment, "text", "").strip()).strip()
                    result = TranscriptionResult(
                        text, request.source,
                        duration_seconds=time.perf_counter() - started)
                request.future.put(result)
            except Exception as exc:
                logger.error("[TRACE-ASR] Error transcribiendo %s: %s",
                             request.source, exc, exc_info=True)
                request.future.put(TranscriptionResult(
                    "", request.source, error=str(exc)))
            finally:
                self._queue.task_done()

    def close(self) -> None:
        self._stop.set()
        if self._worker.is_alive():
            self._worker.join(timeout=5)

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()
