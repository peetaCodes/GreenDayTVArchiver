from __future__ import annotations

import logging
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path


"""
============================================================
Green Day TV 24/7 Archiver
============================================================

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
                    Python producer
                         reader
                            │
                     bounded queue
                            │
                            ▼
                    Python TCP relay
                            │
                            ▼
                         FFmpeg
                     stream copy only
                            │
                            ▼
                     hourly MKV files


Design goals:

* Never silently accept a dead/stalled producer.
* Detect persistent 401/403 HLS failures immediately.
* Restart yt-dlp automatically.
* Keep FFmpeg alive across producer restarts.
* Restart FFmpeg automatically if FFmpeg itself fails.
* Never allow an unbounded RAM queue.
* Never allow a blocked socket write to hang forever.
* Periodically verify free disk space.
* Use stream copy only. No re-encoding.
* Back off only when YouTube itself is unavailable.
* Continue forever until explicitly stopped or disk space
  falls below the configured safety threshold.

No program can guarantee zero loss against an external service
such as YouTube. The purpose of this architecture is instead to
make failures fail FAST and recover AUTOMATICALLY, minimizing
the amount of footage lost.
============================================================
"""


# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

# Green Day TV
STREAM_URL = "https://www.youtube.com/watch?v=Xu90G4oFq2o"

# Archive destination. CHANGE THIS TO AN ACTUAL PATH (I strongly suggest using an external drive to not fill up/slow down your main drive).
ARCHIVE_DIR = Path(r"D:\Archives\GreenDayTV") 

# One new archive file approximately every hour.
SEGMENT_SECONDS = 60 * 60

# Every youtube video quality format has an ID. '96' is the highest-quality available format for GDTV. It may change depending on the stream.
FORMAT_ID = "96"

# Stop before the SSD becomes completely full.
# NOTE: The script NEVER deletes recordings automatically.
MIN_FREE_GB = 10


# ------------------------------------------------------------
# Producer watchdog
# ------------------------------------------------------------

# Refresh the YouTube session well before a typical signed URL
# lifetime expires.
#
# This is deliberately conservative. A refresh costs a small
# amount of footage occasionally, but is much safer than waiting
# until the signed URL is already near expiry.
PRODUCER_REFRESH_SECONDS = 4 * 60 * 60 + 30 * 60


# Once media has started arriving, this much time without ANY
# bytes is considered a hard producer failure.
#
# HLS fragments are about 5 seconds here. 12 seconds therefore
# allows a couple of missed scheduling/network intervals while
# still reacting quickly.
PRODUCER_STALL_SECONDS = 12


# Maximum time allowed to obtain the first media bytes from a
# freshly started producer.
PRODUCER_STARTUP_TIMEOUT_SECONDS = 60


# A repeated 401/403 condition is treated as fatal for the
# current signed HLS session.
#
# One isolated 403 may be transient.
# Two within this window strongly indicate that the current
# HLS session/URL cannot continue successfully.
HTTP_AUTH_FAILURE_THRESHOLD = 2

HTTP_AUTH_FAILURE_WINDOW_SECONDS = 12


# Delay before replacing a failed producer.
PRODUCER_RESTART_DELAY_SECONDS = 1


# ------------------------------------------------------------
# yt-dlp retry policy
# ------------------------------------------------------------

# These are deliberately finite.
#
# They are not relied upon as the main watchdog.  The Python
# supervisor independently detects persistent 401/403 and stalls.
YTDLP_RETRIES = 2
YTDLP_FRAGMENT_RETRIES = 2


# ------------------------------------------------------------
# Relay
# ------------------------------------------------------------

RELAY_HOST = "127.0.0.1"
RELAY_PORT = 48731

RELAY_CHUNK_SIZE = 256 * 1024

QUEUE_MAX_BYTES = 64 * 1024 * 1024

QUEUE_MAX_CHUNKS = max(
    1,
    QUEUE_MAX_BYTES // RELAY_CHUNK_SIZE,
)


# socket.sendall() must never be allowed to block forever.
#
# This is local loopback communication, so 10 seconds is already
# extremely generous.
RELAY_SEND_TIMEOUT_SECONDS = 10


# ------------------------------------------------------------
# Disk monitoring
# ------------------------------------------------------------

DISK_CHECK_INTERVAL_SECONDS = 60


# ------------------------------------------------------------
# FFmpeg / overall supervisor
# ------------------------------------------------------------

