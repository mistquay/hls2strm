"""本地资料筛选；影片级来源条件复用 VideoQuery。"""

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .operation_plans import selection_query


class AssetFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    library_ids: list[int] = Field(default_factory=list)
    video_ids: list[int] = Field(default_factory=list)
    asset_ids: list[int] = Field(default_factory=list)
    exclude_ids: list[int] = Field(default_factory=list)
    source: Literal["nfo", "effective"] = "nfo"
    q: str = ""
    categories: list[str] = Field(default_factory=list)
    category_mode: Literal["any", "all", "not"] = "any"
    tags: list[str] = Field(default_factory=list)
    tag_mode: Literal["any", "all", "not"] = "any"
    models: list[str] = Field(default_factory=list)
    maker: str = ""
    director: str = ""
    series: str = ""
    release_from: str = ""
    release_to: str = ""
    year_from: int | None = Field(None, ge=1000, le=9999)
    year_to: int | None = Field(None, ge=1000, le=9999)
    duration_min: int | None = Field(None, ge=0)
    duration_max: int | None = Field(None, ge=0)
    bytes_min: int | None = Field(None, ge=0)
    extra_bytes_min: int | None = Field(None, ge=0)
    extra_count_min: int | None = Field(None, ge=0)
    status: Literal["", "present", "missing", "conflict", "unmanaged", "cleaned"] = ""
    nfo_status: Literal["", "valid", "missing", "invalid", "conflict"] = ""
    include_unknown: bool = False
    video: dict = Field(default_factory=dict)
    sort: Literal["bytes", "extra_bytes", "release", "id"] = "bytes"

    @field_validator("categories", "tags", "models")
    @classmethod
    def values(cls, value):
        return list(dict.fromkeys(v.strip() for v in value if v.strip()))

    @field_validator("release_from", "release_to")
    @classmethod
    def date(cls, value):
        if value:
            dt.date.fromisoformat(value)
        return value

    def where(self):
        where = ["a.scan_id=r.scan_id", "r.health!='retired'"]
        args = []
        for col, values, negative in (("a.library_id", self.library_ids, False), ("a.video_id", self.video_ids, False),
                                      ("a.id", self.asset_ids, False), ("a.id", self.exclude_ids, True)):
            if values:
                import json
                where.append(f"{col} {'NOT IN' if negative else 'IN'} (SELECT value FROM json_each(?))")
                args.append(json.dumps(sorted(set(values))))
        if self.video:
            condition, params = selection_query({"query": self.video}).where()
            where.append(f"a.video_id IN (SELECT id FROM videos WHERE {condition})")
            args.extend(params)
        if self.q.strip():
            where.append("(a.path LIKE ? OR v.code LIKE ? OR v.title LIKE ? OR json_extract(a.metadata,'$.title') LIKE ?)")
            args += [f"%{self.q.strip()}%"] * 4
        for col, value in (("a.status", self.status), ("a.nfo_status", self.nfo_status)):
            if value:
                where.append(col + "=?")
                args.append(value)
        data = "a.metadata" if self.source == "nfo" else "a.effective"
        for key, values, mode in (("categories", self.categories, self.category_mode), ("tags", self.tags, self.tag_mode),
                                   ("models", self.models, "any")):
            if not values:
                continue
            import json
            check = "EXISTS (SELECT 1 FROM asset_facets f WHERE f.asset_id=a.id AND f.origin=? AND f.kind=? AND f.value=?)"
            clauses = [check] * len(values)
            expression = (" AND " if mode == "all" else " OR ").join(clauses)
            args.extend(x for value in values for x in (self.source, key, value))
            if mode == "not":
                expression = "NOT (" + expression + ")"
            known = f"json_array_length(json_extract({data},'$.{key}'))>0"
            where.append(f"(({known}) AND ({expression}))" + (f" OR COALESCE(json_array_length(json_extract({data},'$.{key}')),0)=0" if self.include_unknown else ""))
        for key, value, op in (("maker", self.maker, "="), ("director", self.director, "="), ("series", self.series, "="),
            ("release_date", self.release_from, ">="), ("release_date", self.release_to, "<="),
            ("year", self.year_from, ">="), ("year", self.year_to, "<="),
            ("duration", self.duration_min, ">="), ("duration", self.duration_max, "<=")):
            if value is None or value == "":
                continue
            col = f"json_extract({data},'$.{key}')"
            check = f"({col} IS NOT NULL AND {col}!='' AND {col}{op}?)"
            if self.include_unknown:
                check = f"({check} OR {col} IS NULL OR {col}='')"
            where.append(check)
            args.append(value)
        for key, value in (("bytes", self.bytes_min), ("extra_bytes", self.extra_bytes_min), ("extra_count", self.extra_count_min)):
            if value is not None:
                where.append(f"a.{key}>=?")
                args.append(value)
        return " AND ".join(f"({part})" for part in where), args


FROM = "library_assets a JOIN maintenance_roots r ON r.library_id=a.library_id AND r.root=a.root LEFT JOIN videos v ON v.id=a.video_id"


async def list_assets(db, query, offset=0, limit=50):
    from .maintenance_scan import decode_asset
    condition, args = query.where()
    sort = {"bytes": "a.bytes DESC", "extra_bytes": "a.extra_bytes DESC", "release": "json_extract(a.metadata,'$.release_date') DESC", "id": "a.id"}[query.sort]
    rows = await db._all(f"SELECT a.*,v.code,v.slug,v.uncensored,v.title AS source_title,r.health,r.checked_at FROM {FROM} WHERE {condition} ORDER BY {sort},a.id LIMIT ? OFFSET ?",
                         (*args, limit, offset), bulk=True)
    count = await db._one(f"SELECT COUNT(*) AS n,COALESCE(SUM(a.bytes),0) AS bytes,COALESCE(SUM(a.extra_bytes),0) AS extra_bytes FROM {FROM} WHERE {condition}", args, bulk=True)
    items = []
    for row in rows:
        item = decode_asset(row)
        item["file_count"] = len(item["data"]["files"])
        item["details"] = {k: v for k, v in item.pop("data").items() if k not in ("files", "folders")}
        items.append(item)
    return {"items": items, "total": count["n"], "bytes": count["bytes"], "extra_bytes": count["extra_bytes"]}
