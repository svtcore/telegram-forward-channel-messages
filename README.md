# Telegram Bot Forward Channel Messages

Provide make backup of messages from telegram channels even if they were delete by the owner of channel or forward messages from private channels to your channel (if you have access and owner does not restrict sharing posts)

Script has forward modes:
* all messages
* contains words
* only images
* only documents
* only documents with special format
* only audio
* only video
* only voice

Script based on [Pyrogram library](https://github.com/pyrogram/pyrogram "Pyrogram library")

# Install

**Do it only on test telegram account**

1. Install [Pyrogram](https://github.com/pyrogram/pyrogram "Pyrogram")
2. Log in on the https://my.telegram.org
3. Go to **API development tools**
4. Fill the fields and click **Create Application**
5. Open in editor file **bot.py**
6. Insert your data **API_ID** and **API_HASH** from step 4
7. Set mode from 0  to 7
8. Insert your data from url into variables **from_channel** and **to_channel**
(For an example from channel https://t.me/durov copy and paste **durov** and same with your channel)
9. To start write on terminal 
> **python bot.py**

# License
[MIT LICENSE](https://github.com/svtcore/TelegramBotForwardChannelMessages/blob/main/LICENSE "MIT LICENSE")
