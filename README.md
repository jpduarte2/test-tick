# Alerta de bilhetes do FC Porto

Avisa no telemovel quando abre a venda de bilhetes para um jogo do **FC Porto
em casa**. Corre no GitHub Actions de 5 em 5 minutos; nao e preciso ter o
computador ligado.

## Como funciona

O site `bilhetes.fcporto.pt` e uma aplicacao React: o HTML que chega ao
navegador vem vazio e o conteudo e carregado depois por JavaScript. Raspar o
HTML nao daria nada.

Em vez disso falamos diretamente com a API GraphQL que o proprio site usa:

```
https://bilhetes-api.fcporto.pt/api/graphql
```

E publica (nao pede autenticacao) e cada jogo traz um campo `status` com um de
quatro valores: `SCHEDULED`, `OPEN`, `SOLD_OUT`, `FINISHED`. Isto e muito mais
fiavel do que procurar a palavra "COMPRAR" num botao, porque nao se parte
quando eles mudarem o design.

O `monitor.py` guarda o ultimo estado conhecido de cada jogo em `estado.json`
e avisa quando algum passa para `OPEN`. A partir do momento em que a venda
abre, **deixa de seguir esse jogo** — ja cumpriu o que tinha a fazer.

Filtramos do lado do servidor com `sport: FOOTBALL` e `locationTypes: [HOME]`.
Na API o futebol da equipa B, dos sub-19 e feminino sao desportos diferentes
(`FOOTBALL_TEAM_B`, etc.), por isso `FOOTBALL` traz mesmo so a equipa
principal — mas traz **todas as provas**. Na epoca 2025/26 foram 27 jogos em
casa: 15 da Liga, 6 da Liga Europa, 4 da Taca de Portugal, 1 da Taca da Liga e
a apresentacao do plantel. Nao ha nada a acrescentar quando comecarem as tacas
ou as provas europeias; entram sozinhas.

## Porque nao vigia tudo ao mesmo ritmo

Nao vale a pena perguntar de 5 em 5 minutos pelo jogo de dezembro. O problema e
que **a API nao da qualquer aviso previo** de quando a venda abre:

- o `localSaleStartsAt` vem `null` em todos os jogos e so aparece *depois* de
  a venda abrir;
- o `salePhases` vem sempre vazio;
- o `allowPublicPurchase` e o `onlineSale` mudam ao mesmo tempo que o `status`.

Resta perguntar amiude. A questao e quando.

### Com que antecedencia abre a venda, na pratica

O `localSaleStartsAt` e inutil para prever, mas serve para *medir*: fica
registado depois de a venda abrir. Nos 27 jogos em casa da epoca 2025/26:

| | Dias entre a abertura e o jogo |
|---|---|
| Minimo | 4,2 (Santa Clara, Liga) |
| Media | 12,9 |
| **Maximo** | **30,1 (Estoril Praia, Liga)** |

E daqui que sai o `DIAS_RITMO_RAPIDO`. Uma regra do tipo "vigiar so nos 10 dias
antes do jogo" deixaria **19 dos 27 jogos (70%)** a abrir em ritmo lento —
incluindo o Benfica, o Sporting e cinco dos seis jogos europeus, que sao
precisamente os que esgotam depressa.

O que o clube faz e abrir a venda **aos lotes, para os proximos jogos em
casa**, com antecedencia variavel e sem data anunciada.

Por isso o que o script varia e o **ritmo**, nao o que olha (o cron do GitHub e
fixo e nao da para agendar de forma dinamica):

| | Quando |
|---|--------|
| **Reconhecimento** | de 6 em 6 horas, aconteca o que acontecer |
| **Vigia rapida** | de 5 em 5 minutos, quando o proximo jogo por abrir e daqui a **30 dias ou menos** |
| **Vigia lenta** | de 30 em 30 minutos, quando ainda falta mais tempo |

Em qualquer dos casos pergunta-se o **calendario inteiro** e detetam-se
aberturas em qualquer jogo, esteja ele em que posicao estiver na fila. Chegou a
haver aqui uma optimizacao que restringia o pedido aos proximos jogos, mas so
poupava uns KB de resposta — o pedido HTTP e um so de qualquer maneira — e
criava um ponto cego: se a venda abrisse num jogo de tras da fila, ficava-se
sem saber ate ao reconhecimento seguinte.

**Sobre a margem do limiar.** O maximo observado numa epoca foi 30,1 dias
(Estoril Praia). Como os dias sao contados por truncagem — um jogo a 30,9 dias
conta como 30 — a fronteira real esta nos 31 dias, e os 27 jogos da epoca
2025/26 teriam sido todos apanhados em ritmo rapido. Ainda assim a folga e de
menos de um dia: se alguma vez reparares que chegaste tarde a uma abertura, poe
o `DIAS_RITMO_RAPIDO` em 40.

