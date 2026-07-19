# flacveritas

Detector de FLAC «falsos»: archivos lossless que en realidad provienen de una fuente con pérdida (MP3, AAC...) o que inflan artificialmente su resolución (falso hi-res).

## 1. Descripción del programa

`flacveritas.py` analiza archivos de audio lossless y determina si su contenido procede de una fuente genuina o de una transcodificación. A diferencia de otros detectores (fakeflac, Fakin' The Funk), no se limita a medir hasta qué frecuencia llega el espectro: analiza la **forma del corte espectral**. La diferencia es clave para reducir falsos positivos con material antiguo:

- un códec lossy deja un **muro casi vertical**: transición estrecha (menos de ~700 Hz) y caída profunda hasta el silencio absoluto
- una fuente analógica o remuestreada deja una **pendiente suave** (1 kHz o más de anchura): es el filtro antialiasing del conversor, no un códec

Metodologías incluidas:

| Comprobación | Qué detecta |
|---|---|
| Corte espectral con porcentaje | equivalente a fakeflac, con clasificación por forma del muro |
| Firma del códec | identifica el origen probable por la frecuencia del corte (16 kHz = MP3 128, 20,5 kHz = MP3 320...) |
| Falso 24 bits | archivos que declaran 24 bits pero solo tienen 16 con contenido (relleno) |
| Falso hi-res en frecuencia | FLAC a 96/192 kHz con contenido solo hasta ~22 kHz (upsampling desde 44,1/48) |
| auCDtect (opcional) | segunda opinión estadística sobre los rastros de cuantización del códec |
| Espectrogramas PNG (opcional) | imagen con el corte marcado para revisar a mano los casos dudosos |

El análisis se hace sobre el **tramo central** de cada tema (las intros suelen tener poco contenido agudo y provocan falsos avisos) y en paralelo. Cada archivo recibe uno de estos veredictos:

- **LOSSLESS**: espectro completo o caída progresiva compatible con fuente genuina
- **LOSSY**: muro de códec detectado, con la firma probable indicada
- **SOSPECHOSO**: caso dudoso que conviene revisar a mano (el detalle explica siempre el porqué)
- **ERROR**: el archivo no se pudo decodificar

Limitación honesta: un AAC/Vorbis de tasa muy alta corta casi en Nyquist y ningún análisis espectral puede condenarlo con certeza; esos casos quedan como SOSPECHOSO y ahí es donde auCDtect ayuda a resolver.

## 2. Requisitos

- Windows, Linux o macOS
- Python 3.8 o superior
- ffmpeg y ffprobe en el PATH
- Librerías Python: numpy y scipy (obligatorias), matplotlib (solo para `--grafico`)
- Opcional: `aucdtect.exe` para la segunda opinión estadística

## 3. Instalación

En Windows:

1. Instalar Python (marcando «Add Python to PATH» en el instalador):
   ```
   winget install Python.Python.3.12
   ```
2. Instalar ffmpeg:
   ```
   winget install Gyan.FFmpeg
   ```
3. Instalar las librerías:
   ```
   pip install numpy scipy matplotlib
   ```
4. Guardar `flacveritas.py` (y opcionalmente `analizar_flac.bat`) en una carpeta cualquiera.
5. Opcional: dejar `aucdtect.exe` junto al BAT para que lo use automáticamente.

Comprobación rápida de que todo está en su sitio:

```
python --version
ffmpeg -version
```

En Linux/macOS basta con el paquete `ffmpeg` de la distribución y el mismo `pip install`.

## 4. Instrucciones de uso

```
python flacveritas.py ENTRADA [opciones]
```

`ENTRADA` puede ser un archivo suelto o una carpeta (se recorre recursivamente). Formatos aceptados: FLAC, WAV, APE, WavPack, ALAC/M4A y AIFF.

| Opción | Efecto |
|---|---|
| `--csv ARCHIVO` | volcar los resultados a un CSV (separador `;`, UTF-8 con BOM, se abre directo en Excel) |
| `--solo-malos` | mostrar únicamente LOSSY, SOSPECHOSO y ERROR |
| `--detalle` | mostrar el motivo también en los LOSSLESS |
| `--segundos N` | segundos analizados por archivo (60 por defecto) |
| `--hilos N` | análisis en paralelo (por defecto, la mitad de los núcleos) |
| `--aucdtect EXE` | pasar auCDtect como segunda opinión a los SOSPECHOSOS; si dice MPEG ≥70%, se reclasifican a LOSSY |
| `--aucdtect-todos` | pasar auCDtect a todos los archivos, no solo a los sospechosos |
| `--grafico [CARPETA]` | guardar espectrograma PNG de los SOSPECHOSOS y LOSSY (carpeta `espectrogramas` por defecto), con el corte detectado marcado |
| `--grafico-todos` | guardar espectrograma de todos los archivos |

Código de salida: 0 si todo es lossless, 1 si hay LOSSY o SOSPECHOSOS, 2 en errores de arranque (útil para scripts).

Con el envoltorio `analizar_flac.bat` (Windows): comprueba requisitos, instala numpy/scipy si faltan, detecta `aucdtect.exe` si está junto al BAT, genera un CSV con marca de tiempo y resume el resultado. Acepta la carpeta como argumento (o arrastrarla encima del BAT) y pasa cualquier opción extra al script tal cual.

## 5. Ejemplos

Escanear una colección completa y guardar informe:

```
python flacveritas.py "E:\Musica" --csv resultados.csv
```

Ver solo lo problemático, con espectrogramas para revisar a mano:

```
python flacveritas.py "E:\Musica" --solo-malos --grafico
```

Análisis con segunda opinión de auCDtect para SOSPECHOSOS:

```
python flacveritas.py "E:\Musica" --aucdtect "C:\herramientas\aucdtect.exe"
```

Análisis con segunda opinión de auCDtect para TODOS y con resultados:

```
python flacveritas.py "E:\Musica" --csv resultados.csv --aucdtect "C:\herramientas\aucdtect.exe" --aucdtect-todos
```

Comprobar un solo archivo con todo el detalle:

```
python flacveritas.py "cancion.flac" --detalle --grafico
```

Escaneo rápido de una carpeta grande (menos segundos por archivo, más hilos):

```
python flacveritas.py "E:\Musica" --segundos 30 --hilos 8 --solo-malos
```

Con el BAT en Windows:

```
analizar_flac.bat "E:\Musica"
analizar_flac.bat "E:\Musica" --solo-malos --grafico
```

Ejemplo de salida:

```
[  LOSSLESS] 100%  E:\Musica\Boytronic - You [1983].flac
[     LOSSY]  78%  E:\Musica\dudoso.flac
             └─ muro en 16.5 kHz (574 Hz de transición) — compatible con MP3 128 kbps (LAME/FhG)
[SOSPECHOSO]  70%  E:\Musica\oscuro.flac
             └─ poca energía aguda (corte suave en 14.7 kHz, caída de solo 28 dB) — compatible con máster oscuro o fuente analógica con siseo; revisar a mano | espectrograma: espectrogramas\oscuro.png

resumen: 3 archivos — 1 lossless, 1 lossy, 1 sospechosos, 0 errores
```
