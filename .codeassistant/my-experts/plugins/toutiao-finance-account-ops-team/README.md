# 头条财经账户运营专家团 (Toutiao Finance Account Ops Team)

> 硬核财经头条号「风中劲草哈」的一站式内容生产团队。

## 团队定位

为头条号「风中劲草哈」硬核财经频道提供从立项、数据、撰稿到审稿的全流程内容生产服务，输出**数据密集 + 表达犀利 + 逻辑闭环**的头条爆款。

## 团队成员

| 花名 | 角色 | 核心职责 |
|------|------|---------|
| 陈统筹 | 投研内容主编（主理人） | 编排调度、终审签发 |
| 林数猎 | 财经数据采集师 | 数据狩猎、可视化、数据源溯源 |
| 齐硬稿 | 财经撰稿人 | 硬核风格撰稿、爆款结构搭建 |
| 严把关 | 内容审核总监 | 事实核查、合规审核、风格校准 |

## 工作流（Team Workflow）

- **Phase 1**：陈统筹选题立项
- **Phase 2**：林数猎数据狩猎
- **Phase 3**：齐硬稿撰稿
- **Phase 4**：严把关四关审核
- **Phase 5**：陈统筹终审签发

## 使用示例

- 给我一个选题（例：「央行降准后 A 股为什么反而跌」），我调度数据、撰稿、审稿三岗协同，把这篇稿子从立项到可发布完整跑通
- 先帮我把最新宏观/行业/公司数据拉出来，附图、附来源、一句话解读
- 帮我严格审这篇稿：事实核查、数据校核、合规审核、风格校准，四道关全跑

## 头像

头像由 ImageGen 自动生成在 `avatars/` 目录下。如需替换为自定义头像，要求：

- 格式：PNG（推荐）或 JPG
- 尺寸：512×512 px
- 大小：单张不超过 500KB
- 文件名：`team.png` / `toutiao-finance-account-ops-team-team-lead.png` / `finance-data-collector.png` / `finance-article-writer.png` / `article-reviewer.png`

## 分类说明

- **categoryId**：`06-ContentCreative`（用户主动指定）
- **规则判断备注**：按"核心能力 = 财经数据分析"严格判断应为 `08-FinanceInvestment`。本专家团按用户主动选择保留 `06-ContentCreative`（产出物为头条文章，自洽），同时在 `displayDescription` 中体现金融专业度。

## 安装与注册

专家包目录：

```
/Users/zhouquan/.workbuddy/plugins/marketplaces/my-experts/plugins/toutiao-finance-account-ops-team/
```

注册命令：

```bash
python3 scripts/register_expert.py <expert-dir>
```

## 打包分享

```bash
python3 scripts/package_expert.py <expert-dir>
```