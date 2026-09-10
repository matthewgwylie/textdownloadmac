#!/usr/bin/env python3
"""
textdownload — Export iPhone Messages conversations with a contact to a PDF.

Two sources are supported and the tool auto-picks between them:

  1. The Mac's live Messages database (~/Library/Messages/chat.db).
     Useful when Messages on the Mac is signed in with the same Apple ID
     as the iPhone (or SMS Forwarding is on) so the threads are already
     mirrored there.

  2. An iPhone backup made by Finder while the phone is USB-attached
     (~/Library/Application Support/MobileSync/Backup/<UDID>/). This is
     the fallback when the phone is only plugged in and NOT synced to
     the Mac. The tool locates the latest local backup, reads its
     Manifest.db, extracts sms.db (and pulls image attachments out of
     the backup on demand).

Default behavior: try the Mac database; if no messages match the
contact, automatically fall back to the latest local iPhone backup.

The exported PDF contains:
  - A "Direct Conversation" section with the 1:1 thread(s) for the number.
  - A separate section for each group chat that includes the number,
    with the other participants' phone numbers or email handles listed
    in the section header. Every message in a group section is labeled
    with the sender's handle.

Phone-number matching is loose: 555-123-4567, (555) 123-4567 and
+15551234567 all resolve to the same contact via last-10-digit match.

Usage:
    python3 textdownload.py                           # prompts for the number
    python3 textdownload.py --phone +15551234567
    python3 textdownload.py --phone 5551234567 --output thread.pdf
    python3 textdownload.py --list-backups            # show phones you've backed up
    python3 textdownload.py --use-backup --phone ...  # force reading from backup

Reading chat.db requires Full Disk Access for the terminal running
this script (System Settings -> Privacy & Security -> Full Disk Access).
Reading iPhone backups does not.

Encrypted iPhone backups are detected but not decrypted here. If your
backup is encrypted, uncheck "Encrypt local backup" in Finder and make
a fresh backup, then re-run.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import plistlib
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

try:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        Image,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
    )
    from reportlab.platypus.flowables import KeepTogether
except ImportError:
    sys.stderr.write(
        "Missing dependency: reportlab.\n"
        "Install with:  python3 -m pip install --user reportlab\n"
    )
    sys.exit(2)


DEFAULT_DB = Path.home() / "Library" / "Messages" / "chat.db"
DEFAULT_BACKUP_ROOT = (
    Path.home() / "Library" / "Application Support" / "MobileSync" / "Backup"
)
COCOA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# A "resolve attachment" function maps the raw attachment.filename value
# from the messages database to a local file we can embed in the PDF, or
# None if that file isn't available. Different sources (live Mac vs iOS
# backup) supply different resolvers.
AttachmentResolver = Callable[[Optional[str]], Optional[Path]]

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff"}
HEIC_EXTS = {".heic", ".heif"}


def cocoa_to_datetime(cocoa: Optional[int]) -> Optional[datetime]:
    """Convert a Cocoa Core Data timestamp to a timezone-aware datetime.

    macOS Sierra and later store this as nanoseconds since 2001-01-01 UTC;
    earlier versions used seconds.
    """
    if not cocoa:
        return None
    seconds = cocoa / 1_000_000_000 if cocoa > 10**12 else float(cocoa)
    return COCOA_EPOCH + timedelta(seconds=seconds)


def normalize_number(raw: str) -> str:
    """Reduce a phone-number-ish string to a canonical form for matching."""
    if not raw:
        return ""
    raw = raw.strip()
    plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    return ("+" if plus else "") + digits


def numbers_match(a: str, b: str) -> bool:
    """Loose equality: exact E.164, or last-10-digit match for NANP numbers."""
    if not a or not b:
        return False
    if a == b:
        return True
    ad = re.sub(r"\D", "", a)
    bd = re.sub(r"\D", "", b)
    if ad == bd and ad:
        return True
    if len(ad) >= 10 and len(bd) >= 10 and ad[-10:] == bd[-10:]:
        return True
    return False


def find_handle_ids(conn: sqlite3.Connection, phone: str) -> list[int]:
    """Return handle.ROWIDs whose id matches the requested phone number."""
    target = normalize_number(phone)
    cur = conn.cursor()
    cur.execute("SELECT ROWID, id FROM handle")
    matches: list[int] = []
    for rowid, hid in cur.fetchall():
        if hid and numbers_match(normalize_number(hid), target):
            matches.append(rowid)
    return matches


def extract_text_from_attributed_body(blob: bytes) -> Optional[str]:
    """Best-effort text extraction from the attributedBody typedstream blob.

    macOS Ventura and later often leave message.text NULL and store the
    body as an NSAttributedString in Apple's typedstream format. A full
    parser is out of scope; this handles the common single-string case.
    """
    if not blob:
        return None
    try:
        marker = blob.find(b"NSString")
        if marker < 0:
            return None
        # After the class definition the string data is introduced by 0x2b ("+").
        plus = blob.find(b"\x2b", marker)
        if plus < 0 or plus + 1 >= len(blob):
            return None
        k = plus + 1
        length_byte = blob[k]
        if length_byte < 0x80:
            length = length_byte
            k += 1
        elif length_byte == 0x81:
            length = int.from_bytes(blob[k + 1 : k + 3], "big", signed=False)
            k += 3
        elif length_byte == 0x82:
            length = int.from_bytes(blob[k + 1 : k + 5], "big", signed=False)
            k += 5
        else:
            return None
        if length <= 0 or k + length > len(blob):
            return None
        text = blob[k : k + length].decode("utf-8", errors="replace")
        # Messages sometimes uses U+FFFC (object replacement) as a placeholder
        # around attachments; drop it so it doesn't appear as a stray box.
        return text.replace("￼", "").strip() or None
    except Exception:
        return None


def find_chats_with_handle(
    conn: sqlite3.Connection, handle_ids: list[int]
) -> list[dict]:
    """Return every chat that contains any of the target handle rows."""
    placeholders = ",".join("?" for _ in handle_ids)
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT DISTINCT c.ROWID, c.chat_identifier, c.display_name, c.style
        FROM chat c
        JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
        WHERE chj.handle_id IN ({placeholders})
        ORDER BY c.ROWID ASC
        """,
        handle_ids,
    )
    return [
        {
            "rowid": r[0],
            "identifier": r[1] or "",
            "display_name": r[2] or "",
            "style": r[3],
        }
        for r in cur.fetchall()
    ]


