from __future__ import annotations

import logging
import signal
import subprocess
import sys
import threading
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
# The archiver itself already has its own reconnect/recovery
# logic. The master therefore does NOT interfere with a healthy
# archiver process.
#
# The stitcher and uploader are one-shot programs. Their complete
# stdout/stderr output is streamed into this master's log with
# clear prefixes:
#
#   [STITCHER]
#   [UPLOADER]
#
# This means the master log contains:
#
#   - every source segment reported by the stitcher
#   - the time span of each segment
#   - total missing footage for each day
#   - daily-file discovery by the uploader
#   - file paths and sizes
#   - upload attempt numbers
#   - upload starts and completions
#   - upload failures
#   - upload verification results
#
# Windows / Python 3.10+
# ============================================================


# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

BASE_DIR = Path(
    r"D:\Pietro\Archives\GreenDayTV"
)

ARCHIVER_SCRIPT = (
    BASE_DIR / "green_day_tv_archiver.py"
)

STITCHER_SCRIPT = (
    BASE_DIR / "green_day_tv_stitcher.py"
)

UPLOADER_SCRIPT = (
    BASE_DIR / "green_day_tv_uploader.py"
)


# Main config file
#
# Format:
#
# [STITCHER]
# local_timezone=CONTINENT/CITY
#
# [UPLOADER]
# access=xxxxxx
# secret=xxxxxx
#
CONFIG_FILE = (
    BASE_DIR / "gdtv_archiver_config.txt"
)


CONFIG_FILE_PRESET = """\
[STITCHER]
# Your local timezone. Needed to convert timestamps into UTC.
# Use IANA format ("Continent/City").
# Examples:
# Europe/Rome
# Europe/London
# America/Los_Angeles
# Asia/Tokyo
local_timezone=Continent/City

[UPLOADER]
# Your Internet Archive S3 API credentials.
# Get them from:
# https://archive.org/developers/tutorial-get-ia-credentials.html
#
# These credentials are private. Do NOT share them with anyone.
# They provide access to your Internet Archive account.
access=xxxxxx
secret=xxxxxx
"""


# ------------------------------------------------------------
# Maintenance interval
# ------------------------------------------------------------
#
# The stitcher/uploader pipeline runs once every 6 hours.
#
# The interval is measured from the END of one maintenance
# cycle to the START of the next one.
#
# This keeps the master extremely lightweight while idle.
#
PIPELINE_INTERVAL_SECONDS = (
    6 * 60 * 60
)


# ------------------------------------------------------------
# Archiver watchdog
# ------------------------------------------------------------
#
# This check only asks whether the archiver process still exists.
# poll() is extremely cheap.
#
# Please keep this above 8 seconds.
#
ARCHIVER_WATCHDOG_INTERVAL_SECONDS = 15


# If the archiver dies, wait this long before restarting it.
#
# Please keep this above 3 seconds.
#
ARCHIVER_RESTART_DELAY_SECONDS = 5


# Small delay between stitcher and uploader.
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

        logging.info(
            "Shutdown requested."
        )


# ------------------------------------------------------------
# Interruptible sleep
# ------------------------------------------------------------

def sleep_interruptibly(
    seconds: float,
):
    """
    Sleep without busy-polling.

    The master therefore consumes essentially no CPU while idle.
    """

    end_time = (
        time.monotonic()
        + seconds
    )

    while not STOP_REQUESTED:

        remaining = (
            end_time
            - time.monotonic()
        )

        if remaining <= 0:
            break

        time.sleep(
            min(
                remaining,
                5.0,
            )
        )


# ------------------------------------------------------------
# Command-line argument formatting
# ------------------------------------------------------------

def format_arg(
    variable_name: str,
):
    """
    Convert a Python keyword-style argument name into a CLI
    argument.

    Example:

        local_timezone
            ->
        --local-timezone
    """

    prefix = (
        "--"
        if len(variable_name) > 1
        else "-"
    )

    return (
        prefix
        + variable_name.replace(
            "_",
            "-",
        )
    )


# ------------------------------------------------------------
# Python command
# ------------------------------------------------------------

def python_command(
    script: Path,
    **kwargs,
) -> list[str]:
    """
    Build a Python command line.

    -u is deliberately used so child stdout/stderr is unbuffered.
    This allows the master to write child output to master.log while
    the child is still running.
    """

    command = [
        sys.executable,
        "-u",
        str(script),
    ]

    command += [
        argument
        for arg, value in kwargs.items()
        for argument in (
            format_arg(arg),
            str(value),
        )
    ]

    return command


# ------------------------------------------------------------
# Configuration file
# ------------------------------------------------------------

