#!/usr/bin/env bash
# likelion 온프레미스 서버 설치 스크립트 (Ubuntu 22.04/24.04, x86_64·arm64)
#
#   curl -fsSL https://api.likelion.uk/api/v1/onprem-servers/install.sh \
#     | sudo bash -s -- --token <registrationToken> [--api-url <URL>] [--dry-run]
#
# 단계와 API 계약: iris-was docs/onprem-server-registration-contract.md §5·§6
# 등록 토큰·Tailscale 가입 키·SA 토큰·serverSecret 은 출력하지 않는다. set -x 를 쓰지 않는다.
set -euo pipefail

API_URL="https://api.likelion.uk"
TOKEN=""
DRY_RUN=0
TOTAL_STEPS=8
SYSTEM_NAMESPACE="iris-system"

SERVER_KEY=""
TAILSCALE_HOSTNAME=""
TAILSCALE_TAGS=""
TAILNET_FQDN=""
K3S_VERSION=""
ROLLOUTS_VERSION=""
SEALED_SECRETS_VERSION=""
SEALED_SECRETS_CERT=""
BOOTSTRAP_DATA=""
API_DATA=""
WORK_DIR=""

log() { printf '%s\n' "$*"; }
plan() { printf '  (dry-run) %s\n' "$*"; }
step() { printf '\n[%d/%d] %s\n' "$1" "$TOTAL_STEPS" "$2"; }
die() { printf '오류: %s\n' "$*" >&2; exit 1; }
is_dry() { [ "$DRY_RUN" -eq 1 ]; }
kc() { k3s kubectl "$@"; }

