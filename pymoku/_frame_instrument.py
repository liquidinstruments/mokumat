
# Pull in Python 3 string object on Python 2.
from builtins import str

import select, socket, struct, sys
import os, os.path
import logging, time, threading, math
import zmq

from collections import deque
from queue import Queue, Empty

from pymoku import Moku, FrameTimeout, NoDataException, StreamException, UncommittedSettings, dataparser, _stream_handler, _get_autocommit

from ._stream_instrument import _STREAM_STATE_NONE, _STREAM_STATE_RUNNING, _STREAM_STATE_WAITING, _STREAM_STATE_INVAL, _STREAM_STATE_FSFULL, _STREAM_STATE_OVERFLOW, _STREAM_STATE_BUSY, _STREAM_STATE_STOPPED

from . import _instrument

log = logging.getLogger(__name__)

class FrameQueue(Queue):
	def put(self, item, block=True, timeout=None):
		# Behaves the same way as default except that instead of raising Full, it
		# just pushes the item on to the deque anyway, throwing away old frames.
		self.not_full.acquire()
		try:
			if self.maxsize > 0 and block:
				if timeout is None:
					while self._qsize() == self.maxsize:
						self.not_full.wait()
				elif timeout < 0:
					raise ValueError("'timeout' must be a non-negative number")
				else:
					endtime = _time() + timeout
					while self._qsize() == self.maxsize:
						remaining = endtime - _time()
						if remaining <= 0.0:
							break
						self.not_full.wait(remaining)
			self._put(item)
			self.unfinished_tasks += 1
			self.not_empty.notify()
		finally:
			self.not_full.release()

	def get(self, block=True, timeout=None):
		item = None
		while True:
			try:
				item = Queue.get(self, block=block, timeout=timeout or 1)
			except Empty:
				if timeout is None:
					continue
				else:
					raise
			else:
				return item

	# The default _init for a Queue doesn't actually bound the deque, relying on the
	# put function to bound.
	def _init(self, maxsize):
		self.queue = deque(maxlen=maxsize)

class InstrumentData(object):
	"""
	Superclass representing a full frame of some kind of data. This class is never used directly,
	but rather it is subclassed depending on the type of data contained and the instrument from
	which it originated. For example, the :any:`Oscilloscope` instrument will generate :any:`VoltsData`
	objects, where :any:`VoltsData` is a subclass of :any:`InstrumentData`.
	"""
	def __init__(self):
		self._complete = False
		self._chs_valid = [False, False]

		#: Channel 1 raw data array. Present whether or not the channel is enabled, but the contents
		#: are undefined in the latter case.
		self._raw1 = []

		#: Channel 2 raw data array.
		self._raw2 = []

		self._stateid = None
		self._trigstate = None

		#: Frame number. Increments monotonically but wraps at 16-bits.
		self._frameid = 0

		#: Incremented once per trigger event. Wraps at 32-bits.
		self.waveformid = 0

		self._flags = None

	def add_packet(self, packet):
		hdr_len = 15
		if len(packet) <= hdr_len:
			# Should be a higher priority but actually seems unexpectedly common. Revisit.
			log.debug("Corrupt frame recevied, len %d", len(packet))
			return

		data = struct.unpack('<BHBBBBBIBH', packet[:hdr_len])
		frameid = data[1]
		instrid = data[2]
		chan = (data[3] >> 4) & 0x0F

		self._stateid = data[4]
		self._trigstate = data[5]
		self._flags = data[6]
		self.waveformid = data[7]
		self._source_serial = data[8]

		if self._frameid != frameid:
			self._frameid = frameid
			self._chs_valid = [False, False]

		log.debug("AP ch %d, f %d, w %d", chan, frameid, self.waveformid)

		# For historical reasons the data length is 1026 while there are only 1024
		# valid samples. Trim the fat.
		if chan == 0:
			self._chs_valid[0] = True
			self._raw1 = packet[hdr_len:-8]
		else:
			self._chs_valid[1] = True
			self._raw2 = packet[hdr_len:-8]

		self._complete = all(self._chs_valid)

		if self._complete:
			if not self.process_complete():
				self._complete = False
				self._chs_valid = [False, False]

	def process_complete(self):
		# Designed to be overridden by subclasses needing to transform the raw data in to Volts etc.
		return True

	def process_buffer(self):
		# Designed to be overridden by subclasses needing to add x-axis to buffer data etc.
		return True


