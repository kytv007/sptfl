import asyncio
import os
import re
import shutil
import signal
import uuid
from pathlib import Path

from telethon import TelegramClient, events, Button


# ============================================================
# Configuration
# ============================================================

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
BOT_TOKEN = os.environ["TG_BOT_TOKEN"]

DOWNLOAD_ROOT = Path("/app/downloads")
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

REGISTRY_URL = (
    "https://raw.githubusercontent.com/spotiflacapp/"
    "SpotiFLAC-Extension/main/registry.json"
)


# ============================================================
# Telegram client
# ============================================================

client = TelegramClient(
    "spotiflac_bot",
    API_ID,
    API_HASH,
)


# ============================================================
# Job storage
#
# job_id -> {
#     "process": subprocess,
#     "task": asyncio.Task,
#     "user_id": int,
#     "message": TelegramMessage,
#     "directory": Path,
#     "url": str,
#     "provider": str,
#     "stopped": bool
# }
# ============================================================

jobs = {}


# ============================================================
# Provider configuration
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
        "name": "Amazon",
        "service": "ext:amazon",
    },
}


# ============================================================
# Helpers
# ============================================================

def is_url(text):
    return bool(
        re.match(
            r"^https?://",
            text.strip(),
            re.IGNORECASE,
        )
    )


def format_size(size):
    units = ["B", "KB", "MB", "GB"]

    value = float(size)

    for unit in units:
        if value < 1024:
            return f"{value:.1f} {unit}"

        value /= 1024

    return f"{value:.1f} TB"


def get_downloaded_files(directory):
    if not directory.exists():
        return []

    return [
        p
        for p in directory.rglob("*")
        if p.is_file()
    ]


def get_total_size(directory):
    total = 0

    for file in get_downloaded_files(directory):
        try:
            total += file.stat().st_size
        except OSError:
            pass

    return total


def make_progress_bar(percent, width=20):
    filled = int(width * percent / 100)
    empty = width - filled

    return "[" + "█" * filled + "░" * empty + "]"


# ============================================================
# Kill process + children
# ============================================================

async def terminate_process(proc):
    if proc is None:
        return

    if proc.returncode is not None:
        return

    try:
        # Because the subprocess is started in its own session,
        # this terminates SpotiFLAC and its Chromium children.
        os.killpg(
            os.getpgid(proc.pid),
            signal.SIGTERM,
        )
    except ProcessLookupError:
        return
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass

    try:
        await asyncio.wait_for(
            proc.wait(),
            timeout=5,
        )
    except asyncio.TimeoutError:
        try:
            os.killpg(
                os.getpgid(proc.pid),
                signal.SIGKILL,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

        try:
            await proc.wait()
        except Exception:
            pass


# ============================================================
# Stop a job
# ============================================================

async def stop_job(job_id):
    job = jobs.get(job_id)

    if not job:
        return False

    job["stopped"] = True

    proc = job.get("process")

    await terminate_process(proc)

    return True


# ============================================================
# Stop button
# ============================================================

def stop_button(job_id):
    return Button.inline(
        "🛑 Stop",
        data=f"stop:{job_id}",
    )


# ============================================================
# Provider buttons
# ============================================================

def provider_buttons():
    return [
        [
            Button.inline(
                "🌊 TIDAL",
                data="provider:tidal",
            ),
            Button.inline(
                "🎵 Qobuz",
                data="provider:qobuz",
            ),
        ],
        [
            Button.inline(
                "💿 Deezer",
                data="provider:deezer",
            ),
            Button.inline(
                "🛒 Amazon",
                data="provider:amazon",
            ),
        ],
    ]


# ============================================================
# URL waiting state
# ============================================================

pending_urls = {}


# ============================================================
# /start
# ============================================================

@client.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):

    await event.respond(
        "🎵 **SpotiFLAC Bot**\n\n"
        "Send me a Spotify track URL.\n\n"
        "Example:\n"
        "`https://open.spotify.com/track/...`",
        parse_mode="md",
    )


# ============================================================
# URL handler
# ============================================================

@client.on(events.NewMessage)
async def url_handler(event):

    # Ignore commands
    if event.raw_text.startswith("/"):
        return

    text = event.raw_text.strip()

    if not is_url(text):
        return

    pending_urls[event.sender_id] = text

    await event.respond(
        "Choose a download provider:",
        buttons=provider_buttons(),
    )


# ============================================================
# Provider selection
# ============================================================

@client.on(events.CallbackQuery(pattern=r"provider:(.+)"))
async def provider_handler(event):

    provider_key = event.pattern_match.group(1).decode()

    url = pending_urls.pop(
        event.sender_id,
        None,
    )

    if not url:
        await event.answer(
            "URL expired. Send the URL again.",
            alert=True,
        )
        return

    provider = PROVIDERS.get(provider_key)

    if not provider:
        await event.answer(
            "Unknown provider.",
            alert=True,
        )
        return

    await event.answer()

    # Create independent job
    job_id = uuid.uuid4().hex[:12]

    job_directory = DOWNLOAD_ROOT / job_id

    job_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    message = await event.edit(
        f"⏳ Starting download...\n\n"
        f"Provider: **{provider['name']}**\n"
        f"Job: `{job_id}`",
        parse_mode="md",
        buttons=[stop_button(job_id)],
    )

    job = {
        "process": None,
        "task": None,
        "user_id": event.sender_id,
        "message": message,
        "directory": job_directory,
        "url": url,
        "provider": provider_key,
        "stopped": False,
    }

    jobs[job_id] = job

    task = asyncio.create_task(
        download_job(job_id)
    )

    job["task"] = task


