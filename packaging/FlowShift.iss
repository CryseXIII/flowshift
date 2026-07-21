#ifndef MyAppVersion
  #error MyAppVersion must be supplied by build_release.ps1
#endif
#ifndef SourceRoot
  #error SourceRoot must be supplied by build_release.ps1
#endif
#ifndef OutputDir
  #error OutputDir must be supplied by build_release.ps1
#endif

[Setup]
AppId={{D3DC7554-DF12-4B7E-A658-B511F933D228}
AppName=FlowShift
AppVersion={#MyAppVersion}
AppPublisher=FlowShift
VersionInfoVersion={#MyAppVersion}
VersionInfoProductName=FlowShift
VersionInfoDescription=FlowShift Setup
DefaultDirName={autopf}\FlowShift
CreateAppDir=no
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=FlowShift-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
Uninstallable=no
CreateUninstallRegKey=no
SetupLogging=yes

[Files]
Source: "{#SourceRoot}\*"; DestDir: "{tmp}\FlowShift"; Flags: ignoreversion recursesubdirs createallsubdirs

[Code]
function HasCommandLineParameter(const Value: String): Boolean;
var
  Index: Integer;
begin
  Result := False;
  for Index := 1 to ParamCount do
  begin
    if CompareText(ParamStr(Index), Value) = 0 then
    begin
      Result := True;
      Exit;
    end;
  end;
end;

procedure RunPowerShellInstaller(const ScriptName, ExtraArguments, StatusText: String);
var
  PowerShellPath: String;
  ScriptPath: String;
  Parameters: String;
  ResultCode: Integer;
  Started: Boolean;
begin
  WizardForm.StatusLabel.Caption := StatusText;
  PowerShellPath := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  ScriptPath := ExpandConstant('{tmp}\FlowShift\') + ScriptName;
  Parameters := '-NoProfile -ExecutionPolicy Bypass -File "' + ScriptPath +
    '" -Elevated -NonInteractive ' + ExtraArguments;
  Started := Exec(PowerShellPath, Parameters, ExpandConstant('{tmp}\FlowShift'),
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if not Started then
    RaiseException('Could not start ' + ScriptName);
  if ResultCode <> 0 then
    RaiseException(ScriptName + ' failed with exit code ' + IntToStr(ResultCode));
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  UpdateArgument: String;
begin
  if CurStep <> ssPostInstall then
    Exit;

  UpdateArgument := '';
  if HasCommandLineParameter('/FLOWUPDATE') then
    UpdateArgument := '-FlowUpdate';

  RunPowerShellInstaller('install_flowshift.ps1', UpdateArgument,
    'Installing the FlowShift runtime...');
  RunPowerShellInstaller('install_webgui.ps1', '-UsePrebuilt ' + UpdateArgument,
    'Installing the FlowShift WebGUI...');
end;