Passar ao ritmo lento **nao faz perder aberturas, so as descobre mais tarde**:
ate 30 minutos em vez de 5.

**Nao e uma questao de performance:** o repositorio e publico, portanto os
minutos do GitHub Actions sao gratuitos e cada execucao e um pedido HTTPS
pequeno. O que se poupa e incomodo a API do clube — 48 pedidos por dia no ritmo
lento em vez de 288; o que se paga e atraso a saber.

Com o calendario cheio (tacas, Europa, dois jogos em casa numa semana) o modo
lento praticamente desaparece: ha quase sempre um jogo por abrir dentro dos 30
dias. So volta nas paragens e no defeso. Nao e preciso mexer em nada para isso
acontecer.

### Como se corre menos vezes se o cron e fixo?

O agendamento e sempre de 5 em 5 minutos — o GitHub nao permite outra coisa. E
o proprio script que, no ritmo lento, desiste das execucoes que nao lhe
competem, olhando so para o relogio: corre nos primeiros 10 minutos de cada
meia hora e sai de imediato nas restantes. A janela e de 10 minutos e nao de um
minuto exato porque o cron do GitHub atrasa-se com frequencia; se exigisse o
minuto certo, um atraso fazia saltar a ronda inteira.

Nao guardamos a hora da ultima vigia porque isso obrigaria a gravar o
`estado.json` a cada 5 minutos — e o workflow faz commit do ficheiro sempre que
ele muda, o que encheria o historico de commits inuteis.

## Por em funcionamento

### 1. Instalar o ntfy no telemovel

