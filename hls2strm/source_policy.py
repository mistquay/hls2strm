"""Local source eligibility. Never resolves URLs or probes playback."""
from .sites import SITES


def eligible(source, settings, lines=()):
    site = SITES.get(source["site"])
    if not site or source["status"] != "active" or not settings.site(source["site"]).enabled:
        return False
    if not site.line_specs:
        return True
    cfg = settings.site(source["site"])
    names = [line["line"] for line in lines] or list(site.line_specs)
    return any(cfg.line(name).enabled and
               (name not in site.line_specs or site.line_specs[name].supported) for name in names)
