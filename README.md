# MFLI Oscillation Control

A Nanonis-inspired Qt interface for controlling a Zurich Instruments **MFLI lock-in amplifier** in an AFM needle-sensor oscillation-control setup.

The current application supervises two hardware feedback loops:

- **PLL1 — phase/frequency feedback:** Demodulator 1 phase → PI → Oscillator 1 frequency
- **PID3 — amplitude feedback:** Demodulator 1 amplitude (`R`) → PI → Signal Output 1 amplitude

The fast feedback loops remain inside the MFLI. Python provides the operator GUI, parameter control, live readback, resonance characterization, and PID advising.

> **Status:** experimental laboratory software. Verify routing, output limits, controller signs, and safe excitation levels on your own instrument before use.

---

## Features

### Oscillation control

- Signal Output 1 on/off switch
- Phase/frequency controller on/off switch
- Amplitude controller on/off switch
- bright PLL-lock LED
- center frequency, live frequency shift, and actual frequency readback
- display-only frequency-shift slider
- linked symmetric lower/upper frequency-shift limits
- phase setpoint, measured phase, and phase error
- amplitude setpoint, measured amplitude, and moving RMS amplitude error
- manual Signal Output amplitude control while PID3 is disabled
- P/I spinboxes and sliders for both controllers
- Nanonis-style SI-prefix numeric entry
- YAML persistence for connection settings

### Resonance characterization

A separate **Frequency Sweep** window sweeps Oscillator 1 around the current center frequency and records Demodulator 1 response.

The sweep window:

1. sweeps frequency over user-defined lower/upper offsets;
2. records Demodulator 1 amplitude and phase;
3. plots `R` and phase versus frequency shift;
4. fits the resonator response;
5. extracts resonance frequency, Q, bandwidth, and phase at resonance;
6. propagates the fitted values back to the main Oscillation Control window.

Applying a fit updates:

```text
Resonance frequency  -> Center Freq.
Q-factor             -> Q-Factor
Phase at resonance   -> Phase Ref.
```

### PID / PLL Advisor

The GUI exposes the LabOne PID Advisor for both feedback loops.

**PLL1 / phase controller**

```text
Controller       PID / PLL 1 (index 0)
Advisor mode     PI
DUT model        Resonator Frequency
Target BW        BW Pha
Frequency        Center Freq.
Q                Q-Factor
Gain             Amp. / Exc.
```

**PID3 / amplitude controller**

```text
Controller       PID / PLL 3 (index 2)
Advisor mode     PI
DUT model        Resonator Amplitude
Target BW        BW Amp
Frequency        Center Freq.
Q                Q-Factor
Gain             Amp. / Exc.
Delay            Delay
```

The advised P/I values are transferred directly to the selected MFLI controller using the Advisor's **To PID** operation.

---

## Nanonis-style controls

### SI-prefix entry

The editable field contains the numerical value and optional SI prefix; the fixed base unit stays in the label.

```text
Center Freq. (Hz)    [ 974.494110k ]
Amplitude (V)        [ 60.000000m ]
Phase Ref. (deg)     [ -138.000000 ]
```

Accepted prefixes include:

```text
y z a f p n u µ m k M G T P E Z Y
```

Examples:

```text
2.5k  -> 2500
60m   -> 0.060
300u  -> 300e-6
```

### Cursor-digit stepping

Place the cursor immediately **before a digit** and use the mouse wheel or `Up` / `Down`. The selected decimal place is changed immediately.

### Sliders

The custom sliders provide:

- Nanonis-style arrow handle
- engineering-notation scale labels
- intermediate ticks
- direct mouse dragging
- editable range endpoints

Double-click only the left or right endpoint label, type the new SI-aware value, and press `Enter`.

---

## Centering the PLL

The **Center** button averages fresh PLL1 output-frequency samples rather than using one instantaneous readback.

The current implementation targets **32 streamed samples**, with a host-side averaging interval bounded between **20 ms and 500 ms**, then writes their arithmetic mean to the PLL center frequency.

