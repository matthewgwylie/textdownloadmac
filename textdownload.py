#!/usr/bin/env python3
"""
textdownload — Export Messages conversations with a contact to a PDF.

Reads the Mac's local Messages database (~/Library/Messages/chat.db),
which stores iMessage and SMS threads synced from your iPhone. When
your iPhone is USB-attached and Messages on the Mac is signed in with
the same Apple ID (or SMS Forwarding is enabled), the conversation
history is already present here — no separate backup step needed.

The exported PDF contains:
  - A "Direct Conversation" section with the 1:1 thread(s) for the number.
  - A separate section for each group chat that includes the number,
    with the other participants' phone numbers or email handles listed
    in the section header. Every message in a group section is labeled
    with the sender's handle.

Usage:
    python3 textdownload.py                           # prompts for the number
    python3 textdownload.py --phone +15551234567
    python3 textdownload.py --phone 555-123-4567 --output thread.pdf

macOS permissions:
    Reading chat.db requires Full Disk Access for the terminal running
    this script (System Settings -> Privacy & Security -> Full Disk
    Access). If you see "operation not permitted", grant access there
    and retry.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

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
COCOA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

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
    if not filename:
        return None
    expanded = os.path.expanduser(filename)
    p = Path(expanded)
    return p if p.exists() else None


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
        path = resolve_attachment_path(att["filename"])
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
    subtitle = (
        f"Exported {escape_html(generated)} &middot; "
        f"{total_msgs} message(s) &middot; "
        f"1 direct thread, {len(group_sections)} group thread(s)"
    )
    story.append(Paragraph(subtitle, styles["subtitle"]))

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
                    )
                )

        doc.build(story)


def prompt_for_phone() -> str:
    try:
        return input("Phone number to export (e.g. +15551234567): ").strip()
    except EOFError:
        return ""


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the Messages database read-only so we never mutate it."""
    uri = f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


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


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Export Messages conversations (direct thread + group threads) "
            "with a given phone number to PDF."
        ),
    )
    parser.add_argument(
        "--phone",
        help="Phone number to export (e.g. +15551234567). Prompted if omitted.",
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
        help=f"Path to chat.db (default: {DEFAULT_DB}).",
    )
    args = parser.parse_args(argv)

    phone = args.phone or prompt_for_phone()
    if not phone:
        print("No phone number provided.", file=sys.stderr)
        return 2

    if not args.db.exists():
        print(f"Messages database not found at {args.db}.", file=sys.stderr)
        print(
            "On macOS this is normally ~/Library/Messages/chat.db and requires "
            "Full Disk Access for your terminal.",
            file=sys.stderr,
        )
        return 1

    # Work on a snapshot so a locked, live database doesn't block us.
    with tempfile.TemporaryDirectory(prefix="textdownload_db_") as tmp:
        snapshot = Path(tmp) / "chat.db"
        try:
            shutil.copy2(args.db, snapshot)
        except PermissionError:
            print(
                "Permission denied reading the Messages database.\n"
                "Grant Full Disk Access to your terminal in "
                "System Settings -> Privacy & Security, then retry.",
                file=sys.stderr,
            )
            return 1

        conn = open_readonly(snapshot)
        try:
            handle_ids = find_handle_ids(conn, phone)
            if not handle_ids:
                print(
                    f"No conversation found for '{phone}'. "
                    "Try the number in +country-code format (e.g. +15551234567).",
                    file=sys.stderr,
                )
                return 1

            direct_messages, group_sections = partition_chats(
                conn, handle_ids, phone
            )
            total = len(direct_messages) + sum(
                len(g["messages"]) for g in group_sections
            )
            if total == 0:
                print(f"No messages found with {phone}.", file=sys.stderr)
                return 1

            if args.output:
                output = args.output
            else:
                safe_phone = re.sub(r"[^\w+]+", "", phone) or "contact"
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                output = Path.cwd() / f"messages_{safe_phone}_{stamp}.pdf"

            build_pdf(conn, phone, direct_messages, group_sections, output)
            print(
                f"Wrote {total} message(s) "
                f"({len(direct_messages)} direct, "
                f"{len(group_sections)} group thread(s)) to {output}",
                file=sys.stdout,
            )
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
