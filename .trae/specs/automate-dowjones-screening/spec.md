# Dow Jones Name Screening 自动化工作流 Spec

## Why
合规/尽调人员需要对一批公司实体 (entity) 和个人 (person) 在 Dow Jones Risk Center 上做 name screening。手动逐条登录、搜索、保存 PDF 效率低且易遗漏。需要一个自动化工作流，给定一份名单模板即可批量完成「登录 → advanced search → 结果页 PDF → 详情页 PDF」的全流程留痕。

## Scope
本工作流为**全新建设**，不依赖现有代码。最终交付：
1. 一份输入模板文件（供用户填写待筛查名单）
2. 一段可重复运行的自动化脚本/编排，读取模板，批量执行筛查并产出 PDF
3. 一个统一输出目录，承载所有生成的 PDF 与运行汇总

## Implementation Approach
采用 **Playwright (Python)** 实现浏览器自动化与页面 PDF 打印。理由：
- 浏览器原生 `page.pdf()` 可直接生成高质量 PDF（不依赖截图拼接）
- 单一长会话复用登录态，避免每条名单重新登录
- 可稳定批量处理、断点续跑、错误隔离
- 不采用 browser_use subagent 逐条交互，因其对批量 PDF 打印不稳定

凭据通过环境变量或配置文件注入，不硬编码进脚本（用户已提供 ISMO-Support@bocigroup.com / 12345678，实现时写入配置文件 `.dowjones.env`，git 忽略）。

## What Changes
- 新增输入模板 `screening_list.xlsx`（或 CSV），字段：`type`, `name`, `remark`
- 新增 Playwright 自动化脚本 `dowjones_screening.py`，含登录、搜索、PDF 打印、详情页访问、批量循环、汇总
- 新增配置文件 `.dowjones.env`（凭据、输出目录、超时）
- 新增输出目录 `output/`，结构见下方「Output Layout」
- 新增运行汇总 `output/run_summary.csv`（每行结果：name / type / 状态 / 生成 PDF 列表）

## Output Layout
```
output/
├── run_summary.csv                          # 每行名单的处理结果
├── BOC Group/                               # 子目录名 = 模板输入名 (input_name)
│   ├── BOC Group - search result.pdf        # 用 input_name 命名
│   ├── Bank of China Group - 1234567.pdf    # 用 搜索出的结果名 + 该结果 profile id 命名
│   └── BOC Group Ltd - 7654321.pdf          # 多结果时，每个结果用各自 result_name + profile_id
└── John Smith/
    ├── John Smith - search result.pdf
    └── John A Smith - 9876543.pdf
```
- 每个名单一个子目录，子目录名 = 模板输入名 (input_name)
- 搜索结果页 PDF 命名：`{input_name} - search result.pdf`（用模板输入名）
- 详情页 PDF 命名：`{result_name} - {profile_id}.pdf`
  - `{result_name}` 与 `{profile_id}` 都来自**点击进入的那个具体搜索结果**
  - 即：搜索结果列表中某条结果的 name（点击它进入详情页）+ 该结果详情页上的 profile id
  - 多结果时，每个结果用各自的 result_name + profile_id 自然区分，无需额外序号
- 文件名含非法字符（`/ \ : * ? " < > |`）时替换为 `_`

## Impact
- Affected specs: 无（全新工作流）
- Affected code: 新增 `screening_list.xlsx`、`dowjones_screening.py`、`.dowjones.env`、`output/`
- 外部依赖: riskcenter.dowjones.com 页面结构、登录会话、Playwright + Chrome/Chromium

## ADDED Requirements

### Requirement: 输入模板
系统 SHALL 提供一份模板文件，供用户填写待筛查名单。

#### Scenario: 模板结构
- **WHEN** 用户打开 `screening_list.xlsx`
- **THEN** 表头为 `type`, `name`, `remark`
- **AND** `type` 取值限定 `person` / `entity`
- **AND** `name` 为待筛查的完整名称
- **AND** `remark` 为可选备注
- **AND** 模板内含示例行（至少 1 个 entity、1 个 person），便于用户参照填写

#### Scenario: 模板示例行
- **WHEN** 用户查看模板
- **THEN** 看到：
  ```
  type,name,remark
  entity,BOC Group,示例-实体
  person,John Smith,示例-个人
  ```

#### Scenario: 模板读取
- **WHEN** 工作流启动并读取模板
- **THEN** 跳过空行与以 `#` 开头的注释行
- **AND** 校验 `type` 字段合法，非法值报错并跳过该行

### Requirement: Dow Jones 登录自动化
系统 SHALL 使用配置中的凭据自动登录 riskcenter.dowjones.com。

#### Scenario: 成功登录
- **WHEN** 工作流启动并打开 `https://riskcenter.dowjones.com`
- **THEN** 系统从 `.dowjones.env` 读取 user id 与 password
- **AND** 填入 user id `ISMO-Support@bocigroup.com` 与 password `12345678`
- **AND** 提交后等待主页面/搜索页加载
- **AND** 整个批量流程复用同一登录会话，不重复登录

