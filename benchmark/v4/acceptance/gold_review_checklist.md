# Gold 人工复核清单

snapshot: `cma-v4-658dd583ab18a47f`；任务数：10。

每个任务必须由至少一名 reviewer 独立核对。自动指标不能替代以下判断。

## v4-single-source-01 — single_source (easy)

**问题**：ingest-gateway 的写入放大根因是什么？请引用日志。


### 必要证据组

- `log_error`
  - `ev_7f106b51b6255f3855a0891dc63607994eeed510b7c80afbf253a9bc7ac1c5eb@aa309c3087cc1e2abe29d6d912144aac4ee2eb324d76c2f54419af54209b068e` source=log event=2025-01-16T17:00:00+00:00 — 日志 log-atlas-00 ERROR ingest-gateway 写入放大：batch_size=500，请求失败率上升。事件时间 2025-01-16T17:00:00+00:00。
- `issue_triage`
  - `ev_eae5c627e9e49311d2734bc744988712d6c7893071aecf70fe1cf3e86e2f2419@6bd156d03e9c837d6bcf8fcdea37a79593400ff46b508dc58b82b589fe01350d` source=issue event=2025-01-14T23:00:00+00:00 — Issue issue-ATLAS-1400 评审记录：根因是上游批次过小，固定开销占比过高；建议把 batch_size 调整到 2000。评审人 赵岚。

### 结构化事实

- [必选] `component` 组件名称 values=['ingest-gateway'] markers=None
- [必选] `log_id` 日志记录标识 values=['log-atlas-00'] markers=None
- [必选] `root_cause` 根因解释 values=None markers=[['上游批次过小，固', '上游批次过小，固定开销占比过高']]
- [可选] `issue_id` 可选关联 Issue values=['issue-ATLAS-1400'] markers=None

### 拒答/不足

```json
{
  "required": false,
  "expected_termination": [
    "sufficient"
  ],
  "must_disclose": []
}
```

### 人工复核

- [ ] Every required fact is semantically supported by the cited Evidence, not merely present as a string.
- [ ] Every accepted alternative Evidence is genuinely equivalent for this question.
- [ ] No forbidden inference appears, especially proposal-as-confirmed or unilateral conflict adjudication.
- [ ] Each citation resolves to the current content hash and the quoted claim appears in that Evidence.
- [ ] Abstention/limitation text is appropriate for the task and does not hide a wrong answer.
- [ ] Tool-failure and budget tasks disclose interruption rather than claiming completion.
- [ ] Check that required facts are supported by cited Evidence, not merely present as strings.
- [ ] Check that alternative Evidence is genuinely equivalent for this question.
- [ ] Do not mark task_success from deterministic metrics alone.

## v4-cross-source-01 — cross_source (medium)

**问题**：query-router 为什么采用 5？讨论理由与哪个提交相互印证？


### 必要证据组

- `chat_proposal`
  - `ev_3cae8d55e82fc3884921267f936dedb9b864962b0b70adf7151c3e49a389bceb@6f0274bb39eb860b9f2c9581218fadccb19506053d57f07300cfec0dd02f10ec` source=chat event=2025-01-19T19:00:00+00:00 — #atlas-obs 2025-01-19 19:00 林澄: 我建议把 query-router 的 shard_timeout_seconds 从 2 调到 5；初步判断根因是跨分片查询默认超时过短。
- `commit_fix`
  - `ev_eed0c6fb65c18e75d60c35bd92af131f0c371b76db3fbeae0c7f718750fbce1d@3f3ca2b8b35cdb102ad88b5c451210198ab0d0cdf461c5b0b0c7344cd452bdfe` source=git_commit event=2025-01-26T19:00:00+00:00 — commit a3ff87577b27f7f49ce2ce9dd340c3dc7d41075b
作者 林澄
query-router: 将 shard_timeout_seconds 调整为 5（Fixes issue-ATLAS-1437）
事件时间 2025-01-26T19:00:00+00:00
关联 Issue: issue-ATLAS-1437

