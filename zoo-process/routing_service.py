# -*- coding: utf-8 -*-
"""ZOO-Project process provider for shortest-path routing on the PDOK NWB."""

import math
import os
import sys

from osgeo import gdal, ogr


def _parse_point(value, label):
    parts = str(value).split(",")
    if len(parts) != 2:
        raise ValueError("{} must use the lon,lat format".format(label))

    longitude = float(parts[0].strip())
    latitude = float(parts[1].strip())
    if not math.isfinite(longitude) or not math.isfinite(latitude):
        raise ValueError("{} contains a non-finite coordinate".format(label))
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        raise ValueError("{} must be expressed in EPSG:4326".format(label))
    return longitude, latitude


def _dsn_value(value):
    return "'{}'".format(str(value).replace("\\", "\\\\").replace("'", "\\'"))


def _setting(conf, name, default=""):
    value = os.environ.get(name)
    if value:
        return value

    secret_path = os.path.join("/etc/zoo-routing", name)
    try:
        with open(secret_path, "r") as secret_file:
            value = secret_file.read().strip()
    except IOError:
        value = None

    return value or conf.get("renv", {}).get(name, default)


def _connection_string(conf):
    settings = {
        "host": _setting(conf, "ROUTING_PGHOST", "pgrouting-db.routing.svc.cluster.local"),
        "port": _setting(conf, "ROUTING_PGPORT", "5432"),
        "dbname": _setting(conf, "ROUTING_PGDATABASE", "routing"),
        "user": _setting(conf, "ROUTING_PGUSER", "routing"),
        "password": _setting(conf, "ROUTING_PGPASSWORD"),
    }
    return "PG:" + " ".join(
        "{}={}".format(key, _dsn_value(value))
        for key, value in settings.items()
        if value
    )


def _route_sql(start, end, corridor_meters):
    return """
WITH input_points AS (
    SELECT
        ST_Transform(ST_SetSRID(ST_Point({start_lon}, {start_lat}), 4326), 28992) AS start_geom,
        ST_Transform(ST_SetSRID(ST_Point({end_lon}, {end_lat}), 4326), 28992) AS end_geom
), nearest AS (
    SELECT
        (SELECT node_id FROM public.road_vertices, input_points
         ORDER BY geom <-> start_geom LIMIT 1) AS start_node,
        (SELECT node_id FROM public.road_vertices, input_points
         ORDER BY geom <-> end_geom LIMIT 1) AS end_node,
        start_geom,
        end_geom
    FROM input_points
), route_bounds AS (
    SELECT
        start_node,
        end_node,
        ST_Expand(ST_Envelope(ST_Collect(start_geom, end_geom)), {corridor}) AS geom
    FROM nearest
), route AS (
    SELECT result.*
    FROM route_bounds
    CROSS JOIN LATERAL pgr_dijkstra(
        format(
            'SELECT id, source, target, cost, reverse_cost FROM public.roads '
            'WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 28992)',
            ST_XMin(geom), ST_YMin(geom), ST_XMax(geom), ST_YMax(geom)
        ),
        start_node,
        end_node,
        directed := true
    ) AS result
), segments AS (
    SELECT
        route.seq,
        route.path_seq,
        route.node,
        route.edge,
        route.cost,
        route.agg_cost,
        roads.road_name,
        roads.road_class,
        CASE
            WHEN route.node = roads.source THEN roads.geom
            ELSE ST_Reverse(roads.geom)
        END AS geom
    FROM route
    JOIN public.roads ON roads.id = route.edge
    WHERE route.edge >= 0
)
SELECT jsonb_build_object(
    'type', 'FeatureCollection',
    'numberReturned', (SELECT count(*) FROM segments),
    'distanceMeters', COALESCE((SELECT max(agg_cost + cost) FROM segments), 0),
    'startNode', (SELECT start_node FROM nearest),
    'endNode', (SELECT end_node FROM nearest),
    'features', COALESCE(
        (
            SELECT jsonb_agg(
                jsonb_build_object(
                    'type', 'Feature',
                    'id', edge,
                    'geometry', ST_AsGeoJSON(ST_Transform(geom, 4326), 7)::jsonb,
                    'properties', jsonb_build_object(
                        'sequence', path_seq,
                        'edgeId', edge,
                        'name', road_name,
                        'roadClass', road_class,
                        'lengthMeters', cost,
                        'cumulativeMeters', agg_cost
                    )
                ) ORDER BY seq
            )
            FROM segments
        ),
        '[]'::jsonb
    )
)::text AS result
FROM nearest;
""".format(
        start_lon=start[0],
        start_lat=start[1],
        end_lon=end[0],
        end_lat=end[1],
        corridor=corridor_meters,
    )


def routing(conf, inputs, outputs):
    import zoo

    dataset = None
    result_set = None
    try:
        start = _parse_point(inputs["startPoint"]["value"], "startPoint")
        end = _parse_point(inputs["endPoint"]["value"], "endPoint")
        corridor = float(inputs.get("corridorMeters", {}).get("value", 5000))
        if not math.isfinite(corridor) or not 100 <= corridor <= 100000:
            raise ValueError("corridorMeters must be between 100 and 100000")

        dataset = ogr.Open(_connection_string(conf))
        if dataset is None:
            raise RuntimeError(
                "Unable to connect to the routing database: {}".format(
                    gdal.GetLastErrorMsg()
                )
            )

        result_set = dataset.ExecuteSQL(_route_sql(start, end, corridor))
        if result_set is None:
            raise RuntimeError("The routing SQL query did not return a result")

        feature = result_set.GetNextFeature()
        if feature is None:
            raise RuntimeError("No route result was returned")

        result = feature.GetField("result")
        outputs["Result"]["value"] = result
        outputs["Result"]["mimeType"] = "application/geo+json"
        outputs["Result"]["encoding"] = "UTF-8"
        return zoo.SERVICE_SUCCEEDED
    except Exception as exc:
        conf["lenv"]["message"] = str(exc)
        print("routing process failed: {}".format(exc), file=sys.stderr)
        return zoo.SERVICE_FAILED
    finally:
        if dataset is not None and result_set is not None:
            dataset.ReleaseResultSet(result_set)
        result_set = None
        dataset = None