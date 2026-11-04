# Atalhos da Fedora

O Ollama lê este arquivo para escolher o atalho de cada pedido; o código em `atalhos.py` executa.
Pode editar os exemplos e os sites à vontade. Apps e jogos instalados entram sozinhos. Atalho novo aqui sem código lá vai para o Claude.

## Rápidos (resposta direta, sem Claude)

- `hora`: diz a hora certa. Ex.: "que horas são", "me diz as horas", "fala a hora aí"
- `data`: diz o dia de hoje. Ex.: "que dia é hoje", "hoje é dia quanto", "que dia da semana é hoje"
- `abrir_pasta` (arg = nome da pasta): Downloads, Documentos, Imagens, Músicas, Vídeos, Área de trabalho, pasta pessoal, Projetos, ou outra pasta da pasta pessoal. Ex.: "abre meus downloads", "abre a pasta projetos"
- `abrir` (arg = o NOME OFICIAL do programa, jogo da Steam ou site, como aparece no menu ou na loja, em inglês se for o caso): o ouvido escreve nomes em inglês do jeito que soam; escreva o nome certo. Ex.: "abre o espectáculo" → Spectacle, "abre o console" → Konsole, "bota o gta" → Grand Theft Auto V, "abre o cs2" → Counter-Strike 2, "abre o trilhear" → trailhead, "abre a calculadora" → KCalc, "abre o youtube" → youtube, "abre a steam" → Steam
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

