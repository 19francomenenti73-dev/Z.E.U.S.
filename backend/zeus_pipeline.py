
#!/usr/bin/env python3
# Z.E.U.S. V1.1 — Nowcasting Italia
# Diagnostica EUMETView WMS
# Feed osservativo per S.I.B.I.L.L.A.
# Python 3.12 — solo libreria standard
# Nessuna immagine HD, nessun dato simulato

import json
import logging
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

WMS_URL = os.getenv(
    "ZEUS_WMS_URL",
    "https://view.eumetsat.int/geoserver/wms"
)
TIMEOUT = 30
MAX_RESPONSE = 16 * 1024 * 1024
USER_AGENT = "ZEUS-NowcastingItalia/1.1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("ZEUS")


def utc_now():
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def write_json(name, payload):
    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / name
    content = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        allow_nan=False
    ) + "\n"

    if path.exists():
        if path.read_text(encoding="utf-8") == content:
            return

    temp = path.with_suffix(".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)
    log.info("Published %s", name)


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def child_text(node, name):
    for child in node:
        if local_name(child.tag) == name:
            return (child.text or "").strip()
    return ""


def get_capabilities():
    diagnostics = {
        "http_status": None,
        "content_type": None,
        "response_bytes": 0,
        "xml_root": None,
        "error_type": None,
        "error_message": None
    }

    query = urllib.parse.urlencode({
        "SERVICE": "WMS",
        "REQUEST": "GetCapabilities",
        "VERSION": "1.1.1"
    })

    url = WMS_URL + (
        "&" if "?" in WMS_URL else "?"
    ) + query

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/xml,text/xml,*/*"
        }
    )

    try:
        with urllib.request.urlopen(
            request, timeout=TIMEOUT
        ) as response:
            diagnostics["http_status"] = response.status
            diagnostics["content_type"] = (
                response.headers.get("Content-Type", "")
            )

            body = response.read(MAX_RESPONSE + 1)
            diagnostics["response_bytes"] = len(body)

        if len(body) > MAX_RESPONSE:
            raise ValueError(
                "Capabilities exceed 16 MB limit"
            )

        if not body.strip():
            raise ValueError("Empty WMS response")

        upper = body.upper()
        if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
            raise ValueError(
                "XML DTD or entity declaration rejected"
            )

        root = ET.fromstring(body)
        root_name = local_name(root.tag)
        diagnostics["xml_root"] = root_name

        if root_name in (
            "ServiceExceptionReport",
            "ExceptionReport"
        ):
            messages = [
                (node.text or "").strip()
                for node in root.iter()
                if local_name(node.tag) in (
                    "ServiceException", "ExceptionText"
                )
            ]
            raise ValueError(
                "WMS service exception: " +
                "; ".join(messages)[:300]
            )

        if root_name not in (
            "WMT_MS_Capabilities",
            "WMS_Capabilities"
        ):
            raise ValueError(
                "Unexpected XML root: " + root_name
            )

        return root, diagnostics

    except Exception as exc:
        diagnostics["error_type"] = type(exc).__name__
        diagnostics["error_message"] = str(exc)[:400]

        if isinstance(exc, urllib.error.HTTPError):
            diagnostics["http_status"] = exc.code

        log.warning(
            "WMS diagnostic: %s: %s",
            type(exc).__name__, exc
        )
        return None, diagnostics


def discover_layers(root):
    layers = []

    for node in root.iter():
        if local_name(node.tag) != "Layer":
            continue

        name = child_text(node, "Name")
        title = child_text(node, "Title")

        if not name:
            continue

        layers.append({
            "name": name,
            "title": title or name
        })

    return layers


def select_candidates(layers):
    candidates = {
        "rgb": [],
        "dust": [],
        "ir": []
    }

    for layer in layers:
        label = (
            layer["name"] + " " + layer["title"]
        ).lower()

        if re.search(
            r"geo[\s_-]?colou?r|true[\s_-]?colou?r",
            label
        ):
            candidates["rgb"].append(layer)

        if re.search(r"\bdust\b", label):
            candidates["dust"].append(layer)

        if re.search(
            r"\binfrared\b|\bthermal\b|\bir[\s_-]?10",
            label
        ):
            candidates["ir"].append(layer)

    return {
        key: values[:10]
        for key, values in candidates.items()
    }


def validate_outputs():
    names = (
        "latest.json",
        "satellite.json",
        "sibilla_feed.json"
    )

    for name in names:
        with (DATA / name).open(
            encoding="utf-8"
        ) as handle:
            value = json.load(handle)

        if not isinstance(value, dict):
            raise ValueError("Invalid JSON: " + name)

        if value.get("schema_version") != 1:
            raise ValueError("Invalid schema: " + name)

    latest = json.loads(
        (DATA / "latest.json").read_text(
            encoding="utf-8"
        )
    )

    assert latest["status"] == "unavailable"
    assert latest["flashes"] == []


def main():
    now = utc_now()
    log.info("ZEUS V1.1 started")

    root, diagnostics = get_capabilities()

    candidates = {
        "rgb": [],
        "dust": [],
        "ir": []
    }
    layer_count = 0
    wms_status = "unavailable"

    if root is not None:
        layers = discover_layers(root)
        candidates = select_candidates(layers)
        layer_count = len(layers)

        wms_status = "capabilities_available"
        log.info("WMS layers: %s", layer_count)

        for key, values in candidates.items():
            log.info(
                "Candidate %s layers: %s",
                key, len(values)
            )

    latest = {
        "schema_version": 1,
        "status": "unavailable",
        "observed_at": None,
        "flashes": [],
        "source": "EUMETSAT MTG LI",
        "reason": "li_ingestion_not_configured",
        "generated_at": now
    }

    satellite = {
        "schema_version": 1,
        "generated_at": now,
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
        "error_type": diagnostics["error_type"],
        "diagnostics": diagnostics
    }

    sibilla = {
        "schema_version": 1,
        "producer": "Z.E.U.S.",
        "generated_at": now,
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

    write_json("latest.json", latest)
    write_json("satellite.json", satellite)
    write_json("sibilla_feed.json", sibilla)

    validate_outputs()
    log.info("ZEUS V1.1 completed")


if __name__ == "__main__":
    main()
    
