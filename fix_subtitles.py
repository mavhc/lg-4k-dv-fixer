#!/usr/bin/env python3
"""
fix_subtitles.py

Repairs character encoding and formatting issues in SubRip (.srt) subtitle files:
  1. Fixes mojibake (corrupted double-encoded characters like 'Ã©' -> 'é', 'â€™' -> ''').
  2. Normalizes typographical punctuation (smart quotes, curly apostrophes, ellipses, non-breaking spaces).
  3. Ensures UTF-8 with BOM (Byte Order Mark, EF BB BF) so players (Smart TVs, Plex, VLC, etc.)
     recognize UTF-8 rather than defaulting to Latin-1/Windows-1252.
  4. Optionally transliterates/flattens accented characters to pure ASCII (--ascii).

Usage:
  python3 fix_subtitles.py [PATH ...] [OPTIONS]

Examples:
  # Inspect current directory for subtitle issues (dry-run):
  python3 fix_subtitles.py -n

  # Fix all subtitles in current directory (adds BOM + fixes mojibake/smart quotes):
  python3 fix_subtitles.py

  # Fix subtitles recursively across all show folders:
  python3 fix_subtitles.py ~/Downloads/AE-DV/ -r

  # Convert all accented/special characters to plain ASCII:
  python3 fix_subtitles.py --ascii
"""

import argparse
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import List, Optional, Tuple

UTF8_BOM = b"\xef\xbb\xbf"

# Common mojibake patterns caused by UTF-8 decoded as Windows-1252 / ISO-8859-1
COMMON_MOJIBAKE = [
    (r"Ã©", "é"),
    (r"Ã¨", "è"),
    (r"Ã ", "à"),
    (r"Ã¡", "á"),
    (r"Ã¢", "â"),
    (r"Ãª", "ê"),
    (r"Ã®", "î"),
    (r"Ã¯", "ï"),
    (r"Ã´", "ô"),
    (r"Ã»", "û"),
    (r"Ã¹", "ù"),
    (r"Ã§", "ç"),
    (r"Ã±", "ñ"),
    (r"Ã¼", "ü"),
    (r"Ã¶", "ö"),
    (r"Ã¤", "ä"),
    (r"Ã‹", "Ë"),
    (r"Ã‰", "É"),
    (r"â€™", "'"),
    (r"â€˜", "'"),
    (r"â€œ", '"'),
    (r"â€\x9d", '"'),
    (r"â€\x9c", '"'),
    (r"â€”", "—"),
    (r"â€“", "–"),
    (r"â€¦", "…"),
    (r"Â ", " "),
    (r"Â·", "·"),
]

# Replacements for standardizing common typographical symbols
TYPOGRAPHY_REPLACEMENTS = {
    "\u00a0": " ",      # Non-breaking space
    "\u200b": "",       # Zero-width space
    "\u2018": "'",      # Left single quote
    "\u2019": "'",      # Right single quote / apostrophe
    "\u201c": '"',      # Left double quote
    "\u201d": '"',      # Right double quote
    "\u2014": " - ",    # Em dash
    "\u2013": "-",      # En dash
    "\u2026": "...",    # Ellipsis
}


def read_subtitle_file(file_path: Path) -> Tuple[str, str, bool]:
    """
    Reads a subtitle file, auto-detecting encoding and whether a UTF-8 BOM is present.
    Returns: (text_content, detected_encoding, has_utf8_bom)
    """
    raw_bytes = file_path.read_bytes()

    has_bom = raw_bytes.startswith(UTF8_BOM)
    if has_bom:
        raw_bytes = raw_bytes[len(UTF8_BOM):]

    # Try standard encodings
    for encoding in ("utf-8", "cp1252", "iso-8859-1", "latin-1", "utf-16"):
        try:
            text = raw_bytes.decode(encoding)
            return text, encoding, has_bom
        except (UnicodeDecodeError, LookupError):
            continue

    # Fallback with replacement
    return raw_bytes.decode("utf-8", errors="replace"), "utf-8 (lossy)", has_bom


