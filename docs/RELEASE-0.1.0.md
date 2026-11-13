# Fedora 0.1.0

Primeira versão estável da Fedora, assistente de voz local, no **Windows 11**. Licença MIT (veja [`LICENSE`](../LICENSE)).

## Destaques
- **Dois motores com troca por cota**: Claude (`claude -p`) e agy (Antigravity). Passa para o agy acima de 55% da semana ou 90% da janela de 5 h; as travas `DENY` valem nos dois.
- **Equipe de agentes** (junior, writer, design, arquiteto, senior, qa, techlead) com níveis N1–N3. Funciona em qualquer motor.
- **Despachante de backlog**: executa os cards do cofre do Obsidian e grava o status de cada um.
- **Fala em etapas**: avisa o início do pedido, quando repassa para um agente e quando ele devolve.
- **UI**: Markdown renderizado e Quadro & Backlog com o status real dos cards (coluna "Aguarda você", card "rodando"). Novos botões de voz muda e Interromper, e campo de escrita maior.
- **Windows nativo**: ouvido push-to-talk, faster-whisper, Piper e buraco negro flutuante. O Linux ficou congelado na branch `linux`.
- Servidor sem traceback quando o navegador derruba a conexão. Docs novas, incluindo `docs/DIAGRAMA.md`.

## Requisitos
Detalhes no roteiro "Versões e instalação" do [`README.md`](../README.md):
- Windows 11 e Python 3.12 (venv com `requirements.txt`)
- `claude` CLI instalado e logado (o agy é opcional)
- Piper e uma voz pt_BR
- Microfone e saída de áudio
- `.env` copiado de `.env.example`, com a `GEMINI_API_KEY`

Lista completa de mudanças: [`CHANGELOG.md`](../CHANGELOG.md).
