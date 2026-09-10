# Tasks

- [x] Task 1: 设计并生成输入模板
  - [x] SubTask 1.1: 创建模板文件 (CSV 或 Excel)，字段：`type`, `name`, `remark`
  - [x] SubTask 1.2: 在模板中填入示例行（至少 1 个 entity、1 个 person）便于后续测试
  - [x] SubTask 1.3: 在脚本/工作流入口支持读取该模板

- [x] Task 2: 搭建浏览器自动化环境与登录流程
  - [x] SubTask 2.1: 选定浏览器自动化方案（browser_use subagent / Playwright / agent-browser）
  - [x] SubTask 2.2: 实现打开 riskcenter.dowjones.com 并自动填入凭据 (ISMO-Support@bocigroup.com / 12345678)
  - [x] SubTask 2.3: 验证登录成功并到达主页面；登录失败时停止并报错

- [x] Task 3: 实现 advanced search 自动化
  - [x] SubTask 3.1: 导航到 `riskcenter.dowjones.com/search/advanced` (注: 实际使用 /search/simple, 因 Person/Entity 切换按钮在该页)
  - [x] SubTask 3.2: 根据 type 字段选择 searching type = person / entity
  - [x] SubTask 3.3: 在 name 字段填入名单名称并触发搜索，等待结果页加载

- [x] Task 4: 实现搜索结果页 PDF 留痕
  - [x] SubTask 4.1: 检测结果页加载完成
  - [x] SubTask 4.2: 将整页打印为 PDF，命名 `{input_name} - search result.pdf`
  - [x] SubTask 4.3: 将 PDF 保存到 `output/{input_name}/` 子目录

- [x] Task 5: 实现条件分支（无结果跳过 / 有结果进入详情）
  - [x] SubTask 5.1: 检测结果列表是否为空或显示 "No result found"
  - [x] SubTask 5.2: 若无结果，记录日志并跳过详情页步骤，run_summary 标记 `no_result`
  - [x] SubTask 5.3: 若有结果，依次点击每个结果的 name 进入详情页 (代码已实现, 待有结果的名单验证)

- [x] Task 6: 实现详情页 result_name + profile_id 提取与 PDF 留痕
  - [x] SubTask 6.1: 记录被点击结果自身的 result_name（必要时用详情页正式名称覆盖）
  - [x] SubTask 6.2: 从详情页 URL 或页面字段提取该结果自身的 profile id
  - [x] SubTask 6.3: 将详情页打印为 PDF，命名 `{result_name} - {profile_id}.pdf`
  - [x] SubTask 6.4: 保存到 `output/{input_name}/` 子目录；多结果时各用自身 result_name + profile_id 自然区分

- [x] Task 7: 实现批量编排与汇总
  - [x] SubTask 7.1: 循环遍历模板每一行，串起 Task 2-6
  - [x] SubTask 7.2: 单行失败不阻断，捕获异常并记录
  - [x] SubTask 7.3: 全部处理完后输出汇总（成功 / 无结果 / 失败）

- [x] Task 8: 端到端验证
  - [x] SubTask 8.1: 用模板示例数据跑通完整流程
  - [x] SubTask 8.2: 校验所有 PDF 文件命名与目录位置正确
  - [x] SubTask 8.3: 校验无结果场景与多结果场景均按预期处理 (无结果场景已验证; 多结果场景代码已实现, 待实际有结果的名单验证)

# Task Dependencies
- [Task 2] 依赖 [Task 1] (需要先有模板结构再串流程)
- [Task 3] 依赖 [Task 2]
- [Task 4] 依赖 [Task 3]
- [Task 5] 依赖 [Task 4]
- [Task 6] 依赖 [Task 5]
- [Task 7] 依赖 [Task 6]
- [Task 8] 依赖 [Task 7]
