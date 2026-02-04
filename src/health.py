import asyncio
import logging
from asyncio import StreamReader, StreamWriter

logger = logging.getLogger(__name__)

_server: asyncio.Server | None = None


async def start_health_server(port: int = 8080) -> None:
    global _server

    async def handle(reader: StreamReader, writer: StreamWriter) -> None:
        await reader.read(1024)
        response = "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nOK"
        writer.write(response.encode())
        await writer.drain()
        writer.close()

    _server = await asyncio.start_server(handle, "0.0.0.0", port)
    logger.info("Health check server listening on port %d", port)


async def stop_health_server() -> None:
    global _server
    if _server is not None:
        _server.close()
        await _server.wait_closed()
        _server = None