O [ntfy](https://ntfy.sh) e gratuito e nao exige conta.

1. Instala a app (Android/iOS).
2. Subscreve um topico. **Escolhe um nome dificil de adivinhar**, tipo
   `fcp-jpd-7k2x9qm`: os topicos do ntfy.sh sao publicos e qualquer pessoa que
   acerte no nome recebe (e pode enviar) as tuas notificacoes.

### 2. Confirmar que funciona antes de publicar

Precisas de Python 3.9 ou superior. Nao ha dependencias a instalar: so
biblioteca padrao.

```powershell
python testar.py
```

Corre 20 verificacoes contra um calendario inventado — sem rede e sem tocar no
teu `estado.json`. Deve acabar em `Tudo certo.`

Depois, uma ronda a serio contra a API do clube, sem notificar ninguem:

```powershell
python monitor.py
```

Deve listar os jogos em casa e acabar em `Estado gravado`. Como e a primeira
execucao, nao envia avisos — so regista.

### 3. Confirmar que a notificacao chega ao telemovel

Este e o passo que mais vezes falha em silencio (topico mal escrito, app sem
subscricao). Vale a pena fazer:

```powershell
$env:NTFY_TOPIC = "o-teu-topico"
python testar.py --notificar
```

Deve aparecer uma notificacao no telemovel. Se nao aparecer, confirma o nome do
topico na app — e o mesmo erro em 9 de cada 10 casos.

### 4. Criar o repositorio

Cria um repositorio **publico** no GitHub e envia estes ficheiros. Publico
porque o GitHub Actions e ilimitado em repositorios publicos; num privado os
2000 minutos/mes gastavam-se em poucos dias a correr de 5 em 5 minutos.

Nao ha aqui nada sensivel — o topico do ntfy vai num secret.

**Antes de enviar, poe o `estado.json` de volta a zeros** (`{}`), senao levas
para o repositorio o estado dos testes:

```bash
echo "{}" > estado.json
git init
git add .
git commit -m "Alerta de bilhetes do FC Porto"
git branch -M main
git remote add origin https://github.com/<utilizador>/<repo>.git
git push -u origin main
```

### 5. Definir o segredo

No repositorio: **Settings > Secrets and variables > Actions > New repository
secret**

| Nome | Valor |
|------|-------|
| `NTFY_TOPIC` | o nome do topico que subscreveste |

Sem isto o workflow corre a direito mas os avisos ficam so no log — nao te
chega nada ao telemovel.

### 6. Ligar

Em **Actions**, ativa os workflows e corre "Vigiar bilhetes" a mao
(*Run workflow*). A partir dai fica sozinho.

A **primeira execucao nao envia avisos** — so regista o estado atual. Se
enviasse, receberias uma notificacao por cada jogo que ja esta a venda. A
partir da segunda, avisa so nas mudancas.

### 7. Ver se esta mesmo a trabalhar

Passados uns minutos:

- **Actions** mostra execucoes de 5 em 5 minutos. As que apanham o ritmo lento
  duram segundos e dizem `Nao e a vez desta execucao; saio`. E o esperado.
- O **historico de commits** tem entradas `Estado dos jogos [skip ci]` pelo
  menos de 6 em 6 horas. Se passar um dia inteiro sem nenhum, algo esta mal.
- O `estado.json` no repositorio deve ter os jogos em casa todos, com
  `"seguir": true` nos que ainda nao abriram venda.

O teste de fogo — receber um aviso a serio — so acontece quando o clube abrir
uma venda. Para nao ficares na duvida ate la, o passo 3 confirma a parte que
esta debaixo do teu controlo.

## Testar depois de mexer no codigo

```powershell
python testar.py
```

O `testar.py` substitui a API por um calendario inventado e encena os casos que
interessam: a venda a abrir, o aviso a nao se repetir, varios jogos a abrir ao
mesmo tempo, a venda a abrir num jogo mais atras na fila, a escolha do ritmo, o
reconhecimento periodico, jogos a entrar e a sair do calendario e o ficheiro de
estado corrompido ou de outra versao.

Sai com codigo de erro se alguma verificacao falhar, portanto serve tal e qual
para um workflow de CI.

Para veres o comportamento a olho, sem notificar ninguem, deixa o `NTFY_TOPIC`
por definir e corre o `monitor.py` — os avisos aparecem no ecra em vez de irem
para o telemovel.

Para forcar um alerta de teste com dados reais, abre o `estado.json` e num jogo
que ja esteja a venda poe `"estado": "SCHEDULED"` e `"seguir": true`. Na
execucao seguinte ele deteta a "abertura" e avisa.

## Configuracao

Variaveis de ambiente, todas opcionais:

| Variavel | Por omissao | Para que serve |
|----------|-------------|----------------|
| `NTFY_TOPIC` | — | Topico do ntfy. Sem ele, so escreve no log. |
| `NTFY_SERVER` | `https://ntfy.sh` | Se tiveres servidor ntfy proprio. |
| `DIAS` | `180` | Janela do calendario, em dias a partir de hoje. |
| `HORAS_RECONHECIMENTO` | `6` | Intervalo maximo entre execucoes, mesmo em ritmo lento. |
| `DIAS_RITMO_RAPIDO` | `30` | Ate quantos dias antes do jogo se vigia de 5 em 5 minutos. |
| `MINUTOS_VIGIA_LENTA` | `30` | Intervalo da vigia quando o jogo ainda esta longe. |

Se o clube abrir a venda de varios jogos ao mesmo tempo, sao todos detetados na
mesma execucao — perguntamos sempre o calendario completo.

## Limitacoes

- **O cron do GitHub nao e pontual.** O minimo sao 5 minutos e sob carga
  atrasa-se com frequencia 10 a 20 minutos. Para uma bancada que esgota em
  minutos, isto pode nao chegar. Se precisares mesmo de reacao rapida, tens de
  correr o script numa maquina sempre ligada (um Raspberry Pi, por exemplo) com
  um intervalo de 60 segundos.
- **Nao avisamos de devolucoes.** Como deixamos de seguir o jogo assim que a
  venda abre, um jogo que esgote e depois volte a ter bilhetes (`SOLD_OUT` ->
  `OPEN`) nao gera aviso. E uma escolha deliberada: o que interessa e apanhar a
  abertura.
- **A API nao avisa com antecedencia.** Nenhum campo diz quando a venda vai
  abrir (ver acima), por isso nao ha como preparar-se para o momento — so
  perguntar amiude.
- **O limiar dos 30 dias tem pouca folga.** Na epoca 2025/26 a antecedencia
  maxima observada foi de 30,1 dias (Estoril Praia) — dentro do limiar, mas por
  pouco. Se reparares que chegaste tarde a uma abertura, sobe o
  `DIAS_RITMO_RAPIDO` para 40.
- **O GitHub desativa os workflows agendados** em repositorios sem atividade
  ha 60 dias. O reconhecimento grava sempre a hora a que correu, o que garante
  um commit por dia mesmo em epoca morta — de proposito, para o workflow nao
  adormecer.
- **A API pode mudar sem aviso.** Se o esquema mudar, o script falha
  ruidosamente (o workflow fica vermelho) em vez de ficar calado a fingir que
  esta tudo bem.
- **Nao detetamos disponibilidade por bancada.** Um jogo `OPEN` pode ja nao ter
  bilhetes na zona que queres.

## Ficheiros

| Ficheiro | O que e |
|----------|---------|
| `monitor.py` | Todo o trabalho: le a API, compara, notifica. |
| `testar.py` | Verifica o monitor contra um calendario inventado. |
| `estado.json` | Ultimo estado conhecido. Gerado automaticamente. |
| `.github/workflows/monitor.yml` | Agendamento e commit do estado. |
