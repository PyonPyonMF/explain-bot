"""Explicit opt-in render modes; ordinary requests remain 2D."""
import re


def mention_mode(text):
    match = re.match(r"^\s*(?:\[3[dд]\]|--3[dд](?=\s|$)|3[dд](?=\s|:|$))\s*:?\s*", text, flags=re.IGNORECASE)
    return (text[match.end():].strip(), "3d") if match else (text, "2d")


def selected_mode(value):
    return "3d" if value == "3d" else "2d"