# How long to wait for FFmpeg to establish the relay connection.
FFMPEG_CONNECT_TIMEOUT_SECONDS = 30


# When a complete FFmpeg/relay session has failed, don't hammer
# the system indefinitely. The delay increases progressively,
# then remains capped.
SESSION_BACKOFF_INITIAL_SECONDS = 1
SESSION_BACKOFF_MAX_SECONDS = 30


# ============================================================
# Global shutdown state
# ============================================================

STOP_EVENT = threading.Event()


# ============================================================
# HTTP failure detection
# ============================================================

HTTP_AUTH_ERROR_RE = re.compile(
    r"HTTP error (401|403)\s",
    re.IGNORECASE,
)


# ============================================================
# Producer state
# ============================================================

class ProducerState:

    def __init__(self):

        self.lock = threading.Lock()

        self.last_data_time = time.monotonic()

        self.bytes_received = 0

        self.eof = False

        self.reader_error = False

        self.reader_error_message = None

        self.fatal_reason = None

        self.http_failures = deque()

        self.start_time = time.monotonic()


    def record_data(
        self,
        byte_count: int,
    ):

        now = time.monotonic()

        with self.lock:

            self.last_data_time = now

            self.bytes_received += byte_count


    def record_http_failure(
        self,
        status_code: str,
    ):

        now = time.monotonic()

        with self.lock:

            self.http_failures.append(now)

            cutoff = (
                now
                - HTTP_AUTH_FAILURE_WINDOW_SECONDS
            )

            while (
                self.http_failures
                and self.http_failures[0] < cutoff
            ):

                self.http_failures.popleft()


            count = len(
                self.http_failures
            )


            if (
                count
                >= HTTP_AUTH_FAILURE_THRESHOLD
                and self.fatal_reason is None
            ):

                self.fatal_reason = (
                    f"repeated HTTP "
                    f"{status_code} errors "
                    f"({count} within "
                    f"{HTTP_AUTH_FAILURE_WINDOW_SECONDS:.0f}s)"
                )


    def set_eof(self):

        with self.lock:

            self.eof = True


    def set_reader_error(
        self,
        message: str,
    ):

        with self.lock:

            self.reader_error = True

            self.reader_error_message = (
                message
            )


    def snapshot(self):

        with self.lock:

            return {
                "last_data_time": (
                    self.last_data_time
                ),
                "bytes_received": (
                    self.bytes_received
                ),
                "eof": self.eof,
                "reader_error": (
                    self.reader_error
                ),
                "reader_error_message": (
                    self.reader_error_message
                ),
                "fatal_reason": (
                    self.fatal_reason
                ),
                "start_time": (
                    self.start_time
                ),
            }


# ============================================================
# Signal handling
# ============================================================

def request_stop(
    signum,
    frame,
):

    if not STOP_EVENT.is_set():

        logging.info(
            "Shutdown requested..."
        )

        STOP_EVENT.set()


# ============================================================
# Disk-space check
# ============================================================

def check_disk_space() -> bool:

    ARCHIVE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    usage = shutil.disk_usage(
        ARCHIVE_DIR
    )

    free_gb = (
        usage.free
        / (1024 ** 3)
    )

    if free_gb < MIN_FREE_GB:

        logging.error(
            "Only %.1f GB remains on "
            "the archive SSD. "
            "Stopping to protect the filesystem.",
            free_gb,
        )

        return False


    return True


# ============================================================
# Dependency checks
# ============================================================

def check_dependencies():

    if shutil.which("ffmpeg") is None:

        logging.error(
            "FFmpeg was not found in PATH."
        )

        sys.exit(1)


    if shutil.which("deno") is None:

        logging.error(
            "Deno was not found in PATH."
        )

        logging.error(
            "Install Deno and reopen the terminal."
        )

        sys.exit(1)


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

        version = result.stdout.strip()

        logging.info(
            "yt-dlp version: %s",
            version,
        )

    except Exception:

        logging.exception(
            "Could not execute yt-dlp."
        )

        sys.exit(1)


# ============================================================
# Process-tree termination
# ============================================================

def terminate_process_tree(
    process: subprocess.Popen | None,
    name: str,
):

    if process is None:
        return

    if process.poll() is not None:
        return

    pid = process.pid

    logging.info(
        "Terminating %s process tree "
        "(PID %s)...",
        name,
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
            timeout=15,
        )

    except Exception:

        logging.exception(
            "Unable to terminate %s "
            "process tree.",
            name,
        )


