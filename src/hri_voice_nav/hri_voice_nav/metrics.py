import csv
import os
import time
from datetime import datetime


def registrar_metrica(nombre_archivo, turn_id, etapa, duracion_ms, extra=''):
    """
    Añade una fila de métrica a un CSV, creando la cabecera si el archivo no existe.

    nombre_archivo: ruta del CSV, específica por nodo (evita escritura concurrente entre procesos)
    turn_id: identificador de la interacción, para poder cruzar datos entre nodos
    etapa: nombre de la etapa medida (ej. 'stt', 'nlu_reglas', 'nlu_llm', 'tts')
    duracion_ms: duración medida, en milisegundos
    extra: campo libre opcional (ej. 'fuente=reglas', o cualquier dato adicional relevante)
    """
    existe = os.path.isfile(nombre_archivo)

    with open(nombre_archivo, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not existe:
            writer.writerow(['timestamp_iso', 'turn_id', 'etapa', 'duracion_ms', 'extra'])
        writer.writerow([
            datetime.now().isoformat(),
            turn_id,
            etapa,
            round(duracion_ms, 2),
            extra,
        ])


def cronometro():
    """Devuelve un timestamp de alta precisión, para medir duraciones con time.perf_counter()."""
    return time.perf_counter()