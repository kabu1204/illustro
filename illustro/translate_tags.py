"""Translate danbooru tags (English -> Chinese) using an OpenAI-compatible LLM API.

Reads tags from the WD14 tags_info.csv, batches them into LLM calls, and writes
{english: chinese} JSON compatible with tags_zh.json / DB.apply_zh_table.

Features:
  - Resumable: partial results are saved after every batch; re-running skips
    already-translated tags.
  - Merges with existing tags_zh.json (built-in or custom) so you don't redo
    hand-translated entries.
  - Category filter: by default translates general (0) + character (4) tags,
    skipping rating (9) which are only 4 fixed labels.
  - Concurrent requests (16 by default) via ThreadPoolExecutor.
  - Configurable via CLI flags or env vars (OPENAI_API_KEY, OPENAI_BASE_URL, ...).

Usage:
  # Basic: translate all general+character tags, merge into data/tags_zh.json
  python -m illustro.translate_tags

  # Point to a local / third-party OpenAI-compatible endpoint
  python -m illustro.translate_tags \\
      --base-url http://localhost:11434/v1 \\
      --api-key dummy \\
      --model qwen2.5-72b-instruct

  # Only translate tags missing from the existing table
  python -m illustro.translate_tags --skip-existing

  # Dry run: show counts, don't call the API
  python -m illustro.translate_tags --dry-run

Env vars (fallbacks for CLI flags):
  OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from openai import OpenAI, APIError, APIStatusError, APITimeoutError, RateLimitError
from tqdm import tqdm

from .config import ROOT

log = logging.getLogger("translate_tags")

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_CSV = ROOT / "illustro" / "data" / "models" / "SmilingWolf" / "wd-swinv2-tagger-v3" / "tags_info.csv"
DEFAULT_OUTPUT = ROOT / "illustro" / "data" / "tags_zh.json"
PARTIAL_SUFFIX = ".partial.json"

# Tags whose "name" is pure punctuation / emoticon — skip, not worth translating
def _is_junk_tag(name: str) -> bool:
    """Filter out emoticon / punctuation-only tags that have no meaningful Chinese."""
    return all(not c.isalnum() for c in name)

SYSTEM_PROMPT = """\
You are a professional translator for danbooru anime illustration tags.
You will receive a JSON object mapping tag IDs to English danbooru tag names.
Return a JSON object mapping the SAME tag IDs to concise Chinese translations.

