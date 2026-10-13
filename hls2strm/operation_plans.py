"""只读影响计算：逐批读取作品，归库原因和范围摘要不访问源站。"""

import copy
import hashlib
import json
from dataclasses import fields

from pydantic import TypeAdapter

from .queries import VideoQuery
from .rules import match_rule, normalize_rule
from .sites import SITES
from .source_policy import eligible

LIB_FIELDS = ("name","dir","path_template","external_dir","versions","rule","sources","excludes")
VIDEO_FIELDS = ("id","slug","code","title","status","uncensored","models","categories","tags","quality","detail_at","duration","release_date","maker","director","series")


def video_input(video):
    values = {key:video.get(key) for key in VIDEO_FIELDS}
    values["detail_at"] = bool(video.get("detail_at"))
    return values


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(",",":")).encode()).hexdigest()


def library_body(engine, payload, *, validate=True):
    from .engine import _check_versions
    raw = payload["values"]
    lib_id = payload.get("library_id")
    if validate:
        name, directory, template, external = engine._check_library(raw["name"],raw["dir"],raw.get("path_template", ""),raw.get("external_dir", ""),lib_id)
        sources, excludes = engine._check_links(lib_id,list(raw.get("sources",[])),list(raw.get("excludes",[])))
    else:
        name,directory = raw["name"].strip(),raw["dir"].strip().rstrip("/\\")
        template = raw.get("path_template","") if raw.get("path_template","").strip() else ""
        external = raw.get("external_dir","").strip().rstrip("/\\")
        sources,excludes = sorted(set(raw.get("sources",[]))),sorted(set(raw.get("excludes",[])))
    return {"name":name,"dir":directory,"path_template":template,"external_dir":external,"versions":_check_versions(raw.get("versions","")),"rule":normalize_rule(raw.get("rule")),"sources":sources,"excludes":excludes}


def selection_query(payload):
    raw = dict(payload.get("query") or {})
    if "filter" in raw:
        raw["status"] = raw.pop("filter")
    for alias,name in (("model","models"),("category","categories"),("tag","tags"),("maker","makers")):
        if alias in raw:
            raw[name] = raw.pop(alias)
    allowed = {f.name for f in fields(VideoQuery)}
    unknown = set(raw)-allowed
    if unknown:
        raise ValueError(f"未知筛选条件：{','.join(sorted(unknown))}")
    return TypeAdapter(VideoQuery).validate_python({k:v for k,v in raw.items() if k in allowed})


def context(engine, request):
    libs = [{"id":lib["id"],**{k:lib[k] for k in LIB_FIELDS}} for lib in sorted(engine.libs.values(),key=lambda x:x["id"])]
    settings = engine.store.current
    common = {"libraries":libs,"output_dir":str(engine.store.output_dir),"archive_dir":str(engine.store.archive_dir),"path_template":settings.path_template,"play_mode":settings.play_mode,"write_nfo":settings.write_nfo,"public_base_url":engine.store.public_base_url,"play_token":settings.play_token}
    if request["kind"] in ("retirement","selection"):
        common["sites"] = {name:{"enabled":cfg.enabled,"lines":{n:{"enabled":v.enabled} for n,v in cfg.lines.items()}} for name,cfg in settings.sites.items()}
        common["site_priority"] = settings.site_priority
    if request["kind"] == "dedupe":
        common["trash_days"] = settings.trash_days
    if request["kind"] == "library":
        common["relocate_scraped"] = settings.relocate_scraped
    return common


def dependency_order(libs):
    out,seen = [],set()
    def visit(lib_id):
        if lib_id in seen:
            return
        seen.add(lib_id)
        for dep in libs[lib_id]["sources"] + libs[lib_id]["excludes"]:
            visit(dep)
        out.append(lib_id)
    for lib_id in libs:
        visit(lib_id)
    return out


def affected_libraries(libs, root):
    affected = {root}
    while True:
        extra = {i for i,l in libs.items() if set(l["sources"]+l["excludes"]) & affected}
        if extra <= affected:
            return affected
        affected.update(extra)


