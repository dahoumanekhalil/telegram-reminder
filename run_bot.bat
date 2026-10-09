@echo off
title Reminder Bot - do not close (close this window to stop the bot)
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
echo Bot is running. Logs are in bot.log - close this window to stop the bot.
python main.py >> bot.log 2>&1
echo.
echo The bot stopped. Check bot.log for the reason.
pause
