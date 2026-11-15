#!/usr/bin/env python3
"""Remove HDR10+ metadata from a Dolby Vision HEVC file and remux to MP4.

The video is streamed through dovi_tool, so no full-size temporary video file
is ever written.  Audio is copied to a small temporary MKA and muxed back with
the original start-time offset.  Finally the Dolby Vision configuration record
(dvvC/dvcC) is copied from the source into the output MP4, because FFmpeg does
not write it when the video arrives as a raw HEVC pipe.

Requirements: ffmpeg, ffprobe, dovi_tool (all on PATH).
"""

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from fractions import Fraction

VIDEO_ENTRY_TYPES = {b"hvc1", b"hev1", b"dvhe", b"dvh1", b"avc1"}
DOVI_BOX_TYPES = {b"dvvC", b"dvcC"}
CONTAINER_TYPES = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def log(msg):
  print(msg, flush=True)


def die(msg):
  print(f"Error: {msg}", file=sys.stderr, flush=True)
  sys.exit(1)


def which(tool):
  return shutil.which(tool)


def parse_rate(value):
  try:
    rate = Fraction(value)
    if rate > 0:
      return rate
  except (ValueError, ZeroDivisionError, TypeError):
    pass
  return None


def rate_arg(rate):
  """Return a value FFmpeg accepts for -r."""
  if rate.numerator > 1_000_000 or rate.denominator > 1_000_000:
    return f"{float(rate):.6f}"
  return f"{rate.numerator}/{rate.denominator}"


def run_json(cmd):
  result = subprocess.run(
      cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
  )
  if result.returncode != 0:
    die(f"{cmd[0]} failed: {result.stderr.strip()}")
  return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------
def probe(path):
  return run_json(
      [
          "ffprobe",
          "-v",
          "error",
          "-print_format",
          "json",
          "-show_format",
          "-show_streams",
          path,
      ]
  )


def get_video_packet_pts(path, progress=None):
  """Presentation timestamps of the video packets, in decode order.

  These are used later to rebuild the MP4's composition offsets: a raw HEVC
  pipe carries no timestamps, so the muxed file would otherwise present frames
  in decode order (wrong for B-frames).

  ``progress`` is an optional callback invoked with the number of packets read
  so far (and ``done=True`` at the end), so callers can display activity while
  a large file is scanned.
  """
  proc = subprocess.Popen(
      [
          "ffprobe",
          "-v",
          "error",
          "-select_streams",
          "v:0",
          "-show_entries",
          "packet=pts_time",
          "-of",
          "csv=p=0",
          path,
      ],
      stdout=subprocess.PIPE,
      stderr=subprocess.DEVNULL,
      text=True,
  )

  timestamps = []
  try:
    for line in proc.stdout:
      value = line.strip().strip(",")
      if not value or value == "N/A":
        timestamps.append(None)
      else:
        try:
          timestamps.append(float(value))
        except ValueError:
          timestamps.append(None)
      if progress is not None and len(timestamps) % 256 == 0:
        progress(len(timestamps))
  except BaseException:
    proc.kill()
    raise
  finally:
    returncode = proc.wait()

  if progress is not None:
    progress(len(timestamps), done=True)

  if returncode != 0:
    return []
  return timestamps


def get_pre_roll(timestamps, rate):
  """Seconds of video packets hidden by the container's edit list."""
  values = [value for value in timestamps if value is not None]
  if not values:
    return 0.0

  minimum = min(values)
  if minimum >= -0.000001:
    return 0.0
  frames = round(-minimum * float(rate))
  return frames / float(rate)


def probe_source(path, progress=None):
  data = probe(path)
  streams = data.get("streams", [])

  video = None
  for stream in streams:
    if stream.get("codec_type") != "video":
      continue
    if stream.get("disposition", {}).get("attached_pic"):
      continue
    video = stream
    break

  if video is None:
    die("No video stream found in the input file.")
  if video.get("codec_name") not in ("hevc", "h265"):
    die(
        f"Input video is {video.get('codec_name')!r}; this tool only handles "
        "HEVC/H.265."
    )

  audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
  camera_audio = None
  for stream in streams:
    if stream.get("codec_type") == "audio":
      camera_audio = stream
      break

  duration = float(
      video.get("duration") or data.get("format", {}).get("duration") or 0
  )
  nb_frames = int(video.get("nb_frames") or 0)

  r_rate = parse_rate(video.get("r_frame_rate"))
  avg_rate = parse_rate(video.get("avg_frame_rate"))
  rate = r_rate or avg_rate
  if rate is None:
    die("Could not determine the source frame rate.")

  if r_rate and avg_rate:
    difference = abs(float(r_rate - avg_rate)) / float(r_rate)
    if difference > 0.005:
      print(
          f"Warning: source appears to be variable frame rate "
          f"(r={float(r_rate):.6f}, avg={float(avg_rate):.6f}).  "
          "Using the average rate; check A/V sync on the result.",
          file=sys.stderr,
      )
      rate = avg_rate

  if not (1 <= float(rate) <= 120):
    die(f"Implausible source frame rate: {float(rate):.6f}")

  dovi = None
  for side_data in video.get("side_data_list", []):
    if "DOVI" in side_data.get("side_data_type", "").upper():
      dovi = side_data
      break

  packet_progress = None
  if progress is not None:
    estimated_packets = int(duration * float(rate)) if duration > 0 else 0

    def packet_progress(count, done=False):
      progress(count, estimated_packets, done)

  packet_pts = get_video_packet_pts(path, progress=packet_progress)
  pre_roll = get_pre_roll(packet_pts, rate)
  known_pts = [value for value in packet_pts if value is not None]
  if known_pts:
    # Extent of the raw stream as it will be piped: the first packet is the
    # earliest pre-roll packet, the last one ends one frame after its PTS.
    video_media_end = (
        max(known_pts) - min(0.0, min(known_pts)) + 1.0 / float(rate)
    )
  else:
    video_media_end = duration + pre_roll
  if not nb_frames and known_pts:
    nb_frames = len(known_pts)

  return {
      "data": data,
      "video": video,
      "video_index": video.get("index"),
      "video_start": float(video.get("start_time") or 0.0),
      "video_duration": duration,
      "video_media_end": video_media_end,
      "nb_frames": nb_frames,
      "rate": rate,
      "pre_roll": pre_roll,
      "packet_pts": packet_pts,
      "audio_streams": audio_streams,
      "audio": camera_audio,
      "audio_start": float((camera_audio or {}).get("start_time") or 0.0),
      "audio_duration": float((camera_audio or {}).get("duration") or 0.0),
      "dovi": dovi,
      "format_name": data.get("format", {}).get("format_name", ""),
  }


