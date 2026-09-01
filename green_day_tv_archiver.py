from __future__ import annotations

import ctypes
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


# ============================================================
# Green Day TV - Robust 24/7 Stream Archiver
#
# yt-dlp downloads the live HLS stream to stdout.
# FFmpeg reads that stream and creates hourly MKV segments.
#
# The source YouTube format is NOT hard-coded to a numeric ID.
# Instead, yt-dlp dynamically selects:
#
#   best 1080p-or-lower H.264 video + best audio
#
# This avoids failures when YouTube changes the available format
# IDs (e.g. format 96 disappearing and format 270 being used).
#
# No source footage is re-encoded.
#
# Each active segment is first written as:
#
#   GreenDayTV_YYYY-MM-DD_HH-MM-SS.local.mkv
#
# A background finalizer remuxes completed segments to:
#
#   GreenDayTV_YYYY-MM-DD_HH-MM-SS.mkv
#
# where the filename timestamp is converted from Europe/Rome
# local time to UTC.
#
# A Windows named mutex prevents multiple copies of this script
# from running simultaneously.
# ============================================================


# ============================================================
# Configuration
# ============================================================

# Your local timezone in IANA format (needed to convert timestamps to UTC).
# IANA format: 'Continent/City', in english, spaces replaced with "_".
# Examples: 'America/Washington_DC', 'Europe/Prague', 'Asia/Tokyo'.
# NOTE: Only capitals and/or cities bounded with a timestamp will work.
# Bad Examples: 'America/Berkley' (Use 'America/Los_Angeles'), 'Europe/Petersborough' (Use 'Europe/London').
LOCAL_TIMEZONE = ZoneInfo("America/Los_Angeles")

STREAM_URL = "https://www.youtube.com/watch?v=Xu90G4oFq2o"

# Where to store the segment files. Change this is necessary.
# I STRONGLY suggest using en external drive to avoid filling up and/or slowing down your hard drive.
# I suggest having at least 100GBs of free space, though the program should never actually use more than ~70GBs.
ARCHIVE_DIR = Path(r"D:\Archives\GreenDayTV")

# Length of a single MKV segment file, in seconds.
# Default: 1 hour (60 minutes * 60 seconds)
SEGMENT_SECONDS = 60 * 60

# Absolute minium amount of space to always leave free on the target drive.
# WARNING: the script will STOP if the free space ever reaches this value.
MIN_FREE_GB = 10

# Number of seconds to wait after an HTTP error occurs before retrying.
RETRY_SECONDS = 7

# If no media data is observed shortly after startup, restart.
STARTUP_TIMEOUT_SECONDS = 20

# If the media stream stops producing data for this long,
# consider the recording stalled.
STALL_SECONDS = 12

# yt-dlp retry settings.
YTDLP_RETRIES = 2
YTDLP_FRAGMENT_RETRIES = 2

# TCP relay is not used here; yt-dlp stdout is piped directly
# into FFmpeg.
#
# Chunk size used by the stdout reader.
PIPE_READ_SIZE = 256 * 1024

# Finalizer behavior.
FINALIZER_INTERVAL = 2.0
FINALIZER_STABLE_DELAY = 1.0

# Temporary output created during finalization.
FINALIZE_TEMP_SUFFIX = ".finalizing.mkv"

# Dynamic yt-dlp selector.
#
# Prefer:
#   - video-only
#   - H.264 / AVC
#   - maximum resolution up to 1080p
#   - best audio
#
# If that combination is unavailable, fall back to the best
# combined format up to 1080p.
FORMAT_SELECTOR = (
    "bestvideo[height<=1080][vcodec^=avc1]+bestaudio"
    "/best[height<=1080]"
)


# ============================================================
# Paths / executables
# ============================================================

YTDLP = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
FFMPEG = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")


# ============================================================
# Logging
# ============================================================

LOG_FILE = ARCHIVE_DIR / "archiver.log"


