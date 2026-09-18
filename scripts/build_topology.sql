\set ON_ERROR_STOP on
\timing on

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgrouting;

ALTER TABLE public.roads
    ADD COLUMN IF NOT EXISTS source BIGINT,
    ADD COLUMN IF NOT EXISTS target BIGINT,
    ADD COLUMN IF NOT EXISTS cost DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS reverse_cost DOUBLE PRECISION;

WITH edge_values AS (
    SELECT
        id,
        (properties_json->>'jte_id_beg')::BIGINT AS source,
        (properties_json->>'jte_id_end')::BIGINT AS target,
        ST_Length(geom) AS length_m,
        properties_json->>'rijrichtng' AS direction
    FROM public.roads
)
UPDATE public.roads AS roads
SET
    source = edge_values.source,
    target = edge_values.target,
    cost = CASE
        WHEN edge_values.direction = 'T' THEN -1
        ELSE edge_values.length_m
    END,
    reverse_cost = CASE
        WHEN edge_values.direction = 'H' THEN -1
        ELSE edge_values.length_m
    END
FROM edge_values
WHERE roads.id = edge_values.id
  AND (
      roads.source IS DISTINCT FROM edge_values.source
      OR roads.target IS DISTINCT FROM edge_values.target
      OR roads.cost IS DISTINCT FROM CASE
          WHEN edge_values.direction = 'T' THEN -1
          ELSE edge_values.length_m
      END
      OR roads.reverse_cost IS DISTINCT FROM CASE
          WHEN edge_values.direction = 'H' THEN -1
          ELSE edge_values.length_m
      END
  );

CREATE INDEX IF NOT EXISTS idx_roads_source ON public.roads (source);
CREATE INDEX IF NOT EXISTS idx_roads_target ON public.roads (target);

DROP TABLE IF EXISTS public.road_vertices;

CREATE TABLE public.road_vertices AS
SELECT DISTINCT ON (node_id)
    node_id,
    geom
FROM (
    SELECT
        source AS node_id,
        ST_StartPoint(ST_GeometryN(geom, 1))::geometry(Point, 28992) AS geom
    FROM public.roads
    UNION ALL
    SELECT
        target AS node_id,
        ST_EndPoint(ST_GeometryN(geom, 1))::geometry(Point, 28992) AS geom
    FROM public.roads
) AS endpoints
ORDER BY node_id;

ALTER TABLE public.road_vertices
    ADD PRIMARY KEY (node_id);

CREATE INDEX idx_road_vertices_geom
    ON public.road_vertices
    USING GIST (geom);

ANALYZE public.roads;
ANALYZE public.road_vertices;