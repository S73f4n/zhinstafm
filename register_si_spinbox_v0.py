"""
Optional Qt Designer plugin registration for SISpinBox.

PowerShell:
    $env:PYSIDE_DESIGNER_PLUGINS = (Get-Location).Path
    pyside6-designer mfli_oscillation_control_v0_si.ui

bash:
    PYSIDE_DESIGNER_PLUGINS="$PWD" pyside6-designer mfli_oscillation_control_v0_si.ui
"""

from PySide6.QtDesigner import QPyDesignerCustomWidgetCollection

from si_spinbox_v0 import SISpinBox


DOM_XML = """
<ui language="c++">
    <widget class="SISpinBox" name="siSpinBox">
        <property name="geometry">
            <rect>
                <x>0</x>
                <y>0</y>
                <width>160</width>
                <height>24</height>
            </rect>
        </property>
        <property name="unit">
            <string>Hz</string>
        </property>
        <property name="displayDecimals">
            <number>6</number>
        </property>
    </widget>
</ui>
"""

QPyDesignerCustomWidgetCollection.registerCustomWidget(
    SISpinBox,
    module="si_spinbox_v0",
    tool_tip="Engineering/SI spin box with cursor-digit stepping",
    group="MFLI Controls",
    xml=DOM_XML,
)
