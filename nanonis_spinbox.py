from __future__ import annotations

import math
import re

from PySide6.QtCore import Property, QPointF, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF, QValidator
from PySide6.QtWidgets import QAbstractSpinBox, QDoubleSpinBox, QSlider, QStyle, QWidget


_PREFIX_TO_EXPONENT = {
    "y": -24,
    "z": -21,
    "a": -18,
    "f": -15,
    "p": -12,
    "n": -9,
    "u": -6,
    "µ": -6,
    "μ": -6,
    "m": -3,
    "": 0,
    "k": 3,
    "K": 3,
    "M": 6,
    "G": 9,
    "T": 12,
    "P": 15,
    "E": 18,
    "Z": 21,
    "Y": 24,
}

_EXPONENT_TO_PREFIX = {
    -24: "y",
    -21: "z",
    -18: "a",
    -15: "f",
    -12: "p",
    -9: "n",
    -6: "µ",
    -3: "m",
    0: "",
    3: "k",
    6: "M",
    9: "G",
    12: "T",
    15: "P",
    18: "E",
    21: "Z",
    24: "Y",
}

# Runtime text is strictly:
#       signed-number [optional SI-prefix]
#
# The base unit never appears in the editor. It belongs in the adjacent label.
_NUMBER_PREFIX_RE = re.compile(
    r"^\s*"
    r"([+-]?(?:(?:\d+(?:[.,]\d*)?)|(?:[.,]\d+))(?:[eE][+-]?\d+)?)"
    r"\s*"
    r"([yzafpnumµμkKMGTPEZY]?)"
    r"\s*$"
)


def _engineering_exponent(value: float, display_decimals: int = 6) -> int:
    if value == 0.0 or not math.isfinite(value):
        return 0

    exponent = int(math.floor(math.log10(abs(value)) / 3.0) * 3)
    exponent = max(-24, min(24, exponent))

    scaled = value / (10.0**exponent)
    if abs(round(scaled, display_decimals)) >= 1000.0 and exponent < 24:
        exponent += 3

    return exponent


def format_eng_number(
    value: float,
    decimals: int = 6,
    show_plus: bool = False,
) -> str:
    """
    Format in Nanonis-style engineering notation: number + SI prefix only.

    Examples:
        974454.238 -> "974.454238k"
        0.060      -> "60.000000m"
        2.46e-3    -> "2.460000m"

    No base-unit text is appended.
    """
    if not math.isfinite(value):
        return str(value)

    exponent = _engineering_exponent(value, decimals)
    prefix = _EXPONENT_TO_PREFIX[exponent]
    scaled = value / (10.0**exponent)

    sign = "+" if show_plus else ""
    return f"{scaled:{sign}.{decimals}f}{prefix}"


class NanonisSpinBox(QDoubleSpinBox):
    """
    Nanonis-style numeric field.

    Runtime appearance/input:
        974.454238k
        60.000000m
        2.500000

    The BASE UNIT is never displayed or accepted inside the field. Put it in
    the field label, e.g. "Center Freq. (Hz)" or "Amplitude Setpoint (V)".

    Input examples, for any base unit:
        2.5k   -> 2500
        60m    -> 0.060
        300u   -> 300e-6
        4M     -> 4e6

    Cursor stepping:
        place the text cursor immediately BEFORE a digit and use Up/Down or
        the mouse wheel; that digit's decimal place is incremented/decremented.

    value() / setValue() always use the base-unit value.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._display_decimals = 6
        self._base_unit = ""

        self.setKeyboardTracking(False)
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    # Metadata only. It never becomes part of the editor text.
    def getBaseUnit(self) -> str:
        return self._base_unit

    def setBaseUnit(self, unit: str) -> None:
        self._base_unit = str(unit)
        if self._base_unit:
            self.setToolTip(
                f"Base unit: {self._base_unit}. Enter SI prefixes only "
                f"(for example m, k, M)."
            )

    baseUnit = Property(str, getBaseUnit, setBaseUnit)

    def getDisplayDecimals(self) -> int:
        return self._display_decimals

    def setDisplayDecimals(self, decimals: int) -> None:
        self._display_decimals = max(0, min(15, int(decimals)))
        self.lineEdit().setText(self.textFromValue(self.value()))

    displayDecimals = Property(int, getDisplayDecimals, setDisplayDecimals)

    # ---- QDoubleSpinBox virtuals -----------------------------------------

    def textFromValue(self, value: float) -> str:
        return format_eng_number(
            float(value),
            decimals=self._display_decimals,
        )

    def valueFromText(self, text: str) -> float:
        parsed = self._parse_text(text)
        if parsed is None:
            return self.value()
        return parsed

    def validate(self, text: str, pos: int):
        stripped = text.strip()

        if stripped in {"", "+", "-", ".", ",", "+.", "-.", "+,", "-,"}:
            return (QValidator.State.Intermediate, text, pos)

        parsed = self._parse_text(text)
        if parsed is not None:
            if self.minimum() <= parsed <= self.maximum():
                return (QValidator.State.Acceptable, text, pos)
            return (QValidator.State.Intermediate, text, pos)

        # Incomplete scientific notation while typing, e.g. "1e-".
        if re.match(
            r"^\s*[+-]?(?:(?:\d+(?:[.,]\d*)?)|(?:[.,]\d+))[eE][+-]?\s*$",
            text,
        ):
            return (QValidator.State.Intermediate, text, pos)

        return (QValidator.State.Invalid, text, pos)

    def stepBy(self, steps: int) -> None:
        editor = self.lineEdit()
        text = editor.text()
        cursor = editor.cursorPosition()

        match = _NUMBER_PREFIX_RE.match(text)
        if match is None:
            super().stepBy(steps)
            return

        number_start, number_end = match.span(1)
        number_token = match.group(1)

        # Digit immediately at/right of the cursor: cursor is "in front of"
        # the digit, matching the Nanonis interaction model.
        digit_pos = None
        for p in range(max(cursor, number_start), number_end):
            if text[p].isdigit():
                digit_pos = p
                break

        # If the cursor is at the end/prefix, use the nearest numeric digit.
        if digit_pos is None:
            for p in range(min(cursor - 1, number_end - 1), number_start - 1, -1):
                if text[p].isdigit():
                    digit_pos = p
                    break

        if digit_pos is None:
            super().stepBy(steps)
            return

        local_index = digit_pos - number_start
        sign_len = 1 if number_token.startswith(("+", "-")) else 0
        core = number_token[sign_len:]
        core_index = local_index - sign_len

        decimal_index = core.find(".")
        if decimal_index < 0:
            decimal_index = core.find(",")
        if decimal_index < 0:
            decimal_index = len(core)

        if core_index < decimal_index:
            digit_exponent = decimal_index - core_index - 1
        else:
            digit_exponent = -(core_index - decimal_index)

        prefix = match.group(2)
        prefix_exponent = _PREFIX_TO_EXPONENT.get(prefix, 0)

        increment = 10.0 ** (digit_exponent + prefix_exponent)

        current = self._parse_text(text)
        if current is None:
            current = self.value()

        new_value = current + float(steps) * increment
        new_value = min(self.maximum(), max(self.minimum(), new_value))
        self.setValue(new_value)

        # Preserve approximately the same cursor location after reformatting.
        old_cursor = cursor
        QTimer.singleShot(
            0,
            lambda: self.lineEdit().setCursorPosition(
                min(old_cursor, len(self.lineEdit().text()))
            ),
        )

    def _parse_text(self, text: str) -> float | None:
        text = text.replace("\N{MINUS SIGN}", "-")
        match = _NUMBER_PREFIX_RE.match(text)
        if match is None:
            return None

        number_text = match.group(1).replace(",", ".")
        prefix = match.group(2)

        try:
            number = float(number_text)
        except ValueError:
            return None

        exponent = _PREFIX_TO_EXPONENT.get(prefix)
        if exponent is None:
            return None

        value = number * (10.0**exponent)
        if not math.isfinite(value):
            return None

        return value



class NanonisSlider(QSlider):
    """Compact Nanonis-style horizontal slider with an engineering scale.

    The control paints a thin groove, a downward-pointing cyan handle, minor
    ticks and five labelled major ticks.  The major labels are derived from a
    *physical* scale range supplied by ``setScaleRange()``; the QSlider value
    itself can remain a high-resolution integer mapping used by the caller.

    Only the two end labels/ticks react to a double-click.  Intermediate scale
    labels are paint-only, so they cannot be edited accidentally.
    """

    minimumDoubleClicked = Signal()
    maximumDoubleClicked = Signal()

    _TICK_COUNT = 20
    _MAJOR_EVERY = 5

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._scale_minimum = 0.0
        self._scale_maximum = 1.0
        self.setMinimumHeight(43)
        self.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.setTracking(True)
        self.setMouseTracking(True)
        self._dragging_handle = False

    def setScaleRange(self, minimum: float, maximum: float) -> None:
        """Set values represented by the left/right scale labels."""
        minimum = float(minimum)
        maximum = float(maximum)
        if not math.isfinite(minimum):
            minimum = 0.0
        if not math.isfinite(maximum) or maximum <= minimum:
            maximum = minimum + 1.0
        self._scale_minimum = minimum
        self._scale_maximum = maximum
        self.update()

    def scaleRange(self) -> tuple[float, float]:
        return self._scale_minimum, self._scale_maximum

    @staticmethod
    def _compact_scale_text(value: float) -> str:
        text = format_eng_number(float(value), decimals=3)
        prefix = text[-1] if text and (text[-1].isalpha() or text[-1] in "µμ") else ""
        number = text[:-1] if prefix else text
        number = number.rstrip("0").rstrip(".")
        if number in {"", "+", "-"}:
            number += "0"
        return number + prefix

    def _geometry(self) -> tuple[float, float, float, float, float]:
        # A small horizontal inset keeps both the handle and endpoint labels
        # from clipping. The label band begins beneath the ticks.
        left = 8.0
        right = max(left + 1.0, float(self.width()) - 8.0)
        groove_y = 8.5
        tick_top = groove_y + 3.0
        label_top = tick_top + 7.0
        return left, right, groove_y, tick_top, label_top

    def _endpoint_hit(self, x: float, y: float) -> int:
        """Return -1 for left endpoint, +1 for right endpoint, else 0."""
        left, right, groove_y, _tick_top, label_top = self._geometry()
        # Restrict edit affordance to the endpoint marker/label region. This
        # deliberately excludes the groove and all intermediate labels.
        if y < groove_y + 7.0:
            return 0
        hit_width = 42.0
        if x <= left + hit_width:
            return -1
        if x >= right - hit_width:
            return +1
        return 0

    def _slider_x(self) -> float:
        """Return the painted handle x coordinate."""
        left, right, _groove_y, _tick_top, _label_top = self._geometry()
        available = max(1, int(round(right - left)))
        pos = QStyle.sliderPositionFromValue(
            self.minimum(),
            self.maximum(),
            self.sliderPosition(),
            available,
            self.invertedAppearance(),
        )
        return left + float(pos)

    def _handle_hit(self, x: float, y: float) -> bool:
        """Generous hit box around the custom painted arrow handle."""
        _left, _right, groove_y, _tick_top, _label_top = self._geometry()
        handle_x = self._slider_x()
        return abs(x - handle_x) <= 9.0 and -2.0 <= y <= groove_y + 4.0

    def _set_position_from_mouse_x(self, x: float) -> None:
        """Map a mouse x position directly onto the slider value."""
        left, right, _groove_y, _tick_top, _label_top = self._geometry()
        available = max(1, int(round(right - left)))
        pixel = int(round(max(0.0, min(right - left, x - left))))
        value = QStyle.sliderValueFromPosition(
            self.minimum(),
            self.maximum(),
            pixel,
            available,
            self.invertedAppearance(),
        )
        self.setSliderPosition(value)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            x = event.position().x()
            y = event.position().y()

            # The lower label band is reserved for double-clicking the two
            # endpoint markers; do not let a normal press there jump the slider.
            if self._endpoint_hit(x, y):
                event.accept()
                return

            # The handle is custom-painted, so Qt's native style does not know
            # where it is. Drag it explicitly while preserving its appearance.
            if self._handle_hit(x, y):
                self._dragging_handle = True
                self.setSliderDown(True)
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                event.accept()
                return

        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._dragging_handle and event.button() == Qt.MouseButton.LeftButton:
            self._dragging_handle = False
            self.setSliderDown(False)
            x = event.position().x()
            y = event.position().y()
            self.setCursor(
                Qt.CursorShape.OpenHandCursor
                if self._handle_hit(x, y)
                else Qt.CursorShape.ArrowCursor
            )
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        hit = self._endpoint_hit(event.position().x(), event.position().y())
        if hit < 0:
            self.minimumDoubleClicked.emit()
            event.accept()
            return
        if hit > 0:
            self.maximumDoubleClicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event) -> None:
        x = event.position().x()
        y = event.position().y()

        if self._dragging_handle:
            self._set_position_from_mouse_x(x)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return

        endpoint_hit = self._endpoint_hit(x, y)
        handle_hit = self._handle_hit(x, y)

        if endpoint_hit:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        elif handle_hit:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self.setCursor(Qt.CursorShape.ArrowCursor)
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        if self.orientation() != Qt.Orientation.Horizontal:
            super().paintEvent(event)
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        left, right, groove_y, tick_top, label_top = self._geometry()
        width = right - left

        # Nanonis-like thin horizontal track.
        painter.setPen(QPen(QColor(92, 92, 92), 1.0))
        painter.drawLine(QPointF(left, groove_y), QPointF(right, groove_y))

        # 20 divisions with five major positions: min, 1/4, 1/2, 3/4, max.
        for index in range(self._TICK_COUNT + 1):
            fraction = index / self._TICK_COUNT
            x = left + fraction * width
            major = (index % self._MAJOR_EVERY == 0)
            tick_length = 6.0 if major else 3.0
            painter.setPen(QPen(QColor(72, 72, 72), 1.0))
            painter.drawLine(
                QPointF(x, tick_top),
                QPointF(x, tick_top + tick_length),
            )

        # Major labels are centered under their tick, except at endpoints where
        # they are aligned inward so text is never clipped. Only the endpoint
        # positions are interactive; these strings themselves are paint-only.
        painter.setPen(QColor(68, 68, 68))
        font = painter.font()
        font.setPointSizeF(max(6.5, font.pointSizeF() - 1.0))
        painter.setFont(font)
        label_height = 14.0
        scale_span = self._scale_maximum - self._scale_minimum

        major_indices = range(0, self._TICK_COUNT + 1, self._MAJOR_EVERY)
        for index in major_indices:
            fraction = index / self._TICK_COUNT
            x = left + fraction * width
            value = self._scale_minimum + fraction * scale_span
            label = self._compact_scale_text(value)

            if index == 0:
                rect = QRectF(left, label_top, 62.0, label_height)
                alignment = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            elif index == self._TICK_COUNT:
                rect = QRectF(right - 62.0, label_top, 62.0, label_height)
                alignment = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            else:
                rect = QRectF(x - 35.0, label_top, 70.0, label_height)
                alignment = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter

            painter.drawText(rect, alignment, label)

        # Map the slider position onto our custom groove.
        x = self._slider_x()

        # Cyan arrow/hand marker. The tip lands on the groove.
        handle = QPolygonF(
            [
                QPointF(x - 5.5, 0.5),
                QPointF(x + 5.5, 0.5),
                QPointF(x + 5.5, 4.3),
                QPointF(x + 2.7, 4.3),
                QPointF(x, groove_y - 0.4),
                QPointF(x - 2.7, 4.3),
                QPointF(x - 5.5, 4.3),
            ]
        )
        painter.setPen(QPen(QColor(70, 105, 120), 1.0))
        painter.setBrush(QColor(70, 175, 215))
        painter.drawPolygon(handle)

        painter.end()

