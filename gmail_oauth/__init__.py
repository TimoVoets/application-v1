from fastapi import APIRouter
from fastapi.responses import RedirectResponse, JSONResponse
import os, requests, datetime, time, re
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

router = APIRouter()

# === Config uit .env ===
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

# ---------- OAuth ----------
@router.get("/oauth/gmail/initiate")
def initiate_oauth(user_id: str):
    url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={GOOGLE_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=https://www.googleapis.com/auth/gmail.readonly"
        f"&access_type=offline"
        f"&state={user_id}"
        f"&prompt=consent"
    )
    return RedirectResponse(url)

@router.get("/oauth/gmail/callback")
def oauth_callback(code: str, state: str):
    token_endpoint = "https://oauth2.googleapis.com/token"
    data = {
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code"
    }
    res = requests.post(token_endpoint, data=data, timeout=20)
    if res.status_code != 200:
        return JSONResponse(status_code=400, content={"error": "Token exchange failed", "detail": res.text})

    token_data = res.json()
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=token_data["expires_in"])

    supabase.table("email_tokens").insert({
        "user_id": state,
        "provider": "gmail",
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token"),
        "expires_at": expires_at.isoformat(),
        "last_sync_ts": None
    }).execute()

    return JSONResponse(content={"message": "Gmail succesvol gekoppeld!"})

# ---------- Helpers ----------
def refresh_access_token(refresh_token: str) -> dict:
    data = {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    r = requests.post("https://oauth2.googleapis.com/token", data=data, timeout=20)
    r.raise_for_status()
    return r.json()

def _to_epoch_seconds(value) -> int:
    """Accepteert None / str / datetime en maakt er epoch-seconden van.
       Normaliseert fracties (bv .15062 -> .150620)."""
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
                frac = (frac + "000000")[:6]  # pad/trim naar 6
                s = f"{base}.{frac}{tz}"
        dt = datetime.datetime.fromisoformat(s)
        return int(dt.timestamp())
    return 0

def get_valid_token(row):
    now = int(time.time())
    expires_at_epoch = _to_epoch_seconds(row.get("expires_at"))
    access_token = row["access_token"]
    if expires_at_epoch and now >= (expires_at_epoch - 60):
        rt = row.get("refresh_token")
        if not rt:
            return access_token
        new = refresh_access_token(rt)
        new_expires = datetime.datetime.utcnow() + datetime.timedelta(seconds=new["expires_in"])
        supabase.table("email_tokens").update({
            "access_token": new["access_token"],
            "expires_at": new_expires.isoformat()
        }).eq("id", row["id"]).execute()
        return new["access_token"]
    return access_token

def fetch_new_message_ids(access_token: str, after_ts_ms: int | None) -> list[str]:
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"maxResults": 50}
    if after_ts_ms:
        params["q"] = f"after:{int(after_ts_ms/1000)}"  # Gmail zoekt in seconden
    r = requests.get(f"{GMAIL_API}/messages", headers=headers, params=params, timeout=20)
    if r.status_code == 200:
        return [m["id"] for m in r.json().get("messages", [])]
    return []

def fetch_message(access_token: str, msg_id: str) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.get(f"{GMAIL_API}/messages/{msg_id}", headers=headers, params={"format": "full"}, timeout=30)
    r.raise_for_status()
    return r.json()

def already_seen(user_id: str, msg_id: str) -> bool:
    res = supabase.table("email_seen").select("message_id").eq("user_id", user_id).eq("message_id", msg_id).execute()
    return len(res.data) > 0

def mark_seen(user_id: str, msg_id: str):
    supabase.table("email_seen").insert({"user_id": user_id, "message_id": msg_id}).execute()

def push_to_n8n(user_id: str, message: dict):
    if not N8N_WEBHOOK_URL:
        return
    requests.post(N8N_WEBHOOK_URL, json={"user_id": user_id, "message": message}, timeout=20)

# ---------- Multi-tenant poll ----------
@router.post("/gmail/poll")
def gmail_poll():
    rows = supabase.table("email_tokens").select("*").eq("provider", "gmail").execute().data
    processed = 0

    for row in rows:
        user_id = row["user_id"]
        last_ts_ms = row.get("last_sync_ts")  # kan None zijn
        token = get_valid_token(row)
        msg_ids = fetch_new_message_ids(token, last_ts_ms)

        max_internal_ms = last_ts_ms or 0
        for msg_id in msg_ids:
            if already_seen(user_id, msg_id):
                continue
            try:
                msg = fetch_message(token, msg_id)
                internal_ms = int(msg.get("internalDate", "0"))  # ms sinds epoch
                if internal_ms > max_internal_ms:
                    max_internal_ms = internal_ms
                push_to_n8n(user_id, msg)
                mark_seen(user_id, msg_id)
                processed += 1
            except Exception:
                # optioneel: logging
                pass

        if max_internal_ms:
            supabase.table("email_tokens").update({"last_sync_ts": max_internal_ms}).eq("id", row["id"]).execute()

    return {"status": "ok", "processed": processed}

# ---------- Attachment endpoint ----------
@router.get("/gmail/attachment")
def gmail_get_attachment(user_id: str, message_id: str, attachment_id: str):
    """
    Haalt een attachment op via Gmail API en retourneert base64url content + size.
    Multi-tenant: user_id bepaalt welke tokens worden gebruikt.
    """
    rows = supabase.table("email_tokens").select("*").eq("provider", "gmail").eq("user_id", user_id).execute().data
    if not rows:
        return JSONResponse(status_code=404, content={"error": "user not found"})
    row = rows[0]

    token = get_valid_token(row)
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{GMAIL_API}/messages/{message_id}/attachments/{attachment_id}"
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code != 200:
        return JSONResponse(status_code=400, content={"error": "failed to fetch attachment", "detail": r.text})

    payload = r.json()  # {"size": ..., "data": "<base64url>"}
    return JSONResponse(content={
        "data": payload.get("data"),
        "size": payload.get("size")
    })
