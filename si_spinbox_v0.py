from __future__ import annotations

import math
import re

from PySide6.QtCore import Property, QTimer
from PySide6.QtGui import QValidator
from PySide6.QtWidgets import QDoubleSpinBox, QWidget


# Engineering/SI prefixes. "u" is accepted as an ASCII alias for micro.
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

_NUMBER_RE = re.compile(
    r"^\s*"
    r"([+-]?(?:(?:\d+(?:[.,]\d*)?)|(?:[.,]\d+))(?:[eE][+-]?\d+)?)"
    r"\s*"
    r"([yzafpnumµμkKMGTPEZY]?)"
    r"\s*"
    r"(.*?)"
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


def format_si(
    value: float,
    unit: str = "",
    decimals: int = 6,
    show_plus: bool = False,
) -> str:
    """Format a value in base SI units using an engineering prefix."""
    if not math.isfinite(value):
        return str(value)

    exponent = _engineering_exponent(value, decimals)
    prefix = _EXPONENT_TO_PREFIX[exponent]
    scaled = value / (10.0**exponent)

    sign = "+" if show_plus else ""
    number = f"{scaled:{sign}.{decimals}f}"

    if unit:
        return f"{number} {prefix}{unit}"
    if prefix:
        return f"{number}{prefix}"
    return number


class SISpinBox(QDoubleSpinBox):
    """
    QDoubleSpinBox with Nanonis-style engineering entry and cursor-digit stepping.

    Examples for a spin box whose base unit is "Hz":
        1000          -> 1.000000 kHz
        type "2.5k"   -> 2500 Hz
        type "3M"     -> 3 MHz
        type "250m"   -> 0.250 Hz

    Up/Down and the mouse wheel change the digit immediately to the right of
    the text cursor. For example, in:

        974.454238 kHz
            ^

    placing the cursor immediately before the first "4" after the decimal
    changes the value in 100 Hz increments.

    value() and setValue() always use the BASE SI unit. The prefix is purely
    display/input syntax.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._unit = ""
        self._display_decimals = 6
        self.setKeyboardTracking(False)

    # ---- Designer-visible properties -------------------------------------

    def getUnit(self) -> str:
        return self._unit

    def setUnit(self, unit: str) -> None:
        self._unit = str(unit)
        # Force a redisplay using the new unit.
        self.lineEdit().setText(self.textFromValue(self.value()))

    unit = Property(str, getUnit, setUnit)

    def getDisplayDecimals(self) -> int:
        return self._display_decimals

    def setDisplayDecimals(self, decimals: int) -> None:
        self._display_decimals = max(0, min(15, int(decimals)))
        self.lineEdit().setText(self.textFromValue(self.value()))

    displayDecimals = Property(int, getDisplayDecimals, setDisplayDecimals)

    # ---- QDoubleSpinBox virtuals -----------------------------------------

    def textFromValue(self, value: float) -> str:
        return format_si(
            float(value),
            unit=self._unit,
            decimals=self._display_decimals,
        )

    def valueFromText(self, text: str) -> float:
        parsed = self._parse_text(text)
        if parsed is None:
            return self.value()
        return parsed

    def validate(self, text: str, pos: int):
        stripped = text.strip()

        # Useful intermediate states while typing.
        if stripped in {"", "+", "-", ".", ",", "+.", "-.", "+,", "-,"}:
            return (QValidator.State.Intermediate, text, pos)

        parsed = self._parse_text(text)
        if parsed is not None:
            if self.minimum() <= parsed <= self.maximum():
                return (QValidator.State.Acceptable, text, pos)
            return (QValidator.State.Intermediate, text, pos)

        # Keep incomplete exponent entry editable, e.g. "1e" or "1e-".
        if re.match(
            r"^\s*[+-]?(?:(?:\d+(?:[.,]\d*)?)|(?:[.,]\d+))[eE][+-]?\s*$",
            text,
        ):
            return (QValidator.State.Intermediate, text, pos)

        return (QValidator.State.Invalid, text, pos)

    def stepBy(self, steps: int) -> None:
        """
        Step the decimal place selected by the text cursor.

        QAbstractSpinBox routes Up/Down and wheel stepping through stepBy(), so
        one implementation covers both input methods.
        """
        editor = self.lineEdit()
        text = editor.text()
        cursor = editor.cursorPosition()

        match = _NUMBER_RE.match(text)
        if match is None:
            super().stepBy(steps)
            return

        number_start, number_end = match.span(1)
        number_token = match.group(1)

        # The selected digit is the first digit at or to the right of the
        # cursor. This matches "put the cursor in front of the digit".
        digit_pos = None
        search_start = max(cursor, number_start)
        for p in range(search_start, number_end):
            if text[p].isdigit():
                digit_pos = p
                break

        # If the cursor is past the numeric field, use the nearest digit to
        # the left rather than falling back to an unrelated singleStep().
        if digit_pos is None:
            for p in range(min(cursor - 1, number_end - 1), number_start - 1, -1):
                if text[p].isdigit():
                    digit_pos = p
                    break

        if digit_pos is None:
            super().stepBy(steps)
            return

        local = digit_pos - number_start
        sign_len = 1 if number_token.startswith(("+", "-")) else 0
        core = number_token[sign_len:]
        core_index = local - sign_len

        decimal_index = core.find(".")
        if decimal_index < 0:
            decimal_index = core.find(",")
        if decimal_index < 0:
            decimal_index = len(core)

        if core_index < decimal_index:
            digit_exponent = decimal_index - core_index - 1
        else:
            # First digit after the decimal point is 10^-1.
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

        # Keep the cursor near the same textual digit after Qt reformats.
        old_cursor = cursor
        QTimer.singleShot(
            0,
            lambda: self.lineEdit().setCursorPosition(
                min(old_cursor, len(self.lineEdit().text()))
            ),
        )

    # ---- Parsing ----------------------------------------------------------

    def _parse_text(self, text: str) -> float | None:
        text = text.replace("\N{MINUS SIGN}", "-")
        match = _NUMBER_RE.match(text)
        if match is None:
            return None

        number_text = match.group(1).replace(",", ".")
        prefix = match.group(2)
        remainder = match.group(3).strip()

        # The unit is optional on typed input. If the user does include it,
        # require it to match this control's configured base unit.
        if remainder:
            if not self._unit:
                return None
            compact_remainder = remainder.replace(" ", "")
            compact_unit = self._unit.replace(" ", "")
            if compact_remainder != compact_unit:
                return None

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
