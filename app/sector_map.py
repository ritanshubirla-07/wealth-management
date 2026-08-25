from typing import Dict, Tuple

SECTOR_MAP: Dict[str, Tuple[str, str]] = {
    "PIRAMAL FINANCE": ("Financial Services", "Large Cap"),
    "AEGIS LOGISTICS": ("Industrials", "Mid Cap"),
    "GLENMARK PHARMACEUTICALS": ("Pharma", "Mid Cap"),
    "EMCURE PHARMACEUTICALS": ("Pharma", "Mid Cap"),
    "BHARTI AIRTEL": ("Telecom", "Large Cap"),
    "VARUN BEVERAGES": ("Consumer Goods", "Large Cap"),
    "ELECON ENGINEERING": ("Industrials", "Mid Cap"),
    "EMAMI": ("Consumer Goods", "Mid Cap"),
    "HDFC BANK": ("Financial Services", "Large Cap"),
    "ICICI BANK": ("Financial Services", "Large Cap"),
    "RELIANCE INDUSTRIES": ("Conglomerate", "Large Cap"),
    "TATA MOTORS": ("Automobile", "Large Cap"),
    "INFOSYS": ("IT", "Large Cap"),
    "TCS": ("IT", "Large Cap"),
    "GODREJ CONSUMER": ("Consumer Goods", "Large Cap"),
    "VEDANT FASHIONS": ("Consumer Goods", "Mid Cap"),
    "MEDI ASSIST HEALTHCARE": ("Healthcare", "Small Cap"),
    "CHALET HOTELS": ("Hospitality", "Mid Cap"),
    "ANAND RATHI WEALTH": ("Financial Services", "Small Cap"),
    "SUVEN PHARMACEUTICALS": ("Pharma", "Mid Cap"),
    "JYOTHY LABS": ("Consumer Goods", "Mid Cap"),
    "ALLIED BLENDERS": ("Consumer Goods", "Mid Cap"),
    "KFIN TECHNOLOGIES": ("Financial Services", "Mid Cap"),
    "WELSPUN LIVING": ("Consumer Goods", "Mid Cap"),
    "SAPPHIRE FOODS": ("Consumer Goods", "Mid Cap"),
    "NUVAMA WEALTH": ("Financial Services", "Mid Cap"),
    "MOTILAL OSWAL": ("Financial Services", "Mid Cap"),
    "VENUS PIPES": ("Industrials", "Small Cap"),
    "KAYNES TECHNOLOGY": ("IT", "Small Cap"),
    "AETHER INDUSTRIES": ("Chemicals", "Small Cap"),
}


def lookup_sector(name: str) -> Tuple[str, str]:
    if not name or not isinstance(name, str):
        return ("Others", "Unknown")

    name_upper = name.upper().strip()
    for key, value in SECTOR_MAP.items():
        if key in name_upper:
            return value

    return ("Others", "Unknown")
