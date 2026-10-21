#!/usr/bin/env bash
# Install and verify a clean XDSH + official DSH pair inside WSL.
#
# The script never reads or modifies a reserved legacy runtime root, never
# accepts credentials as arguments, and only creates files below ~/.xdsh (or a
# dedicated --root).

set -Eeuo pipefail

readonly VALIDATED_XDSH_VERSION="0.1.1-rc.2"
readonly VALIDATED_XDSH_PYTHON_VERSION="0.1.1rc2"
readonly VALIDATED_DSH_VERSION="0.1.1-rc.2"
readonly VALIDATED_NODE_VERSION="22.19.0"
readonly VALIDATED_NODE_DIR="node-v22.19.0-linux-x64"
source_dir="$(pwd -P)"
xdsh_root="${HOME}/.xdsh"
dsh_version="${XDSH_DSH_VERSION:-${VALIDATED_DSH_VERSION}}"
dsh_npm_root=""
npm_cache=""
upgrade_dsh=0
dry_run=0
skip_pip_upgrade=0
xdsh_language=""

usage() {
  cat <<'EOF'
Usage: wsl_install.sh [options]

  --source DIR          XDSH checkout or clean public export (default: cwd)
  --root DIR            dedicated XDSH root (default: ~/.xdsh)
  --dsh-version VER     exact DSH npm version (only 0.1.1-rc.2 is supported)
  --upgrade-dsh         install/refresh the isolated DSH pair after review
  --skip-pip-upgrade    do not upgrade pip in the new virtual environment
  --language CODE       interface/answer language (prompts when omitted on a TTY)
  --dry-run             print bounded actions without changing the filesystem
  -h, --help            show this help

Credentials are never accepted here. After setup, edit ~/.xdsh/config/telegram.env
with your independent Telegram token and loopback LiteLLM key, then run doctor.

Set XDSH_NODE_BIN only when a non-default Linux Node executable must be used.
EOF
}

die() {
  printf 'wsl-install: %s\n' "$*" >&2
  exit 1
}

run() {
  if (( dry_run )); then
    printf '+ '
    printf '%q ' "$@"
    printf '\n'
  else
    "$@"
  fi
}

is_wsl() {
  [[ -n "${WSL_INTEROP:-}" ]] || [[ -r /proc/version && "$(< /proc/version)" =~ [Mm]icrosoft|[Ww][Ss][Ll] ]]
}

validate_version() {
  [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-(rc|alpha|beta)\.[0-9]+)?$ ]] || die "unsafe DSH version: $1"
}

validate_supported_version() {
  [[ "$1" == "$VALIDATED_DSH_VERSION" ]] || die \
    "DSH version $1 is not supported by this stable release; use ${VALIDATED_DSH_VERSION}"
}

validate_xdsh_version() {
  local installed="$1"
  [[ "$installed" == "$VALIDATED_XDSH_PYTHON_VERSION" ]] || die \
    "XDSH package ${installed:-unknown} is not the aligned ${VALIDATED_XDSH_VERSION} release"
}

validate_root() {
  local expanded resolved home_real
  expanded="${1/#\~/$HOME}"
  [[ "$expanded" != *".xclaude"* ]] || die "XDSH root must not overlap a reserved legacy root"
  [[ "$expanded" != *"/.dsh"* ]] || die "XDSH root must not overlap .dsh"
  resolved="$(cd "$(dirname "$expanded")" 2>/dev/null && printf '%s/%s' "$PWD" "$(basename "$expanded")" || printf '%s' "$expanded")"
  home_real="$(cd "$HOME" && pwd -P)"
  [[ "$resolved" != "$home_real" && "$resolved" != "/" ]] || die "root must be a dedicated child directory"
  xdsh_root="$resolved"
}

