"""A fila: expiração preguiçosa, a trava do claim e a hidratação do envelope.

Três coisas moram aqui, e as três são a mecânica da plataforma.

1. **Expiração preguiçosa** (``expirar_vencidas``). Uma atribuição
   ``em_andamento`` que passou do ``expira_em`` vira ``expirada`` por um UPDATE
   rodado **antes de cada claim e de cada listagem** — não há thread de fundo.
   Um relógio de fundo numa app local é complexidade que só aparece quando
   quebra, e o único momento em que a expiração IMPORTA é justamente o momento
   em que alguém pergunta pela fila.

2. **A trava** (``reivindicar``). Sob ``BEGIN IMMEDIATE``, para que a contagem
   de vagas e o INSERT da atribuição sejam a mesma decisão. Com duas abas
   abertas na demonstração — que é o caso normal, não o excepcional — um
   ``SELECT`` seguido de ``INSERT`` fora de transação entrega a mesma tarefa
   duas vezes. ``BEGIN IMMEDIATE`` (e não ``BEGIN``) porque o modo diferido só
   pega o lock de escrita na primeira escrita, e aí já é tarde: as duas
   transações leram "tem vaga".

3. **A hidratação** (``hidratar``), que junta os dois bancos POR ``uid``.

O UID PODE TER SUMIDO, E ISSO VAI ACONTECER
===========================================
O corpus é reconstruído do zero a cada ``pf load-db``: o dedup evolui, uma
linha sai do universo, e uma tarefa criada ontem aponta para um ``uid`` que não
existe mais (o universo desta máquina foi de 144.754 para 159.733 numa única
recarga). A resposta **nunca** é 500: a tarefa é marcada indisponível, o claim
pula para a próxima e o motivo fica gravado para o painel do admin ler.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..config import get as _cfg
from . import db as adb
from . import pool as poolmod

#: O carimbo de tempo, na MESMA expressão do DDL (UTC, ISO-8601 com
#: milissegundos). Comparação de prazo é comparação de string, e ela só funciona
#: porque este formato é lexicograficamente ordenável — trocar por
#: ``datetime('now')`` (que sai com espaço e sem Z) quebraria toda a expiração
#: em silêncio, com os dois lados parecendo datas.
SQL_AGORA = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

#: Quantas tarefas o claim examina antes de desistir. O laço só existe por causa
#: dos uids sumidos: sem eles, a primeira candidata sempre serve. 50 é folgado o
#: bastante para atravessar um lote de tarefas órfãs e curto o bastante para não
#: virar uma varredura da fila inteira dentro de ``BEGIN IMMEDIATE``.
CANDIDATAS_POR_CLAIM = 50

#: Chave que ``payload_json`` da TAREFA ganha quando o uid dela sumiu do corpus.
#: Fica no payload (e não numa coluna nova) porque é um fato sobre aquela tarefa
#: específica, e porque o schema deste banco guarda trabalho humano: acrescentar
#: coluna aqui custa migração, e isto é um bilhete, não um estado.
CHAVE_INDISPONIVEL = "indisponivel"

#: Atribuição VIVA = ocupa uma vaga. ``submetida``/``aprovada`` sempre; uma
#: ``em_andamento`` só enquanto o prazo não venceu. ``expira_em IS NULL`` é o
#: modo livre, que não tem prazo — e não expira nunca.
#:
#: A condição do ``em_andamento`` é redundante depois de ``expirar_vencidas``, e
#: está aqui de propósito: ela é a definição, o UPDATE é só a materialização
#: dela. Se um dia a expiração falhar, a conta de vagas continua certa.
SQL_VIVA = (
    "(a.status IN ('submetida','aprovada') "
    f" OR (a.status = 'em_andamento' AND (a.expira_em IS NULL OR a.expira_em >= {SQL_AGORA})))"
)


def ttl_min() -> int:
    """``[annotate] claim_ttl_min`` — validade de uma atribuição sem submissão."""
    return int(_cfg("annotate", "claim_ttl_min", default=120))


def expirar_vencidas(conn: sqlite3.Connection) -> int:
    """``em_andamento`` com prazo vencido → ``expirada``. Devolve quantas.

    Rodada antes de cada claim e de cada listagem. Idempotente e barata: o
    ``idx_atribuicoes_prazo (status, expira_em)`` é exatamente esta consulta, e
    o UPDATE não toca nas linhas do modo livre (``expira_em IS NULL``).
    """
    cur = conn.execute(
        "UPDATE atribuicoes SET status = 'expirada', "
        f"terminada_em = COALESCE(terminada_em, {SQL_AGORA}) "
        f"WHERE status = 'em_andamento' AND expira_em IS NOT NULL AND expira_em < {SQL_AGORA}"
    )
    return int(cur.rowcount or 0)


# ---------------------------------------------------------------------------
# resolver: corpus ou pacote de demonstração
# ---------------------------------------------------------------------------


def uids_do_pool(conn_corpus: sqlite3.Connection) -> list[str]:
    """Os uids que a plataforma pode oferecer, na ordem determinística do pool."""
    return poolmod.resolver(conn_corpus, com_uids=True).uids


def _do_corpus(conn_corpus: sqlite3.Connection, uid: str) -> dict[str, Any] | None:
    linha = conn_corpus.execute(
        "SELECT uid, COALESCE(text_original, text) AS text, lang, source, license, "
        "       commercial_ok, redistributable, n_chars, n_words "
        "FROM prompts WHERE uid = ?",
        (uid,),
    ).fetchone()
    if linha is None:
        return None
    # Import tardio: `presenters` puxa o `sources.toml` e a taxonomia, e esta
    # app não precisa de nada disso até alguém abrir uma tarefa de verdade.
    from ..app import presenters

    return {
        "uid": str(linha["uid"]),
        "text": str(linha["text"]),
        "lang": str(linha["lang"]),
        "demo": False,
        # PROVENIÊNCIA À VISTA. É o argumento inteiro deste projeto: cada prompt
        # mostra de onde veio e sob qual licença. Sem isto a plataforma seria
        # mais uma tela de anotação sobre texto de origem desconhecida.
        "fonte": str(linha["source"]),
        "licenca": str(linha["license"]),
        "licenca_classe": presenters.classe_da_licenca(
            linha["license"], linha["commercial_ok"], linha["redistributable"]
        ),
        "atribuicao": presenters.atribuicoes().get(str(linha["source"]), ""),
        "n_chars": int(linha["n_chars"]),
        "n_words": int(linha["n_words"]),
    }


def _do_demo(conn: sqlite3.Connection, uid: str) -> dict[str, Any] | None:
    linha = conn.execute(
        "SELECT uid, text, lang FROM prompts_demo WHERE uid = ?", (uid,)
    ).fetchone()
    if linha is None:
        return None
    texto = str(linha["text"])
    return {
        "uid": str(linha["uid"]),
        "text": texto,
        "lang": str(linha["lang"]),
        "demo": True,
        # Fixture escrita à mão NÃO é prompt de ninguém, e dizer isso na tela é
        # metade do valor do pacote: o avaliador precisa distinguir de relance
        # o que é corpus real do que é demonstração.
        "fonte": "pacote de demonstração",
        "licenca": "fixture",
        "licenca_classe": "livre",
        "atribuicao": "escrito à mão para a demonstração da Bancada",
        "n_chars": len(texto),
        "n_words": len(texto.split()),
    }


def resolver_prompt(
    conn: sqlite3.Connection, conn_corpus: sqlite3.Connection, uid: str
) -> dict[str, Any] | None:
    """O prompt inteiro, venha ele do corpus ou do pacote. ``None`` se sumiu.

    O prefixo ``demo:`` é o que decide em qual banco procurar — e ele é cobrado
    por CHECK no DDL de ``prompts_demo``, então não é convenção, é contrato.
    """
    if adb.e_demo(uid):
        return _do_demo(conn, uid)
    return _do_corpus(conn_corpus, uid)


def no_pool(conn: sqlite3.Connection, conn_corpus: sqlite3.Connection, uid: str) -> bool:
    """O uid pode ser oferecido a um anotador?

    Duas portas: o pacote de demonstração (que É a plataforma) e o pool do
    corpus. Um uid que existe no corpus mas está FORA do pool não passa — senão
    o modo livre viraria uma porta lateral para os 159 mil prompts, incluindo os
    que o filtro do pool exclui de propósito (NSFW, robô repetido, 1 MB de
    texto).
    """
    if adb.e_demo(uid):
        return (
            conn.execute(
                "SELECT 1 FROM prompts_demo WHERE uid = ?", (uid,)
            ).fetchone()
            is not None
        )
    return uid in set(uids_do_pool(conn_corpus))


# ---------------------------------------------------------------------------
# a trava
# ---------------------------------------------------------------------------


def _marcar_indisponivel(conn: sqlite3.Connection, tarefa_id: int, uid: str) -> None:
    """Tarefa cujo prompt sumiu do corpus: ``pausada`` + bilhete no payload.

    ``pausada`` (e não uma coluna nova) porque o vocabulário já tem o estado
    certo: a tarefa não foi concluída nem cancelada, ela só não pode ser
    servida agora. Se o próximo ``pf load-db`` trouxer o uid de volta, o admin
    reabre — nada foi destruído.
    """
    linha = conn.execute("SELECT payload_json FROM tarefas WHERE id = ?", (tarefa_id,)).fetchone()
    try:
        payload = json.loads(str(linha["payload_json"])) if linha else {}
        if not isinstance(payload, dict):
            payload = {"payload_anterior": payload}
    except (TypeError, ValueError):
        payload = {}
    payload[CHAVE_INDISPONIVEL] = {
        "motivo": f"o uid {uid} não está no corpus atual",
        "detectado_em": "claim",
    }
    conn.execute(
        "UPDATE tarefas SET status = 'pausada', payload_json = ? WHERE id = ?",
        (json.dumps(payload, ensure_ascii=False), tarefa_id),
    )


#: Motivos de fila vazia. São TEXTO DE TELA, não código de erro: o estado vazio
#: desta plataforma é uma bifurcação, e a frase é metade da bifurcação.
MOTIVO_VAZIA = (
    "Nenhuma tarefa deste tipo esperando por você agora. "
    "Ou não há tarefas abertas, ou as que existem já estão com outra pessoa, "
    "ou você já anotou todas."
)


def reivindicar(
    conn: sqlite3.Connection,
    conn_corpus: sqlite3.Connection,
    anotador_id: int,
    tipo: str,
) -> tuple[int | None, str]:
    """Pega a próxima tarefa do tipo. Devolve ``(atribuicao_id, motivo)``.

    ``(None, motivo)`` é resposta NORMAL — a rota devolve 200 com o motivo, e
    não 404. Fila vazia não é erro: é o estado mais comum de uma plataforma de
    anotação bem servida, e responder 404 faria o cliente tratar o caso normal
    como falha.

    A ordem é exatamente ``idx_tarefas_fila (status, tipo, prioridade DESC, id)``
    — o índice foi criado no P1 com esta consulta em mente.
    """
    expirar_vencidas(conn)

    conn.execute("BEGIN IMMEDIATE")
    try:
        candidatas = conn.execute(
            "SELECT t.id, t.prompt_uid FROM tarefas t "
            "WHERE t.status = 'aberta' AND t.tipo = :tipo "
            # A trava anti-repetição, metade em SQL e metade no
            # UNIQUE(tarefa_id, anotador_id): ninguém pega duas vezes a mesma
            # tarefa, nem para "ver de novo".
            "  AND NOT EXISTS (SELECT 1 FROM atribuicoes a "
            "                  WHERE a.tarefa_id = t.id AND a.anotador_id = :eu) "
            "  AND (SELECT count(*) FROM atribuicoes a "
            f"       WHERE a.tarefa_id = t.id AND {SQL_VIVA}) < t.n_anotacoes_alvo "
            "ORDER BY t.prioridade DESC, t.id "
            "LIMIT :limite",
            {"tipo": tipo, "eu": anotador_id, "limite": CANDIDATAS_POR_CLAIM},
        ).fetchall()

        escolhida: int | None = None
        sumidos = 0
        for linha in candidatas:
            uid = str(linha["prompt_uid"])
            if resolver_prompt(conn, conn_corpus, uid) is None:
                _marcar_indisponivel(conn, int(linha["id"]), uid)
                sumidos += 1
                continue
            escolhida = int(linha["id"])
            break

        if escolhida is None:
            conn.execute("COMMIT")  # as marcações de indisponível ficam
            if sumidos:
                return None, (
                    f"{sumidos} tarefa(s) deste tipo apontam para prompts que não estão "
                    "mais no corpus (ele foi recarregado) e foram pausadas. "
                    "Peça ao administrador para gerar tarefas novas."
                )
            return None, MOTIVO_VAZIA

        # O prazo é calculado pelo SQLite, e não em Python, para sair no MESMO
        # formato do `expira_em` que a expiração compara — e do mesmo relógio.
        # Dois relógios (o do processo e o do banco) produziriam prazos que
        # divergem por milissegundos e uma expiração que erra por um.
        cur = conn.execute(
            "INSERT INTO atribuicoes (tarefa_id, anotador_id, status, expira_em) "
            "VALUES (:tarefa, :eu, 'em_andamento', "
            "        strftime('%Y-%m-%dT%H:%M:%fZ','now', :prazo))",
            {"tarefa": escolhida, "eu": anotador_id, "prazo": f"+{ttl_min()} minutes"},
        )
        atribuicao_id = int(cur.lastrowid or 0)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return atribuicao_id, "tarefa atribuída"


def contagens_por_tipo(
    conn: sqlite3.Connection, anotador_id: int
) -> dict[str, int]:
    """Quantas tarefas de cada tipo estão disponíveis **para esta pessoa**.

    É o número dos contadores das abas. Ele é pessoal de propósito: mostrar "12
    na fila" para quem já anotou as 12 é a forma mais rápida de fazer a tela
    parecer quebrada. Não confere uid sumido (isso custaria uma ida ao corpus
    por tarefa) — o claim confere, e a diferença aparece como "pulei uma".
    """
    expirar_vencidas(conn)
    linhas = conn.execute(
        "SELECT t.tipo, count(*) AS n FROM tarefas t "
        "WHERE t.status = 'aberta' "
        "  AND NOT EXISTS (SELECT 1 FROM atribuicoes a "
        "                  WHERE a.tarefa_id = t.id AND a.anotador_id = :eu) "
        "  AND (SELECT count(*) FROM atribuicoes a "
        f"       WHERE a.tarefa_id = t.id AND {SQL_VIVA}) < t.n_anotacoes_alvo "
        "GROUP BY t.tipo",
        {"eu": anotador_id},
    ).fetchall()
    contagem = dict.fromkeys(adb.TIPOS_TAREFA, 0)
    for linha in linhas:
        contagem[str(linha["tipo"])] = int(linha["n"])
    return contagem


# ---------------------------------------------------------------------------
# hidratação: o envelope que a tela recebe
# ---------------------------------------------------------------------------


def _carregar_json(bruto: Any, padrao: Any) -> Any:
    try:
        valor = json.loads(str(bruto))
    except (TypeError, ValueError):
        return padrao
    return valor


def rubrica_ativa(conn: sqlite3.Connection, uid: str) -> dict[str, Any] | None:
    linha = conn.execute(
        "SELECT id, titulo, criterios_json, origem FROM rubricas "
        "WHERE prompt_uid = ? AND status = 'ativa' ORDER BY id DESC LIMIT 1",
        (uid,),
    ).fetchone()
    if linha is None:
        return None
    conteudo = _carregar_json(linha["criterios_json"], {})
    return {
        "id": int(linha["id"]),
        "titulo": str(linha["titulo"]),
        "criterios": conteudo.get("criterios", []) if isinstance(conteudo, dict) else [],
        "origem": str(linha["origem"]),
        "nota": None,
    }


def _rubrica_proposta_por_mim(
    conn: sqlite3.Connection, uid: str, anotador_id: int
) -> dict[str, Any] | None:
    """A rubrica que ESTA pessoa escreveu para este prompt e ainda está em revisão.

    É o que faz a continuação rubrica → SFT funcionar sem esperar o revisor: a
    rubrica só é materializada em ``rubricas`` na APROVAÇÃO (P3), e até lá ela
    vive dentro do payload da anotação. Sem este atalho, quem acabou de escrever
    uma rubrica escreveria a resposta sem ter a própria rubrica ao lado — que é
    exatamente o valor da continuação.
    """
    linha = conn.execute(
        "SELECT an.id, an.payload_json FROM anotacoes an "
        "JOIN atribuicoes a ON a.id = an.atribuicao_id "
        "JOIN tarefas t ON t.id = a.tarefa_id "
        "WHERE t.prompt_uid = ? AND t.tipo = 'escrever_rubrica' "
        "  AND a.anotador_id = ? AND an.status <> 'rejeitada' "
        "ORDER BY an.id DESC LIMIT 1",
        (uid, anotador_id),
    ).fetchone()
    if linha is None:
        return None
    payload = _carregar_json(linha["payload_json"], {})
    if not isinstance(payload, dict) or not payload.get("criterios"):
        return None
    return {
        "id": None,
        "titulo": str(payload.get("titulo") or "Rubrica proposta"),
        "criterios": payload["criterios"],
        "origem": "anotacao",
        "nota": "esta é a rubrica que você propôs — ela ainda está em revisão",
    }


def _respostas(conn: sqlite3.Connection, uid: str, quais: list[str]) -> list[dict[str, Any]]:
    """As respostas de modelo, **sem o ``meta_json``**.

    O meta guarda qual modelo escreveu e qual defeito foi plantado ali. Ele é o
    gabarito do exercício: vazá-lo para o anotador transformaria o A/B numa
    prova de leitura de metadado. Sai só no painel do admin.
    """
    if not quais:
        return []
    marcas = ", ".join("?" * len(quais))
    linhas = conn.execute(
        f"SELECT rotulo_modelo, texto FROM respostas_modelo "
        f"WHERE prompt_uid = ? AND rotulo_modelo IN ({marcas}) "
        "ORDER BY rotulo_modelo",
        (uid, *quais),
    ).fetchall()
    return [
        {"rotulo": str(linha["rotulo_modelo"]), "texto": str(linha["texto"])}
        for linha in linhas
    ]


def _versao_anterior(conn: sqlite3.Connection, atribuicao_id: int) -> dict[str, Any] | None:
    """A última anotação desta atribuição + o comentário do revisor, se houver.

    RECONHECIMENTO EM VEZ DE MEMÓRIA: no re-trabalho, o formulário volta
    preenchido com o que a pessoa escreveu e o comentário do revisor fica acima
    dele, no caminho do olho. Reconstruir de memória o que se escreveu há dois
    dias é a fricção que faz a devolução ser ignorada.
    """
    linha = conn.execute(
        "SELECT an.id, an.versao, an.payload_json, an.payload_schema, an.status, "
        "       r.veredito, r.comentario, r.criada_em AS revisada_em "
        "FROM anotacoes an LEFT JOIN revisoes r ON r.anotacao_id = an.id "
        "WHERE an.atribuicao_id = ? ORDER BY an.versao DESC LIMIT 1",
        (atribuicao_id,),
    ).fetchone()
    if linha is None:
        return None
    return {
        "anotacao_id": int(linha["id"]),
        "versao": int(linha["versao"]),
        "payload": _carregar_json(linha["payload_json"], {}),
        "payload_schema": str(linha["payload_schema"]),
        "status": str(linha["status"]),
        "veredito": linha["veredito"],
        "comentario_revisor": linha["comentario"],
        "revisada_em": linha["revisada_em"],
    }


def hidratar(
    conn: sqlite3.Connection,
    conn_corpus: sqlite3.Connection,
    atribuicao_id: int,
) -> dict[str, Any] | None:
    """O envelope completo de uma atribuição — tudo que a tela precisa, de uma vez.

    ``None`` quando a atribuição não existe ou o prompt dela sumiu do corpus.

    **``gabarito_json`` NÃO ENTRA AQUI.** O dicionário de ``tarefa`` é montado
    campo a campo, e não com ``dict(linha)``, exatamente por isso: um
    ``dict(linha)`` de hoje passaria no teste e vazaria o gabarito no dia em que
    alguém acrescentasse uma coluna ao SELECT. Um gabarito visível é uma
    tarefa-ouro que mede a capacidade de ler o JSON da resposta.
    """
    linha = conn.execute(
        "SELECT a.id AS atribuicao_id, a.status AS atribuicao_status, a.expira_em, "
        "       a.iniciada_em, a.anotador_id, "
        "       t.id AS tarefa_id, t.tipo, t.origem, t.prompt_uid, t.payload_json, "
        "       t.prioridade, t.n_anotacoes_alvo "
        "FROM atribuicoes a JOIN tarefas t ON t.id = a.tarefa_id "
        "WHERE a.id = ?",
        (atribuicao_id,),
    ).fetchone()
    if linha is None:
        return None

    uid = str(linha["prompt_uid"])
    prompt = resolver_prompt(conn, conn_corpus, uid)
    if prompt is None:
        return None

    tipo = str(linha["tipo"])
    payload_tarefa = _carregar_json(linha["payload_json"], {})
    if not isinstance(payload_tarefa, dict):
        payload_tarefa = {}

    rubrica: dict[str, Any] | None = None
    if tipo in ("avaliar_rubrica", "sft_resposta"):
        rubrica = rubrica_ativa(conn, uid)
        if rubrica is None and tipo == "sft_resposta":
            rubrica = _rubrica_proposta_por_mim(conn, uid, int(linha["anotador_id"]))

    quais: list[str] = []
    if tipo == "avaliar_rubrica":
        alvo = payload_tarefa.get("resposta")
        quais = [str(alvo)] if alvo else list(adb.ROTULOS_MODELO)[:1]
    elif tipo == "comparar_ab":
        quais = list(adb.ROTULOS_MODELO)

    return {
        "atribuicao_id": int(linha["atribuicao_id"]),
        "atribuicao_status": str(linha["atribuicao_status"]),
        "expira_em": linha["expira_em"],
        "iniciada_em": linha["iniciada_em"],
        "tarefa": {
            "id": int(linha["tarefa_id"]),
            "tipo": tipo,
            "origem": str(linha["origem"]),
            "prioridade": int(linha["prioridade"]),
            "n_anotacoes_alvo": int(linha["n_anotacoes_alvo"]),
            # Só as chaves que a TELA usa. `gabarito_json` não é lido do banco
            # nem por acidente: ele nem está no SELECT acima.
            "payload": {k: v for k, v in payload_tarefa.items() if k != CHAVE_INDISPONIVEL},
        },
        "prompt": prompt,
        "rubrica": rubrica,
        "respostas": _respostas(conn, uid, quais),
        "versao_anterior": _versao_anterior(conn, int(linha["atribuicao_id"])),
    }


__all__ = [
    "CANDIDATAS_POR_CLAIM",
    "CHAVE_INDISPONIVEL",
    "MOTIVO_VAZIA",
    "SQL_AGORA",
    "SQL_VIVA",
    "contagens_por_tipo",
    "expirar_vencidas",
    "hidratar",
    "no_pool",
    "reivindicar",
    "resolver_prompt",
    "rubrica_ativa",
    "ttl_min",
    "uids_do_pool",
]
