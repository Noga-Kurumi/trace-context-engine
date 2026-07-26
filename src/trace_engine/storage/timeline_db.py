"""Contexto en forma de timeline persistido en SQLite local (Fase A).

Guarda eventos pasivos del entorno del usuario (cambios de ventana, OCR de
pantalla, portapapeles; audio_in/audio_out reservados para fases futuras) en
una base SQLite con índice full-text FTS5 para recuperación por keywords.

Decisiones de diseño:
- Archivo: data/timeline.db (raíz del repo; la carpeta data/ está gitignored).
- Concurrencia: UNA conexión compartida con check_same_thread=False +
  threading.Lock (los recolectores escriben desde sus hilos y las consultas
  pueden venir del hilo del LLM). SQLite serializa escrituras de todas formas;
  WAL + busy_timeout permiten lecturas mientras se escribe.
- FTS5 "external content" (content=timeline): la tabla virtual no duplica el
  texto; los triggers la mantienen sincronizada en INSERT/UPDATE/DELETE.
- Base circular: cleanup_old_records() borra por antigüedad (lo llama el
  coordinator al arranque y cada hora); los triggers limpian la FTS sola.

API:
    db = TimelineDB()                       # data/timeline.db por defecto
    db.insert("ocr", "Chrome", "Título", "texto...")
    db.cleanup_old_records(retention_hours=72)
    rows = db.search_by_keywords("factura luz", limit=20)   # timestamp DESC
    rows = db.get_by_time_range(t0, t1, limit=200)          # timestamp ASC
    texto = db.format_results(rows, max_chars=200)          # compacto p/ prompt
"""

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "data", "timeline.db")

SOURCES = ("app_change", "ocr", "audio_in", "audio_out", "clipboard")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS timeline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('app_change','ocr','audio_in','audio_out','clipboard')),
    app_name TEXT,
    window_title TEXT,
    content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_timeline_timestamp ON timeline(timestamp);

CREATE VIRTUAL TABLE IF NOT EXISTS timeline_fts USING fts5(
    content, app_name, window_title,
    content='timeline', content_rowid='id'
);

-- Mantenimiento de la FTS (esquema estándar FTS5 external content).
CREATE TRIGGER IF NOT EXISTS timeline_ai AFTER INSERT ON timeline BEGIN
    INSERT INTO timeline_fts(rowid, content, app_name, window_title)
    VALUES (new.id, new.content, new.app_name, new.window_title);
END;
CREATE TRIGGER IF NOT EXISTS timeline_ad AFTER DELETE ON timeline BEGIN
    INSERT INTO timeline_fts(timeline_fts, rowid, content, app_name, window_title)
    VALUES ('delete', old.id, old.content, old.app_name, old.window_title);
END;
CREATE TRIGGER IF NOT EXISTS timeline_au AFTER UPDATE ON timeline BEGIN
    INSERT INTO timeline_fts(timeline_fts, rowid, content, app_name, window_title)
    VALUES ('delete', old.id, old.content, old.app_name, old.window_title);
    INSERT INTO timeline_fts(rowid, content, app_name, window_title)
    VALUES (new.id, new.content, new.app_name, new.window_title);
END;
"""

# Fila devuelta por las consultas: (id, timestamp, source, app_name, window_title, content)
Row = Tuple[int, float, str, Optional[str], Optional[str], str]

_SELECT_COLS = "id, timestamp, source, app_name, window_title, content"


class TimelineDB:
    """Acceso thread-safe a la base del timeline (conexión única + lock)."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        logger.info("✅ [TIMELINE] Base de datos lista en %s", self.db_path)

    # ------------------------------------------------------------- escritura

    def insert(self, source: str, app_name: Optional[str], window_title: Optional[str],
               content: Optional[str], timestamp: Optional[float] = None) -> bool:
        """Inserta un evento. Ignora content vacío/None (devuelve False)."""
        if not content or not str(content).strip():
            return False
        if source not in SOURCES:
            raise ValueError(f"source inválido: {source!r} (válidos: {SOURCES})")
        ts = float(timestamp) if timestamp is not None else time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO timeline (timestamp, source, app_name, window_title, content)"
                " VALUES (?, ?, ?, ?, ?)",
                (ts, source, app_name or "", window_title or "", str(content).strip()))
            self._conn.commit()
        return True

    def cleanup_old_records(self, retention_hours: float) -> int:
        """Borra registros más viejos que retention_hours (base circular).

        Los triggers AFTER DELETE limpian la FTS automáticamente.
        Devuelve cuántos registros se borraron.
        """
        cutoff = time.time() - float(retention_hours) * 3600
        with self._lock:
            cur = self._conn.execute("DELETE FROM timeline WHERE timestamp < ?", (cutoff,))
            self._conn.commit()
            deleted = cur.rowcount
        if deleted:
            logger.info("🧹 [TIMELINE] Limpieza: %d registros con más de %.0fh borrados",
                        deleted, retention_hours)
        return deleted

    # -------------------------------------------------------------- consulta

    def search_by_keywords(self, query: str, limit: int = 20) -> List[Row]:
        """Busca por keywords en la FTS (content, app_name, window_title).

        Cada término se entrecomilla (MATCH seguro, sin inyección de sintaxis
        FTS5) y se unen con OR. Devuelve filas ordenadas por timestamp DESC.
        """
        terms = [t.replace('"', "").strip() for t in (query or "").split()]
        terms = [t for t in terms if t]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        with self._lock:
            cur = self._conn.execute(
                f"SELECT {_SELECT_COLS} FROM timeline"
                " WHERE id IN (SELECT rowid FROM timeline_fts WHERE timeline_fts MATCH ?)"
                " ORDER BY timestamp DESC LIMIT ?",
                (match, int(limit)))
            return cur.fetchall()

    def get_by_time_range(self, start_epoch: float, end_epoch: float,
                          limit: int = 200) -> List[Row]:
        """Filas con timestamp en [start_epoch, end_epoch], orden ASC."""
        with self._lock:
            cur = self._conn.execute(
                f"SELECT {_SELECT_COLS} FROM timeline"
                " WHERE timestamp BETWEEN ? AND ?"
                " ORDER BY timestamp ASC LIMIT ?",
                (float(start_epoch), float(end_epoch), int(limit)))
            return cur.fetchall()

    # --------------------------------------------------------------- formato

    @staticmethod
    def format_results(rows: List[Row], max_chars: int = 200) -> str:
        """Formato compacto (una línea por registro) para minimizar tokens:

            [14:32] ocr/Chrome - Título ventana: contenido truncado…
        """
        lines = []
        for _id, ts, source, app_name, window_title, content in rows:
            hora = datetime.fromtimestamp(ts).strftime("%H:%M")
            texto = " ".join(str(content).split())  # colapsar saltos de línea
            if len(texto) > max_chars:
                texto = texto[:max_chars] + "…"
            origen = source
            if app_name:
                origen += f"/{app_name}"
            cabecera = f"[{hora}] {origen}"
            if window_title:
                cabecera += f" - {window_title}"
            lines.append(f"{cabecera}: {texto}")
        return "\n".join(lines)

    # ---------------------------------------------------------------- cierre

    def close(self) -> None:
        """Cierra la conexión (al salir de la app)."""
        with self._lock:
            self._conn.close()
