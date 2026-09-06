"""Dump textual do annotate.sqlite — fora do git — e prova que restaura.

O annotate.sqlite é o único banco NÃO regenerável do projeto: ele guarda
trabalho humano (anotações, revisões, criações), não artefato de pipeline.
O backup dele é um dump SQL em texto porque texto se lê, se compara e se
restaura com o sqlite3 de qualquer máquina.

Ele NÃO é versionado, e já foi. O dump carrega texto derivado do corpus (um
prompt do WildChat trouxe um nome próprio) e os gabaritos escondidos das
anotações sintéticas — exatamente o que o `.gitignore` mantém fora do git para
o resto do projeto. Histórico de git não se apaga, e este repositório é
público: o arquivo saiu do HEAD e da história na Camada 1 (R12). O destino
padrão é `data/backups/`, que o `.gitignore` cobre; a durabilidade vem de
copiar o dump para OUTRO DISCO (nesta máquina, a convenção é
`F:/backups/prompt-factory`), não de um remoto.

O caminho é backup API → :memory: → iterdump, e não iterdump direto na
conexão do arquivo: a Bancada pode estar NO AR durante o dump, e a backup
API do sqlite dá um snapshot consistente mesmo com escritor ativo. O dump
de uma cópia congelada nunca mistura duas gerações do banco.

Todo passe termina restaurando o dump num :memory: e comparando as
contagens tabela a tabela — um backup que nunca foi restaurado é uma
esperança, não um backup.

Uso:  uv run python scripts/backup_bancada.py [--destino CAMINHO.sql]
"""

import argparse
import sqlite3
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
BANCO = RAIZ / "data" / "db" / "annotate.sqlite"
# data/ é gitignorado inteiro (só os .gitkeep passam): o dump nasce fora do git.
DESTINO_PADRAO = RAIZ / "data" / "backups" / "bancada.sql"

# as tabelas cujo conteúdo é trabalho humano — são elas que o restore confere
TABELAS_CONFERIDAS = (
    "anotadores",
    "tarefas",
    "atribuicoes",
    "anotacoes",
    "revisoes",
    "avaliacoes",
    "criacoes",
)


def contar(conn: sqlite3.Connection) -> dict[str, int]:
    contagens = {}
    for t in TABELAS_CONFERIDAS:
        try:
            contagens[t] = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            # tabela de um schema futuro/passado: ausente conta como ausente,
            # não como zero — zero afirmaria uma tabela vazia que não existe
            contagens[t] = None
    return contagens


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--destino", type=Path, default=DESTINO_PADRAO,
                    help=f"arquivo .sql de saída (padrão: {DESTINO_PADRAO.relative_to(RAIZ)})")
    destino: Path = ap.parse_args().destino

    if not BANCO.exists():
        print(f"[backup] {BANCO} nao existe — nada a fazer")
        return 1

    origem = sqlite3.connect(f"file:{BANCO}?mode=ro", uri=True)
    copia = sqlite3.connect(":memory:")
    origem.backup(copia)
    origem.close()

    destino.parent.mkdir(parents=True, exist_ok=True)
    # escrita de dentro do Python, em utf-8: redirecionar no PowerShell
    # gravaria UTF-16/BOM e quebraria o restore (pegadinha do CLAUDE.md)
    with open(destino, "w", encoding="utf-8", newline="\n") as f:
        for linha in copia.iterdump():
            f.write(linha + "\n")

    antes = contar(copia)

    # a prova: o dump recém-escrito reconstrói um banco com as mesmas contagens
    restaurado = sqlite3.connect(":memory:")
    restaurado.executescript(destino.read_text(encoding="utf-8"))
    depois = contar(restaurado)

    if antes != depois:
        print(f"[backup] RESTORE DIVERGIU: {antes} != {depois}")
        return 1

    kb = destino.stat().st_size / 1024
    resumo = " ".join(f"{t}={n}" for t, n in antes.items() if n is not None)
    print(f"[backup] {destino}: {kb:.0f} KB, restore conferido ({resumo})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