### 结构化事实

- [必选] `metric_value` 配置值 values=['5'] markers=None
- [必选] `commit` 修复提交 values=['a3ff87577b27f7f49ce2ce9dd340c3dc7d41075b', 'a3ff8757'] markers=None
- [必选] `reason` 采用理由 values=None markers=[['跨分片查询默认超']]
- [可选] `meeting_id` 可选会议记录 values=['meeting-atlas-01'] markers=None

### 拒答/不足

```json
{
  "required": false,
  "expected_termination": [
    "sufficient"
  ],
  "must_disclose": []
}
```

### 人工复核

- [ ] Every required fact is semantically supported by the cited Evidence, not merely present as a string.
- [ ] Every accepted alternative Evidence is genuinely equivalent for this question.
- [ ] No forbidden inference appears, especially proposal-as-confirmed or unilateral conflict adjudication.
- [ ] Each citation resolves to the current content hash and the quoted claim appears in that Evidence.
- [ ] Abstention/limitation text is appropriate for the task and does not hide a wrong answer.
- [ ] Tool-failure and budget tasks disclose interruption rather than claiming completion.
- [ ] Check that required facts are supported by cited Evidence, not merely present as strings.
- [ ] Check that alternative Evidence is genuinely equivalent for this question.
- [ ] Do not mark task_success from deterministic metrics alone.

## v4-multi-step-01 — multi_step (medium)

**问题**：从 Issue issue-ATLAS-1474 追查 retention-worker 的修复提交与验证日志。


### 必要证据组

- `issue_fix`
  - `ev_4acf6fad8299112ca7d0ebf5ca0265851e51865456718f9ad2ed936c86d23398@7497654e8d721761d99c6c80a713afa88d0c5f8037ca761f628627efb3fa6c40` source=issue event=2025-02-04T01:00:00+00:00 — Issue issue-ATLAS-1474 修复验证：提交 c7ba02365bb4a02405920baa5d1a190197184b1c 之后 retention_days=14，验证通过。关联提交 c7ba02365bb4a02405920baa5d1a190197184b1c。
- `commit_fix`
  - `ev_803704739ca8b242737a95597c21b8ff2d6fb6df90cba61f04a3686c60e1af28@a34f4094eee262d0e74cbaadf7f71f5db8c956c46ae8c42a7cc6964d625caafd` source=git_commit event=2025-02-04T19:00:00+00:00 — commit c7ba02365bb4a02405920baa5d1a190197184b1c
作者 林澄
retention-worker: 将 retention_days 调整为 14（Fixes issue-ATLAS-1474）
事件时间 2025-02-04T19:00:00+00:00
关联 Issue: issue-ATLAS-1474
- `log_recovery`
  - `ev_50112ada176e77caca4f8c32bc043af632c31a4e586de55b6d5799707ce56093@c2c529c4023429e0848609cd0c507e3ed5874ffcfaa03e9b1161d245230e3c29` source=log event=2025-02-05T17:00:00+00:00 — 日志 log-atlas-02 INFO 调整后 retention_days=14，retention-worker 服务恢复；关联提交 c7ba02365bb4a02405920baa5d1a190197184b1c。

### 结构化事实

- [必选] `issue_id` Issue 标识 values=['issue-ATLAS-1474'] markers=None
- [必选] `commit` 修复提交 values=['c7ba02365bb4a02405920baa5d1a190197184b1c', 'c7ba0236'] markers=None
- [必选] `metric_value` 验证值 values=['14'] markers=None
- [可选] `log_id` 可选恢复日志 values=['log-atlas-02'] markers=None

### 拒答/不足

```json
{
  "required": false,
  "expected_termination": [
    "sufficient"
  ],
  "must_disclose": []
}
```

### 人工复核

