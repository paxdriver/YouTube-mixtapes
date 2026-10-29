#!/usr/bin/env python3

"""
If PLAYLIST_URLS contains items, it quickly extracts the individual video URLs, deduplicates them against your static list, and processes them seamlessly in order.

Sequential YouTube -> MP3 (320k) with simple Rich progress bars.

USAGE:
-----
1) Install dependencies:
   pip install yt-dlp rich

2) Ensure ffmpeg is installed:
   ffmpeg -version

3) Ensure Node is available in this shell (recommended for yt-dlp JS runtime support):
   nvm install --lts
   nvm use --lts

4) Edit CONFIG:
   - OUTPUT_FOLDER to save mp3's
   - URLS to be extracted
   - YOUTUBE_COOKIES_JSON (see step 5)
   - GENERATED_COOKIES_TXT (gets parsed from json and renamed `youtube_cookies.txt`)

5) In Chrome on youtube.com DevTools console run:
   a) `copy(JSON.stringify(await cookieStore.getAll(), null, 2))`
   b) Paste into `youtube_cookies.json`

6) Run:
   python yt-mp3.py


optional in-line for a single video: yt-dlp --cookies youtube_cookies.txt -f "bestaudio/best" -o "test.%(ext)s" "https://youtube.com"
"""

from __future__ import annotations
from urllib.parse import parse_qs, urlparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Optional, cast

import yt_dlp
from rich.console import Console
from rich.progress import (
	BarColumn,
	Progress,
	TaskID,
	TaskProgressColumn,
	TextColumn,
	TimeElapsedColumn,
	TimeRemainingColumn,
)

# =========================
# CONFIG
# =========================

FFMPEG_BIN: str = "/usr/bin/ffmpeg"
FFMPEG_THREADS: int = 1
MAX_RETRIES: int = 2

OUTPUT_FOLDER: str = "2026_NEW" # last one CD243

URLS: list[str] = [
	# ... list youtube urls here 

]

# Add your playlist URLs here. 
# If left empty, the script will naturally just process individual video URLs.
PLAYLIST_URLS: list[str] = [
	"https://youtube.com/playlist?list=PLGnoGOm5niHsNAnUYXvX0zlfYEr6hp1bf&si=A9_Hs5vGtCAgpuRr",
	"https://youtube.com/playlist?list=PLGnoGOm5niHtVGGhHLCIXexGXaFbirrmN&si=pqQxTEd6Q0oOXjlo",

]

FFMPEG_THREADS: int = 1
MAX_RETRIES: int = 2

YOUTUBE_COOKIES_JSON: str = "youtube_cookies.json"
GENERATED_COOKIES_TXT: str = "youtube_cookies.txt"


def sanitize_for_filename(title: Optional[str], fallback: str = "untitled") -> str:
	if not title:
		title = fallback
	s = unicodedata.normalize("NFKC", title)
	s = "".join(ch for ch in s if ch.isprintable())
	s = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "", s)
	s = re.sub(r"\s+", " ", s).strip().rstrip(". ")

	reserved = {
		"CON", "PRN", "AUX", "NUL",
		*(f"COM{i}" for i in range(1, 10)),
		*(f"LPT{i}" for i in range(1, 10)),
	}
	if not s:
		s = fallback
	if s.upper() in reserved:
		s = f"_{s}_"
	if len(s) > 180:
		s = s[:180].rstrip()
	return s or fallback


def unique_output_path(base_path: Path) -> Path:
	if not base_path.exists():
		return base_path
	n = 2
	while True:
		candidate = base_path.with_name(f"{base_path.stem} ({n}){base_path.suffix}")
		if not candidate.exists():
			return candidate
		n += 1


def _to_unix_expiry(exp: Any) -> int:
	if isinstance(exp, (int, float)):
		v = int(exp)
		return v if v > 0 else 0
	return 0


