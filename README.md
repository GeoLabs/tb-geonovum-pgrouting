# PGRouting + PDOK road data deployment

This directory keeps the deployment and integration assets for a dedicated PostgreSQL/PostGIS/PGRouting instance used for road-network analysis and shortest-path computations, independent from the ZOO-Project runtime.

## Goals

- isolate road-network processing from the ZOO-Project database
- keep a stable PostgreSQL service for routing workloads even if the ZOO stack restarts
- integrate road data from the Dutch national road network exposed through the PDOK OGC API - Features service
- expose the resulting data as OGC API - Features compatible tables and layers

## Architecture

- PostgreSQL + PostGIS + PGRouting in a dedicated namespace
- a dedicated PVC so the routing database survives pod recreation
- internal Kubernetes service reachable from the application layer
- import pipeline that reads OGC API - Features documents from PDOK and stores them in the routing database
- optional PostGIS views or materialized views for OGC Feature exposure

## Recommended Docker image

The most direct implementation is the existing public image:

- `pgrouting/pgrouting:16-3.5-4.0.1`

This image already provides PostgreSQL with PostGIS and PGRouting available. For a clean deployment, the database bootstrap script can still enable required extensions explicitly:

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgrouting;
```

## Directory layout

- `k8s/pgrouting-db.yaml` — Kubernetes namespace, secret, PVC, StatefulSet and service for the dedicated database
- `scripts/import_pdok_roads.py` — Python script that consumes OGC API - Features output from the PDOK road dataset and imports it into PostgreSQL
- `docs/` — additional operational notes if needed

## Deployment

### 1. Apply the database manifest

```bash
kubectl apply -f k8s/pgrouting-db.yaml
```

This creates:

- namespace `routing`
- secret `pgrouting-db-secret`
- PVC `pgrouting-data`
- StatefulSet `pgrouting-postgres`
- Service `pgrouting-db`

### 2. Validate readiness

```bash
kubectl -n routing get pods
kubectl -n routing get svc
kubectl -n routing exec -it statefulset/pgrouting-postgres -- psql -U routing -d routing -c "SELECT postgis_version();"
kubectl -n routing exec -it statefulset/pgrouting-postgres -- psql -U routing -d routing -c "SELECT extname FROM pg_extension WHERE extname IN ('postgis', 'pgrouting');"
```

## PDOK data source

The source used for road features is the PDOK OGC API - Features endpoint:

```text
https://api.pdok.nl/rws/nationaal-wegenbestand-wegen/ogc/v1
```

This service exposes vector features, typically as GeoJSON, and can be consumed by a Python importer script or by GDAL/ogr2ogr.

For integration, the recommended pattern is:

1. read the OGC API Feature collection metadata
2. discover the relevant collection(s)
3. fetch feature pages in GeoJSON
4. store the result into PostgreSQL tables with `geometry` columns
5. build routing topology from the stored network using PGRouting

## Import workflow

The script at `scripts/import_pdok_roads.py` performs the following:

1. connects to PostgreSQL
2. creates the target schema and table if needed
3. requests the PDOK OGC API - Features endpoint
4. iterates through returned features and writes them to Postgres
5. creates a geometry column when required
6. optionally creates a simplified routing table with `source`, `target`, `cost`, and `reverse_cost`

The default run imports the complete `wegvakken` collection in EPSG:28992 by
following the cursor-based links returned by PDOK. Pages contain 1,000 features
by default and are committed independently. The next PDOK cursor is stored in
`public.pdok_import_state`, so an interrupted complete import resumes from its
last committed page. Set `PAGE_SIZE` to tune the request size or `MAX_FEATURES`
to run a limited test. Re-running the importer updates existing rows by PDOK
feature identifier instead of duplicating them.

Example for a controlled 100-feature test:

```bash
PAGE_SIZE=100 MAX_FEATURES=100 python scripts/import_pdok_roads.py
```

Example for the complete network:

```bash
PAGE_SIZE=1000 MAX_FEATURES=0 python scripts/import_pdok_roads.py
```

## OGC API - Features integration pattern

To expose the imported data through OGC API - Features, the usual pattern is:

- keep the spatial table in PostgreSQL
- register a view or table with a geometry column
- expose it through a small API layer or through a GeoServer/OGC adapter layer
- ensure the schema includes the mandatory identifiers and feature properties expected by clients

Example for a table named `roads`:

```sql
CREATE TABLE IF NOT EXISTS public.roads (
    id SERIAL PRIMARY KEY,
    road_id TEXT,
    road_name TEXT,
    road_class TEXT,
    geom GEOMETRY(MultiLineString, 28992)
);
```

Then a view can be created if needed:

```sql
CREATE OR REPLACE VIEW public.roads_ogc AS
SELECT id, road_id, road_name, road_class, geom
FROM public.roads;
```

This view can be consumed by an OGC API - Features layer or a conversion service.

## PGRouting topology example

Once the road geometry is imported, build the graph from the authoritative NWB
begin/end junction identifiers:

```bash
kubectl -n routing exec -i statefulset/pgrouting-postgres -- \
    psql -U routing -d routing < scripts/build_topology.sql
