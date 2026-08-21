from __future__ import annotations

import math
import re

from PySide6.QtCore import QEvent, Property, QPointF, QRectF, QSize, QTimer, Qt, Signal, QVariantAnimation
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF, QValidator
from PySide6.QtWidgets import QAbstractButton, QAbstractSpinBox, QDoubleSpinBox, QLineEdit, QSlider, QStyle, QWidget


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


def parse_eng_number(text: str) -> float | None:
    """Parse a number with an optional SI prefix, returning base-unit value."""
    text = str(text).replace("\N{MINUS SIGN}", "-")
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
        return parse_eng_number(text)



class NanonisSwitch(QAbstractButton):
    """Compact GTK/Nanonis-like toggle implemented entirely with Qt.

    It is a normal checkable QAbstractButton, so callers can use the standard
    Qt API and signals:
        setChecked(bool)
        isChecked()
        toggled(bool)

    The switch is custom-painted; no third-party widget library is required.
    """

    _TRACK_W = 42.0
    _TRACK_H = 20.0
    _THUMB_D = 16.0
    _GAP = 7.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(22)

        self._thumb_position = 1.0 if self.isChecked() else 0.0
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(110)
        self._animation.valueChanged.connect(self._on_animation_value)
        self.toggled.connect(self._animate_to_state)

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        caption_w = fm.horizontalAdvance(self.text()) if self.text() else 0
        width = int(self._TRACK_W + (self._GAP + caption_w if caption_w else 0) + 4)
        height = max(22, fm.height() + 4)
        return QSize(width, height)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def _on_animation_value(self, value) -> None:
        self._thumb_position = float(value)
        self.update()

    def _animate_to_state(self, checked: bool) -> None:
        self._animation.stop()
        self._animation.setStartValue(float(self._thumb_position))
        self._animation.setEndValue(1.0 if checked else 0.0)
        self._animation.start()

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        track_y = (self.height() - self._TRACK_H) / 2.0
        track = QRectF(1.0, track_y, self._TRACK_W, self._TRACK_H)

        enabled = self.isEnabled()
        if self.isChecked():
            fill = QColor(79, 190, 84) if enabled else QColor(165, 205, 167)
            border = QColor(61, 157, 67)
        else:
            fill = QColor(190, 190, 190) if enabled else QColor(215, 215, 215)
            border = QColor(150, 150, 150)

        painter.setPen(QPen(border, 1.0))
        painter.setBrush(fill)
        painter.drawRoundedRect(track, self._TRACK_H / 2.0, self._TRACK_H / 2.0)

        travel = self._TRACK_W - self._THUMB_D - 4.0
        thumb_x = track.left() + 2.0 + travel * self._thumb_position
        thumb_y = track.top() + 2.0
        thumb = QRectF(thumb_x, thumb_y, self._THUMB_D, self._THUMB_D)

        painter.setPen(QPen(QColor(130, 130, 130), 0.8))
        painter.setBrush(QColor(250, 250, 250) if enabled else QColor(238, 238, 238))
        painter.drawEllipse(thumb)

        # Subtle focus indication without changing switch geometry.
        if self.hasFocus():
            focus = track.adjusted(-2.0, -2.0, 2.0, 2.0)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(70, 135, 210), 1.0))
            painter.drawRoundedRect(focus, self._TRACK_H / 2.0 + 2.0, self._TRACK_H / 2.0 + 2.0)

        if self.text():
            text_rect = QRectF(
                track.right() + self._GAP,
                0.0,
                max(0.0, self.width() - track.right() - self._GAP),
                float(self.height()),
            )
            text_color = self.palette().text().color()
            if not enabled:
                text_color = self.palette().mid().color()
            painter.setPen(text_color)
            painter.drawText(
                text_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                self.text(),
            )


