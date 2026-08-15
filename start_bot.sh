#!/usr/bin/env bash
# 一键启动（Linux / macOS 本机运行）
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
if [ ! -f config.env ]; then
  cp config.example.env config.env
  echo "已生成 config.env，请先填写配置后再运行。"
  exit 1
fi
python3 bot.py
