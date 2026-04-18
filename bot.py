"""
Telegram File Uploader Bot — Always GDrive -> FilePress (minimal metadata)
Features:
- Upload replied/sent file to Google Drive (user must connect via /connect_gdrive)
- Call FilePress metadata API /api/v1/file/add with {"key": <api_key>, "id": <gdrive_file_id>}
- Build direct FilePress link {domain}/file/{_id} when provider returns an id
- Save provider responses to MongoDB for debugging
- Shortener management per-user, shorten links automatically for results
- Account management (/accounts) shows GDrive, FilePress and Shortener details

This whole repo is coded by ThiruXD - thiruxd.is-a.dev
"""

import re
import os
import io
import sys
import json
import math
import time
import asyncio
import tempfile
import traceback
import datetime
import urllib.parse
from typing import Optional, Any

from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
import motor.motor_asyncio
from bson import ObjectId
from dotenv import load_dotenv

# Google / Drive libs (optional runtime)
try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
except Exception:
    # If Google libs not installed, the bot will still run but GDrive features will fail at runtime
    Credentials = None
    Flow = None
    build = None
    MediaFileUpload = None

import aiohttp

load_dotenv()

# ----------------- CONFIG -----------------
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
MONGO_URI = os.getenv("MONGO_URI")
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_SESSION = os.getenv("BOT_SESSION", "upload_bot")
FILEPRESS_UPLOAD_URL = os.getenv(
    "FILEPRESS_UPLOAD_URL", "https://api.filebee.xyz/api/v1/file/add"
)

# Bot Start Items
WELCOME_PHOTO = os.getenv("WELCOME_PHOTO", "https://i.ibb.co/GfqkHqBF/x.jpg") 
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/ThiruEmpire")
GROUP_URL = os.getenv("GROUP_URL", "https://t.me/AgoraNet_Chat")
DEVELOPER_URL = os.getenv("DEVELOPER_URL", "https://t.me/ThiruXD")

UPLOADS_PAGE_SIZE = 10  # page size (10 links per page)
URL_RE = re.compile(r"https?://[^\s\"']+")

GDRIVE_CLIENT_CONFIG = os.getenv("GDRIVE_CLIENT_CONFIG_JSON")
GDRIVE_CLIENT_CONFIG_PATH = os.getenv("GDRIVE_CLIENT_CONFIG_PATH")
GDRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

DB_NAME = os.getenv("DB_NAME", "telegram_filebot")
COL_USERS = "users"
COL_UPLOADS = "uploads"
# ------------------------------------------

# Minimal checks
if not MONGO_URI:
    raise RuntimeError("Please set MONGO_URI in environment")
if not BOT_TOKEN:
    raise RuntimeError("Please set BOT_TOKEN in environment")
if API_ID is None or API_HASH is None:
    raise RuntimeError(
        "Please set API_ID and API_HASH environment variables from https://my.telegram.org"
    )

# ----------------- DB ---------------------
mongo = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = mongo[DB_NAME]
users_col = db[COL_USERS]
uploads_col = db[COL_UPLOADS]

# ----------------- BOT --------------------
app = Client(
    BOT_SESSION,
    api_id=int(API_ID),
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# ----------------- HELPERS -----------------


async def save_gdrive_tokens(user_id: int, creds: dict):
    await users_col.update_one({"_id": user_id}, {"$set": {"gdrive": creds}}, upsert=True)


async def get_gdrive_tokens(user_id: int) -> Optional[dict]:
    doc = await users_col.find_one({"_id": user_id})
    return doc.get("gdrive") if doc else None


async def save_filepress_api_key(user_id: int, api_key: str):
    await users_col.update_one(
        {"_id": user_id}, {"$set": {"filepress_api_key": api_key}}, upsert=True
    )


async def save_filepress_url(user_id: int, fp_url: str):
    await users_col.update_one(
        {"_id": user_id}, {"$set": {"filepress_url": fp_url}}, upsert=True
    )


async def get_filepress_api_key(user_id: int) -> Optional[str]:
    doc = await users_col.find_one({"_id": user_id})
    return doc.get("filepress_api_key") if doc else None


async def get_filepress_url(user_id: int) -> Optional[str]:
    doc = await users_col.find_one({"_id": user_id})
    return doc.get("filepress_url") if doc else None


def load_client_config():
    if GDRIVE_CLIENT_CONFIG_PATH and os.path.isfile(GDRIVE_CLIENT_CONFIG_PATH):
        with open(GDRIVE_CLIENT_CONFIG_PATH, "r") as f:
            return json.load(f)
    if GDRIVE_CLIENT_CONFIG:
        return json.loads(GDRIVE_CLIENT_CONFIG)
    raise RuntimeError(
        "Provide Google OAuth client config in GDRIVE_CLIENT_CONFIG_JSON or path"
    )


async def build_gdrive_service_from_saved(user_id: int):
    tokens = await get_gdrive_tokens(user_id)
    if not tokens:
        return None
    if Credentials is None or build is None:
        raise RuntimeError("Google API libraries are not installed.")
    creds = Credentials(**tokens)
    return build("drive", "v3", credentials=creds)


async def edit_progress(msg: Message, text: str):
    try:
        await msg.edit_text(text)
    except Exception as e:
        print(f"ERROR (edit_progress): {e}")


def human_size(n: int) -> str:
    try:
        n = int(n or 0)
    except Exception:
        return "0B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.2f}{unit}"
        n /= 1024
    return f"{n:.2f}PB"


def _truncate(s: str, maxlen=40):
    if not s:
        return "unknown"
    s = str(s)
    return s if len(s) <= maxlen else s[: maxlen - 2] + "…"


def _find_service_link(results: list, service_name: str):
    if not results:
        return None
    for r in results:
        if str(r.get("service")).lower() == service_name.lower():
            return r.get("link")
    return None


# ----------------- Shortener helpers -----------------


async def _find_url_in_obj(obj: Any) -> Optional[str]:
    """Recursively search for the first http(s) URL in a JSON-like object."""
    if obj is None:
        return None
    if isinstance(obj, str):
        m = URL_RE.search(obj)
        return m.group(0) if m else None
    if isinstance(obj, dict):
        # common keys first
        for key in ("shortenedUrl", "short_url", "short", "tiny_url", "url", "data", "result"):
            if key in obj:
                val = obj[key]
                if isinstance(val, str):
                    m = URL_RE.search(val)
                    if m:
                        return m.group(0)
                else:
                    found = await _find_url_in_obj(val)
                    if found:
                        return found
        for v in obj.values():
            found = await _find_url_in_obj(v)
            if found:
                return found
    if isinstance(obj, (list, tuple)):
        for v in obj:
            found = await _find_url_in_obj(v)
            if found:
                return found
    return None


async def shorten_user_link(user_id: int, original_url: str, alias: str = None, timeout: int = 15) -> Optional[str]:
    """
    Shorten original_url using the shortener settings stored for user_id.
    Returns shortened URL string on success, or None on failure.
    """
    if not original_url:
        return None

    doc = await users_col.find_one({"_id": user_id}) or {}
    short = doc.get("shortener")
    if not short:
        return None

    host = short.get("host")
    api_key = short.get("api_key")
    if not host or not api_key:
        return None

    try:
        params = {"api": api_key, "url": original_url}
        if alias:
            params["alias"] = alias
        qs = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        req_url = f"https://{host}/api?{qs}"
    except Exception:
        return None

    try:
        timeout_cfg = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_cfg) as sess:
            async with sess.get(req_url) as resp:
                text = await resp.text()
                status = resp.status

                short_url = None
                try:
                    j = await resp.json(content_type=None)
                    short_url = await _find_url_in_obj(j)
                except Exception:
                    j = None

                if not short_url:
                    m = URL_RE.search(text or "")
                    if m:
                        short_url = m.group(0)

                if not short_url and status == 200 and text:
                    return text.strip()

                return short_url
    except Exception:
        return None


