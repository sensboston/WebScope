# Rigol Web Panel

Browser front panel for Rigol DS1000E/D oscilloscopes (DS1052E, DS1102E, ...) connected over USB.

These scopes have no network port, and the vendor software (UltraScope) is hard to use. This is a
small Python server that runs on the computer the scope is plugged into and serves one web page:
live screen of both channels, the usual knobs, trigger, run control, measurements and PNG/CSV export.

## Requirements

- Windows with NI-VISA (installed together with UltraScope), or any VISA backend PyVISA can use
- Python 3.10+

```
pip install -r requirements.txt
python server.py
```

Then open http://localhost:8765/. Options: `--host`, `--port`, `--resource` (VISA resource name;
by default the first Rigol found).

The server listens on 127.0.0.1 by default. Use `--host 0.0.0.0` to reach it from other machines
(and allow the port in the firewall).

## Start with Windows

`install_autostart.ps1` (run once from an elevated PowerShell) registers a scheduled task that starts
the server at boot as the given user, listening on the network, and restarts it if it crashes:

```
powershell -ExecutionPolicy Bypass -File install_autostart.ps1 -User <user> [-Port 8765]
```

The server output goes to `C:\ProgramData\RigolWeb\server.log`. Open the port in the firewall, e.g.
`netsh advfirewall firewall add rule name="Rigol Web Panel" dir=in action=allow protocol=TCP localport=8765 remoteip=localsubnet`.

The server survives the scope being switched off or unplugged: the page shows OFFLINE and the
connection comes back by itself.

## Notes on the DS1000E

- The scope ignores commands sent too close together; the server keeps at least 0.12 s between
  them (UltraScope does the same). That limits the screen to about 2-3 frames per second.
- In remote mode the scope locks its front panel. "Release panel" (or stopping the server) unlocks it.
- Firmware 00.04.01 does not answer the screenshot query, so the screen is drawn from the waveform data.
- Close UltraScope while the panel is running; both cannot hold the USB connection.
