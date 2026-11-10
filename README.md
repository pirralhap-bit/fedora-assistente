# Fedora

Assistente de voz local, com duas versões: **Windows 11** (esta branch, `master`, a mantida) e **Linux** (branch `linux`, congelada). Um buraco negro (na janela e flutuando na tela) que ouve quando você pede, com o sistema e o vault orbitando, e uma página de notas diárias com ditado. O cérebro é o `claude` CLI (assinatura, sem chave de API); a transcrição é o faster-whisper local; atalhos rápidos e a reescrita das notas são o Gemini (`GEMINI_API_KEY` no `.env`). Tudo roda em `127.0.0.1`.

Base: fork de jarvis-claude-code (98b1972), com a interface trocada.

## Versões e instalação

| Sistema | Onde está | Estado |
|---|---|---|
| **Windows 11** | branch `master` (esta) | mantida e testada (`tools\testa.ps1`) |
| **Linux** (feita no Fedora + KDE/Wayland) | branch `linux` (`git checkout linux`) | congelada no último commit que rodava lá; serviços systemd de usuário, whisper.cpp, `pw-play` |
| macOS | não existe | trate como porte a partir do Linux |

As duas têm a mesma arquitetura (servidor local, ouvido, whisper, Piper, `claude -p`, Gemini); mudam o áudio, o STT, o lançador, os atalhos do sistema e o modo de subir no login. Por isso **não há instalador único**: a IA que a pessoa usar (Claude Code ou outra) deve configurar a instalação conforme o sistema dela.

### Roteiro para a IA que vai instalar

1. **Descubra o sistema** (`$env:OS` / `uname -a`, `echo $XDG_SESSION_TYPE`, gerenciador de pacotes) e **escolha a branch**: Windows → `master`; Linux → `git checkout linux`; outro → pergunte, e proponha um porte partindo da `linux`.
2. **Não invente caminhos nem versões.** Veja o que já está instalado (Python 3.12, git, `claude`, Piper, microfone/saída de áudio) e instale só o que falta, confirmando com a pessoa o que mexer fora da pasta do projeto.
3. **Windows**: `py -3.12 -m venv .venv`; `.venv\Scripts\pip install -r requirements.txt`; `copy .env.example .env`; Piper e uma voz pt_BR em `%LOCALAPPDATA%\fedora-assistente\piper` e `voices`; `claude` CLI instalado e logado; depois `.\iniciar.ps1` e `.\tools\testa.ps1`.
4. **Linux** (branch `linux`): venv com `requirements.txt`; whisper.cpp compilado (Vulkan opcional) em `~/.local/opt/whisper.cpp`, modelo em `~/.local/share/whisper-models`, Piper em `~/.local/opt/piper`; `./deploy/instalar.sh` liga os serviços `whisper` e `fedora-assistente` (systemd de usuário) e cria o autostart e o item de menu; `./tools/testa.sh`. Os caminhos e `.desktop` vieram do Fedora/KDE: **adapte à distro e ao ambiente gráfico da pessoa** (outro gerenciador de pacotes, X11 em vez de Wayland, outro player de áudio no lugar do `pw-play`).
5. **`.env`**: peça à pessoa a `GEMINI_API_KEY`, o caminho do vault do Obsidian (opcional) e a cidade do clima. Nunca grave segredo em arquivo versionado nem leia `.env` de volta no chat.
6. **Não afrouxe a segurança**: a lista `DENY` (`runtime.py`) e as `PC_RULES` (`commands.py`) valem em qualquer sistema; se a versão Linux usar comandos diferentes (`rm`, `dd`, `shutdown`), confira se a `DENY` daquela branch cobre o que a pessoa tem.
7. **Confirme ao final**: servidor em `http://127.0.0.1:8720`, falar uma frase de teste, autotestes passando. Se algo não existir no sistema dela, diga o que ficou de fora em vez de fingir que funciona.

## Rodar no Windows

```powershell
.\iniciar.ps1
```

