# 幕间事实仓

演出票务的分层入仓与一致消费服务。它接收多个出票来源的销售和退款，以及场馆验票资料，将原始批次、场次维度、出票权益、资金分录和场次分区关联起来。错误资料会保留来源并进入候选隔离；消费者只读取完整且通过业务检查的已发布版本。

Python 3.11及以上，FastAPI、Uvicorn和HTTP测试依赖的精确版本见 requirements.txt。依赖安装完成后，所有操作使用本地资料，不需要第三方票务账号或外网。SQLite数据库是本机可信数据文件，服务默认只绑定127.0.0.1，没有公网身份认证或租户隔离功能。

```bash
python -m unittest discover -s tests -v
python -m compileall -q stagehouse
python acceptance.py
python -m stagehouse --database stagehouse.sqlite3 serve --port 8014
```

FastAPI交互式接口说明位于服务的 `/docs`。`acceptance.py` 使用真实ASGI请求，走完整的原始导入、候选构建、版本发布、更正退款、历史重放和消费确认，并核对分区导出与备份恢复。`examples/` 为本地合成场馆、场次、票务及退款数据，不代表任何真实院团或观众。

`POST /sources` 登记来源；`POST /catalog` 写入整包维度，记录形如 `{"records":[{"kind":"venue","data":{...}}]}`。支持venue、session、alias和channel，实际字段可见examples/catalog.json。来源外部场次通过alias映射到统一场次；同一来源外部编号只能对应一个场次，每个出票来源至多一个结算渠道。维度修订连续递增，整包引用通过后一起生效，历史维度版本保留。

`POST /batches` 导入原始批次，字段为id、source、first_offset、last_offset、events。事件字段为id、revision、offset、kind、data。kind支持sale、refund和scan，具体字段见合成资料。来源位点从1连续递增，批次必须完整覆盖声明区间；事件修订同样从1连续递增，不能改变事实类型。同一批次编号绑定原始JSON内容，完全相同的重送不改变游标。原始资料先落地，业务引用尚未到达不会被静默丢弃，而会在后续候选中隔离。

sale以出票来源加票号定义票权益，session_ref通过该来源映射。refund和scan明确携带ticket_source与ticket，避免不同渠道相同票号误合并。退款允许来自另一登记来源，但资料账受信，不代表授权系统。当前每个票权益对应一张票和一个场次，不支持联票、转售身份继承或全量删除语义。

金额均为人民币整数分，不能输入浮点金额。有效退款组不能早于销售，累计不能超过售价；组内任一退款不合法时该票整组退款暂不入事实，保留隔离原因，候选不能发布。渠道佣金按净额及基点四舍五入到分；分次退款按剩余应计佣金差额冲回，避免每笔独立舍入导致全退仍留佣金。场馆应收与渠道佣金之和等于净额，资金分录与票级事实也守恒。

验票必须有有效销售、映射到同一场次且处于允许入场时段，取消场次不能形成有效入场。有效入场每票一次，重复扫描进入问题记录；在扫描之前已全额退票会使扫描无效，演出之后退款不会抹去早先真实入场。免费票仍有入场权益。业务日期取场次开始时刻在场馆时区中的日期，跨日退款仍进入原演出场次分区；该仓库不提供银行结算日或跨币种账。

`POST /runs` 根据id建立固定原始水位与维度版本的候选，结果包含权益、验票、结算、资金分录、分区摘要、隔离问题及血缘。`/runs/{id}/profile` 查看来源与分区质量；`POST /runs/{id}/issues` 筛选问题；`POST /runs/{id}/replay` 按原输入验证可复现。维度补齐或原始更正后建立新的候选，不覆盖旧候选。`/run-comparison` 比较问题解决与分区变化，`/dimension-impact` 区分维度变化和原始水位变化。

`POST /releases` 提交id、run、run_digest和expected_head。存在隔离问题、没有原始事实、候选过期或消费前序不匹配时拒绝发布。成功时所有事实分区以同一个版本切换，不让读者看到部分新版。发布请求可重送，但同编号不能改变候选、摘要或前序。历史版本的内容和摘要不随新资料变化。

`POST /query` 指定release和table读取事实；release为null时读取请求开始时的当前版本，返回值包含解析后的具体版本。分页续读应固定该版本，避免跟随head跨版本。支持tickets、admissions、settlements、journal、lineage；业务维度筛选以各表具有的字段为准。`/manifest` 返回分区摘要、来源向量和事实总量；`/tickets/{source}/{ticket}/trace` 追溯已发布票权益、资金和入场；`/compare` 对比两个发布版本。

消费方通过 `/subscriptions` 登记订阅，`/deliveries` 为目标已发布版本生成包含分区替换和移除的包。生成包不推进消费位置；实际应用后向 `/acknowledgements` 确认编号、摘要和代次才推进。两个并行预览不能互相覆盖已确认位置，确认重试不重复推进。这里证明的是本地仓库的投递与确认协议，不会替消费方执行其外部数据写入。

命令行也可source、catalog、ingest、prepare、run、query、publish、manifest、profile、replay、audit、export、export-package、verify-package、backup和verify；使用 `python -m stagehouse --help` 查看参数。验票CSV和逐行JSON通过ingest的format选择，整数和时间仍走相同校验。导出的分区包含manifest.json、release.json和确定编号的分区文件，独立校验检查摘要、行数、覆盖与资金守恒；摘要仅用于完整性检查，不是数字签名。

目录按职责组织：intake保留异构来源及游标，reference解析场次和渠道，facts判定权益与履约，finance解释资金和冲回，warehouse负责候选契约、分区、历史版本和消费协议；service绑定事务，api提供HTTP。`backup`拒绝覆盖现有文件，恢复时可直接将备份路径作为database重新打开，原始历史、版本与消费位置都保留。
