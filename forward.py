import datetime
import logging
from pyrogram import Client

class Forward:
    """
    Forward modes:
		modes (0-7 is in idle mode and forward only new messages)
        0 - all messages
        1 - contains words
        2 - only images
        3 - only documents
        4 - only documents with special format
        5 - only audio
        6 - only video
        7 - only voice
        8 - all messages (full backup channel)
    """
    
    def __init__(self, mode, from_ch, to_ch, words_list, file_formats, api_id, api_hash):
        # Instance attribute initialization
        self.mode = mode
        self.target_from = from_ch
        self.target_to = to_ch
        self.words_list = words_list  # for mode 1
        self.allowed_file_formats = file_formats  # for mode 4
        self.API_ID = api_id
        self.API_HASH = api_hash

        self.target_last_msg_id = None
        self.target_last_msg_obj = None
        self.host_last_msg_id = None
        self.last_forwarded_msg_id = 0  # Stores the last forwarded message ID

        self.app = None

        # Setup logging configuration
        logging.basicConfig(
            level=logging.INFO,
            format='[%(asctime)s] %(levelname)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        self.logger = logging.getLogger(__name__)

    def auth(self):
        """
        Authenticate using Pyrogram.
        """
        try:
            self.app = Client("forward", api_id=self.API_ID, api_hash=self.API_HASH)
            self.app.start()
            self.logger.info("Authentication successful.")
        except Exception as e:
            self.logger.error("Authentication failed: %s", e)
            raise

    def get_target_last_post(self):
        """
        Retrieves the last post from the target channel and stores its ID and object.
        """
        try:
            target_history = list(self.app.get_chat_history(self.target_from, limit=1))
            if not target_history:
                self.logger.warning("No messages found in target channel: %s", self.target_from)
                return False
            self.target_last_msg_obj = target_history[0]
            self.target_last_msg_id = self.target_last_msg_obj.id
            return True
        except Exception as e:
            self.logger.error("Error fetching target last post: %s", e)
            return False

    def get_host_last_post(self):
        """
        Retrieves the last forwarded post from the host channel and stores its ID.
        """
        try:
            host_history = list(self.app.get_chat_history(self.target_to, limit=1))
            if not host_history:
                self.logger.warning("No messages found in host channel: %s", self.target_to)
                return False
            # Some messages may not have forward_from_message_id attribute
            message = host_history[0]
            self.host_last_msg_id = getattr(message, 'forward_from_message_id', None)
            return True
        except Exception as e:
            self.logger.error("Error fetching host last post: %s", e)
            return False

    def check_key(self, key):
        """
        Checks if the last message from the target channel has the attribute `key` 
        and that it is not None.
        """
        if self.target_last_msg_obj and hasattr(self.target_last_msg_obj, key):
            return getattr(self.target_last_msg_obj, key) is not None
        return False

    def check_message_text(self):
        """
        For mode 1. Checks whether the text or caption of the message contains 
        any of the specified words.
        """
        if not self.target_last_msg_obj:
            return False

        text = ""
        if hasattr(self.target_last_msg_obj, "text") and self.target_last_msg_obj.text:
            text = self.target_last_msg_obj.text.lower()
        elif hasattr(self.target_last_msg_obj, "caption") and self.target_last_msg_obj.caption:
            text = self.target_last_msg_obj.caption.lower()
        else:
            return False

        for word in self.words_list:
            if word.lower() in text:
                return True
        return False

    def check_document_format(self):
        """
        For mode 4. Checks if the document's file format is in the list of allowed formats.
        """
        if self.target_last_msg_obj and hasattr(self.target_last_msg_obj, "document") and self.target_last_msg_obj.document:
            file_name = getattr(self.target_last_msg_obj.document, 'file_name', None)
            if file_name and '.' in file_name:
                file_format = file_name.rsplit('.', 1)[1].lower()
                allowed_formats = [fmt.lower() for fmt in self.allowed_file_formats]
                return file_format in allowed_formats
        return False

    def match_messages(self):
        """
        Compares message IDs and, depending on the mode, determines whether the message
        should be forwarded.
        """
        forward = False
        backup = False

        if not (self.target_last_msg_id and self.target_last_msg_obj):
            self.logger.warning("No target message available for matching.")
            return False

        if self.target_last_msg_id == self.last_forwarded_msg_id:
            self.logger.info("Message already forwarded: ID %s", self.target_last_msg_id)
            return False

        if self.host_last_msg_id and (self.target_last_msg_id == self.host_last_msg_id):
            self.logger.info("Target message already exists in host channel: ID %s", self.target_last_msg_id)
            return False

        # Determine forwarding logic based on mode
        if self.mode == 0:
            forward = True
        elif self.mode == 1:
            forward = self.check_message_text()
        elif self.mode == 2:
            forward = self.check_key("photo")
        elif self.mode == 3:
            forward = self.check_key("document")
        elif self.mode == 4:
            forward = self.check_document_format()
        elif self.mode == 5:
            forward = self.check_key("audio")
        elif self.mode == 6:
            forward = self.check_key("video")
        elif self.mode == 7:
            forward = self.check_key("voice")
        elif self.mode == 8:
            backup = True
        else:
            self.logger.warning("Unknown mode: %s", self.mode)
            return False

        if forward:
            try:
                self.target_last_msg_obj.forward(self.target_to)
                self.last_forwarded_msg_id = self.target_last_msg_id
                self.logger.info("Forwarded message ID: %s", self.target_last_msg_id)
                return True
            except Exception as e:
                self.logger.error("Error forwarding message ID %s: %s", self.target_last_msg_id, e)
                return False
        elif backup:
            self.forward_all_messages()
            return True
        else:
            self.logger.info("Conditions not met for forwarding message ID: %s", self.target_last_msg_id)
            return False

    def get_current_datetime(self):
        """
        Returns the current date and time as a formatted string.
        """
        now = datetime.datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S")

    def forward_all_messages(self):
        """
        Backup mode: forwards all messages from the target channel to the host channel.
        """
        try:
            messages = list(self.app.get_chat_history(self.target_from))
            if not messages:
                self.logger.warning("No messages to forward from target channel: %s", self.target_from)
                return
            for message in reversed(messages):
                # Skip service messages
                if hasattr(message, "service") and message.service is not None:
                    self.logger.info("Skipping service message ID: %s", message.id)
                    continue
                if hasattr(message, "channel_chat_created") and message.channel_chat_created:
                    self.logger.info("Skipping channel creation message ID: %s", message.id)
                    continue
                try:
                    message.forward(self.target_to)
                    self.logger.info("Forwarded message ID: %s", message.id)
                except Exception as e:
                    self.logger.error("Error forwarding message ID %s: %s", message.id, e)
            self.app.stop()
        except Exception as e:
            self.logger.error("Error during forwarding all messages: %s", e)

    def start(self):
        """
        Main method that starts the forwarding process.
        """
        if not self.get_target_last_post():
            self.logger.error("Failed to retrieve the last target post.")
            return
        if not self.get_host_last_post():
            self.logger.info("Host last post not retrieved. Proceeding without it.")
        self.match_messages()
