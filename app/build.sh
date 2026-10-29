#!/bin/bash
# 编译并安装 aesmsic.app
#
#   ./build.sh          编译 → 装到 /Applications
#   ./build.sh local    只编译到当前目录，不装
#
# 依赖：Xcode Command Line Tools（swiftc）。
# 不需要 Xcode，不需要签名证书 —— 自签就够了，本机自己用。
#
# ★ 编译时把**仓库根目录**写进 Info.plist 的 AESMSICLabPath。
#   这样 app 装在 /Applications 也知道该去哪个目录找 server.py ——
#   别人 clone 到别处、重新 build 一次，路径自然就对了。
set -e
cd "$(dirname "$0")"

LAB="$(cd .. && pwd)"
APP="aesmsic.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "  compiling…"
swiftc -O -swift-version 5 main.swift \
  -o "$APP/Contents/MacOS/aesmsic" \
  -framework Cocoa -framework WebKit

cp Info.plist "$APP/Contents/Info.plist"
cp icon.icns "$APP/Contents/Resources/icon.icns"

/usr/libexec/PlistBuddy -c "Add :AESMSICLabPath string $LAB" \
  "$APP/Contents/Info.plist" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Set :AESMSICLabPath $LAB" \
     "$APP/Contents/Info.plist"

codesign --force --deep -s - "$APP" 2>/dev/null || true

if [ "$1" = "local" ]; then
  echo "  → ./$APP   (lab: $LAB)"
  exit 0
fi

rm -rf /Applications/aesmsic.app
cp -R "$APP" /Applications/
# 让 Launchpad / Spotlight 立刻认到新版本
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "/Applications/aesmsic.app" >/dev/null 2>&1 || true
echo "  → /Applications/aesmsic.app   (lab: $LAB)"
