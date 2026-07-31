"""Classificador pt-BR x pt-PT por evidência acumulada (s02).

Não é um modelo estatístico: é um **placar de famílias de pistas**, escolhido de
propósito porque o corpus é de prompts curtos, onde um classificador de idioma
treinado erra muito e não sabe explicar. Aqui cada pista é auditável, cada
família tem teto (``cap``) e a decisão só sai quando a margem é grande.

Sete famílias, cada uma com peso por ocorrência e teto de contribuição:

===  ==========================  ==============================================
S1   progressivo                 BR "estou fazendo" x PT "estou a fazer"
S2   segunda pessoa              PT "fazes/queres/tens" x BR "você"
S3   ênclise                     PT "chama-se", "diga-lhe"
S4   coloquialismos              BR "pra/tá/né" x PT "fixe/bué/malta"
S5   léxico                      ônibus x autocarro, celular x telemóvel, ...
S6   ortografia                  gênero x género, contato x contacto, ...
S7   prior do país               country=Brazil → BR; Portugal/Angola/... → PT
===  ==========================  ==============================================

Decisão: ``m`` = maior placar, ``n`` = menor. Empate ou ``m < [variant]
min_score`` → ``pt-indef`` com confiança 0. Senão ``conf = (m-n)/(m+n)``; abaixo
de ``[variant] min_conf`` também vira ``pt-indef`` (mas devolvendo a margem
medida, que é informação útil no relatório). Texto em inglês não passa por aqui:
o s02 grava ``lang_variant`` nulo.

**Pegadinha do NFC**: ``textnorm.norm_display`` não recompõe caracteres, então um
"á" decomposto (U+0061 U+0301) não casaria com o "á" dos padrões. Todo texto
entra por ``unicodedata.normalize("NFC", t).casefold()`` — casefold e não
``lower()`` porque é independente de locale, e mantém os diacríticos (que são
justamente meia dúzia das pistas).

Exclusões deliberadas (falsos amigos que já foram testados e reprovados):

* **tu** sozinho não pontua — no Sul do Brasil "tu foi" é corriqueiro; só a
  conjugação de 2ª pessoa (``fazes``, ``tens``) conta como PT.
* **mouse/rato**, **grama**, **fato**, **legal**, **pedestre/peão**: as duas
  variantes usam as duas palavras com sentidos diferentes.
* **giro** ficou de fora do S4 (é o verbo "girar" em 1ª pessoa).
* **celular** e **arquivo** ficam, com falso positivo conhecido em texto de
  biologia e de arquivística; o teto da família S5 limita o estrago.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import NamedTuple

from . import config
from .schema import Variant

#: Nome curto de cada família, na ordem em que aparece no placar detalhado.
FAMILIAS_NOMES: tuple[str, ...] = ("S1", "S2", "S3", "S4", "S5", "S6", "S7")


class _Familia(NamedTuple):
    """Uma família de pistas, com padrões e pesos independentes por lado."""

    nome: str
    br: tuple[re.Pattern[str], ...]
    pt: tuple[re.Pattern[str], ...]
    peso_br: float
    cap_br: float
    peso_pt: float
    cap_pt: float


def _c(*padroes: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p) for p in padroes)


def _fam(
    nome: str,
    br: tuple[re.Pattern[str], ...],
    pt: tuple[re.Pattern[str], ...],
    peso: float,
    cap: float,
) -> _Familia:
    """Família simétrica: mesmo peso e mesmo teto dos dois lados."""
    return _Familia(nome, br, pt, peso, cap, peso, cap)


# --- S1: perífrase progressiva ---------------------------------------------
# "estou fazendo" (BR) x "estou a fazer" (PT). O `(?!partir\s+de)` evita a
# locução "a partir de", que não é progressiva.
_S1 = _fam(
    "S1",
    _c(r"\b(?:estou|está|estamos|estão|estava|estavam|tô|to)\s+\w+[aei]ndo\b"),
    _c(
        r"\b(?:estou|estás|está|estamos|estão)\s+a\s+(?!partir\s+de\b)"
        r"(?:\w{2,}(?:ar|er|ir)|ler|ver|vir|rir|pôr|ser|ter|dar|ir)\b"
    ),
    1.5,
    3.0,
)

# --- S2: segunda pessoa -----------------------------------------------------
# A conjugação de 2ª pessoa do singular é o sinal PT mais forte que existe em
# prompt curto. "tu" NÃO entra: no Sul do Brasil usa-se "tu" com 3ª pessoa.
# "você" é sinal BR fraco de propósito (aparece em PT formal e em traduções).
_S2 = _Familia(
    "S2",
    _c(r"\bvocês?\b"),
    _c(
        r"\b(?:fazes|queres|podes|sabes|tens|és|vais|estás|achas|dizes|gostas|precisas"
        r"|consegues|deves|ficas|falas|pensas|escreves|lês|vês|dás|pões|vives|sentes"
        r"|entendes|conheces|ajudas|usas|trabalhas|moras|percebes)\b"
    ),
    0.5,
    1.0,
    2.0,
    4.0,
)

# --- S3: ênclise ------------------------------------------------------------
# "chama-se", "diga-lhe", "dá-me". As duas guardas do padrão são obrigatórias:
#   (?<![\w-])  impede casar o miolo de "bem-te-vi";
#   (?!-)       mata "dia-a-dia" (o segundo hífen denuncia).
# "segunda-feira" já não casa porque "feira" não é pronome. Sobra o falso
# positivo do BR formal ("vende-se", "diga-me"), contido pelo teto.
_S3 = _fam(
    "S3",
    (),
    _c(
        r"(?<![\w-])\w{2,}(?:ar|er|ir|e|a|ou|ei)-"
        r"(?:me|te|se|lhe|lhes|nos|vos|o|a|os|as|lho|lha)\b(?!-)"
    ),
    1.5,
    3.0,
)

# --- S4: coloquialismos -----------------------------------------------------
# "pá" só conta colado em pontuação (", pá" / "pá!"), senão casaria com o
# prefixo de qualquer palavra cortada.
_S4 = _fam(
    "S4",
    _c(
        r"\b(?:pra|pras|pro|pros|tá|tô|né|cadê|valeu|beleza)\b",
        r"\ba gente\b",
    ),
    _c(
        r"\b(?:fixe|bué|malta)\b",
        r"\bmiúd[oa]s?\b",
        r"\bestás a ver\b",
        r",\s*pá\b",
        r"\bpá\s*[!.?…]",
    ),
    1.0,
    2.0,
)

# --- S5: léxico -------------------------------------------------------------
# Pares clássicos. "gelado" e "time" exigem determinante porque também são
# adjetivo ("chá gelado") e substantivo/verbo em inglês.
_S5 = _fam(
    "S5",
    _c(
        r"\bônibus\b",
        r"\btrem\b",
        # "celular(es)", não "celulares?": `es?` casaria "celulare" e perderia
        # o singular, que é a forma que aparece em 99% dos prompts.
        r"\bcelular(?:es)?\b",
        r"\bbanheiros?\b",
        r"\bgeladeiras?\b",
        r"\bsorvetes?\b",
        r"\bcafé da manhã\b",
        r"\busuári[oa]s?\b",
        r"\barquivos?\b",
        r"\bsenhas?\b",
        r"\bsucos?\b",
        r"\b(?:o|os|um|uns|meu|meus|seu|seus|nosso|nossos|do|dos|no|nos|esse|este|aquele)"
        r"\s+times?\b",
        r"\bequipes?\b",
        r"\btelas?\b",
        r"\besportes?\b",
        r"\bxícaras?\b",
        r"\baçougues?\b",
        r"\bcaminh(?:ão|ões)\b",
        r"\bfaixa de pedestres\b",
        r"\bternos?\b",
    ),
    _c(
        r"\bautocarros?\b",
        r"\bcomboios?\b",
        r"\btelemó(?:vel|veis)\b",
        r"\bcasa de banho\b",
        r"\bfrigoríficos?\b",
        r"\b(?:um|uns|dois|o|os|meu|meus|seu|seus)\s+gelados?\b",
        r"\bpequeno-almoços?\b",
        r"\butilizador(?:es|a|as)?\b",
        r"\bficheiros?\b",
        r"\bpalavras?-passe\b",
        r"\bsumos?\b",
        r"\bequipas?\b",
        r"\becrãs?\b",
        r"\bdesportos?\b",
        r"\bchávenas?\b",
        r"\btalhos?\b",
        r"\bcami(?:ão|ões)\b",
        r"\bpassadeiras?\b",
        r"\brelva\b",
    ),
    2.0,
    6.0,
)

# --- S6: ortografia ---------------------------------------------------------
# Circunflexo x agudo antes de nasal, e o "c" mudo do Acordo de 1990. Repare que
# a direção do "c" se INVERTE entre as duas últimas linhas: o BR perdeu o c de
# "contacto" mas manteve o de "aspecto"; Portugal fez o contrário.
_S6 = _fam(
    "S6",
    _c(
        r"\bgêner\w*",
        r"\beconôm\w*",
        r"\bfenômen\w*",
        r"\bbônus\b",
        r"\bantôni\w*",
        r"\banônim\w*",
        r"\bgême\w*",
        r"\bcômod\w*",
        r"\btênis\b",
        r"\bregistr\w*",
        r"\bcontatos?\b",
        r"\bcontatar\w*",
        r"\bde fato\b",
        r"\brecep\w*",
        r"\baspectos?\b",
    ),
    _c(
        r"\bgéner\w*",
        r"\beconóm\w*",
        r"\bfenómen\w*",
        r"\bbónus\b",
        r"\bantóni\w*",
        r"\banónim\w*",
        r"\bgéme\w*",
        r"\bcómod\w*",
        r"\bténis\b",
        r"\bregist(?!r)\w+",
        r"\bcontactos?\b",
        r"\bcontactar\w*",
        r"\bde facto\b",
        r"\bfactos?\b",
        r"\brece(?:ção|cionista)\w*",
        r"\baspetos?\b",
    ),
    2.0,
    6.0,
)

#: As seis famílias textuais (o prior de país, S7, não é regex).
FAMILIAS: tuple[_Familia, ...] = (_S1, _S2, _S3, _S4, _S5, _S6)

#: ``country`` chega da fonte por extenso em inglês (WildChat: "Brazil",
#: "Portugal", "Angola"), mas aceitamos também ISO-2/ISO-3 e o nome em
#: português, porque outras fontes podem preencher diferente.
_PAIS_BR: frozenset[str] = frozenset({"BR", "BRA", "BRAZIL", "BRASIL"})
_PAIS_PT: frozenset[str] = frozenset({"PT", "PRT", "PORTUGAL"})
_PAIS_PALOP: frozenset[str] = frozenset(
    {
        "AO",
        "AGO",
        "ANGOLA",
        "MZ",
        "MOZ",
        "MOZAMBIQUE",
        "MOCAMBIQUE",
        "CV",
        "CPV",
        "CABO VERDE",
        "CAPE VERDE",
        "GW",
        "GNB",
        "GUINEA-BISSAU",
        "GUINE-BISSAU",
        "ST",
        "STP",
        "SAO TOME AND PRINCIPE",
        "TL",
        "TLS",
        "TIMOR-LESTE",
        "EAST TIMOR",
    }
)


def _chave_pais(country: str | None) -> str:
    """Normaliza o país para comparação: sem acento, sem espaço sobrando, maiúsculo."""
    if not country:
        return ""
    t = unicodedata.normalize("NFD", str(country).strip())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return " ".join(t.upper().split())


def prior_pais(country: str | None) -> tuple[float, float]:
    """Contribuição ``(br, pt)`` da família S7."""
    chave = _chave_pais(country)
    if not chave:
        return (0.0, 0.0)
    if chave in _PAIS_BR:
        return (2.0, 0.0)
    if chave in _PAIS_PT:
        return (0.0, 2.0)
    if chave in _PAIS_PALOP:
        return (0.0, 1.0)
    return (0.0, 0.0)


@lru_cache(maxsize=1)
def _limiares() -> tuple[float, float]:
    return (
        float(config.get("variant", "min_score", default=2.0)),
        float(config.get("variant", "min_conf", default=0.25)),
    )


def preparar(text: str) -> str:
    """NFC + casefold — o pré-processamento que todos os padrões pressupõem."""
    if not text:
        return ""
    return unicodedata.normalize("NFC", text).casefold()


def placar(text: str, country: str | None = None) -> dict[str, tuple[float, float]]:
    """Placar detalhado ``{familia: (br, pt)}`` — usado por testes e diagnóstico."""
    t = preparar(text)
    detalhe: dict[str, tuple[float, float]] = {}
    for fam in FAMILIAS:
        n_br = sum(len(p.findall(t)) for p in fam.br)
        n_pt = sum(len(p.findall(t)) for p in fam.pt)
        detalhe[fam.nome] = (
            min(n_br * fam.peso_br, fam.cap_br),
            min(n_pt * fam.peso_pt, fam.cap_pt),
        )
    detalhe["S7"] = prior_pais(country)
    return detalhe


def classify_variant(text: str, country: str | None = None) -> tuple[str, float]:
    """``("pt-BR"|"pt-PT"|"pt-indef", confiança)`` do texto em português.

    A confiança é a margem relativa entre os dois placares, com 4 casas. Empate
    ou placar vencedor abaixo de ``[variant] min_score`` devolve confiança 0.
    """
    min_score, min_conf = _limiares()
    detalhe = placar(text, country)
    br = sum(v[0] for v in detalhe.values())
    pt = sum(v[1] for v in detalhe.values())

    m, n = (br, pt) if br >= pt else (pt, br)
    if br == pt or m < min_score:
        return (Variant.PT_INDEF.value, 0.0)

    conf = round((m - n) / (m + n), 4)
    if conf < min_conf:
        return (Variant.PT_INDEF.value, conf)
    return (Variant.PT_BR.value if br > pt else Variant.PT_PT.value, conf)


__all__ = [
    "FAMILIAS",
    "FAMILIAS_NOMES",
    "classify_variant",
    "placar",
    "preparar",
    "prior_pais",
]
