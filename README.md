# TRACE

Temporal Retrieval & Activity Context Engine.

TRACE captura actividad local de Windows, la persiste como eventos de texto en
SQLite y ofrece recuperación por palabras clave, rango temporal y MCP.

Durante el desarrollo se instala como dependencia editable:

```powershell
pip install -e .
```

El proyecto `asistente-desktop` consumirá TRACE como una librería externa.
