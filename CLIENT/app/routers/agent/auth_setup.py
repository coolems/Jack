"""Auth endpoints: setup-mode key bookkeeping + short-lived media tokens."""

from typing import Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import logging

logger = logging.getLogger("COOLEMS")


def register(router: APIRouter, provider, model_name: str, api_timeout: int,
             agent, stop_events) -> None:
    """Attach the /api/auth/* endpoints to *router* (same order as the original file)."""

    # ===== SETUP MODE ENDPOINT (2026-09-01) =====
    @router.post("/api/auth/set-key")
    async def set_api_key(request: Request, data: Dict):
        """Record the credential row {email, date_acquired, key} in CLIENT/config/.api_client_keys.json.

        This is the ONLY endpoint that works while no real key exists yet (setup mode)
        - APIMiddleware routes it through for loopback callers and rejects everything
        else with an actionable 401 until this succeeds. In keyed mode it stays
        available so the owner can change the key from Settings; the NEW key is still
        validated by the SERVER on connect, so nothing untrusted ever gets access.

        STORAGE (2026-10-08): the key IS written to this file in PLAINTEXT (plain mode -
        deliberate trade-off; per-machine, gitignored config file, no registry writes). It
        becomes the persistent outbound-auth source for every future headless start. The
        SERVER's config/.api_keys.json remains the authoritative store on the server side.
        *email* comes from the X-User-Email header when present, otherwise an optional
        "email" body field.

        Body: {"key": "<api key>", "email": "<optional>"}
        """
        try:
            import app.keys as _keys_mod

            email = ""
            headers = dict(request.headers)
            email_header = (headers.get(b"x-user-email") or b"").decode("utf-8", errors="replace").strip()
            if email_header:
                email = email_header
            else:
                email = str(data.get("email") or "").strip()

            result = _keys_mod.set_client_api_key(str(data.get("key", "") or ""), email)
            if not result.get("ok"):
                return JSONResponse(status_code=400, content={"detail": result.get("error")})
            logger.info(f"API key saved via /api/auth/set-key: {result['message']}")
            return {"status": "ok", "message": result["message"]}
        except Exception as e:
            logger.error(f"Failed to save API key: {e}")
            return JSONResponse(
                status_code=500,
                content={"detail": f"Failed to save API key: {str(e)}"}
            )

    # ===== MEDIA TOKEN ENDPOINT (SECURITY 2026-10-05) =====
    @router.post("/api/auth/media-token")
    async def issue_media_token(data: Dict):
        """Mint a short-lived, single-use media token for ONE exact file path.

        Reached only AFTER the APIMiddleware passed header auth (X-API-Key + X-User-Email),
        so it grants nothing beyond what the caller already proved. The returned ?t= token is
        bound to that exact path, expires in 60 s and allows at most 3 uses - a leaked URL is
        useless afterwards (replaces the old ?api_key=...&email=... sub-resource pattern).

        Body: {"path": "<working_root-relative file path>"}
        Returns: {"token": "...", "expires_in": 60}
        """
        try:
            from app.media_tokens import issue_token as _issue_media_token, TTL_S

            rel_path = str((data or {}).get("path") or "").strip()
            if not rel_path:
                return JSONResponse(status_code=400, content={"detail": "Missing path"})
            token = _issue_media_token(rel_path)
            # Log the PATH only - never the token (it is a credential).
            logger.info(f"Media token issued for {rel_path} (ttl={int(TTL_S)}s, max 3 uses)")
            return {"token": token, "expires_in": int(TTL_S)}
        except Exception as e:
            logger.error(f"Failed to issue media token: {e}")
            return JSONResponse(
                status_code=500,
                content={"detail": f"Failed to issue media token: {str(e)}"}
            )
