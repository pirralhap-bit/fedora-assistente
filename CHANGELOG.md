# Changelog

Todas as mudanças relevantes deste projeto ficam registradas aqui.
Formato: [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/); versões no estilo [SemVer](https://semver.org/lang/pt-BR/).

## [0.1.0] - 2026-11-12

Primeira versão estável no Windows 11 (branch `master`). Licença MIT (veja [`LICENSE`](LICENSE)).

### Adicionado
- **Motores Claude e agy com troca por cota**: o `claude -p` (`runtime.py`) e o Antigravity/Google (`motor_agy.py`) como segunda assinatura, ambos sob a mesma lista `DENY`. A regra de troca (`motor_regra.py`) passa para o agy quando o Claude passa de 55% na semana ou 90% na janela de 5 h. O terminal único `dupla` (`dupla.py`) roda os dois em revezamento.
- **Equipe de agentes** (`agentes.py`, `agentes/`): junior, writer, design, arquiteto, senior, qa e techlead, com níveis N1–N3, modelo e esforço por agente. Funcionam com qualquer motor: no agy, Haiku, Sonnet e Opus trocam por Flash ou Argon. O arquiteto roda em Opus com esforço high.
- **Despachante de backlog** (`despachante.py`): lê os cards do cofre do Obsidian, faz a triagem pelo techlead, chama o agente certo e grava o status. Usa trava por card e descarta a resposta se o usuário mudar o status no meio da rodada.
- **Fala em etapas** (`conversa.py`): avisa o início do pedido, quando repassa para um agente e quando o agente devolve.
- **UI** (`ui/index.html`): Markdown renderizado nas respostas; o Quadro & Backlog lê o status real dos cards do cofre por `/api/backlog` (`despachante.resumo_backlog`, só leitura). Ele tem a coluna "Aguarda você" para `bloqueado`/`para_decisao` e marca como "rodando" o card que está com trava ativa.
- **Controles de voz e turno na UI**: botão de voz muda/voz ligada (`/api/mudo`, separado do microfone), botão Interromper (o mesmo que Esc) e campo de escrita de várias linhas (Enter envia, Shift+Enter quebra a linha).
- **Buraco negro flutuante no Windows** (`orb.py`): janela sempre no topo; clicar fala, dá para arrastar e tem menu.
- **Ouvido push-to-talk** (`listener.py`): atalho global ou clique, sem microfone sempre aberto. STT com faster-whisper (modelo medium), VAD e filtro de alucinações.
- Leitura de GPU no Windows, retrospectiva e log de turnos (`retro.py`, `turnolog.py`).
- Documentação: `docs/EQUIPE.md`, `docs/MOTORES.md`, `docs/DUPLA-COORDENACAO.md`, `docs/CONTEXTO-AGY.md`, `docs/DIAGRAMA.md` (novo) e o README com o roteiro de instalação conforme o sistema.
- `LICENSE` MIT na raiz.

### Alterado
- **Porte para Windows 11**: áudio com sounddevice/Piper, STT com faster-whisper, atalhos e informações do sistema sem dependências do Linux. A versão Linux ficou congelada na branch `linux`.
- O Gemini substitui o Ollama como LLM de apoio (`llm.py`, `conversa.py`). A cidade do clima vem de `FEDORA_CIDADE` no `.env`.

### Corrigido
- O servidor não despeja mais traceback quando o navegador derruba a conexão (`Server.handle_error`, WinError 10053/10054).

### Removido
- Legado Linux da `master` (systemd, `.desktop`, scripts `.sh`) e `modelo.py`/serviço do Ollama.
