from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from enum import IntEnum

from pyrogram import Client
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

# Telegram Desktop open-source API credentials
DESKTOP_API_ID = 2040
DESKTOP_API_HASH = "b18441a1ff607e10a989891a5462e627"

SESSION_NAME = "account"

logging.getLogger("pyrogram").setLevel(logging.ERROR)

logger = logging.getLogger(__name__)


class ForwardMode(IntEnum):
    ALL = 0
    WORDS = 1
    PHOTO = 2
    DOCUMENT = 3
    DOCUMENT_FORMAT = 4
    AUDIO = 5
    VIDEO = 6
    VOICE = 7


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
    ):
        self._source_input = source
        self._destination_input = destination
        self.mode = mode
        self.words = [w.lower() for w in (words or [])]
        self.file_formats = [f.lower() for f in (file_formats or [])]

        self.app: Client | None = None
        self._source_id: int = 0
        self._dest_id: int = 0
        self._source_protected: bool = False
        self._forwarded_ids: set[int] = set()
        self._existing_source_ids: set[int] = set()  # source msg ids already in dest
        self._last_checked_id: int = 0

    # Lifecycle

    async def start(self) -> None:
        """Authenticate, resolve source/destination, detect protection"""
        self.app = Client(
            SESSION_NAME,
            api_id=DESKTOP_API_ID,
            api_hash=DESKTOP_API_HASH,
        )
        await self.app.start()

        me = await self.app.get_me()
        logger.info("Logged in as %s (id=%d)", me.first_name, me.id)

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
        logger.info("Destination: '%s' (id=%d)", dst.title, dst.id)

        # Build set of source message IDs already present in destination
        await self._scan_existing()

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
                return chat.id
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

    # Public API

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
