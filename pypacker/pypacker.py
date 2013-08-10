"""Simple packet creation and parsing."""

import copy
import itertools
import socket
import struct
import logging
import copy
from . import triggerlist

logging.basicConfig(format="%(levelname)s (%(funcName)s): %(message)s")
#logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.DEBUG)
logger = logging.getLogger("pypacker")
logger.setLevel(logging.WARNING)
#logger.setLevel(logging.INFO)
#logger.setLevel(logging.DEBUG)

class Error(Exception): pass
class UnpackError(Error): pass
class NeedData(UnpackError): pass


class MetaPacket(type):
	"""
	This Metaclass is a more efficient way of setting attributes than
	using __init__. This is done by reading name / format / default out
	of __hdr__ in every subclass. This configuration is set one time
	when loading the module (not at instatiation). Actual values are
	retrieved using "obj.field" notation.
	CAUTION: list et al are _SHARED_ among all classes! A copy is needed
		on changes to them.
	General note: __new__ is called before __init__
	"""
	def __new__(cls, clsname, clsbases, clsdict):
		t = type.__new__(cls, clsname, clsbases, clsdict)
		# all header field names (shared)
		t.__hdr_fields__ = []
		# List of tuples of (pos, "name", TriggerListClass) pairs to be added on init of a packet (if any)
		# This way every Packet gets is own copy of dynamic fields: no copies needed but more overhead on __init__()
		t.__hdr_dyn__ = []
		# get header-infos from subclass
		st = getattr(t, "__hdr__", None)

		if st is not None:
			#logger.debug("loading meta for: %s, st: %s" % (clsname, st))
			# all header formats including byte order
			t.__hdr_fmt__ = [ getattr(t, "__byte_order__", ">")]
			pos = 0
			dyn_added = 0

			for x in st:
				#logger.debug("meta: %s -> %s" % (x[0], x[2]))
				# check if static field / no TriggerList (int, bytes etc.)
				if type(x[2]) is not type:
					t.__hdr_fields__.append(x[0])
					# set initial value
					setattr(t, x[0], x[2])
					t.__hdr_fmt__.append(x[1])
				else:
					#logger.debug("got dynamic field: %s=%s" % (x[0], x[2]))
					# set initial value
					setattr(t, x[0], None)
					# remmember for later instantiation
					t.__hdr_dyn__.append((pos+dyn_added, x[0], x[2]))
					dyn_added += 1
				pos += 1

			# header fields as set for performance reasons (shared)
			t.__hdr_fields_set__ = set(t.__hdr_fields__)
			# current format bytestring as string for convenience
			t.__hdr_fmtstr__ = "".join(t.__hdr_fmt__)
			#logger.debug("formatstring is: %s" % t.__hdr_fmtstr__)
			t.__hdr_len__ = struct.calcsize(t.__hdr_fmtstr__)			
			# body as raw byte string (None if handler present)
			t._data = b""
			# name of the attribute which holds the object representing the body aka the body handler
			t._bodytypename = None
			# callback to the next lower layer (eg for checksum on IP->TCP/UDP)
			t._callback = None
			# track changes to header values and data: This is needed for layers like TCP for
			# checksum-recalculation. Set to "True" on changes to header/body values, set to False on "bin()"
			## track changes to header values
			t._header_changed = False
			## track changes to header format. This will happen when adding new header or using TriggerLists
			t._header_format_changed = False
			## track changes to body value like [None | bytes | body-handler] -> [None | bytes | body-handler]
			t.body_changed = False
			# cache header for performance reasons, this will be set to None on every change to header values
			t._header_cached = None
			# objects which get notified on changes on _header_ values via "__setattr__()" (shared)
			# TODO: use sets here
			t._changelistener = []
			# skip parsing upper layers for performance reasons
			# set via Classname.skip_upperlayer = [True|False]
			t.skip_upperlayer = False
		return t

