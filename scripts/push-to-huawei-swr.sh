#!/usr/bin/env bash
#
# ============================================================================
# DeepAudit - 华为云 SWR (Software Repository for Container) 镜像构建与推送脚本
# ============================================================================
# 功能:
#   1. 登录华为云 SWR (默认 swr.cn-north-4.myhuaweicloud.com)
#   2. 构建前端 (deepaudit-frontend) / 后端 (deepaudit-backend) / 沙箱 (deepaudit-sandbox)
#   3. 打上正确的标签并推送到指定的华为云 SWR 组织下
#   4. (--base 开关) 将基础依赖镜像 postgres:15-alpine / redis:7-alpine 以
#      "拉取->标记->推送" 方式(无需构建)同步到同一 SWR 组织, 按需一次性推送即可
#
# 安全说明 (重要):
#   - 登录密钥/密码 **只能** 通过环境变量 HUAWEI_SWR_PASSWORD 传入,
#     本脚本绝不硬编码任何明文凭证。
#   - 登录时使用 `docker login --password-stdin`, 避免密码出现在
#     进程列表 (ps)、命令行参数与 shell 历史中。
#
# 必需环境变量:
#   HUAWEI_SWR_USERNAME  SWR 登录用户名, 格式通常为 <region>@<AK>, 例: cn-north-4@XXXXXXXXXXXX
#   HUAWEI_SWR_PASSWORD  SWR 登录密钥 (控制台"生成临时登录指令"获取, 有效期 24h; 或使用长期密钥)
#   HUAWEI_SWR_ORG       SWR 镜像组织名 (镜像命名空间, 例: deepaudit)
#
# 可选环境变量:
#   HUAWEI_SWR_REGION    区域, 默认 cn-north-4
#   HUAWEI_SWR_REGISTRY  完整仓库地址, 默认 swr.<region>.myhuaweicloud.com
#   IMAGE_TAG            镜像标签, 默认 latest
#   DOCKER_PLATFORM      构建平台, 例 linux/amd64 (默认使用宿主机平台; 华为云服务器一般为 amd64)
#
# 用法示例:
#   export HUAWEI_SWR_USERNAME='cn-north-4@XXXXXXXXXXXX'
#   export HUAWEI_SWR_PASSWORD='********'          # 请勿写入代码/提交到仓库
#   export HUAWEI_SWR_ORG='deepaudit'
#   ./scripts/push-to-huawei-swr.sh                 # 构建并推送全部业务镜像
#   ./scripts/push-to-huawei-swr.sh -t v3.0.4 --backend --frontend
#   ./scripts/push-to-huawei-swr.sh --base          # 仅推送 postgres/redis 基础镜像(一次性)
#   ./scripts/push-to-huawei-swr.sh --no-push       # 仅构建与标记, 不推送
# ============================================================================

set -Eeuo pipefail

# ---------------------------------------------------------------------------
# 颜色与日志
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
    BLUE='\033[0;34m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; BLUE=''; NC=''
fi

print_info()    { echo -e "${GREEN}[INFO]${NC} $*"; }
print_warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
print_error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
print_step()    { echo -e "${BLUE}[STEP]${NC} $*"; }

# 全局错误陷阱: 捕获未被显式处理的命令失败
on_error() {
    local exit_code=$1
    local line_no=$2
    print_error "脚本在第 ${line_no} 行执行失败 (退出码 ${exit_code})。"
    print_error "请检查上方日志定位具体原因。"
    exit "${exit_code}"
}
trap 'on_error $? $LINENO' ERR

# ---------------------------------------------------------------------------
# 定位项目根目录 (脚本位于 <root>/scripts/ 下)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

# ---------------------------------------------------------------------------
# 默认配置 (可被环境变量或命令行参数覆盖)
# ---------------------------------------------------------------------------
# 可选: 自动加载项目根目录 .env, 与 docker-compose.huawei.yml 共用同一份配置
# (凭证仍建议用环境变量注入, 不要写入 .env 提交到仓库)
if [[ -f "${PROJECT_ROOT}/.env" ]]; then
    set -a; . "${PROJECT_ROOT}/.env"; set +a