def try_repair_mojibake(text: str) -> str:
    """
    Attempts to fix double-encoded UTF-8 mojibake.
    E.g. text containing 'Ã©' -> 'é'.
    """
    fixed = text
    # 1. Direct regex replacement of known mojibake signatures
    for bad, good in COMMON_MOJIBAKE:
        if bad in fixed:
            fixed = fixed.replace(bad, good)

    # 2. General byte roundtrip test for remaining mojibake sequences
    # If the text has sequences of Latin-1 high bytes that form valid UTF-8
    try:
        if any(c in fixed for c in ("Ã", "â")):
            candidate = fixed.encode("latin-1").decode("utf-8")
            # Sanity check: valid candidate should be shorter and not crash
            if len(candidate) < len(fixed):
                fixed = candidate
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass

    return fixed


def clean_typography(text: str) -> str:
    """Replaces smart quotes, non-breaking spaces, and ellipses with standard punctuation."""
    for bad, good in TYPOGRAPHY_REPLACEMENTS.items():
        if bad in text:
            text = text.replace(bad, good)
    return text


def transliterate_to_ascii(text: str) -> str:
    """
    Converts accented Unicode characters to plain ASCII equivalents (e.g. 'Lénia' -> 'Lenia').
    """
    # Normalize typography first
    text = clean_typography(text)

    # Decompose accented characters (NFKD) and strip non-ASCII combining marks
    normalized = unicodedata.normalize("NFKD", text)
    ascii_bytes = normalized.encode("ascii", "ignore")
    return ascii_bytes.decode("ascii")


def find_non_ascii_samples(text: str, max_samples: int = 5) -> List[str]:
    """Finds sample lines containing non-ASCII characters."""
    samples = []
    for line in text.splitlines():
        trimmed = line.strip()
        if any(ord(c) >= 128 for c in trimmed):
            if trimmed not in samples:
                samples.append(trimmed)
                if len(samples) >= max_samples:
                    break
    return samples


def process_file(
    file_path: Path,
    add_bom: bool = True,
    clean_typo: bool = False,
    to_ascii: bool = False,
    fix_mojibake: bool = True,
    dry_run: bool = False,
    verbose: bool = False,
) -> bool:
    """
    Processes a single subtitle file. Returns True if changes were planned/made.
    """
    try:
        original_text, encoding, has_bom = read_subtitle_file(file_path)
    except Exception as e:
        print(f"  [ERROR] Could not read {file_path.name}: {e}")
        return False

    processed_text = original_text

    # 1. Fix Mojibake
    if fix_mojibake:
        processed_text = try_repair_mojibake(processed_text)

    # 2. Clean typography if requested
    if clean_typo:
        processed_text = clean_typography(processed_text)

    # 3. Transliterate to ASCII if requested
    if to_ascii:
        processed_text = transliterate_to_ascii(processed_text)

    # Determine whether changes are needed
    text_changed = processed_text != original_text
    needs_bom = add_bom and not has_bom and not to_ascii  # Pure ASCII doesn't strictly need BOM, but can have it
    if add_bom and not has_bom:
        needs_bom = True

    if not text_changed and not needs_bom:
        if verbose:
            print(f"  [OK] {file_path.name} (already clean and UTF-8 BOM encoded)")
        return False

    # Summarize actions
    reasons = []
    if text_changed:
        if to_ascii:
            reasons.append("transliterated to ASCII")
        else:
            if fix_mojibake and processed_text != original_text:
                reasons.append("repaired characters / typography")
    if needs_bom:
        reasons.append("added UTF-8 BOM")

    action_summary = ", ".join(reasons)

    non_ascii_samples = find_non_ascii_samples(processed_text)

    print(f"  {'[DRY RUN] Would update' if dry_run else '[UPDATED]'} {file_path.name}")
    print(f"    Actions: {action_summary} (original encoding: {encoding})")
    if non_ascii_samples and not to_ascii and verbose:
        print(f"    Contains Unicode: {non_ascii_samples[:2]}")

    if not dry_run:
        # Write out with UTF-8 BOM (utf-8-sig) or plain UTF-8
        target_encoding = "utf-8-sig" if add_bom else "utf-8"
        file_path.write_text(processed_text, encoding=target_encoding)

    return True


