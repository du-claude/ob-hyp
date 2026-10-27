# 来源与裁剪

候选整理与安装验证：2026-10-27 周二。

OB 源码来自现役 VPS 的三个运行目录，读取方式为只读 SSH 文件回读。三个目录的 Git HEAD 均为 `06433c2adb5161a8b1e3799d4e2e380ccfe7e5ea`；文件存在不同的运行期修改，不能以 HEAD 代替文件哈希。交付外层 REPORT.md 记录原始目录、回读证据和完整哈希，那个报告包含私有部署位置，不属于可公开仓库。

实际纳入的 OB 模块取自第一个个人房间：

| 源文件 | 回读 SHA-256 |
|---|---|
| bucket_manager.py | `69a32ee54c126deb02f8152706cd829c2feadbae5d86cf1a0afd96bd306d1bd3` |
| utils.py | `31295af53646d6b48a17fef8390fe2c1d9e5ab9f5a02cb4a8f4a45c03d65976f` |
| lexical_engine.py | `d77bb09fc940ab8692b2974e99a9e90282d8879fe4899d920de9c274eefa716e` |
| hq.py | `e24b0ee9875b19f45d9c5a8f3d7617e5c29724779d7a9a10d4e1f860ee22cecd` |

OB 仅纳入 bucket_manager.py、utils.py、lexical_engine.py；去除历史注释/文档中的家庭材料，内部模块引用改为包内导入。utils 的旧配置加载器删除，合法受众默认置空，由本地配置注册；路径检查改为真正的目录包含关系。桶核心层原子写、状态/注意力轴、原文元数据、字符匹配与 BM25 融合保留；作者私密过滤改读 private_author，审计默认署名改为 local。

Hyp 提取自 hq.py 的词典读取/合并、分词、别名保护、FTS 查询转义、BM25、节点归一化与节点派生函数；没有复制家庭词库和主程序。原始函数逐一清单见交付外层 evidence/extraction.json。候选新增 JSONL 导入器、不可变 ref 冲突检查、事务索引重建和 CLI/MCP 适配。仅索引显式 document/raw 来源，不套用家庭来源分类。

新增 runtime.py、hyp.py、cli.py、mcp_server.py 为此次候选适配；生产 server.py 没有进入仓库。这是代码提取后的本地版本，不宣称生产工具和生产行为全部可用。

对照设计保留了「整理桶与原文分层、原文不被模型改写、派生索引可重建、来源与作者显式」这几条。设计中的自动带入、图扩散、校准、潜意识便签、完整人格层和家庭拓扑没有在此候选实现。

OB 上游 MIT 条款保留于 NOTICE-UPSTREAM.txt；Hyp 与新增适配代码的授权范围待维护者确定，顶层 LICENSE 按要求留空。
