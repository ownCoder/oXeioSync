; Inno Setup script for oXeioSync.
;
; Per-user, no administrator rights. The application only ever writes to
; %LOCALAPPDATA% and keeps its start-on-login entry under HKCU, so there is
; nothing it needs machine-wide — and asking for elevation it does not need is
; its own kind of bug.
;
; Two things this script is careful about:
;
;   * The user's sync database is never touched. %LOCALAPPDATA%\oXeioSync holds
;     this machine's device identity and folder index. Deleting it does not just
;     reset preferences — it makes the machine a stranger to every device it was
;     paired with. Uninstall leaves it alone unless the user explicitly asks.
;
;   * A running instance is asked to exit, not killed. Closing its window only
;     hides it to the tray, so Windows' own Restart Manager cannot shut it down;
;     and killing it would take the sync engine with it before it had flushed.
;     `oXeioSync.exe --quit` returns once the app has really gone.

#define AppName "oXeioSync"
#define AppExe "oXeioSync.exe"
#define AppPublisher "oXeioSync"
#define SourceDir "..\dist\oXeioSync"

; Read the version straight from the built executable, so the installer can
; never claim a version the payload does not have. ProductVersion is the string
; resource ("0.1.0"); GetVersionNumbersString would give "0.1.0.0", which reads
; badly in Apps & Features and disagrees with the version the app reports.
#define AppVersion GetStringFileInfo(SourceDir + "\" + AppExe, "ProductVersion")

; Compile-time guard. Either of these beside the exe switches the application
; into portable mode, which moves the database into the program folder — the one
; folder an installer sweeps. Shipping one would also ship a device identity.
#if DirExists(SourceDir + "\data") || FileExists(SourceDir + "\portable")
  #error The payload contains a "data" folder or a "portable" marker; remove it.
#endif

[Setup]
AppId={{7F3C2A64-5E11-4E93-9C8B-0B7A1D6E2F41}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}

; `lowest` means no UAC prompt and {autopf} resolves to
; %LOCALAPPDATA%\Programs. The user can still choose another folder.
;
; PrivilegesRequiredOverridesAllowed is deliberately blank — an all-users
; install would be a lie here. The application resolves both its data folder and
; its autostart entry from the *running* user, so "install for everyone" would
; still give each user a separate database and identity, the post-install launch
; would build one in the administrator's profile, and an elevated uninstaller
; could only ever clean that one.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes

OutputDir=..\dist
OutputBaseFilename=oXeioSync-setup
SetupIconFile=oxeiosync.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}

; The payload is ~317 MB of already-compressed DLLs; solid LZMA2 still roughly
; halves it, at the cost of a few minutes here.
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=4

; Restart Manager is off on purpose. It cannot close this application politely —
; closing the window only hides it to the tray — so its remaining move is to
; terminate the process, which is exactly the outcome the --quit handshake below
; exists to avoid.
CloseApplications=no

WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
; The whole one-folder build, including the _internal tree.
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; PyInstaller's _internal tree changes shape between builds; clearing it stops
; a stale DLL from an older version being loaded in preference to the new one.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: dirifempty; Name: "{app}"

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';

var
  { Whether the data folder was there before this uninstall began. Asking
    `--quit` to run creates the folder as a side effect, and offering to delete
    something the uninstaller itself just made would be absurd. }
  DataFolderPreexisted: Boolean;

{ Whether the executable can be replaced. A running image cannot be renamed, so
  this answers the question that actually matters — is the file free? — rather
  than counting processes and hoping. }
function ExeIsFree(const ExePath: String): Boolean;
var
  Probe: String;
begin
  Result := True;
  if not FileExists(ExePath) then
    Exit;

  Probe := ExePath + '.replace-probe';
  DeleteFile(Probe);
  Result := RenameFile(ExePath, Probe);
  if Result then
    RenameFile(Probe, ExePath);
end;

{ Terminate only processes started from *this* install folder.

  Killing by image name would reach an unrelated copy — a portable one on a USB
  stick, or a second installation in another folder — and take its sync engine
  down with it. }
procedure ForceStopThisInstall(const ExePath: String);
var
  Quote, Params: String;
  ResultCode: Integer;
