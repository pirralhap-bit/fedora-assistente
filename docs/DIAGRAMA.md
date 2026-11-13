# Diagrama do Sistema — Fedora Assistente

Visão concisa e objetiva da arquitetura atual do sistema Fedora (Windows 11, local, híbrido Claude/Antigravity).

## 1. Fluxo Principal do Sistema

```mermaid
flowchart LR
    subgraph ENTRADA["Entrada & Captura"]
        V["Voz (Microfone)"]
        W["whisper_server.py (STT 8178)"]
        L["listener.py (VAD/Fila)"]
        UI["UI (ui/index.html)"]
        DUP["dupla.py (CLI Terminal)"]
    end

    subgraph INGRESSO["Servidor & Roteamento"]
        S["server.py (HTTP 8720)"]
        CMD["commands.py (handle/system_block)"]
        AT["atalhos.py (Ações PC / LLM)"]
        LLM["llm.py (Gemini API)"]
        CV["conversa.py (Modo/Sessão/Etapas)"]
        NOT["notes.py (Notas/Daily)"]
    end

    subgraph MOTOR["Motor & Decisão"]
        RT["runtime.py (run/motor_atual/LIMITS)"]
        MR["motor_regra.py (Regra 55%/90%)"]
        RCL["claude -p (Subprocesso CLI)"]
        RAG["motor_agy.py (agy headless)"]
    end

    subgraph CONTEXTO_MEMORIA["Memória & Retrospectiva"]
        CTX["contexto.py (docs/CONTEXTO-AGY.md)"]
        MEM["memory.py (Grafo e Contexto)"]
        RET["retro.py (Retrospectiva Semanal)"]
    end

    subgraph EXECUCAO["Equipe & Backlog"]
        DP["despachante.py (Vigia Backlog)"]
        AG["agentes.py (Subagentes)"]
    end

    subgraph SAIDA["Saída & Ações"]
        SP["speech.py (Piper TTS)"]
        PC["Ação no PC (App/URL/Processo)"]
    end

    subgraph ESTADO["Estados & Persistência (Dono + Gravadores)"]
        MJ[("motor.json<br>Dono: motor_regra.py | Gravam: server, dupla, agentes (via decidir)")]
        SJ[("state.json<br>Dono: commands.py")]
        CF[("Cofre Obsidian<br>Gravam: despachante, notes, retro, motor, UI")]
        LG[("turnos.jsonl / agentes.jsonl<br>Gravam: turnolog.py / agentes.py")]
        MEM_EST[("Memória de Processo<br>LIMITS (runtime) | SESSION/CHAT (server)")]
        CVD[("conversa.md<br>Dono: conversa.py")]
    end

    %% Entrada, Voz, SSE e UI (Ponto único de disparo de turno de voz e fala)
    V --> L
    L <-->|transcrição| W
    L -->|fila / SSE /api/events| S
    S -->|SSE eventos| UI
    UI -->|"/api/run (voz/chat)"| S
    UI -->|"/api/speak"| S
    S -->|speech.say| SP
    L -.->|speech.stop / silencia| SP
    S -.->|EAR.control reabre mic| L
    DUP -->|"runtime.run"| RT
    DUP -.->|sonda| RCL

    %% Servidor e Roteamento
    S --> CMD
    CMD --> AT
    AT -->|startfile / Popen| PC
    AT --> LLM
    CMD -.-> MEM
    S -->|"fala_etapa_inicio/repasse/retorno"| CV
    S -.->|turnolog.registrar| LG
    S -->|salva daily| NOT
    NOT --> CF

    %% Decisão e Motores
    S -->|"runtime.run / motor_atual"| RT
    RT -->|"motor_atual / decidir"| MR
    MR -->|"instalado?"| RAG
    MR --> MJ
    RT -->|"run_claude"| RCL
    RT -->|"motor_agy.run"| RAG
    RAG -.-> CTX

    %% Memória e Retro
    S -.-> MEM
    S -.->|"contexto.atualiza"| CTX
    S -.-> RET
    RET -->|lê| LG
    RET -->|"claude -p (direto)"| RCL
    RET -.-> CF
    MEM -.->|leitura| CF

    %% Equipe e Backlog
    DP -.->|"Popen: agentes.py"| AG
    DP <--> CF
    AG -->|"motor_atual"| RT
    AG -->|"executa direto"| RCL
    AG -->|"executa direto"| RAG
    AG -.-> LG

    %% Estados
    CMD <--> SJ
    CV <--> CVD
    RT <--> MEM_EST
    S <--> MEM_EST
```

---

## 2. Ciclo de Vida de um Card no Backlog

