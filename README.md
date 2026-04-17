# FPV Race Screen Monitor

Windows utilities for local-network FPV race monitoring:

- `agent.py` captures a pilot PC screen, reads the lap timer from a configured screen region, and sends video plus lap metadata to the monitor PC.
- `monitor.py` receives all agent feeds, shows them in a live grid, tracks accepted lap times, and manages a timed race window.
- `monitor_test.py` is a local test monitor that simulates agents and race results without a network setup.

## Included In This Repository

- Source:
  - `agent.py`
  - `monitor.py`
  - `monitor_test.py`
- Prebuilt Windows binaries:
  - `builds/agent-windows-x64.zip`
  - `builds/monitor-windows-x64.zip`
  - `builds/monitor-test-windows-x64.zip`

## Quick Start

### Agent PC

Run `agent.py` or the packaged `agent.exe`, configure:

- monitor IP address
- agent name
- timer OCR region (`X`, `Y`, `Width`, `Height`)

Then connect to the monitor PC.

### Monitor PC

Run `monitor.py` or the packaged `monitor.exe`.

Features:

- multi-PC live preview grid
- lap OCR display per pilot
- best lap tracking
- timed race window with start/stop/reset controls

### Test Mode

Run `monitor_test.py` or `monitor_test.exe` to preview the interface and lap workflow without real agents.

## Build Notes

The Windows builds in `builds/` were created with PyInstaller on Windows x64.

If you want to rebuild locally:

```powershell
python -m PyInstaller --noconfirm --clean agent.spec
python -m PyInstaller --noconfirm --clean monitor.spec
python -m PyInstaller --noconfirm --clean --windowed --name monitor_test monitor_test.py
```

## Network Model

- Agents connect to the monitor over TCP in the same local network.
- OCR is performed on the agent side.
- The monitor accepts lap results only while the race timer is active.
