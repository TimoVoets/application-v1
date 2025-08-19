"""OAuth helpers for Gmail and Outlook.

Required environment variables:
- ``GOOGLE_CLIENT_ID``
- ``GOOGLE_CLIENT_SECRET``
- ``GOOGLE_REDIRECT_URI``
- ``MS_CLIENT_ID``
- ``MS_CLIENT_SECRET``
- ``MS_REDIRECT_URI``
- ``SUPABASE_URL``
- ``SUPABASE_KEY``
"""

from fastapi import APIRouter
from fastapi.responses import RedirectResponse, JSONResponse, StreamingResponse
import os, requests, datetime, time, re, base64, io, urllib.parse
from supabase import create_client
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import Optional, List, Dict
import logging
from utils.logging import get_logger

load_dotenv()


def validate_env() -> None:
    """Validate presence of required environment variables."""
    required_keys = [
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REDIRECT_URI",
        "MS_CLIENT_ID",
        "MS_CLIENT_SECRET",
        "MS_REDIRECT_URI",
        "SUPABASE_URL",
        "SUPABASE_KEY",
    ]
    missing = [k for k in required_keys if not os.getenv(k)]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )


validate_env()

router = APIRouter()
logger = get_logger(__name__)

# === Config uit .env ===
# Gmail
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

# Outlook / Microsoft
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID")
MS_CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET")
MS_REDIRECT_URI = os.getenv("MS_REDIRECT_URI")
MS_TENANT = os.getenv("MS_TENANT", "common")
MS_SCOPES = os.getenv("MS_SCOPES", "openid profile offline_access https://graph.microsoft.com/Mail.Read")
AUTH_URL_MS  = f"https://login.microsoftonline.com/{MS_TENANT}/oauth2/v2.0/authorize"
TOKEN_URL_MS = f"https://login.microsoftonline.com/{MS_TENANT}/oauth2/v2.0/token"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Overig
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ==== Models ====
class OAuthInitiateRequest(BaseModel):
    user_id: str
    redirect_url: str | None = None

class GmailSettingsUpdate(BaseModel):
    user_id: str
    subject_filter: Optional[str] = None
    token_id: Optional[int] = None  # specifiek account; anders alle accounts van user

# ---------- Helpers ----------
def _to_epoch_seconds(value) -> int:
    if value is None:
        return 0
    if isinstance(value, datetime.datetime):
        return int(value.timestamp())
    if isinstance(value, str):
        s = value.strip().replace("Z", "+00:00").replace(" ", "T")
        m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.(\d+))?(.+)?$", s)
        if m:
            base = m.group(1)
            frac = (m.group(3) or "")
            tz = m.group(4) or ""
            if frac:
                frac = (frac + "000000")[:6]
                s = f"{base}.{frac}{tz}"
        dt = datetime.datetime.fromisoformat(s)
        return int(dt.timestamp())
    return 0

def _epoch_ms_to_iso_utc(ms: int) -> str:
    return datetime.datetime.utcfromtimestamp(ms/1000).replace(microsecond=0).isoformat() + "Z"

def already_seen(user_id: str, msg_id: str) -> bool:
    res = supabase.table("email_seen").select("message_id").eq("user_id", user_id).eq("message_id", msg_id).execute()
    return len(res.data) > 0

def mark_seen(user_id: str, msg_id: str):
    # Upsert voorkomt duplicates / race conditions
    supabase.table("email_seen").upsert(
        {"user_id": user_id, "message_id": msg_id},
        on_conflict="user_id,message_id"
    ).execute()

def push_to_n8n(user_id: str, message: dict, provider: str):
    if not N8N_WEBHOOK_URL:
        return
    try:
        requests.post(
            N8N_WEBHOOK_URL,
            json={"user_id": user_id, "provider": provider, "message": message},
            timeout=20,
        )
    except requests.RequestException as e:
        logger.error("Failed to push to n8n", exc_info=e)

