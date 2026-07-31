"""P4d — as duas abas de INFERÊNCIA AO VIVO, provadas sem o Ollama de pé.

**Nenhum teste deste arquivo abre um socket.** A camada de rede da plataforma tem
uma costura só (``modelos._http``), e é ela que o ``monkeypatch`` troca. Isso não
é comodidade: um teste que dependesse do Ollama passaria na máquina de quem
escreveu o marco e falharia na de quem for ler o portfólio — e falharia por um
motivo que nada tem a ver com o código.

As seis provas que carregam o marco, e o defeito real que cada uma impede:

1. **Ollama fora do ar é 503 com conserto**, nunca 500. Um 500 diria "a
   plataforma quebrou" sobre um serviço que não subiu.
2. **Modelo ausente idem**, e com o ``ollama pull`` na resposta: a máquina que
   abre isto pode não ter baixado 7,5 GB ainda, e isso é normal.
3. **O nome do modelo NÃO vaza no duelo** antes do envio. Se vazasse, a
   preferência passaria a ser sobre a marca — e o dado inteiro perderia o valor.
4. **O turno do humano sobrevive a uma geração que falhou.** Ele é gravado
   ANTES da chamada, e é a diferença entre "tente de novo" e "redigite o
   parágrafo".
5. **Quem respondeu fica gravado com nome E digest.** ``qwen3:4b`` é uma tag, e
   tag é reescrita; sem o digest a preferência não significa nada em seis meses.
6. **A nota fora da escala do critério é recusada** pela MESMA função pura que
   os outros dois escritores usam (``tarefas.erro_contra_a_rubrica``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from prompt_factory import db as dbmod
from prompt_factory.annotate import conversa as convmod
from prompt_factory.annotate import db as adb
from prompt_factory.annotate import modelos as modmod
from prompt_factory.annotate import seed as seedmod
from prompt_factory.annotate.main import criar_app

from .test_api import montar_banco

# ---------------------------------------------------------------------------
# o Ollama de mentira
# ---------------------------------------------------------------------------

#: O inventário que o ``/api/tags`` falso devolve — a forma EXATA medida na
#: máquina do dono (``ollama 0.32.5``), inclusive as ``capabilities``.
TAGS_FALSAS: dict[str, Any] = {
    "models": [
        {
            "name": "gemma3n:e4b",
            "model": "gemma3n:e4b",
            "size": 7_500_000_000,
            "digest": "aaaaaaaaaaaa1111111111111111111111111111111111111111111111111111",
            "details": {"family": "gemma3n", "parameter_size": "4.0B"},
            "capabilities": ["completion"],
        },
        {
            "name": "qwen3:4b",
            "model": "qwen3:4b",
            "size": 2_497_293_931,
            "digest": "359d7dd4bcda3333333333333333333333333333333333333333333333333333",
            "details": {"family": "qwen3", "parameter_size": "4.0B"},
            "capabilities": ["completion", "tools", "thinking"],
        },
    ]
}


class Ollama:
    """Um Ollama de mentira, com memória do que foi pedido.

    Guardar as chamadas é o que permite provar coisas que nenhuma resposta
    mostraria — que o histórico mandado no turno 3 leva a resposta VENCEDORA do
    turno 2, por exemplo, que é a definição de "a conversa continua com ela".
    """

    def __init__(self) -> None:
        self.chamadas: list[dict[str, Any]] = []
        self.tags: dict[str, Any] = TAGS_FALSAS
        self.erro: Exception | None = None
        self.raciocinio = ""
        self.done_reason = "stop"

    def __call__(self, caminho: str, corpo: dict[str, Any] | None, timeout: float) -> dict:
        if self.erro is not None:
            raise self.erro
        if caminho == "/api/tags":
            return self.tags
        self.chamadas.append(dict(corpo or {}))
        modelo = str((corpo or {}).get("model") or "")
        n = len(self.chamadas)
        return {
            "model": modelo,
            "message": {
                "role": "assistant",
                # O TEXTO NÃO CARREGA O NOME DO MODELO, de propósito: se
                # carregasse, o teste de vazamento do duelo passaria a acusar a
                # própria fixture e deixaria de medir o produto. Ele é único por
                # chamada, que é o que permite distinguir o lado A do B.
                "content": f"resposta numero {n}",
                "thinking": self.raciocinio,
            },
            "done": True,
            "done_reason": self.done_reason,
            "eval_count": 42,
        }


@pytest.fixture
def ollama(monkeypatch: pytest.MonkeyPatch) -> Ollama:
    falso = Ollama()
    monkeypatch.setattr(modmod, "_http", falso)
    return falso


# ---------------------------------------------------------------------------
# a plataforma
# ---------------------------------------------------------------------------


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    caminho = tmp_path / "prompts.sqlite"
    montar_banco(caminho)
    return caminho


@pytest.fixture
def semeado(tmp_path: Path, corpus: Path) -> Path:
    banco = tmp_path / "annotate.sqlite"
    conn = dbmod.connect(banco)
    corpo = dbmod.connect(corpus, readonly=True)
    try:
        adb.init_db(conn)
        seedmod.semear(conn, corpo)
    finally:
        conn.close()
        corpo.close()
    return banco


@pytest.fixture
def cliente(semeado: Path, corpus: Path):
    with TestClient(criar_app(semeado, corpus)) as c:
        yield c


def anotador(cliente: TestClient) -> int:
    itens = cliente.get("/api/perfis").json()["items"]
    return next(p["id"] for p in itens if p["papel"] == "anotador")


def pegar(cliente: TestClient, quem: int, tipo: str) -> dict[str, Any]:
    r = cliente.post("/api/tarefas/proxima", json={"anotador_id": quem, "tipo": tipo})
    assert r.status_code == 200, r.text
    env = r.json()["tarefa"]
    assert env is not None, f"a fila de {tipo} está vazia — o seed deveria tê-la enchido"
    return env


def turno(cliente: TestClient, quem: int, atribuicao: int, texto: str | None):
    return cliente.post(
        f"/api/conversa/{atribuicao}/turno", json={"anotador_id": quem, "texto": texto}
    )


def escolher(cliente: TestClient, quem: int, atribuicao: int, ordem: int, rotulo: str):
    return cliente.post(
        f"/api/conversa/{atribuicao}/escolher",
        json={
            "anotador_id": quem,
            "ordem": ordem,
            "rotulo": rotulo,
            "justificativa": "Esta resposta manteve a restricao declarada no primeiro turno.",
        },
    )


def notas_boas(env: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"criterio": c["nome"], "nota": 4} for c in env["rubrica"]["criterios"]]


def avaliacao(env: dict[str, Any], **extra: Any) -> dict[str, Any]:
    corpo = {
        "rubrica": env["rubrica"]["rubrica_id"],
        "notas": notas_boas(env),
        "justificativa": (
            "A conversa manteve a restricao ate o fim e recuou quando foi corrigida."
        ),
    }
    corpo.update(extra)
    return corpo


def submeter(cliente: TestClient, env: dict[str, Any], quem: int, payload: dict[str, Any]):
    return cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
        json={"anotador_id": quem, "payload": payload},
    )


# ---------------------------------------------------------------------------
# 1. o runtime: fora do ar e modelo ausente são ESTADO
# ---------------------------------------------------------------------------


def test_ollama_fora_do_ar_e_estado_de_primeira_classe(
    cliente: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """200 com ``ok: false`` e o comando que sobe o servidor. Nunca 500."""
    def morto(caminho: str, corpo: Any, timeout: float) -> dict:
        raise modmod.OllamaIndisponivel("recusou a conexão", url=modmod.base_url())

    monkeypatch.setattr(modmod, "_http", morto)
    r = cliente.get("/api/modelos")
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["ok"] is False
    assert corpo["erro_chave"] == modmod.T_FORA_DO_AR
    assert corpo["instalados"] == []
    # O CONSERTO vem junto, uma linha por modelo que falta.
    assert corpo["comandos"] == [
        modmod.COMANDO_PULL.format(modelo=m) for m in modmod.modelos_configurados()
    ]
    # E o health também reporta — é lá que a tela lê o estado antes de desenhar.
    saude = cliente.get("/api/health").json()
    assert saude["modelos"]["ok"] is False
    assert saude["modelos"]["erro_chave"] == modmod.T_FORA_DO_AR


def test_modelo_ausente_diz_o_que_falta_e_o_comando(
    cliente: TestClient, ollama: Ollama
) -> None:
    """Faltar 7,5 GB de download é normal, e a resposta é uma instrução."""
    ollama.tags = {"models": [TAGS_FALSAS["models"][1]]}  # só o qwen
    corpo = cliente.get("/api/modelos").json()
    assert corpo["ok"] is False
    assert corpo["faltando"] == ["gemma3n:e4b"]
    assert corpo["comandos"] == ["ollama pull gemma3n:e4b"]
    assert corpo["erro_chave"] == modmod.T_FALTAM
    assert [m["nome"] for m in corpo["instalados"]] == ["qwen3:4b"]


def test_com_os_dois_baixados_o_runtime_se_declara_pronto(
    cliente: TestClient, ollama: Ollama
) -> None:
    corpo = cliente.get("/api/modelos").json()
    assert corpo["ok"] is True
    assert corpo["faltando"] == []
    assert corpo["erro_chave"] == modmod.T_PRONTO
    # Os parâmetros que a tela precisa para validar igual ao servidor.
    assert corpo["escala_turno"] == {"min": adb.ESCALA_TURNO[0], "max": adb.ESCALA_TURNO[1]}
    assert corpo["max_turnos"] == modmod.max_turnos()


def test_gerar_sem_o_modelo_baixado_e_503_e_nao_500(
    cliente: TestClient, ollama: Ollama
) -> None:
    quem = anotador(cliente)
    env = pegar(cliente, quem, "conversa_modelo")
    ollama.tags = {"models": []}
    r = turno(cliente, quem, env["atribuicao_id"], "Oi, tudo bem?")
    assert r.status_code == 503, r.text
    detalhe = r.json()["detail"]
    assert detalhe["chave"] == modmod.T_FALTAM
    assert "ollama pull" in detalhe["mensagem"]


def test_o_turno_do_humano_sobrevive_a_geracao_que_falhou(
    cliente: TestClient, ollama: Ollama
) -> None:
    """A ORDEM DAS ESCRITAS é o contrato: grava o humano, DEPOIS chama o modelo.

    Invertida, um timeout de 180 s custaria o parágrafo que a pessoa acabou de
    escrever — e nesta aba perder trabalho é o pior desfecho possível.
    """
    quem = anotador(cliente)
    env = pegar(cliente, quem, "conversa_modelo")
    atribuicao = env["atribuicao_id"]

    ollama.erro = modmod.OllamaIndisponivel("caiu no meio", url="http://127.0.0.1:11434")
    r = turno(cliente, quem, atribuicao, "Um turno que custou trabalho para escrever.")
    assert r.status_code == 503
    # A conversa VOLTA JUNTO com o erro, e o turno está lá.
    conversa = r.json()["detail"]["conversa"]
    assert [x["texto"] for x in conversa["turnos"]] == [
        "Um turno que custou trabalho para escrever."
    ]
    assert conversa["aguardando"] == 0

    # E "tentar de novo" NÃO pede o texto outra vez.
    ollama.erro = None
    r = turno(cliente, quem, atribuicao, None)
    assert r.status_code == 200, r.text
    turnos = r.json()["conversa"]["turnos"]
    assert [x["papel"] for x in turnos] == ["usuario", "modelo"]
    assert turnos[0]["texto"] == "Um turno que custou trabalho para escrever."


def test_escrever_o_proximo_por_cima_de_um_turno_sem_resposta_e_recusado(
    cliente: TestClient, ollama: Ollama
) -> None:
    """Senão a conversa ficaria com um buraco no meio que ninguém vê depois."""
    quem = anotador(cliente)
    env = pegar(cliente, quem, "conversa_modelo")
    ollama.erro = modmod.OllamaIndisponivel("caiu", url="x")
    turno(cliente, quem, env["atribuicao_id"], "primeiro")
    ollama.erro = None
    r = turno(cliente, quem, env["atribuicao_id"], "segundo, por cima do primeiro")
    assert r.status_code == 409
    assert "sem resposta" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 2. a conversa de um modelo só
# ---------------------------------------------------------------------------


def test_a_conversa_registra_quem_respondeu_com_nome_e_digest(
    cliente: TestClient, ollama: Ollama
) -> None:
    """Sem o digest, "o B era melhor" não significa nada daqui a seis meses."""
    quem = anotador(cliente)
    env = pegar(cliente, quem, "conversa_modelo")
    r = turno(cliente, quem, env["atribuicao_id"], "Explique em duas frases.")
    assert r.status_code == 200, r.text
    modelo = r.json()["conversa"]["turnos"][1]
    assert modelo["modelo"] == modmod.modelos_configurados()[0]
    assert modelo["digest"] == TAGS_FALSAS["models"][0]["digest"]
    # Na conversa de um modelo só o nome NÃO é segredo: sem duas respostas não
    # há preferência a enviesar.
    assert convmod.e_cego("conversa_modelo") is False


def test_o_historico_inteiro_vai_ao_modelo_a_cada_turno(
    cliente: TestClient, ollama: Ollama
) -> None:
    """Mandar só o último turno produziria um modelo com amnésia — que é
    justamente o que a rubrica multi-turno mede."""
    quem = anotador(cliente)
    env = pegar(cliente, quem, "conversa_modelo")
    atribuicao = env["atribuicao_id"]
    turno(cliente, quem, atribuicao, "primeiro")
    turno(cliente, quem, atribuicao, "segundo")
    papeis = [m["role"] for m in ollama.chamadas[-1]["messages"]]
    assert papeis == ["user", "assistant", "user"]
    assert ollama.chamadas[-1]["messages"][0]["content"] == "primeiro"


def test_nenhum_prompt_de_sistema_entra_na_conversa(
    cliente: TestClient, ollama: Ollama
) -> None:
    """A REGRA MAIS IMPORTANTE DA CAMADA DE INFERÊNCIA.

    Medido nesta máquina: o ``qwen3:4b`` ignorou a restrição de tamanho e
    respondeu em inglês a uma pergunta em português; o ``gemma3n:e4b`` respeitou
    as duas. Um ``system`` mandando responder em português e ser conciso apagaria
    exatamente o sinal que o anotador existe para medir, e o duelo viraria dois
    textos indistinguíveis. As famílias de defeito são o produto.
    """
    quem = anotador(cliente)
    env = pegar(cliente, quem, "conversa_modelo")
    turno(cliente, quem, env["atribuicao_id"], "uma pergunta qualquer")
    corpo = ollama.chamadas[-1]
    assert [m["role"] for m in corpo["messages"]] == ["user"]
    assert "system" not in json.dumps(corpo)
    # E os parâmetros são os MESMOS para todo modelo: equilibrar o duelo por
    # parâmetro seria conserto por outro caminho.
    assert corpo["options"]["temperature"] == modmod.temperatura()
    assert corpo["options"]["num_predict"] == modmod.num_predict()


def test_o_raciocinio_do_modelo_e_guardado_e_nao_e_a_resposta(
    cliente: TestClient, ollama: Ollama
) -> None:
    """Filtrar em silêncio seria decidir pelo anotador que o raciocínio não conta."""
    ollama.raciocinio = "Okay, the user is asking for a short sentence. Let me think."
    quem = anotador(cliente)
    env = pegar(cliente, quem, "conversa_modelo")
    r = turno(cliente, quem, env["atribuicao_id"], "Explique em duas frases.")
    modelo = r.json()["conversa"]["turnos"][1]
    assert modelo["raciocinio"] == ollama.raciocinio
    assert ollama.raciocinio not in modelo["texto"]


def test_o_turno_cortado_no_nosso_teto_sai_marcado(
    cliente: TestClient, ollama: Ollama
) -> None:
    """O teto é NOSSO. Sem a marca, a parede cortada pareceria decisão do modelo."""
    ollama.done_reason = "length"
    quem = anotador(cliente)
    env = pegar(cliente, quem, "conversa_modelo")
    r = turno(cliente, quem, env["atribuicao_id"], "Escreva um tratado.")
    assert r.json()["conversa"]["turnos"][1]["truncado"] is True


def test_o_teto_de_turnos_e_um_estado_com_saida(
    cliente: TestClient, ollama: Ollama, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prompt_factory import config

    monkeypatch.setitem(config.settings()["annotate"], "max_turnos_conversa", 2)
    quem = anotador(cliente)
    env = pegar(cliente, quem, "conversa_modelo")
    atribuicao = env["atribuicao_id"]
    assert turno(cliente, quem, atribuicao, "primeiro").status_code == 200
    r = turno(cliente, quem, atribuicao, "segundo")
    assert r.status_code == 409
    assert "teto" in r.json()["detail"]


def test_a_conversa_fecha_o_ciclo_e_o_servidor_monta_os_turnos(
    cliente: TestClient, ollama: Ollama
) -> None:
    """Três turnos, um marcado, rubrica aplicada — o roteiro do marco, em teste."""
    quem = anotador(cliente)
    env = pegar(cliente, quem, "conversa_modelo")
    atribuicao = env["atribuicao_id"]
    for texto in ("Responda em no máximo duas frases.", "Corrija: ficou longo.", "E agora?"):
        assert turno(cliente, quem, atribuicao, texto).status_code == 200

    payload = avaliacao(
        env,
        turnos_problematicos=[
            {"ordem": 3, "nota": 2, "motivo": "Ignorou a restricao de tamanho do turno 0."}
        ],
    )
    r = submeter(cliente, env, quem, payload)
    assert r.status_code == 200, r.text
    assert r.json()["versao"] == 1

    conn = dbmod.connect(cliente.app.state.db_anotacao)
    try:
        linha = conn.execute(
            "SELECT payload_schema, payload_json FROM anotacoes ORDER BY id DESC LIMIT 1"
        ).fetchone()
        gravado = json.loads(str(linha["payload_json"]))
        n_turnos = int(
            conn.execute("SELECT count(*) AS n FROM turnos_conversa").fetchone()["n"]
        )
    finally:
        conn.close()
    assert str(linha["payload_schema"]) == "conversa_modelo@1"
    # OS TURNOS SÃO DO SERVIDOR: o cliente não os mandou e eles estão lá, com
    # nome e digest de quem respondeu.
    assert "turnos" not in payload
    assert len(gravado["turnos"]) == 6
    assert n_turnos == 6
    assert gravado["turnos"][1]["modelo"] == modmod.modelos_configurados()[0]
    assert gravado["turnos"][1]["digest"] == TAGS_FALSAS["models"][0]["digest"]
    assert gravado["rubrica"] == convmod.ID_RUBRICA
    assert gravado["turnos_problematicos"][0]["ordem"] == 3


def test_avaliar_uma_conversa_sem_turno_nenhum_e_recusado(
    cliente: TestClient, ollama: Ollama
) -> None:
    """Uma conversa de zero turnos não tem coerência entre turnos para medir."""
    quem = anotador(cliente)
    env = pegar(cliente, quem, "conversa_modelo")
    r = submeter(cliente, env, quem, avaliacao(env))
    assert r.status_code == 409
    assert "turno" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 3. o duelo: o rótulo cego
# ---------------------------------------------------------------------------


def test_o_nome_do_modelo_nao_vaza_no_duelo_antes_do_envio(
    cliente: TestClient, ollama: Ollama
) -> None:
    """A PROVA CENTRAL DESTE MARCO, do lado do envelope.

    Se o nome viajasse — mesmo num campo que a tela não desenha —, bastaria abrir
    o inspetor para a preferência passar a ser sobre a marca. E uma preferência
    sobre a marca não vale o tempo de quem a produziu.
    """
    quem = anotador(cliente)
    env = pegar(cliente, quem, "duelo_modelos")
    atribuicao = env["atribuicao_id"]
    r = turno(cliente, quem, atribuicao, "Explique em duas frases.")
    assert r.status_code == 200, r.text

    nomes = list(modmod.modelos_configurados())
    digests = [m["digest"] for m in TAGS_FALSAS["models"]]

    def limpo(bruto: str, onde: str) -> None:
        for nome in nomes:
            assert nome not in bruto, f"{onde} vazou {nome}"
        for digest in digests:
            assert digest not in bruto, f"{onde} vazou um digest"

    limpo(r.text, "a resposta do turno")
    limpo(cliente.get(
        f"/api/conversa/{atribuicao}", params={"anotador_id": quem}
    ).text, "a leitura da conversa")
    # E pelo envelope da tarefa, que é por onde a tela redesenha depois de um
    # recarregamento de página.
    reaberta = cliente.get(
        f"/api/atribuicoes/{atribuicao}", params={"anotador_id": quem}
    )
    limpo(reaberta.text, "o envelope reaberto")
    # O que a tela recebe são os dois lados cegos.
    turnos = r.json()["conversa"]["turnos"]
    assert sorted(x["rotulo"] for x in turnos if x["papel"] == "modelo") == ["A", "B"]
    assert all("modelo" not in x for x in turnos if x["papel"] == "modelo")


def test_a_trilha_de_auditoria_tambem_nao_vaza_o_lado(
    cliente: TestClient, ollama: Ollama
) -> None:
    """`eventos` é uma porta como qualquer outra — e ninguém está olhando para ela."""
    quem = anotador(cliente)
    env = pegar(cliente, quem, "duelo_modelos")
    turno(cliente, quem, env["atribuicao_id"], "primeiro")
    conn = dbmod.connect(cliente.app.state.db_anotacao)
    try:
        detalhes = [
            json.loads(str(r["detalhe_json"]))
            for r in conn.execute("SELECT detalhe_json FROM eventos WHERE acao = 'rodada_gerada'")
        ]
    finally:
        conn.close()
    assert detalhes, "a geração da rodada não deixou rastro nenhum"
    for d in detalhes:
        # O PAR sai (é auditoria), os LADOS não — e o par vem ordenado, para não
        # ser a ordem do sorteio disfarçada de lista.
        assert d["modelos"] == sorted(modmod.modelos_configurados())
        assert "A" not in json.dumps(d) and "rotulo" not in d


def test_o_lado_de_cada_rodada_e_sorteado(
    cliente: TestClient, ollama: Ollama
) -> None:
    """Fixar ``A = primeiro da configuração`` faria a preferência medir a POSIÇÃO.

    Um anotador que perceba que "A é sempre o gemma" passa a votar na marca — e a
    cegueira do rótulo viraria teatro. Com dez rodadas, ver os dois modelos em
    "A" é praticamente certo (a chance de não ver é 2 em 1024).
    """
    quem = anotador(cliente)
    env = pegar(cliente, quem, "duelo_modelos")
    atribuicao = env["atribuicao_id"]
    conn = dbmod.connect(cliente.app.state.db_anotacao)
    try:
        for i in range(10):
            assert turno(cliente, quem, atribuicao, f"turno {i}").status_code == 200
            ordem = 2 * i + 1
            assert escolher(cliente, quem, atribuicao, ordem, "A").status_code == 200
        em_a = {
            str(r["modelo"])
            for r in conn.execute(
                "SELECT modelo FROM turnos_conversa WHERE rotulo = 'A'"
            )
        }
    finally:
        conn.close()
    assert em_a == set(modmod.modelos_configurados()), f"o lado não foi sorteado: {em_a}"


def test_a_conversa_continua_com_a_VENCEDORA(
    cliente: TestClient, ollama: Ollama
) -> None:
    """A definição do formato, em teste.

    Mandar as duas respostas faria o modelo ler a do concorrente como se fosse
    dele; mandar sempre a "A" faria a escolha do anotador não mudar nada, e o
    duelo deixaria de ser multi-turno para virar dois A/B independentes.
    """
    quem = anotador(cliente)
    env = pegar(cliente, quem, "duelo_modelos")
    atribuicao = env["atribuicao_id"]
    r = turno(cliente, quem, atribuicao, "primeiro")
    turnos = r.json()["conversa"]["turnos"]
    textos = {x["rotulo"]: x["texto"] for x in turnos if x["papel"] == "modelo"}

    assert escolher(cliente, quem, atribuicao, 1, "B").status_code == 200
    assert turno(cliente, quem, atribuicao, "segundo").status_code == 200

    historico = ollama.chamadas[-1]["messages"]
    assert [m["role"] for m in historico] == ["user", "assistant", "user"]
    assert historico[1]["content"] == textos["B"]
    assert historico[1]["content"] != textos["A"]


def test_escrever_o_proximo_turno_sem_decidir_a_rodada_e_recusado(
    cliente: TestClient, ollama: Ollama
) -> None:
    quem = anotador(cliente)
    env = pegar(cliente, quem, "duelo_modelos")
    atribuicao = env["atribuicao_id"]
    turno(cliente, quem, atribuicao, "primeiro")
    r = turno(cliente, quem, atribuicao, "segundo, sem escolher")
    assert r.status_code == 409
    assert "vencedora" in r.json()["detail"]


def test_decidir_duas_vezes_a_mesma_rodada_e_recusado(
    cliente: TestClient, ollama: Ollama
) -> None:
    """Reabrir a escolha reescreveria um histórico que o modelo seguinte já leu."""
    quem = anotador(cliente)
    env = pegar(cliente, quem, "duelo_modelos")
    atribuicao = env["atribuicao_id"]
    turno(cliente, quem, atribuicao, "primeiro")
    assert escolher(cliente, quem, atribuicao, 1, "A").status_code == 200
    r = escolher(cliente, quem, atribuicao, 1, "B")
    assert r.status_code == 409
    assert "decidida" in r.json()["detail"]


def test_a_escolha_sem_motivo_e_422(cliente: TestClient, ollama: Ollama) -> None:
    """O valor de uma preferência está no motivo, e aqui ele é POR RODADA."""
    quem = anotador(cliente)
    env = pegar(cliente, quem, "duelo_modelos")
    atribuicao = env["atribuicao_id"]
    turno(cliente, quem, atribuicao, "primeiro")
    r = cliente.post(
        f"/api/conversa/{atribuicao}/escolher",
        json={"anotador_id": quem, "ordem": 1, "rotulo": "A", "justificativa": "melhor"},
    )
    assert r.status_code == 422


def test_o_duelo_fecha_o_ciclo_e_revela_no_fim(
    cliente: TestClient, ollama: Ollama
) -> None:
    """Duas rodadas com escolha e continuação, e a revelação SÓ depois do envio."""
    quem = anotador(cliente)
    env = pegar(cliente, quem, "duelo_modelos")
    atribuicao = env["atribuicao_id"]
    turno(cliente, quem, atribuicao, "Responda em duas frases.")
    escolher(cliente, quem, atribuicao, 1, "A")
    turno(cliente, quem, atribuicao, "Agora corrija: ficou longo.")
    escolher(cliente, quem, atribuicao, 3, "B")

    r = submeter(cliente, env, quem, avaliacao(env))
    assert r.status_code == 200, r.text
    corpo = r.json()
    # A REVELAÇÃO, e só agora — com a avaliação já gravada.
    revelacao = corpo["revelacao"]
    assert [x["ordem"] for x in revelacao] == [1, 3]
    assert revelacao[0]["vencedora"] == "A"
    assert revelacao[1]["vencedora"] == "B"
    assert set(revelacao[0]["lados"][0]) == {"rotulo", "modelo", "digest"}
    assert revelacao[0]["modelo_vencedor"] in modmod.modelos_configurados()

    conn = dbmod.connect(cliente.app.state.db_anotacao)
    try:
        linha = conn.execute(
            "SELECT payload_schema, payload_json FROM anotacoes ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert str(linha["payload_schema"]) == "duelo_modelos@1"
    gravado = json.loads(str(linha["payload_json"]))
    # `turnos` é a CONVERSA (o humano + a vencedora de cada rodada); `rodadas` é
    # o dado de PREFERÊNCIA (os dois lados, quem venceu, por quê).
    assert [x["papel"] for x in gravado["turnos"]] == [
        "usuario", "modelo", "usuario", "modelo"
    ]
    assert len(gravado["rodadas"]) == 2
    assert [r["vencedora"] for r in gravado["rodadas"]] == ["A", "B"]
    for rodada in gravado["rodadas"]:
        assert len(rodada["respostas"]) == 2
        assert {r["modelo"] for r in rodada["respostas"]} == set(modmod.modelos_configurados())
        assert rodada["justificativa"]


def test_uma_falha_no_segundo_modelo_nao_deixa_meia_rodada(
    cliente: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O DEFEITO VISTO NO NAVEGADOR, em teste.

    Gravando dentro do laço, uma falha no segundo modelo deixaria a rodada com UM
    lado — e o ``sqlite3`` deste projeto é autocommit, então o primeiro já estaria
    no disco. O ``estado`` leria essa meia rodada como completa (só é "pendente"
    quem tem dois lados) e o histórico levaria adiante uma vencedora que ninguém
    escolheu. Gera os dois, DEPOIS grava os dois.
    """
    falso = Ollama()
    chamadas = {"n": 0}

    def instavel(caminho: str, corpo: Any, timeout: float) -> dict:
        if caminho == "/api/chat":
            chamadas["n"] += 1
            if chamadas["n"] == 2:  # o SEGUNDO lado da rodada
                raise modmod.SemResposta("gastou o teto no raciocínio", modelo="x", n=800)
        return falso(caminho, corpo, timeout)

    monkeypatch.setattr(modmod, "_http", instavel)
    quem = anotador(cliente)
    env = pegar(cliente, quem, "duelo_modelos")
    atribuicao = env["atribuicao_id"]

    r = turno(cliente, quem, atribuicao, "um turno qualquer")
    assert r.status_code == 503
    detalhe = r.json()["detail"]
    # A chave é a DAQUELA falha, e os marcadores dela batem com os dados — herdar
    # a chave de outra falha punha `{url}` literal na tela.
    assert detalhe["chave"] == modmod.T_SEM_RESPOSTA
    assert set(detalhe["dados"]) == {"modelo", "n"}

    conversa = detalhe["conversa"]
    assert [x["papel"] for x in conversa["turnos"]] == ["usuario"]
    assert conversa["aguardando"] == 0
    assert conversa["rodada_pendente"] is None

    # E tentar de novo gera a rodada INTEIRA, sem esbarrar no que sobrou.
    monkeypatch.setattr(modmod, "_http", falso)
    r = turno(cliente, quem, atribuicao, None)
    assert r.status_code == 200, r.text
    turnos = r.json()["conversa"]["turnos"]
    assert sorted(x["rotulo"] for x in turnos if x["papel"] == "modelo") == ["A", "B"]


