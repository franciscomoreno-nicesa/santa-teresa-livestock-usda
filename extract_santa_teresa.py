import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import pdfplumber
import requests
from bs4 import BeautifulSoup


# =========================
# CONFIGURACION
# =========================

REPORT_LIMIT = 240

PUBLICATION_BASE_URL = (
    "https://esmis.nal.usda.gov/publication/"
    "santa-teresa-livestock-auction-wtd-avg-santa-teresa-nm"
)

BASE_DIR = Path(__file__).resolve().parent
PDF_DIR = BASE_DIR / "pdfs"
DATA_DIR = BASE_DIR / "data"
OUTPUT_CSV = DATA_DIR / "santa_teresa_livestock.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; SantaTeresaLivestockBot/1.0; "
        "+https://github.com/)"
    )
}


# =========================
# CARPETAS
# =========================

def ensure_dirs():
    PDF_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)


# =========================
# DESCARGA DE REPORTES
# =========================

def get_publication_page_url(report_date):
    """
    Construye una URL de ESMIS/USDA con fecha.

    Ejemplo:
    https://esmis.nal.usda.gov/publication/santa-teresa-livestock-auction-wtd-avg-santa-teresa-nm/2024-10-25
    """
    return f"{PUBLICATION_BASE_URL}/{report_date.isoformat()}"


def find_pdf_url_on_page(page_url):
    """
    Entra a una pagina de publicacion de ESMIS y busca el link al PDF AMS_1290.PDF.
    """
    try:
        response = requests.get(page_url, headers=HEADERS, timeout=30)
    except requests.RequestException:
        return None

    if response.status_code != 200:
        return None

    soup = BeautifulSoup(response.text, "html.parser")

    for link in soup.find_all("a", href=True):
        href = link["href"]
        text = link.get_text(" ", strip=True)

        href_lower = href.lower()
        text_lower = text.lower()

        if "ams_1290" in href_lower and href_lower.endswith(".pdf"):
            return urljoin(page_url, href)

        if "ams_1290" in text_lower and ".pdf" in text_lower:
            return urljoin(page_url, href)

        if href_lower.endswith(".pdf") and "1290" in href_lower:
            return urljoin(page_url, href)

    return None


def discover_last_reports(limit=10, max_days_back=900):
    """
    Busca hacia atras desde hoy hasta encontrar los ultimos reportes disponibles.
    No descarga uno por uno manualmente; el codigo revisa fechas y encuentra los PDFs.

    limit = cuantos reportes quieres descargar.
    max_days_back = cuantos dias hacia atras revisar como maximo.
    """
    found = []
    seen_pdf_urls = set()

    today = date.today()

    for i in range(max_days_back + 1):
        current_date = today - timedelta(days=i)
        page_url = get_publication_page_url(current_date)

        pdf_url = find_pdf_url_on_page(page_url)

        if not pdf_url:
            continue

        if pdf_url in seen_pdf_urls:
            continue

        seen_pdf_urls.add(pdf_url)

        found.append({
            "publication_date": current_date.isoformat(),
            "page_url": page_url,
            "pdf_url": pdf_url,
        })

        print(f"Encontrado {len(found)}/{limit}: {current_date.isoformat()}")

        if len(found) >= limit:
            break

    return found


def download_pdf(report):
    """
    Descarga un PDF encontrado.
    Si ya existe, no lo vuelve a descargar.
    """
    publication_date = report["publication_date"]
    pdf_url = report["pdf_url"]

    pdf_path = PDF_DIR / f"santa_teresa_{publication_date}.pdf"

    if pdf_path.exists() and pdf_path.stat().st_size > 0:
        print(f"PDF ya existe, saltando: {pdf_path.name}")
        return pdf_path

    response = requests.get(pdf_url, headers=HEADERS, timeout=60)
    response.raise_for_status()

    with open(pdf_path, "wb") as f:
        f.write(response.content)

    print(f"PDF descargado: {pdf_path.name}")
    return pdf_path


# =========================
# LECTURA DE PDF
# =========================

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