begin
  Quote := '''';
  Params := '-NoProfile -ExecutionPolicy Bypass -Command "' +
            'Get-Process -Name oXeioSync -ErrorAction SilentlyContinue | ' +
            'Where-Object { $_.Path -eq ' + Quote + ExePath + Quote + ' } | ' +
            'Stop-Process -Force"';
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Params, '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(2500);
end;

{ Ask any running copy to shut down, and wait for it. Returns True if the way is
  clear — either nothing was running, or it has now exited. }
function StopRunningInstance(const ExePath: String): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  if not FileExists(ExePath) then
    Exit;

  { --quit returns only once the instance has really gone, and it is the only
    route that stops the sync engine over its API so it flushes first. It is
    scoped to this folder's data directory, so it cannot reach another copy. }
  if Exec(ExePath, '--quit', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    if ResultCode = 0 then
    begin
      Result := ExeIsFree(ExePath);
      if Result then
        Exit;
      Log('--quit succeeded but the executable is still locked');
    end
    else
      Log('--quit reported ' + IntToStr(ResultCode));

  ForceStopThisInstall(ExePath);
  Result := ExeIsFree(ExePath);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ExePath: String;
begin
  NeedsRestart := False;
  Result := '';
  ExePath := ExpandConstant('{app}\{#AppExe}');

  { Stopping before any file is replaced. Carrying on with the executable still
    locked would leave a half-updated folder — a new _internal beside an old
    exe — which is worse than not installing at all. }
  if not StopRunningInstance(ExePath) then
    Result := 'oXeioSync is still running and could not be closed.' + #13#10 +
              'Please exit it from its tray icon, then run this installer again.';
end;

{ True when oxeiosync.json points the database somewhere other than the data
  folder. The offer below promises to delete "your sync database"; if the
  database is elsewhere, that promise is false and the offer should not be made.
  Anything unparseable counts as "elsewhere" — the cautious answer. }
function DatabaseLivesElsewhere(const DataDir: String): Boolean;
var
  Config, Line: String;
  Lines: TArrayOfString;
  I: Integer;
begin
  Result := False;
  Config := DataDir + '\oxeiosync.json';
  if not FileExists(Config) then
    Exit;
  if not LoadStringsFromFile(Config, Lines) then
  begin
    Result := True;
    Exit;
  end;

  for I := 0 to GetArrayLength(Lines) - 1 do
  begin
    Line := Trim(Lines[I]);
    if Pos('"syncthing_home"', Line) = 1 then
      { An empty string is the default, meaning "keep it in the data folder". }
      Result := Pos('""', Line) = 0;
  end;
end;

function InitializeUninstall(): Boolean;
var
  ExePath: String;
begin
  DataFolderPreexisted := DirExists(ExpandConstant('{localappdata}\{#AppName}'));
  ExePath := ExpandConstant('{app}\{#AppExe}');
  Result := StopRunningInstance(ExePath);
  if not Result then
    MsgBox('oXeioSync is still running and could not be closed.' + #13#10 +
           'Please exit it from its tray icon, then uninstall again.',
           mbError, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir, Prompt, Blank: String;
begin
  Blank := #13#10 + #13#10;

  if CurUninstallStep <> usPostUninstall then
    Exit;

  { The application writes this itself when the user enables start-on-login;
    leaving it behind would point Windows at an executable that is now gone. }
  if RegValueExists(HKEY_CURRENT_USER, RunKey, '{#AppName}') then
    RegDeleteValue(HKEY_CURRENT_USER, RunKey, '{#AppName}');

  DataDir := ExpandConstant('{localappdata}\{#AppName}');
  if not DirExists(DataDir) then
    Exit;

  { An unattended uninstall has nobody to ask, and the safe answer to an
    unasked question about someone's data is to keep it. }
  if UninstallSilent then
  begin
    Log('Silent uninstall: leaving ' + DataDir + ' in place');
    Exit;
  end;

  if not DataFolderPreexisted then
  begin
    Log('Data folder appeared during this uninstall; nothing of the user''s in it');
    Exit;
  end;

  if DatabaseLivesElsewhere(DataDir) then
  begin
    Log('syncthing_home is overridden; not offering to delete ' + DataDir);
    Exit;
  end;

  { Default to No. This folder holds the device identity and the folder index:
    deleting it means every paired device has to be introduced again.

    Note: no line below may *begin* with '#', even indented — the preprocessor
    reads a leading '#' as one of its own directives. }
  Prompt := 'Also delete your settings and sync database?' + Blank +
            DataDir + Blank +
            'This holds this computer''s device identity and its record of ' +
            'what has been synced. Deleting it means re-pairing every device ' +
            'you sync with. Your actual files are not stored here and are ' +
            'never touched.' + Blank +
            'Keep it if you might reinstall.';

  if MsgBox(Prompt, mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    DelTree(DataDir, True, True, True);
end;