fi

REGION="${HUAWEI_SWR_REGION:-cn-north-4}"
# 仓库地址: 优先 HUAWEI_SWR_REGISTRY, 回退 compose 使用的 SWR_REGISTRY, 再回退按区域拼接
REGISTRY="${HUAWEI_SWR_REGISTRY:-${SWR_REGISTRY:-swr.${REGION}.myhuaweicloud.com}}"
# 组织名: 兼容脚本变量 HUAWEI_SWR_ORG 与 compose 变量 SWR_ORG
ORG="${HUAWEI_SWR_ORG:-${SWR_ORG:-}}"
USERNAME="${HUAWEI_SWR_USERNAME:-}"
PASSWORD="${HUAWEI_SWR_PASSWORD:-}"
TAG="${IMAGE_TAG:-latest}"
# 默认目标架构 linux/amd64 (华为云 ECS 部署目标); 避免在 arm64 机器上把错误架构的基础镜像推入 SWR
PLATFORM="${DOCKER_PLATFORM:-linux/amd64}"
# 可选代理: 为 buildx builder 注入代理, 加速 buildkitd 拉取基础镜像 (例 http://127.0.0.1:7890)
PROXY="${BUILD_PROXY:-}"
BUILDER=""

# 构建开关
BUILD_FRONTEND=false
BUILD_BACKEND=false
BUILD_SANDBOX=false
PUSH_BASE=false
ANY_SELECTED=false
DO_PUSH=true
NO_CACHE=false

# 基础依赖镜像 (无需构建, 仅 pull->tag->push; 保留各自原始版本标签)
BASE_IMAGES=(
    "postgres:15-alpine"
    "redis:7-alpine"
)

# 失败记录
FAILED_ITEMS=()
PUSHED_IMAGES=()

# ---------------------------------------------------------------------------
# 帮助信息
# ---------------------------------------------------------------------------
usage() {
    # 打印头部注释块 (从第2行起连续的 # 注释行, 遇到首个非注释行停止), 避免魔法行号漂移
    awk 'NR>=2 { if ($0 ~ /^#/) { sub(/^# ?/, ""); print } else exit }' "${BASH_SOURCE[0]}"
    cat <<EOF

选项:
  -t, --tag <tag>        指定镜像标签 (覆盖 IMAGE_TAG, 默认 latest)
      --org <org>        指定 SWR 组织名 (覆盖 HUAWEI_SWR_ORG)
      --registry <url>   指定仓库地址 (覆盖 HUAWEI_SWR_REGISTRY)
      --platform <p>     指定构建平台 (覆盖 DOCKER_PLATFORM, 例 linux/amd64)
      --proxy <url>      为 buildx builder 注入代理, 加速拉取基础镜像 (覆盖 BUILD_PROXY)
      --all              构建全部三个业务镜像 (未指定单项时的默认行为)
      --frontend         仅构建前端镜像 deepaudit-frontend
      --backend          仅构建后端镜像 deepaudit-backend
      --sandbox          仅构建沙箱镜像 deepaudit-sandbox
      --base             仅推送基础依赖镜像 postgres:15-alpine / redis:7-alpine
                         (无需构建, 保留原始版本标签, 按需执行一次即可)
      --no-push          只构建/标记, 不推送到 SWR
      --no-cache         构建时不使用 Docker 层缓存
  -h, --help             显示本帮助并退出
EOF
}