#### Scenario: 登录失败
- **WHEN** 登录失败（凭据失效 / 页面结构变化 / 验证码 / 超时）
- **THEN** 系统立即停止流程
- **AND** 在 `run_summary.csv` 与终端输出失败原因

### Requirement: Advanced Search 自动化
系统 SHALL 在 `https://riskcenter.dowjones.com/search/advanced` 按 searching type 搜索每一行名单。

#### Scenario: Person 搜索
- **WHEN** 模板某行 type=person
- **THEN** 系统导航到 advanced search 页
- **AND** 选择 searching type = person
- **AND** 在 name 字段输入该 person 名称并触发搜索
- **AND** 等待结果列表加载完成（DOM 稳定 / 等待元素出现）

#### Scenario: Entity 搜索
- **WHEN** 模板某行 type=entity
- **THEN** 系统导航到 advanced search 页
- **AND** 选择 searching type = entity
- **AND** 在 name 字段输入该 entity 名称并触发搜索
- **AND** 等待结果列表加载完成

#### Scenario: 每行重新发起搜索
- **WHEN** 进入下一行名单
- **THEN** 系统回到 advanced search 页（或清空搜索框）重新发起搜索，不复用上一行结果

### Requirement: 搜索结果页 PDF 留痕
系统 SHALL 将搜索结果页打印为 PDF。

#### Scenario: 保存结果页 PDF
- **WHEN** 搜索结果页加载完成
- **THEN** 系统使用 `page.pdf()` 打印整页
- **AND** 等待页面渲染稳定后再打印（含懒加载内容）
- **AND** 文件命名为 `{name} - search result.pdf`
- **AND** 保存到 `output/{name}/` 子目录

### Requirement: 详情页条件访问
系统 SHALL 仅在搜索有结果时进入详情页。

#### Scenario: 有结果
- **WHEN** 搜索结果页显示结果列表非空（存在可点击的 name 链接）
- **THEN** 系统依次点击每个结果的 name 进入详情页

#### Scenario: 无结果
- **WHEN** 搜索结果页显示 "No results found" 或结果列表为空
- **THEN** 系统跳过详情页步骤
- **AND** 在 `run_summary.csv` 标记该 name 状态为 `no_result`
- **AND** 继续处理下一行

### Requirement: 详情页 Profile ID 与结果名提取
系统 SHALL 从每个搜索结果的详情页提取该结果自身的 result_name 与 profile_id。

#### Scenario: 提取 result_name
- **WHEN** 用户在搜索结果页点击某个结果的 name 进入详情页
- **THEN** 系统记录被点击的结果 name 作为 `result_name`
- **AND** 若详情页顶部有更完整的正式名称，则以详情页正式名称覆盖 `result_name`

#### Scenario: 提取 profile id
- **WHEN** 详情页加载完成
- **THEN** 系统从详情页 URL (`/riskentities/profiles/{profile_id}`) 提取 profile id
- **AND** 若 URL 不含 id，则回退到详情页页面字段中定位 profile id 标签
- **AND** 该 profile id 必须属于当前点击的那个搜索结果，而非其它结果

### Requirement: 详情页 PDF 留痕
系统 SHALL 将每个搜索结果的详情页打印为 PDF。

#### Scenario: 保存详情页 PDF
- **WHEN** result_name 与 profile id 提取成功
- **THEN** 系统使用 `page.pdf()` 打印整页
- **AND** 文件命名为 `{result_name} - {profile_id}.pdf`
  - `{result_name}` 与 `{profile_id}` 均来自当前点击的那个搜索结果
- **AND** 保存到 `output/{input_name}/` 子目录（子目录用模板输入名）
- **AND** 同一 input_name 有多个搜索结果时，每个结果用各自的 result_name + profile_id 自然产生不同文件名，无需额外序号

### Requirement: 批量编排与汇总
系统 SHALL 依次处理模板中每一行，并生成所有 PDF 到统一输出目录。

#### Scenario: 批量处理
- **WHEN** 工作流启动
- **THEN** 系统读取模板每一行
- **AND** 对每行执行：advanced search → 结果页 PDF → (如有结果) 详情页 PDF
- **AND** 整个流程复用同一登录会话

#### Scenario: 单条失败不阻断整体
- **WHEN** 某一行在搜索或保存 PDF 时出错
- **THEN** 系统捕获异常，在 `run_summary.csv` 标记该行为 `failed` 并附错误信息
- **AND** 继续处理下一行

#### Scenario: 运行汇总
- **WHEN** 全部行处理完成
- **THEN** 系统输出 `output/run_summary.csv`，列：`type`, `name`, `status` (success/no_result/failed), `pdfs` (生成的 PDF 文件名列表), `error`
- **AND** 在终端打印汇总统计（成功 N 条 / 无结果 M 条 / 失败 K 条）
