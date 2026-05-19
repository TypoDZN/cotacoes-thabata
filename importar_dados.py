import os
import re
import sqlite3
import unicodedata

import pandas as pd

DATA_DIR = "data"
DB_PATH = os.path.join("database", "produtos.db")
EXTENSOES = [".xlsx", ".xls", ".csv"]


# ── Utilitários ──────────────────────────────────────────────────────────────

def normalizar(texto: str) -> str:
    """Remove acentos e coloca em maiúsculas para busca sem distinção de acento."""
    return (
        unicodedata.normalize("NFD", texto)
        .encode("ascii", "ignore")
        .decode("ascii")
        .upper()
    )


def limpar_preco(valor):
    """Converte qualquer formato de preço (R$1.234,56 ou 12.50) para float."""
    if pd.isna(valor):
        return None
    s = str(valor).strip()
    s = re.sub(r"[R$\s ]", "", s)
    if not s:
        return None

    n_pontos = s.count(".")
    n_virgulas = s.count(",")

    if n_virgulas > 0 and n_pontos > 0:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")   # 1.234,56 → 1234.56
        else:
            s = s.replace(",", "")                     # 1,234.56 → 1234.56
    elif n_virgulas > 0:
        partes = s.split(",")
        if len(partes) == 2 and len(partes[1]) <= 2:
            s = s.replace(",", ".")                    # 12,50 → 12.50
        else:
            s = s.replace(",", "")
    elif n_pontos > 1:
        s = s.replace(".", "")                         # 1.234.567 → 1234567

    s = re.sub(r"[^\d.]", "", s)
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


def encontrar_arquivo(base: str) -> str | None:
    """Procura o arquivo pelo nome base tentando .xlsx, .xls e .csv."""
    for ext in EXTENSOES:
        path = os.path.join(DATA_DIR, base + ext)
        if os.path.exists(path):
            return path
    return None


def ler_csv(path: str) -> pd.DataFrame:
    """Lê CSV tentando diferentes encodings e separadores."""
    for enc in ["utf-8-sig", "utf-8", "latin-1", "cp1252"]:
        for sep in [",", ";", "\t"]:
            try:
                df = pd.read_csv(path, encoding=enc, sep=sep, on_bad_lines="skip")
                if len(df.columns) > 1:
                    df.columns = df.columns.str.strip()
                    return df
            except Exception:
                continue
    raise ValueError(f"Não foi possível ler o arquivo: {path}")


def ler_arquivo(path: str) -> pd.DataFrame:
    """Lê Excel ou CSV detectando automaticamente o cabeçalho real."""
    ext = os.path.splitext(path)[1].lower()

    if ext in (".xlsx", ".xls"):
        raw = pd.read_excel(path, header=None)
        # Procura a linha que contém os títulos das colunas
        header_row = 0
        for i, row in raw.iterrows():
            vals = [str(v).strip().upper() for v in row if not pd.isna(v)]
            if any(k in v for v in vals for k in ("PRODUTO", "DESCRI", "NOME")):
                header_row = i
                break
        df = pd.read_excel(path, header=header_row)
        df.columns = df.columns.str.strip()
        return df

    return ler_csv(path)


# ── Importadores por fornecedor ──────────────────────────────────────────────

def _col(df, *palavras_chave):
    """Encontra coluna cujo nome contenha qualquer uma das palavras-chave."""
    return next(
        (c for c in df.columns if any(k in c.lower() for k in palavras_chave)),
        None,
    )


def importar_cia_whisky(conn, base, fornecedor):
    path = encontrar_arquivo(base)
    if not path:
        print(f"  AVISO: arquivo não encontrado — {base}.xlsx/.xls/.csv")
        return 0

    df = ler_arquivo(path)
    col_produto = _col(df, "produto", "descri", "nome")
    col_preco   = _col(df, "preco", "preço", "valor", "prç")

    if not col_produto or not col_preco:
        print(f"  ERRO {base} — colunas encontradas: {list(df.columns)}")
        return 0

    produtos = []
    for _, row in df.iterrows():
        nome = str(row.get(col_produto, "")).strip()
        preco = limpar_preco(row.get(col_preco))
        if nome and nome.upper() != "NAN" and preco:
            produtos.append((fornecedor, nome.upper(), normalizar(nome), preco))

    conn.executemany(
        "INSERT INTO produtos (fornecedor, nome_produto, nome_busca, preco) VALUES (?, ?, ?, ?)",
        produtos,
    )
    print(f"  {fornecedor} ({os.path.basename(path)}): {len(produtos)} produtos")
    return len(produtos)


