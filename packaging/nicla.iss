; The Windows installer. Compiled by build.ps1, which passes StageDir and AppVersion in --
; this script installs a tree that has already been assembled and smoke-tested rather than
; assembling one itself, so what ships is what was verified.
;
;   Program Files\NiclaSense\      python\  app\  service\      (read-only, replaced by an upgrade)
;   ProgramData\NiclaSense\        nicla.conf  logs\  service\  (writable, survives uninstall)
;
; The split is not tidiness. A service running as LocalSystem cannot write to Program
; Files, and the whole point of the thing is that it writes continuously for a year.

#ifndef StageDir
  #error Run this through build.ps1, which defines StageDir and AppVersion.
#endif
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "Nicla Sense ME capture"
#define ServiceName "NiclaCapture"
#define DataDir "{commonappdata}\NiclaSense"

[Setup]
AppId={{6E7B2F31-49B5-4C0E-9A2E-0E1D4C7A5B10}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Chris Roberts
DefaultDirName={autopf}\NiclaSense
DefaultGroupName=Nicla Sense ME
DisableProgramGroupPage=yes
OutputBaseFilename=NiclaSense-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The service needs to be registered and ProgramData needs its permissions set, so this
; cannot be a per-user install and should not pretend it might be.
PrivilegesRequired=admin
; The embeddable interpreter is amd64. Say so rather than failing later with an error
; about a missing DLL.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName}
LicenseFile=
InfoBeforeFile=

[Files]
Source: "{#StageDir}\python\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#StageDir}\app\*";    DestDir: "{app}\app";    Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#StageDir}\service\*"; DestDir: "{app}\service"; Flags: ignoreversion
Source: "{#StageDir}\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#StageDir}\PACKAGING.md"; DestDir: "{app}"; Flags: ignoreversion

; The config is data, not program. onlyifdoesntexist so an upgrade leaves a tuned setup
; alone, uninsneveruninstall so removing the software does not throw away the description
; of how somebody wanted it to run.
Source: "{#StageDir}\nicla.conf"; DestDir: "{#DataDir}"; Flags: onlyifdoesntexist uninsneveruninstall

[Dirs]
Name: "{#DataDir}"
Name: "{#DataDir}\logs"
Name: "{#DataDir}\service"

[Icons]
Name: "{group}\Nicla dashboard"; Filename: "{code:DashboardUrl}"
Name: "{group}\Capture log folder"; Filename: "{#DataDir}\logs"
Name: "{group}\Edit capture settings"; Filename: "notepad.exe"; Parameters: """{#DataDir}\nicla.conf"""

[Run]
; Order matters: register before starting, and start before the dashboard task, so the
; dashboard's first attach attempt has something to attach to.
Filename: "{app}\service\nicla-capture.exe"; Parameters: "install"; StatusMsg: "Registering the capture service..."; Flags: runhidden waituntilterminated
Filename: "{app}\service\nicla-capture.exe"; Parameters: "start"; StatusMsg: "Starting the capture service..."; Flags: runhidden waituntilterminated
Filename: "powershell.exe"; Parameters: "{code:DashboardTaskArgs}"; StatusMsg: "Setting up the dashboard..."; Flags: runhidden waituntilterminated
; Only when this install was actually asked to serve the network. A default install binds
; loopback, needs no rule, and should not be quietly opening ports on the machine.
Filename: "netsh.exe"; Parameters: "{code:FirewallAddArgs}"; StatusMsg: "Allowing the dashboard through the firewall..."; Flags: runhidden waituntilterminated; Check: ServesNetwork
Filename: "{code:DashboardUrl}"; Description: "Open the dashboard"; Flags: postinstall shellexec nowait skipifsilent

