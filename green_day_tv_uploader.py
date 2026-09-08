import re
import time
from pathlib import Path

from internetarchive import get_item

# ============================================================
# Green Day TV - Internet Archive uploader
# ============================================================


# -------------------------------------
# Configuration
# -------------------------------------

# Directory containing the daily MKV files. Change this if necessary
DAILY_DIR = Path(r"D:\Pietro\Archives\GreenDayTV\Daily")

# Directory containing the source 1hr MKV files. Change this if necessary.
# This is needed for automatic source-files deletion can be set to None if you want to delete the source files manually.
# Note that the uploaded daily files will de deleted regardless.
SOURCE_DIR = Path(r"D:\Pietro\Archives\GreenDayTV")

# Prefix used for Internet Archive item identifiers.
#
# Example:
#     green-day-tv-2026-08-08
#
IA_IDENTIFIER_PREFIX = "green-day-tv"

# File containing your Internet Archive S3 credentials.
#
# Expected contents:
#
#     access=xxxxxxxxxxxxx
#     secret=xxxxxxxxxxxxx
#
# Note: you can also pass this using "--acces-key [ACCESS_KEY]" and "--secret-key [SECRET_KEY]"
IA_CREDENTIALS_FILE = Path(
    r"D:\Pietro\Archives\GreenDayTV\ia_api_keys.txt"
)

# Number of times to retry a failed upload.
UPLOAD_RETRIES = 3

# Seconds to wait after an upload to let the IA server update.
# Default is 20 minutes (20 minutes * 60 seconds)
# Please do not set this any lower than 15 minutes
SERVE_UPDATE_DELAY = 20 * 60

# Seconds to wait between upload retries.
UPLOAD_RETRY_DELAY = 30


# Daily filename matching
FILE_RE = re.compile(
    r"^GreenDayTV_(\d{4}-\d{2}-\d{2})\.mkv$"
)


# ------------------------------------------------------------
# Read Internet Archive credentials
# ------------------------------------------------------------

def load_ia_credentials():
    """
    Read Internet Archive S3 credentials from a text file.

    Expected format:

        access=xxxxxxxxxxxxx
        secret=xxxxxxxxxxxxx

    Whitespace around keys/values is ignored.
    Blank lines and lines beginning with '#' are ignored.
    """

    if not IA_CREDENTIALS_FILE.exists():
        raise RuntimeError(f"Internet Archive credentials file does not exist:\n{IA_CREDENTIALS_FILE}")

    access_key = None
    secret_key = None

    with IA_CREDENTIALS_FILE.open("r", encoding="utf-8") as file:
        for raw_line in file:
            line = raw_line.strip()

            if not line:continue
            if line.startswith("#"):continue

            key, value = line.split("=", 1)
            key = key.strip().lower()
            value = value.strip()

            if key == "access" and access_key:
                raise RuntimeError("The Internet Archive credentials file contains two or more 'access=' value. Exactly one is needed")
            elif key == "access" and not access_key: access_key = value
            
            if key == "secret" and secret_key:
                raise RuntimeError("The Internet Archive credentials file contains two or more 'secret=' value. Exactly one is needed")
            elif key == "secret" and not secret_key: secret_key = value

    if not access_key: raise RuntimeError("The Internet Archive credentials file does not contain an 'access=' value.")
    if not secret_key: raise RuntimeError("The Internet Archive credentials file does not contain an 'secret=' value.")

    return access_key, secret_key


# ------------------------------------------------------------
# Find daily files
# ------------------------------------------------------------

def get_daily_files():
    files = []

    for path in DAILY_DIR.glob("GreenDayTV_*.mkv"):
        match = FILE_RE.match(path.name)
        
        if match:
            files.append((match.group(1), path))

    # Sort by date and then filename.
    files.sort(
        key=lambda item: (
            item[0],
            item[1].name,
        )
    )

    return files


# ------------------------------------------------------------
# Internet Archive identifier
# ------------------------------------------------------------

def get_identifier(date_string: str, must_be_new: bool = False):
    base_identifier = f"{IA_IDENTIFIER_PREFIX}-{date_string}"
    if must_be_new:
        identifier = base_identifier
        available = get_item(identifier).identifier_available()
        
        count = 0
        while not available:
            count += 1
            identifier = f"{base_identifier}_{chr(count + 96)}" # if count == 1: add '_a' (Unicode 97), if count == 2: add '_b' (Unicode 98)...
            available = get_item(identifier).identifier_available()
            
        return identifier
    return base_identifier


# ------------------------------------------------------------
# Upload one file
# ------------------------------------------------------------