def convert_devtools_json_to_netscape(json_path: Path, txt_path: Path) -> None:
	raw = json_path.read_text(encoding="utf-8").strip()
	data = json.loads(raw)
	if not isinstance(data, list):
		raise ValueError("Cookie JSON must be an array.")

	lines = [
		"# Netscape HTTP Cookie File",
		"# Generated from Chrome DevTools cookieStore.getAll() JSON",
	]

	kept = 0
	for item in data:
		if not isinstance(item, dict):
			continue
		name = item.get("name")
		value = item.get("value")
		domain = item.get("domain")
		path = item.get("path", "/")
		secure = item.get("secure", False)
		expires = item.get("expires")

		if not (isinstance(name, str) and name):
			continue
		if not (isinstance(value, str) and value):
			continue
		if not (isinstance(domain, str) and domain.strip()):
			continue
		if not isinstance(path, str) or not path:
			path = "/"

		domain = domain.strip()
		include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
		secure_str = "TRUE" if bool(secure) else "FALSE"
		expiry_unix = _to_unix_expiry(expires)

		lines.append(
			f"{domain}\t{include_subdomains}\t{path}\t{secure_str}\t{expiry_unix}\t{name}\t{value}"
		)
		kept += 1

	if kept == 0:
		raise ValueError("No valid cookies found in JSON.")

	txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ydl_base_opts() -> Dict[str, Any]:
	return {
		"quiet": True,
		"no_warnings": True,

		# Every URL passed to process_one() must be treated as one video, even if the original URL contains a playlist parameter.
		"noplaylist": True,

		"cookiefile": GENERATED_COOKIES_TXT,
		"retries": 10,
		"extractor_retries": 10,
		"sleep_interval_requests": 1.0,
		"max_sleep_interval_requests": 3.0,
	}


def extract_info(url: str) -> Dict[str, Any]:
	opts = ydl_base_opts()
	opts["skip_download"] = True
	with yt_dlp.YoutubeDL(opts) as ydl:
		info = ydl.extract_info(url, download=False)
		return cast(Dict[str, Any], info if isinstance(info, dict) else {})


def ffmpeg_encode(src: Path, dst: Path, title: str) -> None:
	cmd = [
		FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
		"-threads", str(FFMPEG_THREADS),
		"-i", str(src),
		"-vn",
		"-codec:a", "libmp3lame",
		"-b:a", "320k",
		"-metadata", f"title={title}",
		str(dst),
	]
	p = subprocess.run(cmd, capture_output=True, text=True)
	if p.returncode != 0:
		raise RuntimeError((p.stderr or "").strip() or "ffmpeg failed")


def process_one(index: int, url: str, output_dir: Path, progress: Progress, task_id: TaskID) -> str:
	last_err: Optional[Exception] = None

	for attempt in range(1, MAX_RETRIES + 1):
		tmpdir = Path(tempfile.mkdtemp(prefix=f"yt_{index}_"))
		try:
			progress.update(task_id, completed=2.0)

			info = extract_info(url)
			raw_title = info.get("title") if isinstance(info, dict) else None
			yt_title = raw_title if isinstance(raw_title, str) and raw_title.strip() else "untitled"
			safe_title = sanitize_for_filename(yt_title)

			final_mp3 = unique_output_path(output_dir / f"{index:03d} - {safe_title}.mp3")
			outtmpl = str(tmpdir / "source.%(ext)s")

			def hook(d: Dict[str, Any]) -> None:
				if d.get("status") == "downloading":
					total = d.get("total_bytes") or d.get("total_bytes_estimate")
					done = d.get("downloaded_bytes")
					if isinstance(total, (int, float)) and total > 0 and isinstance(done, (int, float)):
						frac = max(0.0, min(1.0, done / total))
						progress.update(task_id, completed=frac * 50.0)
				elif d.get("status") == "finished":
					progress.update(task_id, completed=50.0)

			opts = ydl_base_opts()
			opts.update({
				"format": "bestaudio/best",
				"outtmpl": outtmpl,
				"fragment_retries": 10,
				"concurrent_fragment_downloads": 4,
				"progress_hooks": [hook],
			})

			with yt_dlp.YoutubeDL(opts) as ydl:
				ydl.download([url])

			files = [p for p in tmpdir.iterdir() if p.is_file()]
			if not files:
				raise RuntimeError("No downloaded source file found")
			src = max(files, key=lambda p: p.stat().st_size)

			start = time.time()
			proc = subprocess.Popen(
				[
					FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
					"-threads", str(FFMPEG_THREADS),
					"-i", str(src), "-vn",
					"-codec:a", "libmp3lame", "-b:a", "320k",
					"-metadata", f"title={yt_title}",
					str(final_mp3),
				],
				stdout=subprocess.DEVNULL,
				stderr=subprocess.PIPE,
				text=True,
			)

			while proc.poll() is None:
				elapsed = time.time() - start
				simulated = 50.0 + min(45.0, 45.0 * (1.0 - (2.71828 ** (-elapsed / 6.0))))
				progress.update(task_id, completed=simulated)
				time.sleep(0.3)

			_, err = proc.communicate()
			if proc.returncode != 0:
				raise RuntimeError((err or "").strip() or "ffmpeg failed")

			progress.update(task_id, completed=100.0)
			return f"[OK] {index:03d}: {final_mp3.name}"

		except Exception as e:
			last_err = e
			time.sleep(1.5 * attempt)
		finally:
			shutil.rmtree(tmpdir, ignore_errors=True)

	progress.update(task_id, completed=100.0)
	return (
		f"[ERROR] {index:03d}: {url} failed after "
		f"{MAX_RETRIES} attempts. Last error: {last_err}"
	)


YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_youtube_video_id(value: str) -> Optional[str]:
	"""
	Extract a YouTube video ID from common URL forms or from a bare ID.

	Supported examples:
	  dQw4w9WgXcQ
	  https://www.youtube.com/watch?v=dQw4w9WgXcQ
	  https://youtu.be/dQw4w9WgXcQ
	  https://www.youtube.com/shorts/dQw4w9WgXcQ
	  https://www.youtube.com/embed/dQw4w9WgXcQ
	  https://www.youtube.com/live/dQw4w9WgXcQ
	"""
	value = value.strip()

	if YOUTUBE_VIDEO_ID_RE.fullmatch(value):
		return value

	try:
		parsed = urlparse(value)
	except ValueError:
		return None

	host = (parsed.hostname or "").lower()
	path_parts = [part for part in parsed.path.split("/") if part]

	if host in {"youtu.be", "www.youtu.be"}:
		if path_parts and YOUTUBE_VIDEO_ID_RE.fullmatch(path_parts[0]):
			return path_parts[0]
		return None

	if host == "youtube.com" or host.endswith(".youtube.com"):
		query = parse_qs(parsed.query)

		video_values = query.get("v")
		if video_values:
			video_id = video_values[0]
			if YOUTUBE_VIDEO_ID_RE.fullmatch(video_id):
				return video_id

		if len(path_parts) >= 2 and path_parts[0] in {
			"shorts",
			"embed",
			"live",
			"v",
		}:
			video_id = path_parts[1]
			if YOUTUBE_VIDEO_ID_RE.fullmatch(video_id):
				return video_id

	return None


def canonical_youtube_url(video_id: str) -> str:
	return f"https://www.youtube.com/watch?v={video_id}"