# ----------------- GDrive / FilePress / Upload flow -----------------


@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client, message: Message):
    """
    Start message with buttons:
    - Help (callback -> shows help)
    - Channel (url)
    - Group (url)
    - Developer (url)
    """
    user = message.from_user
    name = (user.first_name or "") + (f" {user.last_name}" if user.last_name else "")
    text = (
        f"👋 Hello <b>{name}</b>!\n\n"
        "I'm your upload assistant — I upload files to Google Drive, register them in FilePress, "
        "and can shorten links for you.\n\n"
        "Press <b>Help</b> to see all commands and how to use them."
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📚 Help", callback_data="show_help"),
            ],
            [
                InlineKeyboardButton("📢 Channel", url=CHANNEL_URL),
                InlineKeyboardButton("👥 Group", url=GROUP_URL),
            ],
            [
                InlineKeyboardButton("👨‍💻 Developer", url=DEVELOPER_URL),
            ],
        ]
    )

    # If a welcome photo URL is provided, send as photo with caption; otherwise send text
    try:
        if WELCOME_PHOTO:
            # Try to send photo (if the chat allows). If the bot cannot send photo, fall back to text.
            await message.reply_photo(WELCOME_PHOTO, caption=text, reply_markup=keyboard)
        else:
            await message.reply_text(text, reply_markup=keyboard)
    except Exception:
        # fallback to text-only reply
        await message.reply_text(text, reply_markup=keyboard)


HELP_TEXT = """
<b>Bot Help — Commands & Usage</b>

<b>Google Drive</b>
• /connect_gdrive (reply to client JSON) — Upload your Google OAuth client JSON, reply to it to start auth flow.
• /gdrive_auth &lt;code&gt; — Paste the code shown by Google (or use the automatic loopback flow).
• /check — Basic test: checks GDrive credentials & lists a file (debug).

<b>FilePress</b>
• /connect_filepress &lt;API_KEY&gt; — Save your FilePress API key.
• /filepress_url &lt;DOMAIN&gt; — Save FilePress domain (example: api.myfp.com) — used to build friendly links.

<b>Uploads</b>
• Send a file and use /upload (or reply /upload to a file) — Upload to Google Drive and register in FilePress (if configured).
• /myuploads — Shows a paged list of your uploads (10 per page). Click a filename to view details, open GDrive/FilePress links and see shortened links.
• /clear_uploads — Remove ALL your uploads (confirmation).  
• /clear_uploads &lt;N&gt; — Remove last N uploads only.

<b>Shortener</b>
• /shortener_set &lt;host&gt; &lt;api_key&gt; — Configure your custom shortener (host without https:// and API key).
  Example: /shortener_set short.example.com MYAPIKEY
• /shortener_view — View current shortener configuration.
• /shortener_remove — Remove stored shortener configuration.
• /shorten &lt;url&gt; [alias] — Shorten a URL using your configured shortener.

<b>Account Management</b>
• /accounts — View connected accounts (GDrive, FilePress, Shortener). Buttons allow viewing/removing credentials.

<b>Admin / Debug</b>
• /eval &lt;code&gt; — Run code (admin only, set ADMIN_ID in env).
• /check — Quick GDrive connectivity test.

<b>Notes & Tips</b>
• For Google OAuth use the loopback/localhost redirect (recommended) or host a HTTPS callback. OOB (urn:ietf:wg:oauth:2.0:oob) is deprecated.
• When uploading, the bot will attempt to add minimal metadata to FilePress: { "key": <api_key>, "id": <gdrive_file_id> }.
• If Drive says "processing" for playback, you may need to transcode to mp4 (ffmpeg) or wait for Drive to finish processing.

If you need any specific feature added (auto-transcode, auto-shortening toggle, export account JSON, revoke tokens), reply here or contact the developer.
"""

# show help via command
@app.on_message(filters.command("help") & filters.private)
async def help_cmd(client, message: Message):
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⬅️ Back", callback_data="home")],
            [InlineKeyboardButton("Channel", url=CHANNEL_URL), InlineKeyboardButton("Group", url=GROUP_URL)],
        ]
    )
    # If message too long for edit, send as a document
    if len(HELP_TEXT) > 4000:
        bio = io.BytesIO(HELP_TEXT.encode("utf-8"))
        bio.name = "help.txt"
        await message.reply_document(bio, caption="Bot Help", reply_markup=keyboard)
    else:
        await message.reply_text(HELP_TEXT, reply_markup=keyboard)


