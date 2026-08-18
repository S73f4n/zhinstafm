from __future__ import annotations

import sys
import time

import yaml
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
    QStatusBar,
    QWidget,
    QStyleFactory,
)

from nanonis_spinbox import NanonisSpinBox

from zhinst.toolkit import Session


PHASE_PID_INDEX = 0      # LabOne GUI "PID / PLL 1"
AMPLITUDE_PID_INDEX = 2  # LabOne GUI "PID / PLL 3"
UI_FILENAME = "mfli_oscillation_control_v0.ui"
CONNECTION_CONFIG_FILENAME = "mfli_connection.yaml"

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
            "signal_output_on": int(self.signal_output.on()),
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
    set_requested = Signal(str, object)
    shutdown_requested = Signal()

    def __init__(self, ui_path: Path) -> None:
        super().__init__()
        self.window = load_ui(ui_path)
        self.config_path = ui_path.with_name(CONNECTION_CONFIG_FILENAME)
        self._updating_from_device = False
        self._shutting_down = False

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
        self.phase_center = self.widget(NanonisSpinBox, "phaseCenter")
        self.phase_lower = self.widget(NanonisSpinBox, "phaseLower")
        self.phase_upper = self.widget(NanonisSpinBox, "phaseUpper")
        self.phase_shift_label = self.widget(NanonisSpinBox, "phaseShiftValue")
        self.phase_value_label = self.widget(NanonisSpinBox, "phaseValueValue")

        # Signal / setpoint area
        self.amp_setpoint = self.widget(NanonisSpinBox, "ampSetpoint")
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
            (self.amp_value_label, "Vpk"),
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

    def _connect_gui_signals(self) -> None:
        self.connect_button.clicked.connect(self._connect_with_saved_settings)
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.center_button.clicked.connect(self.center_requested.emit)
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

        self.amp_enable.toggled.connect(
            lambda v: self._emit_if_user("amp_enable", v)
        )
        self.amp_setpoint.valueChanged.connect(
            lambda v: self._emit_if_user("amp_setpoint_v", v)
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
        self.amp_advise_button.setEnabled(True)
        self.advise_button.setEnabled(True)
        self.connect_button.setEnabled(False)
        self.host_edit.setEnabled(False)
        self.serial_edit.setEnabled(False)
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
            self.phase_lower.setValue(float(s["phase_lower_hz"]))
            self.phase_upper.setValue(float(s["phase_upper_hz"]))

            self.amp_enable.setChecked(bool(s["amp_enable"]))
            self.amp_setpoint.setValue(float(s["amp_setpoint_v"]))
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
            self.phase_shift_label.setValue(float(d["phase_shift"]))
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
        if "amp_value" in d:
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
    app.setStyle("Fusion")
    QApplication.styleHints().setColorScheme(Qt.ColorScheme.Light)

    ui_path = Path(__file__).resolve().with_name(UI_FILENAME)
    controller = OscillationControlApp(ui_path)
    app.aboutToQuit.connect(controller.shutdown)

    controller.window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
