"""Cache stable Claude instructions; record token counts without logging prompts or credentials."""
import logging
import os

log = logging.getLogger("claude_usage")


def system(text, suffix=""):
    if os.environ.get("ANTHROPIC_PROMPT_CACHE", "1") != "1" or not text:
        return text + suffix
    # Static instructions recur across jobs; video rendering can outlast a five-minute cache.
    ttl = os.environ.get("ANTHROPIC_CACHE_TTL", "1h")
    if ttl not in ("5m", "1h"):
        ttl = "1h"
    blocks = [{"type": "text", "text": text,
               "cache_control": {"type": "ephemeral", "ttl": ttl}}]
    if suffix:
        blocks.append({"type": "text", "text": suffix})
    return blocks


def record_usage(response, phase):
    usage = response.get("usage") or {}
    if not usage:
        return
    fresh = usage.get("input_tokens", 0) or 0
    written = usage.get("cache_creation_input_tokens", 0) or 0
    read = usage.get("cache_read_input_tokens", 0) or 0
    total = fresh + written + read
    creation = usage.get("cache_creation") or {}
    log.info("claude tokens phase=%s input=%d cache_write=%d cache_read=%d output=%d "
             "cache_write_5m=%d cache_write_1h=%d cache_read_pct=%.1f",
             phase, fresh, written, read, usage.get("output_tokens", 0) or 0,
             creation.get("ephemeral_5m_input_tokens", 0) or 0,
             creation.get("ephemeral_1h_input_tokens", 0) or 0,
             100 * read / total if total else 0)
