"""Helper de lookup de escolas para autocomplete no dashboard.

Fornece listas cacheadas de UFs, cidades e escolas para alimentar
selectboxes com busca nativa do Streamlit (>=1.32). Duas fontes:

- ``school_censo_yearly`` (paginas analytics — ~215k escolas Brasil)
- ``companies`` (paginas CRM — ~88 escolas importadas)

Todas as funcoes usam ``@st.cache_data(ttl=300)`` (5 min) e lazy import
de ``db`` para evitar circular import no boot do Streamlit.

Usage tipico::

    from dashboard.helpers.school_lookup import (
        get_ufs, get_cities, get_schools,
        format_school_option, parse_inep_from_option,
    )

    uf = st.selectbox("UF:", [""] + get_ufs())
    cities = get_cities(uf) if uf else []
    city = st.selectbox("Municipio:", [""] + cities)
    schools = get_schools(uf, city) if uf and city else []
    options = [""] + [format_school_option(n, i) for n, i in schools]
    sel = st.selectbox("Escola:", options)
    inep = parse_inep_from_option(sel)
"""
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import streamlit as st

# Garantir que o ROOT do projeto esteja no path pra importar database.*
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# PostgREST page cap (Supabase hosted silently clamps .limit())
_PAGE_SIZE = 1000


# ===========================================================================
# FUNCOES CACHEADAS — fonte: school_censo_yearly (Brasil todo)
# ===========================================================================

@st.cache_data(ttl=300, show_spinner=False)
def get_ufs() -> List[str]:
    """As 27 UFs do Brasil.

    Isto ja foi um DISTINCT paginado sobre school_censo_yearly: ~185 requisicoes
    lendo o Brasil inteiro (1,28M linhas / 546 MB) pra descobrir 27 siglas — e
    de novo a cada 5 minutos, quando o cache expirava. Media medida:
    972 blocos de disco por chamada, 2.204 chamadas = ~16 GB de I/O.

    A lista de UFs e uma constante da geografia brasileira, nao um dado do
    censo. Conferido em 05/09/2026: o DISTINCT no censo 2024 devolvia
    exatamente estas 27 — a troca nao muda uma linha da tela.
    """
    from dashboard._mec_source import UF_LIST
    return sorted(UF_LIST)


@st.cache_data(ttl=300, show_spinner=False)
def get_cities(uf: str) -> List[str]:
    """Lista de cidades para uma UF (escola_censo_yearly vintage 2024)."""
    from database.supabase_client import db
    if not uf:
        return []
    try:
        all_cities: set = set()
        offset = 0
        while True:
            r = (
                db.client.table("school_censo_yearly")
                .select("city")
                .eq("vintage_censo", 2024)
                .eq("state", uf.upper().strip())
                .not_.is_("city", "null")
                .range(offset, offset + _PAGE_SIZE - 1)
                .execute()
            )
            batch = r.data or []
            if not batch:
                break
            for row in batch:
                c = row.get("city")
                if c:
                    all_cities.add(c)
            if len(batch) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE
        return sorted(all_cities)
    except Exception:
        return []


@st.cache_data(ttl=300, show_spinner=False)
def get_schools(uf: str, city: str) -> List[Tuple[str, str]]:
    """Lista de (nome, inep_code) para UF + cidade.

    Usa o vintage mais recente disponivel por INEP (ORDER BY vintage DESC),
    deduplicando pela primeira ocorrencia (= nome mais recente).
    """
    from database.supabase_client import db
    if not uf or not city:
        return []
    try:
        all_rows = []
        offset = 0
        while True:
            r = (
                db.client.table("school_censo_yearly")
                .select("inep_code,name,vintage_censo")
                .eq("state", uf.upper().strip())
                .eq("city", city)
                .not_.is_("name", "null")
                .order("vintage_censo", desc=True)
                .range(offset, offset + _PAGE_SIZE - 1)
                .execute()
            )
            batch = r.data or []
            if not batch:
                break
            all_rows.extend(batch)
            if len(batch) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE

        # Deduplicar: primeira ocorrencia por INEP = vintage mais recente
        seen: set = set()
        schools: List[Tuple[str, str]] = []
        for row in all_rows:
            inep = str(row.get("inep_code", "")).strip()
            if inep and inep not in seen:
                seen.add(inep)
                schools.append((row.get("name", "?"), inep))
        return sorted(schools, key=lambda x: x[0])
    except Exception:
        return []


# ===========================================================================
# FUNCOES CACHEADAS — fonte: companies (CRM, ~88 escolas)
# ===========================================================================

@st.cache_data(ttl=300, show_spinner=False)
def get_crm_schools() -> List[Tuple[str, str]]:
    """Lista de (nome, inep_code) das companies no CRM."""
    from database.supabase_client import db
    try:
        r = (
            db.client.table("companies")
            .select("name,inep_code")
            .not_.is_("name", "null")
            .order("name")
            .execute()
        )
        return [
            (row["name"], str(row.get("inep_code", "")))
            for row in (r.data or [])
            if row.get("name")
        ]
    except Exception:
        return []


def invalidate_crm_schools() -> None:
    """Invalida o cache de escolas do CRM. Chamar APOS escrever em `companies`.

    get_crm_schools tem TTL 300 e alimenta os seletores "Buscar escola" das
    paginas Escolas e Contatos. Como nada limpava esse cache, a escola recem
    importada nao aparecia no buscador por ate 5 minutos, e a recem excluida
    continuava listada — parecia que a acao nao tinha funcionado.
    """
    try:
        get_crm_schools.clear()
    except Exception:
        pass


# ===========================================================================
# UTILIDADES DE FORMATACAO
# ===========================================================================

def format_school_option(name: str, inep: str) -> str:
    """Formata escola para exibicao no selectbox: 'NOME (INEP: 43012345)'."""
    if inep:
        return f"{name} (INEP: {inep})"
    return name


def parse_inep_from_option(option: str) -> Optional[str]:
    """Extrai INEP de uma opcao no formato 'NOME (INEP: 43012345)'.

    Retorna None se a opcao for vazia ou nao contiver INEP.
    """
    if not option:
        return None
    m = re.search(r"INEP:\s*(\d+)", option)
    return m.group(1) if m else None
