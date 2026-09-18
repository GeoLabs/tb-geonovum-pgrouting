# -*- coding: utf-8 -*-
"""Stage an AHN4 DTM extract for the deployed GDAL profile process."""

import hashlib
import json
import math
import os
import sys
import urllib.parse
import urllib.request

from osgeo import gdal, ogr, osr


AHN_WCS_URL = "https://service.pdok.nl/rws/actueel-hoogtebestand-nederland/wcs/v1_0"
AHN_RESOLUTION = 0.5
AHN_FILL_DISTANCE_PIXELS = 200
MAX_PIXELS = 16000000
MAX_DIMENSION = 4000
MAX_ADAPTIVE_RESOLUTION = 5.0


def _input_value(inputs, name):
    input_map = inputs[name]
    cache_file = input_map.get("cache_file")
    if cache_file:
        with open(cache_file, "r") as input_file:
            return input_file.read()
    return input_map["value"]


def _line_geometry(payload):
    value = json.loads(payload) if isinstance(payload, str) else payload
    value_type = value.get("type")
    if value_type == "FeatureCollection":
        geometries = [feature.get("geometry") for feature in value.get("features", [])]
    elif value_type == "Feature":
        geometries = [value.get("geometry")]
    else:
        geometries = [value]

    result = ogr.Geometry(ogr.wkbLineString)

    def append_line(line):
        points = [line.GetPoint(index) for index in range(line.GetPointCount())]
        if not points:
            return
        if result.GetPointCount():
            last_point = result.GetPoint(result.GetPointCount() - 1)
            distance_to_start = (last_point[0] - points[0][0]) ** 2 + (last_point[1] - points[0][1]) ** 2
            distance_to_end = (last_point[0] - points[-1][0]) ** 2 + (last_point[1] - points[-1][1]) ** 2
            if distance_to_end < distance_to_start:
                points.reverse()
            if last_point[:2] == points[0][:2]:
                points = points[1:]
        for point in points:
            result.AddPoint(point[0], point[1])

    for geometry_json in geometries:
        if not geometry_json:
            continue
        geometry = ogr.CreateGeometryFromJson(json.dumps(geometry_json))
        if geometry is None:
            continue
        geometry_name = geometry.GetGeometryName().upper()
        if geometry_name == "LINESTRING":
            append_line(geometry)
        elif geometry_name == "MULTILINESTRING":
            for index in range(geometry.GetGeometryCount()):
                append_line(geometry.GetGeometryRef(index))

    if result.GetPointCount() < 2:
        raise ValueError("Geometry must contain at least one LineString")
    return result


def _transform_to_rd(geometry):
    source = osr.SpatialReference()
    source.ImportFromEPSG(4326)
    target = osr.SpatialReference()
    target.ImportFromEPSG(28992)
    if hasattr(source, "SetAxisMappingStrategy"):
        source.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        target.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    geometry.Transform(osr.CoordinateTransformation(source, target))
    return geometry


def _coverage_size(envelope):
    min_x, max_x, min_y, max_y = envelope
    native_width = max(1, int(math.ceil((max_x - min_x) / AHN_RESOLUTION)))
    native_height = max(1, int(math.ceil((max_y - min_y) / AHN_RESOLUTION)))
    scale = min(
        1.0,
        math.sqrt(float(MAX_PIXELS) / (native_width * native_height)),
        float(MAX_DIMENSION) / native_width,
        float(MAX_DIMENSION) / native_height,
    )
    width = max(1, int(math.floor(native_width * scale)))
    height = max(1, int(math.floor(native_height * scale)))
    resolution = max((max_x - min_x) / width, (max_y - min_y) / height)
    if resolution > MAX_ADAPTIVE_RESOLUTION:
        raise ValueError(
            "AHN extent requires a resolution of {:.1f} m; maximum allowed is {:.1f} m".format(
                resolution, MAX_ADAPTIVE_RESOLUTION
            )
        )
    return width, height


