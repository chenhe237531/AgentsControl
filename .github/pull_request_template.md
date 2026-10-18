## 这个 PR 做了什么

<!-- 一句话说清。读者应该能在 10 秒内判断"要不要认真看"。 -->

Closes #

## 窗口与 issue

| 项 | 值 |
|---|---|
| 窗口 | W?? |
| issue | W??-I?? |
| Wave | ?? |
| 是否含迁移 | 是 / 否 |
| 是否改契约（OpenAPI） | 是 / 否 |

## 提交清单（本 PR 内按顺序）

<!-- 每个 issue 一行，方便 Marshal 按波次挑选合并 -->

- [ ] `feat(#??): xxx` — issue W??-I??
- [ ] `feat(#??): xxx` — issue W??-I??

## 边界自查（必须逐条确认）

- [ ] 只改了本窗口 `allow` 列表内的文件（`./scripts/pr_precheck.sh --local` 通过）
- [ ] 没有改冻结文件；如有，PR 已带 `x-shared-registry` 标签并单独说明
- [ ] 没有直接写其它模块的表，只通过其 service 或只读查询
- [ ] 更新的 `plan/W??.md` 状态表与实际进度一致

## 契约与迁移

- [ ] 无迁移 / 迁移文件名为 `w??_??_<slug>.py`，`alembic heads` 仍为单头
- [ ] 迁移的 `downgrade()` 真的能跑（CI roundtrip 已验证）
- [ ] OpenAPI 变化已在下方列出，并说明对前端/设备端的影响
- [ ] 若改了 `agent/internal/protocol/types.go`，服务端同名模型已同步修改

<details>
<summary>OpenAPI diff（粘贴 <code>python scripts/openapi_snapshot.py --check</code> 输出）</summary>

```
（无变化则写：无）
```

</details>

## 验收标准对照

<!-- 从 plan/W??.md 抄该 issue 的验收项，逐条打勾。没打勾的必须说明原因。 -->

- [ ] ①
- [ ] ②
- [ ] ③

## 测试证据

<!-- 不要写"已测试"。要写具体命令与结果。 -->

```
$ cd backend && pytest -q
16 passed

$ ./scripts/pr_precheck.sh --local
预检结果：0 error, 0 warning
```

## 截图 / 录屏（涉及 UI 时必填）

<!-- Vercel Preview 链接优先；没有则贴本地截图 -->

## 遗留与风险

<!-- 明确写出"这次没做什么"，否则下一窗口会误以为已经做完。 -->

- 未做：
- 已知风险：
- 需要后续 issue 跟进：

---

> 合并由 **Merge Marshal** 执行。申请格式（PR 评论）：
> `@marshal merge-ready wave=N deps=W01-I01,W03-I01`
> 规则见 `docs/MERGE-REVIEW-v1.md` §4。
