"""Where each user's own files live: their profile, resume, reports, and the resumes and cover letters made for them.

    data/users/<user id>/profile.json, resume.pdf, reports/, Optimized Resumes/

Settings that belong to the whole service, not a person (models, sources, request pacing), stay in config.yaml. Files never
leave the user's own folder, and the folder name is the account's number, never anything the user typed."""
from __future__ import annotations

import json
import re
import shutil
import unicodedata
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


def delete_home(user_id: int) -> None:
    """Remove a user's whole folder: their resume, profile, reports and every file made for them."""
    if not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("no such user")
    shutil.rmtree(BASE / str(user_id), ignore_errors=True)


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


# ---------------------------------------------------------------- what the profile page saves
# Everything goes through these functions, so moving the files somewhere else later (for example cloud storage) is one change.

MAX_RESUME_CHARS = 30_000
MAX_PDF_PAGES = 12
SETTINGS_DEFAULTS = {"titles": [], "exclude_title_words": [], "min_pay": 0, "max_age_days": 30, "us_only": True, "exclude_locations": []}


def load_settings(user_id: int) -> dict:
    """The user's job search settings (titles, words to avoid, pay floor, ...), with defaults for anything not saved."""
    path = home(user_id) / "settings.json"
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return {**SETTINGS_DEFAULTS, **saved}


def save_settings(user_id: int, settings: dict) -> None:
    (home(user_id) / "settings.json").write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_profile(user_id: int) -> dict | None:
    path = home(user_id) / "profile.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def save_profile(user_id: int, profile: dict) -> None:
    (home(user_id) / "profile.json").write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def is_set_up(user_id: int) -> bool:
    """True once the person has saved a profile or settings (or chosen to skip the profile page)."""
    folder = home(user_id)
    return (folder / "profile.json").exists() or (folder / "settings.json").exists()


def resume_kind(user_id: int) -> str | None:
    """'pdf' or 'text' for the resume on file, or None."""
    folder = home(user_id)
    if (folder / "resume.pdf").exists():
        return "pdf"
    return "text" if any((folder / n).exists() for n in ("resume.txt", "resume.md")) else None


def _remove_resumes(folder: Path) -> None:
    for name in ("resume.pdf", "resume.txt", "resume.md"):
        (folder / name).unlink(missing_ok=True)


def clean_text(text) -> str:
    """Plain text with line breaks kept: control, invisible and unpaired-surrogate characters removed, and no more than 30,000 characters."""
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    kept = []
    for ch in text:
        cat = unicodedata.category(ch)
        if ch in "\n\t" or (cat not in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")):
            kept.append(ch)
    return re.sub(r"\n{3,}", "\n\n", "".join(kept)).strip()[:MAX_RESUME_CHARS]


def save_resume_text(user_id: int, text: str) -> int:
    """Keep pasted resume text (or a description of the person's experience) as their resume. Returns its length."""
    text = clean_text(text)
    if len(text) < 80:
        from errors import UserError
        raise UserError("That is too short to work from. Paste your whole resume, or write a few sentences about your jobs and skills.", "resume_too_short")
    folder = home(user_id)
    _remove_resumes(folder)
    (folder / "resume.txt").write_text(text, encoding="utf-8")
    return len(text)


def save_resume_pdf(user_id: int, data: bytes) -> dict:
    """Keep an uploaded PDF as the user's resume if it really is a PDF with readable text. Returns {"chars", "pages"}."""
    import io

    from errors import UserError
    if len(data) > MAX_RESUME_BYTES:
        raise UserError("That file is too large. A resume PDF can be at most 5 MB.", "too_large")
    if not data.startswith(b"%PDF-"):
        raise UserError("That does not look like a PDF. Upload a .pdf file, or paste your resume as text.", "not_pdf")
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise UserError("That PDF is password protected. Remove the password, or paste your resume as text.", "pdf_locked")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise UserError(f"That PDF has more than {MAX_PDF_PAGES} pages, which is too long for a resume.", "pdf_too_long")
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
    except UserError:
        raise
    except Exception:
        raise UserError("We could not read that PDF. Try saving it again from your word processor, or paste your resume as text.", "pdf_unreadable") from None
    text = clean_text(text.replace("ﬁ", "fi").replace("ﬂ", "fl"))
    if len(text) < 80:
        raise UserError("That PDF has no text we can read (it may be a scan or an image). Paste your resume as text instead.", "pdf_no_text")
    folder = home(user_id)
    _remove_resumes(folder)
    (folder / "resume.pdf").write_bytes(data)
    return {"chars": len(text), "pages": len(reader.pages)}
