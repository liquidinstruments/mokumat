
import math
import logging
import re
import os

from ._instrument import *
CHN_BUFLEN = 2**13
from . import _frame_instrument
from . import _waveform_generator
from pymoku._oscilloscope import _CoreOscilloscope
from . import _utils

log = logging.getLogger(__name__)

REG_MMAP_ACCESS = 62 #TODO this should go somewhere more instrument generic

REG_ARB_SETTINGS = 96
REG_ARB_PHASE_STEP1_L = 97
REG_ARB_PHASE_STEP1_H = 98
REG_ARB_PHASE_OFFSET1_L = 101
REG_ARB_PHASE_OFFSET1_H = 102
REG_ARB_AMPLITUDE1 = 105
REG_ARB_PHASE_MOD1_L = 107
REG_ARB_PHASE_MOD1_H = 108
REG_ARB_DEAD_VALUE1 = 111
REG_ARB_LUT_LENGTH1 = 113
REG_ARB_OFFSET1 = 115

REG_ARB_PHASE_STEP2_L = 99
REG_ARB_PHASE_STEP2_H = 100
REG_ARB_PHASE_OFFSET2_L = 103
REG_ARB_PHASE_OFFSET2_H = 104
REG_ARB_AMPLITUDE2 = 106
REG_ARB_PHASE_MOD2_L = 109
REG_ARB_PHASE_MOD2_H = 110
REG_ARB_DEAD_VALUE2 = 112
REG_ARB_LUT_LENGTH2 = 114
REG_ARB_OFFSET2 = 116

ARB_MODE_1000 = 0x0
ARB_MODE_500 = 0x1
ARB_MODE_250 = 0x2
ARB_MODE_125 = 0x3

_ARB_AMPSCALE = 2.0**16
_ARB_VOLTSCALE = 2.0**15
_ARB_LUT_LENGTH = 8192
_ARB_LUT_LSB = 2.0**32
_ARB_LUT_INTERPLOATION_LENGTH = 2**32

_ARB_UPDATE_1GS = 1.0e9
_ARB_UPDATE_500MS = 500.0e6
_ARB_UPDATE_250MS = 250.0e6
_ARB_UPDATE_125MS = 125.0e6

