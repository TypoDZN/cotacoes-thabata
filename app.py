import os
import re
import sqlite3
import unicodedata

import streamlit as st
from rapidfuzz import fuzz, process

import importar_dados

DB_PATH = os.path.join("database", "produtos.db")
THRESHOLD = 62


@st.cache_resource
def inicializar_banco():
    """
    Gera o banco de dados a partir dos arquivos em data/.
    Roda uma vez por sessão do servidor — automaticamente no Streamlit Cloud.
    """
    importar_dados.main()


inicializar_banco()


# ── Formatação de preço ──────────────────────────────────────────────────────

def fmt(valor) -> str:
    """Formata número como R$ 1.234,56"""
    s = f"{float(valor):,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


# ── Normalização para busca sem acento ──────────────────────────────────────

def normalizar(texto: str) -> str:
    return (
        unicodedata.normalize("NFD", texto)
        .encode("ascii", "ignore")
        .decode("ascii")
        .upper()
    )


# ── Busca no banco ───────────────────────────────────────────────────────────

def buscar(query: str, conn) -> list[dict]:
    """
    Busca por palavras-chave na coluna normalizada (sem acento).
    Para queries de 2+ palavras, nunca reduz abaixo de 2 palavras antes de
    tentar busca fuzzy — evita retornos genéricos por palavras soltas.
    """
    palavras = [normalizar(p) for p in query.split() if len(p) > 1]
    if not palavras:
        return []

    min_palavras = min(2, len(palavras))

    for n in range(len(palavras), min_palavras - 1, -1):
        sub = palavras[:n]
        cond = " AND ".join("nome_busca LIKE ?" for _ in sub)
        rows = conn.execute(
            f"SELECT fornecedor, nome_produto, preco FROM produtos WHERE {cond}",
            [f"%{p}%" for p in sub],
        ).fetchall()
        if rows:
            return [{"fornecedor": r[0], "nome": r[1], "preco": r[2]} for r in rows]

    # Fuzzy fallback
    todos = conn.execute(
        "SELECT fornecedor, nome_produto, nome_busca, preco FROM produtos"
    ).fetchall()
    if not todos:
        return []

    query_norm = normalizar(query)
    nomes_norm = [r[2] for r in todos]
    matches = process.extract(query_norm, nomes_norm, scorer=fuzz.token_set_ratio, limit=10)
    return [
        {"fornecedor": todos[i][0], "nome": todos[i][1], "preco": todos[i][3]}
        for _, score, i in matches
        if score >= THRESHOLD
    ]


def mais_barato(produtos: list[dict]) -> list[dict]:
    """Retorna o produto mais barato por nome único (para Lista de Produtos)."""
    best: dict[str, dict] = {}
    for p in produtos:
        if p["nome"] not in best or p["preco"] < best[p["nome"]]["preco"]:
            best[p["nome"]] = p
    return sorted(best.values(), key=lambda x: x["preco"])


# ── Parser de linha de cotação ───────────────────────────────────────────────

_CABECALHO_RE = re.compile(
    r"\b(COTACAO|LISTA|PEDIDO|CLIENTE|RESTAURANTE|GASTRONOMIA|FORNECEDOR|"
    r"TABELA|ORCAMENTO|EMPRESA|ESTABELECIMENTO|CARDAPIO|CARDAPIO|COMPRAS)\b"
)


def e_cabecalho(linha: str) -> bool:
    """Retorna True se a linha parece um título ou cabeçalho, não um produto."""
    return bool(_CABECALHO_RE.search(normalizar(linha)))


_UNIT = (
    r"(?:cx\.?|caixa(?:s)?|un\.?|unid\.?|unidade(?:s)?|"
    r"kg|g\b|lt?\.?|litro(?:s)?|pct\.?|pacote(?:s)?|"
    r"fd\.?|fardo(?:s)?|bd\.?|bandeja(?:s)?|lata(?:s)?|"
    r"garrafa(?:s)?|dz\.?|d[úu]zia(?:s)?)"
)
_NUM = r"\d+(?:[,\.]\d+)?"