class Packet(object, metaclass=MetaPacket):
	"""
	Base packet class, with metaclass magic to generate members from
	self.__hdr__. This class can be instatiated via:

		Packet(byte_array)
		Packet(key1=val1, key2=val2, ...)
	
	Every packet got an optional header and an optional body.
	Body-data can be raw byte string OR a packet itself (the body handler).
	which stores the data. The following schema illustrates the Packet-structure:

	Packet structure
	================
	[Packet:
	[headerfield1]
	[headerfield2]
	...
	[headerfieldN]
		[Packet (handler):
		[headerfield1]
		...
			[Packet (handler):
			... 
				[Packet: raw data]
	]]]


	Requirements
	============
		- Auto-decoding of headers via given format-patterns
		- Auto-decoding of body-handlers like IP -> parse IP-data -> add TCP-handler to IP -> parse TCP-data..
		- Access of fields via "layer1.key" notation
		- some members can be set/retrieved using convenient string-represenations beneath the
			byte-represenation (see Ethernet or IP). This is done by appending "_s" to the attributename:
			obj.key_s = "stringval"
			bytes_or_str = obj.key_s
			Convenient access should be set via: varname_s = pypacker.Packet._get_property_XXX("varname")
		- Access of higher layers via layer1.layer2.layerX or "layer1[layerX]" notation
		- Concatination via "layer1 + layer2 + layerX"
		- There are two types of headers:
			1) static (same order, pre-defined header-names, constant format,
				can be extended by inserting new ones at arbitrary positions)
			2) dynamic (Packet based or textual protocol-headers, changes in format, length and order)
				These header got format "None" (auto-set when adding new header fields)

				Usage with Packet:
				- define an TriggerList as part of the value in __hdr__ (or add via _XXX_headerfield())
				- Packets in this list can be added/set/removed afterwards
					NOTE: deep-layer packets will be omitted in Packets, adding new headers
					to sub-packets after adding to a TriggerList is not permitted

				Usage for text-based protocols (eg when headername is given by protocol itself like
				"Host: xyz.org" in HTTP, usage):
				- define an TriggerList as part of the value in __hdr__ (or add via _XXX_headerfield())
				- define pack() in your TriggerList to reassemble packets (see HTTP).
					Single values in this list are represented as tuples like
					[(key, value), (key, value), ...]
				- Values in this list can be added/set/removed afterwards

				Examples can be found at the ip and tcp-implementations.
		- Header-values with length < 1 Byte should be set by using properties
		- Header formats can not be updated directly
		- Ability to check direction to other Packets via "direction()"
		- Generic callback for rare cases eg where upper layer needs
			to know about lower ones (like TCP->IP for checksum calculation)
		- No correction of given raw packet-data eg checksums when creating a
			packet from it (exception: if the packet can't be build without
			correct data -> raise exception). The internal state will only
			be updated on changes to headers or data.
		- no plausability-checks when changing headers/date manually (type-infos have to be set manually)
		- checksums are auto-recalculated until set manualy
		- General rule: less changes to headers/body-data = more performance


	New Protocols are added by subclassing Packet and defining fields via "__hdr__"
	as a list of (name, format, default value) tuples. __byte_order__ can be set to
	override the default ('>').
	Extending classes should overwrite the "_dissect"-method for diessction the given data.

	Call-flow
	=========
		pypacker(__init__) -auto called->
			-> _dissect(): get to know/verify the real header-structure
				-> (optional): call _add/set_headerfield() to change header structure
				-> (optional): call _parse_handler() setting a handler representing an upper-layer
			-auto called-> _unpack(): set all header values and data using the given format.

	Exceptionally a callback can be used for backward signaling this purposes.
	
	Examples:

	>>> class Foo(Packet):
	...	  __hdr__ = (("foo", "I", 1), ("bar", "H", 2), ("baz", "4s", "quux"))
	...
	>>> foo = Foo(bar=3)
	>>> foo
	Foo(bar=3)
	>>> foo.bin()
	b"\x00\x00\x00\x01\x00\x03quux"
	>>> foo.bar
	3
	>>> foo.baz
	b"quux"
	>>> foo.foo = 7
	>>> foo.baz = "whee"
	>>> foo
	Foo(baz="whee", foo=7, bar=3)
	>>> Foo(b"hello, world!")
	Foo(baz=" wor", foo=1751477356L, bar=28460, data="ld!")
	"""

	# dict for saving body datahandler globaly: { Classname : {id : HandlerClass} }
	_handler = {}
	# basic types allowed for header-values
	__TYPES_ALLOWED_BASIC = set([bytes, int, float])
	# constants for Packet-directons: cancat via DIR_SAME | DIR_REV = DIR_BOTH
	DIR_EOL		= 0	# end of layer reached (neutral)
	DIR_SAME	= 1	# same direction as previous packet
	DIR_REV		= 2	# reversed direction
	DIR_BOTH	= 3	# no direction at all
	

	def __init__(self, *args, **kwargs):
		"""
		Packet constructor with (buf) or ([field=val,...]) prototype.

		buf --- packet buffer to unpack as bytes
		keywords --- arguments correspond to header fields to be set
		"""

		for pos_k_v in self.__hdr_dyn__:
			tl_instance = pos_k_v[2]()
			self._insert_headerfield(pos_k_v[0], pos_k_v[1], None, tl_instance, skip_update=True)
		if self._header_format_changed:
			self._update_fmtstr()

		if args:
			# buffer given: use it to set header fields and body data
			# Don't allow empty buffer, we got the headerfield-constructor for that.
			# Allowing default-values giving empty buffer would lead to confusion:
			# there is no way do disambiguate "no body" from "default value set".
			# Empty buffer in A for subhandler B (this Packet) should lead to A
			# having (data=b"", bodyhandler=None)
			if len(args[0]) == 0:
				raise NeedData("Empty buffer given!")
		
			try:
				# this is called on the extended class if present
				self._dissect(args[0])
				self._unpack(args[0])
			except UnpackError as ex:
				raise UnpackError("could not unpack %s: %s" % (self.__class__.__name__, ex))
		else:
			# n headerfields given to set (n >= 0)
			# additional parameters given, those overwrite the class-based attributes
			#logger.debug("New Packet with keyword args (%s)" % self.__class__.__name__)
			for k, v in kwargs.items():
				#logger.debug("setting: %s=%s" % (k, v))
				object.__setattr__(self, k, v)
			# no reset: directly assigned = changed

	def __len__(self):
		"""Return total length (= header + all upper layer data) in bytes."""
		if self._data is not None:
			return self.hdr_len + len(self._data)
		else:
			return self.hdr_len + len( object.__getattribute__(self, self._bodytypename) )

	#
	# Public access to header length: keep it uptodate
	#
	def __get_hdrlen(self):
		# format changed: recalculate length
		if self._header_format_changed:
			self._update_fmtstr()
		return self.__hdr_len__

	hdr_len = property(__get_hdrlen)

	#
	# Public access to format string: keep it uptodate
	#
	def __get_fmtstr(self):
		# format changed: rebuild format string
		if self._header_format_changed:
			self._update_fmtstr()
		return self.__hdr_fmtstr__

	hdr_fmtstr = property(__get_fmtstr)

	# Two types of data: raw bytes or handler, use property for convenient access
	# The following assumption must be fullfilled: (handler=obj, data=None) OR (handler=None, data=b"")
	def __get_data(self):
		"""
		Return raw data bytes or handler bytes if present. This is the same
		as calling bin() but excluding this header and without resetting changed-status.
		"""
		# return handler as bytes
		if self._bodytypename is not None:
			hndl = object.__getattribute__(self, self._bodytypename)
			return hndl.pack_hdr() + hndl.data
		# return raw bytes
		else:
			return self._data

	def __set_data(self, value):
		"""Allow obj.data = [None | b"" | Packet]. None will reset any body handler."""
		if type(value) is bytes:
			if self._bodytypename is not None:
				self._set_bodyhandler(None)
			# track changes to raw data
			self.body_changed = True
			#logger.debug("setting new raw data: %s (type=%s)" % (v, self.bodytypename))
			self._data = value
		# set body handler (can be None), assume value is a Packet
		else:
			# this will set the changes status to true
			self._set_bodyhandler(value)
	data = property(__get_data, __set_data)

	# public access to body handler.
	def __get_hndl(self):
		"""return --- handler object or None if not present."""
		try:
			return object.__getattribute__(self, self._bodytypename)
		except:
			return None

	def __set_hndl(self, hndl):
		"""Set a new handler. This is the same as calling obj.data = value."""
		self.data = hndl

	handler = property(__get_hndl, __set_hndl)

	def __setattr__(self, k, v):
		"""
		Set value of an attribute "k" via "a.k=v".
		"""
		object.__setattr__(self, k, v)

		if k in self.__hdr_fields_set__:
			#logger.debug("setting attribute: %s: %s->%s" % (self.__class__, k, v))
			self._header_changed = True
			self._header_cached = None
			self.__notity_changelistener()

	def __getitem__(self, k):
		"""
		Check every layer upwards (inclusive this layer) for the given Packet-Type
		and return the first matched instance or None if nothing was found.
		k --- Packet-type to seearch for
		"""
		p_instance = self

		while not type(p_instance) is k:
			btname = object.__getattribute__(p_instance, "_bodytypename")

			if btname is not None:
				# one layer up
				p_instance = object.__getattribute__(p_instance, btname)
			else:
				# layer was not found
				#logger.debug("searching item..")
				p_instance = None
				break
		#logger.debug("returning found packet-handler: %s->%s" % (type(self), type(p_instance)))
		return p_instance	

	def __add__(self, v):
		"""
		Handle concatination of layers like "Ethernet + IP + TCP" and make them accessible
		via "ethernet.ip.tcp" (class names as lowercase). Every "A + B" operation will return A,
		setting B as the handler (of the deepest handler) of A.

		NOTE: changes to A after creating Packet "A+B+C" will affect the new created Packet itself.
		Create a deep copy to avoid this behaviour.

		v --- the packet to be added as new highest layer for this packet
		"""
		# get highest layer from this packet
		highest_layer = self

		while highest_layer is not None:
			if highest_layer._bodytypename is not None:
				highest_layer = object.__getattribute__(highest_layer, highest_layer._bodytypename)
			else:
				break

		# connect callback from lower (this packet, highest_layer) to upper (v) layer eg IP->TCP
		v._callback = highest_layer._callback_impl
		highest_layer.data = v

		return self

	def __repr__(self):
		"""Verbose represention of this packet as "key=value"."""
		# recalculate fields like checksums, lengths etc
		if self._header_changed or self.body_changed:
			self.bin()
		l = [ "%s=%r" % (k, object.__getattribute__(self, k))
			for k in self.__hdr_fields__]
		if self._data is not None:
			l.append("data=%r" % self._data)
		else:
			l.append("handler=%s" % object.__getattribute__(self, self._bodytypename).__class__)
		return "%s(%s)" % (self.__class__.__name__, ", ".join(l))

	#
	# Methods for handling properties for convenient access eg: mac (bytes) -> mac (str), ip (bytes) -> ip (str)
	#
	def _get_property_mac(var):
		"""Create a get/set-property for a mac address as string-representation."""
		return property(lambda self: mac_bytes_to_str(object.__getattribute__(self, var)),
		lambda self, val: object.__setattr__(self, var, mac_str_to_bytes(val)))

	def _get_property_ip4(var):
		"""Create a get/set-property for a ip4 address as string-representation."""
		return property(lambda self: ip4_bytes_to_str(object.__getattribute__(self, var)),
		lambda self, val: object.__setattr__(self, var, ip4_str_to_bytes(val)))
	#
	#
	#

	def _dissect(self, buf):
		"""
		Parse a full layer using bytes in buf. This has to be overridden by a specific
		implementation which parses the protocol.
		"""
		pass

	def _unpack(self, buf):
		"""
		Unpack/import a full layer using bytes in buf and set all headers
		and data accordingly. This will use the current state of "__hdr_fields__"
		to set all field values. This will also set data if not allready set
		by overwriting class in "dissect()".
		NOTE: This is only called by the Packet class itself!

		buf --- the buffer to be parsed
		"""
		cnt = 1

		try:
			self._header_cached = buf[:self.hdr_len]

			for k, v in zip(self.__hdr_fields__,
					struct.unpack(self.hdr_fmtstr, self._header_cached)):
				# only set non-TriggerList fields
				if self.__hdr_fmt__[cnt] != None:
					object.__setattr__(self, k, v)
				cnt += 1
		except IndexError:
			 raise NeedData("Not enough data to unpack: buf %d < %d" % (len(buf), self.hdr_len))

		# extending class didn't set data itself, set raw data
		if not self.body_changed:
			self._data = buf[self.__hdr_len__:]

		#logger.debug("header: %s, body: %s" % (self.__hdr_fmtstr__, self.data))
		# reset the changed-flags: original unpacked = no changes
		self.__reset_changed()

	def _parse_handler(self, type, buffer, offset_start=None, offset_end=None):
		"""
		Parse the handler using the given buffer and set it using the _set_bodyhandler() method.
		This will use the calling class as primary name to add the resulting handler to the handler-dict.
		On any error this will set raw bytes given for data.

		type --- A value to place the handler in the handler-dict like
			dict[Class.__name__][type] (eg type-id, port-number)
		buffer --- The buffer to be used to create the handler
		offset_start / offset_end --- The offsets in buffer to create a subset like buffer[offset_start:offset_end]
			Default is None for both.
		"""
		if self.skip_upperlayer:
			return

		try:
			type_class = Packet._handler[self.__class__.__name__][type]
			type_instance = type_class(buffer[offset_start:offset_end])
			self._set_bodyhandler(type_instance)
		except:
			# set raw bytes as data
			self.data = buffer[offset_start:offset_end]

	def _insert_headerfield(self, pos, name, format, value, skip_update=False):
		"""
		Insert a new headerfield into the current defined list.
		The new header field can be accessed via "obj.attrname".
		This should only be called at the beginning of the packet-creation process.

		pos --- position of header
		name --- name of header
		format --- format of header
		skip_update --- skip update of __hdr_fmtstr__, new header length won't be correct
		"""
		# list of headers via TriggerList (like TCP-options), add packet for status-handling
		if isinstance(value, triggerlist.TriggerList):
			value.packet = self
			# mark this header field as Triggerlist
			format = None
		elif type(value) not in Packet.__TYPES_ALLOWED_BASIC:
			raise Error("can't add this value as new header (no basic type or TriggerList): %s, type: %s" % (value, type(value)))

		object.__setattr__(self, name, value)

		# We need a new shallow copy: these attributes are shared
		if not hasattr(self, "__hdr_ind"):
			self.__hdr_fields__ = list( object.__getattribute__(self, "__hdr_fields__") )
			self.__hdr_fields_set__ = set(self.__hdr_fields__)
			self.__hdr_fmt__ = list( object.__getattribute__(self, "__hdr_fmt__") )
			self.__hdr_ind = True

		self.__hdr_fields__.insert(pos, name)
		self.__hdr_fields_set__.add(name)
		# skip format character
		self.__hdr_fmt__.insert(pos+1, format)

		# skip update for performance reasons
		if not skip_update:
			self._update_fmtstr()
		else:
			self._header_format_changed = True

	def _del_headerfield(self, pos, skip_update=False):
		"""
		Remove a headerfield from the current defined list.
		The new header field can be accessed via "obj.attrname".
		This should only be called at the beginning of the packet-creation process.

		pos --- position of header
		skip_update --- skip update of __hdr_fmtstr__
		"""
		# We need a new shallow copy: these attributes are shared, TODO: more performant
		if not hasattr(self, "__hdr_ind"):
			self.__hdr_fields__ = list( object.__getattribute__(self, "__hdr_fields__") )
			self.__hdr_fields_set__ = set(self.__hdr_fields__)
			self.__hdr_fmt__ = list( object.__getattribute__(self, "__hdr_fmt__") )
			self.__hdr_ind = True

		self.__hdr_fields_set__.remove(self.__hdr_fields__[pos])
		del self.__hdr_fields__[pos]
		del self.__hdr_fmt__[pos+1]

		if not skip_update:
			self._update_fmtstr()
		else:
			self._header_format_changed = True

	def _add_headerfield(self, name, format, value, skip_update=False):
		"""
		Add a new headerfield to the end of all fields. See _insert_headerfield() for more infos.
		"""
		self._insert_headerfield(len(self.__hdr_fields__), name, format, value, skip_update)

	def _callback_impl(self, id):
		"""
		Generic callback. The calling class must know if/how this callback
		is implemented for this class and which id is needed
		(eg. id "calc_sum" for IP checksum calculation in TCP used of pseudo-header).

		id --- a unique id for the given callback
		"""
		pass

	def direction(self, next, last_type=None):
		"""
		Every layer can check the direction to the given layer (of the next packet).
		This continues on the next upper layer if a direction was found.
		This stops if there is no direction or the body data is not a Packet.
		The extending class should call the super implementation on overwriting.
		This will return DIR_EOL if the body (self and next) is just raw bytes.

		next --- Packet to be compared
		last_type --- the last Packet-type which has to be compared in the layer-stack of this packet (returns DIR_EOL)
		return --- DIR_OUT (outgoing direction) | DIR_IN (incoming direction) | DIR_EOL (end of layer reached) | DIR_BOTH
		"""
		# last type reached and everything is directed so far
		if type(last_type) == type(self):	# self is never None
			#logger.debug("direction? DIR_EOL: last type reached")
			return Packet.DIR_EOL
		# EOL if one of both handlers is None (body = b"xyz")
		# Example: TCP ACK (last step of handshake, no payload) <-> TCP ACK + Telnet
		elif self._bodytypename is None or next._bodytypename is None:
			#logger.debug("direction? DIR_EOL: self/next is None: %s/%s" % (self.bodytypename, next.bodytypename))
			#return self.bodytypename == next.bodytypename
			return Packet.DIR_EOL
		# body is a Packet and this layer could be directed, we must go deeper!
		body_p_this = object.__getattribute__(self, self._bodytypename)
		body_p_next = object.__getattribute__(next, next._bodytypename)
		# check upper layers
		#logger.debug("direction? checking next layer")
		return  body_p_this.direction(body_p_next, last_type)

	def _update_fmtstr(self):
		"""
		Update header format string and length using current fields.
		NOTE: only called if format has changed eg after addin/removing header fields,
		changes in TriggerList etc.
		"""
		hdr_fmt_tmp = [ self.__hdr_fmt__[0] ]	# byte-order is set via first character

		# we need to preserve the order of formats / fields
		for idx, field in enumerate(self.__hdr_fields__):
			val = object.__getattribute__(self, field)
			# Three options:
			# - value bytes			-> add given format or calculate by length
			# - value TriggerList		(found via format None)
			#	- type Packet		-> a TriggerList of packets, reassemble formats
			#	- type tuple		-> a TriggerList of tuples, call "reassemble" and use format "s"
			#logger.debug("format update with field/type/val: %s/%s/%s" % (field, type(val), val))
			if self.__hdr_fmt__[1 + idx] is not None:				# bytes/int/float
				hdr_fmt_tmp.append( self.__hdr_fmt__[1 + idx] )			# skip byte-order character
			elif len(val) > 0:							# assume TriggerList
				if isinstance(val[0], Packet):					# Packet
					for p in val:
						hdr_fmt_tmp.append(p.hdr_fmtstr[1:])		# skip byte-order character
						if len(p.data) > 0:
							hdr_fmt_tmp.append( "%ds" % len(p.data))# add data-format
				else:								# tuple or whatever
					# call pack-implementation to get length of this header (eg HTTP header)
					hdr_fmt_tmp.append("%ds" % len(val.pack_cb()))

		hdr_fmt_tmp = "".join(hdr_fmt_tmp)

		# update header info, avoid recursive calls
		self.__hdr_fmtstr__ = hdr_fmt_tmp
		self._header_format_changed = False
		self.__hdr_len__ = struct.calcsize(hdr_fmt_tmp)

	def _set_bodyhandler(self, hndl):
		"""
		Set handler to decode the actual body data using the given handler
		and make it accessible via layername.addedtype like ethernet.ip.
		This will take the classname of the given handler as lowercase.
		If handler is None any handler will be reset and data will be set to an
		empty byte string.

		hndl --- the handler to be set (None or Packet)
		"""
		if hndl is not None and not isinstance(hndl, Packet):
			raise Error("can't set handler which is not a Packet")

		# switch (handler=obj, data=None) to (handler=None, data=b'')
		if hndl is None:
			self._bodytypename = None
			# avoid (data=None, handler=None)
			self._data = b""
		# set a new body handler
		else:
			# associate ip, arp etc with handler-instance to call "ether.ip", "ip.tcp" etc
			self._bodytypename = hndl.__class__.__name__.lower()
			hndl._callback = self._callback_impl
			object.__setattr__(self, self._bodytypename, hndl)
			self._data = None
		
		# new body handler means body data changed
		self.body_changed = True

	def bin(self):
		"""
		Return this header and body (including all upper layers) as byte string
		and reset changed-status.
		"""
		# preserve status until we got all data of all sub-handlers
		# needed for eg IP (changed) -> TCP (check changed for sum)
		if self._bodytypename is not None:
			data_tmp = object.__getattribute__(self, self._bodytypename).bin()
		else:
			data_tmp = self._data

		# now every layer got informed about our status, reset it
		self.__reset_changed()
		return self.pack_hdr() + data_tmp

	def pack_hdr(self, raw=False):
		"""
		Return header as byte string in order of appearance in __hdr_fields__.

		raw --- True: don't format header values, return them as list of bytes, False: return as one byte string
		"""
		if self._header_format_changed:
			self._update_fmtstr()
		# return cached data if nothing changed
		elif self._header_cached is not None and not raw:
			#logger.warning("returning cached header (hdr changed=%s): %s->%s" %\
			#	(self.header_changed, self.__class__.__name__, self._header_cached))
			return self._header_cached

		try:
			hdr_bytes = []
			# skip fields with value None
			for idx, field in enumerate(self.__hdr_fields__):
			#for field in self.__hdr_fields__:
				val = object.__getattribute__(self, field)
				# Three options:
				# - value bytes			-> add given bytes
				# - value TriggerList		(found via format None)
				#	- type Packet		-> a TriggerList of packets, reassemble bytes
				#	- type tuple		-> a Triggerlist of tuples, call "pack_cb"
				#logger.debug("packing header with field/type/val: %s/%s/%s" % (field, type(val), val))
				if self.__hdr_fmt__[1 + idx] is not None:			# bytes/int/float
					hdr_bytes.append( val )
				elif len(val) > 0:
					if isinstance(val[0], Packet):				# Packet
						for p in val:
							hdr_bytes.extend( p.pack_hdr(raw=True) )# list of bytes
							# packet as header: data is part of this header!
							if len(p.data) > 0:
								hdr_bytes.append( p.data )
					else:							# tuple or whatever
						hdr_bytes.append( val.pack_cb() )

			#logger.debug("header bytes for %s: %s = %s" % (self.__class__.__name__, self.__hdr_fmtstr__, hdr_bytes))
			self._header_cached = struct.pack( self.__hdr_fmtstr__, *hdr_bytes )

			if not raw:
				return self._header_cached
			else:
				return hdr_bytes
		except Exception as e:
			logger.warning("error while packing header: %s" % e)

	def _changed(self):
		"""Check if this or any upper layer changed in header or body."""
		changed = False

		p_instance = self
		while p_instance is not None:
			if p_instance._header_changed or p_instance.body_changed:
				changed = True
				p_instance = None
				break
			elif p_instance._bodytypename is not None:
				p_instance = object.__getattribute__(p_instance, p_instance._bodytypename)
			else:
				p_instance = None
		return changed

	def __reset_changed(self):
		"""Set the header/body changed-flag to False. This won't clear caches."""
		self._header_changed = False
		# this will reset the cache
		#self._header_changed = True
		self.body_changed = False

	def add_change_listener(self, listener_cb):
		"""
		Add a new callback to be called on changes to header oder body.
		The only argument is this packet itself.
	
		listener_cb --- the change listener to be added as callback-function
		"""
		if len(self._changelistener) == 0:
			# copy list (shared)
			self._changelistener = []
		# avoid same listener multiple times
		if not listener_cb in self._changelistener:
			self._changelistener.append(listener_cb)

	def remove_change_listener(self, listener_cb, remove_all=False):
		"""
		Remove callback from the list of listeners.
	
		listener_cb --- the change listener to be removed
		remove_all --- remove all listener at once
		"""
		#logger.debug("remove_change_listener, present: %d /// %s /// %s" % (len(self._changelistener),
		#	self._changelistener, listener_cb))
		if not remove_all:
			self._changelistener.remove(listener_cb)
		else:
			del self._changelistener[:]

	def __notity_changelistener(self):
		"""
		Notify listener about changes.
		"""
		try:
			for listener_cb in self._changelistener:
				listener_cb(self)
		except Exception as e:
			logger.debug("error when informing listener: %s" % e)
			pass

	def __load_handler(clz, clz_add, handler):
		"""
		Load Packet handler using a shared dictionary.

		clz_add --- class to be added
		handler --- dict of handlers to be set like { id : class }, id can be a tuple of values
		"""

		clz_name = clz_add.__name__

		try:
			Packet._handler[clz_name]
			logger.debug(">>> handler already loaded: %s (%d)" % clz_add)
			return
		except KeyError:
			pass

		logger.debug("adding classes as handler: [%s] = %s" % (clz_add, handler))

		Packet._handler[clz_name] = {}

		for k,v in handler.items():
			if type(k) is not tuple:
				Packet._handler[clz_name][k] = v
			else:
				for k_item in k:
					Packet._handler[clz_name][k_item] = v

	load_handler = classmethod(__load_handler)


