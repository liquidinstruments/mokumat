import itertools
import pytest, time
from pymoku import Moku
from pymoku.instruments import *
from pymoku._oscilloscope import _OSC_SCREEN_WIDTH, _OSC_ADC_SMPS
from pymoku._siggen import SG_MOD_NONE, SG_MOD_AMPL, SG_MOD_PHASE, SG_MOD_FREQ, SG_MODSOURCE_INT, SG_MODSOURCE_ADC, SG_MODSOURCE_DAC
import conftest
import numpy

SIGGEN_SINE 	= 0
SIGGEN_SQUARE	= 1
SIGGEN_RAMP 	= 2
SIGGEN_DC		= 3

def in_bounds(v, center, err):
	return abs(v - center) < abs(err)

class Test_Siggen:

	# Set the timebase and check it correctly sets the decimation rate
	@pytest.mark.parametrize("t1, t2", [
		(0, 1),
		(0, 1e-1),
		(0, 1e-2),
		(-1,1),
		(-2,1),
		(-0.02,1.2),
		(-10,1e-6)
		])
	def _1test_timebase(self, base_instr, t1, t2):
		base_instr.set_timebase(t1,t2)
		base_instr.commit()
		timebase_res = base_instr._get_timebase(base_instr.decimation_rate, base_instr.pretrigger, base_instr.render_deci, base_instr.offset)
		print timebase_res

	'''
		Test the generated output waveforms are as expected
	'''
	@pytest.mark.parametrize("ch, vpp, freq, offset, waveform", 
		itertools.product([1,2],[0, 0.5, 1.0],[1e3, 1e6], [0, 0.3, 0.5], [SIGGEN_SINE, SIGGEN_SQUARE, SIGGEN_RAMP]))
	def test_waveform_amp(self, base_instr, ch, vpp, freq, offset, waveform):
		# Generate an output sinewave and loop to input
		# Ensure the amplitude is right
		# Ensure the frequency seems correct as well

		# Timebase should allow ~5 cycles of input wave
		if freq == 0:
			tspan = 1.0 # a second
		else:
			tspan = (1.0/freq) * 5.0
		base_instr.set_timebase(0,tspan)

		# Loop back output as input source and trigger on it
		base_instr.set_source(ch,OSC_SOURCE_DAC)
		if(ch==1):
			base_instr.set_trigger(OSC_TRIG_DA1, OSC_EDGE_RISING, 0)
		else:
			base_instr.set_trigger(OSC_TRIG_DA2, OSC_EDGE_RISING, 0)

		# Generate the desired waveform
		if waveform == SIGGEN_SINE:
			base_instr.synth_sinewave(ch, vpp, freq, offset)
		elif waveform == SIGGEN_SQUARE:
			base_instr.synth_squarewave(ch, vpp, freq,offset=offset)
		elif waveform == SIGGEN_RAMP:
			base_instr.synth_rampwave(ch, vpp, freq, offset=offset)
		base_instr.commit()

		# 5mV Tolerance on max/min values
		tolerance = 0.005

		# Get a few frames and test that the max amplitudes of the generated signals are within bounds
		for _ in range(10):
			frame = base_instr.get_frame()

			if(ch==1):
				ch_frame = frame.ch1
			else:
				ch_frame = frame.ch2

			# For debugging the received frame
			for y in ch_frame:
				print y

			maxval = max(x for x in ch_frame if x is not None)
			minval = min(x for x in ch_frame if x is not None)

			assert in_bounds(maxval, (vpp/2.0)+offset, tolerance)
			assert in_bounds(minval, (-1*(vpp/2.0) + offset), tolerance)

	@pytest.mark.parametrize("ch, vpp, freq, waveform", 
		itertools.product([1,2],[1.0],[1, 100, 1e3, 100e3, 1e6, 3e6],[SIGGEN_SINE, SIGGEN_SQUARE, SIGGEN_RAMP]))
	def test_waveform_freq(self, base_instr, ch, vpp, freq, waveform):
		# Set timebase of 5 periods
		number_periods = 5
		period = (1.0/freq)
		tspan = period * number_periods
		base_instr.set_timebase(0,tspan)

		base_instr.set_source(ch,OSC_SOURCE_DAC)
		base_instr.set_xmode(OSC_FULL_FRAME)
		if(ch==1):
			base_instr.set_trigger(OSC_TRIG_DA1, OSC_EDGE_RISING, 0)
		else:
			base_instr.set_trigger(OSC_TRIG_DA2, OSC_EDGE_RISING, 0)

		# Figure out the timebase of frames
		(tstart, tend) = base_instr._get_timebase(base_instr.decimation_rate, base_instr.pretrigger, base_instr.render_deci, base_instr.offset)

		time_per_smp = (tend-tstart)/_OSC_SCREEN_WIDTH 	# Timestep per sample
		smps_per_period = period/time_per_smp 			# Number of samples before a period is reached

		# Test all waveform types
		if waveform == SIGGEN_SINE:
			base_instr.synth_sinewave(ch,vpp,freq,0.0)
			start_xs = [0, int(smps_per_period/2), int(smps_per_period/3), int(smps_per_period/4), int(smps_per_period/8), int(3*smps_per_period/4)]
		elif waveform == SIGGEN_SQUARE:
			base_instr.synth_squarewave(ch, vpp, freq)
			start_xs = [int(smps_per_period/3), int(3*smps_per_period/4), int(2*smps_per_period/3), int(smps_per_period/8), int(7*smps_per_period/8)]
		elif waveform == SIGGEN_RAMP:
			base_instr.synth_rampwave(ch, vpp, freq)
			start_xs = [0, int(smps_per_period/2), int(smps_per_period/3), int(smps_per_period/4), int(smps_per_period/8), int(3*smps_per_period/4)]
		base_instr.commit()

		# 2% amplitude tolerance
		allowable_error = 0.02*vpp

		# Workaround for ensuring we receive a valid waveform in the frame
		# The squarewave generator has unpredictable initial conditions currently
		# So we want to skip the first frame
		time.sleep(3*(tend-tstart))
		base_instr.flush()
		base_instr.get_frame()

		# Test multiple frames worth
		for _ in range(5):

			frame = base_instr.get_frame()
			if(ch==1):
				ch_frame = frame.ch1
			if(ch==2):
				ch_frame = frame.ch2

			# Start checking at different points along the waveform
			for start_x in start_xs:
				# Amplitude expected at multiples of periods along the waveform
				expectedv = ch_frame[start_x]

				# Skip along the waveform, 1 period at a time
				for i in range(number_periods-1):
					x = start_x + int(round(i*smps_per_period))

					actualv = ch_frame[x]

					# For debugging the received frame
					for y in ch_frame:
						print y

					# Debugging info
					print "Allowable tolerance: %.10f, Error: %.10f, Frame index: %d, Expected value: %.10f, Actual value: %.10f, Samples per period: %d, Render deci: %f" % (allowable_error, expectedv-actualv, x, expectedv, actualv, smps_per_period, base_instr.render_deci)
					# Check actual value is within tolerance
					assert in_bounds(actualv, expectedv, allowable_error)

	
	# NOTE: Modulation cannot be tested using the Oscilloscope instrument as it is not enabled.
	# 		The SignalGenerator bitstream should be tested on its own with full modulation functionality enabled.
	@pytest.mark.parametrize("ch, source, depth, frequency", [
		#(1, 0, 0.5, 3)
		])
	def tes2_am_modulation(self, base_instr, ch, source, depth, frequency):
		# Set a sampling frequency
		base_instr.set_timebase(0,1.0) # 1 second
		base_instr.synth_sinewave(1, 1.0, 10, 0)
		base_instr.synth_sinewave(2, 1.0, 5, 0)
		base_instr.synth_modulate(1, SG_MOD_AMPL, SG_MODSOURCE_INT, depth, frequency)
		#base_instr.synth_modulate(ch, SG_MOD_AMPL, source, depth, frequency)
		base_instr.commit()

		# Get sampling frequency
		fs = _OSC_ADC_SMPS / (base_instr.decimation_rate * base_instr.render_deci)
		fstep = fs / _OSC_SCREEN_WIDTH

		assert False