# ---------------------------------------------------------------------------
# MP4 box helpers
# ---------------------------------------------------------------------------
def iter_boxes(buffer, start, end):
  """Yield (offset, type, size, header_size) for boxes in buffer[start:end]."""
  pos = start
  while pos + 8 <= end:
    size, box_type = struct.unpack(">I4s", buffer[pos:pos + 8])
    header = 8
    if size == 1:
      if pos + 16 > end:
        break
      size = struct.unpack(">Q", buffer[pos + 8:pos + 16])[0]
      header = 16
    elif size == 0:
      size = end - pos
    if size < header or pos + size > end:
      break
    yield pos, box_type, size, header
    pos += size


def top_level_boxes(handle, file_size):
  pos = 0
  while pos + 8 <= file_size:
    handle.seek(pos)
    header = handle.read(8)
    if len(header) < 8:
      break
    size, box_type = struct.unpack(">I4s", header)
    header_size = 8
    if size == 1:
      extra = handle.read(8)
      if len(extra) < 8:
        break
      size = struct.unpack(">Q", extra)[0]
      header_size = 16
    elif size == 0:
      size = file_size - pos
    if size < header_size or pos + size > file_size:
      break
    yield pos, box_type, size, header_size
    pos += size


def _replace_moov(handle, moov_offset, moov_size, new_moov, file_size):
  """Write a rebuilt moov without disturbing the media data.

  ffmpeg writes 'moov' after 'mdat' by default, so the moov is normally the
  last box and can be grown or shrunk by rewriting it at the same offset and
  truncating the file.  If the moov is followed by mdat, only shrinking is
  safe; any leftover space becomes a 'free' box so chunk offsets stay valid.
  """
  if not new_moov:
    return False
  if moov_offset + moov_size >= file_size:
    handle.seek(moov_offset)
    handle.write(new_moov)
    handle.truncate(moov_offset + len(new_moov))
    return True
  if len(new_moov) > moov_size:
    return False
  handle.seek(moov_offset)
  handle.write(new_moov)
  remaining = moov_size - len(new_moov)
  if remaining:
    if remaining < 8:
      return False
    handle.seek(moov_offset + len(new_moov))
    handle.write(struct.pack(">I4s", remaining, b"free"))
    handle.write(b"\x00" * (remaining - 8))
  return True


def _find_dovi_box_in_moov(moov):
  def walk(start, end):
    for pos, box_type, size, header in iter_boxes(moov, start, end):
      if box_type == b"stsd":
        # stsd: 4 bytes version/flags + 4 bytes entry_count, then entries
        entry_pos = pos + header + 8
        while entry_pos + 8 <= pos + size:
          entry_size, entry_type = struct.unpack(
              ">I4s", moov[entry_pos:entry_pos + 8]
          )
          if entry_size < 8:
            break
          if entry_type in VIDEO_ENTRY_TYPES:
            # Visual sample entry: 8 byte header + 78 bytes fixed fields
            child_start = entry_pos + 8 + 78
            child_end = entry_pos + entry_size
            for cpos, ctype, csize, _ in iter_boxes(
                moov, child_start, child_end
            ):
              if ctype in DOVI_BOX_TYPES:
                return bytes(moov[cpos:cpos + csize])
          entry_pos += entry_size
      elif box_type in CONTAINER_TYPES:
        found = walk(pos + header, pos + size)
        if found:
          return found
    return None

  return walk(0, len(moov))


def build_dovi_box(dovi):
  """Build a dvvC/dvcC box from ffprobe DOVI side-data fields."""
  major = int(dovi.get("dv_version_major", 1)) & 0xFF
  minor = int(dovi.get("dv_version_minor", 0)) & 0xFF
  profile = int(dovi.get("dv_profile", 8)) & 0x7F
  level = int(dovi.get("dv_level", 6)) & 0x3F
  rpu = int(dovi.get("rpu_present_flag", 1)) & 1
  enhancement = int(dovi.get("el_present_flag", 0)) & 1
  base = int(dovi.get("bl_present_flag", 1)) & 1
  compat = int(dovi.get("dv_bl_signal_compatibility_id", 1)) & 0xF

  flags = (profile << 9) | (level << 3) | (rpu << 2) | (
      enhancement << 1
  ) | base
  payload = (
      bytes((major, minor))
      + struct.pack(">H", flags)
      + bytes(((compat << 4),))
      + b"\x00" * 19
  )
  box_type = b"dvcC" if profile == 5 else b"dvvC"
  return struct.pack(">I4s", len(payload) + 8, box_type) + payload


def extract_dovi_box(path, dovi):
  """Get the source's Dolby Vision configuration box, if it has one."""
  try:
    file_size = os.path.getsize(path)
    with open(path, "rb") as handle:
      for pos, box_type, size, header in top_level_boxes(handle, file_size):
        if box_type == b"moov":
          handle.seek(pos)
          moov = handle.read(size)
          found = _find_dovi_box_in_moov(moov)
          if found:
            return found
          break
  except OSError:
    pass

  if dovi:
    return build_dovi_box(dovi)
  return None


def _locate_video_entry(moov):
  """Find where to insert dvvC and which ancestor sizes must grow."""
  def walk(start, end, ancestors):
    for pos, box_type, size, header in iter_boxes(moov, start, end):
      if box_type == b"stsd":
        entry_pos = pos + header + 8
        while entry_pos + 8 <= pos + size:
          entry_size, entry_type = struct.unpack(
              ">I4s", moov[entry_pos:entry_pos + 8]
          )
          if entry_size < 8:
            break
          if entry_type in VIDEO_ENTRY_TYPES:
            child_start = entry_pos + 8 + 78
            child_end = entry_pos + entry_size
            insert_at = child_end
            found_hvcc = False
            for cpos, ctype, csize, _ in iter_boxes(
                moov, child_start, child_end
            ):
              if ctype in DOVI_BOX_TYPES:
                return None  # already has a DV config
              if ctype == b"hvcC":
                found_hvcc = True
                insert_at = cpos + csize
            if not found_hvcc:
              return None
            # ancestors already starts with the moov offset.
            return insert_at, ancestors + [pos, entry_pos]
          entry_pos += entry_size
      elif box_type in CONTAINER_TYPES:
        found = walk(pos + header, pos + size, ancestors + [pos])
        if found:
          return found
    return None

  return walk(0, len(moov), [])