def make_new_config():
    if CONFIG_FILE.exists():

        if CONFIG_FILE.stat().st_size != 0:

            proceed = input(
                (
                    f"WARNING: config file found at "
                    f"'{CONFIG_FILE}' and is not empty.\n"
                    "Overwrite it? (y/N) "
                )
            )

            if proceed.lower() != "y":

                print(
                    "Operation aborted by user."
                )

                sys.exit(0)

    CONFIG_FILE.write_text(
        CONFIG_FILE_PRESET,
        encoding="utf-8",
    )


def load_config():
    config = {
        "stitcher": {
            "local_timezone": "",
        },

        "uploader": {
            "access": "",
            "secret": "",
        },
    }

    if not CONFIG_FILE.exists():

        make_new_config()

        raise RuntimeError(
            (
                "GDTV Archiver master config file "
                f"does not exist:\n{CONFIG_FILE}\n\n"
                "A blank configuration file has now been "
                "created. Please edit it with the required "
                "information and run the master again."
            )
        )

    current_conf = None

    with CONFIG_FILE.open(
        "r",
        encoding="utf-8",
    ) as file:

        for raw_line in file:

            line = raw_line.strip()

            if not line:
                continue

            if line.startswith("#"):
                continue

            if line == "[STITCHER]":

                current_conf = "stitcher"
                continue

            if line == "[UPLOADER]":

                current_conf = "uploader"
                continue

            if "=" not in line:
                continue

            key, _, value = (
                line.partition("=")
            )

            key = key.strip()
            value = value.strip()

            if (
                current_conf == "stitcher"
                and key == "local_timezone"
            ):

                if config["stitcher"][
                    "local_timezone"
                ]:

                    raise RuntimeError(
                        (
                            "The '[STITCHER]' section "
                            "contains two or more "
                            "'local_timezone=' values. "
                            "Exactly one is needed."
                        )
                    )

                config["stitcher"][
                    "local_timezone"
                ] = value

            if (
                current_conf == "uploader"
                and key == "access"
            ):

                if config["uploader"]["access"]:

                    raise RuntimeError(
                        (
                            "The '[UPLOADER]' section "
                            "contains two or more "
                            "'access=' values. "
                            "Exactly one is needed."
                        )
                    )

                config["uploader"]["access"] = (
                    value
                )

            if (
                current_conf == "uploader"
                and key == "secret"
            ):

                if config["uploader"]["secret"]:

                    raise RuntimeError(
                        (
                            "The '[UPLOADER]' section "
                            "contains two or more "
                            "'secret=' values. "
                            "Exactly one is needed."
                        )
                    )

                config["uploader"]["secret"] = (
                    value
                )

    if (
        not all(config["stitcher"].values())
        or not all(config["uploader"].values())
    ):

        raise RuntimeError(
            (
                f"The config file at '{CONFIG_FILE}' "
                "does not contain every required value.\n\n"
                "The required format is:\n\n"
                f"{CONFIG_FILE_PRESET}"
            )
        )

    return config


# ------------------------------------------------------------
# Start archiver
# ------------------------------------------------------------

