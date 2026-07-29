#!/usr/bin/env python3
"""plaud-sync: Download transcripts and summaries from Plaud.ai"""

import argparse
import gzip
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

API_BASE_DEFAULT = "https://api.plaud.ai"
PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = Path.home() / ".plaud-sync"
CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE = CONFIG_DIR / "state.json"

# `folder_mapping` keys must match your actual Plaud folder names exactly;
# each maps to the directory where that folder's recordings should be filed.
# Map a folder to null to skip it. Edit these to your own projects.
DEFAULT_CONFIG = {
    "folder_mapping": {
        "My Project": "~/git/my-project/docs/transcripts",
        "Archive": None,
    },
    "default_output": str(PROJECT_DIR / "docs" / "transcripts"),
    "timezone": "Europe/Prague",
    "device_id": "plaud-sync-cli",
}

HEADERS_TEMPLATE = {
    "app-platform": "web",
    "app-language": "en",
    "edit-from": "web",
    "timezone": "Europe/Prague",
}

SUMM_TYPE_TO_FILENAME = {
    "MEETING-ARRANGEMENT": "task-assignment",
    "MEETING-REPORT": "meeting-report",
    "DIM-MEETING-DETAILS": "meeting-minutes",
}


def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    return DEFAULT_CONFIG


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_sync": None, "downloaded": {}}


def save_state(state):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    state["last_sync"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def get_token():
    token = os.environ.get("PLAUD_BEARER_TOKEN")
    if token:
        return token
    config = load_config()
    token = config.get("token")
    if token:
        return token
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            data = json.load(f)
            token = data.get("token")
            if token:
                return token
    print("Error: No bearer token found.", file=sys.stderr)
    print("Set PLAUD_BEARER_TOKEN env var or add 'token' to ~/.plaud-sync/config.json", file=sys.stderr)
    sys.exit(1)


def extract_sub_from_jwt(token):
    """Extract the 'sub' field from a JWT token (used as device/user ID)."""
    import base64
    parts = token.split(".")
    if len(parts) != 3:
        return None
    # Add padding for base64 decoding
    payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("sub")
    except Exception:
        return None


def api_headers(token, config):
    headers = {**HEADERS_TEMPLATE}
    headers["Authorization"] = f"Bearer {token}"
    headers["timezone"] = config.get("timezone", "Europe/Prague")
    # Use JWT sub claim as device ID (matches what the web app sends)
    device_id = extract_sub_from_jwt(token) or config.get("device_id", "plaud-sync-cli")
    headers["x-device-id"] = device_id
    headers["x-pld-tag"] = device_id
    headers["Origin"] = "https://web.plaud.ai"
    headers["Referer"] = "https://web.plaud.ai/"
    headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    return headers


def resolve_api_base(token, config):
    """Resolve the correct regional API base URL."""
    cached = config.get("_api_base")
    if cached:
        return cached
    url = f"{API_BASE_DEFAULT}/file/simple/web?skip=0&limit=1&is_trash=0"
    req = urllib.request.Request(url, headers=api_headers(token, config))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("Error: Authentication failed (401). Token may be expired.", file=sys.stderr)
            print("Get a new token from web.plaud.ai DevTools.", file=sys.stderr)
            sys.exit(1)
        raise
    if data.get("status") == -302:
        api_base = data.get("data", {}).get("domains", {}).get("api", API_BASE_DEFAULT)
        config["_api_base"] = api_base
        return api_base
    config["_api_base"] = API_BASE_DEFAULT
    return API_BASE_DEFAULT


def api_get(path, token, config):
    api_base = resolve_api_base(token, config)
    url = f"{api_base}{path}"
    req = urllib.request.Request(url, headers=api_headers(token, config))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("Error: Authentication failed (401). Token may be expired.", file=sys.stderr)
            print("Get a new token from web.plaud.ai DevTools.", file=sys.stderr)
            sys.exit(1)
        raise


def download_gzipped_json(url):
    """Download a gzipped JSON file from S3 and return parsed content."""
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    try:
        decompressed = gzip.decompress(raw)
        return json.loads(decompressed)
    except gzip.BadGzipFile:
        return json.loads(raw)


def fetch_folders(token, config):
    """Fetch folder list and return {id: name} mapping."""
    data = api_get("/filetag/", token, config)
    return {f["id"]: f["name"] for f in data.get("data_filetag_list", [])}


def list_recordings(token, config, days):
    """List recordings from the last N days."""
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    # Fetch all recordings, sorted by start_time descending
    data = api_get(
        f"/file/simple/web?skip=0&limit=99999&is_trash=0&sort_by=start_time&is_desc=true",
        token, config,
    )
    recordings = data.get("data_file_list", [])
    # Filter by date
    return [r for r in recordings if r.get("start_time", 0) >= cutoff_ms]


def slugify(text):
    """Convert text to a filesystem-safe slug."""
    # Transliterate common Czech characters
    tr = str.maketrans(
        "áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ",
        "acdeeinorstuuyzACDEEINORSTUUYZ",
    )
    text = text.translate(tr)
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_]+", "-", text.strip())
    text = re.sub(r"-+", "-", text)
    return text[:80].rstrip("-")