def test_a_conversa_e_apagada_ao_abandonar_a_tarefa(
    cliente: TestClient, ollama: Ollama
) -> None:
    """Herdar os turnos de quem desistiu faria a rubrica ser aplicada a uma
    conversa que a pessoa não teve."""
    quem = anotador(cliente)
    env = pegar(cliente, quem, "conversa_modelo")
    atribuicao = env["atribuicao_id"]
    turno(cliente, quem, atribuicao, "um turno qualquer")
    r = cliente.post(f"/api/atribuicoes/{atribuicao}/abandonar", json={"anotador_id": quem})
    assert r.status_code == 200, r.text
    assert r.json()["turnos_apagados"] == 2
    conn = dbmod.connect(cliente.app.state.db_anotacao)
    try:
        n = int(conn.execute("SELECT count(*) AS n FROM turnos_conversa").fetchone()["n"])
    finally:
        conn.close()
    assert n == 0


def test_conversar_numa_tarefa_de_outro_tipo_e_recusado(
    cliente: TestClient, ollama: Ollama
) -> None:
    quem = anotador(cliente)
    env = pegar(cliente, quem, "escrever_rubrica")
    r = turno(cliente, quem, env["atribuicao_id"], "oi")
    assert r.status_code == 409
    assert "conversa" in r.json()["detail"]


