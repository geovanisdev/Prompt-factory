"""Cache em memória das agregações sem filtro (M9-C).

POR QUE EXISTE
==============
``/api/facets`` e ``/api/stats`` contam o corpus inteiro e são as duas rotas que
o navegador dispara antes de desenhar qualquer coisa. Pior: a interface
re-consulta ``/api/facets`` a cada **virada de página** — e virar a página não
muda faceta nenhuma, porque o filtro é o mesmo. Sem cache, o usuário paga a
contagem do corpus para andar de 50 em 50 itens.

O banco é lido muito e escrito pouco: as únicas escritas são as edições do
usuário (``PATCH``, ``revert``) e as coleções. Entre duas escritas, a resposta de
uma agregação **não pode** mudar.

POR QUE UM CACHE ERRADO É PIOR QUE LENTIDÃO
===========================================
Um número velho na barra lateral não parece defeito: parece o corpus. O usuário
rotula 40 itens, a contagem de "precisa revisão" não desce, e ele conclui que a
ferramenta não salvou — ou pior, não conclui nada e exporta em cima de um recorte
que não é o que a tela prometia. Por isso a invalidação aqui é **grosseira de
propósito**: qualquer escrita joga fora o cache inteiro. Recalcular 90 ms é
barato; explicar um número errado, não.

AS DUAS FORMAS DE FICAR VELHO, E A GUARDA DE CADA UMA
=====================================================
1. **Escrita por esta app.** Toda rota que escreve chama ``invalidar()``. É a
   geração interna (``_geracao``), um contador que só cresce.
2. **Troca do banco por swap de arquivo.** ``pf load-db`` constrói o SQLite ao
   lado e faz ``os.replace``. O servidor continua de pé e as conexões seguintes
   já abrem o arquivo NOVO — mas o cache seria do arquivo velho, e o número
   velho é de um corpus que não existe mais. Por isso toda leitura confere o
   ``db_build_id`` do ``app_meta``, que muda a cada carga. Medido: 0,0 ms (é uma
   busca por chave primária numa tabela de 6 linhas), contra os 90 ms que a
   entrada evita.

O QUE ESTA GUARDA **NÃO** COBRE (e é honesto dizer): escrita feita por OUTRO
processo no mesmo arquivo — um ``pf`` de CLI rodando em paralelo, por exemplo.
Nesse caso o ``db_build_id`` não muda e o cache serve número velho até a próxima
escrita pela app. A app já assume ser a única escritora em outro lugar (o
``n_rows`` do ``/api/health`` é lido UMA vez no lifespan e nunca mais), então a
suposição não é nova; ela apenas passa a valer para mais números.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from ..config import get as _cfg

#: Chave do ``app_meta`` que muda a cada ``pf load-db``. Ver a guarda 2 acima.
CHAVE_BUILD = "db_build_id"


def _max_entradas() -> int:
    return int(_cfg("app", "cache_max_entradas", default=64))


class CacheAgregados:
    """LRU minúsculo, com invalidação por geração e por ``db_build_id``.

    Uma instância POR APP (mora em ``app.state.cache``), nunca um singleton de
    módulo: dois ``criar_app`` no mesmo processo — que é exatamente o que os
    testes fazem — compartilhariam contagens de bancos diferentes.
    """

    def __init__(self, max_entradas: int | None = None) -> None:
        self._max = _max_entradas() if max_entradas is None else max_entradas
        self._itens: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
        self._trava = threading.Lock()
        self._geracao = 0
        self._build: str | None = None
        #: Só para teste e diagnóstico — nenhuma decisão depende deles.
        self.acertos = 0
        self.erros = 0

    # -- estado ------------------------------------------------------------

    @property
    def geracao(self) -> int:
        """Contador de escritas. Só cresce; útil para asserção em teste."""
        return self._geracao

    def __len__(self) -> int:
        return len(self._itens)

    def invalidar(self) -> None:
        """Joga o cache inteiro fora. Chamado por TODA rota que escreve."""
        with self._trava:
            self._itens.clear()
            self._geracao += 1

    def _conferir_build(self, conn: sqlite3.Connection) -> None:
        """Descarta tudo se o arquivo do banco foi trocado embaixo da app."""
        linha = conn.execute(
            "SELECT value FROM app_meta WHERE key = ?", (CHAVE_BUILD,)
        ).fetchone()
        atual = None if linha is None else str(linha["value"])
        with self._trava:
            if atual != self._build:
                self._itens.clear()
                self._build = atual

    # -- uso ---------------------------------------------------------------

    def obter(
        self,
        conn: sqlite3.Connection,
        chave: tuple[Any, ...],
        calcular: Callable[[], Any],
    ) -> Any:
        """Devolve o valor de ``chave``, calculando-o se preciso.

        ``calcular()`` roda FORA da trava de propósito: ele faz SQL de centenas
        de milissegundos, e segurar a trava ali serializaria as requisições
        concorrentes — o preço de duas requisições simultâneas calcularem a mesma
        coisa é uma conta repetida, o preço de segurar a trava é a interface
        travada. Recalcular é idempotente; travar não é grátis.

        O valor guardado é devolvido **por referência**. Quem chama serializa e
        não muta — se um dia alguém precisar mutar a resposta, copie ANTES, senão
        a mutação entra no cache e vaza para o próximo request.
        """
        if self._max <= 0:  # cache desligado pelo settings.toml
            return calcular()
        self._conferir_build(conn)
        with self._trava:
            if chave in self._itens:
                self._itens.move_to_end(chave)
                self.acertos += 1
                return self._itens[chave]
            geracao = self._geracao
        self.erros += 1
        valor = calcular()
        with self._trava:
            # Uma escrita que aconteceu DURANTE o cálculo faz o resultado nascer
            # velho. Descartar em silêncio é o certo: a resposta desta requisição
            # sai como está (era válida quando o SELECT rodou), mas ela não pode
            # ficar guardada como se fosse o estado de agora.
            if geracao == self._geracao:
                self._itens[chave] = valor
                self._itens.move_to_end(chave)
                while len(self._itens) > self._max:
                    self._itens.popitem(last=False)
        return valor


__all__ = ["CHAVE_BUILD", "CacheAgregados"]
