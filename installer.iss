; Inno Setup script for the Emotes Downloader setup .exe
; Built by GitHub Actions:  iscc /DAppVersion=1.1.0 installer.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Emotes Downloader"
#define AppExe "Emotes Downloader.exe"

[Setup]
AppId={{8F3C2A6E-5B1D-4C7A-9E2F-6A1B3C4D5E70}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=ISdook
AppPublisherURL=https://github.com/ISdook/emotes-downloader
AppSupportURL=https://github.com/ISdook/emotes-downloader/issues
AppUpdatesURL=https://github.com/ISdook/emotes-downloader/releases
VersionInfoVersion={#AppVersion}
; Installs for the current user only: no admin prompt, and the app can update itself.
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
OutputDir=dist
OutputBaseFilename=Emotes-Downloader-Setup
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; clear the old build's libraries so updates never mix versions
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "dist\app\{#AppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; also runs after silent auto-updates, so the app comes back by itself
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall
