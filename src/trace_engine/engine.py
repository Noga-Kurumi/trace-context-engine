"""API pública y multiplataforma de TRACE.

El núcleo de TRACE no depende de APIs del sistema operativo. Los recolectores
disponibles se activan de forma opcional según la plataforma y dependencias
instaladas; la persistencia y recuperación funcionan en cualquier sistema.
"""

import logging
import platform
from typing import Any, Dict, Optional

from trace_engine.storage.timeline_db import TimelineDB

logger = logging.getLogger(__name__)


class TraceEngine:
    """Motor de contexto de bajo consumo con ciclo de vida explícito.

    En Windows activa los recolectores existentes cuando están disponibles.
    En otros sistemas sigue siendo útil como timeline y backend de búsqueda,
    y permite que el consumidor inyecte eventos desde sus propios recolectores.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 db: Optional[TimelineDB] = None, transcriber=None):
        self.config = dict(config or {})
        self.db = db or TimelineDB(self.config.get("db_path"))
        if transcriber is not None:
            self.config["whisper_transcriber"] = transcriber
        self.collector = None
        self._started = False

    @property
    def capabilities(self) -> Dict[str, bool]:
        """Capacidades nativas disponibles en esta plataforma."""
        return {
            "timeline": True,
            "windows_collectors": platform.system() == "Windows",
        }

    def start(self) -> None:
        """Inicia los recolectores compatibles sin hacer fallar el núcleo."""
        if self._started:
            return
        self._started = True
        if platform.system() != "Windows":
            logger.info("TRACE iniciado sin recolectores nativos para %s",
                        platform.system())
            return
        try:
            from trace_engine.collectors.coordinator import ContextCollector
            self.collector = ContextCollector(config=self.config, db=self.db)
            self.collector.start()
        except Exception as exc:
            logger.warning("No se pudieron activar los recolectores nativos: %s",
                           exc, exc_info=True)

    def stop(self) -> None:
        """Detiene recolectores y cierra la base de datos."""
        if not self._started and self.collector is None:
            return
        if self.collector is not None:
            self.collector.stop()
            self.collector = None
        self.db.close()
        self._started = False

    def insert(self, source: str, content: str, app_name: str = "",
               window_title: str = "", timestamp: Optional[float] = None) -> bool:
        """Inyecta un evento desde un recolector externo."""
        return self.db.insert(source, app_name, window_title, content, timestamp)

    def search(self, query: str, limit: int = 20):
        """Busca eventos recientes por palabras clave."""
        return self.db.search_by_keywords(query, limit)

    def close(self) -> None:
        """Alias de stop() para facilitar integración con context managers."""
        self.stop()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
