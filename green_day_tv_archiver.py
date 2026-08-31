from __future__ import annotations

import logging
import shutil
import signal
import subprocess
import os
import sys
import re
import threading
import time
from pathlib import Path

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ============================================================
# Green Day TV 24/7 Archiver
#
# Windows / Python 3.10+
#
# yt-dlp:
#   YouTube extraction + HLS download through FFmpeg
#   -> MPEG-TS through stdout
#
# FFmpeg:
#   stdin -> direct stream copy -> hourly MKV files
#
# No video/audio re-encoding.
#
# One recording session contains exactly:
#
#   yt-dlp -> OS pipe -> FFmpeg
#
# If either side fails, BOTH are restarted together.
# There is never more than one active session.
# ============================================================


# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

# Your local timezone. Needed to convert timestamps UTC.
# Change this if necessary.
# (Obviously) this will never be saved anywhere.
LOCAL_TIMEZONE = ZoneInfo("Europe/Rome")

STREAM_URL = (
    "https://www.youtube.com/watch?v=Xu90G4oFq2o" # GDTV
)

# Where to store the archived footage files. Change this is you want.
# I STRONGLY suggest using an external drive to not fill up/slow down your main drive.
ARCHIVE_DIR = Path(
    r"D:\Pietro\Archives\GreenDayTV"
)

# Default length of one source file: 1 hour (60 seconds * 60 minutes).
# I suggest keeping this under 3 hours, with 5 hours as a maximum.
#  this is to avoid chances of corruption
#  and preventing the program from slowing down
#  due to trying to append to an enormous file.
#
# To change this to 2 hours, for example, use 2 * 60 * 60.
SEGMENT_SECONDS = 60 * 60

# Minimum amount of GB to always leave available on the archive destination's drive.
# Note that this is constantly checked for by the program
#  and it will stop archiving if this limit is reached.
MIN_FREE_GB = 10

# Number of seconds to wait before re-connecting to YouTube after an HTTP error.
# Please keep this higher than 5 (seconds) as otherwise it may result in too many requests
#   if this happens, you ISP might slow you down.
RETRY_SECONDS = 6.5

# If yt-dlp produces no download progress for this long after
# media has started, restart the complete recording session.
#
# HLS fragments here are ~5 seconds, so 12 seconds gives plenty
# of tolerance without allowing a dead session to sit forever.
# Please do not set this any lower than 6.5
STALL_SECONDS = 12

# Maximum time allowed for yt-dlp to start producing media.
STARTUP_TIMEOUT_SECONDS = 25

# Global FFmpeg executable
FFMPEG = shutil.which("ffmpeg")


# ------------------------------------------------------------
# Global shutdown/recodring flags
# ------------------------------------------------------------

STOP_REQUESTED = False
RECORDING_ACTIVE = False

# Shell util to run commands
def run(command: list[str]):
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


# ------------------------------------------------------------
# Signal handling
# ------------------------------------------------------------

def request_stop(signum, frame):
    global STOP_REQUESTED

    if not STOP_REQUESTED:
        STOP_REQUESTED = True
        logging.info("Shutdown requested...")


# ------------------------------------------------------------
# Disk-space check
# ------------------------------------------------------------

def check_disk_space():
    ARCHIVE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    usage = shutil.disk_usage(
        ARCHIVE_DIR
    )

    free_gb = usage.free / (1024 ** 3)

    if free_gb < MIN_FREE_GB:
        logging.error(
            "Only %.1f GB remains on the archive SSD. "
            "Stopping.",
            free_gb,
        )
        return False

    return True


# ------------------------------------------------------------
# Process-tree termination
# ------------------------------------------------------------

