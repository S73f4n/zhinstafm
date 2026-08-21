"""
Optional Qt Designer registration for the NanonisSpinBox custom widget.

PowerShell:
    $env:PYSIDE_DESIGNER_PLUGINS = (Get-Location).Path
    pyside6-designer mfli_oscillation_control_v0_nanonis.ui

bash:
    PYSIDE_DESIGNER_PLUGINS="$PWD" pyside6-designer mfli_oscillation_control_v0_nanonis.ui
"""

from PySide6.QtDesigner import QPyDesignerCustomWidgetCollection
from nanonis_spinbox import NanonisLed, NanonisLockButton, NanonisSlider, NanonisSpinBox, NanonisSwitch


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


SLIDER_DOM_XML = """
<ui language="c++">
    <widget class="NanonisSlider" name="nanonisSlider">
        <property name="geometry">
            <rect><x>0</x><y>0</y><width>180</width><height>30</height></rect>
        </property>
        <property name="orientation"><enum>Qt::Horizontal</enum></property>
        <property name="tickPosition"><enum>QSlider::TicksBelow</enum></property>
    </widget>
</ui>
"""

QPyDesignerCustomWidgetCollection.registerCustomWidget(
    NanonisSlider,
    module="nanonis_spinbox",
    tool_tip="Nanonis-style slider with ticks below and arrow handle",
    group="MFLI Controls",
    xml=SLIDER_DOM_XML,
)


SWITCH_DOM_XML = """
<ui language="c++">
    <widget class="NanonisSwitch" name="nanonisSwitch">
        <property name="geometry">
            <rect><x>0</x><y>0</y><width>150</width><height>24</height></rect>
        </property>
        <property name="text"><string>Controller</string></property>
        <property name="checkable"><bool>true</bool></property>
    </widget>
</ui>
"""

QPyDesignerCustomWidgetCollection.registerCustomWidget(
    NanonisSwitch,
    module="nanonis_spinbox",
    tool_tip="Compact GTK/Nanonis-style on/off toggle",
    group="MFLI Controls",
    xml=SWITCH_DOM_XML,
)


LOCK_DOM_XML = """
<ui language="c++">
    <widget class="NanonisLockButton" name="nanonisLockButton">
        <property name="geometry">
            <rect><x>0</x><y>0</y><width>26</width><height>34</height></rect>
        </property>
        <property name="checkable"><bool>true</bool></property>
    </widget>
</ui>
"""

QPyDesignerCustomWidgetCollection.registerCustomWidget(
    NanonisLockButton,
    module="nanonis_spinbox",
    tool_tip="Small lock toggle for linked lower/upper ranges",
    group="MFLI Controls",
    xml=LOCK_DOM_XML,
)


LED_DOM_XML = """
<ui language="c++">
    <widget class="NanonisLed" name="nanonisLed">
        <property name="geometry">
            <rect><x>0</x><y>0</y><width>28</width><height>28</height></rect>
        </property>
        <property name="on"><bool>false</bool></property>
    </widget>
</ui>
"""

QPyDesignerCustomWidgetCollection.registerCustomWidget(
    NanonisLed,
    module="nanonis_spinbox",
    tool_tip="Read-only LED status indicator",
    group="MFLI Controls",
    xml=LED_DOM_XML,
)
