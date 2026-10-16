# Telegram Channel Message Forwarder

A Python utility for forwarding messages between Telegram channels using a Telegram user account. It supports public and private channels, media, albums, protected content, and message filtering.

> This tool uses the Telegram MTProto API with a user account, not a bot. A dedicated account is recommended.

## Requirements

- Python 3.10+
- A Telegram account

## Installation

```bash
pip install -r requirements.txt
```

Create the local configuration file:

```terminal
cp .env.example .env
```

Set at least the source, destination, and forwarding mode:

```dotenv
SOURCE=https://t.me/source_channel
DESTINATION=https://t.me/destination_channel
FORWARD_MODE=ALL
```

Channels can be specified by public link, private invite link, numeric private link (`https://t.me/c/...`), or dialog name.

## Filtering

Set `FORWARD_MODE` to one of the following values:

| Mode | Description |
| --- | --- |
| `ALL` | Forward all messages |
| `WORDS` | Forward messages containing one of the configured keywords |
| `PHOTO` | Forward photos only |
| `DOCUMENT` | Forward documents only |
| `DOCUMENT_FORMAT` | Forward documents with selected file extensions |
| `AUDIO` | Forward audio files only |
| `VIDEO` | Forward videos only |
| `VOICE` | Forward voice messages only |

```dotenv
WORDS=keyword1,keyword2
FILE_FORMATS=pdf,zip
POLL_INTERVAL=5
```

Use `WORDS` with `FORWARD_MODE=WORDS` and `FILE_FORMATS` with `FORWARD_MODE=DOCUMENT_FORMAT`.

## Usage

Start the application interactively:

```bash
python bot.py
```

The application provides two modes:

- `Observer` — wait for and forward new messages.
- `One-time` — process channel history once and exit.

Run a mode directly:

```bash
python bot.py --observer
python bot.py --one-time
```

For one-time runs, choose one of these functions:

| Function | Description |
| --- | --- |
| `full-clone` | Process the complete available source history. |
| `from-post` | Process messages from the specified post through the latest post. |
| `single-post` | Process only the specified post. Albums are kept together. |

Choose a delivery strategy explicitly:

- `forward` - use Telegram's native forwarding when available.
- `reupload` - download and send the content again; this also supports protected content.

Examples:

```bash
python bot.py --one-time --function full-clone --strategy forward
python bot.py --one-time --function from-post --start-post https://t.me/source_channel/123 --strategy reupload
python bot.py --one-time --function single-post --post https://t.me/source_channel/123 --strategy reupload
```

On the first run, Pyrogram will request your phone number and confirmation code, then create `account.session` for subsequent runs.

## Notes

- The `--sync` option is retained as an alias for `--one-time`.
- You are responsible for complying with Telegram's terms and applicable copyright laws.

## License

[MIT](LICENSE)