def importar_apetito(conn):
    path = encontrar_arquivo("apetito")
    if not path:
        print("  AVISO: arquivo não encontrado — apetito.xlsx/.xls/.csv")
        return 0

    df = ler_arquivo(path)
    col_produto  = _col(df, "descri", "produto", "nome")
    col_preco    = _col(df, "preco", "preço", "valor", "prç")
    col_unidade  = _col(df, "unidade", "un", "und")

    if not col_produto or not col_preco:
        print(f"  ERRO apetito — colunas encontradas: {list(df.columns)}")
        return 0

    produtos = []
    for _, row in df.iterrows():
        nome = str(row.get(col_produto, "")).strip()
        if not nome or nome.upper() == "NAN":
            continue
        if re.search(r"fam[ií]lia\s*:", nome, re.IGNORECASE):
            continue

        preco = limpar_preco(row.get(col_preco))
        if not preco:
            continue

        un = str(row.get(col_unidade, "")).strip() if col_unidade else ""
        nome_final = f"{nome} ({un.upper()})" if un and un.upper() != "NAN" else nome
        produtos.append(("Apetito Foods", nome_final.upper(), normalizar(nome_final), preco))

    conn.executemany(
        "INSERT INTO produtos (fornecedor, nome_produto, nome_busca, preco) VALUES (?, ?, ?, ?)",
        produtos,
    )
    print(f"  Apetito Foods ({os.path.basename(path)}): {len(produtos)} produtos")
    return len(produtos)


def importar_forte(conn):
    path = encontrar_arquivo("forte")
    if not path:
        print("  AVISO: arquivo não encontrado — forte.xlsx/.xls/.csv")
        return 0

    df = ler_arquivo(path)
    col_produto = _col(df, "produto", "descri", "nome")
    col_preco   = _col(df, "ddl", "preco", "preço", "valor", "prç")

    if not col_produto or not col_preco:
        print(f"  ERRO forte — colunas encontradas: {list(df.columns)}")
        return 0

    produtos = []
    for _, row in df.iterrows():
        nome = str(row.get(col_produto, "")).strip()
        if not nome or nome.upper() == "NAN":
            continue

        preco = limpar_preco(row.get(col_preco))
        if not preco:
            continue

        produtos.append(("Forte Alimentos", nome.upper(), normalizar(nome), preco))

    conn.executemany(
        "INSERT INTO produtos (fornecedor, nome_produto, nome_busca, preco) VALUES (?, ?, ?, ?)",
        produtos,
    )
    print(f"  Forte Alimentos ({os.path.basename(path)}): {len(produtos)} produtos")
    return len(produtos)


# ── Banco de dados ───────────────────────────────────────────────────────────

def criar_banco():
    os.makedirs("database", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    # Recria a tabela para garantir esquema atualizado
    conn.execute("DROP TABLE IF EXISTS produtos")
    conn.execute(
        """
        CREATE TABLE produtos (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            fornecedor   TEXT NOT NULL,
            nome_produto TEXT NOT NULL,
            nome_busca   TEXT NOT NULL,
            preco        REAL NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX idx_busca ON produtos(nome_busca)")
    conn.commit()
    return conn


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 50)
    print("IMPORTAÇÃO DE TABELAS")
    print("=" * 50)

    conn = criar_banco()

    total = 0
    total += importar_cia_whisky(conn, "cia_whisky_alimentos", "Cia do Whisky")
    total += importar_cia_whisky(conn, "cia_whisky_bebidas",   "Cia do Whisky")
    total += importar_apetito(conn)
    total += importar_forte(conn)

    conn.commit()
    conn.close()

    print("-" * 50)
    print(f"TOTAL: {total} produtos importados com sucesso")
    print("=" * 50)


if __name__ == "__main__":
    main()
