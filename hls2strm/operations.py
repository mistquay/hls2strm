"""后台只读预览、确认范围与持久化目标；文件变更使用已有输出/归档逻辑。"""

import copy
import hashlib
import json
import secrets
import time

from .operation_plans import Planner, context, digest, library_body


class ScopeChanged(ValueError):
    pass


def memberships_match(expected, actual, *, locate=False):
    """接受变更前或本任务已完成的归属，支持文件副作用后、游标落盘前重启。"""
    before = expected["before"]
    after = actual["before"]
    effects = {str(effect["library_id"]):effect for effect in expected["effects"]}
    for lid in set(map(str,before)) | set(map(str,after)) | set(effects):
        old,now = before.get(lid),after.get(int(lid))
        effect = effects.get(lid)
        if old is None and now is None:
            continue
        if old and now and old["via"]==now["via"] and (old["strm_path"]==now["strm_path"] or locate):
            continue
        if effect and effect["action"]=="remove" and now is None:
            continue
        if effect and effect["action"] in ("add","rewrite") and now and now["via"]==effect["via"]:
            continue
        return False
    return True


class Operations:
    def __init__(self,engine):
        self.e,self.db = engine,engine.db

    async def create(self,request):
        if request["kind"]=="retirement":
            from .site_retirement import _retired
            await _retired(self.db,request["payload"]["site"])
        if request["kind"]=="library":
            request["payload"]["values"] = library_body(self.e,request["payload"])
        if request["kind"] == "dedupe":
            from .dedupe import options
            options(request["payload"])
        else:
            Planner(self.e,request)  # 拒绝无效配置/筛选，不创建空壳任务。
        job = await self.db.create_job("preview","计算影响预览",{"operation":request})
        await self.db.add_tasks(job,"preview",["all"],100)
        self.e.notify()
        return job

    async def calculate(self,request,job_id,*,save=False):
        if request["kind"] == "dedupe":
            from .dedupe import calculate
            return await calculate(self.e, request, job_id, save)
        baseline = context(self.e,request)
        planner = Planner(self.e,request)
        counts = {"matched":0,"added":0,"removed":0,"rewritten":0,"archived":0,"kept":0,"unknown":0,"relocated":0,"skipped":0,"already_exists":0,"tasks":0}
        samples = []
        fingerprint = hashlib.sha256(digest(baseline).encode())
        if save:
            await self.db._write("DELETE FROM operation_items WHERE preview_id=?",(job_id,))
        async for batch in planner.batches():
            await self.e.checkpoint(job_id)
            rows = []
            for video_id,item in batch:
                counts["matched"] += 1
                if request["kind"]=="retirement":
                    counts["kept"] += int(item["keep"])
                    counts["removed"] += int(not item["keep"])
                    counts["archived"] += item["outputs"] if not item["keep"] else 0
                elif request["kind"]=="selection":
                    counts["skipped"] += item["skipped"]
                    counts["already_exists"] += item["already_exists"]
                    counts["tasks"] += len(item["targets"])
                    if request["payload"]["action"]=="add" and item["targets"] and not item["already_exists"]:
                        counts["added"] += 1
                    for effect in item["effects"]:
                        counts["archived" if effect["action"]=="archive" else "removed"] += 1
                        counts["unknown"] += int(effect["external"] and effect["had_file"])
                else:
                    counts["kept"] += int(not item["effects"])
                    for effect in item["effects"]:
                        key = {"add":"added","remove":"removed","rewrite":"rewritten"}[effect["action"]]
                        counts[key] += 1
                        counts["unknown"] += int(effect["external"] and effect["had_file"])
                        counts["relocated"] += int(effect.get("relocate", False))
                if len(samples)<30 and (item["effects"] or request["kind"]!="library"):
                    samples.append({"video_id":video_id,"slug":item["slug"],"title":item["title"],"reason":item["reason"],"effects":item["effects"]})
                # 普通源地址/健康数据不参与范围比较。
                if request["kind"]!="library" or item["effects"]:
                    fingerprint.update(digest([video_id,item]).encode())
                if request["kind"]!="library" or item["effects"]:
                    rows.append((job_id,video_id,json.dumps(item,ensure_ascii=False)))
            if save and rows:
                await self.db._write_many("INSERT INTO operation_items(preview_id,video_id,data) VALUES(?,?,?)",rows)
            if save:
                await self.db.update_job(job_id,state={"checked":counts["matched"]})
        if digest(baseline)!=digest(context(self.e,request)):
            raise ScopeChanged("计算期间配置发生变化，请重新预览")
        if request["kind"]=="retirement":
            subscriptions = await self.db._all("SELECT id,name,enabled FROM subscriptions WHERE site=? ORDER BY id",(request["payload"]["site"],),bulk=True)
            fingerprint.update(digest([dict(r) for r in subscriptions]).encode())
        result = {"counts":counts,"samples":samples,"warnings":["文件影响按数据库记录预计；外部工具移动的实际位置、共享目录和挂载状态在执行时由现有保护逻辑核对。"],"library_names":{str(k):v["name"] for k,v in planner.libs.items()}}
        if request["kind"]=="retirement":
            result["warnings"].append("保留项按本地启用/支持策略判断，尚未逐片验证播放。确认后会停用该站及其订阅，取消该站排队中的抓取。")
        if request["kind"]=="selection" and request["payload"].get("ids") is not None:
            counts["requested"] = len(set(request["payload"]["ids"]))
        if request["kind"]=="library":
            result["configuration"] = {"before":{k:planner.old[k] for k in planner.desired} if planner.old else None,"after":planner.desired}
            if planner.locate:
                result["warnings"].append("外部整理目录变化只同步记录位置；不会搬动或覆盖外部 NFO、图片。")
        return fingerprint.hexdigest(),result,baseline

    async def save_library(self,job,request,planner,state):
        body = request["payload"]["values"]
        keys = tuple(body)
        values = [json.dumps(body[k],ensure_ascii=False) if k in ("rule","sources","excludes") else body[k] for k in keys]
        # 库配置和执行断点一起提交，中断后不会把已保存配置误当成未确认的新变更。
        async with self.db._tx() as conn:
            lib_id = request["payload"].get("library_id")
            if lib_id:
                await conn.execute(f"UPDATE libraries SET {','.join(k+'=?' for k in keys)} WHERE id=?",(*values,lib_id))
            else:
                cur = await conn.execute(f"INSERT INTO libraries({','.join(keys)},created_at) VALUES({','.join('?' for _ in keys)},?)",(*values,int(time.time())))
                lib_id = cur.lastrowid
            affected = [lib_id if i==planner.root else i for i in planner.affected]
            await conn.execute(f"UPDATE libraries SET operation_job_id=?,operation_pending=1 WHERE id IN ({','.join('?' for _ in affected)})",(job["id"],*affected))
            after = copy.deepcopy(context(self.e,request))
            after["libraries"] = [lib for lib in after["libraries"] if lib["id"]!=lib_id] + [{"id":lib_id,**body}]
            after["libraries"].sort(key=lambda lib:lib["id"])
            state.update(configured=True,library_id=lib_id,_context_after=after)
            await conn.execute("UPDATE jobs SET state=? WHERE id=?",(json.dumps(state,ensure_ascii=False),job["id"]))
        await self.e.reload_libraries()
        return lib_id

    async def preview(self,job,task):
        request = job["params"]["operation"]
        scope,result,baseline = await self.calculate(request,job["id"],save=True)
        await self.db.update_job(job["id"],state={"checked":result["counts"]["matched"],"preview_token":secrets.token_hex(24),"_scope":scope,"_preview":result,"_context":baseline})
        return {"matched":result["counts"]["matched"]}

    async def approve(self,preview_id,token):
        async with self.db._tx(invalidate=False) as conn:
            preview = await self.db.get_job(preview_id)
            if not preview or preview["kind"]!="preview" or preview["status"]!="done" or preview["state"].get("partial_failure"):
                raise ValueError("预览尚未完成或失败，请重新预览")
            if not token or not secrets.compare_digest(token,str(preview["state"].get("preview_token",""))):
                raise ValueError("预览确认不匹配，请重新预览")
            if preview["state"].get("apply_job_id"):
                return preview["state"]["apply_job_id"]
            request = preview["params"]["operation"]
            selection = request["kind"]=="selection"
            kind = {"probe":"probe","refresh":"videos","add":"library_add","remove":"library_remove","remove_source":"source_remove","restore_source":"source_restore"}.get(request["payload"].get("action")) if selection else "operation_apply"
            params = {"preview_id":preview_id,"library_id":request["payload"].get("library_id"),"remove":request["payload"].get("action")=="remove","sites":request["payload"].get("sites")}
            cur = await conn.execute("INSERT INTO jobs(kind,name,params,status,created_at) VALUES(?,?,?,'running',?)",(kind,"处理已确认的影片范围" if selection else "执行已确认的变更",json.dumps(params),int(time.time())))
            job_id = cur.lastrowid
            await conn.execute("INSERT INTO tasks(job_id,kind,target,priority,updated_at) VALUES(?,?,'all',100,?)",(job_id,"selection_prepare" if selection else "apply_preview",int(time.time())))
            state = {**preview["state"],"apply_job_id":job_id}
            await conn.execute("UPDATE jobs SET state=? WHERE id=?",(json.dumps(state,ensure_ascii=False),preview_id))
        self.e.notify()
        return job_id

    async def selected(self,job,task):
        preview = await self.db.get_job(job["params"]["preview_id"])
        request,baseline = preview["params"]["operation"],preview["state"]["_context"]
        if digest(baseline)!=digest(context(self.e,request)):
            raise ScopeChanged("预览后的目标库或站点设置发生变化，请重新预览")
        cursor = int(job["state"].get("cursor",-1))
        counts = preview["state"]["_preview"]["counts"]
        while True:
            await self.e.checkpoint(job["id"])
            rows = await self.db._all("SELECT video_id,data FROM operation_items WHERE preview_id=? AND video_id>? ORDER BY video_id LIMIT 200",(preview["id"],cursor),bulk=True)
            if not rows:
                break
            # 存量补源属于后台维护，不抢在订阅列表和用户操作之前；播放仍走独立请求链路。
            from .engine import PRIORITY_DETAIL
            priority = PRIORITY_DETAIL if request["payload"]["action"] == "probe" else 100
            targets = [(job["id"],t["kind"],t["target"],priority,t["site"],int(time.time())) for row in rows for t in json.loads(row["data"])["targets"]]
            cursor = rows[-1]["video_id"]
            state = {"cursor":cursor,"matched":counts["matched"],"skipped":counts["skipped"],"already_exists":counts["already_exists"]}
            async with self.db._tx(invalidate=False) as conn:
                await conn.executemany("INSERT OR IGNORE INTO tasks(job_id,kind,target,priority,site,updated_at) VALUES(?,?,?,?,?,?)",targets)
                await conn.execute("UPDATE jobs SET state=? WHERE id=?",(json.dumps(state),job["id"]))
        return {"matched":counts["matched"],"inapplicable":counts["skipped"]}

    async def apply(self,job,task):
        from . import site_retirement
        preview = await self.db.get_job(job["params"]["preview_id"])
        request = preview["params"]["operation"]
        if request["kind"] == "dedupe":
            from .dedupe_apply import apply
            return await apply(self.e, job, preview)
        async with self.e.file_changes.exclusive():
            current = await self.db.get_job(job["id"])
            state = dict(current["state"])
            if state.get("completed"):
                return {"queued":int(state.get("matched",0))} if request["kind"]=="retirement" else {"processed":int(state.get("processed",0))}
            planner = Planner(self.e,request,preview["state"]["_context"])
            if request["kind"]=="retirement" and state.get("child_job_id"):
                baseline = copy.deepcopy(preview["state"]["_context"])
                site = request["payload"]["site"]
                baseline["sites"][site]["enabled"] = self.e.store.current.site(site).enabled
                if digest(baseline)!=digest(context(self.e,request)):
                    raise ScopeChanged("清退设置期间配置发生变化，请核对后重试")
                child = await site_retirement.apply(self.e,site,preview_id=preview["id"],parent_job_id=job["id"])
                await self.db.update_job(job["id"],state={**state,"completed":True,"child_job_id":child})
                return {"queued":int(state["matched"])}
            if not state.get("configured"):
                scope,_,_ = await self.calculate(request,job["id"])
                if scope!=preview["state"]["_scope"]:
                    raise ScopeChanged("预览后影片、归属或配置发生变化，尚未执行变更，请重新预览")
                await self.e.checkpoint(job["id"])
                if request["kind"]=="retirement":
                    matched = preview["state"]["_preview"]["counts"]["matched"]
                    child = await site_retirement.apply(self.e,request["payload"]["site"],preview_id=preview["id"],parent_job_id=job["id"])
                    await self.db.update_job(job["id"],state={"child_job_id":child,"matched":matched,"completed":True})
                    return {"queued":matched}
                await self.save_library(job,request,planner,state)
            if self.e._library(state["library_id"]).get("operation_job_id")!=job["id"]:
                raise ScopeChanged("该库已由新的确认任务接管，请查看最新任务")
            if digest(state["_context_after"])!=digest(context(self.e,request)):
                raise ScopeChanged("执行中配置再次变化，请重新预览剩余范围")
            planner.actual_root = state["library_id"]
            if planner.locate:
                await self.e.strm.locate(self.e._library(state["library_id"]))
            cursor = int(state.get("cursor",-1))
            while True:
                await self.e.checkpoint(job["id"])
                rows = await self.db._all("SELECT video_id,data FROM operation_items WHERE preview_id=? AND video_id>? ORDER BY video_id LIMIT 200",(preview["id"],cursor),bulk=True)
                if not rows:
                    break
                for row in rows:
                    await self.e.checkpoint(job["id"])
                    expected = json.loads(row["data"])
                    raw = await self.db._all("SELECT * FROM videos WHERE id=?",(row["video_id"],),bulk=True)
                    actual = await planner.enrich(raw)
                    if not actual or actual[0][1]["input"]!=expected["input"]:
                        raise ScopeChanged(f"影片 {expected['slug']} 的影响条件已改变；已完成部分保留，请重新预览剩余范围")
                    if not memberships_match(expected,actual[0][1],locate=planner.locate):
                        raise ScopeChanged(f"影片 {expected['slug']} 的归属已改变；未处理该影片，请重新预览剩余范围")
                    video = await self.db.get_video_by_id(row["video_id"])
                    # 先加入再移出：换到排除库的影片，移出旧库时新库已在等写出，整理好的目录能搬过去（见 engine._relocate）
                    for effect in sorted(expected["effects"], key=lambda effect: effect["action"] == "remove"):
                        lid = state["library_id"] if effect["library_id"]==planner.root else effect["library_id"]
                        if effect["action"]=="remove":
                            await self.e.remove_from_library([video["id"]],lid)
                        elif effect["action"]=="add":
                            await self.db.ensure_output(video["id"],lid,via=effect["via"])
                            await self.e._output_one(video,lid,cover=False)
                        else:
                            out = await self.db.get_output(video["id"],lid)
                            if out:
                                await self.e._output_one(video,lid,cover=False,old_strm=out["strm_path"],versions=True)
                    cursor = row["video_id"]
                    state.update(cursor=cursor,processed=int(state.get("processed",0))+1)
                    await self.db.update_job(job["id"],state=state)
            state["completed"] = True
            async with self.db._tx(invalidate=False) as conn:
                await conn.execute("UPDATE libraries SET operation_pending=0 WHERE operation_job_id=?",(job["id"],))
                await conn.execute("UPDATE jobs SET state=? WHERE id=?",(json.dumps(state,ensure_ascii=False),job["id"]))
            await self.e.reload_libraries()
            return {"processed":int(state.get("processed",0))}

    async def check_retirement_item(self,preview_id,video_id):
        preview = await self.db.get_job(preview_id)
        row = await self.db._one("SELECT data FROM operation_items WHERE preview_id=? AND video_id=?",(preview_id,video_id),bulk=True)
        if not preview or not row:
            raise ScopeChanged("清退预览记录不可用")
        if preview["params"]["operation"]["kind"]=="selection":
            return await self.check_selected_item(preview,row,video_id)
        sources = await self.db.get_sources(video_id)
        site = preview["params"]["operation"]["payload"]["site"]
        expected_context = copy.deepcopy(preview["state"]["_context"])
        expected_context["sites"][site]["enabled"] = False
        if digest(expected_context)!=digest(context(self.e,preview["params"]["operation"])):
            raise ScopeChanged("清退执行期间站点或库配置改变，请重新核对剩余范围")
        if all(s["status"]=="removed" for s in sources if s["site"]==site):
            return
        planner = Planner(self.e,preview["params"]["operation"])
        raw = await self.db._all("SELECT * FROM videos WHERE id=?",(video_id,),bulk=True)
        actual = await planner.enrich(raw)
        if not actual or actual[0][1]["input"]!=json.loads(row["data"])["input"]:
            raise ScopeChanged("清退对象的源或归属已改变，未处理该影片，请重新预览")

    async def check_selected_item(self,preview,row,video_id):
        expected = json.loads(row["data"])
        if not expected.get("input"):
            return
        request = preview["params"]["operation"]
        sources = await self.db.get_sources(video_id)
        action = request["payload"]["action"]
        if action=="remove" and not await self.db.get_output(video_id,request["payload"]["library_id"]):
            return
        if action=="restore_source" and not any(s["status"]=="removed" for s in sources):
            return
        if action=="remove_source" and all(s["status"]=="removed" for s in sources if s["site"] in request["payload"].get("sites",[])):
            return
        if digest(preview["state"]["_context"])!=digest(context(self.e,request)):
            raise ScopeChanged("预览后的库或站点设置改变，未处理该影片")
        planner = Planner(self.e,request)
        raw = await self.db._all("SELECT * FROM videos WHERE id=?",(video_id,),bulk=True)
        actual = await planner.enrich(raw)
        if not actual or actual[0][1]["input"]!=expected["input"]:
            raise ScopeChanged("影片源或归属已改变，请重新预览剩余范围")