The PLL remains enabled during centering.

---

## Amplitude error RMS

The visible amplitude error is a moving RMS over a **250 ms** host-side window:

```text
RMS error = sqrt(mean(error²))
```

The instantaneous signed PID3 error is still retained internally for reconstructing the measured amplitude.

---

## Expected MFLI routing

The application currently expects the following instrument configuration.

### PLL1 — phase/frequency controller

```text
LabOne controller    PID / PLL 1
Mode                 PLL
Input                Demodulator phase
Input channel        Demodulator 1
Output               Oscillator frequency
Output channel       Oscillator 1
```

### PID3 — amplitude controller

```text
LabOne controller    PID / PLL 3
Mode                 PID
Input                Demodulator R
Input channel        Demodulator 1
Output               Signal Output amplitude
Output channel       Signal Output 1 / Amplitude 1
```

The application checks this routing when it connects. A mismatch produces a warning; the app does **not** silently reconfigure the instrument.

---

## Requirements

### Hardware / LabOne

- Zurich Instruments MFLI configured for the PID/PLL functionality used by the setup
- LabOne Data Server reachable from the computer running the GUI
- the controller routing described above, or an intentionally modified equivalent

### Python packages

```text
PySide6>=6.7
zhinst-toolkit>=0.7
PyYAML>=6.0
numpy>=1.24
scipy>=1.10
matplotlib>=3.7
```

Install the supplied requirements:

```bash
python -m pip install -r requirements_mfli_gui.txt
```

---

## Installation

Clone the repository:

```bash
git clone <your-repository-url>
cd <repository-directory>
```

Create a virtual environment:

```bash
python -m venv .venv
```

Linux / macOS:

```bash
source .venv/bin/activate
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements_mfli_gui.txt
```

Run the GUI:

```bash
python mfli_oscillation_control_v0_nanonis.py
```

---

## Connection settings

The application stores the most recently used LabOne host and MFLI device name in:

```text
mfli_connection.yaml
```

Example:

```yaml
connection:
  host: 192.168.1.100
  device: DEV1234
```

The file is loaded on startup and saved when connecting and when closing the application.

Because this is usually machine-specific, it is a good candidate for `.gitignore`:

```gitignore
mfli_connection.yaml
```

---

## Typical workflow

1. Start LabOne and verify the MFLI routing and safe output ranges.
2. Launch the application.
3. Enter the LabOne host/IP and device name.
4. Click **Connect**.
5. Enable Signal Output 1 and select a safe initial drive amplitude.
6. Keep PLL1 and PID3 disabled.
7. Open **Frequency Sweep...** and characterize the resonance.
8. Fit the resonance and apply center frequency, Q, and phase.
9. Enter the desired amplitude and phase controller bandwidths.
10. Run **Advise Amp** and **Advise Pha**.
11. Review the resulting P/I values.
12. Enable the Phase Controller and verify the green PLL-lock LED.
13. Enable the Amplitude Controller.
14. Monitor frequency shift, amplitude, phase, and RMS amplitude error during operation.

---

## Frequency sweep safety conditions

The current sweep implementation requires:

- Phase Controller / PLL1 **OFF**
- Amplitude Controller / PID3 **OFF**
- Signal Output 1 **ON**

PID3 is intentionally required to be off because active amplitude regulation would flatten the measured resonance response.

The sweeper uses manual bandwidth control so the application's sweep does not overwrite the existing Demodulator 1 bandwidth/filter configuration. The oscillator frequency present before the sweep is restored when the sweep ends or is stopped.

---

## Manual output amplitude

When PID3 is **disabled**, the Output Amplitude field becomes editable and controls:

```text
Signal Output 1 / Amplitude 1
```

When PID3 is enabled, the same field becomes a grey read-only controller-output readback. Manual amplitude writes are rejected while PID3 is active.

---

## Custom Qt widgets

The project uses only PySide6 / Qt for the custom controls:

