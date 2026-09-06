import asyncio
import os
import shutil
import signal
import time
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

MAX_CONCURRENT_DOWNLOADS = int(
    os.getenv("MAX_CONCURRENT_DOWNLOADS", "3")
)

PROGRESS_INTERVAL = float(
    os.getenv("PROGRESS_INTERVAL", "2")
)


# ============================================================
# Telegram client
# ============================================================

client = TelegramClient(
    "spotiflac_bot",
    API_ID,
    API_HASH
)


# ============================================================
# Job management
# ============================================================

# job_id -> job information
jobs = {}

# Limits the number of SpotiFLAC processes running at once.
download_slots = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)


# ============================================================
# Provider definitions
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
# Utility functions
# ============================================================

def format_bytes(value: int) -> str:
    """Convert bytes into a readable size."""

    if value < 1024:
        return f"{value} B"

    if value < 1024 ** 2:
        return f"{value / 1024:.1f} KB"

    if value < 1024 ** 3:
        return f"{value / 1024 ** 2:.1f} MB"

    return f"{value / 1024 ** 3:.2f} GB"


def progress_bar(percent: int, width: int = 12) -> str:
    """Create a simple text progress bar."""

    percent = max(0, min(100, percent))

    filled = int(width * percent / 100)

    return (
        "█" * filled
        + "░" * (width - filled)
    )


def find_audio_file(job_dir: Path):
    """Find the downloaded audio file."""

    if not job_dir.exists():
        return None

    # Prefer FLAC.
    files = list(job_dir.rglob("*.flac"))

    if files:
        return max(files, key=lambda p: p.stat().st_mtime)

    # Fallback for other audio formats.
    extensions = {
        ".m4a",
        ".mp3",
        ".opus",
        ".ogg",
        ".wav",
        ".aac",
    }

    files = [
        p for p in job_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in extensions
    ]

    if not files:
        return None

    return max(files, key=lambda p: p.stat().st_mtime)


def get_directory_size(path: Path) -> int:
    """Calculate total size of files in a directory."""

    total = 0

    if not path.exists():
        return 0

    for file in path.rglob("*"):
        try:
            if file.is_file():
                total += file.stat().st_size
        except OSError:
            pass

    return total


async def cleanup_job(job_id: str):
    """Remove all files belonging to a job."""

    job = jobs.get(job_id)

    if not job:
        return

    job_dir = job["job_dir"]

    try:
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
    except Exception as exc:
        print(f"[{job_id}] Cleanup error: {exc}")

    jobs.pop(job_id, None)


async def terminate_process(process, job_id: str):
    """Safely terminate a SpotiFLAC subprocess."""

    if process is None:
        return

    if process.returncode is not None:
        return

    print(f"[{job_id}] Terminating SpotiFLAC...")

    try:
        process.terminate()
    except ProcessLookupError:
        return
    except Exception as exc:
        print(f"[{job_id}] terminate() failed: {exc}")

    try:
        await asyncio.wait_for(
            process.wait(),
            timeout=5
        )

        return

    except asyncio.TimeoutError:
        pass

    print(f"[{job_id}] SpotiFLAC did not terminate. Killing...")

    try:
        process.kill()
    except ProcessLookupError:
        pass
    except Exception as exc:
        print(f"[{job_id}] kill() failed: {exc}")

    try:
        await asyncio.wait_for(
            process.wait(),
            timeout=5
        )
    except asyncio.TimeoutError:
        print(f"[{job_id}] Process still running.")


async def update_progress(
    job_id: str,
    message,
):
    """
    Monitor the job directory and update Telegram.

    SpotiFLAC does not necessarily expose a reliable total byte
    count for every provider, so we display downloaded size rather
    than inventing a percentage.
    """

    job = jobs.get(job_id)

    if not job:
        return

    last_size = -1
    last_update = 0

    while True:

        job = jobs.get(job_id)

        if not job:
            return

        process = job.get("process")

        current_size = get_directory_size(
            job["job_dir"]
        )

        now = time.monotonic()

        # Update when size changed or enough time has passed.
        if (
            current_size != last_size
            and now - last_update >= PROGRESS_INTERVAL
        ):
            last_size = current_size
            last_update = now

            if job.get("cancelled"):
                return

            provider_name = job["provider_name"]

            text = (
                f"🎵 <b>Downloading</b>\n\n"
                f"Provider: <b>{provider_name}</b>\n\n"
                f"📦 Downloaded: "
                f"<b>{format_bytes(current_size)}</b>\n\n"
                f"⏳ SpotiFLAC is processing..."
            )

            try:
                await message.edit(
                    text,
                    buttons=[
                        [
                            Button.inline(
                                "⛔ Stop",
                                data=f"stop:{job_id}".encode()
                            )
                        ]
                    ]
                )
            except Exception:
                pass

        # Process finished.
        if process is not None and process.returncode is not None:
            return

        await asyncio.sleep(0.5)