class ArbWaveGen(_CoreOscilloscope):
	def __init__(self):
		super(ArbWaveGen, self).__init__()
		self._register_accessors(_arb_reg_handlers)
		self.id = 15
		self.type = "arbwavegen"

	@needs_commit
	def set_defaults(self):
		super(ArbWaveGen, self).set_defaults()
		self.mode1 = ARB_MODE_125
		self.lut_length1 = _ARB_LUT_LENGTH
		self.mode2 = ARB_MODE_125
		self.lut_length2 = _ARB_LUT_LENGTH
		self.phase_modulo1 = 2**42
		self.phase_modulo2 = 2**42
		self.phase_step1 = _ARB_LUT_LSB
		self.phase_step2 = _ARB_LUT_LSB
		self.dead_value1 = 0x0000
		self.dead_value2 = 0x0000
		self.interpolation1 = False
		self.interpolation2 = False
		self.enable1 = False
		self.enable2 = False
		self.amplitude1 = 1.0
		self.amplitude2 = 1.0
		self.offset1 = 0.0
		self.offset2 = 0.0


	@needs_commit
	def _set_mmap_access(self, access):
		self.mmap_access = access

	@needs_commit
	def _set_mode(self, ch, mode, length):
		"""Changes the mode used to determine outut the waveform.

		:type ch: int; {1,2}
		:param ch: Channel on which the mode is set

		:raises ValueError: if the channel is invalid
		:raises ValueOutOfRangeException: if wave parameters are out of range
		"""
		_utils.check_parameter_valid('set', ch, [1,2],'output channel')
		_utils.check_parameter_valid('set', mode, [ARB_MODE_1000, ARB_MODE_500, ARB_MODE_250, ARB_MODE_125], desc='mode is not vaild')

		if mode is ARB_MODE_500:
			_utils.check_parameter_valid('range', length, [1,2**13], desc='length for lookup table')
		if mode is ARB_MODE_250:
			_utils.check_parameter_valid('range', length, [1,2**14], desc='length for lookup table')
		if mode is ARB_MODE_125:
			_utils.check_parameter_valid('range', length, [1,2**15], desc='length for lookup table')
		if mode is ARB_MODE_1000:
			_utils.check_parameter_valid('range', length, [1,2**16], desc='length for lookup table')
		
		if ch == 1:
			self.mode1 = mode
			self.lut_length1 = length-1
		elif ch ==2:
			self.mode2 = mode
			self.lut_length2 = length-1
			
	def write_lut(self, ch, data, srate=None):
		"""writes the lookup table to memmory in the moku

		To write the lookup table a file is created. It contains the values for both channels.
		On send the file is transmitted to the Moku:Lab device.

		:type ch: int; {1,2}
		:param ch: Channel on which the mode is set

		:raises ValueError: if the channel is invalid
		:raises ValueOutOfRangeException: if wave parameters are out of range
		"""
		_utils.check_parameter_valid('set', ch, [1,2],'output channel')

		if srate is not None:
			self._set_mode(ch, srate, len(data))
		# picks the stepsize and the steps based in the mode
		steps, stepsize = [(8, 8192), (4, 8192 * 2), (2, 8192 * 4), (1, 8192 * 8)][srate]

		with open('.lutdata.dat', 'r+b') as f:
			#first check and make the file the right size
			f.seek(0, os.SEEK_END)
			size = f.tell()
			f.write('\0'.encode(encoding='UTF-8') * (_ARB_LUT_LENGTH * 8 * 4 * 2 - size))
			f.flush()

			#Leave the previous data file so we just rewite the new part,
			#as we have to upload both channels at once.
			if ch == 1:
				offset = 0
			else:
				offset = _ARB_LUT_LENGTH * 8 * 4
			for step in range(steps):
				f.seek(offset + (step * stepsize * 4)) 
				f.write(b''.join([struct.pack('<hh', math.ceil((2.0**15-1) * d),0) for d in data]))
			
			f.flush()

		self._set_mmap_access(True)
		error = self._moku._send_file('j', '.lutdata.dat')
		self._set_mmap_access(False)
	
	@needs_commit
	def gen_waveform(self, ch, period, phase, amplitude, offset=0, interpolation=True, dead_time=0, dead_voltage = 0):
		""" Generate a Wave with the given parameters on the given channel.

		:type ch: int; {1,2}
		:param ch: Channel on which to generate the wave

		:type period: float, [4e-9, 1];
		:param period: periode of the signal in s

		:type phase: float, [0-360] degrees
		:param phase: Phase offset of the wave

		:type amplitude: float, [0.0,2.0] Vpp
		:param amplitude: Waveform peak-to-peak amplitude

		:type offset: float, [-1.0,1.0] Volts
		:param offset: DC offset applied to the waveform

		:type interpolation: bool [True, False]
		:param interpolation: Uses linear interploation if true

		:type dead_time: float [0, 2e18] cyc
		:param dead_time: number of cycles which show the dead voltage. Use 0 for no dead time

		:type dead_voltage: float [0.0,2.0] V
		:param dead_voltage: signal level during dead time in Volts

		:type fifyr: bool [True, False]
		:param fifyr: use of 50 Ohm impedance

		:raises ValueError: if the parameters  is invalid
		:raises ValueOutOfRangeException: if wave parameters are out of range
		:raises InvalidParameterException: if the parameters are the wrong types
		"""
		_utils.check_parameter_valid('set', ch, [1,2], desc='output channel')
		_utils.check_parameter_valid('range', period, [4e-9, 1], desc='periode of the signal')
		_utils.check_parameter_valid('range', amplitude, [0.0,2.0], desc='peak to peak amplitude', units='volts')
		_utils.check_parameter_valid('bool', interpolation, desc='linear interpolation')
		_utils.check_parameter_valid('range', dead_time, [0.0, 2e18], desc='signal dead time', units='cycles')
		_utils.check_parameter_valid('range', dead_voltage, [0.0, 2.0], desc='dead value', units='volts')
		_utils.check_parameter_valid('range', phase, [0, 360], desc='phase offset', units='degrees')

		upper_voltage = offset + (amplitude/2.0)
		lower_voltage = offset - (amplitude/2.0)

		if (upper_voltage > 1.0) or (lower_voltage < -1.0):
			raise ValueOutOfRangeException("Waveform offset limited by amplitude (max output range 2.0Vpp).")

		if(ch == 1):
			freq = 1/period
			self.interpolation1 = interpolation
			phase_modulo = (self.lut_length1 + 1 ) * _ARB_LUT_INTERPLOATION_LENGTH 
			update_rate = [_ARB_UPDATE_1GS, _ARB_UPDATE_500MS, _ARB_UPDATE_250MS, _ARB_UPDATE_125MS][self.mode1]
			self.phase_step1 = freq / update_rate * phase_modulo
			phase_modulo = phase_modulo * dead_time if dead_time > 0 else phase_modulo
			self.phase_modulo1 = phase_modulo
			self.phase_offset1 = (phase / 360) * phase_modulo if dead_time == 0 else 0
			self.dead_value1 = dead_voltage
			self.amplitude1 = amplitude
			self.offset1 = offset
			self.enable1 = True

		if(ch == 2):
			freq = 1/period
			self.interpolation2 = interpolation
			phase_modulo = (self.lut_length2 + 1 ) * _ARB_LUT_INTERPLOATION_LENGTH 
			update_rate = [_ARB_UPDATE_1GS, _ARB_UPDATE_500MS, _ARB_UPDATE_250MS, _ARB_UPDATE_125MS][self.mode2]
			self.phase_step2 = freq / update_rate * phase_modulo
			phase_modulo = phase_modulo * dead_time if dead_time > 0 else phase_modulo
			self.phase_modulo2 = phase_modulo
			self.phase_offset2 = (phase / 360) * phase_modulo if dead_time > 0 else 0
			self.dead_value2 = dead_voltage
			self.amplitude2 = amplitude
			self.offset2 = offset
			self.enable2 = True

	@needs_commit
	def sync_phase(self, ch):
		""" resets the phase off the given channel to the other
		
		:type ch: int; {1,2}
		:param ch: Channel on which to generate the wave

		:raises ValueError: if the channel number is invalid
		"""
		_utils.check_parameter_valid('set', ch, [1,2],'output channel')

		if ch == 1:
			self.phase_sync1 = True
		elif ch ==2:
			self.phase_sync2 = True

	@needs_commit
	def reset_phase(self, ch):
		""" resets the channels phase accumulator to zero
		
		:type ch: int; {1,2}
		:param ch: Channel on which the reset is performed

		:raises ValueError: if the channel number is invalid
		"""
		_utils.check_parameter_valid('set', ch, [1,2],'output channel')

		if ch == 1:
			self.phase_rst1 = True
		elif ch ==2:
			self.phase_rst2 = True

	def get_frequency(self, ch):
		""" returns the frequency for a given channel
		
		:type ch: int; {1,2}
		:param ch: Channel from which the frequency is calculated

		:raises ValueError: if the channel number is invalid
		"""
		_utils.check_parameter_valid('set', ch, [1,2],'output channel')


		if ch == 1:
			update_rate = [_ARB_UPDATE_1GS, _ARB_UPDATE_500MS, _ARB_UPDATE_250MS, _ARB_UPDATE_125MS][self.mode1]
			return (self.phase_step1 / self.phase_modulo1) * update_rate
		if ch == 2:
			update_rate = [_ARB_UPDATE_1GS, _ARB_UPDATE_500MS, _ARB_UPDATE_250MS, _ARB_UPDATE_125MS][self.mode2]
			return (self.phase_step2 / self.phase_modulo2) * update_rate

	@needs_commit
	def gen_off(self, ch=None):
		""" Turn ArbWaveGen output(s) off.

		The channel will be turned on when configuring the waveform type but can be turned off
		using this function. If *ch* is None (the default), both channels will be turned off,
		otherwise just the one specified by the argument.

		:type ch: int; {1,2} or None
		:param ch: Channel to turn off, or both.

		:raises ValueError: invalid channel number
		"""
		_utils.check_parameter_valid('set', ch, [1,2],'output channel', allow_none=True)

		if ch is None or ch == 1:
			self.enable1 = False

		if ch is None or ch == 2:
			self.enable2 = False


