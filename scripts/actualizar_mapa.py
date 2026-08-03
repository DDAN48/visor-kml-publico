import os
import json
import re
import unicodedata
from pathlib import Path
from datetime import datetime
import xml.etree.ElementTree as ET

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


# ======================================================
# CONFIGURACIÓN
# ======================================================

DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")

CARPETA_DOCS = Path("docs")
CARPETA_DATA = Path("docs/data")
CARPETA_KML_LOCAL = Path("kml_descargados")
CARPETA_STATIC_KML = Path("static_kml")

NOMBRE_JSON_AVANCE = "avance_resumen.json"

SCOPES_DRIVE = [
    "https://www.googleapis.com/auth/drive"
]

STATIC_KML_LAYERS = [
    {
        "name": "Zonas APH",
        "file": "zonas_aph.kml",
        "type": "zonas_aph",
        "layer_id": "zonas_aph",
        "grupo": "zonas_aph",
        "color": "#6A1B9A",
        "manzanas": 0,
        "porcentaje": 0,
        "nodos": 0
    }
]


# ======================================================
# UTILIDADES
# ======================================================

def normalizar_texto(valor):
    texto = str(valor or "").strip().lower()
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r"\s+", " ", texto)
    return texto


def nombre_seguro_archivo(nombre):
    texto = normalizar_texto(nombre)
    texto = texto.replace(".kml", "")
    texto = texto.replace(" ", "_")
    texto = re.sub(r"[^a-z0-9_]+", "", texto)
    return texto or "capa"


def limpiar_html(texto):
    if texto is None:
        return ""

    texto = str(texto)
    texto = re.sub(r"<br\s*/?>", "\n", texto, flags=re.IGNORECASE)
    texto = re.sub(r"</p>", "\n", texto, flags=re.IGNORECASE)
    texto = re.sub(r"<[^>]+>", "", texto)

    texto = texto.replace("&nbsp;", " ")
    texto = texto.replace("&amp;", "&")
    texto = texto.replace("&lt;", "<")
    texto = texto.replace("&gt;", ">")
    texto = texto.replace("&quot;", '"')
    texto = texto.replace("&apos;", "'")

    return texto.strip()


def color_para_capa(nombre, indice=0):
    nombre_norm = normalizar_texto(nombre)

    # Tareas finalizadas
    if "tendido" in nombre_norm:
        return "#2E7D32"   # Verde

    if "columna" in nombre_norm:
        return "#1565C0"   # Azul

    if "estado" in nombre_norm:
        return "#EF6C00"   # Naranja

    # Semana de Inicio / Planificación
    if "semana" in nombre_norm:
        return "#D64D4E"   # Rojo planificación

    return "#757575"       # Gris por defecto


def tipo_para_capa(nombre, metadata=None):
    """
    Tipo usado por el HTML para decidir si la capa entra en:
    - finalizado
    - semana
    - zonas_aph
    - general
    """
    if metadata:
        grupo = metadata.get("grupo")
        tipo = metadata.get("tipo")

        if grupo == "semana_inicio" or tipo == "semana":
            return "semana"

        if grupo == "tareas_finalizadas" or str(tipo or "").endswith("_finalizado"):
            return "finalizado"

        if grupo == "zonas_aph" or tipo == "zonas_aph":
            return "zonas_aph"

    nombre_norm = normalizar_texto(nombre)

    if "semana" in nombre_norm:
        return "semana"

    if "finalizado" in nombre_norm:
        return "finalizado"

    if "zonas_aph" in nombre_norm or "zonas aph" in nombre_norm:
        return "zonas_aph"

    return "general"


def leer_json(path):
    if not path.exists():
        return None

    return json.loads(path.read_text(encoding="utf-8"))