def get_chat_participants(
    conn: sqlite3.Connection, chat_id: int
) -> list[str]:
    """Return the handle IDs (phone numbers / emails) for a chat."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT h.id
        FROM chat_handle_join chj
        JOIN handle h ON h.ROWID = chj.handle_id
        WHERE chj.chat_id = ?
        ORDER BY h.id ASC
        """,
        (chat_id,),
    )
    return [r[0] for r in cur.fetchall() if r[0]]


def fetch_chat_messages(conn: sqlite3.Connection, chat_id: int) -> list[dict]:
    """Fetch every message in a chat, in send order."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            m.ROWID, m.date, m.is_from_me, m.text, m.attributedBody,
            m.service, h.id
        FROM chat_message_join cmj
        JOIN message m ON m.ROWID = cmj.message_id
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE cmj.chat_id = ?
        ORDER BY m.date ASC, m.ROWID ASC
        """,
        (chat_id,),
    )
    rows: list[dict] = []
    for r in cur.fetchall():
        msg_id, msg_date, from_me, msg_text, msg_body, service, sender = r
        text = msg_text
        if not text and msg_body:
            text = extract_text_from_attributed_body(msg_body)
        rows.append(
            {
                "id": msg_id,
                "when": cocoa_to_datetime(msg_date),
                "from_me": bool(from_me),
                "text": text or "",
                "service": service or "",
                "sender": sender or "",
            }
        )
    return rows