def terminate_process(process, name):
    if process is None:
        return

    if process.poll() is not None:
        return

    logging.info(
        "Terminating %s (PID %d)...",
        name,
        process.pid,
    )

    try:
        subprocess.run(
            [
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except Exception:
        logging.exception(
            "Failed to terminate %s.",
            name,
        )


# ------------------------------------------------------------
# yt-dlp stderr reader
#
# This has two jobs:
#
# 1. Keep stderr drained so yt-dlp can never block because its
#    stderr pipe became full.
#
# 2. Track the latest download-progress message so the main
#    watchdog can detect a genuinely dead HLS session.
# ------------------------------------------------------------

def read_ytdlp_stderr(
    process,
    state,
):
    if process.stderr is None:
        return

    try:
        for raw_line in iter(
            process.stderr.readline,
            b"",
        ):
            if not raw_line:
                break

            line = raw_line.decode(
                "utf-8",
                errors="replace",
            ).rstrip()

            if not line:
                continue

            logging.info(
                "[yt-dlp] %s",
                line,
            )

            # yt-dlp prints a progress line containing "time="
            # while actual media is being delivered.
            if "time=" in line:
                state["last_progress"] = (
                    time.monotonic()
                )
                state["started"] = True

    except Exception:
        if not STOP_REQUESTED:
            logging.exception(
                "yt-dlp stderr reader failed."
            )


# ------------------------------------------------------------
# FFmpeg stderr reader
#
# FFmpeg is intentionally allowed to print all its diagnostics
# into our log, but its stderr is also drained continuously.
# ------------------------------------------------------------

def read_ffmpeg_stderr(process):
    if process.stderr is None:
        return

    try:
        for raw_line in iter(
            process.stderr.readline,
            b"",
        ):
            if not raw_line:
                break

            line = raw_line.decode(
                "utf-8",
                errors="replace",
            ).rstrip()

            if line:
                logging.warning(
                    "[FFmpeg] %s",
                    line,
                )

    except Exception:
        if not STOP_REQUESTED:
            logging.exception(
                "FFmpeg stderr reader failed."
            )


# ------------------------------------------------------------
# Run one complete recording session
# ------------------------------------------------------------

def finalise_segment(path: Path, ffmpeg):
    match = re.match(
        r"^GreenDayTV_(\d{4}-\d{2}-\d{2})_"
        r"(\d{2}-\d{2}-\d{2})\.local\.mkv$",
        path.name,
    )

    if match is None:
        return

    date_part, time_part = match.groups()

    local_start = datetime.strptime(
        f"{date_part} {time_part}",
        "%Y-%m-%d %H-%M-%S",
    ).replace(
        tzinfo=LOCAL_TIMEZONE
    )

    utc_start = local_start.astimezone(
        timezone.utc
    )

    utc_timestamp = utc_start.strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    output = path.with_name(
        f"GreenDayTV_{utc_start:%Y-%m-%d}_"
        f"{utc_start:%H-%M-%S}.mkv"
    )

    temporary_output = path.with_name(
        f".{output.name}.tmp.mkv"
    )

    result = run([
        ffmpeg,
        "-y",
        "-i", str(path),
        "-map", "0",
        "-c", "copy",
        "-metadata",
        f"comment={utc_timestamp}",
        "-metadata",
        f"creation_time={utc_timestamp}",
        str(temporary_output),
    ])

    if result.returncode != 0:
        logging.error(
            "Failed to finalise segment %s:\n%s",
            path,
            result.stderr,
        )

        temporary_output.unlink(
            missing_ok=True
        )

        return

    path.unlink()

    temporary_output.replace(
        output
    )
    
    
def segment_finaliser_loop():
    global RECORDING_ACTIVE, FFMPEG

    while not STOP_REQUESTED:
        try:
            paths = sorted(
                ARCHIVE_DIR.glob(
                    "GreenDayTV_*.local.mkv"
                )
            )

            if RECORDING_ACTIVE and paths:
                paths = paths[:-1]

            for path in paths:
                finalise_segment(
                    path,
                    FFMPEG,
                )

        except Exception:
            if not STOP_REQUESTED:
                logging.exception(
                    "Segment finalizer failed."
                )

        time.sleep(1)
        

def wait_until_stable(path: Path):
    try:
        size1 = path.stat().st_size
        time.sleep(0.5)
        size2 = path.stat().st_size
    except FileNotFoundError:
        return False

    return size1 == size2
        

def run_recording():
    global RECORDING_ACTIVE
    
    if FFMPEG is None:
        raise RuntimeError(
            "FFmpeg was not found in PATH."
        )

    logging.info("")
    logging.info(
        "=============================================="
    )
    logging.info(
        "Starting recording session"
    )
    logging.info(
        "=============================================="
    )

    # --------------------------------------------------------
    # yt-dlp
    # --------------------------------------------------------

    ytdlp_command = [
        sys.executable,
        "-m",
        "yt_dlp",

        "--downloader",
        "ffmpeg",

        "--hls-use-mpegts",

        "--retries",
        "3",

        "--fragment-retries",
        "3",

        "-o",
        "-",

        STREAM_URL,
    ]

    # --------------------------------------------------------
    # FFmpeg
    # --------------------------------------------------------

    output_template = str(
        ARCHIVE_DIR
        / "GreenDayTV_%Y-%m-%d_%H-%M-%S.local.mkv"
    )

    ffmpeg_command = [
        FFMPEG,

        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",

        "-i",
        "pipe:0",

        "-map",
        "0:v:0",

        "-map",
        "0:a:0",

        "-c:v",
        "copy",

        "-c:a",
        "copy",

        "-f",
        "segment",

        "-segment_time",
        str(SEGMENT_SECONDS),

        "-reset_timestamps",
        "1",

        "-strftime",
        "1",

        "-segment_format",
        "matroska",

        output_template,
    ]

    state = {
        "started": False,
        "last_progress": time.monotonic(),
    }

    ytdlp_process = None
    ffmpeg_process = None

    ytdlp_stderr_thread = None
    ffmpeg_stderr_thread = None

    try:
        # ----------------------------------------------------
        # Start yt-dlp.
        # ----------------------------------------------------

        logging.info("Starting yt-dlp...")

        ytdlp_process = subprocess.Popen(
            ytdlp_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        # ----------------------------------------------------
        # Start FFmpeg with yt-dlp's stdout directly connected
        # to FFmpeg's stdin.
        # ----------------------------------------------------

        logging.info("Starting FFmpeg...")

        ffmpeg_process = subprocess.Popen(
            ffmpeg_command,
            stdin=ytdlp_process.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        
        RECORDING_ACTIVE = True

        # The parent no longer needs this handle.
        ytdlp_process.stdout.close()

        # ----------------------------------------------------
        # Drain both stderr pipes continuously.
        # ----------------------------------------------------

        ytdlp_stderr_thread = threading.Thread(
            target=read_ytdlp_stderr,
            args=(
                ytdlp_process,
                state,
            ),
            daemon=True,
            name="yt-dlp-stderr",
        )

        ffmpeg_stderr_thread = threading.Thread(
            target=read_ffmpeg_stderr,
            args=(ffmpeg_process,),
            daemon=True,
            name="ffmpeg-stderr",
        )

        ytdlp_stderr_thread.start()
        ffmpeg_stderr_thread.start()

        # ----------------------------------------------------
        # Watch the complete pipeline.
        # ----------------------------------------------------

        session_start = time.monotonic()

        while not STOP_REQUESTED:
            now = time.monotonic()

            # ------------------------------------------------
            # FFmpeg unexpectedly died.
            # ------------------------------------------------

            ffmpeg_return_code = (
                ffmpeg_process.poll()
            )

            if ffmpeg_return_code is not None:
                logging.error(
                    "FFmpeg exited with return code %s.",
                    ffmpeg_return_code,
                )

                return False

            # ------------------------------------------------
            # yt-dlp unexpectedly died.
            # ------------------------------------------------

            ytdlp_return_code = (
                ytdlp_process.poll()
            )

            if ytdlp_return_code is not None:
                logging.error(
                    "yt-dlp exited with return code %s.",
                    ytdlp_return_code,
                )

                return False

            # ------------------------------------------------
            # Startup watchdog.
            # ------------------------------------------------

            if not state["started"]:
                if (
                    now - session_start
                    >= STARTUP_TIMEOUT_SECONDS
                ):
                    logging.error(
                        "yt-dlp produced no media within "
                        "%.0f seconds.",
                        STARTUP_TIMEOUT_SECONDS,
                    )

                    return False

            # ------------------------------------------------
            # Media stall watchdog.
            # ------------------------------------------------

            elif (
                now - state["last_progress"]
                >= STALL_SECONDS
            ):
                logging.error(
                    "No yt-dlp media progress for "
                    "%.1f seconds. "
                    "Restarting complete session.",
                    now - state["last_progress"],
                )

                return False

            time.sleep(0.5)

        return STOP_REQUESTED

    finally:
        # ----------------------------------------------------
        # Stop yt-dlp first.
        #
        # This closes the source side of the pipe.
        # ----------------------------------------------------

        terminate_process(
            ytdlp_process,
            "yt-dlp",
        )

        # ----------------------------------------------------
        # Give FFmpeg a brief opportunity to receive EOF and
        # finalize the current MKV cleanly.
        # ----------------------------------------------------

        if (
            ffmpeg_process is not None
            and ffmpeg_process.poll() is None
        ):
            try:
                ffmpeg_process.wait(
                    timeout=5
                )
            except subprocess.TimeoutExpired:
                pass

        # ----------------------------------------------------
        # If FFmpeg is still running, terminate it.
        # ----------------------------------------------------

        terminate_process(
            ffmpeg_process,
            "FFmpeg",
        )

        # ----------------------------------------------------
        # Reap processes.
        # ----------------------------------------------------

        if ytdlp_process is not None:
            try:
                ytdlp_process.wait(
                    timeout=5
                )
            except subprocess.TimeoutExpired:
                terminate_process(
                    ytdlp_process,
                    "yt-dlp",
                )

        if ffmpeg_process is not None:
            try:
                ffmpeg_process.wait(
                    timeout=5
                )
            except subprocess.TimeoutExpired:
                terminate_process(
                    ffmpeg_process,
                    "FFmpeg",
                )
                
        RECORDING_ACTIVE = False

        # ----------------------------------------------------
        # Allow stderr readers to finish.
        # ----------------------------------------------------

        if ytdlp_stderr_thread is not None:
            ytdlp_stderr_thread.join(
                timeout=2
            )

        if ffmpeg_stderr_thread is not None:
            ffmpeg_stderr_thread.join(
                timeout=2
            )


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main():
    global STOP_REQUESTED

    ARCHIVE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s | "
            "%(levelname)s | "
            "%(message)s"
        ),
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                ARCHIVE_DIR / "archive.log",
                encoding="utf-8",
            ),
        ],
    )

    # --------------------------------------------------------
    # Signals
    # --------------------------------------------------------

    signal.signal(
        signal.SIGINT,
        request_stop,
    )

    if hasattr(signal, "SIGTERM"):
        signal.signal(
            signal.SIGTERM,
            request_stop,
        )

    # --------------------------------------------------------
    # Dependencies
    # --------------------------------------------------------

    if shutil.which("ffmpeg") is None:
        logging.error(
            "FFmpeg is not available in PATH."
        )
        return 1

    if shutil.which("deno") is None:
        logging.error(
            "Deno is not available in PATH."
        )
        return 1

    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "yt_dlp",
                "--version",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=True,
        )

        logging.info(
            "yt-dlp version: %s",
            result.stdout.strip(),
        )

    except Exception:
        logging.exception(
            "Unable to run yt-dlp."
        )
        return 1

    # --------------------------------------------------------
    # Information
    # --------------------------------------------------------

    logging.info(
        "=============================================="
    )
    logging.info(
        "Green Day TV archiver"
    )
    logging.info(
        "=============================================="
    )

    logging.info(
        "Archive directory: %s",
        ARCHIVE_DIR,
    )

    logging.info(
        "YouTube format: 1080p H.264 + AAC",
    )

    logging.info(
        "Segment duration: %d seconds",
        SEGMENT_SECONDS,
    )

    logging.info(
        "Video encoding: stream copy"
    )

    logging.info(
        "Audio encoding: stream copy"
    )

    logging.info(
        "Media stall timeout: %d seconds",
        STALL_SECONDS,
    )

    logging.info("")
    
    
    
    finaliser_thread = threading.Thread(
        target=segment_finaliser_loop,
        daemon=True,
        name="segment-finaliser",
    )

    finaliser_thread.start()

    # --------------------------------------------------------
    # Continuous recording loop.
    # --------------------------------------------------------

    while not STOP_REQUESTED:

        if not check_disk_space():
            break

        try:
            clean_shutdown = (
                run_recording()
            )

            if clean_shutdown:
                break

        except Exception:
            logging.exception(
                "Recording session failed."
            )

        if STOP_REQUESTED:
            break

        logging.info(
            "Reconnecting in %d seconds...",
            RETRY_SECONDS,
        )

        for _ in range(RETRY_SECONDS):
            if STOP_REQUESTED:
                break

            time.sleep(1)

    logging.info(
        "Archiver stopped."
    )

    return 0


# ------------------------------------------------------------
# Entry point
# ------------------------------------------------------------

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        STOP_REQUESTED = True
        logging.info("Interrupted by user.")
