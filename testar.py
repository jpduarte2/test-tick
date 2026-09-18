"""
Verifica que o monitor faz o que promete, sem tocar na rede nem no teu estado.

Substituimos a API por um calendario inventado e encenamos os casos que
interessam: a venda a abrir, o aviso a nao se repetir, os ritmos, o ficheiro de
estado corrompido. Corre isto sempre que mexeres no monitor.py.

    python testar.py

No fim, se quiseres confirmar que a notificacao chega mesmo ao telemovel:

    python testar.py --notificar
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import monitor

falhas: list[str] = []
calendario: dict[str, dict] = {}
avisos: list[str] = []      # titulos das notificacoes, por ordem
mensagens: list[str] = []   # e os corpos, na mesma ordem


def jogo(nome: str, dias: float, estado: str = "SCHEDULED") -> dict:
    quando = monitor.agora() + timedelta(days=dias)
    return {
        "id": nome,
        "status": estado,
        "localStartsAt": quando.isoformat(),
        "isDateConfirmed": True,
        # Sinais de venda, todos a zero: e assim que um jogo marcado chega.
        "onlineSale": False,
        "allowPublicPurchase": False,
        "localSaleStartsAt": None,
        "salePhases": [],
        "competition": {"name": "Teste"},
        "homeTeam": {"shortName": "FC Porto"},
        "awayTeam": {"shortName": nome},
    }


def api_falsa(dias: int) -> list[dict]:
    fim = monitor.agora() + timedelta(days=dias)
    # A janela e decidida pelo servidor; um jogo sem data ainda assim vem.
    return [
        j
        for j in calendario.values()
        if (monitor.ler_data(j["localStartsAt"]) or fim) <= fim
    ]


def notificar_falso(titulo: str, mensagem: str, url: str, prioridade: str = "urgent") -> None:
    avisos.append(titulo)
    mensagens.append(mensagem)


def correr() -> str:
    """Corre o monitor uma vez e devolve o que ele escreveu no ecra."""
    import io
    from contextlib import redirect_stdout

    saida = io.StringIO()
    with redirect_stdout(saida):
        monitor.main()
    return saida.getvalue()


def verificar(descricao: str, condicao: bool) -> None:
    print(f"  {'ok  ' if condicao else 'FALHA'}  {descricao}")
    if not condicao:
        falhas.append(descricao)


def estado_atual() -> dict:
    return json.loads(monitor.ESTADO.read_text(encoding="utf-8"))


def preparar(*jogos: dict) -> None:
    calendario.clear()
    for j in jogos:
        calendario[j["id"]] = j
    avisos.clear()
    mensagens.clear()
    monitor.ESTADO.unlink(missing_ok=True)


def testa_primeira_execucao() -> None:
    print("\nPrimeira execucao: regista tudo, nao avisa de nada")
    preparar(jogo("ja-aberto", 10, "OPEN"), jogo("fechado", 20))
    correr()
    verificar("nao envia avisos de jogos que ja estavam a venda", avisos == [])
    verificar("guarda os dois jogos", len(estado_atual()["jogos"]) == 2)


def testa_abertura() -> None:
    print("\nAbertura da venda: avisa uma vez e nunca mais")
    preparar(jogo("alvo", 10))
    correr()
    verificar("silencio enquanto esta SCHEDULED", avisos == [])

    calendario["alvo"]["status"] = "OPEN"
    correr()
    verificar("avisa quando abre", len(avisos) == 1)

    correr()
    correr()
    verificar("nao repete o aviso", len(avisos) == 1)
    verificar("marca o jogo como despachado",
              estado_atual()["jogos"]["alvo"]["seguir"] is False)


def testa_em_breve() -> None:
    print("\nVenda em breve: avisa quando o clube prepara a venda, e depois quando abre")
    # Todos os jogos daqui ficam a 30 dias ou menos: mais longe entra-se no
    # ritmo lento, que so corre em certos minutos, e o teste dependia da hora.
    preparar(jogo("alvo", 20))
    correr()
    verificar("silencio enquanto nao ha sinais de venda", avisos == [])

    calendario["alvo"]["onlineSale"] = True
    correr()
    verificar("avisa quando o onlineSale liga",
              avisos == ["Venda em breve: FC Porto x alvo"])
    verificar("diz que ainda nao ha data", "ainda sem data" in mensagens[-1])
    verificar("grava o sinal no estado",
              estado_atual()["jogos"]["alvo"]["venda_online"] is True)

    calendario["alvo"]["allowPublicPurchase"] = True
    correr()
    correr()
    verificar("nao repete o aviso quando liga mais um booleano", len(avisos) == 1)

    abre = monitor.agora() + timedelta(days=2)
    calendario["alvo"]["localSaleStartsAt"] = abre.isoformat()
    correr()
    verificar("volta a avisar quando aparece a data de abertura",
              len(avisos) == 2 and avisos[-1].startswith("Venda em breve (nova data)"))
    verificar("a mensagem traz a data",
              f"Venda abre a {abre.strftime('%d/%m/%Y as %H:%M')}" in mensagens[-1])
    correr()
    verificar("nao repete a data", len(avisos) == 2)

    calendario["alvo"]["status"] = "OPEN"
    correr()
    verificar("avisa da abertura como sempre",
              len(avisos) == 3 and avisos[-1] == "Bilhetes a venda: FC Porto x alvo")
    verificar("e deixa de seguir o jogo",
              estado_atual()["jogos"]["alvo"]["seguir"] is False)

    print("\nOutros sinais de 'em breve'")
    preparar(jogo("fases", 15))
    correr()
    calendario["fases"]["salePhases"] = [
        {"description": "Socios", "startDate": abre.isoformat()},
        {"description": "Publico", "startDate": None},
    ]
    correr()
    verificar("as fases de venda tambem contam como anuncio", len(avisos) == 1)
    verificar("a mensagem lista as fases",
              "Socios:" in mensagens[-1] and "Publico: data por anunciar" in mensagens[-1])

    preparar(jogo("so-data", 15))
    correr()
    calendario["so-data"]["localSaleStartsAt"] = abre.isoformat()
    correr()
    verificar("a data de abertura sozinha tambem conta", len(avisos) == 1)

    print("\nJogos que ja vem anunciados")
    preparar(jogo("ja-anunciado", 20))
    calendario["ja-anunciado"]["onlineSale"] = True
    correr()
    verificar("na primeira execucao so regista", avisos == [])
    correr()
    verificar("e nao avisa depois, porque nada mudou", avisos == [])

    preparar(jogo("existente", 20))
    correr()
    calendario["novo"] = jogo("novo", 25)
    calendario["novo"]["onlineSale"] = True
    correr()
    verificar("jogo novo que entra ja anunciado gera aviso",
              avisos == ["Venda em breve: FC Porto x novo"])


def testa_varios_ao_mesmo_tempo() -> None:
    print("\nVenda de varios jogos ao mesmo tempo")
    preparar(jogo("a", 7), jogo("b", 14), jogo("c", 21))
    correr()
    for i in ("a", "b", "c"):
        calendario[i]["status"] = "OPEN"
    correr()
    verificar("apanha os tres na mesma execucao", len(avisos) == 3)


def testa_jogo_de_tras_da_fila() -> None:
    print("\nVenda que abre num jogo mais atras na fila")
    preparar(jogo("perto", 7), jogo("medio", 14), jogo("longe", 21))
    correr()
    calendario["longe"]["status"] = "OPEN"
    correr()
    verificar("apanha o jogo de tras sem esperar pelo reconhecimento",
              len(avisos) == 1)


def testa_ritmos() -> None:
    print("\nEscolha do ritmo conforme a proximidade")
    for dias, esperado in ((10, "rapido"), (29, "rapido"), (30, "rapido"),
                           (45, "lento"), (120, "lento")):
        preparar(jogo("unico", dias))
        correr()  # primeira execucao = reconhecimento
        texto = correr()
        # No ritmo lento o script pode sair sem falar da vigia, conforme o
        # relogio; o que importa e que nunca se anuncie o ritmo errado.
        errado = "lento" if esperado == "rapido" else "rapido"
        verificar(f"jogo a {dias:>3} dias -> {esperado}",
                  f"ritmo {errado}" not in texto)

    # Um jogo longe mas com a abertura anunciada conta pela data da abertura.
    for nome, dias_abre in (("daqui a 3 dias", 3), ("ja passada", -1)):
        preparar(jogo("anunciado", 120))
        calendario["anunciado"]["localSaleStartsAt"] = (
            monitor.agora() + timedelta(days=dias_abre)
        ).isoformat()
        correr()
        texto = correr()
        verificar(f"jogo a 120 dias com abertura {nome} -> rapido",
                  "ritmo rapido" in texto)


def testa_reconhecimento_periodico() -> None:
    print("\nReconhecimento periodico")
    preparar(jogo("longe", 120))
    correr()
    inicial = estado_atual()["ultimo_reconhecimento"]
    verificar("regista a hora do reconhecimento", inicial is not None)

    estado = estado_atual()
    estado["ultimo_reconhecimento"] = "2020-01-01T00:00:00+00:00"
    monitor.ESTADO.write_text(json.dumps(estado), encoding="utf-8")
    texto = correr()
    verificar("volta a correr passadas as horas devidas",
              "Reconhecimento" in texto)


def testa_calendario_muda() -> None:
    print("\nJogos que entram e saem do calendario")
    preparar(jogo("fica", 10), jogo("sai", 20))
    correr()
    del calendario["sai"]
    calendario["novo"] = jogo("novo", 30)
    correr()
    jogos = estado_atual()["jogos"]
    verificar("esquece o jogo que saiu do calendario", "sai" not in jogos)
    verificar("aprende o jogo novo", "novo" in jogos)


def testa_ordem_do_ficheiro() -> None:
    print("\nOrdem dos jogos no ficheiro de estado")
    # Os ids sao dados de proposito por ordem alfabetica contraria a das datas,
    # para que ordenar por id de o resultado errado.
    preparar(jogo("aaa", 90), jogo("mmm", 30), jogo("zzz", 5))
    correr()
    ordem = list(estado_atual()["jogos"])
    verificar("grava por data do jogo e nao por id",
              ordem == ["zzz", "mmm", "aaa"])

    # Um jogo com data ilegivel nao pode rebentar a gravacao.
    preparar(jogo("com-data", 10), jogo("sem-data", 20))
    calendario["sem-data"]["localStartsAt"] = None
    correr()
    verificar("jogo sem data vai para o fim",
              list(estado_atual()["jogos"]) == ["com-data", "sem-data"])


def testa_topico_em_falta() -> None:
    print("\nSegredo NTFY_TOPIC em falta no GitHub Actions")
    import os

    preparar(jogo("alvo", 10))
    correr()
    calendario["alvo"]["status"] = "OPEN"
    antes = json.dumps(estado_atual(), sort_keys=True)

    os.environ["GITHUB_ACTIONS"] = "true"
    os.environ.pop("NTFY_TOPIC", None)
    try:
        rebentou = False
        try:
            correr()
        except SystemExit:
            rebentou = True
    finally:
        os.environ.pop("GITHUB_ACTIONS", None)

    verificar("falha ruidosamente em vez de avisar so no log", rebentou)
    verificar("nao da o jogo por avisado", json.dumps(estado_atual(), sort_keys=True) == antes)

    # Fora do Actions (na tua maquina) continua a poder correr sem topico.
    texto = correr()
    verificar("na maquina local corre a mesma", "FC Porto" in texto)


def testa_estado_estragado() -> None:
    print("\nFicheiro de estado invalido ou de outra versao")
    for nome, conteudo in (
        ("lixo", "isto nao e json"),
        ("versao antiga", '{"id-qualquer": "SCHEDULED"}'),
        ("vazio", "{}"),
        ("versao 2", '{"versao": 2, "jogos": {"ja-aberto": {"estado": "SCHEDULED", "seguir": true}}}'),
        ("jogos trocados", '{"versao": 3, "jogos": "isto devia ser um dict"}'),
    ):
        preparar(jogo("ja-aberto", 10, "OPEN"))
        monitor.ESTADO.write_text(conteudo, encoding="utf-8")
        correr()
        verificar(f"recomeca sem avalanche de avisos ({nome})", avisos == [])


def testa_notificacao_real() -> None:
    print("\nNotificacao real (vai mesmo para o telemovel)")
    monitor.notificar(
        "Teste do alerta de bilhetes",
        "Se leste isto no telemovel, esta tudo bem.",
        monitor.SITE,
    )
    print("  Confirma no telemovel. Se nao chegou, ve o NTFY_TOPIC.")


def main() -> int:
    real_notificar = monitor.notificar
    monitor.pedir_api = api_falsa
    monitor.notificar = notificar_falso

    with tempfile.TemporaryDirectory() as pasta:
        monitor.ESTADO = Path(pasta) / "estado-de-teste.json"
        testa_primeira_execucao()
        testa_abertura()
        testa_em_breve()
        testa_varios_ao_mesmo_tempo()
        testa_jogo_de_tras_da_fila()
        testa_ritmos()
        testa_reconhecimento_periodico()
        testa_calendario_muda()
        testa_ordem_do_ficheiro()
        testa_topico_em_falta()
        testa_estado_estragado()

    if "--notificar" in sys.argv:
        monitor.notificar = real_notificar
        testa_notificacao_real()

    print()
    if falhas:
        print(f"{len(falhas)} verificacao(oes) falharam:")
        for f in falhas:
            print(f"  - {f}")
        return 1
    print("Tudo certo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