# callback query handlers for Help/Home button(s)
@app.on_callback_query(filters.regex(r"^(show_help|home)$"))
async def help_callback(client, cq: CallbackQuery):
    data = cq.data
    await cq.answer()
    if data == "show_help":
        # show help by editing the start message
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="home")]])
        try:
            if len(HELP_TEXT) > 4000:
                # reply as document and delete the original message
                bio = io.BytesIO(HELP_TEXT.encode("utf-8"))
                bio.name = "help.txt"
                await cq.message.reply_document(bio, caption="Bot Help")
                try:
                    await cq.message.delete()
                except Exception:
                    pass
            else:
                await cq.message.edit_text(HELP_TEXT, reply_markup=keyboard)
        except Exception:
            # fallback: send help as a new message
            await cq.message.reply_text(HELP_TEXT, reply_markup=keyboard)
        return

    if data == "home":
        # re-render start message to the user by calling start_cmd handler logic
        # create a lightweight start text and buttons (no photo)
        user = cq.from_user
        name = (user.first_name or "") + (f" {user.last_name}" if user.last_name else "")
        start_text = (
            f"👋 Hello <b>{name}</b>!\n\n"
            "I'm your upload assistant — I upload files to Google Drive, register them in FilePress, "
            "and can shorten links for you.\n\n"
            "Press <b>Help</b> to see all commands and how to use them."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📚 Help", callback_data="show_help")],
                [InlineKeyboardButton("📢 Channel", url=CHANNEL_URL), InlineKeyboardButton("👥 Group", url=GROUP_URL)],
                [InlineKeyboardButton("👨‍💻 Developer", url=DEVELOPER_URL)],
            ]
        )
        try:
            await cq.message.edit_text(start_text, reply_markup=keyboard)
        except Exception:
            try:
                await cq.message.reply_text(start_text, reply_markup=keyboard)
            except Exception:
                pass
        return
# --- end of Start / Help UI ---