def refresh_access_token_google(refresh_token: str) -> dict:
    data = {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    r = requests.post("https://oauth2.googleapis.com/token", data=data, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"google_refresh_http_{r.status_code}: {r.text}")
    return r.json()

def refresh_access_token_ms(refresh_token: str) -> dict:
    data = {
        "client_id": MS_CLIENT_ID,
        "client_secret": MS_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "redirect_uri": MS_REDIRECT_URI,
    }
    r = requests.post(TOKEN_URL_MS, data=data, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"ms_refresh_http_{r.status_code}: {r.text}")
    return r.json()

def get_valid_token(row):
    """
    Geef een bruikbare access_token terug. Probeer te refreshen als bijna verlopen.
    Als refreshen faalt (bv. invalid_grant 400), raise RuntimeError zodat caller kan skippen.
    """
    now = int(time.time())
    expires_at_epoch = _to_epoch_seconds(row.get("expires_at"))
    access_token = row["access_token"]
    provider = row["provider"]

    # Nog geldig? direct teruggeven
    if expires_at_epoch and now < (expires_at_epoch - 60):
        return access_token

    rt = row.get("refresh_token")
    if not rt:
        raise RuntimeError("no_refresh_token")

    try:
        if provider == "gmail":
            new = refresh_access_token_google(rt)
        else:
            new = refresh_access_token_ms(rt)
    except (requests.RequestException, RuntimeError) as e:
        raise RuntimeError(f"refresh_failed: {e}")

    new_expires = datetime.datetime.utcnow() + datetime.timedelta(seconds=new.get("expires_in", 3600))
    updates = {
        "access_token": new["access_token"],
        "expires_at": new_expires.isoformat()
    }
    if "refresh_token" in new and new["refresh_token"]:
        updates["refresh_token"] = new["refresh_token"]

    supabase.table("email_tokens").update(updates).eq("id", row["id"]).execute()
    return new["access_token"]

def _fetch_gmail_address(access_token: str) -> str | None:
    """Haalt primair Gmail-adres op (scope: gmail.readonly of gmail.metadata)."""
    try:
        r = requests.get(
            f"{GMAIL_API}/profile",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("emailAddress")
    except requests.RequestException as e:
        logger.error("Failed to fetch Gmail address", exc_info=e)
    return None

# ---------- Gmail: OAuth + helpers ----------
@router.post("/oauth/gmail/initiate")
def initiate_oauth_gmail(request: OAuthInitiateRequest):
    scope = urllib.parse.quote("https://www.googleapis.com/auth/gmail.readonly")
    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={urllib.parse.quote(GOOGLE_REDIRECT_URI, safe='')}"
        f"&response_type=code"
        f"&scope={scope}"
        f"&access_type=offline"
        f"&state={request.user_id}"
        f"&prompt=consent"
    )
    return JSONResponse(content={"auth_url": auth_url})

@router.get("/oauth/gmail/status/{user_id}")
def get_gmail_status(user_id: str):
    try:
        result = supabase.table("email_tokens").select("*").eq("user_id", user_id).eq("provider", "gmail").execute()
        connected_accounts: List[Dict] = []
        for row in result.data:
            # Lazy backfill van email als nog leeg
            email = row.get("email")
            if not email and row.get("access_token"):
                try:
                    token = get_valid_token(row)
                    email = _fetch_gmail_address(token)
                    if email:
                        supabase.table("email_tokens").update({"email": email}).eq("id", row["id"]).execute()
                except RuntimeError as e:
                    logger.error("Failed to backfill Gmail address", exc_info=e)

            now = int(time.time())
            expires_at_epoch = _to_epoch_seconds(row.get("expires_at"))
            is_valid = expires_at_epoch > now if expires_at_epoch else True

            connected_accounts.append({
                "id": row["id"],
                "email": email or "gmail_account",
                "status": "connected" if is_valid else "expired",
                "connected_at": row.get("created_at"),
                "last_sync": row.get("last_sync_ts"),
                "subject_filter": row.get("subject_filter")
            })
        return {"connected": len(connected_accounts) > 0, "accounts": connected_accounts}
    except Exception as e:
        logger.exception("Failed to get Gmail status", exc_info=e)
        return JSONResponse(status_code=500, content={"error": "Failed to get Gmail status", "detail": str(e)})

@router.post("/oauth/gmail/settings")
def update_gmail_settings(settings: GmailSettingsUpdate):
    try:
        update_data = {"subject_filter": settings.subject_filter}
        query = (
            supabase
            .table("email_tokens")
            .update(update_data)
            .eq("user_id", settings.user_id)
            .eq("provider", "gmail")
        )
        if settings.token_id:
            query = query.eq("id", settings.token_id)
        result = query.execute()
        return {"updated": result.data}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": "Failed to update Gmail settings", "detail": str(e)})

@router.get("/oauth/gmail/callback")
def gmail_oauth_callback(code: str, state: str):
    try:
        token_endpoint = "https://oauth2.googleapis.com/token"
        data = {
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        }
        res = requests.post(token_endpoint, data=data, timeout=20)
        if res.status_code != 200:
            return RedirectResponse(url=f"{FRONTEND_URL}?gmail_connected=false")

        token_data = res.json()
        access_token = token_data["access_token"]

        email_addr = _fetch_gmail_address(access_token)

        expires_at = datetime.datetime.utcnow() + datetime.timedelta(
            seconds=token_data.get("expires_in", 3600)
        )
        supabase.table("email_tokens").insert(
            {
                "user_id": state,
                "provider": "gmail",
                "access_token": access_token,
                "refresh_token": token_data.get("refresh_token"),
                "expires_at": expires_at.isoformat(),
                "last_sync_ts": None,
                "email": email_addr,
            }
        ).execute()
        return RedirectResponse(url=f"{FRONTEND_URL}?gmail_connected=true")
    except requests.RequestException as e:
        logger.error("Gmail OAuth token exchange failed", exc_info=e)
    except Exception as e:
        logger.exception("Failed to store Gmail token", exc_info=e)
    return RedirectResponse(url=f"{FRONTEND_URL}?gmail_connected=false")

def fetch_new_message_ids_gmail(access_token: str, after_ts_ms: int | None, subject: str | None = None) -> list[str]:
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"maxResults": 50}
    q_parts = []
    if after_ts_ms:
        q_parts.append(f"after:{int(after_ts_ms/1000)}")
    if subject:
        subj = (subject or "").replace('"', r'\"')
        q_parts.append(f'subject:"{subj}"')
    if q_parts:
        params["q"] = " ".join(q_parts)

    r = requests.get(f"{GMAIL_API}/messages", headers=headers, params=params, timeout=20)
    if r.status_code == 200:
        return [m["id"] for m in r.json().get("messages", [])]
    return []