def parse_cotacao(linha: str) -> tuple[str, float, str]:
    """
    Extrai (nome_produto, quantidade, unidade) de uma linha.
    Formatos suportados:
      aceto balsamico - 2        (separador com traço)
      aceto balsamico: 2         (separador com dois-pontos)
      aceto balsamico 2cx        (quantidade grudada à unidade)
      aceto balsamico 2          (só número no fim)
      2 aceto balsamico          (quantidade no início)
      2cx aceto balsamico        (quantidade+unidade no início)
    """
    linha = linha.strip()

    # Quantidade no FIM — com qualquer separador ou direto após espaço
    m = re.search(
        rf"[\s:,\-–]+({_NUM})\s*({_UNIT})?\s*$",
        linha, re.IGNORECASE,
    )
    if m and m.group(1):
        nome = linha[: m.start()].strip().rstrip(":,-–").strip()
        if nome:
            qty = float(m.group(1).replace(",", "."))
            unit = (m.group(2) or "un").lower().rstrip(".")
            return nome, qty, unit

    # Quantidade no INÍCIO — "2 produto" ou "2cx produto"
    m = re.match(rf"^({_NUM})\s*({_UNIT})?\s+(.+)", linha, re.IGNORECASE)
    if m:
        qty = float(m.group(1).replace(",", "."))
        unit = (m.group(2) or "un").lower().rstrip(".")
        nome = m.group(3).strip()
        return nome, qty, unit

    return linha, 1.0, "un"


# ── Formatadores de saída (texto WhatsApp) ───────────────────────────────────

def texto_lista(itens: list[tuple], nao_encontrados: list[str]) -> str:
    """
    itens: [(query, [produto_dict, ...]), ...]
    Formato: Nome Do Produto — Fornecedor — R$ 0,00
    """
    linhas = []
    for _query, produtos in itens:
        for p in produtos:
            linhas.append(
                f"{p['nome'].title()} — {p['fornecedor']} — {fmt(p['preco'])}"
            )
        linhas.append("")

    if nao_encontrados:
        linhas.append("Não encontrados:")
        for item in nao_encontrados:
            linhas.append(f"❌ {item}")

    return "\n".join(linhas).strip()


def texto_cotacao(itens: list[tuple], nao_encontrados: list[str]) -> str:
    """
    itens: [(query, qty, unit, [produto_dict, ...]), ...]
    Quando múltiplos fornecedores são selecionados para o mesmo produto,
    todas as opções aparecem no texto. O total usa o menor preço por item.
    """
    linhas = []
    total = 0.0

    for _query, qty, unit, produtos in itens:
        preco_min = min(p["preco"] for p in produtos)
        total += qty * preco_min

        for p in produtos:
            subtotal = qty * p["preco"]
            linhas += [
                f"{p['nome'].title()} — {p['fornecedor']}",
                f"{qty:g} {unit} x {fmt(p['preco'])} = {fmt(subtotal)}",
                "",
            ]

    if nao_encontrados:
        for item in nao_encontrados:
            linhas.append(f"❌ {item}")
        linhas.append("")

    sufixo = " (menor preço)" if any(len(t[3]) > 1 for t in itens) else ""
    linhas.append(f"*Total{sufixo}: {fmt(total)}*")
    return "\n".join(linhas).strip()


# ── Configuração da página ───────────────────────────────────────────────────

st.set_page_config(page_title="Cotações Thábata", layout="wide")
st.title("Sistema de Cotações — Thábata")

