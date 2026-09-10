# textdownloadmac

Simple macOS command-line tool for exporting iPhone Messages (iMessage +
SMS) conversations with a given phone number to a PDF, with datetime
stamps and inline images. Both the direct 1:1 thread and every group
chat that includes the number are included, each in its own section.

## Sources it can read

The tool understands three sources:

1. **Mac's live Messages database** at `~/Library/Messages/chat.db`.
   Works when Messages on the Mac is signed in with the same Apple ID
   as your iPhone (or SMS Forwarding is on) so threads are mirrored
   there. Requires **Full Disk Access** for your terminal.

2. **Existing iPhone Finder backup** at
   `~/Library/Application Support/MobileSync/Backup/<UDID>/`.
   Used when the phone is only USB-plugged into the Mac and NOT signed
   in to the same Messages account. The tool reads `Manifest.db`,
   extracts `sms.db`, and pulls image attachments out of the backup on
   demand. Does not require Full Disk Access.

3. **`--via-usb`** — trigger a backup right now over the USB cable, no
   Finder clicks. Uses libimobiledevice's `idevicebackup2` under the
   hood. By default the backup goes into a temp directory that is
   deleted at the end of the run, so nothing is left on the Mac. Pass
   `--usb-cache PATH` to keep an incremental cache directory so future
   runs pull only what has changed (much faster after the first run).

**Default flow (no --via-usb):** try the Mac database first; if it has
no matching handle for the requested number, automatically fall back
to the latest local iPhone backup already on disk.

### Why USB requires a backup at all

Apple deliberately does not expose the Messages database (`sms.db`)
over USB except through the `com.apple.mobilebackup2` service, which
is exactly what Finder uses to make a backup. Every third-party tool
that reads iMessage/SMS off a non-jailbroken iPhone — iMazing,
iExplorer, libimobiledevice — talks to that same service. There is no
"only pull sms.db" API; the initial transfer is always a full backup.
`--via-usb` automates that transfer and cleans up after itself; it
does not sidestep the transfer. If you want to avoid the full copy
entirely, sign in to Messages on this Mac with your Apple ID (option
1) so the threads arrive via iCloud sync instead.

The exported PDF contains:

- A **Direct Conversation** section combining any 1:1 threads with the
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
- A "Source: ..." footer noting which database the export came from.

## Phone-number matching

Numbers are matched loosely — you don't need to guess the format the
database uses:

- Exact E.164 match (`+15551234567`)
- Last-10-digit match, so `5551234567`, `555-123-4567`, `(555) 123-4567`
  and `+15551234567` all resolve to the same contact.

That means giving the tool a bare 10-digit number is enough; it will
still find a stored handle recorded as `+15551234567`.

## Requirements

- macOS with either:
  - Messages set up for the same Apple ID as your iPhone (SMS
    Forwarding is fine), **or**
  - A local Finder backup of the iPhone (below).
- Python 3.9+
- `reportlab` (required) and optionally `pillow` + `pillow-heif` for HEIC
  images

Install dependencies:

```
python3 -m pip install --user -r requirements.txt
```

## Making a Finder backup of a USB-attached iPhone

If your phone is not synced to this Mac's Messages, take a backup:

1. Plug the iPhone into the Mac. On the phone, tap **Trust This
   Computer** if prompted, and enter your passcode.
2. Open **Finder**. Select the iPhone in the sidebar.
3. Under **General → Backups**, choose **Back up all of the data on
   your iPhone to this Mac**.
4. **Uncheck "Encrypt local backup"** — textdownloadmac cannot read
   encrypted backups.
5. Click **Back Up Now** and wait for it to finish.

The backup lands in
`~/Library/Application Support/MobileSync/Backup/<UDID>/`.

> If you have previously enabled encryption and no longer know the
> password, iOS won't let you turn encryption off without setting a new
> password. Set a new one you know, uncheck the box, and take a fresh
> unencrypted backup — the old encrypted one is not needed.

Optional: you can also automate backups with `libimobiledevice`
(`brew install libimobiledevice`) and `idevicebackup2 backup <dir>`.

## Granting Full Disk Access (only needed for the Mac source)

Reading `~/Library/Messages/chat.db` requires **Full Disk Access**.

1. Open **System Settings → Privacy & Security → Full Disk Access**.
2. Click **+** and add the terminal app you use to run the script
   (e.g. Terminal, iTerm, or your IDE).
3. Quit and reopen the terminal.

Without this permission you'll see `operation not permitted` when the
script tries to snapshot `chat.db`. Reading from an iPhone backup does
NOT require this.

## Usage

```
# Prompts for the number, tries Mac chat.db, falls back to latest backup
python3 textdownload.py

# Pass a number directly (any format)
python3 textdownload.py --phone +15551234567
python3 textdownload.py --phone 5551234567
python3 textdownload.py --phone 555-123-4567

# Force reading from the latest iPhone backup (skip Mac chat.db)
python3 textdownload.py --use-backup --phone 5551234567

# Read from a specific backup directory
python3 textdownload.py --backup-path ~/Library/Application\ Support/MobileSync/Backup/<UDID> --phone 5551234567

# Turn OFF backup fallback (Mac chat.db only)
python3 textdownload.py --no-backup --phone 5551234567

# List the backups this Mac has and exit
python3 textdownload.py --list-backups

# Back up over USB right now (deletes backup at end of run)
brew install libimobiledevice          # one-time
python3 textdownload.py --via-usb --phone 5551234567

# Same, but keep an incremental cache for fast repeat runs
python3 textdownload.py --via-usb --usb-cache ~/.cache/textdownloadmac/backup --phone 5551234567

# Custom output filename
python3 textdownload.py --phone 5551234567 --output thread.pdf
```

The default output name is `messages_<phone>_<timestamp>.pdf` in the
current working directory.

## Diagnostics

If the tool can't find a matching handle it prints:

- Which source it looked at
- How many handles that source has
- A sample of the handles present

That tells you whether the source is empty (typical: Mac chat.db when
your phone isn't synced) versus the number genuinely differing from
what's stored.

## Notes and limits

- **Encrypted backups aren't decrypted.** The tool detects them and
  points you at the fix (uncheck "Encrypt local backup" in Finder and
  take a fresh backup).
- **Attributed-body messages.** On Ventura and later some messages
  store text in `attributedBody` (an Apple typedstream blob) rather
  than `text`. There is a best-effort extractor for the common case;
  exotic payloads (rich content, tapbacks, some system messages) may
  come through blank.
- **Group participants** are listed by their raw handle (phone number
  in E.164 form, or email address) — Contacts app names aren't read.
- **Read-only.** The script never writes to the source database; it
  copies it into a temporary directory first and never modifies your
  Mac Messages or your backup.
