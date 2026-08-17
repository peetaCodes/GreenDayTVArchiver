from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

"""
 ============================================================
 Green Day TV 24/7 Archiver

 Windows / Python 3.10+

 Architecture:

                  YouTube
                     │
                     ▼
                  yt-dlp
             format 96 / HLS
                     │
                     ▼
             yt-dlp's FFmpeg
              HLS downloader
                     │
                     │ MPEG-TS
                     ▼
             Python TCP relay
                     │
                     │ continuous TCP stream
                     ▼
                  FFmpeg
              -c:v copy
              -c:a copy
                     │
                     ▼
              hourly MKV files

 FFmpeg is started once and remains alive.

 yt-dlp is periodically restarted so that YouTube's
 short-lived signed HLS URLs never get close to expiring.

 No video or audio is ever re-encoded.
 ============================================================
 """







# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

# Green Day TV
STREAM_URL = "https://www.youtube.com/watch?v=Xu90G4oFq2o"

# Archive destination. CHANGE THIS TO AN ACTUAL PATH (I strongly suggest using an external drive to not fill up/slow down your main drive).
ARCHIVE_DIR = Path(r"D:\Pietro\Archives\GreenDayTV") 

# One new archive file approximately every hour.
SEGMENT_SECONDS = 60 * 60

# Every youtube video quality format has an ID. '96' is the highest-quality available format for GDTV. It may change depending on the stream.
FORMAT_ID = "96"

# Stop before the SSD becomes completely full.
# NOTE: The script NEVER deletes recordings automatically.
MIN_FREE_GB = 10




# ------------------------------------------------------------
# yt-dlp refresh/watchdog settings
# ------------------------------------------------------------

# Proactively replace the YouTube HLS session before its
# signed URL is likely to expire.
#
# My previous URL showed approximately six-hour validity.
# 5.5 hours leaves a good safety margin.
PRODUCER_REFRESH_SECONDS = 5 * 60 * 60 + 30 * 60

# If no bytes have arrived from yt-dlp for this long,
# consider the producer stalled and restart it.
#
# The HLS segments themselves are only ~5 seconds long, so
# 45 seconds without data is (intentionally) very generous.
PRODUCER_STALL_SECONDS = 45

# Small delay before starting a replacement producer.
PRODUCER_RESTART_DELAY_SECONDS = 1

# Local TCP relay address. You micht need to change this depending on your location.
RELAY_HOST = "127.0.0.1"

# Use a high, non-privileged local port.
RELAY_PORT = 48731

# Size of chunks copied from yt-dlp to the relay.
RELAY_CHUNK_SIZE = 256 * 1024



# ------------------------------------------------------------
# Global state
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

def check_disk_space() -> bool:
    ARCHIVE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    usage = shutil.disk_usage(ARCHIVE_DIR)

    free_gb = usage.free / (1024 ** 3)

    if free_gb < MIN_FREE_GB:
        logging.error(
            "Only %.1f GB remains on the archive SSD. "
            "Stopping to protect the filesystem.",
            free_gb,
        )

        return False

    return True


# ------------------------------------------------------------
# Dependency checks
# ------------------------------------------------------------

def check_dependencies():
    if shutil.which("ffmpeg") is None:
        logging.error(
            "FFmpeg was not found in PATH."
        )
        
        logging.error(
            "Install FFmpeg (if not already installed) and add to PATH."
            "If you don't know how to add something to PATH (or what that even means), please search for any guide online."
        )

        sys.exit(1)

    if shutil.which("deno") is None:
        logging.error(
            "Deno was not found in PATH."
        )

        logging.error(
            "Install Deno and open a new terminal before "
            "starting the archiver."
        )

        sys.exit(1)


# ------------------------------------------------------------
# Windows process-tree termination
# ------------------------------------------------------------

