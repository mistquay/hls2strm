# 番号别名、同片整理与版本起播备用：实现与验证

对应方案：`code-alias-dedupe-and-edition-fallback.md`。本次只做仓库实现和离线验证，没有部署或执行生产整理。

## 完成顺序

- [x] P0：v21 只读审计，终端报告和 CSV；不迁移、不扫描媒体文件。
- [x] P1：v2 匹配键、原生无码版本规整、候选写法、数字前缀学习、SupJav 日期番号修复。
- [x] P2：同片整理预览、选保留项、跳过/确认冲突、文件清理、事务合并、断点恢复和旧 slug 兼容。
- [x] P3：默认关闭的版本起播备用、方向限制、请求/供源身份记录、会话与前端展示。

## 入口和迁移

- 审计：`python -m hls2strm.code_audit --db <数据库副本> [--out groups.csv]`，详见 `code-audit.md`。
- 整理：影片库页“同片整理” → 计算影响预览 → 按需调整保留项、跳过组、确认冲突 → 重新预览 → 确认执行。
- 备用：设置页“选源偏好”中的“版本起播备用”，默认关闭，支持双向和两个单向。
- v22：只重算匹配键，清掉键变化影片的负查找记录；不合并、不改 slug/存量版本标记、不动文件。
- v23：增加 `video_aliases` 表及 `video_id` 索引。
- v24：播放记录增加 `served_video_id`；旧记录用原 `video_id` 回填。

## 兼容与实现细节

- 普通版和 -U 版仍独立；`@` 仍只表示画质。版本文件只使用本影片源的画质，备用设置不触发输出重写。
- P2 删除只调用既有 `_remove_files`，由 `writer.remove`、`trash.retire` 完成文件处理。先清并入者文件、删除其输出记录，再事务合并、写别名。
- 清理开始前持久化待清理输出；合并和 `merged` 断点在同一事务提交。任务重试能继续清理，或在合并后继续输出，不会拿新别名回头清保留者的文件。
- 保留者输出使用 `_output_all`（覆盖已有与新加入的输出，内部逐项调用 `_output_one`），随后 `apply_rules` 并设置归并标记；不重复调用 `_output_one` 写同一条输出。
- 外部库的多画质附件仍在整理后才补。清理和补写均遵循原来的 slug 附件归属规则。
- 新旧 slug 都可查询和播放；按旧 slug 搜索影片库也会返回保留者。定位有多份文件时优先现行 slug，再保留原有外部目录优先规则。
- 备用沿用本版优先、字幕档、指定站点、直连限制、画质、排除候选和总时间预算；只有本版继续现场找源。源健康记录各记各的，removed 源不会恢复。
- 中转会话持久化请求影片与实际影片；会话刷新仍固定源/线路。网页起播后的恢复请求使用已播放版本，并带 `edition_only=1` 禁止再次跨版本续播。
- 归档文件保留；共享目录的其他元数据和未找到的输出在预览及日志中列出。

## 与方案的差异和明确口径

1. **已由用户确认：抓取明细主键冲突。** `crawl_items` 的主键为 `(job_id, video_id)`。同任务里保留者和并入者都有明细时，保留保留者的 slug/title 等文本；`is_new`、`added` 取较大值，`excluded` 取较小值（任一记录未排除就不算全被排除），删除另一条重复明细并记日志。没有冲突的行只改 `video_id`，slug 不变。
2. **P0 路径信息不足。** 相对外部目录在数据库里没有可确定的输出根目录时标记“无法判断”，不猜服务器环境变量，也不用本地副本目录替代服务器路径。
3. **冲突百分比基准。** 方案未指定 10% 的分母，实现按两部片中较长的时长计算；P0、P2 共用该判定。
4. **历史迁移算法固定。** v6/v16 继续调用旧键算法；v22 单独切换 v2，保证从旧版本完整升级时负查找记录仍能正确清理。
5. **既有网页行为衔接。** 新增精确旧 slug 搜索兼容，以及起播后固定版本的恢复参数，避免已有网页跳转/自动换源绕过别名和版本边界。

## 文件清单

| 范围 | 文件 |
| --- | --- |
| 只读审计、匹配规则 | `hls2strm/code_audit.py`、`hls2strm/codes.py` |
| 数据库、入库、旧链接 | `hls2strm/db.py`、`hls2strm/engine.py`、`hls2strm/queries.py` |
| 跨站查找 | `hls2strm/sites/__init__.py`、`hls2strm/sites/base.py`、`hls2strm/sites/missav.py`、`hls2strm/sites/supjav.py` |
| 整理及操作框架 | `hls2strm/dedupe.py`、`hls2strm/dedupe_apply.py`、`hls2strm/operations.py`、`hls2strm/operation_plans.py`、`hls2strm/operation_api.py`、`hls2strm/strm_manage.py` |
| 备用、记录与详情 | `hls2strm/config.py`、`hls2strm/play.py`、`hls2strm/playback.py`、`hls2strm/play_history.py`、`hls2strm/api.py` |
| 前端 | `web/src/components/dedupe-preview.tsx`、`web/src/components/operation-preview.tsx`、`web/src/components/use-playback-source.ts`、`web/src/components/video-detail.tsx`、`web/src/pages/videos.tsx`、`web/src/pages/settings.tsx`、`web/src/pages/play-history.tsx`、`web/src/lib/api.ts` |
| 测试 | `tests/test_code_audit.py`、`tests/test_code_identity.py`、`tests/test_dedupe.py`、`tests/test_edition_fallback.py`、`tests/test_multisite.py`、`tests/test_supjav.py` |
| 说明 | `docs/code-audit.md`、本文 |