Sobe `server.py` (porta 8720, que também sobe o ouvido, o buraco negro flutuante, a janela e a retrospectiva) e `whisper_server.py` (porta 8178). Ctrl+C encerra os dois; se o servidor morrer, o whisper sai sozinho.

- Python do projeto: `.venv\Scripts\python.exe`; dependências em `requirements.txt`.
- Configuração em `.env` (copie de `.env.example`): chave e modelos do Gemini, vault, permissão do Claude, porta, atalho, etc. O `.env` nunca vai para o git.
- Precisa do `claude` CLI logado e do Piper (`%LOCALAPPDATA%\fedora-assistente\piper` e `voices`) para falar.

## Como usar

- **Falar**: aperte Ctrl+Alt+Espaço (`FEDORA_HOTKEY`) ou clique no buraco negro. O microfone só abre assim, ou quando ela faz uma pergunta; não há escuta contínua nem palavra de ativação. Fale e pare: o Silero VAD acha o fim da fala. "Ok, Fedora" no começo do pedido é apenas removido; não ativa nada.
- **Esc** cancela o turno em andamento (voz ou chat): o servidor mata o Claude e descarta a sessão daquele canal.
- **`#notas`** (na URL da janela): página de notas diárias. Voz lá: "ditar", "organizar", "salvar", "voltar", "desfazer". O resto vai para o Claude.
- **`#chat`**: conversa digitada, estilo Claude Code (Enter envia, Shift+Enter quebra linha).
- **Modo leve (padrão)**: poucas ferramentas, contexto pequeno. "A fundo", "modo analista" ou "modo completo" no pedido liga o Claude Code completo (skills, PowerShell etc.). `FEDORA_MODO_LEVE=0` deixa sempre completo.
- **Modo equipe**: se o pedido tem "equipe", "delegue", "delegar", "júnior", "sênior", "revisor", "peça ao QA", "mande o QA" ou "passe pro QA", o Claude ganha a ferramenta Agent e o mapa de `docs/EQUIPE.md` (subagentes em `.claude/agents/`: junior, senior, qa). Custa cerca de 2,8 mil tokens a mais por turno.
- **Resposta longa**: o que vem depois de uma linha `---` fica só na tela; só a parte antes dela é falada.
- **Motor Google (agy)**: com o Claude acima de 55% na semana ou em 90% na sessão de 5 h, o Antigravity assume (detalhes em `docs/MOTORES.md`); a tela mostra "Google (Antigravity) conectado, motor principal" e "Claude em pausa até <reset>", e um agente sem o agy instalado falha com erro em vez de usar o Claude.
- **Linha do tempo de agentes**: quando há delegação, a interface mostra quem recebeu o pedido, quais subagentes foram acionados e o que cada um usou.
- Atalhos rápidos sem Claude (hora, data, abrir app/jogo/site/pasta, volume, mídia, print, timer, clima, sistema): ver `atalhos.md`.

## Peças