# ============================================================
# Download job
# ============================================================

async def download_job(job_id):

    job = jobs.get(job_id)

    if not job:
        return

    directory = job["directory"]
    provider_key = job["provider"]
    provider = PROVIDERS[provider_key]

    command = [
        "spotiflac",
        job["url"],
        str(directory),
        "--service",
        provider["service"],
        "--verbose",
    ]

    process = None

    try:

        await job["message"].edit(
            f"⏳ **Downloading...**\n\n"
            f"Provider: {provider['name']}\n"
            f"Job: `{job_id}`\n\n"
            f"{make_progress_bar(0)} 0%\n"
            f"Downloaded: 0 B",
            parse_mode="md",
            buttons=[stop_button(job_id)],
        )

        process = await asyncio.create_subprocess_exec(
            *command,

            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,

            # Creates a separate process group.
            # This is important when Chromium is launched.
            start_new_session=True,
        )

        job["process"] = process

        # Read output while process runs
        while True:

            if job["stopped"]:
                break

            line = await process.stdout.readline()

            if not line:
                break

            try:
                text = line.decode(
                    errors="ignore"
                ).strip()
            except Exception:
                text = ""

            # Update status periodically based on downloaded size.
            size = get_total_size(directory)

            # SpotiFLAC does not always expose a reliable percentage,
            # so don't invent one.
            status = "Downloading"

            lower = text.lower()

            if "metadata" in lower:
                status = "Reading metadata"

            elif "turnstile" in lower:
                status = "Browser verification"

            elif "download" in lower:
                status = "Downloading"

            elif "extract" in lower:
                status = "Extracting"

            try:
                await job["message"].edit(
                    f"⏳ **{status}...**\n\n"
                    f"Provider: {provider['name']}\n"
                    f"Job: `{job_id}`\n\n"
                    f"Downloaded: **{format_size(size)}**",
                    parse_mode="md",
                    buttons=[stop_button(job_id)],
                )
            except Exception:
                pass

        return_code = await process.wait()

        if job["stopped"]:

            try:
                await job["message"].edit(
                    f"🛑 **Download stopped**\n\n"
                    f"Job: `{job_id}`",
                    parse_mode="md",
                )
            except Exception:
                pass

            return

        files = get_downloaded_files(directory)

        if not files:

            try:
                await job["message"].edit(
                    f"❌ **Download failed**\n\n"
                    f"Provider: {provider['name']}\n"
                    f"Job: `{job_id}`\n\n"
                    f"SpotiFLAC exited with code `{return_code}`.",
                    parse_mode="md",
                )
            except Exception:
                pass

            return

        # Find likely audio files
        audio_files = [
            f
            for f in files
            if f.suffix.lower() in {
                ".flac",
                ".m4a",
                ".mp3",
                ".opus",
                ".ogg",
                ".wav",
            }
        ]

        if not audio_files:
            audio_files = files

        # Send every resulting audio file
        for audio_file in audio_files:

            try:
                await job["message"].edit(
                    f"📤 **Uploading...**\n\n"
                    f"`{audio_file.name}`",
                    parse_mode="md",
                )
            except Exception:
                pass

            await client.send_file(
                job["user_id"],
                str(audio_file),
                force_document=True,
                caption=(
                    f"🎵 {audio_file.name}\n"
                    f"Provider: {provider['name']}"
                ),
            )

        try:
            await job["message"].edit(
                f"✅ **Completed**\n\n"
                f"Provider: {provider['name']}\n"
                f"Job: `{job_id}`",
                parse_mode="md",
            )
        except Exception:
            pass

    except asyncio.CancelledError:

        if process:
            await terminate_process(process)

        raise

    except Exception as exc:

        try:
            await job["message"].edit(
                f"❌ **Error**\n\n"
                f"`{type(exc).__name__}: {exc}`",
                parse_mode="md",
            )
        except Exception:
            pass

    finally:

        # Make sure process is gone
        if process and process.returncode is None:
            await terminate_process(process)

        # Remove temporary files
        try:
            shutil.rmtree(
                directory,
                ignore_errors=True,
            )
        except Exception:
            pass

        jobs.pop(
            job_id,
            None,
        )


# ============================================================
# Stop button callback
# ============================================================

@client.on(events.CallbackQuery(pattern=r"stop:(.+)"))
async def stop_handler(event):

    job_id = event.pattern_match.group(1).decode()

    job = jobs.get(job_id)

    if not job:

        await event.answer(
            "This job is no longer running.",
            alert=True,
        )

        return

    # Only allow the owner of the job to stop it
    if event.sender_id != job["user_id"]:

        await event.answer(
            "This isn't your download.",
            alert=True,
        )

        return

    await event.answer(
        "Stopping..."
    )

    await stop_job(job_id)


# ============================================================
# Main
# ============================================================

async def main():

    print("Starting Telegram bot...")

    await client.start(
        bot_token=BOT_TOKEN,
    )

    print("Telegram bot is running.")

    await client.run_until_disconnected()


if __name__ == "__main__":

    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        pass
