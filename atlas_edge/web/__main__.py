"""``python -m atlas_edge.web`` — start the UI with uvicorn."""

from __future__ import annotations

import uvicorn

from ..config import get_settings


def main() -> None:
    s = get_settings()
    uvicorn.run(
        "atlas_edge.web.app:app",
        host=s.web_host,
        port=s.web_port,
        log_level=s.log_level.lower(),
    )


if __name__ == "__main__":
    main()
