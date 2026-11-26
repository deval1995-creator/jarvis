import logging
import os

import uvicorn

from voice.server import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
port = int(os.environ.get("JARVIS_VOICE_PORT", "8766"))
uvicorn.run(create_app(port=port), host="127.0.0.1", port=port, log_level="info")
