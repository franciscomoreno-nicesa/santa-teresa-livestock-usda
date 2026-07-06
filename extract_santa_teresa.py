import os
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import pdfplumber
import requests


# PDF actual de Santa Teresa Livestock Auction
PDF_URL = "https://www.ams.usda.gov/mnreports/ams_1290.pdf"

BASE_DIR = Path(__file__).resolve().parent
PDF_DIR = BASE_DIR / "pdfs"
DATA_DIR = BASE_DIR / "data"

OUTPUT_CSV = DATA_DIR / "santa_teresa_livestock.csv"


def ensure_dirs():
    """
    Crea las carpetas necesarias si no existen.
    """
    PDF_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)


def download_current_pdf():
    """
    Descarga el PDF actual de Santa Teresa.
    Lo guarda con la fecha de hoy para no pisar archivos anteriores.
    """
    response = requests.get(PDF_URL, timeout=60)
    response.raise_for_status()

    today = datetime.today().strftime("%Y-%m-%d")
    pdf_path = PDF_DIR / f"santa_teresa_{today}.pdf"

    with open(pdf_path, "wb") as f:
        f.write(response.content)

    print(f"PDF descargado: {pdf_path}")
    return pdf_path


def extract_text_from_pdf(pdf_path):
    """
    Extrae texto del PDF usando pdfplumber.
    """
    all_text = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            all_text.append(text)

    return "\n".join(all_text)