#
# utility methods
#
def mac_str_to_bytes(mac_str):
	"""Convert mac address AA:BB:CC:DD:EE:FF to byte representation."""
	return b"".join([ bytes.fromhex(x) for x in mac_str.split(":") ])
def mac_bytes_to_str(mac_bytes):
	"""Convert mac address from byte representation to AA:BB:CC:DD:EE:FF."""
	return "%02x:%02x:%02x:%02x:%02x:%02x" % struct.unpack("BBBBBB", mac_bytes)
def ip4_str_to_bytes(ip_str):
	"""Convert ip address 127.0.0.1 to byte representation."""
	ips = [ int(x) for x in ip_str.split(".")]
	return struct.pack("BBBB", ips[0], ips[1], ips[2], ips[3])
def ip4_bytes_to_str(ip_bytes):
	"""Convert ip address from byte representation to 127.0.0.1."""
	return "%d.%d.%d.%d" % struct.unpack("BBBB", ip_bytes)
def byte2hex(buf):
	"""Convert a bytestring to a hex-represenation:
	b'1234' -> '\x31\x32\x33\x34'"""
	return "\\x"+"\\x".join( [ "%02X" % x for x in buf ] )

# XXX - ''.join([(len(`chr(x)`)==3) and chr(x) or '.' for x in range(256)])
__vis_filter = """................................ !"#$%&\'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[.]^_`abcdefghijklmnopqrstuvwxyz{|}~................................................................................................................................."""