_arb_reg_handlers = {
	'mmap_access':		(REG_MMAP_ACCESS,		to_reg_bool(0),			from_reg_bool(0)),
	'enable1':			(REG_ARB_SETTINGS,		to_reg_bool(16),		from_reg_bool(16)),
	'phase_rst1':		(REG_ARB_SETTINGS,		to_reg_bool(20),		from_reg_bool(20)),
	'phase_sync1':		(REG_ARB_SETTINGS,		to_reg_bool(22),		from_reg_bool(22)),
	'mode1':			(REG_ARB_SETTINGS,		to_reg_unsigned(0, 2, allow_set=[ARB_MODE_125, ARB_MODE_250, ARB_MODE_500, ARB_MODE_1000]),
												from_reg_unsigned(0, 2)),
	'interpolation1':	(REG_ARB_SETTINGS,		to_reg_bool(4),			from_reg_bool(4)),
	'lut_length1':		(REG_ARB_LUT_LENGTH1,	to_reg_unsigned(0, 16), from_reg_signed(0, 16)),
	'dead_value1':		(REG_ARB_DEAD_VALUE1,	to_reg_signed(0, 16), 	from_reg_signed(0, 16)),
	'amplitude1':		(REG_ARB_AMPLITUDE1,	to_reg_signed(0, 18, xform=lambda obj, r: r * _ARB_AMPSCALE),
	                                            from_reg_signed(0, 18, xform=lambda obj, r: r / _ARB_AMPSCALE)),
	'offset1':			(REG_ARB_OFFSET1,		to_reg_signed(0, 16, xform=lambda obj, r: r * _ARB_VOLTSCALE),
	                                            from_reg_signed(0, 16, xform=lambda obj, r: r / _ARB_VOLTSCALE)),
	'phase_modulo1':	((REG_ARB_PHASE_MOD1_H, REG_ARB_PHASE_MOD1_L),
												to_reg_unsigned(0, 64), from_reg_unsigned(0, 64)),
	'phase_offset1':	((REG_ARB_PHASE_OFFSET1_H, REG_ARB_PHASE_OFFSET1_L),
												to_reg_unsigned(0, 64), from_reg_unsigned(0, 64)),
	'phase_step1':		((REG_ARB_PHASE_STEP1_H, REG_ARB_PHASE_STEP1_L),
												to_reg_unsigned(0, 64), from_reg_unsigned(0, 64)),
	'enable2':			(REG_ARB_SETTINGS,		to_reg_bool(17),		from_reg_bool(17)),
	'phase_rst2':		(REG_ARB_SETTINGS,		to_reg_bool(21),		from_reg_bool(21)),
	'phase_sync2':		(REG_ARB_SETTINGS,		to_reg_bool(23),		from_reg_bool(23)),
	'mode2':			(REG_ARB_SETTINGS,		to_reg_unsigned(8, 2, allow_set=[ARB_MODE_125, ARB_MODE_250, ARB_MODE_500, ARB_MODE_1000]),
												from_reg_unsigned(8, 2)),
	'interpolation2':	(REG_ARB_SETTINGS,		to_reg_bool(12),			from_reg_bool(12)),
	'lut_length2':		(REG_ARB_LUT_LENGTH2,	to_reg_unsigned(0, 16), from_reg_signed(0, 16)),
	'dead_value2':		(REG_ARB_DEAD_VALUE2,	to_reg_signed(0, 16), 	from_reg_signed(0, 16)),
	'amplitude2':		(REG_ARB_AMPLITUDE2,	to_reg_signed(0, 18, xform=lambda obj, r: r * _ARB_AMPSCALE),
	                                            from_reg_signed(0, 18, xform=lambda obj, r: r / _ARB_AMPSCALE)),
	'offset2':			(REG_ARB_OFFSET2,		to_reg_signed(0, 16, xform=lambda obj, r: r * _ARB_VOLTSCALE),
	                                            from_reg_signed(0, 16, xform=lambda obj, r: r / _ARB_VOLTSCALE)),
	'phase_modulo2':	((REG_ARB_PHASE_MOD2_H, REG_ARB_PHASE_MOD2_L),
												to_reg_unsigned(0, 64), from_reg_unsigned(0, 64)),
	'phase_offset2':	((REG_ARB_PHASE_OFFSET2_H, REG_ARB_PHASE_OFFSET2_L),
												to_reg_unsigned(0, 64), from_reg_unsigned(0, 64)),
	'phase_step2':		((REG_ARB_PHASE_STEP2_H, REG_ARB_PHASE_STEP2_L),
												to_reg_unsigned(0, 64), from_reg_unsigned(0, 64))
}