class Test_Trigger:
	'''
		We want this class to test everything around triggering settings for the oscilloscope
	'''

	@pytest.mark.parametrize("trig_ch, edge, amp", 
		itertools.product([1,2],[OSC_EDGE_RISING, OSC_EDGE_FALLING, OSC_EDGE_BOTH],
		[-0.1, 0.0, 0.1, 0.3]))
	def test_triggered_amplitude(self, base_instr, trig_ch, edge, amp):
		'''
			Ensure that the start of the frame is the expected amplitude (within some error)
		'''
		# Set up the trigger signal
		if trig_ch == 1:
			base_instr.synth_sinewave(1,1.0,100.0,0)
			base_instr.set_timebase(0,0.01)
			base_instr.set_source(1, OSC_SOURCE_DAC)
			base_instr.set_trigger(OSC_TRIG_DA1, edge, amp, hysteresis=0, hf_reject=False, mode=OSC_TRIG_NORMAL)
		if trig_ch == 2:
			base_instr.synth_sinewave(2,1.0,100.0,0)
			base_instr.set_timebase(0,0.01)
			base_instr.set_source(2, OSC_SOURCE_DAC)
			base_instr.set_trigger(OSC_TRIG_DA2, edge, amp, hysteresis=0, hf_reject=False, mode=OSC_TRIG_NORMAL)

		base_instr.commit()

		allowable_error = 0.005 # Volts
		for n in range(10):
			frame = base_instr.get_frame(timeout=5)
			if trig_ch == 1:
				ch_frame = frame.ch1
			elif trig_ch == 2:
				ch_frame = frame.ch2
			print "Start of frame value: %.2f" % (ch_frame[0])
			assert in_bounds(ch_frame[0], amp, allowable_error)

	
	@pytest.mark.parametrize("trig_ch, edge, waveform",
		itertools.product([1,2],[OSC_EDGE_RISING, OSC_EDGE_FALLING, OSC_EDGE_BOTH], [SIGGEN_SINE, SIGGEN_SQUARE, SIGGEN_RAMP]))
	def test_triggered_edge(self, base_instr, trig_ch, edge, waveform):
		half_idx = (_OSC_SCREEN_WIDTH / 2) - 1

		def _is_rising(p1,p2):
			if (p2-p1) > 0:
				return True
			else:
				return False

		def _is_falling(p1,p2):
			if (p2-p1) < 0:
				return True
			else:
				return False
		'''
			Ensure the edge type looks right
		'''
		# Test rising/falling edges
		if waveform == SIGGEN_SINE:
			base_instr.synth_sinewave(trig_ch, 1.0, 100, 0)
		elif waveform == SIGGEN_SQUARE:
			base_instr.synth_squarewave(trig_ch, 1.0, 100, 0)
		elif waveform == SIGGEN_RAMP:
			base_instr.synth_rampwave(trig_ch, 1.0, 100, 0)
		else:
			print "Invalid waveform type"
			assert False

		if trig_ch == 1:
			base_instr.set_timebase(-0.01,0.01)
			base_instr.set_source(1, OSC_SOURCE_DAC)
			base_instr.set_trigger(OSC_TRIG_DA1, edge, 0.0, hysteresis=0, hf_reject=False, mode=OSC_TRIG_NORMAL)
		if trig_ch == 2:
			base_instr.set_timebase(-0.01,0.01)
			print base_instr.pretrigger, base_instr.render_deci, float(base_instr.pretrigger)/float(base_instr.render_deci)
			base_instr.set_source(2, OSC_SOURCE_DAC)
			base_instr.set_trigger(OSC_TRIG_DA2, edge, 0.0, hysteresis=0, hf_reject=False, mode=OSC_TRIG_NORMAL)

		base_instr.commit()

		for _ in range(10):
			frame = base_instr.get_frame(timeout=5)
			if trig_ch == 1:
				ch_frame = frame.ch1
			elif trig_ch == 2:
				ch_frame = frame.ch2

			print "Start of frame values: %s" % (ch_frame[half_idx - 4: half_idx+4])

			if(edge == OSC_EDGE_RISING):
				assert _is_rising(ch_frame[half_idx-1],ch_frame[half_idx])
			elif(edge == OSC_EDGE_FALLING):
				assert _is_falling(ch_frame[half_idx-1], ch_frame[half_idx])
			elif(edge == OSC_EDGE_BOTH):
				assert _is_rising(ch_frame[half_idx-1],ch_frame[half_idx]) or _is_falling(ch_frame[half_idx-1],ch_frame[half_idx])

class Tes2_Timebase:
	'''
		Ensure the timebase is correct
	'''



class Tes2_Source:
	'''
		Ensure the source is set and rendered as expected
	'''
	@pytest.mark.parametrize("ch, amp",[
		(1, 0.2),
		(1, 0.5),
		(2, 0.1),
		(2, 1.0),
		])
	def test_dac(self, base_instr, ch, amp):
		i = base_instr
		i.synth_sinewave(ch,amp,1e6,0)
		i.set_source(ch, OSC_SOURCE_DAC)
		i.set_timebase(0,2e-6)
		i.commit()

		# Max and min should be around ~amp
		frame = i.get_frame()
		assert in_bounds(max(getattr(frame, "ch"+str(ch))), amp, 0.05)
		assert in_bounds(min(getattr(frame, "ch"+str(ch))), amp, 0.05)

