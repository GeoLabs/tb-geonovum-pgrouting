#!/usr/bin/env python3
"""Import PDOK road feature data into PostgreSQL for routing analysis.

This script is intentionally lightweight and designed for an isolated routing
PostgreSQL instance. It reads OGC API - Features output from the PDOK road data
service and stores it in a Postgres table with a geometry column.
"""

import json
import os
from typing import Optional

import psycopg2
import requests
from psycopg2.extras import execute_values
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

PDOK_BASE = "https://api.pdok.nl/rws/nationaal-wegenbestand-wegen/ogc/v1"
DB_HOST = os.getenv("PGHOST", "pgrouting-db.routing.svc.cluster.local")
DB_PORT = os.getenv("PGPORT", "5432")
DB_NAME = os.getenv("PGDATABASE", "routing")
DB_USER = os.getenv("PGUSER", "routing")
DB_PASSWORD = os.getenv("PGPASSWORD", "change-me")
TARGET_TABLE = os.getenv("TARGET_TABLE", "public.roads")
TARGET_SCHEMA = os.getenv("TARGET_SCHEMA", "public")
TARGET_GEOMETRY_COLUMN = os.getenv("GEOM_COLUMN", "geom")
TARGET_SRID = int(os.getenv("TARGET_SRID", "28992"))
PDOK_COLLECTION = os.getenv("PDOK_COLLECTION", "wegvakken")
PAGE_SIZE = int(os.getenv("PAGE_SIZE", os.getenv("IMPORT_LIMIT", "1000")))
MAX_FEATURES = int(os.getenv("MAX_FEATURES", "0"))
PDOK_CRS = f"http://www.opengis.net/def/crs/EPSG/0/{TARGET_SRID}"


def create_http_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


def fetch_json(session: requests.Session, url: str, params: Optional[dict] = None):
    response = session.get(url, params=params, timeout=(15, 120))
    response.raise_for_status()
    return response.json()


def ensure_table(conn, table_name: str, geom_column: str):
    schema_name, _, table_short = table_name.partition(".")
    if not schema_name:
        schema_name = "public"
        table_name = f"{schema_name}.{table_short}"

    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
        cur.execute("CREATE EXTENSION IF NOT EXISTS pgrouting;")
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema_name};")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                id SERIAL PRIMARY KEY,
                source_id TEXT,
                feature_id TEXT,
                road_name TEXT,
                road_class TEXT,
                properties_json JSONB,
                {geom_column} GEOMETRY(MultiLineString, {TARGET_SRID})
            );
            """
        )
        cur.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table_short}_feature_id ON {table_name} (feature_id);"
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table_short}_{geom_column} ON {table_name} USING GIST ({geom_column});"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.pdok_import_state (
                collection_name TEXT PRIMARY KEY,
                next_url TEXT NOT NULL,
                processed_features BIGINT NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
    conn.commit()


def load_feature_collection(conn, feature_collection: dict) -> int:
    if "features" not in feature_collection:
        return 0

    table_name = TARGET_TABLE
    schema, _, table = table_name.partition(".")
    if not schema:
        table = table_name
        table_name = f"public.{table}"

    rows = []
    for feature in feature_collection.get("features", []):
        props = feature.get("properties", {}) or {}
        geometry = feature.get("geometry")
        if not geometry:
            continue

        feature_id = feature.get("id")
        source_id = props.get("wvk_id") or feature_id
        road_name = props.get("stt_naam") or props.get("wegnummer")
        road_class = props.get("wegtype") or props.get("frc")
        rows.append(
            (
                str(source_id),
                str(feature_id),
                road_name,
                road_class,
                json.dumps(props),
                json.dumps(geometry),
            )
        )

    if not rows:
        return 0

    with conn.cursor() as cur:
        execute_values(
            cur,
            f"""
            INSERT INTO {table_name} (source_id, feature_id, road_name, road_class, properties_json, {TARGET_GEOMETRY_COLUMN})
            VALUES %s
            ON CONFLICT (feature_id) DO UPDATE SET
                source_id = EXCLUDED.source_id,
                road_name = EXCLUDED.road_name,
                road_class = EXCLUDED.road_class,
                properties_json = EXCLUDED.properties_json,
                {TARGET_GEOMETRY_COLUMN} = EXCLUDED.{TARGET_GEOMETRY_COLUMN};
            """,
            rows,
            template=f"(%s, %s, %s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), {TARGET_SRID}))",
            page_size=PAGE_SIZE,
        )

    conn.commit()
    return len(rows)


def next_page_url(feature_collection: dict) -> Optional[str]:
    return next(
        (
            link.get("href")
            for link in feature_collection.get("links", [])
            if link.get("rel") == "next" and link.get("href")
        ),
        None,
    )


def load_checkpoint(conn) -> tuple[Optional[str], int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT next_url, processed_features FROM public.pdok_import_state WHERE collection_name = %s;",
            (PDOK_COLLECTION,),
        )
        row = cur.fetchone()
    return (row[0], row[1]) if row else (None, 0)


def save_checkpoint(conn, next_url: Optional[str], processed_features: int):
    with conn.cursor() as cur:
        if next_url:
            cur.execute(
                """
                INSERT INTO public.pdok_import_state (collection_name, next_url, processed_features)
                VALUES (%s, %s, %s)
                ON CONFLICT (collection_name) DO UPDATE SET
                    next_url = EXCLUDED.next_url,
                    processed_features = EXCLUDED.processed_features,
                    updated_at = now();
                """,
                (PDOK_COLLECTION, next_url, processed_features),
            )
        else:
            cur.execute(
                "DELETE FROM public.pdok_import_state WHERE collection_name = %s;",
                (PDOK_COLLECTION,),
            )
    conn.commit()


def main():
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )
    ensure_table(conn, TARGET_TABLE, TARGET_GEOMETRY_COLUMN)

    first_page_url = f"{PDOK_BASE}/collections/{PDOK_COLLECTION}/items"
    checkpoint_url, checkpoint_count = load_checkpoint(conn)
    items_url = checkpoint_url or first_page_url
    session = create_http_session()
    page_number = 0
    total_imported = checkpoint_count
    params = None if checkpoint_url else {"f": "json", "limit": PAGE_SIZE, "crs": PDOK_CRS}
    if checkpoint_url:
        print(f"Resuming after {checkpoint_count} previously processed features", flush=True)
    try:
        while items_url:
            payload = fetch_json(session, items_url, params=params)
            params = None

            if MAX_FEATURES:
                remaining = MAX_FEATURES - total_imported
                payload["features"] = payload.get("features", [])[:remaining]

            imported = load_feature_collection(conn, payload)
            page_number += 1
            total_imported += imported
            print(
                f"Page {page_number}: {imported} features, {total_imported} total",
                flush=True,
            )

            following_url = next_page_url(payload)
            if MAX_FEATURES and total_imported >= MAX_FEATURES:
                break
            save_checkpoint(conn, following_url, total_imported)
            items_url = following_url

        print(
            f"Imported {total_imported} features from collection {PDOK_COLLECTION}",
            flush=True,
        )
    finally:
        session.close()
        conn.close()


if __name__ == "__main__":
    main()
