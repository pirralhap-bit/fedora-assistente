# Sobe o assistente e o STT (faster-whisper) no Windows. Uso: .\iniciar.ps1   (Ctrl+C encerra os dois)
# O STT recebe o PID do server.py (--pai) e sai sozinho se ele morrer, mesmo que esta janela seja fechada no X.
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
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
