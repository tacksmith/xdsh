# XDSH 0.1.1-rc.2 RC2 对齐发布基线

这是一份可复现、可回滚的 XDSH RC2 稳定发布基线。当前活动版本与兼容的
DSH 使用同一条 `0.1.1-rc.2` 版本线；后续 DSH 版本必须配套新的 XDSH
发布，不会覆盖这条基线。

## 固定配对

| 组件 | 版本或约束 |
| --- | --- |
| XDSH Python package | `0.1.1rc2` (PEP 440) |
| XDSH release/tag | `xdsh-0.1.1-rc.2` |
| Release channel/order | `stable` / aligned RC2 publication |
| DSH Node runtime | `0.1.1-rc.2` |
| Python SDK protocol client | `deepseek-harness-sdk==0.1.1rc1` |
| Transport | LiteLLM 的 Anthropic Messages-compatible loopback route |
| 新安装模型 | 由用户配置网关中的精确 model ID；不预置任何厂商凭据或套餐 |
| Runtime entrypoint | 官方 `dsh-sdk-jsonrpc-demo/lib/packaged-bin.js` |

完整的官方 runtime 闭包及其版本锁定在
`src/xdsh/ops/dsh_runtime_package.json`。运行时命令必须显式写入环境变量
`XDSH_DSH_RUNTIME_COMMAND`；凭据只通过环境变量名称引用，不进入源码、归档或
镜像构建上下文。

## 复现与验证

维护者应先从当前 checkout 导出干净的 RC2 source bundle，再从该 bundle 重新生成
发布物。直接从开发树运行 `uv build` 不会注入公开发布所需的
`release_profile.json`，不能作为最终归档路径。当前主分支可能包含后续兼容性
修复（例如新的队列别名），因此不能用任意较新的 checkout 期待旧哈希；精确的
`0.1.0a0-rc2` 副本仍保存在仓库中作为不可变历史证据；当前对齐版本的构建不覆盖
它。下面的命令不访问生产服务，也不需要 Telegram 或 provider 凭据：

```bash
XDSH_CHECK_ROOT="$(mktemp -d)"
xdsh-release export --destination "$XDSH_CHECK_ROOT/source"
SOURCE_DATE_EPOCH=1791504000 uv build \
  --out-dir "$XDSH_CHECK_ROOT/dist" "$XDSH_CHECK_ROOT/source"
python scripts/verify_public_artifacts.py "$XDSH_CHECK_ROOT/dist"
python scripts/public_install_matrix.py "$XDSH_CHECK_ROOT/dist"
```

维护者若在包含不可变归档的内部 checkout 中核对 RC2 副本，可运行：

```bash
if [ -d releases/publication-xdsh-0.1.1-rc.2-final ]; then
  (cd releases/publication-xdsh-0.1.1-rc.2-final && shasum -a 256 -c SHA256SUMS)
else
  echo "dated immutable maintainer archive is not part of this public source bundle"
fi
```

公开 source bundle 通过 `xdsh-release export` 生成，并由
`PUBLIC_RELEASE.json` 的逐文件 SHA-256 清单校验。二进制 wheel/sdist 不作为源代码
状态的唯一依据；基线的不可变事实是本文件、runtime manifest 和锁定的项目版本。
为避免后续 alpha 迭代覆盖 RC2，维护者会在公开源码包之外保存独立的可复现发布物、
`SHA256SUMS`、`RELEASE.json` 和旧版不可变证据；公开源码包不包含这些内部归档。
构建完成后用 `shasum -a 256 -c SHA256SUMS` 校验；该校验文件不被打包进
公开 sdist，因此不会形成自引用哈希。

旧的 `0.1.0a0-rc2` 归档是在许可证选择之前生成的不可变历史副本；正式公开发布使用
单独重建的 Apache-2.0 快照，不能覆盖旧归档或复用其哈希。发布物不包含 DSH runtime 或任何凭据；它们只在安装主机的隔离
`~/.xdsh` 中按上表命令配置。

## 升级与回滚边界

未来 DSH 版本必须配套新的 XDSH 版本，并使用单独的 `DSH_HOME`、runtime
command、镜像 tag 和隔离 smoke 根目录。验证失败时，将
`XDSH_DSH_RUNTIME_COMMAND` 恢复为上表的 RC2 entrypoint，并保留原有 XDSH
状态；不会自动重放有副作用的任务。

这条对齐 RC2 基线继续由默认 Dockerfile、WSL installer 和公开安装文档引用。任何候选
版本都必须先通过单独的兼容性矩阵，才能提出切换请求；候选验证本身不会改变这份
基线。