- [ ] Every required fact is semantically supported by the cited Evidence, not merely present as a string.
- [ ] Every accepted alternative Evidence is genuinely equivalent for this question.
- [ ] No forbidden inference appears, especially proposal-as-confirmed or unilateral conflict adjudication.
- [ ] Each citation resolves to the current content hash and the quoted claim appears in that Evidence.
- [ ] Abstention/limitation text is appropriate for the task and does not hide a wrong answer.
- [ ] Tool-failure and budget tasks disclose interruption rather than claiming completion.
- [ ] Check that required facts are supported by cited Evidence, not merely present as strings.
- [ ] Check that alternative Evidence is genuinely equivalent for this question.
- [ ] Do not mark task_success from deterministic metrics alone.

## v4-decision-history-01 — decision_history (medium)

**问题**：截至 2025-01-18，系统已确认的 写入批次策略 是什么？当时是否已采用 2000？

**known_at 截止时点**：`2025-01-18T03:00:00+00:00`

### 必要证据组

- `doc_v2`
  - `ev_472b355fc537d7628481fd7d534ab638d166b0c7491d85513f8f29d8096e3833@d0f67f6d290cea5d4625a28fd663279b94b387d4ece39038015ba7e7c3a43e82` source=document event=2025-01-17T18:00:00+00:00 — 设计文档 doc-atlas-00 v2（已评审）：ingest-gateway 将 batch_size 从 500 调整为 2000。理由：上游批次过小，固定开销占比过高。评审人 赵岚；该文档是原始证据，不等同于系统确认版本。
- `issue_triage`
  - `ev_eae5c627e9e49311d2734bc744988712d6c7893071aecf70fe1cf3e86e2f2419@6bd156d03e9c837d6bcf8fcdea37a79593400ff46b508dc58b82b589fe01350d` source=issue event=2025-01-14T23:00:00+00:00 — Issue issue-ATLAS-1400 评审记录：根因是上游批次过小，固定开销占比过高；建议把 batch_size 调整到 2000。评审人 赵岚。

### 结构化事实

- [必选] `historical_value` 截止时点已确认值 values=['500', 'dv_ec2e09cb916cd38a1fb2ae90aa77e9e7'] markers=None
- [必选] `not_yet_new` 截止时点尚未确认新值 values=['尚未采用 2000', '未确认 2000', '未确认'] markers=None
- [可选] `reviewer` 可选历史确认人 values=['赵岚'] markers=None

### 禁止推断

- 不得出现：已确认 2000 / 2000 已正式确认 / 已正式采用 2000

### 决策状态

```json
{
  "decision_id": "dec_0524241a5a22c99e5d397f4f27611cf9",
  "known_at": "2025-01-18T03:00:00+00:00",
  "expected_current_version_id": "dv_ec2e09cb916cd38a1fb2ae90aa77e9e7",
  "expected_version_ids": [
    "dv_ec2e09cb916cd38a1fb2ae90aa77e9e7",
    "dv_1262a0169fbb8b2f9ec3197420180560"
  ],
  "must_not_claim_confirmed": [
    "dv_1262a0169fbb8b2f9ec3197420180560"
  ]
}
```

### 拒答/不足

```json
{
  "required": false,
  "expected_termination": [
    "sufficient"
  ],
  "must_disclose": []
}
```

### 人工复核

- [ ] Every required fact is semantically supported by the cited Evidence, not merely present as a string.
- [ ] Every accepted alternative Evidence is genuinely equivalent for this question.
- [ ] No forbidden inference appears, especially proposal-as-confirmed or unilateral conflict adjudication.
- [ ] Each citation resolves to the current content hash and the quoted claim appears in that Evidence.
- [ ] Abstention/limitation text is appropriate for the task and does not hide a wrong answer.
- [ ] Tool-failure and budget tasks disclose interruption rather than claiming completion.
- [ ] Check that required facts are supported by cited Evidence, not merely present as strings.
- [ ] Check that alternative Evidence is genuinely equivalent for this question.
- [ ] Do not mark task_success from deterministic metrics alone.

## v4-conflict-01 — conflict (medium)

