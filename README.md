# ☁️ FileJump Sync Studio

> **Descripción corta sugerida para GitHub (About):**
> Explorador dual estilo Windows, sincronizador de archivos de alta velocidad y centro multimedia web para la nube de FileJump construido en Python (Flask) + Tailwind CSS.
>
> **Topics sugeridos:** `filejump`, `cloud-sync`, `python`, `flask`, `web-audio-api`, `file-explorer`, `mp3-player`, `equalizer`

---

**FileJump Sync Studio** es una suite web local desarrollada en Python y Flask que combina un **comparador y sincronizador bidireccional/incremental** para **FileJump REST API**, un **explorador dual estilo Windows** (Equipo Local vs. Nube FileJump) y un reproductor multimedia con playlists M3U, favoritos sincronizados y perfiles de sincronización por equipo.

---

## ✨ Características Principales

### 🔄 Sincronización y Comparación Inteligente
* **Vista Dual Comparativa (Local vs. Nube):** Identifica de un vistazo archivos **Sincronizados**, **Nuevos** (pendientes de subir), **Modificados** (por tamaño o fecha UTC) y **Solo en FileJump**.
* **Subida Paralela de Alta Velocidad:** Soporte multihilo (`ThreadPoolExecutor`) y conexión directa al endpoint de subida rápida de FileJump.
* **Protección contra Cambios Masivos:** Umbral de impacto configurable (%) que solicita confirmación explícita antes de sobrescribir o alterar grandes volúmenes de archivos.
* **Gestión Segura de Credenciales:** Configuración visual de tokens `Bearer` y URLs almacenadas localmente en `.env`.
* **Perfiles por equipo en la nube:** Descarga perfiles de sincronización desde `filejump_sync_studio/<NOMBRE_EQUIPO>/sync_profiles.json` al iniciar y publica automáticamente los cambios realizados a los perfiles locales.

