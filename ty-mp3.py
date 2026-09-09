#!/usr/bin/env python3
"""
Sequential YouTube -> MP3 (320k) with simple Rich progress bars.

USAGE
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


optional in-line for a single video: yt-dlp --cookies youtube_cookies.txt -f "bestaudio/best" -o "test.%(ext)s" "https://www.youtube.com/watch?v=4Oi4ghnBoRw"
"""

from __future__ import annotations

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
OUTPUT_FOLDER: str = "CD237" # last one CD236

URLS: list[str] = [
	"https://www.youtube.com/watch?v=4Oi4ghnBoRw"
	# ... list youtube urls here 
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
		"ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
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

			final_mp3 = unique_output_path(output_dir / f"{index:02d} - {safe_title}.mp3")
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
					"ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
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
			return f"[OK] {index}: {final_mp3.name}"

		except Exception as e:
			last_err = e
			time.sleep(1.5 * attempt)
		finally:
			shutil.rmtree(tmpdir, ignore_errors=True)

	progress.update(task_id, completed=100.0)
	return f"[FAIL] {index}: {url} -> {last_err}"


def main() -> None:
	console = Console()
	outdir = Path(OUTPUT_FOLDER)

	if outdir.exists():
		console.print(f"[red]ERROR:[/red] Folder '{OUTPUT_FOLDER}' already exists. Aborting.")
		sys.exit(1)

	try:
		convert_devtools_json_to_netscape(Path(YOUTUBE_COOKIES_JSON), Path(GENERATED_COOKIES_TXT))
	except Exception as e:
		console.print(f"[red]ERROR:[/red] Cookie conversion failed: {e}")
		sys.exit(1)

	outdir.mkdir(parents=True, exist_ok=False)

	console.print(f"Starting sequential batch: {len(URLS)} URLs")
	results: list[str] = []

	progress = Progress(
		TextColumn("{task.description}"),
		BarColumn(),
		TaskProgressColumn(),
		TimeElapsedColumn(),
		TimeRemainingColumn(),
		console=console,
		transient=False,
	)

	with progress:
		for i, url in enumerate(URLS, start=1):
			task_id = progress.add_task(f"#{i}", total=100.0, completed=0.0)
			result = process_one(i, url, outdir, progress, task_id)
			results.append(result)

	for line in results:
		console.print(line)
	console.print("Done.")


if __name__ == "__main__":
	main()