# ============================================================
# Start command
# ============================================================

@client.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):

    text = (
        "🎵 <b>SpotiFLAC Bot</b>\n\n"
        "Send me a Spotify track URL and choose "
        "the provider you want to use."
    )

    await event.respond(text)


# ============================================================
# URL handler
# ============================================================

@client.on(events.NewMessage)
async def url_handler(event):

    # Ignore commands.
    if event.raw_text.startswith("/"):
        return

    url = event.raw_text.strip()

    if not (
        "open.spotify.com" in url
        or "spotify.link" in url
    ):
        return

    # Short ID for Telegram callback_data.
    request_id = uuid.uuid4().hex[:12]

    # Store the URL temporarily.
    jobs[request_id] = {
        "type": "pending",
        "user_id": event.sender_id,
        "chat_id": event.chat_id,
        "url": url,
        "created": time.monotonic(),
    }

    buttons = [
        [
            Button.inline(
                "🎧 TIDAL",
                data=f"provider:tidal:{request_id}".encode()
            ),
            Button.inline(
                "🎵 Qobuz",
                data=f"provider:qobuz:{request_id}".encode()
            ),
        ],
        [
            Button.inline(
                "🔊 Deezer",
                data=f"provider:deezer:{request_id}".encode()
            ),
            Button.inline(
                "🛒 Amazon",
                data=f"provider:amazon:{request_id}".encode()
            ),
        ],
    ]

    await event.respond(
        "Choose a provider:",
        buttons=buttons
    )

# ============================================================
# Provider selection
# ============================================================

@client.on(events.CallbackQuery(pattern=b"provider:"))
async def provider_handler(event):

    data = event.data.decode()

    # provider:<provider>:<request_id>
    parts = data.split(":", 2)

    if len(parts) != 3:
        await event.answer(
            "Invalid request.",
            alert=True
        )
        return

    _, provider_key, request_id = parts

    provider = PROVIDERS.get(provider_key)

    if not provider:
        await event.answer(
            "Unknown provider.",
            alert=True
        )
        return

    # Retrieve pending request.
    pending = jobs.get(request_id)

    if not pending or pending.get("type") != "pending":
        await event.answer(
            "This request has expired.",
            alert=True
        )
        return

    # Security: only the user who sent the URL
    # can choose the provider.
    if event.sender_id != pending["user_id"]:
        await event.answer(
            "This isn't your request.",
            alert=True
        )
        return

    url = pending["url"]

    # Now create the actual download job.
    job_id = uuid.uuid4().hex

    job_dir = DOWNLOAD_ROOT / job_id

    job = {
        "job_id": job_id,
        "user_id": pending["user_id"],
        "chat_id": pending["chat_id"],
        "url": url,
        "provider": provider_key,
        "provider_name": provider["name"],
        "service": provider["service"],
        "job_dir": job_dir,
        "process": None,
        "cancelled": False,
        "message": None,
    }

    # Replace pending request with download job.
    jobs.pop(request_id, None)
    jobs[job_id] = job

    job_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    await event.answer(
        f"{provider['name']} selected."
    )

    status_message = await event.edit(
        f"🚀 <b>Starting download...</b>\n\n"
        f"Provider: <b>{provider['name']}</b>\n\n"
        f"Waiting for an available download slot...",
        buttons=[
            [
                Button.inline(
                    "⛔ Stop",
                    data=f"stop:{job_id}".encode()
                )
            ]
        ]
    )

    job["message"] = status_message

    asyncio.create_task(
        download_job(job_id)
    )

# ============================================================
# Download worker
# ============================================================

