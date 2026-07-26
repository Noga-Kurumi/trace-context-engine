"""Servidor MCP del timeline de contexto (Fase D).

Expone la TimelineDB como 2 tools MCP:

- search_timeline_by_keywords: búsqueda FTS5 por palabras clave (cuando la
  consulta NO tiene una franja horaria clara).
- get_timeline_by_time_range: actividad entre dos horas ("09:00" a "10:00" de
  hoy; también acepta fechas ISO si el modelo las da).

Las respuestas son strings compactos (format_results de TimelineDB) para
minimizar tokens del prompt.

DOS MODOS DE USO (mismo servidor, mismo protocolo MCP):

1. En-proceso (lo que usa el asistente): call_timeline_tool() crea una
   sesión cliente MCP conectada en memoria al servidor
   (mcp.shared.memory.create_connected_server_and_client_session). Es MCP
   real — mismo código de protocolo, sin subproceso ni latencia extra — y
   corre en el hilo worker del brain (nunca en la UI).
2. Standalone por stdio (`python -m modules.timeline_mcp_server`): para
   cualquier cliente MCP externo a futuro.

La DB se abre lazy en data/timeline.db con su propia conexión: WAL +
busy_timeout permiten que conviva con los recolectores que escriben desde el
proceso principal (y con lectores externos por stdio).
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from mcp.server.fastmcp import FastMCP

from modules.timeline_db import TimelineDB

logger = logging.getLogger(__name__)

mcp_server = FastMCP("asistente-timeline")

# Conexión lazy del servidor (ver docstring del módulo).
_db: Optional[TimelineDB] = None


def _get_db() -> TimelineDB:
    global _db
    if _db is None:
        _db = TimelineDB()
    return _db


def _parse_time(value: str) -> float:
    """'HH:MM'/'HH:MM:SS' (hoy, hora local) o ISO ('YYYY-MM-DDTHH:MM') → epoch.

    Raises:
        ValueError: si el formato no se reconoce (mensaje claro para el modelo).
    """
    v = (value or "").strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(v, fmt)
            now = datetime.now()
            return datetime(now.year, now.month, now.day,
                            t.hour, t.minute, t.second).timestamp()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(v).timestamp()
    except ValueError:
        raise ValueError(
            f"hora no reconocida: {value!r} (usá 'HH:MM' de hoy o ISO 'YYYY-MM-DDTHH:MM')")


# --------------------------------------------------------------------- tools

@mcp_server.tool()
def search_timeline_by_keywords(query: str, limit: int = 10) -> str:
    """Busca en el timeline de actividad del usuario por PALABRAS CLAVE.

    El timeline registra qué apps usó (cambios de ventana), qué texto tenía
    visible en pantalla (OCR), qué copió al portapapeles y qué dijo/escuchó en
    reuniones de Discord (audio transcrito). Usá esta tool cuando la consulta
    NO mencione una franja horaria concreta.

    Args:
        query: Palabras clave a buscar (texto libre, en español).
        limit: Máximo de registros a devolver (default 10).

    Returns:
        Una línea por registro: "[HH:MM] fuente/app - ventana: contenido",
        ordenadas de más reciente a más vieja; o un aviso si no hay resultados.
    """
    rows = _get_db().search_by_keywords(query, limit=limit)
    logger.info("🔧 [MCP] search_timeline_by_keywords(query=%r, limit=%d) → %d filas",
                query, limit, len(rows))
    if not rows:
        return "Sin registros en el timeline para esa búsqueda."
    return TimelineDB.format_results(rows)


@mcp_server.tool()
def get_timeline_by_time_range(start_time: str, end_time: str, limit: int = 50) -> str:
    """Devuelve la actividad del usuario en una FRANJA HORARIA concreta.

    Usá esta tool cuando la consulta mencione horas o franjas ("esta mañana",
    "de 9 a 10", "ayer a las 15"). La hora actual del sistema se indica en el
    prompt: resolvé franjas relativas a rangos concretos antes de llamarla.

    Args:
        start_time: Hora de inicio: "HH:MM" (hoy) o ISO "YYYY-MM-DDTHH:MM".
        end_time: Hora de fin: "HH:MM" (hoy) o ISO "YYYY-MM-DDTHH:MM".
        limit: Máximo de registros a devolver (default 50).

    Returns:
        Una línea por registro en orden cronológico; o un aviso si no hay
        registros en esa franja.
    """
    try:
        t0 = _parse_time(start_time)
        t1 = _parse_time(end_time)
    except ValueError as e:
        logger.warning("⚠️ [MCP] get_timeline_by_time_range: %s", e)
        return f"Error de parámetros: {e}"
    if t1 < t0:
        t0, t1 = t1, t0
    rows = _get_db().get_by_time_range(t0, t1, limit=limit)
    logger.info("🔧 [MCP] get_timeline_by_time_range(%r→%r, limit=%d) → %d filas",
                start_time, end_time, limit, len(rows))
    if not rows:
        return "Sin registros en el timeline en esa franja horaria."
    return TimelineDB.format_results(rows)


# ---------------------------------------------------- ejecución en-proceso

async def _call_tool_async(name: str, arguments: dict) -> str:
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(
            mcp_server._mcp_server) as session:
        result = await session.call_tool(name, arguments)
    text = "\n".join(getattr(c, "text", str(c)) for c in result.content)
    if result.isError:
        logger.error("❌ [MCP] La tool %s devolvió error: %s", name, text)
    return text


def call_timeline_tool(name: str, arguments: dict) -> str:
    """Ejecuta una tool del servidor MCP del timeline (protocolo MCP real,
    en-proceso). Pensada para el worker del brain (hilo no-UI): crea un loop
    asyncio por llamada. Nunca lanza: los errores vuelven como string.
    """
    try:
        return asyncio.run(_call_tool_async(name, arguments or {}))
    except Exception as e:
        logger.error("❌ [MCP] Error ejecutando la tool %s(%s): %s",
                     name, arguments, e, exc_info=True)
        return f"Error ejecutando la tool {name}: {e}"


# --------------------------------------------------------------- standalone

def main() -> None:
    """Servidor MCP standalone por stdio (clientes externos)."""
    logging.basicConfig(level=logging.INFO)
    logger.info("🚀 [MCP] Servidor del timeline por stdio (data/timeline.db)")
    mcp_server.run()  # transport='stdio' por defecto


if __name__ == "__main__":
    main()
