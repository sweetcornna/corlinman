# corlinman

自托管 LLM agent 工具箱(gateway + agent 双进程,QQ 等 7 渠道)。uv workspace,28 个 Python 包在 `python/packages/`,前端 `ui/`,接口 `proto/`。完整贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md),PR 路由与 owner 地图见 [docs/pr-standards.md](docs/pr-standards.md),CI 各 job 预期见 [docs/ci-status.md](docs/ci-status.md)。

## 命令

- 测试:`uv run pytest <具体路径>`(不要裸 `pytest`,不要本地跑全量套件——太慢且无必要)
- Lint:`uv run ruff check .`、`uv run mypy python/packages/`、`uv run lint-imports`
- 前端:`pnpm -C ui {test,typecheck,lint}`;UI 产物部署 = `pnpm build` + rsync `ui/out`
- Proto 改动:`bash scripts/gen-proto.sh` 后**必须提交生成的 stubs**(`_generated/` 是被跟踪的,否则 `proto-sync` job 红)

## 已知坑

- **CI `py-test` 会间歇性挂死到 6 小时上限**,`main` 上也会发生,与你的 diff 无关。处理方式:rerun 该 job,不要对着自己的改动 debug。
- CI `gate` 绿 ≠ 全绿:`proto-sync` 与 `swift-mac` 不汇入 `gate`,要单独看。
- 没有 CODEOWNERS——跨 owner 区域的改动要手动请 review(区域地图在 docs/pr-standards.md §7)。
- `.importlinter` 只覆盖 4 包核心分层(`corlinman_server → corlinman_agent → corlinman_providers → corlinman_grpc`),其余包不设防,别以为 lint-imports 绿就没有跨层引用。
- 本地 pre-commit 有 `FAST_COMMIT=1` 逃生口,但 CI 没有——绕过只是推迟失败。
- channels 传给 chat_service 的是 duck-typed SimpleNamespace(非 pydantic):给 `_build_chat_start` 加必填字段会静默杀掉全部渠道,而 web chat 和 CI 保持绿。改契约时必须同步 channels。

## 约定

- 文档写中文,代码注释写英文;Conventional Commits(类型表在 CONTRIBUTING §5)。
- 前端设计语言 = Eclipse Minimal v2:纯黑单色、tint 只染光、全仓禁 `backdrop-filter`、字重 ≤500、图标自绘 sprite(零 lucide),有 vitest 强制。
- `audit/*.md` 是审计工作产物,默认不随功能 PR 提交。
- `skills/` 顶层目录是产品 example 默认 profile 的 skills(不是 dev skills);产品种子 skills 在 `python/packages/corlinman-server/src/corlinman_server/bundled_skills/`,两处同名文件要保持同步。
- 产品运行时工具名是 snake_case wire 名(`read_file`/`run_shell`/`web_search`);skill/persona 里写工具名一律用 wire 名,点号旧名仅由 `tool_aliases.py` 兜底。

## Dev skills

- `/verify`(`.claude/skills/verify`):起隔离 server 验证 gateway 改动,含 `config_path=None` 不落 sidecar 的坑。
