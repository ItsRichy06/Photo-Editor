; Photo Editor installer script (Inno Setup).
;
; Bundles a self-contained Python runtime (the official embeddable
; distribution, not a venv referencing this machine's installed Python) with
; every dependency installed directly into it -- PySide6, rawpy, OpenCV,
; CUDA-enabled PyTorch + ultralytics/SAM2. This makes the install genuinely
; standalone: it doesn't require Python, or any specific Python, to already
; be on the target machine. An NVIDIA GPU + driver is used for AI-selection
; acceleration when present; the app runs fine without one (CPU fallback).
; The Visual C++ Redistributable is bundled too, since the compiled
; extensions (numpy/torch/opencv/PySide6) need it and a clean Windows
; install won't have it.

#define MyAppName "Photo Editor"
#define MyAppVersion "1.0"
#define MyAppPublisher "Photo Editor"

[Setup]
; A fresh AppId (was B7B9B9B4-...-1E9F6C2D7A11 under the old "Foto Editor"
; name) -- this is now a genuinely distinct product identity, not an
; in-place upgrade, so it gets its own GUID rather than reusing the old
; one and confusing Inno Setup's upgrade-detection logic.
AppId={{3E79FC5F-81EF-4F55-945F-229A4685B1FD}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\PhotoEditor
DefaultGroupName=Photo Editor
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\installer_output
OutputBaseFilename=PhotoEditor-Setup
SetupIconFile=..\assets\app_icon.ico
UninstallDisplayIcon={app}\assets\app_icon.ico
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall
Source: "..\main.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__,*.pyc"
Source: "..\assets\*"; DestDir: "{app}\assets"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\runtime_build\runtime\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__,*.pyc"

[Icons]
Name: "{group}\Photo Editor"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\main.py"""; WorkingDir: "{app}"; IconFilename: "{app}\assets\app_icon.ico"
Name: "{group}\Uninstall Photo Editor"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Photo Editor"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\main.py"""; WorkingDir: "{app}"; IconFilename: "{app}\assets\app_icon.ico"; Tasks: desktopicon

[Run]
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "Installing Visual C++ Redistributable..."; Flags: waituntilterminated
Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\main.py"""; WorkingDir: "{app}"; Description: "Launch Photo Editor now"; Flags: nowait postinstall skipifsilent
