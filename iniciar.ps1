# Sobe o assistente e o STT (faster-whisper) no Windows. Uso: .\iniciar.ps1   (Ctrl+C encerra os dois)
# O STT recebe o PID do server.py (--pai) e sai sozinho se ele morrer, mesmo que esta janela seja fechada no X.
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

# Nao sobe duas vezes: se a porta do servidor ja esta escutando, avisa e sai.
$porta = $env:JARVIS_PORT
if (-not $porta) {
    $envFile = Join-Path $PSScriptRoot ".env"
    if (Test-Path $envFile) {
        $m = Select-String -Path $envFile -Pattern '^\s*JARVIS_PORT\s*=\s*(\d+)' | Select-Object -First 1
        if ($m) { $porta = $m.Matches[0].Groups[1].Value }
    }
}
if (-not $porta) { $porta = "8720" }
if (Get-NetTCPConnection -LocalPort ([int]$porta) -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "O assistente ja esta rodando (porta $porta). Nada a fazer." -ForegroundColor Yellow
    Start-Sleep -Seconds 3
    exit 0
}

$srv = Start-Process $py -ArgumentList "`"$(Join-Path $PSScriptRoot 'server.py')`"" -WorkingDirectory $PSScriptRoot -NoNewWindow -PassThru
$stt = Start-Process $py -ArgumentList "whisper_server.py", "--pai", $srv.Id -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
try {
    Wait-Process -Id $srv.Id
} finally {
    # o python.exe do venv e um lancador: o processo de verdade e filho dele, por isso /T (a arvore inteira)
    foreach ($p in @($srv, $stt)) {
        if ($p -and -not $p.HasExited) { taskkill.exe /T /F /PID $p.Id | Out-Null }
    }
}