def test_conversar_na_tarefa_de_outra_pessoa_e_403(
    cliente: TestClient, ollama: Ollama
) -> None:
    itens = cliente.get("/api/perfis").json()["items"]
    dois = [p["id"] for p in itens if p["papel"] == "anotador"][:2]
    env = pegar(cliente, dois[0], "conversa_modelo")
    r = turno(cliente, dois[1], env["atribuicao_id"], "oi")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 4. o payload: contrato, escala e a pegadinha do bool
# ---------------------------------------------------------------------------


def test_payload_fora_do_contrato_e_422(cliente: TestClient, ollama: Ollama) -> None:
    quem = anotador(cliente)
    env = pegar(cliente, quem, "conversa_modelo")
    turno(cliente, quem, env["atribuicao_id"], "um turno")
    corpo = avaliacao(env)
    corpo["preferencia"] = "modelo-a"  # campo de OUTRO contrato
    r = submeter(cliente, env, quem, corpo)
    assert r.status_code == 422
    assert any("extra_forbidden" in str(e) for e in r.json()["detail"])


def test_nota_fora_da_escala_do_criterio_e_recusada(
    cliente: TestClient, ollama: Ollama
) -> None:
    """A MESMA função pura dos outros dois escritores: ``erro_contra_a_rubrica``.

    O Pydantic não pode fazer esta checagem — ele não conhece a rubrica. 1..9 é o
    teto ABSOLUTO da plataforma; a escala REAL é a de cada critério, e a da
    rubrica multi-turno vai até 5.
    """
    quem = anotador(cliente)
    env = pegar(cliente, quem, "conversa_modelo")
    turno(cliente, quem, env["atribuicao_id"], "um turno")
    corpo = avaliacao(env)
    corpo["notas"][0]["nota"] = 8  # cabe em 1..9, não cabe em 1..5
    r = submeter(cliente, env, quem, corpo)
    assert r.status_code == 422
    assert "aceita de 1 a 5" in str(r.json()["detail"])