usage() {
  cat <<'EOF'
사용법: install.sh --token <registrationToken> [--api-url <URL>] [--dry-run]
  --token     웹·CLI 에서 서버를 등록할 때 받은 등록 토큰 (필수)
  --api-url   likelion API 주소 (기본 https://api.likelion.uk)
  --dry-run   아무것도 바꾸지 않고 실행할 단계만 출력
EOF
}

usage_error() {
  printf '오류: %s\n\n' "$1" >&2
  usage >&2
  exit 2
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --token) [ $# -ge 2 ] || usage_error "--token 값이 필요합니다"; TOKEN=$2; shift 2 ;;
      --token=*) TOKEN=${1#*=}; shift ;;
      --api-url) [ $# -ge 2 ] || usage_error "--api-url 값이 필요합니다"; API_URL=$2; shift 2 ;;
      --api-url=*) API_URL=${1#*=}; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      -h | --help) usage; exit 0 ;;
      # 잘못 넣은 값이 토큰일 수 있어 옵션 이름만 보여 준다.
      -*) usage_error "알 수 없는 옵션입니다: ${1%%=*}" ;;
      *) usage_error "알 수 없는 인자입니다 (값은 표시하지 않습니다)" ;;
    esac
  done
  [ -n "$TOKEN" ] || usage_error "--token 이 필요합니다"
  API_URL=${API_URL%/}
  [[ $API_URL =~ ^https?://[A-Za-z0-9.:-]+$ ]] || usage_error "--api-url 은 http(s)://호스트 형식이어야 합니다"
}

# 조건 함수가 성공할 때까지 5초 간격으로 기다린다.
wait_until() {
  local timeout=$1 description=$2 waited=0
  shift 2
  until "$@"; do
    [ "$waited" -lt "$timeout" ] || die "${description}을(를) ${timeout}초 동안 기다렸지만 준비되지 않았습니다"
    sleep 5
    waited=$((waited + 5))
  done
}

# POST 본문을 stdin 으로 받는다. 성공하면 응답 봉투의 data 를 API_DATA 에 둔다.
# 본문·응답에 비밀이 있어 파일·인자로 넘기지 않는다.
api_post() {
  local path=$1 response status body
  response=$(curl -sS -X POST --max-time 60 --retry 3 --retry-delay 2 \
    -H 'Content-Type: application/json' -H 'Accept: application/json' \
    --data-binary @- -w '\n%{http_code}' "$API_URL$path") ||
    die "API 에 연결하지 못했습니다: $API_URL$path"
  status=${response##*$'\n'}
  body=${response%$'\n'*}
  printf '%s' "$body" | jq -e 'type == "object"' >/dev/null 2>&1 ||
    die "API 응답을 해석하지 못했습니다 (HTTP $status, $path)"
  if [ "$(printf '%s' "$body" | jq -r '.success')" != "true" ]; then
    die "$(printf '%s' "$body" | jq -r --arg status "$status" \
      '"\(.message // "요청이 실패했습니다") (code: \(.code // "UNKNOWN"), HTTP \($status))"')"
  fi
  API_DATA=$(printf '%s' "$body" | jq -c '.data')
}

with_v() { case "$1" in v*) printf '%s' "$1" ;; *) printf 'v%s' "$1" ;; esac; }

check_environment() {
  step 1 "서버 환경 확인·필수 패키지 설치 중"
  if is_dry; then plan "root·Ubuntu 22.04/24.04·x86_64/arm64 확인, curl·jq·ca-certificates 설치"; return; fi
  [ "$(id -u)" -eq 0 ] || die "root 권한이 필요합니다. sudo 로 실행하세요"
  local os_id os_version missing=()
  # shellcheck source=/dev/null
  os_id=$(. /etc/os-release && printf '%s' "${ID:-}")
  # shellcheck source=/dev/null
  os_version=$(. /etc/os-release && printf '%s' "${VERSION_ID:-}")
  case "$os_id:$os_version" in
    ubuntu:22.04 | ubuntu:24.04) ;;
    *) die "Ubuntu 22.04·24.04 만 지원합니다 (현재: ${os_id} ${os_version})" ;;
  esac
  case "$(uname -m)" in
    x86_64 | aarch64) ;;
    *) die "x86_64·arm64 만 지원합니다 (현재: $(uname -m))" ;;
  esac
  command -v curl >/dev/null || missing+=(curl)
  command -v jq >/dev/null || missing+=(jq)
  [ -f /etc/ssl/certs/ca-certificates.crt ] || missing+=(ca-certificates)
  if [ ${#missing[@]} -gt 0 ]; then
    log "패키지 설치: ${missing[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${missing[@]}" >/dev/null
  fi
}

bootstrap() {
  step 2 "등록 토큰 확인 중"
  if is_dry; then
    plan "POST $API_URL/api/v1/onprem-servers/bootstrap (serverKey·Tailscale 가입 키·버전 받기)"
    SERVER_KEY="<serverKey>" TAILSCALE_HOSTNAME="iris-<serverKey>" TAILSCALE_TAGS="tag:iris-onprem"
    K3S_VERSION="<k3s>" ROLLOUTS_VERSION="<argoRollouts>" SEALED_SECRETS_VERSION="<sealedSecrets>"
    return
  fi
  api_post /api/v1/onprem-servers/bootstrap < <(IRIS_TOKEN=$TOKEN jq -n '{registrationToken: env.IRIS_TOKEN}')
  BOOTSTRAP_DATA=$API_DATA
  SERVER_KEY=$(jq -r '.serverKey // ""' <<<"$BOOTSTRAP_DATA")
  TAILSCALE_HOSTNAME=$(jq -r '.tailscale.hostname // ""' <<<"$BOOTSTRAP_DATA")
  TAILSCALE_TAGS=$(jq -r '.tailscale.tags // [] | join(",")' <<<"$BOOTSTRAP_DATA")
  K3S_VERSION=$(jq -r '.versions.k3s // ""' <<<"$BOOTSTRAP_DATA")
  ROLLOUTS_VERSION=$(jq -r '.versions.argoRollouts // ""' <<<"$BOOTSTRAP_DATA")
  SEALED_SECRETS_VERSION=$(jq -r '.versions.sealedSecrets // ""' <<<"$BOOTSTRAP_DATA")
  [[ $SERVER_KEY =~ ^[a-z][a-z0-9]{7}$ ]] || die "bootstrap 응답의 serverKey 형식이 올바르지 않습니다"
  [ "$TAILSCALE_HOSTNAME" = "iris-$SERVER_KEY" ] || die "bootstrap 응답의 Tailscale hostname 이 serverKey 와 맞지 않습니다"
  local version
  for version in "$K3S_VERSION" "$ROLLOUTS_VERSION" "$SEALED_SECRETS_VERSION"; do
    [[ $version =~ ^v?[0-9]+\.[0-9]+\.[0-9]+([+-][A-Za-z0-9.]+)?$ ]] || die "bootstrap 응답의 버전 형식이 올바르지 않습니다"
  done
  log "서버 키: $SERVER_KEY"
}

current_tailnet_fqdn() {
  tailscale status --json 2>/dev/null | jq -r '.Self.DNSName // "" | rtrimstr(".")' || true
}

tailnet_fqdn_ready() { [ -n "$(current_tailnet_fqdn)" ]; }

join_tailnet() {
  step 3 "Tailscale 가입 중"
  if is_dry; then
    plan "Tailscale 설치 → tailscale up --hostname $TAILSCALE_HOSTNAME --advertise-tags $TAILSCALE_TAGS"
    TAILNET_FQDN="$TAILSCALE_HOSTNAME.<tailnet>.ts.net"
    allow_tailnet_in_firewall
    return
  fi
  if ! command -v tailscale >/dev/null; then
    curl -fsSL https://tailscale.com/install.sh | sh
  fi
  systemctl enable --now tailscaled >/dev/null 2>&1
  local state fqdn
  state=$(tailscale status --json 2>/dev/null | jq -r '.BackendState // ""' || true)
  fqdn=$(current_tailnet_fqdn)
  if [ "$state" = "Running" ] && [[ $fqdn == "$TAILSCALE_HOSTNAME".* ]]; then
    log "이미 $TAILSCALE_HOSTNAME 으로 가입되어 있습니다"
  elif [ "$state" = "Running" ]; then
    # 사용자가 쓰던 tailnet 에서 서버를 빼 오지 않는다.
    die "이 서버는 이미 다른 Tailscale 노드(${fqdn%%.*})로 가입되어 있습니다. 'sudo tailscale logout' 후 다시 실행하세요"
  else
    local key_file="$WORK_DIR/tailscale-auth-key"
    (umask 077 && jq -r '.tailscale.authKey // ""' <<<"$BOOTSTRAP_DATA" >"$key_file")
    [ -s "$key_file" ] || die "bootstrap 응답에 Tailscale 가입 키가 없습니다"
    # 가입 키를 명령줄(ps)에 남기지 않게 file: 로 넘긴다.
    # --accept-dns=false: 서버는 들어오는 연결만 받으므로 사용자 서버의 DNS 설정을 바꾸지 않는다.
    tailscale up --reset --auth-key="file:$key_file" --hostname="$TAILSCALE_HOSTNAME" \
      --advertise-tags="$TAILSCALE_TAGS" --accept-dns=false
    rm -f "$key_file"
  fi
  wait_until 60 "Tailscale 주소" tailnet_fqdn_ready
  TAILNET_FQDN=$(current_tailnet_fqdn)
  [[ $TAILNET_FQDN == "$TAILSCALE_HOSTNAME".* ]] ||
    die "Tailscale 주소($TAILNET_FQDN)가 $TAILSCALE_HOSTNAME 으로 시작하지 않습니다. 같은 이름의 기기가 tailnet 에 남아 있는지 확인하세요"
  log "Tailnet 주소: $TAILNET_FQDN"
  allow_tailnet_in_firewall
}

# ufw 가 켜져 있으면 tailnet(tailscale0)으로 들어오는 K3s API(6443)·앱(80)만 연다. 다른 인터페이스는 그대로 둔다.
# 또 K3s 기본 Pod(10.42.0.0/16)·Service(10.43.0.0/16) 대역의 트래픽을 허용한다. ufw 기본 정책이
# FORWARD DROP 이라 막으면 Pod 가 밖으로(DNS 포함) 나가지 못해 ECR 갱신 Job 과 사용자 앱이 깨진다.
K3S_CLUSTER_CIDRS=(10.42.0.0/16 10.43.0.0/16)

allow_tailnet_in_firewall() {
  if is_dry; then
    plan "ufw 가 켜져 있으면 tailscale0 의 tcp 6443·80 만 허용, K3s Pod·Service 대역(${K3S_CLUSTER_CIDRS[*]}) 허용"
    return
  fi
  command -v ufw >/dev/null || return 0
  ufw status 2>/dev/null | grep -q '^Status: active' || return 0
  local port cidr
  # 같은 규칙이 있으면 ufw 가 건너뛴다(다시 실행해도 같다).
  for port in 6443 80; do
    ufw allow in on tailscale0 to any port "$port" proto tcp >/dev/null
  done
  for cidr in "${K3S_CLUSTER_CIDRS[@]}"; do
    ufw allow from "$cidr" to any >/dev/null
    ufw route allow from "$cidr" >/dev/null
  done
  log "방화벽(ufw): tailscale0 의 tcp 6443·80, K3s Pod·Service 대역 허용"
}

node_ready() {
  kc get nodes -o json 2>/dev/null |
    jq -e '(.items | length) > 0 and all(.items[]; any(.status.conditions[]; .type == "Ready" and .status == "True"))' >/dev/null
}

install_k3s() {
  step 4 "K3s 설치 중"
  if is_dry; then plan "K3s $K3S_VERSION 설치 (tls-san $TAILNET_FQDN, Traefik 유지), 노드 Ready 대기"; return; fi
  # 사용자 config.yaml 을 덮어쓰지 않도록 drop-in 에 두고, tls-san 은 기존 값에 덧붙인다(+).
  local config=/etc/rancher/k3s/config.yaml.d/50-iris.yaml desired installed="" config_changed=0
  desired=$(printf 'tls-san+:\n  - "%s"\nwrite-kubeconfig-mode: "0600"' "$TAILNET_FQDN")
  mkdir -p "$(dirname "$config")"
  if [ ! -f "$config" ] || [ "$(cat "$config")" != "$desired" ]; then
    printf '%s\n' "$desired" >"$config"
    config_changed=1
  fi
  command -v k3s >/dev/null && installed=$(k3s --version | awk 'NR == 1 { print $3 }')
  if [ -z "$installed" ]; then
    curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="$K3S_VERSION" sh -s - server
  else
    # 이미 도는 클러스터는 올리거나 내리지 않는다. 설정이 바뀌었으면 재시작만 한다.
    [ "$installed" = "$K3S_VERSION" ] || log "경고: 설치된 K3s($installed)가 권장 버전($K3S_VERSION)과 달라 그대로 둡니다"
    if [ "$config_changed" -eq 1 ] || ! systemctl is-active --quiet k3s; then systemctl restart k3s; fi
  fi
  wait_until 300 "K3s 노드 Ready" node_ready
}

sa_token_ready() {
  [ -n "$(kc -n "$SYSTEM_NAMESPACE" get secret iris-argocd-token -o jsonpath='{.data.token}' 2>/dev/null)" ]
}

grant_deploy_access() {
  step 5 "배포 권한(ServiceAccount) 만드는 중"
  if is_dry; then plan "namespace $SYSTEM_NAMESPACE, SA iris-argocd, ClusterRole iris-onprem-service-deployer, 만료 없는 토큰 Secret"; return; fi
  # ClusterRole 규칙은 iris-infra clusters/onprem-workload/argocd-service-deployer.yaml 과 같아야 한다.
  # 첫 규칙(apiGroups·resources "*" 의 get·list·watch)은 Secret 을 포함한 클러스터 전체 읽기다.
  # 이 SA 토큰을 가진 Argo CD(management)는 iris-system/iris-server-secret 도 읽을 수 있다(ADR 0029 위험).
  kc apply -f - >/dev/null <<'EOF'
apiVersion: v1
kind: Namespace
metadata:
  name: iris-system
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: iris-argocd
  namespace: iris-system
---
apiVersion: v1
kind: Secret
metadata:
  name: iris-argocd-token
  namespace: iris-system
  annotations:
    kubernetes.io/service-account.name: iris-argocd
type: kubernetes.io/service-account-token
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: iris-onprem-service-deployer
rules:
  - apiGroups: ["*"]
    resources: ["*"]
    verbs: [get, list, watch]
  - apiGroups: [""]
    resources: [namespaces]
    verbs: [create, update, patch]
  - apiGroups: [""]
    resources: [services, configmaps]
    verbs: [create, update, patch, delete]
  - apiGroups: [apps]
    resources: [deployments, replicasets]
    verbs: [create, update, patch, delete]
  - apiGroups: [networking.k8s.io]
    resources: [ingresses, networkpolicies]
    verbs: [create, update, patch, delete]
  - apiGroups: [bitnami.com]
    resources: [sealedsecrets]
    verbs: [create, update, patch, delete]
  - apiGroups: [argoproj.io]
    resources: [rollouts]
    verbs: [create, update, patch, delete]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: iris-onprem-service-deployer
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: iris-onprem-service-deployer
subjects:
  - kind: ServiceAccount
    name: iris-argocd
    namespace: iris-system
---
# probe Application 이 iris-system 에 ConfigMap 을 만든다. 이 Role 은 ConfigMap 쓰기만 더한다(ClusterRole
# 이 바뀌어도 probe 가 깨지지 않게 따로 둔다). 읽기는 위 ClusterRole 이 Secret 까지 클러스터 전체에 주므로
# iris-server-secret 을 숨기지 못한다.
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: iris-probe
  namespace: iris-system
rules:
  - apiGroups: [""]
    resources: [configmaps]
    verbs: [get, list, watch, create, update, patch, delete]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: iris-probe
  namespace: iris-system
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: iris-probe
subjects:
  - kind: ServiceAccount
    name: iris-argocd
    namespace: iris-system
EOF
  wait_until 60 "ServiceAccount 토큰" sa_token_ready
}

# 다른 경로(Helm 등)로 이미 설치된 controller 가 있으면 "namespace/이름" 을 출력한다.
# 두 controller 가 같은 리소스를 두고 다투지 않게 하려는 것이다.
find_existing_controller() {
  local image=$1 ours=$2 found
  found=$(kc get deployments -A -o json | jq -r --arg image "$image" --arg ours "$ours" '
    .items[] | select(any(.spec.template.spec.containers[]; .image | contains($image)))
    | "\(.metadata.namespace)/\(.metadata.name)" | select(. != $ours)' | head -n 1)
  [ -n "$found" ] && printf '%s' "$found"
}

read_sealed_secrets_cert() {
  kc get secrets -A -l sealedsecrets.bitnami.com/sealed-secrets-key=active -o json |
    jq -r '.items | sort_by(.metadata.creationTimestamp) | last | .data["tls.crt"] // empty' | base64 -d
}

sealed_secrets_cert_ready() { [ -n "$(read_sealed_secrets_cert 2>/dev/null)" ]; }

install_controllers() {
  step 6 "Argo Rollouts·Sealed Secrets 설치 중"
  if is_dry; then
    plan "Argo Rollouts $ROLLOUTS_VERSION install.yaml → namespace argo-rollouts"
    plan "Sealed Secrets $SEALED_SECRETS_VERSION controller.yaml → namespace kube-system, 공개 인증서 읽기"
    return
  fi
  local existing
  if existing=$(find_existing_controller argoproj/argo-rollouts argo-rollouts/argo-rollouts); then
    log "이미 설치된 Argo Rollouts($existing)를 그대로 씁니다"
  else
    kc create namespace argo-rollouts --dry-run=client -o yaml | kc apply -f - >/dev/null
    # CRD 가 커서 client-side apply 의 last-applied annotation 한도를 넘는다.
    curl -fsSL "https://github.com/argoproj/argo-rollouts/releases/download/$(with_v "$ROLLOUTS_VERSION")/install.yaml" |
      kc apply -n argo-rollouts --server-side --force-conflicts -f - >/dev/null
    kc -n argo-rollouts rollout status deployment/argo-rollouts --timeout=300s
  fi
  if existing=$(find_existing_controller sealed-secrets-controller kube-system/sealed-secrets-controller); then
    log "이미 설치된 Sealed Secrets($existing)를 그대로 씁니다"
  else
    curl -fsSL "https://github.com/bitnami-labs/sealed-secrets/releases/download/$(with_v "$SEALED_SECRETS_VERSION")/controller.yaml" |
      kc apply --server-side --force-conflicts -f - >/dev/null
    kc -n kube-system rollout status deployment/sealed-secrets-controller --timeout=300s
  fi
  wait_until 120 "Sealed Secrets 인증서" sealed_secrets_cert_ready
  SEALED_SECRETS_CERT=$(read_sealed_secrets_cert)
}

connect_server() {
  step 7 "likelion 에 서버 연결 정보 보내는 중"
  if is_dry; then
    plan "POST $API_URL/api/v1/onprem-servers/connect (Tailnet 주소·API CA·SA 토큰·봉인 인증서)"
    plan "serverSecret → Secret $SYSTEM_NAMESPACE/iris-server-secret, API 주소 → ConfigMap $SYSTEM_NAMESPACE/iris-server-config"
    return
  fi
  local ca sa_token server_secret
  ca=$(kc -n "$SYSTEM_NAMESPACE" get secret iris-argocd-token -o jsonpath='{.data.ca\.crt}' | base64 -d)
  [ -n "$ca" ] || ca=$(cat /var/lib/rancher/k3s/server/tls/server-ca.crt)
  sa_token=$(kc -n "$SYSTEM_NAMESPACE" get secret iris-argocd-token -o jsonpath='{.data.token}' | base64 -d)
  [ -n "$sa_token" ] || die "ServiceAccount 토큰을 읽지 못했습니다"
  api_post /api/v1/onprem-servers/connect < <(
    IRIS_TOKEN=$TOKEN IRIS_FQDN=$TAILNET_FQDN IRIS_CA=$ca IRIS_SA_TOKEN=$sa_token IRIS_CERT=$SEALED_SECRETS_CERT \
      jq -n '{registrationToken: env.IRIS_TOKEN, tailnetFqdn: env.IRIS_FQDN, apiCaCert: env.IRIS_CA,
              serviceAccountToken: env.IRIS_SA_TOKEN, sealedSecretsCert: env.IRIS_CERT}'
  )
  server_secret=$(jq -r '.serverSecret // ""' <<<"$API_DATA")
  [ -n "$server_secret" ] || die "connect 응답에 serverSecret 이 없습니다"
  IRIS_SERVER_SECRET=$server_secret jq -n '{apiVersion: "v1", kind: "Secret", type: "Opaque",
      metadata: {name: "iris-server-secret", namespace: "iris-system"},
      stringData: {serverSecret: env.IRIS_SERVER_SECRET}}' |
    kc apply --server-side --force-conflicts --field-manager=iris-install -f - >/dev/null
  jq -n --arg api "$API_URL" --arg key "$SERVER_KEY" --arg fqdn "$TAILNET_FQDN" '{apiVersion: "v1", kind: "ConfigMap",
      metadata: {name: "iris-server-config", namespace: "iris-system"},
      data: {apiUrl: $api, serverKey: $key, tailnetFqdn: $fqdn}}' |
    kc apply --server-side --force-conflicts --field-manager=iris-install -f - >/dev/null
  log "연결 요청을 보냈습니다 (상태: $(jq -r '.status // "REGISTERING"' <<<"$API_DATA"))"
}

