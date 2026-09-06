from __future__ import annotations

import ctypes
from collections import deque
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

from rotating_file_handler import (
    FileLogFilter,
    GZipRotatingFileHandler,
)


# ============================================================
# Green Day TV - Robust 24/7 Stream Archiver
#
# yt-dlp downloads the YouTube HLS stream through FFmpeg and
# writes MPEG-TS to stdout.
#
# Python places that data into a bounded RAM buffer.
# A second FFmpeg process reads the buffer and creates hourly
# MKV files.
#
# IMPORTANT:
#   archive FFmpeg uses wall-clock segmentation rather than
#   relying on the unstable timestamps coming from the HLS
#   stream. This prevents timestamp discontinuities from
#   generating hundreds of tiny files.
#
# Completed .local.mkv files are finalized in the background:
#
#   GreenDayTV_YYYY-MM-DD_HH-MM-SS.local.mkv
#                         ↓
#   GreenDayTV_YYYY-MM-DD_HH-MM-SS.mkv
#
# The timestamp is converted from Europe/Rome to UTC.
#
# No audio or video is re-encoded.
#
# A Windows named mutex prevents multiple copies using this
# script from running simultaneously.
# ============================================================


# ============================================================
# Configuration
# ============================================================

LOCAL_TIMEZONE = ZoneInfo("Europe/Rome")

STREAM_URL = (
    "https://www.youtube.com/watch?v=Xu90G4oFq2o"
)

ARCHIVE_DIR = Path(
    r"D:\Pietro\Archives\GreenDayTV"
)

SEGMENT_SECONDS = 60 * 60

MIN_FREE_GB = 10

RETRY_SECONDS = 7


# ------------------------------------------------------------
# YouTube format selection
# ------------------------------------------------------------
#
# Keep the known-good video+audio selection.
#
# Do NOT change this to a combined-only selector:
# your current stream demonstrates that YouTube currently
# provides 270 + 234 successfully.
#
FORMAT_SELECTOR = (
    "bestvideo[height<=1080][vcodec^=avc1]+bestaudio"
    "/best[height<=1080]"
)


# ------------------------------------------------------------
# yt-dlp / HLS resilience
# ------------------------------------------------------------

YTDLP_RETRIES = 3
YTDLP_FRAGMENT_RETRIES = 3

# Startup can legitimately take well over one minute because
# yt-dlp has to perform extraction and initialize both HLS
# inputs before useful stdout appears.
STARTUP_TIMEOUT_SECONDS = 180

# Once media has actually started, a complete 60-second absence
# of new bytes is considered a dead source.
SOURCE_STALL_SECONDS = 60


# ------------------------------------------------------------
# RAM media buffer
# ------------------------------------------------------------

BUFFER_MAX_BYTES = 256 * 1024 * 1024
BUFFER_CHUNK_SIZE = 256 * 1024
BUFFER_LOG_INTERVAL_SECONDS = 30


# ------------------------------------------------------------
# Finalizer
# ------------------------------------------------------------

FINALIZE_MIN_AGE_SECONDS = 120
FINALIZER_SCAN_INTERVAL = 30.0
FINALIZER_STABLE_DELAY = 2.0

FINALIZE_TEMP_SUFFIX = ".finalizing.mkv"


# ------------------------------------------------------------
# Files produced by the recorder
# ------------------------------------------------------------

FINALIZER_RE = re.compile(
    r"^GreenDayTV_\d{4}-\d{2}-\d{2}_"
    r"\d{2}-\d{2}-\d{2}\.local\.mkv$"
)


# ============================================================
# Executables
# ============================================================

YTDLP = (
    shutil.which("yt-dlp")
    or shutil.which("yt-dlp.exe")
)

FFMPEG = (
    shutil.which("ffmpeg")
    or shutil.which("ffmpeg.exe")
)


# ============================================================
# Logging
# ============================================================

LOG_FILE = ARCHIVE_DIR / "archiver.log"

LOG_MAX_BYTES = 50 * 1024 * 1024
LOG_TOTAL_MAX_BYTES = 0
LOG_GZIP_LEVEL = 7


