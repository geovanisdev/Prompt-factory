"""Campanha de rotulagem (M5): s07, ``labeling_io`` e s08 — tudo em ``tmp_path``.

Nenhum teste toca em ``labeling/`` nem em ``data/``: a ``StageConfig`` do M5
tem ``labeling_dir`` justamente para isso. O universo é sintético (fixture
``make_table`` das 27 colunas), pequeno e com uma fonte deliberadamente MENOR
que a cota — é o caso do ``oasst`` no universo real, e é o único jeito de provar
que a redistribuição de déficit funciona sem esperar o dado real.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from prompt_factory import labeling_io as lio
from prompt_factory.schema import TAXONOMY_VERSION
from prompt_factory.stages import SEED_LABELS, SEED_PARQUET, SEED_STRATA, UNIVERSE, StageConfig
from prompt_factory.stages import s07_seed_sample as s07
from prompt_factory.stages import s08_merge_labels as s08

from .conftest import escrever

# Cotas do settings.toml real são grandes demais para um universo de teste: as
# funções recebem o alvo por injeção, mas o s07 lê config. Os testes que rodam o
# s07 inteiro monkeypatcham `config.get` com estes valores.
COTAS_TESTE: dict[str, Any] = {
    "size": 60,
    "pt_share": 0.5,
    "rng_seed": 42,
    "bucket_edges": [30, 120],
    "bucket_floor": 0.10,
    "allocation": {
        "pt": {"wildchat_pt": 20, "aya": 8, "oasst": 2},
        "en": {"wildchat_en": 18, "no_robots": 6, "dolly": 6},
    },
}
LABELING_TESTE: dict[str, Any] = {
    "batch_size": 10,
    "gold_per_batch": 3,
    "calibration_size": 6,
    "truncate_chars": 40,
    "agreement_min": 0.80,
    "claim_ttl_hours": 2,
    "max_attempts": 3,
    "mapping_warn": 0.30,
}


@pytest.fixture
def config_teste(monkeypatch: pytest.MonkeyPatch) -> None:
    """Substitui ``config.get`` pelas cotas de teste (thresholds seguem em config)."""
    from prompt_factory import config

    original = config.get

    def _get(secao: str, chave: str, default: Any = None, **kw: Any) -> Any:
        if secao == "seed" and chave in COTAS_TESTE:
            return COTAS_TESTE[chave]
        if secao == "labeling" and chave in LABELING_TESTE:
            return LABELING_TESTE[chave]
        return original(secao, chave, default, **kw) if default is None else original(
            secao, chave, default
        )

    for modulo in ("prompt_factory.labeling_io", "prompt_factory.stages.s07_seed_sample",
                   "prompt_factory.stages.s08_merge_labels"):
        monkeypatch.setattr(f"{modulo}.get", _get)


def _universo(make_table: Callable[..., Any]) -> Any:
    """~180 linhas: 3 fontes pt (uma minúscula) e 3 en, tamanhos variados."""
    linhas = []
    plano = [
        ("pt", "wildchat_pt", 60),
        ("pt", "aya", 30),
        ("pt", "oasst", 1),   # MENOR que a cota de 2: força a redistribuição
        ("en", "wildchat_en", 50),
        ("en", "no_robots", 20),
        ("en", "dolly", 20),
    ]
    categorias = {
        "no_robots": ["Open QA", "Coding", "Generation", "Summarize"],
        "dolly": ["open_qa", "classification", "summarization", "creative_writing"],
    }
    i = 0
    for idioma, fonte, quantos in plano:
        for k in range(quantos):
            i += 1
            # Tamanhos espalhados pelas 3 faixas (<30, 30-120, >120).
            corpo = "x" * (5 + (k % 3) * 70)
            nativa = categorias.get(fonte, [])
            linhas.append(
                {
                    "uid": f"{i:016x}",
                    "text": f"{idioma} {fonte} {k} {corpo}",
                    "lang": idioma,
                    "source": fonte,
                    "source_id": str(i),
                    "native_category": nativa[k % len(nativa)] if nativa else None,
                }
            )
    return make_table(linhas)


@pytest.fixture
def campanha(tmp_path: Path, make_table: Callable[..., Any], config_teste: None) -> StageConfig:
    """Universo sintético + s07 rodado: semente, lotes e manifest em tmp_path."""
    cfg = StageConfig(data_dir=tmp_path / "data", labeling_dir=tmp_path / "labeling")
    cfg.preparar_dirs()
    escrever(_universo(make_table), cfg.caminho(UNIVERSE))
    assert s07.run(cfg) == 0
    return cfg


def _lp(cfg: StageConfig) -> lio.LabelingPaths:
    return lio.LabelingPaths(cfg.labeling_dir)


def _resposta(uids: list[str], **sobrescreve: Any) -> str:
    """JSONL válido para um lote, com campos default sensatos."""
    linhas = []
    for uid in uids:
        obj = {
            "uid": uid,
            "task_type": "qa-aberta",
            "domain": "geral",
            "quality": 3,
            "nsfw": False,
        }
        obj.update(sobrescreve)
        linhas.append(json.dumps(obj, ensure_ascii=False))
    return "\n".join(linhas) + "\n"


# ---------------------------------------------------------------------------
# s07 — amostragem
# ---------------------------------------------------------------------------


def test_s07_produz_semente_lotes_e_manifest(campanha: StageConfig) -> None:
    semente = pq.read_table(campanha.caminho_labeling(SEED_PARQUET))
    assert tuple(semente.schema.names) == s07.COLUNAS_SEED
    manifest = lio.carregar_manifest(_lp(campanha))
    assert manifest["taxonomy_version"] == TAXONOMY_VERSION
    assert manifest["calibration"]["status"] == lio.GOLD_PENDING
    assert manifest["gold"] is None
    # Todo item da semente termina em algum lugar: 6 na calibração + o resto
    # fatiado em lotes de 7 novos (batch_size 10 - 3 ouros).
    novos = sum(r["n_novos"] for r in manifest["batches"].values())
    assert novos + manifest["calibration"]["n_items"] == semente.num_rows
    assert campanha.caminho_labeling(SEED_STRATA).is_file()


def test_s07_e_deterministico(tmp_path: Path, make_table: Callable[..., Any], config_teste: None) -> None:
    uids = []
    for rodada in ("a", "b"):
        cfg = StageConfig(data_dir=tmp_path / rodada, labeling_dir=tmp_path / f"lab_{rodada}")
        cfg.preparar_dirs()
        escrever(_universo(make_table), cfg.caminho(UNIVERSE))
        assert s07.run(cfg) == 0
        tabela = pq.read_table(cfg.caminho_labeling(SEED_PARQUET))
        uids.append(tabela.column("uid").to_pylist())
    assert uids[0] == uids[1]


def test_s07_redistribui_deficit_de_fonte_pequena(campanha: StageConfig) -> None:
    manifest = lio.carregar_manifest(_lp(campanha))
    pt = manifest["allocation"]["pt"]
    # oasst tem 1 linha e cota 2: trava em 1 e o resto vai para as outras.
    assert pt["final"]["oasst"] == 1
    assert pt["final"]["wildchat_pt"] > pt["planejado"]["wildchat_pt"]
    assert sum(pt["final"].values()) == 30
    assert any("oasst cede" in nota for nota in pt["notas"])
    texto = campanha.caminho_labeling(SEED_STRATA).read_text(encoding="utf-8")
    assert "redistribuição de déficit" in texto


def test_s07_calibracao_sai_de_dentro_da_semente(campanha: StageConfig) -> None:
    manifest = lio.carregar_manifest(_lp(campanha))
    da_semente = set(
        pq.read_table(campanha.caminho_labeling(SEED_PARQUET)).column("uid").to_pylist()
    )
    calibracao = set(manifest["calibration"]["uids"])
    assert calibracao <= da_semente
    # ...e NÃO reaparece como item novo de nenhum lote.
    novos: set[str] = set()
    for batch_id, registro in manifest["batches"].items():
        do_lote = set(lio.uids_do_lote(batch_id, _lp(campanha)))
        novos |= do_lote - set(registro["gold_uids"])
    assert not (novos & calibracao)


def test_s07_recusa_reexecucao_com_campanha_viva(campanha: StageConfig) -> None:
    lp = _lp(campanha)
    manifest = lio.carregar_manifest(lp)
    manifest["batches"]["batch_0001"]["status"] = lio.DONE
    lio.salvar_manifest(manifest, lp)
    with pytest.raises(SystemExit, match="campanha em andamento"):
        s07.run(campanha)
    forcado = StageConfig(
        data_dir=campanha.data_dir, labeling_dir=campanha.labeling_dir, force=True
    )
    assert s07.run(forcado) == 0


def test_maior_resto_fecha_a_conta_exata() -> None:
    partes = s07.maior_resto({"a": 1.0, "b": 1.0, "c": 1.0}, 10)
    assert sum(partes.values()) == 10
    # Empate de resto desempata pela ordem do dict (= ordem do settings.toml).
    assert partes["a"] >= partes["c"]


def test_repartir_faixas_respeita_piso_e_disponibilidade() -> None:
    reparte = s07.repartir_faixas(100, {"curto": 5, "medio": 500, "longo": 500}, 0.10)
    assert sum(reparte.values()) == 100
    assert reparte["curto"] == 5  # existem só 5, leva os 5 (o piso pediria 10)


# ---------------------------------------------------------------------------
# lotes: ouro embutido e truncamento
# ---------------------------------------------------------------------------


def test_lote_esconde_os_ouros_sem_marcacao(campanha: StageConfig) -> None:
    lp = _lp(campanha)
    manifest = lio.carregar_manifest(lp)
    batch_id = "batch_0001"
    dados = lio.carregar_lote(batch_id, lp)
    ouros = manifest["batches"][batch_id]["gold_uids"]
    assert len(ouros) == 3
    # Nenhuma chave denuncia o ouro: os itens têm exatamente uid/lang/text.
    assert {tuple(sorted(item)) for item in dados["items"]} == {("lang", "text", "uid")}
    assert set(ouros) <= {item["uid"] for item in dados["items"]}


def test_ouros_nao_ficam_todos_no_fim(campanha: StageConfig) -> None:
    """Se os 3 fossem anexados no fim, o agente aprenderia a posição deles."""
    lp = _lp(campanha)
    manifest = lio.carregar_manifest(lp)
    posicoes: list[int] = []
    for batch_id, registro in manifest["batches"].items():
        uids = lio.uids_do_lote(batch_id, lp)
        posicoes.extend(uids.index(u) for u in registro["gold_uids"])
    assert min(posicoes) < len(lio.uids_do_lote("batch_0001", lp)) - 3


def test_truncamento_avisa_que_cortou() -> None:
    assert lio.truncar("abc", 10) == "abc"
    assert lio.truncar("a" * 10, 10) == "a" * 10  # limite exato NÃO leva sufixo
    cortado = lio.truncar("a" * 50, 10)
    assert cortado == "a" * 10 + lio.SUFIXO_TRUNCADO
    assert len(cortado) == 10 + len(lio.SUFIXO_TRUNCADO)


# ---------------------------------------------------------------------------
# validação
# ---------------------------------------------------------------------------


def test_validar_aceita_resposta_correta(campanha: StageConfig) -> None:
    lp = _lp(campanha)
    uids = lio.uids_do_lote("batch_0001", lp)
    ok, linhas, erros = lio.validar_resposta("batch_0001", _resposta(uids), lp)
    assert ok, erros
    assert [linha["uid"] for linha in linhas] == uids


def test_validar_reordena_e_apenas_avisa(campanha: StageConfig) -> None:
    lp = _lp(campanha)
    uids = lio.uids_do_lote("batch_0001", lp)
    ok, linhas, avisos = lio.validar_resposta("batch_0001", _resposta(uids[::-1]), lp)
    assert ok
    assert [linha["uid"] for linha in linhas] == uids  # sai na ordem do LOTE
    assert any("ordem diferente" in a for a in avisos)


def test_validar_exige_cobertura_exata(campanha: StageConfig) -> None:
    lp = _lp(campanha)
    uids = lio.uids_do_lote("batch_0001", lp)
    ok, _, erros = lio.validar_resposta("batch_0001", _resposta(uids[:-2]), lp)
    assert not ok
    assert any("faltam 2 uid" in e for e in erros)

    repetido = _resposta(uids) + _resposta([uids[0]])
    ok, _, erros = lio.validar_resposta("batch_0001", repetido, lp)
    assert not ok
    assert any("repetido" in e for e in erros)

    intruso = _resposta([*uids, "ffffffffffffffff"])
    ok, _, erros = lio.validar_resposta("batch_0001", intruso, lp)
    assert not ok
    assert any("não pertence ao lote" in e for e in erros)


@pytest.mark.parametrize(
    ("campo", "valor", "trecho"),
    [
        ("task_type", "inventada", "task_type"),
        ("domain", "metaverso", "domain"),
        ("quality", 9, "quality"),
        ("quality", "3", "quality"),
        # bool é subclasse de int: sem o teste explícito, True passaria como 1.
        ("quality", True, "quality"),
        ("nsfw", "false", "nsfw"),
        ("nsfw", 0, "nsfw"),
    ],
)
def test_validar_recusa_valor_fora_do_contrato(
    campanha: StageConfig, campo: str, valor: Any, trecho: str
) -> None:
    lp = _lp(campanha)
    uids = lio.uids_do_lote("batch_0001", lp)
    texto = _resposta(uids[:1], **{campo: valor}) + _resposta(uids[1:])
    ok, _, erros = lio.validar_resposta("batch_0001", texto, lp)
    assert not ok
    assert any(trecho in e and "linha 1" in e for e in erros)


def test_validar_tolera_flag_unsure_e_recusa_campo_extra(campanha: StageConfig) -> None:
    lp = _lp(campanha)
    uids = lio.uids_do_lote("batch_0001", lp)
    ok, linhas, _ = lio.validar_resposta(
        "batch_0001", _resposta(uids, flag="unsure"), lp
    )
    assert ok and linhas[0]["flag"] == "unsure"
    ok, _, erros = lio.validar_resposta("batch_0001", _resposta(uids, motivo="porque sim"), lp)
    assert not ok
    assert any("campo desconhecido" in e for e in erros)


def test_parse_tolera_cerca_prosa_eco_e_crlf() -> None:
    texto = (
        "﻿Aqui está o JSONL pedido:\r\n"
        "```jsonl\r\n"
        '{"uid":"a","task_type":"qa-aberta","domain":"geral","quality":3,"nsfw":false}\r\n'
        '{"uid":"b","lang":"pt","text":"eco do input"}\r\n'
        "{isto não é json}\r\n"
        "```\r\n"
    )
    objetos, avisos = lio.parse_jsonl(texto)
    assert [o["uid"] for _, o in objetos] == ["a"]
    assert any("eco do input" in a for a in avisos)
    assert any("JSON inválido" in a or "não é JSON" in a for a in avisos)


def test_uids_para_retry_lista_so_o_que_falta(campanha: StageConfig) -> None:
    lp = _lp(campanha)
    uids = lio.uids_do_lote("batch_0001", lp)
    _, linhas, _ = lio.validar_resposta("batch_0001", _resposta(uids[:-2]), lp)
    assert lio.uids_para_retry("batch_0001", linhas, lp) == uids[-2:]


# ---------------------------------------------------------------------------
# máquina de estados
# ---------------------------------------------------------------------------


def test_claim_e_conclusao(campanha: StageConfig) -> None:
    lp = _lp(campanha)
    alvos = lio.claim(n=2, lp=lp)
    assert alvos == ["batch_0001", "batch_0002"]
    manifest = lio.carregar_manifest(lp)
    assert manifest["batches"]["batch_0001"]["status"] == lio.CLAIMED
    assert manifest["batches"]["batch_0001"]["claimed_at"]

    uids = lio.uids_do_lote("batch_0001", lp)
    _, linhas, _ = lio.validar_resposta("batch_0001", _resposta(uids), lp)
    lio.concluir("batch_0001", linhas, "haiku", lp=lp)
    manifest = lio.carregar_manifest(lp)
    assert manifest["batches"]["batch_0001"]["status"] == lio.DONE
    assert manifest["batches"]["batch_0001"]["agent_model"] == "haiku"
    assert lp.rotulos("batch_0001").is_file()


def test_claim_nunca_reivindica_duas_vezes(campanha: StageConfig) -> None:
    lp = _lp(campanha)
    assert lio.claim("batch_0001", lp=lp) == ["batch_0001"]
    with pytest.raises(ValueError, match="já está em 'claimed'"):
        lio.claim("batch_0001", lp=lp)
    # E o claim em lote nunca devolve um lote já reivindicado.
    assert "batch_0001" not in lio.claim(n=3, lp=lp)


@pytest.mark.parametrize(
    ("de", "para"),
    [(lio.PENDING, lio.DONE), (lio.PENDING, lio.FAILED), (lio.DONE, lio.CLAIMED)],
)
def test_transicoes_proibidas(campanha: StageConfig, de: str, para: str) -> None:
    lp = _lp(campanha)
    manifest = lio.carregar_manifest(lp)
    manifest["batches"]["batch_0002"]["status"] = de
    lio.salvar_manifest(manifest, lp)
    acao = {lio.DONE: lio.concluir, lio.FAILED: lio.falhar, lio.CLAIMED: lio.claim}[para]
    with pytest.raises(ValueError, match="não é permitida"):
        acao("batch_0002", [], lp=lp) if para != lio.CLAIMED else acao("batch_0002", lp=lp)


def test_varredura_devolve_orfao_e_quarentena_depois_de_n_tentativas(
    campanha: StageConfig,
) -> None:
    lp = _lp(campanha)
    manifest = lio.carregar_manifest(lp)
    registro = manifest["batches"]["batch_0001"]
    for esperado in (1, 2):
        registro["status"] = lio.CLAIMED
        registro["claimed_at"] = "2020-01-01T00:00:00Z"  # muito além do TTL
        devolvidos, quarentena = lio.varrer_orfaos(manifest)
        assert devolvidos == ["batch_0001"] and not quarentena
        assert registro["tentativas"] == esperado
    registro["status"] = lio.CLAIMED
    registro["claimed_at"] = "2020-01-01T00:00:00Z"
    devolvidos, quarentena = lio.varrer_orfaos(manifest)
    assert quarentena == ["batch_0001"] and registro["status"] == lio.FAILED
    lio.reenfileirar("batch_0001", "revivido", lp=lp, manifest=manifest)
    assert registro["status"] == lio.PENDING


def test_claim_recente_nao_e_orfao(campanha: StageConfig) -> None:
    lp = _lp(campanha)
    lio.claim("batch_0001", lp=lp)
    manifest = lio.carregar_manifest(lp)
    devolvidos, quarentena = lio.varrer_orfaos(manifest)
    assert not devolvidos and not quarentena


def test_manifest_e_escrito_atomicamente(campanha: StageConfig, monkeypatch) -> None:
    """Falha no meio da escrita deixa o manifest ANTERIOR intacto, não um híbrido."""
    lp = _lp(campanha)
    antes = lp.manifest.read_text(encoding="utf-8")
    monkeypatch.setattr(
        lio.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("disco cheio"))
    )
    with pytest.raises(OSError, match="disco cheio"):
        lio.salvar_manifest({"taxonomy_version": TAXONOMY_VERSION, "lixo": True}, lp)
    assert lp.manifest.read_text(encoding="utf-8") == antes


def test_manifest_recusa_taxonomia_divergente(campanha: StageConfig) -> None:
    lp = _lp(campanha)
    dados = json.loads(lp.manifest.read_text(encoding="utf-8"))
    dados["taxonomy_version"] = "0.9"
    lp.manifest.write_text(json.dumps(dados), encoding="utf-8")
    with pytest.raises(SystemExit, match="taxonomia"):
        lio.carregar_manifest(lp)


# ---------------------------------------------------------------------------
# ouro e agreement
# ---------------------------------------------------------------------------


def _importar_ouro(cfg: StageConfig, **sobrescreve: Any) -> list[str]:
    lp = _lp(cfg)
    uids = lio.uids_do_lote(lio.BATCH_CALIBRACAO, lp)
    ok, _, erros = lio.importar_ouro(_resposta(uids, **sobrescreve), lp)
    assert ok, erros
    return uids


def test_agreement_sem_ouro_e_none(campanha: StageConfig) -> None:
    lp = _lp(campanha)
    lio.claim("batch_0001", lp=lp)
    uids = lio.uids_do_lote("batch_0001", lp)
    _, linhas, _ = lio.validar_resposta("batch_0001", _resposta(uids), lp)
    # None e 0.0 são coisas diferentes: "não medido" não pode reprovar o lote.
    assert lio.concluir("batch_0001", linhas, lp=lp) is None


def test_agreement_cheio_e_parcial(campanha: StageConfig) -> None:
    lp = _lp(campanha)
    _importar_ouro(campanha)
    manifest = lio.carregar_manifest(lp)
    assert manifest["calibration"]["status"] == lio.GOLD

    uids = lio.uids_do_lote("batch_0001", lp)
    _, iguais, _ = lio.validar_resposta("batch_0001", _resposta(uids), lp)
    assert lio.calcular_agreement("batch_0001", iguais, lp=lp) == 1.0

    # Erra o domain de UM dos 3 ouros: 5 acertos em 6 comparações.
    ouros = manifest["batches"]["batch_0001"]["gold_uids"]
    texto = _resposta([u for u in uids if u != ouros[0]]) + _resposta(
        [ouros[0]], domain="ciencia"
    )
    _, quase, _ = lio.validar_resposta("batch_0001", texto, lp)
    assert lio.calcular_agreement("batch_0001", quase, lp=lp) == pytest.approx(5 / 6)


def test_importar_ouro_faz_backfill_do_agreement(campanha: StageConfig) -> None:
    lp = _lp(campanha)
    lio.claim("batch_0001", lp=lp)
    uids = lio.uids_do_lote("batch_0001", lp)
    _, linhas, _ = lio.validar_resposta("batch_0001", _resposta(uids), lp)
    lio.concluir("batch_0001", linhas, lp=lp)
    assert lio.carregar_manifest(lp)["batches"]["batch_0001"]["agreement"] is None
    _importar_ouro(campanha)
    assert lio.carregar_manifest(lp)["batches"]["batch_0001"]["agreement"] == 1.0


def test_importar_ouro_recusa_cobertura_incompleta(campanha: StageConfig) -> None:
    lp = _lp(campanha)
    uids = lio.uids_do_lote(lio.BATCH_CALIBRACAO, lp)
    ok, manifest, erros = lio.importar_ouro(_resposta(uids[:-1]), lp)
    assert not ok and any("faltam" in e for e in erros)
    assert manifest["gold"] is None


def test_painel_ignora_agreement_de_lote_reenfileirado(campanha: StageConfig) -> None:
    """O lote reprovado guarda a nota que o reprovou, mas sai da média."""
    lp = _lp(campanha)
    _importar_ouro(campanha, task_type="resumo", domain="ciencia")
    lio.claim("batch_0001", lp=lp)
    uids = lio.uids_do_lote("batch_0001", lp)
    _, linhas, _ = lio.validar_resposta("batch_0001", _resposta(uids), lp)
    manifest = lio.carregar_manifest(lp)
    assert lio.concluir("batch_0001", linhas, lp=lp, manifest=manifest) == 0.0
    lio.reenfileirar("batch_0001", "agreement baixo", lp=lp, manifest=manifest)
    lio.salvar_manifest(manifest, lp)

    p = lio.painel(lp)
    assert p["agreement_medio"] is None  # nenhum lote done: nada a promediar
    assert p["n_rotulos"] == 0
    # ...mas a nota fica registrada no lote, para diagnóstico.
    assert lio.carregar_manifest(lp)["batches"]["batch_0001"]["agreement"] == 0.0


def test_o_motivo_de_nao_haver_agreement_nomeia_a_CAUSA(campanha: StageConfig) -> None:
    """São três causas independentes, e dizer a errada é pior que não dizer.

    O caso real que expôs isto: no dia em que o ouro foi importado, com 0 lotes
    concluídos, o painel continuou dizendo "sem ouro" — e quem lesse aquilo
    concluiria que a importação de 100 itens revisados à mão tinha falhado.
    """
    lp = _lp(campanha)

    # 1. Antes do ouro: a causa é o ouro mesmo.
    p = lio.painel(lp)
    assert p["agreement_motivo"] == "sem ouro importado"
    assert lio.texto_agreement(p) == "não medido (sem ouro importado)"

    # 2. Com ouro e nenhum lote concluído: a causa mudou, e a frase também.
    _importar_ouro(campanha)
    p = lio.painel(lp)
    assert p["agreement_motivo"] == "nenhum lote concluído ainda"
    assert "sem ouro" not in lio.texto_agreement(p)

    # 3. Com lote concluído E medido: motivo nenhum, e o número aparece.
    lio.claim("batch_0001", lp=lp)
    uids = lio.uids_do_lote("batch_0001", lp)
    _, linhas, _ = lio.validar_resposta("batch_0001", _resposta(uids), lp)
    lio.concluir("batch_0001", linhas, lp=lp)
    p = lio.painel(lp)
    assert p["agreement_motivo"] is None
    assert lio.texto_agreement(p, casas=2) == f"{p['agreement_medio']:.2f}"


def test_motivo_de_UM_lote_e_diferente_do_motivo_da_campanha(campanha: StageConfig) -> None:
    """Depois do submit a pergunta é sobre o lote, não sobre a média."""
    lp = _lp(campanha)
    _importar_ouro(campanha)
    manifest = lio.carregar_manifest(lp)
    assert (
        lio.motivo_sem_agreement(manifest, "batch_0001")
        == "este lote não tem item de calibração"
    )
    assert lio.motivo_sem_agreement(manifest) == "nenhum lote concluído ainda"


def test_painel_resume_a_campanha(campanha: StageConfig) -> None:
    lp = _lp(campanha)
    lio.claim("batch_0001", lp=lp)
    p = lio.painel(lp)
    assert p["contagem"][lio.CLAIMED] == 1
    assert p["contagem"][lio.PENDING] == p["n_lotes"] - 1
    assert p["calibracao_status"] == lio.GOLD_PENDING
    assert p["agreement_medio"] is None


# ---------------------------------------------------------------------------
# mapeamentos nativos
# ---------------------------------------------------------------------------


def test_mapeamentos_reais_apontam_para_a_taxonomia() -> None:
    from prompt_factory import paths
    from prompt_factory.schema import TASK_TYPES

    mapas = s08.carregar_mapeamentos(paths.MAPPINGS)
    assert set(mapas) == {"no_robots", "dolly"}
    assert len(mapas["no_robots"]["map"]) == 10  # as 10 categorias reais da fonte
    assert len(mapas["dolly"]["map"]) == 8
    for spec in mapas.values():
        assert spec["taxonomy_version"] == TAXONOMY_VERSION
        assert spec["axis"] == "task_type"
        assert 0 < float(spec["weight"]) <= 1
        for alvo in spec["map"].values():
            assert alvo is None or alvo in TASK_TYPES
    # Categoria sem correspondência limpa é null E documentada.
    assert mapas["no_robots"]["map"]["Generation"] is None
    assert "Generation" in mapas["no_robots"]["_unmapped_note"]


def test_mapeamento_com_alvo_invalido_e_fatal(tmp_path: Path) -> None:
    (tmp_path / "x.json").write_text(
        json.dumps({"source": "x", "map": {"a": "classe-que-nao-existe"}}), encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="fora da taxonomia"):
        s08.carregar_mapeamentos(tmp_path)


# ---------------------------------------------------------------------------
# s08 — merge
# ---------------------------------------------------------------------------


def _rodar_campanha_inteira(cfg: StageConfig, quantos: int = 3) -> list[str]:
    """Rotula os ``quantos`` primeiros lotes com respostas válidas."""
    lp = _lp(cfg)
    feitos = lio.claim(n=quantos, lp=lp)
    for batch_id in feitos:
        uids = lio.uids_do_lote(batch_id, lp)
        _, linhas, _ = lio.validar_resposta(batch_id, _resposta(uids), lp)
        lio.concluir(batch_id, linhas, "haiku", lp=lp)
    return feitos


def _mapas_de_teste(cfg: StageConfig) -> None:
    destino = cfg.labeling_dir / "mappings"
    destino.mkdir(parents=True, exist_ok=True)
    (destino / "no_robots.json").write_text(
        json.dumps(
            {
                "source": "no_robots",
                "axis": "task_type",
                "weight": 0.5,
                "map": {
                    "Open QA": "qa-aberta",
                    "Coding": "codigo",
                    "Summarize": "resumo",
                    "Generation": None,
                },
            }
        ),
        encoding="utf-8",
    )
    (destino / "dolly.json").write_text(
        json.dumps(
            {
                "source": "dolly",
                "axis": "task_type",
                "weight": 0.5,
                "map": {
                    "open_qa": "qa-aberta",
                    "classification": "classificacao-extracao",
                    "summarization": "resumo",
                    "creative_writing": "geracao-criativa",
                },
            }
        ),
        encoding="utf-8",
    )


def test_s08_funde_agente_ouro_e_nativo(campanha: StageConfig) -> None:
    _mapas_de_teste(campanha)
    _importar_ouro(campanha, task_type="resumo", domain="ciencia")
    _rodar_campanha_inteira(campanha)
    assert s08.run(campanha) == 0

    tabela = pq.read_table(campanha.caminho(SEED_LABELS))
    assert tuple(tabela.schema.names) == s08.COLUNAS
    # quality tem de sair int8 nullable: uma volta pelo pandas viraria float64.
    assert tabela.schema.field("quality").type == pa.int8()
    linhas = tabela.to_pylist()
    metodos = {linha["label_method"] for linha in linhas}
    assert metodos == {"manual", "agent", "native"}
    # uid é único: a resolução de conflito deixa uma linha por item.
    assert len({linha["uid"] for linha in linhas}) == len(linhas)

    pesos = {linha["label_method"]: linha["label_weight"] for linha in linhas}
    assert pesos["native"] == pytest.approx(0.5)
    assert pesos["agent"] == pytest.approx(1.0)
    nativos = [linha for linha in linhas if linha["label_method"] == "native"]
    assert all(linha["domain"] is None and linha["quality"] is None for linha in nativos)
    assert all(linha["batch_id"] is None for linha in nativos)


def test_s08_agente_vence_nativo_e_manual_vence_agente(campanha: StageConfig) -> None:
    _mapas_de_teste(campanha)
    _importar_ouro(campanha, task_type="resumo", domain="ciencia")
    _rodar_campanha_inteira(campanha, quantos=10)
    assert s08.run(campanha) == 0
    por_uid = {
        linha["uid"]: linha
        for linha in pq.read_table(campanha.caminho(SEED_LABELS)).to_pylist()
    }
    manifest = lio.carregar_manifest(_lp(campanha))

    # Item de calibração: o humano disse `resumo`, o agente disse `qa-aberta`.
    ouro_uid = manifest["calibration"]["uids"][0]
    assert por_uid[ouro_uid]["label_method"] == "manual"
    assert por_uid[ouro_uid]["task_type"] == "resumo"

    # Item de fonte nativa que o agente também rotulou: vence o agente.
    universo = pq.read_table(campanha.caminho(UNIVERSE), columns=["uid", "source"]).to_pylist()
    nativos = {
        linha["uid"] for linha in universo if linha["source"] in {"no_robots", "dolly"}
    }
    do_agente = [
        u for u, linha in por_uid.items() if u in nativos and linha["label_method"] == "agent"
    ]
    assert do_agente, "nenhum item nativo caiu na semente — teste sem valor"
    assert por_uid[do_agente[0]]["task_type"] == "qa-aberta"


def test_s08_descarta_rotulo_de_agente_sobre_item_de_calibracao(
    campanha: StageConfig,
) -> None:
    _importar_ouro(campanha)
    feitos = _rodar_campanha_inteira(campanha, quantos=2)
    manifest = lio.carregar_manifest(_lp(campanha))
    agentes, stats = s08.coletar_agentes(_lp(campanha), manifest, estrito=False)
    calibracao = set(manifest["calibration"]["uids"])
    assert not {linha["uid"] for linha in agentes} & calibracao
    esperado = sum(len(manifest["batches"][b]["gold_uids"]) for b in feitos)
    assert stats["descartados (item de calibração)"] == esperado


def test_s08_dedup_dentro_da_origem_fica_com_a_primeira() -> None:
    linhas = [
        {"uid": "a", "task_type": "resumo", "label_method": "agent", "batch_id": "b1"},
        {"uid": "a", "task_type": "codigo", "label_method": "agent", "batch_id": "b2"},
    ]
    final, stats = s08.resolver(linhas)  # type: ignore[arg-type]
    assert len(final) == 1 and final[0]["task_type"] == "resumo"
    assert stats["duplicata em agent (fica a 1ª)"] == 1


def test_s08_sem_rotulo_nenhum_falha_com_instrucao(campanha: StageConfig) -> None:
    assert s08.run(campanha) == 1


def test_s08_estrito_reclama_de_lote_done_sem_arquivo(campanha: StageConfig) -> None:
    lp = _lp(campanha)
    _rodar_campanha_inteira(campanha, quantos=1)
    lp.rotulos("batch_0001").unlink()
    estrito = StageConfig(
        data_dir=campanha.data_dir, labeling_dir=campanha.labeling_dir, strict=True
    )
    with pytest.raises(SystemExit, match="não existe"):
        s08.run(estrito)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_labels_status_e_submit(campanha: StageConfig, capsys) -> None:
    from prompt_factory.cli import main

    lab = str(campanha.labeling_dir)
    assert main(["labels", "status", "--labeling-dir", lab]) == 0
    assert "calibração" in capsys.readouterr().out

    assert main(["labels", "next", "--labeling-dir", lab, "--out", str(campanha.data_dir / "l.json")]) == 0
    capsys.readouterr()

    lp = _lp(campanha)
    uids = lio.uids_do_lote("batch_0001", lp)
    resposta = campanha.data_dir / "r.jsonl"
    resposta.write_text(_resposta(uids), encoding="utf-8")
    assert main(["labels", "submit", "--batch", "batch_0001", "--file", str(resposta), "--labeling-dir", lab]) == 0
    assert lio.carregar_manifest(lp)["batches"]["batch_0001"]["status"] == lio.DONE


def test_cli_submit_invalido_imprime_retry_uids(campanha: StageConfig, capsys) -> None:
    from prompt_factory.cli import main

    lab = str(campanha.labeling_dir)
    assert main(["labels", "next", "--labeling-dir", lab, "--out", str(campanha.data_dir / "l.json")]) == 0
    lp = _lp(campanha)
    uids = lio.uids_do_lote("batch_0001", lp)
    resposta = campanha.data_dir / "ruim.jsonl"
    resposta.write_text(_resposta(uids[:-1]), encoding="utf-8")
    assert main(["labels", "submit", "--batch", "batch_0001", "--file", str(resposta), "--labeling-dir", lab]) == 1
    saida = capsys.readouterr()
    assert f"RETRY_UIDS: {uids[-1]}" in saida.out
    # Falha de validação NÃO muda o estado: o lote segue reivindicado.
    assert lio.carregar_manifest(lp)["batches"]["batch_0001"]["status"] == lio.CLAIMED


def test_cli_submit_devolve_lote_com_agreement_baixo(campanha: StageConfig, capsys) -> None:
    from prompt_factory.cli import main

    lab = str(campanha.labeling_dir)
    _importar_ouro(campanha, task_type="resumo", domain="ciencia")
    lp = _lp(campanha)
    assert main(["labels", "next", "--labeling-dir", lab, "--out", str(campanha.data_dir / "l.json")]) == 0
    uids = lio.uids_do_lote("batch_0001", lp)
    resposta = campanha.data_dir / "r.jsonl"
    # Responde tudo `qa-aberta`/`geral`: erra os 6 eixos do ouro.
    resposta.write_text(_resposta(uids), encoding="utf-8")
    assert main(["labels", "submit", "--batch", "batch_0001", "--file", str(resposta), "--labeling-dir", lab]) == 1
    assert "portão de calibração" in capsys.readouterr().err
    assert lio.carregar_manifest(lp)["batches"]["batch_0001"]["status"] == lio.PENDING


def test_cli_run_all_nao_inclui_s07_s08() -> None:
    """`pf run` é a cadeia de dados; s07/s08 têm campanha humana no meio."""
    from prompt_factory.cli import expandir_estagios
    from prompt_factory.stages import CADEIA, STAGES

    assert "s07" in STAGES and "s08" in STAGES
    assert expandir_estagios(["all"], list(STAGES), CADEIA) == list(CADEIA)
    assert expandir_estagios(["s07"], list(STAGES), CADEIA) == ["s07"]