# ── Barra lateral ────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("Tabelas de preços")
    if st.button("🔄 Recarregar após trocar arquivo"):
        st.cache_data.clear()
        st.rerun()
    st.divider()

    _conn = sqlite3.connect(DB_PATH)
    _total = _conn.execute("SELECT COUNT(*) FROM produtos").fetchone()[0]
    _forn = _conn.execute(
        "SELECT fornecedor, COUNT(*) FROM produtos GROUP BY fornecedor"
    ).fetchall()
    _conn.close()

    st.metric("Produtos no banco", _total)
    st.caption("")
    for nome, qtd in _forn:
        st.caption(f"**{nome}**: {qtd} produtos")

# ── Abas principais ──────────────────────────────────────────────────────────

tab1, tab2 = st.tabs(["📋 Lista de Produtos", "💰 Cotação Semanal"])


# ─── Aba 1 — Lista de Produtos ───────────────────────────────────────────────

with tab1:
    st.caption(
        "Cole a lista que o cliente enviou. Um produto por linha.  \n"
        "Quando um produto estiver em mais de um fornecedor, você escolhe quais opções incluir."
    )
    entrada1 = st.text_area(
        "Lista do cliente:",
        height=220,
        placeholder="Água mineral\nCoca-Cola 2L\nWhisky Johnnie Walker Red",
        key="lista_entrada",
    )

    if st.button("Buscar preços", type="primary", key="lista_btn"):
        todas = [l.strip() for l in entrada1.strip().splitlines() if l.strip()]
        ignoradas = [l for l in todas if e_cabecalho(l)]
        linhas = [l for l in todas if not e_cabecalho(l)]
        if ignoradas:
            st.info(f"Linha(s) ignorada(s) como cabeçalho: {', '.join(ignoradas)}")
        if not linhas:
            st.warning("Cole algum produto antes de buscar.")
        else:
            conn = sqlite3.connect(DB_PATH)
            resultados_lista: list[dict] = []
            nao_enc_lista: list[str] = []

            with st.spinner("Buscando..."):
                for linha in linhas:
                    opcoes = sorted(buscar(linha, conn), key=lambda x: x["preco"])
                    if opcoes:
                        resultados_lista.append({"linha": linha, "opcoes": opcoes})
                    else:
                        nao_enc_lista.append(linha)
            conn.close()

            st.session_state["lista"] = resultados_lista
            st.session_state["lista_nao"] = nao_enc_lista

    if "lista" in st.session_state:
        resultados_lista = st.session_state["lista"]
        nao_enc_lista = st.session_state.get("lista_nao", [])

        tem_multiplos = any(len(r["opcoes"]) > 1 for r in resultados_lista)
        selecoes_lista: dict[str, list[dict]] = {}

        if tem_multiplos:
            st.divider()
            st.subheader("Escolha o(s) fornecedor(es)")
            st.caption(
                "Marque um ou mais fornecedores por produto. "
                "Quando mais de um for marcado, todas as opções aparecem no texto final."
            )

        for r in resultados_lista:
            opcoes = r["opcoes"]
            if len(opcoes) == 1:
                selecoes_lista[r["linha"]] = [opcoes[0]]
            else:
                st.write(f"**{r['linha'].title()}**")
                selecionados = []
                for i, op in enumerate(opcoes):
                    label = (
                        f"{op['fornecedor']}  —  "
                        f"{op['nome'].title()}  —  "
                        f"{fmt(op['preco'])}"
                    )
                    if st.checkbox(label, value=(i == 0), key=f"lista_chk_{r['linha']}_{i}"):
                        selecionados.append(op)
                selecoes_lista[r["linha"]] = selecionados if selecionados else [opcoes[0]]

        if st.button("Gerar texto", type="primary", key="lista_gerar"):
            itens = [
                (r["linha"], selecoes_lista[r["linha"]])
                for r in resultados_lista
            ]
            texto = texto_lista(itens, nao_enc_lista)
            st.subheader("Texto para WhatsApp")
            st.code(texto, language=None)

        if nao_enc_lista:
            with st.expander(f"❌ {len(nao_enc_lista)} não encontrado(s)"):
                for item in nao_enc_lista:
                    st.write(f"❌ {item}")