def test_criterio_faltando_e_recusado(cliente: TestClient, ollama: Ollama) -> None:
    quem = anotador(cliente)
    env = pegar(cliente, quem, "conversa_modelo")
    turno(cliente, quem, env["atribuicao_id"], "um turno")
    corpo = avaliacao(env)
    corpo["notas"] = corpo["notas"][:2]
    r = submeter(cliente, env, quem, corpo)
    assert r.status_code == 422
    assert "sem nota" in str(r.json()["detail"])


@pytest.mark.parametrize("campo", ["nota", "ordem"])
def test_bool_como_numero_e_recusado_no_turno_marcado(
    cliente: TestClient, ollama: Ollama, campo: str
) -> None:
    """A QUINTA VEZ que esta pegadinha aparece no repositório.

    ``bool`` é subclasse de ``int`` em Python, e o Pydantic coage ``true`` para
    ``1`` ANTES de qualquer validador comum. ``{"nota": true}`` viraria a nota 1 —
    válida, e errada. A checagem tem de ser ``mode="before"``.
    """
    quem = anotador(cliente)
    env = pegar(cliente, quem, "conversa_modelo")
    turno(cliente, quem, env["atribuicao_id"], "um turno")
    marcado: dict[str, Any] = {"ordem": 1, "nota": 3, "motivo": "um motivo qualquer"}
    marcado[campo] = True
    r = submeter(cliente, env, quem, avaliacao(env, turnos_problematicos=[marcado]))
    assert r.status_code == 422
    assert "booleano" in str(r.json()["detail"])


