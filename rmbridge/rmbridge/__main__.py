import logging
import os
from pathlib import Path

import uvicorn

from .app import create_app


def load_dotenv(path: Path) -> None:
    """Minimal .env reader for running outside Docker; real env vars win."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def main() -> None:
    load_dotenv(Path(os.environ.get("RMBRIDGE_ENV", ".env")))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(
        create_app(),
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8484")),
    )


if __name__ == "__main__":
    main()