@app.on_message(filters.command("connect_gdrive") & filters.private)
async def connect_gdrive_cmd(client, message):
    # User must reply to a JSON file
    if not message.reply_to_message or not message.reply_to_message.document:
        return await message.reply(
            "Please reply to your Google OAuth JSON file with:\n`/connect_gdrive`\n\nUpload `client_secret.json` then reply to it."
        )

    doc = message.reply_to_message.document
    if not doc.file_name.lower().endswith(".json"):
        return await message.reply("❌ Please reply to a **.json** file only.")

    tmp = await client.download_media(doc)
    if not tmp:
        return await message.reply("❌ Failed to download JSON file.")

    try:
        with open(tmp, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        return await message.reply(f"❌ Invalid JSON file:\n`{e}`")

    # Build OAuth URL (loopback suggested)
    try:
        flow = Flow.from_client_config(
            config, scopes=GDRIVE_SCOPES, redirect_uri="http://localhost:8080/"
        )
        auth_url, _ = flow.authorization_url(
            access_type="offline", include_granted_scopes="true", prompt="consent"
        )
    except Exception as e:
        return await message.reply(f"❌ Error generating OAuth URL:\n`{e}`")

    # Save config to DB for later use
    await users_col.update_one({"_id": message.from_user.id}, {"$set": {"gdrive_client_config": config}}, upsert=True)

    text = (
        "✅ Google Drive client config loaded successfully!\n\n"
        "Next step:\n"
        f"1. Open this link and authorize:\n{auth_url}\n\n"
        "2. Copy the verification code shown in the browser\n"
        "3. Send it here as: /gdrive_auth <code>"
    )
    code_pic = "https://i.ibb.co/LhHfNg9L/x.jpg"
    await message.reply_photo(code_pic, caption=text)


@app.on_message(filters.command("gdrive_auth") & filters.private)
async def gdrive_auth_cmd(client, message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Usage: /gdrive_auth <AUTH_CODE>")

    code = parts[1].strip()
    user_data = await users_col.find_one({"_id": message.from_user.id})
    if not user_data or "gdrive_client_config" not in user_data:
        return await message.reply("❌ You must first upload your JSON and run `/connect_gdrive`.")

    config = user_data["gdrive_client_config"]

    try:
        flow = Flow.from_client_config(config, scopes=GDRIVE_SCOPES, redirect_uri="http://localhost:8080/")
        flow.fetch_token(code=code)
        creds = flow.credentials

        creds_dict = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes,
        }

        await save_gdrive_tokens(message.from_user.id, creds_dict)
        await message.reply("✅ Google Drive connected and tokens saved!")
    except Exception as e:
        await message.reply(f"❌ Error:\n`{e}`")


@app.on_message(filters.command("check"))
async def check_cmd(client, message):
    user = await users_col.find_one({"_id": message.from_user.id})
    g = user.get("gdrive") if user else None
    check = "<b>System Checking Your GDrive Credentials:</b>\n"
    txt_msg = await message.reply(check)
    check += f"• Gdrive record: {bool(g)} ✅\n"
    await txt_msg.edit(check)
    if g and Credentials:
        try:
            creds = Credentials(**g)
            check += f"• Valid: {getattr(creds, 'valid', None)} | Expired: {getattr(creds, 'expired', None)} ✅\n"
            await txt_msg.edit(check)
            svc = build("drive", "v3", credentials=creds)
            check += "• Service created ok ✅\n"
            await txt_msg.edit(check)
            res = svc.files().list(pageSize=1).execute()
            check += "• files.list Responded, ok ✅\n"
            await txt_msg.edit(check)
        except Exception as e:
            check += f"❌ Service/Error: {e}"
            await txt_msg.edit(check)


@app.on_message(filters.command("filepress_url") & filters.private)
async def filepress_url_cmd(client, message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Usage: /filepress_url <DOMAIN>  (without https:// and trailing slash)")
    fp_url = parts[1].strip()
    await save_filepress_url(message.from_user.id, fp_url)
    await message.reply("✅ FilePress URL saved!")


@app.on_message(filters.command("connect_filepress") & filters.private)
async def connect_filepress_cmd(client, message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /connect_filepress <API_KEY>")
        return
    api_key = parts[1].strip()
    await save_filepress_api_key(message.from_user.id, api_key)
    await message.reply("✅ FilePress API key saved!")


@app.on_message(filters.command("upload") & (filters.private | filters.reply))
async def upload_cmd(client, message):
    target = None
    if message.reply_to_message:
        target = message.reply_to_message
    elif message.media:
        target = message
    else:
        await message.reply("Reply to a message containing a file, or send a file and use /upload.")
        return

    user_id = message.from_user.id
    status_msg = await message.reply("Starting download...")

    # download file to temp
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp_path = tmp.name
    tmp.close()

    # Better filename detection: prefer original file name from Telegram objects; infer extension from mime_type if missing.
    orig_name = None
    try:
        if hasattr(target, "document") and getattr(target.document, "file_name", None):
            orig_name = target.document.file_name
        elif hasattr(target, "video") and getattr(target.video, "file_name", None):
            orig_name = target.video.file_name
        elif hasattr(target, "audio") and getattr(target.audio, "file_name", None):
            orig_name = target.audio.file_name
        elif getattr(target, "file_name", None):
            orig_name = target.file_name
    except Exception:
        orig_name = None

    msg_mime = None
    try:
        if hasattr(target, "mime_type") and target.mime_type:
            msg_mime = target.mime_type
        elif hasattr(target, "document") and getattr(target.document, "mime_type", None):
            msg_mime = target.document.mime_type
        elif hasattr(target, "video") and getattr(target.video, "mime_type", None):
            msg_mime = target.video.mime_type
    except Exception:
        msg_mime = None

    import mimetypes, os

    if orig_name:
        file_name = orig_name
    else:
        file_name = f"tg_file_{getattr(target, 'id', 'unknown')}"

    if "." not in file_name and msg_mime:
        ext = mimetypes.guess_extension(msg_mime)
        if ext:
            file_name = file_name + ext

    file_name = os.path.basename(file_name)

    async def download_progress(current, total):
        percent = (current / total * 100) if total else 0
        await edit_progress(
            status_msg,
            f"Downloading: {file_name}\n{percent:.1f}% — {human_size(current)} / {human_size(total or 0)}",
        )

    try:
        await client.download_media(target, file_name=tmp_path, progress=download_progress)
    except Exception:
        try:
            await client.download_media(target, file_name=tmp_path)
        except Exception as e:
            await status_msg.edit_text(f"Failed to download: {e}")
            return

    file_size = os.path.getsize(tmp_path)
    await status_msg.edit_text(f"Downloaded {file_name} — {human_size(file_size)}\nUploading to Google Drive...")

    results = []

    # Upload to Google Drive
    gdrive_service = await build_gdrive_service_from_saved(user_id)
    if not gdrive_service:
        await status_msg.edit_text(
            "Google Drive not connected for your account. Please /connect_gdrive and try again."
        )
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return

    try:
        media = MediaFileUpload(tmp_path, resumable=True)
        request = gdrive_service.files().create(
            body={"name": file_name}, media_body=media, fields="id,webViewLink,webContentLink"
        )
        response = None
        done = False
        while not done:
            status, response = request.next_chunk()
            if status:
                percent = int(status.progress() * 100)
                await edit_progress(
                    status_msg,
                    f"Uploading to GDrive: {percent}% — {human_size(int(status.resumable_progress))}/{human_size(file_size)}",
                )
            if response:
                done = True

        file_id = response.get("id")
        try:
            gdrive_service.permissions().create(
                fileId=file_id, body={"role": "reader", "type": "anyone"}
            ).execute()
        except Exception:
            pass

        gdrive_link = response.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
        results.append({"service": "gdrive", "link": gdrive_link})
        await edit_progress(status_msg, f"Uploaded to GDrive: {gdrive_link}")
    except Exception as e:
        await edit_progress(status_msg, f"GDrive upload failed: {e}\n{traceback.format_exc()}")
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return

    # FilePress metadata
    api_key = await get_filepress_api_key(user_id)
    fp_url = await get_filepress_url(user_id)
    if not api_key:
        await edit_progress(
            status_msg,
            "FilePress API key not set. Use /connect_filepress <API_KEY> to enable FilePress metadata add.",
        )
        try:
            await uploads_col.insert_one(
                {
                    "user_id": user_id,
                    "file_name": file_name,
                    "size": file_size,
                    "results": results,
                    "timestamp": datetime.datetime.utcnow(),
                }
            )
        except Exception:
            pass
        await status_msg.edit_text("Done. Results:\n" + "\n".join([f" - {r['service']}: {r['link']}" for r in results]))
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return

    payload = {"key": api_key, "id": file_id}
    headers = {"Content-Type": "application/json"}
    try:
        await edit_progress(status_msg, "Sending metadata to FilePress...")
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            resp = await sess.post(FILEPRESS_UPLOAD_URL, json=payload, headers=headers)
            text = await resp.text()
            status = resp.status
            try:
                j = await resp.json(content_type=None)
            except Exception:
                j = None

        # Save provider response for debugging (into uploads document later)
        try:
            await uploads_col.update_one(
                {"user_id": user_id, "file_name": file_name},
                {"$set": {"last_filepress_response": {"status": status, "json": j, "text": text}}},
                upsert=False,
            )
        except Exception:
            pass

        if status in (200, 201, 202):
            filepress_id = None
            link = None
            if isinstance(j, dict):
                if "data" in j and isinstance(j["data"], dict):
                    for fid_key in ("_id", "file_id", "id"):
                        if fid_key in j["data"]:
                            filepress_id = j["data"][fid_key]
                            break
                if not filepress_id:
                    for fid_key in ("_id", "file_id", "id"):
                        if fid_key in j:
                            filepress_id = j[fid_key]
                            break
                for key in ("url", "link", "file_url", "download_url"):
                    if key in j:
                        link = j[key]
                        break
            if not link and filepress_id:
                try:
                    parsed = urllib.parse.urlparse(FILEPRESS_UPLOAD_URL)
                    base = f"{parsed.scheme}://{parsed.netloc}"
                    link = f"{base}/file/{filepress_id}"
                except Exception:
                    link = str(filepress_id)
            if not link:
                link = (text or "").strip() or None

            # Build final FilePress link using stored filepress domain (if any)
            fp_domain = (fp_url or "").strip().rstrip("/")
            if fp_domain and filepress_id:
                link_fp = f"https://{fp_domain}/file/{filepress_id}"
            else:
                link_fp = link

            results.append({"service": "filepress", "link": str(link_fp)})
            await edit_progress(status_msg, f"FilePress metadata added: {link_fp}")
        else:
            body_lower = (text or "").lower()
            if "file not found" in body_lower:
                await edit_progress(
                    status_msg,
                    "FilePress responded 'File Not Found' — the provider expects a Google Drive file id.",
                )
            else:
                await edit_progress(status_msg, f"FilePress returned {status}. Response: {text[:800]}")
    except Exception as e:
        await edit_progress(status_msg, f"FilePress request failed: {e}\n{traceback.format_exc()}")

    # Save record in DB
    try:
        await uploads_col.insert_one(
            {
                "user_id": user_id,
                "file_name": file_name,
                "size": file_size,
                "results": results,
                "timestamp": datetime.datetime.utcnow(),
            }
        )
    except Exception:
        pass

    # Final summary with shortened links (if shortener configured)
    summary_lines = []
    for r in results:
        orginal_link = r.get("link")
        short_url = await shorten_user_link(user_id, orginal_link, alias=None)
        summary_lines.append(f" - {r.get('service')}: {orginal_link}\n   Short: {short_url or '—'}\n")

    await status_msg.edit_text("Done. Results:\n" + "\n".join(summary_lines))

    # cleanup
    try:
        os.remove(tmp_path)
    except Exception:
        pass


# ----------------- Uploads listing (myuploads) -----------------


@app.on_message(filters.command("myuploads") & filters.private)
async def myuploads_cmd(client, message):
    """Show first page (buttons with file names only)."""
    page = 0
    await _render_uploads_list(message, page)


async def _render_uploads_list(message_or_cb, page: int):
    """
    Renders the list view: each item is a single Inline button with the file name.
    message_or_cb can be a Message (initial) or a CallbackQuery (when paginating).
    """
    if isinstance(message_or_cb, CallbackQuery):
        uid = message_or_cb.from_user.id
    else:
        uid = message_or_cb.from_user.id

    total = await uploads_col.count_documents({"user_id": uid})
    if total == 0:
        if isinstance(message_or_cb, CallbackQuery):
            await message_or_cb.answer("No uploads yet.", show_alert=True)
        else:
            await message_or_cb.reply_text("No uploads yet.")
        return

    total_pages = math.ceil(total / UPLOADS_PAGE_SIZE)
    page = max(0, min(page, max(0, total_pages - 1)))
    skip = page * UPLOADS_PAGE_SIZE

    cursor = uploads_col.find({"user_id": uid}).sort("timestamp", -1).skip(skip).limit(UPLOADS_PAGE_SIZE)
    items = await cursor.to_list(length=UPLOADS_PAGE_SIZE)

    header = f"Your uploads — page {page+1}/{total_pages} (showing {len(items)} of {total})\n\n"
    kb_rows = []
    for it in items:
        fname = it.get("file_name", "unknown")
        item_id = str(it.get("_id"))
        kb_rows.append([InlineKeyboardButton(_truncate(fname, 50), callback_data=f"uploads:view:{item_id}:{page}")])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"uploads:page:{page-1}"))
    nav_row.append(InlineKeyboardButton("Close", callback_data="uploads:close"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"uploads:page:{page+1}"))
    kb_rows.append(nav_row)

    keyboard = InlineKeyboardMarkup(kb_rows)
    text = header + "Select an item to view details."

    if isinstance(message_or_cb, CallbackQuery):
        try:
            await message_or_cb.message.edit_text(text, reply_markup=keyboard)
            await message_or_cb.answer()
        except Exception:
            await message_or_cb.message.reply_text(text, reply_markup=keyboard)
    else:
        await message_or_cb.reply_text(text, reply_markup=keyboard)


@app.on_callback_query(filters.regex(r"^uploads:"))
async def uploads_callback(client, cq: CallbackQuery):
    data = cq.data
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else None

    # close
    if action == "close":
        try:
            await cq.message.delete()
        except Exception:
            await cq.answer("Closed.", show_alert=False)
        return

    # pagination
    if action == "page":
        try:
            page = int(parts[2])
        except Exception:
            page = 0
        await _render_uploads_list(cq, page)
        return

    # view details
    if action == "view":
        if len(parts) < 4:
            await cq.answer("Invalid callback.", show_alert=True)
            return
        item_id = parts[2]
        try:
            page = int(parts[3])
        except Exception:
            page = 0

        try:
            _id = ObjectId(item_id)
        except Exception:
            await cq.answer("Invalid id.", show_alert=True)
            return

        item = await uploads_col.find_one({"_id": _id})
        if not item:
            await cq.answer("Upload not found.", show_alert=True)
            return

        fname = item.get("file_name", "unknown")
        size = human_size(item.get("size", 0))
        ts = item.get("timestamp")
        ts_text = ts.strftime("%Y-%m-%d %H:%M:%S UTC") if isinstance(ts, datetime.datetime) else str(ts)

        results = item.get("results", []) or []
        gdrive_link = _find_service_link(results, "gdrive")
        filepress_link = _find_service_link(results, "filepress")

        detail_text = f"<b>{fname}</b>\n\nSize: {size}\nUploaded: {ts_text}\n\nServices:\n"
        for r in results:
            svc = r.get("service")
            link = r.get("link")
            detail_text += f"• {svc}: {link}\n"

        gdrive_link_short = await shorten_user_link(cq.from_user.id, gdrive_link, alias=None)
        filepress_link_short = await shorten_user_link(cq.from_user.id, filepress_link, alias=None)

        detail_text += f"\nShorten Links:\n- Gdrive Shorten: {gdrive_link_short or '—'}\n- Filepress Shorten: {filepress_link_short or '—'}"

        kb = []
        row = []
        if gdrive_link:
            row.append(InlineKeyboardButton("Open GDrive", url=str(gdrive_link)))
        if filepress_link:
            row.append(InlineKeyboardButton("Open FilePress", url=str(filepress_link)))
        if row:
            kb.append(row)

        kb.append([InlineKeyboardButton("⬅️ Back to list", callback_data=f"uploads:page:{page}")])

        try:
            await cq.message.edit_text(detail_text, reply_markup=InlineKeyboardMarkup(kb))
            await cq.answer()
        except Exception:
            await cq.message.reply_text(detail_text, reply_markup=InlineKeyboardMarkup(kb))
        return


# ----------------- Clear uploads -----------------


@app.on_message(filters.command("clear_uploads") & filters.private)
async def clear_uploads_cmd(client, message):
    """
    /clear_uploads        → ask confirmation to delete ALL uploads
    /clear_uploads <n>    → ask confirmation to delete last n uploads
    """
    args = message.text.split()
    uid = message.from_user.id
    delete_count = None

    if len(args) == 2:
        try:
            delete_count = int(args[1])
            if delete_count <= 0:
                return await message.reply("❌ Count must be a positive number.")
        except:
            return await message.reply("Usage:\n/clear_uploads\n/clear_uploads <count>")

    if delete_count:
        text = f"Are you sure you want to delete your **last {delete_count} uploads**?"
        callback_data = f"dodel:last:{delete_count}"
    else:
        text = "Are you sure you want to delete **ALL** your upload history?"
        callback_data = "dodel:all"

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("❌ Cancel", callback_data="dodel:cancel"),
                InlineKeyboardButton("✅ Confirm", callback_data=callback_data),
            ]
        ]
    )
    await message.reply(text, reply_markup=kb)