def test_marcar_um_turno_que_nao_existe_e_recusado(
    cliente: TestClient, ollama: Ollama
) -> None:
    """Uma nota órfã apontaria para um índice que ninguém reencontra depois."""
    quem = anotador(cliente)
    env = pegar(cliente, quem, "conversa_modelo")
    turno(cliente, quem, env["atribuicao_id"], "um turno")
    r = submeter(
        cliente,
        env,
        quem,
        avaliacao(
            env,
            turnos_problematicos=[{"ordem": 99, "nota": 2, "motivo": "um motivo qualquer"}],
        ),
    )
    assert r.status_code == 422
    assert "não existem na conversa" in str(r.json()["detail"])


def test_o_cliente_nao_consegue_inventar_quem_respondeu(
    cliente: TestClient, ollama: Ollama
) -> None:
    """A prova do "o servidor monta os turnos".

    No duelo o cliente NÃO SABE qual modelo respondeu — é o ponto inteiro do
    rótulo cego. Um "quem" vindo de quem não sabia seria fabricação, e é por isso
    que a rota sobrescreve em vez de confiar.
    """
    quem = anotador(cliente)
    env = pegar(cliente, quem, "duelo_modelos")
    atribuicao = env["atribuicao_id"]
    turno(cliente, quem, atribuicao, "primeiro")
    escolher(cliente, quem, atribuicao, 1, "A")

    mentira = avaliacao(env)
    mentira["turnos"] = [
        {"ordem": 0, "papel": "usuario", "texto": "mentira"},
        {"ordem": 1, "papel": "modelo", "texto": "mentira", "modelo": "modelo-inventado"},
    ]
    r = submeter(cliente, env, quem, mentira)
    assert r.status_code == 200, r.text
    conn = dbmod.connect(cliente.app.state.db_anotacao)
    try:
        gravado = json.loads(str(conn.execute(
            "SELECT payload_json FROM anotacoes ORDER BY id DESC LIMIT 1"
        ).fetchone()["payload_json"]))
    finally:
        conn.close()
    assert "modelo-inventado" not in json.dumps(gravado)
    assert gravado["turnos"][0]["texto"] == "primeiro"
    assert gravado["turnos"][1]["modelo"] in modmod.modelos_configurados()