def hexdump(buf, length=16):
	"""Return a hexdump output string of the given buffer."""
	n = 0
	res = []
	while buf:
		line, buf = buf[:length], buf[length:]
		hexa = " ".join(["%02x" % ord(x) for x in line])
		line = line.translate(__vis_filter)
		res.append("  %04d:	 %-*s %s" % (n, length * 3, hexa, line))
		n += length
	return "\n".join(res)

try:
	import dnet
	def in_cksum_add(s, buf):
		return dnet.ip_cksum_add(buf, s)
	def in_cksum_done(s):
		return socket.ntohs(dnet.ip_cksum_carry(s))
except ImportError:
	import array
	def in_cksum_add(s, buf):
		n = len(buf)
		#logger.debug("buflen for checksum: %d" % n)
		cnt = int(n / 2) * 2
		#logger.debug("slicing at: %d, %s" % (cnt, type(cnt)))
		a = array.array("H", buf[:cnt])
		#logger.debug("2-byte values: %s" % a)
		#logger.debug(buf[-1].to_bytes(1, byteorder='big'))

		if cnt != n:
			a.append(struct.unpack("H", buf[-1].to_bytes(1, byteorder="big") + b"\x00")[0])
			##a.append(buf[-1].to_bytes(1, byteorder="big") + b"\x00")
		return s + sum(a)
	def in_cksum_done(s):
		# add carry to sum itself
		s = (s >> 16) + (s & 0xffff)
		s += (s >> 16)
		# return complement of sums
		return socket.ntohs(~s & 0xffff)

def in_cksum(buf):
	"""Return computed Internet Protocol checksum."""
	return in_cksum_done(in_cksum_add(0, buf))