class Planner:
    def __init__(self,engine,request,baseline=None):
        self.e,self.db,self.request = engine,engine.db,request
        self.payload = request["payload"]
        self.kind = request["kind"]
        self.libs = copy.deepcopy(engine.libs if baseline is None else {lib["id"]:lib for lib in baseline["libraries"]})
        self.actual_root = None
        self.root = self.payload.get("library_id") or 0
        self.affected = set()
        if self.kind == "library":
            self.old = self.libs.get(self.root)
            self.desired = library_body(engine,self.payload) if baseline is None else copy.deepcopy(self.payload["values"])
            self.libs[self.root] = {"id":self.root,**self.desired}
            self.affected = affected_libraries(self.libs,self.root)
            self.order = dependency_order(self.libs)
            self.rewrite = bool(self.old and (self.old.get("operation_pending") or any(self.old[k]!=self.desired[k] for k in ("dir","path_template","versions"))))
            self.locate = bool(self.old and self.desired["external_dir"] and self.old["external_dir"]!=self.desired["external_dir"])
        elif self.kind == "retirement":
            if self.payload["site"] not in SITES:
                raise ValueError("未知站点")
        else:
            ids = self.payload.get("ids")
            if ids is not None and (not isinstance(ids,list) or not ids or any(type(i) is not int or i<1 for i in ids)):
                raise ValueError("显式选择必须是非空的影片 ID 列表")
            action = self.payload["action"]
            if action not in ("probe","refresh","add","remove","remove_source","restore_source") or (self.payload.get("ids") is None and action not in ("probe","refresh","add")):
                raise ValueError("按筛选结果仅支持补源、刷新详情和加入输出库；文件移出/删源需显式选择影片")
            if action in ("add","remove"):
                engine._library(self.root)
            if action=="probe" and "sites" in self.payload:
                sites = self.payload["sites"]
                if not isinstance(sites, list) or any(not isinstance(n, str) or n not in SITES for n in sites):
                    raise ValueError("sites 必须是已知站点名称列表")
            self.query = selection_query(self.payload)

    async def batches(self):
        last = -1
        upper = (await self.db._one("SELECT COALESCE(MAX(id),-1) AS id FROM videos",bulk=True))["id"]
        while True:
            params = []
            if self.kind == "selection":
                cond,params = self.query.where()
            elif self.kind == "retirement":
                cond = "id IN (SELECT video_id FROM sources WHERE site=? AND status!='removed')"
                params = [self.payload["site"]]
            else:
                cond = "1=1"
            ids = self.payload.get("ids") if self.kind == "selection" else None
            if ids is not None:
                # 显式跨页选择同样分批读取，避免生成超长 IN SQL。
                sorted_ids = sorted(set(ids))
                for start in range(0,len(sorted_ids),200):
                    chunk = sorted_ids[start:start+200]
                    rows = await self.db._all(f"SELECT * FROM videos WHERE id IN ({','.join('?' for _ in chunk)}) ORDER BY id",chunk,bulk=True)
                    yield await self.enrich(rows)
                return
            rows = await self.db._all(f"SELECT * FROM videos WHERE ({cond}) AND id>? AND id<=? ORDER BY id LIMIT 200",(*params,last,upper),bulk=True)
            if not rows:
                return
            last = rows[-1]["id"]
            yield await self.enrich(rows)

    async def enrich(self,rows):
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        outputs = await self.db.outputs_for(ids)
        sources = await self.db.sources_for(ids) if self.kind != "library" else {}
        lines = await self.db.lines_for([s["id"] for group in sources.values() for s in group]) if self.kind == "retirement" or self.payload.get("action")=="remove_source" else {}
        out = []
        for row in rows:
            v = dict(row)
            for key in ("models","categories","tags"):
                v[key] = json.loads(v[key])
            memberships = {o["library_id"]:o for o in outputs.get(v["id"],[])}
            if self.actual_root and self.actual_root!=self.root and self.actual_root in memberships:
                memberships[self.root] = memberships.pop(self.actual_root)
            srcs = sources.get(v["id"],[])
            item = self.item(v,memberships,srcs,lines)
            out.append((v["id"],item))
        return out

    def item(self,v,outputs,sources,lines):
        item = {"slug":v["slug"],"title":v["title"],"effects":[],"reason":"保持现状"}
        if self.kind == "selection":
            action,settings = self.payload["action"],self.e.store.current
            targets = []
            if action=="probe":
                have,only = {s["site"] for s in sources},self.payload.get("sites") or []  # sites：只到这些站点找
                targets = [{"kind":"probe","site":n,"target":f"{n}:{v['id']}"} for n in settings.site_priority if settings.site(n).enabled and SITES[n].can_lookup and n not in have and (not only or n in only)]
            elif action=="refresh":
                targets = [{"kind":"detail","site":s["site"],"target":s["key"]} for s in sources if s["status"]=="active" and settings.site(s["site"]).enabled]
            elif action=="add" and v["status"]=="active" and not (set(outputs)&set(self.libs[self.root]["excludes"])):
                targets = [{"kind":"membership","site":"","target":str(v["id"])}]
            elif action=="remove" and self.root in outputs:
                targets = [{"kind":"membership","site":"","target":str(v["id"])}]
                item["effects"] = [{"library_id":self.root,"action":"remove","external":bool(self.libs[self.root]["external_dir"]),"had_file":bool(outputs[self.root]["strm_path"])}]
            elif action=="remove_source" and any(s["site"] in self.payload.get("sites",[]) and s["status"]!="removed" for s in sources):
                targets = [{"kind":"source_remove","site":"","target":str(v["id"])}]
                if not any(s["site"] not in self.payload.get("sites",[]) and eligible(s,settings,lines.get(s["id"],[])) for s in sources):
                    item["effects"] = [{"library_id":lid,"action":"archive","external":bool(self.libs[lid]["external_dir"]),"had_file":bool(out["strm_path"])} for lid,out in outputs.items()]
            elif action=="restore_source" and any(s["status"]=="removed" for s in sources):
                targets = [{"kind":"source_restore","site":"","target":str(v["id"])}]
            if action in ("remove","remove_source","restore_source"):
                item["input"] = digest({"video":video_input(v),"outputs":outputs,"sources":[{k:s.get(k) for k in ("id","site","status")} for s in sources]})
            item.update(targets=targets,already_exists=int(action=="add" and self.root in outputs),skipped=int(not targets))
            item["reason"] = "按已选范围排队处理" if targets else "不适用：没有待处理源、影片非活动或被排除库挡住"
            return item
        if self.kind == "retirement":
            site = self.payload["site"]
            keep = any(s["site"]!=site and eligible(s,self.e.store.current,lines.get(s["id"],[])) for s in sources)
            item.update(keep=keep,outputs=len(outputs),reason="另有启用且支持的源，保留影片" if keep else "没有其他启用且支持的源，移出输出并归档")
            item["input"] = digest({"video":video_input(v),"outputs":outputs,"sources":[{k:s.get(k) for k in ("id","site","status")} for s in sources],"lines":{s["id"]:[{k:l.get(k) for k in ("id","line","host")} for l in lines.get(s["id"],[])] for s in sources}})
            return item
        after = copy.deepcopy(outputs)
        root = self.libs[self.root]
        if root["rule"]:
            should = v["status"]=="active" and match_rule(root["rule"],v) and not (set(after)&set(root["excludes"]))
            current = after.get(self.root)
            if should and current is None:
                after[self.root] = {"via":"rule","strm_path":"" if root["excludes"] else "planned"}
                item["reason"] = "命中归库规则" + ("，等待排除库确认后写出" if root["excludes"] else "")
            elif not should and current and current["via"]=="rule":
                del after[self.root]
                item["reason"] = "不再命中规则或被排除库挡住"
        for lid in self.order:
            if lid not in self.affected:
                continue
            lib,current = self.libs[lid],after.get(lid)
            blocked = bool(set(after)&set(lib["excludes"]))
            from_source = any(i in after and after[i]["strm_path"] for i in lib["sources"])
            if current and (blocked or current["via"]=="source" and not from_source):
                del after[lid]
                item["reason"] = "命中排除库，或已不在有效来源库中"
            if lid not in after and from_source and not blocked and v["status"]=="active":
                after[lid] = {"via":"source","strm_path":"" if lib["excludes"] else "planned"}
                item["reason"] = "从来源库归入" + ("，等待排除库确认" if lib["excludes"] else "")
        for lid in sorted(self.affected):
            before,current = outputs.get(lid),after.get(lid)
            action = "add" if not before and current else "remove" if before and not current else "rewrite" if before and current and lid==self.root and self.rewrite else None
            if action:
                item["effects"].append({"library_id":lid,"action":action,"via":current["via"] if current else "","had_file":bool(before and before["strm_path"]),"external":bool(self.libs[lid]["external_dir"]),"waiting":bool(current and not current["strm_path"])})
        # 换到排除库的外部整理库影片：执行时先加入再移出，整理好的目录会搬过去（实际能不能搬执行时按目录核对）
        adds = {e["library_id"] for e in item["effects"] if e["action"]=="add" and e["external"]}
        for effect in item["effects"]:
            if effect["action"]=="remove" and effect["external"] and effect["had_file"] and self.e.store.current.relocate_scraped and adds & set(self.libs[effect["library_id"]]["excludes"]):
                effect["relocate"] = True
        item["input"] = digest({"video":video_input(v),"other_outputs":{i:o for i,o in outputs.items() if i not in self.affected}})
        item["before"] = {i:outputs[i] for i in self.affected if i in outputs}
        return item
