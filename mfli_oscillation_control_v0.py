from __future__ import annotations

import math
import sys
import threading
import time

import numpy as np
import yaml
from scipy.optimize import curve_fit

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from PySide6.QtCore import QFile, QObject, QThread, QTimer, Signal, Slot, Qt
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QProgressBar,
    QSlider,
    QSpinBox,
    QStatusBar,
    QWidget,
    QStyleFactory,
)

from nanonis_spinbox import NanonisSpinBox, format_eng_number

from zhinst.toolkit import Session


PHASE_PID_INDEX = 0      # LabOne GUI "PID / PLL 1"
AMPLITUDE_PID_INDEX = 2  # LabOne GUI "PID / PLL 3"
UI_FILENAME = "mfli_oscillation_control_v0.ui"
SWEEP_UI_FILENAME = "frequency_sweep.ui"
CONNECTION_CONFIG_FILENAME = "mfli_connection.yaml"
AMP_SETPOINT_SLIDER_STEPS = 100_000

T = TypeVar("T", bound=QObject)


def _last_scalar(payload: Any) -> float | None:
    """Extract the newest scalar from a zhinst-toolkit poll payload."""
    if payload is None:
        return None

    value = payload.get("value") if isinstance(payload, dict) else payload
    if value is None:
        return None

    try:
        return float(value[-1])
    except (TypeError, IndexError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None



def _wrap_phase_deg(value: float | np.ndarray) -> float | np.ndarray:
    """Wrap phase to the LabOne/Nanonis-style [-180, 180) interval."""
    return (np.asarray(value) + 180.0) % 360.0 - 180.0


def _resonator_amplitude_model(
    frequency_hz: np.ndarray,
    baseline: float,
    amplitude: float,
    center_hz: float,
    q_factor: float,
) -> np.ndarray:
    """Near-resonance amplitude response of a lightly damped resonator."""
    detuning = 2.0 * q_factor * (frequency_hz - center_hz) / center_hz
    return baseline + amplitude / np.sqrt(1.0 + detuning * detuning)


def fit_resonance_sweep(
    frequency_hz: np.ndarray,
    amplitude_v: np.ndarray,
    phase_deg: np.ndarray,
) -> dict[str, Any]:
    """Fit center frequency/Q from R and phase-at-center from demodulator phase.

    LabOne's Math tab describes the resonance analysis as a Lorentzian fit for
    Demod R and an inverse-tangent fit for Demod Phase.  This implementation
    uses the equivalent near-resonance resonator-amplitude response for R,
    then fits the phase offset of an arctangent response using the resulting
    center frequency and Q.
    """
    f = np.asarray(frequency_hz, dtype=float).reshape(-1)
    r = np.asarray(amplitude_v, dtype=float).reshape(-1)
    phi = np.asarray(phase_deg, dtype=float).reshape(-1)

    finite = np.isfinite(f) & np.isfinite(r) & np.isfinite(phi)
    f, r, phi = f[finite], r[finite], phi[finite]
    if f.size < 12:
        raise ValueError("Need at least 12 finite sweep points for a resonance fit.")

    order = np.argsort(f)
    f, r, phi = f[order], r[order], phi[order]
    span = float(f[-1] - f[0])
    if span <= 0:
        raise ValueError("Sweep frequency span must be greater than zero.")

    peak_index = int(np.argmax(r))
    center_guess = float(f[peak_index])
    baseline_guess = max(0.0, float(np.percentile(r, 10.0)))
    peak_height = float(r[peak_index] - baseline_guess)
    if peak_height <= max(np.finfo(float).eps, 1e-9 * max(abs(float(r[peak_index])), 1.0)):
        raise ValueError("No resolvable amplitude resonance peak was found.")

    # Estimate the 3 dB bandwidth from the amplitude half-power level.
    half_power_level = baseline_guess + peak_height / math.sqrt(2.0)
    left_cross = None
    for i in range(peak_index - 1, -1, -1):
        if r[i] <= half_power_level <= r[i + 1] or r[i] >= half_power_level >= r[i + 1]:
            denom = r[i + 1] - r[i]
            frac = 0.0 if denom == 0 else (half_power_level - r[i]) / denom
            left_cross = float(f[i] + frac * (f[i + 1] - f[i]))
            break
    right_cross = None
    for i in range(peak_index, f.size - 1):
        if r[i] >= half_power_level >= r[i + 1] or r[i] <= half_power_level <= r[i + 1]:
            denom = r[i + 1] - r[i]
            frac = 0.0 if denom == 0 else (half_power_level - r[i]) / denom
            right_cross = float(f[i] + frac * (f[i + 1] - f[i]))
            break

    if left_cross is not None and right_cross is not None and right_cross > left_cross:
        bw_guess = right_cross - left_cross
        q_guess = max(2.0, center_guess / bw_guess)
    else:
        q_guess = max(2.0, 5.0 * center_guess / span)

    amplitude_guess = max(peak_height, np.finfo(float).eps)
    r_max = max(float(np.max(r)), np.finfo(float).eps)
    lower_bounds = [0.0, 0.0, float(f[0]), 1.0]
    upper_bounds = [2.0 * r_max, 10.0 * r_max, float(f[-1]), 1e9]

    popt, _ = curve_fit(
        _resonator_amplitude_model,
        f,
        r,
        p0=[baseline_guess, amplitude_guess, center_guess, q_guess],
        bounds=(lower_bounds, upper_bounds),
        maxfev=30000,
    )
    baseline, resonance_amplitude, center_hz, q_factor = map(float, popt)
    if q_factor <= 0 or center_hz <= 0:
        raise ValueError("Resonance fit returned non-physical center frequency or Q.")

    bandwidth_hz = center_hz / q_factor
    r_fit = _resonator_amplitude_model(f, *popt)
    amplitude_range = max(float(np.ptp(r)), np.finfo(float).eps)
    fit_error = float(np.sqrt(np.mean((r - r_fit) ** 2)) / amplitude_range)

    # Unwrap phase for fitting. The sign is inferred from the measured phase
    # slope so the same model works for either wiring/polarity convention.
    phi_unwrapped = np.rad2deg(np.unwrap(np.deg2rad(phi)))
    slope = float(np.polyfit(f, phi_unwrapped, 1)[0])
    phase_sign = 1.0 if slope >= 0 else -1.0
    phase_shape = phase_sign * np.rad2deg(
        np.arctan(2.0 * q_factor * (f - center_hz) / center_hz)
    )

    # Weight the phase offset estimate toward the resonance where the signal
    # amplitude and therefore phase SNR are best.
    weights = np.maximum(r - baseline, 0.0)
    if not np.any(weights > 0):
        weights = np.ones_like(r)
    phase_center_unwrapped = float(np.average(phi_unwrapped - phase_shape, weights=weights))
    phase_fit_unwrapped = phase_center_unwrapped + phase_shape
    phase_center_deg = float(_wrap_phase_deg(phase_center_unwrapped))
    phase_fit_wrapped = np.asarray(_wrap_phase_deg(phase_fit_unwrapped), dtype=float)

    phase_error_deg = float(
        np.sqrt(np.average((phi_unwrapped - phase_fit_unwrapped) ** 2, weights=weights))
    )

    fit_frequency = np.linspace(float(f[0]), float(f[-1]), 1200)
    fit_amplitude = _resonator_amplitude_model(
        fit_frequency, baseline, resonance_amplitude, center_hz, q_factor
    )
    fit_phase_unwrapped = phase_center_unwrapped + phase_sign * np.rad2deg(
        np.arctan(2.0 * q_factor * (fit_frequency - center_hz) / center_hz)
    )
    fit_phase = np.asarray(_wrap_phase_deg(fit_phase_unwrapped), dtype=float)

    return {
        "center_hz": center_hz,
        "q_factor": q_factor,
        "bandwidth_hz": bandwidth_hz,
        "phase_deg": phase_center_deg,
        "fit_error": fit_error,
        "phase_fit_error_deg": phase_error_deg,
        "fit_frequency_hz": fit_frequency,
        "fit_amplitude_v": fit_amplitude,
        "fit_phase_deg": fit_phase,
    }


@dataclass(frozen=True)
class ExpectedRouting:
    """Expected controller routing for the current needle-sensor AFM setup."""

    phase_mode: int = 1
    phase_input: int = 3
    phase_inputchannel: int = 0
    phase_output: int = 2
    phase_outputchannel: int = 0

    amplitude_mode: int = 0
    amplitude_input: int = 2
    amplitude_inputchannel: int = 0
    amplitude_output: int = 0
    amplitude_outputchannel: int = 0


class MFLIWorker(QObject):
    """Own all LabOne communication in a worker thread."""

    connected = Signal(dict)
    connection_failed = Signal(str)
    disconnected = Signal()
    settings_updated = Signal(dict)
    live_updated = Signal(dict)
    advisor_started = Signal()
    advisor_finished = Signal(dict)
    advisor_failed = Signal(str)
    amp_advisor_started = Signal()
    amp_advisor_finished = Signal(dict)
    amp_advisor_failed = Signal(str)
    sweep_started = Signal()
    sweep_progress = Signal(int, float)
    sweep_finished = Signal(dict)
    sweep_failed = Signal(str)
    warning = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.session: Session | None = None
        self.device = None
        self.phase = None
        self.amplitude = None
        self.signal_output = None

        self._timer: QTimer | None = None
        self._poll_period_s = 0.05
        self._last_phase_stream = 0.0
        self._last_amplitude_stream = 0.0
        self._last_lock_read = 0.0
        self._stream_nodes: dict[str, Any] = {}
        self._sweep_abort = threading.Event()
        self._sweep_running = False

    @Slot(str, str)
    def connect_instrument(self, host: str, serial: str) -> None:
        if self.device is not None:
            self.warning.emit("Already connected.")
            return

        host = host.strip()
        serial = serial.strip()

        if not host:
            self.connection_failed.emit("Data Server host is empty.")
            return
        if not serial:
            self.connection_failed.emit("Device serial is empty (for example DEV1234).")
            return

        try:
            self.session = Session(host, allow_version_mismatch=True)
            self.device = self.session.connect_device(serial)

            self.phase = self.device.pids[PHASE_PID_INDEX]
            self.amplitude = self.device.pids[AMPLITUDE_PID_INDEX]
            self.signal_output = self.device.sigouts[0]

            self.session.sync()

            self._stream_nodes = {
                "phase_error": self.phase.stream.error,
                "phase_shift": self.phase.stream.shift,
                "phase_value": self.phase.stream.value,
                "amp_error": self.amplitude.stream.error,
                "amp_shift": self.amplitude.stream.shift,
                "amp_value": self.amplitude.stream.value,
            }
            for node in self._stream_nodes.values():
                node.subscribe()

            self._last_phase_stream = 0.0
            self._last_amplitude_stream = 0.0
            self._last_lock_read = 0.0

            self._start_timer()

            configuration = self._read_configuration()
            self.connected.emit(configuration)
            self.settings_updated.emit(self._read_settings())
            self._validate_routing(configuration)

        except Exception as exc:
            self._cleanup_connection()
            self.connection_failed.emit(f"{type(exc).__name__}: {exc}")

    def _start_timer(self) -> None:
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.setTimerType(Qt.TimerType.CoarseTimer)
            self._timer.timeout.connect(self._poll_once)
        self._timer.start(round(self._poll_period_s * 1000))

    @Slot()
    def refresh_settings(self) -> None:
        if self.device is None:
            return
        try:
            configuration = self._read_configuration()
            self.settings_updated.emit(self._read_settings())
            self._validate_routing(configuration)
        except Exception as exc:
            self.warning.emit(f"Refresh failed: {type(exc).__name__}: {exc}")

    def _read_configuration(self) -> dict[str, Any]:
        return {
            "serial": str(self.device.serial),
            "phase_mode": int(self.phase.mode(enum=False)),
            "phase_input": int(self.phase.input(enum=False)),
            "phase_inputchannel": int(self.phase.inputchannel()),
            "phase_output": int(self.phase.output(enum=False)),
            "phase_outputchannel": int(self.phase.outputchannel()),
            "amplitude_mode": int(self.amplitude.mode(enum=False)),
            "amplitude_input": int(self.amplitude.input(enum=False)),
            "amplitude_inputchannel": int(self.amplitude.inputchannel()),
            "amplitude_output": int(self.amplitude.output(enum=False)),
            "amplitude_outputchannel": int(self.amplitude.outputchannel()),
        }

    def _read_settings(self) -> dict[str, float | int]:
        return {
            "phase_enable": int(self.phase.enable()),
            "phase_setpoint_deg": float(self.phase.setpoint()),
            "phase_p": float(self.phase.p()),
            "phase_i": float(self.phase.i()),
            "phase_center_hz": float(self.phase.center()),
            "phase_lower_hz": float(self.phase.limitlower()),
            "phase_upper_hz": float(self.phase.limitupper()),
            "amp_enable": int(self.amplitude.enable()),
            "amp_setpoint_v": float(self.amplitude.setpoint()),
            "amp_p": float(self.amplitude.p()),
            "amp_i": float(self.amplitude.i()),
            "amp_center_v": float(self.amplitude.center()),
            "amp_lower_v": float(self.amplitude.limitlower()),
            "amp_upper_v": float(self.amplitude.limitupper()),
            "manual_output_amplitude_v": float(self.signal_output.amplitudes[0]()),
            "signal_output_on": int(self.signal_output.on()),
            "input_range_v": float(self.device.sigins[0].range()),
        }

    def _validate_routing(self, cfg: dict[str, Any]) -> None:
        expected = ExpectedRouting()
        mismatches: list[str] = []

        checks = (
            ("PLL1 mode", cfg["phase_mode"], expected.phase_mode),
            ("PLL1 input", cfg["phase_input"], expected.phase_input),
            ("PLL1 input channel", cfg["phase_inputchannel"], expected.phase_inputchannel),
            ("PLL1 output", cfg["phase_output"], expected.phase_output),
            ("PLL1 output channel", cfg["phase_outputchannel"], expected.phase_outputchannel),
            ("PID3 mode", cfg["amplitude_mode"], expected.amplitude_mode),
            ("PID3 input", cfg["amplitude_input"], expected.amplitude_input),
            ("PID3 input channel", cfg["amplitude_inputchannel"], expected.amplitude_inputchannel),
            ("PID3 output", cfg["amplitude_output"], expected.amplitude_output),
            ("PID3 output channel", cfg["amplitude_outputchannel"], expected.amplitude_outputchannel),
        )

        for label, actual, wanted in checks:
            if actual != wanted:
                mismatches.append(f"{label}: got {actual}, expected {wanted}")

        if mismatches:
            self.warning.emit(
                "Controller routing differs from the expected AFM configuration. "
                "The app has NOT changed it.\n" + "\n".join(mismatches)
            )

    @Slot(str, object)
    def set_parameter(self, key: str, value: object) -> None:
        if self.device is None:
            self.warning.emit("Not connected.")
            return

        try:
            if key == "signal_output_on":
                self.signal_output.on(int(bool(value)), deep=True)
            elif key == "phase_enable":
                self.phase.enable(int(bool(value)), deep=True)
            elif key == "phase_setpoint_deg":
                self.phase.setpoint(float(value), deep=True)
            elif key == "phase_p":
                self.phase.p(float(value), deep=True)
            elif key == "phase_i":
                self.phase.i(float(value), deep=True)
            elif key == "phase_center_hz":
                self.phase.center(float(value), deep=True)
            elif key == "phase_lower_hz":
                self.phase.limitlower(float(value), deep=True)
            elif key == "phase_upper_hz":
                self.phase.limitupper(float(value), deep=True)

            elif key == "amp_enable":
                self.amplitude.enable(int(bool(value)), deep=True)
            elif key == "manual_output_amplitude_v":
                # Manual drive amplitude is only allowed while PID3 is off.
                # This writes Signal Output 1 / Amplitude 1 directly.
                if int(self.amplitude.enable()):
                    self.warning.emit(
                        "Manual output amplitude is unavailable while the "
                        "Amplitude Controller (PID3) is enabled."
                    )
                    self.settings_updated.emit(self._read_settings())
                    return
                self.signal_output.amplitudes[0](float(value), deep=True)
            elif key == "amp_setpoint_v":
                self.amplitude.setpoint(float(value), deep=True)
            elif key == "amp_p":
                self.amplitude.p(float(value), deep=True)
            elif key == "amp_i":
                self.amplitude.i(float(value), deep=True)
            elif key == "amp_center_v":
                self.amplitude.center(float(value), deep=True)
            elif key == "amp_lower_v":
                self.amplitude.limitlower(float(value), deep=True)
            elif key == "amp_upper_v":
                self.amplitude.limitupper(float(value), deep=True)
            else:
                raise KeyError(f"Unknown parameter {key!r}")

            self.settings_updated.emit(self._read_settings())

        except Exception as exc:
            self.warning.emit(f"Could not set {key}: {type(exc).__name__}: {exc}")
            try:
                self.settings_updated.emit(self._read_settings())
            except Exception:
                pass


    @Slot()
    def center_phase_frequency(self) -> None:
        """Rebase PLL1 Center onto the current PLL output without unlocking.

        The current MFLI PID node tree no longer exposes the historic
        AUTOCENTER command.  The least intrusive equivalent is therefore to
        copy the present PID output (Value) into Center while leaving PLL1
        continuously enabled.  We deliberately do not toggle ENABLE, KEEPINT,
        or any PID gain here.

        After the write we read Shift back.  A non-negligible residual is only
        reported; this routine never falls back to restarting the PLL.
        """
        if self.device is None:
            self.warning.emit("Not connected.")
            return

        try:
            current_frequency = float(self.phase.value())

            # One synchronous setting write.  PLL1 remains enabled throughout.
            self.phase.center(current_frequency, deep=True)

            center = float(self.phase.center())
            shift = float(self.phase.shift())
            value = float(self.phase.value())

            self.settings_updated.emit(self._read_settings())
            self.live_updated.emit(
                {
                    "phase_shift": shift,
                    "phase_value": value,
                }
            )

            # A small finite residual can occur because the PLL continues to
            # evolve while the write/readback crosses the Data Server.  Flag
            # only a clearly visible residual; do not disturb the controller.
            tolerance_hz = max(1e-6, abs(value) * 1e-12)
            if abs(shift) > tolerance_hz:
                self.warning.emit(
                    "Center updated without unlocking PLL1, but the immediate "
                    f"Shift readback is {shift:+.6g} Hz (Center {center:.9g} Hz). "
                    "No automatic PLL restart was performed."
                )

        except Exception as exc:
            self.warning.emit(
                f"Could not center frequency: {type(exc).__name__}: {exc}"
            )

    @Slot(float, float, float, float)
    def advise_phase_pll(
        self,
        target_bw_hz: float,
        q_factor: float,
        gain_m_per_v: float,
        resonant_frequency_hz: float,
    ) -> None:
        """Run the LabOne PID Advisor for PLL1 and transfer the result to PID1.

        Configuration is intentionally fixed for this AFM workflow:
          - controller index: PLL1 / PID index 0
          - advisor mode: PI (P + I optimization)
          - DUT model: Resonator Frequency
          - delay: 0 s

        The current PLL1 P/I values are supplied as starting values.  Once the
        calculation finishes, LabOne's native ``To PID`` operation is triggered
        via ``todevice(1)`` and the resulting device values are read back.
        """
        if self.session is None or self.device is None:
            self.advisor_failed.emit("Not connected.")
            return

        if target_bw_hz <= 0:
            self.advisor_failed.emit("PLL Advisor target bandwidth must be > 0 Hz.")
            return
        if q_factor <= 0:
            self.advisor_failed.emit("PLL Advisor Q factor must be > 0.")
            return
        if gain_m_per_v <= 0:
            self.advisor_failed.emit("PLL Advisor Amp./Exc. gain must be > 0 m/V.")
            return
        if resonant_frequency_hz <= 0:
            self.advisor_failed.emit("PLL Advisor resonant frequency must be > 0 Hz.")
            return

        self.advisor_started.emit()

        try:
            advisor = self.session.modules.pid_advisor
            advisor.device(self.device)

            # Do not recalculate merely because a parameter is edited.  The
            # calculation is started explicitly below when the user clicks Advise.
            advisor.auto(False)
            advisor.index(PHASE_PID_INDEX)

            # PID mode is bit-coded: P=1, I=2 -> PI=3.
            advisor.pid.mode(3)
            advisor.pid.targetbw(float(target_bw_hz))

            # DUT source 3 is the Resonator Frequency model.
            advisor.dut.source(3)
            advisor.dut.delay(0.0)
            advisor.dut.fcenter(float(resonant_frequency_hz))
            advisor.dut.q(float(q_factor))

            # Keep the Nanonis Amp./Exc. value connected to the Advisor's DUT
            # gain node.  For Zurich's Resonator Frequency model the dominant
            # parameters are f_res and Q, but the generic module still exposes
            # DUT gain and accepts it for non-internal-PLL models.
            advisor.dut.gain(float(gain_m_per_v))

            # Use the present controller values as optimization starting points.
            advisor.pid.p(float(self.phase.p()))
            advisor.pid.i(float(self.phase.i()))
            advisor.pid.d(0.0)

            # Start the module and request one explicit calculation.
            advisor.raw_module.execute()
            advisor.calculate(1)
            advisor.wait_done(timeout=60.0, sleep_time=0.1)

            advised_p = float(advisor.pid.p())
            advised_i = float(advisor.pid.i())

            try:
                achieved_bw = float(advisor.bw())
            except Exception:
                achieved_bw = float("nan")
            try:
                phase_margin = float(advisor.pm())
            except Exception:
                phase_margin = float("nan")

            # Equivalent to LabOne's "To PID" button for the selected index.
            advisor.todevice(1)
            self.session.sync()

            applied_p = float(self.phase.p())
            applied_i = float(self.phase.i())

            self.settings_updated.emit(self._read_settings())
            self.advisor_finished.emit(
                {
                    "target_bw_hz": float(target_bw_hz),
                    "q_factor": float(q_factor),
                    "gain_m_per_v": float(gain_m_per_v),
                    "resonant_frequency_hz": float(resonant_frequency_hz),
                    "advised_p": advised_p,
                    "advised_i": advised_i,
                    "applied_p": applied_p,
                    "applied_i": applied_i,
                    "achieved_bw_hz": achieved_bw,
                    "phase_margin_deg": phase_margin,
                }
            )

        except Exception as exc:
            self.advisor_failed.emit(
                f"PLL Advisor failed: {type(exc).__name__}: {exc}"
            )

    @Slot(float, float, float, float, float)
    def advise_amplitude_pid(
        self,
        target_bw_hz: float,
        q_factor: float,
        gain_amp_per_exc: float,
        resonant_frequency_hz: float,
        delay_s: float,
    ) -> None:
        """Run the LabOne PID Advisor for PID3 and transfer the PI result.

        This is the amplitude-control counterpart of the PLL1 advisor:
          - controller index: PID3 / PID index 2
          - advisor mode: PI (P + I optimization)
          - DUT model: Resonator Amplitude
          - model parameters: Gain, resonance frequency, Q and external delay

        The present PID3 P/I values are used as optimization starting values.
        Once advising finishes, LabOne's native ``To PID`` operation is used so
        that the calculated coefficients are written directly to PID3.
        """
        if self.session is None or self.device is None:
            self.amp_advisor_failed.emit("Not connected.")
            return

        if target_bw_hz <= 0:
            self.amp_advisor_failed.emit(
                "Amplitude Advisor target bandwidth must be > 0 Hz."
            )
            return
        if q_factor <= 0:
            self.amp_advisor_failed.emit("Amplitude Advisor Q factor must be > 0.")
            return
        if gain_amp_per_exc <= 0:
            self.amp_advisor_failed.emit(
                "Amplitude Advisor Amp./Exc. gain must be > 0."
            )
            return
        if resonant_frequency_hz <= 0:
            self.amp_advisor_failed.emit(
                "Amplitude Advisor resonant frequency must be > 0 Hz."
            )
            return
        if delay_s < 0:
            self.amp_advisor_failed.emit(
                "Amplitude Advisor delay must be >= 0 s."
            )
            return

        self.amp_advisor_started.emit()

        try:
            advisor = self.session.modules.pid_advisor
            advisor.device(self.device)
            advisor.auto(False)
            advisor.index(AMPLITUDE_PID_INDEX)

            # PID mode is bit-coded: P=1, I=2 -> PI=3.
            advisor.pid.mode(3)
            advisor.pid.targetbw(float(target_bw_hz))

            # DUT source 6 is the Resonator Amplitude model.
            advisor.dut.source(6)
            advisor.dut.delay(float(delay_s))
            advisor.dut.gain(float(gain_amp_per_exc))
            advisor.dut.fcenter(float(resonant_frequency_hz))
            advisor.dut.q(float(q_factor))

            # Use present PID3 values as the starting point and keep D disabled.
            advisor.pid.p(float(self.amplitude.p()))
            advisor.pid.i(float(self.amplitude.i()))
            advisor.pid.d(0.0)

            advisor.raw_module.execute()
            advisor.calculate(1)
            advisor.wait_done(timeout=60.0, sleep_time=0.1)

            advised_p = float(advisor.pid.p())
            advised_i = float(advisor.pid.i())

            try:
                achieved_bw = float(advisor.bw())
            except Exception:
                achieved_bw = float("nan")
            try:
                phase_margin = float(advisor.pm())
            except Exception:
                phase_margin = float("nan")

            # Equivalent to LabOne's "To PID" button for selected PID3.
            advisor.todevice(1)
            self.session.sync()

            applied_p = float(self.amplitude.p())
            applied_i = float(self.amplitude.i())

            self.settings_updated.emit(self._read_settings())
            self.amp_advisor_finished.emit(
                {
                    "target_bw_hz": float(target_bw_hz),
                    "q_factor": float(q_factor),
                    "gain_amp_per_exc": float(gain_amp_per_exc),
                    "resonant_frequency_hz": float(resonant_frequency_hz),
                    "delay_s": float(delay_s),
                    "advised_p": advised_p,
                    "advised_i": advised_i,
                    "applied_p": applied_p,
                    "applied_i": applied_i,
                    "achieved_bw_hz": achieved_bw,
                    "phase_margin_deg": phase_margin,
                }
            )

        except Exception as exc:
            self.amp_advisor_failed.emit(
                f"Amplitude Advisor failed: {type(exc).__name__}: {exc}"
            )

    @Slot(float, float, int, float, float, float)
    def run_frequency_sweep(
        self,
        lower_offset_hz: float,
        upper_offset_hz: float,
        points: int,
        period_s: float,
        initial_settling_s: float,
        center_hz: float,
    ) -> None:
        """Sweep Oscillator 1 frequency and record Demodulator 1 X/Y.

        The sweep range is expressed as offsets relative to ``center_hz``.
        ``period_s`` is used as the minimum averaging time per sweep point,
        matching the Nanonis Frequency Sweep notion of a point period.
        Existing Demodulator 1 filter settings are left untouched by using the
        Sweeper Module's manual bandwidth-control mode.
        """
        if self.session is None or self.device is None:
            self.sweep_failed.emit("Not connected.")
            return
        if self._sweep_running:
            self.sweep_failed.emit("A frequency sweep is already running.")
            return
        if int(self.phase.enable()):
            self.sweep_failed.emit(
                "Disable the Phase Controller (PLL1) before a resonance sweep."
            )
            return
        if int(self.amplitude.enable()):
            self.sweep_failed.emit(
                "Disable the Amplitude Controller (PID3) before a resonance sweep; "
                "otherwise the amplitude loop would flatten the resonance."
            )
            return
        if not int(self.signal_output.on()):
            self.sweep_failed.emit("Enable Signal Output 1 before starting the sweep.")
            return
        if points < 16:
            self.sweep_failed.emit("Use at least 16 sweep points.")
            return
        if upper_offset_hz <= lower_offset_hz:
            self.sweep_failed.emit("Sweep Upper must be greater than Lower.")
            return
        if center_hz + lower_offset_hz <= 0:
            self.sweep_failed.emit("Sweep start frequency must be greater than 0 Hz.")
            return
        if period_s < 0 or initial_settling_s < 0:
            self.sweep_failed.emit("Period and initial settling time must be non-negative.")
            return

        self._sweep_running = True
        self._sweep_abort.clear()
        self.sweep_started.emit()

        sweeper = None
        sample_node = None
        oscillator = self.device.oscs[0]
        original_frequency = float(oscillator.freq())

        try:
            sweeper = self.session.modules.sweeper
            sweeper.device(self.device)
            sample_node = self.device.demods[0].sample

            # Frequency sweep of Oscillator 1, recording Demodulator 1.
            sweeper.gridnode(oscillator.freq)
            sweeper.start(float(center_hz + lower_offset_hz))
            sweeper.stop(float(center_hz + upper_offset_hz))
            sweeper.samplecount(int(points))
            sweeper.xmapping(0)          # linear frequency axis
            sweeper.scan(0)              # sequential low -> high
            sweeper.loopcount(1)
            sweeper.phaseunwrap(1)

            # Preserve the user's Demodulator 1 bandwidth/order settings.
            sweeper.filtermode(1)         # advanced
            sweeper.bandwidthcontrol(0)   # manual: leave demodulator untouched
            sweeper.bandwidth(1.0)        # required >0 even though ignored in manual mode
            sweeper.bandwidthoverlap(1)

            # Wait for the existing lock-in filter to settle after each step,
            # then average for at least the requested point period.
            sweeper.settling.inaccuracy(0.01)
            sweeper.settling.time(0.0)
            sweeper.startdelay(float(initial_settling_s))
            sweeper.averaging.time(float(period_s))
            sweeper.averaging.tc(1.0)
            sweeper.averaging.sample(1)

            sweeper.subscribe(sample_node)
            sweeper.execute()

            while True:
                try:
                    progress = float(sweeper.progress())
                except Exception:
                    progress = 0.0
                try:
                    remaining = float(sweeper.remainingtime())
                except Exception:
                    remaining = float("nan")
                self.sweep_progress.emit(
                    max(0, min(100, int(round(progress * 100.0)))), remaining
                )

                if self._sweep_abort.is_set():
                    sweeper.finish()
                    raise InterruptedError("Frequency sweep stopped by user.")

                try:
                    finished = bool(sweeper.raw_module.finished())
                except Exception:
                    finished = progress >= 1.0
                if finished or progress >= 1.0:
                    break
                time.sleep(0.10)

            data = sweeper.read()
            node_samples = data.get(sample_node)
            if not node_samples:
                raise RuntimeError("Sweeper returned no Demodulator 1 samples.")

            record = node_samples[-1]
            # Toolkit sweep results are typically [record_dict] per loop.
            if isinstance(record, (list, tuple)):
                if not record:
                    raise RuntimeError("Sweeper returned an empty result record.")
                record = record[0]

            frequency = np.asarray(record["frequency"], dtype=float).reshape(-1)
            x = np.asarray(record["x"], dtype=float).reshape(-1)
            y = np.asarray(record["y"], dtype=float).reshape(-1)
            if not (frequency.size == x.size == y.size) or frequency.size < 2:
                raise RuntimeError("Unexpected Sweeper result dimensions.")

            amplitude = np.hypot(x, y)
            phase_deg = np.rad2deg(np.angle(x + 1j * y))

            self.sweep_progress.emit(100, 0.0)
            self.sweep_finished.emit(
                {
                    "center_hz": float(center_hz),
                    "lower_offset_hz": float(lower_offset_hz),
                    "upper_offset_hz": float(upper_offset_hz),
                    "frequency_hz": frequency.tolist(),
                    "amplitude_v": amplitude.tolist(),
                    "phase_deg": phase_deg.tolist(),
                }
            )

        except InterruptedError as exc:
            self.sweep_failed.emit(str(exc))
        except Exception as exc:
            self.sweep_failed.emit(
                f"Frequency sweep failed: {type(exc).__name__}: {exc}"
            )
        finally:
            if sweeper is not None and sample_node is not None:
                try:
                    sweeper.unsubscribe(sample_node)
                except Exception:
                    pass
            try:
                oscillator.freq(original_frequency, deep=True)
            except Exception as exc:
                self.warning.emit(
                    f"Could not restore oscillator frequency after sweep: {exc}"
                )
            self._sweep_running = False
            self._sweep_abort.clear()

    @Slot()
    def request_stop_frequency_sweep(self) -> None:
        """Thread-safe stop request; actual Sweeper.finish() runs in worker thread."""
        self._sweep_abort.set()

    @Slot()
    def _poll_once(self) -> None:
        if self.session is None or self.device is None:
            return

        now = time.monotonic()
        live: dict[str, float | int] = {}

        try:
            data = self.session.poll(recording_time=self._poll_period_s, timeout=0.15)

            for name, node in self._stream_nodes.items():
                scalar = _last_scalar(data.get(node))
                if scalar is not None:
                    live[name] = scalar
                    if name.startswith("phase_"):
                        self._last_phase_stream = now
                    elif name.startswith("amp_"):
                        self._last_amplitude_stream = now

            if now - self._last_phase_stream > 0.5:
                live.update(
                    phase_error=float(self.phase.error()),
                    phase_shift=float(self.phase.shift()),
                    phase_value=float(self.phase.value()),
                )

            if now - self._last_amplitude_stream > 0.5:
                live.update(
                    amp_error=float(self.amplitude.error()),
                    amp_shift=float(self.amplitude.shift()),
                    amp_value=float(self.amplitude.value()),
                )

            if now - self._last_lock_read > 0.5:
                live["phase_locked"] = int(self.phase.pll.locked())
                self._last_lock_read = now

            if live:
                self.live_updated.emit(live)

        except Exception as exc:
            self.warning.emit(f"Live update failed: {type(exc).__name__}: {exc}")

    @Slot()
    def shutdown(self) -> None:
        self._cleanup_connection()
        self.disconnected.emit()

    def _cleanup_connection(self) -> None:
        if self._timer is not None:
            self._timer.stop()

        for node in self._stream_nodes.values():
            try:
                node.unsubscribe()
            except Exception:
                pass
        self._stream_nodes.clear()

        if self.session is not None and self.device is not None:
            try:
                self.session.disconnect_device(str(self.device.serial))
            except Exception:
                pass

        self.phase = None
        self.amplitude = None
        self.signal_output = None
        self.device = None
        self.session = None


def load_ui(path: Path) -> QMainWindow:
    """Load the Qt Designer file directly; no pyside6-uic step is required."""
    ui_file = QFile(str(path))
    if not ui_file.open(QFile.OpenModeFlag.ReadOnly):
        raise RuntimeError(f"Could not open UI file: {path}")

    loader = QUiLoader()
    loader.registerCustomWidget(NanonisSpinBox)
    try:
        window = loader.load(ui_file)
    finally:
        ui_file.close()

    if window is None:
        raise RuntimeError(f"QUiLoader could not load {path}: {loader.errorString()}")
    if not isinstance(window, QMainWindow):
        raise TypeError(f"Top-level widget in {path.name} must be a QMainWindow.")
    return window


class FrequencySweepWindow(QObject):
    """Nanonis-style resonance sweep sub-window."""

    start_requested = Signal(float, float, int, float, float, float)
    stop_requested = Signal()
    apply_requested = Signal(dict)

    def __init__(self, ui_path: Path) -> None:
        super().__init__()
        self.window = load_ui(ui_path)
        self._last_fit: dict[str, Any] | None = None
        self._range_initialized = False
        self._bind_widgets()
        self._configure_widgets()
        self._build_plots()
        self._connect_signals()

    def widget(self, cls: type[T], name: str) -> T:
        obj = self.window.findChild(cls, name)
        if obj is None:
            raise RuntimeError(
                f"Required sweep widget {name!r} ({cls.__name__}) was not found in "
                f"{SWEEP_UI_FILENAME}."
            )
        return obj

    def _bind_widgets(self) -> None:
        self.current_shift = self.widget(NanonisSpinBox, "currentShiftValue")
        self.center_value = self.widget(NanonisSpinBox, "centerValue")
        self.lower_offset = self.widget(NanonisSpinBox, "lowerOffset")
        self.upper_offset = self.widget(NanonisSpinBox, "upperOffset")
        self.points = self.widget(QSpinBox, "pointsSpinBox")
        self.period = self.widget(NanonisSpinBox, "periodSpinBox")
        self.initial_settling = self.widget(NanonisSpinBox, "initialSettlingSpinBox")
        self.start_button = self.widget(QPushButton, "startButton")
        self.stop_button = self.widget(QPushButton, "stopButton")
        self.progress = self.widget(QProgressBar, "progressBar")
        self.status_label = self.widget(QLabel, "sweepStatusLabel")

        self.fit_center = self.widget(NanonisSpinBox, "fitCenterValue")
        self.fit_q = self.widget(NanonisSpinBox, "fitQValue")
        self.fit_bw = self.widget(NanonisSpinBox, "fitBwValue")
        self.fit_phase = self.widget(NanonisSpinBox, "fitPhaseValue")
        self.fit_error = self.widget(NanonisSpinBox, "fitErrorValue")
        self.apply_button = self.widget(QPushButton, "applyFitButton")

        self.amplitude_plot_container = self.widget(QWidget, "amplitudePlotContainer")
        self.phase_plot_container = self.widget(QWidget, "phasePlotContainer")

    def _configure_widgets(self) -> None:
        editable = (
            (self.lower_offset, "Hz"),
            (self.upper_offset, "Hz"),
            (self.period, "s"),
            (self.initial_settling, "s"),
        )
        readbacks = (
            (self.current_shift, "Hz"),
            (self.center_value, "Hz"),
            (self.fit_center, "Hz"),
            (self.fit_q, ""),
            (self.fit_bw, "Hz"),
            (self.fit_phase, "deg"),
            (self.fit_error, ""),
        )
        for control, unit in editable:
            control.setBaseUnit(unit)
            control.setDisplayDecimals(6)
        readback_style = (
            "QDoubleSpinBox {"
            " background-color: rgb(238, 238, 238);"
            " color: palette(text);"
            " border: 1px solid palette(mid);"
            " padding: 1px 3px;"
            "}"
        )
        for control, unit in readbacks:
            control.setBaseUnit(unit)
            control.setDisplayDecimals(6)
            control.setReadOnly(True)
            control.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            control.setStyleSheet(readback_style)

    def _build_plots(self) -> None:
        self.amplitude_figure = Figure(figsize=(7, 3), tight_layout=True)
        self.amplitude_canvas = FigureCanvas(self.amplitude_figure)
        self.amplitude_axis = self.amplitude_figure.add_subplot(111)
        self.amplitude_plot_container.layout().addWidget(self.amplitude_canvas)

        self.phase_figure = Figure(figsize=(7, 3), tight_layout=True)
        self.phase_canvas = FigureCanvas(self.phase_figure)
        self.phase_axis = self.phase_figure.add_subplot(111)
        self.phase_plot_container.layout().addWidget(self.phase_canvas)

        self._clear_plots()

    def _clear_plots(self) -> None:
        self.amplitude_axis.clear()
        self.amplitude_axis.set_xlabel("Frequency Shift (Hz)")
        self.amplitude_axis.set_ylabel("Demod 1 R (V RMS)")
        self.amplitude_axis.grid(True, alpha=0.3)
        self.phase_axis.clear()
        self.phase_axis.set_xlabel("Frequency Shift (Hz)")
        self.phase_axis.set_ylabel("Demod 1 Phase (deg)")
        self.phase_axis.grid(True, alpha=0.3)
        self.amplitude_canvas.draw_idle()
        self.phase_canvas.draw_idle()

    def _connect_signals(self) -> None:
        self.start_button.clicked.connect(self._start)
        self.stop_button.clicked.connect(self.stop_requested.emit)
        self.apply_button.clicked.connect(self._apply_fit)

    def prepare(
        self,
        center_hz: float,
        current_shift_hz: float,
        default_lower_hz: float,
        default_upper_hz: float,
        connected: bool,
    ) -> None:
        self.center_value.setValue(float(center_hz))
        self.current_shift.setValue(float(current_shift_hz))
        if not self._range_initialized:
            if default_lower_hz < default_upper_hz:
                self.lower_offset.setValue(float(default_lower_hz))
                self.upper_offset.setValue(float(default_upper_hz))
            self._range_initialized = True
        self.start_button.setEnabled(bool(connected))
        if connected:
            self.status_label.setText(
                "PLL1 and PID3 must be disabled; Signal Output 1 must be enabled."
            )

    def update_center(self, center_hz: float) -> None:
        self.center_value.setValue(float(center_hz))

    def update_current_shift(self, shift_hz: float) -> None:
        self.current_shift.setValue(float(shift_hz))

    def _start(self) -> None:
        lower = float(self.lower_offset.value())
        upper = float(self.upper_offset.value())
        center = float(self.center_value.value())
        period = float(self.period.value())
        settling = float(self.initial_settling.value())
        points = int(self.points.value())

        if upper <= lower:
            self.status_label.setText("Upper sweep limit must be greater than Lower.")
            return
        if center + lower <= 0:
            self.status_label.setText("Sweep start frequency must be greater than 0 Hz.")
            return

        self._last_fit = None
        self.apply_button.setEnabled(False)
        self.progress.setValue(0)
        self.status_label.setText("Starting frequency sweep...")
        self.start_requested.emit(lower, upper, points, period, settling, center)

    def _apply_fit(self) -> None:
        if self._last_fit is not None:
            self.apply_requested.emit(dict(self._last_fit))

    @Slot()
    def on_sweep_started(self) -> None:
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.apply_button.setEnabled(False)
        self.status_label.setText("Sweeping Oscillator 1; recording Demodulator 1 R/Phase...")

    @Slot(int, float)
    def on_sweep_progress(self, progress: int, remaining_s: float) -> None:
        self.progress.setValue(int(progress))
        if math.isfinite(remaining_s) and remaining_s >= 0:
            self.status_label.setText(
                f"Frequency sweep in progress — approximately {remaining_s:.1f} s remaining."
            )

    @Slot(dict)
    def on_sweep_finished(self, data: dict) -> None:
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.progress.setValue(100)

        frequency = np.asarray(data["frequency_hz"], dtype=float)
        amplitude = np.asarray(data["amplitude_v"], dtype=float)
        phase = np.asarray(data["phase_deg"], dtype=float)
        center_used = float(data["center_hz"])

        try:
            fit = fit_resonance_sweep(frequency, amplitude, phase)
        except Exception as exc:
            self._last_fit = None
            self.apply_button.setEnabled(False)
            self.status_label.setText(
                f"Sweep complete, but resonance fit failed: {type(exc).__name__}: {exc}"
            )
            self._plot_data(frequency, amplitude, phase, center_used, None)
            return

        self._last_fit = fit
        self.fit_center.setValue(float(fit["center_hz"]))
        self.fit_q.setValue(float(fit["q_factor"]))
        self.fit_bw.setValue(float(fit["bandwidth_hz"]))
        self.fit_phase.setValue(float(fit["phase_deg"]))
        self.fit_error.setValue(float(fit["fit_error"]))
        self.apply_button.setEnabled(True)
        self._plot_data(frequency, amplitude, phase, center_used, fit)
        self.status_label.setText(
            "Sweep and resonance fit complete. Review the curves, then Apply Fit "
            "to copy Q, center frequency and phase reference to Oscillation Control."
        )

    @Slot(str)
    def on_sweep_failed(self, message: str) -> None:
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.status_label.setText(message)

    def _plot_data(
        self,
        frequency: np.ndarray,
        amplitude: np.ndarray,
        phase: np.ndarray,
        sweep_center: float,
        fit: dict[str, Any] | None,
    ) -> None:
        shift = frequency - sweep_center

        self.amplitude_axis.clear()
        self.amplitude_axis.plot(shift, amplitude, label="Demod 1 R")
        self.amplitude_axis.set_xlabel("Frequency Shift (Hz)")
        self.amplitude_axis.set_ylabel("Demod 1 R (V RMS)")
        self.amplitude_axis.grid(True, alpha=0.3)

        self.phase_axis.clear()
        self.phase_axis.plot(shift, phase, label="Demod 1 Phase")
        self.phase_axis.set_xlabel("Frequency Shift (Hz)")
        self.phase_axis.set_ylabel("Demod 1 Phase (deg)")
        self.phase_axis.grid(True, alpha=0.3)

        if fit is not None:
            fit_frequency = np.asarray(fit["fit_frequency_hz"], dtype=float)
            fit_shift = fit_frequency - sweep_center
            self.amplitude_axis.plot(
                fit_shift,
                np.asarray(fit["fit_amplitude_v"], dtype=float),
                linestyle="--",
                label="Resonance fit",
            )
            self.phase_axis.plot(
                fit_shift,
                np.asarray(fit["fit_phase_deg"], dtype=float),
                linestyle="--",
                label="Phase fit",
            )
            center_shift = float(fit["center_hz"]) - sweep_center
            self.amplitude_axis.axvline(center_shift, linestyle=":")
            self.phase_axis.axvline(center_shift, linestyle=":")
            self.amplitude_axis.legend(loc="best")
            self.phase_axis.legend(loc="best")

        self.amplitude_canvas.draw_idle()
        self.phase_canvas.draw_idle()


class OscillationControlApp(QObject):
    """
    Controller/glue layer for the Designer UI.

    The .ui file owns layout, labels, ranges and visual design.
    This class only finds widgets by objectName and connects them to the MFLI.
    """

    connect_requested = Signal(str, str)
    refresh_requested = Signal()
    center_requested = Signal()
    advise_requested = Signal(float, float, float, float)
    amp_advise_requested = Signal(float, float, float, float, float)
    sweep_requested = Signal(float, float, int, float, float, float)
    sweep_stop_requested = Signal()
    set_requested = Signal(str, object)
    shutdown_requested = Signal()

    def __init__(self, ui_path: Path) -> None:
        super().__init__()
        self.window = load_ui(ui_path)
        self.config_path = ui_path.with_name(CONNECTION_CONFIG_FILENAME)
        self._updating_from_device = False
        self._shutting_down = False
        self.sweep_window: FrequencySweepWindow | None = None

        self._bind_widgets()
        self._load_connection_settings()
        self._configure_nanonis_spinboxes()
        self._connect_gui_signals()
        self._build_worker()

    def widget(self, cls: type[T], name: str) -> T:
        obj = self.window.findChild(cls, name)
        if obj is None:
            raise RuntimeError(
                f"Required widget {name!r} ({cls.__name__}) was not found in "
                f"{UI_FILENAME}. Keep this objectName when editing in Qt Designer."
            )
        return obj

    def _bind_widgets(self) -> None:
        # Connection
        self.host_edit = self.widget(QLineEdit, "hostEdit")
        self.serial_edit = self.widget(QLineEdit, "serialEdit")
        self.connect_button = self.widget(QPushButton, "connectButton")
        self.refresh_button = self.widget(QPushButton, "refreshButton")
        self.statusbar = self.widget(QStatusBar, "statusbar")

        # Output / frequency-generator controls
        self.signal_output_enable = self.widget(QCheckBox, "signalOutputEnable")
        self.center_button = self.widget(QPushButton, "centerButton")
        self.sweep_button = self.widget(QPushButton, "sweepButton")
        self.phase_center = self.widget(NanonisSpinBox, "phaseCenter")
        self.phase_lower = self.widget(NanonisSpinBox, "phaseLower")
        self.phase_upper = self.widget(NanonisSpinBox, "phaseUpper")
        self.phase_shift_label = self.widget(NanonisSpinBox, "phaseShiftValue")
        self.phase_value_label = self.widget(NanonisSpinBox, "phaseValueValue")

        # Signal / setpoint area
        self.amp_setpoint = self.widget(NanonisSpinBox, "ampSetpoint")
        self.amp_setpoint_slider = self.widget(QSlider, "ampSetpointSlider")
        self.amp_setpoint_slider_min = self.widget(QLabel, "ampSetpointSliderMin")
        self.amp_setpoint_slider_max = self.widget(QLabel, "ampSetpointSliderMax")
        self.amp_measured = self.widget(NanonisSpinBox, "ampMeasuredValue")
        self.amp_error_label = self.widget(NanonisSpinBox, "ampErrorValue")
        self.phase_setpoint = self.widget(NanonisSpinBox, "phaseSetpoint")
        self.phase_measured = self.widget(NanonisSpinBox, "phaseMeasuredValue")
        self.phase_error_label = self.widget(NanonisSpinBox, "phaseErrorValue")

        # PerfectPLL / PLL1 Advisor controls
        self.advisor_q = self.widget(NanonisSpinBox, "advisorQ")
        self.advisor_gain = self.widget(NanonisSpinBox, "advisorGain")
        self.amp_advisor_target_bw = self.widget(NanonisSpinBox, "advisorAmpTargetBw")
        self.advisor_target_bw = self.widget(NanonisSpinBox, "advisorTargetBw")
        self.amp_advisor_delay = self.widget(NanonisSpinBox, "advisorDelay")
        self.amp_advise_button = self.widget(QPushButton, "adviseAmpButton")
        self.advise_button = self.widget(QPushButton, "adviseButton")

        # PLL / PID controllers
        self.amp_enable = self.widget(QCheckBox, "ampEnable")
        self.amp_p = self.widget(NanonisSpinBox, "ampP")
        self.amp_i = self.widget(NanonisSpinBox, "ampI")
        self.phase_enable = self.widget(QCheckBox, "phaseEnable")
        self.phase_p = self.widget(NanonisSpinBox, "phaseP")
        self.phase_i = self.widget(NanonisSpinBox, "phaseI")
        self.phase_lock_label = self.widget(QLabel, "phaseLockValue")

        # Amplitude-output values and limits
        self.amp_center = self.widget(NanonisSpinBox, "ampCenter")
        self.amp_lower = self.widget(NanonisSpinBox, "ampLower")
        self.amp_upper = self.widget(NanonisSpinBox, "ampUpper")
        self.amp_shift_label = self.widget(NanonisSpinBox, "ampShiftValue")
        self.amp_value_label = self.widget(NanonisSpinBox, "ampValueValue")
        self.amp_actual_min_label = self.widget(NanonisSpinBox, "ampActualMinValue")
        self.amp_actual_max_label = self.widget(NanonisSpinBox, "ampActualMaxValue")

    def _load_connection_settings(self) -> None:
        """Load the last LabOne host and MFLI device name from YAML."""
        if not self.config_path.exists():
            return

        try:
            data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
            connection = data.get("connection", {})
            host = str(connection.get("host", "")).strip()
            device = str(connection.get("device", "")).strip()

            if host:
                self.host_edit.setText(host)
            if device:
                self.serial_edit.setText(device)
        except Exception as exc:
            self.statusbar.showMessage(
                f"Could not load {self.config_path.name}: {type(exc).__name__}: {exc}",
                12000,
            )

    def _save_connection_settings(self) -> None:
        """Persist the current LabOne host and MFLI device name to YAML."""
        data = {
            "connection": {
                "host": self.host_edit.text().strip(),
                "device": self.serial_edit.text().strip(),
            }
        }

        try:
            self.config_path.write_text(
                yaml.safe_dump(data, sort_keys=False),
                encoding="utf-8",
            )
        except Exception as exc:
            self.statusbar.showMessage(
                f"Could not save {self.config_path.name}: {type(exc).__name__}: {exc}",
                12000,
            )

    def _configure_nanonis_spinboxes(self) -> None:
        """Configure Nanonis-style editable fields and grey readbacks."""
        controls = (
            (self.phase_setpoint, "deg"),
            (self.phase_p, "Hz/deg"),
            (self.phase_i, "Hz/(deg·s)"),
            (self.phase_center, "Hz"),
            (self.phase_lower, "Hz"),
            (self.phase_upper, "Hz"),
            (self.amp_setpoint, "V"),
            (self.amp_p, "V/V"),
            (self.amp_i, "1/s"),
            (self.amp_center, "V"),
            (self.amp_lower, "V"),
            (self.amp_upper, "V"),
            (self.advisor_q, ""),
            (self.advisor_gain, "m/V"),
            (self.amp_advisor_target_bw, "Hz"),
            (self.advisor_target_bw, "Hz"),
            (self.amp_advisor_delay, "s"),
        )

        readbacks = (
            (self.phase_error_label, "deg"),
            (self.phase_measured, "deg"),
            (self.phase_shift_label, "Hz"),
            (self.phase_value_label, "Hz"),
            (self.amp_error_label, "V"),
            (self.amp_measured, "V"),
            (self.amp_shift_label, "V"),
            (self.amp_actual_min_label, "Vpk"),
            (self.amp_actual_max_label, "Vpk"),
        )

        hint = (
            "Enter number + SI prefix only (for example 60m, 974.5k). "
            "The fixed base unit is shown in the label. Put the cursor "
            "immediately before a digit and use mouse wheel or Up/Down "
            "to change that digit."
        )

        for control, base_unit in controls:
            control.setBaseUnit(base_unit)
            control.setDisplayDecimals(6)
            control.setToolTip(f"{hint} Base unit: {base_unit or '1'}.")

        readback_style = (
            "QDoubleSpinBox {"
            " background-color: rgb(238, 238, 238);"
            " color: palette(text);"
            " border: 1px solid palette(mid);"
            " padding: 1px 3px;"
            "}"
        )
        for control, base_unit in readbacks:
            control.setBaseUnit(base_unit)
            control.setDisplayDecimals(6)
            control.setReadOnly(True)
            control.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            control.setStyleSheet(readback_style)
            control.setToolTip(f"Live MFLI readback. Base unit: {base_unit}.")

        # Output Amplitude is a readback while PID3 controls it, but becomes
        # the manual Signal Output 1 / Amplitude 1 control when PID3 is off.
        self.amp_value_label.setBaseUnit("Vpk")
        self.amp_value_label.setDisplayDecimals(6)
        self._set_amplitude_output_manual_mode(False)

        # Nanonis-style amplitude-setpoint slider. Its physical range is
        # updated from Signal Input 1's current input range when connected.
        self._amp_setpoint_slider_max_v = 1.0
        self.amp_setpoint_slider.setRange(0, AMP_SETPOINT_SLIDER_STEPS)
        self.amp_setpoint_slider.setSingleStep(100)
        self.amp_setpoint_slider.setPageStep(1000)
        self.amp_setpoint_slider_min.setText("0")
        self._update_amp_setpoint_slider_range(self._amp_setpoint_slider_max_v)

    @staticmethod
    def _compact_engineering_text(value: float) -> str:
        """Engineering number + prefix, with trailing zeros removed."""
        text = format_eng_number(float(value), decimals=3)
        prefix = text[-1] if text and (text[-1].isalpha() or text[-1] in "µμ") else ""
        number = text[:-1] if prefix else text
        number = number.rstrip("0").rstrip(".")
        if number in {"", "+", "-"}:
            number += "0"
        return number + prefix

    def _update_amp_setpoint_slider_range(self, maximum_v: float) -> None:
        """Set the amplitude slider's 0..max physical range in volts."""
        maximum_v = float(maximum_v)
        if not math.isfinite(maximum_v) or maximum_v <= 0.0:
            maximum_v = max(abs(float(self.amp_setpoint.value())), 1e-3)

        self._amp_setpoint_slider_max_v = maximum_v
        self.amp_setpoint_slider_max.setText(
            self._compact_engineering_text(maximum_v)
        )
        self._sync_amp_setpoint_slider(float(self.amp_setpoint.value()))

    def _sync_amp_setpoint_slider(self, amplitude_v: float) -> None:
        """Move the slider to match a base-unit amplitude without feedback."""
        maximum_v = self._amp_setpoint_slider_max_v
        if maximum_v <= 0.0:
            return

        fraction = min(1.0, max(0.0, float(amplitude_v) / maximum_v))
        slider_value = round(fraction * AMP_SETPOINT_SLIDER_STEPS)
        blocked = self.amp_setpoint_slider.blockSignals(True)
        try:
            self.amp_setpoint_slider.setValue(slider_value)
        finally:
            self.amp_setpoint_slider.blockSignals(blocked)

    def _amp_setpoint_slider_changed(self, slider_value: int) -> None:
        """Immediately apply slider motion through the normal setpoint path."""
        if self._updating_from_device:
            return

        amplitude_v = (
            float(slider_value) / AMP_SETPOINT_SLIDER_STEPS
        ) * self._amp_setpoint_slider_max_v
        self.amp_setpoint.setValue(amplitude_v)

    def _amp_setpoint_changed(self, amplitude_v: float) -> None:
        """Keep spinbox and slider synchronized and write the setpoint."""
        self._sync_amp_setpoint_slider(amplitude_v)
        self._emit_if_user("amp_setpoint_v", amplitude_v)

    def _set_amplitude_output_manual_mode(self, manual: bool) -> None:
        """Switch Output Amplitude between live readback and manual control."""
        connected = not self.connect_button.isEnabled()
        editable = bool(manual and connected)

        self.amp_value_label.setReadOnly(not editable)
        self.amp_value_label.setFocusPolicy(
            Qt.FocusPolicy.StrongFocus if editable else Qt.FocusPolicy.NoFocus
        )

        if editable:
            self.amp_value_label.setStyleSheet("")
            self.amp_value_label.setToolTip(
                "Manual Signal Output 1 / Amplitude 1. Base unit: Vpk. "
                "Changes are written immediately while the Amplitude Controller is off."
            )
        else:
            self.amp_value_label.setStyleSheet(
                "QDoubleSpinBox {"
                " background-color: rgb(238, 238, 238);"
                " color: palette(text);"
                " border: 1px solid palette(mid);"
                " padding: 1px 3px;"
                "}"
            )
            self.amp_value_label.setToolTip(
                "Live PID3 output-amplitude readback. Base unit: Vpk."
            )

    def _on_amp_enable_toggled(self, enabled: bool) -> None:
        # Change the interaction state immediately in the GUI, then send the
        # enable command to the MFLI worker.
        self._set_amplitude_output_manual_mode(not enabled)
        self._emit_if_user("amp_enable", enabled)

    def _manual_output_amplitude_changed(self, value: float) -> None:
        if self._updating_from_device or self.amp_enable.isChecked():
            return
        self.set_requested.emit("manual_output_amplitude_v", value)

    def _connect_gui_signals(self) -> None:
        self.connect_button.clicked.connect(self._connect_with_saved_settings)
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.center_button.clicked.connect(self.center_requested.emit)
        self.sweep_button.clicked.connect(self._open_frequency_sweep)
        self.amp_advise_button.clicked.connect(self._request_amplitude_advice)
        self.advise_button.clicked.connect(self._request_phase_advice)

        self.signal_output_enable.toggled.connect(
            lambda v: self._emit_if_user("signal_output_on", v)
        )
        self.phase_enable.toggled.connect(
            lambda v: self._emit_if_user("phase_enable", v)
        )
        # valueChanged is immediate for wheel/Up/Down stepping. With
        # keyboardTracking(False), typed text still waits for commit.
        self.phase_setpoint.valueChanged.connect(
            lambda v: self._emit_if_user("phase_setpoint_deg", v)
        )
        self.phase_p.valueChanged.connect(
            lambda v: self._emit_if_user("phase_p", v)
        )
        self.phase_i.valueChanged.connect(
            lambda v: self._emit_if_user("phase_i", v)
        )
        self.phase_center.valueChanged.connect(
            lambda v: self._emit_if_user("phase_center_hz", v)
        )
        self.phase_lower.valueChanged.connect(
            lambda v: self._emit_if_user("phase_lower_hz", v)
        )
        self.phase_upper.valueChanged.connect(
            lambda v: self._emit_if_user("phase_upper_hz", v)
        )

        self.amp_enable.toggled.connect(self._on_amp_enable_toggled)
        self.amp_value_label.valueChanged.connect(
            self._manual_output_amplitude_changed
        )
        self.amp_setpoint_slider.valueChanged.connect(
            self._amp_setpoint_slider_changed
        )
        self.amp_setpoint.valueChanged.connect(
            self._amp_setpoint_changed
        )
        self.amp_p.valueChanged.connect(
            lambda v: self._emit_if_user("amp_p", v)
        )
        self.amp_i.valueChanged.connect(
            lambda v: self._emit_if_user("amp_i", v)
        )
        self.amp_center.valueChanged.connect(
            lambda v: self._emit_if_user("amp_center_v", v)
        )
        self.amp_lower.valueChanged.connect(
            lambda v: self._emit_if_user("amp_lower_v", v)
        )
        self.amp_upper.valueChanged.connect(
            lambda v: self._emit_if_user("amp_upper_v", v)
        )

    def _ensure_frequency_sweep_window(self) -> FrequencySweepWindow:
        if self.sweep_window is None:
            ui_path = Path(__file__).resolve().with_name(SWEEP_UI_FILENAME)
            self.sweep_window = FrequencySweepWindow(ui_path)
            self.sweep_window.start_requested.connect(self.sweep_requested.emit)
            self.sweep_window.stop_requested.connect(self.sweep_stop_requested.emit)
            self.sweep_window.apply_requested.connect(self._apply_sweep_fit)

            self.worker.sweep_started.connect(self.sweep_window.on_sweep_started)
            self.worker.sweep_progress.connect(self.sweep_window.on_sweep_progress)
            self.worker.sweep_finished.connect(self.sweep_window.on_sweep_finished)
            self.worker.sweep_failed.connect(self.sweep_window.on_sweep_failed)
        return self.sweep_window

    def _open_frequency_sweep(self) -> None:
        sweep = self._ensure_frequency_sweep_window()
        connected = not self.connect_button.isEnabled()
        sweep.prepare(
            center_hz=float(self.phase_center.value()),
            current_shift_hz=float(self.phase_shift_label.value()),
            default_lower_hz=float(self.phase_lower.value()),
            default_upper_hz=float(self.phase_upper.value()),
            connected=connected,
        )
        sweep.window.show()
        sweep.window.raise_()
        sweep.window.activateWindow()

    @Slot(dict)
    def _apply_sweep_fit(self, fit: dict) -> None:
        """Copy resonance fit results into the main oscillation-control fields."""
        if self.phase_enable.isChecked() or self.amp_enable.isChecked():
            self.statusbar.showMessage(
                "Disable the Phase and Amplitude controllers before applying a resonance fit.",
                12000,
            )
            return

        q_factor = float(fit["q_factor"])
        center_hz = float(fit["center_hz"])
        phase_deg = float(fit["phase_deg"])

        # advisorQ is local UI state. Center and phase reference use their
        # normal valueChanged paths, so they are also written to the MFLI.
        self.advisor_q.setValue(q_factor)
        self.phase_center.setValue(center_hz)
        self.phase_setpoint.setValue(phase_deg)
        self.statusbar.showMessage(
            f"Resonance fit applied: f0={center_hz:.9g} Hz, Q={q_factor:.7g}, "
            f"phase={phase_deg:.5g} deg.",
            15000,
        )
        if self.sweep_window is not None:
            self.sweep_window.update_center(center_hz)

    def _request_phase_advice(self) -> None:
        """Validate the Nanonis-style advisor fields and start PLL1 advising."""
        target_bw = float(self.advisor_target_bw.value())
        q_factor = float(self.advisor_q.value())
        gain = float(self.advisor_gain.value())
        resonant_frequency = float(self.phase_center.value())

        if target_bw <= 0:
            self.statusbar.showMessage("Enter a PLL target bandwidth > 0 Hz.", 8000)
            return
        if q_factor <= 0:
            self.statusbar.showMessage("Enter a Q factor > 0.", 8000)
            return
        if gain <= 0:
            self.statusbar.showMessage("Enter an Amp./Exc. gain > 0 m/V.", 8000)
            return
        if resonant_frequency <= 0:
            self.statusbar.showMessage("Enter a resonant/center frequency > 0 Hz.", 8000)
            return

        self._set_advisor_buttons_busy("phase")
        self.statusbar.showMessage(
            "Running PLL1 PID Advisor in PI / Resonator Frequency mode..."
        )
        self.advise_requested.emit(
            target_bw,
            q_factor,
            gain,
            resonant_frequency,
        )

    def _request_amplitude_advice(self) -> None:
        """Validate shared resonator parameters and start PID3 advising."""
        target_bw = float(self.amp_advisor_target_bw.value())
        q_factor = float(self.advisor_q.value())
        gain = float(self.advisor_gain.value())
        resonant_frequency = float(self.phase_center.value())
        delay = float(self.amp_advisor_delay.value())

        if target_bw <= 0:
            self.statusbar.showMessage(
                "Enter an amplitude target bandwidth > 0 Hz.", 8000
            )
            return
        if q_factor <= 0:
            self.statusbar.showMessage("Enter a Q factor > 0.", 8000)
            return
        if gain <= 0:
            self.statusbar.showMessage("Enter an Amp./Exc. gain > 0.", 8000)
            return
        if resonant_frequency <= 0:
            self.statusbar.showMessage(
                "Enter a resonant/center frequency > 0 Hz.", 8000
            )
            return
        if delay < 0:
            self.statusbar.showMessage("Enter an Advisor delay >= 0 s.", 8000)
            return

        self._set_advisor_buttons_busy("amplitude")
        self.statusbar.showMessage(
            "Running PID3 Advisor in PI / Resonator Amplitude mode..."
        )
        self.amp_advise_requested.emit(
            target_bw,
            q_factor,
            gain,
            resonant_frequency,
            delay,
        )

    def _set_advisor_buttons_busy(self, active: str) -> None:
        """Prevent overlapping PID Advisor jobs on the shared LabOne module."""
        self.amp_advise_button.setEnabled(False)
        self.advise_button.setEnabled(False)
        self.amp_advise_button.setText(
            "Advising..." if active == "amplitude" else "Advise Amp"
        )
        self.advise_button.setText(
            "Advising..." if active == "phase" else "Advise Pha"
        )

    def _set_advisor_buttons_idle(self) -> None:
        connected = not self.connect_button.isEnabled()
        self.amp_advise_button.setEnabled(connected)
        self.advise_button.setEnabled(connected)
        self.amp_advise_button.setText("Advise Amp")
        self.advise_button.setText("Advise Pha")

    def _connect_with_saved_settings(self) -> None:
        self._save_connection_settings()
        self.connect_requested.emit(
            self.host_edit.text(),
            self.serial_edit.text(),
        )

    def _build_worker(self) -> None:
        self.worker_thread = QThread(self)
        self.worker = MFLIWorker()
        self.worker.moveToThread(self.worker_thread)

        self.connect_requested.connect(self.worker.connect_instrument)
        self.refresh_requested.connect(self.worker.refresh_settings)
        self.center_requested.connect(self.worker.center_phase_frequency)
        self.advise_requested.connect(self.worker.advise_phase_pll)
        self.amp_advise_requested.connect(self.worker.advise_amplitude_pid)
        self.sweep_requested.connect(self.worker.run_frequency_sweep)
        # DirectConnection is safe here because this slot only sets a
        # threading.Event; all MFLI/Sweeper calls remain in the worker thread.
        self.sweep_stop_requested.connect(
            self.worker.request_stop_frequency_sweep,
            Qt.ConnectionType.DirectConnection,
        )
        self.set_requested.connect(self.worker.set_parameter)
        self.shutdown_requested.connect(self.worker.shutdown)

        self.worker.connected.connect(self._on_connected)
        self.worker.connection_failed.connect(self._on_connection_failed)
        self.worker.settings_updated.connect(self._apply_settings)
        self.worker.live_updated.connect(self._apply_live)
        self.worker.advisor_started.connect(self._on_advisor_started)
        self.worker.advisor_finished.connect(self._on_advisor_finished)
        self.worker.advisor_failed.connect(self._on_advisor_failed)
        self.worker.amp_advisor_started.connect(self._on_amp_advisor_started)
        self.worker.amp_advisor_finished.connect(self._on_amp_advisor_finished)
        self.worker.amp_advisor_failed.connect(self._on_amp_advisor_failed)
        self.worker.warning.connect(self._show_warning)

        self.worker_thread.finished.connect(self.worker.deleteLater)
        self.worker_thread.start()

    def _emit_if_user(self, key: str, value: object) -> None:
        if not self._updating_from_device:
            self.set_requested.emit(key, value)

    @Slot(dict)
    def _on_connected(self, cfg: dict) -> None:
        self.refresh_button.setEnabled(True)
        self.center_button.setEnabled(True)
        self.sweep_button.setEnabled(True)
        self.amp_advise_button.setEnabled(True)
        self.advise_button.setEnabled(True)
        self.connect_button.setEnabled(False)
        self.host_edit.setEnabled(False)
        self.serial_edit.setEnabled(False)
        self._set_amplitude_output_manual_mode(not self.amp_enable.isChecked())
        self.statusbar.showMessage(
            f"Connected to {cfg.get('serial', 'MFLI')} — live updates active"
        )

    @Slot()
    def _on_advisor_started(self) -> None:
        self._set_advisor_buttons_busy("phase")

    @Slot(dict)
    def _on_advisor_finished(self, result: dict) -> None:
        self._set_advisor_buttons_idle()

        bw = float(result.get("achieved_bw_hz", float("nan")))
        pm = float(result.get("phase_margin_deg", float("nan")))
        p = float(result.get("applied_p", float("nan")))
        i = float(result.get("applied_i", float("nan")))

        details = f"PLL1 Advisor applied P={p:.6g}, I={i:.6g}"
        if bw == bw:  # NaN-safe check
            details += f", BW={bw:.6g} Hz"
        if pm == pm:
            details += f", PM={pm:.3g} deg"
        self.statusbar.showMessage(details, 15000)

    @Slot(str)
    def _on_advisor_failed(self, message: str) -> None:
        self._set_advisor_buttons_idle()
        self.statusbar.showMessage(message, 15000)

    @Slot()
    def _on_amp_advisor_started(self) -> None:
        self._set_advisor_buttons_busy("amplitude")

    @Slot(dict)
    def _on_amp_advisor_finished(self, result: dict) -> None:
        self._set_advisor_buttons_idle()

        bw = float(result.get("achieved_bw_hz", float("nan")))
        pm = float(result.get("phase_margin_deg", float("nan")))
        p = float(result.get("applied_p", float("nan")))
        i = float(result.get("applied_i", float("nan")))

        details = f"PID3 Amplitude Advisor applied P={p:.6g}, I={i:.6g}"
        if bw == bw:
            details += f", BW={bw:.6g} Hz"
        if pm == pm:
            details += f", PM={pm:.3g} deg"
        self.statusbar.showMessage(details, 15000)

    @Slot(str)
    def _on_amp_advisor_failed(self, message: str) -> None:
        self._set_advisor_buttons_idle()
        self.statusbar.showMessage(message, 15000)

    @Slot(str)
    def _on_connection_failed(self, message: str) -> None:
        self.statusbar.showMessage(f"Connection failed: {message}")

    @Slot(str)
    def _show_warning(self, message: str) -> None:
        self.statusbar.showMessage(message.replace("\n", " | "), 12000)

    @Slot(dict)
    def _apply_settings(self, s: dict) -> None:
        self._updating_from_device = True
        try:
            self.signal_output_enable.setChecked(bool(s["signal_output_on"]))
            self.phase_enable.setChecked(bool(s["phase_enable"]))
            self.phase_setpoint.setValue(float(s["phase_setpoint_deg"]))
            self.phase_p.setValue(float(s["phase_p"]))
            self.phase_i.setValue(float(s["phase_i"]))
            self.phase_center.setValue(float(s["phase_center_hz"]))
            if self.sweep_window is not None:
                self.sweep_window.update_center(float(s["phase_center_hz"]))
            self.phase_lower.setValue(float(s["phase_lower_hz"]))
            self.phase_upper.setValue(float(s["phase_upper_hz"]))

            amp_enabled = bool(s["amp_enable"])
            self.amp_enable.setChecked(amp_enabled)
            self._set_amplitude_output_manual_mode(not amp_enabled)
            if not amp_enabled:
                self.amp_value_label.setValue(float(s["manual_output_amplitude_v"]))

            self._update_amp_setpoint_slider_range(float(s["input_range_v"]))
            self.amp_setpoint.setValue(float(s["amp_setpoint_v"]))
            self._sync_amp_setpoint_slider(float(s["amp_setpoint_v"]))
            self.amp_p.setValue(float(s["amp_p"]))
            self.amp_i.setValue(float(s["amp_i"]))
            self.amp_center.setValue(float(s["amp_center_v"]))
            self.amp_lower.setValue(float(s["amp_lower_v"]))
            self.amp_upper.setValue(float(s["amp_upper_v"]))

            actual_min_v = float(s["amp_center_v"]) + float(s["amp_lower_v"])
            actual_max_v = float(s["amp_center_v"]) + float(s["amp_upper_v"])
            self.amp_actual_min_label.setValue(actual_min_v)
            self.amp_actual_max_label.setValue(actual_max_v)
        finally:
            self._updating_from_device = False

    @Slot(dict)
    def _apply_live(self, d: dict) -> None:
        if "phase_error" in d:
            phase_error = float(d["phase_error"])
            self.phase_error_label.setValue(phase_error)
            # MFLI defines Error = Setpoint - Input.
            self.phase_measured.setValue(self.phase_setpoint.value() - phase_error)
        if "phase_shift" in d:
            phase_shift = float(d["phase_shift"])
            self.phase_shift_label.setValue(phase_shift)
            if self.sweep_window is not None:
                self.sweep_window.update_current_shift(phase_shift)
        if "phase_value" in d:
            self.phase_value_label.setValue(float(d["phase_value"]))
        if "phase_locked" in d:
            self.phase_lock_label.setText("LOCKED" if d["phase_locked"] else "UNLOCKED")

        if "amp_error" in d:
            amp_error = float(d["amp_error"])
            self.amp_error_label.setValue(amp_error)
            self.amp_measured.setValue(self.amp_setpoint.value() - amp_error)
        if "amp_shift" in d:
            self.amp_shift_label.setValue(float(d["amp_shift"]))
        if "amp_value" in d and self.amp_enable.isChecked():
            self.amp_value_label.setValue(float(d["amp_value"]))

    @Slot()
    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self._save_connection_settings()
        self.shutdown_requested.emit()
        self.worker_thread.quit()
        self.worker_thread.wait(1500)


def main() -> int:
    app = QApplication(sys.argv)
    QApplication.styleHints().setColorScheme(Qt.ColorScheme.Light)

    ui_path = Path(__file__).resolve().with_name(UI_FILENAME)
    controller = OscillationControlApp(ui_path)
    app.aboutToQuit.connect(controller.shutdown)

    controller.window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
