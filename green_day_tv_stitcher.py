import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zoneinfo import ZoneInfo


# ============================================================
# Green Day TV - Daily Stitcher
#
# Finds archived clips, splits them by calendar day, resolves
# overlaps by quality/length, marks genuine gaps, and stitches
# the resulting timeline into one daily MKV.
#
# No source footage is re-encoded. FFmpeg uses -c copy whenever
# source footage needs to be trimmed.
# ============================================================

# The path containing the archived MKV files. Change this if necessary
ARCHIVE_DIR = Path(r"D:\Pietro\Archives\GreenDayTV")
OUTPUT_DIR = ARCHIVE_DIR / "Daily"

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
MKVMERGE = "mkvmerge"

# Duration in seconds of every notice/warning inserted into the final stitched video.
NOTICE_DURATION = 1.35

# Significant gap threshold (ins seconds)
# If the script detects a gap between the archived footage
# equal or longer than this value, it will insert a notice.
SIGNIFICANT_GAP = 8

# This is deprecated and here only for backwards compatibility.
# The script now calculates the timestamp using and embedded comment in the file.
# For now it's still compatible with older versions of the archiver, producing files without the comment.
# I'll remove this in a further version.
SEGMENT_RE = re.compile( r"^GreenDayTV_(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})\.mkv$" )

def sanitise_windows_path(path: Path):
    return path.as_posix().replace(":", r"\:")


def run(command: list[str]):
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def probe_duration(path: Path):
    result = run([
        FFPROBE,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        str(path),
    ])

    try:
        data = json.loads(result.stdout)
        duration = data.get("format", {}).get("duration")
        if duration is not None:
            return float(duration)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    result = run([
        FFPROBE,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,duration_time",
        "-of", "csv=p=0",
        str(path),
    ])

    last_pts = None
    last_duration = 0.0

    for line in result.stdout.splitlines():
        if not line.strip():
            continue

        parts = line.split(",")

        try:
            pts = float(parts[0])
            duration = float(parts[1]) if len(parts) > 1 and parts[1] else 0.0
        except (ValueError, IndexError):
            continue

        last_pts = pts
        last_duration = duration

    if last_pts is None:
        raise RuntimeError(f"Could not determine duration of {path}")

    return last_pts + last_duration


def probe_media_info(path: Path):
    result = run([
        FFPROBE,
        "-v", "error",
        "-show_entries",
        (
            "format=format_name,bit_rate:"
            "stream=index,codec_type,codec_name,profile,level,"
            "width,height,pix_fmt,r_frame_rate,avg_frame_rate,"
            "bit_rate,sample_rate,channels,channel_layout"
        ),
        "-of", "json",
        str(path),
    ])

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Could not parse ffprobe output for {path}: {exc}"
        )

    streams = data.get("streams", [])
    format_data = data.get("format", {})

    video = next(
        (s for s in streams if s.get("codec_type") == "video"),
        {},
    )

    audio = next(
        (s for s in streams if s.get("codec_type") == "audio"),
        {},
    )

    def parse_rate(value):
        if not value or value == "0/0":
            return 0.0

        value = str(value)

        if "/" in value:
            numerator, denominator = value.split("/", 1)

            try:
                denominator = float(denominator)
                return float(numerator) / denominator if denominator else 0.0
            except ValueError:
                return 0.0

        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    return {
        "container": (
            format_data.get("format_name") or ""
        ).lower(),

        "video_codec": (
            video.get("codec_name") or ""
        ).lower(),

        "video_profile": (
            video.get("profile") or ""
        ),

        "video_level": (
            video.get("level")
            or 0
        ),

        "pixel_format": (
            video.get("pix_fmt") or ""
        ),

        "audio_codec": (
            audio.get("codec_name") or ""
        ).lower(),

        "audio_profile": (
            audio.get("profile") or ""
        ),

        "width": int(
            video.get("width") or 0
        ),

        "height": int(
            video.get("height") or 0
        ),

        "fps": max(
            parse_rate(
                video.get("avg_frame_rate")
            ),
            parse_rate(
                video.get("r_frame_rate")
            ),
        ),

        "video_bitrate": int(
            video.get("bit_rate") or 0
        ),

        "audio_bitrate": int(
            audio.get("bit_rate") or 0
        ),

        "audio_sample_rate": int(
            audio.get("sample_rate") or 0
        ),

        "audio_channels": int(
            audio.get("channels") or 0
        ),

        "audio_channel_layout": (
            audio.get("channel_layout") or ""
        ),
    }


