"""Where each user's own files live: their profile, resume, reports, and the resumes and cover letters made for them.

    data/users/<user id>/profile.json, resume.pdf, reports/, Optimized Resumes/

Settings that belong to the whole service, not a person (models, sources, request pacing), stay in config.yaml. Files never
leave the user's own folder, and the folder name is the account's number, never anything the user typed."""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).parent
BASE = ROOT / "data" / "users"

# What a person owns on disk, relative to the project folder for the single-user setup that came before accounts.
OWN_FILES = ("profile.json", "resume.pdf", "resume.txt", "resume.md")
OWN_FOLDERS = ("reports", "Optimized Resumes")
MAX_RESUME_BYTES = 5 * 1024 * 1024


def home(user_id: int) -> Path:
    """The folder for one user, made if it is not there yet."""
    if not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("no such user")
    path = BASE / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def adopt_project_files(user_id: int, source: Path | None = None) -> list[str]:
    """Copy the files of the single-user setup (profile.json, resume, reports, Optimized Resumes) into this user's folder.
    Nothing is overwritten and the originals stay where they are. Returns what was copied."""
    source = source or ROOT
    target, copied = home(user_id), []
    for name in OWN_FILES:
        if (source / name).is_file() and not (target / name).exists():
            shutil.copy2(source / name, target / name)
            copied.append(name)
    for name in OWN_FOLDERS:
        if (source / name).is_dir() and not (target / name).exists():
            shutil.copytree(source / name, target / name)
            copied.append(name + "/")
    return copied
