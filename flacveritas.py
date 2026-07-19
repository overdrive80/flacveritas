#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
flacveritas - detector de FLAC "falsos" (transcodificados desde lossy)

A diferencia de fakeflac o Fakin' The Funk, no se limita a medir hasta
dónde llega el espectro: analiza la FORMA del corte. Un códec lossy deja
un muro casi vertical (transición estrecha y caída profunda); una fuente
analógica o remuestreada deja una pendiente suave. Eso reduce mucho los
falsos positivos con material antiguo.

Uso:
    python flacveritas.py "D:\\Musica"              # recursivo por defecto
    python flacveritas.py cancion.flac --detalle
    python flacveritas.py "D:\\Musica" --csv resultados.csv --hilos 4

Requisitos: python 3.8+, numpy, scipy y ffmpeg en el PATH.
"""

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import numpy as np
from scipy.fft import rfft
from scipy.ndimage import uniform_filter1d
from scipy.signal.windows import hann

EXTENSIONES = {".flac", ".wav", ".ape", ".wv", ".m4a", ".aiff", ".aif"}

# cortes típicos de codificadores conocidos (Hz aproximados)
FIRMAS_LOSSY = [
    (11000, "MP3 ~64 kbps u origen muy degradado"),
    (15500, "MP3 ~96-112 kbps"),
    (16000, "MP3 128 kbps (LAME/FhG)"),
    (17500, "MP3 ~160 kbps"),
    (18500, "MP3 ~192 kbps / AAC ~128"),
    (19500, "MP3 ~224-256 kbps"),
    (20500, "MP3 320 kbps (LAME -b 320)"),
]


@dataclass
class Resultado:
    ruta: str
    veredicto: str        # LOSSLESS / SOSPECHOSO / LOSSY / ERROR
    porcentaje: int       # % del espectro con contenido (comparable a fakeflac)
    f_inicio: float       # Hz donde empieza a caer la señal
    f_fin: float          # Hz donde toca el suelo de ruido
    anchura: float        # anchura de la transición en Hz
    caida: float          # profundidad de la caída en dB
    detalle: str


def sondear(ruta):
    """Lee frecuencia de muestreo y bits declarados con ffprobe."""
    orden = ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate,bits_per_raw_sample,bits_per_sample,duration",
             "-of", "json", ruta]
    proc = subprocess.run(orden, capture_output=True, timeout=60)
    import json
    flujo = json.loads(proc.stdout or "{}").get("streams", [{}])[0]
    fs = int(flujo.get("sample_rate", 44100))
    bits = int(flujo.get("bits_per_raw_sample") or flujo.get("bits_per_sample") or 16)
    try:
        dur = float(flujo.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    return fs, bits, dur


def bits_reales(ruta, segundos=30):
    """Comprueba si un archivo de 24 bits usa de verdad esos bits."""
    # sin mezclar a mono: la media de canales generaría bits bajos falsos
    orden = ["ffmpeg", "-v", "error", "-i", ruta, "-t", str(segundos),
             "-f", "s32le", "-"]
    proc = subprocess.run(orden, capture_output=True, timeout=300)
    x = np.frombuffer(proc.stdout, dtype=np.int32)
    if len(x) == 0:
        return None
    # en s32, el audio de 24 bits ocupa los bits 8-31; si los bits 8-15
    # están siempre a cero, solo hay 16 bits reales de contenido
    return bool(np.any((x >> 8) & 0xFF))


def decodificar(ruta, segundos, fs, inicio=0.0):
    """Decodifica con ffmpeg a mono float32 vía tubería (sin temporales).

    Con inicio > 0 se salta la intro: los principios de tema suelen tener
    poco contenido agudo y provocan falsos avisos.
    """
    orden = ["ffmpeg", "-v", "error"]
    if inicio > 0:
        orden += ["-ss", f"{inicio:.1f}"]
    orden += ["-i", ruta, "-t", str(segundos), "-ac", "1", "-ar", str(fs),
              "-f", "f32le", "-"]
    proc = subprocess.run(orden, capture_output=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace").strip() or "fallo de ffmpeg")
    audio = np.frombuffer(proc.stdout, dtype=np.float32)
    if len(audio) < fs * 5:
        raise RuntimeError("audio demasiado corto o vacío")
    return fs, audio


def espectro_medio(fs, audio):
    """Espectro promediado en ventanas Hann de 1 s, en dB relativos al pico."""
    n_seg = len(audio) // fs
    ventana = hann(fs)
    acumulado = np.zeros(fs // 2 + 1)
    for t in range(n_seg):
        acumulado += np.abs(rfft(audio[t * fs:(t + 1) * fs] * ventana))
    acumulado /= n_seg
    db = 20 * np.log10(np.maximum(acumulado, 1e-12))
    db -= db.max()
    # suavizado con media móvil de 50 Hz sin artefactos en los bordes
    return uniform_filter1d(db, size=50, mode="nearest")


def analizar_corte(db, fs):
    """Localiza el corte y mide su anchura y profundidad."""
    nyquist = fs // 2
    base = float(np.mean(db[10000:14000]))          # nivel de referencia
    suelo = float(np.median(db[nyquist - 300:nyquist - 50]))
    caida = base - suelo

    if caida < 15:
        # hay energía hasta Nyquist: espectro completo
        return nyquist, nyquist, 0.0, caida

    umbral_alto = base - 10          # aún hay señal
    umbral_bajo = suelo + max(6.0, caida * 0.15)  # ya es ruido

    con_senal = np.where(db[:nyquist] > umbral_alto)[0]
    f_inicio = float(con_senal.max()) if len(con_senal) else 0.0
    # f_fin: primer punto tras f_inicio donde la señal ya es solo ruido
    tramo = db[int(f_inicio):nyquist]
    bajo = np.where(tramo < umbral_bajo)[0]
    f_fin = f_inicio + float(bajo[0]) if len(bajo) else float(nyquist)
    return f_inicio, f_fin, f_fin - f_inicio, caida


def clasificar(f_inicio, f_fin, anchura, caida, fs):
    nyquist = fs // 2
    pct = int(round(min(f_fin, nyquist) * 100.0 / nyquist))

    muro = anchura < 700 and caida > 25       # transición estrecha y profunda

    # corte pegado a Nyquist: lossless, salvo que sea un muro estrecho
    if f_inicio >= nyquist - 1500:
        if muro:
            return "SOSPECHOSO", pct, (
                f"muro estrecho ({anchura:.0f} Hz) en {f_inicio/1000:.1f} kHz pegado a Nyquist "
                "— posible AAC/Vorbis de alta tasa; revisar espectrograma"
            )
        return "LOSSLESS", pct, "espectro completo hasta cerca de Nyquist"
    pendiente = anchura >= 1000                # caída ancha y progresiva

    if muro and f_inicio < 20800:
        firma = min(FIRMAS_LOSSY, key=lambda x: abs(x[0] - f_inicio))
        cercania = abs(firma[0] - f_inicio)
        pista = firma[1] if cercania < 800 else "códec lossy no identificado"
        return "LOSSY", pct, f"muro en {f_inicio/1000:.1f} kHz ({anchura:.0f} Hz de transición) — compatible con {pista}"

    if pendiente:
        return "LOSSLESS", pct, (
            f"caída progresiva de {anchura/1000:.1f} kHz desde {f_inicio/1000:.1f} kHz "
            "— típica de filtro antialiasing o remuestreo, no de un códec"
        )

    # zona gris: corte moderado sin forma clara
    if f_inicio < 17000:
        if caida > 40:
            return "LOSSY", pct, f"contenido solo hasta {f_inicio/1000:.1f} kHz con caída profunda — origen muy limitado en ancho de banda"
        return "SOSPECHOSO", pct, (
            f"poca energía aguda (corte suave en {f_inicio/1000:.1f} kHz, caída de solo {caida:.0f} dB) "
            "— compatible con máster oscuro o fuente analógica con siseo; revisar a mano"
        )
    return "SOSPECHOSO", pct, (
        f"corte en {f_inicio/1000:.1f} kHz con transición intermedia ({anchura:.0f} Hz) — revisar espectrograma a mano"
    )


def generar_espectrograma(ruta, fs, audio, f_ini, f_fin, veredicto, carpeta):
    """Guarda un PNG del espectrograma con el corte detectado marcado."""
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
        from scipy.signal import spectrogram as sg
    except ImportError:
        return None
    os.makedirs(carpeta, exist_ok=True)
    frec, tiempo, potencia = sg(audio, fs=fs, nperseg=4096, noverlap=2048)
    db = 10 * np.log10(np.maximum(potencia, 1e-12))
    tope = db.max()

    fig = Figure(figsize=(14, 6), dpi=100)
    FigureCanvasAgg(fig)
    eje = fig.add_subplot(111)
    malla = eje.pcolormesh(tiempo, frec / 1000.0, db, cmap="inferno",
                           vmin=tope - 110, vmax=tope, shading="auto")
    if 0 < f_ini < fs / 2 - 100:
        eje.axhline(f_ini / 1000.0, color="cyan", ls="--", lw=1,
                    label=f"inicio del corte ({f_ini/1000:.1f} kHz)")
        eje.axhline(f_fin / 1000.0, color="lime", ls="--", lw=1,
                    label=f"suelo de ruido ({f_fin/1000:.1f} kHz)")
        eje.legend(loc="lower right", fontsize=8)
    eje.set_xlabel("tiempo (s, tramo analizado)")
    eje.set_ylabel("kHz")
    eje.set_title(f"[{veredicto}] {os.path.basename(ruta)}", fontsize=10)
    fig.colorbar(malla, ax=eje, label="dB")
    fig.tight_layout()

    nombre = os.path.splitext(os.path.basename(ruta))[0] + ".png"
    destino = os.path.join(carpeta, nombre)
    fig.savefig(destino)
    return destino


def segunda_opinion_aucdtect(exe, ruta, segundos):
    """Pasa el archivo por auCDtect (necesita WAV 16 bits / 44,1 kHz / estéreo).

    Devuelve (veredicto, probabilidad) con veredicto CDDA o MPEG, o None si falla.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    try:
        deco = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", ruta, "-t", str(segundos),
             "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", tmp.name],
            capture_output=True, timeout=300)
        if deco.returncode != 0:
            return None
        proc = subprocess.run([exe, "-m0", tmp.name], capture_output=True, timeout=300)
        texto = (proc.stdout + proc.stderr).decode(errors="replace")
        casa = re.search(r"looks like (CDDA|MPEG) with probability (\d+)%", texto)
        if not casa:
            return None
        return casa.group(1), int(casa.group(2))
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def analizar_archivo(ruta, segundos, aucdtect=None, aucdtect_todos=False, grafico=None, grafico_todos=False):
    try:
        fs, bits, dur = sondear(ruta)
        fs = min(fs, 192000)
        # analizar el tramo central del tema, saltando la intro
        inicio = max(0.0, min(dur * 0.25, dur - segundos)) if dur > segundos else 0.0
        _, audio = decodificar(ruta, segundos, fs, inicio)
        db = espectro_medio(fs, audio)
        f_ini, f_fin, anchura, caida = analizar_corte(db, fs)
        veredicto, pct, detalle = clasificar(f_ini, f_fin, anchura, caida, fs)

        # falso hi-res 1: 24 bits declarados con solo 16 reales
        if bits >= 24 and veredicto != "ERROR":
            usa24 = bits_reales(ruta, min(segundos, 30))
            if usa24 is False:
                veredicto = "SOSPECHOSO" if veredicto == "LOSSLESS" else veredicto
                detalle += f" | {bits} bits declarados pero solo 16 con contenido (upscaling de bits)"

        # falso hi-res 2: fs alta con contenido solo hasta ~22 kHz
        if fs > 48000 and veredicto in ("LOSSLESS", "SOSPECHOSO") and f_fin < fs * 0.27:
            veredicto = "SOSPECHOSO"
            detalle += (f" | {fs/1000:.0f} kHz declarados pero contenido solo hasta "
                        f"{f_fin/1000:.1f} kHz (posible upsampling desde 44,1/48)")

        # segunda opinión con auCDtect (rastros de cuantización del códec)
        if aucdtect and (aucdtect_todos or veredicto == "SOSPECHOSO"):
            opinion = segunda_opinion_aucdtect(aucdtect, ruta, segundos)
            if opinion is None:
                detalle += " | auCDtect: sin resultado"
            else:
                clase, prob = opinion
                detalle += f" | auCDtect: {clase} {prob}%"
                if clase == "MPEG" and prob >= 70:
                    veredicto = "LOSSY"

        # espectrograma PNG para revisar a mano sin abrir Spek
        if grafico and (grafico_todos or veredicto in ("SOSPECHOSO", "LOSSY")):
            png = generar_espectrograma(ruta, fs, audio, f_ini, f_fin, veredicto, grafico)
            if png:
                detalle += f" | espectrograma: {png}"

        return Resultado(ruta, veredicto, pct, f_ini, f_fin, anchura, caida, detalle)
    except Exception as exc:
        return Resultado(ruta, "ERROR", 0, 0, 0, 0, 0, str(exc))


