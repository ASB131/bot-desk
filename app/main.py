import asyncio
import logging

import uvicorn

from app.bot_client import ensure_commands, start_bot
from app.config import get_settings
from app.db import get_db
from app.scheduler import scheduler_loop
from app.web import create_app

log = logging.getLogger("botdesk")


async def _main() -> None:
    settings = get_settings()
    get_db()
    ensure_commands()
    if not settings.panel_password:
        log.warning("PANEL_PASSWORD is empty. The panel will not accept logins.")
    app = create_app()
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=settings.port,
        log_level="info",
        proxy_headers=False,
        forwarded_allow_ips="",
    )
    server = uvicorn.Server(config)
    log.info("Panel listening on port %s", settings.port)
    tasks = [
        asyncio.create_task(server.serve(), name="web"),
        asyncio.create_task(start_bot(), name="discord"),
        asyncio.create_task(scheduler_loop(), name="schedule"),
    ]
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                log.error("%s stopped", task.get_name(), exc_info=exc)
    finally:
        server.should_exit = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
