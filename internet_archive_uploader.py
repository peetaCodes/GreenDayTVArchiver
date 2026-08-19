import os
import logging
import internetarchive
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv("environment_variables.env")

IA_ITEM = os.getenv("IA_ITEM")
IA_COLLECTION = os.getenv("IA_COLLECTION")
IA_ACCESS_KEY = os.getenv("IA_ACCESS_KEY")
IA_SECRET_KEY = os.getenv("IA_SECRET_KEY")

def upload_to_internet_archive(file_path):
    global IA_ITEM
    try:
        filename = file_path.stem
        recording_time = datetime.fromtimestamp(
            file_path.stat().st_mtime
        )

        logging.info(
            "Uploading %s to Internet Archive...",
            file_path.name,
        )

        metadata = {
            "title": filename,
            "description": (
                "Daily recording of Green Day TV."
            ),
            "creator": "", #idk what to put in here
            "subject": "Green Day TV",
            "mediatype": "movies",
            "date": recording_time.strftime("%Y-%m-%d"),
        }

        if IA_COLLECTION != "":
            metadata["collection"] = IA_COLLECTION

        internetarchive.upload(
            IA_ITEM,
            files=[str(file_path)],
            metadata=metadata,
            access_key=IA_ACCESS_KEY,
            secret_key=IA_SECRET_KEY,
            retries=1000,
        )

        logging.info(
            "Successfully uploaded %s",
            file_path.name,
        )

        return True

    except Exception as e:
        logging.exception(
            "Internet Archive upload failed for %s: %s",
            file_path.name,
            e,
        )

        return False