def fetch_attachments(conn: sqlite3.Connection, msg_id: int) -> list[dict]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT a.filename, a.mime_type, a.transfer_name
        FROM message_attachment_join maj
        JOIN attachment a ON a.ROWID = maj.attachment_id
        WHERE maj.message_id = ?
        """,
        (msg_id,),
    )
    out: list[dict] = []
    for filename, mime, transfer in cur.fetchall():
        out.append(
            {
                "filename": filename,
                "mime": mime or "",
                "name": transfer or (Path(filename).name if filename else "attachment"),
            }
        )
    return out


def resolve_attachment_path(filename: Optional[str]) -> Optional[Path]:
    """Attachment resolver for the live Mac chat.db: expand ~ and return
    the local path if the file is still on disk."""
    if not filename:
        return None
    expanded = os.path.expanduser(filename)
    p = Path(expanded)
    return p if p.exists() else None


# --- iPhone backup support ----------------------------------------------

def ios_backup_hash(domain: str, relative_path: str) -> str:
    """Compute the fileID hash iOS uses to name a file in a Finder backup."""
    return hashlib.sha1(f"{domain}-{relative_path}".encode("utf-8")).hexdigest()


def _read_backup_metadata(backup_dir: Path) -> dict:
    info: dict = {
        "udid": backup_dir.name,
        "path": backup_dir,
        "device_name": backup_dir.name,
        "encrypted": False,
        "last_modified": datetime.fromtimestamp(
            backup_dir.stat().st_mtime, tz=timezone.utc
        ),
    }
    # Manifest.plist has IsEncrypted + Date + Lockdown{DeviceName};
    # Info.plist (older) has "Device Name" and "Last Backup Date".
    for name in ("Manifest.plist", "Info.plist"):
        plist_path = backup_dir / name
        if not plist_path.exists():
            continue
        try:
            with open(plist_path, "rb") as f:
                meta = plistlib.load(f)
        except Exception:
            continue
        if "IsEncrypted" in meta:
            info["encrypted"] = bool(meta["IsEncrypted"])
        if isinstance(meta.get("Date"), datetime):
            info["last_modified"] = meta["Date"]
        if isinstance(meta.get("Lockdown"), dict):
            dn = meta["Lockdown"].get("DeviceName")
            if dn:
                info["device_name"] = dn
        if isinstance(meta.get("Device Name"), str):
            info["device_name"] = meta["Device Name"]
        if isinstance(meta.get("Last Backup Date"), datetime):
            info["last_modified"] = meta["Last Backup Date"]
    return info


def list_ios_backups(root: Optional[Path] = None) -> list[dict]:
    """Enumerate iPhone/iPad backups under ~/Library/.../MobileSync/Backup."""
    root = root or DEFAULT_BACKUP_ROOT
    if not root.exists():
        return []
    out: list[dict] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        # A real backup has Manifest.db or Manifest.plist.
        if not (entry / "Manifest.plist").exists() and not (entry / "Manifest.db").exists():
            continue
        out.append(_read_backup_metadata(entry))
    out.sort(key=lambda x: x["last_modified"], reverse=True)
    return out


def find_latest_ios_backup(root: Optional[Path] = None) -> Optional[dict]:
    backups = list_ios_backups(root)
    return backups[0] if backups else None


def _open_backup_manifest(backup_dir: Path) -> sqlite3.Connection:
    manifest_db = backup_dir / "Manifest.db"
    if not manifest_db.exists():
        raise FileNotFoundError(
            f"No Manifest.db in {backup_dir}. If Manifest.plist reports "
            "IsEncrypted=true the backup is password-protected and cannot "
            "be read directly."
        )
    return sqlite3.connect(str(manifest_db))


def _backup_file_id(
    manifest: sqlite3.Connection, domain: str, relative_path: str
) -> Optional[str]:
    cur = manifest.cursor()
    cur.execute(
        "SELECT fileID FROM Files WHERE domain = ? AND relativePath = ?",
        (domain, relative_path),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _backup_file_path(backup_dir: Path, file_id: str) -> Path:
    return backup_dir / file_id[:2] / file_id


def extract_sms_db_from_backup(backup_dir: Path, tmpdir: Path) -> Path:
    """Copy sms.db (and its WAL/SHM sidecars, if any) out of an iOS backup."""
    manifest = _open_backup_manifest(backup_dir)
    try:
        sms_id = _backup_file_id(manifest, "HomeDomain", "Library/SMS/sms.db")
        if not sms_id:
            raise FileNotFoundError(
                "The backup at "
                f"{backup_dir} does not list Library/SMS/sms.db. Make sure "
                "the backup finished and included the phone's messages."
            )
        src = _backup_file_path(backup_dir, sms_id)
        if not src.exists():
            raise FileNotFoundError(
                f"Backup index points at {src} but the file is missing "
                "— the backup is incomplete or encrypted."
            )
        dest = tmpdir / "chat.db"
        shutil.copy2(src, dest)
        for suffix, rel in (
            ("-wal", "Library/SMS/sms.db-wal"),
            ("-shm", "Library/SMS/sms.db-shm"),
        ):
            side_id = _backup_file_id(manifest, "HomeDomain", rel)
            if side_id:
                side_src = _backup_file_path(backup_dir, side_id)
                if side_src.exists():
                    try:
                        shutil.copy2(side_src, tmpdir / f"chat.db{suffix}")
                    except OSError:
                        pass
        return dest
    finally:
        manifest.close()


class BackupAttachmentResolver:
    """Attachment resolver for an iPhone backup.

    Given an ``attachment.filename`` value from sms.db (an iOS-side path
    like ``~/Library/SMS/Attachments/.../IMG_0001.jpg``), look the file
    up in Manifest.db, copy it out of the backup on first use, and
    return the local path. Results are cached so a repeat attachment
    isn't re-copied. This is a callable so it drops into the same slot
    as ``resolve_attachment_path``.
    """

    def __init__(self, backup_dir: Path, tmpdir: Path):
        self.backup_dir = backup_dir
        self.dest_root = tmpdir / "backup_attachments"
        self.dest_root.mkdir(exist_ok=True)
        self.manifest = _open_backup_manifest(backup_dir)
        self._cache: dict[str, Optional[Path]] = {}

    def close(self) -> None:
        try:
            self.manifest.close()
        except Exception:
            pass

    def __call__(self, filename: Optional[str]) -> Optional[Path]:
        if not filename:
            return None
        if filename in self._cache:
            return self._cache[filename]
        rel = filename
        if rel.startswith("~/"):
            rel = rel[2:]
        # SMS attachments live under MediaDomain on modern iOS; older
        # backups sometimes carry them under HomeDomain. Try both, and
        # try with and without a leading "Library/" segment because
        # relativePath in Manifest.db is stored without the "~/".
        candidates: list[tuple[str, str]] = [
            ("MediaDomain", rel),
            ("HomeDomain", rel),
        ]
        if rel.startswith("Library/"):
            trimmed = rel[len("Library/") :]
            candidates.append(("MediaDomain", trimmed))
            candidates.append(("HomeDomain", trimmed))
        for domain, path in candidates:
            file_id = _backup_file_id(self.manifest, domain, path)
            if not file_id:
                continue
            src = _backup_file_path(self.backup_dir, file_id)
            if not src.exists():
                continue
            ext = Path(filename).suffix
            dest = self.dest_root / (file_id + ext)
            if not dest.exists():
                try:
                    shutil.copy2(src, dest)
                except OSError:
                    continue
            self._cache[filename] = dest
            return dest
        self._cache[filename] = None
        return None


# --- end iPhone backup support ------------------------------------------



def convert_heic_to_jpeg(src: Path, tmpdir: Path) -> Optional[Path]:
    """Try to decode a HEIC file to a JPEG in a temp directory. Returns None on failure."""
    try:
        from PIL import Image as PILImage  # type: ignore

        try:
            import pillow_heif  # type: ignore

            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        img = PILImage.open(src)
        img = img.convert("RGB")
        out = tmpdir / (src.stem + ".jpg")
        img.save(out, "JPEG", quality=85)
        return out
    except Exception:
        return None


def is_image(mime: str, path: Path) -> bool:
    if mime.startswith("image/"):
        return True
    return path.suffix.lower() in IMAGE_EXTS | HEIC_EXTS


def flowable_image(path: Path, tmpdir: Path, max_width: float, max_height: float):
    """Return a ReportLab Image flowable scaled to fit, or None if unrenderable."""
    src = path
    if path.suffix.lower() in HEIC_EXTS:
        converted = convert_heic_to_jpeg(path, tmpdir)
        if converted is None:
            return None
        src = converted
    try:
        img = Image(str(src))
    except Exception:
        # ReportLab can't decode this (e.g. unsupported format). Try Pillow.
        try:
            from PIL import Image as PILImage  # type: ignore

            pil = PILImage.open(path).convert("RGB")
            out = tmpdir / (path.stem + ".jpg")
            pil.save(out, "JPEG", quality=85)
            img = Image(str(out))
        except Exception:
            return None
    iw, ih = img.imageWidth, img.imageHeight
    if not iw or not ih:
        return img
    scale = min(max_width / iw, max_height / ih, 1.0)
    img.drawWidth = iw * scale
    img.drawHeight = ih * scale
    return img


def escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def format_stamp(dt: Optional[datetime]) -> str:
    if dt is None:
        return "(unknown time)"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z").strip()


def _make_styles():
    styles = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title", parent=styles["Title"], fontSize=16, spaceAfter=6
        ),
        "subtitle": ParagraphStyle(
            "subtitle",
            parent=styles["Normal"],
            fontSize=9,
            textColor=HexColor("#666666"),
            spaceAfter=18,
        ),
        "section": ParagraphStyle(
            "section",
            parent=styles["Heading2"],
            fontSize=13,
            spaceBefore=6,
            spaceAfter=2,
            textColor=HexColor("#111111"),
        ),
        "section_meta": ParagraphStyle(
            "section_meta",
            parent=styles["Normal"],
            fontSize=9,
            textColor=HexColor("#555555"),
            spaceAfter=12,
        ),
        "stamp_me": ParagraphStyle(
            "stamp_me",
            parent=styles["Normal"],
            fontSize=8,
            textColor=HexColor("#888888"),
            spaceAfter=2,
            alignment=TA_RIGHT,
        ),
        "stamp_them": ParagraphStyle(
            "stamp_them",
            parent=styles["Normal"],
            fontSize=8,
            textColor=HexColor("#888888"),
            spaceAfter=2,
            alignment=TA_LEFT,
        ),
        "me": ParagraphStyle(
            "me",
            parent=styles["Normal"],
            fontSize=11,
            leading=14,
            leftIndent=1.4 * inch,
            alignment=TA_RIGHT,
            textColor=HexColor("#0b5cff"),
            spaceAfter=10,
        ),
        "them": ParagraphStyle(
            "them",
            parent=styles["Normal"],
            fontSize=11,
            leading=14,
            rightIndent=1.4 * inch,
            alignment=TA_LEFT,
            textColor=HexColor("#111111"),
            spaceAfter=10,
        ),
        "note": ParagraphStyle(
            "note",
            parent=styles["Normal"],
            fontSize=9,
            textColor=HexColor("#888888"),
            spaceAfter=6,
        ),
    }


def _render_message(
    msg: dict,
    conn: sqlite3.Connection,
    styles: dict,
    tmpdir: Path,
    image_max_w: float,
    image_max_h: float,
    show_sender: bool,
    fallback_sender: str,
    resolve_attachment: AttachmentResolver,
) -> KeepTogether:
    stamp = format_stamp(msg["when"])
    who = "Me" if msg["from_me"] else (msg["sender"] or fallback_sender)
    header_bits = [escape_html(stamp)]
    if show_sender or msg["from_me"]:
        header_bits.append(escape_html(who))
    if msg["service"]:
        header_bits.append(escape_html(msg["service"]))
    header = " &middot; ".join(header_bits)
    block: list = [
        Paragraph(
            header,
            styles["stamp_me"] if msg["from_me"] else styles["stamp_them"],
        )
    ]
    body_style = styles["me"] if msg["from_me"] else styles["them"]
    if msg["text"]:
        text_html = escape_html(msg["text"]).replace("\n", "<br/>")
        block.append(Paragraph(text_html, body_style))
    attachments = fetch_attachments(conn, msg["id"])
    for att in attachments:
        path = resolve_attachment(att["filename"])
        if path and is_image(att["mime"], path):
            img = flowable_image(path, tmpdir, image_max_w, image_max_h)
            if img is not None:
                block.append(img)
                block.append(Spacer(1, 4))
                continue
        label = att["name"] or "attachment"
        mime = att["mime"] or "unknown type"
        missing = " (file not found)" if not path else ""
        block.append(
            Paragraph(
                f"[Attachment: {escape_html(label)} &middot; "
                f"{escape_html(mime)}{missing}]",
                styles["note"],
            )
        )
    if not msg["text"] and not attachments:
        block.append(Paragraph("[empty message]", styles["note"]))
    return KeepTogether(block)


def build_pdf(
    conn: sqlite3.Connection,
    contact: str,
    direct_messages: list[dict],
    group_sections: list[dict],
    output: Path,
    resolve_attachment: AttachmentResolver,
    source_label: str = "",
) -> None:
    """Write the PDF.

    group_sections: [{"chat": <chat row>, "others": [handle ids],
                       "messages": [...]}, ...]
    """
    doc = SimpleDocTemplate(
        str(output),
        pagesize=LETTER,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        title=f"Messages with {contact}",
        author="textdownload",
    )
    content_width = doc.width
    styles = _make_styles()

    total_msgs = len(direct_messages) + sum(
        len(g["messages"]) for g in group_sections
    )

    story: list = []
    story.append(Paragraph(f"Messages with {escape_html(contact)}", styles["title"]))
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()
    subtitle_parts = [
        f"Exported {escape_html(generated)}",
        f"{total_msgs} message(s)",
        f"1 direct thread, {len(group_sections)} group thread(s)",
    ]
    if source_label:
        subtitle_parts.append(f"Source: {escape_html(source_label)}")
    story.append(Paragraph(" &middot; ".join(subtitle_parts), styles["subtitle"]))

    with tempfile.TemporaryDirectory(prefix="textdownload_") as tmp:
        tmpdir = Path(tmp)
        image_max_w = content_width * 0.6
        image_max_h = 4.5 * inch

        # -------- Direct conversation --------
        story.append(Paragraph("Direct Conversation", styles["section"]))
        story.append(
            Paragraph(
                f"Between you and {escape_html(contact)} &middot; "
                f"{len(direct_messages)} message(s)",
                styles["section_meta"],
            )
        )
        if not direct_messages:
            story.append(
                Paragraph(
                    "No direct (1:1) messages found for this number.",
                    styles["note"],
                )
            )
        else:
            for msg in direct_messages:
                story.append(
                    _render_message(
                        msg,
                        conn,
                        styles,
                        tmpdir,
                        image_max_w,
                        image_max_h,
                        show_sender=False,
                        fallback_sender=contact,
                        resolve_attachment=resolve_attachment,
                    )
                )

        # -------- Group conversations --------
        for section in group_sections:
            story.append(PageBreak())
            chat = section["chat"]
            others = section["others"]
            heading = chat["display_name"] or "Group Conversation"
            story.append(Paragraph(escape_html(heading), styles["section"]))
            others_line = (
                "Other participants: " + ", ".join(escape_html(o) for o in others)
                if others
                else "Other participants: (none listed)"
            )
            story.append(
                Paragraph(
                    others_line
                    + f" &middot; {len(section['messages'])} message(s)",
                    styles["section_meta"],
                )
            )
            for msg in section["messages"]:
                story.append(
                    _render_message(
                        msg,
                        conn,
                        styles,
                        tmpdir,
                        image_max_w,
                        image_max_h,
                        show_sender=True,
                        fallback_sender=contact,
                        resolve_attachment=resolve_attachment,
                    )
                )

        doc.build(story)


def prompt_for_phone() -> str:
    try:
        return input("Phone number to export (e.g. +15551234567): ").strip()
    except EOFError:
        return ""


def snapshot_database(src: Path, dest_dir: Path) -> Path:
    """Copy chat.db plus its WAL/SHM sidecars into a temp directory.

    macOS's Messages database runs in WAL journal mode. Recent, still-
    uncheckpointed commits live in chat.db-wal (with chat.db-shm as its
    shared-memory index), so copying only the main file can leave the
    snapshot un-openable — SQLite raises "unable to open database file"
    if the WAL header points at a sidecar that isn't there. Copying the
    sidecars along keeps the snapshot self-consistent.
    """
    dest = dest_dir / "chat.db"
    shutil.copy2(src, dest)
    for suffix in ("-wal", "-shm"):
        sidecar = src.with_name(src.name + suffix)
        if sidecar.exists():
            try:
                shutil.copy2(sidecar, dest_dir / (src.name + suffix))
            except OSError:
                # Sidecar missing or unreadable isn't fatal by itself; we'll
                # still try to open the main file (and fall back to immutable
                # mode if that fails).
                pass
    return dest


def open_snapshot(db_path: Path) -> sqlite3.Connection:
    """Open our local snapshot of chat.db.

    Tries a plain connect first (works for the common case, and lets us
    read the WAL sidecars we copied). Falls back to URI ``immutable=1``
    if that raises CANTOPEN — that flag tells SQLite to treat the file
    as a stand-alone read-only image and ignore WAL/SHM entirely, which
    covers the case where only the main .db was recoverable.
    """
    try:
        conn = sqlite3.connect(str(db_path))
        # Force SQLite to actually open the file so we surface CANTOPEN
        # here rather than at the first real query.
        conn.execute("SELECT 1").fetchone()
        return conn
    except sqlite3.OperationalError:
        pass
    conn = sqlite3.connect(
        f"file:{db_path}?mode=ro&immutable=1", uri=True
    )
    conn.execute("SELECT 1").fetchone()
    return conn


def partition_chats(
    conn: sqlite3.Connection,
    handle_ids: list[int],
    contact: str,
) -> tuple[list[dict], list[dict]]:
    """Split chats into (direct_messages, group_sections).

    A chat is treated as a group if it has more than one non-self
    participant. All direct chats' messages are merged into one
    time-ordered list; each group chat becomes its own section carrying
    the other participants' handles.
    """
    chats = find_chats_with_handle(conn, handle_ids)
    direct_messages: list[dict] = []
    group_sections: list[dict] = []
    for chat in chats:
        participants = get_chat_participants(conn, chat["rowid"])
        others = [
            p for p in participants if not numbers_match(normalize_number(p), contact)
        ]
        messages = fetch_chat_messages(conn, chat["rowid"])
        if len(participants) <= 1:
            direct_messages.extend(messages)
        else:
            group_sections.append(
                {"chat": chat, "others": others, "messages": messages}
            )
    direct_messages.sort(key=lambda m: (m["when"] or datetime.min, m["id"]))
    group_sections.sort(
        key=lambda s: (
            s["messages"][0]["when"] if s["messages"] and s["messages"][0]["when"] else datetime.min,
            s["chat"]["rowid"],
        )
    )
    return direct_messages, group_sections


def _sample_handles(conn: sqlite3.Connection, limit: int = 12) -> list[str]:
    cur = conn.cursor()
    cur.execute("SELECT id FROM handle ORDER BY ROWID LIMIT ?", (limit,))
    return [r[0] for r in cur.fetchall() if r[0]]


def _count_handles(conn: sqlite3.Connection) -> int:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM handle")
    return cur.fetchone()[0]


def _open_source_from_mac(
    db_path: Path, tmpdir: Path
) -> tuple[sqlite3.Connection, AttachmentResolver, str, Optional[BackupAttachmentResolver]]:
    """Return (conn, resolver, source_label, backup_resolver)."""
    mac_dir = tmpdir / "mac"
    mac_dir.mkdir(exist_ok=True)
    snapshot = snapshot_database(db_path, mac_dir)
    conn = open_snapshot(snapshot)
    return conn, resolve_attachment_path, f"Mac Messages DB ({db_path})", None


def _open_source_from_backup(
    backup: dict, tmpdir: Path
) -> tuple[sqlite3.Connection, AttachmentResolver, str, BackupAttachmentResolver]:
    backup_dir: Path = backup["path"]
    dest = tmpdir / f"backup_{backup_dir.name}"
    dest.mkdir(exist_ok=True)
    snapshot = extract_sms_db_from_backup(backup_dir, dest)
    conn = open_snapshot(snapshot)
    resolver = BackupAttachmentResolver(backup_dir, dest)
    when = backup["last_modified"].astimezone().strftime("%Y-%m-%d %H:%M")
    label = f"iPhone backup: {backup['device_name']} @ {when}"
    return conn, resolver, label, resolver


def _print_no_match_hints(
    conn: sqlite3.Connection, phone: str, source_label: str
) -> None:
    total = _count_handles(conn)
    sample = _sample_handles(conn)
    print(
        f"No matching handle for '{phone}' in {source_label}.",
        file=sys.stderr,
    )
    print(
        f"  This source contains {total} handle(s). "
        "Loose matching already tries the number in +country-code and "
        "10-digit forms.",
        file=sys.stderr,
    )
    if sample:
        print("  Sample of handles present in this source:", file=sys.stderr)
        for h in sample:
            print(f"    {h}", file=sys.stderr)
    if total == 0:
        print(
            "  The source is empty. If your phone isn't synced to this Mac's "
            "Messages app, take a Finder backup of your USB-attached phone "
            "and try again (open Finder -> device -> General -> Back Up Now, "
            "with 'Encrypt local backup' UNCHECKED).",
            file=sys.stderr,
        )


def _do_list_backups() -> int:
    backups = list_ios_backups()
    if not backups:
        print(
            f"No iPhone backups found under {DEFAULT_BACKUP_ROOT}.\n"
            "In Finder, select your USB-attached iPhone, then click "
            "'Back Up Now' (uncheck 'Encrypt local backup' to keep it "
            "readable). Re-run --list-backups afterward.",
            file=sys.stderr,
        )
        return 1
    print(f"iPhone/iPad backups under {DEFAULT_BACKUP_ROOT}:")
    for b in backups:
        when = b["last_modified"].astimezone().strftime("%Y-%m-%d %H:%M")
        enc = "encrypted" if b["encrypted"] else "unencrypted"
        print(f"  {when}  {b['device_name']}  [{enc}]  {b['path']}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Export Messages conversations (direct thread + group threads) "
            "with a given phone number to PDF. Reads the Mac's Messages "
            "database by default and automatically falls back to the latest "
            "local iPhone Finder backup if the Mac has no matching thread."
        ),
    )
    parser.add_argument(
        "--phone",
        help="Phone number to export (e.g. +15551234567). Prompted if omitted. "
             "Formatted numbers like 555-123-4567 also work; a leading +1 is "
             "added automatically when matching.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output PDF path. Defaults to messages_<phone>_<timestamp>.pdf",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"Path to Mac chat.db (default: {DEFAULT_DB}).",
    )
    parser.add_argument(
        "--backup-path",
        type=Path,
        help="Path to a specific iPhone backup directory (skips the Mac DB).",
    )
    parser.add_argument(
        "--use-backup",
        action="store_true",
        help="Read from the latest local iPhone backup instead of the Mac DB.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do NOT fall back to the iPhone backup if the Mac DB has no match.",
    )
    parser.add_argument(
        "--list-backups",
        action="store_true",
        help="List iPhone backups this Mac has and exit.",
    )
    args = parser.parse_args(argv)

    if args.list_backups:
        return _do_list_backups()

    phone = args.phone or prompt_for_phone()
    if not phone:
        print("No phone number provided.", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="textdownload_") as tmp:
        tmpdir = Path(tmp)
        backup_resolver: Optional[BackupAttachmentResolver] = None
        conn: Optional[sqlite3.Connection] = None
        source_label = ""
        resolve_attachment: AttachmentResolver = resolve_attachment_path

        try:
            # -------- Decide the initial source --------
            if args.backup_path:
                backup_dir = args.backup_path
                if not backup_dir.exists():
                    print(f"Backup path does not exist: {backup_dir}", file=sys.stderr)
                    return 1
                info = _read_backup_metadata(backup_dir)
                if info["encrypted"]:
                    print(
                        f"Backup at {backup_dir} is encrypted; textdownload "
                        "cannot read encrypted backups. In Finder, uncheck "
                        "'Encrypt local backup' and take a fresh backup.",
                        file=sys.stderr,
                    )
                    return 1
                conn, resolve_attachment, source_label, backup_resolver = (
                    _open_source_from_backup(info, tmpdir)
                )
            elif args.use_backup:
                latest = find_latest_ios_backup()
                if latest is None:
                    print(
                        f"No iPhone backups found under {DEFAULT_BACKUP_ROOT}. "
                        "Take a Finder backup of your USB-attached phone first.",
                        file=sys.stderr,
                    )
                    return 1
                if latest["encrypted"]:
                    print(
                        f"Latest backup ({latest['device_name']}) is encrypted; "
                        "textdownload cannot read encrypted backups. In Finder, "
                        "uncheck 'Encrypt local backup' and take a fresh backup.",
                        file=sys.stderr,
                    )
                    return 1
                conn, resolve_attachment, source_label, backup_resolver = (
                    _open_source_from_backup(latest, tmpdir)
                )
            else:
                # Try Mac chat.db first.
                if not args.db.exists():
                    print(
                        f"Mac Messages database not found at {args.db}.",
                        file=sys.stderr,
                    )
                    if not args.no_backup:
                        print(
                            "Will try the latest iPhone backup instead...",
                            file=sys.stderr,
                        )
                        latest = find_latest_ios_backup()
                        if latest is None or latest["encrypted"]:
                            reason = (
                                "no backups found" if latest is None
                                else "the latest backup is encrypted"
                            )
                            print(
                                f"No usable source: {reason}. Take a Finder "
                                "backup with 'Encrypt local backup' UNCHECKED.",
                                file=sys.stderr,
                            )
                            return 1
                        conn, resolve_attachment, source_label, backup_resolver = (
                            _open_source_from_backup(latest, tmpdir)
                        )
                    else:
                        return 1
                else:
                    try:
                        conn, resolve_attachment, source_label, backup_resolver = (
                            _open_source_from_mac(args.db, tmpdir)
                        )
                    except PermissionError:
                        print(
                            "Permission denied reading the Mac Messages "
                            "database. Grant Full Disk Access to your terminal "
                            "in System Settings -> Privacy & Security, or use "
                            "--use-backup to read the iPhone backup instead.",
                            file=sys.stderr,
                        )
                        return 1
                    except sqlite3.OperationalError as e:
                        print(
                            f"Could not open the Mac Messages database ({e}).",
                            file=sys.stderr,
                        )
                        return 1

            handle_ids = find_handle_ids(conn, phone)

            # -------- Fall back to backup if Mac had no match --------
            if not handle_ids and not args.no_backup and backup_resolver is None:
                print(
                    f"No matching handle for '{phone}' in the Mac Messages "
                    "database; trying the latest iPhone backup...",
                    file=sys.stderr,
                )
                latest = find_latest_ios_backup()
                if latest is None:
                    _print_no_match_hints(conn, phone, source_label)
                    return 1
                if latest["encrypted"]:
                    _print_no_match_hints(conn, phone, source_label)
                    print(
                        f"  (An iPhone backup exists — {latest['device_name']} "
                        f"@ {latest['last_modified']:%Y-%m-%d %H:%M} — but it "
                        "is encrypted. Uncheck 'Encrypt local backup' in "
                        "Finder and take a fresh backup.)",
                        file=sys.stderr,
                    )
                    return 1
                conn.close()
                conn, resolve_attachment, source_label, backup_resolver = (
                    _open_source_from_backup(latest, tmpdir)
                )
                handle_ids = find_handle_ids(conn, phone)

            if not handle_ids:
                _print_no_match_hints(conn, phone, source_label)
                return 1

            direct_messages, group_sections = partition_chats(
                conn, handle_ids, phone
            )
            total = len(direct_messages) + sum(
                len(g["messages"]) for g in group_sections
            )
            if total == 0:
                print(
                    f"Handle for {phone} exists in {source_label} but there "
                    "are no messages associated with it.",
                    file=sys.stderr,
                )
                return 1

            if args.output:
                output = args.output
            else:
                safe_phone = re.sub(r"[^\w+]+", "", phone) or "contact"
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                output = Path.cwd() / f"messages_{safe_phone}_{stamp}.pdf"

            build_pdf(
                conn,
                phone,
                direct_messages,
                group_sections,
                output,
                resolve_attachment=resolve_attachment,
                source_label=source_label,
            )
            print(
                f"Wrote {total} message(s) "
                f"({len(direct_messages)} direct, "
                f"{len(group_sections)} group thread(s)) to {output}",
                file=sys.stdout,
            )
            print(f"Source: {source_label}", file=sys.stdout)
        finally:
            if conn is not None:
                conn.close()
            if backup_resolver is not None:
                backup_resolver.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
