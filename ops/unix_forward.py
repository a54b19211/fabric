"""Forward TCP 127.0.0.1:<port> to a unix socket.

Used by ops/run_integration_tests.sh inside a private network namespace only, so that fixtures which insist on
fixed loopback ports reach the throwaway containers through their unix sockets.

    python ops/unix_forward.py <unix socket> <port>
"""

import asyncio
import functools
import sys


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def handle(sock, client_reader, client_writer):
    try:
        upstream_reader, upstream_writer = await asyncio.open_unix_connection(sock)
    except OSError:
        client_writer.close()
        return
    await asyncio.gather(pipe(client_reader, upstream_writer), pipe(upstream_reader, client_writer))


async def main(sock, port):
    server = await asyncio.start_server(functools.partial(handle, sock), "127.0.0.1", port)
    print("forwarding", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2])))
