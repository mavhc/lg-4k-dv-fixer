#!/usr/bin/env python3
"""
MP4/Video File Integrity Checker
=================================
Checks video files for:
  1. Container-level corruption (moov atom, stream consistency)
  2. Dolby Vision / HDR metadata consistency
  3. Bitstream decode errors (macroblock/slice errors, truncated NALUs)
  4. Frame/Packet analysis (frozen frames, bitrate drops, PTS discontinuities)
  5. Audio stream decode verification

Usage:
  python3 check_mp4.py <file.mp4> [--full] [--first-minutes N] [--skip-audio] [--skip-frames]

Requirements: ffmpeg, ffprobe (must be in PATH)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


# ── Colours for terminal output ──────────────────────────────────────────────
class C:
    RED = "\033[91m"
    YEL = "\033[93m"
    GRN = "\033[92m"
    CYN = "\033[96m"
    DIM = "\033[2m"
    BLD = "\033[1m"
    RST = "\033[0m"
    CLR = "\033[2K"   # clear entire line


def header(text: str):
    print(f"\n{C.BLD}{C.CYN}{'─' * 60}{C.RST}")
    print(f"{C.BLD}{C.CYN}  {text}{C.RST}")
    print(f"{C.BLD}{C.CYN}{'─' * 60}{C.RST}")


def ok(msg: str):
    print(f"  {C.GRN}✓{C.RST} {msg}")


def warn(msg: str):
    print(f"  {C.YEL}⚠{C.RST} {msg}")


def fail(msg: str):
    print(f"  {C.RED}✗{C.RST} {msg}")


def info(msg: str):
    print(f"  {C.CYN}ℹ{C.RST} {msg}")


# ── Progress bar helpers ─────────────────────────────────────────────────────
def format_time(seconds: float) -> str:
    """Format seconds into HH:MM:SS or MM:SS."""
    s = max(0, int(seconds))
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60:02d}:{s % 60:02d}"


def progress_bar(current: float, total: float, width: int = 30,
                 extra: str = "", errors: int = 0) -> str:
    """Render an inline progress bar string."""
    if total <= 0:
        pct = 0.0
    else:
        pct = min(max(current / total, 0.0), 1.0)
    filled = int(width * pct)
    bar = "█" * filled + "░" * (width - filled)

    err_str = ""
    if errors > 0:
        err_str = f"  {C.RED}{errors} err{C.RST}"

    return (f"\r  {C.CYN}▶{C.RST} [{bar}] {pct * 100:5.1f}%  "
            f"{format_time(current)}/{format_time(total)}"
            f"{err_str}"
            f"  {C.DIM}{extra}{C.RST}")


def clear_progress():
    """Clear the current progress line."""
    print(f"\r{C.CLR}", end="", flush=True)


def parse_ffmpeg_time(text: str) -> float | None:
    """Extract time= value from an ffmpeg progress/stats line."""
    m = re.search(r"\btime=\s*(\d+):(\d+):(\d+)\.(\d+)", text)
    if m:
        h, mn, s, frac = m.groups()
        return int(h) * 3600 + int(mn) * 60 + int(s) + float(f"0.{frac}")
    m = re.search(r"\btime=\s*([\d.]+)", text)
    if m:
        return float(m.group(1))
    return None


def parse_ffmpeg_speed(text: str) -> str:
    """Extract speed= value from ffmpeg stats line."""
    m = re.search(r"\bspeed=\s*([\d.]+x|N/A)", text)
    if m:
        return m.group(1)
    return ""


def parse_ffmpeg_frame(text: str) -> int | None:
    """Extract frame= value from ffmpeg stats line."""
    m = re.search(r"\bframe=\s*(\d+)", text)
    if m:
        return int(m.group(1))
    return None


# ── Data classes ─────────────────────────────────────────────────────────────
@dataclass
class CheckResult:
    name: str
    passed: bool
    details: str = ""
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)


# ── Helper: run subprocess ───────────────────────────────────────────────────
def run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout
    )


def run_ffmpeg_with_progress(cmd: list[str], total_duration: float,
                             label: str = "Decoding",
                             timeout: int = 7200) -> tuple[list[str], float]:
    """Run an ffmpeg command, parsing stderr in real time for progress.

    Works for both video streams (which output frame= and time=) and audio
    streams (which output size= and time= without frame=).
    """
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, text=True, bufsize=1
    )

    error_lines: list[str] = []
    t0 = time.time()
    last_update = 0.0
    current_time = 0.0
    err_count = 0

    try:
        for raw_line in proc.stderr:
            line = raw_line.strip()
            if not line:
                continue

            # ffmpeg stats lines always contain time= (e.g. time=00:01:23.45)
            # or start with frame=
            is_stats = bool(re.search(r"\btime=\s*[\d:.]+", line)) or line.startswith("frame=")

            if is_stats:
                t = parse_ffmpeg_time(line)
                if t is not None:
                    current_time = t
                f = parse_ffmpeg_frame(line)
                speed = parse_ffmpeg_speed(line)

                extra = label
                if f is not None:
                    extra += f"  frame {f}"
                if speed:
                    extra += f"  {speed}"

                # Update progress every 0.15s to avoid terminal flicker
                now = time.time()
                if now - last_update >= 0.15:
                    print(progress_bar(current_time, total_duration,
                                       extra=extra, errors=err_count),
                          end="", flush=True)
                    last_update = now
            else:
                error_lines.append(line)
                err_count += 1

        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        error_lines.append(f"[checker] Process timed out after {timeout}s")
    except KeyboardInterrupt:
        proc.kill()
        print()
        warn("Interrupted by user")

    elapsed = time.time() - t0

    # Render final completed progress bar
    final_time = total_duration if proc.returncode == 0 else current_time
    print(progress_bar(final_time, total_duration,
                       extra=f"done in {format_time(elapsed)}",
                       errors=err_count),
          flush=True)
    print()  # newline after progress bar

    return error_lines, elapsed


def ffprobe_json(filepath: str) -> dict:
    r = run([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", "-show_entries",
        "stream=index,codec_name,codec_type,codec_tag_string,width,height,"
        "pix_fmt,bit_rate,duration,nb_frames,r_frame_rate,avg_frame_rate,"
        "color_range,color_space,color_transfer,color_primaries,profile,level,"
        "bits_per_raw_sample",
        filepath
    ])
    if r.returncode != 0:
        fail(f"ffprobe failed: {r.stderr.strip()}")
        sys.exit(1)
    return json.loads(r.stdout)


def get_total_duration(probe: dict) -> float:
    fmt = probe.get("format", {})
    return float(fmt.get("duration", 0))


# ── Check 1: Container / Format sanity ───────────────────────────────────────
def check_container(filepath: str, probe: dict) -> CheckResult:
    header("1. Container & Format Check")
    result = CheckResult("container", True)

    fmt = probe.get("format", {})
    fmt_name = fmt.get("format_name", "")
    if "mp4" not in fmt_name and "mov" not in fmt_name:
        result.warnings.append(f"Unexpected format: {fmt_name}")

    actual = os.path.getsize(filepath)
    reported = int(fmt.get("size", actual))
    if actual != reported:
        result.errors.append(
            f"File size mismatch: actual={actual} vs container={reported}"
        )
        result.passed = False

    duration = float(fmt.get("duration", 0))
    if duration <= 0:
        result.errors.append("Duration is 0 or negative")
        result.passed = False

    streams = probe.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    if not video_streams:
        result.errors.append("No video stream found!")
        result.passed = False
    if not audio_streams:
        result.warnings.append("No audio stream found")

    for s in video_streams:
        w = s.get("width", 0)
        h = s.get("height", 0)
        info(f"Video: {s.get('codec_name')} ({s.get('codec_tag_string')}) "
             f"{w}x{h} @ {s.get('profile', '?')} "
             f"pix_fmt={s.get('pix_fmt', '?')}")

    for s in audio_streams:
        info(f"Audio: {s.get('codec_name')} "
             f"({s.get('profile', '?')}) "
             f"{s.get('bit_rate', '?')} bps")

    info(f"Duration: {duration:.1f}s ({format_time(duration)})  "
         f"Bitrate: {int(fmt.get('bit_rate', 0)) / 1e6:.1f} Mbps  "
         f"Size: {actual / (1024**3):.2f} GiB")

    for w in result.warnings:
        warn(w)
    for e in result.errors:
        fail(e)
    if result.passed:
        ok("Container structure looks good")
    return result


# ── Check 2: Dolby Vision / HDR metadata ────────────────────────────────────
def check_hdr(filepath: str, probe: dict) -> CheckResult:
    header("2. HDR / Dolby Vision Metadata Check")
    result = CheckResult("hdr", True)

    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        result.errors.append("No video stream")
        result.passed = False
        return result

    codec_tag = video.get("codec_tag_string", "")
    pix_fmt = video.get("pix_fmt", "")
    color_transfer = video.get("color_transfer", "unknown")
    color_primaries = video.get("color_primaries", "unknown")

    is_dv = codec_tag.lower().startswith("dvh") or codec_tag.lower().startswith("dva")
    if is_dv:
        info(f"Dolby Vision detected (codec tag: {codec_tag})")
    else:
        info(f"No Dolby Vision tag (codec tag: {codec_tag})")

    if "10" in pix_fmt or "10le" in pix_fmt or "10be" in pix_fmt:
        ok(f"10-bit pixel format: {pix_fmt}")
    elif "12" in pix_fmt:
        ok(f"12-bit pixel format: {pix_fmt}")
    else:
        warn(f"Pixel format is {pix_fmt} — expected 10-bit for HDR/DV content")
        result.warnings.append(f"Non-HDR pixel format: {pix_fmt}")

    if is_dv and codec_tag.lower() == "dvh1":
        info("DV Profile 5 (dvh1): single-layer, cross-compatible BL")
        r = run([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-show_entries",
            "stream_side_data=side_data_type",
            filepath
        ])
        if r.returncode == 0:
            try:
                sd = json.loads(r.stdout)
                for s in sd.get("streams", []):
                    for side in s.get("side_data_list", []):
                        if "DOVI" in side.get("side_data_type", "").upper():
                            info(f"  DOVI side data present: {side.get('side_data_type')}")
                            dv_profile = side.get("dv_profile", "?")
                            compat_id = side.get("dv_bl_signal_compatibility_id", "?")
                            info(f"  DV Profile: {dv_profile}, Compat ID: {compat_id}")
                            if str(compat_id) == "0":
                                warn("Compat ID = 0: IPTPQc2 BL — TVs without DV "
                                     "may show wrong colours / green/purple tint")
                                result.warnings.append(
                                    "DV compat_id=0 means the base layer uses "
                                    "IPTPQc2 colour matrix — non-DV players will "
                                    "display incorrect colours"
                                )
            except json.JSONDecodeError:
                pass

    info(f"Color transfer: {color_transfer}")
    info(f"Color primaries: {color_primaries}")

    if color_transfer == "unknown" and is_dv:
        warn("Color transfer is 'unknown' — this is normal for DV Profile 5 "
             "(IPTPQc2 matrix), the RPU carries the mapping")

    for w in result.warnings:
        warn(w)
    for e in result.errors:
        fail(e)
    if result.passed and not result.warnings:
        ok("HDR/DV metadata looks consistent")
    return result


# ── Check 3: Bitstream decode test ──────────────────────────────────────────
def check_decode(filepath: str, total_file_duration: float,
                 duration_seconds: int | None = None,
                 full: bool = False) -> CheckResult:
    if full:
        label = "full file"
        time_args = []
        check_duration = total_file_duration
    elif duration_seconds:
        label = f"first {format_time(duration_seconds)}"
        time_args = ["-t", str(duration_seconds)]
        check_duration = min(duration_seconds, total_file_duration)
    else:
        label = f"first {format_time(300)}"
        time_args = ["-t", "300"]
        check_duration = min(300, total_file_duration)

    header(f"3. Bitstream Decode Test ({label})")
    result = CheckResult("decode", True)

    info(f"Decoding video stream — {label} of {format_time(total_file_duration)} total")

    cmd = [
        "ffmpeg", "-v", "error", "-stats",
        *time_args,
        "-i", filepath,
        "-map", "0:v:0",
        "-f", "null", "-"
    ]

    error_lines, elapsed = run_ffmpeg_with_progress(
        cmd, check_duration, label="video decode"
    )

    if error_lines:
        result.passed = False
        fail(f"Found {len(error_lines)} decode error(s):")
        macroblock_errors = []
        slice_errors = []
        nal_errors = []
        ref_errors = []
        other_errors = []
        for e in error_lines:
            e_lower = e.lower()
            if "macroblock" in e_lower or "mb " in e_lower:
                macroblock_errors.append(e)
            elif "nal" in e_lower and ("invalid" in e_lower or "split" in e_lower or "size" in e_lower):
                nal_errors.append(e)
            elif "ref" in e_lower or "poc" in e_lower or "rps" in e_lower:
                ref_errors.append(e)
            elif "slice" in e_lower:
                slice_errors.append(e)
            else:
                other_errors.append(e)

        categories = [
            ("Macroblock errors", macroblock_errors),
            ("NAL unit errors", nal_errors),
            ("Reference/POC errors", ref_errors),
            ("Slice errors", slice_errors),
            ("Other errors", other_errors),
        ]
        for cat_name, cat_errors in categories:
            if cat_errors:
                fail(f"  {cat_name}: {len(cat_errors)}")
                for e in cat_errors[:5]:
                    print(f"    {C.RED}{e}{C.RST}")
                if len(cat_errors) > 5:
                    print(f"    {C.DIM}... and {len(cat_errors) - 5} more{C.RST}")

        result.errors = error_lines
    else:
        ok(f"No decode errors found ({format_time(elapsed)} elapsed)")

    return result


# ── Check 4: Packet / Frame analysis (PTS continuity, gaps, drops) ───────────
def check_frames(filepath: str, total_file_duration: float,
                 duration_seconds: int | None = None,
                 full: bool = False) -> CheckResult:
    if full:
        label = "full file"
        time_args = []
        check_duration = total_file_duration
    elif duration_seconds:
        label = f"first {format_time(duration_seconds)}"
        time_args = ["-read_intervals", f"%+{duration_seconds}"]
        check_duration = min(duration_seconds, total_file_duration)
    else:
        label = f"first {format_time(300)}"
        time_args = ["-read_intervals", "%+300"]
        check_duration = min(300, total_file_duration)

    header(f"4. Frame & Packet Analysis ({label})")
    result = CheckResult("frames", True)

    info(f"Reading packet timestamps & sizes — {label} of {format_time(total_file_duration)} total")

    # Read packet timestamps and sizes from the MP4 container
    # Format: pts_time,size,flags (e.g. 0.000000,4288,K__)
    cmd = [
        "ffprobe", "-v", "quiet", "-select_streams", "v:0",
        *time_args,
        "-show_entries", "packet=pts_time,size,flags",
        "-print_format", "csv=p=0",
        filepath
    ]

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1
    )

    pts_list: list[float] = []
    sizes: list[int] = []
    key_frames = 0
    frame_count = 0
    last_update = 0.0
    current_pts = 0.0
    t0 = time.time()

    estimated_frames = int(check_duration * 24)

    try:
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue

            parts = line.split(",")
            if len(parts) >= 2:
                try:
                    pts = float(parts[0])
                    size = int(parts[1])
                    is_key = ("K" in parts[2]) if len(parts) > 2 else False

                    pts_list.append(pts)
                    sizes.append(size)
                    current_pts = max(current_pts, pts)
                    frame_count += 1
                    if is_key:
                        key_frames += 1
                except ValueError:
                    continue

            # Update progress bar
            now = time.time()
            if now - last_update >= 0.15:
                extra = f"frame {frame_count}"
                if estimated_frames > 0:
                    extra += f"/{estimated_frames} (est)"
                print(progress_bar(current_pts, check_duration, extra=extra),
                      end="", flush=True)
                last_update = now

        proc.wait(timeout=600)
    except KeyboardInterrupt:
        proc.kill()
        print()
        warn("Interrupted by user")
    except subprocess.TimeoutExpired:
        proc.kill()
        result.warnings.append("Packet reading timed out")

    elapsed = time.time() - t0

    # Render final completed progress bar
    print(progress_bar(check_duration, check_duration,
                       extra=f"{frame_count} frames in {format_time(elapsed)}"),
          flush=True)
    print()

    if frame_count == 0:
        result.errors.append("No video packets found!")
        result.passed = False
        fail(result.errors[0])
        return result

    rate = frame_count / elapsed if elapsed > 0 else 0
    info(f"Analyzed {frame_count} frames in {format_time(elapsed)} ({rate:.0f} frames/sec)")
    info(f"Key frames (IDR): {key_frames}")

    # Check PTS continuity
    if len(pts_list) > 1:
        gaps = []
        for i in range(1, len(pts_list)):
            delta = pts_list[i] - pts_list[i - 1]
            if delta < -0.05:  # Backward jump
                gaps.append((i, pts_list[i - 1], pts_list[i], delta))
            elif delta > 1.0:  # Gap greater than 1s
                gaps.append((i, pts_list[i - 1], pts_list[i], delta))

        if gaps:
            warn(f"Found {len(gaps)} PTS discontinuities:")
            for idx, prev, curr, delta in gaps[:10]:
                warn(f"  Frame {idx}: {prev:.3f}s → {curr:.3f}s (Δ={delta:+.3f}s)")
            if len(gaps) > 10:
                warn(f"  ... and {len(gaps) - 10} more")
            result.warnings.append(f"{len(gaps)} PTS gaps/jumps found")
        else:
            ok("PTS is continuous — no unexpected gaps or jumps")

    # Check for packet size anomalies
    if sizes:
        avg_size = sum(sizes) / len(sizes)
        max_size = max(sizes)
        min_size = min(sizes)

        info(f"Frame sizes: avg={avg_size / 1024:.1f}KB, "
             f"min={min_size / 1024:.1f}KB, max={max_size / 1024:.1f}KB")

        zero_frames = [i for i, s in enumerate(sizes) if s == 0]
        if zero_frames:
            fail(f"Found {len(zero_frames)} zero-size frame(s) — container corruption")
            result.errors.append(f"{len(zero_frames)} zero-size frames")
            result.passed = False

        tiny_threshold = max(64, avg_size * 0.001)
        tiny_frames = [i for i in range(len(sizes))
                       if 0 < sizes[i] < tiny_threshold]
        if len(tiny_frames) > 5:
            warn(f"Found {len(tiny_frames)} unusually small frames (< {tiny_threshold:.0f} bytes)")
            result.warnings.append(f"{len(tiny_frames)} tiny frames detected")

        # Frozen frame detection (runs of identical packet sizes)
        repeat_count = 0
        max_repeat = 0
        for i in range(1, len(sizes)):
            if sizes[i] == sizes[i - 1]:
                repeat_count += 1
                max_repeat = max(max_repeat, repeat_count)
            else:
                repeat_count = 0
        if max_repeat > 24:
            warn(f"Longest run of identical-size frames: {max_repeat} (possible frozen frames)")
            result.warnings.append(f"Max identical-size run: {max_repeat} frames")

    if result.passed and not result.warnings:
        ok("Frame & packet analysis looks healthy")
    elif result.passed and result.warnings:
        warn("Frame & packet analysis passed with warnings")

    return result


# ── Check 5: Audio decode test ──────────────────────────────────────────────
def check_audio(filepath: str, total_file_duration: float) -> CheckResult:
    header("5. Audio Stream Decode Test")
    result = CheckResult("audio", True)

    cmd = [
        "ffmpeg", "-v", "error", "-stats",
        "-i", filepath,
        "-map", "0:a:0",
        "-f", "null", "-"
    ]

    info(f"Decoding audio stream — full {format_time(total_file_duration)}")

    error_lines, elapsed = run_ffmpeg_with_progress(
        cmd, total_file_duration, label="audio decode"
    )

    if error_lines:
        result.passed = False
        fail(f"Audio decode errors: {len(error_lines)}")
        for e in error_lines[:10]:
            print(f"    {C.RED}{e}{C.RST}")
        if len(error_lines) > 10:
            print(f"    {C.DIM}... and {len(error_lines) - 10} more{C.RST}")
        result.errors = error_lines
    else:
        ok(f"Audio decoded cleanly ({format_time(elapsed)} elapsed)")

    return result


# ── Summary ──────────────────────────────────────────────────────────────────
def print_summary(results: list[CheckResult]):
    header("SUMMARY")
    all_passed = True
    total_warnings = 0
    total_errors = 0

    for r in results:
        status = f"{C.GRN}PASS{C.RST}" if r.passed else f"{C.RED}FAIL{C.RST}"
        w_count = len(r.warnings)
        e_count = len(r.errors)
        total_warnings += w_count
        total_errors += e_count
        if not r.passed:
            all_passed = False

        extras = []
        if w_count:
            extras.append(f"{C.YEL}{w_count} warning(s){C.RST}")
        if e_count:
            extras.append(f"{C.RED}{e_count} error(s){C.RST}")
        extra_str = f" ({', '.join(extras)})" if extras else ""
        print(f"  [{status}] {r.name}{extra_str}")

    print()
    if all_passed and total_warnings == 0:
        print(f"  {C.GRN}{C.BLD}✓ All checks passed — file appears healthy{C.RST}")
    elif all_passed:
        print(f"  {C.YEL}{C.BLD}⚠ All checks passed but with {total_warnings} warning(s){C.RST}")
    else:
        print(f"  {C.RED}{C.BLD}✗ {total_errors} error(s) and "
              f"{total_warnings} warning(s) found{C.RST}")
    print()


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="MP4/Video File Integrity Checker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("file", help="Path to the MP4 file to check")
    parser.add_argument("--full", action="store_true",
                        help="Decode the entire file (slow for large files)")
    parser.add_argument("--first-minutes", type=int, default=5,
                        help="Minutes of video to decode-test (default: 5)")
    parser.add_argument("--skip-audio", action="store_true",
                        help="Skip audio decode test")
    parser.add_argument("--skip-frames", action="store_true",
                        help="Skip frame-level analysis")

    args = parser.parse_args()
    filepath = args.file

    if not os.path.isfile(filepath):
        fail(f"File not found: {filepath}")
        sys.exit(1)

    print(f"\n{C.BLD}MP4 Integrity Checker{C.RST}")
    print(f"File: {filepath}")
    print(f"Size: {os.path.getsize(filepath) / (1024**3):.2f} GiB")

    # Check for required tools
    for tool in ["ffprobe", "ffmpeg"]:
        r = subprocess.run(["which", tool], capture_output=True)
        if r.returncode != 0:
            fail(f"Required tool not found: {tool}")
            sys.exit(1)

    # Get probe data
    probe = ffprobe_json(filepath)
    total_duration = get_total_duration(probe)

    duration_seconds = args.first_minutes * 60 if not args.full else None
    results = []

    # Run checks
    results.append(check_container(filepath, probe))
    results.append(check_hdr(filepath, probe))
    results.append(check_decode(filepath, total_duration, duration_seconds, args.full))

    if not args.skip_frames:
        results.append(check_frames(filepath, total_duration, duration_seconds, args.full))

    if not args.skip_audio:
        results.append(check_audio(filepath, total_duration))

    print_summary(results)


if __name__ == "__main__":
    main()