[UninstallRun]
; Mirror image, and every one of them tolerant of already being gone: an uninstall that
; fails because the service was stopped by hand is a bad uninstall.
Filename: "netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Nicla dashboard"""; RunOnceId: "FirewallRule"; Flags: runhidden waituntilterminated
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\service\dashboard-task.ps1"" -AppDir ""{app}"" -Uninstall"; RunOnceId: "DashboardTask"; Flags: runhidden waituntilterminated
Filename: "{app}\service\nicla-capture.exe"; Parameters: "stop"; RunOnceId: "StopService"; Flags: runhidden waituntilterminated
Filename: "{app}\service\nicla-capture.exe"; Parameters: "uninstall"; RunOnceId: "RemoveService"; Flags: runhidden waituntilterminated

[UninstallDelete]
; __pycache__ is written into the install tree at runtime, so Inno does not know about it
; and would leave the directories behind.
Type: filesandordirs; Name: "{app}\app\__pycache__"
Type: filesandordirs; Name: "{app}\service\__pycache__"

[Code]
{ Where the dashboard binds, as given on the setup command line:

    setup.exe /VERYSILENT /HTTPHOST=192.168.1.5 /ALLOWHOST=nicla-01.lan

  Defaulting to loopback means an install that says nothing gets exactly the behaviour it
  always had -- no network bind, no firewall rule, no prompt. Everything below keys off
  this one value. }
function HttpHost(): String;
begin
  Result := ExpandConstant('{param:HttpHost|127.0.0.1}');
end;

function ServesNetwork(): Boolean;
begin
  Result := (HttpHost() <> '127.0.0.1') and (HttpHost() <> 'localhost');
end;

{ The URL for the Start Menu shortcut and the post-install tick box.

  This is not cosmetic. Binding one specific address means loopback is no longer listening,
  so a shortcut to 127.0.0.1 would fail on the very machine the dashboard is running on. A
  wildcard bind does answer on loopback, and locally that is the better address to use. }
function DashboardUrl(Param: String): String;
begin
  if (HttpHost() = '0.0.0.0') or not ServesNetwork() then
    Result := 'http://127.0.0.1:8988/'
  else
    Result := 'http://' + HttpHost() + ':8988/';
end;

function DashboardTaskArgs(Param: String): String;
var
  Allow: String;
begin
  Result := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}') +
            '\service\dashboard-task.ps1" -AppDir "' + ExpandConstant('{app}') +
            '" -HttpHost "' + HttpHost() + '"';
  { Comma-separated on the command line, which is also how PowerShell binds a [string[]],
    so /ALLOWHOST=a.lan,b.lan needs no translation. Omitted entirely when empty: passing
    -AllowHost "" would hand the task an array holding one empty name. }
  Allow := ExpandConstant('{param:AllowHost|}');
  if Allow <> '' then
    Result := Result + ' -AllowHost ' + Allow;
end;

{ Scoped to domain and private on purpose. Adding "public" would re-open this port every
  time the machine joined an untrusted network, which is not what "let the office see it"
  was ever meant to mean. The rule names pythonw.exe because that is what the logon task
  runs -- python.exe is the service, and the service is loopback-only. }
function FirewallAddArgs(Param: String): String;
begin
  Result := 'advfirewall firewall add rule name="Nicla dashboard" dir=in action=allow' +
            ' program="' + ExpandConstant('{app}') + '\python\pythonw.exe"' +
            ' protocol=TCP localport=8988 profile=domain,private';
end;

function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  { An upgrade over a running service cannot replace python.exe while the service is
    holding it. Stopping it here is what makes an upgrade an upgrade rather than a
    confusing "file in use" dialog; if it is not installed, sc simply reports so. }
  Exec(ExpandConstant('{sys}\sc.exe'), 'stop {#ServiceName}', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Pages: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    { The captures are the user's data and outlive the software that wrote them, so they
      are kept by default and removed only if asked. Silent uninstalls keep them: an
      unattended run is the last place to make an irreversible choice on someone's behalf. }
    if not UninstallSilent() then
    begin
      Pages := MsgBox('Delete the captured sensor logs in ' + ExpandConstant('{#DataDir}') + '?'
                      + #13#10#13#10 + 'Choose No to keep them.',
                      mbConfirmation, MB_YESNO or MB_DEFBUTTON2);
      if Pages = IDYES then
        DelTree(ExpandConstant('{#DataDir}'), True, True, True);
    end;
  end;
end;
