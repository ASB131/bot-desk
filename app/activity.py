from app.db import get_db


def log_activity(
    kind: str,
    summary: str,
    *,
    detail: str = "",
    guild: str = "",
    channel: str = "",
    ok: bool = True,
) -> None:
    try:
        get_db().add_activity(
            kind=kind,
            summary=summary,
            detail=detail,
            guild=guild,
            channel=channel,
            ok=ok,
        )
    except Exception:
        pass
