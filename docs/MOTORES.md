# Motores e agentes

Duas assinaturas respondem pela Fedora: **Claude** (Claude Code, `claude -p`) e **Google** (Antigravity, `agy`). Não é para o modelo ler (não entra no prompt); o que entra é `docs/EQUIPE.md`.

## Qual motor responde o turno (`motor_regra.py`)
`FEDORA_MOTOR=auto` (padrão): o Claude é o primário. O `agy` assume, e **fica até aquela janela do Claude resetar**, quando:
- o uso **semanal** (`seven_day`) passa de **55%** (`FEDORA_MOTOR_LIMITE`), ou
- o uso da **sessão de 5 h** (`five_hour`) chega a **90%** (`FEDORA_MOTOR_LIMITE_SESSAO`).
Os números são os que o próprio Claude informa a cada resposta. Com o `agy` no comando, **nada roda no Claude**, nem os agentes. Se as duas janelas estouram, espera até o reset da mais longa.
- O uso do `agy` não entra na regra.
- **Se o `agy` falhar durante a espera**: não há volta para o Claude (decisão do usuário, 09/11/2026; o `agy` está logado). O erro aparece no turno e o motivo no `/api/status`; para sair antes do reset, use `FEDORA_MOTOR=claude`.
- Sem dado do Claude (servidor recém-iniciado), `agy` não instalado ou arquivo de estado ilegível: fica no Claude.
- Reset sem horário válido (ausente, não numérico ou em milissegundos, acima de 1e11): segura o `agy` por 12 h. Nenhum reset, lido do Claude ou gravado no `motor.json`, vale mais que **agora + 8 dias** (um valor absurdo é cortado e o corte fica gravado).
- A espera fica em `%LOCALAPPDATA%\fedora-assistente\motor.json` e sobrevive a reinício.
- `FEDORA_MOTOR=claude` ou `agy` força um motor. `/api/status` mostra `motor` e `motor_motivo`.
- Trocar de motor abre uma sessão nova (o id de uma assinatura não serve na outra).

## Motor agy (`motor_agy.py`)
Roda o `agy` headless (stream-json, prompt por stdin) e emite os mesmos eventos do Claude. Variáveis: `AGY_MODEL` (leve, padrão `gemini-3.8-flash-medium`), `AGY_MODEL_COMPLETO` (padrão `gemini-4-argon-medium`), `AGY_PERMISSION` (`bypass` padrão, `auto`, `plan`), `AGY_CMD`.
- **Autonomia**: `bypass` usa `--dangerously-skip-permissions`, como o Claude usa `bypassPermissions`.
- **Travas**: o `agy` não tem `--disallowedTools`. A lista `runtime.DENY` (fonte única) é traduzida a cada turno para `permissions.deny` (`command(regex:...)`, `read_file`, `write_file`) em `%LOCALAPPDATA%\fedora-assistente\agy-settings.json`, passado em `--settings`. Deny vence allow (medido). Se a tradução sair incompleta, o motor **não inicia**. Mexeu na `DENY`? Rode `python motor_agy.py`.
- **Prova ao vivo** (gasta 1 chamada): `.venv\Scripts\python.exe motor_agy.py --verifica-deny` roda o `agy` com bypass e confirma que `hostname` é barrado e que o `agy` não ignorou nenhuma entrada (`ignoring invalid deny entry` no log). Rodada em 09/11/2026 com o `agy` 1.5.3: `hostname` barrado, 0 entradas ignoradas.
- **Diferenças do Claude**: sem system prompt separado (o bloco de sistema abre a mensagem); sem modo leve de ferramentas; sem janelas de cota, só tokens por turno em `motor_agy.STATS`.

## Agentes (`agentes.py`, definições em `agentes/*.md`)
Os agentes **não dependem do motor**: qualquer motor os chama pelo mesmo comando (`agentes.py <agente> "<tarefa>"`), e cada um tem modelo fixo.

| Agente | Nível | Modelo | Assinatura | Faz |
|---|---|---|---|---|
| `junior` | N1 | Haiku | Claude (Gemini Flash quando o agy está no comando) | edita 1 arquivo |
| `flash` | N1 | Gemini 3.8 Flash | só Google | edita 1 arquivo |
| `argon` | N2 | Gemini 4 Argon | só Google | só rascunha |
| `senior` | N3 | Opus 5.5 | Claude (Gemini 4 Argon high quando o agy está no comando) | edita, roda testes |
| `qa` | Revisão | Opus 5.5 | Claude (Gemini 4 Argon high quando o agy está no comando) | só lê, roda testes |

- **Qual assinatura**: cada modelo só existe em uma e roda nela (Haiku, Sonnet e Opus 5.5 no Claude; Gemini no Google). Exceção: com o agy no comando (Claude acima de 55% na semana ou em 90% na sessão de 5 h, `FEDORA_MOTOR_TURNO`, que o runtime põe no ambiente de quem chama), **nada roda no Claude** e os modelos do Claude trocam pelo Gemini de `SUBSTITUTO_NO_AGY`: Haiku → `gemini-3.8-flash-low`, Sonnet → `gemini-3.8-flash-high`, Opus → `gemini-4-argon-high`. Os Claude de dentro do agy (`claude-opus-5-5-*`, `claude-sonnet-5-5-*`) não são usados. Fora de um turno, segue o estado gravado da regra (ou o `FEDORA_MOTOR` forçado). Agy no comando mas não instalado: o agente **falha com erro** (`ESCALAR: ... nada roda no Claude nesse estado`), nunca cai calado no Claude.
- **Na tela**: com o agy no comando, a linha de status diz "Google (Antigravity) conectado, motor principal", a seção "Plano do Claude" vira "Motor: Google (Antigravity)" com "Claude em pausa até <reset>" (`motor_ate` no `/api/status`), e as barras de 5 h e semana continuam visíveis; a tela relê o status a cada 15 s.
- **Permissão mínima, sem bypass**: cada agente recebe só as ferramentas da função (`edita`, `comandos` no cabeçalho do `.md`). `comandos` só aceita itens do catálogo `agentes.COMANDOS` (`git status|diff|log|show`, `python`, `.venv/Scripts/python.exe`); outro valor e o agente não carrega. No Claude vira `--tools`, `--allowedTools` e regras deny do agente; no `agy`, `--mode plan` (lê) ou `accept-edits` (edita), `allow` por regex estreita e deny do agente. Nos dois: python só roda um dos autotestes offline de `tools/testa.ps1` (lista fechada `agentes.AUTOTESTES`; o `--teste` confere que as duas são iguais), exato, com `python`, `.venv/Scripts/python.exe` ou `./.venv/Scripts/python.exe` e um espaço entre as partes (`python ./runtime.py`, barra invertida, argumento extra ou segundo comando: barrado; mudou o `testa.ps1`? mude `AUTOTESTES` junto); nenhum comando aceita aspas, `\`, `$`, `{ }` nem crase (o bash juntaria `--o""utput` em `--output`; no Claude isso é deny por glob, no `agy` o allow não aceita esses caracteres); git sem `--output`, `--ext-diff`, `--textconv`, `--no-index`, `-c`, `--exec-path`; quem não edita (`qa`) não redireciona saída para arquivo. A `DENY` vale por cima nos dois.
- **O que isso isola de verdade**: o allow estreito isola quem **não edita** (`qa`, `argon`). Para quem edita (`senior`, `flash`, `junior`) a `DENY` continua sendo só um freio, não um sandbox: o `senior` edita código e roda autotestes que o usam, e poderia até mexer em `.git/config`. Nos dois lados ninguém redireciona saída (`>` `<`) nem usa `* ? [ ] ( ) ! &` (medido no CLI real do Claude; o `#` é comentário de shell e o CLI o ignora).
- **Modo `plan` no `agy`** (medido no `agy` 1.5.3): `--mode plan` só vale **sem** `--disable-slash-commands`; com a flag, um agente que só lê criou arquivo. Por isso `qa` e `argon` rodam sem a flag e com o deny `write_file(*)` (medido: não criam arquivo, não editam arquivo existente e terminam a revisão com resposta). Quem edita mantém a flag (medido: comando fora da lista é negado com e sem ela).
- **Comando negado no `agy`**: o `agy` headless encerra o agente, sem resposta, no primeiro comando que a regra nega; o Claude devolve a negação ao modelo e ele segue. Por isso o pedido ao agente no `agy` leva um aviso gerado (`aviso_headless_agy`) com a lista exata do que pode rodar e a orientação de ler arquivos só pelas ferramentas de leitura.
- **Tempo**: o agente tem `FEDORA_AGENTE_TIMEOUT` (300 s) + 30 s. Enquanto uma ferramenta roda (ex.: o shell esperando o agente), o motor não conta ociosidade até o teto `max(JARVIS_TIMEOUT, FEDORA_AGENTE_TIMEOUT + 60)`.
- **Executável**: `claude`/`agy` que resolvem para `.cmd`/`.bat` são recusados (o cmd.exe reinterpretaria a tarefa); use o `.exe`. A linha de comando montada precisa caber em 30 mil caracteres depois do escape do Windows.
- **Falha**: a chamada devolve `ESCALAR: <agente> falhou (...)` e o tech lead decide. Agente não chama agente (`FEDORA_AGENTE`).
- **Registro**: uma linha por chamada em `%LOCALAPPDATA%\fedora-assistente\agentes.jsonl` (agente, modelo, assinatura, tokens), base para uma regra de cota por agente.
- **Mudar a equipe**: edite o `.md` do agente (nome, modelo, o que pode fazer) e, se for modelo novo, a tabela `MODELOS` em `agentes.py`. `python agentes.py --teste` confere que `docs/EQUIPE.md` cita todos.
- **Ainda não existe**: o agente tech lead como definição própria (hoje o tech lead é o cérebro do turno) e nomes por função no lugar de `flash` e `argon`.

