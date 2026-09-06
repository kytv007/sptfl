import asyncio
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from telethon import TelegramClient, events, Button


# ============================================================
# CONFIG
# ============================================================

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
BOT_TOKEN = os.environ["TG_BOT_TOKEN"]

DOWNLOAD_ROOT = Path("/app/downloads")
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

REGISTRY = os.environ.get(
    "SPOTIFLAC_REGISTRIES",
    "https://raw.githubusercontent.com/"
    "spotiflacapp/SpotiFLAC-Extension/main/registry.json"
)


# ============================================================
# PROVIDERS
# ============================================================

PROVIDERS = {
    "tidal": {
        "name": "TIDAL",
        "service": "ext:tidal-web",
    },

    "qobuz": {
        "name": "Qobuz",
        "service": "ext:qobuz-web",
    },

    "deezer": {
        "name": "Deezer",
        "service": "ext:deezer",
    },

    "amazon": {
        "name": "Amazon Music",
        "service": "ext:amazon",
    },
}


# ============================================================
# TELEGRAM
# ============================================================

client = TelegramClient(
    "spotiflac_bot",
    API_ID,
    API_HASH,
)


# ============================================================
# USER STATE
# ============================================================

pending_urls = {}

active_users = set()

user_locks = {}


def get_lock(user_id):
    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()

    return user_locks[user_id]


# ============================================================
# URL VALIDATION
# ============================================================

SPOTIFY_RE = re.compile(
    r"^https?://open\.spotify\.com/"
    r"(track|album|playlist|artist)/",
    re.IGNORECASE,
)


def is_spotify_url(url):
    return bool(SPOTIFY_RE.match(url.strip()))


# ============================================================
# FILE HELPERS
# ============================================================

AUDIO_EXTENSIONS = {
    ".flac",
    ".m4a",
    ".mp3",
    ".opus",
    ".ogg",
    ".wav",
    ".aac",
}


def find_audio_files(directory):
    return [
        p
        for p in directory.rglob("*")
        if p.is_file()
        and p.suffix.lower() in AUDIO_EXTENSIONS
    ]


def human_size(size):
    if size < 1024:
        return f"{size} B"

    if size < 1024 ** 2:
        return f"{size / 1024:.1f} KB"

    if size < 1024 ** 3:
        return f"{size / 1024 ** 2:.1f} MB"

    return f"{size / 1024 ** 3:.2f} GB"


def progress_bar(percent, width=20):

    percent = max(0, min(100, percent))

    filled = int(width * percent / 100)
    empty = width - filled

    return (
        "["
        + "█" * filled
        + "░" * empty
        + "]"
    )


# ============================================================
# TELEGRAM STATUS
# ============================================================

async def update_status(message, text):
    try:
        await message.edit(text)
    except Exception:
        pass


# ============================================================
# PROVIDER KEYBOARD
# ============================================================

def provider_keyboard():

    return [
        [
            Button.inline(
                "🎵 TIDAL",
                b"provider:tidal",
            ),
            Button.inline(
                "🎧 Qobuz",
                b"provider:qobuz",
            ),
        ],
        [
            Button.inline(
                "💿 Deezer",
                b"provider:deezer",
            ),
            Button.inline(
                "🛒 Amazon",
                b"provider:amazon",
            ),
        ],
    ]


# ============================================================
# START
# ============================================================

@client.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):

    await event.respond(
        "🎵 **SpotiFLAC Telegram Bot**\n\n"
        "Send me a Spotify track, album or playlist URL.\n\n"
        "Example:\n"
        "`https://open.spotify.com/track/...`"
    )


# ============================================================
# URL RECEIVER
# ============================================================

@client.on(events.NewMessage)
async def url_handler(event):

    if not event.is_private:
        return

    text = (event.raw_text or "").strip()

    if text.startswith("/"):
        return

    if not is_spotify_url(text):

        await event.respond(
            "❌ I don't recognize that URL.\n\n"
            "Currently send a Spotify URL."
        )

        return

    user_id = event.sender_id

    if user_id in active_users:

        await event.respond(
            "⏳ You already have a download running.\n"
            "Please wait for it to finish."
        )

        return

    pending_urls[user_id] = text

    await event.respond(
        "🔗 **Spotify URL received.**\n\n"
        "Choose the audio source:",
        buttons=provider_keyboard(),
    )


# ============================================================
# PROVIDER SELECTION
# ============================================================

@client.on(events.CallbackQuery(pattern=b"provider:"))
async def provider_handler(event):

    user_id = event.sender_id

    if user_id not in pending_urls:

        await event.answer(
            "No pending URL.",
            alert=True,
        )

        return

    provider_key = event.data.decode().split(":", 1)[1]

    if provider_key not in PROVIDERS:

        await event.answer(
            "Unknown provider.",
            alert=True,
        )

        return

    url = pending_urls.pop(user_id)

    provider = PROVIDERS[provider_key]

    await event.answer(
        f"Starting {provider['name']}..."
    )

    status = await event.edit(
        f"🚀 **Starting download**\n\n"
        f"Source: **{provider['name']}**\n"
        f"Quality: **LOSSLESS**\n\n"
        f"Preparing SpotiFLAC..."
    )

    asyncio.create_task(
        download_job(
            user_id,
            url,
            provider_key,
            status,
        )
    )


# ============================================================
# DOWNLOAD JOB
# ============================================================