def fetch_message_gmail(access_token: str, msg_id: str) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.get(f"{GMAIL_API}/messages/{msg_id}", headers=headers, params={"format": "full"}, timeout=30)
    r.raise_for_status()
    return r.json()

# ---------- Gmail Poll ----------
@router.post("/gmail/poll")
def gmail_poll():
    try:
        rows = supabase.table("email_tokens").select("*").eq("provider", "gmail").execute().data
    except Exception as e:
        logger.exception("Failed to fetch Gmail tokens", exc_info=e)
        return JSONResponse(status_code=500, content={"error": "supabase_select_failed", "detail": str(e)})

    processed = 0
    for row in rows:
        user_id = row["user_id"]
        try:
            last_ts_ms = row.get("last_sync_ts")
            token = get_valid_token(row)  # kan RuntimeError gooien
        except RuntimeError as e:
            logger.error("Token refresh failed", exc_info=e)
            # kapotte/ontbrekende refresh_token of refresh-fout → sla account over
            continue

        subject_filter = row.get("subject_filter")
        msg_ids = fetch_new_message_ids_gmail(token, last_ts_ms, subject=subject_filter)
        max_internal_ms = last_ts_ms or 0

        for msg_id in msg_ids:
            if already_seen(user_id, msg_id):
                continue
            try:
                msg = fetch_message_gmail(token, msg_id)
                internal_ms = int(msg.get("internalDate", "0"))
                if internal_ms > max_internal_ms:
                    max_internal_ms = internal_ms
                push_to_n8n(user_id, msg, "gmail")
                mark_seen(user_id, msg_id)
                processed += 1
            except requests.RequestException as e:
                logger.error("Failed to fetch Gmail message", exc_info=e)

        if max_internal_ms:
            supabase.table("email_tokens").update({"last_sync_ts": max_internal_ms}).eq("id", row["id"]).execute()

    return {"status": "ok", "processed": processed}