## Terminal `dupla` (`dupla.py`, atalho `~\.local\bin\dupla.cmd`)
Um terminal só, em qualquer pasta, com as duas assinaturas. Usa `runtime.run()`: **mesma regra, mesmo `motor.json` e mesmas travas** do assistente de voz.
- **Uso**: `dupla` (REPL), `dupla "faça X"` (um pedido), `dupla --uso` (janelas do Claude e quem responde), `dupla --motor` (só imprime `claude|agy`, para roteadores externos), `dupla --teste` (autoteste offline). No REPL: `/uso`, `/motor claude|agy|auto`, `/novo`, `/ajuda`, `/sair`. Ctrl+C durante a resposta só interrompe o turno (mata a árvore de processos).
- **Uso do Claude**: vem de cada resposta dele e da statusline do Claude Code (`~\.ai-sync\claude-statusline.py`, grava `~\.ai-sync\claude-usage.json` e `%LOCALAPPDATA%\dupla\limites.json`). Dado com mais de 10 min: uma sonda mínima (`claude -p`, sem sessão) antes de decidir. Durante a espera no `agy`, nada toca no Claude. Com `/motor` forçado, o cabeçalho mostra o uso do cache (sem sonda).
- **Sincronia entre motores**: cada turno grava em `%LOCALAPPDATA%\dupla\<hash da pasta>\historico.jsonl`. Quem assume recebe `<contexto_da_dupla>` com os turnos que não viu (até 8, 1.500 chars cada, 9.000 no total) + `git status --short`. Voltar ao motor antigo retoma a sessão dele e manda só o que perdeu. O histórico é podado para os últimos 200 turnos quando passa de 300 (`visto` acompanha).
- **HANDOFF**: o `dupla` atualiza `~\.ai-sync\HANDOFF.md` (Estado atual + 1 linha no Histórico) a cada turno ok e termina toda mensagem com `(dupla: nao edite o HANDOFF.md; ...)`; o `CLAUDE.md` e o `~\.gemini\GEMINI.md` respeitam esse aviso. Nos terminais nativos (`claude`/`agy` direto), o próprio agente atualiza o HANDOFF. Pasta sem git: "Arquivos alterados: n/d (sem git)".
- **Pedido grande**: acima de 6.000 chars (`runtime.MSG_ARGV_MAX`) o pedido vai pelo stdin do `claude -p` (só os argumentos fixos já gastam ~14 mil dos 32.767 da linha de comando do Windows). O `agy` sempre recebe pelo stdin.
- **Teste vivo** (09/11/2026, pasta vazia): `/motor agy` criou `teste.txt`; `/motor claude` recebeu "+1 turno(s) de contexto do agy" e descreveu certo o que o agy fez. Pedido de 32.457 chars pelo Claude: ok.
- **Coordenação Claude ⇄ agy** (quem é dono de cada arquivo): `docs/DUPLA-COORDENACAO.md`.

## Cache manual de contexto do agy (`contexto.py`, `docs/CONTEXTO-AGY.md`)
Medido em 09/11/2026 (agy 1.5.3, Flash): pedido mínimo = ~11,4 mil tokens de entrada em qualquer pasta e `cache_read_tokens` = 0 até repetindo; o gasto alto (~90 mil) vem de explorar arquivos em vários passos, cada um reenviando tudo. Então o cache é nosso.
- **O que vai ao agy**: o texto de `docs/CONTEXTO-AGY.md` (sem comentários HTML, no máximo `FEDORA_CONTEXTO_MAX` = 5000 caracteres) abre o bloco de sistema da **primeira mensagem de cada sessão nova**. Sessão retomada não repete. `FEDORA_CONTEXTO_AGY=0` desliga (para comparar A/B).
- **Parte manual**: mapa do projeto, regras e o que não explorar. Você revisa junto com a retrospectiva da semana e atualiza a linha `Revisão manual: AAAA-MM-DD`.
- **Parte automática** (entre as marcas `auto`): hubs e nós centrais do Graphify e aviso se o mapa é de outro commit. `python contexto.py --atualiza`; o servidor também roda depois de cada rodada do `retro.py` e avisa no log quando a revisão manual passa de 14 dias.
- **Não entra texto do Haiku**: a retrospectiva é `fedora-log` e não volta para prompts. Lição da retro que valha virar regra, você copia à mão.
- `python contexto.py --mostra` imprime o que o agy recebe e o tamanho; `python contexto.py --teste` é o autoteste (fora do `testa.ps1`: a lista dos agentes é fechada).