def resolve_all_urls(
	playlist_urls: list[str],
	static_urls: list[str],
) -> list[str]:
	"""
	Create one ordered list containing:

	1. Unique videos from URLS, in their configured order.
	2. Unique videos extracted from PLAYLIST_URLS, in playlist order.

	Deduplication is performed using the YouTube video ID, so URL parameters
	and alternate URL forms do not result in duplicate downloads.
	"""
	combined_urls: list[str] = []
	seen_video_ids: set[str] = set()
	seen_other_urls: set[str] = set()

	def add_video(candidate: str) -> bool:
		candidate = candidate.strip()
		if not candidate:
			return False

		video_id = extract_youtube_video_id(candidate)

		if video_id:
			if video_id in seen_video_ids:
				return False

			seen_video_ids.add(video_id)
			combined_urls.append(canonical_youtube_url(video_id))
			return True

		# Preserve an unrecognized URL so yt-dlp can try it, while still
		# removing exact duplicates.
		if candidate in seen_other_urls:
			return False

		seen_other_urls.add(candidate)
		combined_urls.append(candidate)
		return True

	# Add and deduplicate the manually configured videos first.
	for url in static_urls:
		add_video(url)

	if not playlist_urls:
		return combined_urls

	print(f"Checking {len(playlist_urls)} playlist(s) for additional videos...")

	playlist_opts: Dict[str, Any] = {
		"quiet": True,
		"no_warnings": True,
		"extract_flat": True,
		"skip_download": True,

		# Playlist extraction must be enabled here.
		"noplaylist": False,

		# Continue when an individual deleted/private playlist entry
		# cannot be resolved.
		"ignoreerrors": True,

		"retries": 10,
		"extractor_retries": 10,
	}

	if Path(GENERATED_COOKIES_TXT).is_file():
		playlist_opts["cookiefile"] = GENERATED_COOKIES_TXT

	with yt_dlp.YoutubeDL(playlist_opts) as ydl:
		for playlist_url in playlist_urls:
			try:
				info = ydl.extract_info(playlist_url, download=False)

				if not isinstance(info, dict):
					print(f"  [Warning] No playlist information: {playlist_url}")
					continue

				entries = info.get("entries") or []
				added_count = 0
				duplicate_count = 0
				unavailable_count = 0

				for entry in entries:
					if not isinstance(entry, dict):
						unavailable_count += 1
						continue

					# Flat playlist extraction normally supplies the ID directly. Fall back to URL-related fields if needed.
					candidate = (
						entry.get("id")
						or entry.get("webpage_url")
						or entry.get("url")
					)

					if not isinstance(candidate, str):
						unavailable_count += 1
						continue

					if add_video(candidate):
						added_count += 1
					else:
						duplicate_count += 1

				playlist_title = info.get("title")
				if not isinstance(playlist_title, str):
					playlist_title = playlist_url

				print(
					f"  -> {playlist_title}: "
					f"{added_count} added, "
					f"{duplicate_count} duplicates, "
					f"{unavailable_count} unavailable"
				)

			except Exception as e:
				print(
					f"  [Warning] Could not read playlist "
					f"({playlist_url}): {e}"
				)

	return combined_urls


if __name__ == "__main__":
	console = Console()

	# Prepare cookies
	json_p = Path(YOUTUBE_COOKIES_JSON)
	txt_p = Path(GENERATED_COOKIES_TXT)

	if json_p.exists():
		try:
			convert_devtools_json_to_netscape(json_p, txt_p)
		except Exception as e:
			console.print(f"[bold red]Cookie conversion failed:[/bold red] {e}")
			sys.exit(1)
	elif not txt_p.exists():
		console.print("[bold yellow]Warning: No cookies found. Download might fail or be throttled.[/bold yellow]")

	# Build the consolidated download list from both target fields
	FINAL_DOWNLOAD_LIST = resolve_all_urls(PLAYLIST_URLS, URLS)
	if not FINAL_DOWNLOAD_LIST:
		console.print("[bold red]No track or playlist URLs found to download.[/bold red]")
		sys.exit(0)
	
	out_dir = Path(OUTPUT_FOLDER)
	out_dir.mkdir(parents=True, exist_ok=True)
	console.print(f"[bold green]Starting batch download of {len(FINAL_DOWNLOAD_LIST)} tracks to folder '{OUTPUT_FOLDER}'...[/bold green]\n")
	
	# Main download pipeline pointed directly to our unified list
	with Progress(
			TextColumn("{task.description}"),
			BarColumn(),
			TaskProgressColumn(),
			TimeElapsedColumn(),
			TimeRemainingColumn(),
			console=console,
		) as progress:
			task_id = progress.add_task(
				"Preparing...",
				total=100.0,
				completed=0.0,
			)

			total_tracks = len(FINAL_DOWNLOAD_LIST)

			for i, url in enumerate(FINAL_DOWNLOAD_LIST, start=1):
				progress.reset(
					task_id,
					description=f"Track {i:03d}/{total_tracks:03d}",
					total=100.0,
					completed=0.0,
					start=True,
				)

				res = process_one(
					i,
					url,
					out_dir,
					progress,
					task_id,
				)
				progress.print(res)

			console.print("\n[bold green]Batch processing completed.[/bold green]")