@app.on_callback_query(filters.regex(r"^dodel:"))
async def delete_uploads_callback(client, cq: CallbackQuery):
    data = cq.data
    uid = cq.from_user.id

    if data == "dodel:cancel":
        await cq.answer("Cancelled.", show_alert=False)
        try:
            await cq.message.delete()
        except:
            pass
        return

    if data == "dodel:all":
        result = await uploads_col.delete_many({"user_id": uid})
        await cq.answer("Deleted.", show_alert=False)
        try:
            await cq.message.edit_text(f"🗑️ Deleted **{result.deleted_count} uploads** from history.")
        except:
            pass
        return

    if data.startswith("dodel:last:"):
        try:
            n = int(data.split(":")[2])
        except:
            await cq.answer("Invalid request.", show_alert=True)
            return

        cursor = uploads_col.find({"user_id": uid}).sort("timestamp", -1).limit(n)
        items = await cursor.to_list(length=n)
        ids = [item["_id"] for item in items]
        count = 0
        if ids:
            result = await uploads_col.delete_many({"_id": {"$in": ids}})
            count = result.deleted_count

        await cq.answer("Deleted.", show_alert=False)
        try:
            await cq.message.edit_text(f"🗑️ Deleted **{count} uploads** from the last {n}.")
        except:
            pass
        return