def configure_logging() -> logging.Logger:
    logger = logging.getLogger(
        "green_day_tv_archiver"
    )

    logger.setLevel(logging.INFO)

    logger.propagate = False

    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )

    file_handler = GZipRotatingFileHandler(
        LOG_FILE,
        max_bytes=LOG_MAX_BYTES,
        total_max_bytes=LOG_TOTAL_MAX_BYTES,
        compresslevel=LOG_GZIP_LEVEL,
    )

    file_handler.setFormatter(formatter)

    file_handler.addFilter(
        FileLogFilter()
    )

    console_handler = logging.StreamHandler()

    console_handler.setFormatter(
        formatter
    )

    logger.addHandler(file_handler)

    logger.addHandler(console_handler)

    return logger


# Ensure ARCHIVE_DIR exists before the logger tries to create
# archiver.log there.
ARCHIVE_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

log = configure_logging()


# ============================================================
# Shutdown
# ============================================================

SHUTDOWN_EVENT = threading.Event()


# ============================================================
# Active recording segment
# ============================================================

ACTIVE_SEGMENT_LOCK = threading.Lock()

ACTIVE_SEGMENT: Path | None = None


def get_active_segment() -> Path | None:
    with ACTIVE_SEGMENT_LOCK:
        return ACTIVE_SEGMENT


def set_active_segment(
    path: Path | None,
) -> None:

    global ACTIVE_SEGMENT

    with ACTIVE_SEGMENT_LOCK:
        ACTIVE_SEGMENT = path


# ============================================================
# Runtime statistics
# ============================================================

MEDIA_STATS_LOCK = threading.Lock()

SOURCE_BYTES_RECEIVED = 0

ARCHIVE_BYTES_WRITTEN = 0

LAST_SOURCE_DATA_TIME = 0.0

MEDIA_SEEN = False


def reset_media_stats() -> None:

    global SOURCE_BYTES_RECEIVED
    global ARCHIVE_BYTES_WRITTEN
    global LAST_SOURCE_DATA_TIME
    global MEDIA_SEEN

    with MEDIA_STATS_LOCK:

        SOURCE_BYTES_RECEIVED = 0

        ARCHIVE_BYTES_WRITTEN = 0

        LAST_SOURCE_DATA_TIME = (
            time.monotonic()
        )

        MEDIA_SEEN = False


def note_source_bytes(
    count: int,
) -> None:

    global SOURCE_BYTES_RECEIVED
    global LAST_SOURCE_DATA_TIME
    global MEDIA_SEEN

    with MEDIA_STATS_LOCK:

        SOURCE_BYTES_RECEIVED += count

        LAST_SOURCE_DATA_TIME = (
            time.monotonic()
        )

        MEDIA_SEEN = True


def note_archive_bytes(
    count: int,
) -> None:

    global ARCHIVE_BYTES_WRITTEN

    with MEDIA_STATS_LOCK:

        ARCHIVE_BYTES_WRITTEN += count


def get_media_stats():

    with MEDIA_STATS_LOCK:

        return (
            SOURCE_BYTES_RECEIVED,
            ARCHIVE_BYTES_WRITTEN,
            LAST_SOURCE_DATA_TIME,
            MEDIA_SEEN,
        )


# ============================================================
# Windows single-instance mutex
# ============================================================

MUTEX_NAME = (
    r"Global\GreenDayTV_Archiver_SingleInstance"
)


def acquire_single_instance():

    if os.name != "nt":
        return object()

    kernel32 = ctypes.WinDLL(
        "kernel32",
        use_last_error=True,
    )

    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_bool,
        ctypes.c_wchar_p,
    ]

    kernel32.CreateMutexW.restype = (
        ctypes.c_void_p
    )

    kernel32.GetLastError.restype = (
        ctypes.c_ulong
    )

    ERROR_ALREADY_EXISTS = 183

    handle = kernel32.CreateMutexW(
        None,
        False,
        MUTEX_NAME,
    )

    if not handle:
        raise ctypes.WinError(
            ctypes.get_last_error()
        )

    if (
        kernel32.GetLastError()
        == ERROR_ALREADY_EXISTS
    ):

        kernel32.CloseHandle(handle)

        return None

    return handle


def release_single_instance(
    handle,
) -> None:

    if (
        os.name != "nt"
        or handle is None
    ):
        return

    kernel32 = ctypes.WinDLL(
        "kernel32",
        use_last_error=True,
    )

    kernel32.CloseHandle.argtypes = [
        ctypes.c_void_p,
    ]

    kernel32.CloseHandle.restype = (
        ctypes.c_bool
    )

    kernel32.CloseHandle(handle)