# ---------------------------------------------------------------------------
# 解析命令行参数
# ---------------------------------------------------------------------------
# 校验取值型选项: 值必须存在、非空且不以 '-' 开头 (避免 `-t --backend` 误吞后续选项)
require_value() {
    if [[ -z "${2:-}" || "${2:-}" == -* ]]; then
        print_error "$1 需要一个非空且不以 '-' 开头的参数值 (当前: '${2:-}')"
        exit 1
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -t|--tag)      require_value "$1" "${2:-}"; TAG="$2"; shift 2 ;;
        --org)         require_value "$1" "${2:-}"; ORG="$2"; shift 2 ;;
        --registry)    require_value "$1" "${2:-}"; REGISTRY="$2"; shift 2 ;;
        --platform)    require_value "$1" "${2:-}"; PLATFORM="$2"; shift 2 ;;
        --proxy)       require_value "$1" "${2:-}"; PROXY="$2"; shift 2 ;;
        --all)        ANY_SELECTED=true; BUILD_FRONTEND=true; BUILD_BACKEND=true; BUILD_SANDBOX=true; shift ;;
        --frontend)   ANY_SELECTED=true; BUILD_FRONTEND=true; shift ;;
        --backend)    ANY_SELECTED=true; BUILD_BACKEND=true; shift ;;
        --sandbox)    ANY_SELECTED=true; BUILD_SANDBOX=true; shift ;;
        --base|--base-images) ANY_SELECTED=true; PUSH_BASE=true; shift ;;
        --no-push)    DO_PUSH=false; shift ;;
        --no-cache)   NO_CACHE=true; shift ;;
        -h|--help)    usage; exit 0 ;;
        *)            print_error "未知参数: $1"; echo; usage; exit 1 ;;
    esac
done

# 未显式选择任何镜像时, 默认构建全部
if [[ "${ANY_SELECTED}" == false ]]; then
    BUILD_FRONTEND=true
    BUILD_BACKEND=true
    BUILD_SANDBOX=true
fi

# ---------------------------------------------------------------------------
# 前置检查
# ---------------------------------------------------------------------------
preflight() {
    print_step "执行前置检查..."

    if ! command -v docker >/dev/null 2>&1; then
        print_error "未找到 docker 命令, 请先安装 Docker。"
        exit 1
    fi

    if ! docker info >/dev/null 2>&1; then
        print_error "无法连接 Docker 守护进程, 请确认 Docker 已启动且当前用户有权限 (可能需 sudo 或加入 docker 组)。"
        exit 1
    fi

    # 规范化仓库地址: 去除可能的 scheme 与尾部斜杠 (docker 引用不含 scheme)
    REGISTRY="${REGISTRY#https://}"; REGISTRY="${REGISTRY#http://}"; REGISTRY="${REGISTRY%/}"

    # 校验必需变量 (仅在需要推送时强校验凭证)
    if [[ -z "${ORG}" ]]; then
        print_error "缺少 SWR 组织名。请设置环境变量 HUAWEI_SWR_ORG / SWR_ORG 或使用 --org 指定。"
        exit 1
    fi

    # 格式校验: 尽早失败并给出可读原因, 避免到 docker 阶段才报 invalid reference format
    if [[ ! "${REGISTRY}" =~ ^[A-Za-z0-9.-]+(:[0-9]+)?$ ]]; then
        print_error "非法仓库地址: ${REGISTRY}"; exit 1
    fi
    if [[ ! "${ORG}" =~ ^[a-z0-9][a-z0-9._-]{0,62}$ ]]; then
        print_error "非法 SWR 组织名: ${ORG} (仅允许小写字母/数字/._-, 且以字母或数字开头)"; exit 1
    fi
    if [[ ! "${TAG}" =~ ^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$ ]]; then
        print_error "非法镜像标签: ${TAG}"; exit 1
    fi

    if [[ "${DO_PUSH}" == true ]]; then
        if [[ -z "${USERNAME}" ]]; then
            print_error "缺少登录用户名。请设置环境变量 HUAWEI_SWR_USERNAME (格式: <region>@<AK>)。"
            exit 1
        fi
        if [[ -z "${PASSWORD}" ]]; then
            print_error "缺少登录密钥。请设置环境变量 HUAWEI_SWR_PASSWORD (严禁硬编码到脚本中)。"
            exit 1
        fi
    fi

    # 校验构建上下文目录存在 (仅在需要构建业务镜像时; --base 无需这些目录)
    if [[ "${BUILD_FRONTEND}" == true || "${BUILD_BACKEND}" == true || "${BUILD_SANDBOX}" == true ]]; then
        local dir
        for dir in ./frontend ./backend ./docker/sandbox; do
            if [[ ! -d "${dir}" ]]; then
                print_error "构建上下文目录不存在: ${dir} (请在项目根目录运行本脚本)。"
                exit 1
            fi
        done
    fi

    print_info "Docker 就绪 | 仓库: ${REGISTRY} | 组织: ${ORG} | 标签: ${TAG}"
    if [[ -n "${PLATFORM}" ]]; then
        print_info "构建平台: ${PLATFORM}"
    fi
}

