# Atalhos da Fedora

O Ollama lê este arquivo para escolher o atalho de cada pedido; o código em `atalhos.py` executa.
Pode editar os exemplos, os sites e as palavras à vontade. Atalho novo aqui sem código lá vai para o Claude.

## Rápidos (resposta direta, sem Claude)

- `hora`: diz a hora certa. Ex.: "que horas são", "me diz as horas", "fala a hora aí"
- `data`: diz o dia de hoje. Ex.: "que dia é hoje", "hoje é dia quanto", "que dia da semana é hoje"
- `abrir_pasta` (arg = nome da pasta): Downloads, Documentos, Imagens, Músicas, Vídeos, Área de trabalho, pasta pessoal, Projetos, ou outra pasta da pasta pessoal. Ex.: "abre meus downloads", "abre a pasta projetos"
- `abrir_app` (arg = nome do app ou do jogo): abre um programa instalado ou um jogo da Steam. Ex.: "abre o chrome", "abre o dolphin", "abre o obsidian", "abre a calculadora", "abre o orca slicer", "abre a steam", "abre o cities skylines 2", "abre o cs2", "bota o terraria"
- `abrir_site` (arg = nome do site da lista "Sites" abaixo): Ex.: "abre o youtube", "abre o gmail", "abre o trailhead", "entra no github"
- `pesquisar_web` (arg = termo): abre uma busca do Google no navegador. Só quando o pedido diz pesquisar, procurar ou buscar NO GOOGLE/NA INTERNET. Ex.: "pesquisa preço do PLA", "procura no google suporte de filamento", "busca na internet receita de pão"
- `volume` (arg = número de 0 a 100, "mais", "menos" ou "mudo"): volume da caixa de som. Ex.: "volume 40", "aumenta o volume", "abaixa o som", "muta"
- `brilho` (arg = número de 0 a 100, "mais" ou "menos"): brilho dos monitores. Ex.: "brilho 50", "diminui o brilho", "aumenta o brilho"
- `midia` (arg = "tocar", "pausar", "próxima" ou "anterior"): controla o player (música, YouTube). Ex.: "pausa a música", "pula essa", "volta a música", "continua tocando"
- `print` (arg = "tela", "janela" ou "área"; vazio = tela): captura de tela. Ex.: "tira um print", "printa essa janela", "print de uma área"
- `timer` (arg = duração e, se houver, o motivo): avisa depois do tempo, falando. Ex.: "timer de 20 minutos", "me avisa em 1 hora pra tirar a peça da impressora", "me lembra daqui a meia hora de ligar pro cliente"
- `clima` (arg = cidade, vazio = Cerquilho): previsão do tempo de hoje ou amanhã. Ex.: "vai chover hoje?", "como tá o tempo amanhã", "qual a temperatura em Sorocaba"
- `sistema`: uso de RAM e placa de vídeo. Ex.: "como está o sistema", "quanto de memória estou usando"
- `descarregar_modelo`: tira o modelo do Ollama da placa de vídeo. Ex.: "descarrega o modelo", "libera a placa de vídeo"
- `desligar_ollama`: para o serviço do Ollama (volta quando a janela da Fedora abrir de novo). Ex.: "desliga o ollama"

## Pela janela (já instantâneos, não passam por aqui)

- notas diárias, ditar, organizar, salvar, voltar, desfazer

## Pelo Claude (precisam pensar, pesquisar ou confirmar)

- `nenhum`: qualquer outra coisa. Perguntas para responder falando (inclusive "quanto custa X", "quem é Y"), contas, notícias, escrever ou criar arquivo, mover ou renomear arquivo, fechar app (pergunta "confirma?" antes), tarefas com mais de um passo.
- Apagar não é atalho: só pelo Claude, para a Lixeira e com confirmação.
- Desligar, reiniciar ou suspender o PC: a Fedora não faz.

## Sites

- youtube: https://www.youtube.com
- gmail: https://mail.google.com
- trailhead: https://trailhead.salesforce.com
- whatsapp: https://web.whatsapp.com
- shopee: https://shopee.com.br
- github: https://github.com

## Palavras (nomes em inglês)

O ouvido transcreve em português e escreve esses nomes do jeito que soam ("espectáculo" = Spectacle, "console" = Konsole, "trilhear" = Trailhead, "obsidiana" = Obsidian). No `arg`, escreva sempre o nome certo desta lista quando o pedido soar parecido com um deles.


Chrome, Obsidian, Spectacle, Dolphin, Konsole, Kate, Discord, Steam, Spotify, Firefox, Minecraft, OrcaSlicer, CrealityPrint, OpenSCAD, Visual Studio Code, LibreOffice, Okular, KCalc, LocalSend, RustDesk, Prism Launcher, MakerFlow, Claude, YouTube, Gmail, Trailhead, WhatsApp, Shopee, GitHub, Google, print, timer, PLA, Steam, Counter-Strike, Cities: Skylines, Grand Theft Auto, GTA, ARK, Terraria, tModLoader, Albion Online, The Sandbox Evolution
