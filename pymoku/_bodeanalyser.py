import math
import logging

from ._instrument import *
from . import _frame_instrument
from . import _utils

log = logging.getLogger(__name__)

REG_NA_SWEEP_FREQ_MIN_L 	= 64
REG_NA_SWEEP_FREQ_MIN_H 	= 65
REG_NA_SWEEP_FREQ_DELTA_L 	= 66
REG_NA_SWEEP_FREQ_DELTA_H 	= 67
REG_NA_LOG_EN				= 68
REG_NA_HOLD_OFF_L			= 69
REG_NA_SWEEP_LENGTH			= 71
REG_NA_AVERAGE_TIME			= 72
REG_NA_ENABLES				= 73
REG_NA_SWEEP_AMP_MULT		= 74
REG_NA_SETTLE_CYCLES		= 76
REG_NA_AVERAGE_CYCLES		= 77

_NA_FPGA_CLOCK 		= 125e6
_NA_DAC_SMPS 		= 1e9
_NA_DAC_VRANGE 		= 1
_NA_DAC_BITDEPTH 	= 2**16
_NA_DAC_BITS2V		= _NA_DAC_BITDEPTH/_NA_DAC_VRANGE
_NA_SCREEN_WIDTH	= 1024
_NA_FREQ_SCALE		= 2**48 / _NA_DAC_SMPS
_NA_FXP_SCALE 		= 2.0**30


class _BodeChannelData():

	def __init__(self, input_signal, gain_correction, front_end_scale, output_amp):

		# Extract the length of the signal (this varies with number of sweep points)
		sig_len = len(gain_correction)

		# De-interleave IQ values
		self.i_sig, self.q_sig = zip(*zip(*[iter(input_signal)]*2))
		self.i_sig = self.i_sig[:sig_len]
		self.q_sig = self.q_sig[:sig_len]

		# Calculates magnitude of a sample given I,Q and gain correction factors
		def calculate_magnitude(I,Q,G,frontend_scale):
			if I is None or Q is None:
				return None
			else:
				return 2.0 * math.sqrt((I or 0)**2 + (Q or 0)**2) * front_end_scale / (G or 1)

		self.magnitude = [calculate_magnitude(I,Q,G,front_end_scale) for I, Q, G in zip(self.i_sig, self.q_sig, gain_correction)]

		self.magnitude_dB = [ None if not x else 20.0 * math.log10(x / output_amp) for x in self.magnitude ]

		self.phase = [ None if (I is None or Q is None) else (math.atan2(Q or 0, I or 0))/(2.0*math.pi) for I, Q in zip(self.i_sig, self.q_sig)]

	def __json__(self):
		return { 'magnitude' : self.magnitude, 'magnitude_dB' : self.magnitude_dB, 'phase' : self.phase }


class BodeData(_frame_instrument.InstrumentData):
	"""
	Object representing a frame of dual-channel (amplitude and phase) vs frequency response data.

	This is the native output format of the :any:`BodeAnalyser` instrument.

	This object should not be instantiated directly, but will be returned by a call to
	:any:`get_data <pymoku.instruments.BodeAnalyser.get_data>` on the associated :any:`BodeAnalyser`
	instrument.

	- ``ch1.magnitude`` = ``[CH1_MAG_DATA]`` 
	- ``ch1.magnitude_dB`` = ``[CH1_MAG_DATA_DB]`` 
	- ``ch1.phase`` = ``[CH1_PHASE_DATA]`` 
	- ``ch2.magnitude`` = ``[CH2_MAG_DATA]`` 
	- ``ch2.magnitude_dB`` = ``[CH2_MAG_DATA_DB]`` 
	- ``ch2.phase`` = ``[CH2_PHASE_DATA]`` 
	- ``frequency`` = ``[FREQ]`` 
	- ``waveformid`` = ``n`` 

	"""
	def __init__(self, instrument, scales):
		super(BodeData, self).__init__(instrument)

		#: The frequency range associated with both channels
		self.frequency = []

		#: Obtain all data scaling factors relevant to current NetAn configuration
		self.scales = scales

	def __json__(self):
		return { 'ch1' : self.ch1, 'ch2' : self.ch2, 'frequency' : self.frequency, 'waveform_id' : self.waveformid }

	def process_complete(self):
		super(BodeData, self).process_complete()

		if self._stateid not in self.scales:
			log.debug("Can't render BodeData frame, haven't saved calibration data for state %d", self._stateid)
			self.complete = False
			return

		# Get scaling/correction factors based on current instrument configuration
		scales = self.scales[self._stateid]

		try:
			self.frequency = scales['frequency_axis']

			smpls = int(len(self._raw1) / 4)
			dat = struct.unpack('<' + 'i' * smpls, self._raw1)
			dat = [ x if x != -0x80000000 else None for x in dat ]

			self.ch1_bits = [ float(x) if x is not None else None for x in dat ]
			self.ch1 = _BodeChannelData(self.ch1_bits, scales['gain_correction'], scales['g1'], scales['sweep_amplitude_ch1'])

			smpls = int(len(self._raw2) / 4)
			dat = struct.unpack('<' + 'i' * smpls, self._raw2)
			dat = [ x if x != -0x80000000 else None for x in dat ]

			self.ch2_bits = [ float(x) if x is not None else None for x in dat ]
			self.ch2 = _BodeChannelData(self.ch2_bits, scales['gain_correction'], scales['g2'], scales['sweep_amplitude_ch2'])

		except (IndexError, TypeError, struct.error):
			# If the data is bollocksed, force a reinitialisation on next packet
			log.exception("Invalid Bode Analyser packet")
			self.frameid = None
			self.complete = False

		# A valid frame is there's at least one valid sample in each channel
		return self.ch1 and self.ch2