# ============================================================
# yt-dlp command
# ============================================================

def build_ytdlp_command():

    return [

        sys.executable,

        "-m",
        "yt_dlp",

        "-f",
        FORMAT_ID,

        # Use FFmpeg for live HLS.
        "--downloader",
        "ffmpeg",

        # Ensure MPEG-TS is used while streaming.
        "--hls-use-mpegts",

        # Finite retries.
        "--retries",
        str(YTDLP_RETRIES),

        "--fragment-retries",
        str(YTDLP_FRAGMENT_RETRIES),

        "--retry-sleep",
        "fragment:1",

        # FFmpeg network read/write timeout.
        #
        # This complements the Python watchdog.  It prevents
        # an FFmpeg HTTP operation itself from hanging forever.
        "--downloader-args",
        "ffmpeg:-rw_timeout 15000000",

        "--no-playlist",

        # MPEG-TS to stdout.
        "-o",
        "-",

        STREAM_URL,
    ]


# ============================================================
# Producer stderr reader
# ============================================================

def producer_stderr_reader(
    process: subprocess.Popen,
    producer_number: int,
    state: ProducerState,
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


            # ------------------------------------------------
            # Watch for authentication/signed-URL failures.
            #
            # This is the critical difference from the old
            # implementation.
            # ------------------------------------------------

            match = HTTP_AUTH_ERROR_RE.search(
                line
            )

            if match:

                status_code = match.group(1)

                state.record_http_failure(
                    status_code
                )


            logging.info(
                "[yt-dlp #%d] %s",
                producer_number,
                line,
            )


    except Exception:

        if not STOP_EVENT.is_set():

            logging.exception(
                "yt-dlp stderr reader failed "
                "for producer #%d.",
                producer_number,
            )


# ============================================================
# Producer stdout reader
# ============================================================

def producer_stdout_reader(
    process: subprocess.Popen,
    data_queue: queue.Queue,
    producer_number: int,
    state: ProducerState,
):

    if process.stdout is None:
        return


    try:

        while not STOP_EVENT.is_set():

            data = process.stdout.read(
                RELAY_CHUNK_SIZE
            )


            if not data:

                logging.warning(
                    "yt-dlp producer #%d "
                    "reached EOF.",
                    producer_number,
                )

                state.set_eof()

                return


            # IMPORTANT:
            #
            # Record receipt of actual media BEFORE queue.put().
            #
            # This prevents the watchdog from incorrectly thinking
            # the producer stopped producing merely because the
            # relay queue is temporarily full.
            state.record_data(
                len(data)
            )


            # ------------------------------------------------
            # Bounded queue.
            #
            # Never allow unlimited RAM growth.
            # ------------------------------------------------

            while not STOP_EVENT.is_set():

                try:

                    data_queue.put(
                        data,
                        timeout=0.5,
                    )

                    break

                except queue.Full:

                    continue


    except Exception as exc:

        if not STOP_EVENT.is_set():

            logging.exception(
                "yt-dlp stdout reader failed "
                "for producer #%d.",
                producer_number,
            )

            state.set_reader_error(
                str(exc)
            )


# ============================================================
# Start producer
# ============================================================

def start_producer(
    producer_number: int,
):

    command = build_ytdlp_command()

    logging.info(
        "Starting yt-dlp producer #%d...",
        producer_number,
    )

    process = subprocess.Popen(

        command,

        stdin=subprocess.DEVNULL,

        stdout=subprocess.PIPE,

        stderr=subprocess.PIPE,

        bufsize=0,

        creationflags=getattr(
            subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            0,
        ),
    )

    logging.info(
        "yt-dlp producer #%d started "
        "(PID %d).",
        producer_number,
        process.pid,
    )

    return process


# ============================================================
# FFmpeg stderr reader
# ============================================================

def ffmpeg_stderr_reader(
    process: subprocess.Popen,
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


            if line:

                logging.warning(
                    "[FFmpeg] %s",
                    line,
                )


    except Exception:

        if not STOP_EVENT.is_set():

            logging.exception(
                "FFmpeg stderr reader failed."
            )


# ============================================================
# FFmpeg command
# ============================================================

def build_ffmpeg_command():

    ffmpeg = shutil.which(
        "ffmpeg"
    )

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

        # ----------------------------------------------------
        # MPEG-TS over local TCP.
        # ----------------------------------------------------

        "-f",
        "mpegts",

        "-i",
        f"tcp://{RELAY_HOST}:{RELAY_PORT}",

        # ----------------------------------------------------
        # Stream selection.
        # ----------------------------------------------------

        "-map",
        "0:v:0",

        "-map",
        "0:a:0",

        # ----------------------------------------------------
        # Absolutely no re-encoding.
        # ----------------------------------------------------

        "-c:v",
        "copy",

        "-c:a",
        "copy",

        # ----------------------------------------------------
        # Hourly segmentation.
        # ----------------------------------------------------

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


# ============================================================
# Start FFmpeg
# ============================================================

def start_ffmpeg():

    command = build_ffmpeg_command()

    logging.info(
        "Starting persistent FFmpeg..."
    )

    logging.info(
        "FFmpeg input: tcp://%s:%d",
        RELAY_HOST,
        RELAY_PORT,
    )

    logging.info(
        "Segmentation: %d seconds",
        SEGMENT_SECONDS,
    )

    logging.info(
        "Video: stream copy",
    )

    logging.info(
        "Audio: stream copy",
    )


    process = subprocess.Popen(

        command,

        stdin=subprocess.DEVNULL,

        stdout=subprocess.DEVNULL,

        stderr=subprocess.PIPE,

        bufsize=0,
    )


    threading.Thread(

        target=ffmpeg_stderr_reader,

        args=(process,),

        name="ffmpeg-stderr",

        daemon=True,

    ).start()


    logging.info(
        "Persistent FFmpeg started "
        "(PID %d).",
        process.pid,
    )


    return process


# ============================================================
# Relay server
# ============================================================

def create_relay_server():

    server = socket.socket(
        socket.AF_INET,
        socket.SOCK_STREAM,
    )

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
        "Relay listening on "
        "tcp://%s:%d",
        RELAY_HOST,
        RELAY_PORT,
    )

    return server


# ============================================================
# Accept FFmpeg
# ============================================================

def accept_ffmpeg(
    relay_server: socket.socket,
):

    relay_server.settimeout(
        FFMPEG_CONNECT_TIMEOUT_SECONDS
    )


    try:

        client_socket, client_address = (
            relay_server.accept()
        )

    except socket.timeout:

        raise RuntimeError(
            "FFmpeg did not connect to "
            "the relay within "
            f"{FFMPEG_CONNECT_TIMEOUT_SECONDS} "
            "seconds."
        )

    finally:

        relay_server.settimeout(
            None
        )


    logging.info(
        "FFmpeg connected from %s:%s.",
        client_address[0],
        client_address[1],
    )


    # --------------------------------------------------------
    # Large TCP buffers are useful here, but the Python queue
    # remains independently bounded.
    # --------------------------------------------------------

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


    # IMPORTANT:
    #
    # sendall() gets a finite timeout.  This prevents a dead
    # FFmpeg process from trapping the relay forever.
    client_socket.settimeout(
        RELAY_SEND_TIMEOUT_SECONDS
    )


    return client_socket


# ============================================================
# Relay writer thread
# ============================================================

def relay_writer(
    client_socket: socket.socket,
    data_queue: queue.Queue,
    relay_state: dict,
):

    while not STOP_EVENT.is_set():

        try:

            data = data_queue.get(
                timeout=0.5
            )

        except queue.Empty:

            continue


        try:

            client_socket.sendall(
                data
            )

            with relay_state["lock"]:

                relay_state[
                    "last_successful_send"
                ] = time.monotonic()


        except (
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
            socket.timeout,
            OSError,
        ) as exc:

            with relay_state["lock"]:

                relay_state["error"] = (
                    str(exc)
                )

                relay_state["failed"] = True


            logging.error(
                "FFmpeg relay connection lost: %s",
                exc,
            )

            return


# ============================================================
# Producer supervisor
# ============================================================

def supervise_producer(
    client_socket: socket.socket,
    ffmpeg_process: subprocess.Popen,
):

    producer_number = 0

    data_queue = queue.Queue(
        maxsize=QUEUE_MAX_CHUNKS
    )


    relay_state = {

        "lock": threading.Lock(),

        "failed": False,

        "error": None,

        "last_successful_send": (
            time.monotonic()
        ),
    }


    relay_thread = threading.Thread(

        target=relay_writer,

        args=(
            client_socket,
            data_queue,
            relay_state,
        ),

        name="relay-writer",

        daemon=True,
    )

    relay_thread.start()


    last_disk_check = time.monotonic()


    while not STOP_EVENT.is_set():

        producer_number += 1

        producer = None
        state = ProducerState()


        try:

            producer = start_producer(
                producer_number
            )


            # ------------------------------------------------
            # Producer stderr
            # ------------------------------------------------

            stderr_thread = threading.Thread(

                target=producer_stderr_reader,

                args=(
                    producer,
                    producer_number,
                    state,
                ),

                name=(
                    f"yt-dlp-stderr-{producer_number}"
                ),

                daemon=True,
            )

            stderr_thread.start()


            # ------------------------------------------------
            # Producer stdout
            # ------------------------------------------------

            stdout_thread = threading.Thread(

                target=producer_stdout_reader,

                args=(
                    producer,
                    data_queue,
                    producer_number,
                    state,
                ),

                name=(
                    f"yt-dlp-stdout-{producer_number}"
                ),

                daemon=True,
            )

            stdout_thread.start()


            logging.info(
                "Producer #%d is active.",
                producer_number,
            )


            restart_reason = None


            # =================================================
            # Producer supervision loop
            # =================================================

            while not STOP_EVENT.is_set():

                now = time.monotonic()


                # ------------------------------------------------
                # Is FFmpeg itself dead?
                # ------------------------------------------------

                ffmpeg_return_code = (
                    ffmpeg_process.poll()
                )

                if (
                    ffmpeg_return_code is not None
                ):

                    restart_reason = (
                        "FFmpeg exited "
                        f"({ffmpeg_return_code})"
                    )

                    logging.error(
                        "FFmpeg exited with "
                        "return code %s.",
                        ffmpeg_return_code,
                    )

                    break


                # ------------------------------------------------
                # Did the relay fail?
                # ------------------------------------------------

                with relay_state["lock"]:

                    relay_failed = (
                        relay_state["failed"]
                    )

                    relay_error = (
                        relay_state["error"]
                    )


                if relay_failed:

                    restart_reason = (
                        "FFmpeg relay failed"
                    )

                    if relay_error:

                        logging.error(
                            "Relay failure: %s",
                            relay_error,
                        )

                    break


                # ------------------------------------------------
                # Disk-space watchdog.
                # ------------------------------------------------

                if (
                    now - last_disk_check
                    >= DISK_CHECK_INTERVAL_SECONDS
                ):

                    last_disk_check = now

                    if not check_disk_space():

                        STOP_EVENT.set()

                        restart_reason = (
                            "insufficient disk space"
                        )

                        break


                snapshot = state.snapshot()


                # ------------------------------------------------
                # Repeated 401 / 403.
                #
                # THIS is what fixes the failure shown in
                # your log.
                # ------------------------------------------------

                if snapshot["fatal_reason"]:

                    restart_reason = (
                        snapshot["fatal_reason"]
                    )

                    logging.error(
                        "Producer #%d is unhealthy: %s",
                        producer_number,
                        restart_reason,
                    )

                    break


                # ------------------------------------------------
                # Producer process exited.
                # ------------------------------------------------

                return_code = producer.poll()

                if return_code is not None:

                    restart_reason = (
                        "process exited "
                        f"({return_code})"
                    )

                    logging.warning(
                        "yt-dlp producer #%d "
                        "exited with return code %s.",
                        producer_number,
                        return_code,
                    )

                    break


                # ------------------------------------------------
                # EOF.
                # ------------------------------------------------

                if snapshot["eof"]:

                    restart_reason = (
                        "stdout EOF"
                    )

                    break


                # ------------------------------------------------
                # Reader failure.
                # ------------------------------------------------

                if snapshot["reader_error"]:

                    restart_reason = (
                        "stdout reader error"
                    )

                    logging.error(
                        "Producer #%d stdout reader "
                        "failed: %s",
                        producer_number,
                        snapshot[
                            "reader_error_message"
                        ],
                    )

                    break


                # ------------------------------------------------
                # Scheduled refresh.
                # ------------------------------------------------

                age = (
                    now
                    - snapshot["start_time"]
                )


                if (
                    age
                    >= PRODUCER_REFRESH_SECONDS
                ):

                    restart_reason = (
                        "scheduled refresh"
                    )

                    logging.info(
                        "Producer #%d reached "
                        "%.2f hours; refreshing "
                        "YouTube session.",
                        producer_number,
                        age / 3600,
                    )

                    break


                # ------------------------------------------------
                # Media watchdog.
                # ------------------------------------------------

                if snapshot["bytes_received"] == 0:

                    startup_idle = (
                        now
                        - snapshot["start_time"]
                    )


                    if (
                        startup_idle
                        >= PRODUCER_STARTUP_TIMEOUT_SECONDS
                    ):

                        restart_reason = (
                            "producer never produced "
                            "media"
                        )

                        logging.error(
                            "Producer #%d produced no "
                            "media for %.1f seconds.",
                            producer_number,
                            startup_idle,
                        )

                        break


                else:

                    media_idle = (
                        now
                        - snapshot[
                            "last_data_time"
                        ]
                    )


                    if (
                        media_idle
                        >= PRODUCER_STALL_SECONDS
                    ):

                        restart_reason = (
                            "producer stalled"
                        )

                        logging.warning(
                            "Producer #%d produced no "
                            "media for %.1f seconds.",
                            producer_number,
                            media_idle,
                        )

                        break


                time.sleep(0.25)


            # =================================================
            # Stop current producer
            # =================================================

            if (
                producer is not None
                and producer.poll() is None
            ):

                terminate_process_tree(
                    producer,
                    f"yt-dlp #{producer_number}",
                )


            # ------------------------------------------------
            # Give the stdout reader a chance to finish.
            #
            # This is important: we do NOT start a new producer
            # while the old producer might still be putting
            # stale bytes into the shared queue.
            # ------------------------------------------------

            try:

                stdout_thread.join(
                    timeout=3
                )

            except Exception:

                pass


            try:

                producer.wait(
                    timeout=5
                )

            except subprocess.TimeoutExpired:

                logging.warning(
                    "yt-dlp producer #%d did not "
                    "exit normally.",
                    producer_number,
                )

                terminate_process_tree(
                    producer,
                    f"yt-dlp #{producer_number}",
                )


            if STOP_EVENT.is_set():
                break


            # ------------------------------------------------
            # IMPORTANT:
            #
            # We intentionally keep the same TCP connection to
            # FFmpeg.  The queued old bytes remain ahead of the
            # new producer's bytes, preserving ordering.
            # ------------------------------------------------

            if restart_reason:

                logging.warning(
                    "Replacing producer #%d: %s",
                    producer_number,
                    restart_reason,
                )


            # ------------------------------------------------
            # Short restart delay.
            # ------------------------------------------------

            for _ in range(
                PRODUCER_RESTART_DELAY_SECONDS * 4
            ):

                if STOP_EVENT.is_set():
                    break

                time.sleep(0.25)


        except Exception:

            logging.exception(
                "Unhandled exception while "
                "supervising producer #%d.",
                producer_number,
            )


            if (
                producer is not None
                and producer.poll() is None
            ):

                terminate_process_tree(
                    producer,
                    f"yt-dlp #{producer_number}",
                )


            if STOP_EVENT.is_set():
                break


            # A supervisor exception must NOT kill the whole
            # archiver. Treat it exactly like a failed producer.
            time.sleep(
                PRODUCER_RESTART_DELAY_SECONDS
            )


    return relay_state


# ============================================================
# One complete FFmpeg session
# ============================================================

def run_ffmpeg_session():

    relay_server = None
    client_socket = None
    ffmpeg_process = None


    try:

        relay_server = create_relay_server()

        ffmpeg_process = start_ffmpeg()


        try:

            client_socket = accept_ffmpeg(
                relay_server
            )

        except Exception:

            logging.exception(
                "Could not establish the "
                "FFmpeg relay."
            )

            return False


        # We accept exactly one FFmpeg connection for this
        # session. If FFmpeg later dies, the whole session is
        # rebuilt.
        try:

            relay_server.close()

        except OSError:

            pass

        relay_server = None


        supervise_producer(
            client_socket,
            ffmpeg_process,
        )


        return STOP_EVENT.is_set()


    except Exception:

        logging.exception(
            "FFmpeg session crashed."
        )

        return False


    finally:

        # ----------------------------------------------------
        # Close producer -> FFmpeg TCP connection.
        # ----------------------------------------------------

        if client_socket is not None:

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


        # ----------------------------------------------------
        # Stop FFmpeg.
        # ----------------------------------------------------

        if (
            ffmpeg_process is not None
            and ffmpeg_process.poll() is None
        ):

            if STOP_EVENT.is_set():

                logging.info(
                    "Waiting for FFmpeg to "
                    "finish the current segment..."
                )

                try:

                    ffmpeg_process.wait(
                        timeout=10
                    )

                except subprocess.TimeoutExpired:

                    logging.warning(
                        "FFmpeg did not exit "
                        "normally."
                    )

                    terminate_process_tree(
                        ffmpeg_process,
                        "FFmpeg",
                    )

            else:

                # Unexpected session failure.
                # Do not leave a dead/hung FFmpeg process behind.
                terminate_process_tree(
                    ffmpeg_process,
                    "FFmpeg",
                )


        # ----------------------------------------------------
        # Close relay server.
        # ----------------------------------------------------

        if relay_server is not None:

            try:

                relay_server.close()

            except OSError:

                pass


# ============================================================
# Main
# ============================================================

def main():

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

    check_dependencies()


    # --------------------------------------------------------
    # Startup information
    # --------------------------------------------------------

    logging.info(
        "=============================================="
    )

    logging.info(
        "Green Day TV 24/7 Archiver"
    )

    logging.info(
        "=============================================="
    )

    logging.info(
        "Archive directory: %s",
        ARCHIVE_DIR,
    )

    logging.info(
        "Stream: %s",
        STREAM_URL,
    )

    logging.info(
        "Format: %s",
        FORMAT_ID,
    )

    logging.info(
        "Video: 1920x1080 @ 30 FPS, H.264",
    )

    logging.info(
        "Audio: AAC",
    )

    logging.info(
        "Segment duration: %d seconds",
        SEGMENT_SECONDS,
    )

    logging.info(
        "Video encoding: NONE",
    )

    logging.info(
        "Audio encoding: NONE",
    )

    logging.info(
        "Producer refresh: %.2f hours",
        PRODUCER_REFRESH_SECONDS / 3600,
    )

    logging.info(
        "Producer stall timeout: %.1f seconds",
        PRODUCER_STALL_SECONDS,
    )

    logging.info(
        "Producer startup timeout: %.1f seconds",
        PRODUCER_STARTUP_TIMEOUT_SECONDS,
    )

    logging.info(
        "HTTP 401/403 threshold: %d failures "
        "within %.0f seconds",
        HTTP_AUTH_FAILURE_THRESHOLD,
        HTTP_AUTH_FAILURE_WINDOW_SECONDS,
    )

    logging.info(
        "Relay queue: %.1f MiB",
        QUEUE_MAX_BYTES / (1024 * 1024),
    )

    logging.info("")


    # --------------------------------------------------------
    # Initial disk check
    # --------------------------------------------------------

    if not check_disk_space():

        return


    # ========================================================
    # Outer FFmpeg/session supervisor
    #
    # This loop is intentionally extremely important.
    #
    # The old script had one FFmpeg process for the lifetime
    # of the program.  If that process died, the archive died.
    #
    # Here, FFmpeg itself is replaceable.
    # ========================================================

    backoff = (
        SESSION_BACKOFF_INITIAL_SECONDS
    )


    while not STOP_EVENT.is_set():

        logging.info(
            "Starting archive session..."
        )


        try:

            clean_shutdown = (
                run_ffmpeg_session()
            )


            if STOP_EVENT.is_set():

                break


            if clean_shutdown:

                break


            logging.error(
                "Archive session ended "
                "unexpectedly."
            )


        except Exception:

            logging.exception(
                "Unexpected exception in "
                "outer supervisor."
            )


        if STOP_EVENT.is_set():

            break


        logging.warning(
            "Restarting archive session "
            "in %.1f seconds.",
            backoff,
        )


        deadline = (
            time.monotonic()
            + backoff
        )


        while (
            time.monotonic() < deadline
            and not STOP_EVENT.is_set()
        ):

            time.sleep(0.25)


        backoff = min(
            backoff * 2,
            SESSION_BACKOFF_MAX_SECONDS,
        )


    # --------------------------------------------------------
    # Final shutdown
    # --------------------------------------------------------

    logging.info(
        "Archiver stopped."
    )


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":

    main()
