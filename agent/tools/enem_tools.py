"""
IAlex ENEM analytics tools.

Provides 4 tools that query school_analytics (vintage 2025):
  1. analisar_performance_escola     — snapshot of ONE school
  2. priorizar_leads_enem            — P1/P2/P3 ranking
  3. buscar_escolas_por_enem         — filtered search
  4. analisar_dados_analytics        — free-form query builder

Ethical guardrails (defense in depth):
  - Whitelist: only ALLOWED_ANALYTICS_METRICS and ALLOWED_GROUPINGS accepted
  - Blocklist: PNT_BLOCKED never exposed to LLM (even if asked)
  - Amostra confiavel gate: individual metrics stripped when FALSE
  - Socio rotulo: contexto_municipal always labeled "Perfil do municipio"
  - P3 warning: aviso_fernando returned with defensive-urgency leads

Import pattern (brain.py):
    try:
        from agent.tools.enem_tools import ENEM_TOOLS, ENEM_TOOL_HANDLERS
    except Exception:
        ENEM_TOOLS = []
        ENEM_TOOL_HANDLERS = {}
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from database.supabase_client import db
from utils.logger import logger


# Ano do ENEM "atual" (snapshot mais recente em school_analytics). FONTE UNICA
# usada no system prompt, descricoes de tools e rotulos de saida. Ao importar um
# ENEM novo (runbook ANNUAL_UPDATE.md), bump aqui. Nas saidas POR-escola prefira
# row.get("enem_ano") (ja vem no dado) e use isto so como fallback.
ENEM_VINTAGE = 2025


# ===========================================================================
# WHITELISTS (immutable module-level constants)
# ===========================================================================

# Metricas numericas que o LLM pode pedir. Qualquer outra -> erro amigavel.
ALLOWED_ANALYTICS_METRICS = frozenset({
    # --- enem_* individual (SUJEITO a gate amostra_confiavel) ---
    "enem_media_geral",
    "enem_media_geral_sem_redacao",
    "enem_media_cn", "enem_media_ch", "enem_media_lc", "enem_media_mt",
    "enem_media_redacao",
    "enem_mediana_cn", "enem_mediana_ch", "enem_mediana_lc", "enem_mediana_mt",
    "enem_mediana_redacao", "enem_mediana_geral",
    "enem_std_cn", "enem_std_ch", "enem_std_lc", "enem_std_mt",
    "enem_std_redacao", "enem_std_geral",
    "enem_p25_cn", "enem_p25_ch", "enem_p25_lc", "enem_p25_mt",
    "enem_p25_redacao", "enem_p25_geral",
    "enem_p75_cn", "enem_p75_ch", "enem_p75_lc", "enem_p75_mt",
    "enem_p75_redacao", "enem_p75_geral",
    "enem_p90_cn", "enem_p90_ch", "enem_p90_lc", "enem_p90_mt",
    "enem_p90_redacao", "enem_p90_geral",
    "enem_max_cn", "enem_max_ch", "enem_max_lc", "enem_max_mt",
    "enem_max_redacao", "enem_max_geral",
    "enem_min_cn", "enem_min_ch", "enem_min_lc", "enem_min_mt",
    "enem_min_redacao", "enem_min_geral",
    "enem_pct_acima_500", "enem_pct_acima_600", "enem_pct_acima_700",
    "enem_redacao_pct_ok", "enem_redacao_pct_anulada",
    "enem_redacao_pct_copia", "enem_redacao_pct_em_branco",
    "enem_redacao_pct_fuga_tema", "enem_redacao_pct_fora_padrao",
    "enem_redacao_pct_insuficiente", "enem_redacao_pct_desconectada",
    "enem_redacao_comp1_media", "enem_redacao_comp2_media",
    "enem_redacao_comp3_media", "enem_redacao_comp4_media",
    "enem_redacao_comp5_media",
    "enem_redacao_pct_problemas",
    "enem_pct_ingles", "enem_pct_espanhol",
    "enem_taxa_presenca",
    "enem_percentil_uf_dep", "enem_percentil_uf", "enem_percentil_br",
    "enem_rank_uf_dep", "enem_rank_uf", "enem_rank_br", "enem_rank_mun",
    "enem_quartil_br",
    "enem_gap_vs_peer_2024", "enem_gap_vs_peer_2025",
    "enem_rank_nacional", "enem_rank_uf", "enem_rank_dependencia",
    "enem_percentil_nacional", "enem_classificacao",
    "enem_presentes", "enem_inscritos",
    "enem_presentes_cn", "enem_presentes_ch",
    "enem_presentes_lc", "enem_presentes_mt",
    # --- peer_* (grupo, SEM gate amostra_confiavel) ---
    "peer_media_geral_2020", "peer_media_geral_2021", "peer_media_geral_2022",
    "peer_media_geral_2023", "peer_media_geral_2024", "peer_media_geral_2025",
    "peer_media_cn_2020", "peer_media_cn_2021", "peer_media_cn_2022",
    "peer_media_cn_2023", "peer_media_cn_2024", "peer_media_cn_2025",
    "peer_media_ch_2020", "peer_media_ch_2021", "peer_media_ch_2022",
    "peer_media_ch_2023", "peer_media_ch_2024", "peer_media_ch_2025",
    "peer_media_lc_2020", "peer_media_lc_2021", "peer_media_lc_2022",
    "peer_media_lc_2023", "peer_media_lc_2024", "peer_media_lc_2025",
    "peer_media_mt_2020", "peer_media_mt_2021", "peer_media_mt_2022",
    "peer_media_mt_2023", "peer_media_mt_2024", "peer_media_mt_2025",
    "peer_media_redacao_2020", "peer_media_redacao_2021",
    "peer_media_redacao_2022", "peer_media_redacao_2023",
    "peer_media_redacao_2024", "peer_media_redacao_2025",
    "peer_presentes_2020", "peer_presentes_2021", "peer_presentes_2022",
    "peer_presentes_2023", "peer_presentes_2024", "peer_presentes_2025",
    "peer_pct_acima_500_2020", "peer_pct_acima_500_2021",
    "peer_pct_acima_500_2022", "peer_pct_acima_500_2023",
    "peer_pct_acima_500_2024", "peer_pct_acima_500_2025",
    "peer_pct_acima_600_2020", "peer_pct_acima_600_2021",
    "peer_pct_acima_600_2022", "peer_pct_acima_600_2023",
    "peer_pct_acima_600_2024", "peer_pct_acima_600_2025",
    "peer_pct_acima_700_2020", "peer_pct_acima_700_2021",
    "peer_pct_acima_700_2022", "peer_pct_acima_700_2023",
    "peer_pct_acima_700_2024", "peer_pct_acima_700_2025",
    "peer_redacao_pct_problemas_2020", "peer_redacao_pct_problemas_2021",
    "peer_redacao_pct_problemas_2022", "peer_redacao_pct_problemas_2023",
    "peer_redacao_pct_problemas_2024", "peer_redacao_pct_problemas_2025",
    "peer_delta_media_geral_2020_2024", "peer_delta_media_geral_2020_2025",
    "peer_delta_media_geral_2022_2024", "peer_delta_media_geral_2022_2025",
    "peer_slope_media_geral_ppa", "peer_slope_media_geral_6y",
    "peer_delta_presentes_2020_2024", "peer_delta_presentes_2020_2025",
    "peer_pct_evolucao_alunos_2020_2024", "peer_pct_evolucao_alunos_2020_2025",
    "peer_trajetoria_6y",
    # --- socio_* (SEMPRE rotulado como "perfil do municipio") ---
    "socio_renda_idx_media_2020", "socio_renda_idx_media_2021",
    "socio_renda_idx_media_2022", "socio_renda_idx_media_2023",
    "socio_renda_idx_media_2024", "socio_renda_idx_media_2025",
    "socio_pct_pais_superior_2020", "socio_pct_pais_superior_2021",
    "socio_pct_pais_superior_2022", "socio_pct_pais_superior_2023",
    "socio_pct_pais_superior_2024", "socio_pct_pais_superior_2025",
    "socio_pct_renda_ate_1sm_2020", "socio_pct_renda_ate_1sm_2021",
    "socio_pct_renda_ate_1sm_2022", "socio_pct_renda_ate_1sm_2023",
    "socio_pct_renda_ate_1sm_2024", "socio_pct_renda_ate_1sm_2025",
    "socio_pct_renda_acima_7sm_2020", "socio_pct_renda_acima_7sm_2021",
    "socio_pct_renda_acima_7sm_2022", "socio_pct_renda_acima_7sm_2023",
    "socio_pct_renda_acima_7sm_2024", "socio_pct_renda_acima_7sm_2025",
    "socio_total_inscritos_2020", "socio_total_inscritos_2021",
    "socio_total_inscritos_2022", "socio_total_inscritos_2023",
    "socio_total_inscritos_2024", "socio_total_inscritos_2025",
    "socio_delta_renda_2020_2024", "socio_delta_renda_2020_2025",
    "socio_delta_pais_superior_2020_2024", "socio_delta_pais_superior_2020_2025",
    "socio_pct_evolucao_volume_2020_2024", "socio_pct_evolucao_volume_2020_2025",
    # --- pnt_* SAFE (13 campos, sempre rotulados) ---
    "pnt_total_inscritos", "pnt_pct_treineiros",
    "pnt_pct_ja_concluiu", "pnt_pct_concluindo", "pnt_pct_ate_18_anos",
    "pnt_renda_idx_media",
    "pnt_pct_renda_ate_1sm", "pnt_pct_renda_ate_3sm", "pnt_pct_renda_acima_7sm",
    "pnt_escol_pais_media", "pnt_pct_pais_superior",
    "pnt_pct_pais_ate_fund1", "pnt_ocup_pais_media",
    "pnt_pct_so_publica", "pnt_pct_so_privada",
    # --- companies (subset util para cross-analysis) ---
    "total_matriculas", "matriculas_fund_af", "matriculas_medio",
    "total_docentes", "total_turmas", "qt_coordenadores",
    "alunos_por_docente", "qualification_score",
})

# Campos que o LLM NUNCA pode pedir (regra #10 do system prompt).
# Importados no schema para auditoria interna, mas invisiveis ao agente.
PNT_BLOCKED = frozenset({
    "pnt_pct_feminino",
    "pnt_pct_branca", "pnt_pct_preta", "pnt_pct_parda",
    "pnt_pct_amarela", "pnt_pct_indigena",
    "pnt_pct_com_empregada", "pnt_pct_sem_banheiro",
    "pnt_pct_com_internet", "pnt_pct_com_computador",
})

# Campos SOCIO_* sempre rotulados com "Perfil do municipio" no retorno.
SOCIO_METRICS = frozenset({m for m in ALLOWED_ANALYTICS_METRICS if m.startswith("socio_")})

# Campos PNT_* safe sempre rotulados com "Perfil dos inscritos".
PNT_SAFE = frozenset({m for m in ALLOWED_ANALYTICS_METRICS if m.startswith("pnt_")})

# Campos ENEM_* individuais sujeitos ao gate amostra_confiavel.
# Se enem_amostra_confiavel=False, estes sao removidos do payload.
AMOSTRA_CONFIAVEL_GATED = frozenset({
    "enem_media_geral", "enem_media_geral_sem_redacao",
    "enem_media_cn", "enem_media_ch", "enem_media_lc", "enem_media_mt",
    "enem_media_redacao",
    "enem_mediana_cn", "enem_mediana_ch", "enem_mediana_lc", "enem_mediana_mt",
    "enem_mediana_redacao", "enem_mediana_geral",
    "enem_std_cn", "enem_std_ch", "enem_std_lc", "enem_std_mt",
    "enem_std_redacao", "enem_std_geral",
    "enem_p25_cn", "enem_p25_ch", "enem_p25_lc", "enem_p25_mt",
    "enem_p25_redacao", "enem_p25_geral",
    "enem_p75_cn", "enem_p75_ch", "enem_p75_lc", "enem_p75_mt",
    "enem_p75_redacao", "enem_p75_geral",
    "enem_p90_cn", "enem_p90_ch", "enem_p90_lc", "enem_p90_mt",
    "enem_p90_redacao", "enem_p90_geral",
    "enem_max_cn", "enem_max_ch", "enem_max_lc", "enem_max_mt",
    "enem_max_redacao", "enem_max_geral",
    "enem_min_cn", "enem_min_ch", "enem_min_lc", "enem_min_mt",
    "enem_min_redacao", "enem_min_geral",
    "enem_pct_acima_500", "enem_pct_acima_600", "enem_pct_acima_700",
    "enem_redacao_comp1_media", "enem_redacao_comp2_media",
    "enem_redacao_comp3_media", "enem_redacao_comp4_media",
    "enem_redacao_comp5_media",
    "enem_percentil_uf_dep", "enem_percentil_uf", "enem_percentil_br",
    "enem_rank_uf_dep", "enem_rank_uf", "enem_rank_br", "enem_rank_mun",
    "enem_quartil_br", "enem_gap_vs_peer_2024", "enem_gap_vs_peer_2025",
    "enem_rank_nacional", "enem_rank_uf", "enem_rank_dependencia",
    "enem_percentil_nacional", "enem_classificacao",
})

# Campos que o LLM pode usar em GROUP BY / distribuicao.
ALLOWED_GROUPINGS = frozenset({
    "city", "state", "admin_dependency", "admin_category", "school_size",
    "nivel_tecnologico", "categoria_privada",
    "enem_dependencia", "enem_potencial_melhoria", "enem_area_mais_fraca",
    "peer_trajetoria_5y", "peer_trajetoria_6y",
})

ALLOWED_OPERATIONS = frozenset({
    "valor_unico", "ranking", "comparacao", "serie_temporal", "distribuicao",
})

ALLOWED_AGGREGATIONS = frozenset({
    "media", "mediana", "soma", "min", "max", "count", "p25", "p75", "p90",
})

ALLOWED_COMPARACAO_COM = frozenset({
    "municipio", "estado", "brasil",
    "mesma_dependencia", "mesmo_porte", "mesmo_nivel_tecnologico",
})

# Cenario A (detectado na inspecao do CSV real). Valores aceitos em modo_redacao.
ALLOWED_MODO_REDACAO = frozenset({"com", "sem", "ambos"})

# Cap duro: nunca traga mais que isso do banco em uma unica query.
# 25k cobre 100% das 23.051 escolas com amostra_confiavel=True no Brasil.
# A paginacao automatica em _fetch_filtered faz 25 requests de 1000 cada
# (~2.5s no pior caso — aceitavel para explorador livre e comparacoes amplas).
MAX_ROWS_FETCH = 25000

# Threshold de confiabilidade pratico para ranking/ordenacao.
# Distinto de amostra_confiavel — este e' o gate de "tem massa critica pra ordenar".
MIN_PRESENTES_RANKING = 30
MIN_PRESENTES_GAP = 20

# P3 defensivo: usa delta 2022-2024 (regra #7) com threshold estrito.
P3_DELTA_THRESHOLD = -15.0

# Unicode replacement char — chega do CSV fonte ja corrompido (mojibake).
# Migration 018 limpou os 10.5k rows afetados em school_analytics, mas
# manter o strip em runtime serve de safety net pra:
#   - re-imports do mesmo CSV
#   - outros campos de texto que escapem da migration
#   - edge cases em escolas que nao existem em school_censo_yearly
_FFFD = "\ufffd"


# ===========================================================================
# HELPERS PRIVADOS DE FORMATACAO (defense in depth, camada 2)
# ===========================================================================

def _clean_text(s: Any) -> Any:
    """Remove Unicode replacement chars (U+FFFD) de uma string.

    Passa por valores None ou nao-string sem tocar. Estrategia: strip
    direto do U+FFFD (o caractere em si, nao os bytes), porque o replace
    ja aconteceu upstream e nao conseguimos recuperar o byte original.

    Preferencialmente o caller deve resolver nomes via
    _resolve_school_names (cascata companies -> school_censo_yearly, que
    retornam texto 100% limpo) em vez de depender deste helper. Esse e
    a ultima linha de defesa.
    """
    if s is None or not isinstance(s, str):
        return s
    if _FFFD not in s:
        return s
    # Normaliza dupla-replacement (" " -> " ") pra evitar whitespace duplo
    return " ".join(s.replace(_FFFD, "").split())

def _strip_gated_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    """Remove campos individuais quando amostra_confiavel != True.

    Camada 1 do defense in depth. Chamado antes de qualquer helper que
    formata dados individuais. Se o caller tenta acessar um campo gated
    depois desta chamada, ele nao vai encontrar (safe by construction).
    """
    if not row:
        return {}
    if row.get("enem_amostra_confiavel") is True:
        return dict(row)
    stripped = {k: v for k, v in row.items() if k not in AMOSTRA_CONFIAVEL_GATED}
    return stripped


def _strip_blocked_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    """Remove campos PNT_BLOCKED do payload antes de devolver ao LLM."""
    if not row:
        return {}
    return {k: v for k, v in row.items() if k not in PNT_BLOCKED}


# enem_dependencia tem 4 valores: Federal, Estadual, Municipal, Privada.
# "PUBLICA" e um GRUPO (Federal+Estadual+Municipal), NAO um valor — por isso
# "melhor escola publica" precisa incluir as Federais (ex: UFSM Politecnico),
# senao o filtro vira so 'Estadual' e perde as Federais (bug reportado).
_DEP_PUBLICAS = ["Federal", "Estadual", "Municipal"]
_DEP_CANON = {
    "federal": "Federal", "estadual": "Estadual", "municipal": "Municipal",
    "privada": "Privada", "particular": "Privada",
}


def _norm_dep(raw: Any) -> str:
    import unicodedata
    return (unicodedata.normalize("NFKD", str(raw)).encode("ASCII", "ignore")
            .decode("ASCII").lower().strip())


def _apply_dependencia_filter(q: Any, raw: Any) -> Any:
    """Aplica filtro de dependencia numa query, entendendo 'publica'/'privada'.

    - 'publica'/'publico'/... -> `.in_(Federal, Estadual, Municipal)` (grupo).
    - 'privada'/'particular'/... -> `.eq('Privada')`.
    - valor exato (federal/estadual/municipal/privada, qualquer caixa/acento)
      -> `.eq` canonicalizado.
    Se `raw` for vazio, devolve a query inalterada.
    """
    if not raw:
        return q
    n = _norm_dep(raw)
    if not n:  # so espacos
        return q
    if "public" in n:  # publica, publico, "rede/escola publica"...
        return q.in_("enem_dependencia", _DEP_PUBLICAS)
    if "privad" in n or "particular" in n:
        return q.eq("enem_dependencia", "Privada")
    return q.eq("enem_dependencia", _DEP_CANON.get(n, raw))


def _formatar_performance_individual(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Retorna dados individuais da escola OU None se amostra nao confiavel."""
    if not row or row.get("enem_amostra_confiavel") is not True:
        return None

    def _n(key: str) -> Optional[float]:
        v = row.get(key)
        return float(v) if v is not None else None

    media_com = _n("enem_media_geral")
    media_sem = _n("enem_media_geral_sem_redacao")
    delta_redacao = None
    if media_com is not None and media_sem is not None:
        delta_redacao = round(media_com - media_sem, 1)

    out: Dict[str, Any] = {
        "media_geral_com_redacao": round(media_com, 1) if media_com else None,
        "media_geral_sem_redacao": round(media_sem, 1) if media_sem else None,
        "delta_redacao_vs_geral": delta_redacao,
        "presentes": row.get("enem_presentes"),
        "dependencia": row.get("enem_dependencia"),
        "potencial_melhoria": row.get("enem_potencial_melhoria"),
        "ano_enem": row.get("enem_ano") or ENEM_VINTAGE,
        "gap_vs_peer": _n("enem_gap_vs_peer_2025"),
        "taxa_presenca": _n("enem_taxa_presenca"),
    }
    # Rankings (oferece o mais granular disponivel primeiro)
    rank_uf_dep = row.get("enem_rank_uf_dep")
    rank_mun = row.get("enem_rank_mun")
    rank_uf = row.get("enem_rank_uf")
    rank_br = row.get("enem_rank_br")
    if rank_uf_dep:
        out["rank_uf_dep"] = rank_uf_dep
        out["rank_escopo_sugerido"] = "UF x dependencia (apples-to-apples)"
    if rank_mun:
        out["rank_mun"] = rank_mun
    if rank_uf:
        out["rank_uf"] = rank_uf
    if rank_br:
        out["rank_br"] = rank_br
    # Quartil nacional
    if row.get("enem_quartil_br"):
        out["quartil_br"] = row.get("enem_quartil_br")
    # Areas por disciplina
    areas = {}
    for area, label in [("cn", "Ciencias da Natureza"), ("ch", "Ciencias Humanas"),
                        ("lc", "Linguagens e Codigos"), ("mt", "Matematica"),
                        ("redacao", "Redacao")]:
        v = _n(f"enem_media_{area}")
        if v is not None:
            areas[label] = round(v, 1)
    if areas:
        out["medias_por_area"] = areas
    # Competencias da redacao (5 eixos)
    comps = {}
    for i in range(1, 6):
        v = _n(f"enem_redacao_comp{i}_media")
        if v is not None:
            comps[f"competencia_{i}"] = round(v, 1)
    if comps:
        out["competencias_redacao"] = comps
    # Problemas na redacao
    pct_prob = _n("enem_redacao_pct_problemas")
    if pct_prob is not None:
        out["redacao_pct_problemas"] = round(pct_prob * 100, 1) if pct_prob < 1 else round(pct_prob, 1)
    return out


