import re


def format_product_display_name(name) -> str:
    if not name:
        return ""
    out = re.sub(r"\bOne-NDS\b", "One-SDL", str(name), flags=re.I)
    out = re.sub(r"\bONE NDS\b", "ONE SDL", out, flags=re.I)
    out = re.sub(r"\bOne NDS\b", "One SDL", out, flags=re.I)
    return re.sub(r"\bNDS\b", "SDL", out)


def format_topic_title_for_display(title) -> str:
    if not title:
        return ""
    trimmed = str(title).strip()
    trimmed = re.sub(r"\s+\(previous(?: attempt)?\)$", "", trimmed, flags=re.I)
    if re.match(r"^\[[^\]]+\]", trimmed):
        return trimmed
    return format_product_display_name(trimmed)