def terminate_process_tree(process: subprocess.Popen):
    """
    yt-dlp itself launches FFmpeg for HLS downloading.

    On Windows, killing only the yt-dlp process can leave its
    child FFmpeg process alive.

    Therefore we terminate the entire process tree.
    """

    if process.poll() is not None:
        return

    pid = process.pid

    logging.info(
        "Terminating yt-dlp process tree (PID %s)...",
        pid,
    )

    try:
        subprocess.run(
            [
                "taskkill",
                "/PID",
                str(pid),
                "/T",
                "/F",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    except Exception:
        logging.exception(
            "Unable to terminate yt-dlp process tree normally."
        )


# ------------------------------------------------------------
# yt-dlp command
# ------------------------------------------------------------

def build_ytdlp_command():
    """
    Build the yt-dlp command responsible for obtaining
    Green Day TV format 96.

    stdout is the MPEG-TS media stream.

    stderr remains available for logging.
    """

    return [
        sys.executable,

        "-m",
        "yt_dlp",

        # Explicit format 96.
        "-f",
        FORMAT_ID,

        # FFmpeg is the HLS downloader.
        "--downloader",
        "ffmpeg",

        # Produce MPEG-TS rather than attempting to create
        # a normal completed output file.
        "--hls-use-mpegts",

        # Retry network/fragment failures indefinitely.
        #
        # The watchdog below handles pathological hangs.
        "--retries",
        "infinite",

        "--fragment-retries",
        "infinite",

        # Don't buffer a completed output file; write the
        # downloaded media to stdout.
        "-o",
        "-",

        # This is one livestream, never a playlist.
        "--no-playlist",

        # The stream URL.
        STREAM_URL,
    ]


# ------------------------------------------------------------
# Producer stderr reader
# ------------------------------------------------------------

def read_producer_stderr(
    process: subprocess.Popen,
    producer_number: int,
):
    """
    Continuously copy yt-dlp/FFmpeg downloader diagnostics
    into the main archive log.

    This runs in a background thread because stdout is being
    used for the actual MPEG-TS media data.
    """

    if process.stderr is None:
        return

    try:
        for raw_line in iter(
            process.stderr.readline,
            b"",
        ):
            if STOP_REQUESTED:
                break

            if not raw_line:
                break

            line = raw_line.decode(
                "utf-8",
                errors="replace",
            ).rstrip()

            if line:
                logging.info(
                    "[yt-dlp #%d] %s",
                    producer_number,
                    line,
                )

    except Exception:
        logging.exception(
            "Producer stderr reader failed."
        )


# ------------------------------------------------------------
# Start yt-dlp producer
# ------------------------------------------------------------

def start_producer(producer_number: int):
    command = build_ytdlp_command()

    logging.info(
        "Starting yt-dlp producer #%d...",
        producer_number,
    )

    logging.info(
        "yt-dlp format: %s",
        FORMAT_ID,
    )

    process = subprocess.Popen(
        command,

        # stdout = MPEG-TS media.
        stdout=subprocess.PIPE,

        # stderr = diagnostics.
        stderr=subprocess.PIPE,

        # Don't add unnecessary buffering on the Python side.
        bufsize=0,

        # Keep stdin detached; yt-dlp should never ask us
        # interactive questions.
        stdin=subprocess.DEVNULL,

        # On Windows this ensures the subprocess tree can
        # be terminated using taskkill /T.
        creationflags=getattr(
            subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            0,
        ),
    )

    logging.info(
        "yt-dlp producer #%d started with PID %d.",
        producer_number,
        process.pid,
    )

    stderr_thread = threading.Thread(
        target=read_producer_stderr,
        args=(
            process,
            producer_number,
        ),
        name=f"yt-dlp-stderr-{producer_number}",
        daemon=True,
    )

    stderr_thread.start()

    return process


# ------------------------------------------------------------
# Persistent FFmpeg archive process
# ------------------------------------------------------------

def build_ffmpeg_command():
    """
    FFmpeg reads the persistent TCP MPEG-TS stream supplied
    by the Python relay.

    FFmpeg itself is never restarted during normal operation.
    """

    ffmpeg = shutil.which("ffmpeg")

    if ffmpeg is None:
        raise RuntimeError(
            "FFmpeg was not found in PATH."
        )

    output_template = str(
        ARCHIVE_DIR
        / "GreenDayTV_%Y-%m-%d_%H-%M-%S.mkv"
    )

    return [
        ffmpeg,

        "-hide_banner",
        "-loglevel",
        "warning",

        "-nostdin",

        # Persistent local TCP input

        "-f",
        "mpegts",

        "-i",
        f"tcp://{RELAY_HOST}:{RELAY_PORT}",

        # Stream selection

        "-map",
        "0:v:0",

        "-map",
        "0:a:0",

        # NO re-encoding. Stream is already encoded.

        "-c:v",
        "copy",

        "-c:a",
        "copy",

        # Segmenting

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


# ------------------------------------------------------------
# FFmpeg stderr reader
# ------------------------------------------------------------

def read_ffmpeg_stderr(
    process: subprocess.Popen,
):
    """
    Copy FFmpeg diagnostics into the archive log.
    """

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
        logging.exception(
            "FFmpeg stderr reader failed."
        )


# ------------------------------------------------------------
# Start persistent FFmpeg
# ------------------------------------------------------------

def start_ffmpeg():
    command = build_ffmpeg_command()

    logging.info(
        "Starting persistent FFmpeg archive process..."
    )

    logging.info(
        "FFmpeg input: tcp://%s:%d",
        RELAY_HOST,
        RELAY_PORT,
    )

    logging.info(
        "FFmpeg segmentation: %d seconds",
        SEGMENT_SECONDS,
    )

    logging.info(
        "FFmpeg video: stream copy",
    )

    logging.info(
        "FFmpeg audio: stream copy",
    )

    process = subprocess.Popen(
        command,

        stdin=subprocess.DEVNULL,

        stdout=subprocess.DEVNULL,

        stderr=subprocess.PIPE,

        bufsize=0,
    )

    stderr_thread = threading.Thread(
        target=read_ffmpeg_stderr,
        args=(process,),
        name="ffmpeg-stderr",
        daemon=True,
    )

    stderr_thread.start()

    logging.info(
        "Persistent FFmpeg started with PID %d.",
        process.pid,
    )

    return process


# ------------------------------------------------------------
# Producer -> relay copy
# ------------------------------------------------------------

def relay_producer_output(
    producer: subprocess.Popen,
    client_socket: socket.socket,
    producer_number: int,
):
    """
    Copy MPEG-TS bytes from yt-dlp's stdout to the persistent
    FFmpeg TCP connection.

    Returns:
        ("eof", last_data_time)
        ("error", last_data_time)
    """

    if producer.stdout is None:
        return "error", time.monotonic()

    last_data_time = time.monotonic()

    try:
        while not STOP_REQUESTED:

            data = producer.stdout.read(
                RELAY_CHUNK_SIZE
            )

            if not data:
                logging.warning(
                    "yt-dlp producer #%d reached EOF.",
                    producer_number,
                )

                return "eof", last_data_time

            # Send the media to the persistent FFmpeg process.
            client_socket.sendall(data)

            last_data_time = time.monotonic()

    except (BrokenPipeError, ConnectionResetError):
        logging.error(
            "FFmpeg TCP connection was lost."
        )

        return "error", last_data_time

    except OSError as exc:
        logging.error(
            "Relay socket error: %s",
            exc,
        )

        return "error", last_data_time

    except Exception:
        logging.exception(
            "Unexpected relay failure."
        )

        return "error", last_data_time


# ------------------------------------------------------------
# Producer supervisor
# ------------------------------------------------------------

def supervise_producer(
    client_socket: socket.socket,
):
    """
    Keep yt-dlp alive and periodically replace its YouTube
    HLS session.

    The FFmpeg TCP connection remains open throughout.
    """

    producer_number = 0

    while not STOP_REQUESTED:

        producer_number += 1

        producer = start_producer(
            producer_number
        )

        producer_start_time = time.monotonic()

        last_data_time = producer_start_time

        logging.info(
            "Producer #%d active.",
            producer_number,
        )

        while not STOP_REQUESTED:

            # Proactive refresh
            producer_age = (
                time.monotonic()
                - producer_start_time
            )

            if producer_age >= PRODUCER_REFRESH_SECONDS:

                logging.info(
                    "Producer #%d has been running for "
                    "%.1f hours; proactively refreshing "
                    "the YouTube HLS session.",
                    producer_number,
                    producer_age / 3600,
                )

                terminate_process_tree(
                    producer
                )

                break

            # Read a chunk of media. This call is blocking while the producer is functioning normally.
            if producer.stdout is None:
                break

            data = producer.stdout.read(
                RELAY_CHUNK_SIZE
            )

            if data:

                try:
                    client_socket.sendall(data)

                except (
                    BrokenPipeError,
                    ConnectionResetError,
                    OSError,
                ) as exc:

                    logging.error(
                        "FFmpeg relay connection lost: %s",
                        exc,
                    )

                    terminate_process_tree(
                        producer
                    )

                    return False

                last_data_time = time.monotonic()

                continue

            # EOF from yt-dlp
            return_code = producer.poll()

            if return_code is not None:

                logging.warning(
                    "yt-dlp producer #%d exited with "
                    "return code %s.",
                    producer_number,
                    return_code,
                )

                break

            # No data but process still running.  This can indicate a hung FFmpeg HLS downloader.
            idle_time = (
                time.monotonic()
                - last_data_time
            )

            if idle_time >= PRODUCER_STALL_SECONDS:

                logging.warning(
                    "yt-dlp producer #%d has produced "
                    "no media data for %.1f seconds.",
                    producer_number,
                    idle_time,
                )

                logging.warning(
                    "Assuming the YouTube HLS downloader "
                    "is stalled; restarting it."
                )

                terminate_process_tree(
                    producer
                )

                break

            time.sleep(0.1)

        # Clean up producer
        if producer.poll() is None:
            terminate_process_tree(
                producer
            )

        try:
            producer.wait(
                timeout=10
            )
        except subprocess.TimeoutExpired:
            logging.error(
                "yt-dlp did not terminate after taskkill."
            )

        if STOP_REQUESTED:
            break

        logging.info(
            "Starting replacement yt-dlp producer "
            "in %d second...",
            PRODUCER_RESTART_DELAY_SECONDS,
        )

        for _ in range(
            PRODUCER_RESTART_DELAY_SECONDS
        ):

            if STOP_REQUESTED:
                break

            time.sleep(1)

    return True


# ------------------------------------------------------------
# Relay server
# ------------------------------------------------------------

def create_relay_server():
    """
    Create a persistent local TCP endpoint.

    FFmpeg connects once and stays connected.

    The producer can then be replaced underneath it.
    """

    server = socket.socket(
        socket.AF_INET,
        socket.SOCK_STREAM,
    )

    # Allow immediate reuse if the program is restarted.
    server.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_REUSEADDR,
        1,
    )

    server.bind(
        (
            RELAY_HOST,
            RELAY_PORT,
        )
    )

    server.listen(1)

    logging.info(
        "Relay listening on tcp://%s:%d",
        RELAY_HOST,
        RELAY_PORT,
    )

    return server


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

    signal.signal(
        signal.SIGINT,
        request_stop,
    )

    if hasattr(signal, "SIGTERM"):
        signal.signal(
            signal.SIGTERM,
            request_stop,
        )

    check_dependencies()

    logging.info(
        "=============================================="
    )

    logging.info(
        "Green Day TV 24/7 archiver"
    )

    logging.info(
        "=============================================="
    )

    logging.info(
        "Archive directory: %s",
        ARCHIVE_DIR,
    )

    logging.info(
        "YouTube stream: %s",
        STREAM_URL,
    )

    logging.info(
        "YouTube format: %s",
        FORMAT_ID,
    )

    logging.info(
        "Format: 1920x1080 @ 30 FPS, H.264 + AAC"
    )

    logging.info(
        "Segment duration: %d seconds",
        SEGMENT_SECONDS,
    )

    logging.info(
        "Video encoding: NONE"
    )

    logging.info(
        "Audio encoding: NONE"
    )

    logging.info(
        "Producer refresh: %.1f hours",
        PRODUCER_REFRESH_SECONDS / 3600,
    )

    logging.info(
        "Producer stall timeout: %.1f seconds",
        PRODUCER_STALL_SECONDS,
    )

    logging.info("")


    if not check_disk_space():
        return

    relay_server = create_relay_server()

    ffmpeg_process = start_ffmpeg()
    logging.info(
        "Waiting for FFmpeg to connect to the relay..."
    )

    relay_server.settimeout(30)

    try:
        client_socket, client_address = relay_server.accept()
        
    except socket.timeout:
        logging.error(
            "FFmpeg did not connect to the relay within 30 seconds."
        )

        if ffmpeg_process.poll() is None:
            ffmpeg_process.terminate()

        relay_server.close()

        return

    finally:
        relay_server.settimeout(None)
        

    logging.info(
        "FFmpeg connected from %s:%s.",
        client_address[0],
        client_address[1],
    )

    # Larger socket buffers help absorb small network/restart
    # interruptions without involving FFmpeg itself.
    try:
        client_socket.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_SNDBUF,
            4 * 1024 * 1024,
        )

        client_socket.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_RCVBUF,
            4 * 1024 * 1024,
        )

    except OSError:
        pass

    try:

        supervise_producer(
            client_socket
        )

    except Exception:
        logging.exception(
            "Producer supervisor failed."
        )

    logging.info(
        "Closing relay connection..."
    )

    try:
        client_socket.shutdown(
            socket.SHUT_RDWR
        )
    except OSError:
        pass

    try:
        client_socket.close()
    except OSError:
        pass

    try:
        relay_server.close()
    except OSError:
        pass

    # Give FFmpeg a short opportunity to finish the final
    # partial segment after receiving EOF.
    if ffmpeg_process.poll() is None:

        logging.info(
            "Stopping FFmpeg..."
        )

        try:
            ffmpeg_process.wait(
                timeout=10
            )

        except subprocess.TimeoutExpired:

            logging.warning(
                "FFmpeg did not exit normally; terminating."
            )

            ffmpeg_process.terminate()

            try:
                ffmpeg_process.wait(
                    timeout=5
                )
            except subprocess.TimeoutExpired:
                ffmpeg_process.kill()

    logging.info(
        "FFmpeg final return code: %s",
        ffmpeg_process.returncode,
    )

    logging.info(
        "Archiver stopped."
    )


if __name__ == "__main__":
    main()