def _formatar_area_fraca(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Texto literal de enem_area_mais_fraca (regra #4)."""
    if not row or row.get("enem_amostra_confiavel") is not True:
        return None
    fraca = row.get("enem_area_mais_fraca")
    if not fraca:
        return None
    return {
        "area": fraca,
        "fonte": "calculada a partir da menor media entre as 4 areas de conhecimento + redacao",
        "uso_no_pitch": (
            f"A area mais fraca desta escola e: {fraca}. Use este texto literal "
            f"no email. Ex: 'Pelos dados ENEM {int(row.get('enem_ano') or ENEM_VINTAGE)}, {fraca} aparece como ponto "
            f"de atencao — e justamente onde o IAprendo tem trilhas especificas'."
        ),
    }


def _formatar_trajetoria_peer(
    row: Dict[str, Any],
    *,
    municipio_clean: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Dados do peer group. Regra #3: NUNCA da escola individual.

    Args:
        row: Linha do school_analytics.
        municipio_clean: Nome limpo do municipio, ja resolvido via
            _resolve_school_names (companies -> school_censo_yearly).
            Quando presente, toma prioridade sobre peer_mun_nome
            (que pode ter mojibake em rows antigas nao corrigidas).
    """
    if not row:
        return None
    traj = row.get("peer_trajetoria_6y")
    if not traj:
        return None

    def _n(key: str) -> Optional[float]:
        v = row.get(key)
        return float(v) if v is not None else None

    # Prioridade: municipio_clean (cascata limpa) > peer_mun_nome (pode
    # ter mojibake) > city > default. _clean_text como safety net.
    municipio = (
        municipio_clean
        or _clean_text(row.get("peer_mun_nome"))
        or _clean_text(row.get("city"))
        or "seu municipio"
    )
    dep = row.get("enem_dependencia") or row.get("admin_dependency") or "mesma dependencia"

    out: Dict[str, Any] = {
        "rotulo": "Peer group — escolas do mesmo municipio x mesma dependencia",
        "disclaimer_obrigatorio": (
            "Estes dados referem-se ao GRUPO DE PARES, NUNCA a escola "
            "individual. Formulacao obrigatoria: 'suas concorrentes diretas "
            f"em {municipio} ({dep}) vem [trajetoria]'."
        ),
        "municipio": municipio,
        "dependencia": dep,
        "trajetoria": traj,
        "media_atual": _n("peer_media_geral_2025"),
        "media_2022": _n("peer_media_geral_2022"),
        "media_2020": _n("peer_media_geral_2020"),
        # Regra #7: preferir delta 2022->atual (sem distorcao da pandemia)
        "delta_2022_atual_preferido": _n("peer_delta_media_geral_2022_2025"),
        "delta_2020_atual_alternativo": _n("peer_delta_media_geral_2020_2025"),
        "slope_media_ppa": _n("peer_slope_media_geral_ppa"),
        "presentes_atual": row.get("peer_presentes_2025"),
        "evolucao_alunos_2020_atual": _n("peer_pct_evolucao_alunos_2020_2025"),
    }
    # Frase pronta para o LLM citar
    delta = out["delta_2022_atual_preferido"]
    if delta is not None:
        if delta > 5:
            dir_txt = f"vem subindo {delta:+.1f} pts entre 2022 e {ENEM_VINTAGE}"
        elif delta < -5:
            dir_txt = f"vem caindo {delta:+.1f} pts entre 2022 e {ENEM_VINTAGE}"
        else:
            dir_txt = f"estavel ({delta:+.1f} pts entre 2022 e {ENEM_VINTAGE})"
        out["frase_pronta"] = (
            f"suas concorrentes diretas em {municipio} ({dep}) {dir_txt}"
        )
    return out


def _formatar_contexto_municipal(
    row: Dict[str, Any],
    *,
    municipio_clean: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Dados socio_* SEMPRE com rotulo 'Perfil do municipio'.

    Args:
        row: Linha do school_analytics.
        municipio_clean: Nome limpo do municipio ja resolvido via
            _resolve_school_names. Prioridade sobre socio_mun_nome.
    """
    if not row:
        return None
    renda_2024 = row.get("socio_renda_idx_media_2025")
    pais_superior_2024 = row.get("socio_pct_pais_superior_2025")
    delta_renda = row.get("socio_delta_renda_2020_2025")
    if renda_2024 is None and pais_superior_2024 is None:
        return None

    def _n(key: str) -> Optional[float]:
        v = row.get(key)
        return float(v) if v is not None else None

    municipio = (
        municipio_clean
        or _clean_text(row.get("socio_mun_nome"))
        or _clean_text(row.get("city"))
    )

    return {
        "rotulo": "Perfil do municipio (NAO do aluno)",
        "disclaimer_obrigatorio": (
            "Estes dados sao do municipio onde a escola esta localizada, NUNCA "
            "da escola ou dos alunos individualmente. Formulacao correta: 'o "
            "perfil do municipio e X'. NUNCA 'os alunos desta escola sao X'."
        ),
        "municipio": municipio,
        "renda_indice_atual": round(float(renda_2024), 2) if renda_2024 else None,
        "pct_pais_com_superior_atual": (
            round(float(pais_superior_2024) * 100, 1)
            if pais_superior_2024 and pais_superior_2024 < 1
            else None
        ),
        "delta_renda_2020_atual": _n("socio_delta_renda_2020_2025"),
        "delta_pais_superior_2020_atual": _n("socio_delta_pais_superior_2020_2025"),
        "evolucao_volume_2020_atual": _n("socio_pct_evolucao_volume_2020_2025"),
    }


def campo_por_safra(
    row: Dict[str, Any], base: str, anos: tuple = (2025, 2024)
) -> Any:
    """Le uma coluna com sufixo de SAFRA respeitando o `enem_ano` DA LINHA.

    `school_analytics` nao esta 100% na mesma safra: algumas linhas ficaram em
    2024 (colunas *_2024 preenchidas, *_2025 NULL). Ler `_2025` fixo fazia
    essas escolas perderem gap/peer/socio silenciosamente — e nunca serem
    classificadas em P1/P2/P3. Aqui tentamos primeiro o ano da propria linha e
    depois os demais, em ordem de recencia.
    """
    if not row:
        return None
    ordem = []
    try:
        ano_linha = int(row.get("enem_ano") or 0)
        if ano_linha:
            ordem.append(ano_linha)
    except (TypeError, ValueError):
        pass
    for a in anos:
        if a not in ordem:
            ordem.append(a)
    for a in ordem:
        v = row.get(f"{base}_{a}")
        if v is not None:
            return v
    return None


def trajetoria_peer(row: Dict[str, Any]) -> Optional[str]:
    """Trajetoria do peer group: 6 anos, caindo para 5 se ausente."""
    if not row:
        return None
    return row.get("peer_trajetoria_6y") or row.get("peer_trajetoria_5y")


def _classificar_prioridade(row: Dict[str, Any]) -> Optional[str]:
    """Aplica regras P1/P2/P3 do plano.

    P1: potencial=Alto AND peer Subindo+ AND presentes>=30 (amostra_confiavel=T)
    P2: Privada AND gap<-10 AND peer Subindo+ AND presentes>=20 (amostra_confiavel=T)
    P3: Privada AND peer Caindo forte AND peer_delta_2022_2024<-15 AND presentes>=20
        (amostra_confiavel=T)
    """
    if not row or row.get("enem_amostra_confiavel") is not True:
        return None

    potencial = row.get("enem_potencial_melhoria")
    traj = trajetoria_peer(row)
    presentes = row.get("enem_presentes") or 0
    dep = row.get("enem_dependencia") or ""
    gap = campo_por_safra(row, "enem_gap_vs_peer")
    delta_22_24 = row.get("peer_delta_media_geral_2022_2025") \
        or row.get("peer_delta_media_geral_2022_2024")

    subindo = traj in ("Subindo", "Subindo forte")
    caindo = traj in ("Caindo", "Caindo forte")

    # P1: lead quente ofensivo
    if (potencial == "Alto" and subindo and presentes >= MIN_PRESENTES_RANKING):
        return "P1"

    # P2: privada com gap negativo E mercado subindo
    if (dep == "Privada" and gap is not None and float(gap) < -10
            and subindo and presentes >= MIN_PRESENTES_GAP):
        return "P2"

    # P3: urgencia defensiva (estrita)
    if (dep == "Privada" and caindo and presentes >= MIN_PRESENTES_GAP
            and delta_22_24 is not None
            and float(delta_22_24) < P3_DELTA_THRESHOLD):
        return "P3"

    return None


def _motivo_prioridade(
    row: Dict[str, Any],
    prioridade: str,
    *,
    municipio_clean: Optional[str] = None,
) -> str:
    """Texto etico pre-formatado com o motivo da classificacao.

    Args:
        row: Linha do school_analytics.
        prioridade: "P1" | "P2" | "P3".
        municipio_clean: Nome limpo ja resolvido via _resolve_school_names.
            Prioridade sobre row.peer_mun_nome (pode ter mojibake).
    """
    municipio = (
        municipio_clean
        or _clean_text(row.get("peer_mun_nome"))
        or _clean_text(row.get("city"))
        or "seu municipio"
    )
    dep = row.get("enem_dependencia") or "Privada"
    gap = row.get("enem_gap_vs_peer_2025")
    delta = row.get("peer_delta_media_geral_2022_2025")
    traj = row.get("peer_trajetoria_6y") or ""

    if prioridade == "P1":
        return (
            f"Escola com alto potencial de melhoria (enem_potencial_melhoria=Alto) "
            f"em mercado aquecido: suas concorrentes diretas em {municipio} ({dep}) "
            f"{traj.lower()} — lead quente, pitch ofensivo focado em ganho."
        )
    if prioridade == "P2":
        gap_txt = f"{float(gap):+.1f} pts abaixo do peer" if gap is not None else "abaixo do peer"
        return (
            f"Escola privada com gap negativo ({gap_txt}) enquanto suas "
            f"concorrentes em {municipio} vem {traj.lower()}. Oportunidade clara "
            f"de reposicionamento — focar em fechar o gap."
        )
    if prioridade == "P3":
        delta_txt = f"{float(delta):+.1f} pts em 2 anos" if delta is not None else "queda significativa"
        return (
            f"URGENCIA DEFENSIVA: suas concorrentes diretas em {municipio} ({dep}) "
            f"vem caindo ({delta_txt}) — movimento adverso do mercado. Pitch deve "
            f"focar no MOVIMENTO DE MERCADO, NUNCA atribuir a queda a esta escola."
        )
    return ""


def _aviso_p3(prioridade: Optional[str]) -> Optional[str]:
    """Retorna aviso para Fernando se o lead for P3."""
    if prioridade != "P3":
        return None
    return (
        "AVISO IMPORTANTE: lead P3 (urgencia defensiva). Ao gerar email, o tom "
        "DEVE falar do movimento do mercado na regiao, NUNCA atribuir queda a "
        "esta escola individualmente (regra #3: peer != escola). Revise com "
        "cuidado antes de aprovar — o risco de soar predatorio e alto."
    )


# ===========================================================================
# QUERIES DE SCHOOL_ANALYTICS
# ===========================================================================

def _fetch_school_analytics_by_inep(inep: str) -> Optional[Dict[str, Any]]:
    """Busca 1 linha por inep_code."""
    try:
        r = db.client.table("school_analytics").select("*").eq(
            "inep_code", str(inep).strip()
        ).limit(1).execute()
        if r.data:
            row = dict(r.data[0])
            return _strip_blocked_fields(row)
    except Exception as e:
        logger.warning(f"fetch_school_analytics_by_inep failed: {e}")
    return None


def _fetch_benchmark_for_insights(inep: str) -> Optional[Dict[str, Any]]:
    """Busca medias do peer group (mesmo municipio + dependencia) para insights ENEM.

    Returns:
        Dict com media_cn, media_ch, media_lc, media_mt, media_redacao, media_geral
        ou None se nao encontrar.
    """
    try:
        school = _fetch_school_analytics_by_inep(inep)
        if not school:
            return None
        mun = school.get("peer_mun_nome") or ""
        uf = school.get("peer_uf_sigla") or ""
        dep = school.get("enem_dependencia") or ""
        if not (mun and dep):
            return None

        metrics = [
            "enem_media_cn", "enem_media_ch", "enem_media_lc",
            "enem_media_mt", "enem_media_redacao", "enem_media_geral",
        ]
        fields = ",".join(metrics)
        # `mun` vem do proprio school_analytics (peer_mun_nome da escola), entao
        # e o valor exato — nao ha nada a casar parcialmente. O ilike com %...%
        # custava caro e era arriscado:
        #   - o planner descarta idx_sa_mun_dep (ilike nao usa btree) e cai no
        #     idx_sa_dependencia, lendo 5.937 linhas pra devolver 63:
        #     4.090 buffers / 2.918 ms contra 51 buffers / 5,9 ms com "=".
        #   - "%Sao Jose%" casa 58 municipios ("Sao Jose dos Campos", "do Xingu"...).
        #     Hoje o filtro de UF salva as 11 cidades do CRM (conferido: mesma
        #     contagem e mesma media nas 11), mas e sorte, nao desenho.
        q = db.client.table("school_analytics").select(fields).eq(
            "enem_amostra_confiavel", True
        ).eq("peer_mun_nome", mun).eq(
            "enem_dependencia", dep
        )
        if uf:
            q = q.eq("peer_uf_sigla", uf.upper())
        rows = (q.limit(500).execute()).data or []
        if not rows:
            return None

        result: Dict[str, Any] = {}
        key_map = {
            "enem_media_cn": "media_cn", "enem_media_ch": "media_ch",
            "enem_media_lc": "media_lc", "enem_media_mt": "media_mt",
            "enem_media_redacao": "media_redacao", "enem_media_geral": "media_geral",
        }
        for db_key, out_key in key_map.items():
            vals = [float(r[db_key]) for r in rows if r.get(db_key) is not None]
            result[out_key] = round(sum(vals) / len(vals), 2) if vals else None
        return result
    except Exception as e:
        logger.debug(f"fetch_benchmark_for_insights failed: {e}")
        return None


def _fetch_company_by_inep(inep: str) -> Optional[Dict[str, Any]]:
    """Busca dados cadastrais basicos da escola (se estiver em companies)."""
    try:
        r = db.client.table("companies").select(
            "id,inep_code,name,city,state,admin_dependency,school_size,"
            "nivel_tecnologico,total_matriculas,matriculas_fund_af,matriculas_medio"
        ).eq("inep_code", str(inep).strip()).limit(1).execute()
        if r.data:
            return r.data[0]
    except Exception:
        pass
    return None


def _resolve_school_names(inep_list: List[str]) -> Dict[str, Dict[str, Any]]:
    """Resolve nome/cidade/uf/company_id de uma lista de INEPs via 2 fontes.

    Cascata de lookup:
      1. ``companies`` — autoridade do CRM (~88 escolas). Ganha se existe.
      2. ``school_censo_yearly`` — fallback com nomes do Censo Escolar
         INEP 2020-2025 (~1.1M rows, cobre quase todas as 185k escolas
         do ``school_analytics``). Usa vintage mais recente disponivel
         por INEP (ORDER BY vintage_censo DESC).

    Motivacao: handlers de ranking (``priorizar_leads_enem``,
    ``buscar_escolas_por_enem``, ``analisar_dados_analytics`` branch
    ranking) antes caiam no fallback ``f"Escola INEP {inep}"`` para todo
    lead fora do CRM — o que e a maioria das escolas do pais, porque o
    CRM so tem 88 enquanto o ``school_analytics`` tem 185k. Esse helper
    centraliza a resolucao pra eliminar o fallback na pratica e manter o
    comportamento consistente entre os 3 handlers.

    Args:
        inep_list: Lista de INEP codes (strings). Deduplicacao nao e feita —
            o caller deve passar lista ja unica se quiser otimizar.

    Returns:
        Dict keyed por inep_code (str) com:
            {
                "name": str | None,
                "city": str | None,
                "state": str | None,
                "company_id": UUID | None,  # None se nao esta no CRM
                "fonte_nome": "companies" | "censo_yearly" | None,
            }
        INEPs nao encontrados em nenhuma fonte ficam FORA do dict
        retornado — o caller deve checar com ``.get(inep, {})`` e cair
        no seu proprio fallback (ex: ``f"Escola INEP {inep}"``).

    Performance:
        2 queries PostgREST batched via .in_() — sem N+1. Para 50 INEPs,
        roda em ~100-200ms total no Supabase free.
    """
    if not inep_list:
        return {}

    # Normalizar: strings, deduplicar preservando ordem
    seen = set()
    cleaned: List[str] = []
    for i in inep_list:
        s = str(i).strip()
        if s and s not in seen:
            seen.add(s)
            cleaned.append(s)

    if not cleaned:
        return {}

    out: Dict[str, Dict[str, Any]] = {}

    # Passo 1: companies (autoridade do CRM)
    try:
        cr = db.client.table("companies").select(
            "id,inep_code,name,city,state"
        ).in_("inep_code", cleaned).execute()
        for c in (cr.data or []):
            inep = str(c.get("inep_code"))
            out[inep] = {
                "name": c.get("name"),
                "city": c.get("city"),
                "state": c.get("state"),
                "company_id": c.get("id"),
                "fonte_nome": "companies",
            }
    except Exception as e:
        logger.warning(f"_resolve_school_names companies lookup failed: {e}")

    # Passo 2: school_censo_yearly (fallback) para INEPs ainda sem nome
    faltam = [i for i in cleaned if i not in out]
    if faltam:
        try:
            # ORDER BY vintage_censo DESC — pegamos o nome do Censo mais
            # recente disponivel pra cada INEP. Na iteracao Python,
            # ignoramos linhas subsequentes do mesmo INEP (equivalente a
            # DISTINCT ON (inep_code)).
            sr = db.client.table("school_censo_yearly").select(
                "inep_code,name,city,state,vintage_censo"
            ).in_("inep_code", faltam).order(
                "vintage_censo", desc=True
            ).execute()

            for row in (sr.data or []):
                inep = str(row.get("inep_code"))
                if inep in out:
                    continue  # ja pego de companies ou vintage mais novo
                out[inep] = {
                    "name": row.get("name"),
                    "city": row.get("city"),
                    "state": row.get("state"),
                    "company_id": None,  # nao esta no CRM
                    "fonte_nome": "censo_yearly",
                }
        except Exception as e:
            logger.warning(f"_resolve_school_names censo_yearly lookup failed: {e}")

    return out


# ===========================================================================
# HANDLER 1: analisar_performance_escola
# ===========================================================================

def _handle_analisar_performance_escola(params: Dict) -> str:
    """Snapshot etico da performance ENEM de UMA escola."""
    inep = params.get("inep")
    nome = params.get("escola_nome") or params.get("nome")
    escola_id = params.get("escola_id")

    row = None
    company = None

    if inep:
        row = _fetch_school_analytics_by_inep(str(inep))
        if row:
            company = _fetch_company_by_inep(str(inep))
    elif escola_id:
        try:
            r = db.client.table("companies").select("*").eq("id", escola_id).limit(1).execute()
            if r.data:
                company = r.data[0]
                if company.get("inep_code"):
                    row = _fetch_school_analytics_by_inep(str(company["inep_code"]))
        except Exception as e:
            logger.warning(f"performance_escola by id failed: {e}")
    elif nome:
        # CUIDADO: multiplas escolas podem bater com o mesmo nome (ex:
        # "COLEGIO JOAO PAULO" aparece 3 vezes em PoA). Mesmo fix aplicado
        # no analisar_trajetoria_escola — desambiguacao explicita em vez
        # de escolher silenciosamente a primeira match.
        try:
            r = db.client.table("companies").select(
                "id,inep_code,name,city,state,admin_dependency,school_size,bairro"
            ).ilike("name", f"%{nome}%").limit(10).execute()
            matches = r.data or []

            if len(matches) == 0:
                return json.dumps({
                    "erro": f"Nenhuma escola encontrada com nome contendo '{nome}' no CRM.",
                }, ensure_ascii=False)

            if len(matches) > 1:
                return json.dumps({
                    "ambiguidade": True,
                    "query_original": nome,
                    "n_matches": len(matches),
                    "escolas_encontradas": [
                        {
                            "inep": m.get("inep_code"),
                            "nome": m.get("name"),
                            "cidade": m.get("city"),
                            "bairro": m.get("bairro"),
                            "uf": m.get("state"),
                            "dependencia": m.get("admin_dependency"),
                            "porte": m.get("school_size"),
                        }
                        for m in matches
                    ],
                    "orientacao": (
                        f"Encontrei {len(matches)} escolas no CRM com '{nome}' no nome. "
                        "NAO escolha silenciosamente — apresente a lista ao Fernando "
                        "(incluindo cidade e bairro para diferenciar) e pergunte qual "
                        "delas ele quer. Quando ele responder, chame esta tool de novo "
                        "passando o parametro `inep` especifico."
                    ),
                }, ensure_ascii=False)

            # Exatamente 1 match — segue fluxo normal
            company = matches[0]
            if company.get("inep_code"):
                row = _fetch_school_analytics_by_inep(str(company["inep_code"]))
        except Exception as e:
            logger.warning(f"performance_escola by name failed: {e}")

    if not row and not company:
        return json.dumps({"erro": f"Escola '{nome or inep or escola_id}' nao encontrada."})

    if not row:
        return json.dumps({
            "escola": company.get("name") if company else "?",
            "inep": (company or {}).get("inep_code"),
            "fonte_analytics": "nao_encontrada",
            "aviso": (
                "Esta escola esta no CRM mas NAO tem dados analiticos ENEM. "
                "Possiveis motivos: e do Catalogo INEP, nao participou do ENEM "
                f"{ENEM_VINTAGE}, ou nao fez Medio. Ainda e pitchavel via dados do Censo."
            ),
        }, ensure_ascii=False)

    # Merge company metadata into row for formatters that need it
    if company:
        for k in ("city", "state", "admin_dependency", "school_size",
                  "nivel_tecnologico", "name"):
            if company.get(k) is not None and k not in row:
                row[k] = company[k]

    # Nome/municipio limpos via cascata companies -> censo_yearly. Se company ja
    # veio do CRM (lookup por nome/id), priorizamos; senao resolvemos via INEP.
    municipio_clean: Optional[str] = None
    nome_clean: Optional[str] = None
    state_clean: Optional[str] = None
    if company:
        municipio_clean = company.get("city")
        nome_clean = company.get("name")
        state_clean = company.get("state")
    if (not municipio_clean or not nome_clean) and row.get("inep_code"):
        resolved = _resolve_school_names([str(row["inep_code"])]).get(
            str(row["inep_code"]), {}
        )
        municipio_clean = municipio_clean or resolved.get("city")
        nome_clean = nome_clean or resolved.get("name")
        state_clean = state_clean or resolved.get("state")

    prioridade = _classificar_prioridade(row)
    performance = _formatar_performance_individual(row)
    area_fraca = _formatar_area_fraca(row)
    peer = _formatar_trajetoria_peer(row, municipio_clean=municipio_clean)
    contexto_mun = _formatar_contexto_municipal(row, municipio_clean=municipio_clean)

    output: Dict[str, Any] = {
        "escola": nome_clean or _clean_text(row.get("name")) or f"Escola INEP {row.get('inep_code')}",
        "inep": row.get("inep_code"),
        "cidade": municipio_clean or _clean_text(row.get("city")) or _clean_text(row.get("socio_mun_nome")),
        "estado": state_clean or row.get("state") or row.get("enem_uf_sigla"),
        "dependencia": row.get("enem_dependencia") or row.get("admin_dependency"),
        "fonte_analytics": "school_analytics",
        "amostra_confiavel": row.get("enem_amostra_confiavel") is True,
        "potencial_melhoria": row.get("enem_potencial_melhoria"),
        "prioridade_sugerida": prioridade,
        "performance_individual": performance,
        "area_fraca": area_fraca,
        "peer_group": peer,
        "contexto_municipal": contexto_mun,
    }

    if performance is None:
        output["aviso_amostra"] = (
            "Esta escola NAO tem amostra ENEM confiavel. Todos os rankings e "
            "medias individuais foram OMITIDOS (regra #1). Use APENAS os dados "
            "de peer_group e contexto_municipal para pitch."
        )

    aviso_p3 = _aviso_p3(prioridade)
    if aviso_p3:
        output["aviso_fernando"] = aviso_p3

    logger.info("enem_tool_called", extra={
        "tool": "analisar_performance_escola",
        "inep": row.get("inep_code"),
        "amostra_confiavel": row.get("enem_amostra_confiavel") is True,
        "prioridade": prioridade,
    })

    return json.dumps(output, ensure_ascii=False, default=str)


# ===========================================================================
# HANDLER 2: priorizar_leads_enem
# ===========================================================================

def _handle_priorizar_leads_enem(params: Dict) -> str:
    """Retorna ranking P1/P2/P3 filtrado por municipio/uf/dependencia."""
    municipio = params.get("municipio")
    uf = params.get("uf")
    dependencia = params.get("dependencia")
    prioridade_filter = params.get("prioridade")
    limite = min(int(params.get("limite", 30)), 100)

    # Campos que precisamos para classificar.
    # Inclui as colunas das DUAS safras (2024/2025): linhas antigas so tem as
    # _2024 e ficavam sem gap/trajetoria -> nunca eram classificadas.
    select_fields = (
        "inep_code,company_id,enem_ano,enem_dependencia,enem_amostra_confiavel,"
        "enem_potencial_melhoria,enem_presentes,"
        "enem_gap_vs_peer_2025,enem_gap_vs_peer_2024,"
        "peer_trajetoria_6y,peer_trajetoria_5y,"
        "peer_delta_media_geral_2022_2025,peer_delta_media_geral_2022_2024,"
        "peer_mun_nome,socio_mun_nome"
    )

    def _pagina(offset: int):
        q = db.client.table("school_analytics").select(select_fields).eq(
            "enem_amostra_confiavel", True
        )
        q = _apply_dependencia_filter(q, dependencia)
        if municipio:
            # match EXATO: o ilike '%x%' fazia "Santa Maria" puxar tambem
            # "Santa Maria do Herval"
            q = q.eq("peer_mun_nome", municipio)
        if uf:
            q = q.eq("peer_uf_sigla", uf.upper())
        return q.range(offset, offset + _POSTGREST_PAGE_SIZE - 1).execute()

    # PAGINA: o PostgREST clampa .limit() em 1000. Recortes grandes
    # (ex.: Estadual/Brasil = 15.828 escolas) viravam amostra de 1/16
    # apresentada como total.
    rows: List[Dict[str, Any]] = []
    try:
        _off = 0
        while _off < MAX_ROWS_FETCH:
            _page = (_pagina(_off).data) or []
            rows.extend(_page)
            if len(_page) < _POSTGREST_PAGE_SIZE:
                break
            _off += _POSTGREST_PAGE_SIZE
    except Exception as e:
        return json.dumps({"erro": f"Falha na query: {str(e)[:200]}"})

    # Classificar cada row
    ranked: List[Tuple[str, Dict[str, Any]]] = []
    for row in rows:
        p = _classificar_prioridade(row)
        if p is None:
            continue
        if prioridade_filter and p != prioridade_filter.upper():
            continue
        ranked.append((p, row))

    # Ordenar: P1 > P2 > P3, depois por gap ascendente (maior gap negativo primeiro)
    priority_order = {"P1": 0, "P2": 1, "P3": 2}
    ranked.sort(key=lambda t: (
        priority_order.get(t[0], 99),
        float(t[1].get("enem_gap_vs_peer_2025") or 0),
    ))

    # Resolver nomes para os top N via cascata companies -> school_censo_yearly
    top = ranked[:limite]
    inep_list = [str(r[1].get("inep_code")) for r in top if r[1].get("inep_code")]
    nomes_map = _resolve_school_names(inep_list)

    leads = []
    for prio, row in top:
        inep = str(row.get("inep_code"))
        info = nomes_map.get(inep, {})
        # Municipio limpo via cascata companies -> censo_yearly (sempre UTF-8 OK).
        # Fallback em _clean_text(peer_mun_nome) como safety net.
        municipio_clean = info.get("city") or _clean_text(row.get("peer_mun_nome"))
        leads.append({
            "inep": inep,
            "nome": info.get("name") or f"Escola INEP {inep}",
            "municipio": municipio_clean,
            "uf": info.get("state") or row.get("peer_uf_sigla"),
            "dependencia": row.get("enem_dependencia"),
            "prioridade": prio,
            "motivo": _motivo_prioridade(row, prio, municipio_clean=municipio_clean),
            "presentes": row.get("enem_presentes"),
            "gap_vs_peer":float(row.get("enem_gap_vs_peer_2025")) if row.get("enem_gap_vs_peer_2025") is not None else None,
            "trajetoria_peer": row.get("peer_trajetoria_6y"),
            "esta_em_companies": info.get("company_id") is not None,
            "company_id": info.get("company_id"),
            "fonte_nome": info.get("fonte_nome"),
            "aviso_fernando": _aviso_p3(prio),
        })

    total_p1 = sum(1 for p, _ in ranked if p == "P1")
    total_p2 = sum(1 for p, _ in ranked if p == "P2")
    total_p3 = sum(1 for p, _ in ranked if p == "P3")

    logger.info("enem_tool_called", extra={
        "tool": "priorizar_leads_enem",
        "municipio": municipio, "uf": uf, "dependencia": dependencia,
        "total_p1": total_p1, "total_p2": total_p2, "total_p3": total_p3,
        "retornados": len(leads),
    })

    return json.dumps({
        "total_p1": total_p1,
        "total_p2": total_p2,
        "total_p3": total_p3,
        "total_retornados": len(leads),
        "filtros": {
            "municipio": municipio, "uf": uf,
            "dependencia": dependencia, "prioridade": prioridade_filter,
        },
        "leads": leads,
    }, ensure_ascii=False, default=str)


# ===========================================================================
# HANDLER 3: buscar_escolas_por_enem
# ===========================================================================

def _handle_buscar_escolas_por_enem(params: Dict) -> str:
    """Busca filtrada pelos campos analiticos."""
    area_fraca = params.get("area_fraca")
    potencial = params.get("potencial")
    trajetoria = params.get("trajetoria") or []
    if isinstance(trajetoria, str):
        trajetoria = [trajetoria]
    gap_max = params.get("gap_max")
    uf = params.get("uf")
    cidade = params.get("cidade")
    dependencia = params.get("dependencia")
    only_confiavel = params.get("only_confiavel", True)
    ordenar_por = (params.get("ordenar_por") or "").strip().lower()
    limite = min(int(params.get("limite", 20)), 100)

    # --- Decisao de ordenacao (corrige "melhor nota" caindo em ordem por gap) ---
    # gap asc = escolas MAIS ABAIXO do peer (= LEADS). media desc = MELHORES notas.
    # Se ha filtro de OPORTUNIDADE (lead discovery), gap faz sentido como padrao;
    # senao (ex: so uf/cidade — "melhor escola do RS"), o padrao e MEDIA desc.
    _tem_filtro_oportunidade = bool(
        area_fraca or potencial or trajetoria or gap_max is not None
    )
    _ORDER_MAP = {
        "media": ("enem_media_geral", True),
        "media_geral": ("enem_media_geral", True),
        "media_sem_redacao": ("enem_media_geral_sem_redacao", True),
        "gap": ("enem_gap_vs_peer_2025", False),
    }
    if ordenar_por in _ORDER_MAP:
        _order_col, _order_desc = _ORDER_MAP[ordenar_por]
    elif _tem_filtro_oportunidade:
        _order_col, _order_desc = ("enem_gap_vs_peer_2025", False)
    else:
        _order_col, _order_desc = ("enem_media_geral", True)
    _ordenado_por = "gap_asc" if _order_col == "enem_gap_vs_peer_2025" else f"{_order_col}_desc"

    select_fields = (
        "inep_code,company_id,enem_dependencia,enem_amostra_confiavel,"
        "enem_media_geral,enem_media_geral_sem_redacao,enem_area_mais_fraca,"
        "enem_potencial_melhoria,enem_gap_vs_peer_2025,enem_presentes,"
        "peer_trajetoria_6y,peer_mun_nome,peer_uf_sigla"
    )

    try:
        q = db.client.table("school_analytics").select(select_fields)
        # PISO DE DADO ENEM REAL (defense in depth — bug "escola infantil no topo"):
        # esta e uma busca/ranking POR ENEM, entao so escolas com nota ENEM real
        # E amostra confiavel podem aparecer. O gate da regra #1 NAO e desligavel
        # aqui (only_confiavel=False nao afrouxa o gate — seria a porta do bug).
        q = q.eq("enem_amostra_confiavel", True)
        q = q.not_.is_("enem_media_geral", "null")
        if potencial:
            q = q.eq("enem_potencial_melhoria", potencial)
        if trajetoria:
            q = q.in_("peer_trajetoria_6y", trajetoria)
        if area_fraca:
            q = q.ilike("enem_area_mais_fraca", f"%{area_fraca}%")
        if gap_max is not None:
            q = q.lte("enem_gap_vs_peer_2025", float(gap_max))
        q = _apply_dependencia_filter(q, dependencia)
        if uf:
            q = q.eq("peer_uf_sigla", uf.upper())
        if cidade:
            q = q.ilike("peer_mun_nome", f"%{cidade}%")

        # nullsfirst=False: NULL no fim (PostgREST default p/ asc joga NULL no topo).
        r = q.order(
            _order_col, desc=_order_desc, nullsfirst=False
        ).limit(limite).execute()
    except Exception as e:
        return json.dumps({"erro": f"Falha na busca: {str(e)[:200]}"})

    # Cinto Python (garantia final + ponto de teste): descarta qualquer linha sem
    # nota ENEM utilizavel — pega NULL real E numero suprimido por amostra nao
    # confiavel (_strip_gated_fields zera enem_media_geral quando amostra != True).
    rows = [
        r2 for r2 in (
            _strip_gated_fields(_strip_blocked_fields(dict(row))) for row in (r.data or [])
        )
        if r2.get("enem_media_geral") is not None
    ]

    # Resolver nomes via cascata companies -> school_censo_yearly
    inep_list = [str(row.get("inep_code")) for row in rows if row.get("inep_code")]
    nomes_map = _resolve_school_names(inep_list)

    escolas = []
    for row in rows:
        inep = str(row.get("inep_code"))
        info = nomes_map.get(inep, {})
        escolas.append({
            "inep": inep,
            "nome": info.get("name") or f"Escola INEP {inep}",
            "cidade": info.get("city") or _clean_text(row.get("peer_mun_nome")),
            "uf": info.get("state") or row.get("peer_uf_sigla"),
            "dependencia": row.get("enem_dependencia"),
            "media_geral": row.get("enem_media_geral"),
            "media_sem_redacao": row.get("enem_media_geral_sem_redacao"),
            "area_fraca": row.get("enem_area_mais_fraca"),
            "potencial": row.get("enem_potencial_melhoria"),
            "gap_vs_peer":row.get("enem_gap_vs_peer_2025"),
            "trajetoria_peer": row.get("peer_trajetoria_6y"),
            "esta_em_companies": info.get("company_id") is not None,
            "company_id": info.get("company_id"),
            "fonte_nome": info.get("fonte_nome"),
        })

    logger.info("enem_tool_called", extra={
        "tool": "buscar_escolas_por_enem",
        "filtros_count": sum(1 for v in [area_fraca, potencial, trajetoria, gap_max, uf, cidade, dependencia] if v),
        "retornados": len(escolas),
    })

    return json.dumps({
        "total": len(escolas),
        "filtros_aplicados": {
            "area_fraca": area_fraca,
            "potencial": potencial,
            "trajetoria": trajetoria,
            "gap_max": gap_max,
            "uf": uf,
            "cidade": cidade,
            "dependencia": dependencia,
            "only_confiavel": only_confiavel,
            "ordenado_por": _ordenado_por,
        },
        "escolas": escolas,
    }, ensure_ascii=False, default=str)


# ===========================================================================
# HANDLER 4: analisar_dados_analytics (query builder)
# ===========================================================================

def _validate_metricas(metricas: List[str]) -> Tuple[List[str], List[str]]:
    """Split into (allowed, rejected). Rejected includes blocked and unknown."""
    allowed: List[str] = []
    rejected: List[str] = []
    for m in metricas:
        if m in PNT_BLOCKED:
            rejected.append(f"{m} (bloqueado: campo sensivel, nao uso comercial)")
        elif m in ALLOWED_ANALYTICS_METRICS:
            allowed.append(m)
        else:
            rejected.append(f"{m} (nao esta na whitelist)")
    return allowed, rejected


def _aggregate_values(values: List[float], aggregacao: str) -> Optional[float]:
    """Python-side aggregation (avoids SQL complexity via PostgREST)."""
    vs = [float(v) for v in values if v is not None]
    if not vs:
        return None
    if aggregacao == "media":
        return round(sum(vs) / len(vs), 2)
    if aggregacao == "mediana":
        vs_sorted = sorted(vs)
        n = len(vs_sorted)
        return round(vs_sorted[n // 2] if n % 2 else (vs_sorted[n // 2 - 1] + vs_sorted[n // 2]) / 2, 2)
    if aggregacao == "soma":
        return round(sum(vs), 2)
    if aggregacao == "min":
        return round(min(vs), 2)
    if aggregacao == "max":
        return round(max(vs), 2)
    if aggregacao == "count":
        return len(vs)
    if aggregacao in ("p25", "p75", "p90"):
        pct = {"p25": 25, "p75": 75, "p90": 90}[aggregacao]
        vs_sorted = sorted(vs)
        k = (len(vs_sorted) - 1) * pct / 100
        f = int(k)
        c = min(f + 1, len(vs_sorted) - 1)
        return round(vs_sorted[f] + (vs_sorted[c] - vs_sorted[f]) * (k - f), 2)
    return None


# PostgREST page cap — Supabase hosted silently clamps .limit() to this.
# Para obter >1000 rows, paginamos automaticamente via .range().
_POSTGREST_PAGE_SIZE = 1000


def _fetch_filtered(
    filtros: Dict[str, Any],
    fields: List[str],
    limit: int = MAX_ROWS_FETCH,
    force_confiavel: bool = False,
) -> List[Dict[str, Any]]:
    """Fetch school_analytics rows matching filters via PostgREST.

    Pagina automaticamente se o result set for maior que o page cap do
    PostgREST (1000 rows no Supabase hosted). Para queries de agregacao
    (comparacao, valor_unico), isso garante que a media seja calculada
    sobre TODAS as escolas que matcham os filtros, nao apenas as
    primeiras 1000.
    """
    # Sempre incluir chaves de join
    base_fields = set(fields) | {
        "inep_code", "enem_amostra_confiavel", "enem_dependencia",
        "peer_mun_nome", "peer_uf_sigla",
    }
    sel = ",".join(sorted(base_fields))
    try:
        q = db.client.table("school_analytics").select(sel)
        if force_confiavel:
            q = q.eq("enem_amostra_confiavel", True)
        if filtros.get("amostra_confiavel") is True:
            q = q.eq("enem_amostra_confiavel", True)
        q = _apply_dependencia_filter(q, filtros.get("dependencia"))
        if filtros.get("potencial"):
            q = q.eq("enem_potencial_melhoria", filtros["potencial"])
        if filtros.get("trajetoria_peer"):
            trajs = filtros["trajetoria_peer"]
            if isinstance(trajs, str):
                trajs = [trajs]
            q = q.in_("peer_trajetoria_6y", trajs)
        if filtros.get("area_fraca"):
            q = q.ilike("enem_area_mais_fraca", f"%{filtros['area_fraca']}%")
        if filtros.get("uf"):
            q = q.eq("peer_uf_sigla", filtros["uf"].upper())
        if filtros.get("municipio"):
            q = q.ilike("peer_mun_nome", f"%{filtros['municipio']}%")
        if filtros.get("nome") or filtros.get("inep"):
            # Resolver via companies
            nome = filtros.get("nome")
            inep = filtros.get("inep")
            if inep:
                q = q.eq("inep_code", str(inep).strip())
            elif nome:
                try:
                    cr = db.client.table("companies").select("inep_code").ilike(
                        "name", f"%{nome}%"
                    ).limit(50).execute()
                    ineps = [str(c["inep_code"]) for c in (cr.data or []) if c.get("inep_code")]
                    if not ineps:
                        return []
                    q = q.in_("inep_code", ineps)
                except Exception:
                    return []

        # --- Paginacao automatica ---
        # Se limit <= page cap, 1 request basta (caso mais comum: escala=escola).
        # Se limit > page cap, pagina ate atingir limit ou esgotar dados.
        if limit <= _POSTGREST_PAGE_SIZE:
            r = q.limit(limit).execute()
            return [dict(row) for row in (r.data or [])]

        all_rows: List[Dict[str, Any]] = []
        offset = 0
        while offset < limit:
            page_size = min(_POSTGREST_PAGE_SIZE, limit - offset)
            r = q.range(offset, offset + page_size - 1).execute()
            batch = r.data or []
            all_rows.extend(dict(row) for row in batch)
            if len(batch) < page_size:
                break  # sem mais dados
            offset += page_size
        return all_rows
    except Exception as e:
        logger.warning(f"_fetch_filtered failed: {e}")
        return []


def _apply_comparacao_filters(
    base_filtros: Dict[str, Any],
    comparar_com: str,
    alvo_row: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Derive a new filter set relative to alvo_row for comparacao scenarios."""
    f = dict(base_filtros)
    if comparar_com == "brasil":
        # Sem filtro geografico
        f.pop("uf", None)
        f.pop("municipio", None)
        return f
    if comparar_com == "estado":
        f.pop("municipio", None)
        if alvo_row and alvo_row.get("peer_uf_sigla"):
            f["uf"] = alvo_row["peer_uf_sigla"]
        return f
    if comparar_com == "municipio":
        if alvo_row and alvo_row.get("peer_mun_nome"):
            f["municipio"] = alvo_row["peer_mun_nome"]
        return f
    if comparar_com == "mesma_dependencia":
        if alvo_row and alvo_row.get("enem_dependencia"):
            f["dependencia"] = alvo_row["enem_dependencia"]
        return f
    if comparar_com == "mesmo_porte":
        # school_size esta em companies, nao em school_analytics
        # Buscar porte via inep -> companies -> school_size
        if alvo_row and alvo_row.get("inep_code"):
            c = _fetch_company_by_inep(str(alvo_row["inep_code"]))
            if c and c.get("school_size"):
                f["school_size"] = c["school_size"]
        return f
    if comparar_com == "mesmo_nivel_tecnologico":
        if alvo_row and alvo_row.get("inep_code"):
            c = _fetch_company_by_inep(str(alvo_row["inep_code"]))
            if c and c.get("nivel_tecnologico"):
                f["nivel_tecnologico"] = c["nivel_tecnologico"]
        return f
    return f


def _handle_analisar_dados_analytics(params: Dict) -> str:
    """Query builder flexivel para perguntas abertas. Validado via whitelist."""
    operacao = params.get("operacao")
    alvo = params.get("alvo") or {}
    metricas = params.get("metricas") or []
    agregacao = params.get("agregacao", "media")
    agrupar_por = params.get("agrupar_por")
    comparar_com = params.get("comparar_com") or []
    if isinstance(comparar_com, str):
        comparar_com = [comparar_com]
    anos = params.get("anos")
    modo_redacao = params.get("modo_redacao", "com")
    ordem = params.get("ordem", "desc")
    top_n = min(int(params.get("top_n", 20)), 100)

    # ---- Validacoes ----
    if operacao not in ALLOWED_OPERATIONS:
        return json.dumps({
            "erro": f"operacao='{operacao}' invalida. Use: {sorted(ALLOWED_OPERATIONS)}",
        })
    if agregacao and agregacao not in ALLOWED_AGGREGATIONS:
        return json.dumps({
            "erro": f"agregacao='{agregacao}' invalida. Use: {sorted(ALLOWED_AGGREGATIONS)}",
        })
    if agrupar_por and agrupar_por not in ALLOWED_GROUPINGS:
        return json.dumps({
            "erro": f"agrupar_por='{agrupar_por}' invalida. Use: {sorted(ALLOWED_GROUPINGS)}",
        })
    if modo_redacao and modo_redacao not in ALLOWED_MODO_REDACAO:
        return json.dumps({
            "erro": f"modo_redacao='{modo_redacao}' invalido. Use: {sorted(ALLOWED_MODO_REDACAO)} (Cenario A)",
        })
    for cc in comparar_com:
        if cc not in ALLOWED_COMPARACAO_COM:
            return json.dumps({
                "erro": f"comparar_com='{cc}' invalido. Use: {sorted(ALLOWED_COMPARACAO_COM)}",
            })
    if not isinstance(metricas, list) or not metricas:
        return json.dumps({"erro": "metricas deve ser lista nao vazia."})

    allowed_metricas, rejected = _validate_metricas(metricas)
    if rejected and not allowed_metricas:
        return json.dumps({
            "erro": "Nenhuma metrica valida.",
            "metricas_rejeitadas": rejected,
            "disponivel": sorted(list(ALLOWED_ANALYTICS_METRICS))[:30],
        })

    warnings: List[str] = []
    if rejected:
        warnings.append(f"Metricas rejeitadas: {rejected}")

    # ---- Modo redacao resolution ----
    if modo_redacao == "sem":
        allowed_metricas = [m.replace("enem_media_geral", "enem_media_geral_sem_redacao")
                            if m == "enem_media_geral" else m for m in allowed_metricas]
    elif modo_redacao == "ambos":
        if "enem_media_geral" in allowed_metricas and "enem_media_geral_sem_redacao" not in allowed_metricas:
            allowed_metricas.append("enem_media_geral_sem_redacao")

    # ---- Filtros do alvo ----
    alvo_filtros: Dict[str, Any] = dict(alvo.get("filtros") or {})
    escala = alvo.get("escala", "custom")

    # ---- Queries individuais por operacao ----
    resposta: Dict[str, Any] = {
        "operacao": operacao,
        "alvo": {"escala": escala, "filtros": alvo_filtros},
        "metricas": allowed_metricas,
        "modo_redacao": modo_redacao,
    }

    has_gated = any(m in AMOSTRA_CONFIAVEL_GATED for m in allowed_metricas)

    # Para valor_unico de escola individual: aplicar gate amostra_confiavel
    force_confiavel_for_aggregates = has_gated and operacao != "valor_unico"

    if operacao == "valor_unico":
        rows = _fetch_filtered(alvo_filtros, allowed_metricas, limit=MAX_ROWS_FETCH)
        if not rows:
            return json.dumps({"erro": "Nenhuma escola encontrada com esses filtros.",
                               "filtros": alvo_filtros})
        if escala == "escola":
            row = _strip_gated_fields(_strip_blocked_fields(rows[0]))
            vals = {m: row.get(m) for m in allowed_metricas}
            if has_gated and rows[0].get("enem_amostra_confiavel") is not True:
                warnings.append(
                    "Amostra nao confiavel: metricas individuais omitidas (regra #1)."
                )
            resposta["resultado"] = vals
        else:
            # Agregacao local
            excluded_sample = 0
            if has_gated:
                kept_rows = [r for r in rows if r.get("enem_amostra_confiavel") is True]
                excluded_sample = len(rows) - len(kept_rows)
                if excluded_sample > 0:
                    warnings.append(
                        f"{excluded_sample} escolas sem amostra confiavel excluidas "
                        f"da agregacao (regra #1)."
                    )
                rows = kept_rows
            vals = {
                m: _aggregate_values([r.get(m) for r in rows], agregacao)
                for m in allowed_metricas
            }
            resposta["resultado"] = vals
            resposta["n_escolas_agregadas"] = len(rows)

    elif operacao == "ranking":
        rows = _fetch_filtered(
            alvo_filtros, allowed_metricas,
            limit=MAX_ROWS_FETCH,
            force_confiavel=has_gated,
        )
        ordenavel = [r for r in rows if all(r.get(m) is not None for m in allowed_metricas)]
        if has_gated:
            excluded = len(rows) - len(ordenavel)
            if excluded > 0:
                warnings.append(
                    f"{excluded} escolas sem todas as metricas solicitadas excluidas do ranking."
                )
        # Score composto: media das metricas solicitadas
        for r in ordenavel:
            r["_score"] = sum(float(r.get(m) or 0) for m in allowed_metricas) / max(len(allowed_metricas), 1)
        ordenavel.sort(key=lambda r: r["_score"], reverse=(ordem == "desc"))
        top = ordenavel[:top_n]
        # Resolver nomes via cascata companies -> school_censo_yearly
        inep_list = [str(r.get("inep_code")) for r in top]
        nomes_map = _resolve_school_names(inep_list)
        resposta["resultado"] = [
            {
                "inep": str(r.get("inep_code")),
                "nome": (nomes_map.get(str(r.get("inep_code")), {}).get("name")) or f"INEP {r.get('inep_code')}",
                "municipio": (nomes_map.get(str(r.get("inep_code")), {}).get("city")) or _clean_text(r.get("peer_mun_nome")),
                "uf": (nomes_map.get(str(r.get("inep_code")), {}).get("state")) or r.get("peer_uf_sigla"),
                "dependencia": r.get("enem_dependencia"),
                **{m: r.get(m) for m in allowed_metricas},
            }
            for r in top
        ]
        resposta["n_consideradas"] = len(ordenavel)

    elif operacao == "comparacao":
        if not comparar_com:
            return json.dumps({"erro": "comparacao requer 'comparar_com' nao vazio."})

        # Fetch alvo: o limit e o gate devem ser consistentes com o fetch das
        # comparacoes — caso contrario, alvo e referencias sao calculados sobre
        # populacoes diferentes e os numeros divergem (bug F2.18).
        # - escala=escola: 1 linha basta (limit pequeno OK)
        # - escala=municipio/estado/brasil/custom: agregado, precisa de todas
        if escala == "escola":
            alvo_rows = _fetch_filtered(alvo_filtros, allowed_metricas, limit=10)
        else:
            alvo_rows = _fetch_filtered(
                alvo_filtros, allowed_metricas,
                limit=MAX_ROWS_FETCH,
                force_confiavel=has_gated,
            )

        if not alvo_rows:
            return json.dumps({"erro": "Alvo nao encontrado.", "filtros": alvo_filtros})

        # alvo_row: a primeira linha do alvo. Para escala=escola, e a escola
        # propriamente dita. Para escalas agregadas, usamos a primeira row
        # como fonte de metadados (peer_uf_sigla, peer_mun_nome, etc.) para
        # que _apply_comparacao_filters consiga inferir UF/municipio quando o
        # usuario nao os especificou explicitamente nos filtros.
        alvo_row = alvo_rows[0]

        # Aggregate alvo
        alvo_vals: Dict[str, Any] = {}
        alvo_n = len(alvo_rows)  # exposto no retorno para transparencia
        for m in allowed_metricas:
            if escala == "escola":
                alvo_vals[m] = alvo_rows[0].get(m)
            else:
                # _fetch_filtered ja aplicou force_confiavel para o alvo;
                # nao precisa filtrar de novo. Manter consistencia com comparacao.
                alvo_vals[m] = _aggregate_values([r.get(m) for r in alvo_rows], agregacao)
        # Gate alvo for escola individual
        if escala == "escola" and has_gated and alvo_rows[0].get("enem_amostra_confiavel") is not True:
            alvo_vals = {m: None for m in allowed_metricas}
            warnings.append(
                "Amostra nao confiavel no alvo: metricas individuais omitidas."
            )

        comparacoes = []
        for cc in comparar_com:
            comp_filtros = _apply_comparacao_filters(alvo_filtros, cc, alvo_row)
            # Remover filtros muito especificos para ampliar o comparavel
            if cc in ("estado", "brasil", "mesma_dependencia", "mesmo_porte", "mesmo_nivel_tecnologico"):
                comp_filtros.pop("nome", None)
                comp_filtros.pop("inep", None)
            comp_rows = _fetch_filtered(
                comp_filtros, allowed_metricas,
                limit=MAX_ROWS_FETCH,
                force_confiavel=has_gated,
            )
            comp_vals = {
                m: _aggregate_values([r.get(m) for r in comp_rows], agregacao)
                for m in allowed_metricas
            }
            comparacoes.append({
                "escopo": cc,
                "n_escolas": len(comp_rows),
                "valores": comp_vals,
            })
        resposta["resultado"] = {
            "alvo": alvo_vals,
            "comparacoes": comparacoes,
        }

    elif operacao == "serie_temporal":
        rows = _fetch_filtered(alvo_filtros, allowed_metricas, limit=MAX_ROWS_FETCH)
        if not rows:
            return json.dumps({"erro": "Nenhuma escola encontrada.", "filtros": alvo_filtros})
        if has_gated:
            rows = [r for r in rows if r.get("enem_amostra_confiavel") is True]
        # Agregar por metrica (que ja inclui o ano no nome, ex: peer_media_geral_2020)
        serie: Dict[str, Any] = {}
        for m in allowed_metricas:
            vals = [r.get(m) for r in rows]
            serie[m] = _aggregate_values(vals, agregacao)
        resposta["resultado"] = serie
        resposta["n_escolas"] = len(rows)

    elif operacao == "distribuicao":
        if not agrupar_por:
            return json.dumps({"erro": "distribuicao requer 'agrupar_por'."})
        rows = _fetch_filtered(
            alvo_filtros, allowed_metricas + [agrupar_por],
            limit=MAX_ROWS_FETCH,
            force_confiavel=has_gated,
        )
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            key = r.get(agrupar_por) or "outros"
            groups.setdefault(str(key), []).append(r)
        dist = []
        for key, grp_rows in groups.items():
            entry = {
                "grupo": key,
                "n": len(grp_rows),
            }
            for m in allowed_metricas:
                entry[m] = _aggregate_values([r.get(m) for r in grp_rows], agregacao)
            dist.append(entry)
        # Ordenar por n desc
        dist.sort(key=lambda d: d["n"], reverse=True)
        resposta["resultado"] = dist[:top_n]
        resposta["n_grupos"] = len(dist)

    # ---- Rotulacao etica de metricas socio_* ----
    has_socio = any(m.startswith("socio_") for m in allowed_metricas)
    has_pnt = any(m.startswith("pnt_") for m in allowed_metricas)
    if has_socio:
        resposta["disclaimer_socio"] = (
            "Dados socio_* sao do MUNICIPIO onde a escola esta localizada, "
            "NAO da escola ou dos alunos individualmente. Formulacao correta: "
            "'o perfil do municipio e X'."
        )
    if has_pnt:
        resposta["disclaimer_pnt"] = (
            "Dados pnt_* sao do PERFIL AGREGADO dos inscritos ENEM desta escola, "
            "nao identificam individuos. Campos sensiveis (raca, infraestrutura "
            "domiciliar) estao BLOQUEADOS e nunca aparecem aqui."
        )

    resposta["warnings"] = warnings

    logger.info("enem_tool_called", extra={
        "tool": "analisar_dados_analytics",
        "operacao": operacao,
        "metricas_count": len(allowed_metricas),
        "metricas_rejected": len(rejected),
        "warnings_count": len(warnings),
    })

    return json.dumps(resposta, ensure_ascii=False, default=str)


# ===========================================================================
# HANDLER 5: analisar_trajetoria_escola (serie historica individual)
# ===========================================================================

# Campos do school_censo_yearly que a serie expoe. Seleção enxuta focada
# no que e narrativa comercial: matriculas por etapa, equipe, tech, infra.
_CENSO_YEARLY_FIELDS = [
    "vintage_censo",
    "name",
    "qt_mat_bas", "qt_mat_inf", "qt_mat_fund",
    "qt_mat_fund_ai", "qt_mat_fund_af", "qt_mat_med",
    "qt_mat_eja", "qt_mat_prof",
    "qt_doc_bas", "qt_doc_fund", "qt_doc_med",
    "in_internet", "in_internet_alunos", "in_internet_aprendizagem",
    "in_laboratorio_informatica",
    "qt_desktop_aluno", "qt_comp_portatil_aluno", "qt_tablet_aluno",
    "in_biblioteca", "in_quadra_esportes", "in_laboratorio_ciencias",
    "in_alimentacao",
]

# Campos do school_enem_yearly que a serie expoe (hoje so 2024, cresce no
# futuro). Seleção enxuta: media geral, media por area, area fraca,
# amostra, potencial.
_ENEM_YEARLY_FIELDS = [
    "vintage_enem",
    "enem_amostra_confiavel", "enem_presentes", "enem_inscritos",
    "enem_media_geral", "enem_media_geral_sem_redacao",
    "enem_media_cn", "enem_media_ch", "enem_media_lc", "enem_media_mt",
    "enem_media_redacao",
    "enem_area_mais_fraca", "enem_potencial_melhoria",
    "enem_rank_uf_dep", "enem_rank_mun", "enem_quartil_br",
]


def _fetch_censo_series(inep: str) -> List[Dict[str, Any]]:
    """Serie completa do school_censo_yearly para uma escola."""
    try:
        sel = ",".join(_CENSO_YEARLY_FIELDS)
        r = (
            db.client.table("school_censo_yearly")
            .select(sel)
            .eq("inep_code", str(inep).strip())
            .order("vintage_censo")
            .execute()
        )
        return [dict(row) for row in (r.data or [])]
    except Exception as e:
        logger.warning(f"_fetch_censo_series failed: {e}")
        return []


def _mesma_medicao(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> bool:
    """True se duas safras carregam EXATAMENTE a mesma medicao.

    Por que existe: 141 escolas (5 no CRM) tem a linha de 2025 como COPIA da de
    2024 em `school_enem_yearly` — mesma media ate a 4a casa E mesmo numero de
    presentes. Isso acontece quando a escola NAO teve resultado proprio na safra
    nova (nao atingiu amostra) e algo replicou a anterior.

    Consequencia se ignorado: "quanto a escola evoluiu de 2024 p/ 2025?"
    responde 0.0 — o usuario le "estagnou", quando a verdade e "nao ha dado
    novo". Duas coisas MUITO diferentes para uma decisao comercial.

    Criterio: media geral E presentes identicos. Coincidencia real nos dois,
    com 4 casas decimais, e desprezivel (0,1% da base — justamente as copias).
    """
    if not a or not b:
        return False

    def _num(row: Dict[str, Any], campo: str) -> Optional[float]:
        v = row.get(campo)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    m_a, m_b = _num(a, "enem_media_geral"), _num(b, "enem_media_geral")
    p_a, p_b = _num(a, "enem_presentes"), _num(b, "enem_presentes")
    if m_a is None or m_b is None:
        return False
    return m_a == m_b and p_a == p_b


def _fetch_enem_series(inep: str) -> List[Dict[str, Any]]:
    """Serie completa do school_enem_yearly para uma escola. Aplica gate
    amostra_confiavel: se FALSE, remove metricas individuais do payload
    naquela vintage.
    """
    try:
        sel = ",".join(_ENEM_YEARLY_FIELDS)
        r = (
            db.client.table("school_enem_yearly")
            .select(sel)
            .eq("inep_code", str(inep).strip())
            .order("vintage_enem")
            .execute()
        )
        raw = r.data or []
        # Aplica defense in depth: strip gated fields quando amostra nao confiavel
        gated_fields = {
            "enem_media_geral", "enem_media_geral_sem_redacao",
            "enem_media_cn", "enem_media_ch", "enem_media_lc",
            "enem_media_mt", "enem_media_redacao",
            "enem_rank_uf_dep", "enem_rank_mun", "enem_quartil_br",
        }
        out = []
        for row in raw:
            clean = dict(row)
            if row.get("enem_amostra_confiavel") is not True:
                for f in gated_fields:
                    clean.pop(f, None)
            out.append(clean)

        # Marca safras que sao COPIA da anterior (escola sem resultado proprio
        # naquele ano). Sem isso, a serie sugere "manteve exatamente a nota" —
        # e o LLM conclui "estagnou" onde o certo e "nao ha dado novo".
        for i in range(1, len(out)):
            if _mesma_medicao(out[i - 1], out[i]):
                out[i]["dado_repetido_da_safra_anterior"] = True
                out[i]["aviso"] = (
                    f"Sem resultado proprio no ENEM {out[i].get('vintage_enem')}: "
                    f"os numeros repetem a safra {out[i - 1].get('vintage_enem')} "
                    f"(a escola provavelmente nao atingiu a amostra minima). "
                    f"NAO interprete como estabilidade nem calcule evolucao."
                )
        return out
    except Exception as e:
        logger.warning(f"_fetch_enem_series failed: {e}")
        return []


# ===========================================================================
# METRICAS DERIVADAS — calculadas server-side para cada ano do Censo
# ===========================================================================

def _safe_ratio(numerator: Any, denominator: Any, decimals: int = 1) -> Optional[float]:
    """Divisao segura: retorna None se denominador invalido."""
    try:
        n = float(numerator) if numerator is not None else None
        d = float(denominator) if denominator is not None else None
        if n is None or d is None or d == 0:
            return None
        return round(n / d, decimals)
    except (ValueError, TypeError):
        return None


def _safe_pct(part: Any, total: Any, decimals: int = 1) -> Optional[float]:
    """Percentual seguro: part/total*100, None se invalido."""
    r = _safe_ratio(part, total, decimals + 2)
    if r is None:
        return None
    return round(r * 100, decimals)


def _compute_tech_score(row: Dict[str, Any]) -> Optional[float]:
    """Score tech normalizado 0-10.

    Componentes (peso igual):
    - Internet flags (3): in_internet, in_internet_alunos, in_internet_aprendizagem → 0-3
    - Lab informatica (1): in_laboratorio_informatica → 0-1
    - Devices (3): qt_desktop_aluno, qt_comp_portatil_aluno, qt_tablet_aluno
      → score 0-3 baseado em presenca (>0 = 1 ponto cada)
    - Soma 0-7, normalizada para 0-10 (× 10/7)
    """
    flags = [
        row.get("in_internet"),
        row.get("in_internet_alunos"),
        row.get("in_internet_aprendizagem"),
        row.get("in_laboratorio_informatica"),
    ]
    score_flags = sum(1 for f in flags if f is True)

    devices = [
        row.get("qt_desktop_aluno"),
        row.get("qt_comp_portatil_aluno"),
        row.get("qt_tablet_aluno"),
    ]
    score_devices = sum(1 for d in devices if d is not None and int(d) > 0)

    raw = score_flags + score_devices  # 0-7
    if raw == 0 and all(f is None for f in flags) and all(d is None for d in devices):
        return None  # sem dados
    return round(raw * 10 / 7, 1)


def _compute_infra_score(row: Dict[str, Any]) -> Optional[int]:
    """Score infra 0-4: conta presenca de biblioteca, quadra, lab_ciencias, alimentacao."""
    flags = [
        row.get("in_biblioteca"),
        row.get("in_quadra_esportes"),
        row.get("in_laboratorio_ciencias"),
        row.get("in_alimentacao"),
    ]
    if all(f is None for f in flags):
        return None
    return sum(1 for f in flags if f is True)


def _compute_derived_metrics(row: Dict[str, Any]) -> Dict[str, Any]:
    """Computa metricas derivadas para 1 ano do censo. Campos prefixados com _."""
    derived: Dict[str, Any] = {}

    # Razoes aluno/professor
    derived["_alunos_por_docente"] = _safe_ratio(
        row.get("qt_mat_bas"), row.get("qt_doc_bas")
    )
    derived["_alunos_por_docente_fund"] = _safe_ratio(
        row.get("qt_mat_fund"), row.get("qt_doc_fund")
    )
    derived["_alunos_por_docente_med"] = _safe_ratio(
        row.get("qt_mat_med"), row.get("qt_doc_med")
    )

    # Composicao de matriculas
    derived["_pct_mat_medio"] = _safe_pct(
        row.get("qt_mat_med"), row.get("qt_mat_bas")
    )
    derived["_pct_mat_fund_af"] = _safe_pct(
        row.get("qt_mat_fund_af"), row.get("qt_mat_bas")
    )
    derived["_pct_mat_eja"] = _safe_pct(
        row.get("qt_mat_eja"), row.get("qt_mat_bas")
    )

    # Scores compostos
    derived["_tech_score"] = _compute_tech_score(row)
    derived["_infra_score"] = _compute_infra_score(row)

    return derived


def _detectar_insights(
    trends: Dict[str, Optional[Dict[str, Any]]],
    censo_series: List[Dict[str, Any]],
    enem_data: Optional[Dict[str, Any]] = None,
    benchmark_data: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Detecta correlacoes e padroes nas trends + dados ENEM.

    Retorna lista de textos observacionais.
    Formulacao SEMPRE observacional (o que aconteceu), NUNCA causal (por que).

    Insights ENEM tem prioridade e aparecem primeiro na lista.

    Args:
        trends: Dict com trends do Censo (ratio, tech, mat, etc).
        censo_series: Serie historica do Censo.
        enem_data: Dict com dados school_analytics (opcional). Campos usados:
            enem_media_geral, enem_media_cn/ch/lc/mt/redacao,
            enem_gap_vs_peer_2024, enem_area_mais_fraca,
            enem_pct_acima_600, enem_amostra_confiavel.
        benchmark_data: Dict com medias do benchmark/peer group (opcional). Campos:
            media_cn, media_ch, media_lc, media_mt, media_redacao, media_geral.
    """
    insights_enem: List[str] = []
    insights_censo: List[str] = []

    # =====================================================================
    # INSIGHTS ENEM (prioridade alta — relevancia comercial direta)
    # =====================================================================
    if enem_data and enem_data.get("enem_amostra_confiavel"):
        _AREA_NAMES = {
            "cn": "Ciências da Natureza",
            "ch": "Ciências Humanas",
            "lc": "Linguagens",
            "mt": "Matemática",
            "redacao": "Redação",
        }

        # Gap geral vs peer
        gap_geral = enem_data.get("enem_gap_vs_peer_2025")
        if gap_geral is not None and gap_geral < -15:
            insights_enem.append(
                f"Performance geral {abs(int(gap_geral))} pontos abaixo da média "
                f"das escolas similares — indica espaço significativo para melhoria "
                f"com intervenções pedagógicas direcionadas."
            )

        # Areas abaixo da media das escolas similares (se temos benchmark)
        if benchmark_data:
            gaps_por_area: List[tuple] = []
            for key, label in _AREA_NAMES.items():
                escola_val = enem_data.get(f"enem_media_{key}")
                bench_val = benchmark_data.get(f"media_{key}")
                if escola_val and bench_val and bench_val > 0:
                    gap = escola_val - bench_val
                    if gap < -10:
                        gaps_por_area.append((label, int(gap), int(escola_val), int(bench_val)))

            # Ordenar por gap (mais negativo primeiro)
            gaps_por_area.sort(key=lambda x: x[1])

            if len(gaps_por_area) >= 3:
                areas_texto = ", ".join(f"{g[0]} ({g[1]:+d} pts)" for g in gaps_por_area[:3])
                insights_enem.append(
                    f"Três ou mais áreas do ENEM abaixo da média das escolas similares: {areas_texto}. "
                    f"Um plano de reforço integrado pode ter impacto rápido."
                )
            elif gaps_por_area:
                for label, gap, escola_val, bench_val in gaps_por_area[:2]:
                    insights_enem.append(
                        f"{label}: {abs(gap)} pontos abaixo da média das escolas "
                        f"similares ({escola_val} vs {bench_val}) — área com "
                        f"potencial de ganho rápido com exercícios focados."
                    )

        # Redação fraca (sinal forte — pais e gestores prestam muita atenção)
        redacao = enem_data.get("enem_media_redacao")
        bench_redacao = benchmark_data.get("media_redacao") if benchmark_data else None
        if redacao and bench_redacao and (redacao - bench_redacao) < -30:
            gap_red = int(redacao - bench_redacao)
            insights_enem.append(
                f"Redação {abs(gap_red)} pontos abaixo do esperado para escolas deste perfil "
                f"({int(redacao)} vs {int(bench_redacao)}) — competência textual é "
                f"sensível para famílias e impacta diretamente o score geral."
            )

        # Percentual baixo acima de 600
        pct_600_raw = enem_data.get("enem_pct_acima_600")
        if pct_600_raw is not None:
            # Normalizar: se valor < 1, esta em formato decimal (0.72 = 72%)
            pct_600 = float(pct_600_raw) * 100 if float(pct_600_raw) <= 1 else float(pct_600_raw)

            if pct_600 < 30:
                if pct_600 < 1:
                    pct_texto = "Nenhum dos alunos atingiu 600 pontos no ENEM"
                else:
                    pct_texto = f"Apenas {pct_600:.0f}% dos alunos ultrapassaram 600 pontos no ENEM"
                insights_enem.append(
                    f"{pct_texto} — exercícios adaptativos podem elevar essa faixa, "
                    f"melhorando o posicionamento da escola nos rankings."
                )

    # =====================================================================
    # INSIGHT SUTIL PARA ESCOLAS BOAS: area menos forte como oportunidade
    # So ativa quando NAO ha nenhum insight negativo ENEM (escola boa)
    # =====================================================================
    if not insights_enem and enem_data and benchmark_data and enem_data.get("enem_amostra_confiavel"):
        _AREA_NAMES_SUTIL = {
            "cn": "Ciências da Natureza",
            "ch": "Ciências Humanas",
            "lc": "Linguagens",
            "mt": "Matemática",
            "redacao": "Redação",
        }
        area_gaps = []
        for key, label in _AREA_NAMES_SUTIL.items():
            escola_val = enem_data.get(f"enem_media_{key}")
            bench_val = benchmark_data.get(f"media_{key}")
            if escola_val and bench_val and float(bench_val) > 0:
                gap = float(escola_val) - float(bench_val)
                area_gaps.append((label, gap, int(float(escola_val))))

        if len(area_gaps) >= 3:
            area_gaps.sort(key=lambda x: x[1])  # menor vantagem primeiro
            menor = area_gaps[0]
            maior = area_gaps[-1]

            if menor[1] >= 0:  # todas acima do benchmark = escola boa
                insights_enem.append(
                    f"A escola está acima da média em todas as áreas, mas "
                    f"{menor[0]} tem a menor margem ({menor[1]:+.0f} pts). "
                    f"Fortalecer essa área pode consolidar a escola entre "
                    f"as melhores da região."
                )

    # =====================================================================
    # INSIGHTS CRITICOS DE MATRICULAS (prioridade MAXIMA — antes de ENEM)
    # Queda significativa de matriculas e o sinal mais forte de urgencia
    # =====================================================================
    insights_criticos: List[str] = []

    def _t(key: str) -> Optional[Dict]:
        return trends.get(key)

    def _delta(t: Optional[Dict]) -> Optional[float]:
        return t.get("delta_total_pct") if t else None

    t_mat_check = _t("matriculas_bas")
    if t_mat_check:
        d_mat_total = _delta(t_mat_check)
        first_mat = t_mat_check.get("primeiro_valor")
        last_mat = t_mat_check.get("ultimo_valor")
        if d_mat_total is not None and first_mat and last_mat:
            if d_mat_total < -15:
                insights_criticos.append(
                    f"Matrículas em queda acentuada: de {int(first_mat)} para "
                    f"{int(last_mat)} alunos ({d_mat_total:+.1f}%) nos últimos anos "
                    f"— famílias estão deixando a escola, e cada ano a base diminui. "
                    f"Investir em qualidade pedagógica visível é a forma mais direta "
                    f"de reverter essa tendência."
                )
            elif d_mat_total < -5:
                insights_criticos.append(
                    f"Matrículas em queda moderada: de {int(first_mat)} para "
                    f"{int(last_mat)} alunos ({d_mat_total:+.1f}%) — tendência que "
                    f"merece atenção para não se agravar."
                )

    # =====================================================================
    # INSIGHTS CENSO (complementares — prioridade menor)
    # =====================================================================

    def _first(t: Optional[Dict]) -> Optional[float]:
        return t.get("primeiro_valor") if t else None

    def _last(t: Optional[Dict]) -> Optional[float]:
        return t.get("ultimo_valor") if t else None

    # 1. Ratio vs enrollment
    t_ratio = _t("alunos_por_docente")
    t_mat = _t("matriculas_bas")
    t_doc = _t("docentes")
    if t_ratio and t_mat:
        d_ratio = _delta(t_ratio)
        d_mat = _delta(t_mat)
        if d_ratio is not None and d_mat is not None:
            first_r = _first(t_ratio)
            last_r = _last(t_ratio)
            if first_r and last_r:
                if d_ratio < -5 and d_mat > 0:
                    insights_censo.append(
                        f"A relação aluno/professor melhorou (de {first_r:.1f}:1 para "
                        f"{last_r:.1f}:1) enquanto as matrículas cresceram {d_mat:+.1f}% "
                        f"— a escola contratou docentes em ritmo maior que o crescimento de alunos."
                    )
                elif d_ratio > 5 and d_mat > 0:
                    insights_censo.append(
                        f"As matrículas cresceram {d_mat:+.1f}% mas a relação aluno/professor "
                        f"piorou (de {first_r:.1f}:1 para {last_r:.1f}:1) — o crescimento "
                        f"de alunos superou a contratação de docentes."
                    )
                elif d_ratio < -5 and d_mat < -3:
                    insights_censo.append(
                        f"A relação aluno/professor melhorou (de {first_r:.1f}:1 para "
                        f"{last_r:.1f}:1) mas com matrículas em queda ({d_mat:+.1f}%) — "
                        f"a melhora pode ser por perda de alunos, não por investimento em equipe."
                    )

    # 2. Tech transformation
    t_tech = _t("tech_score")
    if t_tech:
        d_tech = _delta(t_tech)
        first_tech = _first(t_tech)
        last_tech = _last(t_tech)
        if d_tech is not None and first_tech is not None and last_tech is not None:
            if d_tech > 30 and first_tech < 4:
                insights_censo.append(
                    f"Salto tecnológico: tech score subiu de {first_tech:.1f} para "
                    f"{last_tech:.1f} (escala 0-10) — possível transformação digital no período."
                )
            elif last_tech >= 8:
                insights_censo.append(
                    f"Escola com bom nível tecnológico (score {last_tech:.1f}/10)."
                )

    # 3. Enrollment composition shift
    t_pct_med = _t("pct_matriculas_medio")
    t_pct_fund = _t("matriculas_fund_af")
    if t_pct_med:
        d_pct_med = _delta(t_pct_med)
        first_pct = _first(t_pct_med)
        last_pct = _last(t_pct_med)
        if d_pct_med is not None and first_pct is not None and last_pct is not None:
            if d_pct_med > 15:
                insights_censo.append(
                    f"Mudança de perfil: % de matrículas no Ensino Médio subiu de "
                    f"{first_pct:.1f}% para {last_pct:.1f}% do total — crescimento "
                    f"relativo do Médio frente a outras etapas."
                )

    # 4. Infra improvement — REMOVIDO: infraestrutura fisica nao tem correlacao
    # com a proposta do IAprendo (exercicios adaptativos). Incluir esse insight
    # forcaria uma conexao que nao existe e enfraquece a argumentacao.

    # 5. Docente trend standalone
    if t_doc:
        d_doc = _delta(t_doc)
        d_mat_val = _delta(t_mat) if t_mat else None
        if d_doc is not None and d_doc < -10 and (d_mat_val is None or d_mat_val > -3):
            insights_censo.append(
                f"Corpo docente encolheu {d_doc:+.1f}% no período"
                + (f" enquanto matrículas variaram apenas {d_mat_val:+.1f}%." if d_mat_val is not None else ".")
                + " Possível sinal de pressão financeira ou reestruturação."
            )

    # Ordem de prioridade: CRITICOS (matriculas) > ENEM > CENSO
    return insights_criticos + insights_enem + insights_censo


def _interpretar_trend_numerico(valores: List[Optional[float]], vintages: List[int]) -> Optional[Dict[str, Any]]:
    """Dada uma serie de valores (pode ter Nones), retorna uma leitura
    qualitativa da trajetoria. NAO inventa — so sumariza o que esta lah.

    Retorna None se menos de 2 valores nao-nulos.
    """
    pairs = [(v, n) for v, n in zip(vintages, valores) if n is not None]
    if len(pairs) < 2:
        return None

    primeiro = pairs[0]
    ultimo = pairs[-1]
    v_first, n_first = primeiro
    v_last, n_last = ultimo
    if n_first == 0:
        delta_pct = None
    else:
        delta_pct = round((float(n_last) - float(n_first)) / float(n_first) * 100, 1)

    # Recente = ultimos 2 pontos disponiveis
    recente = None
    if len(pairs) >= 2:
        v_prev, n_prev = pairs[-2]
        if n_prev and n_prev != 0:
            recente = round((float(n_last) - float(n_prev)) / float(n_prev) * 100, 1)

    return {
        "primeiro_ano": v_first,
        "primeiro_valor": float(n_first),
        "ultimo_ano": v_last,
        "ultimo_valor": float(n_last),
        "delta_total_pct": delta_pct,
        "delta_recente_pct": recente,
        "n_pontos": len(pairs),
    }


def get_insight_ingredients(inep: str) -> Dict[str, Any]:
    """Retorna trends + censo_series + enem_data necessarios para _detectar_insights.

    Helper publico para consumidores que precisam regenerar insights com
    benchmarks diferentes (ex: OPR interativo com switcher de benchmark).

    Returns:
        Dict com keys: 'trends', 'censo_series', 'enem_data'.
        Se falhar, retorna dict vazio.
    """
    try:
        censo_data = db.client.table("school_censo_yearly").select("*").eq(
            "inep_code", str(inep)
        ).order("vintage_censo").execute()
        censo_series_raw = censo_data.data or []
        if not censo_series_raw:
            return {"trends": {}, "censo_series": [], "enem_data": None}

        censo_vintages = sorted([r.get("vintage_censo") for r in censo_series_raw if r.get("vintage_censo")])
        censo_clean: List[Dict] = []
        for row in censo_series_raw:
            clean = {k: v for k, v in row.items() if v is not None}
            derived = _compute_derived_metrics(clean)
            clean.update({k: v for k, v in derived.items() if v is not None})
            censo_clean.append(clean)

        def _col(col):
            return [r.get(col) for r in censo_clean]

        trends_dict = {
            "matriculas_bas": _interpretar_trend_numerico(_col("qt_mat_bas"), censo_vintages) if censo_clean else None,
            "matriculas_medio": _interpretar_trend_numerico(_col("qt_mat_med"), censo_vintages) if censo_clean else None,
            "matriculas_fund_af": _interpretar_trend_numerico(_col("qt_mat_fund_af"), censo_vintages) if censo_clean else None,
            "docentes": _interpretar_trend_numerico(_col("qt_doc_bas"), censo_vintages) if censo_clean else None,
            "alunos_por_docente": _interpretar_trend_numerico(_col("_alunos_por_docente"), censo_vintages) if censo_clean else None,
            "alunos_por_docente_fund": _interpretar_trend_numerico(_col("_alunos_por_docente_fund"), censo_vintages) if censo_clean else None,
            "alunos_por_docente_med": _interpretar_trend_numerico(_col("_alunos_por_docente_med"), censo_vintages) if censo_clean else None,
            "tech_score": _interpretar_trend_numerico(_col("_tech_score"), censo_vintages) if censo_clean else None,
            "infra_score": _interpretar_trend_numerico(_col("_infra_score"), censo_vintages) if censo_clean else None,
            "pct_matriculas_medio": _interpretar_trend_numerico(_col("_pct_mat_medio"), censo_vintages) if censo_clean else None,
        }

        enem_data = _fetch_school_analytics_by_inep(str(inep))

        return {
            "trends": trends_dict,
            "censo_series": censo_clean,
            "enem_data": enem_data,
        }
    except Exception as e:
        logger.debug(f"get_insight_ingredients failed for {inep}: {e}")
        return {"trends": {}, "censo_series": [], "enem_data": None}


def _handle_analisar_trajetoria_escola(params: Dict) -> str:
    """Retorna a serie historica individual (Censo + ENEM) de UMA escola.

    Use esta tool quando Fernando perguntar sobre evolucao, historico,
    tendencia, trajetoria, ou crescimento de uma escola especifica.
    Distingue automaticamente:
    - Serie Censo 2020-2025 (matriculas, equipe, tech, infra) — disponivel
      para todas as escolas que participaram do Censo naquele ano
    - Serie ENEM (hoje so 2024, cresce a cada ano novo) — sujeita ao gate
      amostra_confiavel
    """
    inep = params.get("inep")
    nome = params.get("escola_nome") or params.get("nome")
    escola_id = params.get("escola_id")

    # Resolver inep a partir de qualquer input
    company = None
    if inep:
        company = _fetch_company_by_inep(str(inep))
    elif escola_id:
        try:
            r = db.client.table("companies").select(
                "id,inep_code,name,city,state,admin_dependency,school_size,bairro"
            ).eq("id", escola_id).limit(1).execute()
            if r.data:
                company = r.data[0]
                inep = company.get("inep_code")
        except Exception as e:
            logger.warning(f"trajetoria by id failed: {e}")
    elif nome:
        # CUIDADO: multiplas escolas podem bater com o mesmo nome (ex:
        # "COLEGIO JOAO PAULO" aparece 3 vezes em PoA). Se houver mais
        # de 1 match, retornar lista de desambiguacao em vez de escolher
        # silenciosamente.
        try:
            r = db.client.table("companies").select(
                "id,inep_code,name,city,state,admin_dependency,school_size,bairro"
            ).ilike("name", f"%{nome}%").limit(10).execute()
            matches = r.data or []

            if len(matches) == 0:
                return json.dumps({
                    "erro": f"Nenhuma escola encontrada com nome contendo '{nome}' no CRM.",
                }, ensure_ascii=False)

            if len(matches) > 1:
                # Ambiguidade: pedir clarificacao ao LLM
                return json.dumps({
                    "ambiguidade": True,
                    "query_original": nome,
                    "n_matches": len(matches),
                    "escolas_encontradas": [
                        {
                            "inep": m.get("inep_code"),
                            "nome": m.get("name"),
                            "cidade": m.get("city"),
                            "bairro": m.get("bairro"),
                            "uf": m.get("state"),
                            "dependencia": m.get("admin_dependency"),
                            "porte": m.get("school_size"),
                        }
                        for m in matches
                    ],
                    "orientacao": (
                        f"Encontrei {len(matches)} escolas no CRM com '{nome}' no nome. "
                        "NAO escolha silenciosamente — apresente a lista ao Fernando "
                        "(incluindo cidade e bairro para diferenciar) e pergunte qual "
                        "delas ele quer. Quando ele responder, chame esta tool de novo "
                        "passando o parametro `inep` especifico (nao por nome), para "
                        "garantir que voce esta olhando a escola certa."
                    ),
                }, ensure_ascii=False)

            # Exatamente 1 match — seguir fluxo normal
            company = matches[0]
            inep = company.get("inep_code")
        except Exception as e:
            logger.warning(f"trajetoria by name failed: {e}")

    if not inep:
        return json.dumps({"erro": (
            "Escola nao encontrada. Forneca inep, escola_nome (para match em "
            "companies) ou escola_id."
        )})

    # Buscar series
    censo_series = _fetch_censo_series(str(inep))
    enem_series = _fetch_enem_series(str(inep))

    if not censo_series and not enem_series:
        return json.dumps({
            "escola": (company or {}).get("name") or f"INEP {inep}",
            "inep": str(inep),
            "erro": (
                "Nenhuma serie historica encontrada para esta escola — nem no "
                "Censo Escolar nem no ENEM. Pode ser uma escola muito nova ou "
                "que nao participou de nenhum dos dois levantamentos."
            ),
        }, ensure_ascii=False, default=str)

    # Extrair vintages disponiveis
    censo_vintages = sorted([row.get("vintage_censo") for row in censo_series if row.get("vintage_censo")])
    enem_vintages = sorted([row.get("vintage_enem") for row in enem_series if row.get("vintage_enem")])

    # Montar serie limpa do Censo + metricas derivadas (ANTES dos trends,
    # porque os trends de derivadas precisam dos campos _* ja computados)
    censo_clean = []
    for row in censo_series:
        clean = {k: v for k, v in row.items() if v is not None}
        derived = _compute_derived_metrics(clean)
        clean.update({k: v for k, v in derived.items() if v is not None})
        censo_clean.append(clean)

    # Interpretacao numerica de trends em metricas chave
    def _col(col):
        return [r.get(col) for r in censo_clean]

    trend_total = _interpretar_trend_numerico(
        _col("qt_mat_bas"), censo_vintages
    ) if censo_clean else None
    trend_medio = _interpretar_trend_numerico(
        _col("qt_mat_med"), censo_vintages
    ) if censo_clean else None
    trend_fund_af = _interpretar_trend_numerico(
        _col("qt_mat_fund_af"), censo_vintages
    ) if censo_clean else None
    trend_docentes = _interpretar_trend_numerico(
        _col("qt_doc_bas"), censo_vintages
    ) if censo_clean else None

    # Trends para metricas derivadas
    trend_ratio = _interpretar_trend_numerico(
        _col("_alunos_por_docente"), censo_vintages
    ) if censo_clean else None
    trend_ratio_fund = _interpretar_trend_numerico(
        _col("_alunos_por_docente_fund"), censo_vintages
    ) if censo_clean else None
    trend_ratio_med = _interpretar_trend_numerico(
        _col("_alunos_por_docente_med"), censo_vintages
    ) if censo_clean else None
    trend_tech = _interpretar_trend_numerico(
        _col("_tech_score"), censo_vintages
    ) if censo_clean else None
    trend_infra = _interpretar_trend_numerico(
        _col("_infra_score"), censo_vintages
    ) if censo_clean else None
    trend_pct_med = _interpretar_trend_numerico(
        _col("_pct_mat_medio"), censo_vintages
    ) if censo_clean else None

    # Serie ENEM ja veio com gate aplicado
    enem_clean = []
    for row in enem_series:
        clean = {k: v for k, v in row.items() if v is not None}
        enem_clean.append(clean)

    # Disclaimer quando algum ano do ENEM tem amostra nao confiavel
    enem_warnings = []
    for row in enem_series:
        if row.get("enem_amostra_confiavel") is False:
            v = row.get("vintage_enem")
            enem_warnings.append(
                f"ENEM {v}: amostra nao confiavel — metricas individuais "
                f"foram omitidas desta vintage (regra #1)."
            )

    output: Dict[str, Any] = {
        "escola": (company or {}).get("name") or f"INEP {inep}",
        "inep": str(inep),
        "cidade": (company or {}).get("city"),
        "estado": (company or {}).get("state"),
        "dependencia": (company or {}).get("admin_dependency"),
        "censo": {
            "vintages_disponiveis": censo_vintages,
            "n_vintages": len(censo_vintages),
            "serie": censo_clean,
            "trends": {
                "matriculas_bas": trend_total,
                "matriculas_medio": trend_medio,
                "matriculas_fund_af": trend_fund_af,
                "docentes": trend_docentes,
                "alunos_por_docente": trend_ratio,
                "alunos_por_docente_fund": trend_ratio_fund,
                "alunos_por_docente_med": trend_ratio_med,
                "tech_score": trend_tech,
                "infra_score": trend_infra,
                "pct_matriculas_medio": trend_pct_med,
            },
        },
        "enem": {
            "vintages_disponiveis": enem_vintages,
            "n_vintages": len(enem_vintages),
            "serie": enem_clean,
            "warnings": enem_warnings,
            "nota_historica": (
                "Serie ENEM individual so existe a partir de 2024 — anos "
                "anteriores foram anonimizados pela INEP (microdados publicos "
                "sem CO_ESCOLA). A serie cresce a cada ENEM novo."
            ),
        },
        "insights_detectados": _detectar_insights(
            {
                "matriculas_bas": trend_total,
                "matriculas_medio": trend_medio,
                "matriculas_fund_af": trend_fund_af,
                "docentes": trend_docentes,
                "alunos_por_docente": trend_ratio,
                "alunos_por_docente_fund": trend_ratio_fund,
                "alunos_por_docente_med": trend_ratio_med,
                "tech_score": trend_tech,
                "infra_score": trend_infra,
                "pct_matriculas_medio": trend_pct_med,
            },
            censo_clean,
            enem_data=_fetch_school_analytics_by_inep(str(inep)),
            benchmark_data=_fetch_benchmark_for_insights(str(inep)),
        ),
        "orientacao_para_o_pitch": (
            "A serie inclui metricas derivadas por ano (_alunos_por_docente, "
            "_tech_score, _infra_score, _pct_mat_medio, etc.) ja calculadas "
            "em Python e trends para TODAS elas. O campo 'insights_detectados' "
            "traz correlacoes pre-identificadas pelo servidor — cite-as com "
            "confianca (vieram no payload). Voce PODE e DEVE raciocinar "
            "sobre os dados: cruzar metricas, identificar padroes, narrar "
            "evolucoes. Mas NAO invente dados que nao estao no payload e NAO "
            "atribua causalidade sem evidencia direta. Contexto (pandemia, "
            "mudanca de gestao, etc.) e desconhecido para voce — cabe ao "
            "Fernando enriquecer a interpretacao."
        ),
    }

    logger.info("enem_tool_called", extra={
        "tool": "analisar_trajetoria_escola",
        "inep": str(inep),
        "censo_vintages": len(censo_vintages),
        "enem_vintages": len(enem_vintages),
    })

    return json.dumps(output, ensure_ascii=False, default=str)


# ===========================================================================
# HANDLER 6: ranking_evolucao_enem (evolucao da nota entre anos, cross-escola)
# ===========================================================================

# UF (sigla) -> codigo IBGE = 2 primeiros digitos do INEP da escola (43=RS).
_UF_TO_IBGE = {
    "RO": "11", "AC": "12", "AM": "13", "RR": "14", "PA": "15", "AP": "16", "TO": "17",
    "MA": "21", "PI": "22", "CE": "23", "RN": "24", "PB": "25", "PE": "26", "AL": "27",
    "SE": "28", "BA": "29", "MG": "31", "ES": "32", "RJ": "33", "SP": "35",
    "PR": "41", "SC": "42", "RS": "43", "MS": "50", "MT": "51", "GO": "52", "DF": "53",
}

# area -> coluna de media em school_enem_yearly (aceita sinonimos pt).
_EVOL_AREA_COL = {
    "geral": "enem_media_geral", "media_geral": "enem_media_geral",
    "geral_sem_redacao": "enem_media_geral_sem_redacao",
    "sem_redacao": "enem_media_geral_sem_redacao",
    "redacao": "enem_media_redacao",
    "mt": "enem_media_mt", "matematica": "enem_media_mt", "matemática": "enem_media_mt",
    "lc": "enem_media_lc", "linguagens": "enem_media_lc",
    "ch": "enem_media_ch", "humanas": "enem_media_ch",
    "cn": "enem_media_cn", "naturais": "enem_media_cn",
}


def _handle_ranking_evolucao_enem(params: Dict) -> str:
    """Ranking de EVOLUCAO da nota ENEM entre dois anos, cruzando escolas.

    Responde "qual escola mais evoluiu/caiu em <area> de <ano> p/ <ano> em
    <regiao>". Le `school_enem_yearly` (serie por escola por ano — unica fonte
    com a nota PROPRIA da escola por ano). So conta escolas com amostra ENEM
    confiavel nos DOIS anos (delta de amostra fraca = ruido). NAO usa peer_delta_*
    (esses sao do grupo/peer, nao da escola).
    """
    area_raw = (params.get("area") or "geral").strip().lower()
    area_col = _EVOL_AREA_COL.get(area_raw)
    if not area_col:
        return json.dumps({
            "erro": f"area '{area_raw}' invalida.",
            "areas_validas": ["geral", "redacao", "geral_sem_redacao", "mt", "lc", "ch", "cn"],
        }, ensure_ascii=False)

    try:
        para_ano = int(params.get("para_ano") or ENEM_VINTAGE)
        de_ano = int(params.get("de_ano") or (para_ano - 1))
    except (ValueError, TypeError):
        return json.dumps({"erro": "de_ano/para_ano devem ser inteiros (ex: 2024, 2025)."})
    if de_ano == para_ano:
        return json.dumps({"erro": "de_ano e para_ano devem ser anos diferentes."})

    uf = ((params.get("uf") or "").strip().upper() or None)
    cidade = ((params.get("cidade") or "").strip() or None)
    dependencia = ((params.get("dependencia") or "").strip() or None)
    ordem = (params.get("ordem") or "desc").strip().lower()
    top_n = min(int(params.get("top_n", 10) or 10), 50)

    ibge = _UF_TO_IBGE.get(uf) if uf else None
    if uf and not ibge:
        return json.dumps({"erro": f"UF '{uf}' invalida.",
                           "ufs_validas": sorted(_UF_TO_IBGE)}, ensure_ascii=False)

    # Regiao: school_enem_yearly NAO tem geo. UF via prefixo INEP; cidade via
    # resolucao de INEPs no school_analytics (peer_mun_nome).
    inep_filter = None
    if cidade:
        try:
            saq = db.client.table("school_analytics").select("inep_code").ilike(
                "peer_mun_nome", f"%{cidade}%")
            if uf:
                saq = saq.eq("peer_uf_sigla", uf)
            sarows = saq.limit(MAX_ROWS_FETCH).execute().data or []
            inep_filter = [str(r["inep_code"]) for r in sarows if r.get("inep_code")]
            if not inep_filter:
                return json.dumps({"erro": f"Nenhuma escola encontrada em '{cidade}'"
                                   + (f"/{uf}" if uf else "") + "."}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"erro": f"Falha ao resolver cidade: {str(e)[:150]}"})

    sel = ("inep_code,vintage_enem,enem_dependencia,enem_amostra_confiavel,"
           "enem_presentes," + area_col)

    def _fetch_year(ano: int) -> List[Dict[str, Any]]:
        def _q():
            q = db.client.table("school_enem_yearly").select(sel).eq("vintage_enem", ano)
            q = q.eq("enem_amostra_confiavel", True).not_.is_(area_col, "null")
            q = _apply_dependencia_filter(q, dependencia)
            if ibge and inep_filter is None:
                q = q.like("inep_code", f"{ibge}%")
            return q
        rows: List[Dict[str, Any]] = []
        if inep_filter is not None:
            for i in range(0, len(inep_filter), 150):
                chunk = inep_filter[i:i + 150]
                rows.extend(_q().in_("inep_code", chunk)
                            .limit(_POSTGREST_PAGE_SIZE).execute().data or [])
            return rows
        offset = 0
        while offset < MAX_ROWS_FETCH:
            page = _q().range(offset, offset + _POSTGREST_PAGE_SIZE - 1).execute().data or []
            rows.extend(page)
            if len(page) < _POSTGREST_PAGE_SIZE:
                break
            offset += _POSTGREST_PAGE_SIZE
        return rows

    try:
        by_inep: Dict[str, Dict[int, Dict[str, Any]]] = {}
        for ano in (de_ano, para_ano):
            for r in _fetch_year(ano):
                by_inep.setdefault(str(r.get("inep_code")), {})[ano] = r
    except Exception as e:
        return json.dumps({"erro": f"Falha ao buscar serie ENEM: {str(e)[:200]}"})

    ranked: List[Dict[str, Any]] = []
    sem_dado_novo = 0
    for inep, anos in by_inep.items():
        ra, rp = anos.get(de_ano), anos.get(para_ano)
        if not ra or not rp:
            continue  # exige os DOIS anos confiaveis
        va, vp = ra.get(area_col), rp.get(area_col)
        if va is None or vp is None:
            continue
        # Safra nova e COPIA da anterior => a escola nao teve resultado proprio.
        # Incluir daria delta 0.0 e ela apareceria como "estagnada" num ranking
        # de evolucao — conclusao errada. Fora do ranking, contada a parte.
        if _mesma_medicao(ra, rp):
            sem_dado_novo += 1
            continue
        ranked.append({
            "inep": inep,
            "de_valor": round(float(va), 1),
            "para_valor": round(float(vp), 1),
            "delta": round(float(vp) - float(va), 1),
            "dependencia": rp.get("enem_dependencia") or ra.get("enem_dependencia"),
            "presentes_de": ra.get("enem_presentes"),
            "presentes_para": rp.get("enem_presentes"),
        })

    if not ranked:
        return json.dumps({
            "erro": f"Nenhuma escola com amostra ENEM confiavel em {de_ano} E "
                    f"{para_ano} com esses filtros.",
            "filtros": {"area": area_raw, "uf": uf, "cidade": cidade,
                        "dependencia": dependencia, "de_ano": de_ano, "para_ano": para_ano},
        }, ensure_ascii=False)

    ranked.sort(key=lambda d: d["delta"], reverse=(ordem != "asc"))
    top = ranked[:top_n]

    nomes = _resolve_school_names([d["inep"] for d in top])
    for d in top:
        info = nomes.get(d["inep"], {})
        d["nome"] = info.get("name") or f"Escola INEP {d['inep']}"
        d["cidade"] = info.get("city")
        d["uf"] = info.get("state")

    logger.info("enem_tool_called", extra={
        "tool": "ranking_evolucao_enem", "area": area_col,
        "de_ano": de_ano, "para_ano": para_ano, "n_consideradas": len(ranked),
        "sem_dado_novo": sem_dado_novo,
    })

    saida = {
        "area": area_raw, "metrica": area_col,
        "de_ano": de_ano, "para_ano": para_ano,
        "ordem": "maior_evolucao" if ordem != "asc" else "maior_queda",
        "n_consideradas": len(ranked),
        "filtros": {"uf": uf, "cidade": cidade, "dependencia": dependencia},
        "nota": (f"delta = nota {para_ano} - nota {de_ano}. So entram escolas com "
                 f"amostra ENEM confiavel nos DOIS anos."),
        "resultado": top,
    }
    if sem_dado_novo:
        saida["excluidas_sem_dado_novo"] = sem_dado_novo
        saida["nota_exclusao"] = (
            f"{sem_dado_novo} escola(s) ficaram FORA do ranking porque os numeros "
            f"de {para_ano} repetem exatamente os de {de_ano} — ou seja, nao houve "
            f"resultado proprio na safra nova (amostra insuficiente). Elas nao "
            f"'estagnaram': nao ha o que comparar."
        )
    return json.dumps(saida, ensure_ascii=False, default=str)


# ===========================================================================
# TOOL SPECS (Anthropic format, converted to OpenAI by brain.py)
# ===========================================================================

ENEM_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "analisar_performance_escola",
        "description": (
            f"Retorna snapshot de performance ENEM {ENEM_VINTAGE} de UMA escola: media geral "
            "(com/sem redacao), ranking, gap vs peer group, area mais fraca, "
            f"trajetoria do peer group 2020-{ENEM_VINTAGE}, contexto socioeconomico municipal, "
            "e prioridade sugerida (P1/P2/P3). Aplica automaticamente o gate "
            "enem_amostra_confiavel — se FALSE, omite rankings individuais. "
            "Use ANTES de gerar email para escolas com Medio."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "inep": {"type": "string", "description": "Codigo INEP da escola (preferido)"},
                "escola_nome": {"type": "string", "description": "Nome da escola (fuzzy match em companies)"},
                "escola_id": {"type": "string", "description": "UUID da escola em companies"},
            },
        },
    },
    {
        "name": "priorizar_leads_enem",
        "description": (
            "Retorna ranking P1/P2/P3 de leads por temperatura baseado em dados ENEM. "
            "P1: potencial=Alto + peer Subindo + presentes>=30 (quente ofensivo). "
            "P2: privada com gap<-10 + peer Subindo + presentes>=20 (oportunidade). "
            "P3: privada com peer Caindo forte + delta 2022-24 < -15 + presentes>=20 "
            "(URGENCIA DEFENSIVA, vem com aviso para revisao de tom). "
            "Use quando Fernando pedir 'leads quentes', 'onde prospectar', 'top "
            "oportunidades', 'ranking de leads'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "municipio": {"type": "string", "description": "Filtrar por municipio (fuzzy)"},
                "uf": {"type": "string", "description": "Filtrar por UF (ex: RS)"},
                "dependencia": {"type": "string", "description": "Privada / Estadual / Federal / Municipal"},
                "prioridade": {"type": "string", "enum": ["P1", "P2", "P3"], "description": "Filtrar so uma prioridade"},
                "limite": {"type": "integer", "description": "Max leads (default 30, max 100)"},
            },
        },
    },
    {
        "name": "buscar_escolas_por_enem",
        "description": (
            "Busca escolas por filtros analiticos ENEM: area fraca (ex: 'Matematica'), "
            "potencial de melhoria, trajetoria do peer group, gap maximo vs peer, "
            "dependencia, UF, cidade. SO retorna escolas com nota ENEM real e "
            "amostra confiavel (escolas sem ENEM — infantil, EJA, sem participantes "
            "— nunca entram). ORDENACAO: com filtro de oportunidade (area_fraca/"
            "potencial/trajetoria/gap_max) ordena por GAP asc (leads mais abaixo do "
            "peer); sem esses filtros ordena por MEDIA desc (melhores notas). Para "
            "forcar, use ordenar_por='media' (melhores) / 'gap' (leads). Para "
            "ranking por nota tambem serve analisar_dados_analytics (operacao=ranking). "
            "Use para investigacoes direcionadas ('me da escolas privadas em Canoas "
            "com potencial alto e area fraca em matematica')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ordenar_por": {"type": "string", "enum": ["media", "media_sem_redacao", "gap"], "description": "Forca a ordenacao: 'media' (melhores notas primeiro), 'media_sem_redacao', ou 'gap' (leads mais abaixo do peer). Se omitido: media desc sem filtro de oportunidade, gap asc com filtro."},
                "area_fraca": {"type": "string", "description": "Texto da area mais fraca (ex: 'Matematica', 'Ciencias')"},
                "potencial": {"type": "string", "enum": ["Alto", "Medio", "Baixo"]},
                "trajetoria": {"type": "array", "items": {"type": "string"}, "description": "Lista de trajetorias ['Subindo','Subindo forte','Estavel','Caindo','Caindo forte']"},
                "gap_max": {"type": "number", "description": "Gap maximo vs peer (ex: -10 retorna escolas com gap < -10)"},
                "uf": {"type": "string"},
                "cidade": {"type": "string"},
                "dependencia": {"type": "string"},
                "only_confiavel": {"type": "boolean", "description": "Compat: esta busca SEMPRE retorna apenas escolas com nota ENEM real e amostra confiavel (escolas sem ENEM — infantil, EJA, sem participantes — nunca aparecem)."},
                "limite": {"type": "integer", "description": "Max resultados (default 20, max 100)"},
            },
        },
    },
    {
        "name": "analisar_dados_analytics",
        "description": (
            "Query builder FLEXIVEL para perguntas abertas sobre dados ENEM/peer/socio. "
            "Use quando Fernando pergunta coisas que nao cabem nas 3 tools narrow acima: "
            "'qual a media de matematica da escola X nos ultimos 3 anos', 'top 10 privadas "
            "em RS por media ENEM', 'compara escola X com privadas do mesmo porte', "
            "'distribuicao de potencial por cidade'. Operacoes suportadas: valor_unico, "
            "ranking, comparacao, serie_temporal, distribuicao. Metricas SO da whitelist. "
            "Valida whitelist e aplica gates eticos (amostra_confiavel, socio_rotulo, "
            "pnt_blocked). Erro amigavel se campo invalido."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "operacao": {
                    "type": "string",
                    "enum": ["valor_unico", "ranking", "comparacao", "serie_temporal", "distribuicao"],
                    "description": "Tipo de query. valor_unico=1 valor; ranking=top N; comparacao=alvo vs referencias; serie_temporal=campos *_YYYY; distribuicao=por grupo.",
                },
                "alvo": {
                    "type": "object",
                    "description": "Alvo da query. escala=[escola,municipio,estado,brasil,custom]. filtros=dict com nome/inep/uf/municipio/dependencia/potencial/trajetoria_peer/area_fraca/amostra_confiavel.",
                    "properties": {
                        "escala": {"type": "string", "enum": ["escola", "municipio", "estado", "brasil", "custom"]},
                        "filtros": {"type": "object"},
                    },
                },
                "metricas": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Lista de campos da whitelist. Ex: ['enem_media_geral','enem_media_mt','peer_media_geral_2025']. Campos invalidos retornam erro.",
                },
                "agregacao": {
                    "type": "string",
                    "enum": ["media", "mediana", "soma", "min", "max", "count", "p25", "p75", "p90"],
                    "description": "Aggregacao para ranking/distribuicao/comparacao. Default=media.",
                },
                "agrupar_por": {
                    "type": "string",
                    "enum": ["city", "state", "admin_dependency", "admin_category", "school_size", "nivel_tecnologico", "categoria_privada", "enem_dependencia", "enem_potencial_melhoria", "enem_area_mais_fraca", "peer_trajetoria_5y", "peer_trajetoria_6y"],
                    "description": "Obrigatorio para distribuicao.",
                },
                "comparar_com": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["municipio", "estado", "brasil", "mesma_dependencia", "mesmo_porte", "mesmo_nivel_tecnologico"]},
                    "description": "Obrigatorio para comparacao. Pode listar varias referencias.",
                },
                "anos": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Anos para serie_temporal (2020..2025). Use nomes de metricas *_YYYY.",
                },
                "modo_redacao": {
                    "type": "string",
                    "enum": ["com", "sem", "ambos"],
                    "description": "Como tratar redacao. com=oficial (media das 5 provas). sem=usa enem_media_geral_sem_redacao (4 areas sem redacao). ambos=mostra os dois.",
                },
                "ordem": {"type": "string", "enum": ["asc", "desc"]},
                "top_n": {"type": "integer", "description": "Max resultados (default 20, max 100)"},
            },
            "required": ["operacao", "alvo", "metricas"],
        },
    },
    {
        "name": "analisar_trajetoria_escola",
        "description": (
            "Retorna a SERIE HISTORICA INDIVIDUAL de UMA escola: evolucao "
            "ano a ano de matriculas (por etapa), equipe docente, tecnologia "
            "e infraestrutura, a partir do Censo Escolar 2020-2025. Tambem "
            "retorna a serie ENEM da escola por ANO (2024 E 2025 — use para "
            "COMPARAR anos da mesma escola, ex '2025 vs 2024'). Aplica o gate "
            "amostra_confiavel nas metricas ENEM individuais. "
            "Use quando Fernando perguntar sobre EVOLUCAO, HISTORICO, "
            "TENDENCIA, TRAJETORIA, CRESCIMENTO ou QUEDA de uma escola "
            "especifica ('como a escola X vem evoluindo?', 'matriculas do "
            "colegio Y nos ultimos 5 anos?', 'a escola Z esta crescendo?'). "
            "Nao use para agregacoes ou rankings — para isso use "
            "analisar_dados_analytics. A resposta inclui trends numericas "
            "(delta total e delta recente) para matriculas total, medio, "
            "fund AF e docentes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "inep": {"type": "string", "description": "Codigo INEP da escola (preferido)"},
                "escola_nome": {"type": "string", "description": "Nome da escola (busca fuzzy em companies)"},
                "escola_id": {"type": "string", "description": "UUID da escola em companies"},
            },
        },
    },
    {
        "name": "ranking_evolucao_enem",
        "description": (
            "Ranking de EVOLUCAO da nota ENEM entre dois anos, comparando a nota "
            "PROPRIA de cada escola (NAO do grupo/peer). Responde 'qual escola mais "
            "evoluiu/caiu em <area> de 2024 p/ 2025' por regiao (uf/cidade/"
            "dependencia). So conta escolas com amostra ENEM confiavel nos DOIS "
            "anos. USE SEMPRE que a pergunta for EVOLUCAO/CRESCIMENTO/QUEDA da nota "
            "entre anos cruzando varias escolas — NUNCA use as colunas peer_delta_* "
            "(essas sao a evolucao do GRUPO/peer, nao da escola)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "area": {"type": "string", "enum": ["geral", "redacao", "geral_sem_redacao", "mt", "lc", "ch", "cn"], "description": "Area da nota (default geral). mt=matematica, lc=linguagens, ch=humanas, cn=naturais."},
                "uf": {"type": "string", "description": "UF (ex: RS)"},
                "cidade": {"type": "string", "description": "Cidade (fuzzy)"},
                "dependencia": {"type": "string", "description": "Privada / Estadual / Federal / Municipal"},
                "de_ano": {"type": "integer", "description": "Ano inicial (default 2024)"},
                "para_ano": {"type": "integer", "description": "Ano final (default 2025)"},
                "ordem": {"type": "string", "enum": ["desc", "asc"], "description": "desc=maior evolucao (default), asc=maior queda"},
                "top_n": {"type": "integer", "description": "Max escolas (default 10, max 50)"},
            },
        },
    },
]

ENEM_TOOL_HANDLERS: Dict[str, Any] = {
    "analisar_performance_escola": _handle_analisar_performance_escola,
    "priorizar_leads_enem": _handle_priorizar_leads_enem,
    "buscar_escolas_por_enem": _handle_buscar_escolas_por_enem,
    "analisar_dados_analytics": _handle_analisar_dados_analytics,
    "analisar_trajetoria_escola": _handle_analisar_trajetoria_escola,
    "ranking_evolucao_enem": _handle_ranking_evolucao_enem,
}
