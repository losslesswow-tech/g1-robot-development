#!/bin/bash
# 从导航容器提取 unitree_sdk2 预编译库到 bridge/unitree_sdk2/，供 docker build 使用。
#
# 这 44 MB 厂商二进制没有入库（见根 README「本仓库未包含」），本脚本是其可执行复原方式。
# 源容器处于 Exited 状态也能提取（docker cp 支持已停止容器）。
#
# 用法:
#   cd bridge && ./fetch_unitree_sdk2.sh [源容器名，默认 robot-navigation]
#
# 前提: 导航镜像里已带 unitree_sdk2（robot-navigation 系列镜像自带）。
set -euo pipefail

SRC="${1:-robot-navigation}"
HERE="$(cd "$(dirname "$0")" && pwd)"
DST="$HERE/unitree_sdk2"

# snap 版 docker 的沙箱拒绝 /tmp 作为 docker cp 目标，必须用 /home 下的绝对路径
case "$DST" in
  /home/*) ;;
  *) echo "错误: 构建目录必须在 /home 下（snap docker 沙箱限制），当前为 $DST" >&2; exit 1 ;;
esac

if ! docker inspect "$SRC" >/dev/null 2>&1; then
  echo "错误: 找不到容器 $SRC。用 'docker ps -a' 确认容器名，或传入正确的源容器。" >&2
  exit 1
fi

echo "源容器: $SRC"
echo "目标  : $DST"
mkdir -p "$DST"

copy() {  # copy <容器内路径> <宿主机相对目标>
  echo "  <- $1"
  docker cp "$SRC:$1" "$DST/$2"
}

echo "[1/3] 头文件"
copy /usr/local/include/unitree  include
copy /usr/local/include/ddsc     include_ddsc
copy /usr/local/include/ddscxx   include_ddscxx
# include_dds（含 ddsrt/、ddsi/）最容易漏，漏了会 dds/features.h 找不到
copy /usr/local/include/dds      include_dds

echo "[2/3] 静态库 / 动态库"
copy /usr/local/lib/libunitree_sdk2.a ./
copy /usr/local/lib/libddsc.so        ./
copy /usr/local/lib/libddscxx.so      ./

echo "[3/3] 校验"
missing=0
for p in include include_ddsc include_ddscxx include_dds \
         libunitree_sdk2.a libddsc.so libddscxx.so; do
  if [ -e "$DST/$p" ]; then
    printf '  OK   %-22s %s\n' "$p" "$(du -sh "$DST/$p" | cut -f1)"
  else
    printf '  缺失 %-22s\n' "$p"; missing=1
  fi
done
[ "$missing" -eq 0 ] || { echo "有文件缺失，检查源容器内路径是否存在。" >&2; exit 1; }

cat <<'EOF'

完成。本目录（bridge/）就是完整的构建上下文：Dockerfile、g1_start.sh、pkg/、
unitree_sdk2/ 齐备，可直接构建：

  cd bridge && docker build -t g1-bridge:1.0 .
EOF