def configure_logging() -> logging.Logger:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("GreenDayTV")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )

    file_handler = logging.FileHandler(
        LOG_FILE,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


log = configure_logging()


# ============================================================
# Global state
# ============================================================

SHUTDOWN_EVENT = threading.Event()

# Exactly one current FFmpeg-generated segment.
#
# This is the important replacement for the old:
#
#     paths = paths[:-1]
#
# approach.
#
# The finalizer never needs to guess which file is active.
ACTIVE_SEGMENT_LOCK = threading.Lock()
ACTIVE_SEGMENT: Path | None = None


# ============================================================
# Windows single-instance mutex
# ============================================================

MUTEX_NAME = "Global\\GreenDayTV_Archiver_SingleInstance"


def acquire_single_instance() -> ctypes.c_void_p | None:
    """
    Acquire a Windows named mutex.

    If another copy of this program already owns the mutex,
    return None.
    """

    if os.name != "nt":
        return object()

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_bool,
        ctypes.c_wchar_p,
    ]
    kernel32.CreateMutexW.restype = ctypes.c_void_p

    kernel32.GetLastError.restype = ctypes.c_ulong

    ERROR_ALREADY_EXISTS = 183

    handle = kernel32.CreateMutexW(
        None,
        False,
        MUTEX_NAME,
    )

    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())

    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None

    return handle


def release_single_instance(handle) -> None:
    if os.name != "nt" or handle is None:
        return

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool

    kernel32.CloseHandle(handle)


# ============================================================
# Utility functions
# ============================================================

def free_space_gb(path: Path) -> float:
    usage = shutil.disk_usage(path)
    return usage.free / (1024 ** 3)


def parse_local_timestamp(path: Path) -> datetime:
    """
    Parse:
        GreenDayTV_YYYY-MM-DD_HH-MM-SS.local.mkv

    as Europe/Rome local time.
    """

    match = re.match(
        r"^GreenDayTV_(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})"
        r"\.local\.mkv$",
        path.name,
    )

    if not match:
        raise ValueError(f"Invalid segment filename: {path.name}")

    date_part = match.group(1)
    time_part = match.group(2)

    naive = datetime.strptime(
        f"{date_part} {time_part}",
        "%Y-%m-%d %H-%M-%S",
    )

    return naive.replace(tzinfo=LOCAL_TIMEZONE)


def utc_output_path(local_path: Path) -> Path:
    local_dt = parse_local_timestamp(local_path)

    utc_dt = local_dt.astimezone(timezone.utc)

    return ARCHIVE_DIR / (
        f"GreenDayTV_{utc_dt:%Y-%m-%d_%H-%M-%S}.mkv"
    )


def is_file_stable(path: Path, delay: float = FINALIZER_STABLE_DELAY) -> bool:
    """
    Check that the file size remains unchanged over a short interval.
    """

    try:
        size1 = path.stat().st_size
        time.sleep(delay)
        size2 = path.stat().st_size
    except (FileNotFoundError, PermissionError):
        return False

    return size1 == size2


# ============================================================
# Finalization
# ============================================================

FINALIZER_RE = re.compile(
    r"^GreenDayTV_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.local\.mkv$"
)


def get_active_segment() -> Path | None:
    with ACTIVE_SEGMENT_LOCK:
        return ACTIVE_SEGMENT


def set_active_segment(path: Path | None) -> None:
    global ACTIVE_SEGMENT

    with ACTIVE_SEGMENT_LOCK:
        ACTIVE_SEGMENT = path


