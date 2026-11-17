
#!/usr/bin/env python3
# Z.E.U.S. V1.5 - LI WMS temporal diagnostics
# Python 3.12 - standard library only
# No fabricated observations or HD imagery

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.parsers import expat
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
WMS_URL = os.getenv(
    "ZEUS_WMS_URL",
    "https://view.eumetsat.int/geoserver/wms"
)
LI_LAYER = "mtg_fd:li_afa"
RGB_LAYER = "mtg_fd:rgb_geocolour"
IR_LAYER = "mtg_fd:ir105_hrfi"
TIMEOUT = 30
MAX_XML = 16 * 1024 * 1024
MAX_IMAGE = 1024 * 1024
USER_AGENT = "ZEUS-NowcastingItalia/1.5"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("ZEUS")


def now_utc():
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def publish(name, value):
    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / name
    content = json.dumps(
        value, ensure_ascii=False,
        indent=2, allow_nan=False
    ) + "\n"
    temp = path.with_suffix(".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)
    log.info("Published %s", name)


def local(tag):
    return tag.rsplit("}", 1)[-1]


def child_text(node, tag):
    for child in node:
        if local(child.tag) == tag:
            return (child.text or "").strip()
    return ""


def parse_xml(body):
    builder = ET.TreeBuilder()
    parser = expat.ParserCreate(
        namespace_separator="}"
    )
    parser.StartElementHandler = builder.start
    parser.EndElementHandler = builder.end
    parser.CharacterDataHandler = builder.data

    def reject(*args):
        raise ValueError(
            "XML entity declaration/reference rejected"
        )

    parser.EntityDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    parser.SetParamEntityParsing(
        expat.XML_PARAM_ENTITY_PARSING_NEVER
    )
    parser.Parse(body, True)
    return builder.close()


def http_time(value):
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            return None
        return dt.astimezone(
            timezone.utc
        ).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError):
        return None


def request_bytes(params, limit):
    url = WMS_URL + (
        "&" if "?" in WMS_URL else "?"
    ) + urllib.parse.urlencode(params)

    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(
        req, timeout=TIMEOUT
    ) as response:
        headers = response.headers
        meta = {
            "http_status": response.status,
            "content_type": headers.get(
                "Content-Type", ""
            ),
            "http_date": http_time(
                headers.get("Date")
            ),
            "last_modified": http_time(
                headers.get("Last-Modified")
            ),
            "cache_control": headers.get(
                "Cache-Control"
            ),
            "etag": headers.get("ETag")
        }
        body = response.read(limit + 1)

    if len(body) > limit:
        raise ValueError(
            "Response size limit exceeded"
        )

    meta["response_bytes"] = len(body)
    return body, meta


def capabilities():
    body, meta = request_bytes({
        "SERVICE": "WMS",
        "REQUEST": "GetCapabilities",
        "VERSION": "1.1.1"
    }, MAX_XML)

    root = parse_xml(body)
    root_name = local(root.tag)

    if root_name not in (
        "WMT_MS_Capabilities",
        "WMS_Capabilities"
    ):
        raise ValueError(
            "Invalid capabilities root: " + root_name
        )

    meta["xml_root"] = root_name
    return root, meta


def layers_from(root):
    layers = []
    for node in root.iter():
        if local(node.tag) != "Layer":
            continue

        name = child_text(node, "Name")
        if name:
            layers.append({
                "name": name,
                "title": child_text(
                    node, "Title"
                ) or name
            })
    return layers


def candidates_from(layers):
    result = {
        "rgb": [],
        "dust": [],
        "ir": []
    }

    for layer in layers:
        label = (
            layer["name"] + " " +
            layer["title"]
        ).lower()

        if re.search(
            r"geo[\s_-]?colou?r|true[\s_-]?colou?r",
            label
        ):
            result["rgb"].append(layer)

        if re.search(r"\bdust\b", label):
            result["dust"].append(layer)

        if re.search(
            r"\binfrared\b|\bthermal\b|\bir[\s_-]?10",
            label
        ):
            result["ir"].append(layer)

    return {
        key: value[:10]
        for key, value in result.items()
    }


def layer_path_from(root, wanted):
    # Preserve parent Layer nodes.
    # WMS time dimensions may be inherited.
    def visit(node, ancestors):
        chain = (
            ancestors + [node]
            if local(node.tag) == "Layer"
            else ancestors
        )

        if (
            local(node.tag) == "Layer"
            and child_text(node, "Name") == wanted
        ):
            return chain

        for child in node:
            if (
                local(child.tag) == "Layer"
                or not chain
            ):
                found = visit(child, chain)
                if found is not None:
                    return found
        return None

    return visit(root, [])


def time_dimension(path):
    # Prefer the nearest time declaration.
    # Extent can carry actual time values.
    inherited = None

    for node in path or []:
        declared = None

        for item in node:
            if local(item.tag) not in (
                "Dimension", "Extent"
            ):
                continue

            if item.attrib.get(
                "name", ""
            ).lower() != "time":
                continue

            candidate = {
                "tag": local(item.tag),
                "value": (
                    item.text or ""
                ).strip(),
                "default": item.attrib.get(
                    "default"
                )
            }

            if (
                declared is None
                or candidate["value"]
            ):
                declared = candidate

        if declared is not None:
            inherited = declared

    return inherited


