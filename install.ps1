# Techne installer: the extended SolidWorks MCP server + the Techne add-in.
#
# Run from anywhere (it elevates itself; the add-in registration needs admin):
#   powershell -ExecutionPolicy Bypass -File "<this folder>\install.ps1"
#
# What it does
#   1. Finds SolidWorks and builds addin\Techne\*.cs with the C# compiler that ships with
#      Windows (.NET Framework 4.x): no Visual Studio needed.
#   2. Registers the add-in (RegAsm /codebase) and turns it on at SolidWorks startup.
#   3. Installs uv if missing, points every Claude Desktop config at the wheel in this folder
#      (adds a "solidworks" server, or replaces an older one; a backup of each config is
#      kept next to it) and pre-installs the server with uvx.
# Keep this folder where it is: Claude Desktop runs the wheel from here.
# Then restart SolidWorks and Claude Desktop.
#
# Options: -SkipAddin (server only), -SkipMcp (add-in only), -NoPause. Output is also written
# to install.log next to this script.

param([switch]$SkipAddin, [switch]$SkipMcp, [switch]$NoPause)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Version = ([regex]::Match((Get-Content (Join-Path $Root "src\solidworks_mcp\__init__.py") -Raw), '__version__\s*=\s*"([^"]+)"')).Groups[1].Value
$Wheel = Join-Path $Root "solidworks_mcp-$Version-py3-none-any.whl"

function Say($text, $color = "Gray") { Write-Host $text -ForegroundColor $color }
function Done { if (-not $NoPause) { Read-Host "Press Enter to close" | Out-Null } }

# ---------------------------------------------------------------- elevate for the add-in
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $SkipAddin -and -not $admin) {
    Say "Asking for administrator rights to register the add-in..." "Yellow"
    $relaunch = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
    if ($SkipMcp) { $relaunch += "-SkipMcp" }
    if ($NoPause) { $relaunch += "-NoPause" }
    Start-Process powershell -Verb RunAs -ArgumentList $relaunch
    exit
}

$log = Join-Path $Root "install.log"
try { Start-Transcript -Path $log -Force | Out-Null } catch { }
Say "Techne installer" "Cyan"
Say "Folder: $Root"

# ---------------------------------------------------------------- 1. add-in
if (-not $SkipAddin) {
    Say "`n[1/3] Building the Techne add-in" "Cyan"
    $redist = $null
    foreach ($key in Get-ChildItem "HKLM:\SOFTWARE\SolidWorks" -ErrorAction SilentlyContinue) {
        $setup = Join-Path $key.PSPath "Setup"
        $folder = (Get-ItemProperty $setup -Name "SolidWorks Folder" -ErrorAction SilentlyContinue)."SolidWorks Folder"
        if ($folder -and (Test-Path (Join-Path $folder "api\redist\SolidWorks.Interop.sldworks.dll"))) {
            $redist = Join-Path $folder "api\redist"
        }
    }
    if (-not $redist) {
        $found = Get-ChildItem "C:\Program Files\SOLIDWORKS Corp\*\api\redist\SolidWorks.Interop.sldworks.dll" -ErrorAction SilentlyContinue |
                 Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($found) { $redist = $found.DirectoryName }
    }
    if (-not $redist) { throw "Could not find SolidWorks' api\redist folder. Is SolidWorks installed?" }
    Say "SolidWorks API: $redist"

    $fw = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319"
    $csc = Join-Path $fw "csc.exe"
    $regasm = Join-Path $fw "RegAsm.exe"
    if (-not (Test-Path $csc)) { throw "The .NET Framework 4 C# compiler is missing ($csc)." }

    $bin = Join-Path $Root "addin\bin"
    New-Item -ItemType Directory -Force -Path $bin | Out-Null
    $dll = Join-Path $bin "Techne.dll"
    if (Test-Path $dll) {
        try { [IO.File]::OpenWrite($dll).Close() }
        catch { throw "Techne.dll is in use: close SolidWorks and run the installer again." }
    }
    $sources = Get-ChildItem (Join-Path $Root "addin\Techne\*.cs") | ForEach-Object { "`"$($_.FullName)`"" }
    $refs = @("sldworks", "swconst", "swpublished") | ForEach-Object { "/reference:`"$redist\SolidWorks.Interop.$_.dll`"" }
    $cscArgs = @("/nologo", "/target:library", "/platform:x64", "/optimize+", "/out:`"$dll`"") + $refs + $sources
    $p = Start-Process -FilePath $csc -ArgumentList $cscArgs -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput (Join-Path $bin "build.log")
    Get-Content (Join-Path $bin "build.log") | ForEach-Object { Say "  $_" }
    if ($p.ExitCode -ne 0) { throw "The add-in did not compile (see addin\bin\build.log)." }
    Say "Built $dll" "Green"

    Say "`n[2/3] Registering the add-in" "Cyan"
    # SolidWorks resolves the interop assemblies from the GAC; copy them beside the
    # add-in too, for installs that did not register them there.
    foreach ($name in "sldworks", "swconst", "swpublished") {
        Copy-Item "$redist\SolidWorks.Interop.$name.dll" $bin -Force
    }
    & $regasm /codebase "$dll" | ForEach-Object { Say "  $_" }
    if ($LASTEXITCODE -ne 0) { throw "RegAsm failed." }
    Say "Techne add-in registered; it loads with SolidWorks (Tools > Add-Ins shows 'Techne')." "Green"
}

# ---------------------------------------------------------------- 2. MCP server
if (-not $SkipMcp) {
    Say "`n[3/3] Installing the MCP server $Version" "Cyan"
    # uv runs the server (and brings its own Python); installed for this user if missing
    $uvBin = Join-Path $env:USERPROFILE ".local\bin"
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if (-not $uv -and (Test-Path (Join-Path $uvBin "uv.exe"))) { $uv = Join-Path $uvBin "uv.exe" }
    if (-not $uv) {
        Say "Installing uv from astral.sh (it runs the server and its Python)..."
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
        $uv = Join-Path $uvBin "uv.exe"
        if (-not (Test-Path $uv)) { throw "uv did not install. Install it from https://docs.astral.sh/uv/ and run this again." }
    }
    $uvx = Join-Path (Split-Path $uv) "uvx.exe"
    Say "uv: $uv"
    # build the wheel from src\ (a fresh clone has none; a rebuild picks up your edits)
    & $uv run --no-project python (Join-Path $Root "build_wheel.py") | ForEach-Object { Say "  $_" }
    if (-not (Test-Path $Wheel)) { throw "build_wheel.py did not produce $Wheel" }
    # every Claude Desktop config gets the server (added, or an older solidworks entry replaced)
    & $uv run --no-project python (Join-Path $Root "claude_config.py") install --wheel "$Wheel" --command "$uvx" |
        ForEach-Object { Say "  $_" "Green" }
    if ($LASTEXITCODE -ne 0) { throw "Could not update the Claude Desktop config." }
    Say "Pre-installing the server and its packages (pywin32, mcp, comtypes)..."
    & $uvx --refresh-package solidworks-mcp --from "$Wheel" solidworks-mcp --help | Out-Null
    if ($LASTEXITCODE -eq 0) { Say "Server ready." "Green" } else { Say "uvx could not start the server; Claude will retry on launch." "Yellow" }
}

Say "`nDone. Restart SolidWorks and Claude Desktop to load everything." "Cyan"
Say "In SolidWorks, Tools > Add-Ins should list 'Techne' (on at startup); turn on SOLIDWORKS Simulation for the sim/topology tools."
try { Stop-Transcript | Out-Null } catch { }
Done
