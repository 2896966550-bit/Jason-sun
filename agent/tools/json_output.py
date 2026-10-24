"""Shared parser for object-shaped JSON returned by language models."""
import json
import re


def parse_json_object(text) -> dict:
    """Accept plain/fenced JSON or a JSON object embedded in prose.

    A valid array or scalar is not an object and is rejected. raw_decode handles
    braces inside strings and adjacent objects without a greedy regular expression.
    """
    if not isinstance(text, str) or not text.strip():
        return {}
    value = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.I)
    value = re.sub(r"\s*```$", "", value)
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        pass
    else:
        return parsed if isinstance(parsed, dict) else {}
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", value):
        try:
            parsed, _ = decoder.raw_decode(value, match.start())
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}