def parse_time(value):
    try:
        dt = datetime.fromisoformat(
            value.strip().replace(
                "Z", "+00:00"
            )
        )
        if dt.tzinfo is None:
            return None
        return dt.astimezone(timezone.utc)
    except (
        AttributeError,
        ValueError,
        TypeError,
        OverflowError
    ):
        return None


def latest_advertised_time(dimension):
    if not dimension:
        return None

    times = []
    for entry in dimension["value"].split(","):
        parts = entry.strip().split("/")

        # Interval end is a bound,
        # not a verified satellite acquisition.
        candidate = (
            parts[0] if len(parts) == 1
            else parts[1] if len(parts) == 3
            else ""
        )

        dt = parse_time(candidate)
        if dt is not None:
            times.append(dt)

    return max(times) if times else None


def satellite_layer_info(root, layer_name, now):
    path = layer_path_from(root, layer_name)
    dimension = time_dimension(path)
    latest = latest_advertised_time(dimension)
    result = {
        "layer": layer_name,
        "layer_found": path is not None,
        "time_dimension_present": bool(
            dimension and dimension["value"]
        ),
        "time_dimension": dimension,
        "latest_advertised_time": None,
        "age_minutes": None,
        "freshness_verified": False,
        "observation_time_verified": False,
        "getmap_at_advertised_time": {
            "attempted": False,
            "image_signature_valid": False
        }
    }
    if latest is None or path is None:
        return result
    iso = latest.isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    result["latest_advertised_time"] = iso
    result["age_minutes"] = round(
        (now - latest).total_seconds() / 60, 1
    )
    # This timestamp is an advertised WMS bound,
    # not independent proof of satellite acquisition.
    result["getmap_at_advertised_time"] = (
        test_layer_getmap(layer_name, iso)
    )
    return result


def test_layer_getmap(layer_name, time_value=None):
    result = {
        "attempted": True,
        "http_status": None,
        "image_signature_valid": False,
        "observation_time_verified": False,
        "pixel_content_verified": False,
        "error_type": None,
        "error_message": None
    }
    params = {
        "SERVICE": "WMS",
        "REQUEST": "GetMap",
        "VERSION": "1.1.1",
        "LAYERS": layer_name,
        "STYLES": "",
        "SRS": "EPSG:4326",
        "BBOX": "30,-15,55,40",
        "WIDTH": "256",
        "HEIGHT": "256",
        "FORMAT": "image/png",
        "TRANSPARENT": "TRUE"
    }
    if time_value:
        params["TIME"] = time_value
    try:
        body, meta = request_bytes(params, MAX_IMAGE)
        result.update(meta)
        if (len(body) < 24 or
                body[:8] != b"\x89PNG\r\n\x1a\n"):
            raise ValueError("GetMap did not return PNG")
        width = int.from_bytes(body[16:20], "big")
        height = int.from_bytes(body[20:24], "big")
        if (width, height) != (256, 256):
            raise ValueError("Unexpected PNG dimensions")
        result["image_signature_valid"] = True
        result["image_width"] = width
        result["image_height"] = height
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error_message"] = str(exc)[:300]
        if isinstance(exc, urllib.error.HTTPError):
            result["http_status"] = exc.code
    return result


def test_li_getmap(time_value=None):
    result = {
        "attempted": True,
        "http_status": None,
        "content_type": None,
        "response_bytes": None,
        "image_signature_valid": False,
        "image_width": None,
        "image_height": None,
        "observation_time_verified": False,
        "pixel_content_verified": False,
        "error_type": None,
        "error_message": None
    }

    params = {
        "SERVICE": "WMS",
        "REQUEST": "GetMap",
        "VERSION": "1.1.1",
        "LAYERS": LI_LAYER,
        "STYLES": "",
        "SRS": "EPSG:4326",
        "BBOX": "30,-15,55,40",
        "WIDTH": "256",
        "HEIGHT": "256",
        "FORMAT": "image/png",
        "TRANSPARENT": "TRUE"
    }

    if time_value:
        params["TIME"] = time_value

    try:
        body, meta = request_bytes(
            params, MAX_IMAGE
        )
        result.update(meta)

        if (
            len(body) < 24
            or body[:8] != b"\x89PNG\r\n\x1a\n"
        ):
            raise ValueError(
                "GetMap did not return expected PNG"
            )

        width = int.from_bytes(
            body[16:20], "big"
        )
        height = int.from_bytes(
            body[20:24], "big"
        )

        if (width, height) != (256, 256):
            raise ValueError(
                "Unexpected PNG dimensions"
            )

        result["image_signature_valid"] = True
        result["image_width"] = width
        result["image_height"] = height

    except Exception as exc:
        result["error_type"] = (
            type(exc).__name__
        )
        result["error_message"] = (
            str(exc)[:300]
        )

        if isinstance(
            exc, urllib.error.HTTPError
        ):
            result["http_status"] = exc.code

    return result


