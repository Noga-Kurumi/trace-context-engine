# TRACE

Temporal Retrieval & Activity Context Engine.

TRACE captura actividad local de Windows, la persiste como eventos de texto en
SQLite y ofrece recuperación por palabras clave, rango temporal y MCP.

Durante el desarrollo se instala como dependencia editable:

```powershell
pip install -e .
```

TRACE puede ser consumido por cualquier aplicación local que necesite contexto
de actividad del equipo, incluyendo asistentes de IA.
