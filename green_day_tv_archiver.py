from __future__ import annotations

import logging
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


# ============================================================
# Green Day TV 24/7 Archiver
#
# Windows / Python 3.10+
#
# Architecture:
#
# yt-dlp -> YouTube extraction + HLS download through FFmpeg
#        -> MPEG-TS stream through stdout
#
# FFmpeg -> receives MPEG-TS through stdin
#        -> direct stream copy
#        -> hourly MKV segmentation
#
# NO VIDEO OR AUDIO RE-ENCODING
# ============================================================


# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

STREAM_URL = "https://www.youtube.com/watch?v=Xu90G4oFq2o" # GREEN DAY TV

ARCHIVE_DIR = Path(r"D:\GreenDayTV") # CHANGE THIS TO THE DIRECTORY YOU WANT TO ARCHIVE THE STREAM TO.

SEGMENT_SECONDS = 60 * 60 # Save everything to file every 1 hour (60 seconds * 60 minutes)

FORMAT_ID = "96" # Every youtube video quality format has an ID. '96' is the highest-quality available format for GDTV. It may change depending on the stream.

MIN_FREE_GB = 20 # Minimum space (GBs) required on target disk (either main or external, set by ARCHIVE_DIR) to allow archiving.

RETRY_SECONDS = 18


# ------------------------------------------------------------
# Global shutdown flag
# ------------------------------------------------------------

STOP_REQUESTED = False


# ------------------------------------------------------------
# Signal handling
# ------------------------------------------------------------

def request_stop(signum, frame):
    global STOP_REQUESTED

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

    usage = shutil.disk_usage(ARCHIVE_DIR)

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
# Run one recording session
# ------------------------------------------------------------

def run_recording():
    ffmpeg = shutil.which("ffmpeg")

    if ffmpeg is None:
        raise RuntimeError(
            "FFmpeg was not found in PATH."
        )

    logging.info("")
    logging.info("==============================================")
    logging.info("Starting recording session")
    logging.info("==============================================")

    logging.info(
        "Format: %s",
        FORMAT_ID,
    )

    logging.info(
        "Segmentation: every %d seconds",
        SEGMENT_SECONDS,
    )

    logging.info(
        "Encoding: NONE (video/audio stream copy)",
    )

    # --------------------------------------------------------
    # yt-dlp
    #
    # This deliberately mirrors the known-good command:
    #
    # python -m yt_dlp -f 96 --downloader ffmpeg ...
    #
    # Instead of giving FFmpeg the YouTube URL ourselves,
    # yt-dlp writes the successfully downloaded MPEG-TS stream
    # to stdout.
    # --------------------------------------------------------

    ytdlp_command = [
        sys.executable,
        "-m",
        "yt_dlp",

        "-f",
        FORMAT_ID,

        "--downloader",
        "ffmpeg",

        "--hls-use-mpegts",

        "-o",
        "-",

        STREAM_URL,
    ]

    # --------------------------------------------------------
    # FFmpeg
    #
    # Receives the MPEG-TS stream from yt-dlp's stdout.
    # --------------------------------------------------------

    output_template = str(
        ARCHIVE_DIR
        / "GreenDayTV_%Y-%m-%d_%H-%M-%S.mkv"
    )

    ffmpeg_command = [
        ffmpeg,

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

    logging.info("Starting yt-dlp...")

    ytdlp_process = subprocess.Popen(
        ytdlp_command,
        stdout=subprocess.PIPE,
    )

    logging.info("Starting FFmpeg...")

    ffmpeg_process = subprocess.Popen(
        ffmpeg_command,
        stdin=ytdlp_process.stdout,
    )

    # The parent process no longer needs its copy of this
    # pipe handle. Closing it here also allows FFmpeg to see
    # EOF correctly when yt-dlp exits.
    ytdlp_process.stdout.close()

    # Wait for FFmpeg to finish consuming the stream.
    ffmpeg_return_code = ffmpeg_process.wait()

    # yt-dlp should normally terminate when its stdout pipe
    # is closed by FFmpeg.
    ytdlp_return_code = ytdlp_process.wait()

    logging.warning(
        "FFmpeg exited with return code %s.",
        ffmpeg_return_code,
    )

    logging.warning(
        "yt-dlp exited with return code %s.",
        ytdlp_return_code,
    )

    return ffmpeg_return_code, ytdlp_return_code


# ------------------------------------------------------------
# Main loop
# ------------------------------------------------------------

def main():
    global STOP_REQUESTED

    ARCHIVE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.StreamHandler(),

            logging.FileHandler(
                ARCHIVE_DIR / "archive.log",
                encoding="utf-8",
            ),
        ],
    )

    signal.signal(
        signal.SIGINT,
        request_stop,
    )

    if hasattr(signal, "SIGTERM"):
        signal.signal(
            signal.SIGTERM,
            request_stop,
        )

    if shutil.which("ffmpeg") is None:
        logging.error(
            "FFmpeg is not available in PATH."
        )

        sys.exit(1)

    if shutil.which("deno") is None:
        logging.error(
            "Deno is not available in PATH."
        )

        sys.exit(1)

    logging.info("==============================================")
    logging.info("Green Day TV archiver")
    logging.info("==============================================")

    logging.info(
        "Archive directory: %s",
        ARCHIVE_DIR,
    )

    logging.info(
        "YouTube format: %s",
        FORMAT_ID,
    )

    logging.info(
        "Segment duration: %d seconds",
        SEGMENT_SECONDS,
    )

    logging.info(
        "Video encoding: stream copy",
    )

    logging.info(
        "Audio encoding: stream copy",
    )

    logging.info("")

    while not STOP_REQUESTED:

        if not check_disk_space():
            break

        try:
            run_recording()

        except Exception as exc:
            logging.exception(
                "Recording session failed: %s",
                exc,
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

    logging.info("Archiver stopped.")


if __name__ == "__main__":
    main()