def upload_day(
    date_string: str,
    path: Path,
    access_key: str,
    secret_key: str,
):

    identifier = get_identifier(date_string)

    print()
    print("=" * 60)
    print(f"Uploading:   {path.name}")
    print(f"Identifier:  {identifier}")
    print("=" * 60)

    # --------------------------------------------------------
    # Check local file
    # --------------------------------------------------------

    if not path.exists():
        print("Local file no longer exists.")
        return False

    if not path.is_file():
        print("Path is not a regular file.")
        return False

    file_size = path.stat().st_size
    print(f"Local size:  {file_size / (1024 ** 3):.2f} GiB")

    # --------------------------------------------------------
    # Get/create IA item
    # --------------------------------------------------------

    item = get_item(identifier)

    # --------------------------------------------------------
    # Check whether the file already exists
    # --------------------------------------------------------

    for remote_file in item.files:

        if remote_file.get("name") == path.name:
            print("File already exists on Internet Archive.")
            print("Skipping.")
            return True

    # --------------------------------------------------------
    # Metadata
    # --------------------------------------------------------

    metadata = {
        "title": (
            f"Green Day TV — {date_string}"
        ),

        "mediatype": "movies",
        "creator": "Green Day",
        "date": date_string,

        "subject": [
            "Green Day TV",
            "Green Day",
            "live webcast",
            "television archive",
        ],

        "description": (
            f"Continuous archive of the Green Day TV "
            f"broadcast for {date_string}."
        ),
    }

    # --------------------------------------------------------
    # Upload with retry loop
    # --------------------------------------------------------

    for attempt in range(1, UPLOAD_RETRIES + 1):

        print()
        print(f"Starting upload (attempt {attempt}/{UPLOAD_RETRIES})...")

        try:
            identifier = get_identifier(date_string, True) # Now we are trying to upload. The identifier MUST be new.
            print(identifier)
            item = get_item(identifier) # Refresh `item` to the new identifier.
            
            responses = item.upload(

                files=[str(path)],

                metadata=metadata,
                access_key=access_key,
                secret_key=secret_key,

                # Ask the library to verify uploaded data.
                verify=True,

                verbose=True,
            )
            del item # this instance of the item won't be needed anymore.

            if not responses:
                print("Upload returned no response.")

            else:
                failed = False

                for response in responses:
                    print(f"Upload response: HTTP {response.status_code}")

                    if response.status_code != 200:
                        from sys import stderr
                        failed = True
                        
                        print(
                            f"Upload failed with non-200 HTTP status {response.status_code}.",
                            file=stderr
                        )


                if not failed:
                    print("Upload request completed.")
     
                    # Give the internet archive server a bit of time to update
                    time.sleep(SERVE_UPDATE_DELAY)
                    
                    # Now refresh the item and verify the file exists.
                    item = get_item(identifier)


                    for remote_file in item.files:
                        print(remote_file.get("name"))

                        if remote_file.get("name") == path.name:
                            remote_size = remote_file.get("size")
                            print("Upload verified successfully.")

                            if remote_size is not None:
                                    remote_size_int = int(remote_size)
                                    print(f"Remote size: {remote_size_int / (1024 ** 3):.2f} GiB")

                                    if remote_size_int != file_size:
                                        print("WARNING: local and remote file sizes differ.")
                                        return False

                            return True
                    
        except Exception as exc:
            print(
                f"\nUpload raised an exception: "
                f"{type(exc).__name__}: {exc}"
            )

        # ----------------------------------------------------
        # Retry
        # ----------------------------------------------------

        if attempt < UPLOAD_RETRIES:
            print(f"Waiting {UPLOAD_RETRY_DELAY} seconds before retrying...")
            time.sleep(UPLOAD_RETRY_DELAY)

    print("Upload failed after all retry attempts.")
    return False


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main(access_key=None, secret_key=None):

    if not DAILY_DIR.exists():
        raise RuntimeError(
            f"Daily directory does not exist: "
            f"{DAILY_DIR}"
        )

    # --------------------------------------------------------
    # Load credentials
    # --------------------------------------------------------

    if not (access_key or secret_key):
        access_key, secret_key = (load_ia_credentials())

    print("Internet Archive credentials loaded.")

    # --------------------------------------------------------
    # Find files
    # --------------------------------------------------------

    daily_files = get_daily_files()

    if not daily_files:
        print("No daily files found.")
        return

    print(f"Found {len(daily_files)} daily file(s).")

    # --------------------------------------------------------
    # Upload sequentially
    # --------------------------------------------------------

    failed_uploads = []

    for date_string, path in daily_files:
        success = upload_day(
            date_string,
            path,
            access_key,
            secret_key,
        )

        if not success:
            failed_uploads.append(path.name)
        else:
            path.unlink() # delete the file if the upload succeeded.
            
            # if SOURCE_DIR is of type pathlib.Path then delete all of the source files for the now uploaded day in that path.
            if type(SOURCE_DIR).__name__ == Path.__name__:
                for file in SOURCE_DIR.glob(f"*.mkv"):
                    if file.name == f"GreenDayTV_{date_string}.mkv": path.unlink()

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print()
    print("=" * 60)
    print("Upload summary")
    print("=" * 60)

    if failed_uploads:

        print("The following files failed:")

        for filename in failed_uploads:
            print(f"  {filename}")

        print()
        print(f"{len(failed_uploads)} upload(s) failed.")

    else:
        print("All files uploaded successfully.")


if __name__ == "__main__":
    from argparse import ArgumentParser
    
    parser = ArgumentParser(
                prog='Green Day TV Archiver - Uploader',
                description=(
                    'This is the third component of the GDTV Archiver. '
                    'It uploads stitched `daily files` to the Internet Archive.'
                    )
            )
            
    parser.add_argument(
        '-a', '--access-key',
        type=str,
        required=False,
        default="",
        help="You Internet Archive S3-API Access Key"
    ) 
    parser.add_argument(
        '-s', '--secret-key',
        type=str,
        required=False,
        default="",
        help="You Internet Archive S3-API Secret Key"
    ) 
    
    args = parser.parse_args()
    
    main(args.access_key, args.secret_key)