def start_archiver():

    global ARCHIVER_PROCESS

    if not ARCHIVER_SCRIPT.exists():

        raise RuntimeError(
            f"Archiver script does not exist:\n"
            f"{ARCHIVER_SCRIPT}"
        )

    logging.info(
        "Starting archiver: %s",
        ARCHIVER_SCRIPT,
    )

    creationflags = (
        subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.CREATE_NO_WINDOW
    )

    try:

        ARCHIVER_PROCESS = subprocess.Popen(

            python_command(
                ARCHIVER_SCRIPT
            ),

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

    if (
        ARCHIVER_PROCESS.poll()
        is not None
    ):

        ARCHIVER_PROCESS = None
        return

    logging.info(
        "Stopping archiver (PID %d)...",
        ARCHIVER_PROCESS.pid,
    )

    try:

        ARCHIVER_PROCESS.send_signal(
            signal.CTRL_BREAK_EVENT
        )

    except Exception:

        logging.exception(
            "Could not send clean shutdown signal "
            "to archiver."
        )

    try:

        ARCHIVER_PROCESS.wait(
            timeout=10
        )

    except subprocess.TimeoutExpired:

        logging.warning(
            "Archiver did not stop cleanly; "
            "terminating it."
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

    global ARCHIVER_PROCESS

    if ARCHIVER_PROCESS is None:

        logging.warning(
            "Archiver process is not running."
        )

        if not start_archiver():
            return

        return

    return_code = (
        ARCHIVER_PROCESS.poll()
    )

    if return_code is None:

        # Healthy. Do absolutely nothing.
        return

    logging.error(
        "Archiver process exited with "
        "return code %s.",
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
# Stream child-process output into the master log
# ------------------------------------------------------------

def _log_child_stream(
    stream,
    prefix: str,
    level: int,
):
    """
    Continuously read one child-process output stream and send
    each complete line to the master logger.

    The master logger has both:
        - the console handler
        - the master.log file handler

    therefore child output appears in both places.
    """

    try:

        for line in iter(
            stream.readline,
            "",
        ):

            if not line:
                break

            line = line.rstrip()

            if not line:
                continue

            logging.log(
                level,
                "[%s] %s",
                prefix,
                line,
            )

    except Exception:

        logging.exception(
            "Failed while reading [%s] output.",
            prefix,
        )

    finally:

        try:
            stream.close()

        except Exception:
            pass


# ------------------------------------------------------------
# Run a Python child process and log all its output
# ------------------------------------------------------------

def run_logged_python(
    script: Path,
    prefix: str,
    *args: str,
) -> bool:
    """
    Run a one-shot Python program.

    All stdout/stderr is streamed into the master logger while
    the program is running.

    Returns True only when the child exits with return code 0.
    """

    if not script.exists():

        logging.error(
            "%s script does not exist:\n%s",
            prefix,
            script,
        )

        return False

    command = [
        sys.executable,
        "-u",
        str(script),
        *args,
    ]

    logging.info(
        "[%s] Process starting: %s",
        prefix,
        script,
    )

    logging.info(
        "[%s] Command started.",
        prefix,
    )

    start_time = time.monotonic()

    try:

        process = subprocess.Popen(

            command,

            stdin=subprocess.DEVNULL,

            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,

            text=True,
            encoding="utf-8",
            errors="replace",

            bufsize=1,
        )

    except Exception:

        logging.exception(
            "[%s] Could not start process.",
            prefix,
        )

        return False

    stdout_thread = threading.Thread(
        target=_log_child_stream,
        args=(
            process.stdout,
            prefix,
            logging.INFO,
        ),
        daemon=True,
        name=f"{prefix.lower()}-stdout",
    )

    stderr_thread = threading.Thread(
        target=_log_child_stream,
        args=(
            process.stderr,
            prefix,
            logging.WARNING,
        ),
        daemon=True,
        name=f"{prefix.lower()}-stderr",
    )

    stdout_thread.start()
    stderr_thread.start()

    return_code = None

    try:

        return_code = process.wait()

    except Exception:

        logging.exception(
            "[%s] Error while waiting for process.",
            prefix,
        )

        try:

            process.kill()

        except Exception:
            pass

        return False

    stdout_thread.join(
        timeout=5
    )

    stderr_thread.join(
        timeout=5
    )

    elapsed = (
        time.monotonic()
        - start_time
    )

    if return_code == 0:

        logging.info(
            "[%s] Process completed successfully "
            "after %.1f seconds.",
            prefix,
            elapsed,
        )

        return True

    logging.error(
        "[%s] Process FAILED with return code "
        "%s after %.1f seconds.",
        prefix,
        return_code,
        elapsed,
    )

    return False


# ------------------------------------------------------------
# Run stitcher
# ------------------------------------------------------------

def run_stitcher(
    config: dict,
):

    logging.info("")

    logging.info(
        "============================================================"
    )

    logging.info(
        "STARTING DAILY STITCHER"
    )

    logging.info(
        "============================================================"
    )

    logging.info(
        "Stitcher archive directory: %s",
        BASE_DIR,
    )

    logging.info(
        "Stitcher local timezone: %s",
        config["local_timezone"],
    )

    success = run_logged_python(

        STITCHER_SCRIPT,

        "STITCHER",

        "--local-timezone",
        config["local_timezone"],

        "--non-interactive",
    )

    if success:

        logging.info(
            "Daily stitcher completed successfully."
        )

    else:

        logging.error(
            "Daily stitcher FAILED."
        )

    return success


# ------------------------------------------------------------
# Run uploader
# ------------------------------------------------------------

def run_uploader(
    config: dict,
):

    logging.info("")

    logging.info(
        "============================================================"
    )

    logging.info(
        "STARTING INTERNET ARCHIVE UPLOADER"
    )

    logging.info(
        "============================================================"
    )

    logging.info(
        "Uploader daily-directory: %s",
        BASE_DIR / "Daily",
    )

    # Deliberately do NOT log the credentials themselves.

    success = run_logged_python(

        UPLOADER_SCRIPT,

        "UPLOADER",

        "--access-key",
        config["access"],

        "--secret-key",
        config["secret"],
    )

    if success:

        logging.info(
            "Internet Archive uploader completed successfully."
        )

    else:

        logging.error(
            "Internet Archive uploader FAILED."
        )

    return success


# ------------------------------------------------------------
# Run complete archive-maintenance cycle
# ------------------------------------------------------------

def run_pipeline(
    config: dict,
):

    logging.info("")

    logging.info(
        "############################################################"
    )

    logging.info(
        "STARTING ARCHIVE MAINTENANCE CYCLE"
    )

    logging.info(
        "############################################################"
    )

    cycle_start = time.monotonic()

    # --------------------------------------------------------
    # Stitcher
    # --------------------------------------------------------

    stitcher_ok = run_stitcher(
        config["stitcher"]
    )

    # --------------------------------------------------------
    # Give the OS a moment to release file handles.
    # --------------------------------------------------------

    sleep_interruptibly(
        BETWEEN_PIPELINE_STEPS_SECONDS
    )

    # --------------------------------------------------------
    # Uploader
    # --------------------------------------------------------

    uploader_ok = run_uploader(
        config["uploader"]
    )

    # --------------------------------------------------------
    # Cycle summary
    # --------------------------------------------------------

    cycle_elapsed = (
        time.monotonic()
        - cycle_start
    )

    logging.info("")

    logging.info(
        "Archive maintenance cycle finished "
        "after %.1f seconds.",
        cycle_elapsed,
    )

    logging.info(
        "  Stitcher: %s",
        "OK" if stitcher_ok else "FAILED",
    )

    logging.info(
        "  Uploader: %s",
        "OK" if uploader_ok else "FAILED",
    )

    logging.info(
        "############################################################"
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
        "Pipeline interval: %.0f seconds (%.1f hours)",
        PIPELINE_INTERVAL_SECONDS,
        PIPELINE_INTERVAL_SECONDS / 3600,
    )

    logging.info(
        "Archiver watchdog interval: %.0f seconds",
        ARCHIVER_WATCHDOG_INTERVAL_SECONDS,
    )

    logging.info(
        "Master log: %s",
        BASE_DIR / "master.log",
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
    # Load configuration
    # --------------------------------------------------------

    try:

        config = load_config()

    except Exception:

        logging.exception(
            "Could not load master configuration."
        )

        return 1

    logging.info(
        "Master configuration loaded successfully."
    )

    logging.info(
        "Stitcher timezone: %s",
        config["stitcher"][
            "local_timezone"
        ],
    )

    # Deliberately do not log IA credentials.

    logging.info(
        "Internet Archive credentials loaded."
    )

    # --------------------------------------------------------
    # Start archiver
    # --------------------------------------------------------

    if not start_archiver():

        logging.error(
            "Initial archiver start failed."
        )

    # --------------------------------------------------------
    # Initial maintenance cycle
    # --------------------------------------------------------
    #
    # This happens immediately at startup.
    #
    # The next cycle will occur PIPELINE_INTERVAL_SECONDS after
    # this cycle finishes.
    #
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
        # Stitcher / uploader pipeline
        # ----------------------------------------------------

        if now >= next_pipeline:

            try:

                # Load the configuration again before every
                # maintenance cycle. This means you can change
                # configuration without restarting the master.
                config = load_config()

                run_pipeline(
                    config
                )

            except Exception:

                logging.exception(
                    "Archive maintenance cycle "
                    "raised an unexpected exception."
                )

            # Schedule the NEXT cycle from the END of this one.
            next_pipeline = (
                time.monotonic()
                + PIPELINE_INTERVAL_SECONDS
            )

        # ----------------------------------------------------
        # Sleep until the next event.
        # ----------------------------------------------------
        #
        # We cap the individual sleep at 5 seconds so Ctrl+C
        # remains responsive.
        #
        # Apart from these occasional wakeups, the master does
        # nothing while idle.
        # ----------------------------------------------------

        next_event = min(
            next_pipeline,
            next_archiver_watchdog,
        )

        sleep_for = max(
            0.1,
            next_event
            - time.monotonic(),
        )

        sleep_interruptibly(
            min(
                sleep_for,
                5.0,
            )
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

    from argparse import ArgumentParser

    parser = ArgumentParser(
        prog="Green Day TV Archiver - Main",

        description=(
            "Main component of the GDTV Archiver. "
            "It orchestrates the three components automatically. "
            "This is the only program normally needed to run."
        ),
    )

    parser.add_argument(
        "--new-config",
        action="store_true",

        help=(
            f"When passed, generate a new configuration file "
            f"at '{CONFIG_FILE}'. "
            "WARNING: if the file already exists, it may be "
            "overwritten after confirmation."
        ),
    )

    args = parser.parse_args()

    if args.new_config:

        make_new_config()

    try:

        sys.exit(
            main()
        )

    except KeyboardInterrupt:

        STOP_REQUESTED = True

        logging.info(
            "Interrupted by user."
        )

        stop_archiver()