# ─── Aba 2 — Cotação Semanal ─────────────────────────────────────────────────

with tab2:
    st.caption(
        "Cole a lista com quantidades. Um produto por linha.  \n"
        "Formatos aceitos: `produto - 2 cx` · `produto 2cx` · `2 produto` · `produto: 2`"
    )
    entrada2 = st.text_area(
        "Lista com quantidades:",
        height=220,
        placeholder=(
            "Água mineral 500ml - 20 cx\n"
            "Coca-Cola 2L - 12 un\n"
            "Whisky Johnnie Walker Red - 5 cx"
        ),
        key="cotacao_entrada",
    )

    if st.button("Buscar", type="primary", key="cotacao_buscar"):
        todas = [l.strip() for l in entrada2.strip().splitlines() if l.strip()]
        ignoradas = [l for l in todas if e_cabecalho(l)]
        linhas = [l for l in todas if not e_cabecalho(l)]
        if ignoradas:
            st.info(f"Linha(s) ignorada(s) como cabeçalho: {', '.join(ignoradas)}")
        if not linhas:
            st.warning("Cole algum produto antes de buscar.")
        else:
            conn = sqlite3.connect(DB_PATH)
            resultados: list[dict] = []
            nao_enc2: list[str] = []

            with st.spinner("Buscando..."):
                for linha in linhas:
                    nome_q, qty, unit = parse_cotacao(linha)
                    opcoes = buscar(nome_q, conn)
                    if opcoes:
                        resultados.append(
                            {
                                "linha": linha,
                                "query": nome_q,
                                "qty": qty,
                                "unit": unit,
                                "opcoes": sorted(opcoes, key=lambda x: x["preco"]),
                            }
                        )
                    else:
                        nao_enc2.append(linha)
            conn.close()

            st.session_state["cot"] = resultados
            st.session_state["cot_nao"] = nao_enc2

    # ── Seleção de fornecedor + geração do texto ──────────────────────────────

    if "cot" in st.session_state:
        resultados = st.session_state["cot"]
        nao_enc2 = st.session_state.get("cot_nao", [])

        tem_multiplos = any(len(r["opcoes"]) > 1 for r in resultados)
        selecoes: dict[str, list[dict]] = {}

        if tem_multiplos:
            st.divider()
            st.subheader("Escolha o(s) fornecedor(es)")
            st.caption(
                "Marque um ou mais fornecedores por produto. "
                "Quando mais de um for marcado, todas as opções aparecem no texto final."
            )

        for r in resultados:
            opcoes = r["opcoes"]  # já ordenadas por preço (mais barato primeiro)

            if len(opcoes) == 1:
                selecoes[r["linha"]] = [opcoes[0]]
            else:
                st.write(f"**{r['query'].title()}**")
                selecionados = []
                for i, op in enumerate(opcoes):
                    label = (
                        f"{op['fornecedor']}  —  "
                        f"{op['nome'].title()}  —  "
                        f"{fmt(op['preco'])}"
                    )
                    # Pré-marca o mais barato (primeiro da lista ordenada)
                    if st.checkbox(label, value=(i == 0), key=f"chk_{r['linha']}_{i}"):
                        selecionados.append(op)

                # Garante pelo menos uma opção selecionada
                selecoes[r["linha"]] = selecionados if selecionados else [opcoes[0]]

        if st.button("Gerar cotação", type="primary", key="cotacao_gerar"):
            itens = [
                (r["linha"], r["qty"], r["unit"], selecoes[r["linha"]])
                for r in resultados
            ]
            texto = texto_cotacao(itens, nao_enc2)
            st.subheader("Texto para WhatsApp")
            st.code(texto, language=None)

        if nao_enc2:
            with st.expander(f"❌ {len(nao_enc2)} não encontrado(s)"):
                for item in nao_enc2:
                    st.write(f"❌ {item}")
