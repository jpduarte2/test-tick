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

Avisamos no maximo duas vezes por jogo:

  1. "Venda em breve" - quando o clube comeca a preparar a venda de um jogo
     ainda SCHEDULED. O `status` nao muda nessa fase, mas mudam outros campos:
     o `onlineSale` liga (e o cartao do site ganha o botao "A venda em
     breve"), o `allowPublicPurchase` liga, aparece o `localSaleStartsAt`
     (a hora agendada da abertura - o status passa a OPEN exatamente a essa
     hora) ou sao publicadas `salePhases` (a pagina do jogo passa a listar
     "Fases de venda", cada uma com "A venda a <data>"). Qualquer destes
     sinais chega para avisar. Se depois aparecer ou mudar uma data, avisamos
     outra vez, so dessa.
  2. "Bilhetes a venda" - quando passa para OPEN, tenha ou nao havido o aviso
     "em breve" antes. E o aviso principal. So depois dele deixamos de seguir
     o jogo: uma vez aberta a venda, nao ha mais nada para avisar.

O separador "Em breve" do proprio site nao serve de sinal: e um filtro da API
(`saleStatus: COMING_SOON`) que devolve simplesmente todos os jogos SCHEDULED.

A filtragem por futebol e por jogos em casa e feita do lado do servidor, com
os filtros `sport: FOOTBALL` e `locationTypes: [HOME]`. FOOTBALL exclui a
equipa B, os sub-19 e o futebol feminino, que sao desportos distintos na API,
mas inclui todas as provas da equipa principal - liga, tacas e Europa.

Dois ritmos
-----------
Nao vale a pena perguntar de 5 em 5 minutos pelo jogo de dezembro. Mas tambem
nao da para adivinhar quando abre a venda: enquanto o clube nao prepara a
venda, o `localSaleStartsAt` vem null, o `salePhases` vazio e os booleanos
desligados. Quando os preenche pode ser com pouca antecedencia (no Academico
de 28/10/2026 foi no proprio dia da abertura) - e ai o jogo passa a "em breve".

Serve, isso sim, para medir o passado: na epoca 2025/26 as vendas abriram entre
4,2 e 30,1 dias antes do jogo (media 12,9). Uma regra do tipo "10 dias antes"
deixaria 19 dos 27 jogos a abrir fora da vigia atenta.

O que o clube faz e abrir a venda aos lotes, para os proximos jogos em casa.
Por isso o que varia e o ritmo, nao o que se olha:

    reconhecimento  de 6 em 6 horas, aconteca o que acontecer
    vigia           de 5 em 5 minutos se o proximo jogo por abrir for daqui a
                    menos de DIAS_RITMO_RAPIDO dias; senao de 30 em 30 minutos

Um jogo com data de abertura anunciada conta pela data da abertura e nao pela
do jogo: a partir dai sabemos quando e, e queremos la estar.

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
# execucao seguinte comporta-se como primeira (regista sem avisar) - salvo se
# for de uma versao que saibamos atualizar no lugar (ver carregar_estado).
# v3: passou a guardar os sinais de venda (venda_online, venda_publico,
#     venda_abre_em, fases) para detetar a fase "em breve".
VERSAO_ESTADO = 3

QUERY = """
query jogos($f: MatchesByDateFilters!) {
  matchesByDate(filters: $f) {
    matches {
      id
      sport
      status
      localStartsAt
      isDateConfirmed
      onlineSale
      allowPublicPurchase
      localSaleStartsAt
      salePhases { description startDate }
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


def formatar(d: datetime | None) -> str:
    return d.strftime("%d/%m/%Y as %H:%M") if d else "data por anunciar"


def sinais_de_venda(m: dict) -> dict:
    """O que a API ja diz sobre a venda de um jogo, para alem do `status`.

    Sao estes os campos que mudam quando o clube prepara a venda de um jogo
    ainda SCHEDULED - e o que o site mostra como "A venda em breve" no cartao
    e "Fases de venda" na pagina do jogo. Guardamo-los no estado com estes
    mesmos nomes, para comparar de uma execucao para a outra.
    """
    fases = [
        {"descricao": f.get("description") or "", "inicio": f.get("startDate")}
        for f in (m.get("salePhases") or [])
        if isinstance(f, dict)
    ]
    return {
        "venda_online": bool(m.get("onlineSale")),
        "venda_publico": bool(m.get("allowPublicPurchase")),
        "venda_abre_em": m.get("localSaleStartsAt"),
        "fases": fases,
    }


def anunciado(sinais: dict) -> bool:
    """Ha algum sinal de que a venda esta a ser preparada?"""
    return bool(
        sinais.get("venda_online")
        or sinais.get("venda_publico")
        or sinais.get("venda_abre_em")
        or sinais.get("fases")
    )


def datas_anunciadas(sinais: dict) -> tuple:
    """So as datas. Depois do primeiro aviso, e so destas que voltamos a avisar."""
    return (
        sinais.get("venda_abre_em"),
        tuple((f.get("descricao"), f.get("inicio")) for f in sinais.get("fases") or []),
    )


def mensagem_do_anuncio(j: dict, sinais: dict) -> str:
    abre = ler_data(sinais.get("venda_abre_em"))
    if abre:
        linhas = [f"Venda abre a {formatar(abre)}."]
    else:
        linhas = ["O clube esta a preparar a venda; ainda sem data."]
    for f in sinais.get("fases") or []:
        linhas.append(f"{f.get('descricao') or 'Fase'}: {formatar(ler_data(f.get('inicio')))}")
    linhas.append(f"Jogo: {quando(j)}")
    competicao = (j.get("competition") or {}).get("name")
    if competicao:
        linhas.append(competicao)
    return "\n".join(linhas)


def notificar(titulo: str, mensagem: str, url: str, prioridade: str = "urgent") -> None:
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
            "Priority": prioridade,
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
    if conteudo.get("versao") == 2:
        # A v3 so acrescentou os sinais de venda; o resto e igual. Nao vale a
        # pena deitar fora a memoria: sem ela, uma venda que abrisse mesmo na
        # primeira execucao passava em silencio (o cron do GitHub chega a
        # atrasar-se horas). Sem sinais gravados, o que aparecer e novidade.
        print("!! Estado da versao 2; atualizo para a 3 sem perder a memoria.")
        conteudo["versao"] = VERSAO_ESTADO
    elif conteudo.get("versao") != VERSAO_ESTADO:
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
    ja se realizaram ficam de fora. Um jogo com data de abertura anunciada
    conta pela data da abertura e nao pela do jogo: e essa que queremos
    apanhar; se ja passou e o status ainda nao mudou, conta como "agora". Um
    jogo sem data legivel vai para o fim da fila mas nao e descartado, e
    devolve dias=None, que o chamador trata como "nao sei, mais vale vigiar
    depressa".
    """
    limite = agora()
    candidatos = []
    for ident, j in jogos.items():
        if not j.get("seguir", True):
            continue
        d = ler_data(j.get("jogo_em"))
        if d and d < limite:
            continue
        abre = ler_data(j.get("venda_abre_em"))
        if abre:
            d = max(abre, limite)
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
    # No GitHub Actions, ficar sem topico e um erro de configuracao, nao uma
    # escolha: o unico efeito do programa e notificar. Falhamos aqui, antes de
    # mexer no estado, senao a abertura era dada por avisada sem aviso nenhum
    # e o jogo ficava despachado em silencio.
    if env("GITHUB_ACTIONS") and not env("NTFY_TOPIC"):
        raise SystemExit(
            "NTFY_TOPIC nao esta definido. Cria o segredo em Settings >"
            " Secrets and variables > Actions (o nome tem de ser exatamente"
            " NTFY_TOPIC) e volta a correr."
        )

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

        sinais = sinais_de_venda(j)
        jogos_antes[ident] = {
            "descricao": descricao(j),
            "estado": estado_agora,
            "jogo_em": j.get("localStartsAt"),
            "seguir": seguir,
            **sinais,
        }
        if not seguir:
            marca = "  (despachado)"
        elif estado_agora == "OPEN":
            marca = ""
        elif sinais["venda_abre_em"]:
            marca = f"  (em breve: abre a {formatar(ler_data(sinais['venda_abre_em']))})"
        elif anunciado(sinais):
            marca = "  (em breve)"
        else:
            marca = ""
        print(
            f"  {descricao(j):<32} {estado_antes or '(novo)':>10}"
            f" -> {estado_agora}{marca}"
        )

        if not seguir:
            continue
        url = f"{SITE}/jogos/{ident}"

        if estado_agora == "OPEN":
            # A venda abriu: avisa uma vez e nunca mais. Um jogo que ja vem
            # OPEN da primeira execucao de todas nao gera aviso; nas seguintes
            # gera, porque ai e mesmo um jogo novo no calendario (tipico das
            # tacas).
            jogos_antes[ident]["seguir"] = False
            if primeira_vez:
                continue
            titulo = f"Bilhetes a venda: {descricao(j)}"
            linhas = ["Abriu a venda.", quando(j)]
            competicao = (j.get("competition") or {}).get("name")
            if competicao:
                linhas.append(competicao)
            notificar(titulo, "\n".join(linhas), url)
            continue

        # Ainda nao abriu. Ha novidade sobre a venda? Avisamos quando o jogo
        # passa a "em breve" e, dai em diante, so se aparecer ou mudar uma
        # data: os booleanos podem ligar-se com minutos de diferenca e nao
        # vale um aviso por cada um. Na primeira execucao nada e novidade.
        # `anterior` vem vazio para um jogo novo no calendario, e um jogo que
        # ja entra anunciado e novidade na mesma.
        if primeira_vez or not anunciado(sinais):
            continue
        if anunciado(anterior):
            if datas_anunciadas(sinais) == datas_anunciadas(anterior):
                continue
            titulo = f"Venda em breve (nova data): {descricao(j)}"
        else:
            titulo = f"Venda em breve: {descricao(j)}"
        notificar(titulo, mensagem_do_anuncio(j, sinais), url, prioridade="high")

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

    por_abrir = [j for j in jogos_antes.values() if j.get("seguir", True)]
    em_breve = sum(1 for j in por_abrir if anunciado(j))
    print(
        f"\nEstado gravado: {len(jogos_antes)} jogos, {len(por_abrir)} por abrir"
        f" ({em_breve} em breve)."
    )


if __name__ == "__main__":
    sys.exit(main())
