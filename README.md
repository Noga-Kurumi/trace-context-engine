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

El nÃºcleo de TRACE es multiplataforma: SQLite, el timeline y la bÃºsqueda no
requieren APIs especÃ­ficas del sistema operativo. Los recolectores nativos de
Windows son opcionales:

```powershell
pip install -e ".[windows]"
```

Uso bÃ¡sico:

```python
from trace_engine import TraceEngine

with TraceEngine() as trace:
    trace.insert("clipboard", "texto copiado")
    resultados = trace.search("texto")
```

En Linux y macOS TRACE funciona como motor de persistencia y recuperaciÃ³n.
Los recolectores especÃ­ficos de cada plataforma pueden ser aportados por la
aplicaciÃ³n o mediante futuros plugins.

Para evitar duplicar modelos de transcripciÃ³n, una aplicaciÃ³n que ya disponga
de un transcriptor puede inyectarlo:

```python
trace = TraceEngine(transcriber=transcriptor_existente)
```

El objeto debe exponer `transcribe(audio)`. El recolector de reuniones usarÃ¡
esa instancia y no cargarÃ¡ otro modelo Whisper.