# ---------- Gmail Attachment ----------
def _b64url_to_bytes(s: str) -> bytes:
    if not s:
        return b""
    pad = '=' * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode())

def _find_part_info(part: dict, attachment_id: str):
    if not part:
        return None, None
    body = part.get("body") or {}
    if body.get("attachmentId") == attachment_id:
        return part.get("filename") or "attachment", part.get("mimeType") or "application/octet-stream"
    for p in (part.get("parts") or []):
        fn, mt = _find_part_info(p, attachment_id)
        if fn or mt:
            return fn, mt
    return None, None

@router.get("/gmail/attachment")
def gmail_get_attachment(user_id: str, message_id: str, attachment_id: str):
    rows = supabase.table("email_tokens").select("*").eq("provider", "gmail").eq("user_id", user_id).execute().data
    if not rows:
        return JSONResponse(status_code=404, content={"error": "user not found"})
    row = rows[0]
    token = get_valid_token(row)
    headers = {"Authorization": f"Bearer {token}"}

    msg = requests.get(f"{GMAIL_API}/messages/{message_id}", headers=headers, params={"format": "full"}, timeout=30)
    if msg.status_code != 200:
        return JSONResponse(status_code=400, content={"error": "failed to fetch message", "detail": msg.text})
    filename, mimetype = _find_part_info((msg.json().get("payload") or {}), attachment_id)
    if not filename:
        filename = f"attachment-{attachment_id}"
    if not mimetype:
        mimetype = "application/octet-stream"

    r = requests.get(f"{GMAIL_API}/messages/{message_id}/attachments/{attachment_id}", headers=headers, timeout=30)
    if r.status_code != 200:
        return JSONResponse(status_code=400, content={"error": "failed to fetch attachment", "detail": r.text})
    payload = r.json()
    raw_bytes = _b64url_to_bytes(payload.get("data", ""))

    return StreamingResponse(io.BytesIO(raw_bytes), media_type=mimetype,
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})

# ---------- OUTLOOK / MICROSOFT GRAPH ----------
@router.post("/oauth/outlook/initiate")
def initiate_oauth_outlook(request: OAuthInitiateRequest):
    state = request.user_id
    params = {
        "client_id": MS_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": MS_REDIRECT_URI,
        "response_mode": "query",
        "scope": MS_SCOPES,
        "state": state,
    }
    url = AUTH_URL_MS + "?" + urllib.parse.urlencode(params)
    return JSONResponse(content={"auth_url": url})

@router.get("/oauth/outlook/status/{user_id}")
def get_outlook_status(user_id: str):
    try:
        result = supabase.table("email_tokens").select("*").eq("user_id", user_id).eq("provider", "outlook").execute()
        connected_accounts = []
        for row in result.data:
            now = int(time.time())
            expires_at_epoch = _to_epoch_seconds(row.get("expires_at"))
            is_valid = expires_at_epoch > now if expires_at_epoch else True
            connected_accounts.append({
                "id": row["id"],
                "email": row.get("email") or "outlook_account",
                "status": "connected" if is_valid else "expired",
                "connected_at": row.get("created_at"),
                "last_sync": row.get("last_sync_ts")
            })
        return {"connected": len(connected_accounts) > 0, "accounts": connected_accounts}
    except Exception as e:
        logger.exception("Failed to get Outlook status", exc_info=e)
        return JSONResponse(status_code=500, content={"error": "Failed to get Outlook status", "detail": str(e)})

