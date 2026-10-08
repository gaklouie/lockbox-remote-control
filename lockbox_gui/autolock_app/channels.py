"""
Channel names and conversions between raw chip volts and physical volts.

ADC inputs pass through a resistor network that scales the real signals
into the ADS1115's input range; the to_* functions undo that scaling.
DAC channel A ("control out") drives a shift/amplify circuit; the
control_out_* functions convert between its raw DAC volts and the
physical volts at the circuit's output.
"""

from .config import DAC_SHIFT_GAIN, DAC_SHIFT_OFFSET


# --------------------------------------------------------------------------
# ADC: raw ADC volts -> true, physical volts
# --------------------------------------------------------------------------
def to_slow_output(adc_voltage):
    return 56 * (-0.227273 + 0.118867 * adc_voltage)


def to_fast_output(adc_voltage):
    return 56 * (-0.227273 + 0.118867 * adc_voltage)


def to_error(adc_voltage):
    return 18 * (-0.185185 + 0.0925927 * adc_voltage)


def to_dc_err(adc_voltage):
    return adc_voltage


# Order matches ADC channels 0-3.
CHANNEL_CONVERTERS = {
    "slow_output": to_slow_output,
    "fast_output": to_fast_output,
    "error": to_error,
    "dc_err": to_dc_err,
}
ERROR_SIGNAL_NAMES = ["error", "dc_err"]
OUTPUT_SIGNAL_NAMES = ["slow_output", "fast_output"]


def raw_to_physical(raw_voltages):
    """Convert [ch0, ch1, ch2, ch3] raw ADC volts to a {name: physical volts} dict."""
    return {name: convert(raw)
            for (name, convert), raw in zip(CHANNEL_CONVERTERS.items(), raw_voltages)}


# --------------------------------------------------------------------------
# DAC channel A ("control out"): physical volts <-> raw DAC volts
# --------------------------------------------------------------------------
def control_out_to_dac_volts(v_physical):
    return (v_physical - DAC_SHIFT_OFFSET) / DAC_SHIFT_GAIN


def dac_volts_to_control_out(v_dac):
    return v_dac * DAC_SHIFT_GAIN + DAC_SHIFT_OFFSET
