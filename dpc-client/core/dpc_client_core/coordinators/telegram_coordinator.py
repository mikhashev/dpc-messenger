"""
Telegram Bridge - Bidirectional Message Translation (v0.14.0+)

This module implements the TelegramBridge coordinator that translates messages
between Telegram and DPC Messenger formats.

Key Features:
- Bidirectional message translation (Telegram ↔ DPC)
- Voice message transcription using LocalWhisperProvider
- Conversation mapping (telegram_chat_id ↔ dpc_conversation_id)
- Message routing to conversation monitors
- Two-way bridge support (forward to P2P peers)

Architecture:
- TelegramBotManager handles low-level Telegram API
- TelegramBridge translates between formats and routes messages
- TelegramIncomingHandler routes Telegram messages to DPC conversations

Message Format Translation:
    Telegram Update → DPC Message:
        {
            "message_id": 123,
            "from": {"id": 123456789, "username": "alice"},
            "text": "Hello from Telegram!"
        }
        ↓
        {
            "sender_node_id": "telegram-bot-123456789",
            "sender_name": "Alice",
            "text": "Hello from Telegram!",
            "timestamp": "2026-01-14T10:00:00Z",
            "source": "telegram",
            "telegram_chat_id": 123456789
        }

    DPC Message → Telegram:
        {
            "sender_name": "Bob",
            "text": "Hi Alice!"
        }
        ↓
        "<b>Bob</b> (DPC):\nHi Alice!"
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional, Dict, Any, TYPE_CHECKING, List
from datetime import datetime, timezone

if TYPE_CHECKING:
    from ..service import CoreService
    from ..managers.telegram_manager import TelegramBotManager

logger = logging.getLogger(__name__)


class TelegramBridge:
    """
    Bidirectional message bridge between Telegram and DPC Messenger.

    This coordinator handles:
    - Converting Telegram updates to DPC message format
    - Routing messages to conversation monitors
    - Triggering voice transcription
    - Forwarding DPC messages to Telegram
    - Managing conversation mappings

    Attributes:
        service: CoreService instance
        telegram_manager: TelegramBotManager for API access
        conversation_map: Maps telegram_chat_id → dpc_conversation_id

    Example:
        >>> bridge = TelegramBridge(service, telegram_manager)
        >>> await bridge.handle_text_message(update)  # From Telegram
        >>> await bridge.forward_dpc_to_telegram(conversation_id, "Bob", "Hello!")
    """

    def __init__(self, service: "CoreService", telegram_manager: "TelegramBotManager"):
        """
        Initialize Telegram Bridge.

        Args:
            service: CoreService instance
            telegram_manager: TelegramBotManager instance
        """
        self.service = service
        self.telegram = telegram_manager

        # Map telegram_chat_id → dpc_conversation_id
        self.conversation_map: Dict[str, str] = {}

        # Track users who have received unknown user info (rate limiting - v0.15.0)
        self._notified_unknown_users = set()

        # Load saved conversation links from settings
        self._load_conversation_links()

        logger.info("TelegramBridge initialized")

    def _load_conversation_links(self):
        """Load saved conversation links from settings."""
        try:
            links_str = self.service.settings.get("telegram", "conversation_links", fallback="{}")
            if links_str:
                import json
                self.conversation_map = json.loads(links_str)
                logger.info(f"Loaded {len(self.conversation_map)} conversation links")
        except Exception as e:
            logger.warning(f"Failed to load conversation links: {e}")

    def _save_conversation_links(self):
        """Save conversation links to settings."""
        try:
            import json
            links_str = json.dumps(self.conversation_map)
            self.service.settings.set("telegram", "conversation_links", links_str)
            logger.debug(f"Saved {len(self.conversation_map)} conversation links")
        except Exception as e:
            logger.error(f"Failed to save conversation links: {e}")

    def _get_or_create_conversation_id(self, telegram_chat_id: str) -> str:
        """
        Get or create DPC conversation ID for a Telegram chat.

        Args:
            telegram_chat_id: Telegram chat ID

        Returns:
            DPC conversation ID (format: telegram-{chat_id})
        """
        # Check if we have a mapping
        if telegram_chat_id in self.conversation_map:
            return self.conversation_map[telegram_chat_id]

        # Create new conversation ID
        conversation_id = f"telegram-{telegram_chat_id}"
        self.conversation_map[telegram_chat_id] = conversation_id
        self._save_conversation_links()

        logger.info(f"Created conversation link: {telegram_chat_id} → {conversation_id}")
        return conversation_id

    def _get_linked_chat_id(self, conversation_id: str) -> Optional[str]:
        """
        Get Telegram chat_id for a DPC conversation.

        Args:
            conversation_id: DPC conversation ID

        Returns:
            Telegram chat_id if linked, None otherwise
        """
        # Check if this is a telegram-prefixed conversation
        if conversation_id.startswith("telegram-"):
            return conversation_id.replace("telegram-", "")

        # Check map for custom links
        for chat_id, conv_id in self.conversation_map.items():
            if conv_id == conversation_id:
                return chat_id

        return None

    async def _send_unknown_user_info(self, chat_id: str, from_user):
        """Send helpful message to unknown user with their chat_id (v0.15.0)."""
        sender_name = from_user.full_name or from_user.username or f"User_{chat_id}"
        username = from_user.username if from_user.username else "N/A"

        # Get bot owner contact from settings
        owner_contact = self.service.settings.get_telegram_owner_contact()

        # Check for custom access denied message
        custom_message = self.service.settings.get_telegram_access_denied_message()
        if custom_message:
            # Use custom message with placeholders
            message = custom_message.format(
                user_name=sender_name,
                user_id=chat_id,
                username=username,
                owner_contact=owner_contact or "the bot owner"
            )
        else:
            # Use default message
            if owner_contact:
                # Enhanced message with owner contact info
                message = (
                    f"⚠️ <b>Access Denied</b>\n\n"
                    f"Hello {sender_name}!\n\n"
                    f"Your Telegram User ID is: <code>{chat_id}</code>\n"
                    f"Username: @{username}\n\n"
                    f"This is a private bot. To request access:\n"
                    f"<b>Contact the bot owner:</b>\n{owner_contact}\n\n"
                    f"Please send your User ID above to request access."
                )
            else:
                # Default message without owner contact
                message = (
                    f"⚠️ <b>Access Denied</b>\n\n"
                    f"Hello {sender_name}!\n\n"
                    f"Your Telegram User ID is: <code>{chat_id}</code>\n"
                    f"Username: @{username}\n\n"
                    f"This is a private bot. To be granted access:\n"
                    f"1. Send your User ID (above) to the bot owner\n"
                    f"2. Wait for the bot owner to add you to the whitelist\n"
                    f"3. Try sending your message again"
                )

        await self.telegram.send_message(
            chat_id,
            message,
            parse_mode='HTML',
            skip_whitelist_check=True  # Bypass whitelist for system messages to unknown users
        )

    async def handle_text_message(self, update, context):
        """
        Handle incoming text message from Telegram.

        This is called by the TelegramBotManager when a text message is received.

        Args:
            update: Telegram Update object
            context: Telegram Context object
        """
        try:
            # Extract message data
            message = update.message
            chat_id = str(message.chat_id)
            text = message.text
            message_id = message.message_id
            from_user = message.from_user

            # Whitelist check with rate limiting (send info reply only once per user - v0.15.0)
            if not self.telegram.is_allowed(chat_id):
                if chat_id not in self._notified_unknown_users:
                    logger.warning(f"Message from unauthorized chat_id {chat_id}, sending info reply")
                    await self._send_unknown_user_info(chat_id, from_user)
                    self._notified_unknown_users.add(chat_id)
                else:
                    logger.warning(f"Message from unauthorized chat_id {chat_id}, already notified")
                return

            # Get sender info
            sender_name = from_user.full_name or from_user.username or f"User_{chat_id}"

            # Get or create conversation
            conversation_id = self._get_or_create_conversation_id(chat_id)

            # Convert to DPC message format
            dpc_message = {
                "sender_node_id": f"telegram-bot-{chat_id}",
                "sender_name": sender_name,
                "text": text,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "telegram",
                "telegram_chat_id": chat_id,
                "telegram_message_id": message_id
            }

            # Get or create conversation monitor
            from ..conversation_monitor import ConversationMonitor, Message as ConvMessage

            monitor = self.service.conversation_monitors.get(conversation_id)
            if not monitor:
                monitor = ConversationMonitor(
                    conversation_id=conversation_id,
                    participants=[{"node_id": f"telegram-bot-{chat_id}", "name": sender_name}],  # Single participant for Telegram chats
                    llm_manager=self.service.llm_manager,
                    knowledge_threshold=0.7,
                    settings=self.service.settings,
                    ai_query_func=self.service.send_ai_query,
                    auto_detect=self.service.auto_knowledge_detection_enabled,
                    instruction_set_name=self.service.instruction_set.default
                )
                self.service.conversation_monitors[conversation_id] = monitor

            # Create Message object
            conv_message = ConvMessage(
                message_id=f"telegram-{message_id}",
                conversation_id=conversation_id,
                sender_node_id=dpc_message["sender_node_id"],
                sender_name=sender_name,
                text=text,
                timestamp=datetime.now(timezone.utc)
            )

            # Add to monitor buffer
            await monitor.on_message(conv_message)

            # Broadcast to UI
            await self.service.local_api.broadcast_event("telegram_message_received", {
                "conversation_id": conversation_id,
                "telegram_chat_id": chat_id,
                "sender_name": sender_name,
                "text": text,
                "timestamp": dpc_message["timestamp"]
            })

            # Forward to P2P peers if enabled
            if self.telegram.bridge_to_p2p:
                await self._forward_to_p2p_peers(conversation_id, dpc_message)

            logger.info(f"Processed Telegram text message from {chat_id}: {text[:50]}...")

            # Track update_id for history recovery
            update_id_to_track = update.update_id if hasattr(update, 'update_id') else message_id
            self.service.settings.set_telegram_last_update_id(chat_id, update_id_to_track)

        except Exception as e:
            logger.error(f"Error handling Telegram text message: {e}", exc_info=True)

    async def handle_voice_message(self, update, context):
        """
        Handle incoming voice message from Telegram with transcription.

        Flow:
        1. Download voice file from Telegram
        2. Save to ~/.dpc/conversations/telegram-{chat_id}/files/
        3. Transcribe using LocalWhisperProvider
        4. Send transcription back to Telegram
        5. Create voice attachment in DPC

        Args:
            update: Telegram Update object
            context: Telegram Context object
        """
        try:
            message = update.message
            voice = message.voice
            chat_id = str(message.chat_id)
            from_user = message.from_user

            # Whitelist check with rate limiting (v0.15.0)
            if not self.telegram.is_allowed(chat_id):
                if chat_id not in self._notified_unknown_users:
                    logger.warning(f"Voice message from unauthorized chat_id {chat_id}, sending info reply")
                    await self._send_unknown_user_info(chat_id, from_user)
                    self._notified_unknown_users.add(chat_id)
                else:
                    logger.warning(f"Voice message from unauthorized chat_id {chat_id}, already notified")
                return

            # Get voice metadata
            duration = voice.duration
            file_size = voice.file_size
            file_id = voice.file_id

            logger.info(f"Received voice message from {chat_id} (duration: {duration}s, size: {file_size} bytes)")

            # Get or create conversation
            conversation_id = self._get_or_create_conversation_id(chat_id)

            # Download voice file
            voice_filename = f"telegram_voice_{message.message_id}.ogg"
            voice_dir = Path.home() / ".dpc" / "conversations" / conversation_id / "files"
            voice_dir.mkdir(parents=True, exist_ok=True)
            voice_path = voice_dir / voice_filename

            # Download from Telegram
            from telegram import Bot
            bot: Bot = self.telegram.application.bot
            file = await bot.get_file(file_id)

            await file.download_to_drive(voice_path)
            logger.info(f"Downloaded voice file to {voice_path}")

            # Transcribe if enabled
            transcription_text = None
            transcription_provider = None

            if self.telegram.transcription_enabled:
                try:
                    # Get voice provider alias
                    voice_provider_alias = self.service.llm_manager.voice_provider

                    if voice_provider_alias:
                        # Read file and encode as base64
                        import base64
                        with open(voice_path, "rb") as f:
                            audio_data = f.read()
                            audio_base64 = base64.b64encode(audio_data).decode("utf-8")

                        # Transcribe using service method (handles provider selection)
                        transcription_result = await self.service.transcribe_audio(
                            audio_base64=audio_base64,
                            mime_type="audio/ogg"
                        )

                        transcription_text = transcription_result.get("text", "")
                        transcription_provider = transcription_result.get("provider", "unknown")

                        logger.info(f"Transcribed voice message ({len(transcription_text)} chars, provider: {transcription_provider})")

                        # Send transcription back to Telegram (v0.15.0 - re-enabled)
                        await self.telegram.send_message(
                            chat_id,
                            f"📝 Transcription:\n{transcription_text}"
                        )
                    else:
                        logger.warning("No voice provider configured, skipping transcription")

                except Exception as e:
                    logger.error(f"Failed to transcribe voice message: {e}", exc_info=True)

            # Create voice attachment
            from ..conversation_monitor import Message as ConvMessage

            sender_name = message.from_user.full_name or message.from_user.username or f"User_{chat_id}"

            voice_attachment = {
                "type": "voice",
                "filename": voice_filename,
                "file_path": str(voice_path),
                "size_bytes": file_size,
                "voice_metadata": {
                    "duration_seconds": duration,
                    "sample_rate": 48000,  # Telegram default
                    "channels": 1,
                    "codec": "opus",
                    "recorded_at": datetime.now(timezone.utc).isoformat()
                },
                "transcription": transcription_text if transcription_text else None,
                "transcription_provider": transcription_provider,
                "source": "telegram",
                "telegram_message_id": message.message_id
            }

            # Get or create conversation monitor
            monitor = self.service.conversation_monitors.get(conversation_id)
            if not monitor:
                from ..conversation_monitor import ConversationMonitor
                monitor = ConversationMonitor(
                    conversation_id=conversation_id,
                    participants=[{"node_id": f"telegram-bot-{chat_id}", "name": sender_name}],  # Single participant for Telegram chats
                    llm_manager=self.service.llm_manager,
                    knowledge_threshold=0.7,
                    settings=self.service.settings,
                    ai_query_func=self.service.send_ai_query,
                    auto_detect=self.service.auto_knowledge_detection_enabled,
                    instruction_set_name=self.service.instruction_set.default
                )
                self.service.conversation_monitors[conversation_id] = monitor

            # Create message with voice attachment
            # NOTE: Include transcription in message text for knowledge extraction (v0.15.1+)
            # Transcription is also shown in VoicePlayer attachment for UI
            message_text = transcription_text if transcription_text else "Voice message"
            conv_message = ConvMessage(
                message_id=f"telegram-voice-{message.message_id}",
                conversation_id=conversation_id,
                sender_node_id=f"telegram-bot-{chat_id}",
                sender_name=sender_name,
                text=message_text,
                timestamp=datetime.now(timezone.utc)
            )
            conv_message.attachment_transfer_id = f"telegram-{message.message_id}"
            conv_message.attachments = [voice_attachment]

            # Add to monitor
            await monitor.on_message(conv_message)

            # Broadcast to UI
            await self.service.local_api.broadcast_event("telegram_voice_received", {
                "conversation_id": conversation_id,
                "telegram_chat_id": chat_id,
                "sender_name": sender_name,
                "filename": voice_filename,
                "file_path": str(voice_path),  # Include actual file path for playback
                "duration_seconds": duration,
                "transcription": transcription_text,
                "transcription_provider": transcription_provider
            })

            logger.info(f"Processed Telegram voice message from {chat_id}")

            # Track update_id for history recovery
            update_id_to_track = update.update_id if hasattr(update, 'update_id') else message.message_id
            self.service.settings.set_telegram_last_update_id(chat_id, update_id_to_track)

        except Exception as e:
            logger.error(f"Error handling Telegram voice message: {e}", exc_info=True)

    async def handle_photo_message(self, update, context):
        """
        Handle incoming photo message from Telegram.

        Downloads the photo and creates an image attachment in the DPC conversation.
        """
        try:
            message = update.message
            photo = message.photo[-1]  # Get largest size
            chat_id = str(message.chat_id)
            from_user = message.from_user

            # Whitelist check with rate limiting (v0.15.0)
            if not self.telegram.is_allowed(chat_id):
                if chat_id not in self._notified_unknown_users:
                    logger.warning(f"Photo from unauthorized chat_id {chat_id}, sending info reply")
                    await self._send_unknown_user_info(chat_id, from_user)
                    self._notified_unknown_users.add(chat_id)
                else:
                    logger.warning(f"Photo from unauthorized chat_id {chat_id}, already notified")
                return

            # Get or create conversation
            conversation_id = self._get_or_create_conversation_id(chat_id)

            # Download photo
            photo_filename = f"telegram_photo_{message.message_id}.jpg"
            photo_dir = Path.home() / ".dpc" / "conversations" / conversation_id / "files"
            photo_dir.mkdir(parents=True, exist_ok=True)
            photo_path = photo_dir / photo_filename

            from telegram import Bot
            bot: Bot = self.telegram.application.bot
            file = await bot.get_file(photo.file_id)
            await file.download_to_drive(photo_path)

            logger.info(f"Downloaded photo to {photo_path}")

            # Create image attachment
            sender_name = message.from_user.full_name or message.from_user.username or f"User_{chat_id}"
            file_size = photo.file_size or photo_path.stat().st_size

            image_attachment = {
                "type": "image",
                "filename": photo_filename,
                "file_path": str(photo_path),
                "size_bytes": file_size,
                "mime_type": "image/jpeg",
                "source": "telegram",
                "telegram_message_id": message.message_id
            }

            # Get or create monitor
            from ..conversation_monitor import ConversationMonitor, Message as ConvMessage
            monitor = self.service.conversation_monitors.get(conversation_id)
            if not monitor:
                monitor = ConversationMonitor(
                    conversation_id=conversation_id,
                    participants=[{"node_id": f"telegram-bot-{chat_id}", "name": sender_name}],
                    llm_manager=self.service.llm_manager,
                    knowledge_threshold=0.7,
                    settings=self.service.settings,
                    ai_query_func=self.service.send_ai_query,
                    auto_detect=self.service.auto_knowledge_detection_enabled,
                    instruction_set_name=self.service.instruction_set.default
                )
                self.service.conversation_monitors[conversation_id] = monitor

            # Create message
            caption = message.caption or "Image"
            conv_message = ConvMessage(
                message_id=f"telegram-photo-{message.message_id}",
                conversation_id=conversation_id,
                sender_node_id=f"telegram-bot-{chat_id}",
                sender_name=sender_name,
                text=caption,
                timestamp=datetime.now(timezone.utc)
            )
            conv_message.attachments = [image_attachment]

            # Add to monitor
            await monitor.on_message(conv_message)

            # Broadcast to UI
            await self.service.local_api.broadcast_event("telegram_image_received", {
                "conversation_id": conversation_id,
                "telegram_chat_id": chat_id,
                "sender_name": sender_name,
                "filename": photo_filename,
                "file_path": str(photo_path),
                "caption": caption
            })

            logger.info(f"Processed Telegram photo from {chat_id}")

            # Track update_id for history recovery
            update_id_to_track = update.update_id if hasattr(update, 'update_id') else message.message_id
            self.service.settings.set_telegram_last_update_id(chat_id, update_id_to_track)

        except Exception as e:
            logger.error(f"Error handling Telegram photo: {e}", exc_info=True)

    async def handle_document_message(self, update, context):
        """
        Handle incoming document/file message from Telegram.

        Downloads the file and creates a file attachment in the DPC conversation.
        """
        try:
            message = update.message
            document = message.document
            chat_id = str(message.chat_id)
            from_user = message.from_user

            # Whitelist check with rate limiting (v0.15.0)
            if not self.telegram.is_allowed(chat_id):
                if chat_id not in self._notified_unknown_users:
                    logger.warning(f"Document from unauthorized chat_id {chat_id}, sending info reply")
                    await self._send_unknown_user_info(chat_id, from_user)
                    self._notified_unknown_users.add(chat_id)
                else:
                    logger.warning(f"Document from unauthorized chat_id {chat_id}, already notified")
                return

            # Get or create conversation
            conversation_id = self._get_or_create_conversation_id(chat_id)

            # Get file extension from mime type or filename
            import mimetypes
            filename = document.file_name or f"telegram_document_{message.message_id}"
            mime_type = document.mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"

            # Download document
            file_dir = Path.home() / ".dpc" / "conversations" / conversation_id / "files"
            file_dir.mkdir(parents=True, exist_ok=True)
            file_path = file_dir / filename

            from telegram import Bot
            bot: Bot = self.telegram.application.bot
            file = await bot.get_file(document.file_id)
            await file.download_to_drive(file_path)

            logger.info(f"Downloaded document to {file_path}")

            # Create file attachment
            sender_name = message.from_user.full_name or message.from_user.username or f"User_{chat_id}"
            file_size = document.file_size or file_path.stat().st_size

            file_attachment = {
                "type": "file",
                "filename": filename,
                "file_path": str(file_path),
                "size_bytes": file_size,
                "mime_type": mime_type,
                "source": "telegram",
                "telegram_message_id": message.message_id
            }

            # Get or create monitor
            from ..conversation_monitor import ConversationMonitor, Message as ConvMessage
            monitor = self.service.conversation_monitors.get(conversation_id)
            if not monitor:
                monitor = ConversationMonitor(
                    conversation_id=conversation_id,
                    participants=[{"node_id": f"telegram-bot-{chat_id}", "name": sender_name}],
                    llm_manager=self.service.llm_manager,
                    knowledge_threshold=0.7,
                    settings=self.service.settings,
                    ai_query_func=self.service.send_ai_query,
                    auto_detect=self.service.auto_knowledge_detection_enabled,
                    instruction_set_name=self.service.instruction_set.default
                )
                self.service.conversation_monitors[conversation_id] = monitor

            # Create message
            caption = message.caption or filename
            conv_message = ConvMessage(
                message_id=f"telegram-document-{message.message_id}",
                conversation_id=conversation_id,
                sender_node_id=f"telegram-bot-{chat_id}",
                sender_name=sender_name,
                text=caption,
                timestamp=datetime.now(timezone.utc)
            )
            conv_message.attachments = [file_attachment]

            # Add to monitor
            await monitor.on_message(conv_message)

            # Broadcast to UI
            await self.service.local_api.broadcast_event("telegram_file_received", {
                "conversation_id": conversation_id,
                "telegram_chat_id": chat_id,
                "sender_name": sender_name,
                "filename": filename,
                "file_path": str(file_path),
                "caption": caption,
                "mime_type": mime_type,
                "size_bytes": file_size
            })

            logger.info(f"Processed Telegram document from {chat_id}")

            # Track update_id for history recovery
            update_id_to_track = update.update_id if hasattr(update, 'update_id') else message.message_id
            self.service.settings.set_telegram_last_update_id(chat_id, update_id_to_track)

        except Exception as e:
            logger.error(f"Error handling Telegram document: {e}", exc_info=True)

    async def handle_video_message(self, update, context):
        """
        Handle incoming video message from Telegram.

        Downloads the video and creates a file attachment in the DPC conversation.
        """
        try:
            message = update.message
            video = message.video
            chat_id = str(message.chat_id)
            from_user = message.from_user

            # Whitelist check with rate limiting (v0.15.0)
            if not self.telegram.is_allowed(chat_id):
                if chat_id not in self._notified_unknown_users:
                    logger.warning(f"Video from unauthorized chat_id {chat_id}, sending info reply")
                    await self._send_unknown_user_info(chat_id, from_user)
                    self._notified_unknown_users.add(chat_id)
                else:
                    logger.warning(f"Video from unauthorized chat_id {chat_id}, already notified")
                return

            # Get or create conversation
            conversation_id = self._get_or_create_conversation_id(chat_id)

            # Get file info
            filename = video.file_name or f"telegram_video_{message.message_id}.{self._get_video_extension(video.mime_type)}"
            mime_type = video.mime_type or "video/mp4"

            # Download video
            file_dir = Path.home() / ".dpc" / "conversations" / conversation_id / "files"
            file_dir.mkdir(parents=True, exist_ok=True)
            file_path = file_dir / filename

            from telegram import Bot
            bot: Bot = self.telegram.application.bot
            file = await bot.get_file(video.file_id)
            await file.download_to_drive(file_path)

            logger.info(f"Downloaded video to {file_path}")

            # Create file attachment
            sender_name = message.from_user.full_name or message.from_user.username or f"User_{chat_id}"
            file_size = video.file_size or file_path.stat().st_size

            video_attachment = {
                "type": "file",
                "filename": filename,
                "file_path": str(file_path),
                "size_bytes": file_size,
                "mime_type": mime_type,
                "source": "telegram",
                "telegram_message_id": message.message_id
            }

            # Get or create monitor
            from ..conversation_monitor import ConversationMonitor, Message as ConvMessage
            monitor = self.service.conversation_monitors.get(conversation_id)
            if not monitor:
                monitor = ConversationMonitor(
                    conversation_id=conversation_id,
                    participants=[{"node_id": f"telegram-bot-{chat_id}", "name": sender_name}],
                    llm_manager=self.service.llm_manager,
                    knowledge_threshold=0.7,
                    settings=self.service.settings,
                    ai_query_func=self.service.send_ai_query,
                    auto_detect=self.service.auto_knowledge_detection_enabled,
                    instruction_set_name=self.service.instruction_set.default
                )
                self.service.conversation_monitors[conversation_id] = monitor

            # Create message
            caption = message.caption or f"Video ({video.duration}s)"
            conv_message = ConvMessage(
                message_id=f"telegram-video-{message.message_id}",
                conversation_id=conversation_id,
                sender_node_id=f"telegram-bot-{chat_id}",
                sender_name=sender_name,
                text=caption,
                timestamp=datetime.now(timezone.utc)
            )
            conv_message.attachments = [video_attachment]

            # Add to monitor
            await monitor.on_message(conv_message)

            # Broadcast to UI
            await self.service.local_api.broadcast_event("telegram_file_received", {
                "conversation_id": conversation_id,
                "telegram_chat_id": chat_id,
                "sender_name": sender_name,
                "filename": filename,
                "file_path": str(file_path),
                "caption": caption,
                "mime_type": mime_type,
                "size_bytes": file_size
            })

            logger.info(f"Processed Telegram video from {chat_id}")

            # Track update_id for history recovery
            update_id_to_track = update.update_id if hasattr(update, 'update_id') else message.message_id
            self.service.settings.set_telegram_last_update_id(chat_id, update_id_to_track)

        except Exception as e:
            logger.error(f"Error handling Telegram video: {e}", exc_info=True)

    def _get_video_extension(self, mime_type: Optional[str]) -> str:
        """Get file extension from video mime type."""
        extensions = {
            "video/mp4": "mp4",
            "video/quicktime": "mov",
            "video/x-msvideo": "avi",
            "video/x-matroska": "mkv",
            "video/webm": "webm"
        }
        return extensions.get(mime_type, "mp4")

    async def forward_dpc_to_telegram(
        self,
        conversation_id: str,
        sender_name: str,
        text: str,
        attachments: Optional[List[Dict]] = None
    ):
        """
        Forward DPC message to linked Telegram chat.

        Args:
            conversation_id: DPC conversation ID
            sender_name: Sender's display name
            text: Message text
            attachments: Optional list of attachments (voice, images, etc.)
        """
        try:
            # Get linked Telegram chat
            telegram_chat_id = self._get_linked_chat_id(conversation_id)
            if not telegram_chat_id:
                logger.debug(f"No Telegram chat linked for conversation {conversation_id}")
                return

            # Format message for Telegram
            message = f"👤 <b>{sender_name}</b> (DPC):\n{text}"

            # Send text message
            await self.telegram.send_message(telegram_chat_id, message)

            # Handle attachments
            if attachments:
                for attachment in attachments:
                    if attachment.get("type") == "voice":
                        # Send voice file
                        file_path = Path(attachment.get("file_path", ""))
                        if file_path.exists():
                            caption = f"🎤 Voice from {sender_name}"
                            duration = attachment.get("voice_metadata", {}).get("duration_seconds")
                            await self.telegram.send_voice(
                                telegram_chat_id,
                                file_path,
                                caption=caption,
                                duration=duration
                            )

            logger.info(f"Forwarded DPC message to Telegram chat {telegram_chat_id}")

        except Exception as e:
            logger.error(f"Error forwarding to Telegram: {e}", exc_info=True)

    async def _forward_to_p2p_peers(self, conversation_id: str, message: Dict[str, Any]):
        """
        Forward Telegram message to P2P peers (if bridge_to_p2p enabled).

        CURRENT LIMITATION (v0.15.0):
        This implementation sends N separate 1:1 messages to each peer, creating
        individual conversations instead of a group chat. This means:
        - Each peer gets their own separate thread
        - No cross-peer visibility (Peer A can't see Peer B received the message)
        - Responses go to the bridge owner, not the group
        - Inefficient: O(n) network cost instead of O(1)

        FUTURE ENHANCEMENT (Group Chat Phase):
        When DPC implements multiparty conversations (see session_manager.py),
        this should be refactored to:
        - Send to a DPC group chat instead of individual peers
        - Use a dedicated TELEGRAM_FORWARD DPTP command with metadata
        - Include original Telegram sender attribution
        - Single message with O(1) network cost
        - Proper threading and reply handling

        Configuration needed for groups:
        [telegram]
        bridge_to_p2p = true
        bridge_target_type = group  # or "all_peers" for current behavior
        bridge_target_group_id = my-team-group

        Args:
            conversation_id: DPC conversation ID
            message: DPC message dict
        """
        try:
            # Get connected peers
            connected_peers = self.service.p2p_manager.get_connected_peers()

            if not connected_peers:
                logger.debug("No connected P2P peers to forward to")
                return

            # FIXME: Group Chat Support (Next Phase)
            # Current: Sends N separate 1:1 messages (creates individual conversations)
            # Future: Send 1 message to a DPC group chat (single threaded conversation)
            #
            # Pseudo-code for group chat implementation:
            # if self.telegram.bridge_target_type == "group":
            #     await self.service.send_group_message(
            #         group_id=self.telegram.bridge_target_group_id,
            #         text=message["text"],
            #         attachments=[],
            #         metadata={
            #             "source": "telegram",
            #             "original_chat_id": telegram_chat_id,
            #             "sender_name": sender_name
            #         }
            #     )
            #     return

            # Forward to each peer individually (current behavior)
            for peer_id in connected_peers:
                try:
                    # Send as text message (for now)
                    # In future, could use a TELEGRAM_FORWARD command with metadata
                    await self.service.send_p2p_message(
                        node_id=peer_id,
                        text=message["text"],
                        attachments=[]
                    )
                    logger.debug(f"Forwarded Telegram message to peer {peer_id}")
                except Exception as e:
                    logger.error(f"Failed to forward to peer {peer_id}: {e}")

        except Exception as e:
            logger.error(f"Error forwarding to P2P peers: {e}", exc_info=True)
