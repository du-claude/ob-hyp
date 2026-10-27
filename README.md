# OB + Hyp 本地初版候选

这是可独立安装的**裁剪候选**，适合先在新目录试用。OB 管理整理后的 Markdown 记忆桶；Hyp 保存逐字原文并生成词面索引。所有示例均为虚构。Python 3.11+，目前实际验证环境为 macOS / Python 3.12；Windows 未核。

本包提取现役 OB 的桶管理、状态/注意力轴与混合词面检索，以及 Hyp 的分词、别名、节点和 BM25 核心。入口重新适配为本地单使用者 CLI / stdio MCP。**不兼容生产版全部工具参数，也不是旧版 OB 的原位升级包。** 不要覆盖已有服务或把旧数据库直接放进来。来源、改动与范围见 [SOURCE.md](SOURCE.md)；升级见 [UPGRADE-NOTES.md](UPGRADE-NOTES.md)。

## 安装与最小示例

在解压后的仓库目录运行。需要 Python 与 uv；也可使用后面的 pip 方式。运行过程中仅安装公开依赖，不调用模型。

```sh
uv venv .venv
uv pip install --python .venv/bin/python '.[mcp]'
cp config.toml.example config.toml
.venv/bin/ob-hyp ob-write '虚构示例：下次去天文台看木星。' --name '虚构观星计划'
.venv/bin/ob-hyp ob-search '天文台'
.venv/bin/ob-hyp hyp-import examples/documents.jsonl
.venv/bin/ob-hyp hyp-query '望远镜'
```

预期：写桶返回随机 bucket_id；检索返回「虚构观星计划」正文；导入返回 imported=2、total=2；Hyp 查询返回 ref=demo:observatory 的完整原文。再次导入同一文件 imported=0；已有 ref 的内容变化会拒绝并回滚该次导入。

不用 uv 时：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install '.[mcp]'
```

如果只要 CLI，用 `.` 替代 `.[mcp]`。Windows 的解释器和入口在 `.venv/Scripts/`，未实测。

所有路径相对 config.toml 所在目录；`--config /你自己的路径/config.toml` 可放在命令之前。配置不会读取旧机器的用户目录、SSH、环境凭据或已有记忆库。`data_dir` 下生成 buckets/（Markdown + YAML）与 hyp.db（SQLite FTS5）。SQLite 必须支持 FTS5 和 trigram；不满足时命令直接报错，不降级伪装成功。

## 配置与原文格式

- `data_dir`：独立数据目录，一个目录只供一个使用者；旧库先备份，再在副本上尝试。
- `actor`：本地写入署名，固定来自配置，MCP 调用方不能自报替换；已有其他作者桶不会被本入口检索返回。
- `audiences`：合法主动浮现路由标签；保留底层校验，当前入口不提供自动浮现。它不是访问控制。
- `private_author`：该作者的 feel 被底层普通搜索排除。
- `dictionary_files`：可选词典路径数组，默认空；格式见 examples/dictionary.txt.example。没有携带个人词库。
- `aliases_file`：可选别名 JSON 路径，格式 `{ "实体规范名": ["别名"] }`；修改后重导原文以重建索引。
- `matching`、`hybrid_search`：桶检索参数，模板启用 BM25 与字符匹配融合，不启用语义向量。
- 底层仍识别 `OMBRE_ALWAYS_MAX`（默认 10）和 `OMBRE_AUDIENCE_ENFORCE`（on/off，未设为 off 兼容并告警），只影响桶核心层校验。CLI 当前只创建普通桶。

Hyp 的 UTF-8 JSONL 每行是一个对象，仅允许 `ref`、`text`、`actor`、`ts`。ref/text 必填非空；actor 可省略并采用配置值，显式给出则必须与配置一致。ts 可省略，提供时建议 ISO 8601 带时区。输入正文原样保存，不读取聊天软件、浏览器、云盘、其他数据库。索引与原文在一次事务提交；修改词典后可再次导入同一文件，原文不变而索引重建。数据量大时当前全量重建方式会慢，尚无大库性能验收。

## 接入 MCP

先安装 `.[mcp]`，客户端以绝对路径启动：

```json
{
  "mcpServers": {
    "local-memory": {
      "command": "/your/repository/.venv/bin/ob-hyp",
      "args": ["--config", "/your/repository/config.toml", "mcp"]
    }
  }
}
```

仅 stdio，不监听 HTTP。三个工具：`hold(content,name)` 原样写桶、`breath(query,max_results)` 显式检索、`hyp_query(query,max_results)` 查原文。导入在命令行完成。没有 grow/trace/dream/pulse、自动浮现、feel 写入口、暗房、共享房间、网页、定时采集、云备份或在线鉴权。原生产入口的自动打标/压缩/合并均未接入，hold 不会替你做摘要。无需 API 密钥或外部模型。

## 验证、备份与边界

```sh
.venv/bin/python examples/check.py
```

脚本只在仓库下新建临时假数据目录，结束自动删除；验证写入/检索、重复导入、冲突拒绝、回滚与作者边界。安装和命令的实跑记录位于候选交付外层的 TEST-RESULTS.md。

停止本地写入后复制整个 data_dir 和 config.toml 即可备份；Hyp 的 spans 是原文，索引可重建，不能把整个 hyp.db 当缓存随意删。当前没有导出/恢复命令，备份恢复需在独立目录手工核验。桶中的 actor 与 Hyp 的 actor 只是署名，本地文件权限与独立目录才是隔离边界；不要把本包当多用户网络服务，也不要混入共享库。多个写进程操作同一 OB 目录没有经过并发验收。

LICENSE 按委托留空，整包许可待维护者确定。OB 上游版权和 MIT 条款保留在 NOTICE-UPSTREAM.txt。此交付是待审候选，尚未获得整包开源许可证。
