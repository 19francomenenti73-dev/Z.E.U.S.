
#!/usr/bin/env python3
# Z.E.U.S. V1.3 - LI WMS diagnostics
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
WMS_URL = os.getenv(
    "ZEUS_WMS_URL",
    "https://view.eumetsat.int/geoserver/wms"
)
LI_LAYER = "mtg_fd:li_afa"
TIMEOUT = 30
MAX_XML = 16 * 1024 * 1024
MAX_IMAGE = 1024 * 1024
USER_AGENT = "ZEUS-NowcastingItalia/1.3"

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
        status = response.status
        content_type = response.headers.get(
            "Content-Type", ""
        )
        body = response.read(limit + 1)

    if len(body) > limit:
        raise ValueError("Response size limit exceeded")
    return status, content_type, body


def capabilities():
    params = {
        "SERVICE": "WMS",
        "REQUEST": "GetCapabilities",
        "VERSION": "1.1.1"
    }
    status, ctype, body = request_bytes(
        params, MAX_XML
    )
    root = parse_xml(body)
    root_name = local(root.tag)

    if root_name not in (
        "WMT_MS_Capabilities",
        "WMS_Capabilities"
    ):
        raise ValueError(
            "Invalid capabilities root: " + root_name
        )

    return root, {
        "http_status": status,
        "content_type": ctype,
        "response_bytes": len(body),
        "xml_root": root_name
    }


def layers_from(root):
    layers = []
    for node in root.iter():
        if local(node.tag) != "Layer":
            continue
        name = child_text(node, "Name")
        if name:
            layers.append({
                "name": name,
                "title": child_text(node, "Title") or name
            })
    return layers


def candidates_from(layers):
    result = {"rgb": [], "dust": [], "ir": []}
    for layer in layers:
        label = (
            layer["name"] + " " + layer["title"]
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


def li_node_from(root):
    for node in root.iter():
        if local(node.tag) == "Layer":
            if child_text(node, "Name") == LI_LAYER:
                return node
    return None


def time_dimension(node):
    if node is None:
        return None

    for item in node:
        if local(item.tag) not in (
            "Dimension", "Extent"
        ):
            continue
        if item.attrib.get(
            "name", ""
        ).lower() == "time":
            return (item.text or "").strip()

    return None


def parse_time(value):
    try:
        dt = datetime.fromisoformat(
            value.strip().replace("Z", "+00:00")
        )
        if dt.tzinfo is None:
            return None
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def latest_advertised_time(dimension):
    if not dimension:
        return None

    times = []
    for entry in dimension.split(","):
        parts = entry.strip().split("/")
        if len(parts) == 1:
            candidate = parts[0]
        elif len(parts) >= 2:
            candidate = parts[1]
        else:
            continue

        dt = parse_time(candidate)
        if dt is not None:
            times.append(dt)

    if not times:
        return None

    return max(times)


def test_li_getmap(time_value):
    result = {
        "attempted": True,
        "http_status": None,
        "content_type": None,
        "response_bytes": None,
        "image_signature_valid": False,
        "image_width": None,
        "image_height": None,
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
        status, ctype, body = request_bytes(
            params, MAX_IMAGE
        )
        result["http_status"] = status
        result["content_type"] = ctype
        result["response_bytes"] = len(body)

        if (
            body[:8] == b"\x89PNG\r\n\x1a\n"
            and len(body) >= 24
        ):
            width = int.from_bytes(
                body[16:20], "big"
            )
            height = int.from_bytes(
                body[20:24], "big"
            )
            if width == 256 and height == 256:
                result["image_signature_valid"] = True
                result["image_width"] = width
                result["image_height"] = height
            else:
                raise ValueError(
                    "Unexpected PNG dimensions"
                )
        else:
            raise ValueError(
                "GetMap did not return expected PNG"
            )

    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error_message"] = str(exc)[:300]
        if isinstance(exc, urllib.error.HTTPError):
            result["http_status"] = exc.code

    return result


def check_li(root, now):
    result = {
        "layer": LI_LAYER,
        "layer_found": False,
        "time_dimension_present": False,
        "latest_advertised_time": None,
        "age_minutes": None,
        "freshness_verified": False,
        "getmap": {
            "attempted": False
        },
        "status": "unavailable"
    }

    node = li_node_from(root)
    if node is None:
        result["status"] = "layer_not_found"
        return result

    result["layer_found"] = True
    dimension = time_dimension(node)
    result["time_dimension_present"] = bool(dimension)

    latest = latest_advertised_time(dimension)
    requested_time = None

    if latest is not None:
        stamp = latest.isoformat().replace(
            "+00:00", "Z"
        )
        result["latest_advertised_time"] = stamp
        age = (
            now - latest
        ).total_seconds() / 60
        result["age_minutes"] = round(age, 1)
        requested_time = stamp

    result["getmap"] = test_li_getmap(
        requested_time
    )

    image_ok = result["getmap"].get(
        "image_signature_valid", False
    )
    fresh = (
        latest is not None
        and -5 <= result["age_minutes"] <= 30
    )

    # Metadata freshness is not proof of image
    # pixel content or lightning occurrence.
    result["freshness_verified"] = bool(fresh)

    if image_ok and fresh:
        result["status"] = "image_response_recent"
    elif image_ok:
        result["status"] = "image_response_unverified_time"
    else:
        result["status"] = "getmap_unavailable"

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
    log.info("ZEUS V1.3 started")

    wms_status = "unavailable"
    layer_count = 0
    candidates = {
        "rgb": [], "dust": [], "ir": []
    }
    diagnostics = {
        "error_type": None,
        "error_message": None
    }
    li = {
        "layer": LI_LAYER,
        "layer_found": False,
        "status": "not_checked"
    }

    try:
        root, diagnostics = capabilities()
        layers = layers_from(root)
        layer_count = len(layers)
        candidates = candidates_from(layers)
        wms_status = "capabilities_available"
        li = check_li(root, current)
    except Exception as exc:
        diagnostics["error_type"] = type(exc).__name__
        diagnostics["error_message"] = str(exc)[:300]
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
            "rgb": None,
            "dust": None,
            "ir": None
        },
        "image_download_enabled": False,
        "high_resolution_enabled": False,
        "observation_time": None,
        "error_type": diagnostics.get("error_type"),
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
    log.info("ZEUS V1.3 completed")


if __name__ == "__main__":
    main()
    