**问题**：query-router 在 prod 的 shard_timeout_seconds 到底是 5 还是 2？两份记录是否冲突？


### 必要证据组

- `chat_conflict`
  - `ev_4920530eb1f4540c4e758f5a5fdf02b2f2f552783d040c002244195c63cad3ee@83ecce4296de02dd985fd37ec20387b65611aa13f5b020451faad7ead22e8e39` source=chat event=2025-01-19T20:00:00+00:00 — #atlas-obs 2025-01-19 20:00 赵岚: 我在 prod 的值班记录里看到 shard_timeout_seconds=2，和上面讨论的 5 不一致，两份记录都要保留。
  - `ev_93cb168c7aee02879543159c3da16e5c8232852c1158e541f421d7a4edffdec3@8e73fb04b8f30a3b45f77bca15feb53adedcfdf2d25c6694925df577ae2bbf81` source=log event=2025-01-25T17:00:00+00:00 — 日志 log-atlas-01 ERROR query-router 慢查询：shard_timeout_seconds=2，请求失败率上升。事件时间 2025-01-25T17:00:00+00:00。
- `doc_v2`
  - `ev_10c9c0f486b9c9433c4598ca58073e9e56876021d34630b1c9c3c709c1d84745@0923b694a9b7ae2a56de1da0af3af9f4c038b6ca73385ad613e5fc9b30004377` source=document event=2025-01-26T18:00:00+00:00 — 设计文档 doc-atlas-01 v2（已评审）：query-router 将 shard_timeout_seconds 从 2 调整为 5。理由：跨分片查询默认超时过短。评审人 赵岚；该文档是原始证据，不等同于系统确认版本。

### 可接受替代证据（人工判断是否真的等价）

- `chat_conflict`
  - `ev_93cb168c7aee02879543159c3da16e5c8232852c1158e541f421d7a4edffdec3@8e73fb04b8f30a3b45f77bca15feb53adedcfdf2d25c6694925df577ae2bbf81` source=log — 日志 log-atlas-01 ERROR query-router 慢查询：shard_timeout_seconds=2，请求失败率上升。事件时间 2025-01-25T17:00:00+00:00。

### 结构化事实

- [必选] `value_new` 设计文档值 values=['5'] markers=None
- [必选] `value_conflict` 冲突记录值 values=['2'] markers=None
- [可选] `issue_id` 可选关联 Issue values=['issue-ATLAS-1437'] markers=None

### 禁止推断

- 不得出现：以文档为准 / 文档是正确 / 已裁决

### 冲突

```json
[
  {
    "field": "shard_timeout_seconds",
    "values": [
      "5",
      "2"
    ],
    "must_disclose_both": true
  }
]
```

### 拒答/不足

```json
{
  "required": false,
  "expected_termination": [
    "sufficient"
  ],
  "must_disclose": []
}
```

### 必须说明的限制

- 不得自行裁决哪一方正确；必须保留双方记录。

### 人工复核

- [ ] Every required fact is semantically supported by the cited Evidence, not merely present as a string.
- [ ] Every accepted alternative Evidence is genuinely equivalent for this question.
- [ ] No forbidden inference appears, especially proposal-as-confirmed or unilateral conflict adjudication.
- [ ] Each citation resolves to the current content hash and the quoted claim appears in that Evidence.
- [ ] Abstention/limitation text is appropriate for the task and does not hide a wrong answer.
- [ ] Tool-failure and budget tasks disclose interruption rather than claiming completion.
- [ ] Check that required facts are supported by cited Evidence, not merely present as strings.
- [ ] Check that alternative Evidence is genuinely equivalent for this question.
- [ ] Do not mark task_success from deterministic metrics alone.

## v4-insufficient-01 — insufficient (hard)

**问题**：retention-worker 的 retention_days=14 是谁正式批准的？是否已有当前确认版本？


### 必要证据组

