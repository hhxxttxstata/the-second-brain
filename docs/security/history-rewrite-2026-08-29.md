# 历史重写：清除个人敏感数据（2026-08-29）

对应 issue [#6](https://github.com/hhxxttxstata/the-second-brain/issues/6)（[security] 重写 git 历史，彻底清除 public 仓库中的个人求职数据）。

## 背景

- 卫生 PR（#4/#5）只让敏感文件"今后不再出现"，但 public 仓库的 git 历史中仍完整可读。
- public 仓库"删除"必须靠重写历史 + 强推实现，全部 commit 哈希改变属预期行为。

## 清除范围（8 个路径 + 3 个晋升用例）

| 路径 | 内容 | 敏感度 |
|---|---|---|
| `秋招投递管理表.xlsx` | 公司/岗位/投递状态/邮件 UID | 高 |
| `make_qiuzhao_xlsx.py` | 硬编码的全部个人信息 | 高 |
| `agent_data/eval/candidate/captured_2026*.json` ×5 | 真实对话捕获（简历优化/笔记整理等） | 中 |
| `agent_data/eval/grader_input.txt` | **真实画像 + 基金持仓代码 + 日记全文摘录** | 高（扫描新发现） |
| `golden/dataset.json` 中 3 个晋升用例 | captured 内容曾被晋升复制（`candidate-captured-20260729132324 / 20260729175856 / 20260802133108`） | 中（内容级清除） |

## 执行方式

1. fresh clone → `git filter-repo --path <x7> --invert-paths`（路径级）
2. `git filter-repo --blob-callback`（内容级，删除 dataset.json 中晋升的 captured 用例）
3. `git filter-repo --path grader_input.txt --invert-paths`
4. 校验：全部路径 `git log --all --oneline` 无输出；`git fsck` 无错误
5. 强推 `bfcf7f3 → c7a23f3 (forced update)`（临时开启 Allow force pushes，完成后恢复）
6. 删除过期分支 `hhxxttxstata-patch-1`（未合并，树+历史含全部敏感文件）

## 验收结果

- [x] 全部敏感路径 `git log --all --oneline` 无输出
- [x] GitHub 网页按 commit 浏览旧历史无法打开敏感文件
- [ ] GitHub Support 工单（清缓存；无 fork）——待提交
- [x] 分支保护规则已恢复（Allow force pushes 重新关闭）

## 保留项（扫描确认）

- `agent_data/eval/2026-07-2x.json`、`2026-08-19.json` 历史快照（纯统计，无对话内容）
- eval 测试用例主题词（"2027秋招offer"、"去北京"、D:/MYWORLD 路径等，无个人细节）
- `tests/` 中"塔塔/子拓"测试 fixture（用户决定保留）

## 后续事项

- 其他机器/备份的 clone 需删除后重新 clone（commit 哈希已全变）
- Support 工单确认后，fork/缓存中的副本由 GitHub 清除
