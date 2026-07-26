"""Smoke tests for TRACE MCP context tools."""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import trace_engine.mcp.timeline_mcp_server as mcp_srv
from trace_engine.storage.timeline_db import TimelineDB

tmp = tempfile.mkdtemp(prefix="trace_mcp_test_")
mcp_srv._db = TimelineDB(db_path=os.path.join(tmp, "timeline.db"))
assert mcp_srv._db.insert("clipboard", "", "", "factura de la luz")

result = mcp_srv.call_timeline_tool("search_timeline_by_keywords", {"query": "factura"})
assert "factura" in result.lower()

result = mcp_srv.call_timeline_tool("get_timeline_by_time_range", {
    "start_time": "00:00", "end_time": "23:59"
})
assert "factura" in result.lower()

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def list_tools():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "trace_engine.mcp.timeline_mcp_server"],
        env={**os.environ, "PYTHONPATH": os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return [tool.name for tool in result.tools]

names = asyncio.run(list_tools())
assert "search_timeline_by_keywords" in names
assert "get_timeline_by_time_range" in names
print("TRACE MCP tools OK:", names)
