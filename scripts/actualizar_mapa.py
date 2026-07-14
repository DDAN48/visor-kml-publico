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

SCOPES_DRIVE = [
    "https://www.googleapis.com/auth/drive"
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


def color_para_capa(nombre, indice):
    nombre_norm = normalizar_texto(nombre)

    if "tendido" in nombre_norm:
        return "#008000"

    if "columna" in nombre_norm:
        return "#1f77b4"

    if "estado" in nombre_norm:
        return "#d62728"

    colores_semana = [
        "#e6dc29",
        "#ff7f0e",
        "#9467bd",
        "#8c564b",
        "#17becf",
    ]

    if "semana" in nombre_norm:
        return colores_semana[indice % len(colores_semana)]

    colores_genericos = [
        "#008000",
        "#1f77b4",
        "#2ca02c",
        "#d62728",
        "#ff7f0e",
        "#9467bd",
        "#8c564b",
        "#17becf",
    ]

    return colores_genericos[indice % len(colores_genericos)]


def tipo_para_capa(nombre):
    nombre_norm = normalizar_texto(nombre)

    if "semana" in nombre_norm:
        return "semana"

    if "finalizado" in nombre_norm:
        return "finalizado"

    return "general"


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


def listar_kml_drive(service):
    archivos = []
    page_token = None

    query = (
        f"'{DRIVE_FOLDER_ID}' in parents "
        f"and trashed = false "
        f"and name contains '.kml'"
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

    archivos = [
        a for a in archivos
        if a["name"].lower().endswith(".kml")
    ]

    return archivos


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
    GeoJSON: [ [lon, lat], [lon, lat] ]
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


def convertir_kml_a_geojson(kml_path, geojson_path, capa_nombre, color):
    """
    Convierte KML de polígonos a GeoJSON usando XML directo.
    """
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
                "bloque": idx
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

    # Limpiar data anterior
    for p in CARPETA_DATA.glob("*"):
        if p.is_file():
            p.unlink()

    for p in CARPETA_KML_LOCAL.glob("*"):
        if p.is_file():
            p.unlink()

    print("Conectando a Google Drive...")
    service = crear_drive_service()

    print("Listando KML en Drive...")
    archivos = listar_kml_drive(service)

    print(f"KML encontrados: {len(archivos)}")

    if not archivos:
        raise ValueError("No se encontraron KML en la carpeta de Drive.")

    layers = []

    for idx, archivo in enumerate(archivos):
        nombre_kml = archivo["name"]
        file_id = archivo["id"]

        nombre_base = nombre_seguro_archivo(nombre_kml)
        color = color_para_capa(nombre_kml, idx)
        tipo = tipo_para_capa(nombre_kml)

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
            color=color
        )

        print(f"Features generadas: {cantidad_features}")

        layers.append({
            "name": nombre_kml.replace(".kml", ""),
            "type": tipo,
            "geojson_file": f"data/{path_geojson.name}",
            "color": color,
            "features": cantidad_features,
            "drive_modified_time": archivo.get("modifiedTime")
        })

    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "drive_folder_id": DRIVE_FOLDER_ID,
        "layers": layers
    }

    (CARPETA_DATA / "layers.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    (CARPETA_DOCS / ".nojekyll").write_text("", encoding="utf-8")

    print("========================================")
    print("Proceso terminado.")
    print(f"Capas generadas: {len(layers)}")
    print("========================================")


if __name__ == "__main__":
    main()