def format_time_ms(ms):
    """Format milliseconds as HH:MM:SS or MM:SS."""
    total_secs = ms // 1000
    hours = total_secs // 3600
    minutes = (total_secs % 3600) // 60
    seconds = total_secs % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def format_duration(ms):
    """Format duration in human-readable form."""
    total_secs = ms // 1000
    hours = total_secs // 3600
    minutes = (total_secs % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_transcript_md(transcript_data, title, recording_date):
    """Format transcript JSON into Markdown."""
    lines = [f"# {title}", "", f"**Date**: {recording_date}", ""]
    current_speaker = None
    for entry in transcript_data:
        speaker = entry.get("speaker", "Unknown")
        content = entry.get("content", "")
        start = format_time_ms(entry.get("start_time", 0))
        if speaker != current_speaker:
            current_speaker = speaker
            lines.append(f"**{speaker}** [{start}]:")
        else:
            lines.append(f"[{start}]:")
        lines.append(f"{content}")
        lines.append("")
    return "\n".join(lines)


def format_outline_md(outline_data, title):
    """Format outline JSON into Markdown."""
    lines = [f"# Outline: {title}", ""]
    for entry in outline_data:
        start = format_time_ms(entry.get("start_time", 0))
        end = format_time_ms(entry.get("end_time", 0))
        topic = entry.get("topic", "")
        lines.append(f"- **[{start} - {end}]** {topic}")
    lines.append("")
    return "\n".join(lines)


def format_summary_md(summary_data):
    """Extract markdown content from summary JSON."""
    if isinstance(summary_data, dict):
        return summary_data.get("ai_content", "")
    return str(summary_data)


def resolve_output_dir(recording, folder_map, folders, config):
    """Determine output directory for a recording based on folder mapping."""
    folder_name = None
    for tag_id in recording.get("filetag_id_list", []):
        if tag_id in folders:
            folder_name = folders[tag_id]
            break

    if folder_name and folder_name in folder_map:
        target = folder_map[folder_name]
        if target is None:
            return None  # Explicitly skipped (e.g., "Archiv": null)
        base_dir = Path(target).expanduser()
    else:
        default = config.get("default_output")
        if not default:
            return None
        base_dir = Path(default).expanduser()

    # Build subdirectory name: YYYY-MM-DD-slug
    start_ms = recording.get("start_time", 0)
    dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    date_prefix = dt.strftime("%Y-%m-%d")
    title = recording.get("filename", "untitled")
    # Strip leading date pattern like "03-24 " from filename
    title_clean = re.sub(r"^\d{2}-\d{2}\s+", "", title)
    slug = slugify(title_clean)
    dir_name = f"{date_prefix}-{slug}"

    return base_dir / dir_name


def sync_recording(recording, token, config, folders, folder_map, state, dry_run=False):
    """Download and save all content for a single recording."""
    file_id = recording["id"]
    filename = recording.get("filename", "untitled")
    version_ms = recording.get("version_ms", 0)

    # Check if already downloaded with same version
    if file_id in state.get("downloaded", {}):
        existing = state["downloaded"][file_id]
        if existing.get("version_ms") == version_ms:
            return None  # Already up to date

    output_dir = resolve_output_dir(recording, folder_map, folders, config)
    if output_dir is None:
        return None  # Skipped (folder mapped to null)

    if dry_run:
        return {"file_id": file_id, "filename": filename, "target_dir": str(output_dir), "dry_run": True}

    # Get file detail with pre-signed URLs
    detail = api_get(f"/file/detail/{file_id}", token, config)
    detail_data = detail.get("data", {})
    content_list = detail_data.get("content_list", [])

    output_dir.mkdir(parents=True, exist_ok=True)

    start_ms = recording.get("start_time", 0)
    dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    recording_date = dt.strftime("%Y-%m-%d")
    title = recording.get("filename", "untitled")

    downloaded_files = []

    for content in content_list:
        data_type = content.get("data_type", "")
        data_link = content.get("data_link", "")
        if not data_link:
            continue

        try:
            parsed = download_gzipped_json(data_link)
        except Exception as e:
            print(f"  Warning: Failed to download {data_type}: {e}", file=sys.stderr)
            continue

        # Defensive backoff between downloads
        time.sleep(0.5)

        if data_type == "transaction":
            md = format_transcript_md(parsed, title, recording_date)
            path = output_dir / "transcript.md"
            path.write_text(md, encoding="utf-8")
            downloaded_files.append("transcript.md")

        elif data_type == "outline":
            md = format_outline_md(parsed, title)
            path = output_dir / "outline.md"
            path.write_text(md, encoding="utf-8")
            downloaded_files.append("outline.md")

        elif data_type in ("auto_sum_note", "sum_multi_note"):
            extra = content.get("extra", {})
            summ_type = extra.get("summ_type", "")
            fname = SUMM_TYPE_TO_FILENAME.get(summ_type)
            if not fname:
                tab_name = content.get("data_tab_name", summ_type)
                fname = slugify(tab_name) or "summary"
            md = format_summary_md(parsed)
            path = output_dir / f"{fname}.md"
            path.write_text(md, encoding="utf-8")
            downloaded_files.append(f"{fname}.md")

    # Save metadata
    folder_name = None
    for tag_id in recording.get("filetag_id_list", []):
        if tag_id in folders:
            folder_name = folders[tag_id]
            break

    metadata = {
        "file_id": file_id,
        "filename": filename,
        "duration": recording.get("duration", 0),
        "duration_human": format_duration(recording.get("duration", 0)),
        "start_time": start_ms,
        "start_date": recording_date,
        "scene": recording.get("scene"),
        "serial_number": recording.get("serial_number"),
        "folder": folder_name,
        "version_ms": version_ms,
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "files": downloaded_files,
    }
    meta_path = output_dir / "metadata.json"
    meta_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    # Update state
    state.setdefault("downloaded", {})[file_id] = {
        "version_ms": version_ms,
        "downloaded_at": metadata["downloaded_at"],
        "target_dir": str(output_dir),
        "folder": folder_name,
    }

    return {
        "file_id": file_id,
        "filename": filename,
        "target_dir": str(output_dir),
        "files": downloaded_files,
    }


def cmd_list(args, token, config):
    """List recent recordings."""
    recordings = list_recordings(token, config, args.days)
    folders = fetch_folders(token, config)
    state = load_state()

    # Filter by folder if specified
    if args.folder:
        folder_id = None
        for fid, fname in folders.items():
            if fname.lower() == args.folder.lower():
                folder_id = fid
                break
        if folder_id:
            recordings = [r for r in recordings if folder_id in r.get("filetag_id_list", [])]

    if not recordings:
        print(f"No recordings found in the last {args.days} days.")
        return

    print(f"Recordings from the last {args.days} days ({len(recordings)} total):\n")

    for r in recordings:
        file_id = r["id"]
        title = r.get("filename", "untitled")
        start_ms = r.get("start_time", 0)
        dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
        date_str = dt.strftime("%Y-%m-%d %H:%M")
        duration = format_duration(r.get("duration", 0))
        is_trans = r.get("is_trans", False)
        is_summary = r.get("is_summary", False)

        # Folder name
        folder_name = None
        for tag_id in r.get("filetag_id_list", []):
            if tag_id in folders:
                folder_name = folders[tag_id]
                break

        # Sync status
        synced = file_id in state.get("downloaded", {})
        version_match = False
        if synced:
            version_match = state["downloaded"][file_id].get("version_ms") == r.get("version_ms")

        status_parts = []
        if not is_trans:
            status_parts.append("not transcribed")
        if not is_summary:
            status_parts.append("no summary")
        if synced and version_match:
            status_parts.append("synced")
        elif synced:
            status_parts.append("updated")

        status = f" [{', '.join(status_parts)}]" if status_parts else ""
        folder_str = f" ({folder_name})" if folder_name else ""

        print(f"  {date_str}  {duration:>6}  {title}{folder_str}{status}")


def cmd_sync(args, token, config):
    """Sync recent recordings."""
    recordings = list_recordings(token, config, args.days)
    folders = fetch_folders(token, config)
    folder_map = config.get("folder_mapping", {})
    state = load_state()

    # Filter by folder if specified
    if args.folder:
        folder_id = None
        for fid, fname in folders.items():
            if fname.lower() == args.folder.lower():
                folder_id = fid
                break
        if not folder_id:
            print(f"Error: Folder '{args.folder}' not found.", file=sys.stderr)
            print(f"Available folders: {', '.join(folders.values())}", file=sys.stderr)
            sys.exit(1)
        recordings = [r for r in recordings if folder_id in r.get("filetag_id_list", [])]

    # Filter: only transcribed recordings
    ready = [r for r in recordings if r.get("is_trans", False)]
    skipped_not_ready = len(recordings) - len(ready)

    if not ready:
        print(f"No recordings ready to sync in the last {args.days} days.")
        if skipped_not_ready:
            print(f"  ({skipped_not_ready} recording(s) not yet transcribed)")
        return

    synced = []
    skipped_existing = 0
    skipped_no_target = 0

    for r in ready:
        title = r.get("filename", "untitled")
        result = sync_recording(r, token, config, folders, folder_map, state, dry_run=args.dry_run)
        if result is None:
            file_id = r["id"]
            if file_id in state.get("downloaded", {}):
                skipped_existing += 1
            else:
                skipped_no_target += 1
            continue
        synced.append(result)
        if not args.dry_run:
            print(f"  Downloaded: {title}")
            for f in result.get("files", []):
                print(f"    -> {result['target_dir']}/{f}")

    if not args.dry_run:
        save_state(state)

    # Summary
    print()
    if args.dry_run:
        print(f"Dry run: {len(synced)} recording(s) would be downloaded")
        for s in synced:
            print(f"  {s['filename']} -> {s['target_dir']}")
    else:
        print(f"Synced: {len(synced)} recording(s)")
    if skipped_existing:
        print(f"Skipped: {skipped_existing} already synced")
    if skipped_no_target:
        print(f"Skipped: {skipped_no_target} no target directory (unmapped folder)")
    if skipped_not_ready:
        print(f"Skipped: {skipped_not_ready} not yet transcribed")


def cmd_init(args, token, config):
    """Initialize config directory with default config."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists() and not args.force:
        print(f"Config already exists at {CONFIG_FILE}")
        print("Use --force to overwrite.")
        return
    with open(CONFIG_FILE, "w") as f:
        json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
    print(f"Config written to {CONFIG_FILE}")
    print("Edit folder_mapping to match your repository paths.")


def main():
    parser = argparse.ArgumentParser(
        prog="plaud-sync",
        description="Download transcripts and summaries from Plaud.ai",
    )
    parser.add_argument("--days", type=int, default=7, help="Number of days to look back (default: 7)")
    parser.add_argument("--folder", type=str, help="Only sync recordings from this Plaud folder")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be downloaded without downloading")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List recent recordings")
    sub.add_parser("sync", help="Download recent recordings (default)")

    init_parser = sub.add_parser("init", help="Initialize config")
    init_parser.add_argument("--force", action="store_true", help="Overwrite existing config")

    # Also support --list as a shorthand
    parser.add_argument("--list", action="store_true", help="List recent recordings (shorthand for 'list' subcommand)")

    args = parser.parse_args()

    token = get_token()
    config = load_config()

    if args.list or args.command == "list":
        cmd_list(args, token, config)
    elif args.command == "init":
        cmd_init(args, token, config)
    else:
        cmd_sync(args, token, config)


if __name__ == "__main__":
    main()
