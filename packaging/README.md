# Windows packaging

Builds an installer that puts the capture on a Windows machine as a service that starts at
boot and never stops, plus a dashboard that appears in the logged-on user's browser. Neither
shows a window. The target machine needs no Python and no internet: the installer carries an
interpreter, which is most of why the offline viewer and its matplotlib dependency were
deleted.

## Building

Every push to `master` builds one in CI and attaches it to the run, so building by hand is
only for changing the packaging itself:

```bash
gh run download --name NiclaSense-Setup
```

To build locally: needs Windows, PowerShell, a Python on `PATH` (only to resolve the pyserial
wheel), and [Inno Setup 6](https://jrsoftware.org/isinfo.php).

```powershell
winget install JRSoftware.InnoSetup
.\build.ps1 -AppVersion 1.0.0
```

Output lands in `dist\`. Three steps, each skippable so a failed compile does not re-download
anything:

| Step | What it does |
|---|---|
| fetch | Embeddable CPython, the pyserial wheel, WinSW → `downloads\` |
| stage | Unpacks those plus `python\` and this directory → `dist\stage\` |
| compile | Runs Inno Setup over the staged tree → `dist\NiclaSense-Setup-*.exe` |

`dist\stage\` **is** what gets installed, so it can be run without installing:

```powershell
dist\stage\python\python.exe dist\stage\service\supervise.py capture --config dist\stage\nicla.conf
```

That is the fastest way to find out whether a packaging problem is in the packaging or in
the app. The build also smoke-tests it — a staged tree that cannot import its own program
fails the build rather than the install.

### Pinning the downloads

Versions are parameters at the top of `build.ps1`. To pin the bytes as well as the numbers,
run once from a build you trust and commit the result:

```powershell
.\build.ps1 -Record
```

after which every build can be run with `-Verify`, which fails on any mismatch. CI runs with
`-Verify` when `hashes.txt` exists.

## What gets installed

```
C:\Program Files\NiclaSense\        replaced wholesale by an upgrade
  python\        embeddable CPython + pyserial unpacked into Lib\site-packages
  app\           the contents of python\, minus the tests and benchmarks
  service\       nicla-capture.exe (WinSW) + its XML, supervise.py, dashboard-task.ps1

C:\ProgramData\NiclaSense\          survives uninstall unless you say otherwise
  nicla.conf     the service's settings; an upgrade never overwrites it
  logs\          the captures
  service\       the service's own stdout/stderr, rolled at 16 MB
```

The split is not tidiness: a service running as LocalSystem cannot write to Program Files,
and the entire point of this thing is that it writes continuously for a year.

## The two launches

**Capture — a service.** WinSW registers `NiclaCapture`, automatic and delayed start, running
`python.exe service\supervise.py capture --config ...\nicla.conf`. Session 0, no console, no
window; WinSW gives the child real pipes so the progress output reaches a log file rather
than a dead handle.

**Dashboard — a logon task.** A hidden scheduled task runs `pythonw.exe service\supervise.py
dashboard ...` as whoever logs on, thirty seconds in. No `--open`: the Start Menu has a
shortcut to <http://127.0.0.1:8988/>. It attaches to the capture over loopback, which crosses
the session boundary, so a service in session 0 and a dashboard in the user's session need
nothing between them and Windows Firewall never prompts.

### Why supervise.py exists

Two Windows facts, neither of them the app's business, which is why the file lives here.

`main.py` is entitled to exit — 1 when the board is unplugged, and at boot it will usually
exit before the first sample, because a service starts before USB enumeration finishes. The
first attempt failing is the normal path. The SCM can restart a service that exits non-zero
but treats a zero exit as a deliberate stop, and `main.py` has clean-exit paths nobody asked
for; supervising in one place gives both the same rule and a backoff that can be stated
plainly. It backs off 5s → 60s and resets after any run that lasted five minutes.

And `pythonw.exe`, which is what makes the dashboard invisible, leaves `sys.stderr` as
`None`. `print()` tolerates that; `sys.stderr.write()` — how both programs report progress —
raises on the first status line. `supervise.py` points the streams at a file before importing
anything that might use them.

## Operating it

```powershell
sc query NiclaCapture
sc stop NiclaCapture ; sc start NiclaCapture      # after editing nicla.conf
Get-Content "$env:ProgramData\NiclaSense\service\nicla-capture.out.log" -Tail 20 -Wait
Get-Content "$env:LOCALAPPDATA\NiclaSense\dashboard.log" -Tail 20 -Wait
Get-ScheduledTask -TaskPath \NiclaSense\ | Format-List
```

Both settings that matter are in `nicla.conf`, which documents itself. The two worth knowing
without opening it: the capture logs one row a minute and bursts to the full 200 Hz whenever
the accelerometer or gyroscope moves, and it keeps a year of that or 4 GB, whichever comes
first.

## Known limits

- **Two users logged on at once means two dashboards**, and the second cannot bind port 8988.
  It logs the clash and gives up; the first is unaffected. Correct behaviour for a
  single-port loopback server, but it is a surprise if you go looking for the second one.
  This depends on `hub.REUSE_ADDR`, which deliberately does not set `SO_REUSEADDR` on
  Windows — with it set, Windows lets the second dashboard bind the port the first is
  serving on and splits connections between them unpredictably. It was that way until the
  first CI run on Windows caught it.
- **The capture takes the serial port exclusively.** Running `main.py` by hand against the
  same board while the service is running will fail with `Resource busy` — stop the service
  first, or attach a second dashboard to the running one, which is what the socket is for.
- **An unattended install cannot start the dashboard immediately**, there being no session to
  start it in. It begins at the next logon.
- **Nothing is signed.** SmartScreen will warn on first run of the installer.