@router.get("/oauth/outlook/callback")
def outlook_oauth_callback(code: str, state: str):
    try:
        data = {
            "client_id": MS_CLIENT_ID,
            "client_secret": MS_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": MS_REDIRECT_URI,
        }
        res = requests.post(TOKEN_URL_MS, data=data, timeout=30)
        if res.status_code != 200:
            return RedirectResponse(url=f"{FRONTEND_URL}?outlook_connected=false")

        token_data = res.json()
        expires_at = datetime.datetime.utcnow() + datetime.timedelta(
            seconds=token_data.get("expires_in", 3600)
        )
        supabase.table("email_tokens").insert(
            {
                "user_id": state,
                "provider": "outlook",
                "access_token": token_data["access_token"],
                "refresh_token": token_data.get("refresh_token"),
                "expires_at": expires_at.isoformat(),
                "last_sync_ts": None,
            }
        ).execute()
        return RedirectResponse(url=f"{FRONTEND_URL}?outlook_connected=true")
    except requests.RequestException as e:
        logger.error("Outlook OAuth token exchange failed", exc_info=e)
    except Exception as e:
        logger.exception("Failed to store Outlook token", exc_info=e)
    return RedirectResponse(url=f"{FRONTEND_URL}?outlook_connected=false")

def fetch_new_message_ids_outlook(access_token: str, after_ts_ms: int | None) -> list[str]:
    headers = {"Authorization": f"Bearer {access_token}"}
    base = f"{GRAPH_BASE}/me/mailFolders/Inbox/messages"
    params = {"$top": 50, "$select": "id,receivedDateTime", "$orderby": "receivedDateTime asc"}
    if after_ts_ms:
        iso = _epoch_ms_to_iso_utc(after_ts_ms)
        params["$filter"] = f"receivedDateTime gt {iso}"

    r = requests.get(base, headers=headers, params=params, timeout=30)
    if r.status_code != 200:
        return []
    items = r.json().get("value", [])
    return [m["id"] for m in items]

def fetch_message_outlook(access_token: str, msg_id: str) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"{GRAPH_BASE}/me/messages/{msg_id}"
    params = {"$expand": "attachments"}
    r = requests.get(url, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

@router.post("/outlook/poll")
def outlook_poll():
    rows = supabase.table("email_tokens").select("*").eq("provider", "outlook").execute().data
    processed = 0
    for row in rows:
        user_id = row["user_id"]
        last_ts_ms = row.get("last_sync_ts")
        try:
            token = get_valid_token(row)
        except RuntimeError as e:
            logger.error("Token refresh failed", exc_info=e)
            continue

        msg_ids = fetch_new_message_ids_outlook(token, last_ts_ms)
        max_recv_ms = last_ts_ms or 0
        for msg_id in msg_ids:
            if already_seen(user_id, msg_id):
                continue
            try:
                msg = fetch_message_outlook(token, msg_id)
                rdt = msg.get("receivedDateTime")
                recv_ms = int(
                    datetime.datetime.fromisoformat(rdt.replace("Z", "+00:00")).timestamp() * 1000
                ) if rdt else 0
                if recv_ms > max_recv_ms:
                    max_recv_ms = recv_ms
                push_to_n8n(user_id, msg, "outlook")
                mark_seen(user_id, msg_id)
                processed += 1
            except requests.RequestException as e:
                logger.error("Failed to fetch Outlook message", exc_info=e)
        if max_recv_ms:
            supabase.table("email_tokens").update({"last_sync_ts": max_recv_ms}).eq("id", row["id"]).execute()
    return {"status": "ok", "processed": processed}
