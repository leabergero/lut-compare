# LUT compare

Aplicación de escritorio para **comparar una foto con dos presets de color a la vez**, pensada para elegir rápido el look correcto sobre tandas de fotos (por ejemplo, las que bajás del iPhone).

*Desktop app to compare a photo against two color presets side by side, with a blended full-width comparison view. Supports Lightroom `.xmp` presets and standard `.cube` 3D LUTs.*

## Qué hace

- **Vista grande fundida por tercios**: la misma foto continua, con el tercio izquierdo original, el centro con el preset 1 y la derecha con el preset 2, fundiéndose gradualmente de un look al otro (ancho del fundido ajustable, u opción de corte neto). Un selector arriba deja ver solo uno de los tres agrandado, en vez del fundido, para juzgar mejor el resultado final.
- **Tres paneles individuales** debajo: original + los dos presets, cada uno con su desplegable, su control de intensidad (0–100 %) y su botón de guardar.
- **Navegador de carpeta**: elegís una carpeta y la tira de miniaturas muestra todas las fotos; pasás con clic, flechas del teclado o la rueda del mouse.
- **Dos formatos de preset**, leídos recursivamente (con subcarpetas) desde una carpeta configurable (botones "Abrir carpeta" / "Cambiar carpeta..."):
  - `.xmp` — presets de Lightroom / Adobe Camera Raw (balance de blancos, exposición, contraste, altas luces/sombras, curvas de tono, vibrance, mezcla HSL, calibración, viñeta y grano). El render es una aproximación al de Adobe: muy cercana, no idéntica píxel a píxel.
  - `.cube` — LUTs 3D estándar (Resolve, Premiere, etc.), con interpolación trilineal.
  Tirá archivos nuevos en la carpeta (o en subcarpetas, que aparecen agrupadas con subtítulo) y tocá "Releer presets". Marcá presets favoritos con la estrella del desplegable: quedan siempre arriba de todo.
- **Guardado no destructivo**: cada guardado procesa la foto a resolución completa y la deja en una subcarpeta `editadas/` dentro de tu carpeta de fotos, como `nombre_preset_timestamp.jpg`, conservando los metadatos EXIF. Los originales nunca se tocan. "Guardar fundido" exporta la vista grande tal cual se ve, con las 2 LUTs fundidas.
- **Aplicar a todas**: procesa la carpeta entera con el preset elegido, en paralelo por núcleos.
- **Zoom continuo sincronizado**: rueda del mouse sobre cualquiera de las 4 vistas (los 3 paneles + la grande) para acercarte al detalle, con paneo por arrastre; las 4 se mueven juntas.
- **Favoritas**: marcá fotos con `F` y filtrá la tira para ver solo esas.
- Lee JPG, PNG, TIFF, WebP, BMP, HEIC/HEIF (fotos de iPhone directas) y
  **RAW de camara** (DNG, CR2/CR3, NEF, ARW, RAF, ORF, RW2, PEF y mas, via
  libraw): las previews se revelan a media resolucion para que sea fluido y
  las miniaturas usan el JPEG embebido del RAW; al guardar se revela a
  resolucion completa.

## Rendimiento

Diseñada para sentirse fluida incluso en máquinas modestas:

- Cada preset `.xmp` se "hornea" una única vez en una LUT 3D interna de 41³ muestras; después toda foto se procesa por el camino rápido de interpolación (los efectos espaciales — viñeta, grano, claridad — se aplican aparte).
- Los JPEG se decodifican ya reducidos (draft mode) para previews y miniaturas.
- Preview en dos etapas: una pasada instantánea a baja resolución y el refinado a calidad completa detrás.
- Precarga en silencio la foto siguiente y la anterior de la tira.
- Cachés LRU en uint8 y procesado por bloques para acotar el pico de memoria (~100 MB incluso con fotos de 12 MP).

## Instalación

### Instaladores (recomendado)