class BodeAnalyser(_frame_instrument.FrameBasedInstrument):
	""" Bode Analyser instrument object. This should be instantiated and attached to a :any:`Moku` instance.
	"""
	def __init__(self):
		super(BodeAnalyser, self).__init__()
		self._register_accessors(_na_reg_handlers)

		self.scales = {}
		self._set_frame_class(BodeData, instrument=self, scales=self.scales)

		self.id = 9
		self.type = "BodeAnalyser"

		self.sweep_amp_volts_ch1 = 0
		self.sweep_amp_volts_ch2 = 0

	def _calculate_sweep_delta(self, start_frequency, end_frequency, sweep_length, log_scale):
		if log_scale:
			sweep_freq_delta = round(((float(end_frequency)/float(start_frequency))**(1.0/(sweep_length - 1)) - 1) * _NA_FXP_SCALE)
		else:
			sweep_freq_delta = round((float(end_frequency - start_frequency)/(sweep_length-1)) * _NA_FREQ_SCALE)

		return sweep_freq_delta

	def _calculate_freq_axis(self):
		# Generates the frequency vector for plotting. 
		f_start = self.sweep_freq_min
		fs = []

		if self.log_en:
			# Delta register becomes a multiplier in the logarithmic case
			# Fixed-point precision is used in the FPGA multiplier (30 fractional bits)
			fs = [ f_start*(1 + (self.sweep_freq_delta/ _NA_FXP_SCALE))**n for n in range(self.sweep_length)]
		else:
			fs = [ (f_start + n*(self.sweep_freq_delta/_NA_FREQ_SCALE)) for n in range(self.sweep_length) ]

		return fs

	def _calculate_gain_correction(self, fs):
		sweep_freq = fs

		cycles_time = [0.0] * self.sweep_length

		if all(sweep_freq):
			cycles_time = [ self.averaging_cycles / sweep_freq[n] for n in range(self.sweep_length)]

		points_per_freq = [math.ceil(a * max(self.averaging_time, b) - 1e-12) for (a, b) in zip(sweep_freq, cycles_time)]

		average_gain = [0.0] * self.sweep_length
		gain_scale = [0.0] * self.sweep_length

		# Calculate gain scaling due to accumulator bit ranging
		for f in range(self.sweep_length):
			sweep_period = 1 / sweep_freq[f]

			# Predict how many FPGA clock cycles each frequency averages for:
			average_period_cycles = self.averaging_cycles * sweep_period * _NA_FPGA_CLOCK
			if self.averaging_time % sweep_period == 0:
				average_period_time = self.averaging_time * _NA_FPGA_CLOCK
			else :
				average_period_time = math.ceil(self.averaging_time / sweep_period) * sweep_period * _NA_FPGA_CLOCK

			if average_period_time >= average_period_cycles:
				average_period = average_period_time
			else :
				average_period = average_period_cycles

			# Scale according to the predicted accumulator counter size:
			if average_period <= 2**16:
				average_gain[f] = 2**4
			elif average_period <= 2**21:
				average_gain[f] = 2**-1
			elif average_period <= 2**26:
				average_gain[f] = 2**-6
			elif average_period <= 2**31:
				average_gain[f] = 2**-11
			elif average_period <= 2**36:
				average_gain[f] = 2**-16
			else :
				average_gain[f] = 2**-20

		for f in range(self.sweep_length):
			if sweep_freq[f] > 0.0 :
				gain_scale[f] =  math.ceil(average_gain[f] * points_per_freq[f] * _NA_FPGA_CLOCK / sweep_freq[f])
			else :
				gain_scale[f] = average_gain[f]

		return gain_scale

	def _calculate_scales(self):
		g1, g2 = self._adc_gains()
		fs = self._calculate_freq_axis()
		gs = self._calculate_gain_correction(fs)

		return {'g1': g1, 'g2': g2,
				'gain_correction' : gs,
				'frequency_axis' : fs,
				'sweep_freq_min': self.sweep_freq_min,
				'sweep_freq_delta': self.sweep_freq_delta,
				'sweep_length': self.sweep_length,
				'log_en': self.log_en,
				'averaging_time': self.averaging_time,
				'sweep_amplitude_ch1' : self.sweep_amp_volts_ch1,
				'sweep_amplitude_ch2' : self.sweep_amp_volts_ch2
				}

	@needs_commit
	def set_sweep(self, f_start=100, f_end=125e6, sweep_points=512, sweep_log=False, averaging_time=1e-3, settling_time=1e-3, averaging_cycles=1, settling_cycles=1):
		""" Set the output sweep parameters

		:type f_start: int; 1 <= f_start <= 125e6 Hz
		:param f_start: Sweep start frequency

		:type f_end: int; 1 <= f_end <= 125e6 Hz
		:param f_end: Sweep end frequency

		:type sweep_points: int; 32 <= sweep_points <= 512
		:param sweep_points: Number of points in the sweep (rounded to nearest power of 2).

		:type sweep_log: bool
		:param sweep_log: Enable logarithmic frequency sweep scale.

		:type averaging_time: float; sec
		:param averaging_time: Minimum averaging time per sweep point.

		:type settling_time: float; sec
		:param settling_time: Minimum setting time per sweep point.

		:type averaging_cycles: int; cycles
		:param averaging_cycles: Minimum averaging cycles per sweep point.
		
		:type settling_cycles: int; cycles
		:param settling_cycles: Minimum settling cycles per sweep point.
		"""
		_utils.check_parameter_valid('range', f_start, [1,125e6],'sweep start frequency', 'Hz')
		_utils.check_parameter_valid('range', f_end, [1,125e6],'sweep end frequency', 'Hz')
		_utils.check_parameter_valid('range', sweep_points, [32,512],'sweep points')
		_utils.check_parameter_valid('bool', sweep_log, desc='sweep log scale enable')
		_utils.check_parameter_valid('range', averaging_time, [1e-6,10], 'sweep averaging time', 'sec')
		_utils.check_parameter_valid('range', settling_time, [1e-6,10], 'sweep settling time', 'sec')
		_utils.check_parameter_valid('range', averaging_cycles, [1,2**20], 'sweep averaging cycles', 'cycles')
		_utils.check_parameter_valid('range', settling_cycles, [1,2**20], 'sweep settling cycles', 'cycles')

		# Frequency span check
		if (f_end - f_start) == 0:
			raise ValueOutOfRangeException("Sweep frequency span must be non-zero: f_start/f_end/span - %.2f/%.2f/%.2f." % (f_start, f_end, f_end-f_start))

		self.sweep_freq_min = f_start
		self.sweep_length = sweep_points
		self.log_en = sweep_log

		self.averaging_time = averaging_time
		self.averaging_cycles = averaging_cycles
		self.settling_time = settling_time

		self.sweep_freq_delta = self._calculate_sweep_delta(f_start, f_end, sweep_points, sweep_log)
		self.settling_cycles = settling_cycles

	@needs_commit
	def start_sweep(self, single=False):
		"""	Start sweeping

		:type single: bool
		:param single: Enable single sweep (otherwise loop)
		"""
		_utils.check_parameter_valid('bool', single, desc='enable single sweep')

		self.single_sweep = single
		self.loop_sweep = not single

	@needs_commit
	def stop_sweep(self):
		""" Stop sweeping. 

		This will stop new data frames from being received, so ensure you implement a timeout
		on :any:`get_data<pymoku.instruments.BodeAnalyser.get_data>` calls. """
		self.single_sweep = self.loop_sweep = False

	def _restart_sweep(self):
		self.sweep_reset = True

	@needs_commit
	def set_output(self, ch, amplitude):
		""" Set the output sweep amplitude.

		.. note::
			Ensure that the output amplitude is set so as to not saturate the inputs.
			Inputs are limited to 1.0Vpp with attenuation turned off.

		:param ch: int; {1,2}
		:type ch: Output channel

		:param amplitude: float; [0.0,2.0] Vpp
		:type amplitude: Sweep amplitude

		"""
		_utils.check_parameter_valid('set', ch, [1,2], 'output channel')
		_utils.check_parameter_valid('range', amplitude, [0.001,2.0], 'sweep amplitude','Vpp')

		# Set up the output scaling register but also save the voltage value away for use
		# in the state dictionary to scale incoming data
		if ch == 1:
			self.sweep_amplitude_ch1 = amplitude
			self.sweep_amp_volts_ch1 = amplitude
			self.channel1_en = amplitude > 0

		elif ch == 2:
			self.sweep_amplitude_ch2 = amplitude
			self.sweep_amp_volts_ch2 = amplitude
			self.channel2_en = amplitude > 0

	@needs_commit
	def gen_off(self, ch=None):
		""" Turn off the output sweep.

		If *ch* is specified, turn off only a single channel, otherwise turn off both.

		:type ch: int; {1,2}
		:param ch: Channel number to turn off (None, or leave blank, for both)

		"""
		_utils.check_parameter_valid('set', ch, [1,2,None],'output sweep channel')
		if ch is None or ch == 1:
			self.channel1_en = False
		if ch is None or ch == 2:
			self.channel2_en = False

	@needs_commit
	def set_defaults(self):
		""" Reset the Bode Analyser to sane defaults """
		super(BodeAnalyser, self).set_defaults()
		self.frame_length = _NA_SCREEN_WIDTH

		self.x_mode = SWEEP
		self.render_mode = RDR_DDS

		self.en_in_ch1 = True
		self.en_in_ch2 = True

		self.set_frontend(1, fiftyr=True, atten=False, ac=False)
		self.set_frontend(2, fiftyr=True, atten=False, ac=False)

		self.set_sweep()
		
		# 100mVpp swept outputs
		self.set_output(1,0.1)
		self.set_output(2,0.1)

		self.start_sweep()

	def get_data(self, timeout=None, wait=True):
		""" Get current sweep data.
		In the BodeAnalyser this is an alias for ``get_realtime_data`` as the data
		is never downsampled. """
		return super(BodeAnalyser, self).get_realtime_data(timeout, wait)

	def commit(self):
		# Restart the sweep as instrument settings are being changed
		self._restart_sweep()

		super(BodeAnalyser, self).commit()

		# Update the scaling factors for processing of incoming frames
		# stateid allows us to track which scales correspond to which register state
		self.scales[self._stateid] = self._calculate_scales()
	commit.__doc__ = MokuInstrument.commit.__doc__


