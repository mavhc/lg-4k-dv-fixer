#!/usr/bin/env python3
import argparse
import glob
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from tqdm import tqdm


HDR10PLUS_SCRIPT = Path(__file__).with_name("remove-hdr10plus.py")


TEXT_SUBTITLE_CODECS = {
    "subrip",
    "srt",
    "ass",
    "ssa",
    "mov_text",
    "webvtt",
    "text",
    "ttml",
    "microdvd",
    "jacosub",
    "sami",
    "stl",
    "subviewer",
    "subviewer1",
    "vplayer",
    "mpl2",
    "pjs",
    "realtext",
}

BITMAP_SUBTITLE_CODECS = {
    "hdmv_pgs_subtitle",
    "dvd_subtitle",
    "dvb_subtitle",
    "dvdsub",
    "pgssub",
    "xsub",
    "dvb_teletext",
    "eia_608",
}


def load_hdr10plus_module():
    """Load the sibling remove-hdr10plus.py script as a module."""
    if not HDR10PLUS_SCRIPT.is_file():
        raise FileNotFoundError(
            f"HDR10+ removal helper not found: {HDR10PLUS_SCRIPT}"
        )
    spec = importlib.util.spec_from_file_location(
        "remove_hdr10plus", HDR10PLUS_SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_text_file(path):
    """Return a log file's contents, or an empty string if it is unreadable."""
    try:
        with open(path, "r", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def run_with_progress(proc, total_us):
    """Show a tqdm bar while a muxer writes -progress output to stdout."""

    def _format_time(us):
        secs = us / 1_000_000
        m, s = divmod(int(secs), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    if total_us:
        bar = tqdm(
            total=total_us,
            unit="us",
            bar_format="  {l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
            leave=True,
            file=sys.stderr,
        )
        bar.unit = ""
        bar.bar_format = "  {l_bar}{bar}| {desc} [{elapsed}<{remaining}]"
        bar.set_description(f"0:00 / {_format_time(total_us)}")
    else:
        bar = tqdm(
            unit=" chunks",
            bar_format="  Remuxing... {n_fmt} chunks [{elapsed}]",
            leave=True,
            file=sys.stderr,
        )

    last_update = 0.0
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time_us="):
            try:
                current_us = int(line.split("=", 1)[1])
            except (ValueError, IndexError):
                continue
            now = time.monotonic()
            if now - last_update < 0.1:
                continue
            last_update = now
            if total_us:
                increment = current_us - bar.n
                if increment > 0:
                    bar.update(increment)
                bar.set_description(
                    f"{_format_time(current_us)} / {_format_time(total_us)}"
                )
            else:
                bar.update(1)
        elif line == "progress=end":
            if total_us and bar.n < total_us:
                bar.update(total_us - bar.n)
                bar.set_description(
                    f"{_format_time(total_us)} / {_format_time(total_us)}"
                )

    bar.close()


def probe_media_file(file_path):
    """
    Probe format tags and stream information using ffprobe.
    Returns:
        (metadata_tuple_or_None, text_sub_indices, skipped_bitmap_count, duration_secs_or_None, probe_succeeded, tags_lower, is_hevc, has_dovi, audio_streams, primary_video_idx)
    """
    try:
        cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(file_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
    except (
        subprocess.SubprocessError,
        FileNotFoundError,
        ValueError,
        json.JSONDecodeError,
    ):
        return None, [], 0, None, False, {}, False, False, [], 0

    metadata = None
    fmt = data.get("format", {})
    tags = fmt.get("tags", {})
    tags_lower = {k.lower(): v for k, v in tags.items()}

    # Duration in seconds (float)
    duration = None
    try:
        duration = float(fmt.get("duration", 0)) or None
    except (TypeError, ValueError):
        pass

    show = tags_lower.get("show") or tags_lower.get("tvshow")
    season = tags_lower.get("season_number") or tags_lower.get("season")
    episode = tags_lower.get("episode_sort") or tags_lower.get("episode_number")

    if show and season and episode:
        try:
            metadata = (
                show.strip(),
                int(season),
                int(episode),
                tags_lower.get("title"),
            )
        except ValueError:
            pass

    streams = data.get("streams", [])
    text_sub_indices = []
    skipped_bitmap_count = 0
    is_hevc = False
    has_dovi = False
    audio_streams = []
    all_video_streams = []

    for s in streams:
        codec_type = s.get("codec_type")
        codec_name = s.get("codec_name", "").lower()
        if codec_type == "video":
            all_video_streams.append(s)
        elif codec_type == "subtitle":
            idx = s.get("index")
            if codec_name in BITMAP_SUBTITLE_CODECS:
                skipped_bitmap_count += 1
            else:
                text_sub_indices.append(idx)
        elif codec_type == "audio":
            s_idx = s.get("index")
            profile = s.get("profile") or ""
            channels = s.get("channels")
            ch_layout = s.get("channel_layout") or (f"{channels}ch" if channels else "")
            bit_rate = s.get("bit_rate")
            br_str = f"{int(bit_rate)//1000} kb/s" if bit_rate and str(bit_rate).isdigit() else ""
            stags = s.get("tags", {})
            lang = stags.get("language") or stags.get("LANGUAGE") or "und"
            title = stags.get("title") or stags.get("TITLE") or ""
            disp = s.get("disposition", {})
            audio_streams.append({
                "stream_index": s_idx,
                "audio_index": len(audio_streams),
                "codec": codec_name,
                "profile": profile,
                "channels": channels,
                "channel_layout": ch_layout,
                "bit_rate_str": br_str,
                "language": lang,
                "title": title,
                "default": bool(disp.get("default", 0)),
                "comment": bool(disp.get("comment", 0)),
                "forced": bool(disp.get("forced", 0)),
                "hearing_impaired": bool(disp.get("hearing_impaired", 0)),
            })

    # Identify primary video stream: skip attached pictures and cover art (mjpeg/png with 1 frame or duration 0)
    primary_video_idx = None
    for v in all_video_streams:
        if any(
            "DOVI" in (sd.get("side_data_type") or "").upper()
            for sd in v.get("side_data_list", [])
        ):
            has_dovi = True
        disp = v.get("disposition", {})
        is_attached = bool(disp.get("attached_pic", 0))
        c_name = v.get("codec_name", "").lower()
        # If there are multiple video streams, skip image tracks/cover art
        if len(all_video_streams) > 1 and (is_attached or c_name in {"mjpeg", "png", "bmp", "tiff"}):
            continue
        primary_video_idx = v.get("index")
        if c_name in {"hevc", "h265"}:
            is_hevc = True
        break

    # Fallback to the first video stream if none matched
    if primary_video_idx is None and all_video_streams:
        primary_video_idx = all_video_streams[0].get("index", 0)
        if all_video_streams[0].get("codec_name", "").lower() in {"hevc", "h265"}:
            is_hevc = True

    return metadata, text_sub_indices, skipped_bitmap_count, duration, True, tags_lower, is_hevc, has_dovi, audio_streams, primary_video_idx


def format_audio_desc(a):
    """Return a human-readable description of an audio track dictionary."""
    lang = a.get("language") or "und"
    codec = (a.get("codec") or "unknown").upper()
    layout = a.get("channel_layout") or ""
    bitrate = a.get("bit_rate_str") or ""
    title = a.get("title") or ""

    desc = f"Stream #{a['stream_index']} ({lang}): {codec}"
    if layout:
        desc += f" {layout}"
    if bitrate:
        desc += f" @ {bitrate}"
    if title:
        desc += f' - "{title}"'

    flags = []
    if a.get("default"):
        flags.append("default")
    if a.get("comment"):
        flags.append("commentary")
    if a.get("hearing_impaired"):
        flags.append("SDH")
    if flags:
        desc += f" [{', '.join(flags)}]"

    return desc


def prompt_user_select_audio(audio_streams, filename):
    """
    Interactively lists audio tracks and prompts the user to select one.
    Returns:
        dict of selected audio track, "all" to keep all tracks, or None.
    """
    if not audio_streams:
        print("  Notice: No audio tracks found to select from.")
        return None

    if len(audio_streams) == 1:
        print(f"  Only 1 audio track found: [{audio_streams[0]['audio_index']}] {format_audio_desc(audio_streams[0])} (auto-selected)")
        return audio_streams[0]

    # Find default track index
    default_idx = 0
    for i, a in enumerate(audio_streams):
        if a.get("default"):
            default_idx = i
            break

    print(f"\n  Found {len(audio_streams)} audio tracks in '{filename}':")
    for i, a in enumerate(audio_streams):
        marker = " *" if i == default_idx else "  "
        print(f"   {marker}[{i}] {format_audio_desc(a)}")

    while True:
        try:
            prompt = f"  Select audio track to keep [0-{len(audio_streams)-1}], 'a' for all [default: {default_idx}]: "
            user_input = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(f"\n  Selection cancelled or EOF. Using default track [{default_idx}].")
            return audio_streams[default_idx]

        if not user_input:
            print(f"  Using default track [{default_idx}].")
            return audio_streams[default_idx]

        if user_input in ("a", "all"):
            print("  Keeping all audio tracks.")
            return "all"

        try:
            choice = int(user_input)
            if 0 <= choice < len(audio_streams):
                print(f"  Selected track [{choice}]: {format_audio_desc(audio_streams[choice])}")
                return audio_streams[choice]
            else:
                print(f"  Please enter a number between 0 and {len(audio_streams)-1}, or 'a'.")
        except ValueError:
            print(f"  Invalid choice '{user_input}'. Please enter a track number or 'a'.")


def get_metadata_via_ffprobe(file_path):
    """Attempt to extract embedded show name, season, and episode using ffprobe."""
    res = probe_media_file(file_path)
    return res[0]


def find_external_subtitles(video_path):
    """Find sidecar subtitle files matching the video file stem."""
    sub_exts = {".srt", ".vtt", ".ass", ".ssa"}
    parent = video_path.parent
    stem = video_path.stem
    subs = []
    if not parent.exists():
        return subs
    for p in sorted(parent.iterdir()):
        if p.is_file() and p.suffix.lower() in sub_exts:
            if p.name.startswith(stem):
                suffix_part = p.name[len(stem) : -len(p.suffix)].strip("._-")
                lang = suffix_part if suffix_part else None
                subs.append((p, lang))
    return subs


ROMAN_NUMERALS = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii"}


def format_title_dotted(name_raw):
    """Format string into Title.Case.With.Dots, preserving Roman numerals."""
    words = []
    for w in name_raw.split():
        if w.lower() in ROMAN_NUMERALS:
            words.append(w.upper())
        else:
            words.append(w.capitalize())
    return ".".join(words)


def parse_tv_filename(file_path):
    """Fallback parser using standard regex patterns for TV filenames."""
    filename = Path(file_path).stem

    # Pattern 1: Show.Name.S01E05.Optional.Title or Show Name - S01E05
    match = re.search(
        r"^(.*?)[. _-]+[Ss](\d{1,2})[Ee](\d{1,2})(?:[. _-]+(.*))?$", filename
    )
    if match:
        show = match.group(1).replace(".", " ").replace("_", " ").strip()
        season = int(match.group(2))
        episode = int(match.group(3))
        ep_title = (
            match.group(4).replace(".", " ").replace("_", " ").strip()
            if match.group(4)
            else None
        )
        return show, season, episode, ep_title

    # Pattern 2: Show.Name.1x05
    match = re.search(
        r"^(.*?)[. _-]+(\d{1,2})x(\d{1,2})(?:[. _-]+(.*))?$", filename
    )
    if match:
        show = match.group(1).replace(".", " ").replace("_", " ").strip()
        season = int(match.group(2))
        episode = int(match.group(3))
        ep_title = (
            match.group(4).replace(".", " ").replace("_", " ").strip()
            if match.group(4)
            else None
        )
        return show, season, episode, ep_title

    return None


parse_filename = parse_tv_filename


def parse_film_filename(file_path):
    """
    Parser for film/movie filenames.
    Extracts film title and optional release year.
    Returns:
        (film_title, year_or_None) or None
    """
    path = Path(file_path)
    stem = path.stem

    # If the file stem is generic, fallback to parent directory name
    generic_stems = {"movie", "feature", "video", "main", "film", "cd1", "cd2"}
    if stem.lower() in generic_stems and path.parent.name:
        stem = path.parent.name

    # Strip release group brackets like [IPT], [rarbg], etc.
    clean = re.sub(r"\[[^\]]*\]", "", stem).strip()

    # Pattern 1: Title (Year) or Title [Year]
    m = re.search(r"^(.*?)[. _-]+[\(\[]((?:18|19|20)\d{2})[\)\]]", clean)
    if m and m.group(1).strip(" ._-"):
        title = m.group(1).replace(".", " ").replace("_", " ").strip()
        year = int(m.group(2))
        return title, year

    # Pattern 2: 4-digit year bounded by word boundaries
    matches = list(re.finditer(r"\b((?:18|19|20)\d{2})\b", clean))
    if matches:
        last = matches[-1]
        if last.start() == 0:
            # Starts with 4 digits and has no subsequent year (e.g. 2012.1080p)
            tag_pattern = r"[. _-]+(?:2160p|1080p|1080i|720p|576p|480p|4k|uhd|bluray|blu-ray|web-dl|webdl|webrip|web-rip|web|hdtv|pdtv|dvdrip|dvd|remux|x264|x265|hevc|h264|h265|avc|av1|10bit|dovi|dv|hdr|hdr10).*$"
            rem = re.sub(tag_pattern, "", clean, flags=re.IGNORECASE)
            title = rem.replace(".", " ").replace("_", " ").strip()
            return title or clean, None
        else:
            title_part = clean[:last.start()].strip(" ._-")
            tag_pattern = r"[. _-]+(?:2160p|1080p|1080i|720p|576p|480p|4k|uhd|bluray|blu-ray|web-dl|webdl|webrip|web-rip|web|hdtv|pdtv|dvdrip|dvd|remux|x264|x265|hevc|h264|h265|avc|av1|10bit|dovi|dv|hdr|hdr10).*$"
            cleaned_title = re.sub(tag_pattern, "", title_part, flags=re.IGNORECASE)
            title = cleaned_title.replace(".", " ").replace("_", " ").strip()
            year = int(last.group(1))
            return title or title_part.replace(".", " ").replace("_", " ").strip(), year

    # Pattern 3: No year found - strip standard release tokens from the end
    tag_pattern = r"[. _-]+(?:2160p|1080p|1080i|720p|576p|480p|4k|uhd|bluray|blu-ray|web-dl|webdl|webrip|web-rip|web|hdtv|pdtv|dvdrip|dvd|remux|x264|x265|hevc|h264|h265|avc|av1|10bit|dovi|dv|hdr|hdr10).*$"
    cleaned = re.sub(tag_pattern, "", clean, flags=re.IGNORECASE)
    title = cleaned.replace(".", " ").replace("_", " ").strip()
    return title or clean, None


def identify_media(file_path, probe_meta=None, tags_lower=None):
    """
    Determine if file is a TV Episode or Film, and extract metadata.
    Returns:
        dict with metadata or None
    """
    tags_lower = tags_lower or {}

    # 1. Embedded TV tags
    if probe_meta:
        show, season, ep, title = probe_meta
        return {
            "type": "episode",
            "show": show,
            "season": season,
            "episode": ep,
            "title": title,
        }

    # 2. TV filename patterns (S01E05, 1x05)
    tv_match = parse_tv_filename(file_path)
    if tv_match:
        show, season, ep, title = tv_match
        return {
            "type": "episode",
            "show": show,
            "season": season,
            "episode": ep,
            "title": title,
        }

    # 3. Film filename patterns
    film_match = parse_film_filename(file_path)
    if film_match:
        film_title, year = film_match
        if not year:
            date_val = tags_lower.get("date") or tags_lower.get("year") or tags_lower.get("creation_time")
            if date_val:
                m_yr = re.search(r"\b((?:18|19|20)\d{2})\b", str(date_val))
                if m_yr:
                    year = int(m_yr.group(1))
        return {
            "type": "film",
            "title": film_title,
            "year": year,
        }

    return None


def find_or_create_show_dir(base_path, show_name_raw):
    """
    Checks base_path for an existing folder matching the show name.
    Matches case-insensitively and standardizes spaces/dots for comparison.
    If found, returns existing folder path. Otherwise returns base_path / Formatted.Show.Name
    """
    target_clean = re.sub(r"[._\s]+", "", show_name_raw).lower()

    if base_path.exists():
        for item in base_path.iterdir():
            if item.is_dir():
                existing_clean = re.sub(r"[._\s]+", "", item.name).lower()
                if existing_clean == target_clean:
                    return item

    # Fallback to Title.Case.With.Dots if no matching folder exists
    formatted_dotted = format_title_dotted(show_name_raw)
    return base_path / formatted_dotted


def find_or_create_film_dir(base_path, film_title_raw, year=None):
    """
    Checks base_path for an existing folder matching the film name and year.
    Matches case-insensitively and standardizes spaces/dots for comparison.
    If found, returns existing folder path. Otherwise returns base_path / Formatted.Film.Name[.Year]
    """
    film_clean = re.sub(r"[._\s]+", "", film_title_raw).lower()
    film_year_clean = f"{film_clean}{year}" if year else film_clean

    if base_path.exists():
        # Check if base_path itself is the film directory
        base_clean = re.sub(r"[._\s]+", "", base_path.name).lower()
        if base_clean in (film_clean, film_year_clean):
            return base_path

        # 1. Exact match with year (e.g. "Scream.1996" or "Scream (1996)")
        if year:
            for item in base_path.iterdir():
                if item.is_dir():
                    existing_clean = re.sub(r"[._\s]+", "", item.name).lower()
                    if existing_clean == film_year_clean:
                        return item

        # 2. Match without year
        for item in base_path.iterdir():
            if item.is_dir():
                existing_clean = re.sub(r"[._\s]+", "", item.name).lower()
                if existing_clean == film_clean:
                    return item

    # Fallback: create Formatted.Film.Title[.Year]
    formatted_dotted = format_title_dotted(film_title_raw)
    if year:
        return base_path / f"{formatted_dotted}.{year}"
    return base_path / formatted_dotted


def check_disk_space(target_dir, required_bytes, buffer_ratio=1.1):
    """Check if the target directory's filesystem has enough free space."""
    check_path = target_dir
    while not check_path.exists() and check_path.parent != check_path:
        check_path = check_path.parent

    total, used, free = shutil.disk_usage(check_path)
    needed_bytes = int(required_bytes * buffer_ratio)

    if free < needed_bytes:
        free_gb = free / (1024**3)
        needed_gb = needed_bytes / (1024**3)
        return False, free_gb, needed_gb
    return True, free / (1024**3), needed_bytes / (1024**3)


SUBTITLE_EXTENSIONS = {".srt", ".vtt", ".ass", ".ssa"}


def clean_matching_episodes(target_dir, season_num, ep_num, label="same", dry_run=False, exclude_file=None):
    """Find and remove existing episode .mp4 files matching SXXEXX in the target directory."""
    if not target_dir.exists() or ep_num < 1:
        return

    pattern = re.compile(rf"[Ss]{season_num:02d}[Ee]{ep_num:02d}", re.IGNORECASE)

    for item in target_dir.iterdir():
        if item.is_file() and pattern.search(item.name):
            # Only delete MP4 files - never delete subtitles (.srt, etc.) or other file types
            if item.suffix.lower() != ".mp4":
                continue
            if exclude_file and item.resolve() == Path(exclude_file).resolve():
                continue
            if dry_run:
                print(f"  [DRY RUN] Would remove ({label} episode S{season_num:02d}E{ep_num:02d}): {item.name}")
            else:
                print(f"  Removing previous MP4 ({label} episode S{season_num:02d}E{ep_num:02d}): {item.name}")
                item.unlink()


def clean_matching_film(target_dir, film_title, year=None, dry_run=False, exclude_file=None, destination=None, is_dedicated_dir=False):
    """Find and remove existing movie .mp4 files matching the film in the target directory."""
    if not target_dir.exists():
        return

    film_clean = re.sub(r"[._\s]+", "", film_title).lower()
    film_year_clean = f"{film_clean}{year}" if year else film_clean

    for item in target_dir.iterdir():
        if not item.is_file() or item.suffix.lower() != ".mp4":
            continue
        if exclude_file and item.resolve() == Path(exclude_file).resolve():
            continue

        should_remove = False
        if destination and item.resolve() == destination.resolve():
            should_remove = True
        elif is_dedicated_dir:
            should_remove = True
        else:
            item_clean = re.sub(r"[._\s]+", "", item.stem).lower()
            if item_clean in (film_clean, film_year_clean):
                should_remove = True

        if should_remove:
            if dry_run:
                print(f"  [DRY RUN] Would remove previous movie MP4: {item.name}")
            else:
                print(f"  Removing previous movie MP4: {item.name}")
                item.unlink()


def load_file_list(list_path):
    """
    Read paths to media files from a text file (one path or wildcard pattern per line).
    Strips whitespace, surrounding quotes, and ignores blank lines and comments (#).
    Resolves wildcards and relative paths against the current working directory, or relative to the list file location.
    """
    path = Path(list_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"List file not found: {list_path}")

    files = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            clean = line.strip()
            if not clean or clean.startswith("#"):
                continue
            # Strip surrounding quotes if present
            if (clean.startswith('"') and clean.endswith('"')) or (
                clean.startswith("'") and clean.endswith("'")
            ):
                clean = clean[1:-1].strip()

            if glob.has_magic(clean):
                expanded = os.path.expanduser(clean)
                matches = glob.glob(expanded, recursive=True)
                if not matches and not os.path.isabs(expanded):
                    candidate = str(path.parent / clean)
                    matches = glob.glob(candidate, recursive=True)
                for m in sorted(matches):
                    if os.path.isfile(m):
                        files.append(str(Path(m).resolve()))
            else:
                item_path = Path(os.path.expanduser(clean))
                # If relative, check relative to current dir first, then relative to list file dir
                if not item_path.is_absolute():
                    if not item_path.exists() and (path.parent / item_path).exists():
                        item_path = (path.parent / item_path).resolve()
                    else:
                        item_path = item_path.resolve()
                files.append(str(item_path))

    return files


def remux_hdr10plus(
    file_path,
    destination,
    info,
    dovi_box,
    selected_audio,
    include_subs,
    active_text_subs,
    probe_ok,
    external_subs,
    keep_chapters,
    duration_secs,
):
    """Stream HEVC through dovi_tool and mux straight into the final MP4.

    No full-size intermediate video or audio file is created: ffmpeg pipes the
    primary HEVC stream through dovi_tool (which drops HDR10+ metadata) into a
    second ffmpeg that muxes the original audio/subtitles.  Neither the input
    file nor any temp media file persists.
    """
    module = load_hdr10plus_module()
    if not shutil.which("dovi_tool"):
        print("  Error: dovi_tool not found on PATH; cannot remove HDR10+.")
        return False

    rate = module.rate_arg(info["rate"])
    factor = max(1, -(-1000 // info["rate"].numerator))
    timescale = info["rate"].numerator * factor
    offset = module.audio_offset_for_mux(info)
    base_input = 1  # input 0 is the cleaned HEVC pipe, input 1 the original

    extract_cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(file_path),
        "-map",
        f"0:{info['video_index']}",
        "-an",
        "-sn",
        "-dn",
        "-c:v",
        "copy",
        "-bsf:v",
        "hevc_mp4toannexb",
        "-f",
        "hevc",
        "pipe:1",
    ]

    mux_cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-r",
        rate,
        "-f",
        "hevc",
        "-i",
        "pipe:0",
    ]
    # Keep audio aligned with the piped video (positive offsets are clamped to
    # zero so the track starts at 0, which some TVs require).
    if abs(offset) >= 0.0005:
        mux_cmd += ["-itsoffset", f"{offset:.6f}"]
    mux_cmd += ["-i", str(file_path)]
    for sub_path, _ in external_subs:
        mux_cmd += ["-i", str(sub_path)]

    # Video always comes from the cleaned pipe.
    mux_cmd += ["-map", "0:v:0"]

    if selected_audio is not None and selected_audio != "all":
        mux_cmd += ["-map", f"{base_input}:{selected_audio['stream_index']}"]
    else:
        mux_cmd += ["-map", f"{base_input}:a?"]

    if include_subs:
        if probe_ok:
            for s_idx in active_text_subs:
                mux_cmd += ["-map", f"{base_input}:{s_idx}"]
        else:
            mux_cmd += ["-map", f"{base_input}:s?"]
        for ext_idx, _ in enumerate(external_subs):
            mux_cmd += ["-map", f"{base_input + 1 + ext_idx}:0"]

    if not keep_chapters:
        mux_cmd += ["-map_chapters", "-1"]

    mux_cmd += ["-c", "copy"]
    if include_subs and (active_text_subs or external_subs or not probe_ok):
        mux_cmd += ["-c:s", "mov_text"]
    if selected_audio is not None and selected_audio != "all":
        mux_cmd += ["-disposition:a:0", "default"]
    mux_cmd += ["-tag:v", "hvc1", "-video_track_timescale", str(timescale)]

    if include_subs and external_subs:
        for out_sub_idx, (_, lang) in enumerate(
            external_subs, start=len(active_text_subs)
        ):
            if lang:
                mux_cmd += [
                    f"-metadata:s:s:{out_sub_idx}",
                    f"language={lang}",
                ]

    # Trim any trailing audio hidden by the source container's edit list.
    if info["video_media_end"]:
        mux_cmd += ["-t", f"{info['video_media_end']:.6f}"]

    mux_cmd += [
        "-strict",
        "unofficial",
        "-max_muxing_queue_size",
        "4096",
        "-progress",
        "pipe:1",
        "-nostats",
        str(destination),
    ]

    total_us = int(duration_secs * 1_000_000) if duration_secs else None
    work_dir = tempfile.mkdtemp(
        prefix=".hdr10plus-", dir=str(destination.parent)
    )
    log_paths = {
        name: os.path.join(work_dir, f"{name}.log")
        for name in ("extract", "dovi", "mux")
    }
    log_files = {}
    procs = []

    try:
        for name, path in log_paths.items():
            log_files[name] = open(path, "w")

        p_extract = subprocess.Popen(
            extract_cmd,
            stdout=subprocess.PIPE,
            stderr=log_files["extract"],
        )
        procs.append(p_extract)
        p_dovi = subprocess.Popen(
            ["dovi_tool", "--drop-hdr10plus", "convert", "-", "-o", "/dev/stdout"],
            stdin=p_extract.stdout,
            stdout=subprocess.PIPE,
            stderr=log_files["dovi"],
        )
        procs.append(p_dovi)
        p_extract.stdout.close()
        p_mux = subprocess.Popen(
            mux_cmd,
            stdin=p_dovi.stdout,
            stdout=subprocess.PIPE,
            stderr=log_files["mux"],
            text=True,
        )
        procs.append(p_mux)
        p_dovi.stdout.close()

        run_with_progress(p_mux, total_us)
        mux_rc = p_mux.wait()
        dovi_rc = p_dovi.wait()
        extract_rc = p_extract.wait()

        if mux_rc != 0 or dovi_rc != 0 or extract_rc != 0:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
            for name in ("extract", "dovi", "mux"):
                log_files[name].flush()
                text = read_text_file(log_paths[name])
                if text:
                    print(f"--- {name} ---\n{text}", file=sys.stderr)
            if destination.exists():
                destination.unlink()
            return False
    finally:
        for proc in procs:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
        for handle in log_files.values():
            handle.close()
        shutil.rmtree(work_dir, ignore_errors=True)

    if not destination.exists() or destination.stat().st_size == 0:
        print("  Error: FFmpeg produced no output file.")
        return False

    print("  Restoring original frame timing (B-frame order)...")
    if not module.repair_video_timing(str(destination), info):
        print("  Error: could not restore B-frame timing; output removed.")
        destination.unlink(missing_ok=True)
        return False

    if dovi_box:
        print("  Restoring Dolby Vision configuration record...")
        if not module.inject_dovi_box(str(destination), dovi_box):
            print(
                "  Error: could not inject the Dolby Vision configuration "
                "record; output removed."
            )
            destination.unlink(missing_ok=True)
            return False

    problems, _ = module.verify_output(
        str(destination), info, True, bool(info["audio_streams"])
    )
    for problem in problems:
        print(f"  Warning: {problem}")

    return True


def process_single_file(
    input_file,
    base_path,
    remove_previous=False,
    dry_run=False,
    skip_space_check=False,
    include_subs=True,
    flat=False,
    delete_source=False,
    select_audio=False,
    audio_track=None,
    keep_chapters=False,
    remove_hdr10plus=False,
):
    file_path = Path(input_file).resolve()
    base = Path(base_path).resolve()

    print(f"\nProcessing: {file_path.name}")

    if not file_path.exists():
        print(f"  Error: File '{file_path}' does not exist. Skipping.")
        return False

    file_size = file_path.stat().st_size

    # 1. Parse Metadata & Probe Streams
    probe_meta, text_sub_indices, skipped_bitmap_count, duration_secs, probe_ok, tags_lower, is_hevc, has_dovi, audio_streams, primary_video_idx = probe_media_file(file_path)
    if not probe_ok and not is_hevc:
        if re.search(r"\b(hevc|x265|h265|h\.265)\b", file_path.name, re.IGNORECASE):
            is_hevc = True
    media_info = identify_media(file_path, probe_meta=probe_meta, tags_lower=tags_lower)

    if not media_info:
        print(
            f"  Error: Could not determine TV or film metadata from '{file_path.name}'. Skipping."
        )
        return False

    media_type = media_info["type"]
    is_dedicated = False

    if media_type == "episode":
        show_name_raw = media_info["show"]
        season_num = media_info["season"]
        ep_num = media_info["episode"]
        ep_title = media_info["title"]

        # Target directory: show folder (no Season subfolders)
        target_dir = find_or_create_show_dir(base, show_name_raw)
        show_name_dotted = format_title_dotted(show_name_raw)

        # Destination Filename: Force extension to .mp4
        if ep_title:
            clean_title = re.sub(r'[\\/*?:"<>|]', "", ep_title).replace(" ", ".")
            new_filename = (
                f"{show_name_dotted}.S{season_num:02d}E{ep_num:02d}.{clean_title}.mp4"
            )
        else:
            new_filename = f"{show_name_dotted}.S{season_num:02d}E{ep_num:02d}.mp4"

        destination = target_dir / new_filename
    else:
        # Film
        film_title = re.sub(r'[\\/*?:"<>|]', "", media_info["title"]).strip()
        year = media_info["year"]
        film_dotted = format_title_dotted(film_title)

        if flat:
            target_dir = base
            is_dedicated = False
        else:
            target_dir = find_or_create_film_dir(base, film_title, year)
            is_dedicated = (target_dir.resolve() != base.resolve())

        if year:
            new_filename = f"{film_dotted}.{year}.mp4"
        else:
            new_filename = f"{film_dotted}.mp4"

        destination = target_dir / new_filename

    # Audio stream selection
    selected_audio = None
    if audio_track is not None:
        matching = [a for a in audio_streams if a["audio_index"] == audio_track]
        if matching:
            selected_audio = matching[0]
            print(f"  Specified audio track [{audio_track}]: {format_audio_desc(selected_audio)}")
        else:
            print(f"  Warning: Specified audio track #{audio_track} not found (available: 0-{len(audio_streams)-1}). Using all audio tracks.")
    elif select_audio:
        if dry_run:
            print(f"  [DRY RUN] Found {len(audio_streams)} audio track(s):")
            for a in audio_streams:
                print(f"    [{a['audio_index']}] {format_audio_desc(a)}")
            default_audio = next((a for a in audio_streams if a.get("default")), audio_streams[0] if audio_streams else None)
            if default_audio:
                selected_audio = default_audio
                print(f"  [DRY RUN] Would prompt to select audio track (defaulting to [{default_audio['audio_index']}]).")
        else:
            selected_audio = prompt_user_select_audio(audio_streams, file_path.name)

    # Subtitle discovery
    external_subs = find_external_subtitles(file_path) if include_subs else []
    active_text_subs = text_sub_indices if include_subs else []
    total_subs = len(active_text_subs) + len(external_subs)

    # HDR10+ removal (opt-in): requires HEVC and Dolby Vision metadata.
    use_hdr10plus = False
    hdr_info = None
    hdr_dovi_box = None
    if remove_hdr10plus:
        if not is_hevc:
            print("  Note: --remove-hdr10plus needs HEVC video; remuxing without it.")
        elif dry_run:
            if has_dovi:
                use_hdr10plus = True
            else:
                print(
                    "  Note: no Dolby Vision metadata found; "
                    "HDR10+ removal skipped."
                )
        else:
            try:
                hdr_module = load_hdr10plus_module()
                hdr_info = hdr_module.probe_source(str(file_path))
            except (SystemExit, FileNotFoundError, OSError) as exc:
                print(f"  Warning: HDR10+ analysis failed ({exc}); remuxing without it.")
                hdr_info = None
            if hdr_info is not None:
                if hdr_info["dovi"] is None:
                    print(
                        "  Note: no Dolby Vision metadata found; "
                        "HDR10+ removal skipped."
                    )
                else:
                    hdr_dovi_box = hdr_module.extract_dovi_box(
                        str(file_path), hdr_info["dovi"]
                    )
                    if hdr_dovi_box is None:
                        print(
                            "  Warning: Dolby Vision configuration could not be "
                            "read; remuxing without HDR10+ removal."
                        )
                    else:
                        use_hdr10plus = True

    if dry_run:
        if media_type == "episode":
            print(f"  [DRY RUN] Detected TV Episode: {show_name_raw} S{season_num:02d}E{ep_num:02d}")
            print(f"  [DRY RUN] Target directory: {target_dir}")
            clean_matching_episodes(target_dir, season_num, ep_num, label="same", dry_run=True, exclude_file=file_path)
            if remove_previous and ep_num > 1:
                clean_matching_episodes(target_dir, season_num, ep_num - 1, label="predecessor", dry_run=True, exclude_file=file_path)
        else:
            year_info = f" ({year})" if year else ""
            print(f"  [DRY RUN] Detected Film: {film_title}{year_info}")
            print(f"  [DRY RUN] Target directory: {target_dir}")
            clean_matching_film(target_dir, film_title, year=year, dry_run=True, exclude_file=file_path, destination=destination, is_dedicated_dir=is_dedicated)

        print(f"  [DRY RUN] Would remux {file_path.suffix} -> MP4 at: {destination}")
        v_idx_str = f"stream #{primary_video_idx}" if primary_video_idx is not None else "primary"
        if is_hevc:
            print(f"  [DRY RUN] Video codec: HEVC ({v_idx_str}) -> tagging as hvc1 (Apple/Smart TV compatible, avoiding hev1)")
        else:
            print(f"  [DRY RUN] Video stream: {v_idx_str}")

        if remove_hdr10plus:
            if use_hdr10plus:
                print("  [DRY RUN] HDR10+: strip HDR10+ metadata, keep Dolby Vision (streamed through dovi_tool)")
            elif not is_hevc:
                print("  [DRY RUN] HDR10+: skipped (source video is not HEVC)")
            else:
                print("  [DRY RUN] HDR10+: skipped (no Dolby Vision metadata)")

        if not keep_chapters:
            print("  [DRY RUN] Chapters: Stripping chapter markers (-map_chapters -1) to prevent webOS pause bug")
        else:
            print("  [DRY RUN] Chapters: Preserving chapter markers")

        if selected_audio is not None and selected_audio != "all":
            print(f"  [DRY RUN] Audio: Keeping single track [{selected_audio['audio_index']}] {format_audio_desc(selected_audio)}")
        elif selected_audio == "all":
            print("  [DRY RUN] Audio: Keeping all audio tracks")
        else:
            print(f"  [DRY RUN] Audio: Mapping all audio tracks (-map 0:a?) ({len(audio_streams)} track(s) found)")

        if include_subs:
            if total_subs > 0:
                print(
                    f"  [DRY RUN] Subtitles to include: {total_subs} track(s) "
                    f"({len(active_text_subs)} embedded, {len(external_subs)} external) -> mov_text"
                )
            elif not probe_ok:
                print("  [DRY RUN] Subtitles: Probing unavailable, will attempt to include via -map 0:s?")
            else:
                print("  [DRY RUN] Subtitles: None found.")
            if skipped_bitmap_count > 0:
                print(
                    f"  [DRY RUN] Notice: Skipping {skipped_bitmap_count} bitmap subtitle track(s) "
                    "(PGS/VobSub unsupported in MP4 container)."
                )
        else:
            print("  [DRY RUN] Subtitles: Disabled.")
        if delete_source:
            print(f"  [DRY RUN] Would delete original file after remux: {file_path.name}")
        else:
            print(f"  [DRY RUN] Original file left intact at: {file_path}")
        return True

    # 4. Clean up destination directory FIRST (frees storage before checking space)
    target_dir.mkdir(parents=True, exist_ok=True)
    
    if media_type == "episode":
        # Clean older MP4 files matching the SAME episode
        clean_matching_episodes(target_dir, season_num, ep_num, label="same", dry_run=False, exclude_file=file_path)
        # Optionally clean the PREVIOUS numerical episode's MP4 (e.g., S03E02.mp4)
        if remove_previous and ep_num > 1:
            clean_matching_episodes(target_dir, season_num, ep_num - 1, label="predecessor", dry_run=False, exclude_file=file_path)
    else:
        # Clean older MP4 files matching the film
        clean_matching_film(target_dir, film_title, year=year, dry_run=False, exclude_file=file_path, destination=destination, is_dedicated_dir=is_dedicated)

    # 5. Check Free Disk Space AFTER space-reclaiming deletes
    if not skip_space_check:
        has_space, free_gb, needed_gb = check_disk_space(target_dir, file_size)
        if not has_space:
            print(
                f"  Error: Insufficient disk space on target partition.\n"
                f"  Free: {free_gb:.2f} GB | Required (with 10% buffer): {needed_gb:.2f} GB. Skipping."
            )
            return False

    # 6. Remux file directly to MP4 in destination folder using FFmpeg
    if destination.resolve() == file_path.resolve():
        print("  Error: output path is the same as the input file; refusing to overwrite the original.")
        return False
    print(f"  Remuxing to MP4: {destination.name}")
    if is_hevc:
        print("  Tagging HEVC video stream as hvc1 (Apple/Smart TV compatible, avoiding hev1)")
    if not keep_chapters:
        print("  Stripping chapter markers (avoids smart TV / webOS playback pause bug)")
    if selected_audio is not None and selected_audio != "all":
        print(f"  Keeping single audio track: [{selected_audio['audio_index']}] {format_audio_desc(selected_audio)}")
    if include_subs:
        if total_subs > 0:
            print(
                f"  Including {total_subs} subtitle track(s) "
                f"({len(active_text_subs)} embedded, {len(external_subs)} external) converted to mov_text."
            )
        if skipped_bitmap_count > 0:
            print(
                f"  Notice: Skipping {skipped_bitmap_count} bitmap subtitle track(s) "
                "(PGS/VobSub unsupported in MP4 container)."
            )

    def _finish_remux():
        """Report the result and apply the opt-in source deletion."""
        if delete_source:
            print("  Remux complete.")
            if destination.exists() and destination.stat().st_size > 0:
                if destination.resolve() != file_path.resolve() and file_path.exists():
                    print(f"  Deleting original file: {file_path.name}")
                    file_path.unlink()
                    print("  Original file deleted successfully.")
                else:
                    print("  Warning: Source file equals destination or does not exist, skipping deletion.")
            else:
                print("  Warning: Output destination missing or empty, original file kept.")
        else:
            print("  Remux complete. Original file left untouched.")
        return True

    if use_hdr10plus and hdr_info is not None:
        print("  Removing HDR10+ metadata (Dolby Vision preserved, streamed through dovi_tool)...")
        try:
            ok = remux_hdr10plus(
                file_path,
                destination,
                hdr_info,
                hdr_dovi_box,
                selected_audio,
                include_subs,
                active_text_subs,
                probe_ok,
                external_subs,
                keep_chapters,
                duration_secs,
            )
        except (FileNotFoundError, OSError) as exc:
            print(f"  Error during HDR10+ remux: {exc}")
            ok = False

        if not ok:
            if destination.exists():
                destination.unlink()
            return False
        return _finish_remux()

    ffmpeg_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(file_path),
    ]

    # Add external subtitle inputs
    for sub_path, _ in external_subs:
        ffmpeg_cmd.extend(["-i", str(sub_path)])

    # Map only primary video stream (avoiding extra video tracks / embedded cover art)
    if primary_video_idx is not None:
        ffmpeg_cmd.extend(["-map", f"0:{primary_video_idx}"])
    else:
        ffmpeg_cmd.extend(["-map", "0:v:0"])

    # Map audio
    if selected_audio is not None and selected_audio != "all":
        ffmpeg_cmd.extend(["-map", f"0:{selected_audio['stream_index']}"])
    else:
        ffmpeg_cmd.extend(["-map", "0:a?"])

    # Map subtitles
    if include_subs:
        if probe_ok:
            for s_idx in active_text_subs:
                ffmpeg_cmd.extend(["-map", f"0:{s_idx}"])
        else:
            # Fallback if ffprobe failed: attempt to map subtitles optionally
            ffmpeg_cmd.extend(["-map", "0:s?"])

        for ext_idx, _ in enumerate(external_subs, start=1):
            ffmpeg_cmd.extend(["-map", f"{ext_idx}:0"])

    # Strip chapters by default to avoid smart TV / webOS playback pause bug
    if not keep_chapters:
        ffmpeg_cmd.extend(["-map_chapters", "-1"])

    # Codecs: stream copy video and audio
    ffmpeg_cmd.extend([
        "-c:v",
        "copy",
        "-c:a",
        "copy",
    ])

    # Ensure HEVC tag is always hvc1 (never hev1) for Apple, QuickTime, and Smart TV compatibility
    if is_hevc:
        ffmpeg_cmd.extend(["-tag:v", "hvc1"])

    # If single audio track selected, ensure it is set as default
    if selected_audio is not None and selected_audio != "all":
        ffmpeg_cmd.extend(["-disposition:a:0", "default"])

    # Subtitle codec: transcode text subtitles to mov_text for MP4 compatibility
    if include_subs and (total_subs > 0 or not probe_ok):
        ffmpeg_cmd.extend(["-c:s", "mov_text"])

    # Language tags for external subtitles
    if include_subs and external_subs:
        for out_sub_idx, (_, lang) in enumerate(external_subs, start=len(active_text_subs)):
            if lang:
                ffmpeg_cmd.extend([f"-metadata:s:s:{out_sub_idx}", f"language={lang}"])

    ffmpeg_cmd.extend([
        "-movflags",
        "+faststart",
        "-strict",
        "unofficial",
        "-progress",
        "pipe:1",
        "-nostats",
        str(destination),
    ])

    try:
        total_us = int(duration_secs * 1_000_000) if duration_secs else None

        proc = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        run_with_progress(proc, total_us)
        proc.wait()

        if proc.returncode != 0:
            stderr_output = proc.stderr.read() if proc.stderr else ""
            raise subprocess.CalledProcessError(proc.returncode, ffmpeg_cmd, stderr=stderr_output)
    except subprocess.CalledProcessError as e:
        stderr_text = e.stderr if isinstance(e.stderr, str) else e.stderr.decode() if e.stderr else ""
        print(f"  Error during FFmpeg remux: {stderr_text.strip()}")
        if destination.exists():
            destination.unlink()
        return False

    return _finish_remux()


process_single_episode = process_single_file


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Organize and remux TV episodes and films into MP4 files.",
        add_help=False,
    )

    parser.add_argument(
        "-h",
        "--help",
        "-?",
        action="help",
        help="Show this help message and exit",
    )
    parser.add_argument(
        "-i",
        "--input",
        type=str,
        nargs="+",
        help="Path to one or more input video files (space-separated or wildcards), or a text file (.txt/.list) containing file paths",
    )
    parser.add_argument(
        "-l",
        "--list",
        type=str,
        nargs="+",
        help="Path to one or more text files containing lists of video files to convert (one path per line)",
    )
    parser.add_argument(
        "-b",
        "--base-path",
        type=str,
        help="Base directory for your media library (e.g. /path/to/media/library)",
    )
    parser.add_argument(
        "-d",
        "--delete-source",
        "--delete-original",
        "--delete-originals",
        action="store_true",
        help="Delete each original input file immediately after successful conversion (one by one)",
    )
    parser.add_argument(
        "-r",
        "--remove-previous",
        action="store_true",
        help="Remove the previous episode (e.g., S03E02 when processing S03E03) from the destination folder (TV only)",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Preview actions without making changes",
    )
    parser.add_argument(
        "--skip-space-check",
        action="store_true",
        help="Bypass disk free space estimation",
    )
    parser.add_argument(
        "--no-subs",
        action="store_true",
        help="Do not include subtitles in the output MP4 files",
    )
    parser.add_argument(
        "--flat",
        action="store_true",
        help="Do not create subdirectories for films; remux directly into the base directory",
    )
    parser.add_argument(
        "-a",
        "--select-audio",
        "--choose-audio",
        action="store_true",
        help="List audio tracks found in each file and prompt to choose which one to keep in the output MP4",
    )
    parser.add_argument(
        "--audio-track",
        type=int,
        default=None,
        help="Zero-based index of audio track to keep (non-interactive, e.g. --audio-track 0)",
    )
    parser.add_argument(
        "--keep-chapters",
        action="store_true",
        help="Preserve chapter markers in the output MP4 (default: strip chapters to prevent LG TV / webOS playback pause bug)",
    )
    parser.add_argument(
        "--remove-hdr10plus",
        "--remove-hdr10plus-metadata",
        action="store_true",
        help=(
            "Strip HDR10+ metadata from Dolby Vision HEVC sources while keeping "
            "Dolby Vision and original A/V timing (opt-in; ignored for non-HEVC "
            "or non-Dolby-Vision files)"
        ),
    )

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args()

    if not args.base_path:
        parser.error("the following arguments are required: -b/--base-path")

    input_files = []
    if args.list:
        expanded_lists = []
        for list_path in args.list:
            if glob.has_magic(list_path):
                matches = sorted(glob.glob(os.path.expanduser(list_path), recursive=True))
                expanded_lists.extend(matches)
            else:
                expanded_lists.append(list_path)

        for list_path in expanded_lists:
            try:
                input_files.extend(load_file_list(list_path))
            except Exception as e:
                parser.error(f"Error reading list file '{list_path}': {e}")

    if args.input:
        for item in args.input:
            if glob.has_magic(item):
                expanded = os.path.expanduser(item)
                matches = sorted(glob.glob(expanded, recursive=True))
                if not matches:
                    print(f"  Warning: Wildcard pattern '{item}' did not match any files.")
                for m in matches:
                    if os.path.isfile(m):
                        input_files.append(str(Path(m).resolve()))
            else:
                p = Path(item).expanduser()
                if p.is_file() and p.suffix.lower() in {".txt", ".list"}:
                    try:
                        input_files.extend(load_file_list(p))
                    except Exception as e:
                        parser.error(f"Error reading list file '{item}': {e}")
                else:
                    input_files.append(str(p))

    if not input_files:
        parser.error("No input files specified. Provide files via -i/--input or a list via -l/--list.")

    # Deduplicate while preserving order
    seen = set()
    unique_input_files = []
    for f in input_files:
        resolved = str(Path(f).resolve())
        if resolved not in seen:
            seen.add(resolved)
            unique_input_files.append(f)

    successful = 0
    total = len(unique_input_files)

    for file_path in unique_input_files:
        if process_single_file(
            file_path,
            args.base_path,
            remove_previous=args.remove_previous,
            dry_run=args.dry_run,
            skip_space_check=args.skip_space_check,
            include_subs=not args.no_subs,
            flat=args.flat,
            delete_source=args.delete_source,
            select_audio=args.select_audio,
            audio_track=args.audio_track,
            keep_chapters=args.keep_chapters,
            remove_hdr10plus=args.remove_hdr10plus,
        ):
            successful += 1

    print(f"\nFinished processing {successful}/{total} files.")