has_validating_admission_policy() {
  kc get --raw /apis/admissionregistration.k8s.io/v1 2>/dev/null |
    jq -e 'any(.resources[]; .name == "validatingadmissionpolicies")' >/dev/null
}

install_ecr_refresh() {
  step 8 "이미지 pull 자격증명 갱신 작업 설치 중"
  if is_dry; then plan "CronJob $SYSTEM_NAMESPACE/iris-ecr-refresh (1분마다) 설치 후 1회 실행"; return; fi
  # RBAC 는 namespace 를 패턴으로 좁히지 못한다. 쓰기 범위(svc-*, 이름·타입)는 아래 admission policy 가 막는다.
  kc apply -f - >/dev/null <<'EOF'
apiVersion: v1
kind: ServiceAccount
metadata:
  name: iris-ecr-refresh
  namespace: iris-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: iris-ecr-refresh
rules:
  - apiGroups: [""]
    resources: [namespaces]
    verbs: [get]
  # create 는 요청 시점에 이름을 몰라 resourceNames 로 좁힐 수 없다.
  - apiGroups: [""]
    resources: [secrets]
    verbs: [create]
  - apiGroups: [""]
    resources: [secrets]
    resourceNames: [iris-ecr-pull]
    verbs: [get, update]
  - apiGroups: [""]
    resources: [serviceaccounts]
    resourceNames: [default]
    verbs: [get, patch]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: iris-ecr-refresh
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: iris-ecr-refresh
subjects:
  - kind: ServiceAccount
    name: iris-ecr-refresh
    namespace: iris-system
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: iris-ecr-refresh-script
  namespace: iris-system
data:
  refresh.sh: |
    set -eu -o pipefail
    response=$(printf 'Authorization: Bearer %s\n' "$SERVER_SECRET" |
      curl -sS -X POST --max-time 30 -H @- -H 'Accept: application/json' --data '' \
        "$API_URL/api/v1/onprem-servers/registry-credentials")
    code=$(printf '%s' "$response" | jq -r '.code // ""' 2>/dev/null || true)
    # 연결 확인 전(409)이나 플랫폼의 ECR 설정 전(503)이면 실패한 Job 을 남기지 않고 다음 실행에서 다시 받는다.
    if [ "$code" = "ONPREM_SERVER_NOT_CONNECTED" ]; then echo "서버 연결 확인 전, 다음 실행에서 다시 시도"; exit 0; fi
    if [ "$code" = "NOT_CONFIGURED" ]; then echo "플랫폼의 ECR 자격증명 설정 전, 다음 실행에서 다시 시도"; exit 0; fi
    if [ "$(printf '%s' "$response" | jq -r '.success // false' 2>/dev/null)" != "true" ]; then
      printf '%s' "$response" | jq -r '"자격증명 요청 실패: \(.message // "unknown") (code: \(.code // "UNKNOWN"))"' 2>/dev/null ||
        echo "자격증명 요청 실패: 응답을 해석하지 못했습니다"
      exit 1
    fi
    dockercfg=$(printf '%s' "$response" | jq -c '.data | select(.password != null)
      | {auths: {(.registry): {username, password, auth: ((.username + ":" + .password) | @base64)}}}')
    ids=$(printf '%s' "$response" | jq -r '.data.serviceIds[]? | select(type == "number") | tostring')
    if [ -z "$ids" ] || [ -z "$dockercfg" ]; then echo "갱신할 서비스가 없습니다"; exit 0; fi
    for id in $ids; do
      ns="svc-$id"
      # namespace 는 Argo CD 가 만든다. 없으면 다음 실행에서 처리한다.
      kubectl get namespace "$ns" >/dev/null 2>&1 || { echo "$ns 없음, 건너뜀"; continue; }
      manifest=$(printf '%s' "$dockercfg" | jq -c --arg ns "$ns" '{apiVersion: "v1", kind: "Secret",
        metadata: {name: "iris-ecr-pull", namespace: $ns}, type: "kubernetes.io/dockerconfigjson",
        data: {".dockerconfigjson": (tojson | @base64)}}')
      if kubectl -n "$ns" get secret iris-ecr-pull >/dev/null 2>&1; then
        printf '%s' "$manifest" | kubectl replace -f - >/dev/null
      else
        printf '%s' "$manifest" | kubectl create -f - >/dev/null
      fi
      pulls=$(kubectl -n "$ns" get serviceaccount default -o json 2>/dev/null | jq -c '.imagePullSecrets // []') ||
        { echo "$ns default ServiceAccount 없음, 건너뜀"; continue; }
      if ! printf '%s' "$pulls" | jq -e 'any(.[]; .name == "iris-ecr-pull")' >/dev/null; then
        kubectl -n "$ns" patch serviceaccount default --type=merge \
          -p "$(printf '%s' "$pulls" | jq -c '{imagePullSecrets: (. + [{name: "iris-ecr-pull"}])}')" >/dev/null
      fi
      echo "$ns/iris-ecr-pull 갱신"
    done
---
# 1분마다: 자격증명은 서버에 서비스가 붙고 CONNECTED 가 된 뒤에야 나오고, Argo 가 svc-* namespace 를
# 만든 직후 첫 배포 전에 Secret 이 있어야 한다. 새 서비스의 첫 Pod 는 다음 회차까지 이미지를 받지 못하므로
# 주기가 곧 첫 배포 대기 시간이다(5분 주기에서 운영 E2E 첫 배포가 약 3분 기다렸다). 매번 우리 API 만 부른다.
# 이미지: curl·jq·kubectl 이 모두 든 multi-arch(amd64·arm64) 이미지다. kubectl 은 K3s 와 같은 minor, digest 로 고정한다.
apiVersion: batch/v1
kind: CronJob
metadata:
  name: iris-ecr-refresh
  namespace: iris-system
spec:
  schedule: "* * * * *"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      backoffLimit: 0
      activeDeadlineSeconds: 120
      ttlSecondsAfterFinished: 3600
      template:
        spec:
          serviceAccountName: iris-ecr-refresh
          restartPolicy: Never
          securityContext:
            runAsNonRoot: true
            runAsUser: 65534
            runAsGroup: 65534
          containers:
            - name: refresh
              image: alpine/k8s:1.33.13@sha256:e521cee1f1cb8699bc6e906b4ce6fe50e84addae97f52d5dcf05d3974002ac26
              command: ["/bin/bash", "/scripts/refresh.sh"]
              env:
                - name: HOME
                  value: /tmp
                - name: API_URL
                  valueFrom:
                    configMapKeyRef: {name: iris-server-config, key: apiUrl}
                - name: SERVER_SECRET
                  valueFrom:
                    secretKeyRef: {name: iris-server-secret, key: serverSecret}
              resources:
                requests: {cpu: 10m, memory: 32Mi}
                limits: {memory: 128Mi}
              securityContext:
                allowPrivilegeEscalation: false
                readOnlyRootFilesystem: true
                capabilities: {drop: [ALL]}
              volumeMounts:
                - {name: script, mountPath: /scripts, readOnly: true}
                - {name: tmp, mountPath: /tmp}
          volumes:
            - name: script
              configMap: {name: iris-ecr-refresh-script}
            - name: tmp
              emptyDir: {}
EOF
  if has_validating_admission_policy; then
    kc apply -f - >/dev/null <<'EOF'
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata:
  name: iris-ecr-refresh-scope
spec:
  failurePolicy: Fail
  matchConstraints:
    resourceRules:
      - apiGroups: [""]
        apiVersions: ["v1"]
        operations: [CREATE, UPDATE]
        resources: [secrets, serviceaccounts]
  matchConditions:
    - name: ecr-refresh-only
      expression: request.userInfo.username == 'system:serviceaccount:iris-system:iris-ecr-refresh'
  validations:
    # request.namespace 는 CEL 예약어라 namespaceObject 로 본다.
    - expression: namespaceObject != null && namespaceObject.metadata.name.startsWith('svc-')
      message: iris-ecr-refresh may only write in svc-* namespaces
    - expression: >-
        request.resource.resource != 'secrets' ||
        (object.metadata.name == 'iris-ecr-pull' && object.type == 'kubernetes.io/dockerconfigjson')
      message: iris-ecr-refresh may only write the iris-ecr-pull docker config Secret
    - expression: request.resource.resource != 'serviceaccounts' || object.metadata.name == 'default'
      message: iris-ecr-refresh may only patch the default ServiceAccount
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicyBinding
metadata:
  name: iris-ecr-refresh-scope
spec:
  policyName: iris-ecr-refresh-scope
  validationActions: [Deny]
EOF
  else
    log "경고: 이 K3s 는 ValidatingAdmissionPolicy 를 지원하지 않아 갱신 작업 범위를 RBAC 로만 제한합니다"
  fi
  # 첫 실행은 서버가 아직 CONNECTED 가 아니라 아무것도 하지 않을 수 있다. 1분마다 다시 시도한다.
  kc -n "$SYSTEM_NAMESPACE" create job --from=cronjob/iris-ecr-refresh "iris-ecr-refresh-install-$(date +%s)" >/dev/null
}

finish() {
  printf '\n설치를 마쳤습니다. 서버 키: %s, Tailnet 주소: %s\n' "$SERVER_KEY" "$TAILNET_FQDN"
  log "likelion 이 서버 연결을 확인하는 데 몇 분 걸립니다(최대 15분)."
  log "웹 대시보드나 CLI(likelion servers)에서 연결 상태가 CONNECTED 인지 확인하세요."
  is_dry && log "(dry-run 이라 실제로는 아무것도 바꾸지 않았습니다)"
  return 0
}

# curl | bash 로 받는 도중 끊겨도 일부만 실행되지 않도록 모든 동작을 main 안에 둔다.
main() {
  parse_args "$@"
  WORK_DIR=$(mktemp -d)
  chmod 700 "$WORK_DIR"
  trap 'rm -rf "$WORK_DIR"' EXIT
  log "likelion 온프레미스 서버 설치 (API: $API_URL)"
  check_environment
  bootstrap
  join_tailnet
  install_k3s
  grant_deploy_access
  install_controllers
  connect_server
  install_ecr_refresh
  finish
}

main "$@"
