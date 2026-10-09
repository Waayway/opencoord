; Inno Setup script for OpenCoord (Windows x64 installer).
;
;   iscc /DAppVersion=0.1.0 packaging\windows\opencoord.iss
;
; packaging/build.py --installer passes AppVersion, BundleDir (the PyInstaller onedir) and
; OutputDir. Relative defaults below assume the script is compiled from the repo checkout.

#ifndef AppVersion
  #error AppVersion is required: iscc /DAppVersion=X.Y.Z opencoord.iss
#endif
#ifndef BundleDir
  #define BundleDir "..\..\dist\pyinstaller\OpenCoord"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif

#define AppName "OpenCoord"
#define AppExe "OpenCoord.exe"
#define AppProgId "OpenCoord.Session"

[Setup]
AppId={{6F0C5B2E-3A1D-4C8E-9B7A-2D4E8F1A0C35}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Waayway
AppPublisherURL=https://github.com/Waayway/opencoord
AppSupportURL=https://github.com/Waayway/opencoord/issues
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesAssociations=yes
LicenseFile=..\..\LICENSE
SetupIconFile=..\icons\opencoord.ico
UninstallDisplayIcon={app}\{#AppExe}
OutputDir={#OutputDir}
OutputBaseFilename={#AppName}-{#AppVersion}-win64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[INI]
; Internet shortcut to the RF Explorer's USB-serial (CP210x) driver download page.
Filename: "{app}\cp210x-driver.url"; Section: "InternetShortcut"; Key: "URL"; String: "https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\CP210x USB driver (Silicon Labs)"; Filename: "{app}\cp210x-driver.url"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; .opencoord session files open in OpenCoord.
Root: HKA; Subkey: "Software\Classes\.opencoord"; ValueType: string; ValueName: ""; ValueData: "{#AppProgId}"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\{#AppProgId}"; ValueType: string; ValueName: ""; ValueData: "OpenCoord session"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\{#AppProgId}\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExe},0"
Root: HKA; Subkey: "Software\Classes\{#AppProgId}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""

[UninstallDelete]
Type: files; Name: "{app}\cp210x-driver.url"

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
