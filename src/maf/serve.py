"""Run the Gateway on loopback and, optionally, the Docker host address."""

import argparse
import socket
from contextlib import ExitStack

import uvicorn

from maf.gateway import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--docker-host", default="")
    args = parser.parse_args()
    addresses = dict.fromkeys(address for address in ("127.0.0.1", args.docker_host) if address)
    with ExitStack() as stack:
        listeners = []
        for address in addresses:
            listener = stack.enter_context(socket.socket())
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind((address, args.port))
            except OSError as error:
                parser.exit(1, f"Gateway không thể nghe tại {address}:{args.port} ({error}).\n")
            listeners.append(listener)
        config = uvicorn.Config(create_app(), port=args.port, workers=1)
        server = uvicorn.Server(config)
        for address in addresses:
            print(f"Gateway: http://{address}:{args.port}/dashboard", flush=True)
        server.run(sockets=listeners)
        return 0 if server.started else 1


if __name__ == "__main__":
    raise SystemExit(main())