用户原有的未提交改动均保留。上述 `web/src/lib/api.ts` 已有其他任务的改动，本任务只增加 `Video.siblings` 类型字段。

## 验证

使用仓库 `.venv` 的 Python，系统默认 Python 未安装 pytest。

| 阶段 | 全量 pytest | 前端构建 |
| --- | --- | --- |
| P0 | 323 passed、2 skipped | 无前端改动 |
| P1 | 333 passed、2 skipped | 无前端改动 |
| P2 | 345 passed、2 skipped | `npm run build` 通过 |
| P3 初次验收 | 365 passed、2 skipped | `npm run build` 通过 |
| 审查修复后 | 415 passed、2 skipped | `npm run build` 通过 |
| 复审修复后 | 421 passed、2 skipped | `npm run build` 通过 |

最终命令：`python -m pytest -q --tb=short --show-capture=no`（选择 `.venv` 解释器）；`cd web && npm run build`。
两项跳过来自本机软链接能力限制；10 条警告为已有 Starlette/httpx 弃用和 Windows curl 事件循环警告。
中途原有站点清退测试出现过一次未复现失败；其单独测试文件及随后两次全量测试通过，没有修改其清退逻辑，失败原因未确认。

测试覆盖 v21 审计/WAL、v22 键迁移、同版入库、原生无码、离线站点查找、三种文件位置清理、附件保护、别名/API/画质参数、数据合并、换库、冲突选项、文件移动和中断恢复，以及备用方向/本版优先/预检/字幕/直连/指定源/排除/会话恢复/播放记录。
前端完成类型检查和生产构建，未进行浏览器人工点击验收；未访问真实站点、生产数据库、生产服务器、Emby 或 MDCNG。
未创建 Git 提交或推送。

## 审查修复

本轮修复用户提出的 7 项问题，新增 50 个回归用例；专项测试 92 项通过。

1. 下划线裸日期按片商补厂牌时，只匹配使用下划线的厂牌，排除 CARIB；片商名先去空白。日文与英文带空格的 Premium 名称均匹配 CARIBPR。
2. 增加对称的 `compatible(a, b)`，仅允许相同键、或裸日期与同日期下划线厂牌键兼容；两个已知的不同厂牌仍不兼容。搜索与详情核对共用该规则；入库优先精确匹配，无精确项且兼容作品唯一才挂接。
3. 原生无码的精确匹配不再比较存量 `uncensored`，旧版本标记不在入库时修改。
4. 整理的身份快照仅包含影片 id/code_key/uncensored/status、源 id/site/key/status 和输出。文件清理前校验，合并事务内复核身份，不因标题、时长等详情刷新失败；补元数据使用最新记录。某组变化时跳过并记录 `skipped`、`skipped_groups` 及日志，继续后续组。执行前仍校验已确认预览自身的指纹和全局配置，不再重算整批保留项。
5. v22 与 `_update_work` 重算键时按 `code or slug` 兜底，避免无显示番号的影片丢失匹配键。
6. 中转播放清单需要重新解析时，从原请求影片的 slug 开始，并保留实际借用时的 `requested`；重新选回请求影片自身时不误报版本备用。画质参数和已有的版本锁定限制继续传递。
7. 整理预估与实际合并共用空值规则：duration/views/likes/favs/detail_at 的 0 可由有效值补齐，其他已有值不覆盖。

本轮代码文件：`hls2strm/codes.py`、`hls2strm/db.py`、`hls2strm/sites/__init__.py`、`hls2strm/dedupe.py`、`hls2strm/dedupe_apply.py`、`hls2strm/play.py`。
回归测试：`tests/test_code_identity.py`、`tests/test_dedupe.py`、`tests/test_edition_fallback.py`。
没有前端代码变更。验证后等待用户确认，不提交、不推送。

## 复审修复

1. 裸日期与带厂牌键的兼容改为单向：`compatible(found, wanted)` 只认“找到的没写厂牌、要找的是同日期下划线厂牌”。
   双向时，没写厂牌的源所在站点优先级最高，作品键一直是裸日期，同日期同序号的一本道、パコパコママ等不同的片会全挂到这一部上。
   现在入库时带厂牌的源不挂到裸日期作品上（查不到精确匹配就新建）；裸日期作品按番号查找时只收同样没写厂牌的结果。
   没写厂牌的源仍可挂到唯一的带厂牌作品上；裸日期作品等详情补上片商、键变成带厂牌的以后，由同片整理合并。
2. 整理执行开始清文件以后，只核对组内影片是否还在、番号和版本是否没变；源、输出、状态之后的变化不再让组停在“文件已清、库没合并”的半截。
   仍因番号或版本变化跳过的，跳过原因注明“并入影片的文件已清理，重新预览后可完成合并”。
3. 执行结果返回 `merged_groups`、`skipped_groups`；任务详情页显示已整理组数、跳过组数，并列出跳过的组、番号和原因。

本轮代码文件：`hls2strm/codes.py`、`hls2strm/db.py`（注释）、`hls2strm/dedupe_apply.py`、`web/src/pages/jobs.tsx`。
回归测试：`tests/test_code_identity.py`、`tests/test_dedupe.py`。
