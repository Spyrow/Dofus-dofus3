# Lance l'interface du bot sur CE PC et la rend joignable depuis le téléphone via Tailscale (réseau privé, HTTPS, rien d'ouvert sur Internet).
# Première fois : demande le mot de passe admin et le garde (hachée) dans %APPDATA%\dofus_net\webui.env. Usage : powershell -File deploy\start_mobile.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$envFile = Join-Path $env:APPDATA "dofus_net\webui.env"
$ts = "C:\Program Files\Tailscale\tailscale.exe"

$st = & $ts status --json | ConvertFrom-Json
if ($st.BackendState -ne "Running") { throw "Tailscale n'est pas connecté (état : $($st.BackendState)). Lancez 'tailscale up', connectez-vous, puis relancez." }
$dns = $st.Self.DNSName.TrimEnd(".")
if (-not $dns) { throw "Pas de nom MagicDNS : activez MagicDNS et HTTPS dans la console admin Tailscale." }

if (-not (Test-Path $envFile)) {
    $pw = Read-Host "Mot de passe admin (12 caractères minimum)" -AsSecureString
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw))
    if ($plain.Length -lt 12) { throw "Mot de passe trop court" }
    $hash = $plain | python -I -c "import sys; sys.path.insert(0, r'$root\client\webui'); import auth; print(auth.hash_password(sys.stdin.readline().rstrip('\r\n')))"
    $secret = -join ((1..32) | ForEach-Object { '{0:x2}' -f (Get-Random -Maximum 256) })
    New-Item -ItemType Directory -Force (Split-Path $envFile) | Out-Null
    @("DOFUS_ADMIN_USER=admin", "DOFUS_ADMIN_PASSWORD_HASH=$hash", "DOFUS_SESSION_SECRET=$secret") | Set-Content -Encoding ascii $envFile
    icacls $envFile /inheritance:r /grant:r "$($env:USERNAME):F" | Out-Null
}
Get-Content $envFile | ForEach-Object { $k, $v = $_ -split "=", 2; Set-Item "env:$k" $v }
$env:DOFUS_PUBLIC_HOST = $dns

Write-Host 'Publication sur Tailscale... (si un lien login.tailscale.com apparait, ouvrez-le pour activer Serve, puis relancez)'
& $ts serve --bg --https=443 http://127.0.0.1:8765
Write-Host "Interface : https://$dns/  (identifiant : $env:DOFUS_ADMIN_USER)  -- Ctrl+C pour arrêter"
try { python (Join-Path $root "client\webui\server.py") --port 8765 } finally { & $ts serve reset | Out-Null }