| Arquivo | O que faz |
|---|---|
| `server.py` | servidor HTTP local (`/api/*` e a UI); só aceita `localhost`, exige token nos POST; decide a rota do turno, grava o registro e sobe o resto |
| `runtime.py` | chama `claude -p` em stream; `DENY` e regras de segredo; modos leve/equipe/completo |
| `commands.py` | `/comandos`, regras do PC (`PC_RULES`), bloco de sistema do prompt, lê `docs/EQUIPE.md` no modo equipe |
| `conversa.py` | sessão do Claude (janela de 900 s, máx. 10 turnos) e escolha do modo (leve/equipe/completo) |
| `memory.py` | lê o vault Obsidian como grafo de notas; ignora notas com `fedora-log: true` |
| `atalhos.py` + `atalhos.md` | pedidos rápidos sem Claude: filtro local, depois o Gemini escolhe o atalho e o código executa |
| `llm.py` | API do Gemini (JSON) com disjuntor: depois de uma falha, o atalho rápido fica 5 min fora e tudo vai direto ao Claude |
| `listener.py` | ouvido push-to-talk: atalho global, microfone, VAD, whisper, entrega o pedido ao servidor |
| `vad.py` | Silero VAD (onnxruntime): decide o que é fala |
| `speech.py` | voz: Piper (pt_BR faber) tocando por sounddevice; também fala o aviso do timer |
| `whisper_server.py` | STT local, porta 8178: faster-whisper `medium`, CPU/int8 (mesmo contrato do whisper.cpp) |
| `orb.py` | buraco negro flutuante sem borda; clique fala/encerra, arrastar move, botão direito abre o menu (`FEDORA_ORB=0` desliga) |
| `notes.py` | lista, lê e salva daily notes; recusa salvar se a nota mudou fora (Obsidian). `python notes.py --anota "texto"` acrescenta uma linha `- HH:MM texto` na seção `## Fedora` da daily de hoje (marcador `<!-- fedora-anotacoes -->`); o Claude chama isso quando você pede para anotar ou conta algo (regra `NOTA_RULES` em `commands.py`) |
| `tidy.py` | reorganiza a nota diária pelas seções (Gemini); não aplica se perder conteúdo |
| `sysinfo.py` | CPU, RAM, GPU e status da última chamada ao Gemini |
| `turnolog.py` | registro por turno (`turnos.jsonl`) |
| `retro.py` | nota semanal de retrospectiva no vault |
| `ui/index.html` | a interface inteira (buraco negro, `#notas`, `#chat`) |
| `iniciar.ps1` | sobe servidor + STT |
| `tools/` | `testa.ps1` (autotestes offline), `compara-voz.py` (compara modelos de voz em clipes salvos) |

STT em CPU: configurado com o modelo `medium` para maior capacidade, compreensão e robustez de vocabulário na transcrição local.

## Segurança

- A Fedora roda o Claude com `bypassPermissions` (`JARVIS_PERMISSION`): ele age sem pedir confirmação, guiado pelo que ouviu. Um `claude -p` sem tela não consegue mostrar prompt de permissão; qualquer modo mais estrito bloqueia as ferramentas em silêncio (`acceptEdits` limita a edições).
- A lista `DENY` em `runtime.py` é um **freio contra frase mal ouvida, não um sandbox**. Ela veta desligar/reiniciar/suspender o PC, apagar de vez (`rm`, `del`, `Remove-Item`, `rmdir`, `format`, `diskpart`, `reg delete`, `os.remove`, `shutil.rmtree`, `git clean`, `robocopy /mir`...), `-EncodedCommand` e matar processos críticos, nas ferramentas Bash e PowerShell. Mover para a Lixeira continua permitido (`PC_RULES` em `commands.py`).
- O que o `DENY` **não** cobre (aceito): maiúsculas e minúsculas misturadas no Bash, ofuscação (concatenação de strings, base64 fora de `-EncodedCommand`, escapes `^` do cmd), `DeleteFile` com um só argumento (apaga de vez por padrão) e `taskkill`/`Stop-Process` por PID em processos críticos. Palavra perigosa dentro de URL ou texto de busca num comando Bash também é negada (falso positivo de propósito).
- Leitura de `.env` e `*.token` é negada (regras `Read(...)` e globs de comando), inclusive o token do servidor em `%LOCALAPPDATA%\fedora-assistente`.
- O servidor só aceita `localhost` e exige o token em todo POST.
- `tidy.py` confere a saída do Gemini linha a linha e não aplica a reorganização se perder conteúdo; `notes.py` não salva por cima de nota alterada fora.

## Registro por turno e retrospectiva