- `issue_triage`
  - `ev_e294faff8ce09a06d09af9b86ef492689b47513ff6358d84a42744a57933502a@f4d28cf1e3283879c8d0eab950a1aa4cbb50c9bbdacd4036f6877cabcde5b32e` source=issue event=2025-02-01T23:00:00+00:00 — Issue issue-ATLAS-1474 评审记录：根因是值班记录仍沿用旧保留窗口；建议把 retention_days 调整到 14。评审人 赵岚。

### 结构化事实

- [必选] `no_confirmation` 说明没有正式批准/确认版本 values=None markers=[['未确认', '待审核', '没有正式批准', '尚无确认版本']]
- [可选] `component` 可选组件名称 values=['retention-worker'] markers=None

### 禁止推断

- 不得出现：已正式批准 / 14 已确认 / 当前确认版本是该提案

### 决策状态

```json
{
  "decision_id": "dec_e468a487ffacff99c393ba5c99b8a1ac",
  "known_at": null,
  "expected_current_version_id": null,
  "expected_version_ids": [],
  "pending_proposal_id": "prop_8a21cb2dbabd903570c2c1749bd79404",
  "must_not_claim_confirmed": [
    "prop_8a21cb2dbabd903570c2c1749bd79404"
  ]
}
```

### 拒答/不足

```json
{
  "required": true,
  "expected_termination": [
    "insufficient",
    "conflict",
    "tool_failure"
  ],
  "must_disclose": [
    [
      "未确认",
      "待审核",
      "没有正式批准",
      "尚无确认版本"
    ]
  ]
}
```

### 人工复核

- [ ] Every required fact is semantically supported by the cited Evidence, not merely present as a string.
- [ ] Every accepted alternative Evidence is genuinely equivalent for this question.
- [ ] No forbidden inference appears, especially proposal-as-confirmed or unilateral conflict adjudication.
- [ ] Each citation resolves to the current content hash and the quoted claim appears in that Evidence.
- [ ] Abstention/limitation text is appropriate for the task and does not hide a wrong answer.
- [ ] Tool-failure and budget tasks disclose interruption rather than claiming completion.
- [ ] Check that required facts are supported by cited Evidence, not merely present as strings.
- [ ] Check that alternative Evidence is genuinely equivalent for this question.
- [ ] Do not mark task_success from deterministic metrics alone.

## v4-multi-turn-01 — multi_turn (medium)

**问题**：先确认 retention-worker 的 日志保留策略 当前配置。然后解释为什么后来从旧值改成新值。

**多轮目标**：
1. retention-worker 的 日志保留策略 当前配置是什么？ → ['new_value']
2. 那后来为什么从 7 改成 14？请引用讨论和提交。 → ['reason']


### 必要证据组

- `chat_proposal`
  - `ev_1d0b6770d559920981bf5c6046c85cd519ee29ec6f1f3af132cad3d4d4da561c@702c055919af6d89988ff0d4f99a61c310ff612b9375b7e58680a3247202a0ad` source=chat event=2025-01-28T19:00:00+00:00 — #atlas-obs 2025-01-28 19:00 林澄: 我建议把 retention-worker 的 retention_days 从 7 调到 14；初步判断根因是值班记录仍沿用旧保留窗口。
- `commit_fix`
  - `ev_803704739ca8b242737a95597c21b8ff2d6fb6df90cba61f04a3686c60e1af28@a34f4094eee262d0e74cbaadf7f71f5db8c956c46ae8c42a7cc6964d625caafd` source=git_commit event=2025-02-04T19:00:00+00:00 — commit c7ba02365bb4a02405920baa5d1a190197184b1c
作者 林澄
retention-worker: 将 retention_days 调整为 14（Fixes issue-ATLAS-1474）
事件时间 2025-02-04T19:00:00+00:00
关联 Issue: issue-ATLAS-1474
- `doc_v2`
  - `ev_0da26510bf4792216f4398195d1dc760da58892abbdeb440559871dd2ded4cb7@f0fcf628e1f9cfd39a6c873bdf4ba89500f9fd60c11c5a8165b3819e4084d2aa` source=document event=2025-02-04T18:00:00+00:00 — 设计文档 doc-atlas-02 v2（已评审）：retention-worker 将 retention_days 从 7 调整为 14。理由：值班记录仍沿用旧保留窗口。评审人 赵岚；该文档是原始证据，不等同于系统确认版本。

