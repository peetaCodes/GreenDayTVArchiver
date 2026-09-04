from __future__ import annotations

import gzip
import logging
import os
import shutil
import threading
from pathlib import Path

class FileLogFilter(logging.Filter):
    """
    Remove extremely repetitive low-value yt-dlp / FFmpeg transport
    messages from the persistent log file.

    The console logger remains completely unfiltered.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()

        # Continuous HLS segment/playlist opening messages.
        #
        # Examples:
        #   [yt-dlp] [https @ ...] Opening 'https://...' for reading
        #   [yt-dlp] [in#0 @ ...] Opening 'https://...' for reading
        #
        # These are useful live in the terminal but generate enormous
        # amounts of redundant log data over days/weeks.
        if "[yt-dlp]" in message and "Opening \'" in message and "for reading" in message:
            return False

        return True


class GZipRotatingFileHandler(logging.Handler):
    """
    Size-based rotating log handler.

    When it reaches MAX_BYTES it is renamed to:
        archiver.log.2026-09-02_12-34-56.log.gz

    Compression is performed in a background thread so the logging call
    itself does not spend time compressing the old file.

    Old compressed logs are retained indefinitely unless TOTAL_MAX_BYTES
    is configured to a non-zero value.
    """

    def __init__(
        self,
        filename: str | Path,
        max_bytes: int = 50 * 1024 * 1024,
        total_max_bytes: int = 0,
        compresslevel: int = 6,
        encoding: str = "utf-8",
    ) -> None:
        super().__init__()

        self.filename = Path(filename)
        self.max_bytes = max_bytes
        self.total_max_bytes = total_max_bytes
        self.compresslevel = compresslevel
        self.encoding = encoding

        self.filename.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._compression_threads: set[threading.Thread] = set()

        self._stream = self._open_stream()

    def _open_stream(self):
        return self.filename.open(
            mode="a",
            encoding=self.encoding,
            newline="",
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)

            # Make one atomic-ish operation under the handler lock.
            with self._lock:
                self._stream.write(message + "\n")
                self._stream.flush()

                try:
                    current_size = self.filename.stat().st_size
                except OSError:
                    current_size = 0

                if current_size >= self.max_bytes:
                    self._rotate_locked()

        except Exception:
            self.handleError(record)

    def _rotate_locked(self) -> None:
        """
        Rotate the active log.

        This function must be called while self._lock is held.
        """
        try:
            self._stream.flush()
            self._stream.close()
        except Exception:
            pass

        if not self.filename.exists():
            self._stream = self._open_stream()
            return

        # Find a unique destination name.
        timestamp = __import__("datetime").datetime.now().strftime(
            "%Y-%m-%d_%H-%M-%S"
        )

        rotated_plain = (
            self.filename.parent
            / f"{self.filename.stem}.{timestamp}.log"
        )

        counter = 1
        while rotated_plain.exists():
            rotated_plain = (
                self.filename.parent
                / f"{self.filename.stem}.{timestamp}.{counter}.log"
            )
            counter += 1

        try:
            self.filename.replace(rotated_plain)
        except OSError:
            # Re-open the current log and leave it untouched if rotation failed.
            self._stream = self._open_stream()
            return

        self._stream = self._open_stream()

        # Compress asynchronously.
        thread = threading.Thread(
            target=self._compress_worker,
            args=(rotated_plain,),
            name="log-compressor",
            daemon=True,
        )

        self._compression_threads.add(thread)
        thread.start()

    def _compress_worker(self, source: Path) -> None:
        try:
            destination = source.with_suffix(source.suffix + ".gz")

            # Compress in reasonably sized chunks instead of loading the
            # entire log into RAM.
            with (
                source.open("rb") as src,
                gzip.open(
                    destination,
                    "wb",
                    compresslevel=self.compresslevel,
                ) as dst,
            ):
                shutil.copyfileobj(src, dst, length=1024 * 1024)

            # Only delete the uncompressed historical log after successful
            # completion of the gzip file.
            source.unlink(missing_ok=True)

        except Exception:
            # Never delete the source if compression failed.
            logging.getLogger(__name__).exception(
                "Failed to compress rotated log %s",
                source,
            )

        finally:
            with self._lock:
                current = threading.current_thread()
                self._compression_threads.discard(current)

            # Enforce the optional total-history limit after compression.
            self._enforce_total_limit()

    def _enforce_total_limit(self) -> None:
        """
        Delete the oldest compressed logs only when TOTAL_MAX_BYTES is exceeded.

        A value of 0 means unlimited retention.
        """
        if self.total_max_bytes <= 0:
            return

        try:
            logs = sorted(
                self.filename.parent.glob(
                    f"{self.filename.stem}.*.log.gz"
                ),
                key=lambda path: path.stat().st_mtime,
            )
        except OSError:
            return

        total_size = 0
        sizes: list[tuple[Path, int]] = []

        for path in logs:
            try:
                size = path.stat().st_size
            except OSError:
                continue

            sizes.append((path, size))
            total_size += size

        while total_size > self.total_max_bytes and sizes:
            oldest, size = sizes.pop(0)

            try:
                oldest.unlink()
                total_size -= size
            except OSError:
                # Don't repeatedly attempt the same file during this pass.
                pass

    def close(self) -> None:
        with self._lock:
            try:
                self._stream.flush()
                self._stream.close()
            except Exception:
                pass

        # We deliberately do not wait indefinitely for compression threads.
        # They are daemon threads and the source .log files remain intact
        # until compression succeeds.
        super().close()