def finalise_segment(path: Path) -> None:
    """
    Convert one completed .local.mkv into its final UTC-named MKV.

    The media itself is copied bit-for-bit; no re-encoding.
    """

    if not path.exists():
        return

    active = get_active_segment()

    if active is not None and path.resolve() == active.resolve():
        return

    if not is_file_stable(path):
        return

    output = utc_output_path(path)

    # Nothing to do if this exact final file already exists.
    if output.exists():
        try:
            path.unlink()
            log.info(
                "Removed duplicate local segment: %s",
                path.name,
            )
        except PermissionError:
            log.warning(
                "Could not remove duplicate local segment yet: %s",
                path.name,
            )

        return

    temporary_output = output.with_suffix(
        output.suffix + FINALIZE_TEMP_SUFFIX
    )

    try:
        if temporary_output.exists():
            temporary_output.unlink()

        local_dt = parse_local_timestamp(path)
        utc_dt = local_dt.astimezone(timezone.utc)

        comment = (
            "Green Day TV archive segment; "
            f"source local time {local_dt.isoformat()}; "
            f"UTC start {utc_dt.isoformat()}"
        )

        command = [
            FFMPEG,
            "-hide_banner",
            "-loglevel", "warning",
            "-y",
            "-i", str(path),

            "-map", "0",

            "-c", "copy",

            "-metadata", f"comment={comment}",
            "-metadata", f"creation_time={utc_dt.isoformat()}",

            str(temporary_output),
        ]

        log.info(
            "Finalizing %s -> %s",
            path.name,
            output.name,
        )

        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        if result.returncode != 0:
            log.warning(
                "Finalization failed for %s (FFmpeg exit %s): %s",
                path.name,
                result.returncode,
                result.stderr.strip(),
            )

            try:
                temporary_output.unlink(missing_ok=True)
            except PermissionError:
                pass

            return

        # Do not delete the source until FFmpeg successfully
        # produced the finalized file.
        #
        # On Windows, an open file can produce WinError 32.
        # Treat that as a transient condition rather than killing
        # the finalizer thread.
        try:
            path.unlink()
        except PermissionError:
            log.warning(
                "Source still locked; will retry finalization later: %s",
                path.name,
            )

            try:
                temporary_output.unlink(missing_ok=True)
            except PermissionError:
                pass

            return

        temporary_output.replace(output)

        log.info(
            "Finalized successfully: %s",
            output.name,
        )

    except FileNotFoundError:
        # File may have disappeared because another cleanup operation
        # handled it.
        return

    except PermissionError as exc:
        log.warning(
            "File temporarily locked during finalization of %s: %s",
            path.name,
            exc,
        )

        try:
            temporary_output.unlink(missing_ok=True)
        except PermissionError:
            pass

    except Exception:
        log.exception(
            "Unexpected finalization error for %s",
            path.name,
        )


def segment_finaliser_loop() -> None:
    log.info("Segment finalizer thread started.")

    while not SHUTDOWN_EVENT.is_set():

        try:
            paths = sorted(
                (
                    p
                    for p in ARCHIVE_DIR.glob(
                        "GreenDayTV_*.local.mkv"
                    )
                    if FINALIZER_RE.match(p.name)
                ),
                key=lambda p: p.stat().st_mtime,
            )

            active = get_active_segment()

            for path in paths:

                if SHUTDOWN_EVENT.is_set():
                    break

                if active is not None:
                    try:
                        if path.resolve() == active.resolve():
                            continue
                    except FileNotFoundError:
                        continue

                finalise_segment(path)

        except Exception:
            log.exception("Error in segment finalizer loop.")

        SHUTDOWN_EVENT.wait(FINALIZER_INTERVAL)

    log.info("Segment finalizer thread stopped.")


# ============================================================
# yt-dlp stderr reader
# ============================================================

def read_ytdlp_stderr(
    process: subprocess.Popen,
) -> None:

    try:
        for raw_line in process.stderr:
            if SHUTDOWN_EVENT.is_set():
                break

            line = raw_line.rstrip()

            if line:
                log.info("[yt-dlp] %s", line)

    except Exception:
        log.exception("yt-dlp stderr reader failed.")


# ============================================================
# Run one recording session
# ============================================================

