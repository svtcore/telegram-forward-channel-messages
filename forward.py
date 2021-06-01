import datetime
from pyrogram import Client

class Forward:

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

	mode = None
	target_last_msg_id = None
	target_last_msg_obj = None
	host_last_msg_id = None
	words_list = []
	API_ID = None
	API_HASH = None
	last_forwarded_msg_id = 0 #store last forwarded message id
	target_from = None
	target_to = None
	allowed_file_formats = []
	

	#Set all data for work
	def __init__(self, mode, from_ch, to_ch, words_list, file_formats, api_id, api_hash):
		self.mode = mode
		self.target_from = from_ch
		self.target_to = to_ch
		self.words_list = words_list #for mode 1
		self.API_ID = api_id
		self.API_HASH = api_hash
		self.allowed_file_formats = file_formats #for mode 4

	#authorization throught pyrogram 
	def auth(self):
		try:
			self.app = Client("forward", api_id=self.API_ID, api_hash=self.API_HASH)
			self.app.start()
		except NameError:
			print(NameError)

	#Get last message from target channel
	#Store message id from it and get message object
	def get_target_last_post(self):
		try:
			target_message_history = self.app.get_history(self.target_from, limit=1)
			self.target_last_msg_id = target_message_history[0].message_id
			self.target_last_msg_obj = self.app.get_messages(self.target_from, self.target_last_msg_id)
		except NameError:
			print(NameError)
	
	#Get last message from second channel
	#Store last forwarded message id if doesn't exist then will have None value
	def get_host_last_post(self):
		try:
			host_message_history = self.app.get_history(self.target_to, limit=1)
			self.host_last_msg_id = host_message_history[0].forward_from_message_id
		except NameError:
			print(NameError)

	#Check type (text, voice, photo etc.) of message by the key if exist return true otherwise false
	def check_key(self, key):
		try:
			if (self.target_last_msg_obj[key] != None):
				return 1
			else:
				return 0
		except NameError:
			return 0

	#for mode 1
	#Check type if message has text or caption then match values from array (words_list)
	#Return true if any words from array match with text in the post
	def check_message_text(self):
		try:
			key = None
			if (self.target_last_msg_obj['text'] != None):
				key = "text"
			elif (self.target_last_msg_obj['caption'] != None):
				key = "caption"
			if key != None:
				for i in range(0, len(self.words_list)):
					if self.words_list[i].lower() in self.target_last_msg_obj[key].lower():
						return 1
				return 0
		except NameError:
			return 0
	
	#for mode 4
	#Check type of message and then cut file format, match it with array (allowed_file_formats)
	#Return true if any value from array match with file format in the post
	def check_document_format(self):
		try:
			if (self.target_last_msg_obj['document'] != None):
				file_format = self.target_last_msg_obj['document']['file_name'].split('.')
				file_format = file_format[len(file_format)-1].lower()
				for i in range(0, len(self.allowed_file_formats)):
					if self.allowed_file_formats[i] == file_format:
						return 1
				return 0
			else:
				return 0
		except NameError:
			return 0

	'''
	Match last message id from target channel and last forwarded message id from second channel
	Check if post from target channel already post to prevent re-post
	Select mode and check the conformity of the conditions, in case of success forward the message
	Set new last forwarded message id
	'''
	def match_messages(self):
		try:
			if ((self.target_last_msg_id != self.host_last_msg_id) and (self.last_forwarded_msg_id != self.target_last_msg_id)):
				if (self.mode == 0):
					forward = True
				elif(self.mode == 1):
					if (self.check_message_text()):
						forward = True
				elif(self.mode == 2):
					if (self.check_key("photo")):
						forward = True
				elif(self.mode == 3):
					if (self.check_key("document")):
						forward = True
				elif(self.mode == 4):
					if (self.check_document_format()):
						forward = True
				elif(self.mode == 5):
					if (self.check_key("audio")):
						forward = True
				elif(self.mode == 6):
					if (self.check_key("video")):
						forward = True
				elif(self.mode == 7):
					if (self.check_key("voice")):
						forward = True
				if forward:
					self.target_last_msg_obj.forward(self.target_to)
					self.last_forwarded_msg_id = self.target_last_msg_id
					return 1
				else:
					return 0
		except:
			pass

	#Get current time
	def get_current_datetime(self):
		now = datetime.datetime.now()
		tm = now.strftime("%Y-%m-%d %H:%M:%S")
		return tm

	#Main functoin
	def start(self):
		self.get_target_last_post()
		self.get_host_last_post()
		if self.match_messages(): #if message forwarded print it in console
			print("["+ self.get_current_datetime() +"] Forwarded message ID: "+ str(self.target_last_msg_id))