# ---------------------------------------------------------------------------
# 5. schema, migração e a rubrica da plataforma
# ---------------------------------------------------------------------------


def test_a_rubrica_multiturno_mede_o_que_so_existe_em_multi_turno() -> None:
    """Uma rubrica de qualidade geral mediria de novo o que o ``avaliar_rubrica``
    já mede — e o formato existe justamente para o que aquele não alcança."""
    r = convmod.rubrica()
    assert r["rubrica_id"] == convmod.ID_RUBRICA
    assert r["origem"] == "plataforma"
    nomes = [c["nome"] for c in r["criterios"]]
    assert len(nomes) == 4 and len(set(nomes)) == 4
    for c in r["criterios"]:
        # Metadado em INGLÊS (é o que o cliente lê), com a tradução ao lado.
        assert set(c["nome_i18n"]) == {"en", "pt"}
        assert c["escala"]["min"] == 1 and c["escala"]["max"] == 5
        assert len(c["escala"]["ancoras"]) >= 2


def test_a_rubrica_nao_e_semeada_por_prompt(cliente: TestClient) -> None:
    """Ela é da PLATAFORMA: uma cópia por item faria a primeira que divergisse
    ser impossível de encontrar."""
    conn = dbmod.connect(cliente.app.state.db_anotacao)
    try:
        titulos = {str(r["titulo"]) for r in conn.execute("SELECT titulo FROM rubricas")}
    finally:
        conn.close()
    assert convmod.rubrica()["titulo"] not in titulos