**Registro (`turnolog.py`).** Cada turno (voz ou chat) vira uma linha JSON em `turnos.jsonl`, fora do repo e do vault: `%USERPROFILE%\.local\state\fedora\turnos.jsonl` (a pasta do `conversa.md`; `XDG_STATE_HOME` muda a base). `FEDORA_TURNLOG` aponta outro arquivo. Passou de 5 MB, vira `turnos.1.jsonl` (só um antigo é mantido).
- Grava: hora, canal, pedido, rota (`comando`, `atalho`, `filtro_local→claude`, `claude`, `erro`), atalho, modo, sessão nova, agentes, nº de ferramentas, tempos, tokens, erro curto, cancelado, estado do Gemini, `sinais`.
- Pedido comum guarda só os primeiros 60 caracteres; turno com sinal guarda até 400. Antes de gravar, números com 6 ou mais dígitos (CPF, cartão, telefone) e o valor depois de "senha", "token", "cpf", "cnpj", "cartão", "pix", "chave" viram `[oculto]`.
- Sinais: `repeticao` (mesmo pedido em 30 s), `atalho_barrado_e_repetido`, `escalar`, `cancelado`, `erro`, `resposta_vazia`.
- Nunca derruba um turno: falha ao gravar só imprime um aviso.

**Retrospectiva (`retro.py`).** Uma nota por semana ISO: `Fedora - Retrospectiva AAAA-Wss.md` em `00-INB*/_a-processar` do vault (se essa pasta não existe, nada é criado).
- Só dias completos (antes de hoje), no máximo 14 dias para trás. Cada dia ganha um bloco: números calculados em código e, com 5 turnos ou mais no dia, uma leitura curta do Haiku (até 3 tentativas por dia; limite de cota não conta tentativa). No máximo 2 dias por execução; custa cerca de 5 mil tokens por dia analisado. Roda 60 s depois de subir o servidor e a cada 6 h.
- A nota só recebe blocos no fim; nada já escrito é reescrito. Pode editar à vontade, mas **não apague os marcadores** `<!-- fedora-retro... -->`: é por eles que ela sabe que o dia já foi feito.
- O estado fica em `retro.json`, na mesma pasta do `turnos.jsonl`. Notas com `fedora-log: true` no frontmatter ficam fora da memória da Fedora.
- `FEDORA_RETRO=0` desliga. `python retro.py --agora` roda já (log, vault e Haiku reais); `python retro.py --teste` é offline.
- Fluxo: a retrospectiva cai no inbox; você pede "processa a nota de retrospectiva"; ela devolve um plano de ação; você aprova; a equipe (modo equipe) executa.

## Filtro local

Antes de qualquer chamada ao Gemini, `atalhos.pode_ir_ao_gemini(pedido)` decide, sem rede, se o pedido pode sair do PC. Só pedidos que parecem atalho vão ao Gemini; o resto vai direto ao Claude (rota `filtro_local→claude`). Conversa, dados pessoais e frases longas nunca saem.
- Duas portas, as duas necessárias, sobre o pedido normalizado (sem acento/pontuação, sem "aí", "por favor", "me" no começo): a frase começa com verbo/expressão de algum atalho ("abre", "sobe", "me avisa", "daqui a", "que horas") e tem objeto compatível (volume/som, brilho, música, print, uma duração, clima...).
- `MAX_PALAVRAS = 12`: acima disso nunca passa. Cada regra em `REGRAS` tem também o seu máximo de palavras.
- "Abrir" com nome que o catálogo não conhece só passa em frase curta e sem palavra de conversa; pesquisa só com "no google/na internet" ou curta e sem nada pessoal.
- Para ampliar: acrescente uma entrada em `REGRAS` (atalho, começo, objeto ou `None`, máximo de palavras), ou novos exemplos no `atalhos.md` (o teste usa os exemplos de lá). Erre para o lado seguro: barrar um atalho custa um turno do Claude; deixar passar conversa manda a frase à rede.
- `python atalhos.py --teste-filtro` (offline) mede os dois lados: legítimas que passam >= 90 % e conversa que vaza <= 2/10 (hoje 100 % e 0/40).

## Testes

```powershell
.\tools\testa.ps1
```

Se a política de execução bloquear: `powershell -ExecutionPolicy Bypass -File tools\testa.ps1`.

Roda os autotestes offline de cada módulo e sai com código diferente de zero se algum falhar. Não chama o Gemini nem o Claude. `python atalhos.py` (completo) e `python tidy.py --gemini` usam o Gemini de verdade e ficam fora; rode à mão quando quiser.