_na_reg_handlers = {
	'loop_sweep':				(REG_NA_ENABLES, to_reg_bool(0), from_reg_bool(0)),
	'single_sweep':				(REG_NA_ENABLES, to_reg_bool(1), from_reg_bool(1)),
	'sweep_reset':				(REG_NA_ENABLES, to_reg_bool(2), from_reg_bool(2)),
	'channel1_en':				(REG_NA_ENABLES, to_reg_bool(3), from_reg_bool(3)),
	'channel2_en':				(REG_NA_ENABLES, to_reg_bool(4), from_reg_bool(4)),

	'sweep_freq_min':			((REG_NA_SWEEP_FREQ_MIN_H, REG_NA_SWEEP_FREQ_MIN_L),
											to_reg_unsigned(0, 48, xform=lambda obj, f: f * _NA_FREQ_SCALE),
											from_reg_unsigned(0, 48, xform=lambda obj, f: f / _NA_FREQ_SCALE)),
	'sweep_freq_delta':			((REG_NA_SWEEP_FREQ_DELTA_H, REG_NA_SWEEP_FREQ_DELTA_L),
											to_reg_signed(0, 48),
											from_reg_signed(0, 48)),

	'log_en':					(REG_NA_LOG_EN, to_reg_bool(0), from_reg_bool(0)),
	'sweep_length':				(REG_NA_SWEEP_LENGTH, to_reg_unsigned(0, 10), from_reg_unsigned(0, 10)),

	'settling_time':			(REG_NA_HOLD_OFF_L,
											to_reg_unsigned(0, 32, xform=lambda obj, t: t * _NA_FPGA_CLOCK),
											from_reg_unsigned(0, 32, xform=lambda obj, t: t / _NA_FPGA_CLOCK)),
	'averaging_time':			(REG_NA_AVERAGE_TIME,
											to_reg_unsigned(0, 32, xform=lambda obj, t: t * _NA_FPGA_CLOCK),
											from_reg_unsigned(0, 32, xform=lambda obj, t: t / _NA_FPGA_CLOCK)),
	'sweep_amplitude_ch1':		(REG_NA_SWEEP_AMP_MULT,
											to_reg_unsigned(0, 16, xform=lambda obj, a: a / obj._dac_gains()[0]),
											from_reg_unsigned(0, 16, xform=lambda obj, a: a * obj._dac_gains()[0])),
	'sweep_amplitude_ch2':		(REG_NA_SWEEP_AMP_MULT,
											to_reg_unsigned(16, 16, xform=lambda obj, a: a / obj._dac_gains()[1]),
											from_reg_unsigned(16, 16, xform=lambda obj, a: a * obj._dac_gains()[1])),

	'settling_cycles':			(REG_NA_SETTLE_CYCLES, to_reg_unsigned(0, 32), from_reg_unsigned(0, 32)),
	'averaging_cycles':			(REG_NA_AVERAGE_CYCLES, to_reg_unsigned(0, 32), from_reg_unsigned(0, 32)),
}