def test_o_banco_v4_migra_e_ganha_os_dois_tipos(tmp_path: Path) -> None:
    """O CHECK velho não parece quebrado: parece um bug de quem tenta escrever.

    ``CREATE TABLE IF NOT EXISTS`` não reescreve o CHECK de uma tabela que já
    existe, então sem esta migração o banco do dono continuaria recusando o
    INSERT de uma tarefa ``conversa_modelo`` com uma regra que nenhum arquivo do
    repositório mostra mais.
    """
    import sqlite3

    from prompt_factory.annotate import migracao

    alvo = tmp_path / "annotate.sqlite"
    # Um banco na v4: o DDL de hoje MENOS a tabela nova e MENOS os dois valores
    # que a v5 acrescentou aos CHECKs. Construído a partir do DDL vigente (e não
    # de uma cópia congelada) para que este teste continue descrevendo a v4 real.
    antes, resto = adb.DDL.split("CREATE TABLE IF NOT EXISTS turnos_conversa", 1)
    depois = resto.split("ON turnos_conversa(atribuicao_id, ordem, rotulo);", 1)[1]
    ddl_v4 = (antes + depois).replace(", 'conversa_modelo', 'duelo_modelos'", "")

    conn = dbmod.connect(alvo)
    try:
        conn.executescript(ddl_v4)
        adb.set_meta(conn, adb.CHAVE_VERSAO, 4)
        conn.execute(
            "INSERT INTO anotadores (nome, papel) VALUES ('Alguem', 'anotador')"
        )
        conn.execute(
            "INSERT INTO tarefas (tipo, prompt_uid, origem) "
            "VALUES ('sft_resposta', 'uid-antigo', 'semente')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tarefas (tipo, prompt_uid, origem) "
                "VALUES ('conversa_modelo', 'uid-novo', 'semente')"
            )
    finally:
        conn.close()

    relatorio = migracao.migrar(alvo)

    assert relatorio["de"] == 4 and relatorio["para"] == adb.SCHEMA_VERSION_ANOTACAO
    assert relatorio["copiadas"]["tarefas"] == 1
    assert any("conversa_modelo" in aviso for aviso in relatorio["avisos"])
    conn = dbmod.connect(alvo)
    try:
        # Agora entra — e a tabela nova nasceu vazia.
        conn.execute(
            "INSERT INTO tarefas (tipo, prompt_uid, origem) "
            "VALUES ('conversa_modelo', 'uid-novo', 'semente')"
        )
        assert int(
            conn.execute("SELECT count(*) AS n FROM turnos_conversa").fetchone()["n"]
        ) == 0
    finally:
        conn.close()


