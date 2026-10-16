from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from pyrogram import Client, enums
from pyrogram.errors import ChatForwardsRestricted, FloodWait, RPCError
from pyrogram.types import (
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

# Link patterns
_RE_PUBLIC = re.compile(r"https?://t\.me/(?:s/)?([a-zA-Z_][a-zA-Z0-9_]{3,})/?$")
_RE_INVITE = re.compile(r"https?://t\.me/(?:\+|joinchat/)([a-zA-Z0-9_-]+)/?$")
_RE_PRIVATE = re.compile(r"https?://t\.me/c/(\d+)(?:/\d+)?/?$")
_RE_PUBLIC_POST = re.compile(
    r"https?://(?:www\.)?t\.me/(?:s/)?"
    r"([a-zA-Z_][a-zA-Z0-9_]{3,})/(\d+)/?(?:\?.*)?$"
)
_RE_PRIVATE_POST = re.compile(
    r"https?://(?:www\.)?t\.me/c/(\d+)/(\d+)/?(?:\?.*)?$"
)

# Telegram Desktop open-source API credentials
DESKTOP_API_ID = 2040
DESKTOP_API_HASH = "b18441a1ff607e10a989891a5462e627"

logging.getLogger("pyrogram").setLevel(logging.ERROR)

logger = logging.getLogger(__name__)

T = TypeVar("T")

MIB = 1024 * 1024
STANDARD_UPLOAD_LIMIT_MIB = 2000
PREMIUM_UPLOAD_LIMIT_MIB = 4000
SEVEN_ZIP_VOLUME_MIB = 1990


class ForwardMode(IntEnum):
    ALL = 0
    WORDS = 1
    PHOTO = 2
    DOCUMENT = 3
    DOCUMENT_FORMAT = 4
    AUDIO = 5
    VIDEO = 6
    VOICE = 7


class OneTimeStrategy(str, Enum):
    """How one-time history cloning delivers source messages"""

    FORWARD = "FORWARD"
    REUPLOAD = "REUPLOAD"


class IntegrityError(RuntimeError):
    """Raised when a downloaded or uploaded media file fails verification"""


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class ConsoleProgress:
    """Throttled in-place console progress for Pyrogram transfers"""

    def __init__(
        self,
        operation: str,
        file_name: str,
        total_items: int = 1,
    ) -> None:
        self.operation = operation
        self.file_name = file_name
        self.total_items = total_items
        self.item_number = 1
        self._last_current = -1
        self._last_percent = -1
        self._last_update = 0.0
        self._line_complete = False
        self._printed = False
        self._lock = threading.Lock()

    def __call__(self, current: int, total: int) -> None:
        with self._lock:
            if self._line_complete:
                self.item_number = min(self.item_number + 1, self.total_items)
                self._last_current = -1
                self._last_percent = -1
                self._last_update = 0.0
                self._line_complete = False

            if current < self._last_current:
                self._last_percent = -1
                self._last_update = 0.0

            percent = int(current * 100 / total) if total else 0
            now = time.monotonic()
            complete = bool(total and current >= total)
            if (
                not complete
                and percent == self._last_percent
                and now - self._last_update < 0.25
            ):
                return

            item = ""
            if self.total_items > 1:
                item = f" [{self.item_number}/{self.total_items}]"
            total_text = _format_bytes(total) if total else "unknown"
            percent_text = f"{current * 100 / total:6.2f}%" if total else "   ?  %"
            line = (
                f"{self.operation}{item} {self.file_name}: "
                f"{_format_bytes(current)} / {total_text} ({percent_text})"
            )
            print(
                "\r" + line.ljust(120),
                end="\n" if complete else "",
                flush=True,
            )
            self._printed = True
            self._line_complete = complete
            self._last_current = current
            self._last_percent = percent
            self._last_update = now

    def finish(self) -> None:
        with self._lock:
            if self._printed and not self._line_complete:
                print(flush=True)
            self._line_complete = True


class ChannelForwarder:
    """Forwards messages between Telegram channels with protected-content support

    Forwarding strategy (per message):
      1. forward_messages  — direct forward (keeps "Forwarded from" header)
      2. copy_message      — server-side copy (no header, but still server-side)
      3. manual resend     — download media locally -> re-upload to destination

    Steps 2/3 are used automatically when the source channel restricts forwarding
    """

    def __init__(
        self,
        source: str,
        destination: str,
        mode: ForwardMode = ForwardMode.ALL,
        words: list[str] | None = None,
        file_formats: list[str] | None = None,
        session_name: str = "account",
        clone_state_file: str = ".clone-state.sqlite3",
        temp_download_dir: str = ".clone-temp",
        seven_zip_path: str | None = None,
    ):
        self._source_input = source
        self._destination_input = destination
        self.mode = mode
        self.words = [w.lower() for w in (words or [])]
        self.file_formats = [f.lower() for f in (file_formats or [])]
        self.session_name = session_name
        self.clone_state_file = Path(clone_state_file)
        self.temp_download_dir = Path(temp_download_dir).expanduser()
        self.seven_zip_path = seven_zip_path

        self.app: Client | None = None
        self._source_id: int = 0
        self._dest_id: int = 0
        self._me_id: int = 0
        self._dest_chat: Any = None
        self._source_protected: bool = False
        self._upload_limit_bytes = STANDARD_UPLOAD_LIMIT_MIB * MIB
        self._forwarded_ids: set[int] = set()
        self._existing_source_ids: set[int] = set()  # source msg ids already in dest
        self._last_checked_id: int = 0

    # Lifecycle

    async def start(self) -> None:
        """Authenticate, resolve source/destination, detect protection"""
        self.app = Client(
            self.session_name,
            api_id=DESKTOP_API_ID,
            api_hash=DESKTOP_API_HASH,
        )
        await self.app.start()

        me = await self.app.get_me()
        self._me_id = me.id
        logger.info("Logged in as %s (id=%d)", me.first_name, me.id)
        upload_limit_mib = (
            PREMIUM_UPLOAD_LIMIT_MIB
            if bool(getattr(me, "is_premium", False))
            else STANDARD_UPLOAD_LIMIT_MIB
        )
        self._upload_limit_bytes = upload_limit_mib * MIB
        logger.info(
            "Telegram upload limit: %d MiB; oversized documents use "
            "%d MiB 7-Zip volumes",
            upload_limit_mib,
            SEVEN_ZIP_VOLUME_MIB,
        )

        # Resolve source
        self._source_id = await self._resolve_chat(self._source_input)
        src = await self.app.get_chat(self._source_id)
        self._source_protected = bool(
            getattr(src, "has_protected_content", False)
        )
        logger.info(
            "Source: '%s' (id=%d, protected=%s)",
            src.title, src.id, self._source_protected,
        )

        # Resolve destination
        self._dest_id = await self._resolve_chat(self._destination_input)
        dst = await self.app.get_chat(self._dest_id)
        self._dest_chat = dst
        logger.info("Destination: '%s' (id=%d)", dst.title, dst.id)

        # Build set of source message IDs already present in destination
        await self._scan_existing()

    async def native_forward_availability(self) -> tuple[bool, str]:
        """Check native-forward prerequisites without sending a test message."""
        if self._source_protected:
            return (
                False,
                "the source channel has protected content enabled",
            )

        try:
            member = await self.app.get_chat_member(
                self._dest_id,
                self._me_id,
            )
        except RPCError as exc:
            return False, f"destination posting permission check failed: {exc}"

        if member.status in {
            enums.ChatMemberStatus.LEFT,
            enums.ChatMemberStatus.BANNED,
        }:
            return False, "the account is not a member of the destination"

        if self._dest_chat.type == enums.ChatType.CHANNEL:
            if member.status == enums.ChatMemberStatus.OWNER:
                return True, "source forwarding and destination posting are available"
            if (
                member.status == enums.ChatMemberStatus.ADMINISTRATOR
                and member.privileges
                and member.privileges.can_post_messages
            ):
                return True, "source forwarding and destination posting are available"
            return False, "the account cannot post messages in the destination channel"

        if member.status == enums.ChatMemberStatus.RESTRICTED:
            permissions = member.permissions
            if not permissions or not (
                permissions.all_perms
                or permissions.can_send_messages
                or permissions.can_send_plain
            ):
                return False, "the account cannot send messages in the destination"

        return True, "source forwarding and destination posting are available"

    async def resolve_source_post_link(self, link: str) -> int:
        """Validate a source post link and return its inclusive start ID."""
        start_message_id, _ = await self.resolve_source_post_range(link)
        return start_message_id

    async def resolve_source_post_range(self, link: str) -> tuple[int, int]:
        """Validate a source post link and return its complete post ID range"""
        value = link.strip()
        private_match = _RE_PRIVATE_POST.fullmatch(value)
        public_match = _RE_PUBLIC_POST.fullmatch(value)

        if private_match:
            linked_chat_id = int(f"-100{private_match.group(1)}")
            message_id = int(private_match.group(2))
        elif public_match:
            linked_chat = await self.app.get_chat(public_match.group(1))
            linked_chat_id = linked_chat.id
            message_id = int(public_match.group(2))
        else:
            raise ValueError(
                "Invalid source post link. Expected "
                "https://t.me/channel/message_id or "
                "https://t.me/c/channel_id/message_id"
            )

        if linked_chat_id != self._source_id:
            raise ValueError("The post link does not belong to the source channel")

        message = await self.app.get_messages(
            self._source_id,
            message_id,
            replies=0,
        )
        if message is None or getattr(message, "empty", False):
            raise ValueError(f"Source post {message_id} does not exist")

        start_message_id = message.id
        end_message_id = message.id
        if message.media_group_id:
            try:
                group = await self.app.get_media_group(
                    self._source_id,
                    message.id,
                )
            except (RPCError, ValueError):
                group = [message]
            start_message_id = min(item.id for item in group)
            end_message_id = max(item.id for item in group)

        return start_message_id, end_message_id

    async def stop(self) -> None:
        if self.app:
            await self.app.stop()
            logger.info("Client stopped")

    async def _resolve_chat(self, identifier: str) -> int:
        """Resolve a Telegram link / username / numeric ID to a chat id"""
        identifier = identifier.strip()

        # Parse known link patterns
        m = _RE_PRIVATE.match(identifier)
        if m:
            # t.me/c/<id> — prepend -100 to get the real supergroup/channel id
            chat_id = int(f"-100{m.group(1)}")
            chat = await self.app.get_chat(chat_id)
            return chat.id

        m = _RE_INVITE.match(identifier)
        if m:
            # Private invite link — first try get_chat, then join_chat
            try:
                chat = await self.app.get_chat(identifier)
                chat_id = getattr(chat, "id", None)
                if chat_id is not None:
                    return chat_id
            except (RPCError, KeyError, ValueError):
                pass
            chat = await self.app.join_chat(identifier)
            return chat.id

        m = _RE_PUBLIC.match(identifier)
        if m:
            username = m.group(1)
            chat = await self.app.get_chat(username)
            return chat.id

        # Strip leading @ if present
        if identifier.startswith("@"):
            identifier = identifier[1:]

        # Try direct resolution (username or numeric id)
        try:
            chat = await self.app.get_chat(identifier)
            return chat.id
        except (RPCError, KeyError, ValueError):
            pass

        # Numeric ID as string
        try:
            numeric = int(identifier)
            chat = await self.app.get_chat(numeric)
            return chat.id
        except (ValueError, RPCError):
            pass

        # Fallback: search own dialogs by exact title
        logger.info("Searching dialogs for '%s'...", identifier)
        async for dialog in self.app.get_dialogs():
            title = dialog.chat.title or ""
            if identifier.lower() == title.lower():
                logger.info("Resolved '%s' -> id=%d", identifier, dialog.chat.id)
                return dialog.chat.id

        raise ValueError(f"Chat '{identifier}' not found")

    async def _scan_existing(self) -> None:
        """Scan destination history to find which source messages are already there

        Checks `forward_from_message_id` (for forwarded posts) and compares
        text fingerprints (for copied/re-sent posts)
        """
        logger.info("Scanning destination for already-forwarded messages...")
        count = 0
        async for msg in self.app.get_chat_history(self._dest_id):
            if msg.service:
                continue
            fwd_id = getattr(msg, "forward_from_message_id", None)
            fwd_chat = getattr(msg, "forward_from_chat", None)
            if fwd_id and fwd_chat and fwd_chat.id == self._source_id:
                self._existing_source_ids.add(fwd_id)
                count += 1
        logger.info("Found %d existing forwarded messages in destination", count)

    # Durable state for local re-uploads

    @staticmethod
    def _ensure_clone_state_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS verified_transfers (
                source_chat_id INTEGER NOT NULL,
                destination_chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                destination_message_id INTEGER NOT NULL,
                sha256 TEXT,
                verification TEXT NOT NULL,
                PRIMARY KEY (
                    source_chat_id,
                    destination_chat_id,
                    source_message_id
                )
            )
            """
        )

    def _load_clone_state(self) -> set[int]:
        """Load verified source message IDs for this source/destination pair."""
        self.clone_state_file.parent.mkdir(parents=True, exist_ok=True)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.clone_state_file)
            self._ensure_clone_state_schema(connection)
            rows = connection.execute(
                """
                SELECT source_message_id
                FROM verified_transfers
                WHERE source_chat_id = ? AND destination_chat_id = ?
                """,
                (self._source_id, self._dest_id),
            ).fetchall()
            connection.commit()
            return {row[0] for row in rows}
        except (OSError, sqlite3.DatabaseError) as exc:
            raise ValueError(
                f"Cannot read clone state {self.clone_state_file}: {exc}"
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    def _record_verified_clone(
        self,
        source_message_id: int,
        destination_message_id: int,
        sha256: str | None,
        verification: str,
    ) -> None:
        self._record_verified_clones(
            [
                (
                    source_message_id,
                    destination_message_id,
                    sha256,
                    verification,
                )
            ]
        )

    def _record_verified_clones(
        self,
        records: list[tuple[int, int, str | None, str]],
    ) -> None:
        """Persist completed items in one transaction so runs can resume."""
        if not records:
            return
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.clone_state_file)
            self._ensure_clone_state_schema(connection)
            connection.executemany(
                """
                INSERT OR REPLACE INTO verified_transfers (
                    source_chat_id,
                    destination_chat_id,
                    source_message_id,
                    destination_message_id,
                    sha256,
                    verification
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        self._source_id,
                        self._dest_id,
                        source_message_id,
                        destination_message_id,
                        sha256,
                        verification,
                    )
                    for (
                        source_message_id,
                        destination_message_id,
                        sha256,
                        verification,
                    ) in records
                ],
            )
            connection.commit()
        except (OSError, sqlite3.DatabaseError) as exc:
            raise IntegrityError(
                f"Cannot save clone state {self.clone_state_file}: {exc}"
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    # Filtering

    def _matches_filter(self, msg: Message) -> bool:
        if self.mode == ForwardMode.ALL:
            return True
        if self.mode == ForwardMode.WORDS:
            text = (msg.text or msg.caption or "").lower()
            return any(w in text for w in self.words)
        if self.mode == ForwardMode.PHOTO:
            return msg.photo is not None
        if self.mode == ForwardMode.DOCUMENT:
            return msg.document is not None
        if self.mode == ForwardMode.DOCUMENT_FORMAT:
            if msg.document and msg.document.file_name:
                return msg.document.file_name.rsplit(".", 1)[-1].lower() in self.file_formats
            return False
        if self.mode == ForwardMode.AUDIO:
            return msg.audio is not None
        if self.mode == ForwardMode.VIDEO:
            return msg.video is not None
        if self.mode == ForwardMode.VOICE:
            return msg.voice is not None
        return False

    # Sending strategies

    async def _send_single(self, msg: Message) -> None:
        """Forward -> copy -> manual resend, with automatic fallback"""

        if not self._source_protected:
            try:
                await self.app.forward_messages(
                    self._dest_id, self._source_id, msg.id,
                )
                logger.info("Forwarded msg %d", msg.id)
                return
            except ChatForwardsRestricted:
                self._source_protected = True
                logger.info(
                    "Forwarding restricted — switching to copy/resend",
                )

        try:
            await self.app.copy_message(
                self._dest_id, self._source_id, msg.id,
            )
            logger.info("Copied msg %d", msg.id)
            return
        except ChatForwardsRestricted:
            pass

        await self._manual_resend(msg)
        logger.info("Re-sent msg %d (manual download)", msg.id)

    async def _send_group(self, messages: list[Message]) -> None:
        """Handle a media-group (album) with the same fallback chain"""
        gid = messages[0].media_group_id
        ids = [m.id for m in messages]

        # Direct forward
        if not self._source_protected:
            try:
                await self.app.forward_messages(
                    self._dest_id, self._source_id, ids,
                )
                logger.info("Forwarded media group %s", gid)
                return
            except ChatForwardsRestricted:
                self._source_protected = True

        # Server-side copy
        try:
            await self.app.copy_media_group(
                self._dest_id, self._source_id, messages[0].id,
            )
            logger.info("Copied media group %s", gid)
            return
        except (ChatForwardsRestricted, RPCError):
            pass

        # Manual download + re-upload
        await self._manual_resend_group(messages)
        logger.info("Re-sent media group %s (manual)", gid)

    # Manual resend helpers

    async def _manual_resend(self, msg: Message) -> None:
        """Download media and recreate the message in the destination"""
        text = msg.text or ""
        caption = msg.caption or ""

        # Text-only message
        if not msg.media:
            if text:
                await self.app.send_message(
                    self._dest_id, text, entities=msg.entities,
                )
            return

        tmp = tempfile.mkdtemp(prefix="tgfwd_")
        try:
            path = await self.app.download_media(
                msg, file_name=os.path.join(tmp, ""),
            )
            if not path:
                # Fallback: send text/caption without media
                content = text or caption
                if content:
                    await self.app.send_message(
                        self._dest_id, content,
                        entities=msg.entities or msg.caption_entities,
                    )
                return

            ce = msg.caption_entities

            if msg.photo:
                await self.app.send_photo(
                    self._dest_id, path,
                    caption=caption, caption_entities=ce,
                )
            elif msg.video:
                v = msg.video
                await self.app.send_video(
                    self._dest_id, path,
                    caption=caption, caption_entities=ce,
                    duration=v.duration, width=v.width, height=v.height,
                )
            elif msg.document:
                await self.app.send_document(
                    self._dest_id, path,
                    caption=caption, caption_entities=ce,
                )
            elif msg.audio:
                a = msg.audio
                await self.app.send_audio(
                    self._dest_id, path,
                    caption=caption, caption_entities=ce,
                    duration=a.duration, performer=a.performer, title=a.title,
                )
            elif msg.voice:
                await self.app.send_voice(
                    self._dest_id, path,
                    caption=caption, caption_entities=ce,
                )
            elif msg.video_note:
                vn = msg.video_note
                await self.app.send_video_note(
                    self._dest_id, path,
                    duration=vn.duration, length=vn.length,
                )
            elif msg.animation:
                await self.app.send_animation(
                    self._dest_id, path,
                    caption=caption, caption_entities=ce,
                )
            elif msg.sticker:
                await self.app.send_sticker(self._dest_id, path)
            else:
                # Fallback: send as document
                await self.app.send_document(
                    self._dest_id, path,
                    caption=caption, caption_entities=ce,
                )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def _manual_resend_group(self, messages: list[Message]) -> None:
        """Download and re-send a media group as an album"""
        media_inputs: list = []
        temp_dirs: list[str] = []

        for i, m in enumerate(messages):
            tmp = tempfile.mkdtemp(prefix="tgfwd_")
            temp_dirs.append(tmp)
            path = await self.app.download_media(
                m, file_name=os.path.join(tmp, ""),
            )
            if not path:
                continue

            cap = m.caption if i == 0 else ""
            ce = m.caption_entities if i == 0 else None

            if m.photo:
                media_inputs.append(
                    InputMediaPhoto(path, caption=cap, caption_entities=ce),
                )
            elif m.video:
                media_inputs.append(
                    InputMediaVideo(path, caption=cap, caption_entities=ce),
                )
            elif m.audio:
                media_inputs.append(
                    InputMediaAudio(path, caption=cap, caption_entities=ce),
                )
            else:
                media_inputs.append(
                    InputMediaDocument(path, caption=cap, caption_entities=ce),
                )

        try:
            if media_inputs:
                await self.app.send_media_group(self._dest_id, media_inputs)
        finally:
            for d in temp_dirs:
                shutil.rmtree(d, ignore_errors=True)

    # Verified, sequential local cloning

    def _make_temp_dir(self, prefix: str) -> str:
        self.temp_download_dir.mkdir(parents=True, exist_ok=True)
        return tempfile.mkdtemp(
            prefix=prefix,
            dir=str(self.temp_download_dir.resolve()),
        )

    @staticmethod
    def _remove_temp_dir(path: str) -> None:
        """Remove temporary data on success, failure or cancellation."""
        if not os.path.exists(path):
            return

        last_error: OSError | None = None

        def remove_readonly(function, item_path, _error_info) -> None:
            os.chmod(item_path, stat.S_IWRITE)
            function(item_path)

        for attempt in range(3):
            try:
                shutil.rmtree(path, onerror=remove_readonly)
                return
            except OSError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.25 * (attempt + 1))

        raise RuntimeError(
            f"Cannot remove temporary directory {path}: {last_error}"
        ) from last_error

    def _cleanup_stale_clone_dirs(self) -> None:
        """Remove temporary clone directories left by an interrupted process."""
        self.temp_download_dir.mkdir(parents=True, exist_ok=True)
        root = self.temp_download_dir.resolve()
        for candidate in root.iterdir():
            if not candidate.name.startswith("tgclone_"):
                continue
            resolved = candidate.resolve()
            if resolved.parent != root or not resolved.is_dir():
                continue
            logger.warning("Removing stale temporary directory: %s", resolved)
            self._remove_temp_dir(str(resolved))

    @staticmethod
    def _media_display_name(msg: Message, kind: str, media: Any) -> str:
        file_name = getattr(media, "file_name", None)
        if file_name:
            return os.path.basename(file_name)
        extensions = {
            "photo": ".jpg",
            "video": ".mp4",
            "audio": ".mp3",
            "voice": ".ogg",
            "video_note": ".mp4",
            "animation": ".mp4",
            "sticker": ".webp",
        }
        return f"{kind}_{msg.id}{extensions.get(kind, '')}"

    async def _retry_flood_wait(
        self,
        operation: Callable[[], Awaitable[T]],
        message_id: int,
    ) -> T:
        while True:
            try:
                return await operation()
            except FloodWait as exc:
                wait = getattr(exc, "value", getattr(exc, "x", 30))
                logger.warning(
                    "FloodWait %ds - retrying operation for msg %d",
                    wait,
                    message_id,
                )
                await asyncio.sleep(wait)

    @staticmethod
    def _file_media(msg: Message) -> tuple[str, Any] | None:
        for kind in (
            "photo",
            "video",
            "document",
            "audio",
            "voice",
            "video_note",
            "animation",
            "sticker",
        ):
            media = getattr(msg, kind, None)
            if media is not None:
                return kind, media
        return None

    @staticmethod
    async def _sha256(path: str) -> str:
        """Hash a file while keeping cancellation and cleanup responsive."""
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                await asyncio.sleep(0)
        return digest.hexdigest()

    def _resolve_seven_zip(self) -> str:
        candidates: list[str] = []
        if self.seven_zip_path:
            candidates.append(self.seven_zip_path)
        candidates.extend((
            "7z",
            "7zz",
            "7za",
            r"C:\Program Files\7-Zip\7z.exe",
            r"C:\Program Files (x86)\7-Zip\7z.exe",
        ))

        for candidate in candidates:
            expanded = os.path.expandvars(os.path.expanduser(candidate))
            if os.path.isfile(expanded):
                return os.path.abspath(expanded)
            executable = shutil.which(expanded)
            if executable:
                return executable

        raise RuntimeError(
            "7-Zip is required to split oversized documents. Install 7-Zip "
            "or set SEVEN_ZIP_PATH in .env."
        )

    async def _run_seven_zip(
        self,
        arguments: list[str],
        cwd: str,
    ) -> None:
        executable = self._resolve_seven_zip()
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                *arguments,
                cwd=cwd,
            )
        except OSError as exc:
            raise RuntimeError(
                f"Cannot start 7-Zip executable {executable}: {exc}"
            ) from exc

        try:
            return_code = await process.wait()
        except asyncio.CancelledError:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
            raise
        if return_code != 0:
            raise IntegrityError(
                f"7-Zip exited with code {return_code}: "
                f"{' '.join(arguments)}"
            )

    async def _create_7z_volumes(
        self,
        msg: Message,
        source_path: str,
        temp_dir: str,
    ) -> list[str]:
        original_name = self._media_display_name(
            msg,
            "document",
            msg.document,
        )
        archive_dir = os.path.join(temp_dir, "split_7z")
        os.mkdir(archive_dir)
        source_dir = os.path.dirname(source_path)
        source_name = os.path.basename(source_path)
        safe_archive_name = re.sub(
            r'[<>:"/\\|?*\x00-\x1f]',
            "_",
            original_name,
        ).rstrip(". ") or source_name
        archive_base = os.path.join(
            archive_dir,
            f"{safe_archive_name}.multipart.7z",
        )

        source_size = os.path.getsize(source_path)
        free_space = shutil.disk_usage(archive_dir).free
        required_space = source_size + max(64 * MIB, source_size // 100)
        if free_space < required_space:
            raise IntegrityError(
                "Not enough temporary disk space to create 7-Zip volumes for "
                f"source msg {msg.id}: required about "
                f"{_format_bytes(required_space)}, available "
                f"{_format_bytes(free_space)}"
            )

        logger.info(
            "Creating a store-only 7-Zip archive with %d MiB volumes for %s",
            SEVEN_ZIP_VOLUME_MIB,
            original_name,
        )
        await self._run_seven_zip(
            [
                "a",
                "-t7z",
                "-mx=0",
                f"-v{SEVEN_ZIP_VOLUME_MIB}m",
                "-y",
                "-bsp1",
                "-bb1",
                archive_base,
                source_name,
            ],
            cwd=source_dir,
        )

        volume_prefix = os.path.basename(archive_base) + "."
        volumes = sorted(
            str(item)
            for item in Path(archive_dir).iterdir()
            if item.is_file()
            and item.name.startswith(volume_prefix)
            and item.name[len(volume_prefix):].isdigit()
        )
        if len(volumes) < 2:
            raise IntegrityError(
                f"7-Zip did not create multiple volumes for source msg {msg.id}"
            )
        for volume in volumes:
            volume_size = os.path.getsize(volume)
            if volume_size <= 0 or volume_size > self._upload_limit_bytes:
                raise IntegrityError(
                    f"Invalid 7-Zip volume size for {os.path.basename(volume)}: "
                    f"{volume_size} bytes"
                )

        logger.info("Testing the complete multi-volume 7-Zip archive")
        await self._run_seven_zip(
            ["t", "-y", "-bsp1", "-bb1", volumes[0]],
            cwd=archive_dir,
        )
        return volumes

    async def _send_document_volume_group(
        self,
        msg: Message,
        volumes: list[str],
    ) -> list[Message]:
        if not 2 <= len(volumes) <= 10:
            raise IntegrityError(
                f"Telegram document albums require 2-10 files; "
                f"7-Zip created {len(volumes)} volumes for source msg {msg.id}"
            )

        media = [
            InputMediaDocument(
                volume,
                caption=(msg.caption or "") if index == 0 else "",
                caption_entities=(msg.caption_entities if index == 0 else None),
                file_name=os.path.basename(volume),
            )
            for index, volume in enumerate(volumes)
        ]
        progress = ConsoleProgress(
            "Uploading 7-Zip volume",
            os.path.basename(volumes[0]).rsplit(".", 1)[0],
            total_items=len(volumes),
        )
        try:
            sent_messages = await self.app.send_media_group(
                self._dest_id,
                media,
                progress=progress,
            )
        finally:
            progress.finish()

        sent_messages = list(sent_messages or [])
        if len(sent_messages) != len(volumes):
            raise IntegrityError(
                f"Telegram returned {len(sent_messages)} of {len(volumes)} "
                f"7-Zip volumes for source msg {msg.id}"
            )
        return sent_messages

    async def _verify_uploaded_document_volume(
        self,
        source_msg: Message,
        sent_msg: Message,
        expected_hash: str,
        expected_size: int,
        part_name: str,
        part_number: int,
        part_count: int,
        temp_dir: str,
    ) -> None:
        if getattr(sent_msg, "document", None) is None:
            raise IntegrityError(
                f"Uploaded volume {part_number}/{part_count} of source msg "
                f"{source_msg.id} returned the wrong media type"
            )

        verify_dir = os.path.join(
            temp_dir,
            f"telegram_verify_{source_msg.id}_part_{part_number:03d}",
        )
        os.mkdir(verify_dir)
        progress = ConsoleProgress("Verifying", part_name, part_count)
        progress.item_number = part_number
        try:
            verified_path = await self._retry_flood_wait(
                lambda: self.app.download_media(
                    sent_msg,
                    file_name=verify_dir + os.sep,
                    progress=progress,
                ),
                source_msg.id,
            )
        finally:
            progress.finish()

        if not verified_path or not os.path.isfile(verified_path):
            raise IntegrityError(
                f"Cannot re-download volume {part_number}/{part_count} "
                f"of source msg {source_msg.id}"
            )
        actual_size = os.path.getsize(verified_path)
        if actual_size != expected_size:
            raise IntegrityError(
                f"Uploaded volume {part_number}/{part_count} size mismatch for "
                f"source msg {source_msg.id}: expected={expected_size}, "
                f"actual={actual_size}"
            )

        uploaded_hash = await self._sha256(verified_path)
        if uploaded_hash != expected_hash:
            raise IntegrityError(
                f"SHA-256 mismatch for volume {part_number}/{part_count} of "
                f"source msg {source_msg.id}: local={expected_hash}, "
                f"telegram={uploaded_hash}"
            )

        logger.info(
            "Verified volume %d/%d of msg %d (%d bytes, sha256=%s)",
            part_number,
            part_count,
            source_msg.id,
            expected_size,
            expected_hash,
        )
        # A verified Telegram round-trip no longer needs its local check copy.
        self._remove_temp_dir(verify_dir)

    async def _clone_oversized_document(
        self,
        msg: Message,
        path: str,
        file_size: int,
        temp_dir: str,
    ) -> tuple[Message, str]:
        original_name = self._media_display_name(msg, "document", msg.document)

        logger.warning(
            "Document %s is %s and exceeds the %s account limit; "
            "packing it into verified %d MiB 7-Zip volumes",
            original_name,
            _format_bytes(file_size),
            _format_bytes(self._upload_limit_bytes),
            SEVEN_ZIP_VOLUME_MIB,
        )
        volumes = await self._create_7z_volumes(msg, path, temp_dir)
        sent_messages = await self._retry_flood_wait(
            lambda: self._send_document_volume_group(msg, volumes),
            msg.id,
        )

        for index, (volume, sent) in enumerate(
            zip(volumes, sent_messages),
            start=1,
        ):
            volume_hash = await self._sha256(volume)
            await self._verify_uploaded_document_volume(
                msg,
                sent,
                volume_hash,
                os.path.getsize(volume),
                os.path.basename(volume),
                index,
                len(volumes),
                temp_dir,
            )
            os.remove(volume)

        logger.info(
            "Uploaded and verified oversized msg %d as one %d-volume "
            "7-Zip document album",
            msg.id,
            len(volumes),
        )
        return (
            sent_messages[0],
            f"sha256-7z-volume-roundtrip:{len(volumes)}-parts",
        )

    async def _send_local_file(
        self,
        msg: Message,
        kind: str,
        path: str,
    ) -> Message:
        caption = msg.caption or ""
        entities = msg.caption_entities
        spoiler = bool(getattr(msg, "has_media_spoiler", False))
        media = getattr(msg, kind)
        progress = ConsoleProgress(
            "Uploading",
            self._media_display_name(msg, kind, media),
        )

        try:
            if kind == "photo":
                sent = await self.app.send_photo(
                    self._dest_id,
                    path,
                    caption=caption,
                    caption_entities=entities,
                    has_spoiler=spoiler,
                    progress=progress,
                )
            elif kind == "video":
                sent = await self.app.send_video(
                    self._dest_id,
                    path,
                    caption=caption,
                    caption_entities=entities,
                    has_spoiler=spoiler,
                    duration=media.duration,
                    width=media.width,
                    height=media.height,
                    file_name=media.file_name,
                    supports_streaming=media.supports_streaming,
                    progress=progress,
                )
            elif kind == "document":
                sent = await self.app.send_document(
                    self._dest_id,
                    path,
                    caption=caption,
                    caption_entities=entities,
                    file_name=media.file_name,
                    progress=progress,
                )
            elif kind == "audio":
                sent = await self.app.send_audio(
                    self._dest_id,
                    path,
                    caption=caption,
                    caption_entities=entities,
                    duration=media.duration,
                    performer=media.performer,
                    title=media.title,
                    file_name=media.file_name,
                    progress=progress,
                )
            elif kind == "voice":
                sent = await self.app.send_voice(
                    self._dest_id,
                    path,
                    caption=caption,
                    caption_entities=entities,
                    duration=media.duration,
                    progress=progress,
                )
            elif kind == "video_note":
                sent = await self.app.send_video_note(
                    self._dest_id,
                    path,
                    duration=media.duration,
                    length=media.length,
                    progress=progress,
                )
            elif kind == "animation":
                sent = await self.app.send_animation(
                    self._dest_id,
                    path,
                    caption=caption,
                    caption_entities=entities,
                    has_spoiler=spoiler,
                    duration=media.duration,
                    width=media.width,
                    height=media.height,
                    file_name=media.file_name,
                    progress=progress,
                )
            else:
                sent = await self.app.send_sticker(
                    self._dest_id,
                    path,
                    progress=progress,
                )
        finally:
            progress.finish()

        if sent is None:
            raise IntegrityError(
                f"Telegram returned no message after uploading source msg {msg.id}"
            )
        return sent

    async def _verify_uploaded_file(
        self,
        source_msg: Message,
        sent_msg: Message,
        kind: str,
        source_path: str,
        temp_dir: str,
    ) -> tuple[str, str]:
        """Re-download an upload and verify it before deleting the local file."""
        sent_media = getattr(sent_msg, kind, None)
        if sent_media is None:
            raise IntegrityError(
                f"Uploaded source msg {source_msg.id} returned the wrong media type"
            )

        verify_dir = os.path.join(
            temp_dir,
            f"telegram_verify_{source_msg.id}",
        )
        os.mkdir(verify_dir)
        source_media = getattr(source_msg, kind)
        progress = ConsoleProgress(
            "Verifying",
            self._media_display_name(source_msg, kind, source_media),
        )
        try:
            verified_path = await self._retry_flood_wait(
                lambda: self.app.download_media(
                    sent_msg,
                    file_name=verify_dir + os.sep,
                    progress=progress,
                ),
                source_msg.id,
            )
        finally:
            progress.finish()
        if not verified_path or not os.path.isfile(verified_path):
            raise IntegrityError(
                f"Cannot re-download uploaded source msg {source_msg.id}"
            )
        if os.path.getsize(verified_path) <= 0:
            raise IntegrityError(
                f"Telegram returned an empty upload for source msg {source_msg.id}"
            )

        source_hash = await self._sha256(source_path)
        uploaded_hash = await self._sha256(verified_path)

        if kind != "photo":
            if uploaded_hash != source_hash:
                raise IntegrityError(
                    f"SHA-256 mismatch after uploading source msg {source_msg.id}: "
                    f"local={source_hash}, telegram={uploaded_hash}"
                )
            return source_hash, "sha256-roundtrip"

        source_photo = source_msg.photo
        uploaded_photo = sent_msg.photo
        if (
            source_photo.width != uploaded_photo.width
            or source_photo.height != uploaded_photo.height
        ):
            raise IntegrityError(
                f"Photo dimensions changed for source msg {source_msg.id}: "
                f"{source_photo.width}x{source_photo.height} -> "
                f"{uploaded_photo.width}x{uploaded_photo.height}"
            )

        # Telegram may recompress photos. The successful round-trip and matching
        # dimensions are the strongest meaningful check while keeping photo UX.
        return source_hash, "photo-roundtrip-dimensions"

    async def _clone_file_via_local(
        self,
        msg: Message,
        kind: str,
        media: Any,
    ) -> tuple[Message, str, str]:
        expected_size = getattr(media, "file_size", None)
        temp_dir = self._make_temp_dir(prefix=f"tgclone_{msg.id}_")
        try:
            progress = ConsoleProgress(
                "Downloading",
                self._media_display_name(msg, kind, media),
            )
            try:
                path = await self._retry_flood_wait(
                    lambda: self.app.download_media(
                        msg,
                        file_name=temp_dir + os.sep,
                        progress=progress,
                    ),
                    msg.id,
                )
            finally:
                progress.finish()
            if not path or not os.path.isfile(path):
                raise IntegrityError(f"Download failed for source msg {msg.id}")

            actual_size = os.path.getsize(path)
            if actual_size <= 0:
                raise IntegrityError(f"Downloaded an empty file for source msg {msg.id}")
            if expected_size and actual_size != expected_size:
                raise IntegrityError(
                    f"Downloaded size mismatch for source msg {msg.id}: "
                    f"expected={expected_size}, actual={actual_size}"
                )

            local_hash = await self._sha256(path)
            logger.info(
                "Downloaded msg %d (%d bytes, sha256=%s)",
                msg.id,
                actual_size,
                local_hash,
            )
            if kind == "document" and actual_size > self._upload_limit_bytes:
                sent, verification = await self._clone_oversized_document(
                    msg,
                    path,
                    actual_size,
                    temp_dir,
                )
                source_hash = local_hash
            else:
                sent = await self._retry_flood_wait(
                    lambda: self._send_local_file(msg, kind, path),
                    msg.id,
                )
                source_hash, verification = await self._verify_uploaded_file(
                    msg,
                    sent,
                    kind,
                    path,
                    temp_dir,
                )
            # Commit the resume checkpoint before deleting either local copy.
            self._record_verified_clone(
                msg.id,
                sent.id,
                source_hash,
                verification,
            )
            return sent, source_hash, verification
        finally:
            self._remove_temp_dir(temp_dir)

    async def _clone_photo_album_via_local(
        self,
        messages: list[Message],
    ) -> list[Message]:
        """Download and upload one complete photo post as a media group."""
        ordered = sorted(messages, key=lambda item: item.id)
        temp_dir = self._make_temp_dir(
            prefix=f"tgclone_album_{ordered[0].media_group_id}_"
        )
        paths: list[str] = []
        media_inputs: list[InputMediaPhoto] = []

        try:
            for message in ordered:
                download_dir = os.path.join(temp_dir, f"source_{message.id}")
                os.mkdir(download_dir)
                progress = ConsoleProgress(
                    "Downloading",
                    self._media_display_name(
                        message,
                        "photo",
                        message.photo,
                    ),
                )
                try:
                    path = await self._retry_flood_wait(
                        lambda message=message, download_dir=download_dir: (
                            self.app.download_media(
                                message,
                                file_name=download_dir + os.sep,
                                progress=progress,
                            )
                        ),
                        message.id,
                    )
                finally:
                    progress.finish()
                if not path or not os.path.isfile(path):
                    raise IntegrityError(
                        f"Photo download failed for source msg {message.id}"
                    )

                actual_size = os.path.getsize(path)
                expected_size = getattr(message.photo, "file_size", None)
                if actual_size <= 0:
                    raise IntegrityError(
                        f"Downloaded an empty photo for source msg {message.id}"
                    )
                if expected_size and actual_size != expected_size:
                    raise IntegrityError(
                        f"Downloaded size mismatch for source msg {message.id}: "
                        f"expected={expected_size}, actual={actual_size}"
                    )

                paths.append(path)
                media_inputs.append(
                    InputMediaPhoto(
                        path,
                        caption=message.caption or "",
                        caption_entities=message.caption_entities,
                        has_spoiler=bool(
                            getattr(message, "has_media_spoiler", False)
                        ),
                    )
                )

            upload_progress = ConsoleProgress(
                "Uploading",
                f"photo_album_{ordered[0].media_group_id}",
                total_items=len(ordered),
            )
            try:
                sent_messages = await self._retry_flood_wait(
                    lambda: self.app.send_media_group(
                        self._dest_id,
                        media_inputs,
                        progress=upload_progress,
                    ),
                    ordered[0].id,
                )
            finally:
                upload_progress.finish()
            sent_messages = list(sent_messages or [])
            if len(sent_messages) != len(ordered):
                raise IntegrityError(
                    f"Photo album {ordered[0].media_group_id} returned "
                    f"{len(sent_messages)} of {len(ordered)} messages"
                )

            state_records: list[tuple[int, int, str | None, str]] = []
            for source, sent, path in zip(ordered, sent_messages, paths):
                source_hash, verification = await self._verify_uploaded_file(
                    source,
                    sent,
                    "photo",
                    path,
                    temp_dir,
                )
                state_records.append(
                    (source.id, sent.id, source_hash, verification)
                )

            # The whole album is checkpointed atomically before local cleanup.
            self._record_verified_clones(state_records)
            return sent_messages
        finally:
            self._remove_temp_dir(temp_dir)

    async def _clone_non_file(self, msg: Message) -> Message:
        if msg.text:
            sent = await self.app.send_message(
                self._dest_id,
                msg.text,
                entities=msg.entities,
            )
            if sent is None or sent.text != msg.text:
                raise IntegrityError(f"Text verification failed for source msg {msg.id}")
            return sent

        if msg.poll:
            poll = msg.poll
            return await self.app.send_poll(
                self._dest_id,
                question=poll.question,
                question_entities=poll.question_entities,
                options=poll.options,
                is_anonymous=poll.is_anonymous,
                type=poll.type,
                allows_multiple_answers=poll.allows_multiple_answers,
                correct_option_id=poll.correct_option_id,
                explanation=poll.explanation,
                explanation_entities=poll.explanation_entities,
                is_closed=poll.is_closed,
            )

        if msg.contact:
            contact = msg.contact
            return await self.app.send_contact(
                self._dest_id,
                contact.phone_number,
                contact.first_name,
                last_name=contact.last_name,
                vcard=contact.vcard,
            )

        if msg.venue:
            venue = msg.venue
            return await self.app.send_venue(
                self._dest_id,
                venue.location.latitude,
                venue.location.longitude,
                venue.title,
                venue.address,
                foursquare_id=venue.foursquare_id or "",
                foursquare_type=venue.foursquare_type or "",
            )

        if msg.location:
            location = msg.location
            return await self.app.send_location(
                self._dest_id,
                location.latitude,
                location.longitude,
                horizontal_accuracy=location.horizontal_accuracy,
            )

        if msg.dice:
            return await self.app.send_dice(self._dest_id, msg.dice.emoji)

        # Some Telegram-only objects (for example, games or paid media) have
        # no downloadable file representation. A server-side copy is the only
        # lossless option for those objects.
        sent = await self.app.copy_message(
            self._dest_id,
            self._source_id,
            msg.id,
        )
        if sent is None:
            raise IntegrityError(
                f"Cannot clone unsupported Telegram object in source msg {msg.id}"
            )
        return sent

    # Public API

    async def clone_all(
        self,
        strategy: OneTimeStrategy,
        start_message_id: int | None = None,
        end_message_id: int | None = None,
    ) -> int:
        """Clone available history once using native forwards or local re-uploads."""
        if strategy == OneTimeStrategy.FORWARD:
            return await self._forward_all_direct(
                start_message_id,
                end_message_id,
            )
        return await self._reupload_all(start_message_id, end_message_id)

    async def _history_candidates(
        self,
        completed: set[int],
        start_message_id: int | None = None,
        end_message_id: int | None = None,
    ) -> list[Message]:
        messages: list[Message] = []
        async for msg in self.app.get_chat_history(self._source_id):
            if (
                msg.service
                or msg.id in completed
                or (start_message_id is not None and msg.id < start_message_id)
                or (end_message_id is not None and msg.id > end_message_id)
                or not self._matches_filter(msg)
            ):
                continue
            messages.append(msg)
        messages.reverse()
        return messages

    async def _forward_all_direct(
        self,
        start_message_id: int | None = None,
        end_message_id: int | None = None,
    ) -> int:
        """Native Telegram forwarding only, preserving albums and attribution."""
        completed = self._existing_source_ids | self._forwarded_ids
        candidates = await self._history_candidates(
            completed,
            start_message_id,
            end_message_id,
        )
        logger.info(
            "Native-forward candidates: %d (range: %s..%s)",
            len(candidates),
            start_message_id or "channel beginning",
            end_message_id or "channel end",
        )

        forwarded = 0
        seen_groups: set[str] = set()
        for msg in candidates:
            while True:
                try:
                    if msg.media_group_id:
                        if msg.media_group_id in seen_groups:
                            break
                        seen_groups.add(msg.media_group_id)
                        group = await self.app.get_media_group(self._source_id, msg.id)
                        group = [
                            item
                            for item in group
                            if item.id not in completed
                            and (
                                start_message_id is None
                                or item.id >= start_message_id
                            )
                            and (
                                end_message_id is None
                                or item.id <= end_message_id
                            )
                            and self._matches_filter(item)
                        ]
                        if not group:
                            break
                        await self.app.forward_messages(
                            self._dest_id,
                            self._source_id,
                            [item.id for item in group],
                        )
                        for item in group:
                            self._mark_done(item.id)
                        forwarded += len(group)
                    else:
                        await self.app.forward_messages(
                            self._dest_id,
                            self._source_id,
                            msg.id,
                        )
                        self._mark_done(msg.id)
                        forwarded += 1
                    break
                except FloodWait as exc:
                    wait = getattr(exc, "value", getattr(exc, "x", 30))
                    logger.warning("FloodWait %ds - retrying msg %d", wait, msg.id)
                    await asyncio.sleep(wait)

            await asyncio.sleep(0.5)

        return forwarded

    async def _reupload_all(
        self,
        start_message_id: int | None = None,
        end_message_id: int | None = None,
    ) -> int:
        """Clone files sequentially and pure photo posts as complete albums."""
        self._cleanup_stale_clone_dirs()
        logger.info(
            "Temporary download directory: %s",
            self.temp_download_dir.resolve(),
        )
        state_completed = self._load_clone_state()
        completed = self._existing_source_ids | state_completed | self._forwarded_ids
        candidates = await self._history_candidates(
            completed,
            start_message_id,
            end_message_id,
        )
        logger.info(
            "Local re-upload candidates: %d "
            "(%d restored from state, range: %s..%s)",
            len(candidates),
            len(state_completed),
            start_message_id or "channel beginning",
            end_message_id or "channel end",
        )

        cloned = 0
        completed_photo_groups: set[str] = set()
        for msg in candidates:
            if (
                msg.media_group_id
                and msg.media_group_id in completed_photo_groups
            ):
                continue

            while True:
                try:
                    if msg.media_group_id:
                        try:
                            group = await self._retry_flood_wait(
                                lambda: self.app.get_media_group(
                                    self._source_id,
                                    msg.id,
                                ),
                                msg.id,
                            )
                        except (RPCError, ValueError):
                            group = [msg]

                        pending_group = [
                            item
                            for item in group
                            if item.id not in completed
                            and (
                                start_message_id is None
                                or item.id >= start_message_id
                            )
                            and (
                                end_message_id is None
                                or item.id <= end_message_id
                            )
                            and self._matches_filter(item)
                        ]
                        if (
                            len(pending_group) >= 2
                            and all(item.photo for item in group)
                        ):
                            sent_group = await self._clone_photo_album_via_local(
                                pending_group
                            )
                            completed_photo_groups.add(msg.media_group_id)
                            for source_item in pending_group:
                                self._mark_done(source_item.id)
                                completed.add(source_item.id)
                            cloned += len(sent_group)
                            logger.info(
                                "Cloned photo album %s (%d images)",
                                msg.media_group_id,
                                len(sent_group),
                            )
                            break

                    file_media = self._file_media(msg)
                    if file_media:
                        kind, media = file_media
                        sent, sha256, verification = await self._clone_file_via_local(
                            msg,
                            kind,
                            media,
                        )
                    else:
                        sent = await self._clone_non_file(msg)
                        if sent is None:
                            raise IntegrityError(
                                f"Telegram returned no message for source msg {msg.id}"
                            )
                        sha256 = None
                        verification = "telegram-response"

                    if not file_media:
                        self._record_verified_clone(
                            msg.id,
                            sent.id,
                            sha256,
                            verification,
                        )
                    self._mark_done(msg.id)
                    completed.add(msg.id)
                    cloned += 1
                    logger.info(
                        "Cloned source msg %d -> destination msg %d (%s)",
                        msg.id,
                        sent.id,
                        verification,
                    )
                    break
                except FloodWait as exc:
                    wait = getattr(exc, "value", getattr(exc, "x", 30))
                    logger.warning("FloodWait %ds - retrying msg %d", wait, msg.id)
                    await asyncio.sleep(wait)

            await asyncio.sleep(0.5)

        return cloned

    async def forward_latest(self) -> int:
        """Check for new messages since last poll and forward them

        Returns the number of forwarded messages
        """
        messages: list[Message] = []
        async for msg in self.app.get_chat_history(self._source_id, limit=50):
            if msg.id <= self._last_checked_id:
                break
            if (
                msg.id not in self._forwarded_ids
                and msg.id not in self._existing_source_ids
                and not msg.service
            ):
                messages.append(msg)

        if not messages:
            return 0

        messages.reverse()  # oldest first
        return await self._process(messages)

    async def forward_all(self) -> int:
        """Forward ALL messages from source that are not yet in destination"""
        return await self.forward_batch(count=0)

    async def forward_batch(self, count: int) -> int:
        """Forward count oldest messages that are not yet in destination"""
        all_msgs: list[Message] = []
        async for msg in self.app.get_chat_history(self._source_id):
            if msg.service:
                continue
            all_msgs.append(msg)

        all_msgs.reverse()  # oldest first

        candidates: list[Message] = []
        for msg in all_msgs:
            if msg.id in self._existing_source_ids:
                continue
            if msg.id in self._forwarded_ids:
                continue
            if self._matches_filter(msg):
                candidates.append(msg)
            if count and len(candidates) >= count:
                break

        logger.info(
            "Source has %d messages total, %d already in dest, %d to forward",
            len(all_msgs), len(self._existing_source_ids), len(candidates),
        )
        return await self._process(candidates)

    async def run_polling(self, interval: float = 5.0) -> None:
        """Infinite loop — poll for new messages and forward them"""
        # Set baseline to the most recent message
        async for msg in self.app.get_chat_history(self._source_id, limit=1):
            self._last_checked_id = msg.id
            break

        logger.info(
            "Polling started (every %.1fs). Last msg id: %d",
            interval, self._last_checked_id,
        )

        while True:
            try:
                n = await self.forward_latest()
                if n:
                    logger.info("Forwarded %d new message(s)", n)
            except FloodWait as e:
                wait = getattr(e, "value", getattr(e, "x", 30))
                logger.warning("FloodWait %ds — sleeping", wait)
                await asyncio.sleep(wait)
            except RPCError as e:
                logger.error("Polling error: %s", e)

            await asyncio.sleep(interval)

    # Internal processing

    async def _process(self, messages: list[Message]) -> int:
        """Apply filters, handle media groups, send with rate-limit awareness"""
        forwarded = 0
        seen_groups: set[str] = set()

        for msg in messages:
            try:
                # Media group
                if msg.media_group_id:
                    if msg.media_group_id in seen_groups:
                        self._mark_done(msg.id)
                        continue
                    seen_groups.add(msg.media_group_id)

                    try:
                        group = await self.app.get_media_group(
                            self._source_id, msg.id,
                        )
                    except (RPCError, ValueError):
                        group = [msg]

                    if not any(self._matches_filter(m) for m in group):
                        for m in group:
                            self._mark_done(m.id)
                        continue

                    await self._send_group(group)
                    for m in group:
                        self._mark_done(m.id)
                    forwarded += len(group)

                # Single message
                else:
                    if not self._matches_filter(msg):
                        self._mark_done(msg.id)
                        continue

                    await self._send_single(msg)
                    self._mark_done(msg.id)
                    forwarded += 1

            except FloodWait as e:
                wait = getattr(e, "value", getattr(e, "x", 30))
                logger.warning("FloodWait %ds — sleeping", wait)
                await asyncio.sleep(wait)
            except RPCError as e:
                logger.error("Error processing msg %d: %s", msg.id, e)

            # Small delay between messages
            await asyncio.sleep(0.5)

        return forwarded

    def _mark_done(self, msg_id: int) -> None:
        self._forwarded_ids.add(msg_id)
        self._existing_source_ids.add(msg_id)
        self._last_checked_id = max(self._last_checked_id, msg_id)
