# Telegram Forward Channel Messages

A tool for forwarding messages between Telegram channels. Supports public and private channels, protected content (automatic download + re-upload fallback), media groups (albums), and flexible content filtering

> **Note:** This tool uses a Telegram user account (not a bot) via the MTProto API. Use a dedicated or test account

---

## Requirements

- Python 3.10+
- A Telegram account (used to make API requests)

---

## Installation

```bash
pip install -r requirements.txt
```

---

## Setup

### 1. Configure source and destination

Open `config.py` and set the channels:

```python
SOURCE = "https://t.me/source_channel"       # channel to forward from
DESTINATION = "https://t.me/+invite_hash"     # channel to forward to
```

Supported link formats:

| Format | Type |
|---|---|
| `https://t.me/username` | Public channel |
| `https://t.me/+InviteHash` | Private channel (invite link) |
| `https://t.me/c/1234567890` | Private channel (numeric ID) |
| `Channel Title` | Search by name in your dialogs |

### 2. Set filter mode

```python
MODE = ForwardMode.ALL
```

| Mode | Description |
|---|---|
| `ALL` | All messages |
| `WORDS` | Messages containing specified keywords |
| `PHOTO` | Photos only |
| `DOCUMENT` | Documents only |
| `DOCUMENT_FORMAT` | Documents with specified file extensions |
| `AUDIO` | Audio files only |
| `VIDEO` | Videos only |
| `VOICE` | Voice messages only |

For `WORDS` mode, specify keywords:

```python
WORDS = ["keyword1", "keyword2"]
```

For `DOCUMENT_FORMAT` mode, specify extensions:

```python
FILE_FORMATS = ["pdf", "zip", "rar"]
```

---

## Usage

### Full sync

Forwards the entire history of the source channel to the destination. Messages already present in the destination are skipped automatically

```bash
python bot.py --sync
```

### Monitor mode

Polls the source channel at a regular interval and forwards any new messages as they appear.

```bash
python bot.py
```

Polling interval is configured in `config.py`:

```python
POLL_INTERVAL = 5.0  # seconds
```

### First run

On first launch, Pyrogram will ask you to authorize your Telegram account (phone number + confirmation code). A session file `account.session` will be created - subsequent runs will not require re-authorization

---

## Forwarding strategy

For each message the tool attempts the following in order:

1. **Direct forward** - native Telegram forward (includes "Forwarded from" header)
2. **Server-side copy** - copy without the "Forwarded from" header
3. **Manual re-upload** - downloads media locally and re-sends it (used when the source channel has content protection enabled)

Fallback to the next strategy happens automatically

---

## Project Structure

```
- bot.py             # Entry point - sync or polling mode
- config.py          # Configuration: channels, mode, filter settings
- forward.py         # Core logic - forwarding, filtering, fallback
- requirements.txt   # Python dependencies
- account.session    # Pyrogram session file (auto-created on first run)
```

---

## Disclaimer

This project is provided for educational and personal use only. The author is not responsible for any misuse, legal issues, or damages caused by using this tool.

Users are solely responsible for their own actions. Telegram may block or limit accounts that violate its policies.

---

## License

[MIT](LICENSE)