def collect_subtitle_files(paths: List[Path], recursive: bool = False) -> List[Path]:
    """Collects all .srt files from the specified paths."""
    srt_files = set()
    for p in paths:
        if p.is_file() and p.suffix.lower() == ".srt":
            srt_files.add(p.resolve())
        elif p.is_dir():
            pattern = "**/*.srt" if recursive else "*.srt"
            for sub_p in p.glob(pattern):
                if sub_p.is_file():
                    srt_files.add(sub_p.resolve())
    return sorted(srt_files)


def main():
    parser = argparse.ArgumentParser(
        description="Fix encoding issues, mojibake, smart typography, and UTF-8 BOM in .srt subtitle files."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Files or directories to process (default: current directory)",
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="Search directories recursively for .srt files",
    )
    parser.add_argument(
        "-b",
        "--add-bom",
        action="store_true",
        default=True,
        help="Add UTF-8 BOM header (EF BB BF) for max player compatibility (enabled by default)",
    )
    parser.add_argument(
        "--no-bom",
        dest="add_bom",
        action="store_false",
        help="Do not add UTF-8 BOM header",
    )
    parser.add_argument(
        "-a",
        "--ascii",
        action="store_true",
        help="Transliterate accented characters to plain ASCII (e.g. 'Lénia' -> 'Lenia')",
    )
    parser.add_argument(
        "--clean-typography",
        action="store_true",
        default=True,
        help="Normalize smart quotes, curly apostrophes, and ellipses to standard ASCII punctuation (default: on)",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying any files",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Execute changes without confirmation prompt",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed information for every file",
    )

    args = parser.parse_args()

    input_paths = [Path(p).resolve() for p in args.paths]
    files_to_check = collect_subtitle_files(input_paths, recursive=args.recursive)

    if not files_to_check:
        print("No .srt subtitle files found.")
        return

    print(f"Scanning {len(files_to_check)} subtitle file(s)...")
    if args.dry_run:
        print("Running in DRY RUN mode (no files will be modified).\n")
    else:
        print()

    # Pass 1: Plan or perform
    if not args.dry_run and not args.yes and sys.stdin.isatty():
        # First do a dry-run check to see what would change
        changes = []
        for srt in files_to_check:
            # Check what would change
            orig_text, enc, has_bom = read_subtitle_file(srt)
            mod_text = orig_text
            mod_text = try_repair_mojibake(mod_text)
            if args.clean_typography:
                mod_text = clean_typography(mod_text)
            if args.ascii:
                mod_text = transliterate_to_ascii(mod_text)
            needs_change = (mod_text != orig_text) or (args.add_bom and not has_bom)
            if needs_change:
                changes.append(srt)

        if not changes:
            print("All subtitle files are already clean and properly formatted.")
            return

        print(f"Found {len(changes)} file(s) that need updating:")
        for c in changes:
            print(f"  - {c.name}")

        try:
            choice = input(f"\nApply changes to these {len(changes)} file(s)? [y/N]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            sys.exit(1)

        if choice not in ("y", "yes"):
            print("Aborted. No changes made.")
            return

        print("\nApplying changes...")
        updated_count = 0
        for srt in changes:
            if process_file(
                srt,
                add_bom=args.add_bom,
                clean_typo=args.clean_typography,
                to_ascii=args.ascii,
                fix_mojibake=True,
                dry_run=False,
                verbose=args.verbose,
            ):
                updated_count += 1
        print(f"\nDone! Successfully updated {updated_count} file(s).")
        return

    # Non-interactive, --yes, or --dry-run
    updated_count = 0
    for srt in files_to_check:
        if process_file(
            srt,
            add_bom=args.add_bom,
            clean_typo=args.clean_typography,
            to_ascii=args.ascii,
            fix_mojibake=True,
            dry_run=args.dry_run,
            verbose=args.verbose,
        ):
            updated_count += 1

    if args.dry_run:
        print(f"\nDry run complete: {updated_count}/{len(files_to_check)} file(s) would be updated.")
    else:
        print(f"\nDone! Successfully updated {updated_count}/{len(files_to_check)} file(s).")


if __name__ == "__main__":
    main()
