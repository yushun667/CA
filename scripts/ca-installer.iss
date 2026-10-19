#ifndef BundleDir
  #error BundleDir must point to a native PyInstaller onedir bundle
#endif
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist\installers"
#endif
#ifndef OutputBase
  #define OutputBase "ca-" + AppVersion + "-windows-x64-setup"
#endif

[Setup]
AppId={{8A3BA45F-7009-49AF-9C90-C3D8F8A5AFCE}
AppName=CA
AppVersion={#AppVersion}
AppPublisher=CA contributors
AppPublisherURL=https://github.com/yushun667/CA
DefaultDirName={localappdata}\Programs\CA
DefaultGroupName=CA
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBase}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ChangesEnvironment=yes
UninstallDisplayIcon={app}\ca.exe
CloseApplications=yes

[Tasks]
Name: "addtopath"; Description: "Add CA to your user PATH (command line and MCP)"; Flags: checkedonce
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\CA"; Filename: "{app}\ca.exe"; Parameters: "desktop"; Flags: runminimized
Name: "{autodesktop}\CA"; Filename: "{app}\ca.exe"; Parameters: "desktop"; Tasks: desktopicon; Flags: runminimized

[Run]
Filename: "{app}\ca.exe"; Parameters: "desktop"; Description: "Open CA in your browser"; Flags: postinstall nowait skipifsilent runminimized

[Code]
const
  EnvironmentKey = 'Environment';
  InstallerKey = 'Software\CA\Installer';

function HasPathEntry(Value, Entry: String): Boolean;
var
  Remaining, Token: String;
  Separator: Integer;
begin
  Result := False;
  Remaining := Value + ';';
  while Length(Remaining) > 0 do begin
    Separator := Pos(';', Remaining);
    Token := Trim(Copy(Remaining, 1, Separator - 1));
    Delete(Remaining, 1, Separator);
    if CompareText(RemoveBackslashUnlessRoot(Token), RemoveBackslashUnlessRoot(Entry)) = 0 then begin
      Result := True;
      Exit;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Value, Entry: String;
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addtopath') then begin
    Entry := ExpandConstant('{app}');
    RegQueryStringValue(HKCU, EnvironmentKey, 'Path', Value);
    if not HasPathEntry(Value, Entry) then begin
      if (Value <> '') and (Copy(Value, Length(Value), 1) <> ';') then Value := Value + ';';
      if not RegWriteExpandStringValue(HKCU, EnvironmentKey, 'Path', Value + Entry) then
        RaiseException('Unable to update your user PATH.');
      RegWriteStringValue(HKCU, InstallerKey, 'PathAdded', Entry);
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Value, Entry, Remaining, Token, Updated: String;
  Separator: Integer;
begin
  if (CurUninstallStep = usPostUninstall) and
      RegQueryStringValue(HKCU, InstallerKey, 'PathAdded', Entry) then begin
    if RegQueryStringValue(HKCU, EnvironmentKey, 'Path', Value) then begin
      Remaining := Value + ';';
      Updated := '';
      while Length(Remaining) > 0 do begin
        Separator := Pos(';', Remaining);
        Token := Copy(Remaining, 1, Separator - 1);
        Delete(Remaining, 1, Separator);
        if CompareText(RemoveBackslashUnlessRoot(Trim(Token)), RemoveBackslashUnlessRoot(Entry)) <> 0 then begin
          if Updated <> '' then Updated := Updated + ';';
          Updated := Updated + Token;
        end;
      end;
      RegWriteExpandStringValue(HKCU, EnvironmentKey, 'Path', Updated);
    end;
    RegDeleteValue(HKCU, InstallerKey, 'PathAdded');
    RegDeleteKeyIfEmpty(HKCU, InstallerKey);
    RegDeleteKeyIfEmpty(HKCU, 'Software\CA');
  end;
end;
