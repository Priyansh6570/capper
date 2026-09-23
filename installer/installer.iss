; ============================================================================
; ReCapper - Windows installer (Inno Setup 6)
;
; Hybrid installer: bundles a static ffmpeg build, then (as post-install
; steps, in order) 1) ensures git is present (winget install if missing) and
; `git clone`s the app code from GitHub over HTTPS - a real git working tree,
; not a plain file copy, so the in-app "Check for updates" button can later
; `git pull` it - then 2) runs setup.bat to build the Python 3.13 + CUDA-torch
; + Chatterbox environment (GPU/driver-specific, can't be pre-bundled).
; Chatterbox's ~4GB model weights are NOT downloaded here; they download on
; the app's first real use (see SETUP_GUIDE.md / work_log.md for what that
; needs in the app itself).
;
; Build with: "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
; See INSTALLER_BUILD.md (next to this file) for the full build/compile guide.
; ============================================================================

#define MyAppName "ReCapper"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "ReCapper"
#define MyAppExeName "start.bat"
#define ProjectRoot "..\"
#define RepoURL "https://github.com/Priyansh6570/capper.git"
#define RepoBranch "main"

[Setup]
AppId={{81CAE141-D97D-4AB5-B39A-6445C8237F11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\ReCapper
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; No admin required: everything installs per-user (venv, ffmpeg, Python via
; "winget install --scope user") so a non-technical user never sees a UAC
; prompt. This also means the default install location is per-user, though
; the user can Browse to any folder/drive they like on the next page.
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=output
OutputBaseFilename=ReCapper_Setup
Compression=lzma2
SolidCompression=yes
SetupIconFile={#ProjectRoot}assets\icon.ico
UninstallDisplayIcon={app}\assets\icon.ico
WizardStyle=modern
; The real space requirement (bundle + post-install downloads) is computed
; and checked ourselves in [Code] (see CheckDiskSpace) - it's far larger than
; what Inno would compute from the bundled [Files] alone.
ChangesEnvironment=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; App code is NOT bundled here - it's `git clone`d fresh as a post-install
; step (see CloneRepo in [Code]) so it's a real working tree the in-app
; "Check for updates" button can `git pull`. CloneRepo clones into a {tmp}
; staging folder first (git clone needs an empty target; {app} already has
; Inno's own unins000.exe/.dat in it by this point) and merges it into {app}
; with robocopy, which has no such requirement - so the static ffmpeg build
; below can still go straight into {app} as normal; the merge leaves it alone
; since it isn't part of the cloned repo. setup.bat looks for ffmpeg at
; exactly this path and skips its own download step when it's already there.
Source: "{#ProjectRoot}vendor\ffmpeg\bin\*"; DestDir: "{app}\vendor\ffmpeg\bin"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\assets\icon.ico"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\assets\icon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent shellexec; Check: EnvSetupOKCheck

[Code]
var
  CloneOK: Boolean;      // did `git clone` (+ the robocopy merge) succeed?
  EnvSetupOK: Boolean;   // did setup.bat's env-setup steps (Python/venv/packages/ffmpeg) succeed?
  CudaOK: Boolean;       // independent post-check: is torch.cuda.is_available() actually True?

// ----------------------------------------------------------------------------
// Disk space check - Inno only knows about the bundled [Files]; the real
// requirement also includes what setup.bat downloads after install (CUDA
// torch ~2.5GB, Chatterbox weights ~4GB on first app run). We check for a
// conservative ~10GB total on whichever drive the user picks, and let them
// Browse to a different one if there isn't enough.
// ----------------------------------------------------------------------------
function GetDiskFreeSpaceEx(lpDirectoryName: string;
  var lpFreeBytesAvailable, lpTotalNumberOfBytes, lpTotalNumberOfFreeBytes: Int64): BOOL;
  external 'GetDiskFreeSpaceExW@kernel32.dll stdcall';

const
  RequiredGB = 10;

// Belt-and-suspenders silent-mode check: parse our OWN command line directly
// instead of trusting WizardSilent()/UninstallSilent() alone. Every MsgBox
// call in this script (confirmations, errors, the finished-page note) MUST
// be guarded by this - a MsgBox is a real modal Win32 dialog that shows on
// screen and blocks regardless of /VERYSILENT, so an unguarded one left
// running unattended can sit there indefinitely, or get clicked through by
// whoever is at the keyboard without understanding what it's agreeing to
// (this cost a real ~4GB Hugging Face cache deletion during this script's
// own testing - see installer/INSTALLER_BUILD.md).
function IsSilentMode(): Boolean;
var
  I: Integer;
  P: String;
begin
  Result := False;
  for I := 1 to ParamCount do
  begin
    P := Uppercase(ParamStr(I));
    if (P = '/SILENT') or (P = '/VERYSILENT') then
    begin
      Result := True;
      Exit;
    end;
  end;
end;

function FormatGB(Bytes: Int64): String;
begin
  Result := Format('%.1f GB', [Bytes / 1024.0 / 1024.0 / 1024.0]);
end;

function CheckDiskSpace(): Boolean;
var
  Drive: String;
  FreeAvail, TotalBytes, TotalFree: Int64;
  RequiredBytes: Int64;
begin
  Result := True;
  Drive := ExtractFileDrive(WizardDirValue);
  if Drive = '' then
    Exit;
  RequiredBytes := Int64(RequiredGB) * 1024 * 1024 * 1024;
  if GetDiskFreeSpaceEx(Drive + '\', FreeAvail, TotalBytes, TotalFree) then
  begin
    if FreeAvail < RequiredBytes then
    begin
      // MsgBox would hang forever waiting for a click if this ever runs
      // unattended (/SILENT or /VERYSILENT) - just refuse without asking then.
      if not IsSilentMode() then
        MsgBox(
          'Not enough free space on ' + Drive + '\.' + #13#10#13#10 +
          'This app needs about ' + IntToStr(RequiredGB) + ' GB total, in stages:' + #13#10 +
          '  - The app itself (this installer)' + #13#10 +
          '  - A Python + CUDA PyTorch environment, set up right after this install (~2.5 GB)' + #13#10 +
          '  - An AI voice model, downloaded the first time you generate narration (~4 GB)' + #13#10#13#10 +
          'Free space on ' + Drive + '\ right now: ' + FormatGB(FreeAvail) + #13#10#13#10 +
          'Please free up space, or click Browse and choose a different drive.',
          mbError, MB_OK);
      Result := False;
    end;
  end;
  // if the API call itself fails, don't block install over it - just proceed.
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpSelectDir then
    Result := CheckDiskSpace();
end;

// ----------------------------------------------------------------------------
// git: find it (PATH, or the well-known Git for Windows install path in case
// PATH hasn't refreshed yet after installing it just now), installing via
// winget if it's missing entirely. Mirrors setup.bat's own Python-3.13
// detection pattern (see :find_python there) for consistency.
// ----------------------------------------------------------------------------
function FindGit(var GitExe: String): Boolean;
var
  ResultCode: Integer;
begin
  GitExe := '';
  if Exec(ExpandConstant('{cmd}'), '/c where git >nul 2>&1', '', SW_HIDE,
          ewWaitUntilTerminated, ResultCode) and (ResultCode = 0) then
  begin
    GitExe := 'git';
    Result := True;
    Exit;
  end;
  if FileExists(ExpandConstant('{pf}\Git\cmd\git.exe')) then
  begin
    GitExe := ExpandConstant('{pf}\Git\cmd\git.exe');
    Result := True;
    Exit;
  end;
  if FileExists(ExpandConstant('{localappdata}\Programs\Git\cmd\git.exe')) then
  begin
    GitExe := ExpandConstant('{localappdata}\Programs\Git\cmd\git.exe');
    Result := True;
    Exit;
  end;
  Result := False;
end;

function EnsureGit(var GitExe: String): Boolean;
var
  ResultCode: Integer;
begin
  if FindGit(GitExe) then
  begin
    Result := True;
    Exit;
  end;
  WizardForm.StatusLabel.Caption := 'Installing Git (needed to download the app)...';
  WizardForm.StatusLabel.Repaint;
  Exec(ExpandConstant('{cmd}'),
       '/c winget install -e --id Git.Git --scope user --silent ' +
       '--accept-package-agreements --accept-source-agreements',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := FindGit(GitExe);
end;

// ----------------------------------------------------------------------------
// Clone the app code as a real git working tree (not a file copy), so the
// in-app "Check for updates" button can `git pull` it later. `git clone`
// needs an empty target directory, but {app} already has Inno's own
// unins000.exe/.dat in it by this point in the install - so we clone into a
// {tmp} staging folder (always empty) and robocopy /E it into {app}
// afterward, which has no such requirement and leaves the ffmpeg files
// already there (from [Files]) untouched, since they aren't part of the
// cloned tree. Verified this exact clone-then-robocopy-merge sequence
// directly before writing it into this script - see work_log.md.
// ----------------------------------------------------------------------------
procedure CloneRepo();
var
  ResultCode: Integer;
  GitExe, StagingDir, SafeDirPath: String;
begin
  CloneOK := False;
  if not EnsureGit(GitExe) then
  begin
    if not IsSilentMode() then
      MsgBox(
        'Could not find or install Git, which is needed to download the app.' + #13#10#13#10 +
        'Please install Git yourself from https://git-scm.com/download/win ' +
        '(defaults are fine), then run this installer again.',
        mbError, MB_OK);
    Exit;
  end;

  StagingDir := ExpandConstant('{tmp}\repo_clone');
  WizardForm.StatusLabel.Caption := 'Downloading the app from GitHub...';
  WizardForm.StatusLabel.Repaint;

  if not Exec(GitExe,
              'clone --branch {#RepoBranch} --depth 1 "{#RepoURL}" "' + StagingDir + '"',
              '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
  begin
    if not IsSilentMode() then
      MsgBox(
        'Could not download the app from GitHub (git clone failed).' + #13#10#13#10 +
        'Check your internet connection and try running this installer again. ' +
        'If it keeps failing, the repository URL may have changed - see ' +
        'install_log.txt if one was written, or contact support.',
        mbError, MB_OK);
    Exit;
  end;

  // robocopy exit codes 0-7 are all success (bit flags for "files copied",
  // "extra files in dest", etc.); 8+ means a real failure.
  Exec(ExpandConstant('{sys}\robocopy.exe'),
       '"' + StagingDir + '" "' + ExpandConstant('{app}') + '" /E /NFL /NDL /NJH /NJS',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if ResultCode >= 8 then
  begin
    if not IsSilentMode() then
      MsgBox('Downloaded the app but could not move it into place (robocopy ' +
             'error ' + IntToStr(ResultCode) + '). Try running this installer again.',
             mbError, MB_OK);
    Exit;
  end;

  CloneOK := FileExists(ExpandConstant('{app}\setup.bat'));
  if not CloneOK and not IsSilentMode() then
    MsgBox('The app was downloaded, but setup.bat is missing from it - ' +
           'something is wrong with the repository. Contact support.',
           mbError, MB_OK);

  // Git flags a repo folder's ownership as "dubious" (and refuses to run any
  // command in it) whenever it looks unexpected - seen in testing on some
  // drives/install locations. The clone+robocopy-merge above can trigger
  // this, which would otherwise silently break the in-app "Check for
  // updates" button forever. Mark this exact folder trusted for this user
  // right now, once, so updates work from the first launch.
  if CloneOK then
  begin
    // StringChange mutates its first arg IN PLACE (var param, not a return
    // value) - it can't be called inline on an ExpandConstant() result,
    // hence the intermediate variable.
    SafeDirPath := ExpandConstant('{app}');
    StringChange(SafeDirPath, '\', '/');
    Exec(GitExe, 'config --global --add safe.directory "' + SafeDirPath + '"',
         '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;

// ----------------------------------------------------------------------------
// Post-install: run setup.bat (Python 3.13 -> venv -> requirements.txt ->
// CUDA torch override -> ffmpeg check -> verification) as a VISIBLE console
// window, so the user sees the exact same plain-English step-by-step output
// setup.bat already gives someone who double-clicks it by hand. This reuses
// setup.bat's own tested logic instead of re-implementing it in Pascal.
// SETUP_UNATTENDED=1 makes it skip its interactive "press any key"/"continue
// without a GPU?" prompts, which would otherwise hang with no one to answer
// them from inside an installer flow.
// ----------------------------------------------------------------------------
procedure RunEnvSetup();
var
  ResultCode: Integer;
  SetupBat, LogFile, InstallLog: String;
begin
  EnvSetupOK := False;
  CudaOK := False;
  if not CloneOK then Exit;   // no setup.bat to run if the clone itself failed
  SetupBat := ExpandConstant('{app}\setup.bat');
  LogFile := ExpandConstant('{app}\setup_log.txt');
  InstallLog := ExpandConstant('{app}\install_log.txt');

  WizardForm.StatusLabel.Caption :=
    'Setting up the Python + AI environment. A console window will open ' +
    'showing live progress - this can take 10-30+ minutes depending on your ' +
    'internet connection. Please don''t close that window.';
  WizardForm.StatusLabel.Repaint;

  if not Exec(ExpandConstant('{cmd}'),
              '/c set "SETUP_UNATTENDED=1" && "' + SetupBat + '"',
              ExpandConstant('{app}'), SW_SHOWNORMAL, ewWaitUntilTerminated, ResultCode) then
  begin
    if not IsSilentMode() then
      MsgBox('Could not launch setup.bat: ' + SysErrorMessage(ResultCode), mbError, MB_OK);
    Exit;
  end;

  if FileExists(LogFile) then
    CopyFile(LogFile, InstallLog, False);

  EnvSetupOK := (ResultCode = 0);

  // Independent check, per the request that we never claim GPU acceleration
  // works without actually re-verifying it ourselves: ask the venv directly,
  // regardless of what setup.bat itself reported.
  if FileExists(ExpandConstant('{app}\.venv\Scripts\python.exe')) then
  begin
    Exec(ExpandConstant('{app}\.venv\Scripts\python.exe'),
         '-c "import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)"',
         ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
    CudaOK := (ResultCode = 0);
  end;

  if not EnvSetupOK and not IsSilentMode() then
    MsgBox(
      'Setup finished copying the app, but the Python/AI environment could ' +
      'NOT be fully set up automatically.' + #13#10#13#10 +
      'Your files ARE installed at:' + #13#10 + ExpandConstant('{app}') + #13#10#13#10 +
      'Details were written to install_log.txt in that folder. To fix it: ' +
      'address whatever it says (a common cause is an old/missing NVIDIA ' +
      'driver, or no internet connection during setup), then double-click ' +
      'setup.bat there - it is safe to re-run and picks up where it left off.',
      mbError, MB_OK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    CloneRepo();
    RunEnvSetup();   // no-ops immediately if CloneOK is False
  end;
end;

function EnvSetupOKCheck(): Boolean;
begin
  Result := EnvSetupOK;
end;

// ----------------------------------------------------------------------------
// Finished page: never just say "Complete" if the environment setup failed,
// and always state the real GPU-acceleration status explicitly (CPU-only
// fallback is supported and works, but the user should know it's active).
// ----------------------------------------------------------------------------
procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then
  begin
    if not CloneOK then
      WizardForm.FinishedLabel.Caption :=
        'Setup could NOT download the app from GitHub - see the error shown ' +
        'earlier for why (usually an internet connection problem, or Git ' +
        'could not be installed).' + #13#10#13#10 +
        'Run this installer again once that''s fixed - nothing usable was ' +
        'installed yet.'
    else if not EnvSetupOK then
      WizardForm.FinishedLabel.Caption :=
        'The app downloaded correctly, but the Python/AI environment setup ' +
        'did NOT finish successfully.' + #13#10#13#10 +
        'See install_log.txt in the install folder for what went wrong, fix ' +
        'it, then double-click setup.bat there - it is safe to run again and ' +
        'will pick up where it left off.'
    else if not CudaOK then
      WizardForm.FinishedLabel.Caption :=
        WizardForm.FinishedLabel.Caption + #13#10#13#10 +
        'Note: GPU acceleration is NOT active on this PC - narration will ' +
        'generate correctly but much more slowly, on your CPU. See ' +
        'SETUP_GUIDE.md in the install folder if you have an NVIDIA GPU and ' +
        'expected this to be faster.'
    else
      WizardForm.FinishedLabel.Caption :=
        WizardForm.FinishedLabel.Caption + #13#10#13#10 +
        'GPU acceleration is active and ready.' + #13#10 +
        'Reminder: the first time you click "Generate audio + video" in the ' +
        'app, it downloads the AI voice model (~4 GB, one time only, needs ' +
        'internet) - watch the app for its own progress message during that.';
  end;
end;

// ----------------------------------------------------------------------------
// Uninstaller: Inno only auto-removes what it tracked in [Files]. The venv
// and the vendored ffmpeg were created/downloaded AFTER that (by setup.bat),
// so they're removed explicitly here - unconditionally, they're build
// artifacts with no user value. The Hugging Face model cache and the user's
// own app data (chapters, renders, downloads) are asked about separately,
// since those are expensive/irreplaceable to regenerate.
// ----------------------------------------------------------------------------
// App code is now a `git clone` (see CloneRepo), not an Inno-tracked [Files]
// copy, so Inno's own uninstall step has nothing to remove for it - this
// whole procedure is responsible for the entire {app} folder, not just the
// venv/ffmpeg extras it originally covered.
const
  DataFolderList = ',work,output,projects,downloads,inputs,series,';

function IsDataFolder(const Name: String): Boolean;
begin
  Result := Pos(',' + Lowercase(Name) + ',', DataFolderList) > 0;
end;

// Removes everything directly under AppDir EXCEPT the data folders above
// (and whatever the caller already deleted itself) - i.e. the app code,
// .git, .venv, vendor, logs, etc. Enumerated dynamically (not a hardcoded
// file list) so it doesn't need updating every time a file is added to the
// repo.
procedure RemoveAppCodeKeepingData(const AppDir: String);
var
  FindRec: TFindRec;
  FullPath: String;
begin
  if FindFirst(AppDir + '\*', FindRec) then
  begin
    try
      repeat
        if (FindRec.Name = '.') or (FindRec.Name = '..') then Continue;
        if IsDataFolder(FindRec.Name) then Continue;
        FullPath := AppDir + '\' + FindRec.Name;
        if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
          DelTree(FullPath, True, True, True)
        else
          DeleteFile(FullPath);
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  AppDir, HFHub, HFLocks: String;
  RemoveData: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    AppDir := ExpandConstant('{app}');

    // An unattended uninstall has no one to ask, and deleting multi-GB
    // downloads + the user's chapters/renders is NOT a safe default to
    // assume - so a silent uninstall leaves that data in place untouched.
    if IsSilentMode() then
      RemoveData := IDNO
    else
      RemoveData := MsgBox(
        'Also remove the downloaded AI voice model and your app data?' + #13#10#13#10 +
        'This permanently deletes:' + #13#10 +
        '  - The Chatterbox voice model (~4 GB) from your Hugging Face cache' + #13#10 +
        '  - Your chapters, boxed frames, and rendered videos (work, output, ' +
        'projects, downloads, inputs, series folders inside the app)' + #13#10#13#10 +
        'Choose No to keep your chapters/renders and the downloaded model ' +
        '(the app itself is removed either way).',
        mbConfirmation, MB_YESNO);

    if RemoveData = IDYES then
    begin
      DelTree(AppDir + '\work', True, True, True);
      DelTree(AppDir + '\output', True, True, True);
      DelTree(AppDir + '\projects', True, True, True);
      DelTree(AppDir + '\downloads', True, True, True);
      DelTree(AppDir + '\inputs', True, True, True);
      DelTree(AppDir + '\series', True, True, True);

      // Exact, known repo folder names only (verified against a real
      // Chatterbox download) - deliberately NOT a wildcard/whole-cache wipe,
      // so any unrelated Hugging Face downloads the user has from other
      // tools are left alone.
      HFHub := ExpandConstant('{%USERPROFILE}') + '\.cache\huggingface\hub';
      HFLocks := HFHub + '\.locks';
      DelTree(HFHub + '\models--ResembleAI--chatterbox', True, True, True);
      DelTree(HFHub + '\models--ResembleAI--chatterbox-turbo', True, True, True);
      DelTree(HFLocks + '\models--ResembleAI--chatterbox', True, True, True);
      DelTree(HFLocks + '\models--ResembleAI--chatterbox-turbo', True, True, True);

      // nothing left to preserve - remove the rest of the app folder too.
      RemoveAppCodeKeepingData(AppDir);
      RemoveDir(AppDir);
    end
    else
    begin
      // keep the data folders; remove everything else (code, .git, .venv,
      // vendor, logs - the "program", not the "content").
      RemoveAppCodeKeepingData(AppDir);
    end;

    if not IsSilentMode() then
      MsgBox(
        '{#MyAppName} has been removed.' + #13#10#13#10 +
        'Note: if this setup installed Python or Git via winget, they were ' +
        'left on your PC in case other apps use them. Remove them yourself ' +
        'from Windows Settings > Apps if you don''t need them.',
        mbInformation, MB_OK);
  end;
end;