class NanonisLockButton(QAbstractButton):
    """Small checkable lock icon for linked lower/upper range controls.

    checked   -> closed lock / symmetric linked range
    unchecked -> open lock / independent endpoints
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Link lower and upper limits symmetrically about zero.")
        self.setFixedSize(26, 34)

    def sizeHint(self) -> QSize:
        return QSize(26, 34)

    def minimumSizeHint(self) -> QSize:
        return QSize(26, 34)

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        color = (
            self.palette().highlight().color()
            if self.isChecked()
            else self.palette().mid().color()
        )
        if not self.isEnabled():
            color.setAlpha(120)

        cx = self.width() / 2.0
        body = QRectF(cx - 7.0, 16.0, 14.0, 11.0)

        painter.setPen(QPen(color, 1.4))
        painter.setBrush(color)
        painter.drawRoundedRect(body, 2.0, 2.0)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(color, 1.8))
        if self.isChecked():
            arc = QRectF(cx - 5.0, 7.0, 10.0, 12.0)
            painter.drawArc(arc, 0, 180 * 16)
            painter.drawLine(QPointF(cx - 5.0, 13.0), QPointF(cx - 5.0, 17.0))
            painter.drawLine(QPointF(cx + 5.0, 13.0), QPointF(cx + 5.0, 17.0))
        else:
            arc = QRectF(cx - 1.0, 7.0, 10.0, 12.0)
            painter.drawArc(arc, 0, 180 * 16)
            painter.drawLine(QPointF(cx + 9.0, 13.0), QPointF(cx + 9.0, 17.0))
            painter.drawLine(QPointF(cx - 1.0, 13.0), QPointF(cx - 1.0, 14.5))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.palette().base().color())
        painter.drawEllipse(QRectF(cx - 1.2, 19.0, 2.4, 2.4))
        painter.drawRect(QRectF(cx - 0.7, 21.0, 1.4, 3.0))

        if self.hasFocus():
            focus = self.palette().highlight().color()
            focus.setAlpha(150)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(focus, 1.0, Qt.PenStyle.DotLine))
            painter.drawRoundedRect(
                QRectF(1.5, 2.5, self.width() - 3.0, self.height() - 5.0),
                3.0,
                3.0,
            )


class NanonisSlider(QSlider):
    """Compact Nanonis-style horizontal slider with an engineering scale.

    The control paints a thin groove, a downward-pointing cyan handle, minor
    ticks and five labelled major ticks.  The major labels are derived from a
    *physical* scale range supplied by ``setScaleRange()``; the QSlider value
    itself can remain a high-resolution integer mapping used by the caller.

    Only the two end labels/ticks react to a double-click.  Intermediate scale
    labels are paint-only, so they cannot be edited accidentally.
    """

    # Legacy double-click signals are retained for compatibility, but the
    # current widget edits endpoint labels inline instead of opening dialogs.
    minimumDoubleClicked = Signal()
    maximumDoubleClicked = Signal()

    minimumEditCommitted = Signal(float)
    maximumEditCommitted = Signal(float)

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

        self._endpoint_editor: QLineEdit | None = None
        self._editing_endpoint = 0  # -1 = minimum, +1 = maximum

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

    def _endpoint_label_rect(self, endpoint: int) -> QRectF:
        """Return the exact painted rectangle for one editable end label."""
        left, right, _groove_y, _tick_top, label_top = self._geometry()
        label_height = 14.0
        if endpoint < 0:
            return QRectF(left, label_top, 62.0, label_height)
        return QRectF(right - 62.0, label_top, 62.0, label_height)

    def _endpoint_hit(self, x: float, y: float) -> int:
        """Return -1/+1 only when the corresponding endpoint LABEL is hit."""
        point = QPointF(float(x), float(y))
        if self._endpoint_label_rect(-1).contains(point):
            return -1
        if self._endpoint_label_rect(+1).contains(point):
            return +1
        return 0

    def _begin_endpoint_edit(self, endpoint: int) -> None:
        """Replace one painted endpoint label with an inline text editor."""
        self._cancel_endpoint_edit()

        endpoint = -1 if endpoint < 0 else +1
        value = self._scale_minimum if endpoint < 0 else self._scale_maximum
        rect = self._endpoint_label_rect(endpoint)

        editor = QLineEdit(self)
        editor.setText(self._compact_scale_text(value))
        editor.setFrame(True)
        editor.setAlignment(
            (Qt.AlignmentFlag.AlignLeft if endpoint < 0 else Qt.AlignmentFlag.AlignRight)
            | Qt.AlignmentFlag.AlignVCenter
        )

        font = self.font()
        font.setPointSizeF(max(6.5, font.pointSizeF() - 1.0))
        editor.setFont(font)

        # Slightly taller than the painted label, but at the same location, so
        # the label visually turns into an editor rather than spawning a dialog.
        edit_rect = rect.adjusted(-2.0, -3.0, 2.0, 4.0)
        editor.setGeometry(
            int(round(edit_rect.x())),
            int(round(edit_rect.y())),
            int(round(edit_rect.width())),
            int(round(edit_rect.height())),
        )
        editor.setStyleSheet(
            "QLineEdit {"
            " background: palette(base);"
            " color: palette(text);"
            " border: 1px solid palette(highlight);"
            " padding: 0px 2px;"
            "}"
        )
        editor.returnPressed.connect(self._commit_endpoint_edit)
        editor.editingFinished.connect(self._cancel_endpoint_edit)
        editor.installEventFilter(self)

        self._endpoint_editor = editor
        self._editing_endpoint = endpoint

        editor.show()
        editor.setFocus(Qt.FocusReason.MouseFocusReason)
        editor.selectAll()
        self.update()

    def _commit_endpoint_edit(self) -> None:
        editor = self._endpoint_editor
        endpoint = self._editing_endpoint
        if editor is None or endpoint == 0:
            return

        value = parse_eng_number(editor.text())
        if value is None:
            # Keep the editor active and make the invalid entry obvious.
            editor.setStyleSheet(
                "QLineEdit {"
                " background: palette(base);"
                " color: palette(text);"
                " border: 1px solid rgb(190, 70, 70);"
                " padding: 0px 2px;"
                "}"
            )
            editor.setFocus()
            editor.selectAll()
            return

        self._endpoint_editor = None
        self._editing_endpoint = 0
        editor.removeEventFilter(self)
        editor.hide()
        editor.deleteLater()
        self.update()

        if endpoint < 0:
            self.minimumEditCommitted.emit(float(value))
        else:
            self.maximumEditCommitted.emit(float(value))

    def _cancel_endpoint_edit(self) -> None:
        editor = self._endpoint_editor
        if editor is None:
            return
        self._endpoint_editor = None
        self._editing_endpoint = 0
        editor.removeEventFilter(self)
        editor.hide()
        editor.deleteLater()
        self.update()

    def eventFilter(self, watched, event) -> bool:
        if watched is self._endpoint_editor:
            if event.type() == QEvent.Type.KeyPress:
                if event.key() == Qt.Key.Key_Escape:
                    self._cancel_endpoint_edit()
                    return True
        return super().eventFilter(watched, event)

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
        if hit:
            self._begin_endpoint_edit(hit)
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

            endpoint = -1 if index == 0 else (+1 if index == self._TICK_COUNT else 0)
            if endpoint == 0 or endpoint != self._editing_endpoint:
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

