"""Caminhos canônicos do repositório.

Tudo é derivado de ``__file__`` — nada depende do diretório de trabalho atual,
porque a CLI e os testes rodam de lugares diferentes.

Layout::

    <ROOT>/
      config/            settings.toml, sources.toml
      data/              (gitignorado, só .gitkeep versionado)
        raw/ interim/ final/ emb/ models/ db/ exports/
      labeling/          taxonomy.json, manifest.json, batches/, mappings/
      src/prompt_factory/
"""

from __future__ import annotations

from pathlib import Path

# src/prompt_factory/paths.py -> src/prompt_factory -> src -> <ROOT>
ROOT: Path = Path(__file__).resolve().parents[2]

CONFIG: Path = ROOT / "config"
SETTINGS_TOML: Path = CONFIG / "settings.toml"
SOURCES_TOML: Path = CONFIG / "sources.toml"

DATA: Path = ROOT / "data"
RAW: Path = DATA / "raw"          # um parquet por fonte, cru-normalizado
INTERIM: Path = DATA / "interim"  # estágios s01..s06
FINAL: Path = DATA / "final"      # universe.parquet e derivados
EMB: Path = DATA / "emb"          # embeddings .npy (float16, mmap)
MODELS: Path = DATA / "models"    # classificadores joblib
DB: Path = DATA / "db"            # prompts.sqlite (+ build/swap)
EXPORTS: Path = DATA / "exports"  # JSONL/CSV + manifests

#: O banco que a interface abre. Nunca é escrito no lugar: o s11 constrói o
#: ``DB_BUILD`` inteiro ao lado e só então faz ``os.replace``, que é atômico.
#: (Os estágios usam ``StageConfig.caminho("db/prompts.sqlite")``, para que
#: ``--data-dir`` redirecione também o banco; estas duas constantes são o
#: caminho absoluto de quem não tem StageConfig — a app e o ``pf db-check``.)
DB_FILE: Path = DB / "prompts.sqlite"
DB_BUILD: Path = DB / "prompts.build.sqlite"

#: Banco da plataforma de anotação ("Bancada", Trilha B). É um arquivo SEPARADO
#: de propósito: o ``prompts.sqlite`` é reconstruído inteiro e trocado por swap
#: a cada ``pf load-db``, e guardar o estado da plataforma lá dentro faria toda
#: recarga do corpus apagar as anotações. Ao contrário do corpus, este banco é
#: criado pela própria app na primeira subida — ela é a dona dele.
ANNOTATE_DB_FILE: Path = DB / "annotate.sqlite"

#: Onde os artefatos de entrega da Bancada (P5b) são gravados. Pasta PRÓPRIA
#: dentro de ``exports/`` porque o produto é outro: ``exports/`` guarda recortes
#: do CORPUS (prompts crus, um manifesto de licença por arquivo) e isto aqui
#: guarda o TRABALHO DE ANOTAÇÃO (pares de SFT, pares de preferência, relatório
#: de QC). Misturados na mesma pasta, um ``export_20260801.jsonl`` não diria qual
#: dos dois produtos é — e eles têm licenças e destinatários diferentes.
ANNOTATE_EXPORTS: Path = EXPORTS / "annotate"

LABELING: Path = ROOT / "labeling"
TAXONOMY_JSON: Path = LABELING / "taxonomy.json"
LABEL_MANIFEST: Path = LABELING / "manifest.json"
BATCHES: Path = LABELING / "batches"      # lotes entregues aos agentes (gitignorado)
LABELS: Path = LABELING / "labels"        # respostas validadas, 1 jsonl por lote
SEED: Path = LABELING / "seed"            # s07: seed.parquet + strata.txt
MAPPINGS: Path = LABELING / "mappings"

#: Campanha de GERAÇÃO da Bancada (P4c). Árvore própria, ao lado de
#: ``labeling/`` e pelo mesmo motivo: o livro-caixa é o que torna a campanha
#: retomável entre sessões, e ele não pode morar em ``data/`` — aquela árvore é
#: regenerável por um ``pf run`` e esta guarda o estado de trabalho que já foi
#: entregue a agentes. Versionado é só o ``manifest.json``; os lotes carregam o
#: texto dos prompts e as respostas carregam o material gerado.
GERACAO: Path = ROOT / "geracao"
GERACAO_MANIFEST: Path = GERACAO / "manifest.json"
GERACAO_LOTES: Path = GERACAO / "lotes"        # o que vai para o agente
GERACAO_RESPOSTAS: Path = GERACAO / "respostas"  # o que voltou, já importado

#: Campanha de DESTILAÇÃO (Central de Briefs Pedagógicos). Terceira árvore de
#: campanha, ao lado de ``labeling/`` e ``geracao/``, e a regra do git aqui é a
#: mais dura das três: os lotes carregam texto de MATERIAL DIDÁTICO DE TERCEIROS,
#: com copyright integral. Versionado é só o ``manifest.json``.
#:
#: O material bruto NÃO mora aqui nem em lugar nenhum do repositório — ele fica
#: fora, e o caminho vem de ``[pedidos] material_dir``.
DESTILACAO: Path = ROOT / "destilacao"
DESTILACAO_MANIFEST: Path = DESTILACAO / "manifest.json"
DESTILACAO_LOTES: Path = DESTILACAO / "lotes"        # o que vai para o agente
DESTILACAO_RESPOSTAS: Path = DESTILACAO / "respostas"  # o que voltou, já importado

SCRIPTS: Path = ROOT / "scripts"
TESTS: Path = ROOT / "tests"

#: Diretórios que ``ensure_dirs()`` garante existirem.
DATA_DIRS: tuple[Path, ...] = (RAW, INTERIM, FINAL, EMB, MODELS, DB, EXPORTS)
LABELING_DIRS: tuple[Path, ...] = (BATCHES, LABELS, SEED, MAPPINGS)


def ensure_dirs(*extra: Path) -> None:
    """Cria (idempotente) toda a árvore de trabalho, mais quaisquer ``extra``."""
    for directory in (*DATA_DIRS, *LABELING_DIRS, *extra):
        directory.mkdir(parents=True, exist_ok=True)


__all__ = [
    "ANNOTATE_DB_FILE",
    "ANNOTATE_EXPORTS",
    "BATCHES",
    "CONFIG",
    "DATA",
    "DATA_DIRS",
    "DB",
    "DB_BUILD",
    "DB_FILE",
    "DESTILACAO",
    "DESTILACAO_LOTES",
    "DESTILACAO_MANIFEST",
    "DESTILACAO_RESPOSTAS",
    "EMB",
    "EXPORTS",
    "FINAL",
    "GERACAO",
    "GERACAO_LOTES",
    "GERACAO_MANIFEST",
    "GERACAO_RESPOSTAS",
    "INTERIM",
    "LABELING",
    "LABELING_DIRS",
    "LABELS",
    "LABEL_MANIFEST",
    "MAPPINGS",
    "MODELS",
    "RAW",
    "ROOT",
    "SCRIPTS",
    "SEED",
    "SETTINGS_TOML",
    "SOURCES_TOML",
    "TAXONOMY_JSON",
    "TESTS",
    "ensure_dirs",
]