async def download_job(job_id: str):

    job = jobs.get(job_id)

    if not job:
        return

    process = None
    progress_task = None

    try:

        # Wait for an available slot.
        async with download_slots:

            job = jobs.get(job_id)

            if not job:
                return

            if job["cancelled"]:
                return

            message = job["message"]

            await message.edit(
                f"🚀 <b>Starting SpotiFLAC...</b>\n\n"
                f"Provider: <b>{job['provider_name']}</b>",
                buttons=[
                    [
                        Button.inline(
                            "⛔ Stop",
                            data=f"stop:{job_id}".encode()
                        )
                    ]
                ]
            )

            # ==================================================
            # Start SpotiFLAC
            # ==================================================

            command = [
                "spotiflac",
                job["url"],
                str(job["job_dir"]),
                "--service",
                job["service"],
                "--verbose",
            ]

            print(
                f"[{job_id}] Starting:"
                f" {' '.join(command)}"
            )

            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            job["process"] = process

            # ==================================================
            # Start progress monitor
            # ==================================================

            progress_task = asyncio.create_task(
                update_progress(
                    job_id,
                    message
                )
            )

            # ==================================================
            # Read SpotiFLAC output
            # ==================================================

            while True:

                line = await process.stdout.readline()

                if not line:
                    break

                decoded = line.decode(
                    "utf-8",
                    errors="replace"
                ).rstrip()

                if decoded:
                    print(
                        f"[{job_id}] {decoded}"
                    )

            return_code = await process.wait()

            print(
                f"[{job_id}] SpotiFLAC exited with "
                f"code {return_code}"
            )

            if job.get("cancelled"):

                await message.edit(
                    "🛑 <b>Download cancelled.</b>"
                )

                return

            # ==================================================
            # Find output
            # ==================================================

            audio_file = find_audio_file(
                job["job_dir"]
            )

            if return_code != 0 or audio_file is None:

                await message.edit(
                    "❌ <b>Download failed.</b>\n\n"
                    "SpotiFLAC could not produce an audio file."
                )

                return

            # ==================================================
            # Upload
            # ==================================================

            await message.edit(
                f"📤 <b>Uploading...</b>\n\n"
                f"🎵 {audio_file.name}"
            )

            print(
                f"[{job_id}] Uploading "
                f"{audio_file}"
            )

            await client.send_file(
                job["chat_id"],
                str(audio_file),
                caption=f"🎵 {audio_file.stem}",
                force_document=True,
            )

            await message.edit(
                "✅ <b>Download complete.</b>\n\n"
                "The temporary file has been removed."
            )

    except asyncio.CancelledError:

        print(
            f"[{job_id}] Download task cancelled."
        )

        if process:
            await terminate_process(
                process,
                job_id
            )

        raise

    except Exception as exc:

        print(
            f"[{job_id}] ERROR: {type(exc).__name__}: {exc}"
        )

        job = jobs.get(job_id)

        if job:

            try:
                await job["message"].edit(
                    f"❌ <b>Error</b>\n\n"
                    f"<code>{type(exc).__name__}</code>"
                )
            except Exception:
                pass

    finally:

        if progress_task:

            progress_task.cancel()

            try:
                await progress_task
            except asyncio.CancelledError:
                pass

        # Make absolutely sure the subprocess is gone.
        if process:

            await terminate_process(
                process,
                job_id
            )

        # Delete all downloaded data.
        await cleanup_job(job_id)

        print(
            f"[{job_id}] Cleanup complete."
        )


# ============================================================
# Stop button
# ============================================================

@client.on(events.CallbackQuery(pattern=b"stop:"))
async def stop_handler(event):

    data = event.data.decode()

    job_id = data.split(":", 1)[1]

    job = jobs.get(job_id)

    if not job:

        await event.answer(
            "This download is no longer active.",
            alert=True
        )

        return

    # Security: only the user who created the job
    # can stop it.
    if event.sender_id != job["user_id"]:

        await event.answer(
            "This is not your download.",
            alert=True
        )

        return

    if job.get("cancelled"):

        await event.answer(
            "Already stopping...",
            alert=False
        )

        return

    job["cancelled"] = True

    await event.answer(
        "Stopping download..."
    )

    try:

        await event.edit(
            "🛑 <b>Stopping download...</b>"
        )

    except Exception:
        pass

    process = job.get("process")

    if process:

        await terminate_process(
            process,
            job_id
        )

    # Cleanup immediately.
    await cleanup_job(job_id)

    try:

        await event.edit(
            "🛑 <b>Download cancelled.</b>"
        )

    except Exception:
        pass


# ============================================================
# Error handling
# ============================================================

@client.on(events.Raw)
async def raw_handler(event):
    pass


# ============================================================
# Main
# ============================================================

async def main():

    DOWNLOAD_ROOT.mkdir(
        parents=True,
        exist_ok=True
    )

    print(
        "Starting SpotiFLAC Telegram bot..."
    )

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

    print(
        f"Maximum simultaneous downloads: "
        f"{MAX_CONCURRENT_DOWNLOADS}"
    )

    await client.run_until_disconnected()


if __name__ == "__main__":

    try:
        asyncio.run(main())

    except KeyboardInterrupt:

        print(
            "Bot stopped."
        )