# ============================================================
# Windows process priority
# ============================================================

def get_process_creation_flags(
    priority: str,
) -> int:

    if os.name != "nt":
        return 0

    create_group = getattr(
        subprocess,
        "CREATE_NEW_PROCESS_GROUP",
        0,
    )

    above_normal = 0x00008000

    below_normal = 0x00004000

    if priority == "above":

        return (
            create_group
            | above_normal
        )

    if priority == "below":

        return (
            create_group
            | below_normal
        )

    return create_group


# ============================================================
# Disk space
# ============================================================

def free_space_gb(
    path: Path,
) -> float:

    return (
        shutil.disk_usage(path).free
        / (1024 ** 3)
    )


# ============================================================
# Timestamp handling
# ============================================================

def parse_local_timestamp(
    path: Path,
) -> datetime:

    match = re.match(
        r"^GreenDayTV_(\d{4}-\d{2}-\d{2})_"
        r"(\d{2}-\d{2}-\d{2})\.local\.mkv$",
        path.name,
    )

    if not match:

        raise ValueError(
            f"Invalid segment filename: "
            f"{path.name}"
        )

    naive = datetime.strptime(
        (
            f"{match.group(1)} "
            f"{match.group(2)}"
        ),
        "%Y-%m-%d %H-%M-%S",
    )

    return naive.replace(
        tzinfo=LOCAL_TIMEZONE
    )


def utc_output_path(
    local_path: Path,
) -> Path:

    local_dt = parse_local_timestamp(
        local_path
    )

    utc_dt = local_dt.astimezone(
        timezone.utc
    )

    return ARCHIVE_DIR / (
        f"GreenDayTV_"
        f"{utc_dt:%Y-%m-%d_%H-%M-%S}.mkv"
    )


# ============================================================
# File stability
# ============================================================

def is_file_stable(
    path: Path,
) -> bool:

    try:

        size1 = path.stat().st_size

        time.sleep(
            FINALIZER_STABLE_DELAY
        )

        size2 = path.stat().st_size

    except (
        FileNotFoundError,
        PermissionError,
    ):

        return False

    return size1 == size2


def is_old_enough(
    path: Path,
) -> bool:

    try:

        age = (
            time.time()
            - path.stat().st_mtime
        )

    except (
        FileNotFoundError,
        PermissionError,
    ):

        return False

    return (
        age >= FINALIZE_MIN_AGE_SECONDS
    )


# ============================================================
# Bounded byte queue
# ============================================================

class BufferedByteQueue:

    def __init__(
        self,
        max_bytes: int,
    ):

        self.max_bytes = max_bytes

        self._condition = (
            threading.Condition()
        )

        self._chunks: deque[bytes] = (
            deque()
        )

        self._bytes = 0

        self._closed = False

        self._aborted = False

    def put(
        self,
        data: bytes,
    ) -> bool:

        if not data:
            return True

        with self._condition:

            while (
                not self._aborted
                and not self._closed
                and (
                    self._bytes
                    + len(data)
                    > self.max_bytes
                )
            ):

                self._condition.wait(
                    timeout=1.0
                )

            if (
                self._aborted
                or self._closed
            ):
                return False

            self._chunks.append(
                data
            )

            self._bytes += len(data)

            self._condition.notify_all()

            return True

    def get(self):

        with self._condition:

            while (
                not self._chunks
                and not self._aborted
                and not self._closed
            ):

                self._condition.wait(
                    timeout=1.0
                )

            if self._aborted:
                return None

            if self._chunks:

                data = (
                    self._chunks.popleft()
                )

                self._bytes -= len(data)

                self._condition.notify_all()

                return data

            return None

    def close(self) -> None:

        with self._condition:

            self._closed = True

            self._condition.notify_all()

    def abort(self) -> None:

        with self._condition:

            self._aborted = True

            self._chunks.clear()

            self._bytes = 0

            self._condition.notify_all()

    def bytes_available(self) -> int:

        with self._condition:
            return self._bytes

    def fraction_full(self) -> float:

        with self._condition:

            return (
                self._bytes
                / self.max_bytes
            )


# ============================================================
# Finalization
# ============================================================

