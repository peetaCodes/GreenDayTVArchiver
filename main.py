from __future__ import annotations

import logging
import signal
import subprocess
import sys
import time
from pathlib import Path


# ============================================================
# Green Day TV - Master Supervisor
#
# Responsibilities:
#
#   1. Keep the archiver process running.
#   2. Periodically run the daily stitcher.
#   3. After the stitcher finishes, run the IA uploader.
#   4. Repeat forever with very low background resource usage.
#
# The archiver itself already contains its own recording/reconnect
# logic. The master therefore does NOT interfere with a healthy
# archiver process.
#
# Windows / Python 3.10+
# ============================================================


# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

BASE_DIR = Path(
    r"D:\Pietro\Archives\GreenDayTV"
)

ARCHIVER_SCRIPT = BASE_DIR / "green_day_tv_archiver.py"
STITCHER_SCRIPT = BASE_DIR / "green_day_tv_stitcher.py"
UPLOADER_SCRIPT = BASE_DIR / "green_day_tv_uploader.py"


# How often the stitcher/uploader pipeline is run.
#
# 4 hours is a good compromise:
#
#   - the check is run only 6 times a day, with very little resource usage while idle
#   - newly completed daily files are noticed reasonably quickly
PIPELINE_INTERVAL_SECONDS = 4 * 60 * 60


# How often the master checks whether the archiver process is alive.
#
# This is intentionally much more frequent than the pipeline, but
# poll() itself is essentially free.
#
# Please keep this over 8
ARCHIVER_WATCHDOG_INTERVAL_SECONDS = 15


# If the archiver process itself dies, wait this this much long before
# starting a replacement process.
# Please keep this over 3
ARCHIVER_RESTART_DELAY_SECONDS = 5


# Wait between the stitcher and uploader.
#
# Normally this can be zero. Keeping a tiny delay gives your OS
# time to finish releasing handles after the stitcher exits.
BETWEEN_PIPELINE_STEPS_SECONDS = 2


# ------------------------------------------------------------
# Globals
# ------------------------------------------------------------

STOP_REQUESTED = False
ARCHIVER_PROCESS: subprocess.Popen | None = None


# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------

def setup_logging():
    BASE_DIR.mkdir(
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
                BASE_DIR / "master.log",
                encoding="utf-8",
            ),
        ],
    )


# ------------------------------------------------------------
# Signal handling
# ------------------------------------------------------------

def request_stop(signum, frame):
    global STOP_REQUESTED

    if not STOP_REQUESTED:
        STOP_REQUESTED = True
        logging.info("Shutdown requested.")


# ------------------------------------------------------------
# Interruptible sleep
# ------------------------------------------------------------

def sleep_interruptibly(seconds: float):
    """
    Sleep without polling continuously.

    The master therefore consumes essentially no CPU while idle.
    """

    end_time = time.monotonic() + seconds

    while not STOP_REQUESTED:
        remaining = end_time - time.monotonic()

        if remaining <= 0:
            break

        # Sleep in moderately sized chunks so shutdown remains
        # responsive without creating a busy loop.
        time.sleep(min(remaining, 5.0))


# ------------------------------------------------------------
# Python subprocess environment
# ------------------------------------------------------------

def python_command(script: Path) -> list[str]:
    return [
        sys.executable,
        str(script),
    ]


# ------------------------------------------------------------
# Start archiver
# ------------------------------------------------------------

