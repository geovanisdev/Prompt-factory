"""Testes da API do M9 (app FastAPI sobre um SQLite sintético).

Nenhum teste toca ``data/``: cada um monta o banco em ``tmp_path`` com
``db.init_db`` e a app recebe o caminho pela fábrica ``criar_app(db, exports)``.

O corpus de teste é pequeno mas **hostil de propósito**. Cada linha esquisita
está aqui porque corresponde a algo medido no corpus real: o prompt de 200 mil
caracteres, o template do robô com ``n_exact_dups=120``, os marcadores
``User:``/``Assistant:`` embutidos, ``nsfw`` nos três estados (NULL, 0 e 1), a
licença que proíbe redistribuir e a que proíbe uso comercial.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from prompt_factory import db as dbmod
from prompt_factory import export as exportmod
from prompt_factory import schema, textnorm
from prompt_factory.app.main import criar_app
from prompt_factory.app.queries import ORDER_BY, sanitize_fts

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

#: ~200 mil caracteres. O corpus real tem prompts de ~1 milhão: mandar isso
#: inteiro numa página de 50 seriam dezenas de MB de JSON.
TEXTO_GIGANTE = "Explique detalhadamente o seguinte trecho. " * 4800

#: As linhas com nome próprio. Todo `source` é uma fonte de verdade do
#: `config/sources.toml`, senão `attribution` sairia vazia no export.
ESPECIAIS: tuple[dict[str, Any], ...] = (
    {
        "uid": "coracao000000001",
        "text": "Meu coração está partido e eu preciso de conselho",
        "lang": "pt", "lang_variant": "pt-BR", "source": "aya", "license": "apache-2.0",
        "task_type": "conselho-opiniao", "domain": "relacionamentos-pessoal", "quality": 3,
        "nsfw": 0,
    },
    {
        "uid": "coracao000000002",
        "text": "O coração dela bate forte quando pensa nisso",
        "lang": "pt", "lang_variant": "pt-indef", "source": "wildchat_pt",
        "license": "odc-by-1.0", "task_type": "geracao-criativa", "domain": "artes-entretenimento",
        "quality": 2,
    },
    {
        "uid": "paodequeijo00001",
        "text": "Quero uma receita de pão de queijo mineiro bem fácil",
        "lang": "pt", "lang_variant": "pt-BR", "source": "aya", "license": "apache-2.0",
        "task_type": "qa-aberta", "domain": "culinaria", "quality": 3,
    },
    {
        "uid": "emailhifen000001",
        "text": "Como configuro meu e-mail no celular novo?",
        "lang": "pt", "lang_variant": "pt-indef", "source": "arena140k", "license": "cc-by-4.0",
    },
    {
        "uid": "gigante000000001",
        "text": TEXTO_GIGANTE,
        "lang": "en", "source": "wildchat_en", "license": "odc-by-1.0",
    },
    {
        "uid": "chatmarkers00001",
        "text": "User: oi tudo bem\nAssistant: tudo, e você?\nUser: resuma isto",
        "lang": "en", "source": "wildchat_en", "license": "odc-by-1.0",
    },
    {
        # O robô: 33,9% do português do WildChat é este template.
        "uid": "robotemplate0001",
        "text": "Usando o seguinte texto: DIÁRIO OFICIAL DO MUNICÍPIO, extraia os nomes",
        "lang": "pt", "lang_variant": "pt-indef", "source": "wildchat_pt",
        "license": "odc-by-1.0", "n_exact_dups": 120,
    },
    {"uid": "nsfwnulo00000001", "text": "linha com nsfw nulo", "lang": "en",
     "source": "dolly", "license": "cc-by-sa-3.0", "nsfw": None},
    {"uid": "nsfwzero00000001", "text": "linha com nsfw zero", "lang": "en",
     "source": "dolly", "license": "cc-by-sa-3.0", "nsfw": 0},
    {"uid": "nsfwum0000000001", "text": "linha marcada como nsfw explicito", "lang": "en",
     "source": "dolly", "license": "cc-by-sa-3.0", "nsfw": 1},
    {
        # redistributable = 0: fica FORA do export por padrão.
        "uid": "lmsysbloqueado01",
        "text": "prompt de fonte que proibe redistribuicao",
        "lang": "pt", "lang_variant": "pt-indef", "source": "lmsys", "license": "lmsys-1m",
        "commercial_ok": 0, "redistributable": 0,
    },
    {
        # commercial_ok = 0: ENTRA no export, contabilizado à parte.
        "uid": "naocomercial0001",
        "text": "prompt sob licenca nao comercial",
        "lang": "en", "source": "no_robots", "license": "cc-by-nc-4.0", "commercial_ok": 0,
    },
    {"uid": "comaspas00000001", "text": 'Ele disse "bom dia" e saiu sem olhar',
     "lang": "pt", "lang_variant": "pt-PT", "source": "arena140k", "license": "cc-by-4.0"},
    {"uid": "soemoji000000001", "text": "🎉🎉🎉", "lang": "en",
     "source": "hh_rlhf", "license": "mit"},
    {"uid": "sopontuacao00001", "text": "!!!???...", "lang": "en",
     "source": "hh_rlhf", "license": "mit"},
    {"uid": "compii0000000001", "text": "meu telefone antigo aparecia aqui",
     "lang": "pt", "lang_variant": "pt-BR", "source": "aya", "license": "apache-2.0",
     "pii_found": 1},
    {"uid": "precisarevisar01", "text": "prompt ambiguo que precisa de revisao",
     "lang": "pt", "lang_variant": "pt-indef", "source": "oasst", "license": "apache-2.0",
     "needs_review": 1},
    {"uid": "japones000000001", "text": "日本語のテキストです", "lang": "en",
     "source": "prism", "license": "cc-by-4.0"},
)

#: Preenchimento com tamanho IDÊNTICO de propósito: empate em ``n_chars`` é o
#: que faz uma ordenação sem desempate por ``id`` repetir linha entre páginas.
N_FILLER = 34


def _linhas() -> list[dict[str, Any]]:
    linhas = [dict(e) for e in ESPECIAIS]
    for i in range(N_FILLER):
        linhas.append(
            {
                "uid": f"filler{i:010d}",
                "text": f"prompt de preenchimento numero {i:03d} para paginacao",
                "lang": "pt" if i % 2 else "en",
                "lang_variant": "pt-indef" if i % 2 else None,
                "source": "aya" if i % 2 else "dolly",
                "license": "apache-2.0" if i % 2 else "cc-by-sa-3.0",
                "task_type": "outro" if i % 3 == 0 else None,
                "domain": "geral" if i % 3 == 0 else None,
                "quality": 1 if i % 5 == 0 else None,
            }
        )
    return linhas


def _inserir(conn: sqlite3.Connection, linha: dict[str, Any], ordem: int) -> None:
    texto = str(linha["text"])
    n_chars, n_words = schema.text_stats(texto)
    dados: dict[str, Any] = {
        "uid": linha["uid"],
        "text": texto,
        "lang": linha.get("lang", "pt"),
        "lang_variant": linha.get("lang_variant"),
        "source": linha.get("source", "aya"),
        "source_id": f"src-{ordem}",
        "source_split": "train",
        "license": linha.get("license", "apache-2.0"),
        "commercial_ok": linha.get("commercial_ok", 1),
        "redistributable": linha.get("redistributable", 1),
        "task_type": linha.get("task_type"),
        "domain": linha.get("domain"),
        "quality": linha.get("quality"),
        "nsfw": linha.get("nsfw"),
        "pii_found": linha.get("pii_found", 0),
        "label_method": "agent" if linha.get("task_type") else None,
        "label_confidence": None,
        "needs_review": linha.get("needs_review", 0 if linha.get("task_type") else 1),
        "hash_norm": hashlib.sha256(textnorm.norm_for_hash(texto).encode()).hexdigest(),
        "n_exact_dups": linha.get("n_exact_dups", 0),
        "n_near_dups": 0,
        "n_chars": n_chars,
        "n_words": n_words,
        # created_ts distinto por linha: `sort=newest` precisa de ordem total.
        "created_ts": f"2024-{1 + ordem % 12:02d}-{1 + ordem % 28:02d}T10:00:00Z",
        "native_category": linha.get("native_category"),
        "meta_json": json.dumps({"origem": "teste", "ordem": ordem}, ensure_ascii=False),
    }
    colunas = ", ".join(dados)
    marcas = ", ".join(f":{c}" for c in dados)
    conn.execute(f"INSERT INTO prompts ({colunas}) VALUES ({marcas})", dados)


def montar_banco(caminho: Path, *, sem_rotulo: bool = False) -> int:
    """Cria o banco sintético. Devolve o número de linhas."""
    conn = dbmod.connect(caminho)
    try:
        dbmod.init_db(conn)
        dbmod.set_meta(conn, "taxonomy_version", schema.TAXONOMY_VERSION)
        dbmod.set_meta(conn, "db_build_id", "0123456789abcdef")
        dbmod.set_meta(conn, "built_at", "2026-07-31T00:00:00Z")
        linhas = _linhas()
        for i, linha in enumerate(linhas):
            if sem_rotulo:
                # O estado REAL do banco hoje: a campanha do M6/M7 não rodou.
                linha = {**linha, "task_type": None, "domain": None,
                         "quality": None, "nsfw": None}
            _inserir(conn, linha, i)
        return len(linhas)
    finally:
        conn.close()


@pytest.fixture
def cliente(tmp_path: Path):
    """App sobre o banco sintético completo. O ``with`` roda o lifespan."""
    montar_banco(tmp_path / "prompts.sqlite")
    with TestClient(criar_app(tmp_path / "prompts.sqlite", tmp_path / "exports")) as c:
        yield c


@pytest.fixture
def cliente_sem_rotulo(tmp_path: Path):
    """App sobre um banco onde NENHUMA linha tem rótulo (o estado de hoje)."""
    montar_banco(tmp_path / "cru.sqlite", sem_rotulo=True)
    with TestClient(criar_app(tmp_path / "cru.sqlite", tmp_path / "exports")) as c:
        yield c


def uids(payload: dict[str, Any]) -> list[str]:
    return [item["uid"] for item in payload["items"]]


def listar(cliente: TestClient, **params: Any) -> dict[str, Any]:
    resposta = cliente.get("/api/prompts", params=params)
    assert resposta.status_code == 200, resposta.text
    return resposta.json()


# ---------------------------------------------------------------------------
# saúde, estáticos e panorama
# ---------------------------------------------------------------------------


def test_health_e_estaticos(cliente: TestClient) -> None:
    saude = cliente.get("/api/health").json()
    assert saude["status"] == "ok"
    assert saude["n_rows"] == len(_linhas())
    assert saude["schema_version"] == dbmod.SCHEMA_VERSION
    assert saude["db_build_id"] == "0123456789abcdef"

    # O mount do StaticFiles vem POR ÚLTIMO: se estivesse antes dos routers,
    # ele engoliria /api/* e o teste acima já teria falhado.
    pagina = cliente.get("/")
    assert pagina.status_code == 200
    assert "Prompt Factory" in pagina.text


def test_app_recusa_subir_sem_banco(tmp_path: Path) -> None:
    """Servidor que sobe e dá 500 em tudo é pior que servidor que não sobe."""
    with pytest.raises(RuntimeError, match="pf load-db"):
        with TestClient(criar_app(tmp_path / "nao-existe.sqlite")):
            pass


def test_stats_traz_a_taxonomia_inteira(cliente: TestClient) -> None:
    s = cliente.get("/api/stats").json()
    assert s["total"] == len(_linhas())
    assert [t["value"] for t in s["taxonomia"]["task_type"]] == list(schema.TASK_TYPES)
    assert [d["value"] for d in s["taxonomia"]["domain"]] == list(schema.DOMAINS)
    # Rótulo legível vindo do taxonomy.json, não uma segunda cópia na interface.
    assert s["taxonomia"]["task_type"][0]["label"] != s["taxonomia"]["task_type"][0]["value"]

    assert s["nsfw"]["sim"] + s["nsfw"]["nao"] + s["nsfw"]["nulo"] == s["total"]
    assert s["com_marcadores_chat"] >= 1
    assert s["top_duplicados"][0]["uid"] == "robotemplate0001"
    assert s["top_duplicados"][0]["n_exact_dups"] == 120


# ---------------------------------------------------------------------------
# busca textual
# ---------------------------------------------------------------------------


def test_busca_sem_acento_acha_com_acento(cliente: TestClient) -> None:
    """O ``remove_diacritics 2`` do FTS tem de valer nos dois sentidos, e a
    ordem também: mesma consulta, mesmos ids, mesma sequência."""
    com = listar(cliente, q="coração")
    sem = listar(cliente, q="coracao")
    maiuscula = listar(cliente, q="CORACAO")
    assert com["total"] == sem["total"] == maiuscula["total"] == 2
    assert uids(com) == uids(sem) == uids(maiuscula)
    assert set(uids(com)) == {"coracao000000001", "coracao000000002"}


def test_busca_por_prefixo(cliente: TestClient) -> None:
    resultado = listar(cliente, q="coraca*")
    assert set(uids(resultado)) == {"coracao000000001", "coracao000000002"}


def test_busca_por_frase_entre_aspas(cliente: TestClient) -> None:
    frase = listar(cliente, q='"pão de queijo"')
    assert uids(frase) == ["paodequeijo00001"]
    # As mesmas palavras soltas casam a frase também; o que a aspa garante é
    # que uma linha com as três palavras separadas NÃO casaria.
    assert listar(cliente, q="queijo")["total"] == 1


def test_hifen_nao_derruba_a_busca(cliente: TestClient) -> None:
    """Sem o sanitizador, ``e-mail`` vira ``e NOT mail`` e o FTS5 responde
    ``no such column: mail``. Este é o caso mais banal que quebra a rota."""
    resultado = listar(cliente, q="e-mail")
    assert resultado["total"] == 1
    assert uids(resultado) == ["emailhifen000001"]


#: 22 entradas hostis. Nenhuma pode devolver 5xx nem alterar o banco.
HOSTIS: tuple[str, ...] = (
    '"', '""', 'a" OR b', "aspas'solta", "(", ")", "()", "*", "***", "---", "-",
    "^", ":", "NEAR", "OR", "AND", "NOT", "a OR b NOT c", "\\", "  ", "",
    "'; DROP TABLE prompts; --",
)


@pytest.mark.parametrize("entrada", HOSTIS)
def test_query_hostil_nao_derruba_a_rota(cliente: TestClient, entrada: str) -> None:
    resposta = cliente.get("/api/prompts", params={"q": entrada})
    assert resposta.status_code == 200, f"{entrada!r} -> {resposta.status_code} {resposta.text[:200]}"
    # E a tabela continua inteira depois de cada uma delas.
    assert cliente.get("/api/stats").json()["total"] == len(_linhas())


def test_query_so_de_pontuacao_nao_vira_match_vazio(cliente: TestClient) -> None:
    """``MATCH ''`` casa zero linhas, o que seria uma busca silenciosamente
    vazia. Sem token sobrando, a busca simplesmente não é aplicada — e o
    envelope diz isso em ``q_fts``."""
    resultado = listar(cliente, q="***", nsfw="include")
    assert resultado["q_fts"] is None
    assert resultado["total"] == len(_linhas())


def test_sanitizador_preserva_frase_e_prefixo() -> None:
    assert sanitize_fts("e-mail") == '"e-mail"'
    assert sanitize_fts('"pão de queijo"') == '"pão de queijo"'
    assert sanitize_fts("coraca*") == '"coraca"*'
    assert sanitize_fts("a OR b") == '"a" "OR" "b"'
    assert sanitize_fts("***") == ""
    assert sanitize_fts(None) == ""


def test_snippet_destaca_o_termo(cliente: TestClient) -> None:
    """Na busca o card mostra o TRECHO com o termo, não o começo do texto —
    num prompt de 200 mil caracteres o começo não diz nada sobre o acerto."""
    resultado = listar(cliente, q="coração")
    for item in resultado["items"]:
        assert item["snippet"] is not None
        assert "<mark>" in item["snippet"]
    # Sem busca não há snippet (e não há score).
    sem_busca = listar(cliente)
    assert all(item["snippet"] is None for item in sem_busca["items"])
    assert all(item["score"] is None for item in sem_busca["items"])


def test_score_so_existe_com_busca(cliente: TestClient) -> None:
    resultado = listar(cliente, q="coração")
    # bm25 é NEGATIVO e menor é melhor; o envelope ordena por ele ascendente.
    assert all(item["score"] is not None and item["score"] < 0 for item in resultado["items"])
    assert resultado["sort_efetivo"] == "relevance"


# ---------------------------------------------------------------------------
# filtros
# ---------------------------------------------------------------------------


def test_nsfw_exclude_mantem_os_nao_rotulados(cliente: TestClient) -> None:
    """``nsfw = 0`` perderia as linhas NULL em silêncio. ``IS NOT 1`` não."""
    padrao = listar(cliente)
    assert "nsfwnulo00000001" in uids(padrao)
    assert "nsfwzero00000001" in uids(padrao)
    assert "nsfwum0000000001" not in uids(padrao)

    incluindo = listar(cliente, nsfw="include", page_size=200)
    assert incluindo["total"] == len(_linhas())

    so_nsfw = listar(cliente, nsfw="only")
    assert uids(so_nsfw) == ["nsfwum0000000001"]


def test_quality_min_exclui_os_sem_rotulo(cliente: TestClient) -> None:
    resultado = listar(cliente, quality_min=3, page_size=200)
    assert set(uids(resultado)) == {"coracao000000001", "paodequeijo00001"}


def test_max_dups_derruba_o_robo(cliente: TestClient) -> None:
    com_robo = listar(cliente, page_size=200)
    assert "robotemplate0001" in uids(com_robo)
    sem_robo = listar(cliente, max_dups=0, page_size=200)
    assert "robotemplate0001" not in uids(sem_robo)
    assert sem_robo["total"] == com_robo["total"] - 1


def test_filtros_combinados(cliente: TestClient) -> None:
    resultado = listar(cliente, lang="pt", lang_variant="pt-BR", source="aya", page_size=200)
    assert set(uids(resultado)) == {"coracao000000001", "paodequeijo00001", "compii0000000001"}

    assert listar(cliente, pii="only")["total"] == 1
    assert listar(cliente, needs_review=True, page_size=200)["total"] >= 1
    assert listar(cliente, commercial_only=True, page_size=200)["total"] == len(_linhas()) - 3
    assert listar(cliente, n_chars_min=1000)["total"] == 1  # só o gigante


def test_parametro_desconhecido_vira_422(cliente: TestClient) -> None:
    """``?task_types=codigo`` (plural) tem de FALHAR. Ignorar o parâmetro
    devolveria o corpus inteiro como se fosse o recorte pedido."""
    assert cliente.get("/api/prompts", params={"task_types": "codigo"}).status_code == 422
    assert cliente.get("/api/facets", params={"lingua": "pt"}).status_code == 422


def test_valor_fora_da_taxonomia_vira_422(cliente: TestClient) -> None:
    assert cliente.get("/api/prompts", params={"task_type": "inventado"}).status_code == 422
    assert cliente.get("/api/prompts", params={"license": "wtfpl"}).status_code == 422
    assert cliente.get("/api/prompts", params={"quality": 9}).status_code == 422


# ---------------------------------------------------------------------------
# ordenação e paginação
# ---------------------------------------------------------------------------


def test_relevance_sem_busca_cai_para_newest(cliente: TestClient) -> None:
    resultado = listar(cliente, sort="relevance")
    assert resultado["sort"] == "relevance"
    assert resultado["sort_efetivo"] == "newest"


@pytest.mark.parametrize("ordem", sorted(ORDER_BY))
def test_paginacao_estavel_em_toda_ordenacao(cliente: TestClient, ordem: str) -> None:
    """Concatenar as páginas tem de dar o conjunto exato: sem repetir e sem
    perder. É o desempate por ``p.id`` que garante isso — sem ele, ``n_chars
    DESC`` com empates (e o preenchimento é todo do mesmo tamanho) devolve a
    mesma linha em duas páginas."""
    total = listar(cliente, nsfw="include")["total"]
    visto: list[str] = []
    for pagina in range(1, 5):
        parcial = listar(cliente, sort=ordem, nsfw="include", page=pagina, page_size=15)
        visto.extend(uids(parcial))
    assert len(visto) == total
    assert len(set(visto)) == total


def test_random_embaralha_de_verdade(cliente: TestClient) -> None:
    """A fórmula de UM módulo — ``(id * seed) % 1000003`` — NÃO embaralha: com
    ids pequenos o produto nunca passa do módulo e a ordem sai idêntica à de
    ``id``. Este teste é exatamente o que pega aquele bug."""
    por_id = uids(listar(cliente, sort="oldest", nsfw="include", page_size=200))
    a = uids(listar(cliente, sort="random", seed=7, nsfw="include", page_size=200))
    b = uids(listar(cliente, sort="random", seed=7, nsfw="include", page_size=200))
    c = uids(listar(cliente, sort="random", seed=99, nsfw="include", page_size=200))

    assert a == b, "mesma semente tem de dar a mesma ordem (senão a paginação repete item)"
    assert a != c, "sementes diferentes têm de dar ordens diferentes"
    assert set(a) == set(por_id), "é permutação, não filtro"
    assert a != por_id, "sort=random devolveu a ordem por id — a fórmula não embaralha"


def test_paginacao_alem_do_teto_vira_400(cliente: TestClient) -> None:
    resposta = cliente.get("/api/prompts", params={"page": 99999, "page_size": 200})
    assert resposta.status_code == 400
    assert "max_offset" in resposta.json()["detail"]


def test_page_size_acima_do_teto_vira_422(cliente: TestClient) -> None:
    assert cliente.get("/api/prompts", params={"page_size": 5000}).status_code == 422


# ---------------------------------------------------------------------------
# truncamento
# ---------------------------------------------------------------------------


def test_listagem_trunca_e_detalhe_nao(cliente: TestClient) -> None:
    resultado = listar(cliente, q="detalhadamente")
    item = next(i for i in resultado["items"] if i["uid"] == "gigante000000001")
    assert item["text_truncated"] is True
    assert len(item["text"]) < 2000
    assert item["n_chars"] == len(TEXTO_GIGANTE)

    inteiro = cliente.get("/api/prompts/gigante000000001").json()
    assert inteiro["text_truncated"] is False
    assert inteiro["text"] == TEXTO_GIGANTE
    assert inteiro["attribution"].startswith("Zhao, Wenting")
    assert inteiro["meta"]["origem"] == "teste"


def test_marcadores_de_chat_viram_sinal(cliente: TestClient) -> None:
    item = cliente.get("/api/prompts/chatmarkers00001").json()
    assert item["has_chat_markers"] is True
    assert cliente.get("/api/prompts/paodequeijo00001").json()["has_chat_markers"] is False


def test_detalhe_de_uid_inexistente_e_404(cliente: TestClient) -> None:
    assert cliente.get("/api/prompts/naoexiste0000000").status_code == 404


def test_booleanos_sao_booleanos_e_null_e_null(cliente: TestClient) -> None:
    """``null`` é "não sabemos", ``false`` é "sabemos que não". A interface
    precisa dos dois; 0/1 no JSON apagaria a diferença."""
    nulo = cliente.get("/api/prompts/nsfwnulo00000001").json()
    zero = cliente.get("/api/prompts/nsfwzero00000001").json()
    assert nulo["nsfw"] is None
    assert zero["nsfw"] is False
    assert zero["pii_found"] is False
    assert zero["commercial_ok"] is True


def test_classe_da_licenca(cliente: TestClient) -> None:
    """Licença é sinal de primeira classe: o usuário monta dataset para
    terceiros e não pode descobrir o CC-BY-NC depois de entregar."""
    classes = {
        "coracao000000001": "livre",          # apache-2.0
        "naocomercial0001": "nao-comercial",  # cc-by-nc-4.0
        "lmsysbloqueado01": "bloqueada",      # redistributable = 0
        "nsfwzero00000001": "viral",          # cc-by-sa-3.0
    }
    for uid, esperado in classes.items():
        assert cliente.get(f"/api/prompts/{uid}").json()["license_class"] == esperado


# ---------------------------------------------------------------------------
# edição e revert
# ---------------------------------------------------------------------------


def test_editar_preserva_o_original_na_primeira_edicao_apenas(cliente: TestClient) -> None:
    original = cliente.get("/api/prompts/coracao000000001").json()["text"]

    primeira = cliente.patch(
        "/api/prompts/coracao000000001", json={"text": "Primeira correção manual"}
    )
    assert primeira.status_code == 200
    assert primeira.json()["edited"] is True
    assert primeira.json()["text_original"] == original

    segunda = cliente.patch(
        "/api/prompts/coracao000000001", json={"text": "Segunda correção manual"}
    ).json()
    assert segunda["text"] == "Segunda correção manual"
    # O CASE do UPDATE lê `text` com o valor ANTIGO da linha: na segunda vez
    # `edited` já é 1 e o original de verdade sobrevive.
    assert segunda["text_original"] == original


def test_fts_reflete_as_duas_transicoes(cliente: TestClient) -> None:
    """Editar e reverter têm de mexer no índice nas DUAS direções — o trigger
    ``prompts_fts_au`` é ``AFTER UPDATE OF text``, então ele só dispara quando
    ``text`` aparece no SET."""
    assert listar(cliente, q="partido")["total"] == 1

    cliente.patch("/api/prompts/coracao000000001", json={"text": "agora fala de abacaxi"})
    assert listar(cliente, q="partido")["total"] == 0
    assert uids(listar(cliente, q="abacaxi")) == ["coracao000000001"]

    cliente.post("/api/prompts/coracao000000001/revert")
    assert listar(cliente, q="abacaxi")["total"] == 0
    assert uids(listar(cliente, q="partido")) == ["coracao000000001"]


def test_revert_devolve_o_original_e_o_segundo_da_400(cliente: TestClient) -> None:
    original = cliente.get("/api/prompts/paodequeijo00001").json()["text"]
    cliente.patch("/api/prompts/paodequeijo00001", json={"text": "outra coisa"})

    revertido = cliente.post("/api/prompts/paodequeijo00001/revert")
    assert revertido.status_code == 200
    assert revertido.json()["text"] == original
    assert revertido.json()["edited"] is False
    assert revertido.json()["text_original"] is None

    repetido = cliente.post("/api/prompts/paodequeijo00001/revert")
    assert repetido.status_code == 400
    assert "não foi editado" in repetido.json()["detail"]


def test_editar_rotulo_vira_manual_e_nao_toca_edited(cliente: TestClient) -> None:
    """Corrigir um rótulo não é editar o texto: ``edited`` continua 0 e o FTS
    não é reindexado. E ``label_confidence`` vira NULL, nunca 1.0 — confiança
    de humano não é probabilidade e envenenaria a fila de revisão."""
    antes = cliente.get("/api/prompts/precisarevisar01").json()
    assert antes["needs_review"] is True

    depois = cliente.patch(
        "/api/prompts/precisarevisar01", json={"task_type": "codigo", "domain": "tecnologia"}
    ).json()
    assert depois["task_type"] == "codigo"
    assert depois["label_method"] == "manual"
    assert depois["label_confidence"] is None
    assert depois["needs_review"] is False
    assert depois["edited"] is False
    assert depois["text"] == antes["text"]


def test_patch_recusa_corpo_vazio_e_valores_invalidos(cliente: TestClient) -> None:
    assert cliente.patch("/api/prompts/coracao000000001", json={}).status_code == 422
    assert cliente.patch(
        "/api/prompts/coracao000000001", json={"task_type": "inventado"}
    ).status_code == 422
    # bool é subclasse de int: sem a checagem explícita, `true` viraria 1.
    assert cliente.patch(
        "/api/prompts/coracao000000001", json={"quality": True}
    ).status_code == 422
    assert cliente.patch(
        "/api/prompts/coracao000000001", json={"text": "   "}
    ).status_code == 422
    assert cliente.patch("/api/prompts/naoexiste0000000", json={"quality": 2}).status_code == 404


# ---------------------------------------------------------------------------
# facetas
# ---------------------------------------------------------------------------


def test_facetas_de_dimensao_nao_filtrada_somam_o_total(cliente: TestClient) -> None:
    dados = cliente.get("/api/facets", params={"lang": "pt"}).json()
    total = dados["total"]
    for dimensao in ("source", "license", "task_type", "domain", "quality"):
        soma = sum(o["count"] for o in dados["facets"][dimensao])
        assert soma == total, f"{dimensao} somou {soma}, esperado {total}"


def test_faceta_filtrada_nao_zera_as_proprias_opcoes(cliente: TestClient) -> None:
    """Marcar "pt" não pode zerar "en": a dimensão é contada com o filtro dela
    mesma removido, senão o usuário fica preso no primeiro clique que deu."""
    dados = cliente.get("/api/facets", params={"lang": "pt"}).json()
    idiomas = {o["value"]: o for o in dados["facets"]["lang"]}
    assert idiomas["pt"]["selected"] is True
    assert idiomas["en"]["selected"] is False
    assert idiomas["en"]["count"] > 0
    assert idiomas["pt"]["count"] + idiomas["en"]["count"] > dados["total"]


def test_facetas_taxonomicas_saem_na_ordem_com_os_zeros(cliente: TestClient) -> None:
    dados = cliente.get("/api/facets").json()
    valores = [o["value"] for o in dados["facets"]["task_type"] if o["value"] is not None]
    assert valores[: len(schema.TASK_TYPES)] == list(schema.TASK_TYPES)
    assert any(o["count"] == 0 for o in dados["facets"]["task_type"])
    # A classe vazia precisa APARECER desabilitada; sumir parece bug.
    assert {o["value"] for o in dados["facets"]["domain"]} >= set(schema.DOMAINS)


def test_facetas_booleanas_tem_rotulo_em_portugues(cliente: TestClient) -> None:
    """``str(True)`` daria "True" e ``str(None)`` daria "None" na barra lateral
    de uma interface em português. O que o usuário precisa ler em ``nsfw=null``
    é "não rotulado", que é uma informação diferente de "não"."""
    dados = cliente.get("/api/facets").json()
    rotulos = {o["value"]: o["label"] for o in dados["facets"]["nsfw"]}
    assert rotulos == {True: "sim", False: "não", None: "não rotulado"}
    assert {o["label"] for o in dados["facets"]["needs_review"]} == {"sim", "não"}


def test_faceta_de_tamanho_devolve_a_faixa_pronta(cliente: TestClient) -> None:
    dados = cliente.get("/api/facets").json()
    tamanhos = {o["value"]: o for o in dados["facets"]["n_chars_bucket"]}
    assert set(tamanhos) == {"curto", "medio", "longo"}
    assert tamanhos["longo"]["filter"]["n_chars_min"] > 0
    aplicado = listar(cliente, **tamanhos["longo"]["filter"], page_size=200)
    assert aplicado["total"] == tamanhos["longo"]["count"]


def test_facetas_com_busca_ativa(cliente: TestClient) -> None:
    dados = cliente.get("/api/facets", params={"q": "coração"}).json()
    assert dados["total"] == 2
    assert sum(o["count"] for o in dados["facets"]["source"]) == 2


# ---------------------------------------------------------------------------
# banco sem rótulo nenhum (o estado REAL de hoje)
# ---------------------------------------------------------------------------


def test_banco_sem_rotulo_nao_quebra_nada(cliente_sem_rotulo: TestClient) -> None:
    c = cliente_sem_rotulo
    assert c.get("/api/health").json()["rotulagem_pendente"] is True
    assert c.get("/api/stats").json()["rotulados"] == 0

    facetas = c.get("/api/facets").json()
    assert facetas["rotulagem_pendente"] is True
    # A taxonomia continua listada, toda zerada: a interface desabilita o grupo
    # com explicação em vez de escondê-lo.
    task = {o["value"]: o["count"] for o in facetas["facets"]["task_type"]}
    assert set(task) >= set(schema.TASK_TYPES)
    assert all(task[k] == 0 for k in schema.TASK_TYPES)
    assert task[None] == c.get("/api/stats").json()["total"]

    # E a listagem segue funcionando, inclusive com nsfw todo NULL.
    assert listar(c, page_size=200)["total"] == len(_linhas())
    assert listar(c, q="coracao")["total"] == 2


# ---------------------------------------------------------------------------
# coleções
# ---------------------------------------------------------------------------


def criar_colecao(cliente: TestClient, nome: str = "curadoria") -> int:
    resposta = cliente.post("/api/collections", json={"name": nome})
    assert resposta.status_code == 201, resposta.text
    return int(resposta.json()["id"])


def test_colecao_crud_e_nome_repetido(cliente: TestClient) -> None:
    colecao_id = criar_colecao(cliente)
    assert cliente.post("/api/collections", json={"name": "curadoria"}).status_code == 409

    renomeada = cliente.patch(f"/api/collections/{colecao_id}", json={"name": "final"})
    assert renomeada.status_code == 200
    assert renomeada.json()["name"] == "final"

    assert cliente.get("/api/collections").json()["items"][0]["n_items"] == 0
    assert cliente.delete(f"/api/collections/{colecao_id}").status_code == 204
    assert cliente.get(f"/api/collections/{colecao_id}").status_code == 404


def test_adicionar_uids_e_idempotente(cliente: TestClient) -> None:
    colecao_id = criar_colecao(cliente)
    alvo = ["coracao000000001", "paodequeijo00001", "coracao000000001", "naoexiste0000000"]

    primeira = cliente.post(f"/api/collections/{colecao_id}/items", json={"uids": alvo}).json()
    assert primeira["added"] == 2
    assert primeira["not_found"] == ["naoexiste0000000"]
    assert primeira["n_items"] == 2

    segunda = cliente.post(f"/api/collections/{colecao_id}/items", json={"uids": alvo}).json()
    assert segunda["added"] == 0
    assert segunda["already_present"] == 2
    assert segunda["n_items"] == 2

    # Listar os itens de uma coleção é a listagem normal com collection_id.
    assert listar(cliente, collection_id=colecao_id)["total"] == 2

    removida = cliente.post(
        f"/api/collections/{colecao_id}/items/remove", json={"uids": ["paodequeijo00001"]}
    ).json()
    assert removida["removed"] == 1
    assert removida["n_items"] == 1


def test_from_filter_adiciona_exatamente_o_que_a_faceta_prometeu(cliente: TestClient) -> None:
    """O gesto que define a ferramenta: facetar, ver o número, e mandar TODOS
    para a coleção sem paginar. Se a contagem prometida e a entregue divergem,
    o usuário para de confiar na barra lateral."""
    facetas = cliente.get("/api/facets").json()
    prometido = {o["value"]: o["count"] for o in facetas["facets"]["source"]}
    assert prometido["aya"] > 3

    colecao_id = criar_colecao(cliente)
    resultado = cliente.post(
        f"/api/collections/{colecao_id}/items/from-filter",
        json={"filter": {"source": ["aya"]}},
    ).json()
    assert resultado["matched"] == prometido["aya"]
    assert resultado["added"] == prometido["aya"]
    assert resultado["n_items"] == prometido["aya"]
    assert listar(cliente, collection_id=colecao_id, page_size=200)["total"] == prometido["aya"]


def test_from_filter_com_limite_e_remocao_da_familia_do_robo(cliente: TestClient) -> None:
    colecao_id = criar_colecao(cliente)
    # "Adicionar os 5 primeiros do filtro atual".
    parcial = cliente.post(
        f"/api/collections/{colecao_id}/items/from-filter",
        json={"filter": {}, "limit": 5, "sort": "oldest"},
    ).json()
    assert parcial["matched"] == 5

    # E agora o inverso: tirar de uma vez tudo que tem duplicata (o robô).
    cliente.post(
        f"/api/collections/{colecao_id}/items/from-filter", json={"filter": {"nsfw": "include"}}
    )
    antes = cliente.get(f"/api/collections/{colecao_id}").json()["n_items"]
    removidos = cliente.post(
        f"/api/collections/{colecao_id}/items/remove-from-filter",
        json={"filter": {"n_chars_min": 0, "nsfw": "include", "max_dups": None}},
    ).json()
    assert removidos["removed"] == antes
    assert removidos["n_items"] == 0


def test_apagar_colecao_nao_apaga_prompt(cliente: TestClient) -> None:
    colecao_id = criar_colecao(cliente)
    cliente.post(f"/api/collections/{colecao_id}/items", json={"uids": ["coracao000000001"]})
    # A coleção aparece no detalhe do item.
    assert cliente.get("/api/prompts/coracao000000001").json()["collections"] == [
        {"id": colecao_id, "name": "curadoria"}
    ]
    cliente.delete(f"/api/collections/{colecao_id}")
    assert cliente.get("/api/prompts/coracao000000001").status_code == 200
    assert cliente.get("/api/stats").json()["total"] == len(_linhas())


def test_colecao_inexistente_e_404(cliente: TestClient) -> None:
    assert cliente.post("/api/collections/999/items", json={"uids": ["x"]}).status_code == 404
    assert cliente.get("/api/collections/999").status_code == 404


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def exportar(cliente: TestClient, **corpo: Any) -> dict[str, Any]:
    resposta = cliente.post("/api/export", json={"mode": "filter", "filter": {}, **corpo})
    assert resposta.status_code == 200, resposta.text
    return resposta.json()


def test_export_jsonl_bate_com_o_manifesto_e_com_a_tabela(cliente: TestClient) -> None:
    """``row_count`` do manifesto == linhas do arquivo == ``exports.row_count``.
    Contado no laço, nunca por um ``count(*)`` prévio: o filtro de licença corta
    linhas DEPOIS da consulta."""
    resultado = exportar(cliente, format="jsonl", name="teste-jsonl")
    manifest = resultado["manifest"]
    arquivo = Path(resultado["path"])

    linhas = [json.loads(linha) for linha in arquivo.read_text(encoding="utf-8").splitlines()]
    assert len(linhas) == manifest["row_count"] == resultado["row_count"]
    assert sum(manifest["counts"]["source"].values()) == manifest["row_count"]

    historico = cliente.get("/api/exports").json()["items"]
    assert historico[0]["row_count"] == manifest["row_count"]
    assert historico[0]["exists"] is True
    assert historico[0]["manifest"]["sha256"] == manifest["sha256"]

    # O sha256 do manifesto é o do arquivo que ficou em disco.
    assert hashlib.sha256(arquivo.read_bytes()).hexdigest() == manifest["sha256"]

    # A ordem das chaves é contrato (um diff entre dois exports iguais é vazio).
    assert tuple(linhas[0]) == exportmod.CAMPOS_FLAT

    # JSON não aceita `null` como chave de objeto: o não rotulado vira "(nulo)",
    # não "None" (que num manifesto em português parece nome de classe).
    assert exportmod.CHAVE_NULA in manifest["counts"]["task_type"]
    assert "None" not in manifest["counts"]["task_type"]


def test_export_exclui_nao_redistribuivel_e_conta(cliente: TestClient) -> None:
    resultado = exportar(cliente, name="sem-lmsys")
    manifest = resultado["manifest"]
    assert manifest["excluded_nonredistributable_count"] == 1
    assert "WARNING" not in manifest

    conteudo = Path(resultado["path"]).read_text(encoding="utf-8")
    assert "lmsysbloqueado01" not in conteudo
    # CC-BY-NC NÃO é excluída — só contabilizada.
    assert "naocomercial0001" in conteudo
    assert manifest["nao_comercial_count"] >= 1
    assert "NOTA_COMERCIAL" in manifest


def test_export_com_nao_redistribuivel_carimba_warning(cliente: TestClient) -> None:
    resultado = exportar(cliente, include_nonredistributable=True, name="com-lmsys")
    assert "lmsysbloqueado01" in Path(resultado["path"]).read_text(encoding="utf-8")
    assert "NÃO publique" in resultado["manifest"]["WARNING"]
    assert resultado["manifest"]["excluded_nonredistributable_count"] == 0


def test_toda_linha_do_export_tem_attribution(cliente: TestClient) -> None:
    """Sem ``attribution`` o export não cumpre a licença: ODC-BY, CC-BY e
    CC-BY-SA exigem crédito a cada uso. E ela **não é coluna do banco** — vem
    de ``config/sources.toml``."""
    resultado = exportar(cliente, name="creditos")
    linhas = [json.loads(x) for x in Path(resultado["path"]).read_text(encoding="utf-8").splitlines()]
    assert linhas, "export vazio"
    for linha in linhas:
        assert linha["attribution"], f"{linha['uid']} ({linha['source']}) sem atribuição"
    assert set(resultado["manifest"]["attributions"]) == {
        linha["source"] for linha in linhas
    }


def test_export_csv_reabre_com_parser_e_o_texto_sobrevive(cliente: TestClient) -> None:
    """O texto tem quebra de linha DENTRO do campo: só um parser de CSV de
    verdade reconstrói. ``utf-8-sig`` é o que faz o Excel pt-BR não quebrar o
    acento."""
    resultado = exportar(cliente, format="csv", name="planilha")
    arquivo = Path(resultado["path"])
    bruto = arquivo.read_bytes()
    assert bruto.startswith(b"\xef\xbb\xbf"), "faltou o BOM que o Excel pt-BR precisa"

    # MEDIDO: o módulo csv recusa campo acima de 131.072 caracteres, e este
    # corpus tem prompts de 200 mil. O arquivo está certo — o LEITOR precisa
    # subir o limite. É por isso que o manifesto avisa.
    assert "NOTA_CSV" in resultado["manifest"]
    limite_antigo = csv.field_size_limit(10**7)
    try:
        with arquivo.open(encoding="utf-8-sig", newline="") as fh:
            linhas = list(csv.DictReader(fh))
    finally:
        csv.field_size_limit(limite_antigo)
    assert len(linhas) == resultado["row_count"]
    por_uid = {linha["uid"]: linha for linha in linhas}
    assert por_uid["chatmarkers00001"]["text"] == (
        "User: oi tudo bem\nAssistant: tudo, e você?\nUser: resuma isto"
    )
    assert "coração" in por_uid["coracao000000001"]["text"]
    # Booleano vira texto estável; nulo vira vazio.
    assert por_uid["nsfwzero00000001"]["nsfw"] == "false"
    assert por_uid["nsfwnulo00000001"]["nsfw"] == ""


def test_export_por_colecao_e_por_uids(cliente: TestClient) -> None:
    colecao_id = criar_colecao(cliente)
    cliente.post(
        f"/api/collections/{colecao_id}/items",
        json={"uids": ["coracao000000001", "paodequeijo00001"]},
    )
    por_colecao = cliente.post(
        "/api/export", json={"mode": "collection", "collection_id": colecao_id, "name": "col"}
    ).json()
    assert por_colecao["row_count"] == 2
    assert por_colecao["manifest"]["filters"]["collection_name"] == "curadoria"

    por_uids = cliente.post(
        "/api/export",
        json={"mode": "uids", "uids": ["coracao000000001"], "name": "uids"},
    ).json()
    assert por_uids["row_count"] == 1

    faltando = cliente.post(
        "/api/export", json={"mode": "uids", "uids": ["naoexiste0000000"]}
    )
    assert faltando.status_code == 400


def test_export_modo_incoerente_vira_422(cliente: TestClient) -> None:
    # mode=filter sem filter, e mode=filter com collection_id junto.
    assert cliente.post("/api/export", json={"mode": "filter"}).status_code == 422
    assert cliente.post(
        "/api/export", json={"mode": "filter", "filter": {}, "collection_id": 1}
    ).status_code == 422
    assert cliente.post(
        "/api/export", json={"mode": "filter", "filter": {}, "profile": "label-studio"}
    ).status_code == 400


def test_export_com_text_original(cliente: TestClient) -> None:
    cliente.patch("/api/prompts/coracao000000001", json={"text": "editado à mão"})
    resultado = exportar(cliente, include_text_original=True, name="com-original")
    linhas = {
        json.loads(x)["uid"]: json.loads(x)
        for x in Path(resultado["path"]).read_text(encoding="utf-8").splitlines()
    }
    assert linhas["coracao000000001"]["text"] == "editado à mão"
    assert "partido" in linhas["coracao000000001"]["text_original"]
    # Linha nunca editada: o "original" é o próprio texto, não NULL.
    assert linhas["paodequeijo00001"]["text_original"] == linhas["paodequeijo00001"]["text"]


def test_download_do_export(cliente: TestClient) -> None:
    resultado = exportar(cliente, name="baixar")
    baixado = cliente.get(resultado["download_url"])
    assert baixado.status_code == 200
    assert hashlib.sha256(baixado.content).hexdigest() == resultado["manifest"]["sha256"]
    assert cliente.get("/api/exports/9999/download").status_code == 404


def test_nome_de_arquivo_e_higienizado() -> None:
    """O nome chega pela API e vira caminho em disco."""
    assert exportmod.nome_de_arquivo("../../etc/passwd") == "etc-passwd"
    assert exportmod.nome_de_arquivo("meu recorte 2024") == "meu-recorte-2024"
    assert exportmod.nome_de_arquivo("").startswith("export_")
    assert exportmod.nome_de_arquivo("///").startswith("export_")


def test_registry_de_formatos_separa_perfil_de_container() -> None:
    """``exports.format`` tem CHECK no DDL: o container é que vai para lá, e é
    isso que deixa um perfil Label Studio entrar sem migração."""
    assert set(exportmod.REGISTRY) == {"flat.jsonl", "flat.csv"}
    for chave, fmt in exportmod.REGISTRY.items():
        assert chave == f"{fmt.profile}.{fmt.container}"
        assert fmt.container in ("jsonl", "csv")
    assert exportmod.REGISTRY["flat.csv"].encoding == "utf-8-sig"
    with pytest.raises(KeyError, match="label-studio"):
        exportmod.formato("label-studio.jsonl")


def test_previa_csv_serializa_como_o_arquivo() -> None:
    texto = exportmod.previa_csv(
        [{"uid": "a", "text": 'com "aspas"\ne quebra'}], ("uid", "text")
    )
    linhas = list(csv.DictReader(io.StringIO(texto)))
    assert linhas[0]["text"] == 'com "aspas"\ne quebra'