def finalise_segment(
    path: Path,
) -> None:

    if not path.exists():
        return

    active = get_active_segment()

    if active is not None:

        try:

            if path.resolve() == active.resolve():

                return

        except FileNotFoundError:

            return

    if not is_old_enough(path):

        return

    if not is_file_stable(path):

        log.info(
            "Segment is still changing: %s",
            path.name,
        )

        return

    output = utc_output_path(path)

    # --------------------------------------------------------
    # Already finalized.
    #
    # If the destination exists, do NOT overwrite it.
    # The local file is redundant only if we are certain the
    # destination is the corresponding completed segment.
    # --------------------------------------------------------

    if output.exists():

        log.warning(
            "Final destination already exists; "
            "leaving local source untouched: %s -> %s",
            path.name,
            output.name,
        )

        return

    try:

        log.info(
            "Finalizing by rename: %s -> %s",
            path.name,
            output.name,
        )

        path.replace(output)

        log.info(
            "Finalized successfully: %s",
            output.name,
        )

    except PermissionError:

        log.info(
            "Segment is still locked; will retry: %s",
            path.name,
        )

    except FileNotFoundError:

        return

    except Exception:

        log.exception(
            "Unexpected finalization error for %s",
            path.name,
        )
        
def segment_finalizer_loop() -> None:

    log.info(
        "Segment finalizer thread started."
    )

    while not SHUTDOWN_EVENT.is_set():

        try:

            paths = sorted(
                (
                    p
                    for p in ARCHIVE_DIR.glob(
                        "GreenDayTV_*.local.mkv"
                    )
                    if FINALIZER_RE.match(
                        p.name
                    )
                ),
                key=lambda p: p.stat().st_mtime,
            )

            eligible = [
                p
                for p in paths
                if is_old_enough(p)
            ]

            if eligible:

                log.info(
                    "Finalizer found %s eligible local segment(s).",
                    len(eligible),
                )

            # Process every eligible file.
            #
            # One problematic file must never prevent newer
            # segments from being finalized.
            for path in eligible:

                if SHUTDOWN_EVENT.is_set():
                    break

                finalise_segment(path)

        except Exception:

            log.exception(
                "Error in segment finalizer loop."
            )

        SHUTDOWN_EVENT.wait(
            FINALIZER_SCAN_INTERVAL
        )

    log.info(
        "Segment finalizer thread stopped."
    )

# ============================================================
# yt-dlp stderr
# ============================================================

def read_ytdlp_stderr(
    process: subprocess.Popen,
) -> None:

    try:

        for raw_line in process.stderr:

            if SHUTDOWN_EVENT.is_set():
                break

            if isinstance(
                raw_line,
                bytes,
            ):

                line = raw_line.decode(
                    "utf-8",
                    errors="replace",
                ).rstrip()

            else:

                line = raw_line.rstrip()

            if line:

                log.info(
                    "[yt-dlp] %s",
                    line,
                )

    except Exception:

        if not SHUTDOWN_EVENT.is_set():

            log.exception(
                "yt-dlp stderr reader failed."
            )


# ============================================================
# yt-dlp stdout
# ============================================================

def read_ytdlp_stdout(
    process: subprocess.Popen,
    media_buffer: BufferedByteQueue,
) -> None:

    try:

        while not SHUTDOWN_EVENT.is_set():

            data = process.stdout.read(
                BUFFER_CHUNK_SIZE
            )

            if not data:
                break

            note_source_bytes(
                len(data)
            )

            if not media_buffer.put(
                data
            ):
                break

    except Exception:

        if not SHUTDOWN_EVENT.is_set():

            log.exception(
                "yt-dlp stdout reader failed."
            )

    finally:

        media_buffer.close()


# ============================================================
# Archive FFmpeg stderr
# ============================================================

def read_archive_ffmpeg_stderr(
    process: subprocess.Popen,
) -> None:

    try:

        for raw_line in process.stderr:

            if isinstance(
                raw_line,
                bytes,
            ):

                line = raw_line.decode(
                    "utf-8",
                    errors="replace",
                ).strip()

            else:

                line = raw_line.strip()

            if line:

                log.warning(
                    "[archive-ffmpeg] %s",
                    line,
                )

    except Exception:

        if not SHUTDOWN_EVENT.is_set():

            log.exception(
                "Archive FFmpeg stderr reader "
                "failed."
            )


# ============================================================
# Archive FFmpeg writer
# ============================================================