### 结构化事实

- [必选] `new_value` 改变后的值 values=['14'] markers=None
- [必选] `reason` 改变原因 values=None markers=[['值班记录仍沿用旧']]
- [可选] `commit` 可选修复提交 values=['c7ba02365bb4a02405920baa5d1a190197184b1c', 'c7ba0236'] markers=None

### 拒答/不足

```json
{
  "required": false,
  "expected_termination": [
    "sufficient"
  ],
  "must_disclose": []
}
```

### 人工复核

- [ ] Every required fact is semantically supported by the cited Evidence, not merely present as a string.
- [ ] Every accepted alternative Evidence is genuinely equivalent for this question.
- [ ] No forbidden inference appears, especially proposal-as-confirmed or unilateral conflict adjudication.
- [ ] Each citation resolves to the current content hash and the quoted claim appears in that Evidence.
- [ ] Abstention/limitation text is appropriate for the task and does not hide a wrong answer.
- [ ] Tool-failure and budget tasks disclose interruption rather than claiming completion.
- [ ] Check that required facts are supported by cited Evidence, not merely present as strings.
- [ ] Check that alternative Evidence is genuinely equivalent for this question.
- [ ] Do not mark task_success from deterministic metrics alone.

## v4-tool-failure-01 — tool_failure (hard)

**问题**：query-router 的慢查询文档如何恢复？相关工具不可用时请换来源并说明缺口。

**注入失败**：`{'skill': 'document', 'error': 'timeout'}`

### 必要证据组

- `alternative`
  - `ev_2e1e2c0ab84c2a362bccb1d6cac85be59370c1eba366c864a70d0dd0d315a8f9@33c66744b765dfab03351f15496ae4414a4ba3c1be15e2cbedac8f52314382df` source=issue event=2025-01-23T23:00:00+00:00 — Issue issue-ATLAS-1437 评审记录：根因是跨分片查询默认超时过短；建议把 shard_timeout_seconds 调整到 5。评审人 赵岚。

### 结构化事实

- [必选] `metric_value` 配置值 values=['5'] markers=None
- [必选] `gap` 说明工具失败或证据缺口 values=None markers=[['工具', '失败', '缺口', '不可用']]
- [可选] `issue_id` 可选关联 Issue values=['issue-ATLAS-1437'] markers=None

### 禁止推断

- 不得出现：工具没有失败 / 不存在缺口

### 拒答/不足

```json
{
  "required": false,
  "expected_termination": [
    "sufficient"
  ],
  "must_disclose": []
}
```

### 必须说明的限制

- 必须说明工具失败或缺失来源。

### 人工复核

- [ ] Every required fact is semantically supported by the cited Evidence, not merely present as a string.
- [ ] Every accepted alternative Evidence is genuinely equivalent for this question.
- [ ] No forbidden inference appears, especially proposal-as-confirmed or unilateral conflict adjudication.
- [ ] Each citation resolves to the current content hash and the quoted claim appears in that Evidence.
- [ ] Abstention/limitation text is appropriate for the task and does not hide a wrong answer.
- [ ] Tool-failure and budget tasks disclose interruption rather than claiming completion.
- [ ] Check that required facts are supported by cited Evidence, not merely present as strings.
- [ ] Check that alternative Evidence is genuinely equivalent for this question.
- [ ] Do not mark task_success from deterministic metrics alone.

## v4-tool-failure-02 — tool_failure (hard)

**问题**：rollout-controller 的灰度比例异常故障如何恢复？相关工具不可用时请换来源并说明缺口。

**注入失败**：`{'skill': 'problem', 'error': 'timeout'}`

### 必要证据组