def probe_audio_sample_rate(path: Path):
    result = run([
        FFPROBE,
        "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])

    value = result.stdout.strip()

    if not value:
        raise RuntimeError(f"Could not determine audio sample rate of {path}")

    return int(value)
    

def parse_filename(path: Path):
    """
    Legacy filename parser.
    
    This is DEPRECATED and here only for backwards compatibility.
    The script now calculates the timestamp using and embedded comment in the file.
    For now it's still compatible with older versions of the archiver, producing files without the comment.
    I'll remove this in a further version.

    Returns a NAIVE datetime because the timezone of old files
    is not known here. The caller is responsible for assigning
    the user-selected legacy timezone and converting it to UTC.
    """
    match = SEGMENT_RE.match(path.name)

    if not match:
        return None

    date_part, time_part = match.groups()

    return datetime.strptime(
        f"{date_part} {time_part}",
        "%Y-%m-%d %H-%M-%S",
    )


def probe_embedded_timestamp(path: Path):
    result = run([
        FFPROBE,
        "-v", "error",
        "-show_entries", "format_tags=comment",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])

    value = result.stdout.strip()

    if not value:
        return None

    try:
        timestamp = datetime.strptime(
            value,
            "%Y-%m-%dT%H:%M:%SZ",
        )

        return timestamp.replace(
            tzinfo=timezone.utc
        )

    except ValueError:
        raise RuntimeError(
            f"Invalid embedded timestamp in {path}: {value!r}"
        )


def get_source_segments():
    segments = []
    legacy_timezone = None

    for path in ARCHIVE_DIR.glob("GreenDayTV_*.mkv"):
        if path.name.endswith(".local.mkv"):
            continue
            
        try:
            start = probe_embedded_timestamp(path)
            duration = probe_duration(path)
            media = probe_media_info(path)
            
            # Backwards compatibility:
            # fall back to the old filename timestamp if the
            # embedded comment is missing or invalid.
            if start is None:
                print(
                    f"[WARNING] No embedded timestamp comment found in "
                    f"{path}. Using legacy filename parsing logic."
                )

                if legacy_timezone is None:
                    while True:
                        timezone_name = input(
                            "Enter the IANA timezone used by these legacy "
                            "files (for example 'America/Los_Angeles', 'Europe/London'...): "
                        ).strip()

                        try:
                            legacy_timezone = ZoneInfo(timezone_name)
                            break
                        except Exception:
                            print(
                                f"[ERROR] Invalid timezone: {timezone_name!r}. "
                                "Please enter a valid IANA timezone."
                            )

                start = parse_filename(path)

                if start is None:
                    continue

                start = start.replace(
                    tzinfo=legacy_timezone
                ).astimezone(timezone.utc)
                
                

        except (
            RuntimeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            print(f"Skipping {path}: {exc}")
            continue

        end = start + timedelta(
            seconds=duration
        )

        segments.append({
            "path": path,
            "source_start": start,
            "source_end": end,
            "start": start,
            "end": end,
            "duration": duration,
            "media": media,
        })

    segments.sort(
        key=lambda s: (
            s["source_start"],
            s["path"].name,
        )
    )

    return segments


def split_by_day(segment: dict):
    parts = []

    source_start = segment["source_start"]
    source_end = segment["source_end"]
    current = source_start

    while current.date() < source_end.date():
        midnight = datetime.combine(
            current.date() + timedelta(days=1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )

        parts.append({
            "path": segment["path"],
            "source_start": source_start,
            "source_end": source_end,
            "start": current,
            "end": midnight,
            "duration": segment["duration"],
            "media": segment["media"],
        })

        current = midnight

    if current < source_end:
        parts.append({
            "path": segment["path"],
            "source_start": source_start,
            "source_end": source_end,
            "start": current,
            "end": source_end,
            "duration": segment["duration"],
            "media": segment["media"],
        })

    return parts

def build_day_segments(source_segments: list[dict]):
    days = {}

    for segment in source_segments:
        for part in split_by_day(segment):
            days.setdefault(part["start"].date(), []).append(part)

    for day in days:
        days[day].sort(
            key=lambda s: (s["start"], s["path"].name)
        )

    return days


# ============================================================
# Overlap resolution
# ============================================================

def get_preferred_output_profile(segments: list[dict]):
    if not segments:
        return None

    best = max(
        segments,
        key=lambda s: (
            s["media"]["height"],
            s["media"]["width"],
            s["media"]["fps"],
            s["media"]["video_bitrate"],
            s["duration"],
            s["path"].name,
        ),
    )

    media = best["media"]

    return {
        "container": media["container"],
        "video_codec": media["video_codec"],
        "audio_codec": media["audio_codec"],
    }


def source_quality_key(segment: dict, preferred_profile: dict):
    media = segment["media"]

    return (
        media["container"] == preferred_profile["container"],
        media["video_codec"] == preferred_profile["video_codec"],
        media["audio_codec"] == preferred_profile["audio_codec"],
        media["height"],
        media["width"],
        media["fps"],
        media["video_bitrate"],
        segment["duration"],
        segment["path"].name,
    )


def resolve_overlaps(segments: list[dict]):
    if not segments:
        return []

    preferred_profile = get_preferred_output_profile(segments)

    boundaries = sorted({
        boundary
        for segment in segments
        for boundary in (segment["start"], segment["end"])
    })

    resolved = []

    for left, right in zip(boundaries, boundaries[1:]):
        if right <= left:
            continue

        active = [
            segment
            for segment in segments
            if segment["start"] <= left and segment["end"] >= right
        ]

        if not active:
            resolved.append({
                "kind": "gap",
                "start": left,
                "end": right,
            })
            continue

        winner = max(
            active,
            key=lambda s: source_quality_key(s, preferred_profile)
        )

        resolved.append({
            "kind": "footage",
            "path": winner["path"],
            "source_start": winner["source_start"],
            "source_end": winner["source_end"],
            "start": left,
            "end": right,
            "media": winner["media"],
        })

    merged = []

    for item in resolved:
        if not merged:
            merged.append(item)
            continue

        previous = merged[-1]

        if (
            item["kind"] == "footage"
            and previous["kind"] == "footage"
            and item["path"] == previous["path"]
            and item["start"] == previous["end"]
        ):
            previous["end"] = item["end"]
            continue

        if (
            item["kind"] == "gap"
            and previous["kind"] == "gap"
            and item["start"] == previous["end"]
        ):
            previous["end"] = item["end"]
            continue

        merged.append(item)

    return merged


# ============================================================
# FFmpeg / MKV helpers
# ============================================================

def make_ffmpeg_part(segment: dict, output: Path):
    offset = (
        segment["start"] - segment["source_start"]
    ).total_seconds()

    duration = (
        segment["end"] - segment["start"]
    ).total_seconds()

    result = run([
        FFMPEG,
        "-y",
        "-ss", str(max(0.0, offset)),
        "-i", str(segment["path"]),
        "-t", str(max(0.0, duration)),
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        str(output),
    ])

    if result.returncode != 0:
        print(result.stderr)
        raise RuntimeError(
            f"FFmpeg failed while cutting {segment['path']}"
        )


def get_reference_media(segments):
    """
    Choose the media profile that will be used to generate
    notices and warnings for this day.

    The first source in the resolved timeline is deliberately
    used because it is the first actual footage that will be
    appended to the daily file.
    """

    for segment in segments:
        if segment.get("kind") == "footage":
            return segment["media"]

    raise RuntimeError(
        "Could not find a footage segment to use as the "
        "reference media profile."
    )


def aac_profile_option(profile):
    profile = (profile or "").lower().strip()

    if profile in {"lc", "aac low complexity"}:
        return "aac_low"

    if profile in {"main", "aac main"}:
        return "aac_main"

    if profile in {"ssr", "aac ssr"}:
        return "aac_ssr"

    if profile in {"ltp", "aac ltp"}:
        return "aac_ltp"

    if profile in {"he-aac", "aac he"}:
        return "aac_he"

    if profile in {"he-aacv2", "aac he v2"}:
        return "aac_he_v2"

    return None
    

def h264_profile_option(profile):
    profile = (profile or "").lower()

    if profile == "baseline":
        return "baseline"

    if profile == "main":
        return "main"

    if profile == "high":
        return "high"

    if "high 10" in profile:
        return "high10"

    if "high 4:2:2" in profile:
        return "high422"

    if "high 4:4:4" in profile:
        return "high444"

    return "high"


def make_notice(text: str, output: Path, media: dict):
    output.parent.mkdir(parents=True, exist_ok=True)

    font = sanitise_windows_path(
        Path(r"C:\Windows\Fonts\arial.ttf")
    )

    textfile = output.with_suffix(".txt")
    textfile.write_text(text, encoding="utf-8")

    width = media["width"] or 1920
    height = media["height"] or 1080
    fps = media["fps"] or 30
    pixel_format = media["pixel_format"] or "yuv420p"

    audio_sample_rate = media["audio_sample_rate"] or 48000
    audio_channels = media["audio_channels"] or 2
    audio_layout = media["audio_channel_layout"] or "stereo"

    video_bitrate = media["video_bitrate"]
    audio_bitrate = media["audio_bitrate"]

    command = [
        FFMPEG,
        "-y",

        # Video source
        "-f", "lavfi",
        "-i",
        f"color=c=black:s={width}x{height}:r={fps}",

        # Audio source
        "-f", "lavfi",
        "-i",
        f"anullsrc=r={audio_sample_rate}:cl={audio_layout}",

        "-t", str(NOTICE_DURATION),

        # Text
        "-vf",
        (
            "drawtext="
            f"fontfile='{font}':"
            f"textfile='{sanitise_windows_path(textfile)}':"
            "fontcolor=white:"
            "fontsize=48:"
            "text_align=M+C:"
            "x=(w-text_w)/2:"
            "y=(h-text_h)/2:"
            "line_spacing=12"
        ),

        # Video
        "-c:v", "libx264",
        "-pix_fmt", pixel_format,
        "-r", str(fps),
        "-s", f"{width}x{height}",
        "-profile:v", h264_profile_option(media["video_profile"]),

        # Audio
        "-c:a", "aac",
        "-ar", str(audio_sample_rate),
        "-ac", str(audio_channels),
        "-b:a", str(audio_bitrate if audio_bitrate > 0 else 192000),

        "-shortest",

        str(output),
    ]

    if video_bitrate > 0:
        command.extend([
            "-b:v", str(video_bitrate),
        ])
    else:
        command.extend([
            "-crf", "20",
        ])

    aac_profile = aac_profile_option(
        media["audio_profile"]
    )

    if aac_profile:
        audio_codec_index = command.index("-c:a")
        command[ audio_codec_index + 2:audio_codec_index + 2 ] = [
            "-profile:a",
            aac_profile,
        ]

    result = run(command)

    if result.returncode != 0:
        print(result.stderr)
        raise RuntimeError(
            f"FFmpeg failed while creating notice {output}"
        )

def remux_for_mkvmerge(
    source: Path,
    output: Path,
    reference_media: dict | None = None,
):
    print(f"repairing container: {source}")

    command = [
        FFMPEG,
        "-y",
        "-i", str(source),
        "-map", "0",
        "-c:v", "copy",
    ]

    if reference_media is not None:
        audio_profile = reference_media.get("audio_profile", "")

        profile = aac_profile_option(audio_profile)

        if profile:
            command.extend([
                "-c:a", "aac",
                "-profile:a", profile,
            ])
        else:
            command.extend([
                "-c:a", "aac",
                "-b:a",
                str(
                    reference_media["audio_bitrate"]
                    or 192000
                ),
            ])
    else:
        command.extend([
            "-c:a", "copy",
        ])

    command.append(str(output))

    result = run(command)

    if result.returncode != 0:
        print(result.stderr)
        raise RuntimeError(
            f"FFmpeg could not repair {source}"
        )
        

def find_failed_inputs(result, parts):
    error_text = result.stdout + "\n" + result.stderr
    failed = []

    for line in error_text.splitlines():
        if "Error:" not in line:
            continue

        for part in parts:
            if str(part) in line and part not in failed:
                failed.append(part)

    return failed


def build_mkvmerge_command(parts, output):
    command = [
        MKVMERGE,
        "-o", str(output),
    ]

    for index, part in enumerate(parts):
        if index == 0:
            command.append(str(part))
        else:
            command.extend(["+", str(part)])

    return command
    
def concat_parts(
    parts: list[Path],
    output: Path,
    work_dir: Path,
    reference_media: dict,
):
    def build_command(inputs):
        command = [
            MKVMERGE,
            "-o", str(output),
        ]

        for index, part in enumerate(inputs):
            if index == 0:
                command.append(str(part))
            else:
                command.extend(["+", str(part)])

        return command

    # --------------------------------------------------------
    # First attempt: use every file exactly as it is.
    # --------------------------------------------------------

    command = build_command(parts)
    result = run(command)

    print(*command, sep=" ")

    if result.returncode in (0, 1):
        return

    # --------------------------------------------------------
    # Find the files explicitly named in mkvmerge errors.
    # --------------------------------------------------------

    failed_parts = find_failed_inputs(
        result,
        parts,
    )

    if not failed_parts:
        print(result.stdout)
        print(result.stderr)

        raise RuntimeError(
            f"mkvmerge returned status {result.returncode}, "
            "but no failing input file could be identified"
        )

    print("mkvmerge rejected these input file(s):")

    for part in failed_parts:
        print(f"  {part}")

    # --------------------------------------------------------
    # Repair only files mkvmerge actually rejected.
    # --------------------------------------------------------

    repaired_parts = list(parts)

    for part in failed_parts:
        index = repaired_parts.index(part)

        repaired = (
            work_dir
            / f"repair_{index:04d}.mkv"
        )

        remux_for_mkvmerge(
            part,
            repaired,
            reference_media,
        )

        repaired_parts[index] = repaired

    # --------------------------------------------------------
    # Try the final mux.
    # --------------------------------------------------------

    command = build_command(
        repaired_parts
    )

    result = run(command)

    print(*command, sep=" ")

    if result.returncode not in (0, 1):
        print(result.stdout)
        print(result.stderr)

        raise RuntimeError(
            "mkvmerge still failed after repairing "
            f"{len(failed_parts)} file(s)"
        )
        

# ============================================================
# Time formatting
# ============================================================

def format_time(value):
    return value.strftime("%H:%M:%S")


def format_delta(hours=0, minutes=0, seconds=0):
    parts = []

    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")

    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")

    if seconds:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")

    if not parts:
        return "0 seconds"

    if len(parts) == 1:
        return parts[0]

    if len(parts) == 2:
        return " and ".join(parts)

    return ", ".join(parts[:-1]) + " and " + parts[-1]


# ============================================================
# Process one day
# ============================================================

def process_day(day, segments: list[dict]):
    output = OUTPUT_DIR / f"GreenDayTV_{day:%Y-%m-%d}.mkv"

    if output.exists():
        print(f"Daily file already exists: {output}")
        return

    day_start = datetime.combine(
        day,
        datetime.min.time(),
        tzinfo=timezone.utc,
    )

    day_end = day_start + timedelta(days=1)

    if not segments:
        return

    day_segments = []

    for source in segments:
        start = max(source["start"], day_start)
        end = min(source["end"], day_end)

        if end <= start:
            continue

        part = dict(source)
        part["start"] = start
        part["end"] = end
        day_segments.append(part)

    if not day_segments:
        return

    timeline = resolve_overlaps(day_segments)
    reference_media = get_reference_media(timeline)

    complete = (
        bool(timeline)
        and timeline[0]["kind"] == "footage"
        and timeline[0]["start"] <= day_start
        and timeline[-1]["kind"] == "footage"
        and timeline[-1]["end"] >= day_end
        and not any(item["kind"] == "gap" for item in timeline)
    )

    work_dir = OUTPUT_DIR / f".work_{day:%Y-%m-%d}"
    work_dir.mkdir(parents=True, exist_ok=True)

    parts = []
    previous_end = day_start
    clip_index = 1

    try:
        if not complete:
            warning = work_dir / "000_warning.mkv"
            make_notice(
                "The archived footage for this day is incomplete",
                warning,
                reference_media,
            )
            parts.append(warning)

        for item in timeline:
            if item["kind"] == "gap":
                gap_start = item["start"]
                gap_end = item["end"]
                gap_seconds = (gap_end - gap_start).total_seconds()

                if gap_seconds >= SIGNIFICANT_GAP:
                    hours = int(gap_seconds // 3600)
                    minutes = int((gap_seconds % 3600) // 60)
                    seconds = int(gap_seconds % 60)

                    notice = work_dir / f"{clip_index:04d}_notice.mkv"

                    if previous_end <= day_start:
                        prefix = "The first captured footage"
                    else:
                        prefix = "The next captured footage"

                    text = (
                        f"{prefix} starts at {format_time(gap_end)}.\n"
                        f"({format_delta(hours, minutes, seconds)} gap)"
                    )

                    make_notice(
                        text,
                        notice,
                        reference_media,
                    )
                    parts.append(notice)

                previous_end = gap_end
                clip_index += 1
                continue

            print(
                f"{item['path']} "
                f"{format_time(item['start'])} -> "
                f"{format_time(item['end'])}"
            )

            start_offset = (
                item["start"] - item["source_start"]
            ).total_seconds()

            end_offset = (
                item["source_end"] - item["end"]
            ).total_seconds()

            needs_editing = (
                start_offset > 0.01
                or end_offset > 0.01
            )

            if needs_editing:
                footage = work_dir / f"{clip_index:04d}_footage.mkv"
                print("making edited part")
                make_ffmpeg_part(item, footage)
                parts.append(footage)
            else:
                print("using original footage")
                parts.append(item["path"])

            previous_end = item["end"]
            clip_index += 1

        if parts:
            concat_parts(
                parts,
                output,
                work_dir,
                reference_media,
            )
        else:
            print(f"No usable footage for {day}")

    finally:
        for path in work_dir.glob("*"):
            path.unlink(missing_ok=True)

        try:
            work_dir.rmdir()
        except OSError:
            pass


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    today = datetime.now().replace(tzinfo=timezone.utc).date() # Convert today to UTC: every time(-zone)-related operation should always use UTC.
    source_segments = get_source_segments()
    days = build_day_segments(source_segments)

    print("Segments for each day:", end="")

    for day, segments in sorted(days.items()):
        print("\n", day, len(segments), end="")

        for segment in segments:
            print("\n", segment["path"], (segment["end"] - segment["start"]).total_seconds(), end="")

        if day >= today:
            continue

        print()
        process_day(day, segments)
        print()


if __name__ == "__main__":
    main()
