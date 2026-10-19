#!/bin/bash
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
/usr/bin/xcodebuild -project "$DIR/QQAgentController.xcodeproj" -scheme QQAgentController -configuration Release -derivedDataPath "$DIR/build/dd" build
if [ -d "$DIR/build/dd/Build/Products/Release/QQ Agent.app" ]; then
    rm -rf "$DIR/../bot/QQ Agent.app"
    cp -R "$DIR/build/dd/Build/Products/Release/QQ Agent.app" "$DIR/../bot/QQ Agent.app"
    echo "SUCCESS: App built and copied to bot/QQ Agent.app"
fi
