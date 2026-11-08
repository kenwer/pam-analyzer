; Inno Setup script for the PAM Analyzer Windows installer.
; scripts/build.py compiles it with ISCC.exe and passes the values below as
; /D defines. The in-app updater runs the result with
; /VERYSILENT /SUPPRESSMSGBOXES /RELAUNCH right before the app exits.

#ifndef AppVersion
  #error Pass /DAppVersion=<version>
#endif
#ifndef NumericVersion
  #error Pass /DNumericVersion=<a.b.c.d>
#endif
#ifndef SourceDir
  #error Pass /DSourceDir=<the Nuitka .dist folder>
#endif
#ifndef OutputDir
  #error Pass /DOutputDir=<folder for the installer>
#endif
#ifndef OutputBaseFilename
  #error Pass /DOutputBaseFilename=<installer name without .exe>
#endif
#ifndef IconFile
  #error Pass /DIconFile=<app.ico>
#endif

[Setup]
; Never change the AppId: Windows identifies the installed app by it
AppId={{5AC44489-58BE-411D-82F4-ABC1648CD0E0}
AppName=PAM Analyzer
AppVersion={#AppVersion}
AppPublisher=de.ken
AppPublisherURL=https://github.com/kenwer/pam-analyzer
VersionInfoVersion={#NumericVersion}
; Per user: no admin prompt to install or to update, and {autopf} resolves
; to %LOCALAPPDATA%\Programs
PrivilegesRequired=lowest
DefaultDirName={autopf}\PAM-Analyzer
; Fixed, because [InstallDelete] empties it. A user-picked folder could
; hold other files.
DisableDirPage=yes
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\PAM-Analyzer.exe
SetupIconFile={#IconFile}
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseFilename}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; The bundled models barely compress, so maximum effort buys little time
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern

[InstallDelete]
; The folder holds only the app, so an update leaves exactly the new version
Type: filesandordirs; Name: "{app}\*"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\PAM Analyzer"; Filename: "{app}\PAM-Analyzer.exe"

[Run]
Filename: "{app}\PAM-Analyzer.exe"; Description: "Launch PAM Analyzer"; Flags: nowait postinstall skipifsilent
Filename: "{app}\PAM-Analyzer.exe"; Flags: nowait; Check: ShouldRelaunch

[Code]
const
  RunningMutex = 'PAMAnalyzerRunning';

function ShouldRelaunch: Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/RELAUNCH') = 0 then
      Result := True;
end;

function InitializeSetup: Boolean;
var
  Tries: Integer;
begin
  if WizardSilent then
  begin
    { The updater starts Setup while the app is still shutting down }
    Tries := 0;
    while CheckForMutexes(RunningMutex) and (Tries < 120) do
    begin
      Sleep(500);
      Tries := Tries + 1;
    end;
  end
  else
    while CheckForMutexes(RunningMutex) do
      if MsgBox('PAM Analyzer is running. Close it, then select Retry.', mbError, MB_RETRYCANCEL) = IDCANCEL then
        Break;
  Result := not CheckForMutexes(RunningMutex);
  if not Result then
    Log('PAM Analyzer is still running, Setup stops');
end;

function InitializeUninstall: Boolean;
begin
  Result := not CheckForMutexes(RunningMutex);
  if not Result then
    MsgBox('PAM Analyzer is running. Close it, then uninstall again.', mbError, MB_OK);
end;
