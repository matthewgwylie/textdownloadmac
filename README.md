# textdownloadmac

Simple macOS command-line tool for exporting Messages (iMessage + SMS)
conversations with a given phone number to a PDF, with datetime stamps
and inline images. Both the direct 1:1 thread and every group chat
that includes the number are included, each in its own section.

## How it works

macOS keeps a local copy of your Messages threads at
`~/Library/Messages/chat.db`. When your iPhone is USB-attached and Messages
on the Mac is signed in with the same Apple ID (or SMS Forwarding is on),
your text history — iMessage and SMS — is already there.
`textdownload.py` reads that database read-only (via a temporary snapshot,
so a live/locked DB isn't a problem), pulls every message with the
requested contact plus their attachments, and lays them out in a PDF with:

- A **Direct Conversation** section combining all 1:1 threads with the
  contact.
- A separate section for **each group chat** that includes the contact,
  headed by the display name (or "Group Conversation") and a line
  listing every other participant's handle (phone number or email).
- Every message carries a datetime stamp. Group-section messages also
  show the sender's handle so you can tell who said what.
- Right-aligned bubbles for messages you sent, left-aligned for the
  other party.
- Inline images (JPEG/PNG/GIF; HEIC too if Pillow + pillow-heif are
  installed).
- A `[Attachment: name · mime]` note for non-image attachments.

## Requirements

- macOS with Messages set up for the same Apple ID as your USB-attached
  iPhone (or SMS Forwarding enabled from the phone)
- Python 3.9+
- `reportlab` (required) and optionally `pillow` + `pillow-heif` for HEIC
  images

Install dependencies:

```
python3 -m pip install --user -r requirements.txt
```

## Granting Full Disk Access

Reading `~/Library/Messages/chat.db` requires **Full Disk Access** on
modern macOS.

1. Open **System Settings → Privacy & Security → Full Disk Access**.
2. Click **+** and add the terminal app you use to run the script
   (e.g. Terminal, iTerm, or your IDE).
3. Quit and reopen the terminal.

Without this permission you will see `operation not permitted` when the
script tries to snapshot `chat.db`.

## Usage

```
# Prompts for the number
python3 textdownload.py

# Or pass it directly
python3 textdownload.py --phone +15551234567

# Custom output file
python3 textdownload.py --phone 555-123-4567 --output thread.pdf

# Point at a different chat.db (e.g. copied from another Mac)
python3 textdownload.py --phone +15551234567 --db /path/to/chat.db
```

Numbers are matched loosely: exact E.164 (`+15551234567`), or a
last-10-digit match for North American numbers, so `555-123-4567`,
`(555) 123-4567`, and `+15551234567` all resolve to the same contact.

The default output name is `messages_<phone>_<timestamp>.pdf` in the
current working directory.

## Notes and limits

- **Attachments live on disk.** The script only renders images whose
  files are still present under `~/Library/Messages/Attachments/`. If
  you've cleaned up old attachments, those messages will show a `(file
  not found)` note instead of the image.
- **Attributed-body messages.** On Ventura and later some messages
  store text in `attributedBody` (an Apple typedstream blob) rather
  than `text`. There is a best-effort extractor for the common case;
  exotic payloads (rich content, tapbacks, some system messages) may
  come through blank.
- **Group participants** are listed by their raw handle (phone number
  in E.164 form, or email address) — Contacts app names aren't read.
- **Read-only.** The script never writes to `chat.db`; it copies it to
  a temp directory first and opens the copy in SQLite read-only mode.