def recolectar(entrada):
    if os.path.isfile(entrada):
        return [entrada]
    archivos = []
    for raiz, _, nombres in os.walk(entrada):
        for nombre in nombres:
            if os.path.splitext(nombre)[1].lower() in EXTENSIONES:
                archivos.append(os.path.join(raiz, nombre))
    return sorted(archivos)


COLORES = {"LOSSLESS": "\033[92m", "SOSPECHOSO": "\033[93m", "LOSSY": "\033[91m", "ERROR": "\033[95m"}


def pintar(res, usar_color, detalle):
    etiqueta = f"[{res.veredicto:>10}]"
    if usar_color:
        etiqueta = f"{COLORES[res.veredicto]}{etiqueta}\033[0m"
    linea = f"{etiqueta} {res.porcentaje:3d}%  {res.ruta}"
    print(linea)
    if detalle or res.veredicto in ("LOSSY", "SOSPECHOSO", "ERROR"):
        print(f"{'':13}└─ {res.detalle}")


def principal():
    parser = argparse.ArgumentParser(description="detector de FLAC transcodificados desde lossy")
    parser.add_argument("entrada", help="archivo o carpeta (se recorre recursivamente)")
    parser.add_argument("--segundos", type=int, default=60, help="segundos a analizar por archivo (60 por defecto)")
    parser.add_argument("--hilos", type=int, default=max(2, (os.cpu_count() or 2) // 2), help="análisis en paralelo")
    parser.add_argument("--csv", help="volcar resultados a un CSV")
    parser.add_argument("--detalle", action="store_true", help="mostrar el motivo también en los LOSSLESS")
    parser.add_argument("--solo-malos", action="store_true", help="mostrar solo LOSSY, SOSPECHOSO y ERROR")
    parser.add_argument("--aucdtect", metavar="EXE", help="ruta a auCDtect; se usa como segunda opinión en los SOSPECHOSO")
    parser.add_argument("--aucdtect-todos", action="store_true", help="pasar auCDtect a todos los archivos, no solo a los sospechosos")
    parser.add_argument("--grafico", nargs="?", const="espectrogramas", metavar="CARPETA",
                        help="guardar PNG del espectrograma de los SOSPECHOSOS y LOSSY (carpeta 'espectrogramas' por defecto); requiere matplotlib")
    parser.add_argument("--grafico-todos", action="store_true", help="guardar espectrograma de todos los archivos")
    args = parser.parse_args()

    if args.aucdtect and not (os.path.isfile(args.aucdtect) or shutil.which(args.aucdtect)):
        print(f"no encuentro el ejecutable de auCDtect: {args.aucdtect}")
        sys.exit(2)

    if args.grafico:
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            print("--grafico necesita matplotlib:  pip install matplotlib")
            sys.exit(2)

    archivos = recolectar(args.entrada)
    if not archivos:
        print("no se han encontrado archivos de audio en la ruta indicada")
        sys.exit(2)

    usar_color = sys.stdout.isatty()
    if os.name == "nt":
        os.system("")  # habilita códigos ANSI en la consola de Windows

    resultados = []
    with ThreadPoolExecutor(max_workers=args.hilos) as ejecutor:
        tareas = {ejecutor.submit(analizar_archivo, ruta, args.segundos, args.aucdtect, args.aucdtect_todos, args.grafico, args.grafico_todos): ruta for ruta in archivos}
        for tarea in as_completed(tareas):
            res = tarea.result()
            resultados.append(res)
            if args.solo_malos and res.veredicto == "LOSSLESS":
                continue
            pintar(res, usar_color, args.detalle)

    total = len(resultados)
    malos = sum(1 for r in resultados if r.veredicto == "LOSSY")
    dudosos = sum(1 for r in resultados if r.veredicto == "SOSPECHOSO")
    errores = sum(1 for r in resultados if r.veredicto == "ERROR")
    print(f"\nresumen: {total} archivos — {total - malos - dudosos - errores} lossless, "
          f"{malos} lossy, {dudosos} sospechosos, {errores} errores")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as salida:
            escritor = csv.writer(salida, delimiter=";")
            escritor.writerow(["archivo", "veredicto", "porcentaje", "corte_inicio_hz",
                               "corte_fin_hz", "anchura_hz", "caida_db", "detalle"])
            for r in sorted(resultados, key=lambda x: x.ruta):
                escritor.writerow([r.ruta, r.veredicto, r.porcentaje, f"{r.f_inicio:.0f}",
                                   f"{r.f_fin:.0f}", f"{r.anchura:.0f}", f"{r.caida:.1f}", r.detalle])
        print(f"csv guardado en {args.csv}")

    sys.exit(1 if malos or dudosos else 0)


if __name__ == "__main__":
    principal()