def find_report_date(text, fallback_date=None):
    """
    Busca la fecha del reporte dentro del texto.

    Formatos esperados:
    Fri Apr 4, 2025
    Tue Jul 15, 2025
    April 4, 2025
    """
    patterns = [
        r"([A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})",
        r"([A-Z][a-z]+\s+\d{1,2},\s+\d{4})",
        r"Livestock Weighted Average Report for\s+(\d{1,2}/\d{1,2}/\d{4})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue

        raw_date = match.group(1).strip()

        for fmt in ["%a %b %d, %Y", "%B %d, %Y", "%m/%d/%Y"]:
            try:
                return datetime.strptime(raw_date, fmt).date()
            except ValueError:
                pass

    if fallback_date:
        try:
            return datetime.strptime(fallback_date, "%Y-%m-%d").date()
        except ValueError:
            pass

    return date.today()


# =========================
# CLASIFICACION POR PESO
# =========================

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


# =========================
# PARSER DE LINEAS
# =========================

def classify_header_line(line):
    """
    Detecta encabezados de seccion.

    Ejemplo:
    STEERS - Medium and Large 1-2 (Per Cwt / Actual Wt)
    HEIFERS - Medium and Large 2-3 (Per Cwt / Actual Wt)
    """
    clean = " ".join(line.split())

    header_pattern = re.compile(
        r"^(STEERS|HEIFERS|COWS|BULLS)\s*-\s*(.+?)\s*\(",
        re.IGNORECASE,
    )

    match = header_pattern.search(clean)

    if not match:
        return None, None

    sex = match.group(1).title()
    class_grade = match.group(2).strip()

    return sex, class_grade


def parse_price_line(line):
    """
    Parsea lineas tipo:

    Head Wt Range Avg Wt Price Range Avg Price

    Ejemplos:
    17 522-525 524 271.50-276.00 273.08
    44 806 806 256.00 256.00
    5 907 907 220.00 220.00
    """
    clean = " ".join(line.split())

    if not clean:
        return None

    # Evita encabezados y textos generales
    lower = clean.lower()
    skip_words = [
        "head wt range",
        "please note",
        "source:",
        "page ",
        "supply included",
        "total receipts",
        "feeder cattle:",
        "livestock weighted average",
        "santa teresa livestock",
        "email us",
        "compared with",
        "trade ",
        "demand ",
        "bulk of supply",
        "next sale",
        "auction",
    ]

    if any(word in lower for word in skip_words):
        return None

    # Patron principal:
    # head, wt_range, avg_wt, price_range, avg_price, notes opcional
    pattern = re.compile(
        r"^\s*"
        r"(?P<head>\d+)\s+"
        r"(?P<wt_range>\d{2,4}(?:\s*-\s*\d{2,4})?)\s+"
        r"(?P<avg_wt>\d{2,4})\s+"
        r"(?P<price_range>\d{2,4}(?:\.\d{2})?(?:\s*-\s*\d{2,4}(?:\.\d{2})?)?)\s+"
        r"(?P<avg_price>\d{2,4}(?:\.\d{2})?)"
        r"(?:\s+(?P<notes>.*))?"
        r"\s*$"
    )

    match = pattern.search(clean)

    if not match:
        return None

    try:
        head_count = int(match.group("head"))
        avg_weight = float(match.group("avg_wt"))
        avg_price = float(match.group("avg_price"))
    except ValueError:
        return None

    wt_range = match.group("wt_range").replace(" ", "")
    price_range = match.group("price_range").replace(" ", "")
    notes = match.group("notes")

    return {
        "head_count": head_count,
        "weight_range": wt_range,
        "avg_weight": avg_weight,
        "weight_bucket": get_weight_bucket(avg_weight),
        "price_range": price_range,
        "avg_price": avg_price,
        "notes": notes,
        "raw_line": clean,
    }


def parse_report(text, pdf_path, report_meta):
    """
    Convierte el texto del PDF en filas para CSV.
    """
    report_date = find_report_date(
        text,
        fallback_date=report_meta.get("publication_date"),
    )

    rows = []
    current_sex = None
    current_class_grade = None

    for line in text.splitlines():
        clean = " ".join(line.split())

        if not clean:
            continue

        detected_sex, detected_class_grade = classify_header_line(clean)

        if detected_sex:
            current_sex = detected_sex

        if detected_class_grade:
            current_class_grade = detected_class_grade

        parsed = parse_price_line(clean)

        if parsed is None:
            continue

        rows.append({
            "report_date": report_date.isoformat(),
            "year": report_date.year,
            "month": report_date.month,
            "market": "Santa Teresa Livestock Auction",
            "source": "USDA ESMIS",
            "commodity": "Feeder Cattle",
            "category": None,
            "class_grade": current_class_grade,
            "sex": current_sex,
            "head_count": parsed["head_count"],
            "weight_range": parsed["weight_range"],
            "avg_weight": parsed["avg_weight"],
            "weight_bucket": parsed["weight_bucket"],
            "price_range": parsed["price_range"],
            "avg_price": parsed["avg_price"],
            "notes": parsed["notes"],
            "publication_page": report_meta.get("page_url"),
            "pdf_url": report_meta.get("pdf_url"),
            "local_pdf": str(pdf_path),
            "raw_line": parsed["raw_line"],
        })

    return rows


# =========================
# GUARDADO
# =========================

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

    combined = combined.drop_duplicates(
        subset=["report_date", "sex", "class_grade", "raw_line"],
        keep="last",
    )

    sort_cols = [
        "report_date",
        "sex",
        "class_grade",
        "weight_bucket",
        "avg_weight",
    ]

    existing_sort_cols = [col for col in sort_cols if col in combined.columns]

    combined = combined.sort_values(
        existing_sort_cols,
        na_position="last",
    )

    combined.to_csv(OUTPUT_CSV, index=False)

    print(f"CSV actualizado: {OUTPUT_CSV}")
    print(f"Filas totales: {len(combined)}")


# =========================
# MAIN
# =========================

def main():
    ensure_dirs()

    print(f"Buscando los ultimos {REPORT_LIMIT} reportes de Santa Teresa...")
    reports = discover_last_reports(limit=REPORT_LIMIT)

    if not reports:
        print("No se encontraron reportes.")
        return

    all_rows = []

    for report in reports:
        print("=" * 80)
        print(f"Procesando reporte: {report['publication_date']}")
        print(report["pdf_url"])

        pdf_path = download_pdf(report)
        text = extract_text_from_pdf(pdf_path)
        rows = parse_report(text, pdf_path, report)

        print(f"Filas extraidas: {len(rows)}")
        all_rows.extend(rows)

    save_rows(all_rows)


if __name__ == "__main__":
    main()
