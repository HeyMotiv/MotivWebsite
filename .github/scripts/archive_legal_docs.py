"""Archive changed legal documents as Google Docs.

For each document in .github/legal-archive.json:
  - compare the legal text (the element matched by "selector") before and after this push
  - skip it if the legal text is unchanged, even if other parts of the file changed
  - otherwise name the doc  YYYY-MM-DD-<title>  (date in America/New_York) and
    replace that doc's content if it already exists, or create it
    (earlier same-day text stays in the doc's version history)
"""
import datetime
import json
import os
import re
import subprocess
import sys
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

CONFIG_PATH = os.environ.get("LEGAL_ARCHIVE_CONFIG", ".github/legal-archive.json")
TIMEZONE = ZoneInfo(os.environ.get("ARCHIVE_TIMEZONE", "America/New_York"))
GDOC = "application/vnd.google-apps.document"


def file_at(sha, path):
    """Return the file's content at a commit, or None if it didn't exist there."""
    result = subprocess.run(["git", "show", f"{sha}:{path}"], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def extract(raw, selector):
    """Return (legal_section_html, full_doc_html), or (None, None) if the selector misses."""
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup.find_all(["script", "noscript", "iframe"]):
        tag.decompose()
    if not selector:
        return str(soup), str(soup)
    node = soup.select_one(selector)
    if node is None:
        return None, None
    styles = "".join(str(s) for s in soup.find_all("style"))
    full = (
        "<html><head><meta charset='utf-8'>" + styles + "</head>"
        "<body>" + str(node) + "</body></html>"
    )
    return str(node), full


def normalize(html):
    return re.sub(r"\s+", " ", html).strip()


def drive_client():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_doc(drive, name, folder_id):
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"name = '{safe}' and '{folder_id}' in parents "
        f"and mimeType = '{GDOC}' and trashed = false"
    )
    files = drive.files().list(
        q=query, fields="files(id, name, webViewLink)",
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute().get("files", [])
    if len(files) > 1:
        print(f"WARNING: {len(files)} docs named '{name}'; updating the first one.")
    return files[0] if files else None


def main():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        documents = json.load(f)["documents"]

    before = os.environ.get("BEFORE_SHA", "")
    after = os.environ.get("AFTER_SHA", "HEAD")
    force = (
        os.environ.get("FORCE_ALL") == "true"
        or not before or set(before) == {"0"}
    )

    to_archive = []
    for doc in documents:
        selector = doc.get("selector")
        with open(doc["path"], encoding="utf-8") as f:
            section, full = extract(f.read(), selector)
        if section is None:
            # Fail loudly rather than archive the wrong content.
            sys.exit(f"ERROR: selector '{selector}' not found in {doc['path']}")

        if not force:
            old_raw = file_at(before, doc["path"])
            old_section = extract(old_raw, selector)[0] if old_raw is not None else None
            if old_section is not None and normalize(old_section) == normalize(section):
                print(f"Unchanged {doc['title']}")
                continue
        to_archive.append((doc, full))

    if not to_archive:
        print("No legal document text changed. Nothing to archive.")
        return

    drive = drive_client()
    today = datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d")

    for doc, html in to_archive:
        name = f"{today}-{doc['title']}"
        media = MediaInMemoryUpload(html.encode("utf-8"), mimetype="text/html", resumable=False)
        existing = find_doc(drive, name, doc["folder_id"])
        if existing:
            result = drive.files().update(
                fileId=existing["id"], media_body=media,
                fields="id, webViewLink", supportsAllDrives=True,
            ).execute()
            print(f"Replaced  {name}  ->  {result['webViewLink']}")
        else:
            result = drive.files().create(
                body={"name": name, "parents": [doc["folder_id"]], "mimeType": GDOC},
                media_body=media, fields="id, webViewLink", supportsAllDrives=True,
            ).execute()
            print(f"Created   {name}  ->  {result['webViewLink']}")


if __name__ == "__main__":
    main()
