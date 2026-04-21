import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-insecure-change-me")
DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"
DATABASE_PATH = BASE_DIR / os.getenv("DATABASE_PATH", "data/pildora.db")

CIMA_API_BASE = os.getenv("CIMA_API_BASE", "https://cima.aemps.es/cima/rest")
NOMENCLATOR_CSV_URL = os.getenv(
    "NOMENCLATOR_CSV_URL",
    "https://www.sanidad.gob.es/profesionales/nomenclator.do?accion=buscarTodas&formato=csv",
)

# Tiempo de caché (segundos) antes de volver a consultar CIMA para el mismo medicamento
CACHE_TTL = int(os.getenv("CACHE_TTL", "86400"))  # 24 h
