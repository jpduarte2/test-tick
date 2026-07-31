"""
Vigia a abertura da venda de bilhetes para os jogos do FC Porto em casa.

O site bilhetes.fcporto.pt e uma SPA: o HTML vem vazio e o conteudo chega por
uma API GraphQL publica. Lemos essa API em vez de raspar HTML, porque ela
devolve o estado de cada jogo num campo proprio (`status`) - nao dependemos do
texto do botao "COMPRAR" nem de mudancas de design.

Estados possiveis (enum MatchStatus):
    SCHEDULED  jogo marcado, venda ainda nao abriu
    OPEN       venda aberta   <- e isto que queremos apanhar
    SOLD_OUT   esgotado
    FINISHED   jogo ja decorrido

Avisamos quando um jogo passa para OPEN e deixamos de o seguir a partir dai:
uma vez aberta a venda, o jogo ja nao nos interessa.

A filtragem por futebol e por jogos em casa e feita do lado do servidor, com
os filtros `sport: FOOTBALL` e `locationTypes: [HOME]`. FOOTBALL exclui a
equipa B, os sub-19 e o futebol feminino, que sao desportos distintos na API,
mas inclui todas as provas da equipa principal - liga, tacas e Europa.

Dois ritmos
-----------
Nao vale a pena perguntar de 5 em 5 minutos pelo jogo de dezembro. Mas tambem
nao da para adivinhar quando abre a venda: o `localSaleStartsAt` so aparece
DEPOIS de abrir, o `salePhases` vem sempre vazio, e nao ha mais nenhum campo
com aviso previo.

Serve, isso sim, para medir o passado: na epoca 2025/26 as vendas abriram entre
4,2 e 30,1 dias antes do jogo (media 12,9). Uma regra do tipo "10 dias antes"
deixaria 19 dos 27 jogos a abrir fora da vigia atenta.

O que o clube faz e abrir a venda aos lotes, para os proximos jogos em casa.
Por isso o que varia e o ritmo, nao o que se olha:

    reconhecimento  de 6 em 6 horas, aconteca o que acontecer
    vigia           de 5 em 5 minutos se o proximo jogo por abrir for daqui a
                    menos de DIAS_RITMO_RAPIDO dias; senao de 30 em 30 minutos

Em qualquer dos casos perguntamos o calendario inteiro e detetamos aberturas em
qualquer jogo - restringir o pedido aos jogos mais proximos so pouparia uns KB
de resposta e criava um ponto cego.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API = "https://bilhetes-api.fcporto.pt/api/graphql"
SITE = "https://bilhetes.fcporto.pt"
ESTADO = Path(__file__).with_name("estado.json")

USER_AGENT = "fcp-bilhetes-alerta/1.0 (monitorizacao pessoal)"

# O formato do estado.json. Se mudar, o ficheiro antigo e ignorado e a
# execucao seguinte comporta-se como primeira (regista sem avisar).
VERSAO_ESTADO = 2

QUERY = """
query jogos($f: MatchesByDateFilters!) {
  matchesByDate(filters: $f) {
    matches {
      id
      sport
      status
      localStartsAt
      isDateConfirmed
      competition { name }
      homeTeam { shortName }
      awayTeam { shortName }
    }
  }
}
"""


def env(nome: str, predefinido: str = "") -> str:
    return (os.environ.get(nome) or predefinido).strip()


def env_int(nome: str, predefinido: int) -> int:
    """Numero vindo do ambiente. Um valor disparatado nao trava o programa."""
    bruto = env(nome)
    if not bruto:
        return predefinido
    try:
        valor = int(bruto)
    except ValueError:
        print(f"!! {nome}={bruto!r} nao e um numero; uso {predefinido}.")
        return predefinido
    if valor < 1:
        print(f"!! {nome}={valor} tem de ser positivo; uso {predefinido}.")
        return predefinido
    return valor


def agora() -> datetime:
    return datetime.now(timezone.utc)


def ler_data(bruto: str | None) -> datetime | None:
    """Data da API, ciente de fuso.

    Mantemos o offset original (+01:00/+00:00) em vez de converter para UTC:
    duas datas com fuso comparam-se bem entre si, e assim a hora que mostramos
    continua a ser a hora a que se joga em Lisboa.
    """
    if not bruto:
        return None
    try:
        d = datetime.fromisoformat(bruto)
    except ValueError:
        return None
    if d.tzinfo is None:
        return d.replace(tzinfo=timezone.utc)
    return d


# Teto de datas que pedimos a API. Na epoca 2025/26 houve 27 jogos em casa em
# 27 datas distintas ao longo do ano inteiro, portanto numa janela de 180 dias
# isto tem folga larga mesmo com tacas e provas europeias. Fica alto na mesma
# porque a API trunca em silencio - pedindo 3 devolve 3, sem erro nem aviso.
LIMITE_DATAS = 200


def pedir_api(dias: int) -> list[dict]:
    """Jogos em casa nos proximos [dias] dias. Tenta 3 vezes antes de desistir."""
    corpo = json.dumps(
        {
            "query": QUERY,
            "variables": {
                "f": {
                    "limitMatchDates": LIMITE_DATAS,
                    "sport": "FOOTBALL",
                    "locationTypes": ["HOME"],
                    "matchDate": {
                        "from": date.today().isoformat(),
                        "to": (date.today() + timedelta(days=dias)).isoformat(),
                    },
                }
            },
        }
    ).encode("utf-8")

    ultimo_erro: Exception | None = None
    for tentativa in range(3):
        try:
            pedido = urllib.request.Request(
                API,
                data=corpo,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
            with urllib.request.urlopen(pedido, timeout=30) as r:
                dados = json.loads(r.read().decode("utf-8"))
            if dados.get("errors"):
                raise RuntimeError(f"GraphQL devolveu erros: {dados['errors']}")
            grupos = dados["data"]["matchesByDate"]
            if len(grupos) >= LIMITE_DATAS:
                # Bateu no teto: e provavel que falte o fim do calendario e nao
                # temos como saber quais os jogos que ficaram de fora.
                print(
                    f"!! A API devolveu {len(grupos)} datas, o maximo que"
                    " pedimos. O calendario pode vir cortado - convem subir o"
                    " LIMITE_DATAS."
                )
            return [m for g in grupos for m in g["matches"]]
        except Exception as e:  # rede, JSON invalido, esquema alterado...
            ultimo_erro = e
            print(f"!! Tentativa {tentativa + 1} falhou: {e}")
            if tentativa < 2:
                time.sleep(5 * (tentativa + 1))

    # Sair com erro sem tocar no estado: se gravassemos agora, perdiamos a
    # memoria e a proxima execucao ja nao detetava a transicao.
    raise SystemExit(f"Nao consegui ler a API depois de 3 tentativas: {ultimo_erro}")


def descricao(m: dict) -> str:
    casa = (m.get("homeTeam") or {}).get("shortName") or "FC Porto"
    fora = (m.get("awayTeam") or {}).get("shortName") or "?"
    return f"{casa} x {fora}"


def quando(m: dict) -> str:
    d = ler_data(m.get("localStartsAt"))
    if not d:
        return "data por confirmar"
    # Jogo sem horario definido chega como 00:00; anunciar "as 00:00" seria
    # inventar uma hora que ninguem marcou.
    if not m.get("isDateConfirmed"):
        return d.strftime("%d/%m/%Y") + " (hora por confirmar)"
    return d.strftime("%d/%m/%Y as %H:%M")


def notificar(titulo: str, mensagem: str, url: str) -> None:
    topico = env("NTFY_TOPIC")
    if not topico:
        print("!! NTFY_TOPIC nao definido - mostro so no log:")
        print(f"   {titulo} | {mensagem}")
        return

    servidor = env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    pedido = urllib.request.Request(
        f"{servidor}/{topico}",
        data=mensagem.encode("utf-8"),
        headers={
            "Title": titulo,
            "Priority": "urgent",
            "Tags": "soccer,tickets",
            "Click": url,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(pedido, timeout=20) as r:
            r.read()
        print(f">> Notificacao enviada: {titulo}")
    except urllib.error.URLError as e:
        # Nao abortamos: perder o aviso e mau, mas nao gravar o estado seria
        # pior, porque repetiriamos o mesmo aviso falhado de 5 em 5 minutos.
        print(f"!! Falhou o envio da notificacao: {e}")


def carregar_estado() -> dict:
    """Estado anterior, ja validado.

    Devolve sempre {"jogos": {...}, "ultimo_reconhecimento": str|None}. Um
    ficheiro ilegivel, de outra versao ou com o conteudo trocado equivale a
    "nao sei nada": a execucao seguinte regista tudo sem avisar, em vez de
    disparar uma notificacao por cada jogo que ja esta a venda.
    """
    vazio: dict = {"versao": VERSAO_ESTADO, "ultimo_reconhecimento": None, "jogos": {}}
    if not ESTADO.exists():
        return vazio
    try:
        conteudo = json.loads(ESTADO.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"!! Estado ilegivel ({e}); recomeco do zero.")
        return vazio

    if not isinstance(conteudo, dict) or not conteudo:
        return vazio
    if conteudo.get("versao") != VERSAO_ESTADO:
        print("!! Estado de outra versao; recomeco do zero.")
        return vazio
    jogos = conteudo.get("jogos")
    if not isinstance(jogos, dict):
        return vazio

    conteudo["jogos"] = {i: j for i, j in jogos.items() if isinstance(j, dict)}
    return conteudo


def gravar_estado(estado: dict) -> None:
    # Os jogos vao por ordem de data e nao por id, senao o ficheiro sai baralhado
    # aos olhos de quem o abre. E so apresentacao: o programa procura sempre pelo
    # id. Jogos sem data legivel vao para o fim em vez de rebentar a ordenacao.
    longe = datetime.max.replace(tzinfo=timezone.utc)
    por_data = sorted(
        (estado.get("jogos") or {}).items(),
        key=lambda par: (ler_data(par[1].get("jogo_em")) or longe, par[1].get("descricao") or ""),
    )
    saida = {
        "versao": estado.get("versao", VERSAO_ESTADO),
        "ultimo_reconhecimento": estado.get("ultimo_reconhecimento"),
        "jogos": {i: {campo: j[campo] for campo in sorted(j)} for i, j in por_data},
    }
    ESTADO.write_text(
        json.dumps(saida, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def proximo_por_abrir(jogos: dict) -> tuple[str, int | None] | None:
    """O jogo mais proximo cuja venda ainda nao abriu, e a quantos dias esta.

    E este que decide o ritmo. Jogos ja despachados (venda aberta) e jogos que
    ja se realizaram ficam de fora. Um jogo sem data legivel vai para o fim da
    fila mas nao e descartado, e devolve dias=None, que o chamador trata como
    "nao sei, mais vale vigiar depressa".
    """
    limite = agora()
    candidatos = []
    for ident, j in jogos.items():
        if not j.get("seguir", True):
            continue
        d = ler_data(j.get("jogo_em"))
        if d and d < limite:
            continue
        candidatos.append((d is None, d or limite, ident))
    if not candidatos:
        return None
    candidatos.sort()
    sem_data, quando_e, ident = candidatos[0]
    return ident, None if sem_data else (quando_e - limite).days


# Largura da janela em que aceitamos correr no ritmo lento, em minutos. Nao
# guardamos a hora da ultima vigia (gravar o estado a cada 5 minutos daria um
# commit a cada 5 minutos), por isso decidimos so pelo relogio. A janela e mais
# larga do que um so tique porque o cron do GitHub atrasa-se com frequencia: se
# exigissemos o minuto exato, um atraso fazia-nos saltar a ronda inteira.
JANELA_LENTA_MIN = 10


def e_hora_da_vigia_lenta(momento: datetime, intervalo_min: int) -> bool:
    return momento.minute % intervalo_min < JANELA_LENTA_MIN


def main() -> None:
    dias = env_int("DIAS", 180)
    horas_reconhecimento = env_int("HORAS_RECONHECIMENTO", 6)
    dias_rapido = env_int("DIAS_RITMO_RAPIDO", 30)
    minutos_lenta = env_int("MINUTOS_VIGIA_LENTA", 30)

    estado = carregar_estado()
    jogos_antes: dict = estado["jogos"]
    # Sem historico tudo pareceria novidade e recebias um aviso por cada jogo
    # que ja esta a venda.
    primeira_vez = not jogos_antes

    ultimo = ler_data(estado.get("ultimo_reconhecimento"))
    reconhecimento = (
        primeira_vez
        or ultimo is None
        or agora() - ultimo >= timedelta(hours=horas_reconhecimento)
    )

    seguinte = proximo_por_abrir(jogos_antes)

    if reconhecimento:
        print(f"Reconhecimento: janela de {dias} dias.\n")
    else:
        if seguinte is None:
            # Nada por abrir e o reconhecimento ainda nao e devido: saimos sem
            # chamar a API.
            print("Nada por vigiar e reconhecimento ainda nao e devido; saio.")
            return

        # Ritmo: enquanto o jogo mais proximo por abrir estiver longe, nao vale
        # a pena perguntar de 5 em 5 minutos. Perde-se atraso, nao se perdem
        # aberturas. Ver o README para a calibracao do limiar - na epoca
        # 2025/26 a antecedencia maxima observada foi de 30,1 dias.
        ident, falta = seguinte
        nome = jogos_antes[ident].get("descricao", ident)
        if falta is not None and falta > dias_rapido:
            if not e_hora_da_vigia_lenta(agora(), minutos_lenta):
                print(
                    f"O proximo jogo por abrir ({nome}) so e daqui a {falta}"
                    f" dias: ritmo de {minutos_lenta} em {minutos_lenta}"
                    " minutos. Nao e a vez desta execucao; saio."
                )
                return
            ritmo = f"lento ({minutos_lenta} min, jogo a {falta} dias)"
        else:
            ritmo = "rapido (5 min)"

        print(f"Vigia, ritmo {ritmo}. Proximo por abrir: {nome}\n")

    # Perguntamos sempre o calendario todo, mesmo na vigia. Chegou-se a
    # restringir a janela aos jogos vigiados, mas isso so poupava uns KB de
    # resposta - o pedido HTTP e um so de qualquer maneira - e criava um ponto
    # cego: se a venda abrisse num jogo de tras da fila, ficavamos sem saber
    # ate ao reconhecimento seguinte. A poupanca a serio esta no ritmo.
    jogos = pedir_api(dias)
    print(f"{len(jogos)} jogos em casa na janela.\n")

    vistos: set[str] = set()
    for j in jogos:
        ident = j["id"]
        vistos.add(ident)
        estado_agora = j.get("status") or "?"
        anterior = jogos_antes.get(ident, {})
        estado_antes = anterior.get("estado")
        seguir = anterior.get("seguir", True)

        jogos_antes[ident] = {
            "descricao": descricao(j),
            "estado": estado_agora,
            "jogo_em": j.get("localStartsAt"),
            "seguir": seguir,
        }
        marca = "" if seguir else "  (despachado)"
        print(
            f"  {descricao(j):<32} {estado_antes or '(novo)':>10}"
            f" -> {estado_agora}{marca}"
        )

        if not seguir or estado_agora != "OPEN":
            continue

        # A venda abriu: avisa uma vez e nunca mais. Um jogo que ja vem OPEN da
        # primeira execucao de todas nao gera aviso; nas seguintes gera, porque
        # ai e mesmo um jogo novo no calendario (tipico das tacas).
        jogos_antes[ident]["seguir"] = False
        if primeira_vez:
            continue

        titulo = f"Bilhetes a venda: {descricao(j)}"
        linhas = ["Abriu a venda.", quando(j)]
        competicao = (j.get("competition") or {}).get("name")
        if competicao:
            linhas.append(competicao)
        notificar(titulo, "\n".join(linhas), f"{SITE}/jogos/{ident}")

    # Como perguntamos sempre o calendario todo, o que nao veio ja saiu da
    # janela (jogou-se ou foi adiado para la dos DIAS). Senao o ficheiro
    # crescia para sempre.
    for ident in set(jogos_antes) - vistos:
        del jogos_antes[ident]

    if reconhecimento:
        # Marcar a hora tem um segundo efeito util: garante que o estado.json
        # muda pelo menos de 6 em 6 horas e portanto que ha commit. O GitHub
        # desativa workflows agendados em repositorios parados ha 60 dias.
        estado["ultimo_reconhecimento"] = agora().isoformat()

    estado["versao"] = VERSAO_ESTADO
    estado["jogos"] = jogos_antes
    gravar_estado(estado)

    por_abrir = sum(1 for j in jogos_antes.values() if j.get("seguir", True))
    print(f"\nEstado gravado: {len(jogos_antes)} jogos, {por_abrir} por abrir.")


if __name__ == "__main__":
    sys.exit(main())