# ---------------------------------------------------------------------------
# 登录华为云 SWR
# ---------------------------------------------------------------------------
login_swr() {
    [[ "${DO_PUSH}" == true ]] || { print_warn "已指定 --no-push, 跳过 SWR 登录。"; return 0; }

    print_step "登录华为云 SWR: ${REGISTRY}"
    # 使用 --password-stdin, 密码不出现在进程列表/命令行中
    if printf '%s' "${PASSWORD}" | docker login --username "${USERNAME}" --password-stdin "${REGISTRY}"; then
        print_info "SWR 登录成功。"
    else
        print_error "SWR 登录失败。请检查用户名 (${USERNAME})、登录密钥是否有效或已过期 (临时密钥有效期 24h)。"
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# 可选: 创建带代理的 buildx builder (docker-container driver)
#   使 buildkitd 拉取基础镜像 (FROM ...) 走代理, 加速慢速源的层下载
# ---------------------------------------------------------------------------
setup_builder() {
    BUILDER=""
    if [[ -z "${PROXY}" ]]; then
        return 0
    fi
    BUILDER="swr-proxy-builder"
    if docker buildx inspect "${BUILDER}" >/dev/null 2>&1; then
        print_info "复用已有代理 builder: ${BUILDER} (proxy=${PROXY})"
        return 0
    fi
    print_step "创建带代理的 buildx builder: ${BUILDER} (proxy=${PROXY})"
    if docker buildx create --name "${BUILDER}" --driver docker-container \
        --driver-opt image=docker.m.daocloud.io/moby/buildkit:latest \
        --driver-opt "env.HTTP_PROXY=${PROXY}" \
        --driver-opt "env.HTTPS_PROXY=${PROXY}" \
        --driver-opt "env.http_proxy=${PROXY}" \
        --driver-opt "env.https_proxy=${PROXY}"; then
        print_info "代理 builder 创建成功, 构建时基础镜像拉取将走代理。"
    else
        print_warn "创建代理 builder 失败, 回退默认 builder (拉取不走代理)。"
        BUILDER=""
    fi
}

# ---------------------------------------------------------------------------
# 构建 -> 推送 单个镜像 (buildx --push, 禁用 attestation)
#   $1 镜像名 (deepaudit-frontend / deepaudit-backend / deepaudit-sandbox)
#   $2 构建上下文
#   $3 Dockerfile 路径
# ---------------------------------------------------------------------------
build_and_push() {
    local name="$1"
    local context="$2"
    local dockerfile="$3"

    local local_image="${name}:${TAG}"
    local swr_image="${REGISTRY}/${ORG}/${name}:${TAG}"

    echo
    print_step "==================== 处理镜像: ${name} ===================="

    if [[ ! -f "${dockerfile}" ]]; then
        print_error "Dockerfile 不存在: ${dockerfile}, 跳过 ${name}。"
        FAILED_ITEMS+=("${name} (缺少 Dockerfile)")
        return 1
    fi

    if [[ "${DO_PUSH}" == true ]]; then
        # ---- 构建并直接推送 (buildx --push) ----
        # --provenance=false --sbom=false: 禁用 attestation, 产出 SWR 可接受的单一 manifest;
        #   否则 SWR 报 "Invalid image, fail to parse 'manifest.json'"
        local bx_args=(buildx build --push --provenance=false --sbom=false -f "${dockerfile}" -t "${swr_image}")
        [[ -n "${PLATFORM}" ]]      && bx_args+=(--platform "${PLATFORM}")
        [[ -n "${BUILDER}" ]]       && bx_args+=(--builder "${BUILDER}")
        [[ "${NO_CACHE}" == true ]] && bx_args+=(--no-cache)
        print_info "[1/1] 构建并推送: ${swr_image}  (context=${context})"
        if ! docker "${bx_args[@]}" "${context}"; then
            print_error "构建/推送 ${name} 失败。请查看上方 buildx 日志。"
            FAILED_ITEMS+=("${name} (buildx push 失败)")
            return 1
        fi
        print_info "推送成功: ${swr_image}"
        PUSHED_IMAGES+=("${swr_image}")
        return 0
    fi

    # ---- --no-push: 仅构建并标记到本地 ----
    local build_args=(-f "${dockerfile}" -t "${local_image}")
    [[ -n "${PLATFORM}" ]]      && build_args+=(--platform "${PLATFORM}")
    [[ "${NO_CACHE}" == true ]] && build_args+=(--no-cache)
    print_info "[1/2] 构建镜像: ${local_image}  (context=${context})"
    if ! docker build "${build_args[@]}" "${context}"; then
        print_error "构建 ${name} 失败。请查看上方 docker build 日志。"
        FAILED_ITEMS+=("${name} (build 失败)")
        return 1
    fi
    print_info "[2/2] 标记镜像: ${local_image} -> ${swr_image}"
    if ! docker tag "${local_image}" "${swr_image}"; then
        print_error "标记 ${name} 失败。"
        FAILED_ITEMS+=("${name} (tag 失败)")
        return 1
    fi
    print_warn "已指定 --no-push, 跳过推送 (镜像已标记为 ${swr_image})。"
    PUSHED_IMAGES+=("${swr_image} (未推送)")
    return 0
}

# ---------------------------------------------------------------------------
# 拉取 -> 标记 -> 推送 基础依赖镜像 (无需构建, 例 postgres/redis)
#   $1 源镜像 (含标签), 例 postgres:15-alpine
#   推送目标沿用相同规律: ${REGISTRY}/${ORG}/<镜像名>:<原始版本标签>
# ---------------------------------------------------------------------------
push_base_image() {
    local src="$1"
    local repo="${src%%:*}"          # postgres
    local src_tag="${src##*:}"       # 15-alpine
    local swr_image="${REGISTRY}/${ORG}/${repo}:${src_tag}"

    echo
    print_step "==================== 处理基础镜像: ${src} ===================="

    # ---- 拉取 ----
    local pull_args=()
    [[ -n "${PLATFORM}" ]] && pull_args+=(--platform "${PLATFORM}")
    pull_args+=("${src}")
    print_info "[1/3] 拉取镜像: ${src}"
    if ! docker pull "${pull_args[@]}"; then
        print_error "拉取 ${src} 失败。请检查网络或镜像名/标签是否正确。"
        FAILED_ITEMS+=("${src} (pull 失败)")
        return 1
    fi

    # ---- 架构校验: 防止把错误架构(如 arm64)的基础镜像以官方版本号推入 SWR, 导致生产机 exec format error ----
    local actual_arch
    actual_arch="$(docker image inspect -f '{{.Os}}/{{.Architecture}}' "${src}" 2>/dev/null || true)"
    if [[ -n "${PLATFORM}" && -n "${actual_arch}" && "${actual_arch}" != "${PLATFORM}" ]]; then
        print_error "${src} 实际架构为 ${actual_arch}, 期望 ${PLATFORM}; 拒绝推送错误架构的基础镜像。请用 --platform 指定正确架构。"
        FAILED_ITEMS+=("${src} (架构不匹配: ${actual_arch} != ${PLATFORM})")
        return 1
    fi
    if [[ -n "${actual_arch}" ]]; then
        print_info "架构校验通过: ${actual_arch}"
    fi

    # ---- 标记 ----
    print_info "[2/3] 标记镜像: ${src} -> ${swr_image}"
    if ! docker tag "${src}" "${swr_image}"; then
        print_error "标记 ${src} 失败。"
        FAILED_ITEMS+=("${src} (tag 失败)")
        return 1
    fi

    # ---- 推送 ----
    if [[ "${DO_PUSH}" != true ]]; then
        print_warn "[3/3] 已指定 --no-push, 跳过推送 (镜像已标记为 ${swr_image})。"
        PUSHED_IMAGES+=("${swr_image} (未推送)")
        return 0
    fi

    print_info "[3/3] 推送镜像: ${swr_image}"
    if ! docker push "${swr_image}"; then
        print_error "推送 ${src} 失败。请确认组织 '${ORG}' 存在且账号有推送权限。"
        FAILED_ITEMS+=("${src} (push 失败)")
        return 1
    fi

    print_info "推送成功: ${swr_image}"
    PUSHED_IMAGES+=("${swr_image}")
    return 0
}

# ---------------------------------------------------------------------------
# 汇总输出
# ---------------------------------------------------------------------------
summary() {
    echo
    print_step "============================ 执行汇总 ============================"
    if [[ ${#PUSHED_IMAGES[@]} -gt 0 ]]; then
        print_info "已处理镜像:"
        local img
        for img in "${PUSHED_IMAGES[@]}"; do
            echo "    - ${img}"
        done
    fi

    if [[ ${#FAILED_ITEMS[@]} -gt 0 ]]; then
        echo
        print_error "以下环节失败:"
        local item
        for item in "${FAILED_ITEMS[@]}"; do
            echo "    - ${item}"
        done
        echo
        print_error "任务未全部完成, 请修复上述问题后重试。"
        exit 1
    fi

    echo
    if [[ "${DO_PUSH}" == true ]]; then
        print_info "✅ 全部选定镜像已成功处理并推送到 ${REGISTRY}/${ORG}。"
    else
        print_info "✅ 全部选定镜像已成功处理并标记 (未推送)。"
    fi
    print_info "部署前请在项目根目录 .env 写入 (docker-compose.huawei.yml 会读取):"
    echo "    SWR_REGISTRY=${REGISTRY}"
    echo "    SWR_ORG=${ORG}"
    if [[ "${TAG}" != "latest" ]]; then
        echo "    IMAGE_TAG=${TAG}    # 必须! 否则 compose 默认拉 :latest 会报 manifest unknown"
    fi
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
main() {
    preflight
    login_swr
    setup_builder

    # 使用 `|| true` 让单个镜像失败不中断整体流程, 失败信息统一在 summary 汇总
    if [[ "${BUILD_FRONTEND}" == true ]]; then
        build_and_push "deepaudit-frontend" "./frontend" "./frontend/Dockerfile" || true
    fi
    if [[ "${BUILD_BACKEND}" == true ]]; then
        build_and_push "deepaudit-backend" "./backend" "./backend/Dockerfile" || true
    fi
    if [[ "${BUILD_SANDBOX}" == true ]]; then
        build_and_push "deepaudit-sandbox" "./docker/sandbox" "./docker/sandbox/Dockerfile" || true
    fi

    # 基础依赖镜像 (postgres/redis): 无需构建, 仅 pull->tag->push, 按需一次性推送
    if [[ "${PUSH_BASE}" == true ]]; then
        local base_src
        for base_src in "${BASE_IMAGES[@]}"; do
            push_base_image "${base_src}" || true
        done
    fi

    summary
}

main "$@"