Gerenciado por `despachante.py` vigiando `backlog/<projeto>/[<grupo>/]<NNNN>-<slug>.md` no cofre Obsidian (usuário pode alterar o status manualmente):

> **Nota:** os estados `aguardando_arquiteto` e `para_decisao` existem no backlog para controle manual/revisão, mas o `despachante.py` os ignora de propósito (não estão em `QUEM_RODA`).

```mermaid
stateDiagram-v2
    [*] --> novo
    novo --> pronto: techlead tria (AGENTE: x)
    escalado --> pronto: techlead retria (AGENTE: y diferente)

    pronto --> em_revisao: dev conclui (OK)
    pronto --> escalado: dev escala (junior/writer/design)
    pronto --> bloqueado: dev senior escala

    em_revisao --> concluido: qa aprova (APROVADO)
    em_revisao --> pronto: qa reprova (REPROVADO, até 2x)
    em_revisao --> bloqueado: 3ª reprovação (MAX_TENTATIVAS = 3)

    novo --> bloqueado: falha contrato / agente inválido / sem triagem
    escalado --> bloqueado: techlead escala, falha ou devolve ao mesmo agente

    state "Qualquer Estado Ativo (novo, pronto, em_revisao, escalado)" as ATIVOS {
        [*] --> ativo
        ativo --> ativo: falha transitória (rede/529, até 2x)
        ativo --> [*]: MAX_TRANSITORIAS = 3 ou MAX_RODADAS = 12
    }
    ATIVOS --> bloqueado: falha transitória repetida (3x) ou MAX_RODADAS (12 passos)

    concluido --> [*]
    bloqueado --> [*]
```

---

## 3. Legenda e Pontos de Acoplamento

### Legenda
- **Entrada & Ingresso**: captura de áudio com VAD/Whisper (`listener.py`, `whisper_server.py`), UI web intermediadora (`ui/index.html`), CLI terminal (`dupla.py`) e endpoints HTTP 8720 (`server.py`).
- **Roteamento & Contexto**: comandos locais (`commands.py`), atalhos com fallback LLM (`atalhos.py`, `llm.py`), etapas de fala (`conversa.py`), notas Obsidian (`notes.py`) e memória (`memory.py`).
- **Motor & Decisão**: Claude e `agy` via `runtime.py` e `motor_regra.py` (limites 55%/90% em `motor.json` e `runtime.LIMITS` em memória).
- **Equipe & Backlog**: subagentes de `agentes.py` disparados por subprocesso/shell pelo `despachante.py` ou motor, chamando CLIs diretamente.
- **Saída & Estados**: síntese Piper (`speech.py`), interface web e arquivos/estruturas de estado com múltiplos gravadores sem trava global.

### Pontos de Acoplamento ou Risco
1. **Resiliência de Decisão de Motor (`motor_regra.py` e `motor.json`)**: se `motor.json` estiver corrompido, é lido como vazio e cai no Claude por padrão sem travar o sistema; falhas de gravação são ignoradas silenciosamente.
2. **Execução de CLIs Externos Headless (`claude -p` e `agy`)**: o motor principal roda com permissão ampla no `runtime.py`/`motor_agy.py`, enquanto os subagentes em `agentes.py` rodam isolados com ferramentas restritas e lista estreita de comandos.
3. **Acoplamento de Contrato de Linha no Backlog**: resposta sem a linha do contrato ou formato inesperado vira FALHA e é bloqueada pelo `despachante.py`.
4. **Disputa e Concorrência em `motor.json`**: servidor, `dupla.py` e `agentes.py` (lançado pelo despachante) realizam leitura-modificação-escrita concorrente sem trava de arquivo entre processos.
5. **Gargalo de Transcrição e Concorrência na Voz**: `listener.py` depende de `whisper_server.py` ativo na porta 8178; novas capturas de comando de voz são bloqueadas durante o `RUN_LOCK` do servidor, embora o ditado continue passando.
6. **UI como Ponto Único de Falha da Voz**: o fluxo de áudio depende da UI aberta para receber eventos SSE de `listener.py` e disparar o `POST /api/run` e `/api/speak`.
7. **Hub de Acoplamento em `runtime.py`**: centraliza dependências de `server`, `agentes`, `dupla`, `retro` e `despachante`, com ciclos de import tardio entre `runtime`, `motor_regra` e `motor_agy`.
8. **Múltiplos Gravadores no Cofre Obsidian sem Coordenação Comum**: despachante, `notes.py`, `retro.py`, Obsidian/usuário e o motor via comandos gravam independentemente no cofre.
