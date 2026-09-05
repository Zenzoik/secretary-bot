from __future__ import annotations


def normalize_username(username: str | None) -> str | None:
    value = (username or "").strip().lstrip("@")
    return value or None


def useful_name(name: str | None) -> str | None:
    value = " ".join((name or "").split())
    return value if value and any(character.isalnum() for character in value) else None


def contact_label(
    name: str | None,
    username: str | None,
    *,
    fallback: str = "Контакт без імені",
    include_username: bool = True,
) -> str:
    clean_name = useful_name(name)
    clean_username = normalize_username(username)
    if clean_name and clean_username and include_username:
        return f"{clean_name} · @{clean_username}"
    if clean_name:
        return clean_name
    if clean_username:
        return f"@{clean_username}"
    return fallback


def user_label(
    display_name: str | None,
    username: str | None,
    *,
    fallback: str = "Користувач без імені",
) -> str:
    return contact_label(display_name, username, fallback=fallback)