def inject_dovi_box(path, box):
  """Insert dvvC/dvcC into the video sample entry of an MP4.

  The moov is rebuilt and written back in place (normally the last box, so it
  may grow freely).  Media data and chunk offsets are never touched.
  """
  if not box:
    return False

  file_size = os.path.getsize(path)
  with open(path, "r+b") as handle:
    moov = next(
        (b for b in top_level_boxes(handle, file_size) if b[1] == b"moov"),
        None,
    )
    if moov is None:
      return False

    moov_offset, _, moov_size, _ = moov

    handle.seek(moov_offset)
    moov_data = bytearray(handle.read(moov_size))
    located = _locate_video_entry(moov_data)
    if not located:
      return False
    insert_at, size_field_offsets = located

    new_moov = moov_data[:insert_at] + box + moov_data[insert_at:]

    # Grow every ancestor box by len(box).
    for offset in size_field_offsets:
      size32 = struct.unpack(">I", new_moov[offset:offset + 4])[0]
      if size32 == 1:
        size64 = struct.unpack(">Q", new_moov[offset + 8:offset + 16])[0]
        struct.pack_into(">Q", new_moov, offset + 8, size64 + len(box))
      else:
        struct.pack_into(">I", new_moov, offset, size32 + len(box))

    if not _replace_moov(
        handle, moov_offset, moov_size, new_moov, file_size
    ):
      return False

    handle.flush()
    os.fsync(handle.fileno())

  return True


def _make_box(box_type, payload):
  return struct.pack(">I4s", len(payload) + 8, box_type) + payload


def _make_stts(sample_count, delta):
  payload = (
      struct.pack(">I", 0)
      + struct.pack(">I", 1)
      + struct.pack(">II", sample_count, delta)
  )
  return _make_box(b"stts", payload)


def _make_ctts(offsets):
  runs = []
  for offset in offsets:
    if runs and runs[-1][1] == offset:
      runs[-1][0] += 1
    else:
      runs.append([1, offset])

  version = 0
  if any(offset < 0 for _, offset in runs):
    version = 1

  payload = struct.pack(">I", version) + struct.pack(">I", len(runs))
  for count, offset in runs:
    if version == 1:
      payload += struct.pack(">Ii", count, offset)
    else:
      payload += struct.pack(">II", count, offset)
  return _make_box(b"ctts", payload)


def _scan_video_trak(moov, trak_pos, trak_size, trak_header):
  found = {
      "trak": trak_pos,
      "elst": None,
      "stbl": None,
      "stbl_size": 0,
      "stbl_header": 8,
      "size_offsets": [trak_pos],
      "stsd_types": [],
      "sample_count": 0,
  }

  def walk(start, end, ancestors):
    for pos, box_type, size, header in iter_boxes(moov, start, end):
      if box_type == b"stsd":
        entry_pos = pos + header + 8
        while entry_pos + 8 <= pos + size:
          entry_size, entry_type = struct.unpack(
              ">I4s", moov[entry_pos:entry_pos + 8]
          )
          if entry_size < 8:
            break
          found["stsd_types"].append(entry_type)
          entry_pos += entry_size
      elif box_type == b"elst":
        found["elst"] = pos
      elif box_type == b"stsz":
        if pos + header + 12 <= pos + size:
          found["sample_count"] = struct.unpack(
              ">I", moov[pos + header + 8:pos + header + 12]
          )[0]
      elif box_type == b"stbl":
        found["stbl"] = pos
        found["stbl_size"] = size
        found["stbl_header"] = header
        found["size_offsets"] = ancestors + [pos]
        walk(pos + header, pos + size, ancestors + [pos])
      elif box_type in (b"mdia", b"minf", b"edts"):
        walk(pos + header, pos + size, ancestors + [pos])

  walk(trak_pos + trak_header, trak_pos + trak_size, [trak_pos])
  if not any(t in VIDEO_ENTRY_TYPES for t in found["stsd_types"]):
    return None
  if found["stbl"] is None:
    return None
  return found


def _find_video_track(moov, moov_end, moov_header=8):
  for pos, box_type, size, header in iter_boxes(
      moov, moov_header, moov_end
  ):
    if box_type != b"trak":
      continue
    result = _scan_video_trak(moov, pos, size, header)
    if result is not None:
      return result
  return None


def _movie_timescale(moov, moov_header):
  for pos, box_type, size, header in iter_boxes(
      moov, moov_header, len(moov)
  ):
    if box_type == b"mvhd":
      version = moov[pos + header]
      if version == 1:
        return struct.unpack(
            ">I", moov[pos + header + 20:pos + header + 24]
        )[0]
      return struct.unpack(
          ">I", moov[pos + header + 12:pos + header + 16]
      )[0]
  return 0


def repair_video_timing(path, info):
  """Rewrite the MP4 sample timing using the source's presentation order.

  The cleaned HEVC is piped as raw NAL units, so the MP4 muxer has no B-frame
  reorder information and writes no ctts box.  The frame data is still in the
  original decode order, so copying the source packet presentation timestamps
  into a fresh stts/ctts pair restores correct playback without touching mdat.
  """
  timestamps = info.get("packet_pts") or []
  if not timestamps:
    return False

  rate = info["rate"]
  factor = max(1, -(-1000 // rate.numerator))  # ceil(1000 / numerator)
  timescale = rate.numerator * factor
  frame_duration = rate.denominator * factor

  file_size = os.path.getsize(path)
  with open(path, "r+b") as handle:
    boxes = list(top_level_boxes(handle, file_size))
    moov = next((b for b in boxes if b[1] == b"moov"), None)
    mdat = next((b for b in boxes if b[1] == b"mdat"), None)
    if moov is None or mdat is None:
      return False

    moov_offset, _, moov_size, moov_header = moov

    handle.seek(moov_offset)
    moov_data = bytearray(handle.read(moov_size))
    layout = _find_video_track(moov_data, len(moov_data), moov_header)
    if not layout:
      return False

    sample_count = layout["sample_count"]
    if sample_count <= 0 or sample_count > len(timestamps):
      return False

    offsets = []
    missing = False
    for index in range(sample_count):
      value = timestamps[index]
      if value is None:
        missing = True
        value = index * frame_duration / timescale
      presentation = round(value * timescale)
      offsets.append(presentation - index * frame_duration)

    if missing:
      print(
          "Warning: some source packets had no timestamp; frame order may be "
          "approximate.",
          file=sys.stderr,
      )

    # Shift composition offsets so they are non-negative (better player
    # compatibility).  The edit list below compensates for the shift.
    shift = max(0, -min(offsets))
    offsets = [offset + shift for offset in offsets]
    media_time = shift + round(info["video_start"] * timescale)

    stts = _make_stts(sample_count, frame_duration)
    ctts = _make_ctts(offsets)

    stbl_offset = layout["stbl"]
    stbl_size = layout["stbl_size"]
    stbl_header = layout["stbl_header"]
    children_start = stbl_offset + stbl_header
    children_end = stbl_offset + stbl_size

    children = bytearray()
    replaced = False
    for pos, box_type, size, _ in iter_boxes(
        moov_data, children_start, children_end
    ):
      if box_type == b"stts":
        children += stts + ctts
        replaced = True
      elif box_type == b"ctts":
        continue
      else:
        children += moov_data[pos:pos + size]
    if not replaced:
      return False

    if stbl_header == 16:
      new_stbl = struct.pack(">I4sQ", 1, b"stbl", len(children) + 16)
    else:
      new_stbl = _make_box(b"stbl", bytes(children))
    delta = len(new_stbl) - stbl_size

    new_moov = (
        moov_data[:stbl_offset] + new_stbl + moov_data[stbl_offset + stbl_size:]
    )

    # The rebuilt stbl already carries its new size; grow only its ancestors.
    for offset in [0] + layout["size_offsets"][:-1]:
      size32 = struct.unpack(">I", new_moov[offset:offset + 4])[0]
      if size32 == 1:
        size64 = struct.unpack(">Q", new_moov[offset + 8:offset + 16])[0]
        struct.pack_into(">Q", new_moov, offset + 8, size64 + delta)
      else:
        struct.pack_into(">I", new_moov, offset, size32 + delta)

    elst = layout["elst"]
    if elst is not None:
      movie_timescale = _movie_timescale(new_moov, moov_header)
      segment_duration = 0
      if movie_timescale and info["video_duration"]:
        segment_duration = round(
            info["video_duration"] * movie_timescale
        )
      version = new_moov[elst + 8]
      if version == 1:
        struct.pack_into(">q", new_moov, elst + 24, media_time)
        if segment_duration:
          struct.pack_into(">Q", new_moov, elst + 16, segment_duration)
      else:
        struct.pack_into(">i", new_moov, elst + 20, media_time)
        if segment_duration:
          struct.pack_into(">I", new_moov, elst + 16, segment_duration)
    elif media_time:
      return False

    if not _replace_moov(
        handle, moov_offset, moov_size, new_moov, file_size
    ):
      return False

    handle.flush()
    os.fsync(handle.fileno())

  return True


# ---------------------------------------------------------------------------
# Container layout
# ---------------------------------------------------------------------------
_LAYOUT_CONTAINERS = {
    b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"udta",
    b"mvex",
}
_LAYOUT_ORDER = [
    b"stsd", b"stts", b"stss", b"ctts", b"stsc", b"stsz", b"co64", b"stco",
]


def _layout_box(box_type, payload):
  return struct.pack(">I4s", len(payload) + 8, box_type) + payload


def _layout_child(moov, start, end, box_type):
  for pos, kind, size, header in iter_boxes(moov, start, end):
    if kind == box_type:
      return pos, size, header
  return None


def _layout_handler(moov, start, end):
  for pos, kind, size, header in iter_boxes(moov, start, end):
    if kind == b"hdlr":
      return moov[pos + header + 8:pos + header + 12]
    if kind in _LAYOUT_CONTAINERS:
      found = _layout_handler(moov, pos + header, pos + size)
      if found:
        return found
  return None


def _layout_expand_chunks(stsc_entries, chunk_offsets, sizes):
  """Return every sample's absolute file offset, in sample order."""
  offsets = []
  index = 0
  chunk_count = len(chunk_offsets)
  for i, (first, per, _desc) in enumerate(stsc_entries):
    last = (
        stsc_entries[i + 1][0] - 1 if i + 1 < len(stsc_entries)
        else chunk_count
    )
    for chunk in range(first, last + 1):
      cursor = chunk_offsets[chunk - 1]
      for _ in range(per):
        if index >= len(sizes):
          break
        offsets.append(cursor)
        cursor += sizes[index]
        index += 1
  if index != len(sizes) or len(offsets) != len(sizes):
    return None
  return offsets


def normalise_container(path):
  """Rewrite the MP4 index into the layout ffmpeg writes for timed input.

  The HDR10+ pipeline muxes raw HEVC, so ffmpeg has no presentation
  timestamps and writes a decode-order index with multi-sample chunks; the
  timing repair then patches stts/ctts.  Some players (LG webOS among them)
  refuse that layout even though the tables are self-consistent.

  This rebuilds only the moov, in place:

    * one sample per chunk (single stsc entry, one offset per sample)
    * ffmpeg child order (stsd, stts, stss, ctts, stsc, stsz, co64)
    * tkhd, elst and mvhd durations consistent with mdhd

  Media data is never moved and no temporary file is used.
  """
  file_size = os.path.getsize(path)
  with open(path, "r+b") as handle:
    boxes = list(top_level_boxes(handle, file_size))
    moov_box = next((b for b in boxes if b[1] == b"moov"), None)
    if moov_box is None:
      return False
    moov_offset, _, moov_size, moov_header = moov_box

    handle.seek(moov_offset)
    moov = handle.read(moov_size)
    if len(moov) != moov_size:
      return False

    replacements = {}
    media = []

    for trak_pos, _, trak_size, trak_header in [
        b for b in iter_boxes(moov, moov_header, len(moov))
        if b[1] == b"trak"
    ]:
      handler = _layout_handler(
          moov, trak_pos + trak_header, trak_pos + trak_size
      )
      media_ts = 0
      media_dur = 0
      new_stbl = None

      mdia = _layout_child(
          moov, trak_pos + trak_header, trak_pos + trak_size, b"mdia"
      )
      if mdia is None:
        media.append((media_ts, media_dur))
        continue
      mdia_pos, mdia_size, mdia_header = mdia

      mdhd = _layout_child(
          moov, mdia_pos + mdia_header, mdia_pos + mdia_size, b"mdhd"
      )
      if mdhd is None:
        media.append((media_ts, media_dur))
        continue
      mdhd_pos, _, mdhd_header = mdhd
      if moov[mdhd_pos + mdhd_header] == 0:
        media_ts = struct.unpack(
            ">I", moov[mdhd_pos + mdhd_header + 12:mdhd_pos + mdhd_header + 16]
        )[0]
        media_dur = struct.unpack(
            ">I", moov[mdhd_pos + mdhd_header + 16:mdhd_pos + mdhd_header + 20]
        )[0]
      else:
        media_ts = struct.unpack(
            ">I", moov[mdhd_pos + mdhd_header + 20:mdhd_pos + mdhd_header + 24]
        )[0]
        media_dur = struct.unpack(
            ">Q", moov[mdhd_pos + mdhd_header + 24:mdhd_pos + mdhd_header + 32]
        )[0]

      minf = _layout_child(
          moov, mdia_pos + mdia_header, mdia_pos + mdia_size, b"minf"
      )
      if minf is None:
        media.append((media_ts, media_dur))
        continue
      minf_pos, minf_size, minf_header = minf

      stbl = _layout_child(
          moov, minf_pos + minf_header, minf_pos + minf_size, b"stbl"
      )
      if stbl is None:
        media.append((media_ts, media_dur))
        continue
      stbl_pos, stbl_size, stbl_header = stbl

      children = list(iter_boxes(
          moov, stbl_pos + stbl_header, stbl_pos + stbl_size
      ))
      by_type = {kind: (pos, size, header)
                 for pos, kind, size, header in children}

      chunk_kind = b"co64" if b"co64" in by_type else b"stco"
      chunk_box = by_type.get(chunk_kind)
      if b"stsz" not in by_type or b"stsc" not in by_type or chunk_box is None:
        media.append((media_ts, media_dur))
        continue

      stsz_pos, stsz_size, stsz_header = by_type[b"stsz"]
      body = moov[stsz_pos + stsz_header:stsz_pos + stsz_size]
      constant = struct.unpack(">I", body[4:8])[0]
      count = struct.unpack(">I", body[8:12])[0]
      if constant:
        sizes = [constant] * count
      else:
        sizes = list(struct.unpack(">%dI" % count, body[12:12 + 4 * count]))

      stsc_pos, stsc_size, stsc_header = by_type[b"stsc"]
      body = moov[stsc_pos + stsc_header:stsc_pos + stsc_size]
      entries = struct.unpack(">I", body[4:8])[0]
      stsc_entries = [
          struct.unpack(">III", body[8 + 12 * i:20 + 12 * i])
          for i in range(entries)
      ]

      chunk_pos, chunk_size, chunk_header = chunk_box
      body = moov[chunk_pos + chunk_header:chunk_pos + chunk_size]
      chunk_count = struct.unpack(">I", body[4:8])[0]
      if chunk_kind == b"stco":
        chunk_offsets = list(struct.unpack(
            ">%dI" % chunk_count, body[8:8 + 4 * chunk_count]
        ))
      else:
        chunk_offsets = list(struct.unpack(
            ">%dQ" % chunk_count, body[8:8 + 8 * chunk_count]
        ))

      offsets = _layout_expand_chunks(stsc_entries, chunk_offsets, sizes)
      if offsets is None:
        print(
            f"  Warning: {handler!r} chunk table is inconsistent; "
            "index left unchanged.",
            file=sys.stderr,
        )
        media.append((media_ts, media_dur))
        continue

      use64 = chunk_kind == b"co64" or any(
          value > 0xFFFFFFFF for value in offsets
      )
      if use64:
        table = _layout_box(
            b"co64",
            struct.pack(">I", 0) + struct.pack(">I", len(offsets))
            + struct.pack(">%dQ" % len(offsets), *offsets),
        )
      else:
        table = _layout_box(
            b"stco",
            struct.pack(">I", 0) + struct.pack(">I", len(offsets))
            + struct.pack(">%dI" % len(offsets), *offsets),
        )
      new_stsc = _layout_box(
          b"stsc",
          struct.pack(">I", 0) + struct.pack(">I", 1)
          + struct.pack(">III", 1, 1, 1),
      )

      ordered = []
      for kind in _LAYOUT_ORDER:
        if kind == b"stsc":
          ordered.append(new_stsc)
        elif kind in (b"stco", b"co64"):
          if kind == chunk_kind and table is not None:
            ordered.append(table)
            table = None
        elif kind in by_type:
          pos, box_size, _ = by_type[kind]
          ordered.append(moov[pos:pos + box_size])
      for pos, kind, box_size, _ in children:
        if kind not in _LAYOUT_ORDER:
          ordered.append(moov[pos:pos + box_size])

      new_stbl = _layout_box(b"stbl", b"".join(ordered))
      replacements[stbl_pos] = new_stbl
      media.append((media_ts, media_dur))

    if not replacements:
      return False

    def rebuild(start, end):
      out = []
      for pos, kind, box_size, header in iter_boxes(moov, start, end):
        if kind == b"stbl" and pos in replacements:
          out.append(replacements[pos])
        elif kind in _LAYOUT_CONTAINERS:
          out.append(_layout_box(kind, rebuild(pos + header, pos + box_size)))
        else:
          out.append(moov[pos:pos + box_size])
      return b"".join(out)

    new_moov = bytearray(_layout_box(b"moov", rebuild(moov_header, len(moov))))

    movie_ts = 0
    for pos, kind, _, header in iter_boxes(new_moov, 8, len(new_moov)):
      if kind == b"mvhd":
        offset = 20 if new_moov[pos + header] == 1 else 12
        movie_ts = struct.unpack(
            ">I", new_moov[pos + header + offset:pos + header + offset + 4]
        )[0]
        break
    if not movie_ts:
      return False

    traks = [b for b in iter_boxes(new_moov, 8, len(new_moov)) if b[1] == b"trak"]
    longest = 0
    for index, (trak_pos, _, trak_size, trak_header) in enumerate(traks):
      media_ts, media_dur = media[index] if index < len(media) else (0, 0)
      if not media_ts or not media_dur:
        continue
      scaled = round(media_dur * movie_ts / media_ts)
      longest = max(longest, scaled)

      tkhd = _layout_child(
          new_moov, trak_pos + trak_header, trak_pos + trak_size, b"tkhd"
      )
      if tkhd is not None:
        tpos, _, theader = tkhd
        if new_moov[tpos + theader] == 0:
          struct.pack_into(">I", new_moov, tpos + theader + 20, scaled)
        else:
          struct.pack_into(">Q", new_moov, tpos + theader + 28, scaled)

      edts = _layout_child(
          new_moov, trak_pos + trak_header, trak_pos + trak_size, b"edts"
      )
      if edts is not None:
        epos, esize, eheader = edts
        elst = _layout_child(new_moov, epos + eheader, epos + esize, b"elst")
        if elst is not None:
          lpos, lsize, lheader = elst
          body = new_moov[lpos + lheader:lpos + lsize]
          version = body[0]
          count = struct.unpack(">I", body[4:8])[0]
          offset = 8
          for _ in range(count):
            if version == 0:
              struct.pack_into(">I", new_moov, lpos + lheader + offset, scaled)
              offset += 12
            else:
              struct.pack_into(">Q", new_moov, lpos + lheader + offset, scaled)
              offset += 20

    for pos, kind, _, header in iter_boxes(new_moov, 8, len(new_moov)):
      if kind == b"mvhd":
        if new_moov[pos + header] == 0:
          struct.pack_into(">I", new_moov, pos + header + 16, longest)
        else:
          struct.pack_into(">Q", new_moov, pos + header + 24, longest)
        break

    if not _replace_moov(handle, moov_offset, moov_size, new_moov, file_size):
      return False

    handle.flush()
    os.fsync(handle.fileno())

  print(
      f"  Index normalised: moov {moov_size} -> {len(new_moov)} bytes, "
      "one sample per chunk."
  )
  return True


def relocate_moov(path):
  """Move a trailing moov box to the front, in place (faststart)."""
  file_size = os.path.getsize(path)
  with open(path, "r+b") as handle:
    boxes = list(top_level_boxes(handle, file_size))
    kinds = [box[1] for box in boxes]
    if b"moov" not in kinds or b"mdat" not in kinds:
      return False
    if kinds.index(b"moov") < kinds.index(b"mdat"):
      return True
    if kinds[-1] != b"moov" or kinds[0] != b"ftyp":
      return False

    moov = next(box for box in boxes if box[1] == b"moov")
    mdat = next(box for box in boxes if box[1] == b"mdat")
    ftyp_end = boxes[0][2]
    moov_offset, _, moov_size, _ = moov
    mdat_offset = mdat[0]

    for pos, kind, _, _ in boxes:
      if pos < mdat_offset and kind not in (b"ftyp", b"free", b"skip"):
        return False

    pre_size = mdat_offset - ftyp_end
    new_mdat_offset = ftyp_end + moov_size + 8
    delta = new_mdat_offset - mdat_offset
    new_end = file_size + 8 - pre_size
    if delta < 0:
      return False

    handle.seek(moov_offset)
    moov_data = bytearray(handle.read(moov_size))
    if len(moov_data) != moov_size:
      return False

    def shift(start, end, amount):
      for pos, kind, size, header in iter_boxes(moov_data, start, end):
        if kind == b"stco":
          count = struct.unpack(
              ">I", moov_data[pos + header + 4:pos + header + 8]
          )[0]
          offset = pos + header + 8
          for _ in range(count):
            value = struct.unpack(">I", moov_data[offset:offset + 4])[0]
            if value + amount > 0xFFFFFFFF:
              raise ValueError("chunk offset overflow; co64 required")
            struct.pack_into(">I", moov_data, offset, value + amount)
            offset += 4
        elif kind == b"co64":
          count = struct.unpack(
              ">I", moov_data[pos + header + 4:pos + header + 8]
          )[0]
          offset = pos + header + 8
          for _ in range(count):
            value = struct.unpack(">Q", moov_data[offset:offset + 8])[0]
            struct.pack_into(">Q", moov_data, offset, value + amount)
            offset += 8
        elif kind in _LAYOUT_CONTAINERS:
          shift(pos + header, pos + size, amount)

    shift(0, len(moov_data), delta)

    chunk = 8 * 1024 * 1024
    os.ftruncate(handle.fileno(), file_size + delta)
    pos = moov_offset
    while pos > mdat_offset:
      step = min(chunk, pos - mdat_offset)
      pos -= step
      handle.seek(pos)
      data = handle.read(step)
      if len(data) != step:
        return False
      handle.seek(pos + delta)
      handle.write(data)

    handle.seek(ftyp_end)
    handle.write(moov_data)
    handle.seek(ftyp_end + moov_size)
    handle.write(struct.pack(">I4s", 8, b"free"))
    os.ftruncate(handle.fileno(), new_end)
    handle.flush()
    os.fsync(handle.fileno())

  print(f"  moov moved to the front (media shifted {delta} bytes).")
  return True


def check_space(input_path, output_path):
  input_size = os.path.getsize(input_path)
  free = shutil.disk_usage(os.path.dirname(input_path)).free
  reclaimable = os.path.getsize(output_path) if os.path.exists(output_path) else 0
  # Output roughly matches input; allow a little overhead for audio/moov.
  needed = int(input_size * 1.01) + 32 * 1024 * 1024
  available = free + reclaimable
  if available < needed:
    die(
        f"Not enough free space in {os.path.dirname(input_path)!r}: "
        f"{available / 1024**3:.2f} GiB available, "
        f"about {needed / 1024**3:.2f} GiB needed.\n"
        "The original is kept until the output has been written, so both "
        "files must fit at the same time."
    )


def extract_audio(source, audio_index, destination, log_path, duration=None):
  cmd = [
      "ffmpeg",
      "-nostdin",
      "-hide_banner",
      "-loglevel",
      "error",
      "-i",
      source,
      "-map",
      f"0:{audio_index}",
      "-vn",
      "-sn",
      "-dn",
      "-c",
      "copy",
  ]
  if duration and duration > 0:
    # Drop packets that the source container's edit list never presents at
    # the end.  Without this, cut/edited files can carry extra trailing audio.
    cmd += ["-t", f"{duration:.6f}"]
  cmd += ["-y", destination]
  with open(log_path, "w") as log_file:
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=log_file)
  if result.returncode != 0:
    return False, _read_log(log_path)
  return True, ""


def _read_log(path):
  try:
    with open(path, "r", errors="replace") as handle:
      return handle.read().strip()
  except OSError:
    return ""


def audio_offset_for_mux(info):
  """Audio offset to apply, clamped so the output track starts at t=0.

  A positive offset makes the MP4 muxer insert an empty edit, which delays the
  audio track's start.  Some TVs (LG webOS, for example) then refuse to play
  the track at all, producing silence and no Dolby Digital lock over ARC.
  Positive offsets are normally just a few tens of milliseconds of container
  padding, so aligning both streams at zero is safe.  Negative offsets are kept
  because they trim audio that starts before the video (no empty edit).
  """
  offset = info["audio_start"] - info["video_start"]
  return min(0.0, offset)


def remux_streaming(source, info, audio_path, output_path, use_dovi_tool,
                    work_dir):
  """Run ffmpeg -> dovi_tool -> ffmpeg as a three-process pipeline."""
  video_index = info["video_index"]
  rate = rate_arg(info["rate"])

  # The offset that must be applied to audio so it keeps the same position
  # relative to video.  Pre-roll video packets are hidden again by the edit
  # list after repair_video_timing(), so they do not affect the offset.
  audio_offset = audio_offset_for_mux(info)
  source_offset = info["audio_start"] - info["video_start"]
  if source_offset > 0.100:
    print(
        f"Warning: audio starts {source_offset:.3f}s after video; aligning it "
        "to 0s so the track starts at zero (some TVs reject a delayed track). "
        "Check A/V sync on the result.",
        file=sys.stderr,
    )

  extract_log = os.path.join(work_dir, "ffmpeg_extract.log")
  dovi_log = os.path.join(work_dir, "dovi_tool.log")
  wrap_log = os.path.join(work_dir, "ffmpeg_wrap.log")
  mux_log = os.path.join(work_dir, "ffmpeg_mux.log")

  extract_cmd = [
      "ffmpeg",
      "-nostdin",
      "-hide_banner",
      "-loglevel",
      "error",
      "-i",
      source,
      "-map",
      f"0:{video_index}",
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

  # Input 0 is the timed NUT stream produced by wrap_cmd, so let ffmpeg
  # detect the format.  Declaring it as raw HEVC here makes the demuxer
  # misparse the wrapper and silently truncate the video track.
  mux_cmd = [
      "ffmpeg",
      "-nostdin",
      "-hide_banner",
      "-loglevel",
      "error",
      "-i",
      "pipe:0",
  ]
  expected_duration = info["video_media_end"]
  if audio_path:
    # -itsoffset applies to the next input, so it must be placed directly
    # before the audio file (not before the video pipe).
    if abs(audio_offset) >= 0.0005:
      mux_cmd += ["-itsoffset", f"{audio_offset:.6f}"]
    # Cap the audio at the source's presented duration.  Some containers hide
    # trailing audio behind an edit list; the edit list is gone once the
    # streams have been extracted, so apply the limit here as well.  The cap
    # is an input option so it never drops video packets (their timestamps are
    # synthetic and may not line up with the source's edit list).
    if expected_duration > 0:
      mux_cmd += ["-t", f"{expected_duration:.6f}"]
    mux_cmd += ["-i", audio_path]

  mux_cmd += ["-map", "0:v:0"]
  if audio_path:
    mux_cmd += ["-map", "1:a:0"]

  factor = max(1, -(-1000 // info["rate"].numerator))
  timescale = info["rate"].numerator * factor

  # Raw HEVC carries no timestamps, so the muxer cannot interleave the audio
  # and writes every video packet before the audio track.  Players that read
  # such a file sequentially then stall trying to reach the audio at the end
  # of a multi-gigabyte file.  Stamp each packet at the constant frame rate
  # and wrap the stream in a timed pipe (NUT) so the muxer sees real DTS.
  ticks = factor * info["rate"].denominator
  wrap_cmd = [
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
      "-c",
      "copy",
      "-bsf:v",
      f"setts=dts=N*{ticks}:pts=N*{ticks}:time_base=1/{timescale}",
      "-f",
      "nut",
      "pipe:1",
  ]

  # Write a plain ffmpeg MP4 (moov after mdat, no padding).  The index is
  # normalised and moved to the front afterwards: a moov written by the piped
  # mux does not direct-play on LG webOS, while ffmpeg's normal layout does.
  mux_cmd += [
      "-c",
      "copy",
      "-tag:v",
      "hvc1",
      "-video_track_timescale",
      str(timescale),
      "-map_chapters",
      "-1",
      "-strict",
      "unofficial",
      "-max_muxing_queue_size",
      "4096",
      "-y",
      output_path,
  ]

  extract_log_file = open(extract_log, "w")
  dovi_log_file = open(dovi_log, "w")
  wrap_log_file = open(wrap_log, "w")
  mux_log_file = open(mux_log, "w")

  try:
    p_extract = subprocess.Popen(
        extract_cmd,
        stdout=subprocess.PIPE,
        stderr=extract_log_file,
    )

    if use_dovi_tool:
      p_dovi = subprocess.Popen(
          ["dovi_tool", "--drop-hdr10plus", "convert", "-", "-o", "/dev/stdout"],
          stdin=p_extract.stdout,
          stdout=subprocess.PIPE,
          stderr=dovi_log_file,
      )
      p_extract.stdout.close()
      producer = p_dovi
    else:
      p_dovi = None
      producer = p_extract

    p_wrap = subprocess.Popen(
        wrap_cmd,
        stdin=producer.stdout,
        stdout=subprocess.PIPE,
        stderr=wrap_log_file,
    )
    producer.stdout.close()
    p_mux = subprocess.Popen(
        mux_cmd,
        stdin=p_wrap.stdout,
        stdout=subprocess.DEVNULL,
        stderr=mux_log_file,
    )
    p_wrap.stdout.close()

    mux_rc = p_mux.wait()
    if p_dovi is not None:
      dovi_rc = p_dovi.wait()
    else:
      dovi_rc = 0
    wrap_rc = p_wrap.wait()
    extract_rc = p_extract.wait()
    if mux_rc == 0 and wrap_rc != 0:
      mux_rc = wrap_rc

    if mux_rc != 0:
      for process in (p_mux, p_wrap, p_dovi, p_extract):
        if process is not None and process.poll() is None:
          process.terminate()
    return mux_rc, dovi_rc, extract_rc
  finally:
    for process in (locals().get("p_mux"), locals().get("p_wrap"),
                    locals().get("p_dovi"), locals().get("p_extract")):
      if process is not None and process.poll() is None:
        try:
          process.kill()
        except OSError:
          pass
    extract_log_file.close()
    dovi_log_file.close()
    wrap_log_file.close()
    mux_log_file.close()


def verify_output(path, info, expect_dovi, had_audio):
  problems = []
  data = probe(path)

  streams = data.get("streams", [])
  video = next(
      (s for s in streams
       if s.get("codec_type") == "video"
       and not s.get("disposition", {}).get("attached_pic")),
      None,
  )
  audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

  if video is None:
    problems.append("no video stream")
  else:
    if video.get("codec_tag_string") != "hvc1":
      problems.append(f"video tag is {video.get('codec_tag_string')}, not hvc1")

    dovi_present = any(
        "DOVI" in side.get("side_data_type", "").upper()
        for side in video.get("side_data_list", [])
    )
    if expect_dovi and not dovi_present:
      problems.append("Dolby Vision configuration record was not written")

    if info["nb_frames"]:
      out_frames = int(video.get("nb_frames") or 0)
      if out_frames and abs(out_frames - info["nb_frames"]) > 1:
        problems.append(
            f"frame count changed: {info['nb_frames']} -> {out_frames}"
        )

    out_duration = float(
        video.get("duration") or data.get("format", {}).get("duration") or 0
    )
    expected_duration = info["video_media_end"]
    if expected_duration and out_duration:
      delta = abs(out_duration - expected_duration)
      if delta > max(1.0, expected_duration * 0.002):
        problems.append(
            f"video duration changed by {delta:.2f}s "
            f"({expected_duration:.2f}s expected -> {out_duration:.2f}s)"
        )

    if audio is not None and info["video_duration"]:
      out_audio_start = float(audio.get("start_time") or 0.0)
      # One AC-3/E-AC-3 frame is 32 ms, and MKV sources commonly carry that
      # much codec delay as a real audio start offset.  Only flag delays that
      # exceed a frame.
      if out_audio_start > 0.05:
        problems.append(
            f"audio track starts at {out_audio_start:.3f}s instead of 0s; "
            "some TVs refuse to play such a track (silent output over ARC)"
        )
      out_audio_duration = float(
          audio.get("duration") or data.get("format", {}).get("duration") or 0
      )
      # Compare against the presented (post-edit-list) video duration.
      video_end = info["video_start"] + info["video_duration"]
      audio_end = out_audio_start + out_audio_duration
      difference = abs(video_end - audio_end)
      if difference > max(1.0, info["video_duration"] * 0.002):
        problems.append(
            f"audio/video end times differ by {difference:.2f}s "
            f"(video ends {video_end:.2f}s, audio ends {audio_end:.2f}s)"
        )

  if had_audio and audio is None:
    problems.append("audio stream disappeared")

  return problems, data


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
  parser = argparse.ArgumentParser(
      description=(
          "Remove HDR10+ metadata from Dolby Vision HEVC files while keeping "
          "Dolby Vision and original A/V timing. Output is an MP4."
      )
  )
  parser.add_argument("input_file", help="Source file (MP4, MKV, ...).")
  parser.add_argument(
      "-k",
      "--keep-original",
      action="store_true",
      help="Keep the source file after a successful conversion.",
  )
  parser.add_argument(
      "-a",
      "--audio-track",
      type=int,
      default=0,
      help="Zero-based audio track to keep (default: 0).",
  )
  parser.add_argument(
      "--keep-temp",
      action="store_true",
      help="Keep temporary files for debugging.",
  )
  args = parser.parse_args()

  input_path = os.path.abspath(args.input_file)
  if not os.path.isfile(input_path):
    die(f"Input file not found: {input_path}")

  for tool in ("ffmpeg", "ffprobe"):
    if not which(tool):
      die(f"Required tool not found on PATH: {tool}")

  info = probe_source(input_path)
  base, _ = os.path.splitext(input_path)
  output_path = f"{base}.hdr10plus-removed.mp4"

  if os.path.abspath(output_path) == input_path:
    die("Output path would overwrite the input file.")

  if args.audio_track < 0 or args.audio_track >= len(info["audio_streams"]):
    die(
        f"Audio track {args.audio_track} does not exist "
        f"(found {len(info['audio_streams'])} audio track(s))."
    )

  selected_audio = (
      info["audio_streams"][args.audio_track]
      if info["audio_streams"]
      else None
  )
  had_audio = selected_audio is not None

  check_space(input_path, output_path)

  dovi_box = extract_dovi_box(input_path, info["dovi"])
  use_dovi_tool = info["dovi"] is not None
  if not use_dovi_tool:
    print(
        "Warning: no Dolby Vision configuration found. HDR10+ removal is "
        "skipped and the video will be remuxed unchanged.",
        file=sys.stderr,
    )
  if dovi_box is None and use_dovi_tool:
    die(
        "Dolby Vision was detected, but its configuration record could not "
        "be read or reconstructed."
    )

  print(f"Input:  {input_path}")
  print(f"Output: {output_path}")
  print(
      f"Video:  {info['video'].get('width')}x{info['video'].get('height')} "
      f"HEVC, {float(info['rate']):.6f} fps"
  )
  if selected_audio:
    print(
        f"Audio:  track {args.audio_track} "
        f"({selected_audio.get('codec_name')}, "
        f"{selected_audio.get('channels', '?')} ch)"
    )
  else:
    print("Audio:  none")
  print(f"Dolby Vision: {'yes' if use_dovi_tool else 'no'}")
  if info["pre_roll"]:
    print(
        f"Pre-roll:    {info['pre_roll']:.3f}s of leading video packets kept"
    )
  source_offset = info["audio_start"] - info["video_start"]
  applied_offset = audio_offset_for_mux(info)
  if abs(applied_offset - source_offset) > 0.0005:
    print(
        "A/V offset:  "
        f"{source_offset:+.3f}s (clamped to {applied_offset:+.3f}s so the "
        "audio track starts at 0)"
    )
  else:
    print(f"A/V offset:  {applied_offset:+.3f}s")

  work_dir = tempfile.mkdtemp(prefix=".hdr10plus-", dir=os.path.dirname(input_path))
  audio_path = None

  try:
    if selected_audio:
      print("Extracting audio...")
      audio_path = os.path.join(work_dir, "audio.mka")
      ok, error = extract_audio(
          input_path,
          selected_audio.get("index"),
          audio_path,
          os.path.join(work_dir, "ffmpeg_audio.log"),
          info["audio_duration"] or info["video_duration"],
      )
      if not ok:
        die(f"Audio extraction failed:\n{error}")

    print("Stripping HDR10+ and muxing MP4 (streaming, no large temp files)...")
    mux_rc, dovi_rc, extract_rc = remux_streaming(
        input_path,
        info,
        audio_path,
        output_path,
        use_dovi_tool,
        work_dir,
    )

    if mux_rc != 0 or dovi_rc != 0 or extract_rc != 0:
      print("FFmpeg/dovi_tool pipeline failed.", file=sys.stderr)
      for label, path in (
          ("video extraction", "ffmpeg_extract.log"),
          ("dovi_tool", "dovi_tool.log"),
          ("MP4 mux", "ffmpeg_mux.log"),
      ):
        text = _read_log(os.path.join(work_dir, path))
        if text:
          print(f"--- {label} ---\n{text}", file=sys.stderr)
      if os.path.exists(output_path):
        os.remove(output_path)
      sys.exit(1)

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
      die("FFmpeg produced no output file.")

    print("Restoring original frame timing (B-frame order)...")
    if not repair_video_timing(output_path, info):
      die(
          "Could not restore B-frame presentation timing in the output MP4. "
          "The file was left in place for inspection."
      )

    if dovi_box:
      print("Restoring Dolby Vision configuration record...")
      if not inject_dovi_box(output_path, dovi_box):
        die(
            "Could not inject the Dolby Vision configuration record into the "
            "output MP4. The file was left in place for inspection."
        )

    print("Normalising MP4 index...")
    if not normalise_container(output_path):
      die(
          "Could not normalise the MP4 sample index. The file was left in "
          "place for inspection."
      )

    relocate_moov(output_path)

    problems, output_data = verify_output(
        output_path, info, use_dovi_tool, had_audio
    )
    if problems:
      print("Output verification warnings:", file=sys.stderr)
      for problem in problems:
        print(f"  - {problem}", file=sys.stderr)

    size_gib = os.path.getsize(output_path) / 1024 ** 3
    print(f"Done: {output_path} ({size_gib:.2f} GiB)")

    if not args.keep_original:
      print("Removing original source file...")
      os.remove(input_path)
      print("Original removed.")
    else:
      print("Original kept (--keep-original).")

    if problems:
      sys.exit(2)

  finally:
    if args.keep_temp:
      print(f"Temporary files kept in: {work_dir}")
    else:
      shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
  main()