def run_recording() -> bool:
    """
    Run yt-dlp + FFmpeg until the stream/session dies.

    Returns:
        True  = recording produced media successfully
        False = startup failed before media was received
    """

    global ACTIVE_SEGMENT

    log.info("")
    log.info("==============================================")
    log.info("Starting recording session")
    log.info("==============================================")

    # --------------------------------------------------------
    # yt-dlp command
    # --------------------------------------------------------

    ytdlp_command = [
        YTDLP,

        "--no-playlist",

        # Explicit dynamic format selection.
        "-f",
        FORMAT_SELECTOR,

        # yt-dlp's FFmpeg downloader for HLS.
        "--downloader",
        "ffmpeg",

        "--hls-use-mpegts",

        "--retries",
        str(YTDLP_RETRIES),

        "--fragment-retries",
        str(YTDLP_FRAGMENT_RETRIES),

        "--retry-sleep",
        "exp=1:5",

        # Stream media to stdout.
        "-o",
        "-",

        STREAM_URL,
    ]

    # --------------------------------------------------------
    # FFmpeg command
    # --------------------------------------------------------

    output_template = ARCHIVE_DIR / (
        "GreenDayTV_%Y-%m-%d_%H-%M-%S.local.mkv"
    )

    ffmpeg_command = [
        FFMPEG,

        "-hide_banner",
        "-loglevel", "warning",

        "-i",
        "pipe:0",

        "-map", "0:v:0",
        "-map", "0:a:0",

        "-c:v", "copy",
        "-c:a", "copy",

        "-f", "segment",

        "-segment_time",
        str(SEGMENT_SECONDS),

        "-reset_timestamps",
        "1",

        "-strftime",
        "1",

        "-segment_format",
        "matroska",

        str(output_template),
    ]

    log.info("Starting yt-dlp...")
    log.info("Format selector: %s", FORMAT_SELECTOR)

    try:
        ytdlp = subprocess.Popen(
            ytdlp_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            bufsize=0,
        )
    except Exception:
        log.exception("Failed to start yt-dlp.")
        return False

    log.info("Starting FFmpeg...")

    try:
        ffmpeg = subprocess.Popen(
            ffmpeg_command,
            stdin=ytdlp.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except Exception:
        log.exception("Failed to start FFmpeg.")

        try:
            ytdlp.kill()
        except Exception:
            pass

        return False

    # Parent no longer needs its copy of this descriptor.
    try:
        ytdlp.stdout.close()
    except Exception:
        pass

    # --------------------------------------------------------
    # yt-dlp stderr monitoring
    # --------------------------------------------------------

    stderr_thread = threading.Thread(
        target=read_ytdlp_stderr,
        args=(ytdlp,),
        daemon=True,
    )

    stderr_thread.start()

    # --------------------------------------------------------
    # Monitor actual segment creation / file growth.
    #
    # This is deliberately NOT based on yt-dlp's textual
    # "time=" progress output. The actual media pipeline is
    # what matters.
    # --------------------------------------------------------

    session_start = time.monotonic()

    media_seen = False
    last_total_size = 0
    last_growth_time = session_start

    while not SHUTDOWN_EVENT.is_set():

        # ----------------------------------------------------
        # Check processes
        # ----------------------------------------------------

        ytdlp_returncode = ytdlp.poll()
        ffmpeg_returncode = ffmpeg.poll()

        if ffmpeg_returncode is not None:
            log.error(
                "FFmpeg exited with return code %s.",
                ffmpeg_returncode,
            )
            break

        if ytdlp_returncode is not None:
            log.warning(
                "yt-dlp exited with return code %s.",
                ytdlp_returncode,
            )
            break

        # ----------------------------------------------------
        # Discover current local segments.
        # ----------------------------------------------------

        try:
            local_files = sorted(
                ARCHIVE_DIR.glob(
                    "GreenDayTV_*.local.mkv"
                ),
                key=lambda p: p.stat().st_mtime,
            )

            if local_files:
                current = local_files[-1]

                set_active_segment(current)

                total_size = sum(
                    p.stat().st_size
                    for p in local_files
                    if p.exists()
                )

                now = time.monotonic()

                if total_size > last_total_size:
                    media_seen = True
                    last_total_size = total_size
                    last_growth_time = now

        except FileNotFoundError:
            pass

        # ----------------------------------------------------
        # Startup timeout
        # ----------------------------------------------------

        now = time.monotonic()

        if not media_seen:
            if (
                now - session_start
                >= STARTUP_TIMEOUT_SECONDS
            ):
                log.error(
                    "No media data received within %s seconds.",
                    STARTUP_TIMEOUT_SECONDS,
                )
                break

        # ----------------------------------------------------
        # Media stall timeout
        # ----------------------------------------------------

        elif (
            now - last_growth_time
            >= STALL_SECONDS
        ):
            log.error(
                "Media output has not grown for %s seconds.",
                STALL_SECONDS,
            )
            break

        time.sleep(1.0)

    # --------------------------------------------------------
    # Shut down session
    # --------------------------------------------------------

    log.info("Stopping recording session...")

    # Close FFmpeg input by terminating yt-dlp first.
    try:
        if ytdlp.poll() is None:
            ytdlp.terminate()
    except Exception:
        pass

    # Give yt-dlp a moment to terminate cleanly.
    try:
        ytdlp.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            ytdlp.kill()
        except Exception:
            pass

    # FFmpeg should then see EOF on stdin.
    try:
        ffmpeg.wait(timeout=10)
    except subprocess.TimeoutExpired:
        log.warning(
            "FFmpeg did not exit cleanly; terminating it."
        )

        try:
            ffmpeg.terminate()
        except Exception:
            pass

        try:
            ffmpeg.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                ffmpeg.kill()
            except Exception:
                pass

    # The current segment is no longer active once FFmpeg has
    # definitely exited. At this point the finalizer may process it.
    set_active_segment(None)

    # Drain/finish stderr reader.
    try:
        stderr_thread.join(timeout=2)
    except Exception:
        pass

    if media_seen:
        log.info("Recording session ended after receiving media.")
        return True

    log.warning("Recording session ended without receiving media.")
    return False


# ============================================================
# Main loop
# ============================================================

def main() -> None:
    if not YTDLP:
        raise RuntimeError(
            "yt-dlp executable was not found in PATH."
        )

    if not FFMPEG:
        raise RuntimeError(
            "FFmpeg executable was not found in PATH."
        )

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    mutex_handle = acquire_single_instance()

    if mutex_handle is None:
        log.error(
            "Another Green Day TV archiver instance is already running."
        )
        log.error(
            "This instance will now exit."
        )
        return

    try:
        log.info("yt-dlp version:")

        try:
            version_result = subprocess.run(
                [YTDLP, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )

            version = version_result.stdout.strip()

            if version:
                log.info("%s", version)

        except Exception:
            log.warning(
                "Could not determine yt-dlp version."
            )

        log.info("==============================================")
        log.info("Green Day TV archiver")
        log.info("==============================================")

        log.info(
            "Archive directory: %s",
            ARCHIVE_DIR,
        )

        log.info(
            "Stream: %s",
            STREAM_URL,
        )

        log.info(
            "Format selector: %s",
            FORMAT_SELECTOR,
        )

        log.info(
            "Target video: up to 1920x1080, H.264"
        )

        log.info(
            "Target audio: best available audio"
        )

        log.info(
            "Segment duration: %s seconds",
            SEGMENT_SECONDS,
        )

        log.info(
            "Video encoding: stream copy"
        )

        log.info(
            "Audio encoding: stream copy"
        )

        log.info(
            "Media stall timeout: %s seconds",
            STALL_SECONDS,
        )

        log.info("")

        # Start finalizer.
        finalizer_thread = threading.Thread(
            target=segment_finaliser_loop,
            daemon=True,
        )

        finalizer_thread.start()

        # ----------------------------------------------------
        # Recovery loop
        # ----------------------------------------------------

        while not SHUTDOWN_EVENT.is_set():

            # Disk-space protection.
            try:
                free_gb = free_space_gb(ARCHIVE_DIR)

                log.info(
                    "Free disk space: %.2f GB",
                    free_gb,
                )

                if free_gb < MIN_FREE_GB:
                    log.error(
                        "Free disk space has fallen below "
                        "%.2f GB. Stopping archiver.",
                        MIN_FREE_GB,
                    )
                    break

            except Exception:
                log.exception(
                    "Unable to check free disk space."
                )

            run_recording()

            if SHUTDOWN_EVENT.is_set():
                break

            log.info(
                "Reconnecting in %s seconds...",
                RETRY_SECONDS,
            )

            SHUTDOWN_EVENT.wait(RETRY_SECONDS)

    except KeyboardInterrupt:
        log.info("Keyboard interrupt received.")

    finally:
        SHUTDOWN_EVENT.set()

        set_active_segment(None)

        log.info("Shutdown requested...")

        try:
            finalizer_thread.join(timeout=5)
        except Exception:
            pass

        release_single_instance(mutex_handle)

        log.info("Archiver stopped.")


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    main()