def test_a_copia_da_v4_cobre_todas_as_tabelas_que_existiam() -> None:
    from prompt_factory.annotate import migracao

    cobertas = {t for t, _ in migracao.COPIA_V4_ANTES + migracao.COPIA_V4_DEPOIS}
    assert cobertas == set(adb.TABELAS) - {"app_meta"} - migracao.SEM_ORIGEM[4]


def test_os_dois_tipos_tem_diretriz_nas_duas_linguas() -> None:
    """Um tipo sem diretriz abriria um workspace sem instrução nenhuma."""
    from prompt_factory.annotate import diretrizes as dirmod

    pacote = dirmod.carregar()
    for tipo in adb.TIPOS_CONVERSA:
        assert set(pacote["tipos"][tipo]) == set(dirmod.IDIOMAS)
        for lang in dirmod.IDIOMAS:
            assert pacote["tipos"][tipo][lang]


def test_o_catalogo_oferece_conversa_sobre_qualquer_prompt(cliente: TestClient) -> None:
    """A conversa não precisa de material: a resposta é gerada na hora."""
    quem = anotador(cliente)
    corpo = cliente.get(
        "/api/catalogo", params={"anotador_id": quem, "tipo": "duelo_modelos"}
    ).json()
    assert corpo["items"]
    for item in corpo["items"]:
        assert "duelo_modelos" in item["pode"]
        assert "conversa_modelo" in item["pode"]