def escribir_json(path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def construir_indice_avance(avance):
    """
    Devuelve un diccionario para ubicar rápido la metadata de cada KML:
    {
      "julio_semana_1.kml": {...},
      "julio_tareas_de_tendido_finalizado.kml": {...}
    }
    """
    indice = {}

    if not avance:
        return indice

    for capa in avance.get("capas", []):
        kml = capa.get("kml")

        if not kml:
            continue

        indice[normalizar_texto(kml)] = capa

    return indice


def metadata_para_kml(nombre_kml, indice_avance):
    return indice_avance.get(normalizar_texto(nombre_kml), {})


def capa_base_desde_metadata(nombre_kml, metadata, idx=0):
    nombre_sin_ext = nombre_kml[:-4] if nombre_kml.lower().endswith(".kml") else nombre_kml

    color = metadata.get("color") or color_para_capa(nombre_kml, idx)
    tipo = tipo_para_capa(nombre_kml, metadata)
    layer_id = metadata.get("layer_id") or nombre_seguro_archivo(nombre_sin_ext)

    return {
        "layer_id": layer_id,
        "name": metadata.get("nombre") or nombre_sin_ext,
        "display_name": metadata.get("nombre") or nombre_sin_ext,
        "type": tipo,
        "grupo": metadata.get("grupo") or tipo,
        "tipo": metadata.get("tipo") or tipo,
        "color": color,
        "kml": nombre_kml,
        "mes_id": metadata.get("mes_id"),
        "mes_label": metadata.get("mes_label"),
        "tab_label": metadata.get("tab_label"),
        "manzanas": metadata.get("manzanas", 0),
        "porcentaje": metadata.get("porcentaje", 0),
        "nodos": metadata.get("nodos", 0)
    }


# ======================================================
# GOOGLE DRIVE
# ======================================================

def crear_drive_service():
    client_id = os.getenv("DRIVE_CLIENT_ID")
    client_secret = os.getenv("DRIVE_CLIENT_SECRET")
    refresh_token = os.getenv("DRIVE_REFRESH_TOKEN")

    if not DRIVE_FOLDER_ID:
        raise ValueError("Falta DRIVE_FOLDER_ID en secrets.")

    if not client_id:
        raise ValueError("Falta DRIVE_CLIENT_ID en secrets.")

    if not client_secret:
        raise ValueError("Falta DRIVE_CLIENT_SECRET en secrets.")

    if not refresh_token:
        raise ValueError("Falta DRIVE_REFRESH_TOKEN en secrets.")

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES_DRIVE
    )

    return build("drive", "v3", credentials=creds)


def listar_archivos_drive(service):
    archivos = []
    page_token = None

    query = (
        f"'{DRIVE_FOLDER_ID}' in parents "
        f"and trashed = false"
    )

    while True:
        response = service.files().list(
            q=query,
            spaces="drive",
            fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
            orderBy="name",
            pageToken=page_token
        ).execute()

        archivos.extend(response.get("files", []))
        page_token = response.get("nextPageToken")

        if not page_token:
            break

    return archivos


def filtrar_kml(archivos):
    return [
        a for a in archivos
        if a.get("name", "").lower().endswith(".kml")
    ]


def buscar_archivo_json_avance(archivos):
    for archivo in archivos:
        if archivo.get("name", "").lower() == NOMBRE_JSON_AVANCE.lower():
            return archivo

    return None


def descargar_archivo_drive(service, file_id, output_path):
    request = service.files().get_media(fileId=file_id)

    with open(output_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)

        done = False
        while not done:
            status, done = downloader.next_chunk()


# ======================================================
# KML XML → GEOJSON
# ======================================================

def texto_hijo(elemento, tag, ns):
    hijo = elemento.find(f"kml:{tag}", ns)

    if hijo is None:
        hijo = elemento.find(tag)

    if hijo is not None and hijo.text:
        return hijo.text.strip()

    return ""


def parsear_coordinates(coord_text):
    """
    Convierte texto KML coordinates a lista GeoJSON:
    KML: lon,lat,alt lon,lat,alt
    GeoJSON: [[lon, lat], [lon, lat]]
    """
    coords = []

    if not coord_text:
        return coords

    partes = coord_text.strip().split()

    for item in partes:
        vals = item.split(",")

        if len(vals) < 2:
            continue

        try:
            lon = float(vals[0])
            lat = float(vals[1])
            coords.append([lon, lat])
        except ValueError:
            continue

    return coords


def extraer_polygons_de_placemark(pm, ns):
    """
    Extrae todos los Polygon dentro de un Placemark,
    incluyendo los que estén dentro de MultiGeometry.
    """
    polygons = []

    polygon_elements = pm.findall(".//kml:Polygon", ns)

    if not polygon_elements:
        polygon_elements = pm.findall(".//Polygon")

    for poly_el in polygon_elements:
        # Exterior
        outer_el = poly_el.find(".//kml:outerBoundaryIs/kml:LinearRing/kml:coordinates", ns)

        if outer_el is None:
            outer_el = poly_el.find(".//outerBoundaryIs/LinearRing/coordinates")

        if outer_el is None or not outer_el.text:
            continue

        exterior = parsear_coordinates(outer_el.text)

        if len(exterior) < 4:
            continue

        # Interiores / agujeros
        holes = []

        inner_els = poly_el.findall(".//kml:innerBoundaryIs/kml:LinearRing/kml:coordinates", ns)

        if not inner_els:
            inner_els = poly_el.findall(".//innerBoundaryIs/LinearRing/coordinates")

        for inner_el in inner_els:
            if inner_el is not None and inner_el.text:
                hole = parsear_coordinates(inner_el.text)

                if len(hole) >= 4:
                    holes.append(hole)

        # GeoJSON Polygon: [exterior, hole1, hole2...]
        polygons.append([exterior] + holes)

    return polygons


