import runpy
import sys
print("Starting bot via app.py wrapper...")
sys.argv = ['bot.py']
runpy.run_path('bot.py', run_name='__main__')
