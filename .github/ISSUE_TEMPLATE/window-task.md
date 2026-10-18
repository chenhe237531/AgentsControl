---
name: Issue（窗口任务）
about: 10 窗口并行开发的标准任务单。一个 issue = 一个可合并的增量。
title: "W??-I?? "
labels: ""
assignees: ""
---

<!--
标题格式：W03-I02 租约与心跳回收
创建后请补两件事：
1) 打上窗口标签 win/w03 与波次标签 wave-2
2) 在 project 里关联 plan/windows.yaml 中对应条目

一个 issue 的规模上限：一个 Agent 窗口 4 小时内能做完并提交。
超过这个规模请拆成两个 issue，否则进度不可追踪，中断后无法续跑。
-->

## 归属

| 项 | 值 |
|---|---|
| 窗口 | W??（`plan/W??.md`） |
| Wave | ?? |
| 前置依赖 | W??-I?? / 无 |
| 阻断谁 | W??-I?? / 无 |

## 目标（一句话）

<!-- "做完之后，什么变了？" 如果这句话写不出来，说明它还不是一个可验收的任务。 -->

## 验收标准

<!-- 必须可验证。禁止出现"体验良好""基本可用"这类无法判定的表述。 -->

1. 
2. 
3. 

## 允许改动的文件

<!-- 从 plan/windows.yaml 的 files 字段抄过来，超出范围会被 CI 阻断 -->

```
backend/app/modules/tasks/
backend/tests/test_tasks.py
```

## 明确不做

<!-- 这一栏比"做什么"更重要：它防止 Agent 顺手扩大范围。 -->

- 

## 风险与坑

<!-- 已知的坑写在这里，省掉接手者半小时 -->

- 

## 完成定义（DoD）

- [ ] 代码 + 测试，`./scripts/pr_precheck.sh --local` 通过
- [ ] 提交消息含 `(#<本 issue 号>)`
- [ ] `plan/W??.md` 中本行状态改为 DONE，并填上 commit sha
- [ ] `.fleet/handoff/W??.md` 已刷新
- [ ] PR 已更新，且 CI `gate` 通过