Rules:
- Translate the visual/semantic meaning, not literally. E.g. "ahoge" -> "呆毛", "twintails" -> "双马尾".
- For character names (e.g. "hatsune_miku"), use the established Chinese name (初音未来).
- For series/franchise qualifiers in parentheses, keep them concise: "hatsune_miku_(vocaloid)" -> "初音未来(Vocaloid)".
- For clothing/item tags, use natural Chinese: "school_uniform" -> "校服".
- For NSFW tags, translate clinically and concisely.
- Keep translations short (1-6 characters when possible), suitable for a UI tag chip.
- If a tag is pure punctuation or an emoticon (e.g. ":3", ">_<"), translate it as the original string.
- Output ONLY the JSON object, no commentary, no markdown fences.\
"""

BATCH_SIZE = 80  # Tags per API call — balance between context window and latency
DEFAULT_CONCURRENCY = 16  # Concurrent API requests


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def load_tags_from_csv(csv_path: Path, categories: set[int]) -> list[str]:
    """Read tag names from WD14 tags_info.csv, filtered by category."""
    names: list[str] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if int(row["category"]) in categories:
                name = row["name"].strip()
                if name:
                    names.append(name)
    return names


def load_existing(path: Path) -> dict[str, str]:
    """Load an existing {english: chinese} table, tolerating missing file / _comment key."""
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _strip_code_fences(content: str) -> str:
    """Strip markdown code fences if the model added them despite instructions."""
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
    return content


def call_llm(
    batch: dict[int, str],
    *,
    client: OpenAI,
    model: str,
    batch_num: int,
    max_retries: int = 4,
) -> dict[int, str]:
    """Send a batch of tags to the LLM and parse the returned {id: chinese} mapping.

    The openai SDK handles transport-level retries (connection errors, 429, 5xx)
    internally via its `max_retries` setting. This function adds an application-
    level retry for JSON parse errors (when the model returns malformed output),
    with exponential backoff. Each retry is logged at WARNING; permanent failure
    at ERROR.
    """
    user_content = json.dumps(batch, ensure_ascii=False)
    last_err = None

    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                response_format={"type": "json_object"},
            )
            content = _strip_code_fences(resp.choices[0].message.content)
            parsed = json.loads(content)
            # Validate: keys should match batch keys
            result: dict[int, str] = {}
            for k, v in parsed.items():
                try:
                    idx = int(k)
                except (ValueError, TypeError):
                    continue
                if idx in batch and isinstance(v, str) and v.strip():
                    result[idx] = v.strip()
            if attempt > 0:
                log.info("batch %d: succeeded after %d retries", batch_num, attempt + 1)
            return result
        except (APIStatusError, RateLimitError, APITimeoutError, APIError) as e:
            # SDK already retried internally; these are failures after its retries
            last_err = f"{type(e).__name__}: {e}"
            wait = 2 ** attempt
            log.warning("batch %d: retry %d/%d — %s — waiting %ds", batch_num, attempt + 1, max_retries, last_err, wait)
            time.sleep(wait)
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            # Model returned malformed JSON — retry with fresh generation
            last_err = f"{type(e).__name__}: {e}"
            wait = 2 ** attempt
            log.warning("batch %d: retry %d/%d — %s — waiting %ds", batch_num, attempt + 1, max_retries, last_err, wait)
            time.sleep(wait)

    log.error("batch %d: failed after %d retries: %s", batch_num, max_retries, last_err)
    raise RuntimeError(f"LLM call failed after {max_retries} retries: {last_err}")


def save_partial(path: Path, mapping: dict[str, str]) -> None:
    """Atomically write the partial {english: chinese} table."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def merge_and_save(output_path: Path, new_mapping: dict[str, str], existing: dict[str, str]) -> None:
    """Merge new translations over existing (new wins) and write final tags_zh.json."""
    merged = dict(existing)
    merged.update(new_mapping)
    # Preserve _comment if present in existing file
    if output_path.exists():
        raw = json.loads(output_path.read_text(encoding="utf-8"))
        if "_comment" in raw:
            merged = {"_comment": raw["_comment"], **merged}
    output_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[done] Merged {len(new_mapping)} new translations into {output_path}")
    print(f"       Total entries: {len(merged)} (existing: {len(existing)}, new: {len(new_mapping)})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="illustro.translate_tags",
        description="Translate danbooru tags to Chinese via an OpenAI-compatible LLM.",
    )
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV, help=f"Path to tags_info.csv (default: {DEFAULT_CSV})")
    p.add_argument("--output", "-o", type=Path, default=DEFAULT_OUTPUT, help=f"Output tags_zh.json path (default: {DEFAULT_OUTPUT})")
    p.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                   help="OpenAI-compatible API base URL (env: OPENAI_BASE_URL)")
    p.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", ""),
                   help="API key (env: OPENAI_API_KEY)")
    p.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                   help="Model name (env: OPENAI_MODEL)")
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE, help=f"Tags per API call (default: {BATCH_SIZE})")
    p.add_argument("--concurrency", "-j", type=int, default=DEFAULT_CONCURRENCY,
                   help=f"Concurrent API requests (default: {DEFAULT_CONCURRENCY})")
    p.add_argument("--max-retries", type=int, default=4, help="Max application-level retries per batch (default: 4)")
    p.add_argument("--timeout", type=float, default=120, help="Per-request timeout in seconds (default: 120)")
    p.add_argument("--categories", default="0,4", help="Comma-separated category IDs to translate (default: 0=general,4=character)")
    p.add_argument("--skip-existing", action="store_true", help="Skip tags already present in the output table")
    p.add_argument("--no-junk-filter", action="store_true", help="Don't filter punctuation/emoticon-only tags")
    p.add_argument("--dry-run", action="store_true", help="Show counts without calling the API")
    p.add_argument("--limit", type=int, default=None, help="Translate at most N tags (for testing)")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="Logging level (default: INFO)")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    if not args.api_key and not args.dry_run:
        p.error("--api-key is required (or set OPENAI_API_KEY env var)")

    # --- Load source tags ---
    if not args.csv.exists():
        print(f"[!] CSV not found: {args.csv}", file=sys.stderr)
        print("    Run: python -m illustro.cli download-models", file=sys.stderr)
        return 1

    categories = {int(c) for c in args.categories.split(",") if c.strip()}
    cat_names = {0: "general", 4: "character", 9: "rating"}
    print(f"[*] Loading tags from {args.csv}")
    print(f"    Categories: {', '.join(cat_names.get(c, str(c)) for c in sorted(categories))}")
    all_tags = load_tags_from_csv(args.csv, categories)
    print(f"    Total tags in CSV: {len(all_tags)}")

    if not args.no_junk_filter:
        before = len(all_tags)
        all_tags = [t for t in all_tags if not _is_junk_tag(t)]
        print(f"    After junk filter: {len(all_tags)} (removed {before - len(all_tags)} emoticon/punctuation tags)")

    # --- Load existing translations ---
    existing = load_existing(args.output)
    print(f"[*] Existing translations in {args.output}: {len(existing)}")

    if args.skip_existing:
        all_tags = [t for t in all_tags if t not in existing]
        print(f"    After --skip-existing: {len(all_tags)} tags to translate")

    if args.limit:
        all_tags = all_tags[:args.limit]
        print(f"    Limited to {len(all_tags)} tags (--limit)")

    if not all_tags:
        print("[*] Nothing to translate. Exiting.")
        return 0

    n_batches = (len(all_tags) + args.batch_size - 1) // args.batch_size
    print(f"[*] {len(all_tags)} tags -> {n_batches} batches of ~{args.batch_size} (concurrency={args.concurrency})")

    if args.dry_run:
        print("[*] Dry run — not calling API. First 10 tags:")
        for t in all_tags[:10]:
            print(f"    {t}")
        return 0

    # --- Resume from partial file ---
    partial_path = args.output.with_suffix(args.output.suffix + PARTIAL_SUFFIX)
    partial: dict[str, str] = load_existing(partial_path) if partial_path.exists() else {}
    remaining = [t for t in all_tags if t not in partial]
    if partial:
        print(f"[*] Resuming: {len(partial)} already translated in {partial_path.name}, {len(remaining)} remaining")
    else:
        print(f"[*] Starting fresh. Partial progress will be saved to {partial_path.name}")

    # --- Create OpenAI client (thread-safe, shared across workers) ---
    # The SDK handles transport-level retries (max_retries=3) and connection pooling.
    client = OpenAI(
        base_url=args.base_url,
        api_key=args.api_key,
        max_retries=3,        # SDK-level retries for 429/5xx/connection errors
        timeout=args.timeout,
    )

    # --- Translate (concurrent) ---
    model = args.model
    batch_size = args.batch_size
    concurrency = max(1, args.concurrency)
    max_retries = args.max_retries

    # Build all (batch_num, chunk) work items
    work: list[tuple[int, list[str], dict[int, str]]] = []
    for bi in range(0, len(remaining), batch_size):
        chunk = remaining[bi:bi + batch_size]
        batch = {i: name for i, name in enumerate(chunk)}
        work.append((bi // batch_size + 1, chunk, batch))

    failed_batches: list[int] = []
    partial_lock = threading.Lock()
    save_lock = threading.Lock()

    pbar = tqdm(total=len(remaining), desc="translating", unit="tag")

    def _worker(item: tuple[int, list[str], dict[int, str]]) -> tuple[int, list[str], dict[int, str] | None]:
        batch_num, chunk, batch = item
        try:
            result = call_llm(batch, client=client, model=model, batch_num=batch_num, max_retries=max_retries)
            return (batch_num, chunk, result)
        except RuntimeError:
            return (batch_num, chunk, None)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_worker, item): item for item in work}
        for fut in as_completed(futures):
            batch_num, chunk, result = fut.result()
            if result is None:
                failed_batches.append(batch_num)
            else:
                with partial_lock:
                    for idx, zh in result.items():
                        partial[chunk[idx]] = zh
                with save_lock:
                    save_partial(partial_path, partial)
            pbar.update(len(chunk))
            with partial_lock:
                done = len(partial)
            pbar.set_postfix(done=done, failed=len(failed_batches))

    pbar.close()

    if failed_batches:
        print(f"\n[!] {len(failed_batches)} batches failed. Partial results saved to {partial_path}")
        print(f"    Failed batch numbers: {failed_batches}")
        print(f"    Re-run the same command to retry (completed tags are skipped).")

    # --- Merge into final output ---
    new_count = len(partial)
    if new_count == 0:
        print("\n[!] No translations produced. Nothing to merge.")
        return 1

    merge_and_save(args.output, partial, existing)

    # Clean up partial on full success
    if not failed_batches:
        partial_path.unlink(missing_ok=True)
        print(f"    Removed partial file {partial_path.name}")

    return 0 if not failed_batches else 2


if __name__ == "__main__":
    sys.exit(main())
