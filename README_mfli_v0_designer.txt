MFLI Oscillation Control v0 — Designer split
============================================

Files:
  mfli_oscillation_control_v0.py
      Instrument communication and GUI/controller logic.

  mfli_oscillation_control_v0.ui
      Qt Designer user interface. Edit this file visually.

  requirements_mfli_gui.txt
      Python dependencies.

Run:
  pip install -r requirements_mfli_gui.txt
  python mfli_oscillation_control_v0.py

Edit the UI:
  pyside6-designer mfli_oscillation_control_v0.ui

Important:
The Python code locates widgets by their Qt objectName. You can freely change
layout, sizes, labels, fonts, styles, grouping, etc., but keep these objectNames
unless you also update the Python bindings:

  hostEdit
  serialEdit
  connectButton
  refreshButton
  statusbar

  phaseEnable
  phaseSetpoint
  phaseP
  phaseI
  phaseCenter
  phaseLower
  phaseUpper
  phaseErrorValue
  phaseShiftValue
  phaseValueValue
  phaseLockValue

  ampEnable
  ampSetpoint
  ampP
  ampI
  ampCenter
  ampLower
  ampUpper
  ampErrorValue
  ampShiftValue
  ampValueValue
  ampActualMinValue
  ampActualMaxValue

The UI is loaded dynamically with PySide6.QtUiTools.QUiLoader, so there is no
pyside6-uic compilation step after editing the .ui file.
