from __future__ import annotations

import sys
import time
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
)

from nanonis_spinbox import NanonisSpinBox, format_eng_number

from zhinst.toolkit import Session


PHASE_PID_INDEX = 0      # LabOne GUI "PID / PLL 1"
AMPLITUDE_PID_INDEX = 2  # LabOne GUI "PID / PLL 3"
UI_FILENAME = "mfli_oscillation_control_v0.ui"

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
    warning = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.session: Session | None = None
        self.device = None
        self.phase = None
        self.amplitude = None

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
            self.session = Session(host)
            self.device = self.session.connect_device(serial)

            self.phase = self.device.pids[PHASE_PID_INDEX]
            self.amplitude = self.device.pids[AMPLITUDE_PID_INDEX]

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
            if key == "phase_enable":
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
    set_requested = Signal(str, object)
    shutdown_requested = Signal()

    def __init__(self, ui_path: Path) -> None:
        super().__init__()
        self.window = load_ui(ui_path)
        self._updating_from_device = False
        self._shutting_down = False

        self._bind_widgets()
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

        # PLL 1
        self.phase_enable = self.widget(QCheckBox, "phaseEnable")
        self.phase_setpoint = self.widget(NanonisSpinBox, "phaseSetpoint")
        self.phase_p = self.widget(NanonisSpinBox, "phaseP")
        self.phase_i = self.widget(NanonisSpinBox, "phaseI")
        self.phase_center = self.widget(NanonisSpinBox, "phaseCenter")
        self.phase_lower = self.widget(NanonisSpinBox, "phaseLower")
        self.phase_upper = self.widget(NanonisSpinBox, "phaseUpper")
        self.phase_error_label = self.widget(QLabel, "phaseErrorValue")
        self.phase_shift_label = self.widget(QLabel, "phaseShiftValue")
        self.phase_value_label = self.widget(QLabel, "phaseValueValue")
        self.phase_lock_label = self.widget(QLabel, "phaseLockValue")

        # PID 3
        self.amp_enable = self.widget(QCheckBox, "ampEnable")
        self.amp_setpoint = self.widget(NanonisSpinBox, "ampSetpoint")
        self.amp_p = self.widget(NanonisSpinBox, "ampP")
        self.amp_i = self.widget(NanonisSpinBox, "ampI")
        self.amp_center = self.widget(NanonisSpinBox, "ampCenter")
        self.amp_lower = self.widget(NanonisSpinBox, "ampLower")
        self.amp_upper = self.widget(NanonisSpinBox, "ampUpper")
        self.amp_error_label = self.widget(QLabel, "ampErrorValue")
        self.amp_shift_label = self.widget(QLabel, "ampShiftValue")
        self.amp_value_label = self.widget(QLabel, "ampValueValue")
        self.amp_actual_min_label = self.widget(QLabel, "ampActualMinValue")
        self.amp_actual_max_label = self.widget(QLabel, "ampActualMaxValue")

    def _configure_nanonis_spinboxes(self) -> None:
        """
        Nanonis-style fields: editor text is number + SI prefix only.

        Base units are fixed metadata and are shown in the adjacent labels.
        NanonisSpinBox.value() always returns the value in the base unit.
        """
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

    def _connect_gui_signals(self) -> None:
        self.connect_button.clicked.connect(
            lambda: self.connect_requested.emit(
                self.host_edit.text(), self.serial_edit.text()
            )
        )
        self.refresh_button.clicked.connect(self.refresh_requested.emit)

        self.phase_enable.toggled.connect(
            lambda v: self._emit_if_user("phase_enable", v)
        )
        self.phase_setpoint.editingFinished.connect(
            lambda: self._emit_if_user("phase_setpoint_deg", self.phase_setpoint.value())
        )
        self.phase_p.editingFinished.connect(
            lambda: self._emit_if_user("phase_p", self.phase_p.value())
        )
        self.phase_i.editingFinished.connect(
            lambda: self._emit_if_user("phase_i", self.phase_i.value())
        )
        self.phase_center.editingFinished.connect(
            lambda: self._emit_if_user("phase_center_hz", self.phase_center.value())
        )
        self.phase_lower.editingFinished.connect(
            lambda: self._emit_if_user("phase_lower_hz", self.phase_lower.value())
        )
        self.phase_upper.editingFinished.connect(
            lambda: self._emit_if_user("phase_upper_hz", self.phase_upper.value())
        )

        self.amp_enable.toggled.connect(
            lambda v: self._emit_if_user("amp_enable", v)
        )
        self.amp_setpoint.editingFinished.connect(
            lambda: self._emit_if_user("amp_setpoint_v", self.amp_setpoint.value())
        )
        self.amp_p.editingFinished.connect(
            lambda: self._emit_if_user("amp_p", self.amp_p.value())
        )
        self.amp_i.editingFinished.connect(
            lambda: self._emit_if_user("amp_i", self.amp_i.value())
        )
        self.amp_center.editingFinished.connect(
            lambda: self._emit_if_user("amp_center_v", self.amp_center.value())
        )
        self.amp_lower.editingFinished.connect(
            lambda: self._emit_if_user("amp_lower_v", self.amp_lower.value())
        )
        self.amp_upper.editingFinished.connect(
            lambda: self._emit_if_user("amp_upper_v", self.amp_upper.value())
        )

    def _build_worker(self) -> None:
        self.worker_thread = QThread(self)
        self.worker = MFLIWorker()
        self.worker.moveToThread(self.worker_thread)

        self.connect_requested.connect(self.worker.connect_instrument)
        self.refresh_requested.connect(self.worker.refresh_settings)
        self.set_requested.connect(self.worker.set_parameter)
        self.shutdown_requested.connect(self.worker.shutdown)

        self.worker.connected.connect(self._on_connected)
        self.worker.connection_failed.connect(self._on_connection_failed)
        self.worker.settings_updated.connect(self._apply_settings)
        self.worker.live_updated.connect(self._apply_live)
        self.worker.warning.connect(self._show_warning)

        self.worker_thread.finished.connect(self.worker.deleteLater)
        self.worker_thread.start()

    def _emit_if_user(self, key: str, value: object) -> None:
        if not self._updating_from_device:
            self.set_requested.emit(key, value)

    @Slot(dict)
    def _on_connected(self, cfg: dict) -> None:
        self.refresh_button.setEnabled(True)
        self.connect_button.setEnabled(False)
        self.host_edit.setEnabled(False)
        self.serial_edit.setEnabled(False)
        self.statusbar.showMessage(
            f"Connected to {cfg.get('serial', 'MFLI')} — live updates active"
        )

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
            self.amp_actual_min_label.setText(format_eng_number(actual_min_v))
            self.amp_actual_max_label.setText(format_eng_number(actual_max_v))
        finally:
            self._updating_from_device = False

    @Slot(dict)
    def _apply_live(self, d: dict) -> None:
        if "phase_error" in d:
            self.phase_error_label.setText(
                format_eng_number(float(d["phase_error"]), show_plus=True)
            )
        if "phase_shift" in d:
            self.phase_shift_label.setText(
                format_eng_number(float(d["phase_shift"]), show_plus=True)
            )
        if "phase_value" in d:
            self.phase_value_label.setText(
                format_eng_number(float(d["phase_value"]))
            )
        if "phase_locked" in d:
            self.phase_lock_label.setText("LOCKED" if d["phase_locked"] else "UNLOCKED")

        if "amp_error" in d:
            self.amp_error_label.setText(
                format_eng_number(float(d["amp_error"]), show_plus=True)
            )
        if "amp_shift" in d:
            self.amp_shift_label.setText(
                format_eng_number(float(d["amp_shift"]), show_plus=True)
            )
        if "amp_value" in d:
            self.amp_value_label.setText(
                format_eng_number(float(d["amp_value"]))
            )

    @Slot()
    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self.shutdown_requested.emit()
        self.worker_thread.quit()
        self.worker_thread.wait(1500)


def main() -> int:
    app = QApplication(sys.argv)

    ui_path = Path(__file__).resolve().with_name(UI_FILENAME)
    controller = OscillationControlApp(ui_path)
    app.aboutToQuit.connect(controller.shutdown)

    controller.window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