# ----------------- Eval helper (admin) -----------------


async def aexec(code: str, client: Client, message: Message):
    exec(
        "async def __aexec(client, message): " + "".join(f"\n {l_}" for l_ in code.split("\n"))
    )
    return await locals()["__aexec"](client, message)


@app.on_message(filters.command(["eval", "bash", "run", "e"]))
async def eval_cmd(client, message):
    status_message = await message.reply_text("Processing ...")
    # replace admin id with your id
    ADMIN_ID = int(os.getenv("ADMIN_ID") or 1989750989)
    if int(message.from_user.id) != ADMIN_ID:
        return await status_message.edit("You don't have permission to run this command.")
    if len(message.command) < 2:
        return await status_message.edit("`Give A Command To Run..`")
    cmd = message.text.split(" ", maxsplit=1)[1]
    reply_to_ = message.reply_to_message or message

    old_stderr = sys.stderr
    old_stdout = sys.stdout
    redirected_output = sys.stdout = io.StringIO()
    redirected_error = sys.stderr = io.StringIO()
    stdout, stderr, exc = None, None, None

    try:
        await aexec(cmd, client, message)
    except Exception:
        exc = traceback.format_exc()

    stdout = redirected_output.getvalue()
    stderr = redirected_error.getvalue()
    sys.stdout = old_stdout
    sys.stderr = old_stderr

    if exc:
        evaluation = exc
    elif stderr:
        evaluation = stderr
    elif stdout:
        evaluation = stdout
    else:
        evaluation = "Success"

    final_output = "<b>EVAL</b>: "
    final_output += f"<code>{cmd}</code>\n\n"
    final_output += "<b>OUTPUT</b>:\n"
    final_output += f"<code>{evaluation.strip()}</code>\n"

    if len(final_output) > 4096:
        with io.BytesIO(str.encode(final_output)) as out_file:
            out_file.name = "eval.text"
            await reply_to_.reply_document(document=out_file, caption=cmd, disable_notification=True)
    else:
        await reply_to_.reply_text(final_output)
    await status_message.delete()


# ----------------- Account management (accounts) -----------------


def mask(s: Optional[str], keep=6):
    if not s:
        return "—"
    s = str(s)
    if len(s) <= keep:
        return "•••"
    return s[:keep] + "•••" + s[-3:]


