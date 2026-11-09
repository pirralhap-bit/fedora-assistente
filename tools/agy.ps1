# Chama o CLI do Antigravity (agy) com o modelo certo para cada nível da equipe.
# Uso:  powershell -NoProfile -File tools\agy.ps1 -Nivel n1 -Pedido "texto"
#       powershell -NoProfile -File tools\agy.ps1 -Modelo claude-opus-5-5-high -Pedido "texto"
# Lista de modelos da conta: agy models
param(
    [ValidateSet('n1', 'n2', 'n3', 'qa')] [string]$Nivel = 'n1',
    [string]$Modelo = '',
    [Parameter(Mandatory = $true)] [string]$Pedido,
    # plan = só lê e planeja (padrão, seguro); accept-edits = pode editar arquivos
    [ValidateSet('plan', 'accept-edits')] [string]$Modo = 'plan',
    [int]$TimeoutSeg = 300
)

# Nível -> modelo (ajuste aqui para redistribuir o trabalho entre as cotas)
$MAPA = @{
    n1 = 'gemini-3.8-flash-low'      # mecânico, barato
    n2 = 'gemini-4-argon-medium'     # lógica nova num módulo
    n3 = 'claude-opus-5-5-high'      # runtime/server/listener/DENY: só planejar, nunca editar
    qa = 'claude-sonnet-5-5-medium'  # revisão
}

$agy = Join-Path $env:LOCALAPPDATA 'agy\bin\agy.exe'
if (-not (Test-Path $agy)) { Write-Error "agy não encontrado em $agy"; exit 2 }

if ($Modelo -eq '') { $Modelo = $MAPA[$Nivel] }
# Segurança: o nível n3 nunca roda com permissão de editar por aqui
if ($Nivel -eq 'n3' -and $Modo -ne 'plan') { Write-Error 'n3 só roda em modo plan'; exit 3 }

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
& $agy --model $Modelo --mode $Modo --print-timeout "${TimeoutSeg}s" -p $Pedido
exit $LASTEXITCODE