def _coverage_url(envelope, width, height):
    min_x, max_x, min_y, max_y = envelope
    parameters = [
        ("service", "WCS"),
        ("version", "2.0.1"),
        ("request", "GetCoverage"),
        ("coverageId", "dtm_05m"),
        ("format", "image/tiff"),
        ("subset", "X({:.3f},{:.3f})".format(min_x, max_x)),
        ("subset", "Y({:.3f},{:.3f})".format(min_y, max_y)),
        ("scaleSize", "X({})".format(width)),
        ("scaleSize", "Y({})".format(height)),
    ]
    return AHN_WCS_URL + "?" + urllib.parse.urlencode(parameters)


def _prepare_raster(path, geometry):
    dataset = gdal.Open(path, gdal.GA_Update)
    if dataset is None or dataset.RasterCount < 1:
        raise RuntimeError("Downloaded AHN coverage is not a readable raster")

    band = dataset.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        gdal.FillNodata(band, None, AHN_FILL_DISTANCE_PIXELS, 0)
        band.FlushCache()

        sample_geometry = geometry.Clone()
        transform = dataset.GetGeoTransform()
        sample_geometry.Segmentize(abs(transform[1]))
        for index in range(sample_geometry.GetPointCount()):
            x_coordinate, y_coordinate, _ = sample_geometry.GetPoint(index)
            pixel_x = int(math.floor((x_coordinate - transform[0]) / transform[1]))
            pixel_y = int(math.floor((y_coordinate - transform[3]) / transform[5]))
            value = band.ReadAsArray(pixel_x, pixel_y, 1, 1)[0][0]
            if abs(float(value) - nodata) <= abs(nodata) * 1e-6:
                raise RuntimeError("AHN DTM still contains NoData values along the route")
    dataset = None


def _stage_raster(data_path, geometry, padding):
    min_x, max_x, min_y, max_y = geometry.GetEnvelope()
    envelope = (min_x - padding, max_x + padding, min_y - padding, max_y + padding)
    width, height = _coverage_size(envelope)

    digest = hashlib.sha256(
        (
            b"scaled-filled-v2:"
            + geometry.ExportToWkb()
            + str(round(padding, 3)).encode("ascii")
            + "{}x{}".format(width, height).encode("ascii")
        )
    ).hexdigest()[:20]
    relative_path = os.path.join("ahn-cache", "dtm_{}.tif".format(digest))
    absolute_path = os.path.join(data_path, relative_path)
    if os.path.isfile(absolute_path):
        return relative_path

    cache_directory = os.path.dirname(absolute_path)
    if not os.path.isdir(cache_directory):
        os.makedirs(cache_directory)
    temporary_path = absolute_path + ".part"

    request = urllib.request.Request(
        _coverage_url(envelope, width, height),
        headers={"User-Agent": "GeoNovum-ZOO-Testbed/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            if "tiff" not in response.headers.get("Content-Type", "").lower():
                raise RuntimeError("AHN WCS did not return a GeoTIFF")
            with open(temporary_path, "wb") as output_file:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output_file.write(chunk)

        _prepare_raster(temporary_path, geometry)
        os.rename(temporary_path, absolute_path)
        return relative_path
    except Exception:
        try:
            os.remove(temporary_path)
        except OSError:
            pass
        raise


def StageAHN(conf, inputs, outputs):
    import zoo

    try:
        geometry = _transform_to_rd(_line_geometry(_input_value(inputs, "Geometry")))
        padding = float(inputs.get("PaddingMeters", {}).get("value", 10))
        if not math.isfinite(padding) or not 0 <= padding <= 1000:
            raise ValueError("PaddingMeters must be between 0 and 1000")

        relative_path = _stage_raster(conf["main"]["dataPath"], geometry, padding)
        outputs["RasterFile"]["value"] = relative_path
        outputs["Geometry28992"]["value"] = geometry.ExportToJson()
        outputs["Geometry28992"]["mimeType"] = "application/geo+json"
        outputs["Geometry28992"]["encoding"] = "UTF-8"
        return zoo.SERVICE_SUCCEEDED
    except Exception as exc:
        conf["lenv"]["message"] = str(exc)
        print("StageAHN failed: {}".format(exc), file=sys.stderr)
        return zoo.SERVICE_FAILED