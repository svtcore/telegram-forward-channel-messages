from forward import ForwardMode

# Source channel (public link, private invite link, or chat title)
SOURCE = "https://t.me/channel_name_from_which_to_forward"

# Destination channel (public link, private invite link, or chat title)
DESTINATION = "https://t.me/channel_name_for_forwarding_to"

# Forwarding mode
#   ForwardMode.ALL             - all messages
#   ForwardMode.WORDS           - messages containing specified words
#   ForwardMode.PHOTO           - only photos
#   ForwardMode.DOCUMENT        - only documents
#   ForwardMode.DOCUMENT_FORMAT - documents with specified extensions
#   ForwardMode.AUDIO           - only audio
#   ForwardMode.VIDEO           - only video
#   ForwardMode.VOICE           - only voice messages
MODE = ForwardMode.ALL

# Keywords for ForwardMode.WORDS
WORDS = ["sport", "internet", "meme"]

# File extensions for ForwardMode.DOCUMENT_FORMAT
FILE_FORMATS = ["pdf", "zip", "rar"]

# Polling interval in seconds (for monitoring mode)
POLL_INTERVAL = 5.0
