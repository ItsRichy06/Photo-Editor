; Photo Editor "update only" installer: app code and assets, NOT the bundled
; Python runtime (~4.8 GB, the bulk of PhotoEditor.iss's 2.7 GB installer).
; Most releases only change app code -- the runtime last changed in 1.14.0 --
; so this turns a 2.7 GB update into a few MB.
;
; It is only safe on top of the exact runtime it was built against, so it is
; built with that runtime's fingerprint (installer\runtime_id.py, written into
; the runtime as runtime_id.txt and installed by the full installer) and
; refuses to install unless the installed copy of runtime_id.txt matches.
; Worst case it declines; it never puts new code on an old runtime.
;
; Built by installer\build.ps1, which passes both values:
;   ISCC /DMyAppVersion=1.18.0 /DRuntimeId=<id> PhotoEditor-Update.iss

#define MyAppName "Photo Editor"
#define MyAppPublisher "Photo Editor"
#ifndef MyAppVersion
  #error Pass /DMyAppVersion=<version> (installer\build.ps1 does)
#endif
#ifndef RuntimeId
  #error Pass /DRuntimeId=<id> from installer\runtime_id.py (installer\build.ps1 does)
#endif
#ifndef AppIdValue
  ; Overridable only so tests can install a copy without touching the real
  ; install's Windows "Installed apps" entry.
  #define AppIdValue "{{3E79FC5F-81EF-4F55-945F-229A4685B1FD}"
#endif

[Setup]
; Same AppId as PhotoEditor.iss: this is the same product. Inno Setup appends
; to the existing uninstall log, so uninstalling still removes everything the
; full installer put down, runtime included.
AppId={#AppIdValue}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
; Stamped into the .exe so installer\publish.ps1 can refuse a stale build.
VersionInfoVersion={#MyAppVersion}
VersionInfoProductTextVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\PhotoEditor
UsePreviousAppDir=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\installer_output
OutputBaseFilename=PhotoEditor-Update
SetupIconFile=..\assets\app_icon.ico
UninstallDisplayIcon={app}\assets\app_icon.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; Keep in sync with the non-runtime entries in PhotoEditor.iss.
Source: "..\main.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__,*.pyc"
Source: "..\assets\*"; DestDir: "{app}\assets"; Flags: ignoreversion recursesubdirs createallsubdirs

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\main.py"""; WorkingDir: "{app}"; Description: "Launch Photo Editor now"; Flags: nowait postinstall skipifsilent

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  IdFile: String;
  Installed: AnsiString;
begin
  Result := '';
  IdFile := ExpandConstant('{app}\runtime\runtime_id.txt');
  if not LoadStringFromFile(IdFile, Installed) then
    Result := 'This is a small update that only works on top of an existing Photo Editor install, '
      + 'and none with a matching runtime was found (' + IdFile + ' is missing). '
      + 'Please download and run the full Photo Editor installer instead.'
  else if Trim(String(Installed)) <> '{#RuntimeId}' then
    Result := 'This small update was built for a different version of Photo Editor''s bundled runtime '
      + '(installed: ' + Trim(String(Installed)) + ', needed: {#RuntimeId}). '
      + 'Please download and run the full Photo Editor installer instead.';
end;
