# 终止与恢复流程

## 运行前

1. 确认 operator 已单独授权付费调用；准备精确 HTTPS origin、provider、model 和定价表。
2. 不读取 `.env`；只在进程环境中设置所选 provider 的 Key 变量名。
3. 两个 backend 使用同一份冻结快照、同一任务列表、同一 retrieval mode 和同一组上限。
4. `hybrid` 索引必须真实构建并校验；任一项目失败即中止，不生成对照结果。

## 运行中

| 条件 | 行为 | 记录 |
|---|---|---|
| HTTP 402 | 立即终止整个 run，不再发起请求 | `stopped_insufficient_balance`, `http_402` |
| HTTP 429/503 | 对当前任务最多重试 `max_retries=1` 次，指数退避 | 每次 attempt 与状态码写入 results |
| `max_model_calls` | 在下一任务前停止，不越过硬上限 | `stopped_model_call_budget` |
| `max_budget_tokens` | 在下一请求前停止 | `stopped_token_budget` / `budget_exhausted` |
| 索引准备失败 | 中止，不写正式 summary | 异常退出，无结果目录 |
| dense/hybrid 回退 | 中止，不把 BM25 结果记为 hybrid | `RetrievalChannelError` |
| 工具注入失败 | 不终止 run；记录模型是否恢复或披露 | `failure_triggered`, trace |

## 恢复

- 只对未完成/失败任务续跑：`--resume <原输出目录>`。
- 续跑要求配置指纹完全一致（含 cap、模型、origin、任务集）。修改 cap 或模型必须新建授权 run。
- 已完成任务的结果行保留，不重复调用。
- 402 中止后，只有 operator 确认账户/额度已恢复才可续跑。
- 429/503 中止后，续跑前应检查 provider 状态；不得通过无限重试绕过限制。
- 重新运行必须写入新目录；已有正式报告和已生成验收材料不被覆盖。

## 本次准备边界

- 本目录生成于 plan `cma-v4-acceptance-v1`。
- 本轮没有读取 Key、没有发起请求、没有产生费用。
- 实际执行需要单独授权，并遵守上表终止规则。
