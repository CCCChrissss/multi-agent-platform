"""Windows-compatible launcher for the shared agent runtime."""

from __future__ import annotations

import uvicorn

from persistence.asyncio_compat import configure_asyncio_for_psycopg


def main() -> None:
    # Uvicorn normally creates the event loop before importing the ASGI app.
    # Psycopg needs the selector policy to be installed before that happens.
    configure_asyncio_for_psycopg()
    # Uvicorn 0.36+ otherwise supplies ProactorEventLoop as an explicit loop
    # factory on Windows, overriding the selector policy installed above.
    uvicorn.run("agents.runtime:app", host="127.0.0.1", port=8003, loop="none")


if __name__ == "__main__":
    main()
