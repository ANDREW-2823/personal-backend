from __future__ import annotations

import html

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.core.security import require_token
from app.db.session import get_db
from app.services import spotify_service

router = APIRouter(prefix="/spotify", tags=["spotify"])


def _callback_page(title: str, message: str, ok: bool) -> HTMLResponse:
    tone = "#67e8c7" if ok else "#ff7a7a"
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
    background:#04080b; color:#e8fbff; font:15px/1.6 'Segoe UI',system-ui,sans-serif; }}
  main {{ width:min(520px,90vw); padding:32px; border:1px solid #17323a; border-radius:18px;
    background:linear-gradient(145deg,#081116,#04090c); box-shadow:0 0 60px -24px #22d3ee; }}
  .dot {{ width:10px; height:10px; border-radius:50%; background:{tone};
    box-shadow:0 0 18px {tone}; }}
  h1 {{ font-size:20px; letter-spacing:.08em; margin:18px 0 8px; text-transform:uppercase; }}
  p {{ color:#9ec5ce; margin:0; }}
</style></head>
<body><main><div class="dot"></div>
<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></main></body></html>"""
    return HTMLResponse(body, status_code=200 if ok else 400)


@router.get("/status", dependencies=[Depends(require_token)])
def status(db: Session = Depends(get_db)) -> dict:
    return spotify_service.account_status(db)


@router.post("/connect", dependencies=[Depends(require_token)])
def connect(db: Session = Depends(get_db)) -> dict:
    try:
        return spotify_service.authorization_url(db)
    except spotify_service.SpotifyError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/disconnect", dependencies=[Depends(require_token)])
def disconnect(db: Session = Depends(get_db)) -> dict:
    spotify_service.disconnect(db)
    return {"ok": True, "message": "Spotify disconnected. Local tokens were deleted."}


@router.get("/callback")
def callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    # This endpoint is called by Spotify's redirect in the system browser, which
    # cannot attach the API token. Security comes from the single-use, signed
    # state value stored server-side; no token or account data is returned.
    if error:
        return _callback_page(
            "Spotify connection cancelled",
            "Spotify did not grant access. You can close this tab and try again in JARVIS.",
            False,
        )
    if not code or not state:
        return _callback_page(
            "Spotify connection failed",
            "The callback was missing its authorization code or state.",
            False,
        )
    try:
        account = spotify_service.exchange_code(db, code=code, state=state)
    except spotify_service.SpotifyError as exc:
        return _callback_page("Spotify connection failed", str(exc), False)
    name = account.get("display_name") or "your Spotify account"
    return _callback_page(
        "Spotify connected",
        f"JARVIS is connected to {name}. You can close this tab and return to the HUD.",
        True,
    )


@router.get("/playback", dependencies=[Depends(require_token)])
def playback(db: Session = Depends(get_db)) -> dict:
    try:
        return spotify_service.playback(db)
    except spotify_service.SpotifyError as exc:
        return {"active": False, "is_playing": False, "track": None, "error": exc.as_dict()}


@router.get("/devices", dependencies=[Depends(require_token)])
def devices(db: Session = Depends(get_db)) -> dict:
    try:
        return {"devices": spotify_service.devices(db)}
    except spotify_service.SpotifyError as exc:
        return {"devices": [], "error": exc.as_dict()}


@router.post("/search", dependencies=[Depends(require_token)])
def search(payload: dict, db: Session = Depends(get_db)) -> dict:
    query = str(payload.get("query") or "")
    try:
        return spotify_service.search(db, query=query, limit=int(payload.get("limit") or 8))
    except spotify_service.SpotifyError as exc:
        return {"tracks": [], "error": exc.as_dict()}


@router.post("/play", dependencies=[Depends(require_token)])
def play(payload: dict, db: Session = Depends(get_db)) -> dict:
    try:
        return spotify_service.play(
            db,
            query=(str(payload["query"]).strip() if payload.get("query") else None),
            uri=(str(payload["uri"]).strip() if payload.get("uri") else None),
            device_id=(str(payload["device_id"]) if payload.get("device_id") else None),
        )
    except spotify_service.SpotifyError as exc:
        return exc.as_dict()


@router.post("/pause", dependencies=[Depends(require_token)])
def pause(db: Session = Depends(get_db)) -> dict:
    try:
        return spotify_service.pause(db)
    except spotify_service.SpotifyError as exc:
        return exc.as_dict()


@router.post("/resume", dependencies=[Depends(require_token)])
def resume(db: Session = Depends(get_db)) -> dict:
    try:
        return spotify_service.resume(db)
    except spotify_service.SpotifyError as exc:
        return exc.as_dict()


@router.post("/next", dependencies=[Depends(require_token)])
def next_track(db: Session = Depends(get_db)) -> dict:
    try:
        return spotify_service.skip(db, direction="next")
    except spotify_service.SpotifyError as exc:
        return exc.as_dict()


@router.post("/previous", dependencies=[Depends(require_token)])
def previous_track(db: Session = Depends(get_db)) -> dict:
    try:
        return spotify_service.skip(db, direction="previous")
    except spotify_service.SpotifyError as exc:
        return exc.as_dict()


@router.put("/volume", dependencies=[Depends(require_token)])
def volume(payload: dict, db: Session = Depends(get_db)) -> dict:
    try:
        percent = int(payload.get("percent"))
        return spotify_service.set_volume(db, percent)
    except (TypeError, ValueError):
        return {"ok": False, "code": "invalid_request", "message": "Volume must be 0–100."}
    except spotify_service.SpotifyError as exc:
        return exc.as_dict()
