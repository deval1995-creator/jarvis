"""Run Jarvis Core: `uv run python -m core` then open http://127.0.0.1:8765"""

import logging
import os

import uvicorn

from core.api import DEFAULT_PORT, create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    port = int(os.environ.get("JARVIS_PORT", DEFAULT_PORT))
    # 127.0.0.1 only: never listen on the network.
    # JARVIS_CONNECTORS=0: no claude.ai connectors (slim sessions, e.g. for test runs).
    connectors = os.environ.get("JARVIS_CONNECTORS", "1") != "0"
    uvicorn.run(create_app(port=port, connectors=connectors), host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
