import re
from pathlib import Path
from typing import Optional

from core.security import safe_storage_filename
from services import storage

CATEGORIES = ("BR", "JD", "Resumes", "Corp Pool", "Portal Mapping")


def _bucket(category: str) -> str:
    if category not in CATEGORIES:
        raise ValueError(f"Unknown docs category: {category}")
    return f"docs/{category}"


def usable(name: str) -> bool:
    return bool(name) and not name.startswith(".")


def list_files(category: str) -> list[str]:
    return [name for name in storage.disk_names(_bucket(category)) if usable(name)]


def _path(category: str, name: str) -> Optional[Path]:
    safe = safe_storage_filename(name)
    return storage.disk_path(_bucket(category), safe) if safe else None


def read_file(category: str, name: str) -> Optional[bytes]:
    path = _path(category, name)
    return path.read_bytes() if path and path.is_file() else None


def write_file(category: str, name: str, data: bytes) -> str:
    path = _path(category, name)
    if not path:
        raise ValueError("Invalid file name")
    path.write_bytes(data)
    return path.name[len(storage.prefix(_bucket(category))):]


def delete_file(category: str, name: str) -> bool:
    path = _path(category, name)
    if path and path.is_file():
        path.unlink()
        return True
    return False


def mtime(category: str, name: str) -> float:
    path = _path(category, name)
    return path.stat().st_mtime if path and path.is_file() else 0.0


def is_portal_credentials_name(name: str) -> bool:
    n = _norm(name)
    return bool(re.search(r"\.(xlsx|xls|csv)$", n)) and ("user credential" in n or "employee user credential" in n)


def is_portal_mapping_name(name: str) -> bool:
    n = _norm(name)
    if not re.search(r"\.(xlsx|xls)$", n) or is_portal_credentials_name(name):
        return False
    return "resource question mapping" in n or "question mapping" in n


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[_'`’]", " ", str(name or "").lower())).strip()


PORTAL_MAPPING_STORED_NAME = "Resource_Question_Mapping.xlsx"