Los `.deb` (Linux), `.msi` (Windows) y `.pkg` (macOS) están en
[`installers/`](installers/). El venv con las dependencias se crea en el
primer arranque (necesita internet esa única vez); no requieren permisos
de administrador (Linux instala en `/opt`, Windows y macOS por-usuario).

```bash
sudo apt install ./installers/lut-compare_*_all.deb   # Linux
```

En Windows, doble clic en el `.msi`: crea el acceso directo "LUT Compare"
en el menú Inicio. En macOS, doble clic en el `.pkg`: instala
"LUT Compare.app" en Aplicaciones.

Para instalar desde el código fuente, requiere **Python 3.9 o superior**
([python.org](https://www.python.org/downloads/)):

### Windows — desde el código

```bat
git clone https://github.com/leabergero/lut-compare.git
cd lut-compare
python -m pip install -r requirements.txt
```

Para abrirla: doble clic en `LUT Compare.bat`, o `python app.py`.

### Linux

```bash
git clone https://github.com/leabergero/lut-compare.git
cd lut-compare
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

En distribuciones sin las librerías gráficas de Qt (instalación mínima de Debian/Ubuntu), puede hacer falta:

```bash
sudo apt install libxcb-cursor0 libgl1
```

### macOS

```bash
git clone https://github.com/leabergero/lut-compare.git
cd lut-compare
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Uso

1. Tocá **Examinar…** (barra inferior) y elegí la carpeta con tus fotos.
2. Elegí un preset en cada desplegable; la vista grande y los paneles se actualizan al instante.
3. Ajustá la **intensidad** si el preset queda fuerte.
4. **Guardar** en cada panel (o teclas `1`/`2`/`3`) exporta esa versión a `editadas/`.
5. **Aplicar a todas** procesa la carpeta completa con ese preset.

### Atajos de teclado

| Tecla | Acción |
|---|---|
| `←` / `→` | Foto anterior / siguiente |
| `1` / `2` / `3` | Guardar original / panel 2 / panel 3 |
| `F` | Marcar/desmarcar favorita |
| Rueda del mouse en un panel | Zoom continuo sincronizado (arrastrar para mover) |
| Clic en panel | Salta a 3× / vuelve a ajustar |
| Rueda del mouse en la tira | Desplazarse por las miniaturas |

## Presets

La app busca `.xmp` y `.cube` en la carpeta `presets/` junto a `app.py` (y en sus subcarpetas, que aparecen agrupadas por nombre en el desplegable) — o en la que elijas con "Cambiar carpeta...". El repo incluye una LUT de ejemplo (`Calido suave.cube`); tus presets personales de Lightroom no se distribuyen con el repo — exportalos desde Lightroom (clic derecho sobre el preset → *Export*) y copialos ahí.

## Estructura

```
lut-compare/
├── app.py             # interfaz (PySide6): paneles, fundido, navegador, guardado
├── preset_engine.py   # motor: parser .xmp/.cube, horneado a LUT 3D, render numpy
├── presets/           # tus .xmp y .cube van acá (con subcarpetas si querés)
├── requirements.txt
├── LUT Compare.bat    # lanzador para Windows (desde el código)
├── installers/        # .deb / .msi / .pkg ya generados
└── packaging/         # scripts que generan los instaladores (ver packaging/README.md)
```

## Limitaciones conocidas

- El algoritmo de revelado de Adobe es cerrado: el render de `.xmp` es una aproximación fiel del *look*, no una réplica exacta píxel a píxel de Lightroom.
- Nitidez y reducción de ruido del preset se omiten (no afectan el color).
- Al exportar desde un RAW, el JPG resultante no conserva el EXIF original;
  desde JPG/HEIC/TIFF el EXIF se conserva siempre.
- Solo se soportan LUTs 3D en `.cube` (no 1D ni `.3dl`).

## Licencia

[MIT](LICENSE)