def write_buffer_to_ffmpeg(
    ffmpeg: subprocess.Popen,
    media_buffer: BufferedByteQueue,
) -> None:

    try:

        while not SHUTDOWN_EVENT.is_set():

            data = media_buffer.get()

            if data is None:
                break

            try:

                ffmpeg.stdin.write(
                    data
                )

            except (
                BrokenPipeError,
                OSError,
            ):

                break

            note_archive_bytes(
                len(data)
            )

    except Exception:

        if not SHUTDOWN_EVENT.is_set():

            log.exception(
                "Archive FFmpeg writer failed."
            )

    finally:

        try:
            ffmpeg.stdin.close()

        except Exception:
            pass


# ============================================================
# Process shutdown
# ============================================================

def stop_process(
    process: subprocess.Popen,
    name: str,
    timeout: float,
) -> None:

    try:

        if process.poll() is not None:
            return

        process.terminate()

        try:

            process.wait(
                timeout=timeout
            )

            return

        except subprocess.TimeoutExpired:

            pass

        log.warning(
            "%s did not terminate cleanly; "
            "killing it.",
            name,
        )

        process.kill()

        try:

            process.wait(timeout=5)

        except subprocess.TimeoutExpired:

            pass

    except Exception:

        log.exception(
            "Error stopping %s.",
            name,
        )


# ============================================================
# Recording session
# ============================================================

