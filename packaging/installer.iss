; installer.iss — 答题卡扫描识别 Windows 安装包（Inno Setup 6）
; 先运行 PyInstaller 生成 dist\答题卡扫描识别\，再编译本脚本:
;   ISCC.exe /DMyAppVersion=0.1.0 packaging\installer.iss
; 输出: dist\installer\答题卡扫描识别-<版本>-setup.exe

#define MyAppName "答题卡扫描识别"
#define MyAppExeName "答题卡扫描识别.exe"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{6F1C2B7E-3A4D-4E8B-9C5F-2D7A1B0E9F31}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=scan-answer
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename={#MyAppName}-{#MyAppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\{#MyAppExeName}
#if FileExists(AddBackslash(SourcePath) + "icon.ico")
SetupIconFile=icon.ico
#endif

[Languages]
; ChineseSimplified.isl 不在 Inno Setup 官方语言包内，由 CI 下载到 packaging\；缺失时退回英文
#if FileExists(AddBackslash(SourcePath) + "ChineseSimplified.isl")
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
#else
Name: "english"; MessagesFile: "compiler:Default.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\{#MyAppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
