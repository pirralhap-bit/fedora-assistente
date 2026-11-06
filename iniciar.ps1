# Sobe o STT (faster-whisper) e o assistente no Windows. Uso: .\iniciar.ps1   (Ctrl+C encerra os dois)
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$stt = Start-Process $py -ArgumentList "whisper_server.py" -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
try {
    & $py (Join-Path $PSScriptRoot "server.py")
} finally {
    if ($stt -and -not $stt.HasExited) { Stop-Process -Id $stt.Id -Force }
}
