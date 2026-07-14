# Visor KML público desde Google Drive

Este repositorio publica un mapa web estático con GitHub Pages. El workflow descarga los KML desde una carpeta de Google Drive, los convierte a GeoJSON y publica el visor.

## Secrets requeridos

En `Settings → Secrets and variables → Actions`, crear:

- `DRIVE_FOLDER_ID`
- `DRIVE_CLIENT_ID`
- `DRIVE_CLIENT_SECRET`
- `DRIVE_REFRESH_TOKEN`

## Activar GitHub Pages

En `Settings → Pages`, seleccionar:

- Source: `GitHub Actions`

## Ejecutar manualmente

Ir a `Actions → Actualizar mapa desde Drive → Run workflow`.

El workflow está programado para correr todos los días a las 14:20 UTC, equivalente a 11:20 Argentina.