def start_archiver():
    global ARCHIVER_PROCESS

    if not ARCHIVER_SCRIPT.exists():
        raise RuntimeError(
            f"Archiver script does not exist:\n{ARCHIVER_SCRIPT}"
        )

    logging.info(
        "Starting archiver: %s",
        ARCHIVER_SCRIPT,
    )

    # Windows process flags.
    #
    # CREATE_NEW_PROCESS_GROUP:
    #   Gives the archiver its own process group.
    #
    # CREATE_NO_WINDOW:
    #   Prevents a separate console window from appearing.
    #
    # The archiver remains an independent process and does not need
    # the master to stay attached to its stdin/stdout/stderr.
    creationflags = (
        subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.CREATE_NO_WINDOW
    )

    try:
        ARCHIVER_PROCESS = subprocess.Popen(
            python_command(ARCHIVER_SCRIPT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )

    except Exception:
        logging.exception(
            "Could not start the archiver."
        )
        ARCHIVER_PROCESS = None
        return False

    logging.info(
        "Archiver started with PID %d.",
        ARCHIVER_PROCESS.pid,
    )

    return True


# ------------------------------------------------------------
# Stop archiver
# ------------------------------------------------------------

def stop_archiver():
    global ARCHIVER_PROCESS

    if ARCHIVER_PROCESS is None:
        return

    if ARCHIVER_PROCESS.poll() is not None:
        ARCHIVER_PROCESS = None
        return

    logging.info(
        "Stopping archiver (PID %d)...",
        ARCHIVER_PROCESS.pid,
    )

    try:
        # Since the archiver is in its own process group, use
        # CTRL_BREAK_EVENT first so its Python signal handling has
        # an opportunity to perform a clean shutdown.
        #
        # This requires CREATE_NEW_PROCESS_GROUP above.
        ARCHIVER_PROCESS.send_signal(
            signal.CTRL_BREAK_EVENT
        )

    except Exception:
        logging.exception(
            "Could not send clean shutdown signal to archiver."
        )

    try:
        ARCHIVER_PROCESS.wait(
            timeout=10
        )

    except subprocess.TimeoutExpired:
        logging.warning(
            "Archiver did not stop cleanly; terminating it."
        )

        try:
            subprocess.run(
                [
                    "taskkill",
                    "/PID",
                    str(ARCHIVER_PROCESS.pid),
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
                "Could not terminate archiver."
            )

    ARCHIVER_PROCESS = None


# ------------------------------------------------------------
# Archiver watchdog
# ------------------------------------------------------------

def check_archiver():
    """
    Ensure that the archiver process exists.

    A healthy archiver is completely left alone.

    If the Python archiver process itself exits unexpectedly,
    restart it. This is an additional safety net around the
    archiver's own reconnect/recovery logic.
    """

    global ARCHIVER_PROCESS

    if ARCHIVER_PROCESS is None:
        logging.warning(
            "Archiver process is not running."
        )

        if not start_archiver():
            return

        return

    return_code = ARCHIVER_PROCESS.poll()

    if return_code is None:
        # Healthy. Do absolutely nothing.
        return

    logging.error(
        "Archiver process exited with return code %s.",
        return_code,
    )

    ARCHIVER_PROCESS = None

    if STOP_REQUESTED:
        return

    logging.info(
        "Restarting archiver in %.1f seconds...",
        ARCHIVER_RESTART_DELAY_SECONDS,
    )

    sleep_interruptibly(
        ARCHIVER_RESTART_DELAY_SECONDS
    )

    if not STOP_REQUESTED:
        start_archiver()


# ------------------------------------------------------------
# Run stitcher
# ------------------------------------------------------------

def run_stitcher():
    if not STITCHER_SCRIPT.exists():
        logging.error(
            "Stitcher script does not exist:\n%s",
            STITCHER_SCRIPT,
        )
        return False

    logging.info("")
    logging.info(
        "============================================================"
    )
    logging.info(
        "Starting daily stitcher..."
    )
    logging.info(
        "============================================================"
    )

    try:
        result = subprocess.run(
            python_command(STITCHER_SCRIPT),
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            check=False,
        )

    except Exception:
        logging.exception(
            "Could not start stitcher."
        )
        return False

    if result.returncode == 0:
        logging.info(
            "Daily stitcher completed successfully."
        )
        return True

    logging.error(
        "Daily stitcher exited with return code %d.",
        result.returncode,
    )

    return False


# ------------------------------------------------------------
# Run uploader
# ------------------------------------------------------------

def run_uploader():
    if not UPLOADER_SCRIPT.exists():
        logging.error(
            "Uploader script does not exist:\n%s",
            UPLOADER_SCRIPT,
        )
        return False

    logging.info("")
    logging.info(
        "============================================================"
    )
    logging.info(
        "Starting Internet Archive uploader..."
    )
    logging.info(
        "============================================================"
    )

    try:
        result = subprocess.run(
            python_command(UPLOADER_SCRIPT),
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            check=False,
        )

    except Exception:
        logging.exception(
            "Could not start uploader."
        )
        return False

    if result.returncode == 0:
        logging.info(
            "Internet Archive uploader completed successfully."
        )
        return True

    logging.error(
        "Internet Archive uploader exited with return code %d.",
        result.returncode,
    )

    return False


# ------------------------------------------------------------
# Run complete archive-maintenance cycle
# ------------------------------------------------------------

def run_pipeline():
    """
    Stitch first, then upload.

    Even if stitching fails, still run the uploader. This is useful
    because an older daily file may already exist and be waiting
    for upload.

    The uploader itself will decide which files are actually ready.
    """

    logging.info("")
    logging.info(
        "############################################################"
    )
    logging.info(
        "Starting archive maintenance cycle."
    )
    logging.info(
        "############################################################"
    )

    stitcher_ok = run_stitcher()

    sleep_interruptibly(
        BETWEEN_PIPELINE_STEPS_SECONDS
    )

    uploader_ok = run_uploader()

    logging.info(
        "Archive maintenance cycle finished "
        "(stitcher=%s, uploader=%s).",
        "OK" if stitcher_ok else "FAILED",
        "OK" if uploader_ok else "FAILED",
    )


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main():
    global STOP_REQUESTED

    setup_logging()

    logging.info(
        "============================================================"
    )
    logging.info(
        "Green Day TV master supervisor"
    )
    logging.info(
        "============================================================"
    )

    logging.info(
        "Pipeline interval: %.0f seconds (%.1f minutes)",
        PIPELINE_INTERVAL_SECONDS,
        PIPELINE_INTERVAL_SECONDS / 60,
    )

    logging.info(
        "Archiver watchdog interval: %.0f seconds",
        ARCHIVER_WATCHDOG_INTERVAL_SECONDS,
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
    # Start archiver
    # --------------------------------------------------------

    if not start_archiver():
        logging.error(
            "Initial archiver start failed."
        )

    # Run an initial maintenance cycle immediately instead of
    # waiting 30 minutes after startup.
    next_pipeline = time.monotonic()

    next_archiver_watchdog = (
        time.monotonic()
    )

    # --------------------------------------------------------
    # Supervisor loop
    # --------------------------------------------------------

    while not STOP_REQUESTED:

        now = time.monotonic()

        # ----------------------------------------------------
        # Archiver watchdog
        # ----------------------------------------------------

        if now >= next_archiver_watchdog:

            check_archiver()

            next_archiver_watchdog = (
                now
                + ARCHIVER_WATCHDOG_INTERVAL_SECONDS
            )

        # ----------------------------------------------------
        # Stitcher -> uploader pipeline
        # ----------------------------------------------------

        if now >= next_pipeline:

            run_pipeline()

            # Schedule from the END of the cycle rather than the
            # beginning. This prevents multiple cycles from piling
            # up if stitching or uploading takes a long time.
            next_pipeline = (
                time.monotonic()
                + PIPELINE_INTERVAL_SECONDS
            )

        # ----------------------------------------------------
        # Sleep until something needs doing.
        #
        # This is the important part for low resource usage.
        # There is no continuous polling loop.
        # ----------------------------------------------------

        next_event = min(
            next_pipeline,
            next_archiver_watchdog,
        )

        sleep_for = max(
            0.1,
            next_event - time.monotonic(),
        )

        sleep_interruptibly(
            min(sleep_for, 5.0)
        )

    # --------------------------------------------------------
    # Shutdown
    # --------------------------------------------------------

    logging.info(
        "Master supervisor shutting down."
    )

    stop_archiver()

    logging.info(
        "Master supervisor stopped."
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

        logging.info(
            "Interrupted by user."
        )

        stop_archiver()
