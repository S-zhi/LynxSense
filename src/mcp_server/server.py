"""Subtitles AI MCP Server 入口。

默认使用 stdio，适合桌面 MCP Host；设置
``SUBTRANS_MCP_TRANSPORT=streamable-http`` 可启动独立 HTTP MCP 服务。
"""

from __future__ import annotations

import logging
import os

from mcp.server import MCPServer

from .tools import register_tools

logging.basicConfig(level=os.getenv("SUBTRANS_MCP_LOG_LEVEL", "INFO"))

mcp = MCPServer("Subtitles AI MCP")
registered_tools = register_tools(mcp)


def _port() -> int:
    try:
        return int(os.getenv("SUBTRANS_MCP_PORT", "3001"))
    except ValueError:
        return 3001


def main() -> None:
    transport = os.getenv("SUBTRANS_MCP_TRANSPORT", "stdio").strip().lower()
    if transport == "stdio":
        mcp.run()
        return
    if transport in {"streamable-http", "streamable_http"}:
        mcp.run(
            transport="streamable-http",
            host=os.getenv("SUBTRANS_MCP_HOST", "127.0.0.1"),
            port=_port(),
            streamable_http_path=os.getenv("SUBTRANS_MCP_PATH", "/mcp"),
        )
        return
    raise ValueError(
        "SUBTRANS_MCP_TRANSPORT 必须是 stdio 或 streamable-http"
    )


if __name__ == "__main__":
    main()
