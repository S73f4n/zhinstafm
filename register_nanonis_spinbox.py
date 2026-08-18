"""
Optional Qt Designer registration for the NanonisSpinBox custom widget.

PowerShell:
    $env:PYSIDE_DESIGNER_PLUGINS = (Get-Location).Path
    pyside6-designer mfli_oscillation_control_v0_nanonis.ui

bash:
    PYSIDE_DESIGNER_PLUGINS="$PWD" pyside6-designer mfli_oscillation_control_v0_nanonis.ui
"""

from PySide6.QtDesigner import QPyDesignerCustomWidgetCollection
from nanonis_spinbox import NanonisSpinBox


DOM_XML = """
<ui language="c++">
    <widget class="NanonisSpinBox" name="nanonisSpinBox">
        <property name="geometry">
            <rect>
                <x>0</x>
                <y>0</y>
                <width>95</width>
                <height>22</height>
            </rect>
        </property>
        <property name="displayDecimals">
            <number>6</number>
        </property>
        <property name="baseUnit">
            <string>Hz</string>
        </property>
        <property name="buttonSymbols">
            <enum>QAbstractSpinBox::NoButtons</enum>
        </property>
    </widget>
</ui>
"""

QPyDesignerCustomWidgetCollection.registerCustomWidget(
    NanonisSpinBox,
    module="nanonis_spinbox",
    tool_tip="Nanonis-style engineering field: number + SI prefix only",
    group="MFLI Controls",
    xml=DOM_XML,
)