@app.on_message(filters.command("accounts") & filters.private)
async def accounts_cmd(client, message):
    """Show connected accounts and actions (view / remove)."""
    uid = message.from_user.id
    user = await users_col.find_one({"_id": uid}) or {}

    # GDrive status
    gdrive_tokens = user.get("gdrive")
    gdrive_cfg = user.get("gdrive_client_config") or {}
    gd_connected = bool(gdrive_tokens)
    gd_client_id = None
    if isinstance(gdrive_cfg, dict):
        sec = gdrive_cfg.get("installed") or gdrive_cfg.get("web") or {}
        gd_client_id = sec.get("client_id") or sec.get("clientId") or None

    # FilePress status
    filepress_key = user.get("filepress_api_key")
    fp_connected = bool(filepress_key)
    fp_url = user.get("filepress_url")

    # Shortener status
    shortener = user.get("shortener")
    short_host = shortener.get("host") if isinstance(shortener, dict) else None
    short_key = shortener.get("api_key") if isinstance(shortener, dict) else None
    short_connected = bool(shortener)

    text = "<b>Connected Accounts</b>\n\n"
    text += "<b>Google Drive</b>\n"
    text += f"• Connected: {'✅' if gd_connected else '❌'}\n"
    text += f"• Client ID: {mask(gd_client_id, keep=10)}\n"
    if gd_connected:
        rt = gdrive_tokens.get("refresh_token")
        acc_token = gdrive_tokens.get("token")
        text += f"• Refresh token: {'✅' if rt else '❌'}\n"
        text += f"• Access token: {mask(acc_token, keep=8)}\n"
    text += "\n"

    text += "<b>FilePress</b>\n"
    text += f"• Connected: {'✅' if fp_connected else '❌'}\n"
    if fp_connected:
        text += f"• API key: {mask(filepress_key, keep=8)}\n"
        text += f"• Domain: {fp_url or '—'}\n"
    text += "\n"

    text += "<b>Shortener</b>\n"
    text += f"• Configured: {'✅' if short_connected else '❌'}\n"
    if short_connected:
        text += f"• Host: {short_host}\n"
        text += f"• API key: {mask(short_key, keep=8)}\n"
    text += "\n"
    text += "Use the buttons below to view more details or remove an account."

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("View GDrive", callback_data="acct_view_gdrive"),
                InlineKeyboardButton("Remove GDrive", callback_data="acct_rm_gdrive"),
            ],
            [
                InlineKeyboardButton("View FilePress", callback_data="acct_view_fp"),
                InlineKeyboardButton("Remove FilePress", callback_data="acct_rm_fp"),
            ],
            [
                InlineKeyboardButton("View Shortener", callback_data="acct_view_short"),
                InlineKeyboardButton("Remove Shortener", callback_data="acct_rm_short"),
            ],
            [InlineKeyboardButton("Close", callback_data="acct_close")],
        ]
    )

    await message.reply_text(text, reply_markup=keyboard)


def format_gdrive_info(user_doc: dict) -> str:
    gdrive = user_doc.get("gdrive")
    client_cfg = user_doc.get("gdrive_client_config") or {}
    sec = client_cfg.get("installed") or client_cfg.get("web") or {}
    client_id = sec.get("client_id") or sec.get("clientId") or "—"
    client_name = sec.get("client_name") or sec.get("name") or "—"

    text = "<b>Google Drive details</b>\n\n"
    text += f"• Client name: {client_name}\n"
    text += f"• Client ID: {client_id}\n\n"
    if not gdrive:
        text += "No Google Drive tokens stored.\n"
        return text

    access = gdrive.get("token") or "—"
    refresh = gdrive.get("refresh_token") or "—"
    token_uri = gdrive.get("token_uri") or "—"
    scopes = gdrive.get("scopes") or []

    text += f"• Access token: {access}\n"
    text += f"• Refresh token: {refresh}\n"
    text += f"• Token URI: {token_uri}\n"
    text += f"• Scopes: {', '.join(scopes) if scopes else '—'}\n"
    return text


def format_filepress_info(user_doc: dict) -> str:
    key = user_doc.get("filepress_api_key")
    url = user_doc.get("filepress_url")
    text = "<b>FilePress details</b>\n\n"
    if not key:
        text += "No FilePress API key stored.\n"
        return text
    text += f"• FP Domain: {url}\n"
    text += f"• API key: {key}\n"
    last = user_doc.get("last_filepress_response")
    if last and isinstance(last, dict):
        st = last.get("status")
        text += f"• Last FilePress status: {st}\n"
        file_id = None
        js = last.get("json")
        if isinstance(js, dict):
            if "data" in js and isinstance(js["data"], dict):
                file_id = js["data"].get("_id") or js["data"].get("id") or js["data"].get("file_id")
            else:
                file_id = js.get("_id") or js.get("id") or js.get("file_id")
        if file_id:
            text += f"• Example file id: {file_id}\n"
    return text


def format_shortener_info(user_doc: dict) -> str:
    short = user_doc.get("shortener")
    text = "<b>Shortener details</b>\n\n"
    if not short:
        text += "No shortener configured.\n"
        return text
    text += f"• Host: {short.get('host')}\n"
    text += f"• API key: {short.get('api_key')}\n"
    return text