async def download_job(
    user_id,
    url,
    provider_key,
    status,
):

    lock = get_lock(user_id)

    async with lock:

        active_users.add(user_id)

        job_id = uuid.uuid4().hex

        job_dir = DOWNLOAD_ROOT / job_id

        job_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        provider = PROVIDERS[provider_key]

        process = None

        try:

            command = [
                "spotiflac",

                url,

                "/app/downloads",

                "--service",
                provider["service"],

                "--quality",
                "LOSSLESS",

                "--verbose",
            ]

            await update_status(
                status,
                f"🚀 **SpotiFLAC started**\n\n"
                f"Source: **{provider['name']}**\n"
                f"Quality: **LOSSLESS**\n\n"
                f"🔎 Resolving track..."
            )

            # ------------------------------------------------
            # Start SpotiFLAC
            # ------------------------------------------------

            process = await asyncio.create_subprocess_exec(
                *command,
                cwd="/app",

                stdout=asyncio.subprocess.PIPE,

                stderr=asyncio.subprocess.STDOUT,
            )

            start_time = time.monotonic()

            last_update = 0

            output_lines = []

            while True:

                line = await process.stdout.readline()

                if not line:
                    break

                text = line.decode(
                    "utf-8",
                    errors="replace",
                ).strip()

                if not text:
                    continue

                output_lines.append(text)

                if len(output_lines) > 200:
                    output_lines.pop(0)

                now = time.monotonic()

                # ------------------------------------------------
                # Search for useful progress information
                # ------------------------------------------------

                audio_files = find_audio_files(
                    DOWNLOAD_ROOT
                )

                # Only consider files created/modified by
                # this job after its start.
                audio_files = [
                    p
                    for p in audio_files
                    if p.stat().st_mtime >= start_time
                ]

                if now - last_update >= 3:

                    elapsed = int(
                        now - start_time
                    )

                    if audio_files:

                        latest = max(
                            audio_files,
                            key=lambda p: p.stat().st_mtime,
                        )

                        size = latest.stat().st_size

                        await update_status(
                            status,
                            f"🎵 **Downloading**\n\n"
                            f"Source: **{provider['name']}**\n\n"
                            f"`{latest.name}`\n\n"
                            f"📦 {human_size(size)}\n"
                            f"⏱ {elapsed}s\n\n"
                            f"🔄 Processing..."
                        )

                    else:

                        # Extract useful SpotiFLAC messages.
                        display = text

                        interesting = (
                            "Download",
                            "Matching",
                            "Searching",
                            "Resolving",
                            "Starting",
                            "Track",
                            "FLAC",
                            "session",
                        )

                        if not any(
                            x.lower()
                            in text.lower()
                            for x in interesting
                        ):
                            display = (
                                "Waiting for provider..."
                            )

                        if len(display) > 300:
                            display = display[-300:]

                        await update_status(
                            status,
                            f"🎵 **SpotiFLAC**\n\n"
                            f"Source: **{provider['name']}**\n\n"
                            f"⏳ `{display}`\n\n"
                            f"Elapsed: {elapsed}s"
                        )

                    last_update = now

            return_code = await process.wait()

            # ------------------------------------------------
            # Find resulting audio
            # ------------------------------------------------

            audio_files = find_audio_files(
                DOWNLOAD_ROOT
            )

            audio_files = [
                p
                for p in audio_files
                if p.stat().st_mtime >= start_time
            ]

            if return_code != 0 or not audio_files:

                errors = []

                for line in output_lines[-40:]:

                    lower = line.lower()

                    if any(
                        word in lower
                        for word in [
                            "failed",
                            "error",
                            "timeout",
                            "unavailable",
                            "wrong track",
                        ]
                    ):

                        errors.append(line)

                details = "\n".join(errors[-5:])

                if not details:
                    details = (
                        "SpotiFLAC did not produce "
                        "an audio file."
                    )

                await update_status(
                    status,
                    f"❌ **Download failed**\n\n"
                    f"Source: **{provider['name']}**\n\n"
                    f"{details[:1500]}"
                )

                return

            # ------------------------------------------------
            # Pick newest file
            # ------------------------------------------------

            audio_file = max(
                audio_files,
                key=lambda p: p.stat().st_mtime,
            )

            await update_status(
                status,
                f"✅ **Download complete**\n\n"
                f"Source: **{provider['name']}**\n"
                f"File: `{audio_file.name}`\n"
                f"Size: {human_size(audio_file.stat().st_size)}\n\n"
                f"📤 Uploading to Telegram..."
            )

            # ------------------------------------------------
            # Upload
            # ------------------------------------------------

            await client.send_file(
                user_id,
                str(audio_file),
                caption=(
                    f"🎵 **{audio_file.stem}**\n\n"
                    f"Source: {provider['name']}\n"
                    f"Format: {audio_file.suffix.upper()}\n"
                    f"Size: {human_size(audio_file.stat().st_size)}"
                ),

                # Telegram should treat it as a document,
                # preserving the FLAC file.
                force_document=True,
            )

            await update_status(
                status,
                "✅ **Finished**\n\n"
                "Your audio file has been sent above."
            )

        except asyncio.CancelledError:

            if process:
                try:
                    process.kill()
                except Exception:
                    pass

            raise

        except Exception as exc:

            await update_status(
                status,
                f"❌ **Bot error**\n\n"
                f"`{str(exc)[:1500]}`"
            )

        finally:

            active_users.discard(user_id)

            # ------------------------------------------------
            # Cleanup
            # ------------------------------------------------

            try:
                shutil.rmtree(
                    job_dir,
                    ignore_errors=True,
                )
            except Exception:
                pass


# ============================================================
# MAIN
# ============================================================

async def main():

    print("Starting SpotiFLAC Telegram bot...")

    await client.start(
        bot_token=BOT_TOKEN
    )

    me = await client.get_me()

    print(
        f"Bot running as @{me.username}"
    )

    print(
        "SpotiFLAC Telegram bot is ready."
    )

    await client.run_until_disconnected()


if __name__ == "__main__":

    asyncio.run(main())
