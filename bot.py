from forward import Forward
import time

'''
Forward modes
0 - all messages
1 - contains words
2 - only images
3 - only documents
4 - only documents with special format
5 - only audio
6 - only video
7 - only voice
'''

#API ID and API hash you can find on my.telegram.org

API_ID = 0 #YOUR API_ID
API_HASH = "YOUR_API_HASH_HERE"
mode = 0 #mode - default 0 - forward all messages
from_channel = "iwantforwardfromhere" #channel name in url
to_channel = "forwardtothischannel" #channel name
words_list = ['sports', 'win', 'funny', 'http', 'https'] #if you use mode №1, it will forward only messages if they contains any of these words
allowed_file_formats = ['rar', 'zip', '7z'] #if you use mode №4, it will forward only messages if they have any of these file formats


fwd = Forward(mode, from_channel, to_channel, words_list, allowed_file_formats, API_ID, API_HASH)
fwd.auth() #auth through pytrogram
while True:
	fwd.start()
	time.sleep(5) #delay while checking new posts