- `NanonisSpinBox` — SI-prefix input and cursor-digit stepping
- `NanonisSlider` — custom ticks, labels, arrow handle, dragging, and inline endpoint editing
- `NanonisSwitch` — compact GTK-style enable switch
- `NanonisLockButton` — linked-range lock
- `NanonisLed` — read-only PLL-lock LED

No third-party Qt widget library is required.

---

## Project structure

```text
.
├── mfli_oscillation_control_v0_nanonis.py
│   Main application, MFLI worker, sweep logic, fit, and PID Advisor
│
├── mfli_oscillation_control_v0_nanonis.ui
│   Main Qt Designer UI
│
├── frequency_sweep.ui
│   Resonance sweep sub-window
│
├── nanonis_spinbox.py
│   Custom Nanonis-style Qt widgets
│
├── register_nanonis_spinbox.py
│   Optional Qt Designer custom-widget registration
│
├── requirements_mfli_gui.txt
│   Python dependencies
│
└── mfli_connection.yaml
    Runtime-generated connection settings
```

---

## Editing the UI with Qt Designer

The application loads the `.ui` files dynamically with `QUiLoader`; there is no generated `ui_*.py` file that has to be rebuilt after every visual change.

Main window:

```bash
pyside6-designer mfli_oscillation_control_v0_nanonis.ui
```

Sweep window:

```bash
pyside6-designer frequency_sweep.ui
```

The Python controller looks up widgets by their Qt `objectName`. You can change layouts, sizing, text, fonts, and styling freely, but renaming an `objectName` also requires changing the corresponding Python binding.

### Custom widgets in Designer

For full Qt Designer integration:

Linux / macOS:

```bash
export PYSIDE_DESIGNER_PLUGINS="$PWD"
pyside6-designer mfli_oscillation_control_v0_nanonis.ui
```

Windows PowerShell:

```powershell
$env:PYSIDE_DESIGNER_PLUGINS = (Get-Location).Path
pyside6-designer mfli_oscillation_control_v0_nanonis.ui
```

---

## Architecture

```text
┌──────────────────────────────┐
│          PySide6 GUI         │
│ .ui files + custom widgets   │
└──────────────┬───────────────┘
               │ Qt signals / slots
┌──────────────▼───────────────┐
│       MFLI worker thread     │
│ settings / polling / sweep   │
│ PID Advisor / averaging      │
└──────────────┬───────────────┘
               │ zhinst-toolkit
┌──────────────▼───────────────┐
│      LabOne Data Server      │
└──────────────┬───────────────┘
               │
┌──────────────▼───────────────┐
│             MFLI             │
│ PLL / PID loops in hardware  │
└──────────────────────────────┘
```

Python is deliberately **not part of the fast feedback path**. The hardware PLL/PID loops continue running in the MFLI while the application performs supervisory control and readout.

---

## Safety and scope

This application can directly change oscillator frequency, Signal Output amplitude, PID gains, limits, and feedback enable states.

Before using it on an AFM:

- verify Signal Output range and excitation amplitude;
- verify Demodulator 1, Oscillator 1, and Signal Output 1 routing;
- start with conservative excitation values;
- check the signs and magnitudes of P/I gains before enabling a loop;
- inspect the resonance fit before applying it;
- keep LabOne available during development to cross-check node values.

This software is an experimental control interface, not a machine-safety or equipment-protection system.

---

## Development roadmap

Useful next steps include:

- persistent storage for UI slider ranges and Advisor parameters
- user-selectable demodulator / oscillator / PID routing
- selectable resonance-fit interval
- sweep-data export and logging
- configurable averaging windows
- mocked LabOne node-tree tests
- packaging into an installable desktop application

---

## Acknowledgement

The workflow and visual language are inspired by the Nanonis Oscillation Control interface. Instrument communication uses Zurich Instruments LabOne through `zhinst-toolkit`.

This is an independent project and is not an official Zurich Instruments or Nanonis product.