- `alternative`
  - `ev_b4577c6090e8504514a9dbcdb317172bbe9685c606cd7a404fd4abe903574827@b81af0807a11e8d38c2410e29ff3361dbd9e568989d205219d8cc6ec7c1238dc` source=document event=2025-02-22T18:00:00+00:00 — 设计文档 doc-atlas-04 v2（已评审）：rollout-controller 将 canary_percent 从 5 调整为 10。理由：灰度控制器读取了旧配置。评审人 赵岚；该文档是原始证据，不等同于系统确认版本。

### 结构化事实

- [必选] `metric_value` 配置值 values=['10'] markers=None
- [必选] `gap` 说明工具失败或证据缺口 values=None markers=[['工具', '失败', '缺口', '不可用']]
- [可选] `issue_id` 可选关联 Issue values=['issue-ATLAS-1548'] markers=None

### 禁止推断

- 不得出现：工具没有失败 / 不存在缺口

### 拒答/不足

```json
{
  "required": false,
  "expected_termination": [
    "sufficient"
  ],
  "must_disclose": []
}
```

### 必须说明的限制

- 必须说明工具失败或缺失来源。

### 人工复核

- [ ] Every required fact is semantically supported by the cited Evidence, not merely present as a string.
- [ ] Every accepted alternative Evidence is genuinely equivalent for this question.
- [ ] No forbidden inference appears, especially proposal-as-confirmed or unilateral conflict adjudication.
- [ ] Each citation resolves to the current content hash and the quoted claim appears in that Evidence.
- [ ] Abstention/limitation text is appropriate for the task and does not hide a wrong answer.
- [ ] Tool-failure and budget tasks disclose interruption rather than claiming completion.
- [ ] Check that required facts are supported by cited Evidence, not merely present as strings.
- [ ] Check that alternative Evidence is genuinely equivalent for this question.
- [ ] Do not mark task_success from deterministic metrics alone.

## v4-budget-01 — budget (hard)

**问题**：在受限预算下调查 ingest-gateway 的写入放大，说明已完成与未完成部分。

**预算限制**：`{'max_tokens': 1, 'max_model_rounds': 1, 'max_tool_calls': 1}`

### 必要证据组

- `issue_triage`
  - `ev_eae5c627e9e49311d2734bc744988712d6c7893071aecf70fe1cf3e86e2f2419@6bd156d03e9c837d6bcf8fcdea37a79593400ff46b508dc58b82b589fe01350d` source=issue event=2025-01-14T23:00:00+00:00 — Issue issue-ATLAS-1400 评审记录：根因是上游批次过小，固定开销占比过高；建议把 batch_size 调整到 2000。评审人 赵岚。

### 结构化事实

- [必选] `budget_limitation` 说明预算受限与未完成部分 values=None markers=[['预算'], ['未完成', '未确认', '停止', '无法完成']]
- [可选] `component` 可选组件名称 values=['ingest-gateway'] markers=None

### 禁止推断

- 不得出现：已完成全部调查 / 结论完整

### 拒答/不足

```json
{
  "required": false,
  "expected_termination": [
    "sufficient"
  ],
  "must_disclose": []
}
```

### 必须说明的限制

- 预算耗尽时必须明确说明未完成，不得伪造结论。

### 人工复核

- [ ] Every required fact is semantically supported by the cited Evidence, not merely present as a string.
- [ ] Every accepted alternative Evidence is genuinely equivalent for this question.
- [ ] No forbidden inference appears, especially proposal-as-confirmed or unilateral conflict adjudication.
- [ ] Each citation resolves to the current content hash and the quoted claim appears in that Evidence.
- [ ] Abstention/limitation text is appropriate for the task and does not hide a wrong answer.
- [ ] Tool-failure and budget tasks disclose interruption rather than claiming completion.
- [ ] Check that required facts are supported by cited Evidence, not merely present as strings.
- [ ] Check that alternative Evidence is genuinely equivalent for this question.
- [ ] Do not mark task_success from deterministic metrics alone.
