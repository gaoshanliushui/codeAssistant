---
name: commit-message
description: 根据暂存区的改动生成规范的 Git 提交信息（Conventional Commits）
---

# 生成提交信息

## 1. 看清改了什么

```bash
git status
git diff --staged
```

改动为空时，先提醒用户 `git add`，不要凭空编提交信息。

## 2. 定类型

| 前缀 | 用于 |
|------|------|
| `feat` | 新功能 |
| `fix` | 缺陷修复 |
| `refactor` | 重构，行为不变 |
| `docs` | 文档 |
| `test` | 测试 |
| `chore` | 构建 / 依赖 / 杂项 |
| `perf` | 性能优化 |

## 3. 写正文

- 首行不超过 72 字符，用祈使句现在时（`add` 而不是 `added`）；
- 首行之后空一行，正文说明**为什么**改，而不是重复改了什么；
- 有破坏性变更时在正文末尾加 `BREAKING CHANGE: …`；
- 用中文写正文即可，类型前缀保持英文。

## 4. 输出

直接给出可以粘贴的提交信息全文（用 ``` 包裹），不要额外解释。
