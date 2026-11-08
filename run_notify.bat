@echo off
rem Windows タスクスケジューラ用。例: 毎時実行 → 新しい便/状態変化だけ通知
cd /d %~dp0
python changi_nagoya.py --only-new