@app.on_callback_query(filters.regex(r"^acct_"))
async def acct_callback(client, cq: CallbackQuery):
    data = cq.data
    uid = cq.from_user.id
    user_doc = await users_col.find_one({"_id": uid}) or {}
    await cq.answer()

    if data == "acct_close":
        try:
            await cq.message.delete()
        except Exception:
            await cq.message.edit_text("Closed.")
        return

    if data == "acct_view_gdrive":
        txt = format_gdrive_info(user_doc)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="acct_back")]])
        await cq.message.edit_text(txt, reply_markup=keyboard)
        return

    if data == "acct_view_fp":
        txt = format_filepress_info(user_doc)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="acct_back")]])
        await cq.message.edit_text(txt, reply_markup=keyboard)
        return

    if data == "acct_view_short":
        txt = format_shortener_info(user_doc)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="acct_back")]])
        await cq.message.edit_text(txt, reply_markup=keyboard)
        return

    if data == "acct_rm_gdrive":
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Confirm remove GDrive", callback_data="acct_rm_gdrive_confirm"),
                    InlineKeyboardButton("Cancel", callback_data="acct_back"),
                ]
            ]
        )
        await cq.message.edit_text(
            "Are you sure you want to remove stored Google Drive credentials? This will delete refresh/access tokens from the bot.",
            reply_markup=kb,
        )
        return

    if data == "acct_rm_gdrive_confirm":
        await users_col.update_one({"_id": uid}, {"$unset": {"gdrive": "", "gdrive_client_config": ""}})
        await cq.message.edit_text("✅ Google Drive credentials removed.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="acct_back")]]))
        return

    if data == "acct_rm_fp":
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Confirm remove FilePress", callback_data="acct_rm_fp_confirm"),
                    InlineKeyboardButton("Cancel", callback_data="acct_back"),
                ]
            ]
        )
        await cq.message.edit_text("Are you sure you want to remove stored FilePress API key?", reply_markup=kb)
        return

    if data == "acct_rm_fp_confirm":
        await users_col.update_one({"_id": uid}, {"$unset": {"filepress_api_key": "", "filepress_url": ""}})
        await cq.message.edit_text("✅ FilePress API key removed.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="acct_back")]]))
        return

    if data == "acct_rm_short":
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Confirm remove Shortener", callback_data="acct_rm_short_confirm"),
                    InlineKeyboardButton("Cancel", callback_data="acct_back"),
                ]
            ]
        )
        await cq.message.edit_text("Are you sure you want to remove stored Shortener configuration?", reply_markup=kb)
        return

    if data == "acct_rm_short_confirm":
        await users_col.update_one({"_id": uid}, {"$unset": {"shortener": ""}})
        await cq.message.edit_text("✅ Shortener configuration removed.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="acct_back")]]))
        return

    if data == "acct_back":
        # re-render main summary
        user = await users_col.find_one({"_id": uid}) or {}
        gdrive_tokens = user.get("gdrive")
        gdrive_cfg = user.get("gdrive_client_config") or {}
        gd_connected = bool(gdrive_tokens)
        sec = gdrive_cfg.get("installed") or gdrive_cfg.get("web") or {}
        gd_client_id = sec.get("client_id") or sec.get("clientId") or None
        filepress_key = user.get("filepress_api_key")
        fp_connected = bool(filepress_key)
        fp_url = user.get("filepress_url")
        shortener = user.get("shortener")
        short_host = shortener.get("host") if isinstance(shortener, dict) else None
        short_key = shortener.get("api_key") if isinstance(shortener, dict) else None
        short_connected = bool(shortener)

        text = "<b>Connected Accounts</b>\n\n"
        text += "<b>Google Drive</b>\n"
        text += f"• Connected: {'✅' if gd_connected else '❌'}\n"
        text += f"• Client ID: {mask(gd_client_id, keep=10)}\n\n"
        text += "<b>FilePress</b>\n"
        text += f"• Connected: {'✅' if fp_connected else '❌'}\n\n"
        text += "<b>Shortener</b>\n"
        text += f"• Configured: {'✅' if short_connected else '❌'}\n"
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("View GDrive", callback_data="acct_view_gdrive"),
                    InlineKeyboardButton("Remove GDrive", callback_data="acct_rm_gdrive"),
                ],
                [
                    InlineKeyboardButton("View FilePress", callback_data="acct_view_fp"),
                    InlineKeyboardButton("Remove FilePress", callback_data="acct_rm_fp"),
                ],
                [
                    InlineKeyboardButton("View Shortener", callback_data="acct_view_short"),
                    InlineKeyboardButton("Remove Shortener", callback_data="acct_rm_short"),
                ],
                [InlineKeyboardButton("Close", callback_data="acct_close")],
            ]
        )
        await cq.message.edit_text(text, reply_markup=keyboard)
        return


# ----------------- Shortener commands (set/view/remove/shorten) -----------------


@app.on_message(filters.command("shortener_set") & filters.private)
async def shortener_set_cmd(client, message: Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        return await message.reply(
            "Usage:\n`/shortener_set <shortener_host> <api_key>`\n\nExample: `/shortener_set short.example.com MYAPIKEY123`"
        )

    host = parts[1].strip()
    api_key = parts[2].strip()
    host = host.replace("https://", "").replace("http://", "").strip().rstrip("/")
    if not host:
        return await message.reply("Invalid shortener host.")

    await users_col.update_one({"_id": message.from_user.id}, {"$set": {"shortener.host": host, "shortener.api_key": api_key}}, upsert=True)
    await message.reply(f"✅ Shortener saved:\n• Host: `{host}`\n• API key: `{api_key}`")


@app.on_message(filters.command("shortener_view") & filters.private)
async def shortener_view_cmd(client, message: Message):
    uid = message.from_user.id
    doc = await users_col.find_one({"_id": uid}) or {}
    short = doc.get("shortener")
    if not short:
        return await message.reply("You have no shortener configured. Use `/shortener_set` to add one.")
    host = short.get("host")
    api_key = short.get("api_key")
    text = f"<b>Your shortener</b>\n\n• Host: <code>{host}</code>\n• API key: <code>{api_key}</code>\n\nRequest format used:\n<code>https://{host}/api?api={api_key}&url={{original_url}}&alias={{alias}}</code>"
    await message.reply(text)


@app.on_message(filters.command("shortener_remove") & filters.private)
async def shortener_remove_cmd(client, message: Message):
    uid = message.from_user.id
    doc = await users_col.find_one({"_id": uid}) or {}
    if not doc.get("shortener"):
        return await message.reply("No shortener configured.")
    await users_col.update_one({"_id": uid}, {"$unset": {"shortener": ""}})
    await message.reply("✅ Shortener configuration removed.")


@app.on_message(filters.command("shorten") & filters.private)
async def shorten_cmd(client, message: Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        return await message.reply("Usage:\n`/shorten <url> [alias]`")

    original = parts[1].strip()
    alias = parts[2].strip() if len(parts) == 3 else None

    status_msg = await message.reply("Shortening...")
    short = await shorten_user_link(message.from_user.id, original, alias=alias)

    if short:
        await status_msg.edit_text(f"🔗 Shortened:\n{short}")
    else:
        await status_msg.edit_text(
            "❌ Failed to shorten the link. Possible causes:\n"
            "- No shortener configured (use /shortener_set)\n"
            "- Shortener server returned unexpected response\n"
            "If you want the raw response for debugging, try again and then use `/shortener_view` to check config."
        )


# ----------------- END OF FILE - Coded by ThiruXD | thiruxd.is-a.dev -----------------

if __name__ == "__main__":
    print("Starting bot...")
    app.run()
