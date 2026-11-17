
#!/usr/bin/env python3
# Z.E.U.S. V1 — Nowcasting Italia
# Backend satellitare pubblico e feed S.I.B.I.L.L.A.
# Python 3.12 — nessuna dipendenza esterna
# Nessun dato simulato, nessuna immagine HD

import json
import logging
import os
import re
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
TIMEOUT = 25
MAX_RESPONSE = 4 * 1024 * 1024
USER_AGENT = "ZEUS-NowcastingItalia/1.0"

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
        payload, ensure_ascii=False, indent=2,
        allow_nan=False
    ) + "\n"

    if path.exists():
        if path.read_text(encoding="utf-8") == content:
            log.info("%s unchanged", name)
            return

    temp = path.with_suffix(".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)
    log.info("%s published", name)


def get_capabilities():
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
            "Accept": "application/xml,text/xml"
        }
    )

    with urllib.request.urlopen(
        request, timeout=TIMEOUT
    ) as response:
        content_type = response.headers.get(
            "Content-Type", ""
        ).lower()
        if not any(x in content_type for x in (
            "xml", "text/plain", "application/octet-stream"
        )):
            raise ValueError("Unexpected content type")

        body = response.read(MAX_RESPONSE + 1)
        if len(body) > MAX_RESPONSE:
            raise ValueError("Capabilities too large")

    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise ValueError("XML entities are not allowed")

    return ET.fromstring(body)


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def child_text(node, tag):
    for child in node:
        if local_name(child.tag) == tag:
            return (child.text or "").strip()
    return ""


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
    rgb = []
    dust = []

    for layer in layers:
        text = (
            layer["name"] + " " + layer["title"]
        ).lower()

        if re.search(
            r"geo.?colou?r|true.?colou?r",
            text
        ):
            rgb.append(layer)

        if re.search(r"\bdust\b", text):
            dust.append(layer)

    return {
        "rgb": rgb[:10],
        "dust": dust[:10]
    }


def validate_outputs():
    for name in (
        "latest.json",
        "satellite.json",
        "sibilla_feed.json"
    ):
        with (DATA / name).open(
            encoding="utf-8"
        ) as handle:
            value = json.load(handle)

        if not isinstance(value, dict):
            raise ValueError("Invalid JSON: " + name)

    latest = json.loads(
        (DATA / "latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert latest["schema_version"] == 1
    assert latest["status"] == "unavailable"
    assert latest["flashes"] == []


def main():
    now = utc_now()
    log.info("Z.E.U.S. pipeline started")

    wms_status = "unavailable"
    candidates = {"rgb": [], "dust": []}
    layer_count = 0
    error_message = None

    try:
        root = get_capabilities()
        layers = discover_layers(root)
        candidates = select_candidates(layers)
        layer_count = len(layers)
        wms_status = "capabilities_available"
        log.info("WMS layers discovered: %s", layer_count)

    except (
        urllib.error.URLError,
        TimeoutError,
        ValueError,
        ET.ParseError,
        OSError
    ) as exc:
        error_message = type(exc).__name__
        log.warning("WMS unavailable: %s", exc)

    # Compatibile con l'index.html esistente.
    # Un array vuoto NON indica assenza di fulmini.
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
            "dust": None
        },
        "image_download_enabled": False,
        "high_resolution_enabled": False,
        "observation_time": None,
        "error_type": error_message
    }

    # Interfaccia osservativa per SIBILLA.
    # Non altera gli orizzonti 3/24/48/72h.
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
    log.info("Z.E.U.S. pipeline completed")


if __name__ == "__main__":
    main()