def convertir_kml_a_geojson(
    kml_path,
    geojson_path,
    capa_nombre,
    color,
    tipo_salida="general",
    layer_metadata=None
):
    """
    Convierte KML de polígonos a GeoJSON usando XML directo.
    Además agrega metadata del JSON avance_resumen a cada feature,
    para que popup/visor puedan conocer mes, layer_id, manzanas y porcentaje.
    """
    layer_metadata = layer_metadata or {}

    tree = ET.parse(kml_path)
    root = tree.getroot()

    ns = {
        "kml": "http://www.opengis.net/kml/2.2"
    }

    placemarks = root.findall(".//kml:Placemark", ns)

    if not placemarks:
        placemarks = root.findall(".//Placemark")

    features = []

    for idx, pm in enumerate(placemarks, start=1):
        name = texto_hijo(pm, "name", ns) or f"{capa_nombre} - Bloque {idx}"

        desc_el = pm.find("kml:description", ns)

        if desc_el is None:
            desc_el = pm.find("description")

        descripcion = limpiar_html(desc_el.text) if desc_el is not None and desc_el.text else ""

        polygons = extraer_polygons_de_placemark(pm, ns)

        if not polygons:
            continue

        if len(polygons) == 1:
            geometry = {
                "type": "Polygon",
                "coordinates": polygons[0]
            }
        else:
            geometry = {
                "type": "MultiPolygon",
                "coordinates": polygons
            }

        features.append({
            "type": "Feature",
            "properties": {
                "name": name,
                "grupo": capa_nombre,
                "descripcion": descripcion,
                "color": color,
                "bloque": idx,
                "tipo_salida": tipo_salida,

                # Metadata nueva para vincular capa ↔ JSON ↔ KML
                "layer_id": layer_metadata.get("layer_id"),
                "kml": layer_metadata.get("kml"),
                "mes_id": layer_metadata.get("mes_id"),
                "mes_label": layer_metadata.get("mes_label"),
                "tab_label": layer_metadata.get("tab_label"),
                "grupo_logico": layer_metadata.get("grupo"),
                "tipo": layer_metadata.get("tipo"),
                "manzanas": layer_metadata.get("manzanas", 0),
                "porcentaje": layer_metadata.get("porcentaje", 0),
                "nodos": layer_metadata.get("nodos", 0)
            },
            "geometry": geometry
        })

    geojson = {
        "type": "FeatureCollection",
        "features": features
    }

    geojson_path.write_text(
        json.dumps(geojson, ensure_ascii=False),
        encoding="utf-8"
    )

    return len(features)


# ======================================================
# MAIN
# ======================================================