def check_li(root, now):
    result = {
        "layer": LI_LAYER,
        "layer_found": False,
        "time_dimension_present": False,
        "time_dimension": None,
        "latest_advertised_time": None,
        "age_minutes": None,
        "freshness_verified": False,
        "safe_for_nowcasting": False,
        "getmap": {
            "attempted": False
        },
        "status": "unavailable"
    }

    path = layer_path_from(root, LI_LAYER)
    if path is None:
        result["status"] = "layer_not_found"
        return result

    result["layer_found"] = True
    dimension = time_dimension(path)
    result["time_dimension_present"] = bool(
        dimension and dimension["value"]
    )
    result["time_dimension"] = dimension

    latest = latest_advertised_time(
        dimension
    )

    if latest is not None:
        iso = latest.isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")

        result["latest_advertised_time"] = iso
        result["age_minutes"] = round(
            (now - latest).total_seconds() / 60,
            1
        )

    # Frontend currently sends no TIME.
    result["getmap"] = test_li_getmap()

    if latest is not None:
        result["getmap_at_advertised_time"] = (
            test_li_getmap(
                result["latest_advertised_time"]
            )
        )

    # PNG validity does not prove freshness.
    if not result["getmap"].get(
        "image_signature_valid", False
    ):
        result["status"] = "getmap_unavailable"

    elif (
        latest is not None
        and -5 <= result["age_minutes"] <= 30
    ):
        result["status"] = (
            "recent_time_advertised_not_verified"
        )
    else:
        result["status"] = (
            "image_response_unverified_time"
        )

    return result


def validate():
    for name in (
        "latest.json",
        "satellite.json",
        "sibilla_feed.json"
    ):
        with (DATA / name).open(
            encoding="utf-8"
        ) as handle:
            data = json.load(handle)

        if data.get("schema_version") != 1:
            raise ValueError(
                "Invalid schema in " + name
            )


def main():
    stamp = now_utc()
    current = datetime.now(timezone.utc)
    log.info("ZEUS V1.5 started")

    wms_status = "unavailable"
    layer_count = 0
    candidates = {
        "rgb": [],
        "dust": [],
        "ir": []
    }
    diagnostics = {
        "error_type": None,
        "error_message": None
    }
    satellite_times = {}
    li = {
        "layer": LI_LAYER,
        "layer_found": False,
        "status": "not_checked",
        "safe_for_nowcasting": False
    }

    try:
        root, diagnostics = capabilities()
        layers = layers_from(root)
        layer_count = len(layers)
        candidates = candidates_from(layers)
        wms_status = "capabilities_available"
        li = check_li(root, current)
        for key, layer_name in (
            ("rgb", RGB_LAYER), ("ir", IR_LAYER)
        ):
            satellite_times[key] = satellite_layer_info(
                root, layer_name, current
            )

    except Exception as exc:
        diagnostics["error_type"] = (
            type(exc).__name__
        )
        diagnostics["error_message"] = (
            str(exc)[:300]
        )
        log.warning("WMS: %s", exc)

    latest = {
        "schema_version": 1,
        "status": "unavailable",
        "observed_at": None,
        "flashes": [],
        "source": "EUMETSAT MTG LI",
        "reason": "li_ingestion_not_configured",
        "generated_at": stamp
    }

    satellite = {
        "schema_version": 1,
        "generated_at": stamp,
        "provider": "EUMETSAT EUMETView",
        "service": "WMS",
        "endpoint": WMS_URL,
        "status": wms_status,
        "layer_count": layer_count,
        "candidates": candidates,
        "selected_layers": {
            "rgb": RGB_LAYER if satellite_times.get(
                "rgb", {}
            ).get("layer_found") else None,
            "dust": None,
            "ir": IR_LAYER if satellite_times.get(
                "ir", {}
            ).get("layer_found") else None
        },
        "satellite_times": satellite_times,
        "image_download_enabled": False,
        "high_resolution_enabled": False,
        "observation_time": None,
        "error_type": diagnostics.get(
            "error_type"
        ),
        "diagnostics": diagnostics,
        "lightning_imager": li
    }

    sibilla = {
        "schema_version": 1,
        "producer": "Z.E.U.S.",
        "generated_at": stamp,
        "region": "Italy-Mediterranean",
        "status": "unavailable",
        "observation_time": None,
        "lightning": {
            "status": "unavailable",
            "source": "EUMETSAT MTG LI",
            "flashes": [],
            "flash_count": None,
            "density": None,
            "trend": None
        },
        "satellite": {
            "metadata_status": wms_status,
            "rgb_observation": None,
            "ir_observation": None
        },
        "forecast_horizons_modified": False,
        "usable_for_forecasting": False
    }

    publish("latest.json", latest)
    publish("satellite.json", satellite)
    publish("sibilla_feed.json", sibilla)
    validate()

    log.info(
        "LI status: %s",
        li.get("status")
    )
    log.info("ZEUS V1.5 completed")


if __name__ == "__main__":
    main()
    