while (($#)); do
  case "$1" in
    --source)
      (($# >= 2)) || die "--source requires a directory"
      source_dir="$2"
      shift 2
      ;;
    --root)
      (($# >= 2)) || die "--root requires a directory"
      validate_root "$2"
      shift 2
      ;;
    --dsh-version)
      (($# >= 2)) || die "--dsh-version requires a value"
      dsh_version="$2"
      shift 2
      ;;
    --upgrade-dsh)
      upgrade_dsh=1
      shift
      ;;
    --skip-pip-upgrade)
      skip_pip_upgrade=1
      shift
      ;;
    --language)
      (($# >= 2)) || die "--language requires a value"
      xdsh_language="$2"
      shift 2
      ;;
    --dry-run)
      dry_run=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

if ! is_wsl && [[ "${XDSH_ALLOW_NON_WSL:-}" != "1" ]]; then
  die "this installer must run inside WSL (set XDSH_ALLOW_NON_WSL=1 only for a hermetic test)"
fi
source_dir="$(cd "$source_dir" 2>/dev/null && pwd -P)" || die "source directory is unavailable"
[[ -f "$source_dir/pyproject.toml" ]] || die "source directory has no pyproject.toml"
[[ "$source_dir" != *".xclaude"* ]] || die "source path must not contain a reserved legacy root"
validate_root "$xdsh_root"
validate_version "$dsh_version"
validate_supported_version "$dsh_version"
dsh_npm_root="$xdsh_root/dsh-npm"
npm_cache="$xdsh_root/npm-cache"

command -v python3 >/dev/null 2>&1 || die "python3 is required"
requested_node_bin="${XDSH_NODE_BIN:-}"
if [[ -n "$requested_node_bin" ]]; then
  node_bin="$requested_node_bin"
else
  node_bin="$(command -v node 2>/dev/null || true)"
fi
managed_node_bin="$xdsh_root/node-download/$VALIDATED_NODE_DIR/bin/node"
if [[ -z "$node_bin" || "$node_bin" == *.exe ]]; then
  if [[ -z "$requested_node_bin" && -x "$managed_node_bin" ]]; then
    node_bin="$managed_node_bin"
    printf 'Using XDSH-managed Linux Node: %s\n' "$node_bin"
  else
    [[ -n "$node_bin" ]] || die "a Linux node executable is required by the official DSH JSON-RPC runtime"
    die "PATH resolved to a Windows node executable; install Linux Node in WSL first"
  fi
fi
[[ "$node_bin" != *[[:space:]]* ]] || die "node path must not contain whitespace"
node_version="$($node_bin --version 2>/dev/null || true)"
if [[ ! "$node_version" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
  die "could not read a valid Linux Node version from $node_bin"
fi
node_major="${BASH_REMATCH[1]}"
node_minor="${BASH_REMATCH[2]}"
if (( node_major < 22 || (node_major == 22 && node_minor < 19) )); then
  if [[ -z "$requested_node_bin" && -x "$managed_node_bin" && "$node_bin" != "$managed_node_bin" ]]; then
    node_bin="$managed_node_bin"
    node_version="$($node_bin --version 2>/dev/null || true)"
    printf 'Using XDSH-managed Linux Node: %s\n' "$node_bin"
    if [[ ! "$node_version" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
      die "could not read a valid Linux Node version from $node_bin"
    fi
    node_major="${BASH_REMATCH[1]}"
    node_minor="${BASH_REMATCH[2]}"
    if (( node_major < 22 || (node_major == 22 && node_minor < 19) )); then
      die "Node >=${VALIDATED_NODE_VERSION} is required by the selected DSH runtime (found $node_version)"
    fi
  else
    die "Node >=${VALIDATED_NODE_VERSION} is required by the selected DSH runtime (found $node_version)"
  fi
fi
# Prefer npm shipped with the validated Linux Node over a Windows interop
# npm that may appear earlier in a WSL user's inherited PATH.
export PATH="$(dirname "$node_bin"):$PATH"
command -v npm >/dev/null 2>&1 || die "npm is required to install the official DSH release"

# npm's peer-dependency graph for the selected official distribution is
# intentionally complete.  The default Node heap (roughly 2 GiB) can be too small even on a
# modest WSL VM, while blindly requesting a large heap can starve a small VM.
# Reserve a quarter of MemAvailable for the rest of the installer and cap the
# one-shot heap at 6 GiB.  Operators can override NODE_OPTIONS explicitly.
if [[ -z "${NODE_OPTIONS:-}" ]]; then
  available_kib="$(awk '/^MemAvailable:/ {print $2; exit}' /proc/meminfo 2>/dev/null || true)"
  if [[ "$available_kib" =~ ^[0-9]+$ ]] && (( available_kib > 0 )); then
    heap_mib=$((available_kib * 3 / 4 / 1024))
    (( heap_mib < 2048 )) && heap_mib=2048
    (( heap_mib > 6144 )) && heap_mib=6144
  else
    heap_mib=4096
  fi
  export NODE_OPTIONS="--max-old-space-size=${heap_mib}"
fi

# Keep npm's package prefix and cache inside the XDSH root.  This avoids a
# global/system DSH mutation and makes the WSL installation reversible by
# removing only the XDSH-owned root during an explicit teardown.
export NPM_CONFIG_CACHE="$npm_cache"
export NPM_CONFIG_UPDATE_NOTIFIER=false
export NPM_CONFIG_FUND=false
export NPM_CONFIG_AUDIT=false
export NPM_CONFIG_LEGACY_PEER_DEPS="${NPM_CONFIG_LEGACY_PEER_DEPS:-false}"
export PIP_CACHE_DIR="$xdsh_root/pip-cache"
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHONNOUSERSITE=1

printf 'XDSH WSL install · root=%s · source=%s\n' "$xdsh_root" "$source_dir"

# Establish and harden the owner-only root before npm gets a chance to create
# its prefix or cache below it.  Existing roots are tightened in place; a
# foreign-owned root fails closed rather than being used as shared state.
run mkdir -m 700 -p "$xdsh_root"
if (( ! dry_run )); then
  chmod 700 "$xdsh_root" || die "cannot harden XDSH root permissions"
fi

if command -v dsh >/dev/null 2>&1; then
  current_dsh="$(dsh --version 2>/dev/null | head -n 1 || true)"
  printf 'DSH detected: %s\n' "${current_dsh:-version unavailable}"
  if (( ! upgrade_dsh )); then
    current_version="$(printf '%s' "$current_dsh" | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+(-(rc|alpha|beta)\.[0-9]+)?' | head -n 1 || true)"
    if [[ "$current_version" != "$dsh_version" ]]; then
      printf 'Existing system DSH %s differs from %s; leaving it untouched and using the XDSH-local prefix.\n' \
        "${current_version:-unknown}" "$dsh_version"
    else
      printf 'Existing system DSH matches the requested version and is left untouched.\n'
    fi
  else
    printf 'Existing system DSH is left untouched; --upgrade-dsh applies only to the XDSH-local prefix.\n'
  fi
fi

existing_local_dsh=0
local_dsh="$dsh_npm_root/node_modules/.bin/dsh"
runtime_plugins_ready=0
if [[ -f "$dsh_npm_root/node_modules/@deepseek-ai/dsh-sdk-jsonrpc-server/package.json" \
  && -f "$dsh_npm_root/node_modules/@deepseek-ai/dsh-agent-spine-demo/package.json" ]]; then
  runtime_plugins_ready=1
fi
if [[ -x "$local_dsh" ]]; then
  existing_local_dsh=1
  local_dsh_version_text="$("$local_dsh" --version 2>/dev/null | head -n 1 || true)"
  printf 'XDSH-local DSH detected: %s\n' "${local_dsh_version_text:-version unavailable}"
  local_version="$(printf '%s' "$local_dsh_version_text" | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+(-(rc|alpha|beta)\.[0-9]+)?' | head -n 1 || true)"
  if [[ "$local_version" != "$dsh_version" && $upgrade_dsh -eq 0 ]]; then
    die "XDSH-local DSH version ${local_version:-unknown} differs from ${dsh_version}; rerun with --upgrade-dsh after review"
  fi
fi

run mkdir -m 700 -p "$dsh_npm_root"
if (( ! dry_run )); then
  chmod 700 "$dsh_npm_root" || die "cannot harden XDSH-local npm prefix"
fi
if (( ! existing_local_dsh || upgrade_dsh || ! runtime_plugins_ready )); then
  if (( existing_local_dsh && ! runtime_plugins_ready && ! upgrade_dsh )); then
    printf 'DSH runtime plugin closure is incomplete; adding the official packages.\n'
  fi
  run npm install --prefix "$dsh_npm_root" --no-fund --no-audit \
    "@deepseek-ai/dsh@${dsh_version}" \
    "@deepseek-ai/dsh-sdk-jsonrpc-demo@${dsh_version}" \
    "@deepseek-ai/dsh-sdk-jsonrpc-server@${dsh_version}" \
    "@deepseek-ai/dsh-agent-spine-demo@${dsh_version}"
else
  printf 'Skipping npm mutation; XDSH-local DSH already matches the requested version.\n'
fi

if (( ! dry_run )); then
  npm_root="$(npm root --prefix "$dsh_npm_root")"
  rpc_bin="$npm_root/@deepseek-ai/dsh-sdk-jsonrpc-demo/lib/packaged-bin.js"
  if [[ ! -f "$rpc_bin" ]]; then
    run npm install --prefix "$dsh_npm_root" --no-fund --no-audit \
      "@deepseek-ai/dsh-sdk-jsonrpc-demo@${dsh_version}"
  fi
  npm_root="$(npm root --prefix "$dsh_npm_root")"
  rpc_bin="$npm_root/@deepseek-ai/dsh-sdk-jsonrpc-demo/lib/packaged-bin.js"
  [[ -f "$rpc_bin" ]] || die "official JSON-RPC demo binary was not found after DSH install"
  runtime_command_text="$node_bin $rpc_bin"
  local_dsh="$dsh_npm_root/node_modules/.bin/dsh"
  [[ -x "$local_dsh" ]] || die "XDSH-local DSH executable was not found after install"
  installed_local_text="$("$local_dsh" --version 2>/dev/null | head -n 1 || true)"
  installed_local_version="$(printf '%s' "$installed_local_text" | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+(-(rc|alpha|beta)\.[0-9]+)?' | head -n 1 || true)"
  [[ "$installed_local_version" == "$dsh_version" ]] || die \
    "XDSH-local DSH reports ${installed_local_version:-unknown}, expected ${dsh_version}"
else
  rpc_bin="$dsh_npm_root/node_modules/@deepseek-ai/dsh-sdk-jsonrpc-demo/lib/packaged-bin.js"
  runtime_command_text="$node_bin $rpc_bin"
fi

write_dsh_wrapper() {
  local wrapper_dir="$xdsh_root/bin"
  local wrapper="$wrapper_dir/dsh"
  if (( dry_run )); then
    printf '+ create owner-only DSH wrapper %q\n' "$wrapper"
    return
  fi
  run mkdir -m 700 -p "$wrapper_dir"
  chmod 700 "$wrapper_dir" || die "cannot harden DSH wrapper directory"
  if [[ -L "$wrapper" || ( -e "$wrapper" && ! -f "$wrapper" ) ]]; then
    die "refusing non-regular DSH wrapper path: $wrapper"
  fi
  if [[ -f "$wrapper" ]] && ! grep -Fq 'XDSH-managed DSH wrapper' "$wrapper"; then
    die "refusing to overwrite an operator-owned DSH wrapper: $wrapper"
  fi
  local temporary
  temporary="$(mktemp "$wrapper_dir/.dsh-wrapper.XXXXXX")" || die \
    "cannot create a private temporary DSH wrapper"
  umask 077
  {
    printf '%s\n' '#!/usr/bin/env bash' '# XDSH-managed DSH wrapper' 'set -Eeuo pipefail'
    printf 'node_bin=%q\n' "$node_bin"
    printf 'dsh_bin=%q\n' "$local_dsh"
    printf '%s\n' \
      'if [[ "$node_bin" == *.exe || ! -x "$node_bin" || ! -x "$dsh_bin" ]]; then' \
      "  printf '%s\\n' 'xdsh: managed Linux DSH runtime is incomplete; rerun the WSL installer' >&2" \
      '  exit 1' \
      'fi' \
      'export PATH="$(dirname "$node_bin"):${PATH:-}"' \
      'exec "$dsh_bin" "$@"'
  } > "$temporary"
  chmod 700 "$temporary"
  mv -f "$temporary" "$wrapper"
}

write_dsh_wrapper

venv="$xdsh_root/venv"
run python3 -m venv "$venv"
if (( ! skip_pip_upgrade )); then
  run "$venv/bin/python" -m pip install --upgrade pip
fi
# Install the optional channel crypto extra by default: Discord signature
# verification and encrypted Feishu callbacks must work immediately after a
# clean field install, while the core package remains usable without it.
run "$venv/bin/python" -m pip install "${source_dir}[channels]"
if (( dry_run )); then
  installed_xdsh_version="$VALIDATED_XDSH_PYTHON_VERSION"
else
  installed_xdsh_version="$($venv/bin/python -c 'import importlib.metadata as m; print(m.version("xdsh"))' 2>/dev/null || true)"
fi
validate_xdsh_version "$installed_xdsh_version"
setup_args=(
  --root "$xdsh_root"
  --mode both
  --runtime-command "$runtime_command_text"
)
if [[ -n "$xdsh_language" ]]; then
  setup_args+=(--language "$xdsh_language")
fi
run "$venv/bin/xdsh-setup" "${setup_args[@]}"

if (( ! dry_run )); then
  [[ -d "$xdsh_root/config" ]] || die "XDSH setup did not create the config directory"
  chmod 700 "$xdsh_root" "$xdsh_root/config"
  printf '\nSetup complete. No credential was created or read.\n'
  printf '1) cp %s/config/telegram.env.example %s/config/telegram.env\n' "$xdsh_root" "$xdsh_root"
  printf '2) edit the copy and chmod 600 it (Telegram token/user ID + loopback LiteLLM key).\n'
  printf '3) %s/bin/xdsh-doctor --mode telegram --env-file %s/config/telegram.env\n' "$venv" "$xdsh_root"
  printf '4) %s/bin/xdsh-telegram --env-file %s/config/telegram.env\n' "$venv" "$xdsh_root"
  printf 'Linux Node runtime: %s (%s)\n' "$node_bin" "$node_version"
  printf 'Aligned release pair: XDSH %s · DSH %s\n' "$VALIDATED_XDSH_VERSION" "$dsh_version"
  printf 'Isolated DSH executable: %s\n' "$dsh_npm_root/node_modules/.bin/dsh"
  printf 'Convenience DSH wrapper: %s\n' "$xdsh_root/bin/dsh"
  printf 'Optional shell path: export PATH="%s:$PATH"\n' "$xdsh_root/bin"
  printf 'Optional channels: edit %s/config/channels.env and run xdsh-channel-serve per platform.\n' "$xdsh_root"
fi
