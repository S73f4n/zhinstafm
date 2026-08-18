from __future__ import annotations

import math
import re

from PySide6.QtCore import Property, QTimer, Qt
from PySide6.QtGui import QValidator
from PySide6.QtWidgets import QAbstractSpinBox, QDoubleSpinBox, QWidget


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
