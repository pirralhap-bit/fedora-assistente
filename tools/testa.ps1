# Autotestes OFFLINE de cada modulo. Uso: .\tools\testa.ps1   (sai com codigo != 0 se algum falhar)
# Fora de proposito: `atalhos.py` completo e `tidy.py --gemini` chamam o Gemini de verdade.
$raiz = Split-Path $PSScriptRoot -Parent
Set-Location $raiz
$py = Join-Path $raiz ".venv\Scripts\python.exe"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$testes = @(
    @("conversa", @("conversa.py")),
    @("runtime", @("runtime.py")),
    @("motor_agy", @("motor_agy.py")),
    @("motor_regra", @("motor_regra.py")),
    @("agentes", @("agentes.py", "--teste")),
    @("despachante", @("despachante.py", "--teste")),
    @("server", @("server.py", "--teste")),
    @("listener", @("listener.py")),
    @("speech", @("speech.py")),
    @("vad", @("vad.py")),
    @("llm", @("llm.py", "--teste")),
    @("notes", @("notes.py", "--teste")),
    @("tidy", @("tidy.py")),
    @("turnolog", @("turnolog.py", "--teste")),
    @("retro", @("retro.py", "--teste")),
    @("atalhos (filtro)", @("atalhos.py", "--teste-filtro")),
    @("whisper_server", @("whisper_server.py", "--teste")),
    @("orb", @("orb.py", "--teste")),
    @("memory", @("memory.py"))
)
$falhou = 0
foreach ($t in $testes) {
    $saida = & $py @($t[1]) 2>&1 | ForEach-Object { "$_" }
    if ($LASTEXITCODE -eq 0) { Write-Host ("ok     " + $t[0]) }
    else {
        Write-Host ("FALHOU " + $t[0])
        $saida | Select-Object -Last 8 | ForEach-Object { Write-Host ("       " + $_) }
        $falhou = 1
    }
}
exit $falhou
