import os
import re
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
import internet_archive_uploader


# ============================================================
# Green Day TV - Daily Stitcher
#
# This script queries every archived clip it can find (ingoring the ones from present day),
# It then analyses them to find any gaps (if any), signals them properly
# and then stitched everything together into (hopefully) 24hrs-long daily files.
#
# ============================================================

UPLOAD_TO_INTERNET_ARCHIVE = True

ARCHIVE_DIR = Path(r"D:\GreenDayTV") # The directory conaining the archived clips. CHANGE THIS.
OUTPUT_DIR = ARCHIVE_DIR / "Daily" # The path to store the Daily videos. By default is ARCHIVE_DIR/Daily. Change this if you want

# Make sure you install all of theese and they are in your path. For "mkvmerge" you have to install "MKVToolNix".
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
MKVMERGE = "mkvmerge"

SEGMENT_RE = re.compile(
    r"^GreenDayTV_(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})\.mkv$"
)

NOTICE_DURATION = 2
SIGNIFICANT_GAP = 7

def _sanitise_windows_path(path: Path):
    return path.as_posix().replace(":", r"\:")


def run(command: list):
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        #check=True
    )


def probe_duration(path: Path):
    result = run([
        FFPROBE,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        str(path)
    ])

    data = json.loads(result.stdout)
    if "duration" in tuple(data["format"].keys()):
        return float(data["format"]["duration"]) # try to probe duration from 'stream' key
        
        
    result = run([
        FFPROBE,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,duration_time",
        "-of", "csv=p=0",
        str(path)
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


def probe_audio_sample_rate(path: Path):
    result = run([
        FFPROBE,
        "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ])

    value = result.stdout.strip()

    if not value:
        raise RuntimeError(f"Could not determine audio sample rate of {path}")

    return int(value)


def parse_filename(path: Path):
    match = SEGMENT_RE.match(path.name)

    if not match:
        return None

    date_part, time_part = match.groups()

    return datetime.strptime(
        f"{date_part} {time_part}",
        "%Y-%m-%d %H-%M-%S"
    )


def get_source_segments():
    segments = []

    for path in ARCHIVE_DIR.glob("GreenDayTV_*.mkv"):
        start = parse_filename(path)

        if start is None:
            continue

        try:
            duration = probe_duration(path)
        except subprocess.CalledProcessError:
            continue

        end = start + timedelta(seconds=duration)

        segments.append({
            "path": path,
            "start": start,
            "end": end,
        })

    segments.sort(key=lambda x: x["start"])

    return segments


def split_by_day(segment: dict):
    parts = []

    source_start = segment["start"]
    source_end = segment["end"]

    current = source_start

    while current.date() < source_end.date():
        midnight = datetime.combine(
            current.date() + timedelta(days=1),
            datetime.min.time()
        )

        parts.append({
            "path": segment["path"],
            "source_start": source_start,
            "source_end": source_end,
            "start": current,
            "end": midnight,
        })

        current = midnight

    if current < source_end:
        parts.append({
            "path": segment["path"],
            "source_start": source_start,
            "source_end": source_end,
            "start": current,
            "end": source_end,
        })

    return parts


def build_day_segments(source_segments: list[dict]):
    days = {}

    for segment in source_segments:
        for part in split_by_day(segment):
            day = part["start"].date()

            days.setdefault(day, []).append(part)

    for day in days:
        days[day].sort(key=lambda x: x["start"])

    return days


def make_ffmpeg_part(segment, output: Path):
    source = segment["path"]

    offset = (
        segment["start"] - segment["source_start"]
    ).total_seconds()

    duration = (
        segment["end"] - segment["start"]
    ).total_seconds()

    run([
        FFMPEG,
        "-y",
        "-ss", str(offset),
        "-i", str(source),
        "-t", str(duration),
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        str(output)
    ])


def make_notice(text, output: Path, audio_sample_rate):
    output.parent.mkdir(parents=True, exist_ok=True)
    
    font = _sanitise_windows_path(Path(r"C:\Windows\Fonts\arial.ttf"))
    textfile = output.with_suffix(".txt")
    textfile.write_text(text, encoding="utf-8")

    try:
        run([
            FFMPEG,
            "-y",
            "-f", "lavfi",
            "-i", "color=c=black:s=1920x1080:r=30",
            "-f", "lavfi",
            "-i", f"anullsrc=r={audio_sample_rate}:cl=stereo",
            "-t", str(NOTICE_DURATION),
            "-vf",
            (
                "drawtext="
                f"fontfile='{font}':"
                f"textfile='{_sanitise_windows_path(textfile)}':"
                "fontcolor=white:"
                "fontsize=48:"
                "text_align=M+C:"
                "x=(w-text_w)/2:"
                "y=(h-text_h)/2:"
                "line_spacing=12"
            ),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "20",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            str(output)
        ])
    finally:
        #textfile.unlink(missing_ok=True)
        pass


def concat_parts(parts, output: Path):
    command = [
        MKVMERGE,
        "-o", str(output),
    ]

    for index, part in enumerate(parts):
        if index == 0:
            command.append(str(part))
        else:
            command.extend([
                "+",
                str(part),
            ])

    result = run(command)
    if result.returncode not in (0, 1): raise RuntimeError(f"mkvmerge returned non-zero and non-one status {result.returncode}")


def format_time(value):
    return value.strftime("%H:%M:%S")


def format_delta(hours: int = None, minutes: int = None, seconds: float = None):
    if (hours, minutes, seconds) == (0,0,0.0): return "null"
    parts = []
    if hours  : parts.append(f"{hours} hour{'s' if hours > 1 else ''}"); parts.append(", ")
    if minutes: parts.append(f"{minutes} minute{'s' if minutes > 1 else ''}"); parts.append(", ")
    if seconds: parts.append(f"{seconds} second{'s' if seconds > 1 else ''}"); parts.append(", ")
    
    if len(parts) >= 4:  parts[-3] = " and " # if we have 4 or more elements (2+ words + 2+ commas) replace the comma before the last word with an "and"
    parts.pop() # remove the last separator.

    return("".join(parts))
     

def process_day(day, segments: list[dict]):
    output = OUTPUT_DIR / f"GreenDayTV_{day:%Y-%m-%d}.mkv"

    if output.exists():
        return

    day_start = datetime.combine(day, datetime.min.time())
    day_end = day_start + timedelta(days=1)

    if not segments:
        return

    # Clamp footage to the calendar day.
    for segment in segments:
        segment["start"] = max(segment["start"], day_start)
        segment["end"] = min(segment["end"], day_end)

    segments = [
        s for s in segments
        if s["end"] > s["start"]
    ]
    
    segments.sort(key=lambda x: x["start"])
    
    # Determine the audio sample rate for the clips of this day (This is probably unnecessary: it should always be 44.1KHz)
    audio_sample_rate = probe_audio_sample_rate(segments[0]["path"])

    # Determine whether the day is incomplete.
    complete = (
        len(segments) == 1
        and segments[0]["start"] <= day_start
        and segments[0]["end"] >= day_end
    )

    parts = []

    work_dir = OUTPUT_DIR / f".work_{day:%Y-%m-%d}"
    work_dir.mkdir(parents=True, exist_ok=True)
    
    previous_end = day_start
    is_first_clip = True

    try:
        if not complete:
            warning = work_dir / "000_warning.mkv"

            make_notice(
                "The archived footage for this day is incomplete",
                warning,
                audio_sample_rate
            )

            parts.append(warning)

        for index, segment in enumerate(segments, start=1):
            print(segment['path']) # Debug print. Remove if you want
            delta = (segment['start'] - previous_end)
            delta_seconds = round(delta.total_seconds(), 2)  
                       
            if delta_seconds >= SIGNIFICANT_GAP:
          
                notice = work_dir / f"{index:04d}_notice.mkv"          
                make_notice(
                    f"""The {"first" if is_first_clip else "next"} captured footage starts at {format_time(segment['start'])} and ends at {format_time(segment['end'])}.
(There's a {format_delta(delta.seconds//3600, (delta.seconds//60)%60, int(delta_seconds%60))} gap since the {"start of the day" if is_first_clip else "previous clip"})""",
                    notice,
                    audio_sample_rate
                )

                parts.append(notice)
                
            # Use the original file directly unless this segment was
            # actually altered by a calendar-day split.
            start_offset = abs(
                (segment["start"] - segment["source_start"]).total_seconds()
            )

            end_offset = abs(
                (segment["end"] - segment["source_end"]).total_seconds()
            )

            needs_editing = (
                start_offset > 0.01
                or end_offset > 0.01
            )

            if needs_editing:
                footage = work_dir / f"{index:04d}_footage.mkv"

                print("making edited part") # Debug print. Remove if you want

                make_ffmpeg_part(
                    segment,
                    footage
                )

                parts.append(footage)

            else:
                print("using original footage") # Debug print. Remove if you want

                parts.append(segment["path"])
            
            previous_end = segment['end']
            is_first_clip = False

        concat_parts(parts, output)
        if UPLOAD_TO_INTERNET_ARCHIVE == True:
            upload_to_internet_archive(output)

    finally:
        for path in work_dir.glob("*"):
            path.unlink(missing_ok=True)

        work_dir.rmdir()

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    today = datetime.now().date()
    source_segments = get_source_segments()
    days = build_day_segments(source_segments)
 
    print("segments for the day:") # Debug print. Remove if you want
    for day, segments in sorted(days.items()):
        print(day, len(segments)) # Debug print. Remove if you want
        for segment in segments:
            print(segment['path'], (segment['end'] - segment['start']).seconds)
        if day >= today:
            continue

        print("\n\n")
        process_day(day, segments)


if __name__ == "__main__":
    main()
