#define AppVersion "2.0.2"
#define AppName "MOOC 下载器"
[Setup]
AppId={{A72F26C5-2A3F-47F9-96B8-B282D2C44F75}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=MOOC Downloader
DefaultDirName={localappdata}\Programs\MOOCDownloader
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\release
OutputBaseFilename=MOOCDownloader-Setup-{#AppVersion}-x64
Compression=lzma2/ultra64
SolidCompression=yes
LZMANumBlockThreads=2
WizardStyle=modern
SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\MOOCDownloader.exe
DisableProgramGroupPage=yes
CloseApplications=yes
RestartApplications=no
InfoBeforeFile=..\installer\installation-notes.txt

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Default.isl,ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Files]
Source: "..\dist\MOOCDownloader\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\MOOCDownloader.exe"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\MOOCDownloader.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\MOOCDownloader.exe"; Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent
