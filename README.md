# lg-4k-dv-fixer

Utilities for making 4K Dolby Vision / HDR10+ remuxes behave on LG TVs and
older AV receivers connected over plain HDMI ARC.

The tools focus on three things that commonly break playback on webOS:

- HDR10+ metadata that the TV or receiver mishandles (removed while keeping
  Dolby Vision intact).
- MP4 layouts that webOS cannot direct-play (padded `moov` boxes, fragmented
  MP4, chapter markers).
- Audio tracks that start at a non-zero timestamp, which some TVs refuse to
  play over ARC.

Everything is stream-copy based. No re-encoding, no full-size intermediate
video files, and low memory/disk use.

## Files

| File | Purpose |
| --- | --- |
| `mp4ify.py` | Batch-organises TV episodes and films into MP4 files for a media library. Tags HEVC as `hvc1`, converts text subtitles to `mov_text`, strips chapters by default, and optionally removes HDR10+ while keeping Dolby Vision. |
| `remove-hdr10plus.py` | Standalone streaming HDR10+ stripper for Dolby Vision HEVC sources. Pipes `ffmpeg -> dovi_tool -> ffmpeg`, restores the Dolby Vision configuration record (`dvvC`/`dvcC`), and repairs B-frame presentation timing. |
| `check_mp4.py` | Integrity checker: container structure, HDR/DV metadata, decode errors, frozen frames, PTS discontinuities, and audio decode. |
| `fix_subtitles.py` | Repairs `.srt` files: mojibake, smart quotes/typography, and UTF-8 BOM, with optional ASCII transliteration. |

## Requirements

- Python 3.9+
- `ffmpeg` and `ffprobe` on `PATH`
- `tqdm` for `mp4ify.py` (`pip install tqdm`)
- [`dovi_tool`](https://github.com/quietvoid/dovi_tool) on `PATH` for HDR10+
  removal

`mp4ify.py --remove-hdr10plus` loads `remove-hdr10plus.py` from the same
directory, so keep the two files together.

## Usage

### Convert and file TV episodes / films

```bash
./mp4ify.py -b /path/to/media/library -i /path/to/downloads/Show.S01E01.mkv
```

Preview first:

```bash
./mp4ify.py -b /path/to/media/library --dry-run -i /path/to/downloads/*.mkv
```

Useful options:

| Option | Effect |
| --- | --- |
| `-d`, `--delete-source` | Delete each original input after a successful conversion (off by default). |
| `-r`, `--remove-previous` | Remove the previous episode's MP4 from the destination folder. |
| `--no-subs` | Do not include subtitles. |
| `--flat` | Do not create subdirectories for films. |
| `-a`, `--select-audio` | Prompt for which audio track to keep. |
| `--audio-track N` | Non-interactive audio track selection. |
| `--keep-chapters` | Keep chapter markers (default strips them). |
| `--remove-hdr10plus` | Strip HDR10+ from Dolby Vision HEVC while keeping Dolby Vision. |
| `--skip-space-check` | Bypass the free-space estimate. |

### Remove HDR10+ from a single file

```bash
./remove-hdr10plus.py -k input.mkv
```

`-k` keeps the original. Without it the source is deleted after a successful
conversion; use `-k` when you want to keep it.

### Check a finished file

```bash
./check_mp4.py output.mp4
./check_mp4.py output.mp4 --full        # decode the whole file
./check_mp4.py output.mp4 --first-minutes 10
```

### Repair .srt files

```bash
./fix_subtitles.py -n -r /path/to/subtitles     # preview only
./fix_subtitles.py -r /path/to/subtitles        # apply fixes
./fix_subtitles.py --ascii -r /path/to/subtitles
```

## How the fixes work

### HDR10+ removal

The HEVC stream is extracted to a pipe, cleaned by
`dovi_tool --drop-hdr10plus convert`, and muxed straight into the output MP4.
Dolby Vision RPU data is preserved. No intermediate video or audio file is
written, so a two-hour 4K episode needs only a few megabytes of temporary
space.

### TV-friendly MP4 layout

The final MP4 uses the standard ffmpeg layout (`ftyp / mdat / moov`, with
`moov` at the end). Large `moov`-at-front padding and fragmented MP4 both
caused webOS to spin or freeze a second into playback, so neither is used.

Because the video is piped as raw HEVC, ffmpeg cannot know the original B-frame
presentation order. The script copies the source packet timestamps into fresh
`stts`/`ctts` tables after muxing, and re-inserts the Dolby Vision
configuration record, without touching the media data or chunk offsets.

### Audio over ARC

Positive audio start offsets make ffmpeg insert an empty edit, which some LG
sets refuse to play (silent output, no Dolby Digital lock over ARC). The tools
clamp those offsets so the audio track starts at zero. Atmos is carried as
E-AC-3, which older receivers accept over plain ARC.

## Safety notes

- Input files are never deleted unless you pass `--delete-source` (for
  `mp4ify.py`) or omit `-k` (for `remove-hdr10plus.py`).
- `mp4ify.py` refuses to run when the output path would overwrite the input.
- Temporary files are created inside the destination directory and removed
  automatically.
- Always keep a backup of irreplaceable media. These scripts remux rather than
  re-encode, but container-level surgery is still surgery.

## License

No license file is included yet. Add one before redistributing.