def run_recording() -> bool:

    reset_media_stats()

    media_buffer = BufferedByteQueue(
        BUFFER_MAX_BYTES
    )

    log.info("")
    log.info(
        "=============================================="
    )
    log.info(
        "Starting recording session"
    )
    log.info(
        "=============================================="
    )

    # --------------------------------------------------------
    # yt-dlp
    # --------------------------------------------------------

    ytdlp_command = [

        YTDLP,

        "--no-playlist",

        "-f",
        FORMAT_SELECTOR,

        "--downloader",
        "ffmpeg",

        "--hls-use-mpegts",

        "--retries",
        str(YTDLP_RETRIES),

        "--fragment-retries",
        str(YTDLP_FRAGMENT_RETRIES),

        "--retry-sleep",
        "exp=1:5",

        "--downloader-args",
        (
            "ffmpeg_i:"
            "-reconnect 1 "
            "-reconnect_streamed 1 "
            "-reconnect_delay_max 5"
        ),

        "-o",
        "-",

        STREAM_URL,
    ]

    # --------------------------------------------------------
    # Archive FFmpeg
    # --------------------------------------------------------
    #
    # The important part is:
    #
    #   -segment_atclocktime 1
    #
    # This makes the hourly boundaries depend on wall clock,
    # not on the HLS timestamps.
    #
    # This is the key fix for the hundreds of tiny files.
    # --------------------------------------------------------

    output_template = ARCHIVE_DIR / (
        "GreenDayTV_%Y-%m-%d_%H-%M-%S.local.mkv"
    )

    ffmpeg_command = [

        FFMPEG,

        "-hide_banner",
        "-loglevel", "warning",
        "-nostdin",

        "-i",
        "pipe:0",

        "-map", "0:v:0",
        "-map", "0:a:0",

        "-c:v", "copy",
        "-c:a", "copy",

        "-f", "segment",

        "-segment_time",
        str(SEGMENT_SECONDS),

        "-segment_atclocktime",
        "1",

        "-min_seg_duration",
        "300",

        "-reset_timestamps",
        "1",

        "-strftime",
        "1",

        "-segment_format",
        "matroska",

        str(output_template),
    ]

    log.info(
        "Format selector: %s",
        FORMAT_SELECTOR,
    )

    log.info(
        "RAM media buffer: %.1f MiB",
        BUFFER_MAX_BYTES
        / (1024 ** 2),
    )

    log.info(
        "Startup timeout: %s seconds",
        STARTUP_TIMEOUT_SECONDS,
    )

    log.info(
        "Source stall timeout: %s seconds",
        SOURCE_STALL_SECONDS,
    )

    try:

        ytdlp = subprocess.Popen(
            ytdlp_command,

            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,

            stdin=subprocess.DEVNULL,

            bufsize=0,

            creationflags=(
                get_process_creation_flags(
                    "above"
                )
            ),
        )

    except Exception:

        log.exception(
            "Failed to start yt-dlp."
        )

        return False

    try:

        ffmpeg = subprocess.Popen(
            ffmpeg_command,

            stdin=subprocess.PIPE,

            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,

            bufsize=0,

            creationflags=(
                get_process_creation_flags(
                    "above"
                )
            ),
        )

    except Exception:

        log.exception(
            "Failed to start archive FFmpeg."
        )

        stop_process(
            ytdlp,
            "yt-dlp",
            8,
        )

        return False

    # --------------------------------------------------------
    # Threads
    # --------------------------------------------------------

    stdout_thread = threading.Thread(
        target=read_ytdlp_stdout,
        args=(
            ytdlp,
            media_buffer,
        ),
        name="yt-dlp-stdout-reader",
        daemon=True,
    )

    ytdlp_stderr_thread = threading.Thread(
        target=read_ytdlp_stderr,
        args=(ytdlp,),
        name="yt-dlp-stderr-reader",
        daemon=True,
    )

    writer_thread = threading.Thread(
        target=write_buffer_to_ffmpeg,
        args=(
            ffmpeg,
            media_buffer,
        ),
        name="ffmpeg-buffer-writer",
        daemon=True,
    )

    ffmpeg_stderr_thread = threading.Thread(
        target=read_archive_ffmpeg_stderr,
        args=(ffmpeg,),
        name="ffmpeg-stderr-reader",
        daemon=True,
    )

    stdout_thread.start()

    ytdlp_stderr_thread.start()

    writer_thread.start()

    ffmpeg_stderr_thread.start()

    session_start = time.monotonic()

    last_buffer_log = session_start

    last_source_bytes = 0

    # --------------------------------------------------------
    # Monitoring
    # --------------------------------------------------------

    while not SHUTDOWN_EVENT.is_set():

        now = time.monotonic()

        ytdlp_rc = ytdlp.poll()

        ffmpeg_rc = ffmpeg.poll()

        if ffmpeg_rc is not None:

            log.error(
                "Archive FFmpeg exited with "
                "return code %s.",
                ffmpeg_rc,
            )

            break

        if ytdlp_rc is not None:

            log.warning(
                "yt-dlp exited with "
                "return code %s.",
                ytdlp_rc,
            )

            break

        (
            source_bytes,
            archive_bytes,
            last_source_data,
            media_seen,
        ) = get_media_stats()

        # ----------------------------------------------------
        # Periodic pipeline status
        # ----------------------------------------------------

        if (
            now - last_buffer_log
            >= BUFFER_LOG_INTERVAL_SECONDS
        ):

            log.info(
                "Pipeline status: "
                "source=%.2f MiB, "
                "archive=%.2f MiB, "
                "buffer=%.1f MiB (%.0f%%)",
                source_bytes
                / (1024 ** 2),

                archive_bytes
                / (1024 ** 2),

                media_buffer.bytes_available()
                / (1024 ** 2),

                media_buffer.fraction_full()
                * 100,
            )

            last_buffer_log = now

            last_source_bytes = (
                source_bytes
            )

        # ----------------------------------------------------
        # Startup
        # ----------------------------------------------------

        if not media_seen:

            if (
                now - session_start
                >= STARTUP_TIMEOUT_SECONDS
            ):

                log.error(
                    "No media bytes received from "
                    "yt-dlp within %s seconds.",
                    STARTUP_TIMEOUT_SECONDS,
                )

                break

        # ----------------------------------------------------
        # Source stall
        # ----------------------------------------------------

        elif (
            now - last_source_data
            >= SOURCE_STALL_SECONDS
        ):

            log.error(
                "No new media bytes have arrived "
                "from yt-dlp for %s seconds.",
                SOURCE_STALL_SECONDS,
            )

            break

        # ----------------------------------------------------
        # Disk space
        # ----------------------------------------------------

        if (
            int(now - session_start) % 60
            == 0
        ):

            try:

                free_gb = free_space_gb(
                    ARCHIVE_DIR
                )

                if free_gb < MIN_FREE_GB:

                    log.error(
                        "Free disk space has fallen "
                        "below %.2f GB. "
                        "Stopping archiver.",
                        MIN_FREE_GB,
                    )

                    break

            except Exception:

                log.exception(
                    "Unable to check free disk space."
                )

        time.sleep(1.0)

    # ========================================================
    # Session shutdown
    # ========================================================

    log.info(
        "Stopping recording session..."
    )

    # Stop the source first.
    stop_process(
        ytdlp,
        "yt-dlp",
        8,
    )

    # Let the stdout reader finish and close the buffer.
    media_buffer.close()

    stdout_thread.join(
        timeout=5
    )

    # Let the archive writer drain buffered bytes.
    writer_thread.join(
        timeout=15
    )

    # FFmpeg receives EOF after stdin is closed by writer_thread.
    stop_process(
        ffmpeg,
        "archive FFmpeg",
        15,
    )

    ytdlp_stderr_thread.join(
        timeout=2
    )

    ffmpeg_stderr_thread.join(
        timeout=2
    )

    set_active_segment(
        None
    )

    (
        source_bytes,
        archive_bytes,
        _last_source_data,
        media_seen,
    ) = get_media_stats()

    log.info(
        "Session totals: "
        "%.2f MiB received, "
        "%.2f MiB sent to archive FFmpeg.",
        source_bytes / (1024 ** 2),
        archive_bytes / (1024 ** 2),
    )

    if media_seen:

        log.info(
            "Recording session ended after "
            "receiving media."
        )

        return True

    log.warning(
        "Recording session ended without "
        "receiving media."
    )

    return False


