"""Source metadata patches: retain existing XML, fill only empty fields."""

import io
import xml.etree.ElementTree as ET

from PIL import Image

from . import nfo


def patch_nfo(video, original=None):
    if original is not None:
        nfo.parse(original)
        root = ET.fromstring(original, parser=ET.XMLParser(target=nfo.SafeTree(insert_comments=True, insert_pis=True)))
    else:
        root = ET.Element("movie")
    changes = []

    def fill(tag, value, field=None):
        if value in (None, "", []):
            return
        nodes = root.findall(tag)
        if any((node.text or "").strip() for node in nodes):
            return
        node = nodes[0] if nodes else ET.SubElement(root, tag)
        node.text = str(value)
        changes.append({"field": field or tag, "before": "", "after": value})

    fill("title", video.get("title"))
    fill("originaltitle", video.get("title"))
    if not root.findtext("num") and not any((n.text or "").strip() for n in root.findall("uniqueid")):
        fill("num", video.get("code"))
    for key, tag in (("maker", "studio"), ("director", "director")):
        fill(tag, video.get(key), key)
    if video.get("duration", 0) and video["duration"] > 0:
        fill("runtime", max(1, round(video["duration"] / 60)), "duration_minutes")
    for key, tag in (("categories", "genre"), ("tags", "tag"), ("models", "actor")):
        nodes = root.findall(tag)
        if any(((n.findtext("name") if tag == "actor" else n.text) or "").strip() for n in nodes):
            continue
        names = list(dict.fromkeys(v["name"].strip() for v in video.get(key, []) if v.get("name", "").strip()))
        if names:
            for name in names:
                node = ET.SubElement(root, tag)
                if tag == "actor":
                    ET.SubElement(node, "name").text = name
                else:
                    node.text = name
            changes.append({"field": key, "before": [], "after": names})
    if video.get("series") and not (root.findtext("set/name") or root.findtext("set") or "").strip():
        node = root.find("set")
        if node is None:
            node = ET.SubElement(root, "set")
        name = node.find("name")
        if name is None:
            name = ET.SubElement(node, "name")
        name.text = video["series"]
        changes.append({"field": "series", "before": "", "after": video["series"]})
    # Historical release_date has no provenance and may be an upload date.
    data = ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"
    return data, changes


def picture(data):
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        image = image.convert("RGB")
        # Do not assume that a wide site screenshot is a DVD wraparound cover.
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=92)
        return output.getvalue()
