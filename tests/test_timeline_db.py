"""Test vital (barato): roundtrip de TimelineDB con FTS5.

Insert de cada source, search_by_keywords (MATCH), get_by_time_range,
cleanup_old_records borrando lo viejo y FTS sincronizada tras el delete.
Usa una DB temporal (no toca data/timeline.db real).
"""

import os
import sys

# tests/ esta un nivel debajo de la raiz: que los imports del proyecto resuelvan.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import os
import tempfile
import time

from trace_engine.storage.timeline_db import TimelineDB, SOURCES

tmp = tempfile.mkdtemp(prefix="timeline_test_")
db = TimelineDB(db_path=os.path.join(tmp, "timeline.db"))
now = time.time()

# ---- insert de cada source + ignorar vacíos ----
for i, source in enumerate(SOURCES):
    assert db.insert(source, "App%d" % i, "Ventana %d" % i,
                     f"contenido de {source} numero {i}", timestamp=now - i)
assert db.insert("ocr", "Chrome", "Tab", "") is False      # vacío: ignora
assert db.insert("ocr", "Chrome", "Tab", None) is False
try:
    db.insert("source_trucho", "", "", "x")
    raise AssertionError("source inválido debió lanzar ValueError")
except ValueError:
    pass
print("insert OK (5 sources, vacíos ignorados, source inválido rechazado)")

# ---- search_by_keywords (FTS MATCH) ----
rows = db.search_by_keywords("clipboard")
assert len(rows) == 1 and rows[0][2] == "clipboard", rows
rows = db.search_by_keywords("contenido numero")
assert len(rows) == 5, rows  # OR entre términos, todas las filas matchean
assert rows[0][1] >= rows[-1][1], "debe ordenar por timestamp DESC"
rows = db.search_by_keywords('inyeccion "peligrosa" ; DROP')
assert rows == [], "la query con caracteres FTS no debe romper ni matchear"
print("search_by_keywords OK (MATCH, OR, orden DESC, sanitizada)")

# ---- get_by_time_range ----
rows = db.get_by_time_range(now - 3, now)
assert len(rows) == 4, rows
assert [r[1] for r in rows] == sorted(r[1] for r in rows), "debe ordenar ASC"
rows = db.get_by_time_range(now - 100, now - 10)
assert rows == []
print("get_by_time_range OK (rango, orden ASC)")

# ---- sesiones de ventana con duración ----
session_id = db.start_window_session("Code.exe", "archivo.py", now - 20)
db.end_window_session(session_id, now - 5)
sessions = db.get_window_sessions(now - 30, now)
assert len(sessions) == 1 and sessions[0][0] == session_id, sessions
assert sessions[0][2] == now - 5, sessions
print("window_sessions OK (inicio, fin y rango temporal)")

# ---- format compacto ----
rows = db.search_by_keywords("clipboard")
texto = TimelineDB.format_results(rows, max_chars=20)
assert texto.startswith("[") and "clipboard/" in texto and "…" in texto, texto
print("format_results OK:", texto.splitlines()[0][:70], "...")

# ---- cleanup_old_records + FTS sincronizada tras delete ----
borrados = db.cleanup_old_records(retention_hours=0)  # todo es "viejo"
assert borrados == 5, borrados
assert db.search_by_keywords("contenido") == [], \
    "la FTS debe quedar sincronizada tras el DELETE (triggers)"
assert db.get_by_time_range(0, now + 10) == []
print("cleanup_old_records OK (5 borrados, FTS sincronizada)")

db.close()
print("\nTODO OK")