# ============================================================
# Main
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

    ARCHIVE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    mutex_handle = acquire_single_instance()

    if mutex_handle is None:

        log.error(
            "Another Green Day TV archiver instance "
            "is already running."
        )

        return

    finalizer_thread = None

    try:

        # ----------------------------------------------------
        # Version and process identity
        # ----------------------------------------------------

        try:

            result = subprocess.run(
                [
                    YTDLP,
                    "--version",
                ],

                capture_output=True,

                text=True,

                encoding="utf-8",

                errors="replace",

                timeout=10,
            )

            version = (
                result.stdout.strip()
            )

            if version:

                log.info(
                    "yt-dlp version: %s",
                    version,
                )

        except Exception:

            log.warning(
                "Could not determine yt-dlp version."
            )

        log.info(
            "Archiver PID: %s",
            os.getpid(),
        )

        log.info(
            "Active log file: %s",
            LOG_FILE.resolve(),
        )

        log.info(
            "=============================================="
        )

        log.info(
            "Green Day TV archiver"
        )

        log.info(
            "=============================================="
        )

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
            "Target video: up to 1920x1080 H.264"
        )

        log.info(
            "Target audio: best available"
        )

        log.info(
            "Segment duration: %s seconds",
            SEGMENT_SECONDS,
        )

        log.info(
            "Segmentation: wall-clock aligned"
        )

        log.info(
            "Video encoding: stream copy"
        )

        log.info(
            "Audio encoding: stream copy"
        )

        log.info(
            "RAM media buffer: %.1f MiB",
            BUFFER_MAX_BYTES
            / (1024 ** 2),
        )

        log.info(
            "Startup timeout: %s seconds",
            STARTUP_TIMEOUT_SECONDS,
        )

        log.info(
            "Source stall timeout: %s seconds",
            SOURCE_STALL_SECONDS,
        )

        log.info(
            "Finalization delay: %s seconds",
            FINALIZE_MIN_AGE_SECONDS,
        )

        log.info("")

        # ----------------------------------------------------
        # Finalizer
        # ----------------------------------------------------

        finalizer_thread = threading.Thread(
            target=segment_finalizer_loop,
            name="segment-finalizer",
            daemon=True,
        )

        finalizer_thread.start()

        # ----------------------------------------------------
        # Recovery loop
        # ----------------------------------------------------

        while not SHUTDOWN_EVENT.is_set():

            try:

                free_gb = free_space_gb(
                    ARCHIVE_DIR
                )

                log.info(
                    "Free disk space: %.2f GB",
                    free_gb,
                )

                if free_gb < MIN_FREE_GB:

                    log.error(
                        "Free disk space has fallen "
                        "below %.2f GB. "
                        "Stopping archiver.",
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

            SHUTDOWN_EVENT.wait(
                RETRY_SECONDS
            )

    except KeyboardInterrupt:

        log.info(
            "Keyboard interrupt received."
        )

    finally:

        SHUTDOWN_EVENT.set()

        log.info(
            "Shutdown requested..."
        )

        if finalizer_thread is not None:

            finalizer_thread.join(
                timeout=5
            )

        release_single_instance(
            mutex_handle
        )

        log.info(
            "Archiver stopped."
        )


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    main()
