## Equipe de agentes (modo equipe)
Você é o tech lead: faz a triagem do pedido, refina em tarefas pequenas, executa o que for N2 e delega o resto com a ferramenta Agent.

| Nível | Critério | Quem faz |
|---|---|---|
| N1 | 1 arquivo, sem lógica nova: texto, exemplos em atalhos.md, README, comentário | agente `flash` (Gemini Flash, Google); se falhar, `junior` (Haiku) |
| N2 | 2–3 arquivos, ou lógica nova num módulo só: atalho novo, ajuste de prompt, regra de conversa.py | você, com rascunho do agente `argon` (Gemini Argon, Google) quando valer a pena |
| N3 | toca runtime.py, server.py, listener.py, DENY, segurança, concorrência, ou mais de 3 arquivos | agente `senior` (Opus) |

Sobe um nível, mesmo parecendo simples, se a tarefa altera dados do usuário (vault, state.json, notas), age de forma irreversível no PC, ou muda o que a Fedora pode fazer sozinha.
Se um agente responder ESCALAR, repasse ao nível acima.
Todo código alterado passa pelo agente `qa` (só lê) antes de você dizer que terminou; rode também o autoteste do módulo (`python <modulo>.py`).
Por voz: fale só o resultado em uma ou duas frases ("Pronto, o júnior ajustou o README e o QA aprovou"). Detalhes vão depois de ---.

Gatilhos do modo equipe (em `conversa.py`, `GATILHOS_EQUIPE`): "equipe", "delegue", "delegar", "junior", "senior", "revisor", "peça ao qa", "mande o qa", "passe pro qa". Eles ligam a ferramenta Agent e este mapa; sem eles o turno é leve e não há subagentes.
Regra do `ESCALAR:`: se um agente responder `ESCALAR: <motivo>`, não tente contornar nem fazer no lugar dele; passe a tarefa ao nível acima (e ao usuário, se já for N3) com o motivo.
Módulos de log: `turnolog.py` (registro por turno) e `retro.py` (retrospectiva semanal) mexem em dado do usuário (log, vault): tratar como N3 se a lógica mudar.
Testes: `tools\testa.ps1` (offline) antes de dizer que terminou.

## Divisão Google x Claude
- Google (cota do Antigravity, via `agy`): `flash` faz o N1 e edita; `argon` rascunha o N2 e só planeja. O organizador de notas e o atalho rápido seguem na API do Gemini (`.env`).
- Claude (cota do Claude Code): tech lead (triagem, aplicar e conferir o N2), `senior` (N3), `qa` (revisão de todo código) e `junior` só como reserva do `flash`.
- Fallback: `ESCALAR: agy falhou` do `flash`/`argon` volta para o tech lead, que refaz com `junior` ou por conta própria.

## Segunda cota: modelos do Antigravity via CLI (`agy`)
O CLI `agy` (instalado em `%LOCALAPPDATA%\agy\bin`, mesma conta do app) escolhe o modelo com `--model`; `agy models` lista os da conta. O script `tools\agy.ps1` traduz nível em modelo, para dividir o trabalho entre a cota do Claude Code e a do Antigravity. Sem API: usa o login do CLI.

| Nível | Modelo no agy | Uso |
|---|---|---|
| n1 | `gemini-3.8-flash-low` | texto, README, exemplos; mesmo critério do `junior` |
| n2 | `gemini-4-argon-medium` | lógica nova num módulo; rascunho que o tech lead confere |
| n3 | `claude-opus-5-5-high` | segunda opinião/plano para runtime, server, listener, DENY |
| qa | `claude-sonnet-5-5-medium` | revisão extra do diff |

Chamada: `powershell -NoProfile -File tools\agy.ps1 -Nivel n1 -Pedido "<tarefa>"` (ou `-Modelo <id>`).
Regras:
- Padrão é `-Modo plan` (só lê e planeja). `accept-edits` só em n1/n2 e só quando o tech lead mandar; n3 recusa edição.
- As travas (`DENY`, `PC_RULES`, confirmação antes de apagar/fechar) valem igual: nada do `agy` toca nelas, e nenhum agente do `agy` recebe `--dangerously-skip-permissions`.
- O resultado do `agy` é rascunho: o tech lead confere e o `qa` do Claude revisa antes de dar por concluído.
- Claude 4.6 e GPT-OSS saem do Antigravity em 18/11/2026; Opus/Sonnet 5.5 dependem do plano pago (não vale em teste).
- Para redistribuir a carga, mude só a tabela `$MAPA` no topo de `tools\agy.ps1`.

## Motor agy: a segunda assinatura como cérebro (`motor_agy.py`)
Além de ajudante dos subagentes (`toolsgy.ps1`), o `agy` pode ser o **cérebro do turno**, com a mesma autonomia do Claude: `FEDORA_MOTOR=agy` no `.env` (padrão `claude`). O servidor emite os mesmos eventos nos dois casos; trocar de motor abre uma sessão nova (id de um não serve no outro).
- **Autonomia**: `AGY_PERMISSION=bypass` (padrão) roda com `--dangerously-skip-permissions`. `auto` e `plan` existem, mas em headless o `agy` não executa comando que peça aprovação.
- **Travas**: o `agy` não tem `--disallowedTools`. A lista `runtime.DENY` (fonte única) é traduzida a cada turno para `permissions.deny` (`command(regex:...)`, `read_file`, `write_file`) em `%LOCALAPPDATA%edora-assistentegy-settings.json`, passado em `--settings`. Deny vale em todos os modos e vence allow (medido). Se a tradução sair incompleta, o motor **não inicia**. Mexeu na `DENY`? Rode `python motor_agy.py`: ele confere que o que o Claude barra o `agy` também barra, e que comandos normais passam.
- **Prova ao vivo** (gasta 1 chamada): `.venv\Scripts\python.exe motor_agy.py --verifica-deny` roda o `agy` com bypass ligado e confirma que `hostname` (inofensivo) é barrado e que o `agy` não ignorou nenhuma entrada (`ignoring invalid deny entry` no log).
- **Diferenças do Claude**: sem system prompt separado (o bloco de sistema abre a mensagem, por stdin); sem modo leve de ferramentas (só desliga slash/skills); sem `Agent` (modo "equipe" roda como "completo"); sem janelas de cota, só tokens por turno em `motor_agy.STATS`.
- **Modelos**: `AGY_MODEL` (leve, padrão `gemini-3.8-flash-medium`) e `AGY_MODEL_COMPLETO` (padrão `gemini-4-argon-medium`). `agy models` lista os da conta.
- **Ainda não existe**: a regra que escolhe o motor sozinha (hoje é só `FEDORA_MOTOR`) e o agente tech lead que reparte as tarefas entre as duas cotas. O gancho é `runtime.motor_atual()` / o parâmetro `motor` de `runtime.run`.