# Revisit: Should this be a Mixin? Are there more instrument classifications of this type, recording ability, for example?
class FrameBasedInstrument(_stream_handler.StreamHandler, _instrument.MokuInstrument):
	def __init__(self):
		super(FrameBasedInstrument, self).__init__()
		self._buflen = 1
		self._queue = FrameQueue(maxsize=self._buflen)
		self._hb_forced = False

	def _set_frame_class(self, frame_class, **frame_kwargs):
		self._frame_class = frame_class
		self._frame_kwargs = frame_kwargs

	def _flush(self):
		""" Clear the Frame Buffer.
		This is normally not required as one can simply wait for the correctly-generated frames to propagate through
		using the appropriate arguments to :any:`get_data`.
		"""
		with self._queue.mutex:
			self._queue.queue.clear()

	def _set_buffer_length(self, buflen):
		""" Set the internal frame buffer length."""
		self._buflen = buflen
		self._queue = FrameQueue(maxsize=buflen)

	def _get_buffer_length(self):
		""" Return the current length of the internal frame buffer
		"""
		return self._buflen


	def get_data(self, timeout=None, wait=True):
		""" Get full-resolution data from the instrument.

		This will pause the instrument and download the entire contents of the instrument's
		internal memory. This may include slightly more data than the instrument is set up
		to record due to rounding of some parameters in the instrument.

		All settings must be committed before you call this function. If *pymoku.autocommit=True*
		(the default) then this will always be true, otherwise you will need to have called
		:any:`commit` first.

		The download process may take a second or so to complete. If you require high rate
		data, e.g. for rendering a plot, see `get_realtime_data`.

		:type timeout: float
		:param timeout: Maximum time to wait to receive the samples over the network, or *None* 
			for indefinite.

		:return: :any:`InstrumentData` subclass, specific to the instrument.
		"""
		if self._moku is None: raise NotDeployedException()

		if self.check_uncommitted_state():
			raise UncommittedSettings("Detected uncommitted instrument settings.")

		# Stop existing logging sessions
		self._stream_stop()

		# Block waiting on state to propagate (if wait=True)
		# This also gives us acquisition parameters for the buffer we will subsequently stream
		try:
			frame = self.get_realtime_data(timeout=timeout, wait=wait)
		except FrameTimeout:
			raise BufferTimeout('Timed out waiting on valid data.')

		# Check if it is already paused
		was_paused = self.get_pause()

		# Force a pause so we can start streaming the buffer out
		if not was_paused:
			self.set_pause(True)
			if not _get_autocommit():
				self.commit()

		# Get buffer data using a network stream
		self._stream_start(start=0, duration=0, use_sd=False, ch1=True, ch2=True, filetype='net')

		while True:
			try:
				self._stream_receive_samples(timeout)
			except NoDataException:
				break

		# Clean up data streaming threads
		self._stream_stop()

		# Set pause state to what it was before
		if not was_paused:
			self.set_pause(False)
			if not _get_autocommit():
				self.commit()

		channel_data = self._stream_get_processed_samples()
		self._stream_clear_processed_samples()

		# Take the channel buffer data and put it into an 'InstrumentData' object
		if(getattr(self, '_frame_class', None)):
			buff = self._frame_class(**self._frame_kwargs)
			buff.ch1 = channel_data[0]
			buff.ch2 = channel_data[1]
			buff.waveformid = frame.waveformid
			buff._stateid = frame._stateid
			buff._trigstate = frame._trigstate
			# Finalise the buffer processing stage
			buff.process_buffer()
			return buff
		else:
			raise Exception("Unable to process instrument data.")

	def get_realtime_data(self, timeout=None, wait=True):
		""" Get downsampled data from the instrument with low latency.

		Returns a new :any:`InstrumentData` subclass (instrument-specific), containing
		a version of the data that may have been downsampled from the original in order to
		be transferred quickly.

		This function always returns a new object at `framerate` (10Hz by default), whether
		or not there is new data in that object. This can be verified by checking the return
		object's *waveformid* parameter, which increments each time a new waveform is captured
		internally.

		The downsampled, low-latency nature of this data makes it particularly suitable for
		plotting in real time. If you require high-accuracy, high-resolution data for analysis,
		see `get_data`.

		If the *wait* parameter is true (the default), this function will wait for any new
		settings to be applied before returning. That is, if you have set a new timebase (for example),
		calling this with *wait=True* will guarantee that the object returned has this new timebase.
		This may include waiting for a trigger event, and therefore can take an arbitrary amount of
		time to return, or not return at all (if for example the instrument is paused), and therefore
		must have *timeout* set appropriately.

		:type wait: bool
		:param wait: If *true* (default), waits for a new waveform to be captured with the most
			recently-applied settings, otherwise just return the most recently captured data.
		:type timeout: float
		:param timeout: Maximum time to wait for a new frame. This makes most sense when combined
			with the *wait* parameter.
		:return: :any:`InstrumentData` subclass, specific to the instrument.
		"""
		try:
			# Dodgy hack, infinite timeout gets translated in to just an exceedingly long one
			endtime = time.time() + (timeout or sys.maxsize)
			while self._running:
				frame = self._queue.get(block=True, timeout=timeout)
				# Should really just wait for the new stateid to propagte through, but
				# at the moment we don't support stateid and stateid_alt being different;
				# i.e. we can't rerender already aquired data. Until we fix this, wait
				# for a trigger to propagate through so we don't at least render garbage
				if not wait or frame._trigstate == self._stateid:
					return frame
				elif time.time() > endtime:
					raise FrameTimeout()
				else:
					log.debug("Incorrect state received: %d/%d", frame._trigstate, self._stateid)
		except Empty:
			raise FrameTimeout()

	def _set_running(self, state):
		prev_state = self._running
		super(FrameBasedInstrument, self)._set_running(state)
		if state and not prev_state:
			self._fr_worker = threading.Thread(target=self._frame_worker)
			self._fr_worker.start()
		elif not state and prev_state:
			self._fr_worker.join()


	def _frame_worker(self):
		if(getattr(self, '_frame_class', None)):
			ctx = zmq.Context.instance()
			skt = ctx.socket(zmq.SUB)
			skt.connect("tcp://%s:27185" % self._moku._ip)
			skt.setsockopt_string(zmq.SUBSCRIBE, u'')
			skt.setsockopt(zmq.RCVHWM, 8)
			skt.setsockopt(zmq.LINGER, 5000)

			fr = self._frame_class(**self._frame_kwargs)

			try:
				while self._running:
					if skt in zmq.select([skt], [], [], 1.0)[0]:
						d = skt.recv()
						fr.add_packet(d)

						if fr._complete:
							self._queue.put_nowait(fr)
							fr = self._frame_class(**self._frame_kwargs)
			finally:
				skt.close()