def main():
    CARPETA_DOCS.mkdir(exist_ok=True)
    CARPETA_DATA.mkdir(parents=True, exist_ok=True)
    CARPETA_KML_LOCAL.mkdir(exist_ok=True)
    CARPETA_STATIC_KML.mkdir(exist_ok=True)

    # Limpiar data anterior
    for p in CARPETA_DATA.glob("*"):
        if p.is_file():
            p.unlink()

    for p in CARPETA_KML_LOCAL.glob("*"):
        if p.is_file():
            p.unlink()

    print("Conectando a Google Drive...")
    service = crear_drive_service()

    print("Listando archivos en Drive...")
    archivos_drive = listar_archivos_drive(service)

    archivos_kml = filtrar_kml(archivos_drive)
    archivo_json_avance = buscar_archivo_json_avance(archivos_drive)

    print(f"KML encontrados: {len(archivos_kml)}")

    if not archivos_kml:
        raise ValueError("No se encontraron KML en la carpeta de Drive.")

    # ======================================================
    # DESCARGAR Y PUBLICAR avance_resumen.json
    # ======================================================

    avance = None
    indice_avance = {}

    if archivo_json_avance:
        print(f"Descargando JSON de avance: {NOMBRE_JSON_AVANCE}")

        path_json_local = CARPETA_KML_LOCAL / NOMBRE_JSON_AVANCE
        path_json_publico = CARPETA_DATA / NOMBRE_JSON_AVANCE

        descargar_archivo_drive(
            service=service,
            file_id=archivo_json_avance["id"],
            output_path=path_json_local
        )

        avance = leer_json(path_json_local)

        if not avance:
            raise ValueError(f"No se pudo leer correctamente {NOMBRE_JSON_AVANCE}.")

        # Publicar el JSON tal cual para que el HTML lo consuma
        escribir_json(path_json_publico, avance)

        indice_avance = construir_indice_avance(avance)

        print(f"Capas en {NOMBRE_JSON_AVANCE}: {len(indice_avance)}")
    else:
        print(f"ADVERTENCIA: No se encontró {NOMBRE_JSON_AVANCE} en Drive.")
        print("Se publicará un JSON vacío de fallback, pero la leyenda no tendrá manzanas/porcentajes.")

        avance = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "unidad": "manzanas",
            "meses_config": [],
            "capas": [],
            "resumen_por_mes": {},
            "resumen_general": {}
        }

        escribir_json(CARPETA_DATA / NOMBRE_JSON_AVANCE, avance)

    layers = []

    # ======================================================
    # CAPAS DINÁMICAS DESDE GOOGLE DRIVE
    # ======================================================

    for idx, archivo in enumerate(archivos_kml):
        nombre_kml = archivo["name"]
        file_id = archivo["id"]

        metadata = metadata_para_kml(nombre_kml, indice_avance)
        capa_base = capa_base_desde_metadata(nombre_kml, metadata, idx)

        nombre_base = nombre_seguro_archivo(nombre_kml)
        color = capa_base["color"]
        tipo = capa_base["type"]

        path_kml = CARPETA_KML_LOCAL / nombre_kml
        path_geojson = CARPETA_DATA / f"{nombre_base}.geojson"

        print("----------------------------------------")
        print(f"Descargando: {nombre_kml}")
        descargar_archivo_drive(service, file_id, path_kml)

        print(f"Convirtiendo a GeoJSON: {nombre_kml}")
        cantidad_features = convertir_kml_a_geojson(
            kml_path=path_kml,
            geojson_path=path_geojson,
            capa_nombre=nombre_kml.replace(".kml", ""),
            color=color,
            tipo_salida=tipo,
            layer_metadata=capa_base
        )

        print(f"Features generadas: {cantidad_features}")

        layers.append({
            **capa_base,
            "name": nombre_kml.replace(".kml", ""),
            "geojson_file": f"data/{path_geojson.name}",
            "features": cantidad_features,
            "drive_modified_time": archivo.get("modifiedTime"),
            "static": False
        })

    # ======================================================
    # CAPAS ESTÁTICAS DEL REPOSITORIO
    # ======================================================

    print("Procesando capas estáticas locales...")

    for static_layer in STATIC_KML_LAYERS:
        nombre_capa = static_layer["name"]
        archivo_kml = static_layer["file"]
        tipo = static_layer.get("type", "estatica")
        color = static_layer.get("color", "#6A1B9A")

        path_kml = CARPETA_STATIC_KML / archivo_kml

        if not path_kml.exists():
            print(f"No existe el KML estático: {path_kml}. Se omite.")
            continue

        nombre_base = nombre_seguro_archivo(nombre_capa)
        path_geojson = CARPETA_DATA / f"{nombre_base}.geojson"

        layer_metadata = {
            "layer_id": static_layer.get("layer_id", nombre_seguro_archivo(nombre_capa)),
            "kml": archivo_kml,
            "mes_id": None,
            "mes_label": None,
            "tab_label": None,
            "grupo": static_layer.get("grupo", "zonas_aph"),
            "tipo": tipo,
            "manzanas": static_layer.get("manzanas", 0),
            "porcentaje": static_layer.get("porcentaje", 0),
            "nodos": static_layer.get("nodos", 0)
        }

        print("----------------------------------------")
        print(f"Convirtiendo capa estática a GeoJSON: {nombre_capa}")

        cantidad_features = convertir_kml_a_geojson(
            kml_path=path_kml,
            geojson_path=path_geojson,
            capa_nombre=nombre_capa,
            color=color,
            tipo_salida=tipo,
            layer_metadata=layer_metadata
        )

        print(f"Features generadas capa estática {nombre_capa}: {cantidad_features}")

        layers.append({
            "layer_id": layer_metadata["layer_id"],
            "name": nombre_capa,
            "display_name": nombre_capa,
            "type": tipo,
            "grupo": layer_metadata["grupo"],
            "tipo": tipo,
            "geojson_file": f"data/{path_geojson.name}",
            "color": color,
            "features": cantidad_features,
            "drive_modified_time": None,
            "static": True,
            "kml": archivo_kml,
            "mes_id": None,
            "mes_label": None,
            "tab_label": None,
            "manzanas": static_layer.get("manzanas", 0),
            "porcentaje": static_layer.get("porcentaje", 0),
            "nodos": static_layer.get("nodos", cantidad_features)
        })

    # ======================================================
    # MANIFEST PARA EL VISOR
    # ======================================================

    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "drive_folder_id": DRIVE_FOLDER_ID,
        "avance_json": f"data/{NOMBRE_JSON_AVANCE}",
        "layers": layers
    }

    escribir_json(CARPETA_DATA / "layers.json", manifest)

    (CARPETA_DOCS / ".nojekyll").write_text("", encoding="utf-8")

    print("========================================")
    print("Proceso terminado.")
    print(f"Capas generadas: {len(layers)}")
    print(f"JSON publicado: data/{NOMBRE_JSON_AVANCE}")
    print("========================================")


if __name__ == "__main__":
    main()