### 🗂️ Explorador Dual Estilo Windows
* **Panel Dividido (Árbol + Contenido):** Árbol jerárquico lateral izquierdo con **carga perezosa (`+` / `−`)** bajo demanda y tabla de contenido a la derecha con barra de ruta (*breadcrumbs*) y navegación por doble clic.
* **Exploración Completa de Discos Locales:** Detección automática de unidades locales (`C:\`, `D:\`, `E:\`, montajes Unix) con apertura directa sin duplicar archivos en caché.
* **Diseño Adaptable:** Los exploradores ocupan el ancho disponible y se ajustan a la altura de la ventana en escritorio; en pantallas estrechas apilan árbol y contenido, priorizando nombres y estados de archivos.

### 🎬 Visor Universal de Archivos y Caché Persistente
* **Caché Persistente (`temp_cache`):** Descarga archivos remotos únicamente bajo demanda y conserva las copias al cerrar el servidor. Al abrir un archivo en caché, verifica la fecha de modificación remota y descarga la versión más reciente cuando detecta cambios. Si cambia la API key, elimina la caché asociada a las credenciales anteriores.
* **Soporte Multiformato:**
  * **PDF:** Apertura directa en nueva ventana con visor nativo.
  * **Ofimática (`.docx`, `.xlsx`, `.pptx`, `.odt`, `.ods`):** Extracción y renderizado de vista previa estructurada sin dependencias externas pesadas.
  * **Texto y Código (`.txt`, `.log`, `.json`, `.py`, `.md`, `.csv`, `.sql`, etc.):** Lector monoespaciado integrado.
  * **Imágenes y Videos:** Visor integrado con carrusel (`◀ Anterior` / `Siguiente ▶` y atajos de teclado `←` `→` `Esc`) y soporte opcional de transcodificación con FFmpeg para formatos `.avi`/`.mkv`.

### 🎵 Reproductor Musical Flotante (ID3 + EQ 5 Bandas + Visualizador)
* **3 Modos de Vista:** Minimizado en burbuja compacta tipo chat en vivo, panel flotante normal o vista **Maximizada** con carátula en gran tamaño y lista de reproducción (*Playlist*).
* **Metadatos musicales:** Extrae Título, Artista, Álbum, **género** desde `TCON`/ID3v1 y metadatos compatibles, y **tonalidad DJ** desde `TKEY` o campos `TXXX` (como `INITIALKEY`, `KEY` y `CAMELOT`). Para formatos no ID3 también consulta etiquetas de `ffprobe`, si está instalado.
* **Carátula y letras incrustadas:** Lee la carátula (`APIC`) y letras desde `USLT` o etiquetas `TXXX` del archivo; permite ampliar y descargar la carátula. El panel de letras muestra únicamente contenido incrustado en la pista y puede ocultarse con una preferencia persistida localmente.
* **Precarga en Caché + Modo Aleatorio:** Genera colas aleatorias y descarga en segundo plano la siguiente canción remota para una reproducción sin interrupciones.
* **Ecualizador Gráfico de 5 Bandas (`Web Audio API`):** Bandas de `60Hz`, `230Hz`, `910Hz`, `3.6kHz` y `14kHz` (`-12 dB` a `+12 dB`) con presets (*Plano, Bass Boost, Electrónica, Pop, Rock, Vocal*).
* **Visualizador Rítmico en Tiempo Real:** Analizador de espectro en `<canvas>` activable/desactivable con 3 modos visuales (*Barras, Espejo, Onda*).
* **Playlists M3U en la nube:** Guarda la cola actual como `.m3u` estándar en `filejump_sync_studio/playlist/`, con metadatos `#EXTINF` y `#EXTFJ`; se evita reemplazar una lista existente con el mismo nombre.
* **Gestor de playlists guardadas:** Explora las listas remotas y su número de pistas, selecciónalas, reprodúcelas o elimínalas desde el reproductor maximizado. También puedes abrir archivos `.m3u` y `.m3u8` directamente desde ambos exploradores.
* **Favoritos sincronizados:** Marca pistas desde el reproductor para mantener favoritos en `favorites.json` y generar automáticamente `filejump_sync_studio/playlist/favorites.m3u`, con una copia local de respaldo.
* **Sugerencia de nombre para M3U:** Propone el nombre de la carpeta común de las pistas o, si pertenecen a distintas ubicaciones, `playlist-DD-MM-YYYY`.

### 🧰 Estabilidad y compatibilidad
* **Listados remotos completos:** La paginación de archivos y carpetas contempla metadatos `meta`, `links`, `next_page` y `last_page`, y evita repetir páginas si la API ignora el parámetro solicitado.
* **Escaneo sin bloquear la interfaz:** La búsqueda recursiva de audio se ejecuta en segundo plano e informa el progreso mientras se siguen usando los exploradores.
* **Perfiles únicos por equipo:** Al actualizar perfiles se reemplaza la copia remota anterior para conservar un único `sync_profiles.json` por equipo.
* **Gestor de playlists estable:** La función global `escapeHtml()` evita errores al renderizar los nombres de las listas guardadas.
* **Metadatos y fechas preservados:** Las playlists M3U guardan los datos necesarios para resolver archivos locales y remotos; las transferencias conservan la fecha de modificación original cuando está disponible.
* **Servidor Flask concurrente:** Atiende solicitudes en paralelo para que la reproducción y las operaciones de la interfaz no se bloqueen entre sí.

---

## 🛠️ Requisitos e Instalación

1. **Clonar el repositorio:**
   ```bash
   git clone https://github.com/totoday72/FileJump-Sync-Web-Studio.git
   cd FileJump-Sync-Web-Studio
   ```

2. **Instalar dependencias de Python:**
   ```bash
   pip install -r requirements.txt
   ```
   *(Opcional)* Instala **FFmpeg** en tu sistema si deseas convertir al vuelo videos `.avi` o `.mkv` dentro del navegador.

3. **Ejecutar la aplicación:**
   ```bash
   python filejump_sync_web_studio.py
   ```
   En Windows también puedes iniciar la aplicación con `exec.bat`. Python debe estar instalado y disponible en las variables de entorno `PATH`.

4. **Abrir en el navegador:**
   Ingresa a `http://127.0.0.1:5000`, abre **Configuración & .env**, pega tu `FILEJUMP_API_KEY` y añade tus carpetas locales a sincronizar.

---

## 📁 Estructura del Proyecto

```text
├── filejump_sync_web_studio.py # Punto de entrada Flask
├── config_manager.py       # Configuración, perfiles por equipo y rutas locales de datos
├── filejump_client.py      # Cliente REST de FileJump y operaciones de playlists/favoritos
├── routes_api.py           # Endpoints de la aplicación
├── sync_engine.py          # Comparación y sincronización
├── media_utils.py          # Metadatos y utilidades multimedia
├── templates/index.html    # Interfaz principal
├── static/js/              # Explorador, reproductor y lógica de interfaz
├── static/css/styles.css   # Estilos de la aplicación
├── requirements.txt        # Dependencias de Python
├── .env                    # Credenciales API (ignorado en Git)
├── sync_config.json        # Tareas y perfiles de sincronización (local)
├── sync_records/           # Historial local de sincronización
├── temp_cache/             # Caché local persistente de archivos remotos
└── playlists/              # Playlists M3U locales y respaldo de favoritos
```

---

## 🔒 Datos locales

`.env`, `sync_config.json`, `sync_records/`, `temp_cache/`, `favorites_cache.json` y `playlists/` contienen credenciales, caché o datos del usuario y están excluidos del repositorio mediante `.gitignore`. Los perfiles por equipo y las playlists seleccionadas pueden sincronizarse con FileJump en `filejump_sync_studio/`.