def find_report_date(text):
    """
    Busca la fecha del reporte dentro del texto.

    Intenta detectar formatos como:
    Tue Jul 01, 2025
    July 01, 2025
    """
    patterns = [
        r"([A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})",
        r"([A-Z][a-z]+\s+\d{1,2},\s+\d{4})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            raw_date = match.group(1)

            for fmt in ["%a %b %d, %Y", "%B %d, %Y"]:
                try:
                    return datetime.strptime(raw_date, fmt).date()
                except ValueError:
                    pass

    return datetime.today().date()


def get_weight_bucket(avg_weight):
    """
    Clasifica el peso promedio en rangos de 100 lb.

    Ejemplos:
    665 -> 600-700
    712 -> 700-800
    450 -> 400-500
    1025 -> 1000-1100
    """
    if avg_weight is None or pd.isna(avg_weight):
        return None

    try:
        weight = float(avg_weight)
    except Exception:
        return None

    lower = int(weight // 100) * 100
    upper = lower + 100

    return f"{lower}-{upper}"


def parse_price_line(line):
    """
    Parser inicial para líneas con pesos y precios.

    Guarda la línea original y trata de sacar:
    - head_count
    - weight_range
    - avg_weight
    - weight_bucket
    - price_range
    - avg_price

    Este parser es conservador porque los PDFs de USDA pueden cambiar
    un poco de formato.
    """
    clean = " ".join(line.split())

    numbers = re.findall(r"\d+(?:\.\d+)?", clean)

    if len(numbers) < 3:
        return None

    weight_range = None
    price_range = None

    # Busca rango de peso tipo 500-600
    range_matches = re.findall(r"(\d{3,4})\s*-\s*(\d{3,4})", clean)
    if range_matches:
        weight_range = f"{range_matches[0][0]}-{range_matches[0][1]}"

    # Busca rango de precio tipo 350.00-375.00
    price_matches = re.findall(r"(\d{2,3}\.\d{2})\s*-\s*(\d{2,3}\.\d{2})", clean)
    if price_matches:
        price_range = f"{price_matches[-1][0]}-{price_matches[-1][1]}"

    head_count = None
    avg_weight = None
    avg_price = None

    try:
        head_count = int(float(numbers[0]))
    except Exception:
        pass

    # Avg weight suele ser un número entre 300 y 1200 lb.
    possible_weights = [float(n) for n in numbers if 300 <= float(n) <= 1200]
    if possible_weights:
        avg_weight = possible_weights[-1]

    # Avg price suele ser número entre 100 y 600.
    possible_prices = [float(n) for n in numbers if 100 <= float(n) <= 600]
    if possible_prices:
        avg_price = possible_prices[-1]

    return {
        "head_count": head_count,
        "weight_range": weight_range,
        "avg_weight": avg_weight,
        "weight_bucket": get_weight_bucket(avg_weight),
        "price_range": price_range,
        "avg_price": avg_price,
        "raw_line": clean,
    }


def classify_line(line):
    """
    Clasifica la línea en:
    - sex: Steers, Heifers, Cows, Bulls
    - class_grade: Medium and Large 1, Medium and Large 1-2, etc.
    """
    lower = line.lower()

    sex = None
    if "steer" in lower:
        sex = "Steers"
    elif "heifer" in lower:
        sex = "Heifers"
    elif "cow" in lower:
        sex = "Cows"
    elif "bull" in lower:
        sex = "Bulls"

    class_grade = None
    grade_match = re.search(
        r"(Medium and Large\s+\d(?:-\d)?|Medium\s+\d|Large\s+\d|Small\s+\d)",
        line,
        re.IGNORECASE,
    )

    if grade_match:
        class_grade = grade_match.group(1)

    return sex, class_grade


def parse_report(text, pdf_path):
    """
    Convierte el texto del PDF en filas para CSV.
    """
    report_date = find_report_date(text)

    rows = []
    current_sex = None
    current_class_grade = None

    for line in text.splitlines():
        clean = " ".join(line.split())

        if not clean:
            continue

        detected_sex, detected_grade = classify_line(clean)

        if detected_sex:
            current_sex = detected_sex

        if detected_grade:
            current_class_grade = detected_grade

        parsed = parse_price_line(clean)

        if parsed is None:
            continue

        # Evita líneas que claramente no son de precios
        if parsed["avg_price"] is None:
            continue

        rows.append({
            "report_date": report_date.isoformat(),
            "year": report_date.year,
            "month": report_date.month,
            "market": "Santa Teresa Livestock Auction",
            "source": "USDA AMS",
            "commodity": "Livestock",
            "category": None,
            "class_grade": current_class_grade,
            "sex": current_sex,
            "head_count": parsed["head_count"],
            "weight_range": parsed["weight_range"],
            "avg_weight": parsed["avg_weight"],
            "weight_bucket": parsed["weight_bucket"],
            "price_range": parsed["price_range"],
            "avg_price": parsed["avg_price"],
            "notes": None,
            "pdf_url": PDF_URL,
            "local_pdf": str(pdf_path),
            "raw_line": parsed["raw_line"],
        })

    return rows


def save_rows(rows):
    """
    Guarda las filas nuevas en CSV sin duplicar.
    """
    new_df = pd.DataFrame(rows)

    if new_df.empty:
        print("No se encontraron filas para guardar.")
        return

    if OUTPUT_CSV.exists():
        old_df = pd.read_csv(OUTPUT_CSV)
        combined = pd.concat([old_df, new_df], ignore_index=True)
    else:
        combined = new_df

    # Quita duplicados usando fecha + línea original
    combined = combined.drop_duplicates(
        subset=["report_date", "raw_line"],
        keep="last",
    )

    # Ordena para que quede más limpio
    sort_cols = ["report_date", "sex", "class_grade", "weight_bucket"]
    existing_sort_cols = [col for col in sort_cols if col in combined.columns]

    combined = combined.sort_values(
        existing_sort_cols,
        na_position="last",
    )

    combined.to_csv(OUTPUT_CSV, index=False)

    print(f"CSV actualizado: {OUTPUT_CSV}")
    print(f"Filas totales: {len(combined)}")


def main():
    ensure_dirs()

    pdf_path = download_current_pdf()
    text = extract_text_from_pdf(pdf_path)
    rows = parse_report(text, pdf_path)
    save_rows(rows)


if __name__ == "__main__":
    main()