```

The script adds the PGRouting edge columns, calculates metric costs, creates
indexes and materializes `public.road_vertices` for nearest-node searches. NWB
direction codes are mapped as follows:

- `B`: both directions
- `H`: begin junction to end junction only
- `T`: end junction to begin junction only
- `O`: unknown, treated as both directions

For route queries, first resolve the nearest graph vertices and restrict the
edge SQL to a suitable spatial corridor. This avoids loading the complete
national graph into memory for every request:

```sql
WITH points AS (
    SELECT
        (SELECT node_id FROM public.road_vertices
         ORDER BY geom <-> ST_SetSRID(ST_Point(121700, 487400), 28992)
         LIMIT 1) AS start_node,
        (SELECT node_id FROM public.road_vertices
         ORDER BY geom <-> ST_SetSRID(ST_Point(122000, 486700), 28992)
         LIMIT 1) AS end_node
)
SELECT route.*
FROM points
CROSS JOIN LATERAL pgr_dijkstra(
    'SELECT id, source, target, cost, reverse_cost
     FROM public.roads
     WHERE geom && ST_MakeEnvelope(117000, 482000, 127000, 492000, 28992)',
    start_node,
    end_node,
    directed := true
) AS route;
```

## Operational notes

- Keep this database in its own namespace to avoid coupling with ZOO lifecycle events.
- Use a dedicated PVC and regular backups.
- If the dataset is large, run the importer with incremental batches instead of loading all features in one transaction.
- Add indexes to geometry and identifier columns after import.

## Minimal imports for the importer

The Python importer script depends on:

```bash
pip install requests psycopg2-binary shapely
```

## Security

- do not expose the routing database publicly
- use a Kubernetes Secret for passwords
- limit access to the service to the application namespace that needs routing results

## Example runtime access

From an application container, the service is reachable as:

```text
pgrouting-db.routing.svc.cluster.local:5432
```

## ZOO-Project routing process

The `routing` process follows the former MapMint WPS routing design: it accepts
`startPoint` and `endPoint`, finds their nearest NWB graph vertices, executes
`pgr_dijkstra`, and returns the ordered route segments as GeoJSON. Coordinates
use the `longitude,latitude` format in EPSG:4326.

Deploy the process into `/usr/lib/cgi-bin` in both ZOO runtimes:

```bash
./scripts/deploy_zoo_process.sh
```

The script mounts the process from a ConfigMap, copies the routing database
credentials into a namespace-local Secret, and restarts `zookernel` and
`zoofpm`. Run it again after a Helm upgrade because the current chart does not
declare these additional mounts.

### OGC API - Processes demonstration

Describe the process:

```bash
curl https://host1.tb.geonovum.geolabs.fr/ogc-api/processes/routing
```

Execute it synchronously with the provided request document:

```bash
curl -X POST \
    https://host1.tb.geonovum.geolabs.fr/ogc-api/processes/routing/execution \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/geo+json' \
    -H 'Prefer: respond-sync' \
    --data @examples/routing-request.json
```

The validated example returns 20 ordered road segments over approximately
1,147.54 metres.

### AHN elevation profile chain

The routing UI automatically adds a terrain profile by chaining three
ZOO-Project processes:

1. `routing` returns the route as GeoJSON in EPSG:4326.
2. `StageAHN` merges the route segments, transforms the line to EPSG:28992,
   downloads a bounded `dtm_05m` GeoTIFF from the PDOK AHN4 WCS, and stores it
   below `/usr/com/zoo-project/ahn-cache`.
3. `GdalExtractProfile` samples the staged raster along the transformed line.

`StageAHN` is deliberately restricted to the fixed PDOK AHN endpoint. It caps
requests at 16 million pixels, uses deterministic cache names, and fills AHN
NoData gaps before exposing the raster. The latter is required because the
legacy C implementation of `GdalExtractProfile` cannot safely serialize the
large float value used by AHN as NoData.

The tested 1,147.54 metre route produced 2,340 profile samples with elevations
between 0.396 and 2.852 metres NAP.

### WPS 1.0 compatibility demonstration

```bash
curl --get \
    'https://host1.tb.geonovum.geolabs.fr/cgi-bin/zoo_loader.cgi' \
    --data-urlencode 'service=WPS' \
    --data-urlencode 'version=1.0.0' \
    --data-urlencode 'request=Execute' \
    --data-urlencode 'Identifier=routing' \
    --data-urlencode 'DataInputs=startPoint=4.8952,52.3702;endPoint=4.9001,52.3640;corridorMeters=5000' \
    --data 'RawDataOutput=Result'
```

## Interactive routing map

Open the deployed interface at:

```text
https://host1.tb.geonovum.geolabs.fr/routing-ui/
```

The map deliberately separates display and calculation responsibilities:

- OpenStreetMap provides the background map.
- The visible green road network is fetched directly from the PDOK OGC API -
    Features `wegvakken` collection using the current map bounding box.
- The red route is returned by the local ZOO-Project `routing` process, backed
    by the imported PostgreSQL/PGRouting graph.
- The elevation chart is sampled from a route-specific PDOK AHN4 DTM extract
    through `StageAHN` and the existing `GdalExtractProfile` process.

Move the pointer over the elevation chart, or touch it on mobile, to display
the nearest elevation and travelled distance. A synchronized green point marks
the corresponding position on the route and disappears when the pointer leaves
the chart.

At zoom level 16 or higher, the UI requests up to 3,000 road features for the
visible extent. Click the map to place start marker A and finish marker B, then
select **Calculate route**. Both markers can be dragged before recalculating.

## Next steps

- automate the PDOK import as a scheduled Kubernetes CronJob

This design keeps the ZOO environment stable while providing a dedicated, durable platform for road-network analysis and route computation.
