def slugify(text: str) -> str:
    return "-".join(text.lower().split())
